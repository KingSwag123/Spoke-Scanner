"""
TCG domain engine: listing classification, title parsing, and live market-price
lookups for every game (Pokémon, Magic: The Gathering, Lorcana, One Piece).

This module owns:
  • the cheap classifiers (language / authenticity / lot / graded slab / sealed /
    seller trust),
  • the per-game title parsers,
  • the live price sources (pokemontcg.io, Scryfall, Lorcast, tcgcsv) plus the
    bulk indexes and the in-process caches / rate-limit state they depend on.

All tunable settings come from `config`; nothing here imports the discord or
main modules, so it is safe to import from anywhere downstream.
"""

import math
import re
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

import requests

from config import (
    _API_MIN_INTERVAL,
    _ART_CASE_RE,
    _BLOCKED_LANGS,
    _COND_FLAW_RE,
    _COND_NEG_RE,
    _COND_POINTER_RE,
    _COND_WEIGHT_RE,
    _CUSTOM_WORD_RE,
    _FAKE_INDICATORS,
    _FAST_API_HEADERS,
    _FAST_API_INTERVAL,
    _FRACTION_RE,
    _GAME_PREFIXES,
    _HTTP_HEADERS,
    _JP_INDEX_RETRY,
    _JP_INDEX_TTL,
    _JP_SCRIPT_RE,
    _JP_TOKEN_RE,
    _LOT_INDICATORS,
    _NAME_NOISE,
    _NAME_STOPWORDS,
    _NAME_SUFFIX,
    _NOT_PRESALE_RE,
    _ONEPIECE_INDEX_RETRY,
    _ONEPIECE_INDEX_TTL,
    _OP_CODE_RE,
    _OPENED_CONDITION_RE,
    _OVERSIZE_INDICATORS,
    _PRESALE_RE,
    _REPRINT_MARKER_RE,
    _REPRINT_OWN_TOTALS,
    _SEALED_ALIAS,
    _SEALED_AMBIG_PRICE_TOL,
    _SEALED_BULK_TOKENS,
    _SEALED_DROP,
    _SEALED_DROP_BY_GAME,
    _SEALED_ERA_SETS,
    _SEALED_FORM_TOKENS,
    _SEALED_INDEX_RETRY,
    _SEALED_INDEX_TTL,
    _SEALED_INDICATORS,
    _SEALED_LANG_RE,
    _SEALED_LANG_WORDS,
    _SEALED_LOT_EXEMPT_RE,
    _SEALED_LOT_RE,
    _SEALED_MIN_TOKENS,
    _SEALED_PACK_COUNT_RE,
    _SEALED_PARTIAL_TOKENS,
    _SINGLE_OVERRIDE,
    _SLAB_GRADE_RE,
    _SLAB_INDICATORS,
    _YUGIOH_CODE_RE,
    _YUGIOH_INDEX_RETRY,
    _YUGIOH_INDEX_TTL,
    MIN_SELLER_FEEDBACK_PCT,
    MIN_SELLER_FEEDBACK_SCORE,
    POKEMON_TCG_URL,
    SEALED_PACK_COUNT_MAX,
    SEALED_PACK_COUNT_MAX_DEFAULT,
    SEALED_REPEAT_FREE,
    SEALED_REPEAT_WINDOW,
    TCGCSV_CATEGORY,
    TCGCSV_ONEPIECE_CAT,
    TCGCSV_POKEMON_JP_CAT,
)

# pokemontcg.io anonymous access is rate-limited — space out live calls.
_last_api_ts      = 0.0

# Market-price cache PERSISTED ACROSS CYCLES with a TTL. Clearing it every cycle
# (the old behavior) forced a re-lookup of hundreds of the same cards every 5 min,
# hammering the rate-limited free price APIs → repeated 429s + 60s back-offs that
# stalled the deal pass and delayed pings by up to an hour. Prices barely move
# minute-to-minute, so we keep them and only re-price newly-seen cards.
#   key -> (value, monotonic_expiry)   value = (market_price, matched_name) | None
_price_cache: dict  = {}
_CACHE_MISS         = object()   # "not cached" — distinct from a cached None (genuine no-match)
_PRICE_CACHE_TTL    = 1800.0     # successful price: 30 min
_NEG_CACHE_TTL      = 600.0      # genuine no-match (None): 10 min — retry sooner in case it later prices


def _cache_get(key):
    """Return the cached value, or _CACHE_MISS if absent/expired (evicting it)."""
    entry = _price_cache.get(key)
    if entry is None:
        return _CACHE_MISS
    value, expiry = entry
    if time.monotonic() >= expiry:
        _price_cache.pop(key, None)
        return _CACHE_MISS
    return value


def _cache_put(key, value):
    """Cache a result with a TTL (shorter for a genuine no-match). Returns value."""
    ttl = _PRICE_CACHE_TTL if value is not None else _NEG_CACHE_TTL
    _price_cache[key] = (value, time.monotonic() + ttl)
    return value


def reset_cycle_cache() -> None:
    """Evict only EXPIRED price entries (called once per scan cycle). Prices now
    persist across cycles via their TTL instead of being wiped every cycle, so we
    stop re-pricing the same cards every 5 min and starving the rate-limited APIs."""
    now = time.monotonic()
    for k in [k for k, (_, expiry) in _price_cache.items() if now >= expiry]:
        _price_cache.pop(k, None)


# ---------------------------------------------------------------------------
# Language filtering
# ---------------------------------------------------------------------------

def detect_language(title: str) -> str:
    t = title.lower()
    if (any(k in t for k in ("japanese", "japan", "jpn"))
            or _JP_TOKEN_RE.search(title) or _JP_SCRIPT_RE.search(title)):
        return "Japanese"
    if any(k in t for k in ("english", " eng ")):
        return "English"
    return "Unknown"


def is_allowed_language(title: str) -> bool:
    t = title.lower()
    return not any(k in t for k in _BLOCKED_LANGS)


# ---------------------------------------------------------------------------
# Authenticity filter — block DIY / proxy / fan-art listings
# ---------------------------------------------------------------------------

def is_official_card(title: str, game: str | None = None) -> bool:
    """Return False if the title contains any known fake/DIY indicator, or
    describes a product that is not the standard card (oversized promo, art
    case). Pass the game so Magic's genuine oversized cards are not dropped."""
    t = title.lower()
    if any(ind in t for ind in _FAKE_INDICATORS) or _CUSTOM_WORD_RE.search(t):
        return False
    if game == "mtg":
        # Oversized-only Magic cards and the "(Extended Art)" treatment are real
        # and priced as themselves; fetch_mtg_price rejects an oversized copy of
        # a normal card.
        return True
    return not (any(ind in t for ind in _OVERSIZE_INDICATORS) or _ART_CASE_RE.search(t))


def is_anniversary_reprint(title: str, set_total: str) -> bool:
    """True for an English Pokémon anniversary reprint that keeps an older
    card's number and set total, which the price lookup would match to the
    original printing. A set total the anniversary set uses itself is fine."""
    if not _REPRINT_MARKER_RE.search(title):
        return False
    return int(set_total) not in _REPRINT_OWN_TOTALS


def is_opened_condition(condition: str) -> bool:
    """True if an eBay item condition says the product has been opened or used."""
    return bool(_OPENED_CONDITION_RE.search(condition or ""))


def sealed_language_mismatch(title: str, matched_name: str) -> bool:
    """A sealed listing that says it is non-English, matched to a catalog
    product whose own name does not. Callers must exempt the Japanese-marketplace
    lane, which is Japanese by design and priced on the Japanese catalog."""
    if _sealed_tokens(matched_name) & _SEALED_LANG_WORDS:
        return False
    return detect_language(title) == "Japanese" or bool(_SEALED_LANG_RE.search(_ascii(title)))


def is_sealed_lot(title: str, matched_name: str) -> bool:
    """True if the title states a quantity of whole units ("2x", "3 boxes") that
    would be priced as one unit of the matched product."""
    if _SEALED_LOT_EXEMPT_RE.search(matched_name) or _SEALED_LOT_RE.search(_ascii(matched_name)):
        return False
    return bool(_SEALED_LOT_RE.search(_ascii(title)))


def is_presale(title: str) -> bool:
    """True if the title says the item is a presale / pre-order."""
    t = _ascii(title)
    return bool(_PRESALE_RE.search(t)) and not _NOT_PRESALE_RE.search(t)


def sealed_condition_caveat(title: str, matched_name: str) -> str | None:
    """"flaw", "weight" or "pointer" if the title carries a condition note (torn
    seal, weighed pack, "read description"), else None."""
    t = _COND_NEG_RE.sub(" ", _ascii(title))
    own = _sealed_tokens(matched_name)
    for tier, rx in (("flaw", _COND_FLAW_RE), ("weight", _COND_WEIGHT_RE),
                     ("pointer", _COND_POINTER_RE)):
        for m in rx.finditer(t):
            words = set(re.findall(r"[a-z]+", m.group(0).lower()))
            if words and words <= own:
                continue                  # part of the product's own name
            return tier
    return None


def repeat_allowed(recent: dict, key: str, total: float, now: float) -> bool:
    """Repeat limit for one catalog product. Its first SEALED_REPEAT_FREE alerts
    inside SEALED_REPEAT_WINDOW always post; after that a listing posts only if
    it is cheaper than every alert already posted for the product in the window.
    `recent` maps key -> [[epoch_seconds, total], ...] of posted alerts; expired
    entries for `key` are dropped here."""
    hist = [p for p in recent.get(key, []) if now - p[0] < SEALED_REPEAT_WINDOW]
    if hist:
        recent[key] = hist
    else:
        recent.pop(key, None)
    return len(hist) < SEALED_REPEAT_FREE or total < min(p[1] for p in hist)


# ---------------------------------------------------------------------------
# Lot filter — single cards only
# ---------------------------------------------------------------------------

def is_single_card(title: str) -> bool:
    """Return False if the listing is a lot, bundle, or choice listing."""
    t = f" {title.lower()} "
    return not any(ind in t for ind in _LOT_INDICATORS)


# ---------------------------------------------------------------------------
# Graded slab detector — drives routing only (PSA/BGS/CGC/Slab → PREMIUM)
# ---------------------------------------------------------------------------

def is_graded_slab(title: str) -> bool:
    """Return True if the listing is a graded slab (drives PREMIUM routing only)."""
    t = f" {title.lower()} "
    if any(kw in t for kw in _SLAB_INDICATORS):
        return True
    return bool(_SLAB_GRADE_RE.search(title))


# ---------------------------------------------------------------------------
# Sealed-product detector — drives SEALED routing, bypasses single/lot filters
# ---------------------------------------------------------------------------

def is_sealed(title: str) -> bool:
    """Return True if the listing is a sealed product (box, ETB, blister, pack…)."""
    # A graded slab is a single card, never a sealed product — route it through
    # the singles path (→ #premium) even if the title mentions e.g. "sealed".
    if is_graded_slab(title):
        return False
    t = f" {title.lower()} "
    # A card-number fraction (NNN/NNN) marks a specific single card; sealed
    # products never carry one. This alone rejects most single-card noise.
    if _FRACTION_RE.search(title):
        return False
    # Explicit single-card / accessory signals override sealed keywords.
    if any(sig in t for sig in _SINGLE_OVERRIDE):
        return False
    if any(kw in t for kw in _SEALED_INDICATORS):
        return True
    # Standalone "display"/"sealed" only count with reinforcing context, to avoid
    # matching e.g. "display only" graphics or "sealed in toploader" singles.
    if " display " in t and ("box" in t or "case" in t or "pack" in t):
        return True
    return False


def is_yugioh_sealed(title: str) -> bool:
    """Yu-Gi-Oh-only sealed forms not shared safely with the other games."""
    if is_sealed(title):
        return True
    if is_graded_slab(title) or _YUGIOH_CODE_RE.search(title):
        return False
    t = f" {_ascii(title).casefold()} "
    return any(kind in t for kind in (
        " structure deck ", " starter deck ", " speed duel box ",
        " mega tin ", " collector tin ", " sealed tin ",
    ))


# ---------------------------------------------------------------------------
# Seller feedback filter — keep scam/low-trust sellers out of alerts
# ---------------------------------------------------------------------------

def is_trusted_seller(item: dict) -> bool:
    """Return False if seller feedback is below minimum thresholds."""
    seller = item.get("_seller", {})
    score  = seller.get("score", 0)
    pct    = seller.get("pct", 100.0)
    if score < MIN_SELLER_FEEDBACK_SCORE:
        return False
    if pct < MIN_SELLER_FEEDBACK_PCT:
        return False
    return True


# ---------------------------------------------------------------------------
# Real-time title parsing — extract a card-number fraction + a species keyword
# ---------------------------------------------------------------------------

def parse_card(title: str):
    """
    Parse a free-form eBay title into (species, number, set_total).

      species   — distinctive name keyword (e.g. "charizard") used to constrain
                  the API query; biased toward the token nearest the fraction.
      number    — the fraction numerator with leading zeros stripped ("074"→"74").
      set_total — the fraction denominator (printed set total, "073"→"73").

    Returns None when no number fraction is present or no usable species token can
    be isolated — those listings are skipped (bias toward precision).
    """
    # Fold accents to ASCII so "Pokémon" → "Pokemon" (recognized as noise) and
    # accented species names tokenize cleanly instead of splitting on the accent.
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")

    m = _FRACTION_RE.search(title)
    if not m:
        return None

    try:
        number    = str(int(m.group(1)))
        set_total = str(int(m.group(2)))
    except ValueError:
        return None

    before = title[: m.start()]
    tokens = re.findall(r"[A-Za-z]+", before)

    species = None
    for tok in reversed(tokens):
        low = tok.lower()
        if low in _NAME_SUFFIX or low in _NAME_NOISE:
            continue
        if len(low) < 3:          # skip tiny tokens like set codes "sv", "sm"
            continue
        species = low
        break

    if not species:
        return None
    return species, number, set_total


# ---------------------------------------------------------------------------
# On-the-fly TCG market lookup (pokemontcg.io) — cached + rate-limited
# ---------------------------------------------------------------------------

def _rate_limit() -> None:
    """Block as needed so live TCG calls stay spaced by at least _API_MIN_INTERVAL."""
    global _last_api_ts
    now  = time.monotonic()
    wait = _API_MIN_INTERVAL - (now - _last_api_ts)
    if wait > 0:
        time.sleep(wait)
    _last_api_ts = time.monotonic()


def _digits(value) -> str:
    """Strip non-digits and leading zeros so '074'/'74'/'TG74' compare equal."""
    d = re.sub(r"\D", "", str(value))
    return str(int(d)) if d else ""


def fetch_market_price(species: str, number: str, set_total: str):
    """
    Live lookup constrained to (species name + number fraction). Returns
    (market_price, matched_card_name) or None.

    Disambiguation / precision rules:
      • Query pokemontcg.io with `name:<species> number:<number>`.
      • Keep cards whose number matches the numerator.
      • Prefer cards whose set.printedTotal matches the denominator (the /XXX);
        this pins the exact set even when a number repeats across sets.
      • If more than one distinct card still matches, treat it as ambiguous and
        skip (return None) rather than risk a false-positive alert.
      • Market price = lowest market across the matched card's variants
        (conservative — avoids over-stating value and firing false deals).
    """
    key = (species, number, set_total)
    cached = _cache_get(key)
    if cached is not _CACHE_MISS:
        return cached

    _rate_limit()
    try:
        resp = requests.get(
            POKEMON_TCG_URL,
            params={
                "q":        f"name:{species} number:{number}",
                "select":   "id,name,number,tcgplayer,set",
                "pageSize": "50",
            },
            timeout=15,
        )
        resp.raise_for_status()
        cards = resp.json().get("data", [])
    except requests.RequestException as e:
        print(f"  [WARN] pokemontcg.io request failed for '{species} {number}': {e}")
        return None     # transient — do NOT cache, retry next cycle

    want_num = _digits(number)
    candidates = [c for c in cards if _digits(c.get("number", "")) == want_num]
    if not candidates:
        return _cache_put(key, None)

    # Prefer exact set-total (denominator) matches; fall back to all number matches.
    exact = [c for c in candidates if c.get("set", {}).get("printedTotal") == int(set_total)]
    pool  = exact if exact else candidates

    distinct_ids = {c.get("id") for c in pool}
    if len(distinct_ids) > 1:
        return _cache_put(key, None)     # ambiguous — skip for precision

    card    = pool[0]
    markets = [
        float(v["market"])
        for v in card.get("tcgplayer", {}).get("prices", {}).values()
        if v.get("market")
    ]
    if not markets:
        return _cache_put(key, None)

    result = (min(markets), card.get("name", species.title()))
    return _cache_put(key, result)


# ---------------------------------------------------------------------------
# Multi-game market lookup
#   MTG       → Scryfall   (fuzzy card-name search, no key)
#   Lorcana   → Lorcast    (card-name search, no key)
#   One Piece → tcgcsv     (bulk TCGplayer price index keyed by card code)
# Pokémon keeps its pokemontcg.io path above. All sources are free / keyless.
# ---------------------------------------------------------------------------
_last_fast_ts      = 0.0
_fast_cooldown_until = 0.0     # monotonic deadline: after a 429 we pause ALL fast calls
_FAST_COOLDOWN_MAX_WAIT = 65.0 # block long enough to wait out Scryfall's Retry-After: 60 window

# Sentinels returned by the fast fetchers instead of None when a failure is
# TRANSIENT. Distinct from None (a genuine no-match) so the caller skips WITHOUT
# caching it — caching a transient failure would wrongly mark a real card as
# unpriceable for the whole TTL window.
_RATELIMITED = object()   # 429 / active cooldown — also pauses the whole fast path
_TRANSIENT   = object()   # network error / 5xx / unparseable body — retry next cycle


def _fast_throttle() -> None:
    """Space out the high-volume Scryfall/Lorcast calls."""
    global _last_fast_ts
    wait = _FAST_API_INTERVAL - (time.monotonic() - _last_fast_ts)
    if wait > 0:
        time.sleep(wait)
    _last_fast_ts = time.monotonic()


def _fast_gate() -> bool:
    """Gate a fast-path call against the post-429 cooldown window.

    If a cooldown is active, block until it clears — bounded by
    _FAST_COOLDOWN_MAX_WAIT — so an occasional 429 only briefly pauses the scan
    instead of dropping every remaining MTG/Lorcana card in the cycle (each game
    phase runs faster than the old fixed 60s window, so a skip lost the whole
    phase). Returns False only when the remaining window exceeds what we will
    wait, in which case the caller skips and retries next cycle.
    """
    remaining = _fast_cooldown_until - time.monotonic()
    if remaining <= 0:
        return True
    if remaining > _FAST_COOLDOWN_MAX_WAIT:
        return False
    time.sleep(remaining)
    return True


def _trip_fast_cooldown(resp) -> None:
    """Back off all Scryfall/Lorcast calls after a 429, honoring Retry-After.

    Scryfall explicitly warns that ignoring 429s leads to a network block, so we
    stop hammering and pause the whole fast path until the window clears.
    """
    global _fast_cooldown_until
    retry = 10.0
    try:
        ra = resp.headers.get("Retry-After")
        if ra:
            retry = min(max(float(ra), 1.0), 300.0)
    except (TypeError, ValueError, AttributeError):
        pass
    _fast_cooldown_until = time.monotonic() + retry
    code = getattr(resp, "status_code", "?")
    print(f"  [WARN] fast API throttled ({code}) — backing off MTG/Lorcana lookups {retry:.0f}s")


def _ascii(text: str) -> str:
    """Fold accents to ASCII and collapse whitespace (eBay titles are messy)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


def _name_tokens(title: str, game: str) -> list[str]:
    """Isolate the leading card-name tokens from a noisy eBay title (max 6)."""
    t = _ascii(title).lower().replace(":", " ").replace("|", " ")
    t = re.sub(r"\s+", " ", t).strip(" -,")
    for pre in _GAME_PREFIXES.get(game, ()):
        if t.startswith(pre):
            t = t[len(pre):].strip(" -,")
            break
    out: list[str] = []
    for tok in t.split(" "):
        clean = tok.strip(",.-'")
        if not clean:
            continue
        if re.search(r"\d", clean) or "#" in tok or "/" in tok:
            break
        if clean in _NAME_STOPWORDS:
            break
        out.append(clean)
    return out[:6]


def _progressive_price(tokens: list[str], fetch_one, cache_prefix: str):
    """
    Try the longest leading name first, dropping one trailing token at a time
    until a source matches — set-name/cruft trails the real card name in eBay
    titles. Returns (market_price, matched_name) or None; caches each attempt.
    """
    for k in range(len(tokens), 0, -1):
        name = " ".join(tokens[:k])
        key  = (cache_prefix, name)
        res  = _cache_get(key)
        if res is _CACHE_MISS:
            res = fetch_one(name)
            # A transient failure (rate-limit / network / 5xx / bad JSON) must NOT
            # be cached (it would poison a real card for the TTL) and means every
            # further prefix is also unreachable — abort and retry next cycle.
            if res is _RATELIMITED or res is _TRANSIENT:
                return None
            _cache_put(key, res)
        if res:
            return res
    return None


def _scryfall_one(name: str, foil: bool, oversize: bool = False):
    """One Scryfall fuzzy-name lookup. With oversize=True (the listing is sold as
    an oversized card) the match only counts when Scryfall's card is itself
    oversized — a plane, scheme, Vanguard or MicroProse card. An oversized copy
    of a normal card is a different, cheaper product: a genuine no-match."""
    if not _fast_gate():
        return _RATELIMITED
    _fast_throttle()
    try:
        r = requests.get(
            "https://api.scryfall.com/cards/named",
            params={"fuzzy": name}, headers=_FAST_API_HEADERS, timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [WARN] Scryfall request failed for '{name}': {e}")
        return _TRANSIENT               # network blip — retry next cycle, don't cache
    if r.status_code in (429, 403):
        # 429 = rate-limited; 403 = anti-abuse block. Both are transient and must
        # NOT be cached as a real no-match — back off and retry next cycle so one
        # blocked call doesn't poison every later MTG card in the cycle.
        _trip_fast_cooldown(r)
        return _RATELIMITED
    if r.status_code >= 500:            # server error — transient, don't cache
        return _TRANSIENT
    if r.status_code != 200:            # 404 = no/ambiguous fuzzy match (genuine)
        return None
    try:
        d = r.json()
    except ValueError:
        return _TRANSIENT               # unparseable body — transient, don't cache
    if oversize and not d.get("oversized"):
        return None
    prices = d.get("prices", {}) or {}
    raw = (prices.get("usd_foil") if foil else None) or prices.get("usd") or prices.get("usd_foil")
    try:
        return (float(raw), d.get("name", name.title())) if raw else None
    except (TypeError, ValueError):
        return None


def fetch_mtg_price(tokens: list[str], title: str):
    t = title.lower()
    foil = "foil" in t
    # Magic sellers say "oversized"; "jumbo" alone is not used here because
    # "Jumbo Cactuar" is a card name.
    oversize = "oversize" in t
    return _progressive_price(
        tokens, lambda n: _scryfall_one(n, foil, oversize),
        f"mtg{'F' if foil else ''}{'O' if oversize else ''}",
    )


def _lorcast_one(name: str, foil: bool):
    if not _fast_gate():
        return _RATELIMITED
    _fast_throttle()
    try:
        r = requests.get(
            "https://api.lorcast.com/v0/cards/search",
            params={"q": name}, headers=_FAST_API_HEADERS, timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [WARN] Lorcast request failed for '{name}': {e}")
        return _TRANSIENT               # network blip — retry next cycle, don't cache
    if r.status_code in (429, 403):
        # 429 = rate-limited; 403 = anti-abuse block. Both transient — back off and
        # retry next cycle instead of caching a false no-match.
        _trip_fast_cooldown(r)
        return _RATELIMITED
    if r.status_code >= 500:            # server error — transient, don't cache
        return _TRANSIENT
    if r.status_code != 200:
        return None
    try:
        results = r.json().get("results", []) or []
    except ValueError:
        return _TRANSIENT               # unparseable body — transient, don't cache
    if not results:
        return None
    c = results[0]
    prices = c.get("prices", {}) or {}
    raw = (prices.get("usd_foil") if foil else None) or prices.get("usd") or prices.get("usd_foil")
    try:
        val = float(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not val:
        return None
    nm = c.get("name", name.title())
    if c.get("version"):
        nm = f"{nm} - {c['version']}"
    return val, nm


def fetch_lorcana_price(tokens: list[str], title: str):
    foil = "foil" in title.lower()
    return _progressive_price(tokens, lambda n: _lorcast_one(n, foil), f"lorcana{'F' if foil else ''}")


# One Piece price index (tcgcsv category 68). See config for the category id and
# matching regex. We build {card_code: (name, market_price)} once per TTL window.
_op_index: dict       = {}
_op_index_until: float = 0.0       # monotonic deadline; rebuild only once it passes


def _build_onepiece_index() -> dict:
    """Build {card_code: (name, market_price)} from tcgcsv One Piece data."""
    base  = f"https://tcgcsv.com/tcgplayer/{TCGCSV_ONEPIECE_CAT}"
    index: dict = {}
    try:
        groups = requests.get(f"{base}/groups", headers=_HTTP_HEADERS, timeout=15).json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        print(f"  [WARN] tcgcsv groups fetch failed: {e}")
        return index
    results, _complete = _fetch_all_groups(base, groups, "One Piece index")
    for _g, prods, prices in results:
        pmap = {
            p.get("productId"): p["marketPrice"]
            for p in prices
            if p.get("subTypeName") == "Normal" and p.get("marketPrice")
        }
        for prod in prods:
            num = next((e["value"] for e in prod.get("extendedData", []) if e.get("name") == "Number"), None)
            mp  = pmap.get(prod.get("productId"))
            if num and mp:
                try:
                    index[num.upper()] = (prod.get("name", num), float(mp))
                except (TypeError, ValueError):
                    pass
    return index


def _onepiece_index() -> dict:
    """
    Return the cached One Piece price index, (re)building only once the deadline
    passes. A single `_op_index_until` gate covers both the success TTL and a
    short failure backoff, so a failed build can't trigger a full rebuild on
    every One Piece listing in the same cycle (which would burst tcgcsv traffic
    and overrun the scan interval).
    """
    global _op_index, _op_index_until
    now = time.monotonic()
    if now < _op_index_until:
        return _op_index            # fresh index, or still inside failure backoff
    print("[INFO] Building One Piece price index from tcgcsv …")
    idx = _build_onepiece_index()
    if idx:
        _op_index, _op_index_until = idx, now + _ONEPIECE_INDEX_TTL
        print(f"[INFO] One Piece index ready — {len(idx)} cards priced")
    else:
        _op_index_until = now + _ONEPIECE_INDEX_RETRY
        print(f"[WARN] One Piece index build returned no data; backing off "
              f"{_ONEPIECE_INDEX_RETRY // 60} min before retry")
    return _op_index


def fetch_onepiece_price(code: str, title: str):
    idx = _onepiece_index()
    entry = idx.get(code) if idx else None
    if not entry:
        return None
    # The index stores (name, market_price); callers expect (market_price, name).
    name, market_price = entry
    return market_price, name


# Yu-Gi-Oh singles are identified by their printed set code (LOB-001,
# RA01-EN001, etc.), never by a generic card-name search. The code prefix is
# matched to TCGplayer's live group abbreviation, so only that one group needs
# two tcgcsv requests instead of downloading all ~650 groups.
_ygo_groups: dict[str, list[dict]] = {}
_ygo_groups_until = 0.0
_ygo_group_indexes: dict[int, dict[str, list]] = {}
_ygo_group_until: dict[int, float] = {}


def _yugioh_groups() -> dict[str, list[dict]]:
    global _ygo_groups, _ygo_groups_until
    now = time.monotonic()
    if now < _ygo_groups_until:
        return _ygo_groups
    base = f"https://tcgcsv.com/tcgplayer/{TCGCSV_CATEGORY['yugioh']}"
    try:
        categories = requests.get(
            "https://tcgcsv.com/tcgplayer/categories",
            headers=_HTTP_HEADERS, timeout=15,
        ).json().get("results", [])
        category = next(
            (c for c in categories if c.get("categoryId") == TCGCSV_CATEGORY["yugioh"]),
            None,
        )
        if not category or str(category.get("name", "")).casefold() != "yugioh":
            print("  [WARN] tcgcsv category 2 did not verify as YuGiOh; pricing disabled")
            _ygo_groups = {}
            _ygo_groups_until = now + _YUGIOH_INDEX_RETRY
            return {}
        groups = requests.get(
            f"{base}/groups", headers=_HTTP_HEADERS, timeout=15,
        ).json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  [WARN] tcgcsv Yu-Gi-Oh catalog verification failed: {type(exc).__name__}")
        _ygo_groups = {}
        _ygo_groups_until = now + _YUGIOH_INDEX_RETRY
        return {}
    verified: dict[str, list[dict]] = {}
    for group in groups:
        if group.get("groupId") is None or not group.get("abbreviation"):
            continue
        verified.setdefault(
            str(group["abbreviation"]).upper(), []
        ).append(group)
    if verified:
        _ygo_groups = verified
        _ygo_groups_until = now + _YUGIOH_INDEX_TTL
    else:
        _ygo_groups = {}
        _ygo_groups_until = now + _YUGIOH_INDEX_RETRY
    return _ygo_groups


def _build_yugioh_group(group: dict) -> dict[str, list]:
    base = f"https://tcgcsv.com/tcgplayer/{TCGCSV_CATEGORY['yugioh']}"
    _group, products, prices = _fetch_group(base, group)
    by_product: dict = {}
    for row in prices:
        try:
            market = float(row.get("marketPrice") or 0)
        except (TypeError, ValueError):
            continue
        edition = str(row.get("subTypeName") or "").casefold()
        if market > 0 and edition in {"1st edition", "unlimited"}:
            by_product.setdefault(row.get("productId"), []).append((edition, market))
    index: dict[str, list] = {}
    for product in products:
        code = next(
            (str(e.get("value", "")).upper() for e in product.get("extendedData", [])
             if e.get("name") == "Number"),
            "",
        )
        if not _YUGIOH_CODE_RE.fullmatch(code):
            continue
        name = str(product.get("name") or "")
        rarity = next(
            (str(e.get("value") or "") for e in product.get("extendedData", [])
             if e.get("name") == "Rarity"),
            "",
        )
        for edition, market in by_product.get(product.get("productId"), []):
            index.setdefault(code, []).append((name, rarity, edition, market))
    return index


def _yugioh_group_index(group: dict) -> dict[str, list]:
    gid = int(group["groupId"])
    now = time.monotonic()
    if now < _ygo_group_until.get(gid, 0):
        return _ygo_group_indexes.get(gid, {})
    index = _build_yugioh_group(group)
    if index:
        _ygo_group_indexes[gid] = index
        _ygo_group_until[gid] = now + _YUGIOH_INDEX_TTL
    else:
        _ygo_group_until[gid] = now + _YUGIOH_INDEX_RETRY
    return _ygo_group_indexes.get(gid, {})


def _normalized_phrase(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _ascii(value).casefold()))


_YGO_RARITY_PATTERNS = (
    ("quarter century secret rare",
     r"\b(?:quarter century(?: secret rare)?|qcsr|qcr)\b"),
    ("platinum secret rare", r"\b(?:platinum secret rare|psr)\b"),
    ("prismatic ultimate rare",
     r"\b(?:prismatic ultimate rare|pur)\b"),
    ("prismatic collector s rare",
     r"\b(?:prismatic collector'?s rare|pcr)\b"),
    ("starlight rare", r"\b(?:starlight rare|starlight)\b"),
    ("ghost rare", r"\bghost rare\b"),
    ("ultimate rare", r"\b(?:ultimate rare|utr)\b"),
    ("collector s rare", r"\bcollector'?s rare\b"),
    ("secret rare", r"\b(?:secret rare|scr)\b"),
    ("ultra rare", r"\b(?:ultra rare|ur)\b"),
    ("super rare", r"\b(?:super rare|sr)\b"),
    ("rare", r"\brare\b"),
    ("common", r"\bcommon\b"),
)
_YGO_RARITY_ALIASES = {
    "quarter century rare": "quarter century secret rare",
    "qcsr": "quarter century secret rare",
    "qcr": "quarter century secret rare",
    "psr": "platinum secret rare",
    "pur": "prismatic ultimate rare",
    "pcr": "prismatic collector s rare",
    "starlight": "starlight rare",
    "utr": "ultimate rare",
    "scr": "secret rare",
    "ur": "ultra rare",
    "sr": "super rare",
}


def _yugioh_rarity(value: str) -> str:
    normalized = _normalized_phrase(value)
    return _YGO_RARITY_ALIASES.get(normalized, normalized)


def _yugioh_title_rarities(title: str) -> set[str]:
    """Canonical explicit rarity signals, with specific rarities taking precedence."""
    found: set[str] = set()
    occupied: list[tuple[int, int]] = []
    for canonical, pattern in _YGO_RARITY_PATTERNS:
        for match in re.finditer(pattern, title, re.I):
            if any(match.start() >= start and match.end() <= end
                   for start, end in occupied):
                continue
            found.add(canonical)
            occupied.append(match.span())
    return found


def _yugioh_base_name(name: str, rarity: str) -> str:
    """Remove TCGplayer's trailing rarity annotation, not card-name parentheses."""
    match = re.search(r"\s+\(([^()]*)\)\s*$", name)
    if match and _yugioh_rarity(match.group(1)) == _yugioh_rarity(rarity):
        return name[:match.start()].strip()
    return name


def _select_yugioh_group(groups: list[dict], title: str) -> dict | None:
    """Resolve duplicate set abbreviations only from explicit print-run qualifiers."""
    if len(groups) == 1:
        return groups[0]
    normalized = f" {_normalized_phrase(title)} "
    if " reprint " in normalized:
        pool = [g for g in groups if "reprint" in str(g.get("name", "")).casefold()]
        return pool[0] if len(pool) == 1 else None
    if " original " in normalized:
        pool = [g for g in groups if "reprint" not in str(g.get("name", "")).casefold()]
        return pool[0] if len(pool) == 1 else None
    title_years = set(re.findall(r"\b(?:19|20)\d{2}\b", title))
    if title_years:
        pool = []
        for group in groups:
            group_years = set(re.findall(
                r"\b(?:19|20)\d{2}\b",
                f"{group.get('name', '')} {group.get('publishedOn', '')}",
            ))
            if title_years & group_years:
                pool.append(group)
        return pool[0] if len(pool) == 1 else None
    return None


def fetch_yugioh_price(code: str, title: str):
    """Return an exact Yu-Gi-Oh print price or None.

    Set code, card name, and edition must all agree. If a catalog row has both
    Unlimited and 1st Edition prices and the listing omits edition, fail closed.
    """
    prefix = code.split("-", 1)[0].upper()
    groups = _yugioh_groups().get(prefix, [])
    group = _select_yugioh_group(groups, title)
    if not group:
        return None
    candidates = _yugioh_group_index(group).get(code.upper(), [])
    if not candidates:
        return None
    normalized_title = f" {_normalized_phrase(title)} "
    candidates = [
        c for c in candidates
        if f" {_normalized_phrase(_yugioh_base_name(c[0], c[1]))} "
        in normalized_title
    ]
    if not candidates:
        return None
    first = bool(re.search(r"\b(?:1st|first)\s+edition\b", title, re.I))
    unlimited = bool(re.search(r"\bunlimited\b", title, re.I))
    if first == unlimited:  # neither or contradictory
        editions = {c[2] for c in candidates}
        if len(editions) != 1:
            return None
    else:
        wanted = "1st edition" if first else "unlimited"
        candidates = [c for c in candidates if c[2] == wanted]
    explicit_rarities = _yugioh_title_rarities(title)
    if len(explicit_rarities) > 1:
        return None
    if explicit_rarities:
        wanted_rarity = next(iter(explicit_rarities))
        candidates = [
            c for c in candidates
            if _yugioh_rarity(c[1]) == wanted_rarity
        ]
    elif len({_yugioh_rarity(c[1]) for c in candidates}) != 1:
        return None
    identities = {(c[0], c[1], c[2]) for c in candidates}
    if len(identities) != 1:
        return None
    name, rarity, edition = next(iter(identities))
    market = min(c[3] for c in candidates)
    return market, f"{name} {code.upper()} ({rarity}, {edition.title()})"


# ---------------------------------------------------------------------------
# Japanese Pokémon singles pricing via tcgcsv (category 85 = "Pokemon Japan").
#   English Pokémon are priced by pokemontcg.io above (English TCGplayer market).
#   Japanese cards trade in a DIFFERENT market — often wildly different — so they
#   MUST be priced against the Japanese category or their value is meaningless.
#   We build a bulk index keyed by the card-number numerator →
#   [(name_lower, denominator, market_price, name)] and match a parsed
#   (species, number, set_total) with the SAME precision rules as the English
#   path: species token must appear in the name, prefer an exact printed-total
#   (denominator) match, and skip when still ambiguous.
# ---------------------------------------------------------------------------
_jp_index: dict[str, list]  = {}
_jp_index_until: float       = 0.0

# tcgcsv cat 85 holds ~450 set "groups", each needing 2 requests. Done serially
# that is ~2.5 min of blocking — and because this build runs INSIDE the scan loop
# (lazily, on the first Japanese Pokémon card) while MTG/Lorcana/One Piece are
# evaluated AFTER Pokémon, a serial build starves every later game in the cycle.
# So fetch the groups concurrently and cap the whole build with a wall-clock
# budget: it must never block the cycle long enough to suffocate MTG pricing.
_JP_INDEX_WORKERS = 8       # parallel group fetches (tcgcsv is a static price dump)
_JP_INDEX_BUDGET  = 75.0    # hard wall-clock cap (s); partial index is used if exceeded


def _fetch_group(base: str, g: dict) -> tuple[dict, list, list]:
    """Fetch one set group's (group, products, prices); ([], []) on any failure.

    Returns the group dict back so callers that need the group name (sealed IDF)
    can use it without a second lookup.
    """
    gid = g.get("groupId")
    try:
        prods  = requests.get(f"{base}/{gid}/products", headers=_HTTP_HEADERS, timeout=15).json().get("results", [])
        prices = requests.get(f"{base}/{gid}/prices",   headers=_HTTP_HEADERS, timeout=15).json().get("results", [])
        return g, prods, prices
    except (requests.RequestException, ValueError):
        return g, [], []


def _fetch_all_groups(base: str, groups: list, label: str) -> tuple[list, bool]:
    """Fetch every group's (group, prods, prices) concurrently under one wall-clock
    budget. Returns (results, complete); `complete` is False if the budget cut it
    short. Shared by every tcgcsv bulk index so none can serially starve the scan.
    """
    gids = [g for g in groups if g.get("groupId") is not None]
    if not gids:
        return [], False
    results: list = []
    pool = ThreadPoolExecutor(max_workers=_JP_INDEX_WORKERS)
    futures = [pool.submit(_fetch_group, base, g) for g in gids]
    try:
        for fut in as_completed(futures, timeout=_JP_INDEX_BUDGET):
            results.append(fut.result())
    except FuturesTimeout:
        print(f"  [WARN] {label} hit {_JP_INDEX_BUDGET:.0f}s budget — using partial "
              f"({len(results)}/{len(gids)} groups)")
    finally:
        # Never wait on stragglers (that re-introduces the block); cancel pending
        # fetches and let any in-flight ones die with their threads.
        pool.shutdown(wait=False, cancel_futures=True)
    return results, len(results) >= len(gids)


def _index_jp_group(index: dict, prods: list, prices: list) -> None:
    """Fold one group's products/prices into the shared numerator->candidates index."""
    # Japanese singles are often priced ONLY under "Holofoil"/"Reverse Holofoil"
    # (no "Normal" row), so aggregate across ALL subtypes and keep the lowest
    # market per product — conservative, mirroring the English path's
    # min-across-variants (avoids over-stating value / false deals).
    pmap: dict = {}
    for p in prices:
        pid, mp = p.get("productId"), p.get("marketPrice")
        if pid is None or not mp:
            continue
        if pid not in pmap or mp < pmap[pid]:
            pmap[pid] = mp
    for prod in prods:
        numfield = next((e["value"] for e in prod.get("extendedData", []) if e.get("name") == "Number"), None)
        mp = pmap.get(prod.get("productId"))
        if not numfield or "/" not in numfield or not mp:
            continue
        a, b = numfield.split("/")[:2]
        numerator, denom = _digits(a), _digits(b)
        if not numerator:
            continue
        name = prod.get("name", "")
        try:
            index.setdefault(numerator, []).append((name.lower(), denom, float(mp), name))
        except (TypeError, ValueError):
            pass


def _build_japanese_index() -> tuple[dict, bool]:
    """Build {numerator: [(name_lower, denominator, market_price, name)]} from tcgcsv cat 85.

    Group fetches run concurrently under a hard wall-clock budget so this can
    never block the scan long enough to starve the other games. Returns
    (index, complete); `complete` is False when the budget cut the build short.
    """
    base = f"https://tcgcsv.com/tcgplayer/{TCGCSV_POKEMON_JP_CAT}"
    index: dict[str, list] = {}
    try:
        groups = requests.get(f"{base}/groups", headers=_HTTP_HEADERS, timeout=15).json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        print(f"  [WARN] tcgcsv JP groups fetch failed: {e}")
        return index, False
    results, complete = _fetch_all_groups(base, groups, "JP index")
    for _g, prods, prices in results:
        _index_jp_group(index, prods, prices)
    return index, complete


def _japanese_index() -> dict:
    """Cached Japanese Pokémon price index; (re)build only once the deadline passes."""
    global _jp_index, _jp_index_until
    now = time.monotonic()
    if now < _jp_index_until:
        return _jp_index
    print("[INFO] Building Japanese Pokémon price index from tcgcsv (cat 85) …")
    idx, complete = _build_japanese_index()
    if idx and complete:
        _jp_index, _jp_index_until = idx, now + _JP_INDEX_TTL
        print(f"[INFO] Japanese Pokémon index ready — {sum(len(v) for v in idx.values())} cards priced")
    elif idx:
        # Partial build (hit the time budget): use it now, but refresh soon so the
        # missing sets fill in — better than blocking the whole cycle to finish.
        _jp_index, _jp_index_until = idx, now + _JP_INDEX_RETRY
        print(f"[INFO] Japanese Pokémon index partial — {sum(len(v) for v in idx.values())} cards priced; "
              f"refresh in {_JP_INDEX_RETRY // 60} min")
    else:
        _jp_index_until = now + _JP_INDEX_RETRY
        print(f"[WARN] Japanese index build returned no data; backing off {_JP_INDEX_RETRY // 60} min")
    return _jp_index


def fetch_japanese_price(species: str, number: str, set_total: str):
    """Japanese-market lookup mirroring fetch_market_price's precision rules.

    Returns (market_price, name) or None. There is deliberately NO English
    fallback: pricing a Japanese card at its English value is exactly the
    mix-up this path exists to prevent.
    """
    idx = _japanese_index()
    if not idx:
        return None
    cands = idx.get(_digits(number))
    if not cands:
        return None
    sp = species.lower()
    matches = [c for c in cands if sp in c[0]]
    if not matches:
        return None
    exact = [c for c in matches if c[1] == _digits(set_total)]
    pool  = exact if exact else matches
    # If the surviving pool spans more than one distinct card, it's ambiguous → skip.
    if len({c[3] for c in pool}) > 1:
        return None
    name  = pool[0][3]
    price = min(c[2] for c in pool)   # conservative, mirrors the English path
    return price, name


# ---------------------------------------------------------------------------
# Sealed-product market pricing (all games) via tcgcsv
#   Sealed products are the tcgcsv entries that carry NO card "Number" in their
#   extendedData (booster boxes, ETBs, bundles, blisters, decks, cases, …).
#   We build a per-game index of [(distinctive_tokens, market_price, name)] and
#   match a noisy eBay title by requiring the product's distinctive tokens to be
#   a subset of the title tokens, preferring the most specific (longest) match.
#   The same total <= market × DEAL_RATIO test as singles is then applied, so a
#   sealed listing only alerts when it is a genuine deal.
# ---------------------------------------------------------------------------
_sealed_index: dict[str, list]  = {}    # game -> [(frozenset(tokens), price, name)]
_sealed_until: dict[str, float] = {}    # game -> monotonic deadline (TTL / backoff)
_sealed_idf:   dict[str, dict]  = {}    # game -> {token: distinctiveness weight}
_sealed_sets:  dict[str, set]   = {}    # game -> {frozenset(expansion-name tokens)}


def _sealed_tokens(name: str, game: str = "") -> frozenset[str]:
    """Lowercase alnum tokens of a name, minus brand/filler words (and the
    words that are filler only in `game`'s own catalog)."""
    toks = re.findall(r"[a-z0-9]+", _ascii(name).lower())
    extra = _SEALED_DROP_BY_GAME.get(game, frozenset())
    return frozenset(t for t in toks if t not in _SEALED_DROP and t not in extra)


def _sealed_set_name(toks: frozenset, game: str) -> frozenset:
    """A product's tokens minus form words and its era name: what is left names
    the expansion ("surging sparks"). Empty for an era-level product."""
    name = toks - _SEALED_FORM_TOKENS
    for era in _SEALED_ERA_SETS.get(game, ()):
        if era <= toks:
            name = name - era
    return frozenset(name)


def _sealed_is_era_level(toks: frozenset, game: str) -> bool:
    """True for a product named only by an era and form words."""
    return (any(era <= toks for era in _SEALED_ERA_SETS.get(game, ()))
            and not _sealed_set_name(toks, game))


def _sealed_set_names(idx: list, game: str) -> set:
    """Expansion names in a game's index, used to tell when a title names a
    specific set. Only booster and Elite Trainer Box products count, so product
    names like "Charizard ex Box" cannot make "Charizard ex" a set name."""
    eras = _SEALED_ERA_SETS.get(game, ())
    if not eras:
        return set()
    era_words = frozenset().union(*eras)
    out: set = set()
    for toks, _price, _name in idx:
        if not ("booster" in toks or {"elite", "trainer"} <= toks):
            continue
        if any(era <= toks for era in eras):
            continue
        name = _sealed_set_name(toks, game)
        if name and not name <= era_words and any(len(t) >= 3 for t in name):
            out.add(name)
    return out


def _build_sealed_index(game: str):
    """Build the sealed index + a token-distinctiveness (IDF) map for one game.

    Returns (out, idf) where out = [(tokens, market_price, name)] of sealed
    products and idf = {token: weight}. The weight is an inverse-group-frequency:
    tokens that appear across MANY tcgcsv groups (set-era words like "scarlet"/
    "violet" and product-type words like "booster"/"box") get a LOW weight, while
    tokens unique to one set ("stellar"/"crown"/"151") get a HIGH weight. Matching
    scores by summed weight so a specific set always beats the generic era/base
    box even though both are valid token subsets of a noisy eBay title."""
    cat = TCGCSV_CATEGORY.get(game)
    if cat is None:
        return [], {}
    base = f"https://tcgcsv.com/tcgplayer/{cat}"
    out: list = []
    group_df: Counter = Counter()   # token -> number of groups it appears in
    n_groups = 0
    try:
        groups = requests.get(f"{base}/groups", headers=_HTTP_HEADERS, timeout=15).json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        print(f"  [WARN] tcgcsv sealed groups fetch failed ({game}): {e}")
        return out, {}
    results, _complete = _fetch_all_groups(base, groups, f"{game} sealed index")
    for g, prods, prices in results:
        n_groups += 1
        # Group "vocabulary" = group-name tokens + ALL product-name tokens in it.
        # Used only to measure how common a token is across groups (era/type words
        # span many groups; set names live in exactly one).
        gtokens = set(re.findall(r"[a-z0-9]+", _ascii(g.get("name", "")).lower()))
        pmap = {
            p.get("productId"): p["marketPrice"]
            for p in prices
            if p.get("subTypeName") == "Normal" and p.get("marketPrice")
        }
        for prod in prods:
            gtokens.update(re.findall(r"[a-z0-9]+", _ascii(prod.get("name", "")).lower()))
            # Sealed = no card "Number" in extendedData (singles carry one).
            if any(e.get("name") == "Number" for e in prod.get("extendedData", [])):
                continue
            mp = pmap.get(prod.get("productId"))
            if not mp:
                continue
            name = prod.get("name", "")
            toks = _sealed_tokens(name, game)
            if len(toks) < _SEALED_MIN_TOKENS:
                continue
            # Skip bulk container SKUs (Case/Display) — their 10×+ price makes a
            # normal single look like an unreal deal when a title contains "case".
            if toks & _SEALED_BULK_TOKENS:
                continue
            try:
                out.append((toks, float(mp), name))
            except (TypeError, ValueError):
                pass
        for t in gtokens:
            group_df[t] += 1
    # IDF weight per token, restricted to the sealed vocabulary we actually score.
    n = max(n_groups, 1)
    vocab: set = set().union(*(t for t, _p, _n in out)) if out else set()
    idf = {t: math.log(1.0 + n / group_df[t]) for t in vocab if group_df.get(t)}
    return out, idf


def _sealed_idx(game: str) -> list:
    """Return the cached sealed index for a game, rebuilding past its deadline.

    A single per-game deadline gate covers both the success TTL and a short
    failure backoff, so a failed build can't re-trigger a full rebuild on every
    sealed listing in the same cycle (mirrors the One Piece singles gate)."""
    now = time.monotonic()
    if now < _sealed_until.get(game, 0.0):
        return _sealed_index.get(game, [])
    print(f"[INFO] Building {game} sealed price index from tcgcsv …")
    idx, idf = _build_sealed_index(game)
    if idx:
        _sealed_index[game] = idx
        _sealed_idf[game]   = idf
        _sealed_sets[game]  = _sealed_set_names(idx, game)
        _sealed_until[game] = now + _SEALED_INDEX_TTL
        print(f"[INFO] {game} sealed index ready — {len(idx)} products priced")
    else:
        _sealed_until[game] = now + _SEALED_INDEX_RETRY
        print(f"[WARN] {game} sealed index build returned no data; backing off "
              f"{_SEALED_INDEX_RETRY // 60} min before retry")
    return _sealed_index.get(game, [])


def _sealed_title_is_partial(title: str, title_toks: set, product_toks: frozenset,
                             game: str = "") -> bool:
    """True if the title describes something other than one unit of the matched
    product: a partial-product, other-form or lot word the product itself lacks,
    or a small pack count on a booster box. Only booster boxes: an Elite Trainer
    Box really does hold "9 packs"."""
    words = title_toks | ({"elite"} if "etb" in title_toks else set())
    other = (words & _SEALED_PARTIAL_TOKENS) - product_toks
    # "bundle" is also a verb in seller chatter ("DM to bundle!"); it only means
    # another product when the match is a plain booster box.
    if "bundle" in other and not {"booster", "box"} <= product_toks:
        other = other - {"bundle"}
    if other:
        return True
    # Collector Booster Boxes genuinely hold as few as 4 packs, and a Half
    # Booster Box 18.
    if not {"booster", "box"} <= product_toks or {"pack", "collector", "half"} & product_toks:
        return False
    limit = SEALED_PACK_COUNT_MAX.get(game, SEALED_PACK_COUNT_MAX_DEFAULT)
    return any(int(m.group(1)) <= limit for m in _SEALED_PACK_COUNT_RE.finditer(title))


def fetch_sealed_price(game: str, title: str):
    """Market price for a sealed listing, or None if no confident match.

    Among products whose tokens ⊆ the title's tokens, pick the one with the
    highest summed token distinctiveness (IDF) — so a specific set ("Stellar
    Crown Booster Box") beats the generic era/base box ("Scarlet & Violet Booster
    Box") even when the seller put the era in the title. If the top score is tied
    by two products with materially different prices, the match is ambiguous and
    we return None rather than ping a wrong price. Returns (market_price, name)."""
    idx = _sealed_idx(game)
    if not idx:
        return None
    idf = _sealed_idf.get(game, {})
    title_toks = set(re.findall(r"[a-z0-9]+", _ascii(title).lower()))
    title_toks |= {_SEALED_ALIAS[t] for t in title_toks if t in _SEALED_ALIAS}
    # A title that names a specific expansion must not fall back to the era-level
    # product ("Scarlet & Violet Booster Box") when that expansion's own product
    # is missing or loses on tokens.
    names_a_set = any(s <= title_toks for s in _sealed_sets.get(game, ()))
    scored = [
        (sum(idf.get(t, 0.0) for t in toks), len(toks), price, name, toks)
        for toks, price, name in idx
        if toks <= title_toks
        and not (names_a_set and _sealed_is_era_level(toks, game))
    ]
    if not scored:
        return None
    # Deterministic order: highest IDF score, then most tokens (more specific),
    # then name — so equal-score ties resolve the same way every run instead of
    # depending on index insertion order.
    scored.sort(key=lambda x: (-x[0], -x[1], x[3]))
    best_score, _best_ntoks, best_price, best_name, best_toks = scored[0]
    if _sealed_title_is_partial(title, title_toks, best_toks, game):
        return None
    # Ambiguity guard: a different product tied at the top score with a materially
    # different price means we can't tell them apart — don't guess.
    for score, _ntoks, price, name, _toks in scored[1:]:
        if best_score - score > 1e-9:
            break
        if name != best_name and abs(price - best_price) > _SEALED_AMBIG_PRICE_TOL * max(price, best_price):
            return None
    return (best_price, best_name)


def parse_title(game: str, title: str):
    """Return the per-game lookup identifier, or None if none can be isolated."""
    if game == "pokemon":
        return parse_card(title)                       # (species, number, set_total)
    if game == "onepiece":
        m = _OP_CODE_RE.search(_ascii(title))
        return m.group(1).upper() if m else None       # "OP01-024"
    if game == "yugioh":
        m = _YUGIOH_CODE_RE.search(_ascii(title))
        return m.group(1).upper() if m else None
    if game in ("mtg", "lorcana"):
        return _name_tokens(title, game) or None        # ["sol", "ring"]
    return None


def fetch_price(game: str, parsed, title: str):
    """Dispatch to the right price source. Returns (market_price, name) or None."""
    if game == "pokemon":
        # English vs Japanese trade in separate markets — price each against its
        # own. Japanese cards use tcgcsv cat 85; English use pokemontcg.io. A
        # Japanese card with no Japanese-market match is left unpriced rather
        # than mispriced at its English value.
        if detect_language(title) == "Japanese":
            return fetch_japanese_price(*parsed)
        return fetch_market_price(*parsed)
    if game == "mtg":
        return fetch_mtg_price(parsed, title)
    if game == "lorcana":
        return fetch_lorcana_price(parsed, title)
    if game == "onepiece":
        return fetch_onepiece_price(parsed, title)
    if game == "yugioh":
        return fetch_yugioh_price(parsed, title)
    return None
