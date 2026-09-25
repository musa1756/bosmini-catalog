"""
Pre-encode catalog tile images as small WebP files and point catalog.json at them.

Runs on the Beget VPS right after sync_catalog.py (see vps-sync.sh), before
the catalog is published. For every product's first photo it takes uCoz's
square 'b' variant (1000 px, white-padded — the framing the app grid already
shows), scales it to 600 px and encodes WebP q88: ~70 KB instead of the
~225 KB the Expo grid pulled per tile (500 px 'm' base + 1000 px 'b'), with
no visible difference at tile size (SSIM ≈ 0.985 on a 39-photo sample).

Files are content-addressed (<hash of encoder settings + source bytes>.webp)
and Caddy serves them from /etc/caddy/thumbs with an immutable 1-year cache.
A URL never changes meaning: a photo replaced in uCoz gets a new hash and a
new URL. Every product whose tile is on disk gets "tile_url"; the app falls
back to the uCoz JPEGs without it, so this step is best-effort and must
never block the catalog publish.

Sources are revalidated with conditional GETs (If-None-Match), a slice per
run, so a photo replaced under the same uCoz URL is picked up within about an
hour without re-downloading ~500 photos every 5 minutes.

Usage:
    python make_tiles.py --catalog catalog.json --state tiles.json \\
        --out-dir /opt/beget/supabase/volumes/proxy/caddy/thumbs \\
        --base-url https://api.boss-mini-app.ru/t
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ORIGIN = "https://bosminiofficial.com/"
SIZE = 600
QUALITY = 88
# Part of every file hash: changing the encoder settings re-encodes all tiles
# under new URLs instead of silently swapping bytes behind cached ones.
VERSION = f"webp{SIZE}q{QUALITY}m6-v1"
USER_AGENT = "bosmini-tiles/1 (+https://api.boss-mini-app.ru)"


def first_photo(product: dict) -> str | None:
    for key in ("full_image_urls", "image_urls", "local_images"):
        urls = product.get(key)
        if isinstance(urls, list) and urls and isinstance(urls[0], str):
            return urls[0]
    return None


def square_variant(photo: str) -> str | None:
    """uCoz's 1000 px white-padded square ('b') for an original shop photo."""
    if photo.startswith(ORIGIN) and photo.lower().endswith(".jpg"):
        return photo[:-4] + "b.jpg"
    return None


def tile_name(source: bytes) -> str:
    return hashlib.sha256(VERSION.encode() + b"\0" + source).hexdigest()[:24]


def encode_tile(source: bytes) -> bytes:
    """Square, white-backed, at most SIZE px, WebP q88 with the ICC profile kept."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(source)) as opened:
        icc = opened.info.get("icc_profile")
        im = ImageOps.exif_transpose(opened)
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            im = Image.new("RGB", rgba.size, "white")
            im.paste(rgba, mask=rgba.getchannel("A"))
        else:
            im = im.convert("RGB")
        w, h = im.size
        if w != h:
            side = max(w, h)
            canvas = Image.new("RGB", (side, side), "white")
            canvas.paste(im, ((side - w) // 2, (side - h) // 2))
            im = canvas
        if im.width > SIZE:
            im = im.resize((SIZE, SIZE), Image.LANCZOS)
        out = io.BytesIO()
        extra = {"icc_profile": icc} if icc else {}
        im.save(out, "WEBP", quality=QUALITY, method=6, **extra)
        return out.getvalue()


def write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("version") != VERSION:
        # New encoder settings: forget the old photo → tile map (old files
        # stay referenced by their last-seen time and age out via GC).
        return {"version": VERSION, "photos": {}, "tiles": state.get("tiles", {})}
    state.setdefault("photos", {})
    state.setdefault("tiles", {})
    return state


def process(client: httpx.Client, photo: str, rec: dict | None, out_dir: Path) -> dict:
    """Fetch (conditionally when [rec] is known) and encode one photo's tile.

    Returns the new record for [photo]; raises on network/decode failures so
    the caller keeps the previous record.
    """
    src = rec["src"] if rec else square_variant(photo)
    headers = {}
    if rec and (out_dir / f"{rec['tile']}.webp").exists():
        if rec.get("etag"):
            headers["If-None-Match"] = rec["etag"]
        if rec.get("last_modified"):
            headers["If-Modified-Since"] = rec["last_modified"]
    resp = client.get(src, headers=headers)
    if resp.status_code == 304 and rec:
        return {**rec, "checked": int(time.time())}
    if resp.status_code == 404 and src != photo:
        # No square variant: pad the original ourselves.
        src = photo
        resp = client.get(src)
    resp.raise_for_status()
    name = tile_name(resp.content)
    target = out_dir / f"{name}.webp"
    if not target.exists():
        write_atomic(target, encode_tile(resp.content))
    return {
        "tile": name,
        "src": src,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
        "checked": int(time.time()),
    }


def plan(photos: list[str], state: dict, out_dir: Path, now: int, revalidate_after: int, revalidate_per_run: int) -> list[str]:
    """New or missing tiles first (catalog order), then the stalest revalidations."""
    fresh, stale = [], []
    for photo in photos:
        rec = state["photos"].get(photo)
        if not rec or not (out_dir / f"{rec['tile']}.webp").exists():
            fresh.append(photo)
        elif now - rec.get("checked", 0) >= revalidate_after:
            stale.append(photo)
    stale.sort(key=lambda p: state["photos"][p].get("checked", 0))
    return fresh + stale[:revalidate_per_run]


def annotate(products: list[dict], state: dict, out_dir: Path, base_url: str) -> set[str]:
    """Sets/clears tile_url on each product; returns the tile names in use."""
    used: set[str] = set()
    base = base_url.rstrip("/")
    for p in products:
        photo = first_photo(p)
        rec = state["photos"].get(photo) if photo else None
        if rec and (out_dir / f"{rec['tile']}.webp").exists():
            p["tile_url"] = f"{base}/{rec['tile']}.webp"
            used.add(rec["tile"])
        else:
            p.pop("tile_url", None)
    return used


def collect_garbage(state: dict, used: set[str], out_dir: Path, now: int, keep_days: float) -> int:
    """Deletes tiles unreferenced for [keep_days] — clients may still hold an
    older cached catalog pointing at them."""
    horizon = now - keep_days * 86400
    for name in used:
        state["tiles"][name] = now
    deleted = 0
    for name, last in list(state["tiles"].items()):
        if last < horizon:
            (out_dir / f"{name}.webp").unlink(missing_ok=True)
            del state["tiles"][name]
            deleted += 1
    # Files a crashed run wrote but never recorded.
    for f in out_dir.glob("*.webp"):
        if f.stem not in state["tiles"] and f.stat().st_mtime < horizon:
            f.unlink(missing_ok=True)
            deleted += 1
    return deleted


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True, help="catalog.json to annotate in place")
    ap.add_argument("--state", required=True, help="JSON state file (photo → tile map)")
    ap.add_argument("--out-dir", required=True, help="Directory Caddy serves as /t/")
    ap.add_argument("--base-url", required=True, help="Public URL of --out-dir, e.g. https://api.boss-mini-app.ru/t")
    ap.add_argument("--budget", type=float, default=150, help="Seconds to spend fetching/encoding; the rest waits for the next run")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--revalidate-after", type=int, default=3600, help="Seconds before a tile's source is re-checked")
    ap.add_argument("--revalidate-per-run", type=int, default=60)
    ap.add_argument("--keep-days", type=float, default=30)
    args = ap.parse_args(argv)

    started = time.monotonic()
    now = int(time.time())
    catalog_path = Path(args.catalog)
    state_path = Path(args.state)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    products = payload.get("products") or []
    state = load_state(state_path)

    photos = list(dict.fromkeys(p for p in map(first_photo, products) if p and square_variant(p)))
    todo = plan(photos, state, out_dir, now, args.revalidate_after, args.revalidate_per_run)

    done = failed = skipped = 0
    deadline = started + args.budget

    def job(photo: str):
        if time.monotonic() > deadline:
            return photo, None, "skipped"
        try:
            return photo, process(client, photo, state["photos"].get(photo), out_dir), None
        except Exception as e:  # noqa: BLE001 — one bad photo must not stop the rest
            return photo, None, f"{type(e).__name__}: {e}"

    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for photo, rec, err in pool.map(job, todo):
                if rec:
                    state["photos"][photo] = rec
                    done += 1
                elif err == "skipped":
                    skipped += 1
                else:
                    failed += 1
                    print(f"tile failed: {photo}: {err}", file=sys.stderr)

    # Forget photos that left the catalog; their files age out via GC.
    live = set(photos)
    state["photos"] = {k: v for k, v in state["photos"].items() if k in live}

    used = annotate(products, state, out_dir, args.base_url)
    deleted = collect_garbage(state, used, out_dir, now, args.keep_days)

    write_atomic(state_path, json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    # Same formatting as sync_catalog.py so the GitHub replica diff stays small.
    write_atomic(catalog_path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    with_tile = sum(1 for p in products if p.get("tile_url"))
    print(
        f"tiles: {with_tile}/{len(products)} products have tile_url ({len(used)} files); "
        f"processed {done}, failed {failed}, deferred {skipped}, deleted {deleted} "
        f"in {time.monotonic() - started:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
