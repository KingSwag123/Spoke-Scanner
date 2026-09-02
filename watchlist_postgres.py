"""Persistent PostgreSQL storage for Discord personal watchlists."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

from watchlist_bot import PendingDM, Watch, WatchlistStore, _normalize_term


class PostgresWatchlistStore:
    def __init__(self):
        self.claim_token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM watchlists LIMIT 1")

    @staticmethod
    def _connect():
        return psycopg.connect(row_factory=dict_row, connect_timeout=10)

    def upsert_watch(self, user_id: int, item_name: str, max_price: float) -> None:
        normalized = _normalize_term(item_name)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlists
                    (user_id, item_name, normalized_name, max_price)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, normalized_name) DO UPDATE SET
                    item_name = EXCLUDED.item_name,
                    max_price = EXCLUDED.max_price,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, item_name.strip(), normalized, max_price),
            )

    @staticmethod
    def _to_watches(rows) -> list[Watch]:
        return [
            Watch(
                id=row["id"],
                user_id=row["user_id"],
                item_name=row["item_name"],
                normalized_name=row["normalized_name"],
                max_price=float(row["max_price"]),
            )
            for row in rows
        ]

    def list_watches(self) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, user_id, item_name, normalized_name, max_price "
                "FROM watchlists"
            ).fetchall()
        return self._to_watches(rows)

    def list_watches_for_user(self, user_id: int) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, item_name, normalized_name, max_price
                FROM watchlists
                WHERE user_id = %s
                ORDER BY normalized_name
                """,
                (user_id,),
            ).fetchall()
        return self._to_watches(rows)

    def delete_watch(self, user_id: int, item_name: str) -> str | None:
        normalized = _normalize_term(item_name)
        with self._connect() as conn:
            row = conn.execute(
                """
                DELETE FROM watchlists
                WHERE user_id = %s AND normalized_name = %s
                RETURNING item_name
                """,
                (user_id, normalized),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM pending_watch_dms "
                "WHERE user_id = %s AND normalized_name = %s",
                (user_id, normalized),
            )
        return str(row["item_name"])

    def was_delivered(self, user_id: int, item_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM watchlist_deliveries "
                "WHERE user_id = %s AND item_id = %s",
                (user_id, item_id),
            ).fetchone()
        return row is not None

    def mark_delivered(self, user_id: int, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO watchlist_deliveries (user_id, item_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, item_id),
            )

    def enqueue_matches(self, listings: Iterable[dict]) -> int:
        watches = self.list_watches()
        if not watches:
            return 0
        queued = 0
        with self._connect() as conn:
            for item in listings:
                item_id = str(item.get("item_id") or "")
                title = str(item.get("title") or "")
                url = str(item.get("url") or "")
                try:
                    total = float(item.get("price", 0)) + float(
                        item.get("shipping", 0)
                    )
                except (TypeError, ValueError):
                    continue
                if not item_id or not title or not url or total <= 0:
                    continue
                bounded_title = f" {_normalize_term(title)} "
                matching_by_user: dict[int, Watch] = {}
                for watch in watches:
                    if (f" {watch.normalized_name} " not in bounded_title
                            or total > watch.max_price):
                        continue
                    previous = matching_by_user.get(watch.user_id)
                    if (previous is None
                            or len(watch.normalized_name)
                            > len(previous.normalized_name)):
                        matching_by_user[watch.user_id] = watch
                for watch in matching_by_user.values():
                    conn.execute(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(%s, 0))",
                        (f"{watch.user_id}:{item_id}",),
                    )
                    result = conn.execute(
                        """
                        INSERT INTO pending_watch_dms
                            (user_id, item_id, item_name, normalized_name,
                             max_price, payload)
                        SELECT %s, %s, %s, %s, %s, %s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM watchlist_deliveries
                            WHERE user_id = %s AND item_id = %s
                        )
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            watch.user_id,
                            item_id,
                            watch.item_name,
                            watch.normalized_name,
                            watch.max_price,
                            json.dumps(item, ensure_ascii=False),
                            watch.user_id,
                            item_id,
                        ),
                    )
                    queued += result.rowcount
        return queued

    def pending(self, limit: int = 100) -> list[PendingDM]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH ready AS (
                    SELECT user_id, item_id
                    FROM pending_watch_dms
                    WHERE next_attempt <= CURRENT_TIMESTAMP
                      AND (
                          in_flight_until IS NULL
                          OR in_flight_until <= CURRENT_TIMESTAMP
                      )
                    ORDER BY created_at, user_id, item_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE pending_watch_dms AS pending
                SET claim_token = %s,
                    in_flight_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
                FROM ready
                WHERE pending.user_id = ready.user_id
                  AND pending.item_id = ready.item_id
                RETURNING pending.user_id, pending.item_id, pending.item_name,
                          pending.max_price, pending.payload, pending.attempts
                """,
                (limit, self.claim_token),
            ).fetchall()
        pending: list[PendingDM] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                self.complete(row["user_id"], row["item_id"])
                continue
            pending.append(
                PendingDM(
                    user_id=row["user_id"],
                    item_id=row["item_id"],
                    item_name=row["item_name"],
                    max_price=float(row["max_price"]),
                    payload=payload,
                    attempts=row["attempts"],
                )
            )
        return pending

    def complete(self, user_id: int, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{user_id}:{item_id}",),
            )
            owned = conn.execute(
                """
                SELECT 1 FROM pending_watch_dms
                WHERE user_id = %s AND item_id = %s AND claim_token = %s
                """,
                (user_id, item_id, self.claim_token),
            ).fetchone()
            if owned is None:
                return
            conn.execute(
                "INSERT INTO watchlist_deliveries (user_id, item_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, item_id),
            )
            conn.execute(
                "DELETE FROM pending_watch_dms "
                "WHERE user_id = %s AND item_id = %s AND claim_token = %s",
                (user_id, item_id, self.claim_token),
            )

    def retry_later(self, user_id: int, item_id: str, attempts: int) -> None:
        delay = min(900, 30 * (2 ** min(attempts, 5)))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pending_watch_dms
                SET attempts = attempts + 1,
                    next_attempt = CURRENT_TIMESTAMP
                        + (%s * INTERVAL '1 second'),
                    claim_token = NULL,
                    in_flight_until = NULL
                WHERE user_id = %s AND item_id = %s AND claim_token = %s
                """,
                (delay, user_id, item_id, self.claim_token),
            )

    def pending_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM pending_watch_dms"
            ).fetchone()
        return int(row["count"])

    def import_sqlite(self, db_path: str) -> tuple[int, int, int]:
        """Import legacy data if the deployment's SQLite file still exists."""
        if not os.path.isfile(db_path):
            return (0, 0, 0)
        source = WatchlistStore(db_path)
        watches = source.list_watches()
        with sqlite3.connect(db_path) as old:
            old.row_factory = sqlite3.Row
            deliveries = old.execute(
                "SELECT user_id, item_id, delivered_at "
                "FROM watchlist_deliveries"
            ).fetchall()
            pending = old.execute(
                """
                SELECT user_id, item_id, item_name, max_price, payload,
                       attempts, next_attempt, created_at
                FROM pending_watch_dms
                """
            ).fetchall()
        imported_watches = 0
        imported_deliveries = 0
        imported_pending = 0
        with self._connect() as conn:
            for watch in watches:
                result = conn.execute(
                    """
                    INSERT INTO watchlists
                        (user_id, item_name, normalized_name, max_price)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id, normalized_name) DO NOTHING
                    """,
                    (
                        watch.user_id,
                        watch.item_name,
                        watch.normalized_name,
                        watch.max_price,
                    ),
                )
                imported_watches += result.rowcount
            for delivery in deliveries:
                result = conn.execute(
                    """
                    INSERT INTO watchlist_deliveries
                        (user_id, item_id, delivered_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        delivery["user_id"],
                        delivery["item_id"],
                        delivery["delivered_at"],
                    ),
                )
                imported_deliveries += result.rowcount
            for alert in pending:
                normalized = _normalize_term(alert["item_name"])
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{alert['user_id']}:{alert['item_id']}",),
                )
                result = conn.execute(
                    """
                    INSERT INTO pending_watch_dms
                        (user_id, item_id, item_name, normalized_name,
                         max_price, payload, attempts, next_attempt, created_at)
                    SELECT %s, %s, %s, %s, %s, %s, %s,
                           to_timestamp(%s), %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM watchlist_deliveries
                        WHERE user_id = %s AND item_id = %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        alert["user_id"],
                        alert["item_id"],
                        alert["item_name"],
                        normalized,
                        alert["max_price"],
                        alert["payload"],
                        alert["attempts"],
                        alert["next_attempt"],
                        alert["created_at"],
                        alert["user_id"],
                        alert["item_id"],
                    ),
                )
                imported_pending += result.rowcount
        return imported_watches, imported_deliveries, imported_pending