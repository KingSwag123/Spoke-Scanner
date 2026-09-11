"""Bounded, shared targeted marketplace searches for personal watches.

The marketplace adapters are deliberately synchronous.  This coordinator is
called from ``asyncio.to_thread`` by the bot, so marketplace I/O never blocks
the Discord gateway while the lock still keeps concurrent /watch commands from
duplicating a metered lookup.
"""

from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING

from watch_sources import SOURCE_REQUEST_CAPS

if TYPE_CHECKING:
    from watchlist_bot import Watch


SOURCES = ("ebay", "mercari", "tcgplayer")
# Adapter-maintained, documented maxima.  These reservations prevent concurrent
# distinct queries from making a source exceed its own per-call allowance.
SOURCE_REQUEST_BUDGETS = dict(SOURCE_REQUEST_CAPS)
CACHE_TTL_SECONDS = 120.0
ERROR_BACKOFF_SECONDS = 60.0
MERCARI_DAILY_PERSONAL_REQUEST_CAP = 24


class TargetedWatchSearch:
    """Coalesce identical searches and enforce a conservative source budget."""

    def __init__(
        self,
        cache_ttl: float = CACHE_TTL_SECONDS,
        budget_window: float = CACHE_TTL_SECONDS,
        budget_store=None,
    ):
        self.cache_ttl = cache_ttl
        self.budget_window = budget_window
        self.budget_store = budget_store
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, dict]] = {}
        self._inflight: dict[tuple[str, tuple[tuple[str, str], ...]], Future] = {}
        self._backoff_until: dict[str, float] = {}
        self._window_started = time.monotonic()
        self._reserved: dict[str, int] = {source: 0 for source in SOURCES}
        self._mercari_day_started = self._window_started
        self._mercari_reserved = 0

    @staticmethod
    def query_for(watch: "Watch") -> dict:
        return {
            "item_name": watch.item_name,
            "normalized_name": watch.normalized_name,
            "max_price": watch.max_price,
            "game": watch.game,
            "set_name": watch.set_name,
            "set_code": watch.set_code,
            "rarity": watch.rarity,
        }

    @staticmethod
    def _key(source: str, query: dict) -> tuple[str, tuple[tuple[str, str], ...]]:
        # Do not include a raw user id: identical user watches should share one
        # lookup.  Values are stringified only for immutable cache keys.
        return source, tuple(sorted(
            (key, "" if value is None else str(value))
            for key, value in query.items()
        ))

    @staticmethod
    def _unavailable(source: str, message: str) -> dict:
        return {
            "source": source, "status": "unavailable", "listings": [],
            "checked": 0, "message": message, "requests": 0,
        }

    def _reset_window_if_needed(self, now: float) -> None:
        if now - self._window_started >= self.budget_window:
            self._window_started = now
            self._reserved = {source: 0 for source in SOURCES}
        if now - self._mercari_day_started >= 24 * 60 * 60:
            self._mercari_day_started = now
            self._mercari_reserved = 0

    def _mercari_daily_remaining(self) -> int:
        if self.budget_store is not None:
            return int(self.budget_store.mercari_daily_remaining(
                MERCARI_DAILY_PERSONAL_REQUEST_CAP
            ))
        return max(0, MERCARI_DAILY_PERSONAL_REQUEST_CAP - self._mercari_reserved)

    def _with_remaining(self, source: str, result: dict) -> dict:
        """Expose coordinator limits to the command without altering adapters."""
        result = copy.deepcopy(result)
        result["remaining_requests"] = max(
            0, SOURCE_REQUEST_BUDGETS[source] - self._reserved[source]
        )
        if source == "mercari":
            result["mercari_daily_remaining"] = self._mercari_daily_remaining()
        return result

    def remaining_limits(self) -> dict[str, int]:
        """Return conservative personal-lane capacity without making a request."""
        with self._lock:
            self._reset_window_if_needed(time.monotonic())
            return {
                **{
                    source: max(0, SOURCE_REQUEST_BUDGETS[source] - self._reserved[source])
                    for source in SOURCES
                },
                "mercari_daily": self._mercari_daily_remaining(),
            }

    @staticmethod
    def _sanitize(source: str, value: object) -> dict:
        if not isinstance(value, dict):
            return {
                "source": source, "status": "error", "listings": [],
                "checked": 0, "message": "Source returned an invalid response.",
                "requests": 0,
            }
        status = str(value.get("status") or "error")
        if status not in {"ok", "partial", "unavailable", "error"}:
            status = "error"
        listings = value.get("listings")
        listings = listings if isinstance(listings, list) else []
        try:
            checked = max(0, int(value.get("checked", 0)))
            requests = max(0, int(value.get("requests", 0)))
        except (TypeError, ValueError):
            checked, requests = 0, 0
        return {
            "source": source, "status": status, "listings": listings,
            "checked": checked, "message": str(value.get("message") or ""),
            "requests": requests,
        }

    def _one_source(self, source: str, query: dict) -> dict:
        key = self._key(source, query)
        owner = False
        with self._lock:
            now = time.monotonic()
            self._reset_window_if_needed(now)
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return self._with_remaining(source, cached[1])
            waiting = self._inflight.get(key)
            if waiting is None:
                if self._backoff_until.get(source, 0) > now:
                    return self._with_remaining(source, self._unavailable(
                        source, "Source is temporarily backed off after an error."
                    ))
                reserve = SOURCE_REQUEST_BUDGETS[source]
                if self._reserved[source] + reserve > SOURCE_REQUEST_BUDGETS[source]:
                    return self._with_remaining(source, self._unavailable(
                        source, "Targeted-search request quota is currently unavailable."
                    ))
                if (source == "mercari" and self.budget_store is None
                        and self._mercari_reserved + reserve
                        > MERCARI_DAILY_PERSONAL_REQUEST_CAP):
                    return self._with_remaining(source, self._unavailable(
                        source,
                        "Mercari's daily personal-search quota is exhausted; "
                        "this watch is deferred until it resets.",
                    ))
                if source == "mercari" and self.budget_store is not None:
                    try:
                        reserved_daily = self.budget_store.reserve_mercari_daily(
                            MERCARI_DAILY_PERSONAL_REQUEST_CAP, reserve
                        )
                    except Exception as exc:
                        return self._with_remaining(source, self._unavailable(
                            source,
                            "Mercari's metered-search quota could not be reserved "
                            f"({type(exc).__name__}); this watch is deferred.",
                        ))
                    if not reserved_daily:
                        return self._with_remaining(source, self._unavailable(
                            source,
                            "Mercari's daily personal-search quota is exhausted; "
                            "this watch is deferred until it resets.",
                        ))
                self._reserved[source] += reserve
                if source == "mercari" and self.budget_store is None:
                    self._mercari_reserved += reserve
                waiting = Future()
                self._inflight[key] = waiting
                owner = True
        if not owner:
            # The owner always resolves its Future, including import/adapter
            # failures, so followers cannot spin or begin a second request.
            return self._with_remaining(source, waiting.result())

        try:
            import watch_sources

            result = self._sanitize(
                source, watch_sources.search_watch_source(source, query)
            )
            if result["requests"] > SOURCE_REQUEST_BUDGETS[source]:
                result = {
                    "source": source, "status": "error", "listings": [],
                    "checked": 0,
                    "message": "Source exceeded its targeted-search request budget.",
                    "requests": result["requests"],
                }
        except Exception as exc:
            result = {
                "source": source, "status": "error", "listings": [],
                "checked": 0,
                "message": f"Targeted search failed ({type(exc).__name__}).",
                "requests": 0,
            }
        with self._lock:
            now = time.monotonic()
            if result["status"] in {"ok", "partial"}:
                self._cache[key] = (now + self.cache_ttl, copy.deepcopy(result))
            else:
                # Keep concurrent callers coherent briefly, but make an
                # explicitly unavailable source distinguishable from zero hits.
                self._cache[key] = (now + min(15.0, self.cache_ttl), copy.deepcopy(result))
                if result["status"] == "error":
                    self._backoff_until[source] = now + ERROR_BACKOFF_SECONDS
            future = self._inflight.pop(key)
            future.set_result(copy.deepcopy(result))
        with self._lock:
            return self._with_remaining(source, result)

    def search(self, watch: "Watch") -> dict[str, dict]:
        """Search every supported source for one watch, within hard budgets."""
        query = self.query_for(watch)
        # Each adapter has independent network I/O and its reservation is made
        # under `_lock` before this pool begins.  This avoids a slow rendered
        # Mercari request delaying the inexpensive eBay/TCGplayer counts.
        with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
            futures = {
                source: pool.submit(self._one_source, source, query)
                for source in SOURCES
            }
            return {source: futures[source].result() for source in SOURCES}