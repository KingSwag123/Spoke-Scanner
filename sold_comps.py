"""Cached recent eBay sold-price enrichment backed by an Apify connector."""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from config import (
    POST_TO_DISCORD,
    SOLD_COMPS_CACHE_FILE,
    SOLD_COMPS_CACHE_TTL,
    SOLD_COMPS_DAILY_LOOKUP_LIMIT,
    SOLD_COMPS_EMPTY_TTL,
)

_ROOT = Path(__file__).resolve().parent
_HELPER = _ROOT / "apify_sold_comps.cjs"
_CACHE_PATH = _ROOT / SOLD_COMPS_CACHE_FILE
_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Event] = {}

_STOP_WORDS = {
    "a", "an", "and", "card", "cards", "for", "from", "in", "magic",
    "new", "of", "one", "piece", "pokemon", "the", "tcg", "trading",
    "with", "wotc", "english", "en", "sealed", "factory", "genuine",
    "authentic", "official", "free", "shipping",
}
_BAD_COMP_WORDS = {
    "empty", "proxy", "replica", "repack", "custom", "digital", "code",
    "damaged", "read description",
}
_VARIANT_PATTERNS = (
    ("acrylic", re.compile(r"\bacrylic\b", re.I)),
    ("half", re.compile(r"\bhalf\b", re.I)),
    ("mini", re.compile(r"\bmini\b", re.I)),
    ("opened", re.compile(r"\bopen(?:ed)?\b", re.I)),
    ("lot", re.compile(r"\blot\b", re.I)),
    ("first_edition", re.compile(r"\b(?:1st|first)\s+edition\b", re.I)),
    ("unlimited", re.compile(r"\bunlimited\b", re.I)),
    ("shadowless", re.compile(r"\bshadowless\b", re.I)),
    ("reverse_holo", re.compile(r"\breverse\s+holo(?:foil|graphic)?\b", re.I)),
    ("non_holo", re.compile(r"\bnon[- ]?holo\b", re.I)),
    ("holo", re.compile(r"(?<!reverse )\bholo(?:foil|graphic)?\b", re.I)),
    ("etched_foil", re.compile(r"\betched\s+foil\b", re.I)),
    ("surge_foil", re.compile(r"\bsurge\s+foil\b", re.I)),
    ("nonfoil", re.compile(r"\bnon[- ]?foil\b", re.I)),
    ("foil", re.compile(r"(?<!etched )(?<!surge )\bfoil\b", re.I)),
    ("stamped", re.compile(r"\bstamp(?:ed)?\b", re.I)),
    ("promo", re.compile(r"\bpromo\b", re.I)),
    ("misprint", re.compile(r"\b(?:error|misprint)\b", re.I)),
    ("serialized", re.compile(r"\bseriali[sz]ed\b|\b\d+\s*/\s*\d+\s+serial\b", re.I)),
    ("borderless", re.compile(r"\bborderless\b", re.I)),
    ("showcase", re.compile(r"\bshowcase\b", re.I)),
    ("extended_art", re.compile(r"\bextended\s+art\b", re.I)),
    ("alt_art", re.compile(r"\b(?:alt|alternate)\s+art\b", re.I)),
    ("full_art", re.compile(r"\bfull\s+art\b", re.I)),
)
_PACKAGE_PATTERNS = (
    ("booster_case", re.compile(
        r"\bbooster\s+(?:box\s+)?case\b|\bcase\s+of\b.*\bboosters?\b",
        re.I,
    )),
    ("deck_display", re.compile(
        r"\b(?:starter|commander|theme|structure)\s+deck\b.*\bdisplay\b"
        r"|\bdisplay\b.*\b(?:starter|commander|theme|structure)\s+deck\b",
        re.I,
    )),
    ("booster_box", re.compile(
        r"\b(?:collector|play|draft|set|jumpstart)?\s*booster\s+"
        r"(?:box|display)\b|\bdisplay\b.*\bbooster\b",
        re.I,
    )),
    ("elite_trainer_box", re.compile(r"\belite\s+trainer\s+box\b|\betb\b", re.I)),
    ("booster_bundle", re.compile(r"\bbooster\s+bundle\b", re.I)),
    ("build_battle", re.compile(r"\bbuild\s*(?:&|and)\s*battle\b", re.I)),
    ("prerelease", re.compile(r"\bpre[- ]?release\s+(?:pack|kit|box)\b", re.I)),
    ("trove", re.compile(r"\billumineer'?s?\s+trove\b|\btrove\b", re.I)),
    ("blister", re.compile(r"\bblister\b", re.I)),
    ("booster_pack", re.compile(
        r"\bbooster\s+pack\b|\b(?:collector|play|draft|set)\s+booster\b"
        r"|\bomega\s+pack\b",
        re.I,
    )),
    ("gift_bundle", re.compile(r"\bgift\s+bundle\b", re.I)),
    ("collection_box", re.compile(
        r"\b(?:premium|poster|special)?\s*collection\s+box\b"
        r"|\bscene\s+box\b",
        re.I,
    )),
    ("starter_deck", re.compile(r"\bstarter\s+deck\b", re.I)),
    ("commander_deck", re.compile(r"\bcommander\s+deck\b", re.I)),
    ("theme_deck", re.compile(r"\btheme\s+deck\b", re.I)),
    ("structure_deck", re.compile(r"\bstructure\s+deck\b", re.I)),
    ("double_pack", re.compile(r"\bdouble\s+pack\b", re.I)),
    ("beginner_box", re.compile(r"\bbeginner\s+box\b", re.I)),
    ("tin", re.compile(r"\b(?:mini\s+)?tin\b", re.I)),
    ("bundle", re.compile(r"\bbundle\b", re.I)),
    ("sealed_box", re.compile(r"\bsealed\s+box\b|\bdisplay\s+box\b", re.I)),
)
_JP_RE = re.compile(r"\b(?:japanese|japan|jpn|jp)\b", re.I)
_OTHER_LANGUAGE_RE = re.compile(
    r"\b(?:chinese|korean|simplified chinese|traditional chinese|kr|cn)\b",
    re.I,
)
_GRADED_RE = re.compile(
    r"\b(psa|bgs|cgc|sgc|csg|ace)\s*[-:]?\s*"
    r"(?:(?:gem\s+)?(?:mint|mt)\s*)?(\d{1,2}(?:\.\d)?)\b",
    re.I,
)
_SLAB_RE = re.compile(
    r"\b(?:psa|bgs|cgc|sgc|csg|ace|graded|slabbed?|gem\s+(?:mint|mt))\b",
    re.I,
)


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _package_kind(value: str) -> str | None:
    return next(
        (kind for kind, pattern in _PACKAGE_PATTERNS if pattern.search(value)),
        None,
    )


def _grade(value: str) -> tuple[str, float] | None:
    match = _GRADED_RE.search(value)
    if not match:
        return None
    score = float(match.group(2))
    if not 1 <= score <= 10:
        return None
    return match.group(1).casefold(), score


def _variant_signature(value: str) -> frozenset[str]:
    return frozenset(
        name for name, pattern in _VARIANT_PATTERNS if pattern.search(value)
    )


def _quantity_signature(value: str, package_kind: str | None) -> int | None:
    patterns = [
        r"\blot\s+of\s+(\d+)\b",
        r"\bcase\s+of\s+(\d+)\b",
        r"\b(\d+)\s*[x×]\s*(?:boxes?|decks?|tins?|etbs?|blisters?|bundles?)\b",
        r"\b[x×]\s*(\d+)\b",
        r"\b(\d+)\s+(?:boxes?|decks?|tins?|etbs?|blisters?|bundles?|cases?)\b",
    ]
    if package_kind in {"booster_pack", "blister"}:
        patterns.extend((
            r"\b(\d+)[ -]?pack\b",
            r"\b(\d+)\s+booster\s+packs?\b",
        ))
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            count = int(match.group(1))
            if count > 1:
                return count
    return None


def _important_tokens(value: str) -> set[str]:
    return {
        token for token in _normalize(value).split()
        if token not in _STOP_WORDS and len(token) > 1
    }


def _same_item(
    candidate: dict[str, Any],
    identity: str,
    listing_title: str,
    language: str,
    sealed: bool,
) -> bool:
    title = str(candidate.get("title") or "")
    normalized = _normalize(title)
    target = identity or listing_title
    target_normalized = _normalize(target)
    if not title or any(word in normalized for word in _BAD_COMP_WORDS):
        return False

    target_kind = _package_kind(target)
    candidate_kind = _package_kind(title)
    if sealed and (target_kind is None or candidate_kind != target_kind):
        return False
    if not sealed and candidate_kind is not None:
        return False
    if (" case" in f" {normalized}" or "case of" in normalized) != (
        " case" in f" {target_normalized}" or "case of" in target_normalized
    ):
        return False
    if _variant_signature(f"{target} {listing_title}") != _variant_signature(title):
        return False
    if _quantity_signature(
        f"{target} {listing_title}", target_kind
    ) != _quantity_signature(title, candidate_kind):
        return False

    target_jp = language.casefold() == "japanese" or bool(
        _JP_RE.search(f"{target} {listing_title}")
    )
    candidate_jp = bool(_JP_RE.search(title))
    if target_jp != candidate_jp:
        return False
    if _OTHER_LANGUAGE_RE.search(title):
        return False

    target_grade = _grade(f"{target} {listing_title}")
    candidate_grade = _grade(title)
    target_is_slab = bool(_SLAB_RE.search(f"{target} {listing_title}"))
    candidate_is_slab = bool(_SLAB_RE.search(title))
    if target_is_slab:
        # A slab without an explicit recognized company+score cannot be
        # compared safely. Never mix unknown grades or raw sales.
        if target_grade is None or candidate_grade != target_grade:
            return False
    elif candidate_is_slab:
        return False

    if sealed and str(candidate.get("condition") or "").casefold() not in {
        "", "brand new", "new",
    }:
        return False

    # Set/card numbers and product codes are identity-critical.
    target_numbers = set(re.findall(r"\b\d+\b", target_normalized))
    candidate_numbers = set(re.findall(r"\b\d+\b", normalized))
    if target_numbers and not target_numbers.issubset(candidate_numbers):
        return False

    important = _important_tokens(target)
    if len(important) < 2:
        return False
    overlap = len(important & _important_tokens(title)) / len(important)
    return overlap >= 0.75


def _load_cache() -> dict[str, Any]:
    try:
        data = json.loads(_CACHE_PATH.read_text())
        if isinstance(data, dict) and data.get("version") == 1:
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "entries": {}, "usage": {}}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        temporary = _CACHE_PATH.with_suffix(_CACHE_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(cache, separators=(",", ":")))
        os.replace(temporary, _CACHE_PATH)
    except OSError as exc:
        print(f"[SOLD-COMPS][WARN] Cache write failed: {type(exc).__name__}")


def _cached(cache: dict[str, Any], key: str) -> tuple[bool, dict | None]:
    entry = cache.get("entries", {}).get(key)
    if not isinstance(entry, dict):
        return False, None
    value = entry.get("value")
    ttl = SOLD_COMPS_CACHE_TTL if value else SOLD_COMPS_EMPTY_TTL
    if time.time() - float(entry.get("cached_at", 0)) > ttl:
        return False, None
    return True, value if isinstance(value, dict) else None


def _reserve_lookup(cache: dict[str, Any]) -> bool:
    today = date.today().isoformat()
    usage = cache.get("usage")
    if not isinstance(usage, dict) or usage.get("day") != today:
        usage = {"day": today, "count": 0}
        cache["usage"] = usage
    if int(usage.get("count", 0)) >= SOLD_COMPS_DAILY_LOOKUP_LIMIT:
        return False
    usage["count"] = int(usage.get("count", 0)) + 1
    _save_cache(cache)
    return True


def _actor_lookup(query: str) -> list[dict] | None:
    try:
        completed = subprocess.run(
            ["node", str(_HELPER)],
            input=json.dumps({"query": query[:120]}),
            text=True,
            capture_output=True,
            cwd=_ROOT,
            timeout=75,
            check=False,
        )
        payload = json.loads(completed.stdout)
        if completed.returncode or not payload.get("ok"):
            print("[SOLD-COMPS][WARN] Apify lookup unavailable")
            return None
        items = payload.get("items")
        return items if isinstance(items, list) else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        print("[SOLD-COMPS][WARN] Apify lookup failed")
        return None


def _search_query(identity: str, listing_title: str, language: str) -> str:
    query = identity.strip()
    combined = f"{identity} {listing_title}"
    grade = _grade(combined)
    if grade and not _grade(query):
        score = f"{grade[1]:g}"
        query += f" {grade[0].upper()} {score}"
    if (
        language.casefold() == "japanese" or _JP_RE.search(combined)
    ) and not _JP_RE.search(query):
        query += " Japanese"
    elif language.casefold() == "english":
        query += " -Japanese -JP -Chinese -Korean"
    normalized = _normalize(combined)
    for modifier in ("half", "mini", "acrylic", "case", "lot"):
        if modifier not in normalized:
            query += f" -{modifier}"
    return query


def _summarize(
    rows: list[dict],
    identity: str,
    listing_title: str,
    language: str,
    sealed: bool,
) -> dict | None:
    matches: list[dict] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not _same_item(
            row, identity, listing_title, language, sealed
        ):
            continue
        if str(row.get("soldCurrency") or "USD") != "USD":
            continue
        try:
            total = float(row.get("totalPrice") or row["soldPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        item_id = str(row.get("itemId") or row.get("url") or "")
        if not item_id or item_id in seen_ids or not (0 < total < 1_000_000):
            continue
        seen_ids.add(item_id)
        matches.append({
            "total": total,
            "date": str(row.get("endedAt") or ""),
            "url": str(row.get("url") or ""),
        })

    if len(matches) < 3:
        return None
    median = statistics.median(row["total"] for row in matches)
    matches = [
        row for row in matches
        if median * 0.65 <= row["total"] <= median * 1.55
    ]
    if len(matches) < 3:
        return None
    recent = matches[:5]
    totals = [row["total"] for row in recent]
    return {
        "average": statistics.fmean(totals),
        "median": statistics.median(totals),
        "count": len(recent),
        "sales": recent,
    }


def get_sold_comps(
    identity: str,
    listing_title: str = "",
    language: str = "Unknown",
    sealed: bool = False,
) -> dict | None:
    """Return recent comparable USD sold totals, or None without blocking alerts."""
    if not POST_TO_DISCORD or not identity.strip():
        return None
    grade = _grade(f"{identity} {listing_title}")
    grade_key = (
        f"{grade[0]}-{grade[1]:g}" if grade else "raw"
    )
    key = "|".join((
        _normalize(identity),
        language.casefold(),
        "sealed" if sealed else "single",
        grade_key,
    ))

    with _LOCK:
        cache = _load_cache()
        hit, value = _cached(cache, key)
        if hit:
            return value
        waiter = _INFLIGHT.get(key)
        if waiter is None:
            if not _reserve_lookup(cache):
                return None
            waiter = threading.Event()
            _INFLIGHT[key] = waiter
            owner = True
        else:
            owner = False

    if not owner:
        waiter.wait(80)
        with _LOCK:
            return _cached(_load_cache(), key)[1]

    value: dict | None = None
    rows: list[dict] | None = None
    try:
        rows = _actor_lookup(_search_query(identity, listing_title, language))
        if rows is not None:
            value = _summarize(rows, identity, listing_title, language, sealed)
            with _LOCK:
                cache = _load_cache()
                cache.setdefault("entries", {})[key] = {
                    "cached_at": time.time(),
                    "value": value,
                }
                _save_cache(cache)
        return value
    finally:
        with _LOCK:
            event = _INFLIGHT.pop(key, None)
            if event:
                event.set()


def format_sold_comps(comps: dict) -> str:
    """Format a compact Discord field with linked recent sold totals."""
    links = []
    for sale in comps["sales"]:
        price = f"${sale['total']:,.2f}"
        links.append(f"[{price}]({sale['url']})" if sale.get("url") else price)
    return (
        f"**Avg total: ${comps['average']:,.2f}** • "
        f"Median: ${comps['median']:,.2f}\n"
        f"Last {comps['count']}: {' • '.join(links)}\n"
        "*U.S. eBay completed sales; price + shipping when published*"
    )