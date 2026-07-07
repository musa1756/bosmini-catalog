#!/usr/bin/env bash
# BOS-MINI catalog sync — runs on the Beget VPS (5.181.108.11, RU IP).
#
# Primary catalog pipeline (GitHub Actions is no longer in the critical path):
#   uCoz uAPI → catalog.json → POST inline to the sync-catalog Edge Function
#   (self-hosted Supabase on this same box) → catalog_products +
#   catalog_snapshot → mobile app / place-order.
# GitHub receives the committed catalog.json afterwards as a best-effort
# replica: history, bundled-asset source, input for the manual-fallback
# Actions workflow and the WooCommerce mirror. GitHub being slow or
# unreachable never blocks the catalog.
#
# Why VPS, not GitHub Actions: uCoz and reg.ru intermittently drop/throttle
# GitHub's datacenter runner IPs (2026-07-07: ~10 h outage, every run stuck in
# "Pull catalog" until timeout). RU→RU is the reliable path, same reasoning as
# sync_to_woo.py living here (see VPS.md).
#
# Install (as root) — see also VPS.md:
#   cd /opt
#   git clone git@github.com:musa1756/bosmini-catalog.git bosmini-catalog-sync
#   cd bosmini-catalog-sync
#   python3 -m venv venv && ./venv/bin/pip install httpx
#   cat >/etc/bos-catalog-sync.env <<'EOF'
#   UCOZ_SITE=bosminiofficial.com
#   UCOZ_TOKEN=...
#   SYNC_SECRET=...           # must equal the Edge Function's SYNC_SECRET
#   TG_BOT_TOKEN=...          # optional; alerts are silent when unset
#   ALERT_CHAT_ID=...
#   EOF
#   chmod 600 /etc/bos-catalog-sync.env
#   ssh-keygen -t ed25519 -N '' -f /root/.ssh/bosmini_catalog_deploy
#   # add /root/.ssh/bosmini_catalog_deploy.pub as a *write* deploy key on
#   # github.com/musa1756/bosmini-catalog, then:
#   ( crontab -l; echo '*/5 * * * * /opt/bosmini-catalog-sync/vps-sync.sh >> /var/log/bos-catalog-sync.log 2>&1' ) | crontab -
set -uo pipefail

ENV_FILE="${ENV_FILE:-/etc/bos-catalog-sync.env}"
# set -a: sync_catalog.py reads UCOZ_SITE/UCOZ_TOKEN from the environment, so
# everything sourced here must be exported, not just shell-local.
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

DIR="${DIR:-/opt/bosmini-catalog-sync}"
FUNCTIONS_URL="${FUNCTIONS_URL:-https://mujafejeji.beget.app/functions/v1}"
STATE_DIR="${STATE_DIR:-/var/lib/bos-catalog-sync}"
FAILS_FILE="$STATE_DIR/fails"
LOCK_FILE="${LOCK_FILE:-/var/lock/bos-catalog-sync.lock}"
THRESH="${THRESH:-3}"
MIN_RATIO="${MIN_RATIO:-0.8}"
# Hard cap on one scrape. A healthy run is minutes; a degraded uAPI must fail
# this run (next cron tick retries) instead of holding the flock for hours.
# Generous on purpose: unlike GitHub's 10-min job timeout, a slow-but-moving
# run is still better than no catalog at all.
SCRAPE_TIMEOUT="${SCRAPE_TIMEOUT:-2700}"
GIT_PUSH="${GIT_PUSH:-1}"
GIT_SSH_KEY="${GIT_SSH_KEY:-/root/.ssh/bosmini_catalog_deploy}"
export GIT_SSH_COMMAND="ssh -i $GIT_SSH_KEY -o BatchMode=yes -o ConnectTimeout=10"

log() { printf '%s %s\n' "$(date -Is)" "$*"; }

send_alert() {
  [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${ALERT_CHAT_ID:-}" ] || return 0
  curl -sS --max-time 10 \
    "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${ALERT_CHAT_ID}" \
    --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# Alert once when the consecutive-failure count crosses THRESH; the matching
# recovery ping is sent from the success path below.
fail() {
  local fails
  fails=$(cat "$FAILS_FILE" 2>/dev/null || echo 0)
  fails=$((fails + 1))
  echo "$fails" > "$FAILS_FILE"
  log "FAIL (${fails} in a row): $1"
  if [ "$fails" -eq "$THRESH" ]; then
    send_alert "🚨 BOS-MINI catalog sync FAILED: $1 (${fails} прогонов подряд). Лог: /var/log/bos-catalog-sync.log на VPS."
  fi
  exit 1
}

# Single instance: a slow uAPI run must not overlap the next cron tick.
exec 9>"$LOCK_FILE"
flock -n 9 || { log "another run holds the lock; skipping"; exit 0; }

mkdir -p "$STATE_DIR" 2>/dev/null || true
cd "$DIR" || { log "missing $DIR"; exit 2; }

[ -n "${UCOZ_TOKEN:-}" ] || fail "UCOZ_TOKEN not set in $ENV_FILE"
[ -n "${SYNC_SECRET:-}" ] || fail "SYNC_SECRET not set in $ENV_FILE"

# 0. Self-update from origin (best-effort — GitHub may be unreachable from a
#    RU IP). Keeps sync_catalog.py/this script fresh and the replica
#    fast-forward. reset --hard only touches tracked files; the fresh catalog
#    is written to an untracked temp name below.
git_ok=0
if [ "$GIT_PUSH" = "1" ]; then
  if timeout 60 git fetch -q origin main 2>/dev/null; then
    git_ok=1
    git reset -q --hard origin/main 2>/dev/null || true
  else
    log "git fetch failed — GitHub unreachable? replica push skipped this run"
  fi
fi

# 1. Pull the catalog from uCoz (RU→RU). Tight timeouts: a healthy run takes
#    ~30 s; a degraded uAPI should fail this run fast and let the next tick
#    retry, not grind for an hour.
#    -u: stream stdout to the temp log unbuffered so a long run can be
#    watched live (tail -f /tmp/bos-catalog-sync.out).
if ! timeout "$SCRAPE_TIMEOUT" ./venv/bin/python -u sync_catalog.py --out catalog.json.new \
    --timeout 30 --max-retries 3 --retry-max-delay 30 \
    >/tmp/bos-catalog-sync.out 2>&1; then
  tail -3 /tmp/bos-catalog-sync.out | while IFS= read -r l; do log "  $l"; done
  fail "sync_catalog.py failed (details: /tmp/bos-catalog-sync.out)"
fi

# 2. Sanity: refuse to publish a collapsed catalog (partial uAPI failure —
#    e.g. one category 5xx'd mid-fetch). Same MIN_RATIO guard the Actions
#    workflow used.
if [ -f catalog.json ]; then
  if ! MIN_RATIO="$MIN_RATIO" ./venv/bin/python - <<'PY'
import json, os, sys
old = json.load(open("catalog.json")).get("scraped_count") or 0
new = json.load(open("catalog.json.new")).get("scraped_count") or 0
ok = old <= 0 or new >= old * float(os.environ["MIN_RATIO"])
print(f"scraped_count: {old} -> {new} ({'ok' if ok else 'COLLAPSED'})")
sys.exit(0 if ok else 1)
PY
  then
    fail "catalog collapsed below ${MIN_RATIO} of previous scraped_count; refusing to publish"
  fi
fi
mv catalog.json.new catalog.json
count=$(./venv/bin/python -c "import json; print(json.load(open('catalog.json'))['scraped_count'])")

# 3. Publish: POST the payload inline to the Edge Function. This is the
#    critical path — the app and place-order read what this call writes.
published=0
for attempt in 1 2 3; do
  resp=$(curl -sS --max-time 90 -X POST "$FUNCTIONS_URL/sync-catalog" \
    -H "Content-Type: application/json" \
    -H "X-Sync-Secret: ${SYNC_SECRET}" \
    --data-binary @catalog.json 2>&1)
  case "$resp" in
    *'"ok":true'*) published=1; break ;;
  esac
  log "publish attempt ${attempt} failed: ${resp:0:300}"
  [ "$attempt" -lt 3 ] && sleep 10
done
[ "$published" = "1" ] || fail "sync-catalog Edge Function did not accept the payload"

# 4. GitHub replica (best-effort; never blocks the catalog).
replica="skipped"
if [ "$GIT_PUSH" = "1" ] && [ "$git_ok" = "1" ]; then
  if [ -n "$(git status --porcelain catalog.json)" ]; then
    if git add catalog.json &&
        git -c user.name="bos-vps-sync" \
            -c user.email="bos-vps-sync@users.noreply.github.com" \
            commit -q -m "Auto-sync catalog (${count} products)" &&
        timeout 60 git push -q origin HEAD:main 2>/dev/null; then
      replica="pushed"
    else
      replica="push-failed"
      log "replica push failed (non-fatal; next run retries after fetch/reset)"
    fi
  else
    replica="unchanged"
  fi
fi

# 5. Success bookkeeping + recovery alert paired with fail()'s crossing alert.
prev=$(cat "$FAILS_FILE" 2>/dev/null || echo 0)
echo 0 > "$FAILS_FILE"
if [ "$prev" -ge "$THRESH" ]; then
  send_alert "✅ BOS-MINI catalog sync восстановился: опубликовано ${count} товаров."
fi
log "ok: published ${count} products (github replica: ${replica})"
