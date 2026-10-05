import unittest
from unittest.mock import patch

import api_engines
import config
import discord_router


class OfficialCardFilterTests(unittest.TestCase):
    def test_jumbo_and_oversized_cards_are_rejected(self):
        for title in (
            "Pokemon Gengar EX 34/119 Jumbo Card XY: Phantom Forces 034/119 Holofoil MP",
            "Pokémon TCG Deoxys EX Holo Rare Card 53/116 JUMBO Oversized Pokemon Card MP",
            "MTG The Ur-Dragon Oversized Commander 2017 Foil NM",
            "Atraxa, Praetors' Voice (Commander 2016) Oversize Cards Foil",
        ):
            self.assertFalse(api_engines.is_official_card(title), title)

    def test_display_cases_and_custom_items_are_rejected(self):
        for title in (
            "Zapdos Ex 202/165 - 151 | Pokémon Custom Extended Artwork Display Case",
            "POKEMON TCG EXTENDED ART CASE Hydreigon ex SIR 169/086 SV: White Flare",
            "Hydreigon Ex 169/086 Pokémon Card Extended Art Display Case White Flare",
            "Charizard 4/102 custom holo",
        ):
            self.assertFalse(api_engines.is_official_card(title), title)

    def test_genuine_cards_still_pass(self):
        for title in (
            # "Extended Art" is an official Magic treatment.
            "Bloodline Recollector (Extended Art) 427 NM Reality Fracture MTG",
            "Tifa, Martial Artist (Extended Art) -Foil Mint",
            # "custom" must match as a whole word only.
            "Charizard ex 199/165 no customs fees for US customers",
            "Pokemon Gengar EX 34/119 XY Phantom Forces Holo Rare",
        ):
            self.assertTrue(api_engines.is_official_card(title), title)


class AnniversaryReprintTests(unittest.TestCase):
    def test_reprint_carrying_the_original_set_total_is_flagged(self):
        for title, total in (
            ("Dark Tyranitar 19/109 English 30th Anniversary Pokemon Card", "109"),
            ("Pokemon TGC 30th Anniversary - Dark Tyranitar 19/109 Holo Rare Classic", "109"),
            ("Genesect Ex 11/101 30th Anniversary Pokémon Tcg Card English", "101"),
            ("Charizard 4/102 Celebrations Classic Collection", "102"),
            ("Pikachu 58/102 25th Anniv Classic Collection", "102"),
        ):
            self.assertTrue(api_engines.is_anniversary_reprint(title, total), title)

    def test_original_printings_are_not_flagged(self):
        for title, total in (
            ("Dark Tyranitar 19/109 Rare Team Rocket Returns Pokemon Card TCG", "109"),
            ("Dark Tyranitar 19/109 – Team Rocket Returns – Rare – NM – 2004 Pokémon", "109"),
        ):
            self.assertFalse(api_engines.is_anniversary_reprint(title, total), title)

    def test_celebrations_own_numbering_is_not_flagged(self):
        self.assertFalse(api_engines.is_anniversary_reprint("Mew 11/25 Celebrations Holo", "25"))
        self.assertFalse(api_engines.is_anniversary_reprint("Mew 011/025 25th Anniversary", "025"))

    def test_flagged_titles_parse_to_the_total_the_scanner_passes_in(self):
        parsed = api_engines.parse_title(
            "pokemon", "Dark Tyranitar 19/109 English 30th Anniversary Pokemon Card"
        )
        self.assertIsNotNone(parsed)
        self.assertTrue(api_engines.is_anniversary_reprint("30th Anniversary", parsed[2]))


class OpenedConditionTests(unittest.TestCase):
    def test_opened_and_used_conditions(self):
        for condition in ("Used", "Open Box/Used", "Open box", "Pre-owned", "Opened"):
            self.assertTrue(api_engines.is_opened_condition(condition), condition)

    def test_new_conditions(self):
        for condition in (
            "New", "New/Factory Sealed", "New/Sealed", "Brand New",
            "New/Unopened (JP)", "Unused", "Not specified", "",
        ):
            self.assertFalse(api_engines.is_opened_condition(condition), condition)


class JapanImportCostTests(unittest.TestCase):
    def test_cost_is_percentage_plus_flat(self):
        self.assertEqual(
            config.jp_import_cost(100.0),
            round(100.0 * config.JP_IMPORT_FEE_PCT + config.JP_IMPORT_FLAT_USD, 2),
        )

    def test_typical_japanese_box_is_no_longer_a_deal(self):
        # The median Mercari JP alert before this change: $68.73 against a
        # $108.25 US market price for the same Japanese box.
        price, market = 68.73, 108.25
        self.assertLessEqual(price, market * config.DEAL_RATIO)
        self.assertGreater(price + config.jp_import_cost(price), market * config.DEAL_RATIO)

    def test_a_much_cheaper_box_still_is(self):
        price, market = 45.0, 108.25
        self.assertLessEqual(price + config.jp_import_cost(price), market * config.DEAL_RATIO)

    @patch("discord_router._post_embed", return_value=True)
    def test_sealed_alert_names_the_import_estimate(self, post):
        discord_router.send_sealed_alert(
            "ストームエメラルダ BOX", "https://jp.mercari.com/item/m1", 45.0, 28.6, 108.25,
            "https://example.com/webhook", "pokemon",
            en_title="Storm Emeralda Booster Box", ship_label="est. import cost",
        )
        price_field = post.call_args.args[1]["fields"][0]["value"]
        self.assertIn("+ $28.60 est. import cost", price_field)


class PartialSealedProductTests(unittest.TestCase):
    def setUp(self):
        products = [
            ("Burst Protocol Booster Box [1st Edition]", 89.26),
            ("Chaos Origins Booster Box [1st Edition]", 89.80),
            ("Pokemon GO Mini Tin", 12.0),
            ("Surging Sparks Elite Trainer Box", 55.0),
            ("Paldea Evolved 3 Pack Blister", 14.0),
        ]
        api_engines._sealed_index["testgame"] = [
            (api_engines._sealed_tokens(name), price, name) for name, price in products
        ]
        api_engines._sealed_idf["testgame"] = {}
        api_engines._sealed_until["testgame"] = float("inf")

    def tearDown(self):
        for cache in (api_engines._sealed_index, api_engines._sealed_idf, api_engines._sealed_until):
            cache.pop("testgame", None)

    def price(self, title):
        return api_engines.fetch_sealed_price("testgame", title)

    def test_full_box_still_matches(self):
        self.assertEqual(
            self.price("Yu-Gi-Oh Burst Protocol 1st Edition Booster Box Factory Sealed"),
            (89.26, "Burst Protocol Booster Box [1st Edition]"),
        )
        # A large pack count is how sellers describe a genuine box.
        self.assertIsNotNone(self.price("Chaos Origins 1st Edition Booster Box 24 Packs Sealed"))

    def test_partial_products_are_not_priced_as_the_box(self):
        for title in (
            "Yu-Gi-Oh Burst Protocol 1st Edition English TCG Sealed Mini Booster Box",
            "Yu-Gi-Oh Chaos Origins 4 Pack Booster Box 1st Ed Token Card Konami TCG",
            "Chaos Origins 1st Edition Booster Box 3-Pack",
            "EMPTY Burst Protocol 1st Edition Booster Box display only",
        ):
            self.assertIsNone(self.price(title), title)

    def test_words_the_product_itself_carries_are_allowed(self):
        self.assertEqual(self.price("Pokemon GO Mini Tin sealed")[1], "Pokemon GO Mini Tin")
        # An Elite Trainer Box genuinely contains 9 packs.
        self.assertEqual(
            self.price("Pokemon Surging Sparks Elite Trainer Box 9 Packs Sealed")[1],
            "Surging Sparks Elite Trainer Box",
        )
        self.assertEqual(
            self.price("Pokemon Paldea Evolved 3 Pack Blister new")[1],
            "Paldea Evolved 3 Pack Blister",
        )


if __name__ == "__main__":
    unittest.main()
