"""Fixture tests for bounded watch marketplace source adapters."""

import os
import unittest
from unittest.mock import Mock, patch

import watch_sources
from tcgcsv_catalog import rarity_matches_title


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class WatchSourceTests(unittest.TestCase):
    def setUp(self):
        watch_sources._token_cache.update({"token": None, "expires_at": 0})

    @patch.dict(os.environ, {"EBAY_APP_ID": "id", "EBAY_CERT_ID": "secret"}, clear=False)
    @patch("watch_sources.requests.get")
    @patch("watch_sources.requests.post")
    def test_ebay_empty_is_ok_but_request_error_is_error(self, post, get):
        post.return_value = _Response({"access_token": "token", "expires_in": 3600})
        get.return_value = _Response({"itemSummaries": []})
        empty = watch_sources.search_watch_source("ebay", {"item_name": "Charizard"})
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["checked"], 0)
        get.side_effect = __import__("requests").RequestException("offline")
        failed = watch_sources.search_watch_source("ebay", {"item_name": "Charizard"})
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["requests"], 1)  # cached OAuth token + one Browse call

    @patch.dict(os.environ, {"EBAY_APP_ID": "id", "EBAY_CERT_ID": "secret"}, clear=False)
    @patch("watch_sources.requests.get")
    @patch("watch_sources.requests.post")
    def test_ebay_requires_known_shipping_and_preserves_actual_rarity(self, post, get):
        post.return_value = _Response({"access_token": "token"})
        get.return_value = _Response({"itemSummaries": [
            {
                "itemId": "123", "title": "Charizard ex 199/165", "itemWebUrl": "https://example/123",
                "price": {"value": "100.00", "currency": "USD"},
                "buyingOptions": ["FIXED_PRICE"],
                "shippingOptions": [{"shippingCost": {"value": "4.99", "currency": "USD"}}],
                "localizedAspects": [
                    {"name": "Rarity", "value": "Special Illustration Rare"},
                    {"name": "Game", "value": "Pokemon TCG"},
                ],
            },
            {
                "itemId": "missing-ship", "title": "Charizard", "itemWebUrl": "https://example/no-ship",
                "price": {"value": "50", "currency": "USD"}, "buyingOptions": ["FIXED_PRICE"],
            },
        ]})
        result = watch_sources.search_watch_source(
            "ebay", {"item_name": "Charizard", "max_price": 105, "rarity": "Ultra Rare"})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["listings"][0]["shipping"], 4.99)
        self.assertEqual(result["listings"][0]["rarity"], "Special Illustration Rare")
        self.assertNotEqual(result["listings"][0]["rarity"], "Ultra Rare")
        self.assertEqual(result["listings"][0]["game_name"], "pokemon")

    def test_mercari_detail_requires_purchase_state_and_parses_shipping(self):
        available, shipping, price = watch_sources._mercari_detail(
            '<main><button>Buy now</button><div>Price: $12.50</div>'
            '<div>Shipping &amp; delivery: $7.99</div></main>', "m123")
        self.assertTrue(available)
        self.assertEqual(shipping, 7.99)
        self.assertEqual(price, 12.5)
        sold, sold_shipping, sold_price = watch_sources._mercari_detail(
            '<main><button>Buy now</button><p>This item is sold</p><p>Free shipping</p></main>', "m123")
        self.assertFalse(sold)
        self.assertIsNone(sold_shipping)
        self.assertIsNone(sold_price)
        recommendation_only = watch_sources._mercari_detail(
            '<main><button>Buy now</button></main><aside>Price: $1.00 Free shipping</aside>', "m123")
        self.assertEqual(recommendation_only, (True, None, None))

    def test_mercari_current_anchor_card_markup_parses_locally(self):
        page = (
            '<a href="/us/item/m12345678901/?ref=search_results">'
            '<img alt="Pikachu Illustration Rare" src="https://img.example/p.jpg">'
            '<span>$12.50</span></a>'
        )
        with patch("watch_sources._parse_mercari_search", return_value=[]):
            cards = watch_sources._parse_mercari_cards(page)
        self.assertEqual(cards, [{
            "id": "m12345678901", "name": "Pikachu Illustration Rare", "price": "12.50",
            "price_is_dollars": True, "photos": [{"thumbnail": "https://img.example/p.jpg"}],
        }])

    def test_game_title_inference_uses_listing_evidence_not_query(self):
        self.assertEqual(watch_sources._game_from_title("Exodia the Forbidden One"), "yugioh")
        self.assertIsNone(watch_sources._game_from_title("Vintage trading card"))
        self.assertEqual(watch_sources._normalized_game_name("Pokémon TCG"), "pokemon")
        self.assertEqual(watch_sources._normalized_game_name("Yu-Gi-Oh! TCG"), "yugioh")

    @patch("watch_sources.requests.post")
    def test_tcgplayer_empty_and_live_offer_parsing(self, post):
        post.return_value = _Response({"results": [{"results": []}]})
        empty = watch_sources.search_watch_source("tcgplayer", {"item_name": "Black Lotus"})
        self.assertEqual(empty["status"], "ok")
        post.return_value = _Response({"results": [{"results": [{
            "productId": 42.0, "productName": "Charizard ex",
            "lowestPrice": 90, "lowestPriceWithShipping": 94.5,
            "marketPrice": 110, "rarity": "Double Rare", "setName": "Obsidian Flames",
            "game": "YuGiOh",
        }]}]})
        result = watch_sources.search_watch_source("tcgplayer", {"item_name": "Charizard"})
        listing = result["listings"][0]
        self.assertEqual(listing["shipping"], 4.5)
        self.assertEqual(listing["rarity"], "Double Rare")
        self.assertEqual(listing["game_name"], "yugioh")
        self.assertEqual(listing["listing_identity_type"], "product_offer_aggregate")
        self.assertEqual(listing["item_id"], "tcgplayer-product-offer-42-9450-live-marketplace-offer")
        self.assertEqual(listing["url"], "https://www.tcgplayer.com/product/42")
        self.assertEqual(result["requests"], 1)

    def test_tcgplayer_quote_identity_changes_only_with_quote_or_condition(self):
        self.assertEqual(watch_sources._quote_identity_suffix(12.5, "Near Mint"),
                         "1250-near-mint")
        self.assertNotEqual(watch_sources._quote_identity_suffix(12.5, "Near Mint"),
                            watch_sources._quote_identity_suffix(12.51, "Near Mint"))

    def test_rarity_family_matching_is_not_a_substring_match(self):
        self.assertFalse(rarity_matches_title("yugioh", "Rare", "Exodia Ultra Rare"))
        self.assertFalse(rarity_matches_title("yugioh", "Rare", "Exodia Secret Rare"))
        self.assertFalse(rarity_matches_title("yugioh", "Rare", "Exodia Super Rare"))
        self.assertFalse(rarity_matches_title("onepiece", "Common", "Luffy Uncommon"))
        self.assertTrue(rarity_matches_title("yugioh", "Rare", "Exodia Rare"))
        self.assertTrue(rarity_matches_title("onepiece", "Uncommon", "Luffy Uncommon"))


if __name__ == "__main__":
    unittest.main()