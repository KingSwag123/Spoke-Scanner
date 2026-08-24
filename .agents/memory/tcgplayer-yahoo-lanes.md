---
name: TCGplayer & Yahoo JP lanes
description: Non-obvious API/parsing constraints and alerting rules for the TCGplayer and Yahoo Auctions JP deal sources.
---

## TCGplayer (mp-search-api.tcgplayer.com/v1/search/request)
- Public, no key. Max page size 50 — larger → HTTP 400. Pace pages (~1.5s) to avoid throttling.
- listingSearch accepts `condition: ["Unopened"]`; always include it so the quoted lowestPriceWithShipping can never be a damaged/opened offer.
- **Alert dedup rule:** results are aggregate product quotes, not listings. Never embed the price in the dedup ID alone — that re-alerts on every oscillation. Gate emission on beating the previously-alerted best all-in price by ≥5% (per-productId, in-process state), THEN use price-embedding id for the permanent store.
- Items carry `source_market` (API marketPrice); main.py drops the item if the tcgcsv title-match market diverges >±30% (token-subset match can land on the wrong SKU).
- **Why:** architect review flagged alert storms + condition leakage as blocking; these rules fixed both.

## Yahoo Auctions JP
- Search HTML loads direct (no challenge). Fixed-price only: `fixed=1&s1=new&o1=d&n=50`.
- Item data is on each result anchor as `data-auction-id/-title/-price/-isfreeshipping`; anchors span multiple lines — regex must allow that.
- Keep ONLY `isfreeshipping=="1"` (fail-closed on unknown shipping). Reuses mercari_source JP matching + FX; source="yahoo_jp" is in JP_MARKET_SOURCES (main.py) alongside mercari_jp for language bypass + pokemon_jp pricing.
