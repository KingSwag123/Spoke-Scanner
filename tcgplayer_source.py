"""
TCGplayer marketplace — sealed-product deal lane.

TCGplayer's public marketplace search API (mp-search-api.tcgplayer.com) works
without a key or proxy and returns, per sealed product, both the MARKET price
and the LOWEST live listing price incl. shipping — everything needed for a
deal test in one call. Listings are emitted in the common shape and flow
through main.py's normal sealed evaluation: since the tcgcsv price index is
built from the same TCGplayer catalog, product names match essentially 1:1.

Only products whose lowest live listing already undercuts the API's own market
price are emitted (cheap pre-filter — no point pushing thousands of at-market
products through the pipeline). The pipeline then re-verifies against tcgcsv
and applies the usual DEAL_RATIO / sanity-floor / dedup logic.

Swept on an interval (default 30 min), newest sets first — that's where the
supply/demand gaps show up.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import requests

from config import (
    TCGPLAYER_PAGES_PER_GAME,
    TCGPLAYER_PRODUCT_LINES,
    TCGPLAYER_SCAN_INTERVAL,
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126 Safari/537.36")
_SEARCH_URL = "https://mp-search-api.tcgplayer.com/v1/search/request?q=&isList=false"
_PAGE_SIZE = 50          # API rejects anything above 50

_last_scan: float | None = None
_PAGE_PAUSE = 1.5        # polite spacing between API page requests (s)

# productId → best (lowest) all-in price already emitted this process.
# A product only re-emits when it beats its previous best by IMPROVE_RATIO —
# this kills alert storms from cent-level oscillation while still letting a
# genuinely better new listing through. Process-lifetime state: one baseline
# re-emit per product after a restart, which the permanent seen-store then
# dedups unless the price actually changed.
_best_emitted: dict[int, float] = {}
_IMPROVE_RATIO = 0.95    # must be >=5% cheaper than the last alerted price
_BEST_MEMORY_DAYS = 7    # how far back seed_best_emitted() reads alerted prices
_SEEN_ID_RE = re.compile(r"tcgp-(\d+)-(\d+)")


def seed_best_emitted(seen: dict) -> int:
    """Rebuild _best_emitted from the dedup store after a restart.

    Alerted TCGplayer listings are stored as "tcgp-<productId>-<cents>" with the
    time they alerted, so the lowest price alerted for each product in the last
    _BEST_MEMORY_DAYS can be recovered. Without this every restart forgot the
    baseline and re-alerted products at prices already posted. Returns the
    number of products seeded."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_BEST_MEMORY_DAYS)
    for item_id, stamp in seen.items():
        m = _SEEN_ID_RE.fullmatch(item_id)
        if not m:
            continue
        try:
            if datetime.fromisoformat(stamp) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        pid, total = int(m.group(1)), int(m.group(2)) / 100
        best = _best_emitted.get(pid)
        _best_emitted[pid] = total if best is None else min(best, total)
    return len(_best_emitted)


def _search_page(product_line: str, offset: int) -> list[dict]:
    body = {
        "algorithm": "sales_synonym_v2",
        "from": offset, "size": _PAGE_SIZE,
        "filters": {
            "term": {"productLineName": [product_line],
                     "productTypeName": ["Sealed Products"]},
            "range": {}, "match": {},
        },
        "listingSearch": {
            "context": {"cart": {}},
            # condition:Unopened — sealed-product listings are condition-locked
            # to Unopened on TCGplayer, and the explicit filter guarantees the
            # quoted lowest price can never come from a damaged/opened offer.
            "filters": {"term": {"sellerStatus": "Live", "channelId": 0,
                                 "condition": ["Unopened"]},
                        "range": {"quantity": {"gte": 1}},
                        "exclude": {"channelExclusion": 0}},
        },
        "context": {"cart": {}, "shippingCountry": "US"},
        "settings": {"useFuzzySearch": True},
        "sort": {"field": "release-date", "order": "desc"},
    }
    try:
        r = requests.post(_SEARCH_URL, json=body,
                          headers={"User-Agent": _UA}, timeout=30)
        if r.status_code != 200:
            print(f"  [TCGPLAYER][WARN] search '{product_line}' HTTP {r.status_code}")
            return []
        return (r.json().get("results") or [{}])[0].get("results") or []
    except (requests.RequestException, ValueError) as e:
        print(f"  [TCGPLAYER][WARN] search failed: {type(e).__name__}")
        return []


def fetch_tcgplayer_listings() -> list[dict]:
    """Fetch sealed products whose lowest live listing undercuts market."""
    global _last_scan
    now = time.monotonic()
    if _last_scan is not None and (now - _last_scan) < TCGPLAYER_SCAN_INTERVAL:
        return []
    _last_scan = now

    out: list[dict] = []
    for game, product_line in TCGPLAYER_PRODUCT_LINES:
        scanned = kept = 0
        for page in range(TCGPLAYER_PAGES_PER_GAME):
            if page or out:
                time.sleep(_PAGE_PAUSE)
            results = _search_page(product_line, page * _PAGE_SIZE)
            if not results:
                break
            for p in results:
                scanned += 1
                try:
                    pid    = int(p["productId"])
                    market = float(p["marketPrice"] or 0)
                    lowest = float(p["lowestPrice"] or 0)
                    total  = float(p["lowestPriceWithShipping"] or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                name = p.get("productName") or ""
                if not name or market <= 0 or lowest <= 0 or total < lowest:
                    continue
                if total >= market:          # cheap pre-filter; pipeline re-verifies
                    continue
                best = _best_emitted.get(pid)
                if best is not None and total > best * _IMPROVE_RATIO:
                    continue                 # not meaningfully better than last alert
                _best_emitted[pid] = total if best is None else min(best, total)
                kept += 1
                out.append({
                    # price in the id: a NEW lower listing on a previously-seen
                    # product must still be able to alert.
                    "item_id":   f"tcgp-{pid}-{int(round(total * 100))}",
                    "title":     name,
                    "price":     lowest,
                    "shipping":  round(total - lowest, 2),
                    "url":       f"https://www.tcgplayer.com/product/{pid}",
                    "condition": "New/Sealed",
                    "language":  "English",
                    "image_url": f"https://tcgplayer-cdn.tcgplayer.com/product/{pid}_in_400x400.jpg",
                    "store":     "TCGplayer",
                    "source":    "tcgplayer",
                    "sealed":    True,
                    "game_name": game,
                    # The API's own market price: main.py cross-checks the
                    # tcgcsv title-match against it and drops on divergence
                    # (guards against a token-subset match landing on the
                    # wrong SKU).
                    "source_market": market,
                })
        print(f"  [TCGPLAYER] {product_line}: {scanned} sealed products scanned, "
              f"{kept} below-market → pipeline")
    return out
