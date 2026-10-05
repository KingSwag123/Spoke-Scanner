---
name: Duplicate sealed pings
description: Why Shopify sealed alerts duplicated and the dedup principle that fixes them
---

# Duplicate sealed Discord pings — root cause & dedup principle

## Root cause
All alert dedup (per-cycle set, permanent "seen", per-variant restock availability)
is keyed by `item_id`. Some Shopify retailers expose ONE sealed product under
multiple ids — separate variant ids OR duplicate catalog entries with the SAME
store + title (seen at Potomac Distribution: a Booster Bundle listed twice, even
at different prices). Each distinct id then fires its own restock/deal alert →
the user sees duplicate pings for one box.

eBay is NOT a source of this: its item ids are stable per listing and different
sellers listing the same title are genuinely distinct deals — never collapse eBay
by title.

## Fix principle (durable)
- Collapse Shopify listings by `(store, whitespace/case-normalized title)` at the
  source (final return of the fetch-all path), so BOTH the restock pass and the
  deal pass see one entry per product.
- Representative MUST use a **stable id** — the lowest `item_id` in the group.
  Variant ids never change, so restock/`seen` state keyed on it stays consistent
  across cycles (no spurious re-baseline / re-alert). Do NOT pick the
  representative by price or availability — those flip and break cross-cycle state.
- Aggregate the mutable fields onto the representative: `available = any in stock`,
  and `price`/`url` = the cheapest in-stock duplicate (keeps the best buyable deal).
- Key includes the store, so the same title across DIFFERENT stores stays separate
  — those are legit cross-store deals (e.g. "Ascended Heroes Booster Bundle" at two
  stores with different prices is two real alerts, not a duplicate).

**Why:** dedup identity was variant-level but the user perceives duplicates at the
product level; the stable-min-id rule keeps the cheap source-level collapse from
disturbing the item_id-keyed persistent state.

## Second, separate duplicate path
The SAME box can fire a `[RESTOCK]` (keyed by availability) and a `[SEALED]` deal
(keyed by `seen`) in the same cycle. Restock pass runs first and does NOT mark
`seen`. Suppress by adding the id to the per-cycle dedup set **only on a delivered
restock** — a failed restock send must leave the deal pass free to surface it
(no lost signal, no duplicate). A below-market box that simply stays in stock is
picked up by the deal pass on a later cycle (no restock transition there).

## Third path — the BIGGEST duplicate source: two instances posting at once
This is NOT a code bug. The workspace workflow (`python main.py`) and the published
Deployment run the SAME loop against the SAME Discord webhook secrets, but each keeps
its OWN dedup state files, so they cannot dedup against each other → every alert fires
twice. Whenever the user reports duplicates "even after the dedup fix," suspect this
first: check `fetch_deployment_logs` — if prod is actively posting `[OK]` embeds while
the dev workflow is also running, that's the cause.

**Fix (durable):** post to Discord from exactly ONE instance.
- `REPLIT_DEPLOYMENT == "1"` is set ONLY inside a published Deployment, never in the
  workspace — use it as the live/dry-run discriminator. `DISCORD_LIVE=1` is the manual
  override to force the workspace to post for real (intentional delivery tests only).
- Gate the single Discord POST choke point (`discord_router._post_embed`): in dry-run,
  log `[DRY-RUN] would send …` and **return True** so callers still advance dedup/seen
  state like prod (returning False re-spams every cycle and stalls restock state).
- Startup banner prints LIVE vs DRY-RUN so prod logs visibly confirm posting after a
  republish (it fails CLOSED — if `REPLIT_DEPLOYMENT` were ever unset in prod it goes
  silent; the banner + `DISCORD_LIVE` override are the safety net).
- **Critical coupling:** because dry-run returns True, the workspace mutates its state
  files. Those files are git-TRACKED, so committing dev's dry-run "seen" items would
  seed the Deployment bundle on republish and make prod SILENTLY SKIP real pings.
  Therefore dev MUST write to SEPARATE git-ignored `*.local.json` state files. The
  live/dry-run bool also picks the state-file path suffix (`""` live vs `".local"`
  dry-run); `*.local.json` is in `.gitignore`. Keep prod's committed seed files clean.

**Why:** restarting the guarded dev workflow stops duplicates IMMEDIATELY (dev goes
silent) even before a republish; republishing then carries the guard to prod, which
keeps posting because `REPLIT_DEPLOYMENT=1` there. Runtime state must never be a
tracked artifact shared across environments.

## Repeat limit for sealed products (owner decision, 2026-10-05)

The rule above ("eBay multi-seller is legit, never collapse") still holds for
dedup by listing id, but the owner chose to LIMIT how often one sealed catalog
product alerts: its first 3 alerts in 24h post as usual, after that only a
listing cheaper than everything already posted for it in the window
(`config.SEALED_REPEAT_FREE`, `SEALED_REPEAT_WINDOW`, `api_engines.repeat_allowed`,
state in `product_alerts.json`). Do not remove it as a "collapse" bug.

**Why:** hot products (a new set's ETB) produced dozens of alerts a day that all
cleared the deal test because the catalog price sat above the going price —
1,117 of 5,044 sealed alerts in 30 days were seven products. Replayed, the limit
kept the day's cheapest listing of a product on 97% of days.

**How to apply:** the limit runs LAST in the sealed branch, after every guard
that can drop a listing, so a dropped listing never uses a slot or sets the low.
Listings with a condition note are keyed apart (`|caveat`). It must fail open.

## Dedup state is in PostgreSQL too (2026-10-05)

`seen_listings.json` did not survive republishes (554 duplicate alerts in 30
days, mostly one week with seven publishes). `scanner_state.py` mirrors the ids
into table `scanner_seen` (in `database/watchlist_schema.sql`); `main.load_seen`
unions file + table on the live instance. It fails soft to the file if the table
is missing. The TCGplayer "best price already alerted" baseline is rebuilt from
those ids at startup (`tcgplayer_source.seed_best_emitted`).
