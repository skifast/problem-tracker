"""
One-time cleanup: merge problems that got duplicated across sources before
SLUG_ALIASES covered their NeetCode<->LeetCode slug pair (see tracker.py's
SLUG_ALIASES comment for the root cause).

Each duplicate is a pair of rows with the same `name`: one source='neetcode'
(carries category, starred, review_history -- the one you actually interact
with) and one source='leetcode' (a stray "new discovery" row inserted only
because sync_leetcode_solved() couldn't match its slug back to the NeetCode
row -- carries difficulty and nothing else of value).

For each pair found:
  - copy difficulty (and last_accepted, if the neetcode row is missing it)
    from the leetcode row onto the surviving neetcode row
  - delete the leetcode row

Every review_history row already points at a neetcode-sourced url in this
dataset (verified before writing this script), so no problem_url rewriting
is needed -- but this script re-checks that per pair and refuses to delete a
row that review_history still references, rather than assume it.

Safe to re-run: once duplicates are merged, a second run finds none and
does nothing.

Run: python3 dedupe_leetcode_duplicates.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "problems.db"


def find_duplicate_pairs(con):
    """(neetcode_row, leetcode_row) pairs sharing the same name (case-insensitive)."""
    names = [
        name for (name,) in con.execute(
            "SELECT lower(name) FROM problems GROUP BY lower(name) HAVING COUNT(*) > 1"
        ).fetchall()
    ]

    pairs = []
    for name in names:
        rows = con.execute(
            "SELECT url, source, starred, difficulty, last_accepted, category "
            "FROM problems WHERE lower(name) = ?",
            (name,),
        ).fetchall()
        neet = [r for r in rows if r[1] == "neetcode"]
        leet = [r for r in rows if r[1] == "leetcode"]
        if len(neet) == 1 and len(leet) == 1:
            pairs.append((neet[0], leet[0]))
    return pairs


def main():
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA foreign_keys = ON")

    pairs = find_duplicate_pairs(con)
    print(f"Found {len(pairs)} duplicate pair(s).")

    merged = 0
    skipped = 0
    for neet, leet in pairs:
        neet_url, _, _, neet_diff, neet_last_acc, _ = neet
        leet_url, _, _, leet_diff, leet_last_acc, _ = leet

        review_count = con.execute(
            "SELECT COUNT(*) FROM review_history WHERE problem_url = ?", (leet_url,)
        ).fetchone()[0]
        if review_count:
            # Shouldn't happen in this dataset (verified beforehand), but
            # don't silently drop real review history if it ever does.
            print(f"SKIP {leet_url}: has {review_count} review_history row(s), not deleting.")
            skipped += 1
            continue

        new_difficulty = neet_diff or leet_diff
        new_last_accepted = neet_last_acc or leet_last_acc
        con.execute(
            "UPDATE problems SET difficulty = ?, last_accepted = ? WHERE url = ?",
            (new_difficulty, new_last_accepted, neet_url),
        )
        con.execute("DELETE FROM problems WHERE url = ?", (leet_url,))
        merged += 1

    con.commit()
    remaining = con.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    con.close()

    print(f"Merged {merged} duplicate pair(s), skipped {skipped}. {remaining} problems remain.")


if __name__ == "__main__":
    main()
