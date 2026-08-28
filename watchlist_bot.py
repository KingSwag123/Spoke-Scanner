"""Discord slash-command bot and SQLite-backed personal listing watchlists."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands


def _normalize_term(value: str) -> str:
    # Treat punctuation as a separator so "Pikachu-V" matches "Pikachu V",
    # while boundary-aware matching prevents "ex" from matching "box".
    return " ".join(re.sub(r"[\W_]+", " ", value.casefold()).split())


@dataclass(frozen=True)
class Watch:
    id: int
    user_id: int
    item_name: str
    normalized_name: str
    max_price: float


@dataclass(frozen=True)
class PendingDM:
    user_id: int
    item_id: str
    item_name: str
    max_price: float
    payload: dict
    attempts: int


class WatchlistStore:
    """Short-lived SQLite connections make cross-thread access predictable."""

    def __init__(self, db_path: str):
        self.db_path = db_path
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
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, item_id)
                );

                CREATE INDEX IF NOT EXISTS idx_pending_watch_dms_ready
                    ON pending_watch_dms(next_attempt);
                """
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

    def upsert_watch(self, user_id: int, item_name: str, max_price: float) -> None:
        normalized = _normalize_term(item_name)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlists
                    (user_id, item_name, normalized_name, max_price)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, normalized_name) DO UPDATE SET
                    item_name = excluded.item_name,
                    max_price = excluded.max_price,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, item_name.strip(), normalized, max_price),
            )

    def list_watches(self) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, user_id, item_name, normalized_name, max_price "
                "FROM watchlists"
            ).fetchall()
        return [Watch(**dict(row)) for row in rows]

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
                            or total > watch.max_price):
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
                            (user_id, item_id, item_name, max_price, payload)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            watch.user_id,
                            item_id,
                            watch.item_name,
                            watch.max_price,
                            json.dumps(item, ensure_ascii=False),
                        ),
                    )
                    if conn.total_changes > before:
                        queued += 1
        return queued

    def pending(self, limit: int = 100) -> list[PendingDM]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, item_id, item_name, max_price, payload, attempts
                FROM pending_watch_dms
                WHERE next_attempt <= ?
                ORDER BY created_at, user_id, item_id
                LIMIT ?
                """,
                (time.time(), limit),
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
                    max_price=row["max_price"],
                    payload=payload,
                    attempts=row["attempts"],
                )
            )
        return pending

    def complete(self, user_id: int, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist_deliveries (user_id, item_id) "
                "VALUES (?, ?)",
                (user_id, item_id),
            )
            conn.execute(
                "DELETE FROM pending_watch_dms WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            )

    def retry_later(self, user_id: int, item_id: str, attempts: int) -> None:
        delay = min(900, 30 * (2 ** min(attempts, 5)))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pending_watch_dms
                SET attempts = attempts + 1, next_attempt = ?
                WHERE user_id = ? AND item_id = ?
                """,
                (time.time() + delay, user_id, item_id),
            )

    def pending_count(self) -> int:
        with self._connect() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM pending_watch_dms").fetchone()[0]
            )


class WatchlistBot(commands.Bot):
    def __init__(self, token: str, db_path: str):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.none())
        self.token_value = token
        self.store = WatchlistStore(db_path)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._last_health_warning = 0.0
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="watch",
            description="DM me when a matching listing is at or below my maximum price.",
        )
        @app_commands.describe(
            item_name="Words to match in the listing title",
            max_price="Maximum total price including shipping (USD)",
        )
        async def watch(
            interaction: discord.Interaction,
            item_name: app_commands.Range[str, 2, 100],
            max_price: app_commands.Range[float, 0.01, 1_000_000.0],
        ) -> None:
            cleaned = " ".join(item_name.split())
            if len(_normalize_term(cleaned)) < 2:
                await interaction.response.send_message(
                    "Please enter at least two visible characters.", ephemeral=True
                )
                return
            try:
                await asyncio.to_thread(
                    self.store.upsert_watch,
                    interaction.user.id,
                    cleaned,
                    float(max_price),
                )
            except sqlite3.Error:
                print("[WATCHLIST][ERROR] Could not save /watch entry")
                await interaction.response.send_message(
                    "I couldn't save that watch right now. Please try again later.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Watching **{cleaned}** at **${float(max_price):,.2f} or less**, "
                "including shipping. I'll DM you when a match appears.",
                ephemeral=True,
            )

    async def setup_hook(self) -> None:
        synced = await self.tree.sync()
        print(f"[WATCHLIST] Synced {len(synced)} global slash command(s)")
        self.loop.create_task(self._listing_worker())

    async def on_ready(self) -> None:
        self._ready.set()
        print(f"[WATCHLIST] Discord bot ready as {self.user}")

    async def on_disconnect(self) -> None:
        self._ready.clear()

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
        except (sqlite3.Error, TypeError, ValueError) as exc:
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
        while await self._drain_pending_once():
            pass

    async def _drain_pending_once(self) -> bool:
        pending = await asyncio.to_thread(self.store.pending)
        if not pending:
            return False
        delivered = 0
        for entry in pending:
            item = entry.payload
            total = float(item.get("price", 0)) + float(item.get("shipping", 0))
            result = await self._send_watch_dm(entry, item, total)
            if result in ("sent", "permanent") or result is True:
                await asyncio.to_thread(
                    self.store.complete, entry.user_id, entry.item_id
                )
                if result == "sent" or result is True:
                    delivered += 1
            else:
                await asyncio.to_thread(
                    self.store.retry_later,
                    entry.user_id,
                    entry.item_id,
                    entry.attempts,
                )
        if delivered:
            print(f"[WATCHLIST] Delivered {delivered} personal DM alert(s)")
        return True

    async def _send_watch_dm(
        self, watch: Watch | PendingDM, item: dict, total: float
    ) -> str:
        try:
            user = self.get_user(watch.user_id) or await self.fetch_user(watch.user_id)
            shipping = float(item.get("shipping", 0))
            embed = discord.Embed(
                title="Watchlist match",
                description=f"[{item['title']}]({item['url']})",
                url=item["url"],
                color=0x5865F2,
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
                value=f"{watch.item_name} ≤ ${watch.max_price:,.2f}",
                inline=False,
            )
            image_url = str(item.get("image_url") or "")
            if image_url:
                embed.set_thumbnail(url=image_url)
            await user.send(embed=embed)
            return "sent"
        except (discord.Forbidden, discord.NotFound):
            print(f"[WATCHLIST][WARN] Cannot DM Discord user {watch.user_id}")
            return "permanent"
        except (discord.HTTPException, sqlite3.Error) as exc:
            print(f"[WATCHLIST][WARN] DM delivery failed: {type(exc).__name__}")
        return "retry"


def create_watchlist_bot(token: str, db_path: str) -> WatchlistBot:
    return WatchlistBot(token=token, db_path=db_path)