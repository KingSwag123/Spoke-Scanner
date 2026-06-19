"""
Shopify retail source — public /products.json scan.

A SECOND product source alongside the eBay open-market scan. Many TCG retailers
run on Shopify and expose their whole catalog (title, variants, prices,
availability) at the unauthenticated `https://<domain>/products.json` endpoint.
This module pages through that feed for a curated store list (config.SHOPIFY_STORES),
fetching the stores concurrently (bounded by config.SHOPIFY_MAX_WORKERS) so the
fetch phase stays roughly flat as the list grows, and normalizes each VARIANT into
the same listing dict the eBay pipeline already consumes, so the existing
classify → price → deal-test → route stages work unchanged.

Two signals come from this source:

  1. Below-market deals — AVAILABLE, SEALED variants from USD stores are fed into
     the normal deal pipeline by main.scan_open_market. The market prices we
     compare against (pokemontcg.io / Scryfall / Lorcast / tcgcsv) are USD, so a
     non-USD retail price can't be compared safely — non-USD stores are
     restock-only. Singles are excluded from the deal test: retail stores price
     them at market (so they never clear the test) and pricing thousands of them
     per cycle would overrun the cycle and throttle the price APIs.

  2. Sealed restock alerts — detect_restocks() watches each SEALED variant's
     availability ACROSS cycles (persisted in config.SHOPIFY_AVAILABILITY_FILE)
     and flags every out-of-stock → in-stock transition. The first time a variant
     is seen it is recorded silently as a baseline (no alert), so enabling a new
     store never floods the channel.

Normalized listing dicts carry three keys beyond the eBay shape:
  source="shopify", currency=<store currency>, available=<bool>.
The eBay-only seller-trust gate is skipped for source != "ebay" in main.
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from config import (
    _HTTP_HEADERS,
    MIN_PRICE_FLOOR,
    RESIDENTIAL_PROXY_URL,
    SHOPIFY_AVAILABILITY_FILE,
    SHOPIFY_GLOBAL_MIN_INTERVAL,
    SHOPIFY_MAX_PAGES,
    SHOPIFY_MAX_WORKERS,
    SHOPIFY_PAGE_LIMIT,
    SHOPIFY_REQUEST_INTERVAL,
    SHOPIFY_STATE_EXPIRY_DAYS,
    SHOPIFY_TIMEOUT,
    shopify_proxies,
)
from api_engines import detect_language, is_sealed


# ---------------------------------------------------------------------------
# Game inference (multi-game stores)
# ---------------------------------------------------------------------------
# Strong, low-false-positive tokens. A title/type that matches none yields None
# and the item is skipped downstream — we never guess a game.
_GAME_TOKENS = (
    ("pokemon",  ("pokemon", "pok\u00e9mon")),
    ("mtg",      ("magic: the gathering", "magic the gathering", " mtg ")),
    ("lorcana",  ("lorcana",)),
    ("onepiece", ("one piece", "one-piece")),
)


def infer_game(text: str) -> str | None:
    """Best-effort game id from a product's title/type; None if ambiguous."""
    low = f" {text.lower()} "
    for game, tokens in _GAME_TOKENS:
        if any(tok in low for tok in tokens):
            return game
    return None


# ---------------------------------------------------------------------------
# Fetch + normalize a store's /products.json into listing dicts
# ---------------------------------------------------------------------------

def _product_image(product: dict) -> str:
    imgs = product.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        return imgs[0].get("src", "") or ""
    return ""


# Proxy connection/auth errors raised by requests/urllib3 can embed the full
# RESIDENTIAL_PROXY_URL — including user:pass credentials — in their message.
# Redact any embedded credentials before an exception is ever logged.
_CREDENTIAL_RE = re.compile(r"(\w+://)[^/\s@]+@")


def _safe_err(e: Exception) -> str:
    """`Type: message` for logging, with the proxy URL / any embedded
    credentials (scheme://user:pass@host) redacted so secrets never hit logs."""
    msg = str(e)
    if RESIDENTIAL_PROXY_URL:
        msg = msg.replace(RESIDENTIAL_PROXY_URL, "<proxy>")
    msg = _CREDENTIAL_RE.sub(r"\1<redacted>@", msg)
    return f"{type(e).__name__}: {msg}"


# Rate limiters shared by ALL fetch threads. Most stores run on Shopify, which
# throttles /products.json by SOURCE IP across stores — so spacing requests within
# a single store (SHOPIFY_REQUEST_INTERVAL) is not enough once many stores fetch
# concurrently. This caps the AGGREGATE request rate from this process so scaling
# to many concurrent stores does not trip 429s.
#
# Direct and proxied requests get SEPARATE limiters: direct requests all egress
# from this one datacenter IP (Cloudflare rate-limits by source IP across every
# store), while proxied requests egress through a rotating residential IP that
# does not share that bottleneck — so the proxied stores fetch as a parallel
# stream and never steal request slots from the direct-path stores.
_DIRECT_STREAM = {"lock": threading.Lock(), "last": 0.0}
_PROXY_STREAM  = {"lock": threading.Lock(), "last": 0.0}


def _throttle(stream: dict) -> None:
    """Block until SHOPIFY_GLOBAL_MIN_INTERVAL has elapsed since the last request
    on this stream (direct or proxied) started, measured across every thread."""
    with stream["lock"]:
        wait = SHOPIFY_GLOBAL_MIN_INTERVAL - (time.monotonic() - stream["last"])
        if wait > 0:
            time.sleep(wait)
        stream["last"] = time.monotonic()


def fetch_shopify_listings(store: dict) -> list[dict]:
    """Page through a store's /products.json and normalize every variant.

    Returns ALL variants (available and not) so restock detection can see
    out-of-stock items; the deal pipeline filters to available ones itself.
    Network/parse errors are logged and end that store's scan — one bad store
    never aborts the cycle.
    """
    domain   = store["domain"]
    currency = store.get("currency", "USD")
    fixed    = store.get("game")
    name     = store.get("name", domain)
    proxied  = bool(store.get("proxy"))
    proxies  = shopify_proxies() if proxied else None
    stream   = _PROXY_STREAM if proxied else _DIRECT_STREAM

    listings: list[dict] = []
    for page in range(1, SHOPIFY_MAX_PAGES + 1):
        try:
            _throttle(stream)
            resp = requests.get(
                f"https://{domain}/products.json",
                headers=_HTTP_HEADERS,
                params={"limit": SHOPIFY_PAGE_LIMIT, "page": page},
                timeout=SHOPIFY_TIMEOUT,
                proxies=proxies,
            )
            resp.raise_for_status()
            products = resp.json().get("products", [])
        except (requests.RequestException, ValueError) as e:
            print(f"  [SHOPIFY][ERROR] {name} p{page} — {_safe_err(e)}")
            break
        if not products:
            break

        for p in products:
            title_base   = p.get("title", "")
            handle       = p.get("handle", "")
            product_type = p.get("product_type", "")
            image_url    = _product_image(p)
            # Prefer a confident inference over the store's fixed game so a
            # cross-listed other-game box (e.g. an MTG/One Piece box in a
            # pokemon-fixed store) routes to the right channel; fall back to the
            # fixed game when inference is unsure (most of a fixed store's catalog
            # has no explicit token, e.g. "Prismatic Evolutions Booster Box").
            game = infer_game(f"{title_base} {product_type}") or fixed

            for v in p.get("variants", []):
                try:
                    price = float(v["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                vid    = v.get("id")
                vtitle = (v.get("title") or "").strip()
                # Variant title is the condition/finish for singles ("Near Mint",
                # "Foil") and "Default Title" for sealed — fold the useful ones
                # into the listing title so the classifier/parser see them.
                has_variant = bool(vtitle) and vtitle.lower() != "default title"
                full_title  = f"{title_base} - {vtitle}" if has_variant else title_base
                listings.append({
                    "item_id":      f"shopify:{domain}:{vid}",
                    "title":        full_title,
                    "price":        price,
                    "shipping":     0.0,            # not exposed by products.json
                    "url":          f"https://{domain}/products/{handle}?variant={vid}",
                    "condition":    vtitle if has_variant else "Brand New",
                    "language":     detect_language(full_title),
                    "image_url":    image_url,
                    "store":        name,
                    "source":       "shopify",
                    "currency":     currency,
                    "available":    bool(v.get("available")),
                    "product_type": product_type,
                    "game_name":    game,
                })

        time.sleep(SHOPIFY_REQUEST_INTERVAL)

    print(f"  [SHOPIFY] {name}: {len(listings)} variants ({currency})")
    return listings


def fetch_all_shopify_listings(stores: list[dict]) -> list[dict]:
    """Fetch every store's /products.json concurrently; return all variants.

    The fetches are I/O-bound and each store is a separate domain, so running
    them in a bounded thread pool does NOT raise the request rate against any
    single store — each store still pages sequentially with its own polite
    SHOPIFY_REQUEST_INTERVAL delay. Concurrency (SHOPIFY_MAX_WORKERS) keeps the
    fetch phase roughly flat as the store list grows, so the cycle scales to
    many stores. fetch_shopify_listings handles its own network/parse errors and
    never raises; the gather is still defensive so no single store can abort it.
    """
    if not stores:
        return []
    # Proxy-only stores are unreachable from the bare datacenter IP. When no
    # RESIDENTIAL_PROXY_URL is configured, skip them outright rather than burning
    # a guaranteed-429 request on each one every cycle.
    if shopify_proxies() is None:
        proxy_only = [s for s in stores if s.get("proxy")]
        if proxy_only:
            stores = [s for s in stores if not s.get("proxy")]
            print(f"  [SHOPIFY] {len(proxy_only)} proxy-only store(s) skipped — "
                  f"set RESIDENTIAL_PROXY_URL to enable them")
    if not stores:
        return []
    workers = min(SHOPIFY_MAX_WORKERS, len(stores))
    listings: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shopify") as pool:
        futures = {pool.submit(fetch_shopify_listings, s): s for s in stores}
        for fut in as_completed(futures):
            store = futures[fut]
            try:
                listings.extend(fut.result())
            except Exception as e:  # defensive — per-store errors are handled inside
                name = store.get("name", store.get("domain", "?"))
                print(f"  [SHOPIFY][ERROR] {name} — gather failed: {_safe_err(e)}")
    return listings


# ---------------------------------------------------------------------------
# Restock state — per-variant availability across cycles
# ---------------------------------------------------------------------------

def load_availability() -> dict:
    """Load {item_id: snapshot} restock state from disk."""
    if Path(SHOPIFY_AVAILABILITY_FILE).exists():
        try:
            with open(SHOPIFY_AVAILABILITY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_availability(state: dict) -> None:
    with open(SHOPIFY_AVAILABILITY_FILE, "w") as f:
        json.dump(state, f, indent=2)


def cleanup_availability(state: dict) -> dict:
    """Drop variants not seen for SHOPIFY_STATE_EXPIRY_DAYS (anti-bloat)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SHOPIFY_STATE_EXPIRY_DAYS)
    out: dict = {}
    for k, v in state.items():
        ts = v.get("ts") if isinstance(v, dict) else None
        try:
            if ts and datetime.fromisoformat(ts) > cutoff:
                out[k] = v
        except (ValueError, TypeError):
            out[k] = v
    return out


def _snapshot(listing: dict, available: bool) -> dict:
    return {
        "available": available,
        "ts":        datetime.now(timezone.utc).isoformat(),
        "title":     listing["title"],
        "price":     listing["price"],
        "currency":  listing.get("currency", "USD"),
        "store":     listing.get("store", ""),
    }


def commit_available(state: dict, listing: dict) -> None:
    """Record a variant as in-stock — called only after a restock alert is
    delivered, so a transient send failure retries next cycle instead of being
    silently lost (mirrors the seen-on-confirmed-delivery rule for deals)."""
    state[listing["item_id"]] = _snapshot(listing, True)


def detect_restocks(shopify_listings: list[dict], state: dict) -> list[dict]:
    """Return SEALED variants that just transitioned out-of-stock → in-stock.

    Seeds unseen variants silently (baseline, no alert) and keeps non-transition
    states fresh (including in-stock → sold-out, so the next restock can fire).
    Transition variants are NOT committed here; main commits them to in-stock
    only after the alert is delivered (see commit_available).
    """
    restocks: list[dict] = []
    for it in shopify_listings:
        if it.get("game_name") is None:
            continue
        if not is_sealed(it["title"]):
            continue
        if it["price"] < MIN_PRICE_FLOOR:    # ignore cheap packs / singles
            continue
        item_id = it["item_id"]
        prev    = state.get(item_id)
        cur     = bool(it.get("available"))
        if prev is None:
            state[item_id] = _snapshot(it, cur)        # baseline — silent
            continue
        if (not prev.get("available")) and cur:
            restocks.append(it)                         # transition — alert + commit on send
        else:
            state[item_id] = _snapshot(it, cur)         # keep fresh (incl. sold-out)
    return restocks
