---
name: Market-price cache TTL (latency)
description: Why the TCG price cache persists across scan cycles, and the transient-vs-genuine caching rule
---

# Cross-cycle market-price cache

The TCG market-price cache (`_price_cache` in `api_engines.py`) **persists across scan
cycles with a TTL**; `reset_cycle_cache()` only evicts EXPIRED entries — it must NOT clear
the whole cache.

**Why:** the old behavior wiped the cache every cycle, so each ~5-min cycle re-priced
hundreds of the same cards against the rate-limited free price APIs (pokemontcg.io,
Scryfall, Lorcast). That self-inflicted load triggered repeated 429s + stacked 60s
back-offs, which serialized the deal pass and delayed Discord pings by up to an hour —
long enough that the listing had already sold. Prices barely move minute-to-minute, so
caching them is safe and is the single highest-leverage latency fix.

**How to apply:**
- Successful prices use a longer TTL; genuine no-match (`None`) uses a shorter TTL so a
  card that later gets listed/priced is retried sooner.
- **Never persist a TRANSIENT failure** (HTTP 429/403, network/RequestException, 5xx,
  unparseable body). Caching one would mark a real card unpriceable for the whole TTL.
  The fast path returns sentinels `_RATELIMITED` / `_TRANSIENT` (never `None`) for these;
  `_progressive_price` aborts the card without caching. Only HTTP 404 / empty results /
  ambiguous / no-market are genuine no-matches and may be cached as `None`.
- Do NOT lower `CHECK_INTERVAL` (300s) to chase latency: ~20 eBay streams/cycle is already
  near the Browse API daily quota; cutting the interval just moves the bottleneck to eBay
  429s. Let the cache shrink cycle runtime instead.
- A per-cycle `[TIMING] eBay | Shopify | deal pass | total` log in `main.py` is the way to
  confirm the deal pass shrank after the cache warms (first cycle is always cold).
