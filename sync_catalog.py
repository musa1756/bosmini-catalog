"""
Sync full catalog from uCoz uAPI into Flutter-compatible assets/catalog.json.

Iterates all categories (page=categories) and paginates page=category&cat_id=N
to collect every product, dedupes by entry_id, then writes the JSON in the
schema Product.fromJson (lib/shared/data/product.dart) expects.

Usage:
    python sync_catalog.py --out catalog.json
    python sync_catalog.py --dry-run --out catalog.json
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

# Manager-side artifacts that creep into uCoz product titles:
#   * leading "/////" / "***" — hack to pin items to the top of category lists
#   * ",,9''" / ",,10\"" — German-style low quote used as opening inch-mark,
#     usually paired with double apostrophe as the closing one
# Cleaning at the sync stage means every device gets clean names and we don't
# repeat the cleanup commit (f49f657) each time a sale runs through.
_LEAD_JUNK_RE = re.compile(r"^[\\/*~`«»]+\s*")
_INCH_LOW_QUOTE_RE = re.compile(r",,\s*(\d+)\s*(?:''|\"+|”)")
_INCH_TRAILING_QUOTE_RE = re.compile(r"(\d+)\s*(?:''|”)")
_BARE_LOW_QUOTE_RE = re.compile(r",,(?=\S)")
_SMART_CLOSE_QUOTE_RE = re.compile(r"”")


def clean_product_name(name: str) -> str:
    """Normalize uCoz title artifacts. Idempotent."""
    s = name.strip()
    s = _LEAD_JUNK_RE.sub("", s)
    s = _INCH_LOW_QUOTE_RE.sub(r'\1"', s)
    s = _INCH_TRAILING_QUOTE_RE.sub(r'\1"', s)
    s = _BARE_LOW_QUOTE_RE.sub('"', s)
    s = _SMART_CLOSE_QUOTE_RE.sub('"', s)
    return s.strip()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_env(script_dir: Path) -> dict[str, str]:
    """Resolve UCOZ_SITE/UCOZ_TOKEN. Priority:
      1. Process environment (used by CI workflows)
      2. .env beside the script
      3. .env in the script's parent directory (legacy layout when this
         script lived inside bosmini_app/tools/ucoz/, with .env in tools/)
    """
    if os.environ.get("UCOZ_SITE") and os.environ.get("UCOZ_TOKEN"):
        return {
            "UCOZ_SITE": os.environ["UCOZ_SITE"],
            "UCOZ_TOKEN": os.environ["UCOZ_TOKEN"],
        }
    for candidate in (script_dir / ".env", script_dir.parent / ".env"):
        if candidate.exists():
            env: dict[str, str] = {}
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
            return env
    return {}


def fetch_categories(client: httpx.Client, base: str) -> list[dict]:
    """Return flat list of every category (including children)."""
    flat: list[dict] = []

    def walk(parent_id: int | None = None) -> None:
        params: dict = {"page": "categories"}
        if parent_id is not None:
            params["parent_id"] = parent_id
        r = client.get(f"{base}/shop/request", params=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Categories error: {data['error']}")
        for cat in data.get("success", []):
            flat.append(cat)
            childs = cat.get("childs")
            if isinstance(childs, list) and childs:
                walk(int(cat["cat_id"]))

    walk()
    return flat


def fetch_category_products(client: httpx.Client, base: str, cat_id: int) -> list[dict]:
    """Paginate through one category and return all goods_list items merged."""
    items: list[dict] = []
    page = 1
    while True:
        r = client.get(
            f"{base}/shop/request",
            params={"page": "category", "cat_id": cat_id, "page": page},  # 'page' twice ok
        )
        # The duplicate 'page' key above is wrong — httpx will keep last. Reset:
        r = client.get(
            f"{base}/shop/request",
            params=[("page", "category"), ("cat_id", str(cat_id)), ("page", str(page))],
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            if err.get("code") == "INCORRECT_PARAMETERS":
                # Empty category or already past last page
                break
            raise RuntimeError(f"Category {cat_id} page {page} error: {err}")
        success = data.get("success", {})
        goods = success.get("goods_list") or {}
        if isinstance(goods, dict):
            for entry_id, entry in goods.items():
                if isinstance(entry, dict):
                    entry.setdefault("entry_id", int(entry_id) if str(entry_id).isdigit() else entry_id)
                    items.append(entry)
        paginator = success.get("paginator") or {}
        cur = int(paginator.get("cur_page", 1))
        total = int(paginator.get("num_pages", 1))
        if cur >= total:
            break
        page = cur + 1
        time.sleep(0.1)
    return items


def to_product_json(entry: dict, cat_lookup: dict[int, str]) -> dict:
    """Map a uAPI shop entry → HEAD Product.fromJson schema."""
    entry_id = entry.get("entry_id")
    slug = (entry.get("entry_hgu") or f"product-{entry_id}").strip()

    cats_obj = entry.get("entry_cats") or {}
    raw_cats = cats_obj.get("cats") or []
    cat_names = [c.get("name", "").strip() for c in raw_cats if c.get("name")]
    if not cat_names:
        single = entry.get("entry_cat") or {}
        if single.get("name"):
            cat_names = [single["name"].strip()]

    price = entry.get("entry_price") or {}
    price_raw = price.get("price_raw") or 0
    try:
        price_rub = int(float(price_raw))
    except (TypeError, ValueError):
        price_rub = 0

    old = entry.get("entry_price_old") or {}
    old_raw = old.get("price_raw") or 0
    try:
        old_rub = int(float(old_raw))
    except (TypeError, ValueError):
        old_rub = 0
    old_price_rub = old_rub if old_rub > 0 and old_rub != price_rub else None

    photo = entry.get("entry_photo") or {}
    def_p = photo.get("def_photo") or {}
    # Single full-resolution URL serves both the catalog grid and the
    # detail gallery. ProductThumb decodes via memCacheWidth, so memory
    # stays small while the disk cache is shared (tile → detail = warm).
    full_url = def_p.get("photo") or def_p.get("middl") or def_p.get("thumb") or ""

    full_urls: list[str] = [full_url] if full_url else []

    others = photo.get("others_photo")
    if isinstance(others, list):
        for o in others:
            if not isinstance(o, dict):
                continue
            f = o.get("photo") or o.get("middl") or o.get("thumb")
            if f and f not in full_urls:
                full_urls.append(f)

    brand = (entry.get("entry_brand") or "").strip()
    sku = (entry.get("entry_art_no") or "").strip()
    brief = (entry.get("entry_brief") or "").strip()

    # If brief has the ✅ checklist, prefer it (matches HEAD highlights parser).
    # Else fall back to entry_description (HTML — strip tags below).
    if brief:
        description = html.unescape(brief)
    else:
        from html.parser import HTMLParser

        class _Strip(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts: list[str] = []

            def handle_data(self, data):
                self.parts.append(data)

        s = _Strip()
        s.feed(entry.get("entry_description") or "")
        description = html.unescape("\n".join(p.strip() for p in s.parts if p.strip()))

    stock = entry.get("entry_stock") or {}
    stock_total = stock.get("stock_total") or 0
    # uCoz shop has wholesale min_amount=10000 — stock 0 usually means "not tracked"
    # rather than out-of-stock. Treat positive price as in-stock unless hidden.
    is_hidden = bool(entry.get("entry_is_hidden"))
    in_stock = (not is_hidden) and price_rub > 0

    # Behavioural signals for the home "Хиты" / "Новые прибытия" rails. uCoz
    # exposes no manual hit/new flag via uAPI, so we drive these from organic
    # data instead: entry_ordered (cumulative order count) is the strongest
    # popularity signal, entry_added_time the real creation timestamp.
    # entry_solds is ~always 0, so it's intentionally dropped.
    def _as_int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    popularity = _as_int(entry.get("entry_ordered"))
    added_at = _as_int(entry.get("entry_added_time"))
    views = _as_int(entry.get("entry_views"))

    return {
        "slug": slug,
        "url": entry.get("entry_shop_url") or "",
        "name": clean_product_name(entry.get("entry_title") or ""),
        "sku": sku,
        "price_rub": price_rub,
        "old_price_rub": old_price_rub,
        "in_stock": in_stock,
        "categories": cat_names,
        "brand_tags": [brand] if brand else [],
        "description": description,
        "specs": [],
        "image_urls": full_urls,
        "full_image_urls": full_urls,
        "local_images": full_urls,
        "thumb_image_urls": full_urls,
        "stock_count": int(stock_total) if isinstance(stock_total, (int, float)) else 0,
        "popularity": popularity,
        "added_at": added_at,
        "views": views,
    }


def run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print stats without writing the file")
    ap.add_argument("--out", default=None, help="Override output path")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    env = load_env(here)
    site = env.get("UCOZ_SITE")
    token = env.get("UCOZ_TOKEN")
    if not (site and token):
        print(
            "ERROR: UCOZ_SITE and UCOZ_TOKEN must be set as env vars "
            "or in a .env file next to the script.",
            file=sys.stderr,
        )
        return 2

    base = f"https://{site}/uapi"
    out_path = Path(args.out) if args.out else (here / "catalog.json")
    print(f"Site:  {site}")
    print(f"Base:  {base}")
    print(f"Out:   {out_path}\n")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    with httpx.Client(timeout=args.timeout, headers=headers, follow_redirects=True) as client:
        print("Fetching categories…")
        cats = fetch_categories(client, base)
        cat_lookup = {int(c["cat_id"]): c.get("cat_name", "") for c in cats}
        print(f"  found {len(cats)} categories")

        all_entries: dict[int, dict] = {}
        for c in cats:
            cid = int(c["cat_id"])
            name = c.get("cat_name", "?")
            goods_count = c.get("goods_count", 0)
            if goods_count == 0:
                print(f"  [skip] cat {cid:>4} {name!r} (0 goods)")
                continue
            entries = fetch_category_products(client, base, cid)
            new = 0
            for e in entries:
                eid = e.get("entry_id")
                if eid not in all_entries:
                    all_entries[eid] = e
                    new += 1
            print(f"  [ok]   cat {cid:>4} {name[:30]!r:32}  fetched={len(entries):>3}  new={new:>3}  total={len(all_entries)}")

    print(f"\nTotal unique entries: {len(all_entries)}")

    products = [to_product_json(e, cat_lookup) for e in all_entries.values()]
    products.sort(key=lambda p: (p["categories"][0] if p["categories"] else "", p["name"]))

    payload = {
        "source": "ucoz-uapi",
        "site": site,
        "generated_at": int(time.time()),
        "scraped_count": len(products),
        "products": products,
    }

    if args.dry_run:
        print(f"\n[DRY] would write {len(products)} products → {out_path}")
        print("Sample product:")
        if products:
            print(json.dumps(products[0], ensure_ascii=False, indent=2))
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"\nWrote {len(products)} products → {out_path}  ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
