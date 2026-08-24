"""
Configuration for the Multi-Game Open-Market Dynamic Lookup Engine.

Holds ALL tunable settings and module-level data for the price watcher:
scan/deal thresholds, per-game eBay streams, the 3-tier × 4-game WEBHOOKS matrix,
API endpoints + credentials, HTTP headers, tcgcsv category ids, and the keyword
indicator sets / regexes that the classifier and parser functions consume.

This module performs NO project imports so it can be imported everywhere without
risk of a circular import.

Requirements:
  pip install requests
  EBAY_APP_ID  — Production App ID from developer.ebay.com/my/keys
  EBAY_CERT_ID — Production Cert ID (Client Secret) from same page
"""

import os
import re
import urllib.parse

# ---------------------------------------------------------------------------
# Live-posting / dry-run discriminator (see POST_TO_DISCORD below for the full
# rationale). Defined up here because the runtime STATE FILE paths depend on it:
# the workspace (dry-run) must keep its dedup/restock state in SEPARATE, git-
# ignored *.local.json files so its simulated "seen" items never get committed
# and seed the Deployment — which would make production silently skip real pings.
# ---------------------------------------------------------------------------
_DISCORD_LIVE = (
    os.environ.get("REPLIT_DEPLOYMENT") == "1"
    or os.environ.get("DISCORD_LIVE", "").strip().lower() in ("1", "true", "yes")
)
_STATE_SUFFIX = "" if _DISCORD_LIVE else ".local"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEEN_FILE        = f"seen_listings{_STATE_SUFFIX}.json"   # permanent dedup state; .local in dry-run

CHECK_INTERVAL   = 300     # seconds between scan cycles (5 min)
SEEN_EXPIRY_DAYS = 90      # drop seen entries older than this (anti-bloat)

DEAL_RATIO       = 0.85   # a deal = (price + shipping) <= market_price * DEAL_RATIO
SEALED_SANITY_FLOOR = 0.20 # backstop: a sealed "deal" below market × this is almost
                           # always a wrong/bulk match, not a real deal — drop it
SINGLE_SANITY_FLOOR = 0.25 # same backstop for singles: a listing below market × this
                           # is almost always a wrong cross-set/printing match (e.g. a
                           # $5 Celebrations reprint priced against a $229 Base Set card)
MIN_PRICE_FLOOR  = 15.00   # hard floor: drop any listing under this BEFORE any API call
PREMIUM_THRESHOLD = 100.00 # singles with market price >= this → premium channel, else budget
EBAY_STREAM_LIMIT = 50     # listings fetched per broad stream per cycle

# Per-game broad newly-listed Buy-It-Now streams. Each game gets both singles-
# oriented and sealed-oriented queries so sealed product actually surfaces.
# Every listing fetched here is tagged with its game_name through the pipeline.
GAME_STREAMS = {
    "pokemon": [
        "pokemon card single",
        "pokemon single card",
        "pokemon ex card",
        "pokemon card psa",
        "pokemon booster box",
        "pokemon elite trainer box",
    ],
    "mtg": [
        "mtg single card",
        "magic the gathering card",
        "mtg booster box",
        "magic the gathering elite trainer box",
    ],
    "lorcana": [
        "disney lorcana single card",
        "lorcana card",
        "lorcana booster box",
        "lorcana illumineer trove",
    ],
    "onepiece": [
        "one piece card single",
        "one piece tcg card",
        "one piece booster box",
        "one piece booster case",
    ],
}

# Human-readable game labels for Discord embeds and logs.
GAME_DISPLAY = {
    "pokemon":  "Pokémon",
    "mtg":      "Magic: The Gathering",
    "lorcana":  "Disney Lorcana",
    "onepiece": "One Piece",
}

# Pricing/deal-testing is wired up for every game with a live price source:
#   pokemon → pokemontcg.io | mtg → Scryfall | lorcana → Lorcast | onepiece → tcgcsv
PRICED_GAMES = {"pokemon", "mtg", "lorcana", "onepiece"}

# ---------------------------------------------------------------------------
# Shopify retail source (see shopify_source.py) — a second product source that
# scans curated TCG retailers' public /products.json for restock + deal signals.
# ---------------------------------------------------------------------------
SHOPIFY_AVAILABILITY_FILE = f"shopify_availability{_STATE_SUFFIX}.json"  # restock state; .local in dry-run
SHOPIFY_MAX_PAGES         = 5      # /products.json pages scanned per store (250 products each)
SHOPIFY_PAGE_LIMIT        = 250    # products per page (Shopify hard max)
SHOPIFY_REQUEST_INTERVAL  = 0.5    # polite delay (seconds) between Shopify HTTP requests
SHOPIFY_TIMEOUT           = 20     # per-request timeout — stores often sit behind a CDN
SHOPIFY_STATE_EXPIRY_DAYS = 30     # prune availability entries unseen this long
SHOPIFY_MAX_WORKERS       = 16     # upper bound on concurrent in-flight fetches. Throughput is
                                   # governed by SHOPIFY_GLOBAL_MIN_INTERVAL (below), not this —
                                   # workers only cap how many requests are in flight at once when
                                   # responses are slow. Each store is a separate domain and pages
                                   # sequentially with its own polite SHOPIFY_REQUEST_INTERVAL.
SHOPIFY_GLOBAL_MIN_INTERVAL = 0.5  # min seconds between ANY two Shopify requests across ALL
                                   # fetch threads (~2 req/s aggregate). These stores sit behind
                                   # Cloudflare, which bot-challenges (cf-mitigated: challenge →
                                   # 429) by SOURCE IP across ALL stores at once — a concurrent
                                   # burst from our datacenter IP soft-bans every store, even
                                   # ones that were working. 0.5s mirrors the proven-safe rate of
                                   # the original sequential fetch; it spaces request STARTS (the
                                   # in-flight count is still bounded by SHOPIFY_MAX_WORKERS under
                                   # slow responses). Fetch time is linear in total requests, so
                                   # ~250 req/cycle (~50 stores × 5 pages) is the practical ceiling
                                   # within the 5-min window.

# Proxy lane is metered (residential proxies bill by GB), so proxy=True stores
# are swept on a slower cadence and at shallower depth than the free direct
# stores. ~2 pages every 30 min × 14 stores ≈ 3.4 GB/mo (gzipped ~85 KB/page) —
# fits a 5 GB plan with headroom. Direct stores keep SHOPIFY_MAX_PAGES/cycle.
SHOPIFY_PROXY_MAX_PAGES     = 2     # /products.json pages per PROXY store (direct uses SHOPIFY_MAX_PAGES)
SHOPIFY_PROXY_SCAN_INTERVAL = 1800  # min seconds between proxy-store sweeps (30 min)


# ---------------------------------------------------------------------------
# Mercari sources (see mercari_source.py) — third product source.
#   JP lane: Mercari Japan's official app API (DPoP-signed request, free, works
#     from the datacenter IP — no proxy). Sealed Japanese Pokémon product only:
#     titles are Japanese, so a curated JP→EN set-name matcher maps each listing
#     to its tcgcsv "Pokemon Japan" (cat 85) sealed product for the deal test.
#     Prices are JPY → converted to USD with a cached FX rate (fail-closed:
#     no rate ⇒ the lane is skipped that cycle, never mispriced).
#   US lane: mercari.com sits behind a JS-challenge wall (blocks datacenter AND
#     residential-proxy plain HTTP), so it is fetched through the Scrapfly
#     scraping API. Gated on SCRAPFLY_API_KEY — lane is skipped when unset.
# ---------------------------------------------------------------------------
MERCARI_JP_QUERIES = [
    # Sealed-box focused searches; each is (game, keyword). Japanese keywords
    # deliberately include 未開封/シュリンク (unopened / shrink-wrapped) signals.
    ("pokemon", "ポケモンカード BOX シュリンク付き 未開封"),
    ("pokemon", "ポケモンカード 拡張パック BOX 新品未開封"),
]
MERCARI_JP_PAGE_SIZE        = 60     # newest-first listings per query
MERCARI_JP_REQUEST_INTERVAL = 2.0    # polite spacing between JP API calls (s)
MERCARI_JP_MIN_PRICE_JPY    = 4000   # ignore loose packs / junk below this

# USD↔JPY conversion for the JP lane (open.er-api.com, free, no key).
FX_RATE_URL = "https://open.er-api.com/v6/latest/USD"
FX_RATE_TTL = 6 * 3600               # refresh at most every 6h; stale rate kept on failure

SCRAPFLY_API_KEY = os.environ.get("SCRAPFLY_API_KEY", "")
MERCARI_US_QUERIES = [
    # (game, keyword) — English titles flow through the normal eBay-style
    # sealed/single evaluation, so broad sealed searches are enough.
    ("pokemon",  "pokemon booster box sealed"),
    ("pokemon",  "pokemon elite trainer box"),
    ("mtg",      "mtg booster box sealed"),
    ("lorcana",  "lorcana booster box sealed"),
    ("onepiece", "one piece booster box sealed"),
]
MERCARI_US_SCAN_INTERVAL = 1800      # min seconds between US sweeps (Scrapfly credits)
MERCARI_US_ASSUMED_SHIPPING = 8.00   # search data hides the buyer's shipping cost;
                                     # assume a typical charge so the deal test stays
                                     # conservative (never understate the total)

# ---------------------------------------------------------------------------
# Residential proxy (optional) — unlocks the Cloudflare-strict stores that 429
# this datacenter IP on every request. Set RESIDENTIAL_PROXY_URL to a rotating
# residential proxy endpoint, e.g. "http://user:pass@gateway.provider.com:7000".
# Stores marked {"proxy": True} fetch through it as a SEPARATE request stream
# (their own rate limiter), so they never slow the direct-path stores. When this
# is unset those stores are skipped entirely — no dead-weight 429s.
# ---------------------------------------------------------------------------
RESIDENTIAL_PROXY_URL = os.environ.get("RESIDENTIAL_PROXY_URL", "").strip()


_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


def proxy_url_is_valid() -> bool:
    """True only when RESIDENTIAL_PROXY_URL looks like a usable proxy endpoint
    (scheme://[user:pass@]host[:port]). Guards against a stray value — e.g. a
    pasted `curl ...` command or a host:port with no scheme — that would
    otherwise raise InvalidURL on every proxied request."""
    url = RESIDENTIAL_PROXY_URL
    if "://" not in url:
        return False
    if url.split("://", 1)[0].lower() not in _PROXY_SCHEMES:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # accessing .port raises ValueError on a non-numeric / out-of-range port
    except ValueError:
        return False
    return bool(parts.hostname)


def shopify_proxies() -> dict | None:
    """requests-style proxies mapping for proxy=True stores, or None when no
    valid RESIDENTIAL_PROXY_URL is configured (callers then skip those stores)."""
    if not proxy_url_is_valid():
        return None
    return {"http": RESIDENTIAL_PROXY_URL, "https": RESIDENTIAL_PROXY_URL}


# Curated TCG retailers exposing a public Shopify /products.json (verified live).
#   currency : the store's selling currency (from https://<domain>/meta.json).
#              Only USD stores run the below-market deal test — the market prices
#              we compare against are USD, so a non-USD price isn't comparable.
#              Non-USD stores are RESTOCK-ONLY (still useful for sealed restocks).
#   game     : fixed game id for single-game stores, or None to infer per item
#              from the product title/type (multi-game stores).
# Add or remove stores here; nothing else needs to change.
SHOPIFY_STORES = [
    {"domain": "pokemonplug.com",    "name": "Pokémon Plug",         "currency": "USD", "game": "pokemon"},
    {"domain": "pokeboxusa.com",     "name": "PokeBox USA",          "currency": "USD", "game": "pokemon"},
    {"domain": "poke-collect.com",   "name": "Poke-Collect",         "currency": "USD", "game": "pokemon"},
    {"domain": "skyboxct.com",       "name": "Skybox Collectibles",  "currency": "USD", "game": None},
    {"domain": "flipsidegaming.com", "name": "Flipside Gaming",      "currency": "USD", "game": None},
    {"domain": "finalbossgames.com", "name": "Final Boss Games",     "currency": "USD", "game": None},
    # High-traffic multi-game retailers (USD) — full deal test + restock:
    {"domain": "trollandtoad.com",   "name": "Troll and Toad",       "currency": "USD", "game": None},
    {"domain": "gamersguildaz.com",  "name": "Gamers Guild AZ",      "currency": "USD", "game": None},
    # High-traffic non-USD retailers — RESTOCK-ONLY (deal test skipped, prices not USD):
    {"domain": "store.401games.ca",  "name": "401 Games",            "currency": "CAD", "game": None},
    {"domain": "facetofacegames.com","name": "Face to Face Games",   "currency": "CAD", "game": None},
    {"domain": "totalcards.net",     "name": "Total Cards",          "currency": "GBP", "game": None},
    # Non-USD stores are restock-only (deal test skipped; prices not USD-comparable).
    # Additional verified Shopify TCG retailers (live /products.json confirmed):
    {"domain": "skyfoxgames.com",          "name": "Sky Fox Games",           "currency": "USD", "game": None},
    {"domain": "cardmerchant.co.nz",       "name": "Card Merchant NZ",        "currency": "NZD", "game": None},
    {"domain": "gameknight.ca",            "name": "Game Knight",             "currency": "CAD", "game": None},
    {"domain": "everythinggames.ca",       "name": "Everything Games",        "currency": "CAD", "game": None},
    {"domain": "enterthebattlefield.ca",   "name": "Enter the Battlefield",   "currency": "CAD", "game": None},
    {"domain": "mythicstore.ca",           "name": "Mythic Store",            "currency": "CAD", "game": None},
    {"domain": "levelupgames.ca",          "name": "Level Up Games",          "currency": "CAD", "game": None},
    {"domain": "gamezilla.ca",             "name": "Gamezilla",               "currency": "CAD", "game": None},
    {"domain": "trinityhobby.ca",          "name": "Trinity Hobby",           "currency": "CAD", "game": None},
    # Batch 3 — verified live AND reliably reachable from this datacenter IP (restock-only, CAD):
    {"domain": "hobbiesville.com",         "name": "Hobbiesville",            "currency": "CAD", "game": None},
    {"domain": "hairyt.com",               "name": "Hairy Tarantula",         "currency": "CAD", "game": None},
    {"domain": "blackknightgames.ca",      "name": "Black Knight Games",      "currency": "CAD", "game": None},
    {"domain": "fusiongamingonline.com",   "name": "Fusion Gaming Online",    "currency": "CAD", "game": None},
    {"domain": "gamebreakers.ca",          "name": "Game Breakers",           "currency": "CAD", "game": None},
    {"domain": "untouchables.ca",          "name": "Untouchables",            "currency": "CAD", "game": None},
    {"domain": "vortexgames.ca",           "name": "Vortex Games",            "currency": "CAD", "game": None},
    # Cloudflare-strict stores — they 429 this datacenter IP on every request, so
    # each is marked proxy=True: fetched through RESIDENTIAL_PROXY_URL when it is
    # set (as a separate request stream), and skipped entirely when it is not.
    # The USD ones run the full below-market deal test; the rest are restock-only.
    {"domain": "cardsmiths.com",           "name": "Cardsmiths",              "currency": "USD", "game": None, "proxy": True},
    {"domain": "collectorstore.com",       "name": "Collector Store",         "currency": "USD", "game": None, "proxy": True},
    {"domain": "gamekastle.com",           "name": "Game Kastle",             "currency": "USD", "game": None, "proxy": True},
    {"domain": "gnomegames.com",           "name": "Gnome Games",             "currency": "USD", "game": None, "proxy": True},
    {"domain": "potomacdistribution.com",  "name": "Potomac Distribution",    "currency": "USD", "game": None, "proxy": True},
    {"domain": "thecardvault.com",         "name": "The Card Vault",          "currency": "USD", "game": None, "proxy": True},
    {"domain": "thegamersden.com",         "name": "The Gamers Den",          "currency": "USD", "game": None, "proxy": True},
    {"domain": "yourplaymat.com",          "name": "Your Playmat",            "currency": "USD", "game": None, "proxy": True},
    {"domain": "goblingaming.co.uk",       "name": "Goblin Gaming",           "currency": "GBP", "game": None, "proxy": True},
    {"domain": "leisuregames.com",         "name": "Leisure Games",           "currency": "GBP", "game": None, "proxy": True},
    {"domain": "guf.com.au",               "name": "GUF",                     "currency": "AUD", "game": None, "proxy": True},
    {"domain": "goodgames.com.au",         "name": "Good Games",              "currency": "AUD", "game": None, "proxy": True},
    {"domain": "gamesportal.com.au",       "name": "Games Portal",            "currency": "AUD", "game": None, "proxy": True},
    {"domain": "topdeckgames.com.au",      "name": "Top Deck Games",          "currency": "AUD", "game": None, "proxy": True},
]

# ---------------------------------------------------------------------------
# Webhook routing matrix — 4 games × 3 tiers (premium / budget / sealed).
# URLs come from the Secrets tab, one secret per slot, named GAME_TIER_WEBHOOK:
#   POKEMON_PREMIUM_WEBHOOK   POKEMON_BUDGET_WEBHOOK   POKEMON_SEALED_WEBHOOK
#   MTG_PREMIUM_WEBHOOK       MTG_BUDGET_WEBHOOK       MTG_SEALED_WEBHOOK
#   LORCANA_PREMIUM_WEBHOOK   LORCANA_BUDGET_WEBHOOK   LORCANA_SEALED_WEBHOOK
#   ONEPIECE_PREMIUM_WEBHOOK  ONEPIECE_BUDGET_WEBHOOK  ONEPIECE_SEALED_WEBHOOK
# Any slot you leave unset falls back to DISCORD_WEBHOOK_URL (a single
# catch-all channel). Slots that resolve to nothing are skipped gracefully.
# ---------------------------------------------------------------------------
WEBHOOK_PLACEHOLDER = "URL_HERE"
_WEBHOOK_FALLBACK = os.environ.get("DISCORD_WEBHOOK_URL", "")


def _slot(game: str, tier: str) -> str:
    """Read GAME_TIER_WEBHOOK from the env, else fall back to DISCORD_WEBHOOK_URL."""
    return os.environ.get(f"{game.upper()}_{tier.upper()}_WEBHOOK", "") or _WEBHOOK_FALLBACK


WEBHOOKS = {
    game: {tier: _slot(game, tier) for tier in ("premium", "budget", "sealed")}
    for game in ("pokemon", "mtg", "lorcana", "onepiece")
}

# Dedicated restock channel — a SINGLE webhook that receives every game's retail
# restock alert. When set, restock pings go here instead of each game's #sealed
# channel; when unset they fall back to #sealed (legacy behavior), so this is
# safe to leave blank.
RESTOCK_WEBHOOK = os.environ.get("RESTOCK_WEBHOOK", "")

# ---------------------------------------------------------------------------
# Live-posting guard — the fix for duplicate Discord pings from two instances.
#
# The dev workspace and the published Deployment run the SAME loop against the
# SAME webhook secrets but keep SEPARATE dedup state, so when both run every
# alert fires twice. Discord posting is therefore LIVE only inside a Deployment:
# Replit sets REPLIT_DEPLOYMENT="1" there and nowhere else, so the workspace copy
# runs in DRY-RUN (logs what it WOULD post, sends nothing). Set DISCORD_LIVE=1 to
# force real delivery from the workspace when you explicitly want to test posting.
# (Decided once as _DISCORD_LIVE at the top of this file; reused here.)
# ---------------------------------------------------------------------------
POST_TO_DISCORD = _DISCORD_LIVE

EBAY_APP_ID  = os.environ.get("EBAY_APP_ID", "")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "")

EBAY_TOKEN_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE      = "https://api.ebay.com/oauth/api_scope"
POKEMON_TCG_URL = "https://api.pokemontcg.io/v2/cards"

# pokemontcg.io anonymous access is rate-limited — space out live calls.
_API_MIN_INTERVAL = 0.6    # minimum seconds between TCG API calls

# ---------------------------------------------------------------------------
# Language filtering
# ---------------------------------------------------------------------------
_BLOCKED_LANGS = {
    "chinese", "simplified chinese", "traditional chinese",
    "korean", "french", "german", "spanish", "portuguese",
    "italian", "thai", "s-chinese",
}


# Japanese-script chars (hiragana / katakana / CJK ideographs) are an unambiguous
# JP signal even when the seller writes the rest of the title in English.
_JP_SCRIPT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]")
# Bare "jp" token (word-boundary) — a very common JP marker eBay sellers use that
# the plain substring checks miss; must be Japanese, never English fallback.
_JP_TOKEN_RE  = re.compile(r"\bjp\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Authenticity filter — block DIY / proxy / fan-art listings
# ---------------------------------------------------------------------------
# NOTE: "display card" was removed as a fake indicator because "display" is now a
# legitimate sealed-product keyword (booster display); keeping it here would drop
# legit sealed display listings. Fan-art/proxy displays are still caught by the
# other indicators below.
_FAKE_INDICATORS = {
    "fan art", "fan-art", "custom card", "proxy", "diy",
    "replica", "metal card", "art card",
    "3d print", "bootleg", "homemade", "unofficial",
    "gold foil fan", "foil fan art", "not official", "custom made",
    "custom printed", "novelty", "altered art",
}


# ---------------------------------------------------------------------------
# Lot filter — single cards only
# ---------------------------------------------------------------------------
_LOT_INDICATORS = {
    "card lot", "lot of", " lot ", "bulk lot",
    "random lot", "mixed lot", "wholesale", "collection lot",
    "mystery lot", "grab bag",
    "50 cards", "100 cards", "25 cards", "20 cards", "10 cards",
    "choose your", "choose a ", " choose ", "pick your", " pick ",
    " bulk ", " bundle ", " playset ",
    " collection ", "card collection",
    " complete ", "complete set",
    " set ", "card set",
    " pack ", " booster ",
}


# ---------------------------------------------------------------------------
# Graded slab detector — drives routing only (PSA/BGS/CGC/Slab → PREMIUM)
# ---------------------------------------------------------------------------
_SLAB_INDICATORS = {
    " psa ", " bgs ", " cgc ", " sgc ", " ace ", " slab ", " slabbed ",
    " graded ", " gem mint ", " gem mt ",
}

# Grade with an inline numeric score and no/loose separator: "PSA10", "PSA-10",
# "BGS 9.5", "CGC9.8", "SGC 10". The space-padded set above misses these.
_SLAB_GRADE_RE = re.compile(r"(?:^|[^a-z])(psa|bgs|cgc|sgc|csg)\s*-?\s*(?:10|\d(?:\.\d)?)", re.I)


# ---------------------------------------------------------------------------
# Sealed-product detector — drives SEALED routing, bypasses single/lot filters
# ---------------------------------------------------------------------------
_SEALED_INDICATORS = (
    "booster box", "elite trainer box", "etb", "blister",
    "factory sealed", "booster pack", "booster case", "booster bundle",
    "booster display", "display box", "illumineer trove", "build & battle",
    "build and battle", "sealed box", "collection box",
)


# Strong single-card / accessory signals. If present, the listing is a single
# (or a card accessory), never a sealed product — even when the title also
# contains a sealed keyword as a substring (e.g. "Charizard ETB Individual Card
# Sleeve" is a single card, not a sealed Elite Trainer Box).
_SINGLE_OVERRIDE = (
    " individual ", " single card ", " card single ",
    " card sleeve ", " card sleeves ", " toploader ", " top loader ", " toploaded ",
)


# ---------------------------------------------------------------------------
# Seller feedback filter — keep scam/low-trust sellers out of alerts
# ---------------------------------------------------------------------------
MIN_SELLER_FEEDBACK_SCORE = 10     # minimum number of feedback ratings
MIN_SELLER_FEEDBACK_PCT   = 97.0   # minimum positive feedback percentage


# ---------------------------------------------------------------------------
# Real-time title parsing — extract a card-number fraction + a species keyword
# ---------------------------------------------------------------------------
_FRACTION_RE = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")

# Suffixes that are part of a card name but not the distinctive species token.
_NAME_SUFFIX = {"ex", "gx", "v", "vmax", "vstar", "vunion", "tera"}

# Tokens that appear near the number but are NOT the species name (finishes,
# rarities, card types, grading, condition, language, generic noise).
_NAME_NOISE = {
    "pokemon", "card", "cards", "tcg", "the", "and", "with", "new", "vintage",
    "set", "error", "misprint", "swirl", "centered", "pack", "fresh", "lot",
    "single", "htf", "official", "genuine", "authentic", "no",
    # finishes / colors
    "holo", "holographic", "foil", "reverse", "rev", "shiny", "rainbow",
    "gold", "golden", "silver", "radiant", "amazing", "shining", "crystal",
    "prime", "prism", "star", "tera",
    # rarity descriptors
    "rare", "common", "uncommon", "secret", "ultra", "hyper", "full", "art",
    "illustration", "special", "sir", "ir", "alt", "alternate", "promo",
    "character", "trainer", "supporter", "stadium", "energy", "basic", "item",
    "tool", "stage",
    # grading / condition
    "near", "mint", "nm", "lp", "mp", "hp", "dmg", "played", "gem", "psa",
    "bgs", "cgc", "ace", "graded", "grade", "slab",
    # language
    "japanese", "english", "jpn", "eng", "japan",
}


# ---------------------------------------------------------------------------
# Multi-game market lookup HTTP config
#   MTG       → Scryfall   (fuzzy card-name search, no key)
#   Lorcana   → Lorcast    (card-name search, no key)
#   One Piece → tcgcsv     (bulk TCGplayer price index keyed by card code)
# Pokémon keeps its pokemontcg.io path. All sources are free / keyless.
# ---------------------------------------------------------------------------
_HTTP_HEADERS      = {
    # tcgcsv 403-blocks identifiable bot User-Agents ("flagged for overuse"); a
    # standard browser UA is served normally. Keep this realistic, not custom.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
# Scryfall/Lorcast are the OPPOSITE of tcgcsv: their API guidelines REQUIRE an
# identifying (non-browser) User-Agent plus an explicit Accept header, and their
# anti-abuse layer returns 429/403 for generic browser UAs hammering the API.
# Sending the tcgcsv browser UA here is what triggered the mid-cycle 429 cascade
# that starved MTG/Lorcana lookups, so the fast path gets its own honest headers.
_FAST_API_HEADERS  = {
    "User-Agent": "TCGDealWatcher/1.0 (open-market deal monitor)",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}
_FAST_API_INTERVAL = 0.2       # Scryfall/Lorcast cap at 10 req/s — 5 req/s keeps margin for bursts


# Tokens that mark the END of a card name in an eBay title (rarity / condition /
# finish / language / brand / generic noise). The card name is the leading run
# of tokens before any of these or any digit.
_NAME_STOPWORDS = {
    "nm", "mint", "lp", "mp", "hp", "dmg", "played", "gem", "psa", "bgs", "cgc",
    "graded", "grade", "slab", "foil", "holo", "holographic", "nonfoil", "etched",
    "reverse", "shiny", "rainbow", "rare", "common", "uncommon", "mythic",
    "legendary", "enchanted", "super", "secret", "ultra", "hyper", "promo",
    "presale", "preorder", "new", "sealed", "near", "lightly", "moderately",
    "heavily", "damaged", "english", "japanese", "jpn", "eng", "lot", "single",
    "tcg", "ccg", "card", "cards", "disney", "ravensburger", "bandai", "namco",
    "parallel", "alt", "alternate", "leader",
}

# Leading game-brand prefixes stripped before isolating the card name.
_GAME_PREFIXES = {
    "mtg":      ("magic the gathering", "mtg", "magic"),
    "lorcana":  ("disney lorcana", "lorcana tcg", "lorcana"),
    "onepiece": ("one piece card game", "one piece tcg", "one piece"),
}


# One Piece: tcgcsv exposes TCGplayer data per game. Category 68 = One Piece.
# We build a {card_code: (name, market_price)} index once, refreshed periodically,
# and match each listing by its OP/ST/EB/PRB card code (e.g. "OP01-024").
# tcgcsv category ids per game (used by the One Piece singles index AND the
# cross-game sealed-product price index further below).
# "pokemon_jp" is a pseudo-game used ONLY by the sealed-price index so Mercari JP
# boxes are priced against the Japanese category (85) instead of the English one.
TCGCSV_CATEGORY = {"pokemon": 3, "mtg": 1, "lorcana": 71, "onepiece": 68,
                   "pokemon_jp": 85}
TCGCSV_ONEPIECE_CAT   = TCGCSV_CATEGORY["onepiece"]
_OP_CODE_RE           = re.compile(r"\b((?:OP|ST|EB|PRB)\d{2}-\d{3})\b", re.IGNORECASE)
_ONEPIECE_INDEX_TTL   = 6 * 3600   # rebuild the index at most every 6h on success
_ONEPIECE_INDEX_RETRY = 600        # after a failed build, wait 10 min before retrying


# ---------------------------------------------------------------------------
# Japanese Pokémon singles pricing via tcgcsv (category 85 = "Pokemon Japan").
#   English Pokémon are priced by pokemontcg.io (English TCGplayer market).
#   Japanese cards trade in a DIFFERENT market — often wildly different — so they
#   MUST be priced against the Japanese category or their value is meaningless.
# ---------------------------------------------------------------------------
TCGCSV_POKEMON_JP_CAT = 85
_JP_INDEX_TTL   = 6 * 3600        # rebuild at most every 6h on success
_JP_INDEX_RETRY = 600             # after a failed build, wait 10 min before retry


# ---------------------------------------------------------------------------
# Sealed-product market pricing (all games) via tcgcsv
#   Sealed products are the tcgcsv entries that carry NO card "Number" in their
#   extendedData (booster boxes, ETBs, bundles, blisters, decks, cases, …).
# ---------------------------------------------------------------------------
_SEALED_INDEX_TTL   = 6 * 3600    # rebuild a game's sealed index at most every 6h
_SEALED_INDEX_RETRY = 600         # after a failed build, wait 10 min before retry
_SEALED_MIN_TOKENS  = 3           # require this many distinctive tokens to match

# Brand / filler tokens dropped from a product name so they aren't *required* in
# the eBay title (sellers omit "Disney", "of", "the", etc.). Distinctive set-name
# and product-type tokens (booster, box, bundle, etb, blister, case, …) survive.
_SEALED_DROP = {
    "the", "of", "a", "an", "and", "tcg", "ccg", "trading", "card", "game",
    "games", "sealed", "new", "english", "edition", "factory", "disney",
    "lorcana", "pokemon", "pokémon", "magic", "gathering", "mtg", "one", "piece",
}

# Bulk / wholesale container SKUs. A "Case" holds ~6-12 retail units, so its market
# price is 10×+ a single unit's. This wrecks single-unit matching: a normally-priced
# single whose noisy eBay title merely contains "case" (e.g. a "protective case",
# "factory sealed case fresh") matches the bulk SKU and looks like an unreal deal.
# We only hard-exclude "case" — it is unambiguously a bulk container in tcgcsv. We do
# NOT exclude "display" because in some games a "Booster Display" is a legitimate
# single retail unit (the booster box itself); genuine bulk "display" mismatches are
# instead caught by the SEALED_SANITY_FLOOR backstop below.
_SEALED_BULK_TOKENS = {"case"}

# Tie-break safety: if two products tie on the top match score but their market
# prices differ by more than this fraction, the match is ambiguous — skip rather
# than ping with a guessed (and likely wrong) price.
_SEALED_AMBIG_PRICE_TOL = 0.05


# ---------------------------------------------------------------------------
# Discord alert — channel colors
# ---------------------------------------------------------------------------
_CHANNEL_COLORS = {
    "premium": 0xF1C40F,   # gold  — graded slabs / high-end
    "budget":  0x2ECC71,   # green — raw singles
    "sealed":  0x3498DB,   # blue  — sealed product
    "restock": 0x1ABC9C,   # teal  — Shopify retail restock alerts
}
