---
name: Mercari sources
description: How the Mercari JP and US lanes work, what's blocked, and the pokemon_jp pseudo-game pricing trick.
---

# Mercari sources

- **Mercari US is fully Cloudflare/PerimeterX-blocked** for plain HTTP — 403 "Just a moment" on pages AND internal API paths, both direct and via the residential proxy; `api.mercari.com` doesn't resolve. Only a browser-solving scraping API works (we use Scrapfly, gated on `SCRAPFLY_API_KEY`, ~30-min sweeps to bound credit spend). Don't retry plain requests.
- **Mercari JP's app API works directly** (no proxy): POST `api.mercari.jp/v2/entities:search` with a per-request self-signed ES256 DPoP JWT (pyjwt + cryptography; headers `DPoP`, `X-Platform: web`). If it starts failing, check whether they tightened DPoP validation before blaming IP blocks.
- **pokemon_jp pseudo-game**: `TCGCSV_CATEGORY["pokemon_jp"]=85` lets `fetch_sealed_price("pokemon_jp", en_title)` price JP sealed boxes against the Japanese catalog with zero api_engines changes. The pseudo-game must never enter channel routing — listings carry `game_name="pokemon"`.
- **JP matching is fail-closed by design**: curated JP→EN set-name map; title must positively signal sealed (シュリンク付/未開封) and not negate it (シュリンクなし/開封済/空箱/…); search is server-filtered to seller-paid shipping (`shippingPayerId: [2]`). Unmapped sets log `[JPNOMAP]`-style samples — extend `_JP_SET_MAP` as new sets release.
- **Why:** review found $0-shipping + keyword-only sealed assumptions could create false pings; every unverifiable attribute now drops the listing instead of alerting.
- FX (JPY→USD) via open.er-api.com, cached 6h, stale-kept on failure; if no rate has EVER been fetched the JP lane skips the cycle (never misprices).
- US lane adds an assumed $8 shipping since search data hides buyer shipping cost — deal test stays conservative.
- Never log Scrapfly error bodies (they can echo the keyed query string) — status code only.
