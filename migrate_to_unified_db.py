"""
One-time migration: collapse neetcode_tracker.db + neetcode_reviews.db (+ CSV)
into a single problems.db with a proper FK between problems and review_history.

Safe to re-run: drops and rebuilds problems.db only. Source files are read-only
here and are never modified or deleted.

Run: python migrate_to_unified_db.py
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OLD_TRACKER_DB = ROOT / "neetcode_tracker.db"      # has last_seen (scrape timestamps)
OLD_REVIEWS_DB = ROOT / "neetcode_reviews.db"       # has review_history + notes
NEW_DB = ROOT / "problems.db"

SCHEMA = """
    PRAGMA foreign_keys = ON;

    CREATE TABLE problems (
        url           TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        category      TEXT NOT NULL,
        starred       INTEGER NOT NULL DEFAULT 0,
        last_accepted TEXT,
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL
    );

    CREATE TABLE review_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        problem_url TEXT NOT NULL REFERENCES problems(url) ON DELETE CASCADE,
        reviewed_at TEXT NOT NULL,
        result      TEXT NOT NULL,
        notes       TEXT NOT NULL DEFAULT ''
    );
"""


def load_last_seen_by_url() -> dict:
    """last_seen timestamps from the old scraper DB, keyed by url."""
    if not OLD_TRACKER_DB.exists():
        return {}
    con = sqlite3.connect(str(OLD_TRACKER_DB))
    rows = con.execute("SELECT url, last_seen FROM completed_problems").fetchall()
    con.close()
    return {url: last_seen for url, last_seen in rows}


def main():
    if NEW_DB.exists():
        print(f"{NEW_DB.name} already exists — deleting and rebuilding from source DBs.")
        NEW_DB.unlink()

    if not OLD_REVIEWS_DB.exists():
        raise SystemExit(f"Missing {OLD_REVIEWS_DB} — this is the source of truth for problems + review history.")

    last_seen_by_url = load_last_seen_by_url()
    now = datetime.now(timezone.utc).isoformat()

    src = sqlite3.connect(str(OLD_REVIEWS_DB))
    problems = src.execute(
        "SELECT url, name, category, starred, last_accepted FROM problems"
    ).fetchall()
    history = src.execute(
        "SELECT problem_url, reviewed_at, result, notes FROM review_history ORDER BY id"
    ).fetchall()
    src.close()

    dest = sqlite3.connect(str(NEW_DB))
    dest.executescript(SCHEMA)

    for url, name, category, starred, last_accepted in problems:
        last_seen = last_seen_by_url.get(url, now)
        dest.execute(
            """
            INSERT INTO problems (url, name, category, starred, last_accepted, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (url, name, category, starred, last_accepted, last_seen, last_seen),
        )

    skipped = 0
    for problem_url, reviewed_at, result, notes in history:
        exists = dest.execute("SELECT 1 FROM problems WHERE url = ?", (problem_url,)).fetchone()
        if not exists:
            skipped += 1
            continue
        dest.execute(
            """
            INSERT INTO review_history (problem_url, reviewed_at, result, notes)
            VALUES (?, ?, ?, ?)
            """,
            (problem_url, reviewed_at, result, notes),
        )

    dest.commit()

    problem_count = dest.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    history_count = dest.execute("SELECT COUNT(*) FROM review_history").fetchone()[0]
    dest.close()

    print(f"Migrated {problem_count} problems and {history_count} review_history rows into {NEW_DB.name}.")
    if skipped:
        print(f"WARNING: skipped {skipped} review_history rows with no matching problem url.")
    print("Old files (neetcode_tracker.db, neetcode_reviews.db, neetcode_tracker.csv) were left untouched.")


if __name__ == "__main__":
    main()
