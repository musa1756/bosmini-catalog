# bosmini-catalog

Live catalog feed for the **BOS·MINI** mobile app
([musa1756/bosmini_app](https://github.com/musa1756/bosmini_app), private).

`catalog.json` is auto-synced from
[bosminiofficial.com](https://bosminiofficial.com) via uCoz uAPI by
[`.github/workflows/sync.yml`](.github/workflows/sync.yml) every 5
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

`catalog.json` is also mirrored into the **bosminiofficial.ru** WooCommerce
store by `sync_to_woo.py`, **incrementally** (only changed products are
pushed; products that vanished from the catalog are pruned). This does
**not** run in GitHub Actions — reg.ru blocks GitHub's runner IPs — it runs
from the Beget VPS (`5.181.108.11`) on its own cron. See **[VPS.md](VPS.md)** for the full
deployment.

Manual / test runs (from a machine that can reach reg.ru, e.g. a Russian IP):

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
