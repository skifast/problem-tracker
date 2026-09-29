"""
One-time cleanup: merge `problems` rows that got duplicated because
NeetCode's scraped URL included which list you were viewing
(?list=neetcode150 vs ?list=allNC), and `url` -- including that query
string -- was the primary key. Switching the scrape target between
neetcode150 and allNC this session made every problem common to both lists
get stored under two different "identities". Root cause fixed in
tracker.py (the query string is now stripped before a URL is ever stored);
this cleans up what's already in the DB from before that fix.

Pairs found here are neetcode.io URLs differing only in ?list=..., always
same name/category/source (confirmed by inspection before writing this).
For each duplicate pair:
  - prefer the ?list=neetcode150 url as the survivor (the current,
    going-forward scrape target, and it also carries the earlier, more
    accurate first_seen) -- falls back to whichever url sorts first if
    neither/both match, so this still terminates for an edge case
  - COALESCE every column from the loser into the survivor for whichever
    fields the survivor is missing (defensive; the two rows were identical
    in every observed case)
  - move any review_history / accepted_dates rows pointing at the loser's
    url over to the survivor's url first (accepted_dates de-duplicates
    naturally via its composite PK, so a moved row that collides with one
    the survivor already has is just dropped, not double-counted)
  - delete the loser row

Safe to re-run: once merged, a second run finds no duplicate groups and
does nothing.

Run: python3 dedupe_query_string_duplicates.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "problems.db"

MERGE_COLUMNS = ["starred", "last_accepted", "first_seen", "last_seen", "source", "difficulty", "topics"]


def pick_survivor(rows):
    """rows: list of sqlite3.Row for one duplicate-name group. Returns
    (survivor_row, [loser_rows])."""
    nc150 = [r for r in rows if "list=neetcode150" in r["url"]]
    survivor = nc150[0] if nc150 else sorted(rows, key=lambda r: r["url"])[0]
    losers = [r for r in rows if r["url"] != survivor["url"]]
    return survivor, losers


def main():
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row

    names = [
        name for (name,) in con.execute(
            "SELECT lower(name) FROM problems GROUP BY lower(name) HAVING COUNT(*) > 1"
        ).fetchall()
    ]
    print(f"Found {len(names)} duplicate-name group(s).")

    merged = 0
    for name in names:
        rows = con.execute("SELECT * FROM problems WHERE lower(name) = ?", (name,)).fetchall()
        survivor, losers = pick_survivor(rows)

        for loser in losers:
            # Fill in anything the survivor is missing from the loser.
            updates, params = [], []
            for col in MERGE_COLUMNS:
                if survivor[col] is None and loser[col] is not None:
                    updates.append(f"{col} = ?")
                    params.append(loser[col])
            if updates:
                params.append(survivor["url"])
                con.execute(f"UPDATE problems SET {', '.join(updates)} WHERE url = ?", params)

            # Re-point child rows before deleting the loser, so ON DELETE
            # CASCADE can't silently drop history that was never migrated.
            con.execute(
                "UPDATE review_history SET problem_url = ? WHERE problem_url = ?",
                (survivor["url"], loser["url"]),
            )
            for (accepted_date,) in con.execute(
                "SELECT accepted_date FROM accepted_dates WHERE problem_url = ?", (loser["url"],)
            ).fetchall():
                con.execute(
                    "INSERT OR IGNORE INTO accepted_dates (problem_url, accepted_date) VALUES (?, ?)",
                    (survivor["url"], accepted_date),
                )
            con.execute("DELETE FROM accepted_dates WHERE problem_url = ?", (loser["url"],))

            con.execute("DELETE FROM problems WHERE url = ?", (loser["url"],))

        merged += len(losers)

    con.commit()
    remaining = con.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    remaining_dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT lower(name) FROM problems GROUP BY lower(name) HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    con.close()

    print(f"Merged {merged} duplicate row(s) across {len(names)} group(s).")
    print(f"{remaining} problems remain; {remaining_dupes} duplicate-name group(s) left.")


if __name__ == "__main__":
    main()
