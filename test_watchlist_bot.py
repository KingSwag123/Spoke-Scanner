import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from watchlist_bot import WatchlistBot, WatchlistStore


class WatchlistStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()