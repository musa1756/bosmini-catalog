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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


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


class UcozApiClient:
    def __init__(
        self,
        client: httpx.Client,
        base: str,
        *,
        request_delay: float,
        max_retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
        request_deadline: float = 0.0,
    ) -> None:
        self.client = client
        self.base = base.rstrip("/")
        self.request_delay = max(0.0, request_delay)
        self.max_retries = max(1, max_retries)
        self.retry_base_delay = max(0.1, retry_base_delay)
        self.retry_max_delay = max(self.retry_base_delay, retry_max_delay)
        self.request_deadline = max(0.0, request_deadline)
        # Workers for _get_with_deadline: an abandoned (timed-out) call keeps
        # its thread blocked on the socket, so keep a few spares. The process
        # is short-lived (one sync run), leaked threads die with it.
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._last_request_at = 0.0

    def _get_with_deadline(self, url: str, params) -> httpx.Response:
        """GET with a hard wall-clock cap on the WHOLE request.

        httpx's read timeout is per-chunk: a server that drips one byte every
        few seconds resets it forever and can hold a request open for hours
        (observed on the uCoz uAPI, 2026-07-07). The future timeout below is
        total elapsed time, which such tarpits cannot defeat.
        """
        if self.request_deadline <= 0:
            return self.client.get(url, params=params)
        future = self._executor.submit(self.client.get, url, params=params)
        try:
            return future.result(timeout=self.request_deadline)
        except FutureTimeout:
            future.cancel()
            raise httpx.ReadTimeout(
                f"no complete response within {self.request_deadline:.0f}s"
            )

    def shop_request(self, params, *, label: str) -> dict:
        url = f"{self.base}/shop/request"
        for attempt in range(1, self.max_retries + 1):
            self._wait_for_slot()
            try:
                response = self._get_with_deadline(url, params)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    delay = self._retry_delay(response, attempt)
                    print(
                        f"  [retry] {label}: HTTP {response.status_code}, "
                        f"attempt {attempt}/{self.max_retries}, waiting {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self._backoff_delay(attempt)
                print(
                    f"  [retry] {label}: {exc.__class__.__name__}, "
                    f"attempt {attempt}/{self.max_retries}, waiting {delay:.1f}s"
                )
                time.sleep(delay)

        raise RuntimeError(f"{label}: request failed after {self.max_retries} attempts")

    def _wait_for_slot(self) -> None:
        if self._last_request_at <= 0:
            self._last_request_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_at = time.monotonic()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(self.retry_max_delay, max(self.request_delay, float(retry_after)))
            except ValueError:
                pass
        return self._backoff_delay(attempt)

    def _backoff_delay(self, attempt: int) -> float:
        return min(self.retry_max_delay, self.retry_base_delay * (2 ** (attempt - 1)))


def fetch_categories(api: UcozApiClient) -> list[dict]:
    """Return flat list of every category (including children)."""
    flat: list[dict] = []

    def walk(parent_id: int | None = None) -> None:
        params: dict = {"page": "categories"}
        if parent_id is not None:
            params["parent_id"] = parent_id
        data = api.shop_request(params, label=f"categories parent={parent_id or 'root'}")
        if "error" in data:
            raise RuntimeError(f"Categories error: {data['error']}")
        for cat in data.get("success", []):
            flat.append(cat)
            childs = cat.get("childs")
            if isinstance(childs, list) and childs:
                walk(int(cat["cat_id"]))

    walk()
    return flat


def fetch_category_products(api: UcozApiClient, cat_id: int) -> list[dict]:
    """Paginate through one category and return all goods_list items merged."""
    items: list[dict] = []
    page = 1
    while True:
        data = api.shop_request(
            [("page", "category"), ("cat_id", str(cat_id)), ("page", str(page))],
            label=f"category {cat_id} page {page}",
        )
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
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--request-delay", type=float, default=1.0, help="Minimum seconds between uAPI requests")
    ap.add_argument("--max-retries", type=int, default=6, help="Attempts per uAPI request")
    ap.add_argument("--retry-base-delay", type=float, default=10.0, help="Initial retry backoff in seconds")
    ap.add_argument("--retry-max-delay", type=float, default=90.0, help="Maximum retry backoff in seconds")
    ap.add_argument(
        "--request-deadline", type=float, default=0.0,
        help="Hard wall-clock cap per uAPI request in seconds (0 = off). "
        "Catches drip-feed responses that never trip the read timeout.",
    )
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
    print(
        "uAPI limits: "
        f"timeout={args.timeout:.1f}s, delay={args.request_delay:.1f}s, "
        f"retries={args.max_retries}, backoff={args.retry_base_delay:.1f}-{args.retry_max_delay:.1f}s\n"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    with httpx.Client(timeout=args.timeout, headers=headers, follow_redirects=True) as client:
        api = UcozApiClient(
            client,
            base,
            request_delay=args.request_delay,
            max_retries=args.max_retries,
            request_deadline=args.request_deadline,
            retry_base_delay=args.retry_base_delay,
            retry_max_delay=args.retry_max_delay,
        )
        print("Fetching categories…")
        cats = fetch_categories(api)
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
            entries = fetch_category_products(api, cid)
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
