---
name: Watch catalog completeness
description: Why bounded catalog loading must preserve incremental progress.
---

Bounded catalog refreshes must retain completed groups and advance through missing groups rather than restarting from the first group.

**Why:** Large TCG catalogs cannot reliably load within an interactive request budget. Restarting the same prefix can permanently exclude later sets from card-specific suggestions.

**How to apply:** Preserve per-group results, distinguish successful empty responses from transient failures, and prioritize untouched groups over retrying failed groups. Do not treat a partial catalog as proof that a card has no other sets.