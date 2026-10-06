"""Sealed-channel accuracy rules: wrong-product matches, title guards, the
repeat limit and the durable dedup store. All offline."""

import unittest
from unittest.mock import MagicMock, patch

import api_engines
import config
import discord_router
import scanner_state
import tcgplayer_source


class SealedIndexCase(unittest.TestCase):
    """Builds a small in-memory sealed index for one game."""

    game = "testgame"
    products: list = []
    idf: dict = {}

    def setUp(self):
        idx = [(api_engines._sealed_tokens(name, self.game), price, name)
               for name, price in self.products]
        api_engines._sealed_index[self.game] = idx
        api_engines._sealed_idf[self.game] = dict(self.idf)
        api_engines._sealed_sets[self.game] = api_engines._sealed_set_names(idx, self.game)
        api_engines._sealed_until[self.game] = float("inf")

    def tearDown(self):
        for cache in (api_engines._sealed_index, api_engines._sealed_idf,
                      api_engines._sealed_sets, api_engines._sealed_until):
            cache.pop(self.game, None)

    def price(self, title):
        return api_engines.fetch_sealed_price(self.game, title)


class CelebrationAliasTests(SealedIndexCase):
    products = [
        ("Celebrations Elite Trainer Box", 362.05),
        ("30th Celebration Elite Trainer Box", 171.0),
    ]
    idf = {"30th": 3.0, "celebration": 3.0, "celebrations": 3.0,
           "elite": 0.5, "trainer": 0.5, "box": 0.1}

    def test_plural_spelling_of_the_2026_set_matches_the_2026_product(self):
        for title in (
            "Pokemon 30th Celebrations Elite Trainer Box (ETB) Sealed",
            "Pokémon TCG Celebrations 30th Anniversary Elite Trainer Box ETB English SEALED",
        ):
            self.assertEqual(self.price(title), (171.0, "30th Celebration Elite Trainer Box"), title)

    def test_the_2021_box_still_matches_itself(self):
        self.assertEqual(
            self.price("Pokemon Celebrations Elite Trainer Box 25th Anniversary 2021 Sealed"),
            (362.05, "Celebrations Elite Trainer Box"),
        )

    def test_singular_spelling_is_unchanged(self):
        self.assertEqual(
            self.price("Pokémon TCG 30th Celebration Elite Trainer Box")[1],
            "30th Celebration Elite Trainer Box",
        )


class OtherFormAndLotTests(SealedIndexCase):
    products = [
        ("Twilight Masquerade Booster Box", 353.97),
        ("Twilight Masquerade Half Booster Box", 180.0),
        ("Temporal Forces Booster Box", 316.5),
        ("Perfect Order Booster Bundle", 38.0),
        ("Ascended Heroes Elite Trainer Box", 155.45),
        ("Lorwyn Booster Box", 2000.0),
        ("Awakening of the New Era - Booster Box", 1073.52),
    ]
    idf = {"twilight": 3.0, "masquerade": 3.0, "temporal": 3.0, "forces": 3.0,
           "half": 2.0, "booster": 0.2, "box": 0.1}

    def test_other_product_forms_are_not_priced_as_the_booster_box(self):
        for title in (
            "Pokémon TCG Temporal Forces Elite Trainer Box Booster Promo EN",
            "Pokemon Temporal Forces ETB + Booster Box art sleeves",
            "Pokémon TCG Temporal Forces Booster Bundle Box 6 Packs",
            "MTG Lorwyn Eclipsed Collector Booster Box Sealed",
        ):
            self.assertIsNone(self.price(title), title)

    def test_half_box_goes_to_the_half_box_product_or_nowhere(self):
        self.assertEqual(
            self.price("Pokémon TCG: Twilight Masquerade Half Booster Box (18 Packs) - New & Sealed")[1],
            "Twilight Masquerade Half Booster Box",
        )
        self.assertIsNone(self.price("Pokémon TCG Temporal Forces Half Booster Box (18 Packs)"))

    def test_eighteen_packs_is_a_partial_box_except_for_magic(self):
        title = "Temporal Forces Booster Box 18 Packs Sealed"
        toks = set("temporal forces booster box 18 packs sealed".split())
        product = api_engines._sealed_tokens("Temporal Forces Booster Box")
        self.assertTrue(api_engines._sealed_title_is_partial(title, toks, product, "pokemon"))
        self.assertFalse(api_engines._sealed_title_is_partial(title, toks, product, "mtg"))

    def test_a_set_code_is_not_a_pack_count(self):
        self.assertEqual(
            self.price("One Piece OP-05 Booster Pack Awakening of the New Era Booster Box English")[1],
            "Awakening of the New Era - Booster Box",
        )

    def test_bundle_as_seller_chatter_does_not_block_another_product(self):
        self.assertEqual(
            self.price("NEW RELEASE!!! Pokémon Ascended Heroes Elite Trainer Box!!! DM To Bundle!!!")[1],
            "Ascended Heroes Elite Trainer Box",
        )

    def test_lots_are_not_priced_as_one_unit(self):
        for title in (
            "Pokémon Mega Evolution Ascended Heroes Elite Trainer Box Lot of 2 Dragonite",
            "Temporal Forces Pokemon TCG Booster Packs - Lot of 20 (BOOSTER BOX FRESH)",
        ):
            self.assertIsNone(self.price(title), title)
        self.assertEqual(
            self.price("Pokémon Ascended Heroes Elite Trainer Box Sealed")[1],
            "Ascended Heroes Elite Trainer Box",
        )


class EraLevelGuardTests(SealedIndexCase):
    game = "pokemon"
    products = [
        ("Scarlet & Violet Booster Box", 296.19),
        ("Scarlet & Violet Booster Bundle", 104.49),
        ("Sword & Shield Booster Box", 688.99),
        ("Black Bolt Booster Bundle", 60.0),
        ("Surging Sparks Booster Box", 303.34),
        ("Charizard ex Box", 40.0),
    ]
    idf = {"scarlet": 1.0, "violet": 1.0, "sword": 1.0, "shield": 1.0, "black": 2.0,
           "bolt": 3.0, "surging": 3.0, "sparks": 3.0, "booster": 0.2, "box": 0.1,
           "bundle": 0.5, "charizard": 2.0, "ex": 0.5}

    def test_a_named_expansion_goes_to_its_own_product(self):
        self.assertEqual(
            self.price("Pokémon TCG Scarlet & Violet Black Bolt Booster Bundle Sealed")[1],
            "Black Bolt Booster Bundle",
        )

    def test_a_named_expansion_never_falls_back_to_the_era_product(self):
        # Surging Sparks has a box but no bundle in the index.
        self.assertIsNone(
            self.price("Pokémon TCG Scarlet & Violet Surging Sparks Booster Bundle Sealed"))

    def test_the_era_product_still_matches_its_own_listing(self):
        self.assertEqual(
            self.price("Pokemon Scarlet & Violet Base Set Booster Box 36 Packs Sealed")[1],
            "Scarlet & Violet Booster Box",
        )
        self.assertEqual(
            self.price("Pokemon Scarlet & Violet Booster Box English factory sealed")[1],
            "Scarlet & Violet Booster Box",
        )

    def test_only_expansion_products_supply_set_names(self):
        names = api_engines._sealed_sets["pokemon"]
        self.assertIn(frozenset({"surging", "sparks"}), names)
        self.assertNotIn(frozenset({"charizard", "ex"}), names)


class DropWordsByGameTests(unittest.TestCase):
    def test_one_is_kept_outside_one_piece(self):
        self.assertIn("one", api_engines._sealed_tokens("Play! Pokemon Prize Pack Series One", "pokemon"))
        series_one = api_engines._sealed_tokens("Play! Pokemon Prize Pack Series One", "pokemon")
        title = set("play pokemon prize pack series 6 sealed".split())
        self.assertFalse(series_one <= title)

    def test_one_piece_brand_words_stay_dropped_for_one_piece(self):
        toks = api_engines._sealed_tokens("One Piece Card Game Illustration Box Vol. 6", "onepiece")
        self.assertFalse({"one", "piece"} & toks)


class LanguageGuardTests(unittest.TestCase):
    def test_non_english_listing_against_an_english_product(self):
        for title in (
            "ONE PIECE CARD GAME OP-11 A Fist of Divine Speed Booster Box Japanese",
            "Pokemon TCG Sword & Shield Dark Phantasma s10a JP 1x Sealed Booster Box",
            "Pokemon Mega Dream ex M2a Booster Box JPN",
            "Lorcana Azurite Sea Booster Box Japan Import",
            "Pokemon 151 Booster Box KOR sealed",
            "MTG Bloomburrow Play Booster Box ITA",
            "MTG Foundations Play Booster Box RUSSIAN",
            "Pokemon Karmesin & Purpur Display DEUTSCH",
        ):
            self.assertTrue(api_engines.sealed_language_mismatch(title, "Some Set Booster Box"), title)

    def test_english_listing_passes(self):
        self.assertFalse(api_engines.sealed_language_mismatch(
            "Pokemon Surging Sparks Booster Box English Factory Sealed", "Surging Sparks Booster Box"))

    def test_product_that_carries_the_language_word_passes(self):
        self.assertFalse(api_engines.sealed_language_mismatch(
            "Pokemon 151 Booster Box Japanese sv2a", "Pokemon Card 151 Japanese Booster Box"))

    def test_japanese_script_counts_so_main_must_exempt_the_japanese_lane(self):
        # main.py skips this guard for JP_MARKET_SOURCES; without that exemption
        # the Mercari JP / Yahoo JP lane would go silent.
        self.assertTrue(api_engines.sealed_language_mismatch(
            "ストームエメラルダ BOX 新品未開封 シュリンク付き", "Storm Emeralda Booster Box"))


class LotGuardTests(unittest.TestCase):
    def test_stated_quantities_of_whole_units(self):
        for title in (
            "2X Pokemon 30th Celebrations ETB Elite Trainer Box Factory Sealed",
            "Pokémon 30th Celebration Elite Trainer Box x2 Sealed",
            "Twilight Masquerade Booster Box 2box sealed",
            "Pokemon Surging Sparks 3 BOXES Booster Box",
            "Ascended Heroes Elite Trainer Box X2 (2 ETB's)",
        ):
            self.assertTrue(api_engines.is_sealed_lot(title, "Some Set Booster Box"), title)

    def test_counts_inside_one_product_are_not_lots(self):
        for title in (
            "Surging Sparks Booster Box x36 packs factory sealed",
            "Pokemon Mini Tin x4 Booster Packs sealed",
            "Twilight Masquerade Booster Box (6 boxes available)",
            "( LEGENDARY DUELISTS SEASON 2 ) 1st Edition Box - Sealed",
        ):
            self.assertFalse(api_engines.is_sealed_lot(title, "Some Set Booster Box"), title)

    def test_display_and_case_products_are_exempt(self):
        self.assertFalse(api_engines.is_sealed_lot(
            "Pokemon Booster Bundle Display 10x Booster Bundles", "Prismatic Evolutions Booster Bundle Display"))


class PresaleTests(unittest.TestCase):
    def test_presale_wording(self):
        for title in (
            "PRESALE Pokemon Center Delta Reign Elite Trainer Box ETB Exclusive Sealed 6 Nov",
            "Pokémon TCG 30th Celebration Booster Bundle Box 6 Packs English 2026  PRE-ORDER",
            "Delta Reign Booster Bundle Pre Order sealed",
            "Delta Reign Booster Box PREORDER",
            "Delta Reign Elite Trainer Box Ships Nov 6",
            "Delta Reign Booster Box *Expected Release Date 11-06-2026*",
        ):
            self.assertTrue(api_engines.is_presale(title), title)

    def test_in_hand_wording(self):
        for title in (
            "Reality Fracture - Prerelease Pack",
            "Pokemon Booster Box Pre-Owned display",
            "FREE SHIP March of the Machine Draft Booster Box",
            "Yu-Gi-Oh Structure Deck sealed ships fast",
            "Delta Reign Booster Box IN HAND not a presale",
        ):
            self.assertFalse(api_engines.is_presale(title), title)


class ConditionCaveatTests(unittest.TestCase):
    def caveat(self, title, product="Some Set Booster Box"):
        return api_engines.sealed_condition_caveat(title, product)

    def test_each_tier(self):
        self.assertEqual(self.caveat("Pokémon TCG 30th Celebration Elite Trainer Box(Slight Tear!)"), "flaw")
        self.assertEqual(self.caveat("DAMAGED - Perfect Order Pokemon Center Elite Trainer Box"), "flaw")
        self.assertEqual(self.caveat("Base Set 2 Booster Pack WOTC Sealed POTENTIALLY TAMPERED"), "flaw")
        self.assertEqual(self.caveat("Jungle Booster Pack 1st Edition HEAVY 21.3g"), "weight")
        self.assertEqual(self.caveat("Evolving Skies Booster Box READ DESCRIPTION"), "pointer")

    def test_clean_and_negated_titles(self):
        for title in (
            "Surging Sparks Booster Box Factory Sealed",
            "Evolving Skies Booster Box No Rips Tears Mint",
            "Evolving Skies Booster Box no rips or tears",
            "Fusion Strike Booster Box Mint No DMG",
            "Lost Origin Booster Box never opened unopened",
            "151 Booster Bundle unweighed sealed",
            "Booster Box shipped in heavy duty box",
        ):
            self.assertIsNone(self.caveat(title), title)

    def test_a_word_in_the_products_own_name_is_not_a_caveat(self):
        self.assertIsNone(self.caveat(
            "Unova Heavy Hitters Premium Collection Box Sealed", "Unova Heavy Hitters Premium Collection"))

    @patch("discord_router._post_embed", return_value=True)
    def test_the_note_is_shown_under_the_headline_and_the_alert_still_posts(self, post):
        sent = discord_router.send_sealed_alert(
            "Surging Sparks Booster Box (torn wrap)", "https://example.com/b", 200.0, 0.0, 303.34,
            "https://example.com/webhook", "pokemon",
            note="Seller's title mentions damage or an opened seal",
        )
        self.assertTrue(sent)
        embed = post.call_args.args[1]
        self.assertIn("Seller's title mentions damage", embed["description"])
        self.assertIn("[Surging Sparks Booster Box (torn wrap)](https://example.com/b)", embed["description"])
        self.assertNotIn("Restock", embed["title"])


class RepeatLimitTests(unittest.TestCase):
    def setUp(self):
        self.recent = {}
        self.key = "pokemon|30th Celebration Elite Trainer Box"

    def post(self, total, now):
        ok = api_engines.repeat_allowed(self.recent, self.key, total, now)
        if ok:
            self.recent.setdefault(self.key, []).append([now, total])
        return ok

    def test_first_three_post_then_only_a_new_low(self):
        self.assertTrue(self.post(150.0, 0))
        self.assertTrue(self.post(160.0, 100))
        self.assertTrue(self.post(155.0, 200))
        self.assertFalse(self.post(152.0, 300))    # not below the low of 150
        self.assertFalse(self.post(150.0, 400))    # equal is not lower
        self.assertTrue(self.post(149.99, 500))    # new low
        self.assertFalse(self.post(149.99, 600))

    def test_window_expires(self):
        for i, total in enumerate((150.0, 160.0, 155.0)):
            self.assertTrue(self.post(total, i))
        later = config.SEALED_REPEAT_WINDOW + 10
        self.assertTrue(self.post(170.0, later))   # old alerts aged out: slots are free again
        self.assertEqual(len(self.recent[self.key]), 1)

    def test_products_are_independent(self):
        for i in range(3):
            self.assertTrue(self.post(150.0 + i, i))
        self.assertTrue(api_engines.repeat_allowed(self.recent, "pokemon|Other Box", 999.0, 5))
        # A damaged copy is keyed apart and cannot hide clean ones.
        self.assertTrue(api_engines.repeat_allowed(self.recent, self.key + "|caveat", 999.0, 5))


class DurableSeenStoreTests(unittest.TestCase):
    def setUp(self):
        scanner_state._available = False
        scanner_state._failures = 0

    tearDown = setUp

    def connection(self, rows=()):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = list(rows)
        conn.cursor.return_value.__enter__.return_value = conn.cursor.return_value
        return conn

    def test_load_returns_iso_strings(self):
        from datetime import datetime, timezone
        when = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        with patch.object(scanner_state, "_connect", return_value=self.connection([("v1|1|0", when)])):
            seen = scanner_state.load_seen(90)
        self.assertEqual(seen, {"v1|1|0": "2026-10-01T12:00:00+00:00"})
        self.assertTrue(scanner_state._available)

    def test_missing_database_fails_soft(self):
        with patch.object(scanner_state, "_connect", side_effect=RuntimeError("no table")):
            self.assertIsNone(scanner_state.load_seen(90))
        self.assertFalse(scanner_state._available)
        # Nothing is attempted, and nothing raises, while the store is unavailable.
        with patch.object(scanner_state, "_connect", side_effect=AssertionError("must not connect")):
            scanner_state.add_seen({"v1|1|0": "2026-10-01T12:00:00+00:00"})

    def test_write_failures_never_raise_and_pause_after_three(self):
        scanner_state._available = True
        with patch.object(scanner_state, "_connect", side_effect=RuntimeError("down")):
            for _ in range(3):
                scanner_state.add_seen({"v1|1|0": "2026-10-01T12:00:00+00:00"})
        self.assertFalse(scanner_state._available)

    def test_reconnect_recovers_after_a_bad_start(self):
        from datetime import datetime, timezone
        when = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        conn = self.connection([("old", when)])
        seen = {"new": "2026-10-05T12:00:00+00:00"}
        with patch.object(scanner_state, "_connect", return_value=conn):
            scanner_state.reconnect(seen, 90)
        self.assertTrue(scanner_state._available)
        self.assertEqual(set(seen), {"old", "new"})          # adopted what was stored
        stored = conn.cursor.return_value.executemany.call_args.args[1]
        self.assertEqual([k for k, _ in stored], ["new"])    # and stored what was missing

    def test_reconnect_is_silent_while_the_database_is_down(self):
        with patch.object(scanner_state, "_connect", side_effect=RuntimeError("down")):
            scanner_state.reconnect({"a": "2026-10-05T12:00:00+00:00"}, 90)
        self.assertFalse(scanner_state._available)

    def test_write_inserts_each_id(self):
        scanner_state._available = True
        conn = self.connection()
        with patch.object(scanner_state, "_connect", return_value=conn):
            scanner_state.add_seen({"a": "2026-10-01T12:00:00+00:00", "b": "2026-10-02T12:00:00+00:00"})
        sql, params = conn.cursor.return_value.executemany.call_args.args
        self.assertIn("ON CONFLICT (item_id) DO UPDATE SET seen_at", sql)
        self.assertEqual(len(params), 2)


class TcgplayerBaselineTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(tcgplayer_source._best_emitted)
        tcgplayer_source._best_emitted.clear()

    def tearDown(self):
        tcgplayer_source._best_emitted.clear()
        tcgplayer_source._best_emitted.update(self.saved)

    def test_recent_alerted_prices_restore_the_baseline(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        seen = {
            "tcgp-555-2685": (now - timedelta(days=1)).isoformat(),
            "tcgp-555-2499": (now - timedelta(days=2)).isoformat(),
            "tcgp-777-9900": (now - timedelta(days=30)).isoformat(),   # too old
            "v1|123|0": now.isoformat(),                               # not TCGplayer
            "tcgp-888-1000": "not a date",
        }
        self.assertEqual(tcgplayer_source.seed_best_emitted(seen), 1)
        self.assertEqual(tcgplayer_source._best_emitted, {555: 24.99})


if __name__ == "__main__":
    unittest.main()
