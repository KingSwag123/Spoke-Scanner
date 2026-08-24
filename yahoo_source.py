"""
Yahoo! Auctions Japan — a sealed-JP-box lane like Mercari JP.

Search pages load directly (no challenge, no proxy). Only FIXED-PRICE
(Buy-It-Now, `fixed=1`) listings are queried so the compared price is the real
purchase price, never an in-progress auction bid. Each result card carries
`data-auction-id/-title/-price/-isfreeshipping` on one anchor; only
free-shipping listings survive (shipping is otherwise unknowable from the
search page — fail closed, mirrors Mercari JP's seller-pays filter).

Titles are Japanese, so listings reuse Mercari JP's curated set matcher
(`_jp_match_en_title`) and cached FX rate (`_jpy_per_usd`); unmapped or
not-positively-sealed listings are dropped, never alerted. Normalized items
carry source="yahoo_jp" and are priced against tcgcsv cat 85 in main.py.
"""

import html as _html
import re
import time

import requests

from config import (
    MERCARI_JP_MIN_PRICE_JPY,
    YAHOO_JP_QUERIES,
    YAHOO_JP_REQUEST_INTERVAL,
    YAHOO_JP_SCAN_INTERVAL,
)
from mercari_source import _jp_match_en_title, _jpy_per_usd

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126 Safari/537.36")

_last_scan: float | None = None

# One product anchor carries every field we need as data attributes; pull them
# per-attribute (scoped to the anchor tag) so attribute order can't break us.
_ANCHOR_RE = re.compile(r'<a\s[^>]*class="[^"]*Product__imageLink[^"]*"[^>]*>')
_ATTR_RE = {
    "id":    re.compile(r'data-auction-id="(\w+)"'),
    "title": re.compile(r'data-auction-title="([^"]+)"'),
    "img":   re.compile(r'data-auction-img="([^"]*)"'),
    "price": re.compile(r'data-auction-price="(\d+)"'),
    "free":  re.compile(r'data-auction-isfreeshipping="([^"]*)"'),
}


def _parse_cards(page: str) -> list[dict]:
    cards = []
    for m in _ANCHOR_RE.finditer(page):
        tag = m.group(0)
        vals = {}
        for k, rx in _ATTR_RE.items():
            a = rx.search(tag)
            vals[k] = a.group(1) if a else None
        if vals["id"] and vals["title"] and vals["price"]:
            cards.append(vals)
    return cards


def _search(keyword: str) -> list[dict]:
    url = ("https://auctions.yahoo.co.jp/search/search?p="
           + requests.utils.quote(keyword)
           + "&va=1&fixed=1&s1=new&o1=d&n=50")   # fixed-price only, newest first
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        if r.status_code != 200:
            print(f"  [YAHOO-JP][WARN] search HTTP {r.status_code}")
            return []
        return _parse_cards(r.text)
    except requests.RequestException as e:
        print(f"  [YAHOO-JP][WARN] search failed: {type(e).__name__}")
        return []


def fetch_yahoo_jp_listings() -> list[dict]:
    """Fetch + normalize sealed JP Pokémon boxes from Yahoo! Auctions (BIN only)."""
    global _last_scan
    if not YAHOO_JP_QUERIES:
        return []
    now = time.monotonic()
    if _last_scan is not None and (now - _last_scan) < YAHOO_JP_SCAN_INTERVAL:
        return []
    rate = _jpy_per_usd()
    if rate is None:
        print("  [YAHOO-JP] skipped — no JPY/USD rate available (fail-closed)")
        return []
    _last_scan = now

    out: list[dict] = []
    seen_ids: set[str] = set()
    matched = skipped = 0
    for i, (game, keyword) in enumerate(YAHOO_JP_QUERIES):
        if i:
            time.sleep(YAHOO_JP_REQUEST_INTERVAL)
        for c in _search(keyword):
            iid = c["id"]
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            title = _html.unescape(c["title"])
            jpy = float(c["price"])
            if jpy < MERCARI_JP_MIN_PRICE_JPY:
                continue
            # Shipping unknowable from search results — free-shipping only.
            if c.get("free") != "1":
                skipped += 1
                continue
            en_title = _jp_match_en_title(title)
            if en_title is None:
                skipped += 1
                continue
            matched += 1
            out.append({
                "item_id":   f"yja-{iid}",
                "title":     title,
                "price":     round(jpy / rate, 2),
                "shipping":  0.0,           # free-shipping listings only (filtered above)
                "url":       f"https://auctions.yahoo.co.jp/jp/auction/{iid}",
                "condition": "New/Unopened (JP)",
                "language":  "Japanese",
                "image_url": _html.unescape(c.get("img") or ""),
                "store":     "Yahoo! Auctions JP",
                "source":    "yahoo_jp",
                "sealed":    True,
                "en_title":  en_title,
                "game_name": game,
            })
    print(f"  [YAHOO-JP] {matched} mapped sealed box(es), {skipped} unmapped/skipped")
    return out
