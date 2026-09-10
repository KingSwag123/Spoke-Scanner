"""Discord slash-command bot with persistent personal listing watchlists."""

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
import psycopg
from discord import app_commands
from discord.ext import commands

from sold_comps import format_sold_comps, get_sold_comps


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

    def list_watches_for_user(self, user_id: int) -> list[Watch]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, item_name, normalized_name, max_price
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
    def __init__(self, token: str, db_path: str, store=None):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.none())
        self.token_value = token
        self.store = store or WatchlistStore(db_path)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._last_health_warning = 0.0
        self._enrichment_tasks: set[asyncio.Task] = set()
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="watch",
            description="Send your card-shop scout to find a listing within budget.",
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
                    "Give me at least two visible characters so I know what to scout for.",
                    ephemeral=True,
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
            try:
                await asyncio.to_thread(
                    self.store.upsert_watch,
                    interaction.user.id,
                    cleaned,
                    float(max_price),
                )
            except (sqlite3.Error, psycopg.Error):
                print("[WATCHLIST][ERROR] Could not save /watch entry")
                await interaction.response.send_message(
                    "My clipboard slipped—I couldn't save that watch. "
                    "Please try again in a moment.",
                    ephemeral=True,
                )
                return
            safe_name = discord.utils.escape_markdown(cleaned)
            await interaction.response.send_message(
                f"I'm on the hunt for **{safe_name}** at "
                f"**${float(max_price):,.2f} or less**, including shipping. "
                "I'll send you a DM if I spot one!",
                ephemeral=True,
            )

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
                value=f"{watch.item_name} ≤ ${watch.max_price:,.2f}",
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