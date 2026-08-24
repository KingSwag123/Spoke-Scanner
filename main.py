"""
Multi-Game Open-Market Dynamic Lookup Engine
--------------------------------------------
Monitors broad "Buy It Now" streams of newly-listed TCG listings on eBay across
four games (Pokémon, Magic: The Gathering, Lorcana, One Piece). Each listing is
classified up front as either a SEALED product or a single card, and routed into
that game's dedicated Discord channel via a hardcoded 3-tier webhook matrix.

Per scan cycle:
  per-game eBay streams (FIXED_PRICE, newly listed; singles + sealed queries)
    → de-dupe across overlapping streams
    → $15 hard price floor / language / seller-trust safeguards
    → SEALED detection (booster box, ETB, blister, …) — bypasses single-card
      and official-card filters, skips pricing, routes straight to #sealed
    → single cards: official-card / single-vs-lot safeguards
        → pricing is wired up for Pokémon only (pokemontcg.io); other games'
          singles are skipped with a "pricing not yet supported" log line
        → parse title → live lookup → deal test (price+shipping <= market*0.75)
        → route: graded slab → #premium, else → #budget
    → rich Discord embed + permanent per-listing dedup

A second source (shopify_source) scans curated TCG retailers' public Shopify
/products.json each cycle: AVAILABLE USD SEALED variants flow through the same
deal pipeline, and sealed out-of-stock → in-stock transitions fire restock alerts.

Routing (3 tiers × 4 games = 12 channels) is read from the Secrets tab, one
secret per slot named GAME_TIER_WEBHOOK (e.g. MTG_PREMIUM_WEBHOOK). Any unset
slot falls back to DISCORD_WEBHOOK_URL. Empty slots are skipped, never crash.

This module is the orchestrator: eBay OAuth + feed fetching, the permanent
per-listing dedup store, the scan cycle, and the main loop. The TCG lookup
engine (api_engines), Discord routing/sending (discord_router), and all
settings (config) live in their own modules.

Requirements:
  pip install requests
  EBAY_APP_ID  — Production App ID from developer.ebay.com/my/keys
  EBAY_CERT_ID — Production Cert ID (Client Secret) from same page
"""

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from config import (
    CHECK_INTERVAL,
    DEAL_RATIO,
    EBAY_APP_ID,
    EBAY_BROWSE_URL,
    EBAY_CERT_ID,
    EBAY_SCOPE,
    EBAY_STREAM_LIMIT,
    EBAY_TOKEN_URL,
    GAME_DISPLAY,
    GAME_STREAMS,
    MIN_PRICE_FLOOR,
    POST_TO_DISCORD,
    PREMIUM_THRESHOLD,
    PRICED_GAMES,
    RESIDENTIAL_PROXY_URL,
    MERCARI_JP_QUERIES,
    MERCARI_US_QUERIES,
    MERCARI_US_SCAN_INTERVAL,
    RESTOCK_WEBHOOK,
    SCRAPFLY_API_KEY,
    SEALED_SANITY_FLOOR,
    SEEN_EXPIRY_DAYS,
    SEEN_FILE,
    SHOPIFY_PROXY_MAX_PAGES,
    SHOPIFY_PROXY_SCAN_INTERVAL,
    SHOPIFY_STORES,
    SINGLE_SANITY_FLOOR,
    WEBHOOKS,
    shopify_proxies,
)
from api_engines import (
    detect_language,
    fetch_price,
    fetch_sealed_price,
    is_allowed_language,
    is_official_card,
    is_sealed,
    is_single_card,
    is_trusted_seller,
    parse_title,
    reset_cycle_cache,
)
from discord_router import (
    determine_channel,
    send_discord_alert,
    send_restock_alert,
    send_sealed_alert,
    webhook_is_set,
)
from mercari_source import (
    fetch_mercari_jp_listings,
    fetch_mercari_us_listings,
)
from shopify_source import (
    cleanup_availability,
    commit_available,
    detect_restocks,
    fetch_all_shopify_listings,
    load_availability,
    save_availability,
)


# ---------------------------------------------------------------------------
# Seen-listings store (permanent per-listing dedup)
# ---------------------------------------------------------------------------

def load_seen() -> dict:
    """Load {item_id: iso_timestamp} from disk."""
    if Path(SEEN_FILE).exists():
        try:
            with open(SEEN_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_seen(seen: dict) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def is_seen(item_id: str, seen: dict) -> bool:
    """Return True if this listing ID has already been alerted (permanent dedup)."""
    return item_id in seen


def mark_seen(item_id: str, seen: dict) -> None:
    seen[item_id] = datetime.now(timezone.utc).isoformat()
    # Write-through: persist immediately so a mid-cycle restart/crash can never
    # re-alert an item already sent (the end-of-cycle save alone loses sends
    # from a partial cycle and causes duplicate pings on the next run).
    save_seen(seen)


def cleanup_seen(seen: dict) -> dict:
    """Drop entries older than SEEN_EXPIRY_DAYS to prevent unbounded file growth."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_EXPIRY_DAYS)
    out = {}
    for k, v in seen.items():
        try:
            if datetime.fromisoformat(v) > cutoff:
                out[k] = v
        except (ValueError, TypeError):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# eBay OAuth — client credentials (app-level, no user login needed)
# ---------------------------------------------------------------------------

_token_cache: dict = {"token": None, "expires_at": 0}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    resp = requests.post(
        EBAY_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data=f"grant_type=client_credentials&scope={EBAY_SCOPE}",
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["token"]      = payload["access_token"]
    _token_cache["expires_at"] = now + payload.get("expires_in", 7200)
    return _token_cache["token"]


# ---------------------------------------------------------------------------
# eBay Browse — broad newly-listed Buy-It-Now stream
# ---------------------------------------------------------------------------

def fetch_ebay_listings(keywords: str, limit: int = EBAY_STREAM_LIMIT) -> list[dict]:
    """Query one broad keyword stream against eBay's newly-listed FIXED_PRICE feed."""
    try:
        token = get_access_token()
    except requests.RequestException as e:
        print(f"[ERROR] Could not get eBay token: {e}")
        return []

    try:
        resp = requests.get(
            EBAY_BROWSE_URL,
            headers={
                "Authorization":           f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            params={
                "q":             keywords,
                "filter":        "buyingOptions:{FIXED_PRICE}",
                "sort":          "newlyListed",
                "limit":         str(limit),
                "aspect_filter": "categoryAspect:Language:English|Japanese",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[ERROR] eBay Browse API failed: {e}")
        return []

    items = []
    for raw in data.get("itemSummaries", []):
        try:
            title = raw["title"]

            # Shipping cost: use the MAX buyer-paid option so total cost is never
            # understated (precision bias — avoids false-positive deal alerts).
            # Default to 0.0 when no shipping cost is published.
            ship_costs = [
                float(opt["shippingCost"]["value"])
                for opt in raw.get("shippingOptions", [])
                if opt.get("shippingCost", {}).get("value") is not None
            ]
            shipping = max(ship_costs) if ship_costs else 0.0

            thumb_list = raw.get("thumbnailImages", [])
            image_url  = (
                thumb_list[0]["imageUrl"] if thumb_list
                else raw.get("image", {}).get("imageUrl", "")
            )

            seller = raw.get("seller", {})
            items.append({
                "item_id":   raw["itemId"],
                "title":     title,
                "price":     float(raw["price"]["value"]),
                "shipping":  shipping,
                "url":       raw["itemWebUrl"],
                "condition": raw.get("condition", "Not specified"),
                "language":  detect_language(title),
                "image_url": image_url,
                "store":     "eBay",
                "_seller": {
                    "name":  seller.get("username", "unknown"),
                    "score": int(seller.get("feedbackScore", 0)),
                    "pct":   float(seller.get("feedbackPercentage", 100.0)),
                },
            })
        except (KeyError, ValueError):
            continue
    return items


# ---------------------------------------------------------------------------
# Scan cycle — broad streams → parse → live lookup → deal test → route
# ---------------------------------------------------------------------------

def scan_open_market(seen: dict, availability: dict) -> None:
    reset_cycle_cache()   # evict expired price-cache entries (prices persist across cycles via TTL)
    t_start = time.monotonic()

    # 1) Gather listings across every game's streams, de-duped by item_id this
    #    cycle. Each listing carries its game_name through the pipeline.
    cycle_ids: set[str] = set()
    listings: list[dict] = []
    for game_name, queries in GAME_STREAMS.items():
        for query in queries:
            results = fetch_ebay_listings(query)
            fresh   = 0
            for it in results:
                if it["item_id"] in cycle_ids:
                    continue
                cycle_ids.add(it["item_id"])
                it["game_name"] = game_name
                listings.append(it)
                fresh += 1
            print(f"[STREAM] {game_name}/'{query}' → {len(results)} listings ({fresh} new this cycle)")
    t_ebay = time.monotonic()

    # 1b) Shopify retail sources — restock alerts (sealed) + below-market deals.
    #     Fetch every store's full variant feed once: restock detection needs to
    #     see out-of-stock variants, the deal pipeline only the available ones.
    shopify_all = fetch_all_shopify_listings(SHOPIFY_STORES)

    # Restock pass: sealed variants that went out-of-stock → in-stock since the
    # last cycle. A variant's first sighting is seeded silently (no alert).
    restock_alerts = 0
    for it in detect_restocks(shopify_all, availability):
        game = it["game_name"]
        # Dedicated restock channel when configured (one channel for all games);
        # otherwise fall back to the game's #sealed channel (legacy behavior).
        if webhook_is_set(RESTOCK_WEBHOOK):
            rchannel, rwebhook = "restock", RESTOCK_WEBHOOK
        else:
            rchannel, rwebhook = determine_channel(game, it["title"], sealed=True)
        if not webhook_is_set(rwebhook):
            continue
        print(f"  [RESTOCK] {it['store']} {it['price']:.2f} {it['currency']} "
              f"→ #{game}/{rchannel} — {it['title'][:48]}")
        if send_restock_alert(
            it["title"], it["url"], it["price"], it["currency"],
            rwebhook, game, it["store"],
            language=it["language"], image_url=it["image_url"], channel=rchannel,
        ):
            commit_available(availability, it)
            restock_alerts += 1
            # Suppress a same-cycle [SEALED] deal ping for this same box: the
            # restock alert already fired, so claim its id in cycle_ids so the
            # deal pass below skips it. Only on a delivered restock — a failed
            # send leaves the deal pass free to still surface it (no lost
            # signal, no duplicate). A below-market box that stays in stock will
            # be picked up by the deal pass on a later cycle (no restock there).
            cycle_ids.add(it["item_id"])
    save_availability(availability)

    # Feed AVAILABLE, USD-priced, SEALED Shopify variants into the same deal
    # pipeline as eBay. Restriction rationale:
    #   • Non-USD stores are restock-only (prices aren't USD-comparable).
    #   • Sealed-only: retail stores price singles at market, so singles almost
    #     never clear the deal test — but each would cost a rate-limited market
    #     lookup (0.6s), and these stores carry thousands, which would overrun
    #     the 5-min cycle and throttle the price APIs. Sealed (boxes/ETBs at MSRP
    #     vs an inflated market) is the realistic, bounded retail arbitrage signal.
    shopify_deal = 0
    for it in shopify_all:
        if not it["available"] or it.get("currency") != "USD":
            continue
        if it["game_name"] is None or not is_sealed(it["title"]):
            continue
        if it["item_id"] in cycle_ids:
            continue
        cycle_ids.add(it["item_id"])
        listings.append(it)
        shopify_deal += 1
    print(f"[SHOPIFY] {len(shopify_all)} variants | {restock_alerts} restock alert(s) | "
          f"{shopify_deal} available USD sealed variant(s) → deal pipeline")

    # 1c) Mercari lanes — JP (free API, sealed JP boxes pre-mapped to tcgcsv
    #     names) and US (Scrapfly-fetched, evaluated like eBay). Both normalize
    #     to the common listing shape; per-listing errors are already handled
    #     inside each fetcher, and the whole lane must never abort the cycle.
    for fetcher, label in ((fetch_mercari_jp_listings, "Mercari JP"),
                           (fetch_mercari_us_listings, "Mercari US")):
        try:
            for it in fetcher():
                if it["item_id"] in cycle_ids:
                    continue
                cycle_ids.add(it["item_id"])
                listings.append(it)
        except Exception as e:   # defensive — a source outage isn't a cycle abort
            print(f"  [{label}][ERROR] gather failed: {type(e).__name__}: {e}")
    t_shopify = time.monotonic()

    print(f"[SCAN] {len(listings)} unique listings to evaluate")

    deals   = 0
    sealed_alerts = 0
    nopricing_logged: set[str] = set()
    drop = {
        "floor": 0, "language": 0, "untrusted": 0, "unofficial": 0,
        "lot": 0, "nopricing": 0, "noparse": 0, "seen": 0,
        "nomatch": 0, "nodeal": 0, "sanity": 0, "noslot": 0, "error": 0,
    }
    for item in listings:
        # Per-listing isolation: one malformed listing or unexpected lookup
        # error must never abort the whole cycle (which would also suppress the
        # [FUNNEL] summary). Log it, count it, and move on.
        try:
            title    = item["title"]
            price    = item["price"]
            shipping = item["shipping"]
            item_id  = item["item_id"]
            game     = item["game_name"]

            # 2) Hard price floor + cheap universal safeguards (no API cost).
            if price < MIN_PRICE_FLOOR:
                drop["floor"] += 1
                continue
            # Language filter: keyword-based, tuned for English titles. The JP
            # lane is Japanese BY DESIGN (priced against the Japanese market),
            # so it bypasses this check.
            if item.get("source") != "mercari_jp" and not is_allowed_language(title):
                drop["language"] += 1
                continue
            # Seller-trust is an eBay-only signal (feedback score/%). Curated
            # Shopify retailers carry no such object — trust them by inclusion.
            if item.get("source", "ebay") == "ebay" and not is_trusted_seller(item):
                drop["untrusted"] += 1
                continue

            # 3) Sealed products: look up the sealed market price and apply the same
            #    deal test as singles (total <= market × DEAL_RATIO). Routes to that
            #    game's #sealed channel; unmatched/over-priced sealed is dropped.
            #    Mercari JP listings force the sealed path (is_sealed can't read
            #    Japanese) and are priced against the JAPANESE sealed catalog
            #    (tcgcsv cat 85) via their pre-mapped English product phrase —
            #    JP boxes trade in a different market than English product.
            if is_sealed(title) or item.get("sealed"):
                if is_seen(item_id, seen):
                    drop["seen"] += 1
                    continue
                if item.get("source") == "mercari_jp":
                    match = fetch_sealed_price("pokemon_jp", item.get("en_title") or title)
                else:
                    match = fetch_sealed_price(game, title)
                if not match:
                    drop["nomatch"] += 1
                    print(f"  [NOMATCH] ${price:.2f} {game} sealed — {title[:48]}")
                    continue
                market_price, matched_name = match
                total = price + shipping
                if total > market_price * DEAL_RATIO:
                    drop["nodeal"] += 1
                    print(f"  [NODEAL]  sealed ${total:.2f} vs mkt ${market_price:.2f} — {matched_name[:40]}")
                    continue
                # Too-good-to-be-true backstop: a sealed total far below market is almost
                # always a wrong/bulk match (or empty/damaged lot), not a real deal.
                if total < market_price * SEALED_SANITY_FLOOR:
                    drop["nodeal"] += 1
                    print(f"  [BADMATCH] sealed ${total:.2f} vs mkt ${market_price:.2f} "
                          f"(<{SEALED_SANITY_FLOOR:.0%}) — {title[:40]}")
                    continue
                channel, webhook_url = determine_channel(game, title, sealed=True)
                if not webhook_is_set(webhook_url):
                    drop["noslot"] += 1
                    print(f"  [SKIP] empty slot #{game}/{channel} — {title[:48]}")
                    continue
                print(f"  [SEALED] ${total:.2f} vs mkt ${market_price:.2f} → #{game}/{channel} — {title[:48]}")
                sent = send_sealed_alert(
                    title, item["url"], price, shipping, market_price, webhook_url, game,
                    store=item.get("store", "eBay"),
                    condition=item["condition"],
                    language=item["language"],
                    image_url=item["image_url"],
                    matched_name=matched_name,
                )
                if sent:
                    mark_seen(item_id, seen)
                    sealed_alerts += 1
                continue

            # 4) Single cards: authenticity + lot safeguards.
            if not is_official_card(title):
                drop["unofficial"] += 1
                continue
            if not is_single_card(title):
                drop["lot"] += 1
                continue

            # 5) Pricing is only wired up for games with a live price source.
            if game not in PRICED_GAMES:
                drop["nopricing"] += 1
                if game not in nopricing_logged:
                    nopricing_logged.add(game)
                    print(f"  [NOPRICE] pricing not yet supported for {game} singles — routing skipped")
                continue

            # 6) Parse the title into this game's lookup identifier; skip if none.
            parsed = parse_title(game, title)
            if not parsed:
                drop["noparse"] += 1
                continue

            # 7) Permanent dedup — before the API call so repeats don't cost lookups.
            if is_seen(item_id, seen):
                drop["seen"] += 1
                continue

            # 8) Live market lookup (rate-limited, cached, per-game source).
            match = fetch_price(game, parsed, title)
            if not match:
                drop["nomatch"] += 1
                print(f"  [NOMATCH] ${price:.2f} {game} — {title[:48]}")
                continue
            market_price, matched_name = match

            # Pokémon embeds historically show the card number; keep that detail.
            match_label = matched_name
            if game == "pokemon":
                _, number, set_total = parsed
                mkt_src = "JP" if detect_language(title) == "Japanese" else "EN"
                match_label = f"{matched_name} {number}/{set_total} [{mkt_src}]"

            # 9) Deal test: (price + shipping) <= market * 0.75
            total = price + shipping
            if total > market_price * DEAL_RATIO:
                drop["nodeal"] += 1
                print(f"  [NODEAL]  ${total:.2f} vs mkt ${market_price:.2f} — {match_label}")
                continue

            # 9b) Too-good-to-be-true backstop: a listing priced absurdly below the
            #     matched market is almost always a WRONG cross-set/printing match
            #     (e.g. a cheap Celebrations reprint matched to Base Set's price),
            #     which would otherwise fire a false deal AND mis-route to #premium.
            if total < market_price * SINGLE_SANITY_FLOOR:
                drop["sanity"] += 1
                print(f"  [SANITY]  ${total:.2f} vs mkt ${market_price:.2f} "
                      f"(<{SINGLE_SANITY_FLOOR:.0%}) wrong match — {match_label}")
                continue

            # 10) Route: graded slab or market >= $100 → #premium, else → #budget.
            channel, webhook_url = determine_channel(
                game, title, sealed=False, market_price=market_price
            )
            if not webhook_is_set(webhook_url):
                drop["noslot"] += 1
                print(f"  [SKIP] empty slot #{game}/{channel} — {match_label}")
                continue

            disc = (market_price - total) / market_price * 100
            print(f"  [DEAL] ${total:.2f} vs mkt ${market_price:.2f} ({disc:.0f}% off) → #{game}/{channel} — {match_label}")
            sent = send_discord_alert(
                title, item["url"], price, shipping, market_price,
                webhook_url, channel, game,
                store=item.get("store", "eBay"),
                condition=item["condition"],
                language=item["language"],
                image_url=item["image_url"],
                matched_name=match_label,
            )
            # Only mark seen on confirmed delivery so a transient webhook failure
            # is retried next cycle instead of silently losing the deal forever.
            if sent:
                mark_seen(item_id, seen)
                deals += 1
        except Exception as e:
            drop["error"] += 1
            print(f"  [ERROR] listing {item.get('item_id', '?')} skipped — {type(e).__name__}: {e}")
            continue

    print(
        f"[FUNNEL] {len(listings)} eval | "
        f"floor {drop['floor']} | language {drop['language']} | "
        f"untrusted {drop['untrusted']} | unofficial {drop['unofficial']} | "
        f"lot {drop['lot']} | nopricing {drop['nopricing']} | "
        f"noparse {drop['noparse']} | seen {drop['seen']} | "
        f"nomatch {drop['nomatch']} | nodeal {drop['nodeal']} | "
        f"sanity {drop['sanity']} | noslot {drop['noslot']} | error {drop['error']} | "
        f"RESTOCK {restock_alerts} | SEALED {sealed_alerts} | DEALS {deals}"
    )
    t_deal = time.monotonic()
    print(f"[TIMING] eBay {t_ebay - t_start:.0f}s | Shopify {t_shopify - t_ebay:.0f}s | "
          f"deal pass {t_deal - t_shopify:.0f}s | total {t_deal - t_start:.0f}s")
    print(f"[SCAN] cycle complete — {deals} deal(s), {sealed_alerts} sealed alert(s), "
          f"{restock_alerts} restock alert(s)")
    save_seen(seen)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Line-buffer stdout so this long-running monitor's logs flush promptly
    # instead of appearing in delayed block-buffered chunks.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    missing = [k for k in ("EBAY_APP_ID", "EBAY_CERT_ID") if not os.environ.get(k)]
    if missing:
        print(f"[ERROR] Missing required env vars: {', '.join(missing)}")
        raise SystemExit(1)

    # Report the 12-slot webhook matrix: which are filled vs. still placeholders.
    filled = 0
    print(f"[START] Multi-Game Open-Market Dynamic Lookup Engine")
    print(f"[START] Webhook matrix (game × tier):")
    for game_name in WEBHOOKS:
        states = []
        for tier in ("premium", "budget", "sealed"):
            ok = webhook_is_set(WEBHOOKS[game_name].get(tier, ""))
            filled += 1 if ok else 0
            states.append(f"{tier}={'SET' if ok else 'empty'}")
        print(f"[START]   {game_name:9s} {' | '.join(states)}")
    print(f"[START] {filled}/12 webhook slots filled")
    if webhook_is_set(RESTOCK_WEBHOOK):
        print(f"[START] Restock channel: SET → all games' retail restocks post to #restock")
    else:
        print(f"[START] Restock channel: unset → restocks fall back to each game's #sealed")
    if filled == 0:
        print("[WARN] No webhook slots are filled — nothing will be sent until you "
              "add at least one GAME_TIER_WEBHOOK (or DISCORD_WEBHOOK_URL) secret.")
    print(f"[START] Games: {', '.join(GAME_DISPLAY[g] for g in GAME_STREAMS)}")
    print(f"[START] Singles pricing enabled for: {', '.join(GAME_DISPLAY.get(g, g) for g in GAME_STREAMS if g in PRICED_GAMES)}")
    print(f"[START] Pokémon market: English → pokemontcg.io | Japanese → tcgcsv cat 85 (separate markets)")
    print(f"[START] Premium routing: graded slabs + market >= ${PREMIUM_THRESHOLD:.0f} → #premium (grails)")
    print(f"[START] Price floor: ${MIN_PRICE_FLOOR:.2f} | Deal threshold: total <= market × {DEAL_RATIO}")
    print(f"[START] Dedup: permanent per listing ID | Check interval: {CHECK_INTERVAL // 60} min")
    if POST_TO_DISCORD:
        print(f"[START] Discord posting: LIVE — this instance delivers alerts")
    else:
        print(f"[START] Discord posting: DRY-RUN — workspace copy, sends nothing "
              f"(only the Deployment posts; set DISCORD_LIVE=1 to override)")
    print(f"[START] Mercari JP: {len(MERCARI_JP_QUERIES)} sealed-box quer(ies) via official API "
          f"(JPY→USD, priced vs Japanese sealed catalog)")
    if SCRAPFLY_API_KEY:
        print(f"[START] Mercari US: ACTIVE via Scrapfly — {len(MERCARI_US_QUERIES)} quer(ies) "
              f"every {MERCARI_US_SCAN_INTERVAL // 60} min")
    else:
        print(f"[START] Mercari US: idle — set SCRAPFLY_API_KEY to enable the Cloudflare-solving lane")
    _proxy_stores  = [s for s in SHOPIFY_STORES if s.get("proxy")]
    _direct_stores = [s for s in SHOPIFY_STORES if not s.get("proxy")]
    if shopify_proxies() is not None:
        print(f"[START] Shopify retail watch: {len(SHOPIFY_STORES)} store(s) "
              f"({len(_proxy_stores)} via residential proxy: {SHOPIFY_PROXY_MAX_PAGES} pages every "
              f"{SHOPIFY_PROXY_SCAN_INTERVAL // 60} min, bandwidth-saver) — sealed restock + below-market (USD)\n")
    else:
        if RESIDENTIAL_PROXY_URL:
            print(f"[START][WARN] RESIDENTIAL_PROXY_URL is set but is not a valid proxy URL "
                  f"(expected scheme://user:pass@host:port) — the {len(_proxy_stores)} proxied "
                  f"store(s) are skipped this run.")
        print(f"[START] Shopify retail watch: {len(_direct_stores)} store(s) active "
              f"(+{len(_proxy_stores)} proxy-only idle — set RESIDENTIAL_PROXY_URL) — "
              f"sealed restock + below-market (USD)\n")

    seen = load_seen()
    availability = load_availability()

    while True:
        try:
            seen = cleanup_seen(seen)
            availability = cleanup_availability(availability)
            scan_open_market(seen, availability)
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"\n[INFO] Sleeping {CHECK_INTERVAL // 60} min...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
