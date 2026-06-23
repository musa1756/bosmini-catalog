# .ru WooCommerce mirror — runs on the Beget VPS

`catalog.json` is mirrored into the **bosminiofficial.ru** WooCommerce store
by `sync_to_woo.py`. This **cannot** run from GitHub Actions: reg.ru drops TCP
connections from GitHub's datacenter runner IPs (`httpx.ConnectTimeout`). From
a Russian IP the store answers in ~2 s, so the push runs on the **Beget VPS**
(`5.181.108.11`) on its own cron.

## What runs where

- **GitHub Actions** (`.github/workflows/sync.yml`): `bosminiofficial.com`
  (uCoz uAPI) → `catalog.json`, committed to `main`, every 5 min.
- **Beget VPS** cron: pulls the committed `catalog.json` from
  `raw.githubusercontent.com` and pushes it into WooCommerce, every 5 min,
  offset to `:03,:08,...` so it reads the catalog after GitHub has committed it.

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
curl -fsSL --max-time 30 \
  https://raw.githubusercontent.com/musa1756/bosmini-catalog/main/catalog.json \
  -o catalog.json
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
