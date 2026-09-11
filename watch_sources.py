"""Bounded, read-only marketplace adapters for individual watch searches.

These adapters intentionally do not schedule work, cache results, or notify
Discord.  A caller supplies one specific watch query and is responsible for
matching/deduplicating the returned listings.

HTTP request ceilings per call (including authentication) are:

* eBay: 2 (one OAuth token request, when the cached token is stale, plus one
  Browse search page of at most 100 results).
* Mercari US: 4 (one Scrapfly-rendered search and at most three rendered item
  pages to verify shipping and availability).
* TCGplayer: 1 (one targeted public product search with its embedded live-offer
  quote).  The API does not expose a seller offer id in this response, so these
  records are explicitly labelled product offers and use a stable product id.

No adapter falls back to a broad catalogue scan.  Missing shipping, currency,
or availability is a reason to omit a listing, never an excuse to invent it.
"""

from __future__ import annotations

import base64
import html
import os
import re
import time
import unicodedata
from typing import Any
from urllib.parse import quote_plus

import requests

try:
    # This is a parser only; it does not run a search or import the bot.
    from mercari_source import _parse_us_search as _parse_mercari_search
except ImportError:  # Keep this module importable in isolated adapter tests.
    _parse_mercari_search = None


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126 Safari/537.36")
_EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_MERCARI_SCRAPE_URL = "https://api.scrapfly.io/scrape"
_TCG_SEARCH_URL = "https://mp-search-api.tcgplayer.com/v1/search/request"

# Public for coordinators that want to reserve source budgets before calling.
SOURCE_REQUEST_CAPS = {"ebay": 2, "mercari": 4, "tcgplayer": 1}
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


def _result(source: str, status: str, listings: list[dict] | None = None,
            checked: int = 0, message: str = "", requests_used: int = 0) -> dict:
    """Build the fixed adapter response shape."""
    return {
        "source": source,
        "status": status,
        "listings": listings or [],
        "checked": checked,
        "message": message,
        "requests": requests_used,
    }


def _query_text(query: dict) -> str:
    """Return a narrow marketplace query, preferring the normalized item name."""
    name = str(query.get("normalized_name") or query.get("item_name") or "").strip()
    if not name:
        return ""
    # Set code is often more precise than a set name.  Rarity is included in
    # the marketplace search but is never copied into output metadata.
    extras = [
        str(query[key]).strip()
        for key in ("set_code", "set_name", "rarity")
        if query.get(key) and str(query[key]).strip().lower() not in name.lower()
    ]
    return " ".join([name, *extras])


def _max_price(query: dict) -> float | None:
    value = query.get("max_price")
    if value in (None, ""):
        return None
    try:
        maximum = float(value)
    except (TypeError, ValueError):
        return None
    return maximum if maximum >= 0 else None


def _money(value: Any, currency: Any = "USD") -> float | None:
    """Parse a published USD money value, rejecting unsupported currencies."""
    if str(currency or "").upper() != "USD":
        return None
    try:
        amount = float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return round(amount, 2) if amount >= 0 else None


def _actual_metadata(raw: dict) -> dict:
    """Copy only marketplace-supplied game/set/rarity fields, if present."""
    aliases = {
        "game": ("game", "gameName", "productLineName", "productLine"),
        "set_name": ("set_name", "setName", "set", "groupName"),
        "set_code": ("set_code", "setCode", "setNumber", "number"),
        "rarity": ("rarity", "rarityName"),
    }
    out: dict[str, str] = {}

    def find(node: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return value
            for nested_key in ("extendedData", "product", "attributes", "details"):
                value = node.get(nested_key)
                found = find(value, keys)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = find(child, keys)
                if found is not None:
                    return found
        return None

    for output_key, keys in aliases.items():
        value = find(raw, keys)
        if value is not None:
            out[output_key] = str(value)
    return out


def _normalized_game_name(value: Any) -> str | None:
    """Map a marketplace-provided game label to the coordinator's game key."""
    ascii_value = unicodedata.normalize("NFKD", str(value or "")).encode(
        "ascii", "ignore"
    ).decode()
    text = re.sub(r"[^a-z0-9]+", "", ascii_value.lower())
    aliases = {
        "pokemon": "pokemon",
        "pokemontcg": "pokemon",
        "pokemontradingcardgame": "pokemon",
        "magic": "mtg",
        "magicthegathering": "mtg",
        "magicgathering": "mtg",
        "magicthegatheringtcg": "mtg",
        "mtg": "mtg",
        "disneylorcana": "lorcana",
        "disneylorcanatcg": "lorcana",
        "lorcana": "lorcana",
        "onepiece": "onepiece",
        "onepiececardgame": "onepiece",
        "yugioh": "yugioh",
        "yugiohtcg": "yugioh",
    }
    return aliases.get(text)


def _game_from_title(title: Any) -> str | None:
    """Infer only from distinctive marketplace title evidence, never a watch filter."""
    text = str(title or "").lower()
    signals = (
        ("pokemon", ("pokemon", "pokémon", "pikachu", "charizard")),
        ("mtg", ("magic: the gathering", "magic the gathering", " mtg ", "planeswalker")),
        ("lorcana", ("lorcana",)),
        ("onepiece", ("one piece card game", "one piece tcg")),
        ("yugioh", ("yu-gi-oh", "yugioh", "yu gi oh", "exodia", "dark magician")),
    )
    padded = f" {text} "
    for game_name, terms in signals:
        if any(term in padded for term in terms):
            return game_name
    return None


def _stable_id(value: Any) -> str:
    """Render numeric API ids without a JSON-decoder-introduced `.0` suffix."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _ebay_aspect_metadata(raw: dict) -> dict:
    """Translate eBay's named aspects without treating query filters as facts."""
    metadata = _actual_metadata(raw)
    wanted = {
        "game": {"game"},
        "set_name": {"set", "set name", "card set"},
        "set_code": {"set code", "card number", "card number/name"},
        "rarity": {"rarity"},
    }
    aspects = raw.get("localizedAspects") or raw.get("aspects") or []
    if isinstance(aspects, dict):
        aspects = [{"name": key, "value": value} for key, value in aspects.items()]
    for aspect in aspects:
        if not isinstance(aspect, dict):
            continue
        name = str(aspect.get("name") or aspect.get("localizedName") or "").lower()
        value = aspect.get("value") or aspect.get("localizedValue")
        if isinstance(value, list):
            value = value[0] if value else None
        if not value:
            continue
        for key, labels in wanted.items():
            if name in labels:
                metadata[key] = str(value)
    return metadata


def _get_ebay_token() -> tuple[str | None, str | None, int]:
    """Return (token, diagnostic, HTTP calls); never include credentials in text."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"], None, 0
    app_id = os.environ.get("EBAY_APP_ID", "").strip()
    cert_id = os.environ.get("EBAY_CERT_ID", "").strip()
    if not app_id or not cert_id:
        return None, "eBay credentials are not configured", 0
    credential = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    try:
        response = requests.post(
            _EBAY_TOKEN_URL,
            headers={"Authorization": f"Basic {credential}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload["access_token"]
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + float(payload.get("expires_in", 7200))
        return token, None, 1
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None, "eBay authentication request failed", 1


def _search_ebay(query: dict) -> dict:
    text = _query_text(query)
    if not text:
        return _result("ebay", "error", message="item_name or normalized_name is required")
    token, error, used = _get_ebay_token()
    if token is None:
        return _result("ebay", "unavailable" if used == 0 else "error",
                       message=error or "eBay authentication unavailable", requests_used=used)
    try:
        response = requests.get(
            _EBAY_BROWSE_URL,
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            params={"q": text, "limit": "100", "sort": "newlyListed",
                    "filter": "buyingOptions:{FIXED_PRICE}"},
            timeout=20,
        )
        response.raise_for_status()
        raw_items = response.json().get("itemSummaries") or []
    except (requests.RequestException, ValueError, AttributeError):
        return _result("ebay", "error", message="eBay Browse search failed",
                       requests_used=used + 1)

    maximum = _max_price(query)
    listings: list[dict] = []
    skipped_shipping = skipped_unavailable = skipped_currency = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id, title, url = raw.get("itemId"), raw.get("title"), raw.get("itemWebUrl")
        price_data = raw.get("price") or {}
        price = _money(price_data.get("value"), price_data.get("currency"))
        options = raw.get("shippingOptions") or []
        shipping_values = [
            _money((option.get("shippingCost") or {}).get("value"),
                   (option.get("shippingCost") or {}).get("currency"))
            for option in options if isinstance(option, dict) and option.get("shippingCost") is not None
        ]
        shipping_values = [value for value in shipping_values if value is not None]
        if not shipping_values:
            skipped_shipping += 1
            continue
        if price is None:
            skipped_currency += 1
            continue
        if "FIXED_PRICE" not in (raw.get("buyingOptions") or []) or not item_id or not title or not url:
            skipped_unavailable += 1
            continue
        shipping = max(shipping_values)  # never understate a published option range
        if maximum is not None and price + shipping > maximum:
            continue
        listing = {
            "id": f"ebay:{item_id}",
            "item_id": str(item_id),
            "title": str(title),
            "price": price,
            "shipping": shipping,
            "total_price": round(price + shipping, 2),
            "currency": "USD",
            "url": str(url),
            "condition": raw.get("condition"),
            "availability": "available",
            "source": "ebay",
            "listing_identity_type": "marketplace_listing",
        }
        listing.update(_ebay_aspect_metadata(raw))
        game_name = _normalized_game_name(listing.get("game")) or _game_from_title(title)
        if game_name:
            listing["game_name"] = game_name
        listings.append(listing)
    skipped = skipped_shipping + skipped_unavailable + skipped_currency
    status = "partial" if skipped else "ok"
    message = (f"inspected {len(raw_items)} results; skipped {skipped_shipping} with unknown shipping, "
               f"{skipped_unavailable} unavailable/incomplete, {skipped_currency} non-USD/invalid price")
    return _result("ebay", status, listings, len(raw_items), message, used + 1)


def _mercari_detail(
    html_text: str, expected_item_id: str | None = None,
) -> tuple[bool, float | None, float | None]:
    """Return availability, shipping, and item price from Mercari's main panel.

    Detail pages may contain recommended listings.  Production calls supply the
    expected id and inspect only the document's ``main`` region, ensuring a
    recommendation's "sold", "Buy now", or "Free shipping" text cannot validate
    the requested search result.  The no-id mode exists solely for compact parser
    fixtures.
    """
    text = html.unescape(html_text)
    if expected_item_id is not None:
        main = re.search(r"<main\b[^>]*>(.*?)</main>", text, re.IGNORECASE | re.DOTALL)
        if not main:
            return False, None, None
        text = main.group(1)
        # Do not let a carousel lower in the main landmark validate this item.
        text = re.split(r"(?:recommended\s+(?:for\s+you|items)|you\s+may\s+also\s+like|more\s+from)",
                        text, maxsplit=1, flags=re.IGNORECASE)[0]
    lowered = text.lower()
    sold = ("this item is sold" in lowered or '"status":"sold"' in lowered
            or '"itemstatus":"sold"' in lowered or "sold out" in lowered)
    # A detail page must positively show that it can be purchased; a search-card
    # appearance alone is not sufficient because Mercari listings sell quickly.
    available = not sold and bool(re.search(
        r"(?:buy\s+now|add\s+to\s+cart|available\s+for\s+purchase|schema\.org/instock)",
        text, re.IGNORECASE))
    if not available:
        return False, None, None
    # The search-card price is deliberately not trusted for an alert.  Require
    # an explicit USD price belonging to the primary item region on the detail
    # page, before it is paired with shipping.
    price_match = (
        re.search(r'itemprop=["\']price["\'][^>]*\bcontent=["\']([\d,]+(?:\.\d{1,2})?)',
                  text, re.IGNORECASE)
        or re.search(r'(?:item\s+)?price\D{0,80}\$\s*([\d,]+(?:\.\d{1,2})?)',
                     text, re.IGNORECASE)
    )
    item_price = _money(price_match.group(1)) if price_match else None
    if re.search(r"\bfree\s+shipping\b|\bshipping\s*:\s*free\b", text, re.IGNORECASE):
        return True, 0.0, item_price
    shipping = re.search(
        r"(?:shipping(?:\s*(?:&amp;|&)?\s*delivery)?|delivery)\D{0,120}\$\s*([\d,]+(?:\.\d{1,2})?)",
        text, re.IGNORECASE,
    )
    return True, _money(shipping.group(1)) if shipping else None, item_price


def _scrapfly_html(url: str, key: str) -> str | None:
    """One authenticated rendered-page read.  Do not expose provider responses."""
    try:
        response = requests.get(
            _MERCARI_SCRAPE_URL,
            params={
                "key": key, "url": url, "asp": "true", "render_js": "true", "country": "us",
                # Current Mercari pages expose their result anchors only after
                # rendering settles; this was confirmed by the bounded live probe.
                "rendering_wait": "10000",
            },
            timeout=90,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        content = (payload.get("result") or {}).get("content")
        return content if isinstance(content, str) else None
    except (requests.RequestException, ValueError, AttributeError):
        return None


def _parse_mercari_cards(page: str) -> list[dict]:
    """Parse current Mercari anchors when the older data-productid card markup is absent."""
    parsed = _parse_mercari_search(page) if _parse_mercari_search else []
    if parsed:
        return parsed
    # Mercari's current rendered markup can expose result URLs as anchors without
    # the former data-productid attribute.  Keep parsing local to each anchor so
    # a price/title from a neighbouring result cannot be joined to this id.
    found: dict[str, dict] = {}
    anchor_re = re.compile(
        r'<a\b[^>]*\bhref="(?:https://www\.mercari\.com)?/us/item/(m\d{8,})[^"]*"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_re.finditer(page):
        item_id, card = match.groups()
        title = re.search(r'<img[^>]*\balt="([^"]+)"', card, re.IGNORECASE)
        price = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)', card)
        if not title or not price:
            continue
        image = re.search(r'<img[^>]*\bsrc(?:set)?="(https?://[^"\s,]+)', card, re.IGNORECASE)
        found.setdefault(item_id, {
            "id": item_id,
            "name": html.unescape(title.group(1)),
            "price": price.group(1).replace(",", ""),
            "price_is_dollars": True,
            "photos": [{"thumbnail": image.group(1)}] if image else [],
        })
    return list(found.values())


def _search_mercari(query: dict) -> dict:
    text = _query_text(query)
    if not text:
        return _result("mercari", "error", message="item_name or normalized_name is required")
    key = os.environ.get("SCRAPFLY_API_KEY", "").strip()
    if not key:
        return _result("mercari", "unavailable", message="Mercari Scrapfly credential is not configured")
    search_url = ("https://www.mercari.com/search/?keyword=" + quote_plus(text)
                  + "&sortBy=2&itemStatuses=1")
    page = _scrapfly_html(search_url, key)
    if page is None:
        return _result("mercari", "error", message="Mercari search request failed", requests_used=1)
    raw_items = _parse_mercari_cards(page)
    if not raw_items:
        explicit_empty = bool(re.search(r"\b(?:no results|0 results|nothing found)\b", page, re.I))
        return _result(
            "mercari", "ok" if explicit_empty else "partial", checked=0, requests_used=1,
            message="Mercari returned no matching listings" if explicit_empty
            else "Mercari page had no recognizable listing cards; no listings were trusted",
        )

    maximum = _max_price(query)
    candidates: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        price = _money(item.get("price"))
        if price is None or (maximum is not None and price > maximum):
            continue
        candidates.append(item)
        if len(candidates) == 3:
            break
    listings: list[dict] = []
    skipped_shipping = skipped_unavailable = skipped_price = 0
    requests_used = 1
    for item in candidates:
        item_id = str(item["id"])
        detail = _scrapfly_html(f"https://www.mercari.com/us/item/{item_id}/", key)
        requests_used += 1
        if detail is None:
            skipped_unavailable += 1
            continue
        available, shipping, direct_price = _mercari_detail(detail, item_id)
        if not available:
            skipped_unavailable += 1
            continue
        if shipping is None:
            skipped_shipping += 1
            continue
        if direct_price is None:
            skipped_price += 1
            continue
        if maximum is not None and direct_price + shipping > maximum:
            continue
        photos = item.get("photos") or []
        image = photos[0].get("thumbnail", "") if photos and isinstance(photos[0], dict) else ""
        listing = {
            "id": f"mercari:{item_id}",
            "item_id": item_id,
            "title": str(item["name"]),
            "price": direct_price,
            "shipping": shipping,
            "total_price": round(direct_price + shipping, 2),
            "currency": "USD",
            "url": f"https://www.mercari.com/us/item/{item_id}/",
            "condition": (item.get("itemCondition") or {}).get("name")
                         if isinstance(item.get("itemCondition"), dict) else None,
            "availability": "available",
            "image_url": image,
            "source": "mercari",
            "listing_identity_type": "marketplace_listing",
        }
        # Usually absent from rendered Mercari search cards, but retain it when
        # their page supplies structured product attributes.
        listing.update(_actual_metadata(item))
        game_name = _normalized_game_name(listing.get("game")) or _game_from_title(item.get("name"))
        if game_name:
            listing["game_name"] = game_name
        listings.append(listing)
    skipped = skipped_shipping + skipped_unavailable + skipped_price
    status = "partial" if skipped or len(candidates) < len(raw_items) else "ok"
    message = (f"inspected {len(raw_items)} search cards and {len(candidates)} item pages; "
               f"skipped {skipped_shipping} with unknown shipping, {skipped_price} with unverified "
               f"direct-item price, {skipped_unavailable} unavailable/detail failures")
    return _result("mercari", status, listings, len(raw_items), message, requests_used)


def _tcg_results(payload: dict) -> list[dict]:
    """Extract product rows from the documented, but slightly nested, response."""
    results = payload.get("results") or []
    if results and isinstance(results[0], dict) and isinstance(results[0].get("results"), list):
        results = results[0]["results"]
    return results if isinstance(results, list) else []


def _quote_identity_suffix(total: float, condition: Any) -> str:
    """Stable quote identity: a changed all-in price/condition can alert once."""
    condition_key = re.sub(r"[^a-z0-9]+", "-", str(condition or "").casefold()).strip("-")
    return f"{int(round(total * 100))}-{condition_key or 'live-marketplace-offer'}"


def _search_tcgplayer(query: dict) -> dict:
    text = _query_text(query)
    if not text:
        return _result("tcgplayer", "error", message="item_name or normalized_name is required")
    game = str(query.get("game") or "").lower()
    product_lines = {
        "pokemon": "pokemon", "mtg": "magic", "magic": "magic",
        "lorcana": "disney lorcana", "onepiece": "one piece card game",
        "one piece": "one piece card game", "yugioh": "yugioh", "yu-gi-oh": "yugioh",
    }
    term: dict[str, list[str]] = {}
    if game in product_lines:
        term["productLineName"] = [product_lines[game]]
    # Deliberately no productTypeName restriction: target watches may be singles
    # or sealed.  listingSearch supplies the lowest currently live, in-stock offer.
    body = {
        "algorithm": "sales_synonym_v2", "from": 0, "size": 10,
        "filters": {"term": term, "range": {}, "match": {}},
        "listingSearch": {
            "context": {"cart": {}},
            "filters": {"term": {"sellerStatus": "Live", "channelId": 0},
                        "range": {"quantity": {"gte": 1}},
                        "exclude": {"channelExclusion": 0}},
        },
        "context": {"cart": {}, "shippingCountry": "US"},
        "settings": {"useFuzzySearch": True},
    }
    try:
        response = requests.post(f"{_TCG_SEARCH_URL}?q={quote_plus(text)}&isList=false", json=body,
                                 headers={"User-Agent": _UA}, timeout=30)
        response.raise_for_status()
        raw_items = _tcg_results(response.json())
    except (requests.RequestException, ValueError, AttributeError):
        return _result("tcgplayer", "error", message="TCGplayer product search failed", requests_used=1)

    maximum = _max_price(query)
    listings: list[dict] = []
    skipped_price = skipped_currency = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        product_id = raw.get("productId")
        title = raw.get("productName")
        # This endpoint's US shipping context quotes USD.  If a response does
        # explicitly identify another currency, reject it rather than convert.
        explicit_currency = raw.get("currency") or raw.get("currencyCode") or "USD"
        lowest = _money(raw.get("lowestPrice"), explicit_currency)
        total = _money(raw.get("lowestPriceWithShipping"), explicit_currency)
        if str(explicit_currency).upper() != "USD":
            skipped_currency += 1
            continue
        if not product_id or not title or lowest is None or total is None or total < lowest:
            skipped_price += 1
            continue
        if maximum is not None and total > maximum:
            continue
        stable_product_id = _stable_id(product_id)
        condition = raw.get("condition") or "Live marketplace offer"
        quote_suffix = _quote_identity_suffix(total, condition)
        listing = {
            # Product ids are not individual seller offer ids.  Include the
            # stable all-in quote/condition signature so a genuinely changed
            # live quote is eligible for a new alert without random IDs.
            "id": f"tcgplayer:product-offer:{stable_product_id}:{quote_suffix}",
            "item_id": f"tcgplayer-product-offer-{stable_product_id}-{quote_suffix}",
            "title": str(title),
            "price": lowest,
            "shipping": round(total - lowest, 2),
            "total_price": total,
            "currency": "USD",
            "url": f"https://www.tcgplayer.com/product/{product_id}",
            "condition": condition,
            "availability": "available",
            "source": "tcgplayer",
            "listing_identity_type": "product_offer_aggregate",
            "offer_identity_note": "Live in-stock offer quote; TCGplayer did not expose a seller offer id",
        }
        market = _money(raw.get("marketPrice"), explicit_currency)
        if market is not None:
            listing["source_market"] = market
        listing.update(_actual_metadata(raw))
        # Do not use the requested game as proof: only normalize a game label
        # that the TCGplayer response itself supplied.
        game_name = _normalized_game_name(listing.get("game"))
        if game_name:
            listing["game_name"] = game_name
        listings.append(listing)
    skipped = skipped_price + skipped_currency
    status = "partial" if skipped else "ok"
    message = (f"inspected {len(raw_items)} targeted products; skipped {skipped_price} without "
               f"a verifiable live all-in offer, {skipped_currency} non-USD")
    return _result("tcgplayer", status, listings, len(raw_items), message, 1)


def search_watch_source(source: str, query: dict) -> dict:
    """Synchronously search one marketplace for one target watch.

    `checked` is the number of returned search records inspected, not a count
    advertised by a marketplace.  `partial` means some returned records could
    not be safely verified; a successful true empty search is `ok`.
    """
    normalized = str(source or "").strip().lower()
    if not isinstance(query, dict):
        return _result(normalized or str(source), "error", message="query must be a dictionary")
    dispatch = {
        "ebay": _search_ebay,
        "mercari": _search_mercari,
        "tcgplayer": _search_tcgplayer,
    }
    handler = dispatch.get(normalized)
    if handler is None:
        return _result(normalized or str(source), "error",
                       message="unsupported source; use ebay, mercari, or tcgplayer")
    return handler(query)


def smoke_check_watch_source(source: str) -> dict:
    """Perform one low-cost, read-only adapter health search for a generic target.

    This helper performs no caching, writes, or notifications.  It is intentionally
    separate from scheduling so a coordinator can choose when its bounded request
    cost is acceptable.
    """
    return search_watch_source(source, {"item_name": "trading card", "max_price": 0})