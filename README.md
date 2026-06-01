# bosmini-catalog

Live catalog feed for the **BOS·MINI** mobile app
([musa1756/bosmini_app](https://github.com/musa1756/bosmini_app), private).

`catalog.json` is auto-synced from
[bosminiofficial.com](https://bosminiofficial.com) via uCoz uAPI by
[`.github/workflows/sync.yml`](.github/workflows/sync.yml) every 10
minutes. The Flutter app fetches it from
`https://raw.githubusercontent.com/musa1756/bosmini-catalog/main/catalog.json`
at startup (and on pull-to-refresh), with a local cache and a bundled
fallback snapshot for offline cold-start.

## Manual sync

```sh
echo "UCOZ_SITE=bosminiofficial.com"  > .env
echo "UCOZ_TOKEN=sk_live_…"         >> .env
pip install httpx
python sync_catalog.py --out catalog.json
```

`--out` can be omitted for manual repo-local sync; the default output is
`catalog.json`.

`UCOZ_TOKEN` is also stored as a repo secret for the cron workflow.

## WooCommerce mirror (bosminiofficial.ru)

The same `sync.yml` run also mirrors `catalog.json` into the WooCommerce
store on **bosminiofficial.ru** via `sync_to_woo.py`, right after the
catalog commit + Supabase trigger (so a reg.ru hiccup never blocks the
app feed). It's **incremental**: `woo_state.json` tracks a content-hash
of each product's last successfully-pushed payload, so a run only pushes
the products that actually changed and force-deletes the ones that
vanished from the catalog (matched by `sku=slug` — the client's manual
blank-sku products are never touched; `--max-deletes` caps deletions).

Requires two repo secrets (WooCommerce → Settings → Advanced → REST API →
Add key, **Read/Write**); `WOO_SITE=bosminiofficial.ru` is set inline in
the workflow:

```
WOO_CK   ck_…      # consumer key
WOO_CS   cs_…      # consumer secret
```

Until `WOO_CK`/`WOO_CS` exist the WooCommerce steps self-skip, so the
catalog + app sync keep working on their own.

Manual / test runs:

```sh
python sync_to_woo.py --changed-only --prune              # dry-run, show the plan
python sync_to_woo.py --apply --changed-only --limit 5     # write 5 (test batch)
python sync_to_woo.py --apply --changed-only --prune       # full incremental sync
```

## Schema

`catalog.json` payload:

| Field            | Type        | Notes                                  |
|------------------|-------------|----------------------------------------|
| `source`         | string      | `ucoz-uapi`                            |
| `site`           | string      | `bosminiofficial.com`                  |
| `generated_at`   | int (epoch) | unix seconds                           |
| `scraped_count`  | int         | total products                         |
| `products`       | array       | see Flutter `Product.fromJson`         |

Product fields match `lib/shared/data/product.dart` in the app repo
(`slug`, `name`, `price_rub`, `categories`, `local_images`, etc).
