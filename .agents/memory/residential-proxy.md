---
name: Residential proxy for Cloudflare-strict stores
description: How proxy=True stores are routed/skipped, the separate throttle stream, and the must-redact-credentials-in-logs rule.
---

# Residential proxy support (config.py + shopify_source.py)

**Design:** Stores in SHOPIFY_STORES marked `"proxy": True` are the ~14 that 429 this datacenter IP forever. They route through `RESIDENTIAL_PROXY_URL` (env secret, format `scheme://user:pass@host:port`, a rotating residential gateway). `shopify_proxies()` in config returns `{"http","https"}` or None. Direct stores are untouched.

**Two separate throttle streams** (`_DIRECT_STREAM` / `_PROXY_STREAM` in shopify_source.py, each `{lock, last}`): proxied requests egress a rotating residential IP that does NOT share the datacenter IP's Cloudflare per-source-IP limit, so they run as a parallel stream and never steal request slots from / slow the direct-path stores.
**Why:** the whole point of the global throttle is the shared datacenter source IP; proxied traffic isn't on that IP, so forcing it through the same limiter would needlessly slow the working stores.

**Skip-when-unset:** when `shopify_proxies()` is None, `fetch_all_shopify_listings` drops `proxy=True` stores entirely (logs once) — no dead-weight guaranteed-429 requests. Startup banner shows "N active (+M proxy-only idle)". Proxy-only stores are never fetched direct because they 429 every request from this IP (see shopify-watchlist.md).

# SECURITY RULE — do not regress
Proxy connection/auth errors from requests/urllib3 can embed the FULL proxy URL incl. `user:pass` in their exception string. Any log of a requests exception in the Shopify path MUST go through `_safe_err()` (redacts the configured URL literal + any `scheme://...@` userinfo via regex). **Never** `print(... {e})` raw for a request that may use a proxy.
**Why:** workflow logs are visible to the user/anyone with the repl; leaking the proxy URL would expose a paid credential.
**How to apply:** if you add another proxied request path, route its error logging through `_safe_err` too.
