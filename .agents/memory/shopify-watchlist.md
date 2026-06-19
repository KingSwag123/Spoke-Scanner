---
name: TCG Shopify watchlist
description: How to add Shopify retailers to SHOPIFY_STORES safely — which stores 429 forever, verifier gotchas, USD-only deal pipeline, log-reading gotcha.
---

# Adding stores to SHOPIFY_STORES (config.py)

**Rule:** Only keep a store if it returns data while the scanner is actually running. Verify with a method that reflects sustained load, not a single cold request.

**Why:** A one-off `/products.json` request can return 200 from a Cloudflare-strict store, but those same stores then return 429 on *every* request from this datacenter IP — under the scanner's full cycle and even on a quiet idle re-test (tested at limit=50 and 250). Such stores add only dead weight: 0 variants every cycle plus a wasted failed request, which hurts cycle efficiency for zero benefit.

**How to apply:**
- The ~14 stores that permanently 429 this datacenter IP are now in SHOPIFY_STORES tagged `"proxy": True` — they fetch only when RESIDENTIAL_PROXY_URL is set, otherwise they're skipped entirely. See [residential proxy](residential-proxy.md).
- **Deep-page 429s are NORMAL, not failures:** big stores (5000+ products) often 429 on page 2/3. They are gracefully handled and the store still returns its earlier pages with thousands of variants. Do NOT prune a store just for deep-page 429s — only prune ones that return **0 variants** (429 on page 1).

# Deal pipeline / currency
- main.py only feeds **available USD** variants into the below-market deal pipeline. Non-USD stores (CAD/GBP/AUD/NZD) are **restock-only** (no currency-conversion table). Adding non-USD stores improves restock coverage, not deal coverage.

# Verifier script gotchas (one-off /products.json checkers)
- Run the verifier with the **scanner STOPPED**, else it competes for the IP and triggers 429s.
- Background bash processes do NOT survive across separate tool calls — run the verifier in the foreground, keep it ≤120s.
- `pkill -f verify_stores.py` matches its own shell; use the bracket trick `[v]erify_stores.py`.

# Log-reading gotcha
- `refresh_all_logs` writes timestamped snapshot files into /tmp/logs, so `ls -t /tmp/logs/... | head -1` can grab a stale snapshot instead of the live workflow log. Prefer `refresh_all_logs` for the authoritative current log.
