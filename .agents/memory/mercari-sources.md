---
name: Mercari sources
description: How the Mercari JP and US lanes work, what's blocked, and the pokemon_jp pseudo-game pricing trick.
---

# Mercari sources

- **Mercari US is fully Cloudflare/PerimeterX-blocked** for plain HTTP — 403 "Just a moment" on pages AND internal API paths, both direct and via the residential proxy; `api.mercari.com` doesn't resolve. Only a browser-solving scraping API works (we use Scrapfly, gated on `SCRAPFLY_API_KEY`, ~30-min sweeps to bound credit spend). Don't retry plain requests.
- **Self-hosted evasion was tried exhaustively and FAILED (Aug 2026)** — don't repeat: curl_cffi (chrome124 impersonation + residential proxy) gets HTTP 200 on the search HTML, but that page is an empty client-rendered shell (`pageProps: {}`); the data lives behind POST `/v1/api`, which is challenge-gated (needs JS-executed `cf_clearance`) and 403s even with warmed session cookies + DPoP. Item pages and `_next/data` routes also 403/404. Headless nix chromium (stealth patches), headed chromium under xvfb, and patchright persistent-context all sat on "Just a moment" for 60-80s without clearing. Conclusion: only a challenge-solving scraping API works; deps were removed after the experiment to keep the deploy lean.
- **Mercari JP's app API works directly** (no proxy): POST `api.mercari.jp/v2/entities:search` with a per-request self-signed ES256 DPoP JWT (pyjwt + cryptography; headers `DPoP`, `X-Platform: web`). If it starts failing, check whether they tightened DPoP validation before blaming IP blocks.
- **pokemon_jp pseudo-game**: `TCGCSV_CATEGORY["pokemon_jp"]=85` lets `fetch_sealed_price("pokemon_jp", en_title)` price JP sealed boxes against the Japanese catalog with zero api_engines changes. The pseudo-game must never enter channel routing — listings carry `game_name="pokemon"`.
- **JP matching is fail-closed by design**: curated JP→EN set-name map; title must positively signal sealed (シュリンク付/未開封) and not negate it (シュリンクなし/開封済/空箱/…); search is server-filtered to seller-paid shipping (`shippingPayerId: [2]`). Unmapped sets log `[JPNOMAP]`-style samples — extend `_JP_SET_MAP` as new sets release.
- **Why:** review found $0-shipping + keyword-only sealed assumptions could create false pings; every unverifiable attribute now drops the listing instead of alerting.
- FX (JPY→USD) via open.er-api.com, cached 6h, stale-kept on failure; if no rate has EVER been fetched the JP lane skips the cycle (never misprices).
- US lane adds an assumed $8 shipping since search data hides buyer shipping cost — deal test stays conservative.
- Never log Scrapfly error bodies (they can echo the keyed query string) — status code only.

## US lane parsing (Scrapfly)
- Scrapfly (asp + render_js) does get past CF — 200, real page, ~400KB.
- mercari.com's __NEXT_DATA__ script tag carries extra attributes (crossorigin) — match with [^>]*, and the blob contains NO item data (results fetched client-side). Parse the rendered DOM cards instead: data-productid="m…", img alt = title (HTML-escaped, unescape; suffixed " - <brand>"), $X.XX price in dollars, srcset first URL for image. JSON walker kept as primary in case they revert.
- **Health rule:** if a page loads but no listing format is recognized, fail closed and stop that sweep immediately; while unhealthy, spend only one recovery-probe request every 10 minutes, then automatically resume normal full sweeps after a valid parse.
- **Why:** all configured queries share Mercari's page format, so running the remaining searches after one parser failure only wastes metered Scrapfly credits and cannot yield safe alerts.
