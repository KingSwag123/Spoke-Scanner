import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg

from watchlist_bot import WatchlistStore
from watchlist_postgres import PostgresWatchlistStore


@unittest.skipUnless(
    os.environ.get("PGHOST") or os.environ.get("DATABASE_URL"),
    "PostgreSQL is not attached",
)
class PostgresWatchlistStoreTests(unittest.TestCase):
    def setUp(self):
        self.user_id = 800_000_000_000_000_000 + (
            uuid.uuid4().int % 10_000_000_000
        )
        self.store = PostgresWatchlistStore()

    def tearDown(self):
        with psycopg.connect() as conn:
            conn.execute(
                "DELETE FROM pending_watch_dms WHERE user_id = %s",
                (self.user_id,),
            )
            conn.execute(
                "DELETE FROM watchlist_deliveries WHERE user_id = %s",
                (self.user_id,),
            )
            conn.execute(
                "DELETE FROM watchlists WHERE user_id = %s",
                (self.user_id,),
            )

    @staticmethod
    def listing(item_id: str) -> dict:
        return {
            "item_id": item_id,
            "title": "Persistent Pikachu V listing",
            "price": 20,
            "shipping": 2,
            "url": "https://example.com/persistent",
        }

    def test_watch_survives_new_store_instance(self):
        self.store.upsert_watch(self.user_id, "Pikachu V", 30)
        reopened = PostgresWatchlistStore()
        watches = reopened.list_watches_for_user(self.user_id)
        self.assertEqual([(w.item_name, w.max_price) for w in watches], [
            ("Pikachu V", 30),
        ])

    def test_filtered_watch_round_trips_and_pending_alert_keeps_filters(self):
        self.store.upsert_watch(
            self.user_id,
            "Pikachu V",
            30,
            "pokemon",
            "Base Set",
            "BS",
            "Holo Rare",
        )
        watch = self.store.list_watches_for_user(self.user_id)[0]
        self.assertEqual(
            (watch.game, watch.set_name, watch.set_code, watch.rarity),
            ("pokemon", "Base Set", "BS", "Holo Rare"),
        )
        self.store.enqueue_matches([
            {
                "item_id": f"wrong-filter-{self.user_id}",
                "title": "Pokemon Pikachu V Base Set Ultra Rare",
                "game_name": "pokemon",
                "price": 20,
                "shipping": 2,
                "url": "https://example.com/wrong-filter",
            },
            {
                "item_id": f"right-filter-{self.user_id}",
                "title": "Pokemon Pikachu V Base Set Holo Rare",
                "game_name": "pokemon",
                "price": 20,
                "shipping": 2,
                "url": "https://example.com/right-filter",
            },
        ])
        pending = self.store.pending(1000)
        matching = [
            alert for alert in pending
            if alert.user_id == self.user_id
            and alert.item_id == f"right-filter-{self.user_id}"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            (matching[0].game, matching[0].set_name,
             matching[0].set_code, matching[0].rarity),
            ("pokemon", "Base Set", "BS", "Holo Rare"),
        )
        self.assertFalse(any(
            alert.user_id == self.user_id
            and alert.item_id == f"wrong-filter-{self.user_id}"
            for alert in pending
        ))

    def test_updating_phrase_rechecks_its_pending_alert_transactionally(self):
        item_id = f"replace-filter-{self.user_id}"
        listing = {
            "item_id": item_id,
            "title": "Pokemon Pikachu V Base Set Holo Rare",
            "game_name": "pokemon",
            "price": 20,
            "shipping": 2,
            "url": "https://example.com/replace-filter",
        }
        self.store.upsert_watch(self.user_id, "Pikachu V", 30)
        self.store.enqueue_matches([listing])
        stale_alert = next(
            alert for alert in self.store.pending(1000)
            if alert.user_id == self.user_id and alert.item_id == item_id
        )
        self.store.upsert_watch(
            self.user_id, "Pikachu V", 30, "pokemon", "Base Set",
            None, "Ultra Rare",
        )
        self.assertFalse(any(
            alert.user_id == self.user_id and alert.item_id == item_id
            for alert in self.store.pending(1000)
        ))
        self.assertFalse(self.store.pending_is_current(stale_alert))

    def test_enqueue_and_complete_are_race_safe(self):
        item_id = f"race-{self.user_id}"
        item = self.listing(item_id)
        self.store.upsert_watch(self.user_id, "Pikachu V", 30)
        self.store.enqueue_matches([item])
        claimed = self.store.pending(1000)
        self.assertTrue(any(alert.item_id == item_id for alert in claimed))

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(self.store.complete, self.user_id, item_id)]
            futures.extend(
                pool.submit(self.store.enqueue_matches, [item])
                for _ in range(20)
            )
            for future in futures:
                future.result()

        self.assertTrue(self.store.was_delivered(self.user_id, item_id))
        self.assertFalse(
            any(
                pending.user_id == self.user_id and pending.item_id == item_id
                for pending in self.store.pending(1000)
            )
        )

    def test_two_workers_only_one_claims_pending_alert(self):
        item_id = f"claim-{self.user_id}"
        self.store.upsert_watch(self.user_id, "Pikachu V", 30)
        self.store.enqueue_matches([self.listing(item_id)])
        other_worker = PostgresWatchlistStore()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.store.pending, 1000)
            second = pool.submit(other_worker.pending, 1000)
            claimed = first.result() + second.result()

        matching = [
            alert for alert in claimed
            if alert.user_id == self.user_id and alert.item_id == item_id
        ]
        self.assertEqual(len(matching), 1)

    def test_expired_claim_is_recovered_by_another_worker(self):
        item_id = f"lease-{self.user_id}"
        self.store.upsert_watch(self.user_id, "Pikachu V", 30)
        self.store.enqueue_matches([self.listing(item_id)])
        claimed = self.store.pending(1000)
        self.assertTrue(any(alert.item_id == item_id for alert in claimed))
        with psycopg.connect() as conn:
            conn.execute(
                """
                UPDATE pending_watch_dms
                SET in_flight_until = CURRENT_TIMESTAMP - INTERVAL '1 second'
                WHERE user_id = %s AND item_id = %s
                """,
                (self.user_id, item_id),
            )
        recovered = PostgresWatchlistStore().pending(1000)
        self.assertTrue(
            any(
                alert.user_id == self.user_id and alert.item_id == item_id
                for alert in recovered
            )
        )

    def test_imports_legacy_watches_deliveries_and_pending_alerts(self):
        delivered_id = f"delivered-{self.user_id}"
        pending_id = f"pending-{self.user_id}"
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "watchlists.db")
            old = WatchlistStore(path)
            old.upsert_watch(self.user_id, "Pikachu V", 30)
            old.mark_delivered(self.user_id, delivered_id)
            old.enqueue_matches([self.listing(pending_id)])

            watches, deliveries, pending = self.store.import_sqlite(path)

        self.assertEqual((watches, deliveries, pending), (1, 1, 1))
        self.assertTrue(self.store.was_delivered(self.user_id, delivered_id))
        self.assertEqual(len(self.store.list_watches_for_user(self.user_id)), 1)
        self.assertTrue(
            any(
                alert.user_id == self.user_id and alert.item_id == pending_id
                for alert in self.store.pending(1000)
            )
        )