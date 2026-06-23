# Memory Index

- [TCG Shopify watchlist](shopify-watchlist.md) — which stores are addable, why some 429 forever, verifier gotchas, USD-only deal pipeline.
- [Residential proxy](residential-proxy.md) — proxy=True stores route via RESIDENTIAL_PROXY_URL on a separate throttle stream, skipped when unset/invalid; metered lane uses shallower pages + slower cadence to fit GB budget; MUST redact creds in logs.
- [Duplicate sealed pings](duplicate-pings.md) — duplicates: #1 BIGGEST is dev workflow + Deployment BOTH posting to same webhooks (gate on REPLIT_DEPLOYMENT, dev dry-runs to .local state); also Shopify one-box-many-ids (collapse by store+normalized title, stable min-id rep); eBay multi-seller is legit, never collapse.
