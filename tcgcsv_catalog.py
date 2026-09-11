"""Read-only, bounded-TTL TCGCSV card/set catalog used by Discord /watch.

This is intentionally separate from pricing.  It never searches a marketplace
and only reads TCGCSV's public category, group, and product endpoints.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass

import requests

from config import TCGCSV_CATEGORY, _HTTP_HEADERS


CATALOG_TTL = 60 * 60
CATALOG_RETRY_TTL = 120
CATALOG_BUILD_BUDGET = 12.0
CATALOG_WORKERS = 4
CATALOG_BATCH_SIZE = 32
# Discord requires an autocomplete response in roughly three seconds.  Leave
# margin for scheduling/serialization around this synchronous catalog budget.
CATALOG_AUTOCOMPLETE_BUDGET = 1.8
CATALOG_LOADING_VALUE = "__tcgcsv_catalog_loading__"

GAMES = ("pokemon", "mtg", "lorcana", "onepiece", "yugioh")
GAME_LABELS = {
    "pokemon": "Pokémon",
    "mtg": "Magic: The Gathering",
    "lorcana": "Disney Lorcana",
    "onepiece": "One Piece",
    "yugioh": "Yu-Gi-Oh!",
}

_RARITY_CODES = {
    "mtg": {
        "m": "Mythic Rare",
        "r": "Rare",
        "u": "Uncommon",
        "c": "Common",
    },
    "onepiece": {
        "c": "Common",
        "uc": "Uncommon",
        "r": "Rare",
        "sr": "Super Rare",
        "sec": "Secret Rare",
        "sp": "Special Rare",
        "tr": "Treasure Rare",
        "l": "Leader",
        "p": "Promo",
    },
}


def normalize_catalog_text(value: str) -> str:
    """Normalize searchable catalog text without importing the Discord bot."""
    import re

    return " ".join(re.sub(r"[\W_]+", " ", value.casefold()).split())


def contains_catalog_phrase(value: str, phrase: str) -> bool:
    """Whole normalized words only: ``Mew`` must not match ``Mewtwo``."""
    needle = normalize_catalog_text(phrase)
    return bool(needle) and f" {needle} " in f" {normalize_catalog_text(value)} "


def canonical_rarity(game: str, value: str) -> str:
    """Turn terse catalog rarity codes into readable, stable watch labels."""
    normalized = normalize_catalog_text(value)
    code_label = _RARITY_CODES.get(game, {}).get(normalized)
    if code_label:
        return code_label
    # TCGCSV sometimes supplies a readable label instead of its code. Keep it
    # readable while normalizing whitespace/casing consistently for validation.
    for label in _RARITY_CODES.get(game, {}).values():
        if normalized == normalize_catalog_text(label):
            return label
    return " ".join(word.capitalize() for word in normalized.split())


def rarity_matches_title(game: str, rarity: str, value: str) -> bool:
    """Match an exact recognised rarity family against title/metadata aliases.

    A whole-word substring check alone is not enough: ``Rare`` is present in
    ``Ultra Rare``, ``Secret Rare``, and ``Super Rare``.  First resolve the
    longest recognised rarity phrase that the listing actually names, then
    compare that family exactly.  This similarly keeps ``Common`` distinct
    from ``Uncommon``.
    """
    wanted = normalize_catalog_text(rarity)
    aliases = {wanted}
    for code, label in _RARITY_CODES.get(game, {}).items():
        if normalize_catalog_text(label) == wanted:
            aliases.add(code)
    # These occur as readable catalog/API labels outside the compact code maps
    # above (especially Pokémon and Yu-Gi-Oh).  Longest-first resolution makes
    # the specific family win over its trailing "Rare" word.
    recognised = {
        "quarter century secret rare", "prismatic ultimate rare",
        "prismatic collectors rare", "special illustration rare",
        "illustration rare", "reverse holo rare", "double rare",
        "ultimate rare", "collector rare", "treasure rare", "special rare",
        "secret rare", "super rare", "ultra rare", "hyper rare", "holo rare",
        "mythic rare", "uncommon", "common", "rare", "promo", "leader",
    }
    recognised.update(normalize_catalog_text(label)
                      for labels in _RARITY_CODES.values() for label in labels.values())
    recognised.add(wanted)
    matches = [
        phrase for phrase in recognised
        if phrase and contains_catalog_phrase(value, phrase)
    ]
    if matches:
        observed = max(matches, key=lambda phrase: (len(phrase.split()), len(phrase)))
        return observed == wanted
    # Short catalog codes are retained as a fallback only when no readable
    # rarity family appeared, e.g. "(SEC)".
    return any(contains_catalog_phrase(value, alias) for alias in aliases - {wanted})


@dataclass(frozen=True)
class CatalogProduct:
    name: str
    rarity: str | None


@dataclass(frozen=True)
class CatalogSet:
    game: str
    group_id: int
    name: str
    abbreviation: str | None
    products: tuple[CatalogProduct, ...]
    abbreviation_unique: bool = False

    @property
    def token(self) -> str:
        # The group id makes duplicate group names/unusual Yu-Gi-Oh reprints
        # unambiguous.  It is an opaque catalog key, not user-supplied data.
        return f"{self.game}:{self.group_id}"


@dataclass(frozen=True)
class CatalogSnapshot:
    sets: tuple[CatalogSet, ...]
    complete: bool


@dataclass(frozen=True)
class CatalogSetLookup:
    """A card-verified lookup for the text currently in the set field.

    ``pending`` deliberately means "not proven either way", including a
    temporary upstream failure.  Callers must not translate it into a
    card-missing result.
    """

    sets: tuple[CatalogSet, ...]
    pending: bool


class TCGCSVWatchCatalog:
    """Thread-safe, in-process cache for autocomplete and server validation."""

    def __init__(self):
        self._snapshots: dict[str, CatalogSnapshot] = {}
        self._until: dict[str, float] = {}
        self._groups: dict[str, tuple[dict, ...]] = {}
        self._groups_until: dict[str, float] = {}
        self._products: dict[str, dict[int, tuple[CatalogProduct, ...]]] = {}
        self._products_until: dict[str, dict[int, float]] = {}
        self._failed_until: dict[str, dict[int, float]] = {}
        self._next_batch_at: dict[str, float] = {}
        self._building: set[str] = set()
        self._groups_building: set[str] = set()
        self._products_building: dict[str, set[int]] = {}
        self._lock = threading.Lock()
        # A no-game autocomplete may warm all supported games. Limit aggregate
        # TCGCSV traffic as well as each game's own worker pool.
        self._network_slots = threading.BoundedSemaphore(2)

    def _load_groups_until(
        self, game: str, deadline: float
    ) -> tuple[dict, ...] | None:
        """Fetch one game's group index once, respecting a shared deadline."""
        now = time.monotonic()
        with self._lock:
            cached = self._groups.get(game, ())
            if game in self._groups and now < self._groups_until.get(game, 0):
                return cached
            if game in self._groups_building:
                return None
            self._groups_building.add(game)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._network_slots.acquire(
                timeout=max(0, remaining)
            ):
                return None
            try:
                response = requests.get(
                    f"https://tcgcsv.com/tcgplayer/{TCGCSV_CATEGORY[game]}/groups",
                    headers=_HTTP_HEADERS,
                    timeout=max(0.05, min(4.0, deadline - time.monotonic())),
                )
                response.raise_for_status()
                groups = response.json().get("results", [])
            except (requests.RequestException, ValueError, AttributeError):
                return None
            finally:
                self._network_slots.release()
            valid_groups = tuple(
                group for group in groups
                if group.get("groupId") is not None
                and str(group.get("name") or "").strip()
            )
            with self._lock:
                allowed = {int(group["groupId"]) for group in valid_groups}
                self._products[game] = {
                    group_id: products
                    for group_id, products in self._products.get(game, {}).items()
                    if group_id in allowed
                }
                self._products_until[game] = {
                    group_id: expiry
                    for group_id, expiry in self._products_until.get(game, {}).items()
                    if group_id in allowed
                }
                self._failed_until[game] = {
                    group_id: retry
                    for group_id, retry in self._failed_until.get(game, {}).items()
                    if group_id in allowed
                }
                self._groups[game] = valid_groups
                self._groups_until[game] = time.monotonic() + CATALOG_TTL
            return valid_groups
        finally:
            with self._lock:
                self._groups_building.discard(game)

    def _store_targeted_products(
        self,
        game: str,
        groups: tuple[dict, ...],
        results: list[tuple[dict, list[CatalogProduct] | None]],
        unfinished: tuple[dict, ...],
    ) -> None:
        """Persist targeted product rows and make them usable before full scans."""
        now = time.monotonic()
        with self._lock:
            saved = self._products.setdefault(game, {})
            expiries = self._products_until.setdefault(game, {})
            failures = self._failed_until.setdefault(game, {})
            building = self._products_building.setdefault(game, set())
            for group, products in results:
                group_id = int(group["groupId"])
                building.discard(group_id)
                if products is None:
                    failures[group_id] = now + CATALOG_RETRY_TTL
                else:
                    saved[group_id] = tuple(products)
                    expiries[group_id] = now + CATALOG_TTL
                    failures.pop(group_id, None)
            for group in unfinished:
                # A deadline is not a verified empty group. Back it off so
                # rapid Discord keystrokes cannot create a download storm.
                group_id = int(group["groupId"])
                building.discard(group_id)
                failures[group_id] = now + CATALOG_RETRY_TTL
            snapshot = self._snapshot_from_products(
                game, groups, dict(saved), dict(expiries)
            )
            if snapshot.sets:
                self._snapshots[game] = snapshot
                self._until[game] = now + CATALOG_TTL
                self._next_batch_at[game] = (
                    float("inf") if snapshot.complete else now
                )

    def matching_sets_for_typed_set(
        self,
        card_name: str,
        set_name: str,
        game: str | None = None,
        budget: float = CATALOG_AUTOCOMPLETE_BUDGET,
    ) -> CatalogSetLookup:
        """Find a typed set without scanning arbitrary product groups.

        Group indexes are fetched in parallel when no game was selected, then
        only groups whose names contain the typed set phrase have their products
        downloaded.  A product still has to contain the exact card phrase before
        it becomes an autocomplete selection.
        """
        card_phrase = normalize_catalog_text(card_name)
        set_phrase = normalize_catalog_text(set_name)
        games = (game,) if game in GAMES else GAMES
        if len(card_phrase) < 2 or len(set_phrase) < 2:
            return CatalogSetLookup((), False)
        deadline = time.monotonic() + max(0.01, budget)

        group_rows: dict[str, tuple[dict, ...] | None] = {}
        # A selected game makes one request. With no game, only inexpensive group
        # indexes are concurrent; product downloads remain limited below.
        workers = min(len(games), CATALOG_WORKERS)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        futures: dict[concurrent.futures.Future, str] = {}
        try:
            futures = {
                pool.submit(self._load_groups_until, candidate, deadline): candidate
                for candidate in games
            }
            try:
                for future in concurrent.futures.as_completed(
                    futures, timeout=max(0.01, deadline - time.monotonic())
                ):
                    group_rows[futures[future]] = future.result()
            except concurrent.futures.TimeoutError:
                pass
            finally:
                for future in futures:
                    future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            # A worker failure is still not evidence that the card/set pair is
            # absent. Let the caller show its retry sentinel.
            group_rows = {}
        for future, candidate in futures.items():
            if candidate not in group_rows and future.done() and not future.cancelled():
                try:
                    group_rows[candidate] = future.result()
                except Exception:
                    group_rows[candidate] = None

        candidates: list[tuple[str, dict]] = []
        pending = any(groups is None for groups in group_rows.values())
        # Futures that could not start before this call's deadline are unknown,
        # not proof that the game lacks a matching set.
        pending = pending or len(group_rows) != len(games)
        for candidate_game, groups in group_rows.items():
            if groups is None:
                continue
            candidates.extend(
                (candidate_game, group) for group in groups
                if contains_catalog_phrase(str(group.get("name") or ""), set_phrase)
            )

        fetches: list[tuple[str, dict]] = []
        with self._lock:
            now = time.monotonic()
            for candidate_game, group in candidates:
                group_id = int(group["groupId"])
                products = self._products.get(candidate_game, {}).get(group_id)
                expiry = self._products_until.get(candidate_game, {}).get(group_id, 0)
                if products is not None and now < expiry:
                    continue
                if group_id in self._products_building.get(candidate_game, set()):
                    pending = True
                elif now < self._failed_until.get(candidate_game, {}).get(group_id, 0):
                    # Failure/backoff cannot be advertised as a negative lookup.
                    pending = True
                else:
                    self._products_building.setdefault(candidate_game, set()).add(group_id)
                    fetches.append((candidate_game, group))

        results_by_game: dict[str, list[tuple[dict, list[CatalogProduct] | None]]] = {
            candidate: [] for candidate in games
        }
        unfinished_by_game: dict[str, list[dict]] = {
            candidate: [] for candidate in games
        }
        if fetches and time.monotonic() < deadline:
            def fetch_target(candidate_game: str, group: dict):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._network_slots.acquire(
                    timeout=max(0, remaining)
                ):
                    return candidate_game, (group, None)
                try:
                    base = (
                        f"https://tcgcsv.com/tcgplayer/"
                        f"{TCGCSV_CATEGORY[candidate_game]}"
                    )
                    return candidate_game, self._fetch_products(
                        base, group, candidate_game,
                        timeout=max(0.05, min(4.0, deadline - time.monotonic())),
                    )
                finally:
                    self._network_slots.release()

            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(CATALOG_WORKERS, len(fetches))
            )
            futures: dict[concurrent.futures.Future, tuple[str, dict]] = {}
            try:
                futures = {
                    pool.submit(fetch_target, candidate_game, group): (candidate_game, group)
                    for candidate_game, group in fetches
                }
                completed: set[tuple[str, int]] = set()
                try:
                    for future in concurrent.futures.as_completed(
                        futures, timeout=max(0.01, deadline - time.monotonic())
                    ):
                        candidate_game, result = future.result()
                        results_by_game[candidate_game].append(result)
                        completed.add((candidate_game, int(result[0]["groupId"])))
                except concurrent.futures.TimeoutError:
                    pending = True
                finally:
                    for future in futures:
                        future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                for candidate_game, group in fetches:
                    if (candidate_game, int(group["groupId"])) not in completed:
                        unfinished_by_game[candidate_game].append(group)
                        pending = True
            except Exception:
                pending = True
                for candidate_game, group in fetches:
                    unfinished_by_game[candidate_game].append(group)
        elif fetches:
            pending = True
            for candidate_game, group in fetches:
                unfinished_by_game[candidate_game].append(group)

        for candidate_game, groups in group_rows.items():
            if groups is not None:
                self._store_targeted_products(
                    candidate_game, groups, results_by_game[candidate_game],
                    tuple(unfinished_by_game[candidate_game]),
                )

        matches: list[CatalogSet] = []
        with self._lock:
            for candidate_game, group in candidates:
                group_id = int(group["groupId"])
                products = self._products.get(candidate_game, {}).get(group_id)
                if products is None or time.monotonic() >= self._products_until.get(
                    candidate_game, {}
                ).get(group_id, 0):
                    pending = True
                    continue
                if any(contains_catalog_phrase(product.name, card_phrase)
                       for product in products):
                    snapshot = self._snapshots.get(candidate_game)
                    card_set = next((
                        entry for entry in (snapshot.sets if snapshot else ())
                        if entry.group_id == group_id
                    ), None)
                    if card_set:
                        matches.append(card_set)
        return CatalogSetLookup(
            tuple(sorted(matches, key=lambda item: (GAME_LABELS[item.game], item.name))),
            pending,
        )

    def is_ready(self, game: str) -> bool:
        with self._lock:
            return game in self._snapshots and time.monotonic() < self._until.get(game, 0)

    def needs_refresh(self, game: str) -> bool:
        """Whether a cold, expired, or partial catalog should fetch another batch."""
        now = time.monotonic()
        with self._lock:
            snapshot = self._snapshots.get(game)
            if snapshot is None or now >= self._until.get(game, 0):
                return True
            return not snapshot.complete and now >= self._next_batch_at.get(game, 0)

    def ensure_game(self, game: str) -> bool:
        """Refresh one game's catalog, with a hard wall-clock budget.

        This synchronous method is deliberately called through asyncio.to_thread
        by Discord code, keeping gateway/autocomplete work non-blocking.
        """
        if game not in GAMES:
            return False
        now = time.monotonic()
        with self._lock:
            existing = self._snapshots.get(game)
            if (existing is not None and existing.complete
                    and now < self._until.get(game, 0)):
                return game in self._snapshots
            if (existing is not None and not existing.complete
                    and now < self._next_batch_at.get(game, 0)):
                return True
            if game in self._building:
                return False
            self._building.add(game)
        try:
            with self._network_slots:
                snapshot = self._build_game(game)
        except Exception:
            # Autocomplete must never surface a network/parser failure through
            # the Discord gateway. Keep any last good snapshot below.
            snapshot = CatalogSnapshot((), False)
        finally:
            with self._lock:
                self._building.discard(game)
        with self._lock:
            if snapshot.sets:
                self._snapshots[game] = snapshot
                # Partial snapshots are useful and safe (they only offer
                # catalog-confirmed choices), so serve them while later
                # autocomplete calls advance the missing group batches.
                self._until[game] = time.monotonic() + CATALOG_TTL
                self._next_batch_at[game] = (
                    float("inf") if snapshot.complete else time.monotonic()
                )
                return True
            # A failure does not erase the last known-good snapshot, but does
            # stop a burst of autocomplete calls from repeatedly hitting TCGCSV.
            made_progress = bool(self._products.get(game))
            self._next_batch_at[game] = time.monotonic() + (
                0 if made_progress else CATALOG_RETRY_TTL
            )
            return game in self._snapshots

    def _build_game(self, game: str) -> CatalogSnapshot:
        category = TCGCSV_CATEGORY[game]
        base = f"https://tcgcsv.com/tcgplayer/{category}"
        now = time.monotonic()
        with self._lock:
            valid_groups = self._groups.get(game, ())
            groups_expired = now >= self._groups_until.get(game, 0)
        if not valid_groups or groups_expired:
            try:
                response = requests.get(
                    f"{base}/groups", headers=_HTTP_HEADERS, timeout=4
                )
                response.raise_for_status()
                groups = response.json().get("results", [])
            except (requests.RequestException, ValueError, AttributeError):
                with self._lock:
                    previous = self._snapshots.get(game)
                return previous or CatalogSnapshot((), False)
            valid_groups = tuple(
                group for group in groups
                if group.get("groupId") is not None
                and str(group.get("name") or "").strip()
            )
            with self._lock:
                # Retain completed product rows that still belong to a current
                # group. This avoids re-downloading a whole category on TTL
                # refresh and keeps progress across partial batches.
                allowed = {int(group["groupId"]) for group in valid_groups}
                old = self._products.get(game, {})
                self._products[game] = {
                    group_id: products for group_id, products in old.items()
                    if group_id in allowed
                }
                self._products_until[game] = {
                    group_id: expiry for group_id, expiry
                    in self._products_until.get(game, {}).items()
                    if group_id in allowed
                }
                self._failed_until[game] = {
                    group_id: retry for group_id, retry
                    in self._failed_until.get(game, {}).items()
                    if group_id in allowed
                }
                self._groups[game] = valid_groups
                self._groups_until[game] = time.monotonic() + CATALOG_TTL

        with self._lock:
            products_by_group = self._products.setdefault(game, {})
            products_until = self._products_until.setdefault(game, {})
            failed_until = self._failed_until.setdefault(game, {})
            now = time.monotonic()
            missing = [
                group for group in valid_groups
                if (int(group["groupId"]) not in products_by_group
                    or now >= products_until.get(int(group["groupId"]), 0))
                and now >= failed_until.get(int(group["groupId"]), 0)
            ]
            missing.sort(
                key=lambda group: (
                    int(group["groupId"]) in failed_until,
                    int(group["groupId"]),
                )
            )
        # Work only an incremental batch. Failed groups are retried after their
        # short backoff, while untouched groups stay ahead of them so one
        # transient outage cannot starve every later set.
        batch = missing[:CATALOG_BATCH_SIZE]
        if not batch:
            with self._lock:
                product_rows = dict(self._products.get(game, {}))
                product_expiry = dict(self._products_until.get(game, {}))
            return self._snapshot_from_products(
                game, valid_groups, product_rows, product_expiry
            )

        deadline = time.monotonic() + CATALOG_BUILD_BUDGET
        results: list[tuple[dict, list[CatalogProduct] | None]] = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=CATALOG_WORKERS)
        future_groups = {
            pool.submit(self._fetch_products, base, group, game): group
            for group in batch
        }
        futures = list(future_groups)
        try:
            for future in concurrent.futures.as_completed(
                futures, timeout=max(0.01, deadline - time.monotonic())
            ):
                group, products = future.result()
                results.append((group, products))
        except concurrent.futures.TimeoutError:
            pass
        finally:
            for future in futures:
                future.cancel()
            # Do not wait for socket timeouts after the autocomplete budget.
            pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            saved = self._products.setdefault(game, {})
            expiries = self._products_until.setdefault(game, {})
            failures = self._failed_until.setdefault(game, {})
            completed_ids = {int(group["groupId"]) for group, _ in results}
            for group, products in results:
                group_id = int(group["groupId"])
                if products is None:
                    failures[group_id] = time.monotonic() + CATALOG_RETRY_TTL
                else:
                    # Empty is a successful response and must not be repeatedly
                    # fetched as though it were a network failure.
                    saved[group_id] = tuple(products)
                    expiries[group_id] = time.monotonic() + CATALOG_TTL
                    failures.pop(group_id, None)
            # Futures that exceeded the batch deadline are retryable failures,
            # not empty sets. Marking them lets subsequent calls progress to
            # later untouched groups instead of restarting the same slow batch.
            for future, group in future_groups.items():
                group_id = int(group["groupId"])
                if group_id not in completed_ids:
                    failures[group_id] = time.monotonic() + CATALOG_RETRY_TTL
            product_rows = dict(saved)
            product_expiry = dict(expiries)
        return self._snapshot_from_products(
            game, valid_groups, product_rows, product_expiry
        )

    @staticmethod
    def _snapshot_from_products(
        game: str, groups: tuple[dict, ...],
        products_by_group: dict[int, tuple[CatalogProduct, ...]],
        products_until: dict[int, float],
    ) -> CatalogSnapshot:
        now = time.monotonic()
        abbreviation_counts: dict[str, int] = {}
        for group in groups:
            abbreviation = normalize_catalog_text(str(group.get("abbreviation") or ""))
            if abbreviation:
                abbreviation_counts[abbreviation] = (
                    abbreviation_counts.get(abbreviation, 0) + 1
                )
        sets = tuple(
            CatalogSet(
                game=game,
                group_id=int(group["groupId"]),
                name=str(group["name"]).strip(),
                abbreviation=(str(group["abbreviation"]).strip() or None)
                if group.get("abbreviation") is not None else None,
                products=products_by_group[int(group["groupId"])],
                abbreviation_unique=(
                    game != "yugioh"
                    and bool(normalize_catalog_text(
                        str(group.get("abbreviation") or "")
                    ))
                    and abbreviation_counts[
                        normalize_catalog_text(
                            str(group.get("abbreviation") or "")
                        )
                    ] == 1
                ),
            )
            for group in groups
            if (products_by_group.get(int(group["groupId"]))
                and now < products_until.get(int(group["groupId"]), 0))
        )
        return CatalogSnapshot(
            sets,
            all(
                group_id in products_by_group
                and now < products_until.get(group_id, 0)
                for group_id in (int(group["groupId"]) for group in groups)
            ),
        )

    @staticmethod
    def _fetch_products(
        base: str, group: dict, game: str, timeout: float = 4
    ) -> tuple[dict, list[CatalogProduct] | None]:
        try:
            response = requests.get(
                f"{base}/{group['groupId']}/products",
                headers=_HTTP_HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            raw_products = response.json().get("results", [])
        except (requests.RequestException, ValueError, AttributeError):
            # None is deliberately distinct from a verified empty group; the
            # latter is cached while the former is retried in a later batch.
            return group, None
        products = []
        for product in raw_products:
            name = str(product.get("name") or "").strip()
            if not name:
                continue
            extended = product.get("extendedData") or []
            rarity = next(
                (
                    str(entry.get("value") or "").strip()
                    for entry in extended
                    if str(entry.get("name") or "").casefold() == "rarity"
                    and str(entry.get("value") or "").strip()
                ),
                None,
            )
            # Product-only set entries do not represent a card name and should
            # never make a set appear in card autocomplete results.
            has_card_number = any(
                str(entry.get("name") or "").casefold() == "number"
                for entry in extended
            )
            if has_card_number or rarity:
                products.append(CatalogProduct(
                    name=name,
                    rarity=canonical_rarity(game, rarity) if rarity else None,
                ))
        return group, products

    def matching_sets(self, card_name: str, game: str | None = None) -> list[CatalogSet] | None:
        """Return matching catalog sets, or None while one needed cache is cold."""
        wanted = normalize_catalog_text(card_name)
        games = (game,) if game else GAMES
        if not wanted or any(not self.is_ready(candidate) for candidate in games):
            return None
        matches: list[CatalogSet] = []
        with self._lock:
            snapshots = [self._snapshots.get(candidate) for candidate in games]
        for snapshot in snapshots:
            if snapshot is None:
                continue
            for card_set in snapshot.sets:
                if any(
                    contains_catalog_phrase(product.name, wanted)
                    for product in card_set.products
                ):
                    matches.append(card_set)
        return sorted(matches, key=lambda item: (GAME_LABELS[item.game], item.name))

    def rarities(self, card_name: str, set_token: str) -> list[str] | None:
        """Return rarities for exactly the card + selected catalog set."""
        card_set = self.resolve_set(set_token)
        if card_set is None:
            return [] if self._known_token(set_token) else None
        wanted = normalize_catalog_text(card_name)
        return sorted({
            product.rarity
            for product in card_set.products
            if product.rarity and contains_catalog_phrase(product.name, wanted)
        }, key=str.casefold)

    def resolve_set(self, set_token: str) -> CatalogSet | None:
        try:
            game, raw_id = set_token.split(":", 1)
            group_id = int(raw_id)
        except (ValueError, AttributeError):
            return None
        if game not in GAMES or not self.is_ready(game):
            return None
        with self._lock:
            snapshot = self._snapshots.get(game)
        return next(
            (entry for entry in (snapshot.sets if snapshot else ())
             if entry.group_id == group_id),
            None,
        )

    def _known_token(self, set_token: str) -> bool:
        try:
            game, _raw_id = set_token.split(":", 1)
        except (ValueError, AttributeError):
            return False
        return game in GAMES and self.is_ready(game)

    def validate(
        self,
        card_name: str,
        game: str | None,
        set_token: str | None,
        rarity: str | None,
    ) -> tuple[CatalogSet | None, str | None, str | None]:
        """Validate selections against the cache.

        Returns (set, rarity, error).  No free-form set or rarity is accepted.
        """
        if game is not None and game not in GAMES:
            return None, None, "Choose a game from the provided list."
        if set_token == CATALOG_LOADING_VALUE:
            return None, None, (
                "That set is still loading. Please retry set autocomplete shortly "
                "and choose a catalog result before saving your watch."
            )
        if rarity and not set_token:
            return None, None, "Choose a set before choosing a rarity."
        if not set_token:
            return None, None, None
        card_set = self.resolve_set(set_token)
        if card_set is None:
            return None, None, (
                "That set is not a current catalog selection. Please choose it "
                "from autocomplete after the catalog finishes loading."
            )
        if game and card_set.game != game:
            return None, None, "The selected set belongs to a different game."
        matching_sets = self.matching_sets(card_name, card_set.game)
        if matching_sets is None:
            return None, None, "The card catalog is still loading. Please try again shortly."
        if card_set not in matching_sets:
            return None, None, "That set does not contain the entered card name."
        if rarity:
            scoped_rarities = self.rarities(card_name, set_token)
            if scoped_rarities is None or rarity not in scoped_rarities:
                return None, None, (
                    "That rarity is not available for this card in the selected set."
                )
        return card_set, rarity, None