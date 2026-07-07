# Catalog sync + .ru WooCommerce mirror — runs on the Beget VPS

Both pipelines live on the **Beget VPS** (`5.181.108.11`) because RU services
(uCoz, reg.ru) intermittently drop/throttle GitHub's datacenter runner IPs —
2026-07-07 uCoz stopped answering GitHub runners entirely for ~10 h while
answering RU IPs in ~1 s. From a Russian IP everything responds fast, so the
VPS is the reliable place to run.

## What runs where

- **Beget VPS cron — catalog sync** (`/opt/bosmini-catalog-sync/vps-sync.sh`,
  every 5 min): `bosminiofficial.com` (uCoz uAPI) → `catalog.json` → sanity
  check → POST inline to the `sync-catalog` Edge Function (self-hosted
  Supabase on the same box) → `catalog_products` + `catalog_snapshot` → app.
  Afterwards it commits `catalog.json` to this repo as a best-effort replica
  (history, bundled-asset source, WooCommerce input). Install instructions are
  in the header of `vps-sync.sh`; state lives in `/var/lib/bos-catalog-sync/`,
  log in `/var/log/bos-catalog-sync.log`, Telegram alert after 3 consecutive
  failures + recovery ping.
- **GitHub Actions** (`.github/workflows/sync.yml`): **manual fallback only**
  (`workflow_dispatch`, no schedule). Use it when the VPS is down and GitHub
  can reach uCoz. The old external dispatcher
  (`bos-catalog-dispatch.sh` + its crontab line) is retired.
- **GitHub Actions — watchdog** (`.github/workflows/watchdog.yml`, every 20
  min): a dead man's switch for the VPS cron. `vps-sync.sh`'s own Telegram
  alert only fires when the script *runs and fails* — if the box dies, the
  crontab gets wiped, or the disk fills up, nothing runs there to raise that
  alarm and `catalog.json` quietly goes stale. This workflow instead checks
  `generated_at` on the committed `catalog.json` and pages Telegram if it's
  older than 60 min (VPS cron is every 5 min; one degraded scrape can
  legitimately take up to `SCRAPE_TIMEOUT`=45 min), with the same
  alert-once / recover-once hysteresis as `vps-sync.sh`'s own alerting
  (tracked in the `WATCHDOG_ALERTED` repo variable). Reuses the same
  `TG_BOT_TOKEN`/`TG_CHAT_ID` repo secrets as `sync.yml` — no extra setup if
  those are already configured. Test the alert path with
  `gh workflow run watchdog.yml -f force_stale=true`.
  For faster detection (minutes, not up to 60), optionally also set
  `HC_PING_URL` in `/etc/bos-catalog-sync.env` to a
  [healthchecks.io](https://healthchecks.io) check URL (or similar); the
  script pings it at the end of every successful run.
- **Beget VPS cron — Woo mirror**: copies the local
  `/opt/bosmini-catalog-sync/catalog.json` (falls back to
  `raw.githubusercontent.com` if missing) and pushes it into WooCommerce,
  every 5 min, offset to `:03,:08,...` so it reads the catalog after the sync
  has finished.

## Layout on the VPS — `/opt/bosmini-woo-sync/`

```
sync_to_woo.py     # pulled from raw.githubusercontent (this repo)
woo_state.json     # slug -> content-hash, the incremental state (VPS-owned)
.env               # WOO_SITE / WOO_CK / WOO_CS  (chmod 600, real Read/Write keys)
run.sh             # fetch catalog.json, then sync_to_woo.py --apply --changed-only --prune
venv/              # python3 venv with httpx
sync.log           # cron output
```

`run.sh`:

```sh
#!/usr/bin/env bash
set -euo pipefail
cd /opt/bosmini-woo-sync
# Prefer the locally built catalog (no GitHub in the path); fall back to the
# committed copy on raw.githubusercontent if the local sync dir is missing.
if [ -s /opt/bosmini-catalog-sync/catalog.json ]; then
  cp /opt/bosmini-catalog-sync/catalog.json catalog.json
else
  curl -fsSL --max-time 30 \
    https://raw.githubusercontent.com/musa1756/bosmini-catalog/main/catalog.json \
    -o catalog.json
fi
exec ./venv/bin/python sync_to_woo.py \
  --apply --changed-only --prune --catalog catalog.json --timeout 60
```

crontab (`crontab -e` as root):

```
3,8,13,18,23,28,33,38,43,48,53,58 * * * * /opt/bosmini-woo-sync/run.sh >> /opt/bosmini-woo-sync/sync.log 2>&1
```

## Re-provisioning from scratch

```sh
ssh root@5.181.108.11
apt-get install -y python3.12-venv
mkdir -p /opt/bosmini-woo-sync && cd /opt/bosmini-woo-sync
python3 -m venv venv && ./venv/bin/pip install httpx
curl -fsSL https://raw.githubusercontent.com/musa1756/bosmini-catalog/main/sync_to_woo.py -o sync_to_woo.py
printf 'WOO_SITE=bosminiofficial.ru\nWOO_CK=ck_…\nWOO_CS=cs_…\n' > .env && chmod 600 .env
# write run.sh (above), chmod +x, install the crontab line (above)
# first run with no woo_state.json safely does a one-time full update (idempotent)
```

## Operating notes

- **`--prune` force-deletes** products that vanished from `catalog.json`
  (matched by `sku=slug`; the client's manual blank-sku products are never
  touched). `--max-deletes 30` aborts the run instead of deleting more — a
  partial uAPI fetch can't wipe the store. The storefront tolerates deletions
  because of the Code Snippets ACF filter that backfills theme blocks; if that
  protection is ever removed, switch `run.sh` away from `--prune`.
- **Check it's alive:** `tail -n 40 /opt/bosmini-woo-sync/sync.log`
- **Dry-run the plan:** `cd /opt/bosmini-woo-sync && ./venv/bin/python sync_to_woo.py --changed-only --prune --catalog catalog.json`
- `sync.log` grows ~slowly; add a logrotate rule if it ever matters.
