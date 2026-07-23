import tempfile
import unittest
from pathlib import Path

import sync_to_woo as sync


class WooSyncHelpersTest(unittest.TestCase):
    def setUp(self):
        self.source = {
            "slug": "demo",
            "name": "Demo",
            "price_rub": 2250,
            "old_price_rub": None,
            "description": "✅ Первый пункт ✅ Второй пункт",
            "categories": ["Категория"],
            "in_stock": True,
            "image_urls": ["https://source.example/2975.jpg"],
        }
        self.expected = sync.product_payload(self.source, [7], with_images=True)
        self.actual = {
            "name": "Demo",
            "slug": "demo",
            "sku": "demo",
            "type": "simple",
            "regular_price": "2250.00",
            "sale_price": "",
            "description": "<ul>\n<li>Первый пункт</li>\n<li>Второй пункт</li>\n</ul>\n",
            "categories": [{"id": 7}],
            "stock_status": "instock",
            "catalog_visibility": "visible",
            "images": [{"src": "https://store.example/uploads/2975-1.jpg"}],
        }

    def test_store_product_match_ignores_wordpress_html_formatting_and_image_suffix(self):
        self.assertEqual(
            sync.store_product_matches(self.actual, self.expected, check_images=True),
            (True, True),
        )

    def test_store_product_match_detects_price_and_image_drift(self):
        changed = {**self.actual, "regular_price": "2450", "images": [{"src": "https://store/2967.webp"}]}
        self.assertEqual(
            sync.store_product_matches(changed, self.expected, check_images=True),
            (False, False),
        )

    def test_image_hash_changes_with_source_url(self):
        changed = {**self.source, "image_urls": ["https://source.example/2976.jpg"]}
        self.assertNotEqual(sync.image_hash(self.source), sync.image_hash(changed))

    def test_legacy_and_v2_state_are_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text('{"demo": "content-hash"}', encoding="utf-8")
            self.assertEqual(
                sync.load_state(path),
                {"demo": {"content": "content-hash", "images": None}},
            )
            path.write_text(
                '{"version": 2, "products": {"demo": {"content": "c", "images": "i"}}}',
                encoding="utf-8",
            )
            self.assertEqual(
                sync.load_state(path),
                {"demo": {"content": "c", "images": "i"}},
            )


if __name__ == "__main__":
    unittest.main()
