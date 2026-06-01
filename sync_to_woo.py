"""
Push the synced catalog (catalog.json) into the WooCommerce store on
bosminiofficial.ru via the WooCommerce REST API (wc/v3).

This is the mirror of sync_catalog.py: that script *reads* the uCoz uAPI
catalog into catalog.json; this one *writes* catalog.json into WooCommerce.
catalog.json stays the single source of truth (already deduped + name-cleaned),
so we never re-scrape uCoz here.

Idempotency
-----------
uCoz exposes a real SKU for only ~19 of 449 products, so SKU is useless as a
match key. The uCoz `slug` is unique and present for every product, so we
write it into the WooCommerce product `sku` field and match on it. Re-running
therefore *updates* existing products instead of creating duplicates.

Safety
------
Writing 449 products into a live store is hard to undo, so the script is
dry-run by default: it prints exactly what it would create/update and exits.
Pass --apply to actually write. --limit N restricts to the first N products
for a small test batch first.

Incremental sync (cron)
-----------------------
For the every-10-minutes cron we don't want to re-write all 449 products on
every run — that hammers the reg.ru shared host (we already fight PHP
memory_limit 500s there). --changed-only keeps a tiny `woo_state.json`
sidecar mapping slug -> content-hash of the last *successfully pushed*
payload; a run then only creates/updates products whose hash changed (or that
are new), and skips the rest. State is only advanced for items that pushed
without error, so a failed item retries next run (self-healing). --prune
force-deletes products that vanished from the catalog (matched by sku=slug, so
the client's manual blank-sku products are never touched); --max-deletes caps
how many one run may delete, so a partial uAPI fetch can't wipe the store.

Auth
----
WooCommerce REST keys (Settings → Advanced → REST API → Add key, Read/Write):
    WOO_SITE=bosminiofficial.ru
    WOO_CK=ck_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    WOO_CS=cs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
in a .env next to this script, or as process env vars.

Usage
-----
    pip install httpx
    python sync_to_woo.py                 # dry-run, all products
    python sync_to_woo.py --limit 5        # dry-run, first 5
    python sync_to_woo.py --apply --limit 5   # write 5 (test batch)
    python sync_to_woo.py --apply          # write everything
    python sync_to_woo.py --apply --update-images   # also re-sideload images on updates
    python sync_to_woo.py --apply --changed-only --prune   # cron: deltas only + delete vanished
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def norm_cat(name: str) -> str:
    """Match-key for category names: collapse every kind of whitespace
    (incl. the U+2028 line-separator / NBSP that creep in from manual entry)
    to one space, then lowercase. Stops us creating duplicate categories that
    differ only by an invisible character.
    """
    return re.sub(r"[\s\u2028\u2029\u00a0]+", " ", name).strip().lower()


def load_env(script_dir: Path) -> dict[str, str]:
    """Resolve WOO_SITE/WOO_CK/WOO_CS. Priority: process env, then .env."""
    keys = ("WOO_SITE", "WOO_CK", "WOO_CS")
    if all(os.environ.get(k) for k in keys):
        return {k: os.environ[k] for k in keys}
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


def description_html(raw: str) -> str:
    """Turn the uCoz ✅-checklist brief into tidy WooCommerce HTML.

    Briefs look like "✅Экран-9" ✅Ядер-8 ✅CarPlay". Split on the check mark
    into list items; if there's no check mark, fall back to <p> with <br>.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "✅" in raw:
        items = [seg.strip() for seg in raw.split("✅") if seg.strip()]
        lis = "".join(f"<li>{html_mod.escape(i)}</li>" for i in items)
        return f"<ul>{lis}</ul>"
    return "<p>" + html_mod.escape(raw).replace("\n", "<br>") + "</p>"


def product_payload(p: dict, cat_ids: list[int], *, with_images: bool) -> dict:
    """Map a catalog.json product → WooCommerce product create/update body."""
    body: dict = {
        "name": p["name"],
        "slug": p["slug"],
        "sku": p["slug"],  # idempotency key (uCoz SKUs are mostly empty)
        "type": "simple",
        "regular_price": str(p.get("price_rub") or 0),
        "description": description_html(p.get("description", "")),
        "categories": [{"id": cid} for cid in cat_ids],
        "stock_status": "instock" if p.get("in_stock") else "outofstock",
        "catalog_visibility": "visible",
    }
    # old_price_rub is null for the whole current catalog; wire sale price only
    # if a future sync ever surfaces a genuine strike-through price.
    old = p.get("old_price_rub")
    if old and old > (p.get("price_rub") or 0):
        body["regular_price"] = str(old)
        body["sale_price"] = str(p["price_rub"])
    if with_images:
        urls = p.get("image_urls") or p.get("full_image_urls") or []
        body["images"] = [{"src": u} for u in urls]
    return body


def product_hash(p: dict, cat_ids: list[int]) -> str:
    """Stable hash of the fields we actually push, for --changed-only.

    Computed from the *image-less* payload on purpose: we never re-sideload
    images on updates (it's brutally heavy on reg.ru), so a change in image
    URLs alone must not mark a product dirty. Includes resolved category ids
    so a re-categorisation still triggers an update.
    """
    body = product_payload(p, cat_ids, with_images=False)
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Woo:
    def __init__(self, site: str, ck: str, cs: str, timeout: float):
        self.base = f"https://{site}/wp-json/wc/v3"
        self.client = httpx.Client(
            timeout=timeout,
            auth=(ck, cs),
            headers={"User-Agent": UA, "Accept": "application/json"},
            follow_redirects=True,
        )

    def _check(self, r: httpx.Response) -> dict | list:
        if r.status_code >= 400:
            raise RuntimeError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def ping(self) -> None:
        """Confirm creds + endpoint before doing anything destructive."""
        r = self.client.get(f"{self.base}/products", params={"per_page": 1})
        self._check(r)

    def existing_categories(self) -> dict[str, int]:
        """name (lowercased) -> id for every product category."""
        out: dict[str, int] = {}
        page = 1
        while True:
            data = self._check(self.client.get(
                f"{self.base}/products/categories",
                params={"per_page": 100, "page": page},
            ))
            if not data:
                break
            for c in data:
                out[norm_cat(c["name"])] = int(c["id"])
            if len(data) < 100:
                break
            page += 1
        return out

    def create_category(self, name: str) -> int:
        data = self._check(self.client.post(
            f"{self.base}/products/categories", json={"name": name},
        ))
        return int(data["id"])

    def existing_skus(self) -> dict[str, int]:
        """sku -> product id for every product already in the store."""
        out: dict[str, int] = {}
        page = 1
        while True:
            data = self._check(self.client.get(
                f"{self.base}/products",
                params={"per_page": 100, "page": page, "_fields": "id,sku"},
            ))
            if not data:
                break
            for prod in data:
                if prod.get("sku"):
                    out[prod["sku"]] = int(prod["id"])
            if len(data) < 100:
                break
            page += 1
        return out

    def batch(self, create: list[dict], update: list[dict]) -> dict:
        payload = {}
        if create:
            payload["create"] = create
        if update:
            payload["update"] = update
        return self._check(self.client.post(f"{self.base}/products/batch", json=payload))

    def ids_with_empty_sku(self) -> list[int]:
        """Every product whose sku is blank — i.e. the client's manual entries.

        Our imported products always carry sku=slug, so this never includes
        anything this importer created. Used by --delete-empty-sku.
        """
        out: list[int] = []
        page = 1
        while True:
            data = self._check(self.client.get(
                f"{self.base}/products",
                params={"per_page": 100, "page": page, "_fields": "id,sku"},
            ))
            if not data:
                break
            for prod in data:
                if not (prod.get("sku") or "").strip():
                    out.append(int(prod["id"]))
            if len(data) < 100:
                break
            page += 1
        return out

    def batch_delete(self, ids: list[int]) -> dict:
        # force=true skips the trash and removes for good (REST batch delete).
        return self._check(self.client.post(
            f"{self.base}/products/batch", json={"delete": ids},
            params={"force": "true"},
        ))


def run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write to the store. Without it, dry-run only.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process the first N products (test batch).")
    ap.add_argument("--batch-size", type=int, default=20,
                    help="Products per WooCommerce batch call (image sideload is heavy).")
    ap.add_argument("--update-images", action="store_true",
                    help="Re-send image src on updates too (default: images only on create).")
    ap.add_argument("--delete-empty-sku", action="store_true",
                    help="Force-delete every product with a blank sku (the client's manual "
                         "entries) and exit. Combine with --apply to actually delete.")
    ap.add_argument("--changed-only", action="store_true",
                    help="Incremental sync: only push products whose content-hash changed "
                         "since the last run (tracked in --state). Skips unchanged ones.")
    ap.add_argument("--state", default=None,
                    help="Path to the woo_state.json sidecar for --changed-only "
                         "(default: woo_state.json beside this script).")
    ap.add_argument("--prune", action="store_true",
                    help="Force-delete products that vanished from the catalog (matched by "
                         "sku=slug; never touches the client's blank-sku products). "
                         "Incompatible with --limit.")
    ap.add_argument("--max-deletes", type=int, default=30,
                    help="Safety cap for --prune: abort instead of deleting more than N "
                         "products in one run (guards against a partial uAPI fetch).")
    ap.add_argument("--catalog", default=None, help="Path to catalog.json")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    if args.prune and args.limit:
        print("ERROR: --prune with --limit would treat every product outside the limit as "
              "vanished and delete it. Refusing.", file=sys.stderr)
        return 2

    here = Path(__file__).resolve().parent
    env = load_env(here)
    site, ck, cs = env.get("WOO_SITE"), env.get("WOO_CK"), env.get("WOO_CS")
    if not (site and ck and cs):
        print("ERROR: WOO_SITE, WOO_CK and WOO_CS must be set (env or .env).", file=sys.stderr)
        return 2

    catalog_path = Path(args.catalog) if args.catalog else (here / "catalog.json")
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    products = payload["products"]
    if args.limit:
        products = products[: args.limit]

    mode = "APPLY (writing to live store)" if args.apply else "DRY-RUN (no writes)"
    print(f"Mode:    {mode}")
    print(f"Target:  https://{site}/wp-json/wc/v3")
    print(f"Source:  {catalog_path}  ({len(products)} products)\n")

    woo = Woo(site, ck, cs, args.timeout)
    print("Pinging WooCommerce REST API…")
    woo.ping()
    print("  OK\n")

    # --- Optional: purge the client's manual (empty-sku) products ---
    if args.delete_empty_sku:
        ids = woo.ids_with_empty_sku()
        print(f"Products with blank sku (manual entries): {len(ids)}")
        if not args.apply:
            print("[DRY-RUN] would force-delete these. Re-run with --apply to delete.")
            return 0
        deleted = 0
        bs = max(1, args.batch_size)
        for i in range(0, len(ids), bs):
            chunk = ids[i : i + bs]
            res = woo.batch_delete(chunk)
            deleted += sum(1 for it in res.get("delete", []) if it and not it.get("error"))
            print(f"  deleted {deleted}/{len(ids)}")
            time.sleep(0.5)
        print(f"\nDone. force-deleted {deleted} products.")
        return 0

    # --- Categories: ensure every catalog category exists, build name->id ---
    cat_map = woo.existing_categories()
    needed = sorted({c for p in products for c in (p.get("categories") or [])})
    print(f"Categories: {len(needed)} in catalog, {len(cat_map)} already in store")
    for name in needed:
        if norm_cat(name) in cat_map:
            continue
        if args.apply:
            cid = woo.create_category(name)
            cat_map[norm_cat(name)] = cid
            print(f"  [created] {name!r} -> {cid}")
        else:
            print(f"  [would create] {name!r}")

    # --- Split products into create vs update by sku(=slug) ---
    # sku_map (sku->id) is needed for create-vs-update, for --changed-only, and
    # for --prune, so fetch it whenever any of those is in play.
    want_store = args.apply or args.changed_only or args.prune
    sku_map = woo.existing_skus() if want_store else {}
    if want_store:
        print(f"\nStore has {len(sku_map)} products with a sku.")

    # --changed-only: load the slug->hash sidecar of what we last pushed OK.
    state_path = Path(args.state) if args.state else (here / "woo_state.json")
    state: dict[str, str] = {}
    if args.changed_only and state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"  [warn] couldn't read state {state_path}: {e} — treating as empty.")
        else:
            print(f"State:  {len(state)} known products in {state_path.name}")

    def cat_ids_for(p: dict) -> list[int]:
        ids = []
        for name in (p.get("categories") or []):
            cid = cat_map.get(norm_cat(name))
            if cid:
                ids.append(cid)
        return ids

    to_create: list[dict] = []
    to_update: list[tuple[int, dict]] = []
    new_hashes: dict[str, str] = {}
    skipped = 0
    for p in products:
        slug = p["slug"]
        cids = cat_ids_for(p)
        h = product_hash(p, cids)
        new_hashes[slug] = h
        existing_id = sku_map.get(slug)
        # Unchanged + already in store + we have a matching state hash -> skip.
        if args.changed_only and existing_id and state.get(slug) == h:
            skipped += 1
            continue
        if existing_id:
            body = product_payload(p, cids, with_images=args.update_images)
            body["id"] = existing_id
            to_update.append((existing_id, body))
        else:
            to_create.append(product_payload(p, cids, with_images=True))

    # --prune: products in the store (by sku=slug) that no longer exist in the
    # catalog. Blank-sku products (the client's manual entries) aren't in
    # sku_map, so they can never be deleted here.
    to_delete: list[tuple[str, int]] = []
    if args.prune:
        new_slugs = {p["slug"] for p in products}
        to_delete = [(sku, pid) for sku, pid in sku_map.items() if sku not in new_slugs]
        if len(to_delete) > args.max_deletes:
            print(f"ERROR: --prune would delete {len(to_delete)} products "
                  f"(> --max-deletes {args.max_deletes}). Likely a partial fetch — "
                  f"aborting WITHOUT any writes.", file=sys.stderr)
            return 2

    plan = f"create {len(to_create)}, update {len(to_update)}"
    if args.changed_only:
        plan += f", skip {skipped}"
    if args.prune:
        plan += f", delete {len(to_delete)}"
    print(f"\nPlan: {plan}")

    if not args.apply:
        if to_delete:
            print("\n[DRY-RUN] would delete (vanished from catalog):")
            for sku, pid in to_delete[:20]:
                print(f"    - {sku} (id {pid})")
            if len(to_delete) > 20:
                print(f"    … +{len(to_delete) - 20} more")
        if to_create:
            print("\n[DRY-RUN] Sample create payload:")
            print(json.dumps(to_create[0], ensure_ascii=False, indent=2)[:1200])
        print("\nRe-run with --apply to write. Tip: --apply --limit 5 first.")
        return 0

    # --- Apply in batches ---
    created = updated = deleted = errors = 0
    synced: set[str] = set()  # slugs that pushed without error (for state)
    bs = max(1, args.batch_size)
    # Creates
    for i in range(0, len(to_create), bs):
        chunk = to_create[i : i + bs]
        res = woo.batch(create=chunk, update=[])
        for item in res.get("create", []):
            if item.get("error"):
                errors += 1
                print(f"  [create ERR] {item.get('error')}")
            else:
                created += 1
                if item.get("sku"):
                    synced.add(item["sku"])
        print(f"  created {created}/{len(to_create)}")
        time.sleep(0.5)
    # Updates
    upd_bodies = [b for _, b in to_update]
    for i in range(0, len(upd_bodies), bs):
        chunk = upd_bodies[i : i + bs]
        res = woo.batch(create=[], update=chunk)
        for item in res.get("update", []):
            if item.get("error"):
                errors += 1
                print(f"  [update ERR] {item.get('error')}")
            else:
                updated += 1
                if item.get("sku"):
                    synced.add(item["sku"])
        print(f"  updated {updated}/{len(upd_bodies)}")
        time.sleep(0.5)
    # Deletes (--prune)
    del_ids = [pid for _, pid in to_delete]
    for i in range(0, len(del_ids), bs):
        chunk = del_ids[i : i + bs]
        res = woo.batch_delete(chunk)
        for item in res.get("delete", []):
            if item and not item.get("error"):
                deleted += 1
            elif item and item.get("error"):
                errors += 1
                print(f"  [delete ERR] {item.get('error')}")
        print(f"  deleted {deleted}/{len(del_ids)}")
        time.sleep(0.5)

    # --- Persist state for --changed-only ---
    # Record the current hash only for products that pushed OK or were unchanged
    # (already in sync). Products whose push errored are left out so they retry
    # next run. Pruned products fall out naturally (not in `products`).
    if args.changed_only:
        new_state: dict[str, str] = {}
        for p in products:
            slug = p["slug"]
            h = new_hashes[slug]
            if slug in synced or (slug in sku_map and state.get(slug) == h):
                new_state[slug] = h
        try:
            state_path.write_text(
                json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"\nState written: {len(new_state)} products → {state_path.name}")
        except OSError as e:
            print(f"  [warn] couldn't write state {state_path}: {e}")

    summary = f"created={created} updated={updated}"
    if args.prune:
        summary += f" deleted={deleted}"
    summary += f" skipped={skipped} errors={errors}"
    print(f"\nDone. {summary}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(run())
