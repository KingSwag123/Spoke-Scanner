"""
Discord routing + delivery.

Decides which game/tier channel a listing belongs to (the WEBHOOKS matrix lives
in `config`) and renders + posts the rich Discord embeds. Routing needs the
graded-slab classifier, which it imports from `api_engines`.
"""

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

import requests

from config import (
    _CHANNEL_COLORS,
    GAME_DISPLAY,
    POST_TO_DISCORD,
    PREMIUM_THRESHOLD,
    WEBHOOK_PLACEHOLDER,
    WEBHOOKS,
    YUGIOH_WEBHOOK,
)
from api_engines import is_graded_slab
from sold_comps import format_sold_comps, get_sold_comps


_COMPS_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="sold-comps",
)
_SOLD_AVG_FIELD = "📈  Recent eBay Avg Sold"
_SOLD_DETAIL_FIELD = "🧾  Recent eBay Sold Comps"


# ---------------------------------------------------------------------------
# Webhook routing matrix helpers
# ---------------------------------------------------------------------------

def webhook_is_set(url: str) -> bool:
    """A slot is usable only if it holds a real URL (not blank / placeholder)."""
    return bool(url) and url.strip() != "" and url.strip() != WEBHOOK_PLACEHOLDER


def determine_channel(
    game_name: str,
    title: str,
    sealed: bool,
    market_price: float | None = None,
) -> tuple[str, str]:
    """
    Resolve (channel_name, webhook_url) for a listing using the spec precedence:
      1) sealed product                       → that game's 'sealed' channel
      2) graded slab OR market >= PREMIUM_THRESHOLD → that game's 'premium' channel
      3) single, market <  PREMIUM_THRESHOLD  → that game's 'budget' channel
    Graded slabs (PSA/BGS/CGC/…) always route to premium regardless of price;
    other singles route by the card's live market price. The URL may be
    empty/placeholder; callers must check webhook_is_set().
    """
    if game_name == "yugioh":
        return "yugioh", YUGIOH_WEBHOOK
    slots = WEBHOOKS.get(game_name, {})
    if sealed:
        channel = "sealed"
    elif is_graded_slab(title) or (market_price is not None and market_price >= PREMIUM_THRESHOLD):
        channel = "premium"
    else:
        channel = "budget"
    return channel, slots.get(channel, "")


# ---------------------------------------------------------------------------
# Discord alert — rich embed (listing price, shipping, market, % discount)
# ---------------------------------------------------------------------------

def _edit_with_sold_comps(
    webhook_url: str,
    message_id: str,
    embed: dict,
    context: dict,
) -> None:
    try:
        comps = get_sold_comps(**context)
    except Exception as exc:
        print(f"  [SOLD-COMPS][WARN] Enrichment failed: {type(exc).__name__}")
        return
    if not comps:
        return
    updated = deepcopy(embed)
    fields = [
        field for field in updated.setdefault("fields", [])
        if field.get("name") not in {_SOLD_AVG_FIELD, _SOLD_DETAIL_FIELD}
    ]
    average_field = {
        "name": _SOLD_AVG_FIELD,
        "value": (
            f"${comps['average']:,.2f}\n"
            f"*{comps['count']} completed sales*"
        ),
        "inline": True,
    }
    tcg_index = next(
        (
            index for index, field in enumerate(fields)
            if field.get("name") == "📊  TCGplayer Market Price"
        ),
        None,
    )
    if tcg_index is not None:
        # This makes the first price row Listing | TCGplayer | eBay average.
        fields.insert(tcg_index + 1, average_field)
    else:
        fields.append(average_field)
    fields.append({
        "name": _SOLD_DETAIL_FIELD,
        "value": format_sold_comps(comps, include_average=False),
        "inline": False,
    })
    updated["fields"] = fields
    parts = urlsplit(webhook_url)
    edit_path = parts.path.rstrip("/") + f"/messages/{message_id}"
    edit_url = urlunsplit((parts.scheme, parts.netloc, edit_path, parts.query, ""))
    try:
        response = requests.patch(edit_url, json={"embeds": [updated]}, timeout=10)
        response.raise_for_status()
        print(f"  [SOLD-COMPS] Added {comps['count']} recent sale(s) to ping")
    except requests.RequestException as exc:
        print(f"  [SOLD-COMPS][WARN] Ping update failed: {type(exc).__name__}")


def _post_embed(
    webhook_url: str,
    embed: dict,
    channel: str,
    game_name: str,
    sold_context: dict | None = None,
) -> bool:
    """POST a single embed; return True only on confirmed 2xx delivery.

    In the workspace this runs in DRY-RUN (POST_TO_DISCORD is False): nothing is
    sent, but we return True so the caller still advances its dedup/seen state
    exactly as production would — keeping the dev log clean across cycles and
    ensuring only ONE running instance (the Deployment) actually posts."""
    if not POST_TO_DISCORD:
        print(f"  [DRY-RUN] would send → #{game_name}/{channel} "
              f"(workspace copy; set DISCORD_LIVE=1 to post for real)")
        return True
    try:
        resp = requests.post(
            webhook_url,
            params={"wait": "true"} if sold_context else None,
            json={"embeds": [embed]},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"  [OK] Discord embed sent → #{game_name}/{channel}")
        if sold_context:
            try:
                message_id = str(resp.json().get("id") or "")
            except (requests.JSONDecodeError, ValueError, TypeError):
                message_id = ""
            if message_id:
                try:
                    _COMPS_EXECUTOR.submit(
                        _edit_with_sold_comps,
                        webhook_url,
                        message_id,
                        embed,
                        sold_context,
                    )
                except RuntimeError:
                    print("  [SOLD-COMPS][WARN] Enrichment worker unavailable")
        return True
    except requests.RequestException as e:
        print(f"  [ERROR] Discord alert failed (#{game_name}/{channel}): {e}")
        return False


def send_discord_alert(
    title: str,
    url: str,
    listing_price: float,
    shipping: float,
    market_price: float,
    webhook_url: str,
    channel: str,
    game_name: str,
    store: str = "eBay",
    condition: str = "Not specified",
    language: str = "Unknown",
    image_url: str = "",
    matched_name: str | None = None,
) -> bool:
    total    = listing_price + shipping
    diff     = market_price - total
    pct      = (diff / market_price * 100) if market_price else 0.0
    color    = _CHANNEL_COLORS.get(channel, 0x00C805)
    ship_str = "free shipping" if shipping <= 0 else f"+ ${shipping:.2f} ship"
    game     = GAME_DISPLAY.get(game_name, game_name.title())

    price_row = [
        {"name": "💰  Listing Price", "value": f"# ${listing_price:.2f}\n*{ship_str}*", "inline": True},
        {"name": "📊  TCGplayer Market Price", "value": f"${market_price:.2f}", "inline": True},
        {"name": "💸  Discount", "value": f"✅  **Save ${diff:.2f}  ({pct:.0f}%)**", "inline": True},
    ]

    divider  = {"name": "\u200b", "value": "\u200b", "inline": False}
    meta_row = [
        {"name": "🎮  Game",      "value": game,      "inline": True},
        {"name": "🏪  Store",     "value": store,      "inline": True},
        {"name": "📦  Condition", "value": condition,  "inline": True},
    ]
    if language and language not in ("Unknown", "English"):
        meta_row.append({"name": "🌐  Language", "value": language, "inline": True})
    if matched_name:
        meta_row.append({"name": "🃏  Matched", "value": matched_name, "inline": True})

    embed = {
        "title":       f"🏷️  {game} Deal — " + ("Graded Slab" if is_graded_slab(title) else "Raw Single"),
        "description": f"### [{title}]({url})",
        "url":         url,
        "color":       color,
        "fields":      price_row + [divider] + meta_row,
        "footer":      {"text": f"#{game_name}/{channel}  •  Open-Market Engine"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    return _post_embed(
        webhook_url,
        embed,
        channel,
        game_name,
        sold_context={
            "identity": matched_name or title,
            "listing_title": title,
            "language": language,
            "sealed": False,
        },
    )


def send_sealed_alert(
    title: str,
    url: str,
    listing_price: float,
    shipping: float,
    market_price: float,
    webhook_url: str,
    game_name: str,
    store: str = "eBay",
    condition: str = "Not specified",
    language: str = "Unknown",
    image_url: str = "",
    matched_name: str | None = None,
    en_title: str | None = None,
    ship_label: str = "ship",
) -> bool:
    """Sealed-product deal alert — same market/discount layout as singles.

    en_title: English translation/mapping of a Japanese listing title. When
    given, it becomes the headline link and the original JP title is shown
    beneath it in italics.

    ship_label: what the `shipping` amount is called under the price. Japanese
    marketplace listings pass an estimated import cost, not a quoted shipping
    charge, and must say so.
    """
    total    = listing_price + shipping
    diff     = market_price - total
    pct      = (diff / market_price * 100) if market_price else 0.0
    color    = _CHANNEL_COLORS.get("sealed", 0x3498DB)
    ship_str = "free shipping" if shipping <= 0 else f"+ ${shipping:.2f} {ship_label}"
    game     = GAME_DISPLAY.get(game_name, game_name.title())

    price_row = [
        {"name": "💰  Listing Price", "value": f"# ${listing_price:.2f}\n*{ship_str}*", "inline": True},
        {"name": "📊  TCGplayer Market Price", "value": f"${market_price:.2f}", "inline": True},
        {"name": "💸  Discount", "value": f"✅  **Save ${diff:.2f}  ({pct:.0f}%)**", "inline": True},
    ]

    divider  = {"name": "\u200b", "value": "\u200b", "inline": False}
    meta_row = [
        {"name": "🎮  Game",      "value": game,      "inline": True},
        {"name": "🏪  Store",     "value": store,      "inline": True},
        {"name": "📦  Condition", "value": condition,  "inline": True},
    ]
    if language and language not in ("Unknown", "English"):
        meta_row.append({"name": "🌐  Language", "value": language, "inline": True})
    if matched_name:
        meta_row.append({"name": "🃏  Matched", "value": matched_name, "inline": True})

    embed = {
        "title":       f"📦  {game} Deal — Sealed Product",
        "description": (f"### [{en_title}]({url})\n*🇯🇵 {title}*"
                        if en_title else f"### [{title}]({url})"),
        "url":         url,
        "color":       color,
        "fields":      price_row + [divider] + meta_row,
        "footer":      {"text": f"#{game_name}/sealed  •  Open-Market Engine"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    return _post_embed(
        webhook_url,
        embed,
        "sealed",
        game_name,
        sold_context={
            "identity": matched_name or en_title or title,
            "listing_title": en_title or title,
            "language": language,
            "sealed": True,
        },
    )


def send_restock_alert(
    title: str,
    url: str,
    price: float,
    currency: str,
    webhook_url: str,
    game_name: str,
    store_name: str,
    language: str = "Unknown",
    image_url: str = "",
    channel: str = "restock",
) -> bool:
    """Sealed-product restock alert — fires when an out-of-stock retail item
    comes back in stock at a Shopify store. Unlike the deal alerts this shows the
    retail price in the store's own currency and makes NO market/discount claim
    (retail restocks aren't necessarily below market — the value is availability).
    Routes to the dedicated #restock channel when configured, else the game's
    #sealed channel (caller passes the resolved `channel`)."""
    color = _CHANNEL_COLORS.get("restock", 0x1ABC9C)
    game  = GAME_DISPLAY.get(game_name, game_name.title())

    meta_row = [
        {"name": "🎮  Game",  "value": game,                      "inline": True},
        {"name": "🏪  Store", "value": store_name or "Shopify",   "inline": True},
        {"name": "💵  Retail Price", "value": f"{price:.2f} {currency}", "inline": True},
    ]
    if language and language not in ("Unknown", "English"):
        meta_row.append({"name": "🌐  Language", "value": language, "inline": True})

    embed = {
        "title":       f"🔄  {game} Restock — Back in Stock",
        "description": f"### [{title}]({url})",
        "url":         url,
        "color":       color,
        "fields":      meta_row,
        "footer":      {"text": f"#{game_name}/{channel}  •  Retail Restock Watch"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    return _post_embed(
        webhook_url,
        embed,
        channel,
        game_name,
        sold_context={
            "identity": title,
            "listing_title": title,
            "language": language,
            "sealed": True,
        },
    )
