"""Persistent PostgreSQL storage for Discord personal watchlists."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

from watchlist_bot import (
    PendingDM,
    Watch,
    WatchlistStore,
    _listing_matches_watch_filters,
    _normalize_term,
    _watch_accepts_listing,
)


class PostgresWatchlistStore:
    def __init__(self):
        self.claim_token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM watchlists LIMIT 1")

    @staticmethod
    def _connect():
        return psycopg.connect(row_factory=dict_row, connect_timeout=10)

    def upsert_watch(
        self, user_id: int, item_name: str, max_price: float,
        game: str | None = None, set_name: str | None = None,
        set_code: str | None = None, rarity: str | None = None,
    ) -> None:
        normalized = _normalize_term(item_name)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlists
                    (user_id, item_name, normalized_name, max_price, game,
                     set_name, set_code, rarity)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, normalized_name) DO UPDATE SET
                    item_name = EXCLUDED.item_name,
                    max_price = EXCLUDED.max_price,
                    game = EXCLUDED.game,
                    set_name = EXCLUDED.set_name,
                    set_code = EXCLUDED.set_code,
                    rarity = EXCLUDED.rarity,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, item_name.strip(), normalized, max_price, game,
                 set_name, set_code, rarity),
            )
            # Replace/re-evaluate any queued alert for this phrase in the same
            # transaction as the upsert so an old filter cannot leak a DM.
            replacement = Watch(
                id=0, user_id=user_id, item_name=item_name.strip(),
                normalized_name=normalized, max_price=max_price, game=game,
                set_name=set_name, set_code=set_code, rarity=rarity,
            )
            pending = conn.execute(
                """
                DELETE FROM pending_watch_dms
                WHERE user_id = %s AND normalized_name = %s
                RETURNING item_id, payload
                """,
                (user_id, normalized),
            ).fetchall()
            for alert in pending:
                try:
                    item = json.loads(alert["payload"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not _watch_accepts_listing(replacement, item):
                    continue
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{user_id}:{alert['item_id']}",),
                )
                conn.execute(
                    """
                    INSERT INTO pending_watch_dms
                        (user_id, item_id, item_name, normalized_name, max_price,
                         game, set_name, set_code, rarity, payload, total_price)
                    SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM watchlist_deliveries
                        WHERE user_id = %s AND item_id = %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        user_id, alert["item_id"], replacement.item_name,
                        replacement.normalized_name, replacement.max_price,
                        replacement.game, replacement.set_name,
                        replacement.set_code, replacement.rarity,
                        json.dumps(item, ensure_ascii=False),
                        float(item.get("price", 0)) + float(item.get("shipping", 0)),
                        user_id, alert["item_id"],
                    ),
                )
            # /watch performs an immediate search, then this durable fair job
            # retries its watch on the next interval even across restarts.
            conn.execute(
                """
                INSERT INTO watch_targeted_search_jobs
                    (user_id, normalized_name, next_attempt, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP + INTERVAL '120 seconds',
                        CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, normalized_name) DO UPDATE SET
                    next_attempt = EXCLUDED.next_attempt,
                    claim_token = NULL,
                    in_flight_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, normalized),
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
                game=row["game"],
                set_name=row["set_name"],
                set_code=row["set_code"],
                rarity=row["rarity"],
            )
            for row in rows
        ]

    def list_watches(self) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, user_id, item_name, normalized_name, max_price, "
                "game, set_name, set_code, rarity "
                "FROM watchlists"
            ).fetchall()
        return self._to_watches(rows)

    def list_watches_for_user(self, user_id: int) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, item_name, normalized_name, max_price,
                       game, set_name, set_code, rarity
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
            conn.execute(
                "DELETE FROM watch_targeted_search_jobs "
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
                            or total > watch.max_price
                            or not _listing_matches_watch_filters(watch, item, title)):
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
                              max_price, game, set_name, set_code, rarity,
                               payload, total_price)
                        SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                            watch.game,
                            watch.set_name,
                            watch.set_code,
                            watch.rarity,
                            json.dumps(item, ensure_ascii=False),
                            total,
                            watch.user_id,
                            item_id,
                        ),
                    )
                    queued += result.rowcount
        return queued

    def enqueue_watch_matches(
        self, watch: Watch, listings: Iterable[dict], limit: int | None = None,
        initial: bool = False,
    ) -> int:
        candidates = [
            (float(item.get("price", 0)) + float(item.get("shipping", 0)), item)
            for item in listings if _watch_accepts_listing(watch, item)
        ]
        candidates.sort(key=lambda pair: (pair[0], str(pair[1].get("item_id") or "")))
        if limit is not None:
            candidates = candidates[:limit]
        queued = 0
        with self._connect() as conn:
            current = conn.execute(
                """
                SELECT item_name, max_price, game, set_name, set_code, rarity
                FROM watchlists
                WHERE user_id = %s AND normalized_name = %s
                FOR UPDATE
                """, (watch.user_id, watch.normalized_name),
            ).fetchone()
            if current is None or (
                current["item_name"], float(current["max_price"]), current["game"],
                current["set_name"], current["set_code"], current["rarity"],
            ) != (
                watch.item_name, float(watch.max_price), watch.game,
                watch.set_name, watch.set_code, watch.rarity,
            ):
                return 0
            for position, (total, item) in enumerate(candidates):
                item_id = str(item.get("item_id") or "")
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{watch.user_id}:{item_id}",),
                )
                result = conn.execute(
                    """
                    INSERT INTO pending_watch_dms
                        (user_id, item_id, item_name, normalized_name, max_price,
                         game, set_name, set_code, rarity, payload, total_price,
                         initial_batch)
                    SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM watchlist_deliveries
                        WHERE user_id = %s AND item_id = %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (watch.user_id, item_id, watch.item_name, watch.normalized_name,
                     watch.max_price, watch.game, watch.set_name, watch.set_code,
                     watch.rarity, json.dumps(item, ensure_ascii=False), total,
                     initial and position < 3,
                     watch.user_id, item_id),
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
                          pending.max_price, pending.game, pending.set_name,
                          pending.set_code, pending.rarity, pending.payload,
                          pending.attempts
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
                    game=row["game"],
                    set_name=row["set_name"],
                    set_code=row["set_code"],
                    rarity=row["rarity"],
                )
            )
        return pending

    def pending_is_current(self, alert: PendingDM) -> bool:
        """Confirm this worker still owns the unchanged queued alert."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT item_name, max_price, game, set_name, set_code, rarity
                FROM pending_watch_dms
                WHERE user_id = %s AND item_id = %s AND claim_token = %s
                """,
                (alert.user_id, alert.item_id, self.claim_token),
            ).fetchone()
        return row is not None and (
            row["item_name"], float(row["max_price"]), row["game"],
            row["set_name"], row["set_code"], row["rarity"],
        ) == (
            alert.item_name, float(alert.max_price), alert.game,
            alert.set_name, alert.set_code, alert.rarity,
        )

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

    def reserve_mercari_daily(self, cap: int, requests: int) -> bool:
        """Atomically reserve a metered personal-lane request allowance."""
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO watch_source_daily_budgets
                    (source, budget_day, used_requests)
                VALUES ('mercari', CURRENT_DATE, %s)
                ON CONFLICT (source) DO UPDATE SET
                    budget_day = CURRENT_DATE,
                    used_requests = CASE
                        WHEN watch_source_daily_budgets.budget_day = CURRENT_DATE
                        THEN watch_source_daily_budgets.used_requests
                             + EXCLUDED.used_requests
                        ELSE EXCLUDED.used_requests
                    END
                WHERE watch_source_daily_budgets.budget_day <> CURRENT_DATE
                   OR watch_source_daily_budgets.used_requests
                      + EXCLUDED.used_requests <= %s
                RETURNING used_requests
                """,
                (requests, cap),
            ).fetchone()
        return row is not None

    def mercari_daily_remaining(self, cap: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT CASE WHEN budget_day = CURRENT_DATE
                            THEN used_requests ELSE 0 END AS used_requests
                FROM watch_source_daily_budgets WHERE source = 'mercari'
                """
            ).fetchone()
        used = int(row["used_requests"]) if row else 0
        return max(0, cap - used)

    def claim_targeted_searches(self, limit: int = 1) -> list[Watch]:
        """Lock and lease due watch searches, preserving FIFO fairness."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT w.id, w.user_id, w.item_name, w.normalized_name, w.max_price,
                       w.game, w.set_name, w.set_code, w.rarity
                FROM watch_targeted_search_jobs job
                JOIN watchlists w ON w.user_id = job.user_id
                               AND w.normalized_name = job.normalized_name
                WHERE job.next_attempt <= CURRENT_TIMESTAMP
                  AND (job.in_flight_until IS NULL
                       OR job.in_flight_until <= CURRENT_TIMESTAMP)
                ORDER BY job.next_attempt, job.created_at, job.user_id, job.normalized_name
                FOR UPDATE OF job SKIP LOCKED
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE watch_targeted_search_jobs
                    SET claim_token = %s,
                        in_flight_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
                    WHERE user_id = %s AND normalized_name = %s
                    """,
                    (self.claim_token, row["user_id"], row["normalized_name"]),
                )
        return self._to_watches(rows)

    def finish_targeted_search(self, watch: Watch, delay: float = 120.0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE watch_targeted_search_jobs
                SET next_attempt = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    claim_token = NULL, in_flight_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND normalized_name = %s AND claim_token = %s
                """,
                (delay, watch.user_id, watch.normalized_name, self.claim_token),
            )

    def claim_digest_batches(
        self, user_limit: int = 20, per_user: int = 5, interval: float = 120.0,
    ) -> list[list[PendingDM]]:
        """Claim at most one paced, cheapest-first digest per user."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH candidate_users AS (
                    SELECT p.user_id
                    FROM pending_watch_dms p
                    LEFT JOIN watchlist_dm_pacing pace ON pace.user_id = p.user_id
                    WHERE p.next_attempt <= CURRENT_TIMESTAMP
                      AND (p.in_flight_until IS NULL
                           OR p.in_flight_until <= CURRENT_TIMESTAMP)
                      AND COALESCE(pace.next_digest_at, CURRENT_TIMESTAMP)
                          <= CURRENT_TIMESTAMP
                    GROUP BY p.user_id, pace.last_digest_at
                    ORDER BY COALESCE(pace.last_digest_at, to_timestamp(0)),
                             MIN(p.created_at), p.user_id
                    LIMIT %s
                ), eligible_users AS (
                    SELECT user_id
                    FROM candidate_users
                    WHERE pg_try_advisory_xact_lock(
                        hashtextextended('watch-digest:' || user_id::text, 0)
                    )
                    LIMIT %s
                ), ranked AS (
                    SELECT p.user_id, p.item_id, p.initial_batch,
                           BOOL_OR(p.initial_batch) OVER (
                               PARTITION BY p.user_id
                           ) AS has_initial,
                           ROW_NUMBER() OVER (
                               PARTITION BY p.user_id
                               ORDER BY p.initial_batch DESC, p.total_price,
                                        p.created_at, p.item_id
                           ) AS position
                    FROM pending_watch_dms p
                    JOIN eligible_users u ON u.user_id = p.user_id
                    WHERE p.next_attempt <= CURRENT_TIMESTAMP
                      AND (p.in_flight_until IS NULL
                           OR p.in_flight_until <= CURRENT_TIMESTAMP)
                ), claimed AS (
                    UPDATE pending_watch_dms p
                    SET claim_token = %s,
                        in_flight_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
                    FROM ranked r
                    WHERE p.user_id = r.user_id AND p.item_id = r.item_id
                      AND (NOT r.has_initial OR r.initial_batch)
                      AND r.position <= CASE WHEN r.has_initial THEN 3 ELSE %s END
                      AND (p.in_flight_until IS NULL
                           OR p.in_flight_until <= CURRENT_TIMESTAMP)
                      AND p.next_attempt <= CURRENT_TIMESTAMP
                    RETURNING p.user_id, p.item_id, p.item_name, p.max_price,
                              p.game, p.set_name, p.set_code, p.rarity,
                              p.payload, p.attempts, p.total_price
                ), paced AS (
                    INSERT INTO watchlist_dm_pacing
                        (user_id, next_digest_at, last_digest_at)
                    SELECT DISTINCT user_id,
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                        CURRENT_TIMESTAMP
                    FROM claimed
                    ON CONFLICT (user_id) DO UPDATE SET
                        next_digest_at = EXCLUDED.next_digest_at,
                        last_digest_at = EXCLUDED.last_digest_at
                    RETURNING user_id
                )
                SELECT * FROM claimed ORDER BY user_id, total_price, item_id
                """,
                (user_limit * 4, user_limit, self.claim_token, per_user, interval),
            ).fetchall()
        batches: dict[int, list[PendingDM]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                # This worker owns the corrupt item; leave completion to the
                # normal terminal path rather than retrying malformed payloads.
                self.complete(row["user_id"], row["item_id"])
                continue
            batches.setdefault(row["user_id"], []).append(PendingDM(
                user_id=row["user_id"], item_id=row["item_id"],
                item_name=row["item_name"], max_price=float(row["max_price"]),
                payload=payload, attempts=row["attempts"], game=row["game"],
                set_name=row["set_name"], set_code=row["set_code"],
                rarity=row["rarity"],
            ))
        return list(batches.values())

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
                SELECT user_id, item_id, item_name, max_price, game, set_name,
                       set_code, rarity, payload, attempts, next_attempt, created_at
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
                        (user_id, item_name, normalized_name, max_price, game,
                         set_name, set_code, rarity)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, normalized_name) DO NOTHING
                    """,
                    (
                        watch.user_id,
                        watch.item_name,
                        watch.normalized_name,
                        watch.max_price,
                        watch.game,
                        watch.set_name,
                        watch.set_code,
                        watch.rarity,
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
                             max_price, game, set_name, set_code, rarity,
                              payload, total_price, attempts, next_attempt, created_at)
                        SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, to_timestamp(%s), %s
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
                        alert["game"],
                        alert["set_name"],
                        alert["set_code"],
                        alert["rarity"],
                        alert["payload"],
                        0,
                        alert["attempts"],
                        alert["next_attempt"],
                        alert["created_at"],
                        alert["user_id"],
                        alert["item_id"],
                    ),
                )
                imported_pending += result.rowcount
        return imported_watches, imported_deliveries, imported_pending