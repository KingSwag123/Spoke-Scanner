---
name: Watch catalog completeness
description: Why bounded catalog loading must preserve incremental progress.
---

Bounded catalog refreshes must retain completed groups and advance through missing groups rather than restarting from the first group.

**Why:** Large TCG catalogs cannot reliably load within an interactive request budget. Restarting the same prefix can permanently exclude later sets from card-specific suggestions.

**How to apply:** Preserve per-group results, distinguish successful empty responses from transient failures, and prioritize untouched groups over retrying failed groups. Do not treat a partial catalog as proof that a card has no other sets.

Autocomplete with a typed set name must prioritize candidate set groups and verify their card membership without waiting for every game's full catalog.

**Why:** The broad incremental loader still left Lugia/Silver Tempest invisible while unrelated groups or games were incomplete. Background progress alone is not sufficient for an interactive dropdown.

**How to apply:** Keep typed-set lookups bounded and cached, show unresolved/loading status rather than an empty-match claim, and allow verified selected sets to supply rarity choices independently of broad catalog completeness.