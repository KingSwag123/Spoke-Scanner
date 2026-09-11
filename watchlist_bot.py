"""Discord slash-command bot with persistent personal listing watchlists."""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import discord
import psycopg
from discord import app_commands
from discord.ext import commands

from sold_comps import format_sold_comps, get_sold_comps
from tcgcsv_catalog import (
    GAME_LABELS,
    GAMES,
    TCGCSVWatchCatalog,
    rarity_matches_title,
)
from watch_search import SOURCES, TargetedWatchSearch


_SUPPORTED_GAME_ALIASES = (
    "pokemon",
    "pokémon",
    "magic the gathering",
    "mtg",
    "lorcana",
    "one piece",
    "onepiece",
    "yu gi oh",
    "yugioh",
)

_UNSUPPORTED_GAME_ALIASES = {
    "digimon": "Digimon",
    "flesh and blood": "Flesh and Blood",
    "fab tcg": "Flesh and Blood",
    "star wars unlimited": "Star Wars: Unlimited",
    "weiss schwarz": "Weiss Schwarz",
    "cardfight vanguard": "Cardfight!! Vanguard",
    "dragon ball super": "Dragon Ball Super",
    "final fantasy tcg": "Final Fantasy TCG",
    "union arena": "Union Arena",
    "grand archive": "Grand Archive",
    "altered tcg": "Altered TCG",
    "riftbound": "Riftbound",
}


def _normalize_term(value: str) -> str:
    # Treat punctuation as a separator so "Pikachu-V" matches "Pikachu V",
    # while boundary-aware matching prevents "ex" from matching "box".
    return " ".join(re.sub(r"[\W_]+", " ", value.casefold()).split())


def _unsupported_game_name(value: str) -> str | None:
    normalized = f" {_normalize_term(value)} "
    if any(f" {_normalize_term(alias)} " in normalized
           for alias in _SUPPORTED_GAME_ALIASES):
        return None
    for alias, display_name in _UNSUPPORTED_GAME_ALIASES.items():
        if f" {alias} " in normalized:
            return display_name
    return None


@dataclass(frozen=True)
class Watch:
    id: int
    user_id: int
    item_name: str
    normalized_name: str
    max_price: float
    game: str | None = None
    set_name: str | None = None
    set_code: str | None = None
    rarity: str | None = None


@dataclass(frozen=True)
class PendingDM:
    user_id: int
    item_id: str
    item_name: str
    max_price: float
    payload: dict
    attempts: int
    game: str | None = None
    set_name: str | None = None
    set_code: str | None = None
    rarity: str | None = None


def _watch_filter_description(watch: Watch | PendingDM) -> str:
    """A compact user-facing description of optional validated catalog filters."""
    parts = []
    if watch.game:
        parts.append(GAME_LABELS.get(watch.game, watch.game))
    if watch.set_name:
        label = watch.set_name
        if watch.set_code:
            label += f" ({watch.set_code})"
        parts.append(f"set: {label}")
    if watch.rarity:
        parts.append(f"rarity: {watch.rarity}")
    return " • ".join(parts)


def _listing_matches_watch_filters(watch: Watch, item: dict, title: str) -> bool:
    """Fail closed when a selected catalog filter cannot be proven by a listing."""
    if not any((watch.game, watch.set_name, watch.rarity)):
        return True
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    def values_for(*keys: str) -> list[str]:
        values = [str(item[key]) for key in keys if item.get(key) is not None]
        values.extend(
            str(metadata[key]) for key in keys if metadata.get(key) is not None
        )
        return values

    if watch.game:
        listing_games = values_for("game_name", "game")
        # Game inference is already performed by every normalized source. Do not
        # try to guess from an ambiguous title when its metadata is missing.
        if not listing_games or _normalize_term(listing_games[0]) != _normalize_term(watch.game):
            return False

    searchable = " ".join([title, *values_for(
        "set_name", "set", "tcg_set", "rarity", "card_rarity",
    )])
    bounded = f" {_normalize_term(searchable)} "

    if watch.set_name:
        set_name_matches = f" {_normalize_term(watch.set_name)} " in bounded
        # set_code is persisted only when TCGCSV established that it is unique
        # inside its non-Yu-Gi-Oh game. Yu-Gi-Oh's repeated abbreviations must
        # always retain exact-name/metadata evidence (see yugioh-catalog.md).
        set_code_matches = bool(watch.set_code and watch.game != "yugioh") and (
            f" {_normalize_term(watch.set_code)} " in bounded
        )
        if not (set_name_matches or set_code_matches):
            return False
    if watch.rarity:
        explicit_rarities = values_for("rarity", "card_rarity")
        if explicit_rarities:
            # Seller-title wording must never override conflicting structured
            # marketplace rarity evidence.
            if not all(rarity_matches_title(
                watch.game or "", watch.rarity, value
            ) for value in explicit_rarities):
                return False
        elif not rarity_matches_title(watch.game or "", watch.rarity, title):
            return False
    return True


def _watch_accepts_listing(watch: Watch, item: dict) -> bool:
    """Shared predicate for new listings and pending-DM re-evaluation."""
    title = str(item.get("title") or "")
    url = str(item.get("url") or "")
    try:
        total = float(item.get("price", 0)) + float(item.get("shipping", 0))
    except (TypeError, ValueError):
        return False
    return bool(
        title and url and total > 0
        and f" {watch.normalized_name} " in f" {_normalize_term(title)} "
        and total <= watch.max_price
        and _listing_matches_watch_filters(watch, item, title)
    )


class WatchlistStore:
    """Short-lived SQLite connections make cross-thread access predictable."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.claim_token = uuid.uuid4().hex
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            existing_delivery_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(watchlist_deliveries)"
                ).fetchall()
            }
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlists (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    item_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    max_price REAL NOT NULL CHECK (max_price > 0),
                    game TEXT,
                    set_name TEXT,
                    set_code TEXT,
                    rarity TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, normalized_name)
                );

                CREATE TABLE IF NOT EXISTS watchlist_deliveries (
                    user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, item_id)
                );

                CREATE INDEX IF NOT EXISTS idx_watchlists_user
                    ON watchlists(user_id);

                CREATE TABLE IF NOT EXISTS pending_watch_dms (
                    user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    max_price REAL NOT NULL,
                    game TEXT,
                    set_name TEXT,
                    set_code TEXT,
                    rarity TEXT,
                    payload TEXT NOT NULL,
                    total_price REAL NOT NULL DEFAULT 0,
                    initial_batch INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    in_flight_until REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, item_id)
                );

                CREATE INDEX IF NOT EXISTS idx_pending_watch_dms_ready
                    ON pending_watch_dms(next_attempt);

                CREATE TABLE IF NOT EXISTS watchlist_dm_pacing (
                    user_id INTEGER PRIMARY KEY,
                    next_digest_at REAL NOT NULL DEFAULT 0,
                    last_digest_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS watch_source_daily_budgets (
                    source TEXT PRIMARY KEY,
                    budget_day TEXT NOT NULL,
                    used_requests INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watch_targeted_search_jobs (
                    user_id INTEGER NOT NULL,
                    normalized_name TEXT NOT NULL,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    in_flight_until REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, normalized_name),
                    FOREIGN KEY (user_id, normalized_name)
                        REFERENCES watchlists(user_id, normalized_name)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_watch_targeted_search_jobs_due
                    ON watch_targeted_search_jobs(next_attempt);
                """
            )
            # SQLite is the backward-compatible local/legacy store. PostgreSQL
            # intentionally receives its additive columns through Publish rather
            # than application-start DDL.
            watch_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(watchlists)"
                ).fetchall()
            }
            pending_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(pending_watch_dms)"
                ).fetchall()
            }
            for column in ("game", "set_name", "set_code", "rarity"):
                if column not in watch_columns:
                    conn.execute(f"ALTER TABLE watchlists ADD COLUMN {column} TEXT")
                if column not in pending_columns:
                    conn.execute(
                        f"ALTER TABLE pending_watch_dms ADD COLUMN {column} TEXT"
                    )
            for column, definition in (
                ("total_price", "REAL NOT NULL DEFAULT 0"),
                ("initial_batch", "INTEGER NOT NULL DEFAULT 0"),
                ("claim_token", "TEXT"),
                ("in_flight_until", "REAL"),
            ):
                if column not in pending_columns:
                    conn.execute(
                        f"ALTER TABLE pending_watch_dms ADD COLUMN {column} {definition}"
                    )
            # Normalize watches created by older versions before delivery rows
            # are converted from watch ownership to user ownership. If multiple
            # old spellings collapse to the same phrase, the most recently
            # updated watch wins (highest id breaks timestamp ties).
            old_watches = conn.execute(
                """
                SELECT id, user_id, item_name, normalized_name, max_price,
                       COALESCE(updated_at, created_at, '') AS changed_at
                FROM watchlists
                ORDER BY user_id, id
                """
            ).fetchall()
            grouped: dict[tuple[int, str], list[sqlite3.Row]] = {}
            for row in old_watches:
                grouped.setdefault(
                    (row["user_id"], _normalize_term(row["item_name"])), []
                ).append(row)
            old_delivery_is_watch_keyed = (
                existing_delivery_columns
                and "user_id" not in existing_delivery_columns
            )
            for (_, normalized), rows in grouped.items():
                canonical = max(
                    rows, key=lambda row: (row["changed_at"], row["id"])
                )
                for duplicate in rows:
                    if duplicate["id"] == canonical["id"]:
                        continue
                    if old_delivery_is_watch_keyed:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO watchlist_deliveries
                                (watch_id, item_id, delivered_at)
                            SELECT ?, item_id, delivered_at
                            FROM watchlist_deliveries
                            WHERE watch_id = ?
                            """,
                            (canonical["id"], duplicate["id"]),
                        )
                        conn.execute(
                            "DELETE FROM watchlist_deliveries WHERE watch_id = ?",
                            (duplicate["id"],),
                        )
                    conn.execute(
                        "DELETE FROM watchlists WHERE id = ?", (duplicate["id"],)
                    )
                conn.execute(
                    """
                    UPDATE watchlists
                    SET normalized_name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (normalized, canonical["id"]),
                )
            # Preserve delivery history from the pre-release watch-keyed schema.
            if old_delivery_is_watch_keyed:
                conn.execute(
                    """
                    CREATE TABLE watchlist_deliveries_new (
                        user_id INTEGER NOT NULL,
                        item_id TEXT NOT NULL,
                        delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, item_id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO watchlist_deliveries_new
                        (user_id, item_id, delivered_at)
                    SELECT w.user_id, d.item_id, MIN(d.delivered_at)
                    FROM watchlist_deliveries d
                    JOIN watchlists w ON w.id = d.watch_id
                    GROUP BY w.user_id, d.item_id
                    """
                )
                conn.execute("DROP TABLE watchlist_deliveries")
                conn.execute(
                    "ALTER TABLE watchlist_deliveries_new "
                    "RENAME TO watchlist_deliveries"
                )

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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, normalized_name) DO UPDATE SET
                    item_name = excluded.item_name,
                    max_price = excluded.max_price,
                    game = excluded.game,
                    set_name = excluded.set_name,
                    set_code = excluded.set_code,
                    rarity = excluded.rarity,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, item_name.strip(), normalized, max_price, game,
                 set_name, set_code, rarity),
            )
            # A changed budget/filter must not leave an old pending match on its
            # way to the user. Re-check its saved listing under the replacement
            # watch inside this transaction, retaining only still-valid alerts.
            replacement = Watch(
                id=0, user_id=user_id, item_name=item_name.strip(),
                normalized_name=normalized, max_price=max_price, game=game,
                set_name=set_name, set_code=set_code, rarity=rarity,
            )
            pending = conn.execute(
                """
                SELECT item_id, item_name, payload
                FROM pending_watch_dms
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()
            for alert in pending:
                if _normalize_term(alert["item_name"]) != normalized:
                    continue
                conn.execute(
                    "DELETE FROM pending_watch_dms WHERE user_id = ? AND item_id = ?",
                    (user_id, alert["item_id"]),
                )
                try:
                    item = json.loads(alert["payload"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not _watch_accepts_listing(replacement, item):
                    continue
                delivered = conn.execute(
                    "SELECT 1 FROM watchlist_deliveries WHERE user_id = ? AND item_id = ?",
                    (user_id, alert["item_id"]),
                ).fetchone()
                if delivered:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO pending_watch_dms
                        (user_id, item_id, item_name, max_price, game, set_name,
                         set_code, rarity, payload, total_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id, alert["item_id"], replacement.item_name,
                        replacement.max_price, replacement.game,
                        replacement.set_name, replacement.set_code,
                        replacement.rarity, json.dumps(item, ensure_ascii=False),
                        float(item.get("price", 0)) + float(item.get("shipping", 0)),
                    ),
                )
            # A command gets its immediate check separately.  Its durable job
            # begins one interval later so an API-quota deferral cannot strand
            # the watch or let a restart erase its next attempt.
            conn.execute(
                """
                INSERT INTO watch_targeted_search_jobs
                    (user_id, normalized_name, next_attempt, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, normalized_name) DO UPDATE SET
                    next_attempt = excluded.next_attempt,
                    claim_token = NULL,
                    in_flight_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, normalized, time.time() + 120),
            )

    def list_watches(self) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, user_id, item_name, normalized_name, max_price, "
                "game, set_name, set_code, rarity "
                "FROM watchlists"
            ).fetchall()
        return [Watch(**dict(row)) for row in rows]

    def list_watches_for_user(self, user_id: int) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, item_name, normalized_name, max_price,
                       game, set_name, set_code, rarity
                FROM watchlists
                WHERE user_id = ?
                ORDER BY normalized_name
                """,
                (user_id,),
            ).fetchall()
        return [Watch(**dict(row)) for row in rows]

    def delete_watch(self, user_id: int, item_name: str) -> str | None:
        normalized = _normalize_term(item_name)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT item_name
                FROM watchlists
                WHERE user_id = ? AND normalized_name = ?
                """,
                (user_id, normalized),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM watchlists "
                "WHERE user_id = ? AND normalized_name = ?",
                (user_id, normalized),
            )
            conn.execute(
                "DELETE FROM watch_targeted_search_jobs "
                "WHERE user_id = ? AND normalized_name = ?",
                (user_id, normalized),
            )
            # Do not send alerts that were matched but not yet delivered before
            # the user stopped this watch.
            pending = conn.execute(
                "SELECT item_id, item_name FROM pending_watch_dms WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            for alert in pending:
                if _normalize_term(alert["item_name"]) == normalized:
                    conn.execute(
                        "DELETE FROM pending_watch_dms "
                        "WHERE user_id = ? AND item_id = ?",
                        (user_id, alert["item_id"]),
                    )
        return str(row["item_name"])

    def was_delivered(self, user_id: int, item_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM watchlist_deliveries "
                "WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            ).fetchone()
        return row is not None

    def mark_delivered(self, user_id: int, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist_deliveries (user_id, item_id) "
                "VALUES (?, ?)",
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
                    delivered = conn.execute(
                        "SELECT 1 FROM watchlist_deliveries "
                        "WHERE user_id = ? AND item_id = ?",
                        (watch.user_id, item_id),
                    ).fetchone()
                    if delivered:
                        continue
                    before = conn.total_changes
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO pending_watch_dms
                            (user_id, item_id, item_name, max_price, game,
                             set_name, set_code, rarity, payload, total_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            watch.user_id,
                            item_id,
                            watch.item_name,
                            watch.max_price,
                            watch.game,
                            watch.set_name,
                            watch.set_code,
                            watch.rarity,
                            json.dumps(item, ensure_ascii=False),
                            total,
                        ),
                    )
                    if conn.total_changes > before:
                        queued += 1
        return queued

    def enqueue_watch_matches(
        self, watch: Watch, listings: Iterable[dict], limit: int | None = None,
        initial: bool = False,
    ) -> int:
        """Queue only listings proven to match this exact current watch.

        Targeted searches must not turn a result for one user's command into an
        uncontrolled queue drain for every overlapping watch.
        """
        candidates = []
        for item in listings:
            if not _watch_accepts_listing(watch, item):
                continue
            candidates.append((
                float(item.get("price", 0)) + float(item.get("shipping", 0)), item
            ))
        candidates.sort(key=lambda pair: (pair[0], str(pair[1].get("item_id") or "")))
        if limit is not None:
            candidates = candidates[:limit]
        return self.enqueue_matches_for_watch(
            watch, [item for _, item in candidates], initial=initial
        )

    def enqueue_matches_for_watch(
        self, watch: Watch, listings: Iterable[dict], initial: bool = False,
    ) -> int:
        """SQLite implementation used by targeted checks after strict matching."""
        queued = 0
        with self._connect() as conn:
            current = conn.execute(
                """
                SELECT item_name, max_price, game, set_name, set_code, rarity
                FROM watchlists WHERE user_id = ? AND normalized_name = ?
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
            for position, item in enumerate(listings):
                item_id = str(item.get("item_id") or "")
                if not item_id or not _watch_accepts_listing(watch, item):
                    continue
                delivered = conn.execute(
                    "SELECT 1 FROM watchlist_deliveries WHERE user_id = ? AND item_id = ?",
                    (watch.user_id, item_id),
                ).fetchone()
                if delivered:
                    continue
                total = float(item.get("price", 0)) + float(item.get("shipping", 0))
                before = conn.total_changes
                conn.execute(
                    """
                    INSERT OR IGNORE INTO pending_watch_dms
                        (user_id, item_id, item_name, max_price, game, set_name,
                         set_code, rarity, payload, total_price, initial_batch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (watch.user_id, item_id, watch.item_name, watch.max_price,
                     watch.game, watch.set_name, watch.set_code, watch.rarity,
                     json.dumps(item, ensure_ascii=False), total,
                     int(initial and position < 3)),
                )
                queued += int(conn.total_changes > before)
        return queued

    def pending(self, limit: int = 100) -> list[PendingDM]:
        """Legacy single-alert claims, retained for callers outside digest mode."""
        with self._connect() as conn:
            now = time.time()
            token = self.claim_token
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT user_id, item_id
                FROM pending_watch_dms
                WHERE next_attempt <= ?
                  AND (in_flight_until IS NULL OR in_flight_until <= ?)
                ORDER BY total_price, created_at, user_id, item_id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            keys = [(row["user_id"], row["item_id"]) for row in rows]
            for user_id, item_id in keys:
                conn.execute(
                    """
                    UPDATE pending_watch_dms SET claim_token = ?, in_flight_until = ?
                    WHERE user_id = ? AND item_id = ?
                    """,
                    (token, now + 600, user_id, item_id),
                )
            rows = [
                conn.execute(
                    """
                    SELECT user_id, item_id, item_name, max_price, game, set_name,
                           set_code, rarity, payload, attempts
                    FROM pending_watch_dms WHERE user_id = ? AND item_id = ?
                    """, key,
                ).fetchone()
                for key in keys
            ]
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
                    max_price=row["max_price"],
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
        """Avoid delivering an alert replaced after the worker read its queue."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT item_name, max_price, game, set_name, set_code, rarity
                FROM pending_watch_dms
                WHERE user_id = ? AND item_id = ? AND claim_token = ?
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
            owned = conn.execute(
                """
                SELECT 1 FROM pending_watch_dms
                WHERE user_id = ? AND item_id = ? AND claim_token = ?
                """,
                (user_id, item_id, self.claim_token),
            ).fetchone()
            if owned is None:
                return
            conn.execute(
                "INSERT OR IGNORE INTO watchlist_deliveries (user_id, item_id) "
                "VALUES (?, ?)",
                (user_id, item_id),
            )
            conn.execute(
                "DELETE FROM pending_watch_dms WHERE user_id = ? AND item_id = ? "
                "AND claim_token = ?",
                (user_id, item_id, self.claim_token),
            )

    def retry_later(self, user_id: int, item_id: str, attempts: int) -> None:
        delay = min(900, 30 * (2 ** min(attempts, 5)))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pending_watch_dms
                SET attempts = attempts + 1, next_attempt = ?,
                    claim_token = NULL, in_flight_until = NULL
                WHERE user_id = ? AND item_id = ? AND claim_token = ?
                """,
                (time.time() + delay, user_id, item_id, self.claim_token),
            )

    def pending_count(self) -> int:
        with self._connect() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM pending_watch_dms").fetchone()[0]
            )

    def reserve_mercari_daily(self, cap: int, requests: int) -> bool:
        """Persistently reserve metered personal-lane requests before I/O."""
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT budget_day, used_requests FROM watch_source_daily_budgets "
                "WHERE source = 'mercari'"
            ).fetchone()
            used = int(row["used_requests"]) if row and row["budget_day"] == today else 0
            if used + requests > cap:
                return False
            conn.execute(
                """
                INSERT INTO watch_source_daily_budgets(source, budget_day, used_requests)
                VALUES ('mercari', ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    budget_day = excluded.budget_day,
                    used_requests = excluded.used_requests
                """, (today, used + requests),
            )
        return True

    def mercari_daily_remaining(self, cap: int) -> int:
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT budget_day, used_requests FROM watch_source_daily_budgets "
                "WHERE source = 'mercari'"
            ).fetchone()
        used = int(row["used_requests"]) if row and row["budget_day"] == today else 0
        return max(0, cap - used)

    def claim_targeted_searches(self, limit: int = 1) -> list[Watch]:
        """Claim due watches fairly; leases make restarts/multiple workers safe."""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT w.id, w.user_id, w.item_name, w.normalized_name, w.max_price,
                       w.game, w.set_name, w.set_code, w.rarity
                FROM watch_targeted_search_jobs job
                JOIN watchlists w ON w.user_id = job.user_id
                               AND w.normalized_name = job.normalized_name
                WHERE job.next_attempt <= ?
                  AND (job.in_flight_until IS NULL OR job.in_flight_until <= ?)
                ORDER BY job.next_attempt, job.created_at, job.user_id, job.normalized_name
                LIMIT ?
                """, (now, now, limit),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE watch_targeted_search_jobs
                    SET claim_token = ?, in_flight_until = ?
                    WHERE user_id = ? AND normalized_name = ?
                    """,
                    (self.claim_token, now + 600, row["user_id"], row["normalized_name"]),
                )
        return [Watch(**dict(row)) for row in rows]

    def finish_targeted_search(self, watch: Watch, delay: float = 120.0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE watch_targeted_search_jobs
                SET next_attempt = ?, claim_token = NULL, in_flight_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND normalized_name = ? AND claim_token = ?
                """,
                (time.time() + delay, watch.user_id, watch.normalized_name,
                 self.claim_token),
            )

    def claim_digest_batches(
        self, user_limit: int = 20, per_user: int = 5, interval: float = 120.0,
    ) -> list[list[PendingDM]]:
        """Atomically claim paced, cheapest-first batches fairly by user."""
        now = time.time()
        token = self.claim_token
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            users = conn.execute(
                """
                SELECT p.user_id
                FROM pending_watch_dms p
                LEFT JOIN watchlist_dm_pacing pace ON pace.user_id = p.user_id
                WHERE p.next_attempt <= ?
                  AND (p.in_flight_until IS NULL OR p.in_flight_until <= ?)
                  AND COALESCE(pace.next_digest_at, 0) <= ?
                GROUP BY p.user_id
                ORDER BY COALESCE(pace.last_digest_at, 0), MIN(p.created_at), p.user_id
                LIMIT ?
                """, (now, now, now, user_limit),
            ).fetchall()
            result: list[list[PendingDM]] = []
            for selected in users:
                user_id = selected["user_id"]
                has_initial = conn.execute(
                    """
                    SELECT 1 FROM pending_watch_dms
                    WHERE user_id = ? AND initial_batch = 1 AND next_attempt <= ?
                      AND (in_flight_until IS NULL OR in_flight_until <= ?)
                    LIMIT 1
                    """, (user_id, now, now),
                ).fetchone() is not None
                rows = conn.execute(
                    """
                    SELECT user_id, item_id, item_name, max_price, game, set_name,
                           set_code, rarity, payload, attempts
                    FROM pending_watch_dms
                    WHERE user_id = ? AND next_attempt <= ?
                      AND (in_flight_until IS NULL OR in_flight_until <= ?)
                      AND (? = 0 OR initial_batch = 1)
                    ORDER BY total_price, created_at, item_id LIMIT ?
                    """, (user_id, now, now, int(has_initial),
                          3 if has_initial else per_user),
                ).fetchall()
                if not rows:
                    continue
                for row in rows:
                    conn.execute(
                        "UPDATE pending_watch_dms SET claim_token = ?, in_flight_until = ? "
                        "WHERE user_id = ? AND item_id = ?",
                        (token, now + 600, user_id, row["item_id"]),
                    )
                conn.execute(
                    """
                    INSERT INTO watchlist_dm_pacing(user_id, next_digest_at, last_digest_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        next_digest_at = excluded.next_digest_at,
                        last_digest_at = excluded.last_digest_at
                    """, (user_id, now + interval, now),
                )
                entries = []
                for row in rows:
                    try:
                        payload = json.loads(row["payload"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    entries.append(PendingDM(
                        user_id=row["user_id"], item_id=row["item_id"],
                        item_name=row["item_name"], max_price=row["max_price"],
                        payload=payload, attempts=row["attempts"], game=row["game"],
                        set_name=row["set_name"], set_code=row["set_code"],
                        rarity=row["rarity"],
                    ))
                if entries:
                    result.append(entries)
        return result


class WatchlistBot(commands.Bot):
    def __init__(self, token: str, db_path: str, store=None, catalog=None):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.none())
        self.token_value = token
        self.store = store or WatchlistStore(db_path)
        self.catalog = catalog or TCGCSVWatchCatalog()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._last_health_warning = 0.0
        self._enrichment_tasks: set[asyncio.Task] = set()
        self._catalog_tasks: dict[str, asyncio.Task] = {}
        self.search_scheduler = TargetedWatchSearch(budget_store=self.store)
        self._target_scan_cursor = 0
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="watch",
            description="Send your card-shop scout to find a listing within budget.",
        )
        @app_commands.describe(
            item_name="Words to match in the listing title",
            max_price="Maximum total price including shipping (USD)",
            game="Optional game filter; choose this when names are ambiguous",
            set_name=("Optional catalog-validated set (autocomplete after card "
                      "name; retry shortly if results are loading)"),
            rarity=("Optional catalog-validated rarity (requires a set; retry "
                    "shortly if results are loading)"),
        )
        @app_commands.choices(game=[
            app_commands.Choice(name=GAME_LABELS[game], value=game)
            for game in GAMES
        ])
        async def watch(
            interaction: discord.Interaction,
            item_name: app_commands.Range[str, 2, 100],
            max_price: app_commands.Range[float, 0.01, 1_000_000.0],
            game: str | None = None,
            set_name: str | None = None,
            rarity: str | None = None,
        ) -> None:
            # Namespace values are normally raw strings, but normalize Choice
            # too so direct callback invocation and Discord.py version changes
            # cannot bypass the fixed game allowlist.
            game = getattr(game, "value", game)
            cleaned = " ".join(item_name.split())
            if len(_normalize_term(cleaned)) < 2:
                await interaction.response.send_message(
                    "Give me at least two visible characters so I know what to scout for.",
                    ephemeral=True,
                )
                return
            if game is not None and game not in GAMES:
                await interaction.response.send_message(
                    "Choose a game from the provided list.", ephemeral=True
                )
                return
            unsupported_game = _unsupported_game_name(cleaned)
            if unsupported_game:
                await interaction.response.send_message(
                    f"I can't scout **{unsupported_game}** yet. Right now I can "
                    "watch Pokémon, Magic: The Gathering, Disney Lorcana, "
                    "One Piece, and Yu-Gi-Oh!. More games may join the hunt later!",
                    ephemeral=True,
                )
                return
            # A set choice is an opaque TCGCSV game:group id, so direct typed
            # values cannot turn into arbitrary filters. Validation happens again
            # here rather than trusting Discord's autocomplete client.
            catalog_set = None
            if set_name or rarity:
                catalog_set, rarity, validation_error = await asyncio.to_thread(
                    self.catalog.validate, cleaned, game, set_name, rarity
                )
                if validation_error:
                    await interaction.response.send_message(
                        validation_error, ephemeral=True
                    )
                    return
                if catalog_set:
                    game = catalog_set.game
            try:
                await asyncio.to_thread(
                    self.store.upsert_watch,
                    interaction.user.id,
                    cleaned,
                    float(max_price),
                    game,
                    catalog_set.name if catalog_set else None,
                    (catalog_set.abbreviation
                     if catalog_set and catalog_set.abbreviation_unique else None),
                    rarity,
                )
            except (sqlite3.Error, psycopg.Error):
                print("[WATCHLIST][ERROR] Could not save /watch entry")
                await interaction.response.send_message(
                    "My clipboard slipped—I couldn't save that watch. "
                    "Please try again in a moment.",
                    ephemeral=True,
                )
                return
            saved_watch = Watch(
                id=0, user_id=interaction.user.id, item_name=cleaned,
                normalized_name=_normalize_term(cleaned), max_price=float(max_price),
                game=game, set_name=catalog_set.name if catalog_set else None,
                set_code=(catalog_set.abbreviation
                          if catalog_set and catalog_set.abbreviation_unique
                          else None),
                rarity=rarity,
            )
            safe_name = discord.utils.escape_markdown(cleaned)
            filter_text = _watch_filter_description(saved_watch)
            filters = f"\nFilters: **{discord.utils.escape_markdown(filter_text)}**." if filter_text else ""
            # Targeted API calls are sync and can take longer than Discord's
            # acknowledgement window, so acknowledge before moving them off the
            # event loop.  A quota failure is reported explicitly, never as 0.
            await interaction.response.defer(ephemeral=True, thinking=True)
            report = await self._initial_targeted_check(saved_watch)
            await interaction.followup.send(
                f"I'm on the hunt for **{safe_name}** at "
                f"**${float(max_price):,.2f} or less**, including shipping. "
                f"I'll send you paced digest DMs if I spot one!{filters}\n"
                "One watch is kept per item phrase; adding it again updates its filters.\n"
                f"{report}",
                ephemeral=True,
            )

        async def set_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            card_name = str(getattr(interaction.namespace, "item_name", "") or "")
            game = getattr(interaction.namespace, "game", None)
            game = getattr(game, "value", game)
            if game not in GAMES:
                game = None
            if len(_normalize_term(card_name)) < 2:
                return []
            if ((game and self.catalog.needs_refresh(game))
                    or (not game and any(
                        self.catalog.needs_refresh(candidate)
                        for candidate in GAMES
                    ))):
                self._start_catalog_load(game)
            matching_sets = self.catalog.matching_sets(card_name, game)
            if matching_sets is None:
                self._start_catalog_load(game)
                # Discord autocomplete cannot display a durable status message.
                # Submit validation explicitly tells the user to retry instead of
                # accepting a guessed set while this background read is cold.
                return []
            needle = _normalize_term(current)
            return [
                app_commands.Choice(
                    name=(f"{GAME_LABELS[entry.game]}: {entry.name}"
                          + (f" [{entry.abbreviation}]" if entry.abbreviation else ""))[:100],
                    value=entry.token,
                )
                for entry in matching_sets
                if not needle or needle in _normalize_term(entry.name)
            ][:25]

        async def rarity_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            card_name = str(getattr(interaction.namespace, "item_name", "") or "")
            set_token = getattr(interaction.namespace, "set_name", None)
            if len(_normalize_term(card_name)) < 2 or not set_token:
                return []
            try:
                selected_game = str(set_token).split(":", 1)[0]
            except (AttributeError, IndexError):
                selected_game = None
            if selected_game in GAMES and self.catalog.needs_refresh(selected_game):
                self._start_catalog_load(selected_game)
            rarities = self.catalog.rarities(card_name, set_token)
            if rarities is None:
                self._start_catalog_load(
                    selected_game if selected_game in GAMES else None
                )
                return []
            needle = _normalize_term(current)
            return [
                app_commands.Choice(name=entry[:100], value=entry)
                for entry in rarities
                if not needle or needle in _normalize_term(entry)
            ][:25]

        watch.autocomplete("set_name")(set_autocomplete)
        watch.autocomplete("rarity")(rarity_autocomplete)

        @self.tree.command(
            name="unwatch",
            description="Call your card-shop scout off the hunt for an item.",
        )
        @app_commands.describe(item_name="Item name from your active watches")
        async def unwatch(
            interaction: discord.Interaction,
            item_name: app_commands.Range[str, 2, 100],
        ) -> None:
            try:
                deleted_name = await asyncio.to_thread(
                    self.store.delete_watch,
                    interaction.user.id,
                    item_name,
                )
            except (sqlite3.Error, psycopg.Error):
                print("[WATCHLIST][ERROR] Could not delete /unwatch entry")
                await interaction.response.send_message(
                    "I couldn't call off that hunt right now. "
                    "Please try again in a moment.",
                    ephemeral=True,
                )
                return
            if deleted_name is None:
                await interaction.response.send_message(
                    "That one isn't on my scouting list. "
                    "Use `/mywatches` to see the exact names you're tracking.",
                    ephemeral=True,
                )
                return
            safe_name = discord.utils.escape_markdown(deleted_name)
            await interaction.response.send_message(
                f"Got it—I'm off the hunt for **{safe_name}**.",
                ephemeral=True,
            )

        @self.tree.command(
            name="mywatches",
            description="Check everything your card-shop scout is hunting.",
        )
        async def mywatches(interaction: discord.Interaction) -> None:
            try:
                watches = await asyncio.to_thread(
                    self.store.list_watches_for_user,
                    interaction.user.id,
                )
            except (sqlite3.Error, psycopg.Error):
                print("[WATCHLIST][ERROR] Could not load /mywatches entries")
                await interaction.response.send_message(
                    "I can't open my scouting notebook right now. "
                    "Please try again in a moment.",
                    ephemeral=True,
                )
                return
            if not watches:
                await interaction.response.send_message(
                    "My scouting list is empty. Use `/watch` and send me on a hunt!",
                    ephemeral=True,
                )
                return

            lines = [
                f"• **{discord.utils.escape_markdown(entry.item_name)}** — "
                f"${entry.max_price:,.2f} max"
                + (f" — {discord.utils.escape_markdown(_watch_filter_description(entry))}"
                   if _watch_filter_description(entry) else "")
                for entry in watches
            ]
            chunks: list[str] = []
            current = "**My current scouting list**\n"
            for line in lines:
                if len(current) + len(line) + 1 > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current += ("\n" if current else "") + line
            if current:
                chunks.append(current)
            await interaction.response.send_message(chunks[0], ephemeral=True)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=True)

    def _start_catalog_load(self, game: str | None) -> None:
        """Start cold TCGCSV reads once per game without delaying autocomplete."""
        for candidate in ((game,) if game else GAMES):
            if not self.catalog.needs_refresh(candidate):
                continue
            task = self._catalog_tasks.get(candidate)
            if task and not task.done():
                continue
            task = self.loop.create_task(
                asyncio.to_thread(self.catalog.ensure_game, candidate)
            )
            self._catalog_tasks[candidate] = task

    async def setup_hook(self) -> None:
        # A transient command-registration failure must not prevent the gateway
        # connection or pending-DM worker from starting. Existing commands keep
        # working and registration will be retried on the next process restart.
        try:
            synced = await self.tree.sync()
            print(f"[WATCHLIST] Synced {len(synced)} global slash command(s)")
        except discord.HTTPException as exc:
            print(
                f"[WATCHLIST][WARN] Slash command sync failed: "
                f"{type(exc).__name__}; bot startup will continue"
            )
        self.loop.create_task(self._listing_worker())
        self.loop.create_task(self._targeted_watch_worker())

    async def on_ready(self) -> None:
        self._ready.set()
        print(f"[WATCHLIST] Discord bot ready as {self.user}")

    async def on_disconnect(self) -> None:
        self._ready.clear()

    async def on_resumed(self) -> None:
        # Discord may resume the existing gateway session without sending a new
        # READY event. Restore health so normal reconnects are not reported as
        # a permanently unavailable watchlist bot.
        self._ready.set()
        print("[WATCHLIST] Discord gateway session resumed")

    def start_in_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def runner() -> None:
            try:
                self.run(self.token_value, log_handler=None)
            except Exception as exc:
                print(f"[WATCHLIST][ERROR] Discord bot stopped: {type(exc).__name__}")
            finally:
                self._ready.clear()

        self._thread = threading.Thread(
            target=runner, name="discord-watchlist-bot", daemon=True
        )
        self._thread.start()

    def submit_listings(self, listings: Iterable[dict]) -> None:
        try:
            queued = self.store.enqueue_matches(listings)
            if queued:
                print(f"[WATCHLIST] Persisted {queued} pending personal alert(s)")
        except (sqlite3.Error, psycopg.Error, TypeError, ValueError) as exc:
            print(
                f"[WATCHLIST][ERROR] Could not queue listing matches: "
                f"{type(exc).__name__}"
            )
            return
        if (not self._ready.is_set()
                and time.monotonic() - self._last_health_warning >= 300):
            self._last_health_warning = time.monotonic()
            print(
                f"[WATCHLIST][WARN] Discord bot is not ready; "
                f"{self.store.pending_count()} alert(s) safely pending in SQLite"
            )

    @staticmethod
    def _total(item: dict) -> float:
        return float(item.get("price", 0)) + float(item.get("shipping", 0))

    async def _initial_targeted_check(self, watch: Watch) -> str:
        """Run a newly saved watch against every source and queue its cheapest 3."""
        results = await asyncio.to_thread(self.search_scheduler.search, watch)
        matching: list[dict] = []
        parts: list[str] = []
        for source in SOURCES:
            result = results[source]
            verified = [
                listing for listing in result["listings"]
                if _watch_accepts_listing(watch, listing)
            ]
            matching.extend(verified)
            label = {"ebay": "eBay", "mercari": "Mercari",
                     "tcgplayer": "TCGplayer"}[source]
            if result["status"] in {"ok", "partial"}:
                suffix = " (partial)" if result["status"] == "partial" else ""
                if result["status"] == "partial" and result.get("message"):
                    suffix += f" — {result['message']}"
                remaining = f"; {result.get('remaining_requests', 0)} request slot(s) remain this window"
                if source == "mercari":
                    remaining += (
                        f", {result.get('mercari_daily_remaining', 0)} "
                        "Mercari personal request(s) remain today"
                    )
                parts.append(
                    f"{label}: **{len(verified)}** verified match(es) among "
                    f"{result['checked']} checked{suffix}{remaining}"
                )
            else:
                reason = f" — {result['message']}" if result["message"] else ""
                remaining = ""
                if source == "mercari":
                    remaining = (
                        f" ({result.get('mercari_daily_remaining', 0)} "
                        "Mercari personal request(s) remain today)"
                    )
                parts.append(f"{label}: **{result['status']}**{reason}{remaining}")
        matching.sort(key=lambda listing: (
            self._total(listing), str(listing.get("item_id") or "")
        ))
        try:
            queued = await asyncio.to_thread(
                self.store.enqueue_watch_matches, watch, matching, None, True
            )
        except (sqlite3.Error, psycopg.Error, TypeError, ValueError) as exc:
            print(f"[WATCHLIST][ERROR] Initial match queue failed: {type(exc).__name__}")
            return "Initial source check completed, but I could not queue its matches."
        successful = any(
            result["status"] in {"ok", "partial"} for result in results.values()
        )
        queue_text = (
            f"Queued **{queued}** new verified match(es). Your first digest "
            "prioritizes its 3 lowest-priced matches; later digests send up to 5."
            if matching else (
                "No verified matches were returned in this check."
                if successful else
                "All targeted sources were unavailable or failed, so no zero-match "
                "claim was made and this watch remains scheduled."
            )
        )
        return "**Initial targeted check** — " + " • ".join(parts) + f"\n{queue_text}"

    async def _targeted_watch_worker(self) -> None:
        """Claim one durable due watch per interval to stay fair and metered."""
        while not self.is_closed():
            try:
                watches = await asyncio.to_thread(
                    self.store.claim_targeted_searches, 1
                )
                if watches:
                    watch = watches[0]
                    results = await asyncio.to_thread(self.search_scheduler.search, watch)
                    matches = [
                        listing
                        for result in results.values()
                        if result["status"] in {"ok", "partial"}
                        for listing in result["listings"]
                        if _watch_accepts_listing(watch, listing)
                    ]
                    # A periodic query is a bounded follow-up batch, not a
                    # queue drain; delivery pacing below still caps the DM.
                    await asyncio.to_thread(
                        self.store.enqueue_watch_matches, watch, matches
                    )
                    await asyncio.to_thread(
                        self.store.finish_targeted_search, watch, 120
                    )
            except Exception as exc:
                print(f"[WATCHLIST][ERROR] Targeted watch scan failed: {type(exc).__name__}")
            await asyncio.sleep(120)

    async def _listing_worker(self) -> None:
        while not self.is_closed():
            try:
                processed = await self._drain_pending_once()
                if not processed:
                    await asyncio.sleep(2)
            except Exception as exc:
                print(f"[WATCHLIST][ERROR] Pending DM worker failed: {type(exc).__name__}")
                await asyncio.sleep(5)

    async def _process_batch(self, listings: list[dict]) -> None:
        await asyncio.to_thread(self.store.enqueue_matches, listings)
        # One bounded claim is sufficient; the worker handles the remainder.
        await self._drain_pending_once()

    async def _drain_pending_once(self) -> bool:
        batches = await asyncio.to_thread(self.store.claim_digest_batches)
        if not batches:
            return False
        delivered = 0
        for entries in batches:
            current = [
                entry for entry in entries
                if await asyncio.to_thread(self.store.pending_is_current, entry)
            ]
            if not current:
                continue
            if len(current) == 1:
                entry = current[0]
                item = entry.payload
                result = await self._send_watch_dm(entry, item, self._total(item))
            else:
                result = await self._send_watch_digest(current)
            for entry in current:
                if result in ("sent", "permanent") or result is True:
                    await asyncio.to_thread(self.store.complete, entry.user_id, entry.item_id)
                    if result == "sent" or result is True:
                        delivered += 1
                else:
                    await asyncio.to_thread(
                        self.store.retry_later, entry.user_id, entry.item_id, entry.attempts
                    )
        if delivered:
            print(f"[WATCHLIST] Delivered {delivered} personal DM alert(s)")
        return True

    async def _send_watch_digest(self, entries: list[PendingDM]) -> str:
        """Send one DM for a bounded user batch; individual alerts stay durable."""
        try:
            user_id = entries[0].user_id
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            embeds: list[discord.Embed] = []
            for position, entry in enumerate(entries[:5], start=1):
                item = entry.payload
                total = self._total(item)
                title = discord.utils.escape_markdown(str(item.get("title") or "Listing"))
                url = str(item.get("url") or "")
                marketplace = str(item.get("store") or item.get("source") or "Unknown")
                embed = discord.Embed(
                    title=(
                        f"Scout digest: {len(entries)} new matches"
                        if position == 1 else f"Scout digest match {position}"
                    ),
                    description=f"[{title}]({url})",
                    url=url,
                    color=0xF1C40F,
                )
                embed.add_field(
                    name="Total", value=f"${total:,.2f}", inline=True,
                )
                embed.add_field(
                    name="Marketplace", value=marketplace[:1024], inline=True,
                )
                embed.add_field(
                    name="Your watch",
                    value=(
                        f"{discord.utils.escape_markdown(entry.item_name)} "
                        f"≤ ${entry.max_price:,.2f}"
                    )[:1024],
                    inline=False,
                )
                image_url = str(item.get("image_url") or "")
                if image_url:
                    embed.set_thumbnail(url=image_url)
                embeds.append(embed)
            for embed in embeds:
                embed.set_footer(text="Your card-shop scout • paced every 2 minutes")
            message = await user.send(embeds=embeds)
            # Each task edits the same complete embed list, never a lone embed,
            # so a sold-comps update cannot erase its digest siblings.
            for index, entry in enumerate(entries[:5]):
                task = asyncio.create_task(
                    self._enrich_digest_watch_dm(message, embeds, index, entry.payload)
                )
                self._enrichment_tasks.add(task)
                task.add_done_callback(self._enrichment_tasks.discard)
            return "sent"
        except (discord.Forbidden, discord.NotFound):
            print(f"[WATCHLIST][WARN] Cannot DM Discord user {entries[0].user_id}")
            return "permanent"
        except (discord.HTTPException, sqlite3.Error, psycopg.Error) as exc:
            print(f"[WATCHLIST][WARN] Digest delivery failed: {type(exc).__name__}")
            return "retry"

    async def _enrich_digest_watch_dm(
        self, message: discord.Message, embeds: list[discord.Embed], index: int, item: dict,
    ) -> None:
        """Add comps to one digest embed while preserving every sibling embed."""
        try:
            title = str(item.get("title") or "")
            comps = await asyncio.to_thread(
                get_sold_comps, title, title, str(item.get("language") or "Unknown"),
                bool(item.get("sealed")),
            )
            if not comps:
                return
            embed = embeds[index]
            embed.add_field(
                name="Recent eBay Avg Sold",
                value=f"${comps['average']:,.2f}\n*{comps['count']} completed sales*",
                inline=True,
            )
            embed.add_field(
                name="Recent eBay Sold Comps",
                value=format_sold_comps(comps, include_average=False),
                inline=False,
            )
            await message.edit(embeds=embeds)
        except Exception as exc:
            print(f"[WATCHLIST][WARN] Digest sold-comp update failed: {type(exc).__name__}")

    async def _send_watch_dm(
        self, watch: Watch | PendingDM, item: dict, total: float
    ) -> str:
        try:
            user = self.get_user(watch.user_id) or await self.fetch_user(watch.user_id)
            shipping = float(item.get("shipping", 0))
            embed = discord.Embed(
                title="Scout report: I found a match!",
                description=f"[{item['title']}]({item['url']})",
                url=item["url"],
                color=0xF1C40F,
            )
            embed.add_field(name="Total", value=f"${total:,.2f}", inline=True)
            embed.add_field(
                name="Price",
                value=f"${float(item.get('price', 0)):,.2f}",
                inline=True,
            )
            embed.add_field(
                name="Shipping",
                value="Free" if shipping <= 0 else f"${shipping:,.2f}",
                inline=True,
            )
            embed.add_field(
                name="Marketplace",
                value=str(item.get("store") or item.get("source") or "Unknown"),
                inline=True,
            )
            embed.add_field(
                name="Your watch",
                value=(
                    f"{watch.item_name} ≤ ${watch.max_price:,.2f}"
                    + (f"\n{_watch_filter_description(watch)}"
                       if _watch_filter_description(watch) else "")
                ),
                inline=False,
            )
            image_url = str(item.get("image_url") or "")
            if image_url:
                embed.set_thumbnail(url=image_url)
            embed.set_footer(
                text="Your card-shop scout • Price includes listed shipping"
            )
            message = await user.send(embed=embed)
            task = self.loop.create_task(
                self._enrich_watch_dm(message, embed, item)
            )
            self._enrichment_tasks.add(task)
            task.add_done_callback(self._enrichment_tasks.discard)
            return "sent"
        except (discord.Forbidden, discord.NotFound):
            print(f"[WATCHLIST][WARN] Cannot DM Discord user {watch.user_id}")
            return "permanent"
        except (discord.HTTPException, sqlite3.Error, psycopg.Error) as exc:
            print(f"[WATCHLIST][WARN] DM delivery failed: {type(exc).__name__}")
        return "retry"

    async def _enrich_watch_dm(
        self,
        message: discord.Message,
        embed: discord.Embed,
        item: dict,
    ) -> None:
        try:
            title = str(item.get("title") or "")
            comps = await asyncio.to_thread(
                get_sold_comps,
                title,
                title,
                str(item.get("language") or "Unknown"),
                bool(item.get("sealed")),
            )
            if not comps:
                return
            embed.add_field(
                name="Recent eBay Avg Sold",
                value=(
                    f"${comps['average']:,.2f}\n"
                    f"*{comps['count']} completed sales*"
                ),
                inline=True,
            )
            embed.add_field(
                name="Recent eBay Sold Comps",
                value=format_sold_comps(comps, include_average=False),
                inline=False,
            )
            await message.edit(embed=embed)
            print(
                f"[WATCHLIST] Added {comps['count']} recent sale(s) "
                "to personal ping"
            )
        except Exception as exc:
            print(
                f"[WATCHLIST][WARN] Sold-comp update failed: "
                f"{type(exc).__name__}"
            )


def create_watchlist_bot(token: str, db_path: str) -> WatchlistBot:
    from watchlist_postgres import PostgresWatchlistStore

    store = PostgresWatchlistStore()
    imported_watches, imported_deliveries, imported_pending = (
        store.import_sqlite(db_path)
    )
    if imported_watches or imported_deliveries or imported_pending:
        print(
            f"[WATCHLIST] Imported {imported_watches} legacy watch(es) and "
            f"{imported_deliveries} delivery record(s), with "
            f"{imported_pending} pending alert(s), into PostgreSQL"
        )
    print("[WATCHLIST] Persistent PostgreSQL storage active")
    return WatchlistBot(token=token, db_path=db_path, store=store)