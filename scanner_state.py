"""
Durable copy of the scanner's "already alerted" list in PostgreSQL.

The dedup list lives in a JSON file (see main.load_seen). The deployment's disk
does not keep that file across a republish or restart, so afterwards every
listing still in the feeds alerted a second time: 554 duplicate alerts in 30
days, most in one week with seven republishes. The watchlist bot already keeps
its data in the deployment's PostgreSQL database; this stores the alerted ids
there too.

Table `scanner_seen` comes from database/watchlist_schema.sql, applied at
publish like the watchlist tables — nothing here runs DDL. Every function fails
soft: if the database or the table is missing, the scanner logs one warning and
carries on with the file alone, exactly as before.
"""

from __future__ import annotations

_available = False      # True once load_seen() has read the table successfully
_failures  = 0          # consecutive write failures; writes stop after _MAX_FAILURES
_MAX_FAILURES = 3


def _connect():
    import psycopg      # lazy: the dry-run workspace and the tests never need it
    return psycopg.connect(connect_timeout=10)


def load_seen(expiry_days: int) -> dict | None:
    """Return {item_id: iso_timestamp} for ids newer than `expiry_days`, pruning
    older rows, or None when the database cannot be used."""
    global _available, _failures
    try:
        with _connect() as conn:
            conn.execute(
                "DELETE FROM scanner_seen WHERE seen_at < now() - make_interval(days => %s)",
                (expiry_days,),
            )
            rows = conn.execute("SELECT item_id, seen_at FROM scanner_seen").fetchall()
    except Exception as e:
        _available = False
        print(f"[STATE][WARN] dedup database unavailable ({type(e).__name__}) — "
              f"using the local file only")
        return None
    _available, _failures = True, 0
    return {item_id: seen_at.isoformat() for item_id, seen_at in rows}


def add_seen(entries: dict) -> None:
    """Store {item_id: iso_timestamp} entries. Never raises."""
    global _available, _failures
    if not _available or not entries:
        return
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO scanner_seen (item_id, seen_at) VALUES (%s, %s::timestamptz) "
                    "ON CONFLICT (item_id) DO NOTHING",
                    list(entries.items()),
                )
        _failures = 0
    except Exception as e:
        _failures += 1
        print(f"[STATE][WARN] could not store {len(entries)} dedup id(s) ({type(e).__name__})")
        if _failures >= _MAX_FAILURES:
            _available = False
            print("[STATE][WARN] dedup database writes paused until the next restart")
