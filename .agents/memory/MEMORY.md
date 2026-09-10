# Memory Index

- [Yu-Gi-Oh catalog ambiguity](yugioh-catalog.md) — set abbreviations repeat across print runs; card codes can repeat across rarities.

- [TCG Shopify watchlist](shopify-watchlist.md) — which stores are addable, why some 429 forever, verifier gotchas, USD-only deal pipeline.
- [Residential proxy](residential-proxy.md) — proxy=True stores route via RESIDENTIAL_PROXY_URL on a separate throttle stream, skipped when unset/invalid; metered lane uses shallower pages + slower cadence to fit GB budget; MUST redact creds in logs.
- [Duplicate sealed pings](duplicate-pings.md) — duplicates: #1 BIGGEST is dev workflow + Deployment BOTH posting to same webhooks (gate on REPLIT_DEPLOYMENT, dev dry-runs to .local state); also Shopify one-box-many-ids (collapse by store+normalized title, stable min-id rep); eBay multi-seller is legit, never collapse.
- [Mercari sources](mercari-sources.md) — US is CF-blocked (Scrapfly only); JP app API works via self-signed DPoP JWT; pokemon_jp pseudo-game prices JP boxes vs tcgcsv cat 85; fail-closed matching.
- [TCGplayer & Yahoo JP lanes](tcgplayer-yahoo-lanes.md) — mp-search API quirks (50/page, Unopened filter), best-price-improvement alert gating, Yahoo free-ship-only anchor parsing.
- [Market-price cache TTL](price-cache-ttl.md) — price cache MUST persist across cycles (TTL); wiping it every cycle caused 429 storms that delayed pings ~1hr. Never cache transient failures (429/5xx/network/bad-JSON) — only genuine no-match. Don't lower CHECK_INTERVAL (eBay quota).
- [Apify connector runtime](apify-connector-runtime.md) — sold comps use the JS connector helper because the generated Python SDK is unavailable; enrichment must stay async, strict, cached, and cost-capped.
