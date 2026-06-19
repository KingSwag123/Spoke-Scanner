---
name: Residential proxy for Cloudflare-strict stores
description: How proxy=True stores are routed/skipped, the separate throttle stream, and the must-redact-credentials-in-logs rule.
---

# Residential proxy support (config.py + shopify_source.py)

**Design:** Stores in SHOPIFY_STORES marked `"proxy": True` are the ~14 that 429 this datacenter IP forever. They route through `RESIDENTIAL_PROXY_URL` (env secret, format `scheme://user:pass@host:port`, a rotating residential gateway). `shopify_proxies()` in config returns `{"http","https"}` or None. Direct stores are untouched.

**Two separate throttle streams** (`_DIRECT_STREAM` / `_PROXY_STREAM` in shopify_source.py, each `{lock, last}`): proxied requests egress a rotating residential IP that does NOT share the datacenter IP's Cloudflare per-source-IP limit, so they run as a parallel stream and never steal request slots from / slow the direct-path stores.
**Why:** the whole point of the global throttle is the shared datacenter source IP; proxied traffic isn't on that IP, so forcing it through the same limiter would needlessly slow the working stores.

**Skip-when-unset-or-invalid:** `shopify_proxies()` returns None when `RESIDENTIAL_PROXY_URL` is empty OR fails `proxy_url_is_valid()` (must be `scheme://...host`, scheme in http/https/socks5/socks5h/socks4). When None, `fetch_all_shopify_listings` drops `proxy=True` stores entirely (logs once) — no dead-weight guaranteed-429 requests. Startup banner shows "N active (+M proxy-only idle)", plus a `[START][WARN]` line when the URL is set-but-invalid. Proxy-only stores are never fetched direct because they 429 every request from this IP (see shopify-watchlist.md).

**Validation gate — why it exists:** a non-validated proxy URL raises `InvalidURL: Failed to parse` on EVERY proxied request (14×/cycle of noise). The classic bad value is a user pasting the provider's whole `curl -x host:port -U "user:pass" ...` command instead of just `http://user:pass@host:port`. `proxy_url_is_valid()` catches this once at startup so the run degrades to direct-only with one clear message instead of per-store spam.
**How to apply:** never assume the env value is a clean URL; gate any new use of `RESIDENTIAL_PROXY_URL` through `proxy_url_is_valid()` / `shopify_proxies()`.

# Proxy lane is METERED — bandwidth budget drives a separate cadence
Residential proxies bill by **GB transferred**, so the proxy lane is the cost bottleneck, not request rate. Measured `/products.json` page ≈ **85 KB gzipped on the wire** (range ~70–93 KB; gzip is on because `_HTTP_HEADERS` omits Accept-Encoding so `requests` defaults to gzip). At the direct cadence (5 pages every ~5 min, 24/7) ONE proxy store burns ~3 GB/mo — a 5 GB plan would cover only ~1–2 stores.
**Fix (decoupled proxy lane):** proxy stores use `SHOPIFY_PROXY_MAX_PAGES` (2) and a slow sweep gated by `SHOPIFY_PROXY_SCAN_INTERVAL` (1800 s / 30 min) via module-global `_last_proxy_scan` in `fetch_shopify_listings`/`fetch_all_shopify_listings`; direct stores keep `SHOPIFY_MAX_PAGES`/cycle. 2 pages × 85 KB × 48 sweeps/day × 30 × 14 stores ≈ **3.4 GB/mo** → fits 5 GB with ~1.5 GB headroom.
**Why:** deals/restocks on retail Shopify don't need 5-min freshness; trading freshness for depth+cadence is what makes all 14 blocked stores fit the budget.
**How to apply:** to add proxy stores or change the budget, scale via these two constants (monthly GB ≈ pages × 0.085 MB × (1440/interval_min) × 30 × n_stores). Deferring proxy stores is safe: `detect_restocks` only touches variants present this cycle and `cleanup_availability` expires at 30 days, far beyond a 30-min gap. NOTE: `_last_proxy_scan` resets on process restart (monotonic clock) → each restart forces one immediate sweep (~2.4 MB), negligible unless restarts are very frequent.

# SECURITY RULE — do not regress
Proxy connection/auth errors from requests/urllib3 can embed the FULL proxy URL incl. `user:pass` in their exception string. Any log of a requests exception in the Shopify path MUST go through `_safe_err()` (redacts the configured URL literal + any `scheme://...@` userinfo via regex). **Never** `print(... {e})` raw for a request that may use a proxy.
**Why:** workflow logs are visible to the user/anyone with the repl; leaking the proxy URL would expose a paid credential.
**How to apply:** if you add another proxied request path, route its error logging through `_safe_err` too.
