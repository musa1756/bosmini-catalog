import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

import make_tiles as tiles

PHOTO = "https://bosminiofficial.com/_sh/19/1935.jpg"
SQUARE = "https://bosminiofficial.com/_sh/19/1935b.jpg"


def jpeg(size, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeOrigin:
    """Static nginx stand-in: serves bytes by URL, honours If-None-Match."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requests: list[httpx.Request] = []

    def etag(self, url: str) -> str:
        return '"' + hashlib.md5(self.files[url]).hexdigest()[:12] + '"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url not in self.files:
            return httpx.Response(404)
        tag = self.etag(url)
        if request.headers.get("if-none-match") == tag:
            return httpx.Response(304, headers={"etag": tag})
        return httpx.Response(200, content=self.files[url], headers={"etag": tag})


class MakeTilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root / "thumbs"
        self.state = root / "tiles.json"
        self.catalog = root / "catalog.json"
        self.origin = FakeOrigin({SQUARE: jpeg((1000, 1000))})

    def tearDown(self):
        self.tmp.cleanup()

    def write_catalog(self, products):
        self.catalog.write_text(json.dumps({"products": products}, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_tiles(self, *extra):
        real_client = httpx.Client

        def client(**kw):
            kw.pop("follow_redirects", None)
            return real_client(transport=httpx.MockTransport(self.origin), **kw)

        with mock.patch.object(tiles.httpx, "Client", client), mock.patch("sys.stdout", io.StringIO()):
            tiles.run([
                "--catalog", str(self.catalog), "--state", str(self.state),
                "--out-dir", str(self.out), "--base-url", "https://cdn.example/t/", *extra,
            ])
        return json.loads(self.catalog.read_text(encoding="utf-8"))["products"]

    def test_square_variant_only_for_ucoz_jpegs(self):
        self.assertEqual(tiles.square_variant(PHOTO), SQUARE)
        self.assertIsNone(tiles.square_variant("https://elsewhere.example/1.jpg"))
        self.assertIsNone(tiles.square_variant("https://bosminiofficial.com/_sh/1/1.png"))

    def test_encode_pads_to_white_square_and_never_upscales(self):
        tall = Image.open(io.BytesIO(tiles.encode_tile(jpeg((500, 1000), (0, 0, 0)))))
        self.assertEqual((tall.format, tall.size), ("WEBP", (600, 600)))
        self.assertGreater(tall.convert("RGB").getpixel((5, 300))[0], 240)  # padding is white
        self.assertLess(tall.convert("RGB").getpixel((300, 300))[0], 20)  # photo kept centred
        small = Image.open(io.BytesIO(tiles.encode_tile(jpeg((480, 480)))))
        self.assertEqual(small.size, (480, 480))

    def test_first_run_writes_tile_and_annotates_product(self):
        products = self.run_tiles_for([{"slug": "a", "full_image_urls": [PHOTO]}])
        url = products[0]["tile_url"]
        self.assertRegex(url, r"^https://cdn\.example/t/[0-9a-f]{24}\.webp$")
        self.assertTrue((self.out / url.rsplit("/", 1)[1]).exists())
        self.assertEqual(str(self.origin.requests[0].url), SQUARE)

    def run_tiles_for(self, products, *extra):
        self.write_catalog(products)
        return self.run_tiles(*extra)

    def test_revalidation_uses_etag_and_keeps_url_when_unchanged(self):
        first = self.run_tiles_for([{"slug": "a", "full_image_urls": [PHOTO]}])[0]["tile_url"]
        self.origin.requests.clear()
        second = self.run_tiles("--revalidate-after", "0")[0]["tile_url"]
        self.assertEqual(first, second)
        self.assertEqual(self.origin.requests[0].headers.get("if-none-match"), self.origin.etag(SQUARE))

    def test_replaced_photo_gets_new_url(self):
        first = self.run_tiles_for([{"slug": "a", "full_image_urls": [PHOTO]}])[0]["tile_url"]
        self.origin.files[SQUARE] = jpeg((1000, 1000), (10, 120, 10))
        self.assertEqual(self.run_tiles()[0]["tile_url"], first)  # not due for revalidation yet
        self.assertNotEqual(self.run_tiles("--revalidate-after", "0")[0]["tile_url"], first)

    def test_missing_square_variant_falls_back_to_original(self):
        self.origin.files = {PHOTO: jpeg((800, 1000))}
        products = self.run_tiles_for([{"slug": "a", "full_image_urls": [PHOTO]}])
        self.assertIn("tile_url", products[0])
        self.assertEqual([str(r.url) for r in self.origin.requests], [SQUARE, PHOTO])

    def test_failed_fetch_leaves_product_without_tile(self):
        self.origin.files = {}
        products = self.run_tiles_for([
            {"slug": "a", "full_image_urls": [PHOTO]},
            {"slug": "b", "full_image_urls": ["https://elsewhere.example/1.jpg"], "tile_url": "stale"},
        ])
        self.assertNotIn("tile_url", products[0])
        self.assertNotIn("tile_url", products[1])

    def test_unreferenced_tiles_are_deleted_after_keep_days(self):
        url = self.run_tiles_for([{"slug": "a", "full_image_urls": [PHOTO]}])[0]["tile_url"]
        tile = self.out / url.rsplit("/", 1)[1]
        self.run_tiles_for([{"slug": "b", "full_image_urls": []}])
        self.assertTrue(tile.exists())  # still within keep-days
        state = json.loads(self.state.read_text())
        state["tiles"][tile.stem] = int(time.time()) - 31 * 86400
        self.state.write_text(json.dumps(state))
        self.run_tiles()
        self.assertFalse(tile.exists())

    def test_catalog_keeps_sync_catalog_formatting(self):
        self.run_tiles_for([{"slug": "a", "name": "Магнитола", "full_image_urls": [PHOTO]}])
        text = self.catalog.read_text(encoding="utf-8")
        self.assertIn('  "products": [', text)
        self.assertIn("Магнитола", text)


if __name__ == "__main__":
    unittest.main()
