"""
Mercari sources — a THIRD product source alongside eBay and Shopify.

Two independent lanes:

  • JP lane (fetch_mercari_jp_listings): Mercari Japan's official app API,
    reached with a per-request DPoP-signed token (ES256). Free, no proxy needed.
    Scope is SEALED Japanese Pokémon product only: titles are Japanese, so each
    listing is mapped to its English tcgcsv product name via a curated
    JP→EN set-name table (`_JP_SET_MAP`). Listings whose set can't be mapped are
    dropped (and counted) — pinging an unpriceable box would just be noise.
    JPY prices convert to USD with a cached FX rate; if no rate has EVER been
    fetched the lane fails CLOSED (skipped) rather than mispricing.

  • US lane (fetch_mercari_us_listings): mercari.com blocks plain HTTP (JS
    challenge) from both the datacenter IP and the residential proxy, so pages
    are fetched through the Scrapfly scraping API (anti-bot solving + JS
    rendering). English titles/USD prices flow through the normal eBay-style
    evaluation in main.py. Gated on SCRAPFLY_API_KEY; swept on a slow interval
    because each request spends metered Scrapfly credits.

Both lanes normalize into the common listing dict shape used by main.py's
evaluation loop (item_id/title/price/shipping/url/condition/language/
image_url/store/game_name), plus:
    source    — "mercari_jp" | "mercari_us"
    sealed    — True on JP listings (forces the sealed path; is_sealed() can't
                read Japanese titles)
    en_title  — JP lane only: the mapped English product phrase handed to
                fetch_sealed_price("pokemon_jp", en_title)
"""

import base64
import html as _html
import json
import re
import time
import uuid

import requests

from config import (
    FX_RATE_TTL,
    FX_RATE_URL,
    MERCARI_JP_MIN_PRICE_JPY,
    MERCARI_JP_PAGE_SIZE,
    MERCARI_JP_QUERIES,
    MERCARI_JP_REQUEST_INTERVAL,
    MERCARI_US_ASSUMED_SHIPPING,
    MERCARI_US_QUERIES,
    MERCARI_US_SCAN_INTERVAL,
    SCRAPFLY_API_KEY,
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126 Safari/537.36")


# ---------------------------------------------------------------------------
# JPY→USD conversion — cached, fail-closed
# ---------------------------------------------------------------------------
_fx_rate:  float | None = None   # JPY per USD
_fx_until: float = 0.0


def _jpy_per_usd() -> float | None:
    """Cached JPY-per-USD rate. Keeps the last good rate on refresh failure;
    returns None only if a rate has NEVER been fetched (lane must then skip)."""
    global _fx_rate, _fx_until
    now = time.monotonic()
    if _fx_rate is not None and now < _fx_until:
        return _fx_rate
    try:
        r = requests.get(FX_RATE_URL, headers={"User-Agent": _UA}, timeout=15)
        rate = float(r.json()["rates"]["JPY"])
        if rate > 0:
            _fx_rate, _fx_until = rate, now + FX_RATE_TTL
            return _fx_rate
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"  [WARN] FX rate refresh failed: {type(e).__name__}")
    if _fx_rate is not None:
        _fx_until = now + 600      # stale rate: retry refresh in 10 min
        return _fx_rate
    _fx_until = now + 600
    return None


# ---------------------------------------------------------------------------
# JP→EN set-name matcher (sealed boxes)
#   Maps the Japanese set name in a Mercari title to the English tcgcsv
#   "Pokemon Japan" product-name phrase. Values must contain every token of the
#   tcgcsv product name (minus filler) — fetch_sealed_price requires the
#   product's tokens to be a subset of the title we hand it.
#   Unmapped sets are logged ([JPNOMAP]) so the table can be extended.
# ---------------------------------------------------------------------------
_JP_SET_MAP = {
    # ---- MEGA era ----
    "ストームエメラルダ":  "storm emeralda",
    "アビスアイ":          "abyss eye",
    "ニンジャスピナー":    "ninja spinner",
    "メガブレイブ":        "mega brave",
    "メガシンフォニア":    "mega symphonia",
    # ---- Scarlet & Violet era ----
    "ロケット団の栄光":    "glory of team rocket",
    "ブラックボルト":      "black bolt",
    "ホワイトフレア":      "white flare",
    "バトルパートナーズ":  "battle partners",
    "熱風のアリーナ":      "heat wave arena",
    "テラスタルフェス":    "terastal festival ex",
    "超電ブレイカー":      "supercharged breaker",
    "楽園ドラゴーナ":      "paradise dragona",
    "ステラミラクル":      "stellar miracle",
    "ナイトワンダラー":    "night wanderer",
    "変幻の仮面":          "mask of change",
    "クリムゾンヘイズ":    "crimson haze",
    "ワイルドフォース":    "wild force",
    "サイバージャッジ":    "cyber judge",
    "シャイニートレジャー": "shiny treasure ex",
    "未来の一閃":          "future flash",
    "古代の咆哮":          "ancient roar",
    "レイジングサーフ":    "raging surf",
    "黒炎の支配者":        "ruler of the black flame",
    "ポケモンカード151":   "pokemon card 151",
    "クレイバースト":      "clay burst",
    "スノーハザード":      "snow hazard",
}

# Product-type detection in the Japanese title. BOX/ボックス → booster box.
# カートン (carton/case) is a bulk container — skip (mirrors _SEALED_BULK_TOKENS).
_JP_BOX_RE    = re.compile(r"(?:box|ｂｏｘ|ボックス|1box|１box)", re.IGNORECASE)
_JP_CARTON_RE = re.compile(r"カートン")

# Sealed status must be POSITIVELY signaled — search keywords alone don't prove
# an individual result is sealed (titles can negate them, sell empty boxes, etc.)
_JP_SEALED_POSITIVE_RE = re.compile(r"(?:シュリンク付|未開封|未サーチ)")
_JP_SEALED_NEGATIVE_RE = re.compile(
    r"(?:シュリンクなし|シュリンク無し|シュリンク剥がし|開封済|開封品|開封後|"
    r"空箱|箱のみ|ボックスのみ|ペリペリ|ジャンク|バラ|サーチ済)"
)


def _jp_match_en_title(title: str) -> str | None:
    """Map a Japanese Mercari title to an English tcgcsv sealed-product phrase.

    Returns e.g. "storm emeralda booster box", or None when the set is unknown,
    the listing isn't a single booster box, or sealed status isn't positively
    signaled in the title (fail-closed: unverifiable ⇒ dropped, never alerted).
    """
    if _JP_CARTON_RE.search(title):
        return None                       # bulk carton — price index would mislead
    if not _JP_BOX_RE.search(title):
        return None                       # only boxes for now (packs are noise)
    if _JP_SEALED_NEGATIVE_RE.search(title):
        return None                       # opened / no-shrink / empty box / junk
    if not _JP_SEALED_POSITIVE_RE.search(title):
        return None                       # sealed not positively claimed — skip
    for jp, en in _JP_SET_MAP.items():
        if jp in title:
            return f"{en} booster box"
    return None


# ---------------------------------------------------------------------------
# JP lane — Mercari Japan app API (DPoP)
# ---------------------------------------------------------------------------
_JP_SEARCH_URL = "https://api.mercari.jp/v2/entities:search"


def _dpop_token() -> str | None:
    """Self-signed ES256 DPoP JWT — what Mercari JP's own web client sends."""
    try:
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as e:
        print(f"  [MERCARI-JP][ERROR] missing dependency: {e}")
        return None
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_numbers()

    def b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

    jwk = {"crv": "P-256", "kty": "EC", "x": b64(pub.x), "y": b64(pub.y)}
    payload = {
        "iat": int(time.time()), "jti": str(uuid.uuid4()),
        "htu": _JP_SEARCH_URL, "htm": "POST", "uuid": str(uuid.uuid4()),
    }
    return pyjwt.encode(payload, key, algorithm="ES256",
                        headers={"typ": "dpop+jwt", "jwk": jwk})


def _jp_search(keyword: str) -> list[dict]:
    """One JP API search, newest first, on-sale only. Returns raw items ([] on failure)."""
    tok = _dpop_token()
    if tok is None:
        return []
    body = {
        "userId": "", "pageSize": MERCARI_JP_PAGE_SIZE, "pageToken": "",
        "searchSessionId": uuid.uuid4().hex,
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED", "thumbnailTypes": [],
        "searchCondition": {
            "keyword": keyword, "excludeKeyword": "",
            "sort": "SORT_CREATED_TIME", "order": "ORDER_DESC",
            "status": ["STATUS_ON_SALE"],
            "sizeId": [], "categoryId": [], "brandId": [], "sellerId": [],
            "priceMin": MERCARI_JP_MIN_PRICE_JPY, "priceMax": 0,
            # 2 = seller pays (送料込み). Server-side filter so buyer-paid/COD
            # listings never reach the deal test with an understated total.
            "itemConditionId": [], "shippingPayerId": [2], "shippingFromArea": [],
            "shippingMethod": [], "colorId": [], "hasCoupon": False,
            "attributes": [], "itemTypes": [], "skuIds": [],
        },
        "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
        "serviceFrom": "suruga",
    }
    headers = {"Content-Type": "application/json", "DPoP": tok,
               "X-Platform": "web", "User-Agent": _UA}
    try:
        r = requests.post(_JP_SEARCH_URL, json=body, headers=headers, timeout=25)
        if r.status_code != 200:
            print(f"  [MERCARI-JP][WARN] search '{keyword[:20]}…' HTTP {r.status_code}")
            return []
        return r.json().get("items", []) or []
    except (requests.RequestException, ValueError) as e:
        print(f"  [MERCARI-JP][WARN] search failed: {type(e).__name__}")
        return []


def fetch_mercari_jp_listings() -> list[dict]:
    """Fetch + normalize sealed JP Pokémon boxes from Mercari Japan.

    Only listings whose set maps to a tcgcsv product survive (the rest are
    unpriceable). Prices arrive in USD; `en_title` carries the phrase for
    fetch_sealed_price("pokemon_jp", …).
    """
    if not MERCARI_JP_QUERIES:
        return []
    rate = _jpy_per_usd()
    if rate is None:
        print("  [MERCARI-JP] skipped — no JPY/USD rate available (fail-closed)")
        return []
    out: list[dict] = []
    seen_ids: set[str] = set()
    matched = unmapped = 0
    unmapped_sample: str | None = None
    for i, (game, keyword) in enumerate(MERCARI_JP_QUERIES):
        if i:
            time.sleep(MERCARI_JP_REQUEST_INTERVAL)
        for it in _jp_search(keyword):
            item_id = it.get("id")
            title   = it.get("name") or ""
            try:
                jpy = float(it.get("price"))
            except (TypeError, ValueError):
                continue
            if not item_id or item_id in seen_ids or jpy < MERCARI_JP_MIN_PRICE_JPY:
                continue
            seen_ids.add(item_id)
            en_title = _jp_match_en_title(title)
            if en_title is None:
                unmapped += 1
                if unmapped_sample is None and _JP_BOX_RE.search(title):
                    unmapped_sample = title
                continue
            matched += 1
            thumbs = it.get("thumbnails") or []
            out.append({
                "item_id":   f"mjp-{item_id}",
                "title":     title,
                "price":     round(jpy / rate, 2),
                "shipping":  0.0,          # JP listings are overwhelmingly 送料込み (shipping incl.)
                "url":       f"https://jp.mercari.com/item/{item_id}",
                "condition": "New/Unopened (JP)",
                "language":  "Japanese",
                "image_url": thumbs[0] if thumbs else "",
                "store":     "Mercari JP",
                "source":    "mercari_jp",
                "sealed":    True,
                "en_title":  en_title,
                "game_name": game,
            })
    msg = f"  [MERCARI-JP] {matched} mapped sealed box(es), {unmapped} unmapped/skipped"
    if unmapped_sample:
        msg += f" (sample unmapped: {unmapped_sample[:40]})"
    print(msg)
    return out


# ---------------------------------------------------------------------------
# US lane — mercari.com via Scrapfly (anti-bot + JS rendering)
# ---------------------------------------------------------------------------
_last_us_scan: float | None = None
_us_parser_healthy = True
_us_consecutive_failures = 0
_US_UNHEALTHY_RETRY_INTERVAL = 600  # low-cost recovery probe every 10 minutes


def _scrapfly_get(url: str) -> str | None:
    """Fetch a URL through Scrapfly with anti-bot solving. Returns HTML or None."""
    try:
        r = requests.get(
            "https://api.scrapfly.io/scrape",
            params={
                "key": SCRAPFLY_API_KEY,
                "url": url,
                "asp": "true",          # anti-scraping protection solving
                "render_js": "true",    # Cloudflare challenge needs a real browser
                "country": "us",
            },
            timeout=90,
        )
        data = r.json()
        if r.status_code != 200:
            # Redacted diagnostics ONLY: this is an authenticated request, and
            # provider error bodies can echo the query string (which carries the
            # key). Log the status code and nothing free-form.
            print(f"  [MERCARI-US][WARN] Scrapfly HTTP {r.status_code}")
            return None
        return (data.get("result") or {}).get("content")
    except (requests.RequestException, ValueError) as e:
        print(f"  [MERCARI-US][WARN] Scrapfly request failed: {type(e).__name__}")
        return None


def _parse_us_search(html: str) -> list[dict]:
    """Extract raw item dicts from a mercari.com search page's __NEXT_DATA__.

    The exact JSON path shifts with frontend releases, so walk the whole blob
    for objects that look like search items (id starting with 'm' + name +
    price). If the blob carries no items (the current frontend fetches results
    client-side), fall back to parsing the rendered product cards in the DOM.
    Defensive by design; returns [] when neither shape is recognized.
    """
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    found: dict[str, dict] = {}
    if m:
        try:
            blob = json.loads(m.group(1))
        except ValueError:
            blob = None
        if blob is not None:
            def walk(node):
                if isinstance(node, dict):
                    iid = node.get("id")
                    if (isinstance(iid, str) and re.fullmatch(r"m\d{8,}", iid)
                            and node.get("name") and node.get("price") is not None):
                        found.setdefault(iid, node)
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            walk(blob)
    if found:
        return list(found.values())
    return _parse_us_search_dom(html)


def _parse_us_search_dom(html: str) -> list[dict]:
    """Fallback: parse rendered product cards (data-productid=…) from the DOM.

    Current mercari.com renders search results client-side, so the data is only
    in the HTML: each card carries data-productid, an <img alt="…"> title, and
    a $-prefixed price. Emits dicts in the same shape the JSON walker yields
    (price in DOLLARS as a string with no 'cents' ambiguity)."""
    items: list[dict] = []
    for m in re.finditer(r'data-productid="(m\d{8,})"', html):
        iid = m.group(1)
        seg = html[m.start():m.start() + 8000]
        nxt = re.search(r'data-productid="m\d{8,}"', seg[20:])
        if nxt:
            seg = seg[:nxt.start() + 20]
        alt = re.search(r'<img[^>]*\balt="([^"]+)"', seg)
        # Anchor to the card's own price element (data-testid/class contains
        # "Price") so we never grab shipping, strikethrough, or promo amounts;
        # fall back to the first $ in the card segment only if that fails.
        price = (re.search(r'Price[^$]{0,400}?\$\s?([\d,]+(?:\.\d\d)?)', seg)
                 or re.search(r'\$\s?([\d,]+(?:\.\d\d)?)', seg))
        if not alt or not price:
            continue
        title = _html.unescape(alt.group(1))
        # img alt is "<listing title> - <brand>"; the trailing brand suffix is
        # harmless for matching, keep as-is.
        img = re.search(r'<img[^>]*\bsrcset="(https://[^\s"]+)', seg)
        items.append({
            "id":     iid,
            "name":   title,
            "price":  price.group(1).replace(",", ""),
            "price_is_dollars": True,      # DOM prices are always dollars — never cents-convert
            "photos": [{"thumbnail": img.group(1)}] if img else [],
        })
    return items


def fetch_mercari_us_listings() -> list[dict]:
    """Fetch + normalize Mercari US listings via Scrapfly.

    Skipped when SCRAPFLY_API_KEY is unset, and swept at most every
    MERCARI_US_SCAN_INTERVAL seconds to bound Scrapfly credit spend.
    English titles/USD prices go through the normal evaluation path in main.py.
    """
    global _last_us_scan, _us_parser_healthy, _us_consecutive_failures
    if not SCRAPFLY_API_KEY:
        return []
    now = time.monotonic()
    interval = (MERCARI_US_SCAN_INTERVAL if _us_parser_healthy
                else _US_UNHEALTHY_RETRY_INTERVAL)
    if _last_us_scan is not None and (now - _last_us_scan) < interval:
        return []
    _last_us_scan = now

    out: list[dict] = []
    seen_ids: set[str] = set()
    # While unhealthy, spend only one metered request as a recovery probe.
    # A successful probe restores normal full sweeps automatically.
    queries = MERCARI_US_QUERIES if _us_parser_healthy else MERCARI_US_QUERIES[:1]
    for game, keyword in queries:
        url = ("https://www.mercari.com/search/?keyword="
               + requests.utils.quote(keyword)
               + "&sortBy=2&itemStatuses=1")          # newest first, on sale
        html = _scrapfly_get(url)
        if not html:
            continue
        items = _parse_us_search(html)
        if not items:
            _us_consecutive_failures += 1
            _us_parser_healthy = False
            print(
                "  [MERCARI-US][HEALTH] FAIL-CLOSED: page loaded but no "
                f"listing format was recognized (failure {_us_consecutive_failures}). "
                "Full sweep paused; one recovery probe will run every 10 min."
            )
            # All searches use the same page format. Stop now rather than
            # spending four more Scrapfly requests that cannot parse.
            break

        if not _us_parser_healthy:
            print(
                f"  [MERCARI-US][HEALTH] RECOVERED: recognized {len(items)} "
                "listings; normal full sweeps resumed."
            )
            _us_parser_healthy = True
            _us_consecutive_failures = 0
        fresh = 0
        for it in items:
            iid = it["id"]
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            try:
                price = float(it["price"])
            except (TypeError, ValueError):
                continue
            # Cents-vs-dollars guard applies ONLY to JSON-sourced values of
            # unknown unit; DOM-parsed prices are explicitly dollars.
            if not it.get("price_is_dollars") and price > 20000:
                price = price / 100.0
            out.append({
                "item_id":   f"mus-{iid}",
                "title":     it.get("name", ""),
                "price":     price,
                # Search data doesn't reliably expose the buyer's shipping cost,
                # so assume a typical charge rather than $0 — conservative: a
                # listing only pings if it clears the deal test WITH shipping.
                "shipping":  MERCARI_US_ASSUMED_SHIPPING,
                "url":       f"https://www.mercari.com/us/item/{iid}/",
                "condition": it.get("itemCondition", {}).get("name", "Not specified")
                             if isinstance(it.get("itemCondition"), dict) else "Not specified",
                "language":  "English",
                "image_url": (it.get("photos") or [{}])[0].get("thumbnail", "")
                             if isinstance((it.get("photos") or [None])[0], dict) else "",
                "store":     "Mercari US",
                "source":    "mercari_us",
                "game_name": game,
            })
            fresh += 1
        print(f"  [MERCARI-US] '{keyword}' → {len(items)} items ({fresh} kept)")
    return out
