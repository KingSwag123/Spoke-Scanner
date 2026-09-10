import unittest
from unittest.mock import Mock, patch

import api_engines
from shopify_source import infer_game


class YugiohPricingTests(unittest.TestCase):
    def setUp(self):
        self.group = {"groupId": 330, "abbreviation": "LOB"}
        self.rows = {
            "LOB-001": [
                ("Blue-Eyes White Dragon", "Ultra Rare", "unlimited", 75.0),
                ("Blue-Eyes White Dragon", "Ultra Rare", "1st edition", 5000.0),
            ]
        }

    def test_parser_requires_printed_set_code(self):
        self.assertEqual(
            api_engines.parse_title(
                "yugioh", "Yu-Gi-Oh Blue-Eyes White Dragon LOB-001 1st Edition"
            ),
            "LOB-001",
        )
        self.assertEqual(
            api_engines.parse_title(
                "yugioh", "Ash Blossom RA01-EN008 Quarter Century"
            ),
            "RA01-EN008",
        )
        self.assertEqual(
            api_engines.parse_title("yugioh", "Speed Duel Card SS04-ENA01"),
            "SS04-ENA01",
        )
        self.assertIsNone(
            api_engines.parse_title("yugioh", "Blue-Eyes White Dragon card")
        )

    @patch("api_engines.requests.get")
    def test_catalog_preserves_duplicate_group_abbreviations(self, get):
        categories = Mock()
        categories.json.return_value = {
            "results": [{"categoryId": 2, "name": "YuGiOh"}]
        }
        groups = Mock()
        groups.json.return_value = {"results": [
            {"groupId": 1292, "abbreviation": "RP02", "name": "Retro Pack 2"},
            {"groupId": 24352, "abbreviation": "RP02",
             "name": "Retro Pack 2 (2020 Date Reprint)"},
        ]}
        get.side_effect = [categories, groups]
        old_groups, old_until = api_engines._ygo_groups, api_engines._ygo_groups_until
        try:
            api_engines._ygo_groups = {}
            api_engines._ygo_groups_until = 0
            result = api_engines._yugioh_groups()
            self.assertEqual(
                [group["groupId"] for group in result["RP02"]],
                [1292, 24352],
            )
        finally:
            api_engines._ygo_groups = old_groups
            api_engines._ygo_groups_until = old_until

    @patch("api_engines._yugioh_group_index")
    @patch("api_engines._yugioh_groups")
    def test_exact_code_name_and_edition_are_required(self, groups, index):
        groups.return_value = {"LOB": [self.group]}
        index.return_value = self.rows
        first = api_engines.fetch_yugioh_price(
            "LOB-001", "Blue-Eyes White Dragon LOB-001 1st Edition"
        )
        self.assertEqual(first[0], 5000.0)
        unlimited = api_engines.fetch_yugioh_price(
            "LOB-001", "Blue-Eyes White Dragon LOB-001 Unlimited"
        )
        self.assertEqual(unlimited[0], 75.0)
        self.assertIsNone(api_engines.fetch_yugioh_price(
            "LOB-001", "Blue-Eyes White Dragon LOB-001"
        ))
        self.assertIsNone(api_engines.fetch_yugioh_price(
            "LOB-001", "Dark Magician LOB-001 1st Edition"
        ))

    @patch("api_engines._yugioh_group_index")
    @patch("api_engines._yugioh_groups")
    def test_duplicate_group_abbreviation_requires_print_run_qualifier(
        self, groups, index
    ):
        original = {
            "groupId": 1292, "abbreviation": "RP02",
            "name": "Retro Pack 2", "publishedOn": "2009-07-28",
        }
        reprint = {
            "groupId": 24352, "abbreviation": "RP02",
            "name": "Retro Pack 2 (2020 Date Reprint)",
            "publishedOn": "2025-08-22",
        }
        groups.return_value = {"RP02": [reprint, original]}
        index.side_effect = lambda group: {
            "RP02-EN001": [
                ("Card Name", "Ultra Rare", "unlimited", 10.0)
            ]
        } if group["groupId"] == 1292 else {
            "RP02-EN001": [
                ("Card Name", "Ultra Rare", "unlimited", 2.0)
            ]
        }
        self.assertIsNone(api_engines.fetch_yugioh_price(
            "RP02-EN001", "Card Name RP02-EN001 Unlimited Ultra Rare"
        ))
        self.assertEqual(api_engines.fetch_yugioh_price(
            "RP02-EN001",
            "Card Name RP02-EN001 2020 Date Reprint Unlimited Ultra Rare",
        )[0], 2.0)
        self.assertEqual(api_engines.fetch_yugioh_price(
            "RP02-EN001", "Card Name RP02-EN001 2009 Unlimited UR"
        )[0], 10.0)

    @patch("api_engines._yugioh_group_index")
    @patch("api_engines._yugioh_groups")
    def test_multi_rarity_requires_and_matches_explicit_rarity(
        self, groups, index
    ):
        group = {"groupId": 23233, "abbreviation": "RA01"}
        groups.return_value = {"RA01": [group]}
        index.return_value = {
            "RA01-EN008": [
                ("Ash Blossom & Joyous Spring", "Super Rare", "1st edition", 1.0),
                ("Ash Blossom & Joyous Spring (UR)", "Ultra Rare", "1st edition", 2.0),
                ("Ash Blossom & Joyous Spring (PUR)", "Prismatic Ultimate Rare",
                 "1st edition", 7.0),
                ("Ash Blossom & Joyous Spring (PCR)",
                 "Prismatic Collector's Rare", "1st edition", 8.0),
                ("Ash Blossom & Joyous Spring (Quarter Century Secret Rare)",
                 "Quarter Century Secret Rare", "1st edition", 50.0),
            ]
        }
        base = "Ash Blossom & Joyous Spring RA01-EN008 1st Edition"
        self.assertIsNone(api_engines.fetch_yugioh_price("RA01-EN008", base))
        for qualifier, expected in (
            ("UR", 2.0),
            ("PUR", 7.0),
            ("PCR", 8.0),
            ("Quarter Century", 50.0),
        ):
            self.assertEqual(api_engines.fetch_yugioh_price(
                "RA01-EN008", f"{base} {qualifier}"
            )[0], expected)
        self.assertIsNone(api_engines.fetch_yugioh_price(
            "RA01-EN008", f"{base} UR PCR"
        ))

    def test_shopify_and_sealed_classification(self):
        self.assertEqual(infer_game("Yu-Gi-Oh Structure Deck"), "yugioh")
        self.assertTrue(api_engines.is_yugioh_sealed("Yu-Gi-Oh Structure Deck"))
        self.assertFalse(api_engines.is_yugioh_sealed(
            "Blue-Eyes White Dragon LOB-001 PSA 10"
        ))


if __name__ == "__main__":
    unittest.main()