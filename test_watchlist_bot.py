import asyncio
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import discord

import tcgcsv_catalog
from tcgcsv_catalog import (
    CatalogProduct,
    CatalogSet,
    CatalogSnapshot,
    TCGCSVWatchCatalog,
    canonical_rarity,
    rarity_matches_title,
)
from watchlist_bot import (
    PendingDM,
    Watch,
    WatchlistBot,
    WatchlistStore,
    _listing_matches_watch_filters,
    _unsupported_game_name,
)
from watch_search import MERCARI_DAILY_PERSONAL_REQUEST_CAP, TargetedWatchSearch


class WatchlistStoreTests(unittest.TestCase):
    def test_explicit_rarity_cannot_be_overridden_by_title(self):
        watch = Watch(1, 1, "Exodia", "exodia", 50, "yugioh", rarity="Rare")
        listing = {"game_name": "yugioh", "rarity": "Ultra Rare"}
        self.assertFalse(_listing_matches_watch_filters(watch, listing, "Exodia Rare"))
        listing["rarity"] = "Rare"
        self.assertTrue(_listing_matches_watch_filters(watch, listing, "Exodia"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "watchlists.db")
        self.store = WatchlistStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_updates_same_user_and_term(self):
        self.store.upsert_watch(123, "  Booster   Box ", 100)
        self.store.upsert_watch(123, "booster box", 80)

        watches = self.store.list_watches()
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0].max_price, 80)
        self.assertEqual(watches[0].normalized_name, "booster box")

    def test_delivery_dedup_is_per_user_and_listing(self):
        self.store.upsert_watch(123, "Pikachu", 50)
        watch = self.store.list_watches()[0]
        self.assertFalse(self.store.was_delivered(watch.user_id, "listing-1"))
        self.store.mark_delivered(watch.user_id, "listing-1")
        self.assertTrue(self.store.was_delivered(watch.user_id, "listing-1"))

    def test_list_and_delete_watches_are_isolated_by_user(self):
        self.store.upsert_watch(123, "Pikachu-V", 50)
        self.store.upsert_watch(123, "Booster Box", 100)
        self.store.upsert_watch(456, "Pikachu V", 75)

        own = self.store.list_watches_for_user(123)
        self.assertEqual(
            [(watch.item_name, watch.max_price) for watch in own],
            [("Booster Box", 100), ("Pikachu-V", 50)],
        )
        self.assertEqual(self.store.delete_watch(123, "pikachu v"), "Pikachu-V")
        self.assertIsNone(self.store.delete_watch(123, "Pikachu-V"))
        self.assertEqual(
            [watch.item_name for watch in self.store.list_watches_for_user(123)],
            ["Booster Box"],
        )
        self.assertEqual(
            [watch.item_name for watch in self.store.list_watches_for_user(456)],
            ["Pikachu V"],
        )

    def test_delete_watch_cancels_its_pending_alerts(self):
        self.store.upsert_watch(123, "Pikachu V", 50)
        self.store.enqueue_matches(
            [{
                "item_id": "pending-listing",
                "title": "Pikachu-V Full Art",
                "price": 40,
                "shipping": 0,
                "url": "https://example.com/pending",
            }]
        )
        self.assertEqual(self.store.pending_count(), 1)
        self.store.delete_watch(123, "Pikachu-V")
        self.assertEqual(self.store.pending_count(), 0)

    def test_catalog_filters_fail_closed_but_unfiltered_watches_still_work(self):
        self.store.upsert_watch(
            123, "Pikachu V", 50, "pokemon", "Base Set", "BS", "Holo Rare"
        )
        self.store.upsert_watch(456, "Pikachu V", 50)
        listings = [
            {
                "item_id": "wrong-rarity",
                "title": "Pokemon Pikachu V Base Set Ultra Rare",
                "game_name": "pokemon",
                "price": 20, "shipping": 0, "url": "https://example.com/1",
            },
            {
                "item_id": "unproven-set",
                "title": "Pokemon Pikachu V Holo Rare",
                "game_name": "pokemon",
                "price": 20, "shipping": 0, "url": "https://example.com/2",
            },
            {
                "item_id": "matching-print",
                "title": "Pokemon Pikachu V Base Set Holo Rare",
                "game_name": "pokemon",
                "price": 20, "shipping": 0, "url": "https://example.com/3",
            },
        ]
        self.assertEqual(self.store.enqueue_matches(listings), 4)
        pending = self.store.pending(10)
        # User 123 receives only the proven set + rarity match. User 456's
        # existing unfiltered watch continues to receive all three.
        self.assertEqual(
            sorted((entry.user_id, entry.item_id) for entry in pending),
            [(123, "matching-print"), (456, "matching-print"),
             (456, "unproven-set"), (456, "wrong-rarity")],
        )
        filtered = next(entry for entry in pending if entry.user_id == 123)
        self.assertEqual(filtered.set_name, "Base Set")
        self.assertEqual(filtered.rarity, "Holo Rare")

    def test_updating_same_phrase_rechecks_queued_alerts_in_transaction(self):
        item = {
            "item_id": "recheck",
            "title": "Pokemon Pikachu V Base Set Holo Rare",
            "game_name": "pokemon",
            "price": 20, "shipping": 0, "url": "https://example.com/recheck",
        }
        self.store.upsert_watch(123, "Pikachu V", 50)
        self.store.enqueue_matches([item])
        self.assertEqual(self.store.pending_count(), 1)
        stale_alert = self.store.pending(10)[0]

        # Same listing still satisfies the replacement filter, so it remains
        # queued with the replacement filter details instead of stale details.
        self.store.upsert_watch(
            123, "Pikachu V", 50, "pokemon", "Base Set", None, "Holo Rare"
        )
        pending = self.store.pending(10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            (pending[0].set_name, pending[0].rarity), ("Base Set", "Holo Rare")
        )
        self.assertFalse(self.store.pending_is_current(stale_alert))

        # A changed rarity invalidates that previously queued match immediately.
        self.store.upsert_watch(
            123, "Pikachu V", 50, "pokemon", "Base Set", None, "Ultra Rare"
        )
        self.assertEqual(self.store.pending_count(), 0)

    def test_old_watch_keyed_delivery_history_is_migrated(self):
        legacy_path = str(Path(self.tmp.name) / "legacy.db")
        with sqlite3.connect(legacy_path) as conn:
            conn.executescript(
                """
                CREATE TABLE watchlists (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    item_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    max_price REAL NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(user_id, normalized_name)
                );
                INSERT INTO watchlists
                    (id, user_id, item_name, normalized_name, max_price)
                VALUES (7, 999, 'Pikachu-V', 'pikachu-v', 50);
                INSERT INTO watchlists
                    (id, user_id, item_name, normalized_name, max_price,
                     updated_at)
                VALUES (8, 999, 'Pikachu V', 'pikachu v', 40,
                        '2000-01-01 00:00:00');
                CREATE TABLE watchlist_deliveries (
                    watch_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    delivered_at TEXT,
                    PRIMARY KEY (watch_id, item_id)
                );
                INSERT INTO watchlist_deliveries
                    (watch_id, item_id, delivered_at)
                VALUES (7, 'old-listing', CURRENT_TIMESTAMP);
                """
            )
        migrated = WatchlistStore(legacy_path)
        self.assertTrue(migrated.was_delivered(999, "old-listing"))
        watches = migrated.list_watches()
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0].normalized_name, "pikachu v")
        # Normalization collisions use the most recently updated watch.
        self.assertEqual(watches[0].max_price, 40)
        queued = migrated.enqueue_matches(
            [{
                "item_id": "new-listing",
                "title": "Pikachu-V Full Art",
                "price": 30,
                "shipping": 0,
                "url": "https://example.com/new",
            }]
        )
        self.assertEqual(queued, 1)

    def test_initial_digest_claim_is_three_cheapest_and_pacing_is_per_user(self):
        self.store.upsert_watch(123, "Pikachu V", 100)
        self.store.upsert_watch(456, "Mew V", 100)
        pikachu = self.store.list_watches_for_user(123)[0]
        mew = self.store.list_watches_for_user(456)[0]
        self.store.enqueue_watch_matches(pikachu, [
            {"item_id": f"p-{price}", "title": "Pikachu V", "price": price,
             "shipping": 0, "url": f"https://example.com/p-{price}"}
            for price in (30, 10, 20, 40)
        ], limit=3, initial=True)
        self.store.enqueue_watch_matches(mew, [{
            "item_id": "m-1", "title": "Mew V", "price": 5, "shipping": 0,
            "url": "https://example.com/m-1",
        }])
        batches = self.store.claim_digest_batches(interval=1000)
        self.assertEqual(
            [entry.item_id for entry in next(batch for batch in batches
                                             if batch[0].user_id == 123)],
            ["p-10", "p-20", "p-30"],
        )
        # User 123 is paced after its initial digest, while other users were
        # fairly claimable in the same transaction.
        self.assertTrue(any(batch[0].user_id == 456 for batch in batches))
        self.assertEqual(self.store.claim_digest_batches(interval=1000), [])

    def test_initial_snapshot_of_eighteen_drains_three_then_five_without_starvation(self):
        self.store.upsert_watch(777, "Charizard V", 100)
        watch = self.store.list_watches_for_user(777)[0]
        listings = [
            {"item_id": f"snapshot-{price:02}", "title": "Charizard V",
             "price": price, "shipping": 0,
             "url": f"https://example.com/snapshot-{price:02}"}
            for price in range(18, 0, -1)
        ]
        self.assertEqual(
            self.store.enqueue_watch_matches(watch, listings, initial=True), 18
        )
        sizes = []
        seen = []
        for expected_size in (3, 5, 5, 5):
            batches = self.store.claim_digest_batches(interval=1000)
            batch = next(group for group in batches if group[0].user_id == 777)
            self.assertEqual(len(batch), expected_size)
            sizes.append(len(batch))
            seen.extend(entry.item_id for entry in batch)
            for entry in batch:
                self.store.complete(entry.user_id, entry.item_id)
            with self.store._connect() as conn:
                conn.execute(
                    "UPDATE watchlist_dm_pacing SET next_digest_at = 0 WHERE user_id = 777"
                )
            # Repeated source snapshots contain the already delivered/queued
            # cheapest records but cannot displace later pending records.
            self.assertEqual(self.store.enqueue_watch_matches(watch, listings), 0)
        self.assertEqual(sizes, [3, 5, 5, 5])
        self.assertEqual(len(set(seen)), 18)
        self.assertEqual(seen, [f"snapshot-{price:02}" for price in range(1, 19)])

    def test_durable_daily_cap_and_stale_search_job_update_are_safe(self):
        self.assertTrue(self.store.reserve_mercari_daily(24, 20))
        self.assertFalse(self.store.reserve_mercari_daily(24, 4 + 1))
        # The accounting is stored in SQLite rather than scheduler memory.
        reopened = WatchlistStore(self.db_path)
        self.assertEqual(reopened.mercari_daily_remaining(24), 4)

        self.store.upsert_watch(888, "Mew V", 50)
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE watch_targeted_search_jobs SET next_attempt = 0 "
                "WHERE user_id = 888 AND normalized_name = 'mew v'"
            )
        old_watch = self.store.claim_targeted_searches()[0]
        self.store.upsert_watch(888, "Mew V", 25)  # clears old job lease
        self.store.finish_targeted_search(old_watch, delay=0)
        with self.store._connect() as conn:
            row = conn.execute(
                """
                SELECT next_attempt, claim_token FROM watch_targeted_search_jobs
                WHERE user_id = 888 AND normalized_name = 'mew v'
                """
            ).fetchone()
        self.assertGreater(row["next_attempt"], 0)
        self.assertIsNone(row["claim_token"])
        self.assertEqual(self.store.claim_targeted_searches(), [])


class TargetedWatchSearchTests(unittest.TestCase):
    def test_cache_and_quota_keep_zero_results_distinct_from_unavailable(self):
        calls = []

        def search(source, query):
            calls.append((source, query["normalized_name"]))
            return {
                "source": source, "status": "ok", "listings": [],
                "checked": 7, "message": "", "requests": 1,
            }

        scheduler = TargetedWatchSearch(cache_ttl=1000, budget_window=1000)
        module = types.SimpleNamespace(search_watch_source=search)
        first = Watch(1, 1, "Pikachu V", "pikachu v", 50)
        second = Watch(2, 2, "Mew V", "mew v", 50)
        with patch.dict(sys.modules, {"watch_sources": module}):
            results = scheduler.search(first)
            cached = scheduler.search(first)
            exhausted = scheduler.search(second)
        self.assertEqual(results["ebay"]["status"], "ok")
        self.assertEqual(results["ebay"]["checked"], 7)
        self.assertEqual(cached["ebay"]["status"], "ok")
        self.assertEqual(len(calls), 3)  # each source shared from the TTL cache
        self.assertEqual(exhausted["ebay"]["status"], "unavailable")

    def test_mercari_daily_cap_reserves_adapter_maximum_before_calls(self):
        calls = []

        def search(source, _query):
            calls.append(source)
            return {
                "source": source, "status": "ok", "listings": [],
                "checked": 0, "message": "", "requests": 4,
            }

        scheduler = TargetedWatchSearch(cache_ttl=0, budget_window=1000)
        module = types.SimpleNamespace(search_watch_source=search)
        with patch.dict(sys.modules, {"watch_sources": module}):
            for index in range(MERCARI_DAILY_PERSONAL_REQUEST_CAP // 4):
                # Simulate an elapsed short source window without advancing a
                # day; every distinct query is still charged four requests.
                scheduler._window_started = 0
                result = scheduler._one_source("mercari", {"item_name": f"Card {index}"})
                self.assertEqual(result["status"], "ok")
            scheduler._window_started = 0
            exhausted = scheduler._one_source("mercari", {"item_name": "One more"})
        self.assertEqual(exhausted["status"], "unavailable")
        self.assertIn("daily personal-search quota", exhausted["message"])
        self.assertEqual(exhausted["mercari_daily_remaining"], 0)
        self.assertEqual(scheduler.remaining_limits()["mercari_daily"], 0)
        self.assertEqual(len(calls), MERCARI_DAILY_PERSONAL_REQUEST_CAP // 4)


class WatchlistGameValidationTests(unittest.TestCase):
    def test_recognizes_common_unsupported_games(self):
        self.assertEqual(
            _unsupported_game_name("Star Wars Unlimited booster display"),
            "Star Wars: Unlimited",
        )
        self.assertEqual(_unsupported_game_name("Digimon BT-20"), "Digimon")

    def test_allows_supported_games_and_item_only_searches(self):
        self.assertIsNone(_unsupported_game_name("Pokemon booster box"))
        self.assertIsNone(_unsupported_game_name("MTG Final Fantasy booster box"))
        self.assertIsNone(_unsupported_game_name("Pikachu V alternate art"))
        self.assertIsNone(_unsupported_game_name("Yu-Gi-Oh booster box"))
        self.assertIsNone(_unsupported_game_name("Yugioh LOB-001 PSA 10"))


class TCGCSVWatchCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = TCGCSVWatchCatalog()
        pokemon = CatalogSet(
            "pokemon", 101, "Base Set", "BS",
            (CatalogProduct("Pikachu V", "Holo Rare"),
             CatalogProduct("Mew V", "Ultra Rare")),
        )
        mtg = CatalogSet(
            "mtg", 202, "Example Expansion", "EX",
            (CatalogProduct("Pikachu V", "Mythic Rare"),),
        )
        no_pikachu = CatalogSet(
            "pokemon", 303, "Other Set", "OS",
            (CatalogProduct("Charizard", "Rare"),),
        )
        self.catalog._snapshots = {
            "pokemon": CatalogSnapshot((pokemon, no_pikachu), True),
            "mtg": CatalogSnapshot((mtg,), True),
            "lorcana": CatalogSnapshot((), True),
            "onepiece": CatalogSnapshot((), True),
            "yugioh": CatalogSnapshot((), True),
        }
        self.catalog._until = {
            game: float("inf") for game in self.catalog._snapshots
        }

    def test_set_results_are_card_scoped_not_all_game_sets(self):
        self.assertEqual(
            [(entry.game, entry.name) for entry in self.catalog.matching_sets("Pikachu V")],
            [("mtg", "Example Expansion"), ("pokemon", "Base Set")],
        )
        self.assertNotIn(
            "Other Set",
            [entry.name for entry in self.catalog.matching_sets("Pikachu V")],
        )

    def test_rarity_is_scoped_to_card_and_selected_set_and_validation_is_strict(self):
        self.assertEqual(
            self.catalog.rarities("Pikachu V", "pokemon:101"), ["Holo Rare"]
        )
        selected, rarity, error = self.catalog.validate(
            "Pikachu V", "pokemon", "pokemon:101", "Holo Rare"
        )
        self.assertEqual((selected.name, rarity, error), ("Base Set", "Holo Rare", None))
        self.assertEqual(
            self.catalog.validate(
                "Pikachu V", "pokemon", "pokemon:303", "Rare"
            )[2],
            "That set does not contain the entered card name.",
        )
        self.assertIn(
            "not available",
            self.catalog.validate(
                "Pikachu V", "pokemon", "pokemon:101", "Made Up Rare"
            )[2],
        )

    def test_card_names_use_whole_normalized_words(self):
        self.catalog._snapshots["pokemon"] = CatalogSnapshot((
            CatalogSet(
                "pokemon", 404, "Mewtwo Set", "MS",
                (CatalogProduct("Mewtwo V", "Rare"),),
            ),
        ), True)
        self.assertEqual(self.catalog.matching_sets("Mew", "pokemon"), [])
        self.assertEqual(self.catalog.rarities("Mew", "pokemon:404"), [])
        self.assertEqual(
            self.catalog.matching_sets("Mewtwo", "pokemon")[0].name,
            "Mewtwo Set",
        )

    def test_only_catalog_proven_non_yugioh_unique_codes_are_marked_safe(self):
        groups = ({"groupId": 1, "name": "Example", "abbreviation": "ABC"},)
        products = {1: (CatalogProduct("Example Card", "Rare"),)}
        expiry = {1: float("inf")}
        mtg = self.catalog._snapshot_from_products("mtg", groups, products, expiry)
        ygo = self.catalog._snapshot_from_products("yugioh", groups, products, expiry)
        self.assertTrue(mtg.sets[0].abbreviation_unique)
        self.assertFalse(ygo.sets[0].abbreviation_unique)

    def test_rarity_codes_are_readable_and_match_only_as_full_tokens(self):
        self.assertEqual(canonical_rarity("mtg", "M"), "Mythic Rare")
        self.assertEqual(canonical_rarity("onepiece", "SEC"), "Secret Rare")
        self.assertEqual(canonical_rarity("onepiece", "UC"), "Uncommon")
        self.assertTrue(rarity_matches_title(
            "mtg", "Mythic Rare", "Black Lotus (M)"
        ))
        self.assertTrue(rarity_matches_title(
            "onepiece", "Secret Rare", "Monkey D. Luffy SEC"
        ))
        self.assertFalse(rarity_matches_title(
            "mtg", "Mythic Rare", "Mewtwo V Rare"
        ))

    def test_unique_non_yugioh_set_code_is_allowed_but_yugioh_code_is_not(self):
        item = {
            "title": "Magic Example Card ABC Mythic Rare",
            "game_name": "mtg",
        }
        mtg_watch = Watch(
            1, 1, "Example Card", "example card", 50,
            "mtg", "Long Set Name", "ABC", "Mythic Rare",
        )
        self.assertTrue(_listing_matches_watch_filters(
            mtg_watch, item, item["title"]
        ))
        ygo_watch = Watch(
            1, 1, "Example Card", "example card", 50,
            "yugioh", "Long Set Name", "ABC", "Mythic Rare",
        )
        ygo_item = {**item, "game_name": "yugioh"}
        self.assertFalse(_listing_matches_watch_filters(
            ygo_watch, ygo_item, ygo_item["title"]
        ))

    def test_partial_build_keeps_completed_groups_and_advances_next_batch(self):
        calls = []

        class Response:
            def __init__(self, body):
                self.body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self.body

        def fake_get(url, **_kwargs):
            calls.append(url)
            if url.endswith("/groups"):
                return Response({"results": [
                    {"groupId": 1, "name": "First Set", "abbreviation": "FS"},
                    {"groupId": 2, "name": "Second Set", "abbreviation": "SS"},
                ]})
            group_id = url.split("/")[-2]
            return Response({"results": [{
                "name": f"Pikachu V {group_id}",
                "extendedData": [
                    {"name": "Number", "value": "1"},
                    {"name": "Rarity", "value": "Rare"},
                ],
            }]})

        catalog = TCGCSVWatchCatalog()
        with patch.object(tcgcsv_catalog, "CATALOG_BATCH_SIZE", 1), patch(
            "tcgcsv_catalog.requests.get", side_effect=fake_get
        ):
            self.assertTrue(catalog.ensure_game("pokemon"))
            self.assertFalse(catalog._snapshots["pokemon"].complete)
            self.assertTrue(catalog.ensure_game("pokemon"))

        product_calls = [url for url in calls if url.endswith("/products")]
        self.assertEqual(
            product_calls,
            [
                "https://tcgcsv.com/tcgplayer/3/1/products",
                "https://tcgcsv.com/tcgplayer/3/2/products",
            ],
        )
        self.assertEqual(calls.count("https://tcgcsv.com/tcgplayer/3/groups"), 1)
        self.assertTrue(catalog._snapshots["pokemon"].complete)
        self.assertEqual(
            [entry.name for entry in catalog.matching_sets("Pikachu V", "pokemon")],
            ["First Set", "Second Set"],
        )

    def test_transient_group_failure_retries_without_redownloading_successes(self):
        calls = []
        attempts = 0

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": [{
                    "name": "Exodia the Forbidden One",
                    "extendedData": [
                        {"name": "Number", "value": "LOB-124"},
                        {"name": "Rarity", "value": "Ultra Rare"},
                    ],
                }]}

        def fake_get(url, **_kwargs):
            nonlocal attempts
            calls.append(url)
            if url.endswith("/groups"):
                return type("Groups", (), {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {"results": [{
                        "groupId": 1, "name": "Legend of Blue Eyes", "abbreviation": "LOB",
                    }]},
                })()
            attempts += 1
            if attempts == 1:
                raise tcgcsv_catalog.requests.RequestException("temporary outage")
            return Response()

        catalog = TCGCSVWatchCatalog()
        with patch("tcgcsv_catalog.requests.get", side_effect=fake_get):
            self.assertFalse(catalog.ensure_game("yugioh"))
            # Expire this test's bounded retry window rather than sleeping.
            catalog._failed_until["yugioh"][1] = 0
            catalog._next_batch_at["yugioh"] = 0
            self.assertTrue(catalog.ensure_game("yugioh"))

        self.assertEqual(
            calls.count("https://tcgcsv.com/tcgplayer/2/groups"), 1
        )
        self.assertEqual(
            calls.count("https://tcgcsv.com/tcgplayer/2/1/products"), 2
        )
        self.assertEqual(
            [entry.name for entry in catalog.matching_sets(
                "Exodia the Forbidden One", "yugioh"
            )],
            ["Legend of Blue Eyes"],
        )


class WatchlistMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = WatchlistBot(
            token="test-token",
            db_path=str(Path(self.tmp.name) / "watchlists.db"),
        )
        self.bot.store.upsert_watch(123, "Pikachu V", 30)
        self.sent = []

        async def fake_send(watch, item, total):
            self.sent.append((watch.user_id, item["item_id"], total))
            return True

        self.bot._send_watch_dm = fake_send

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_matches_case_insensitively_at_max_total_and_dedups(self):
        item = {
            "item_id": "ebay-1",
            "title": "Pokemon PIKACHU V Full Art",
            "price": 25,
            "shipping": 5,
            "url": "https://example.com/item",
            "store": "eBay",
        }
        await self.bot._process_batch([item])
        await self.bot._process_batch([item])
        self.assertEqual(self.sent, [(123, "ebay-1", 30)])

    async def test_does_not_match_over_price_or_partial_word_sequence(self):
        self.bot.store.upsert_watch(456, "ex", 100)
        await self.bot._process_batch(
            [
                {
                    "item_id": "over-price",
                    "title": "Pikachu V Full Art",
                    "price": 30,
                    "shipping": 0.01,
                    "url": "https://example.com/over",
                },
                {
                    "item_id": "wrong-title",
                    "title": "Pikachu collectible box",
                    "price": 10,
                    "shipping": 0,
                    "url": "https://example.com/wrong",
                },
            ]
        )
        self.assertEqual(self.sent, [])

    async def test_punctuation_matches_and_overlapping_watches_send_once(self):
        self.bot.store.upsert_watch(123, "Pikachu", 40)
        item = {
            "item_id": "punctuation",
            "title": "Pikachu-V Full Art",
            "price": 20,
            "shipping": 0,
            "url": "https://example.com/punctuation",
        }
        await self.bot._process_batch([item])
        self.assertEqual(self.sent, [(123, "punctuation", 20)])

    @patch("watchlist_bot.get_sold_comps")
    async def test_personal_dm_enrichment_has_one_average_and_one_detail(
        self, lookup
    ):
        lookup.return_value = {
            "average": 280,
            "median": 275,
            "count": 3,
            "sales": [
                {"total": 270, "url": "https://example.com/1"},
                {"total": 275, "url": "https://example.com/2"},
                {"total": 295, "url": "https://example.com/3"},
            ],
        }
        message = AsyncMock()
        embed = discord.Embed(title="Watchlist match")
        await self.bot._enrich_watch_dm(
            message,
            embed,
            {
                "title": "Surging Sparks Booster Box",
                "language": "English",
                "sealed": True,
            },
        )
        names = [field["name"] for field in embed.to_dict()["fields"]]
        self.assertEqual(names.count("Recent eBay Avg Sold"), 1)
        self.assertEqual(names.count("Recent eBay Sold Comps"), 1)
        self.assertIn("$280.00", embed.to_dict()["fields"][0]["value"])
        message.edit.assert_awaited_once_with(embed=embed)

    async def test_submission_persists_while_bot_is_not_ready(self):
        item = {
            "item_id": "offline",
            "title": "Pikachu V Alternate Art",
            "price": 20,
            "shipping": 0,
            "url": "https://example.com/offline",
        }
        self.bot.submit_listings([item])
        self.assertEqual(self.bot.store.pending_count(), 1)
        await self.bot._drain_pending_once()
        self.assertEqual(self.sent, [(123, "offline", 20)])
        self.assertEqual(self.bot.store.pending_count(), 0)

    async def test_initial_targeted_check_reports_verified_counts_and_queues_cheapest(self):
        watch = self.bot.store.list_watches_for_user(123)[0]
        results = {
            "ebay": {
                "source": "ebay", "status": "ok", "checked": 4, "requests": 2,
                "remaining_requests": 0, "listings": [
                    {"item_id": "ebay-high", "title": "Pikachu V", "price": 28,
                     "shipping": 0, "url": "https://example.com/high"},
                    {"item_id": "ebay-low", "title": "Pikachu V", "price": 10,
                     "shipping": 0, "url": "https://example.com/low"},
                ], "message": "",
            },
            "mercari": {
                "source": "mercari", "status": "unavailable", "checked": 0,
                "requests": 0, "remaining_requests": 4,
                "mercari_daily_remaining": 0, "listings": [],
                "message": "Mercari daily personal-search quota is exhausted",
            },
            "tcgplayer": {
                "source": "tcgplayer", "status": "partial", "checked": 2,
                "requests": 1, "remaining_requests": 0, "listings": [
                    {"item_id": "tcg-mid", "title": "Pikachu V", "price": 20,
                     "shipping": 0, "url": "https://example.com/mid"},
                ], "message": "",
            },
        }
        with patch.object(self.bot.search_scheduler, "search", return_value=results):
            report = await self.bot._initial_targeted_check(watch)
        self.assertIn("eBay: **2** verified match(es) among 4 checked", report)
        self.assertIn("TCGplayer: **1** verified match(es) among 2 checked", report)
        self.assertIn("Mercari: **unavailable**", report)
        claimed = self.bot.store.claim_digest_batches()
        self.assertEqual(
            [entry.item_id for entry in claimed[0]], ["ebay-low", "tcg-mid", "ebay-high"]
        )

    async def test_watch_command_defers_and_returns_mocked_initial_count(self):
        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=987),
            response=types.SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )
        self.bot._initial_targeted_check = AsyncMock(
            return_value="**Initial targeted check** — eBay: **2** verified match(es) among 4 checked"
        )
        command = self.bot.tree.get_command("watch")
        await command.callback(interaction, "Mew V", 25.0, None, None, None)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertIn(
            "eBay: **2** verified match(es) among 4 checked",
            interaction.followup.send.await_args.args[0],
        )
        self.assertEqual(
            [(entry.item_name, entry.max_price) for entry in self.bot.store.list_watches_for_user(987)],
            [("Mew V", 25.0)],
        )

    async def test_initial_all_source_failures_do_not_claim_a_zero_match(self):
        watch = self.bot.store.list_watches_for_user(123)[0]
        results = {
            source: {
                "source": source, "status": "unavailable", "checked": 0,
                "requests": 0, "listings": [], "message": "quota deferred",
            }
            for source in ("ebay", "mercari", "tcgplayer")
        }
        with patch.object(self.bot.search_scheduler, "search", return_value=results):
            report = await self.bot._initial_targeted_check(watch)
        self.assertIn("no zero-match claim was made", report)
        self.assertEqual(self.bot.store.pending_count(), 0)

    async def test_digest_sold_comps_edits_complete_sibling_embed_list(self):
        user = AsyncMock()
        message = AsyncMock()
        user.send.return_value = message
        self.bot.fetch_user = AsyncMock(return_value=user)
        entries = [
            PendingDM(123, "digest-1", "Pikachu V", 30, {
                "item_id": "digest-1", "title": "Pikachu V", "price": 20,
                "shipping": 0, "url": "https://example.com/digest-1",
            }, 0),
            PendingDM(123, "digest-2", "Pikachu V", 30, {
                "item_id": "digest-2", "title": "Pikachu V Alt", "price": 21,
                "shipping": 0, "url": "https://example.com/digest-2",
            }, 0),
        ]
        comps = {"average": 22, "median": 22, "count": 2, "sales": [
            {"total": 20, "url": "https://example.com/sale-1"},
            {"total": 24, "url": "https://example.com/sale-2"},
        ]}
        with patch("watchlist_bot.get_sold_comps", return_value=comps):
            self.assertEqual(await self.bot._send_watch_digest(entries), "sent")
            await asyncio.sleep(0)
            await asyncio.gather(*list(self.bot._enrichment_tasks))
        sent_embeds = user.send.await_args.kwargs["embeds"]
        self.assertEqual(len(sent_embeds), 2)
        # Every enrichment edit carries both embeds, so a sibling is never
        # edited away by another async sold-comps task.
        self.assertEqual(message.edit.await_count, 2)
        self.assertTrue(all(
            len(call.kwargs["embeds"]) == 2 for call in message.edit.await_args_list
        ))


if __name__ == "__main__":
    unittest.main()