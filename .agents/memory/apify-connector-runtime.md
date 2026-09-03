---
name: Apify connector runtime
description: Why sold-comps enrichment crosses from Python into a small JavaScript connector helper, plus its reliability constraints.
---

Use the managed Apify connection through the JavaScript connector SDK helper rather than expecting a Python connector package. Keep sold-comps enrichment asynchronous, fail-open, conservatively matched, cached, and protected by a daily paid-lookup cap.

**Why:** The integration-generated Python package was unavailable in the project registry, and the generated JavaScript version was stale; the current JavaScript SDK worked. eBay sold results are noisy enough that broad matching can create dangerously misleading averages, and actor runs are paid.

**How to apply:** When changing sold comps, preserve immediate initial Discord delivery and only edit in enrichment afterward. Fail closed on comp identity but fail open on alert delivery. Verify the installed connector SDK rather than trusting generated version text.