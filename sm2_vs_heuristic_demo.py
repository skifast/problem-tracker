"""
Runs both scheduling algorithms -- the app's ad-hoc score_row() heuristic and
canonical SM-2 -- over the same real problems in problems.db, and prints them
side by side.

SM-2's input is accepted_dates, not review_history: review_history only spans
the few weeks since this app started recording in-app reviews (22 rows,
total), while accepted_dates carries LeetCode/NeetCode's own multi-year
accepted-submission record for each problem -- exactly the "mature dataset"
docs/scheduling-comparison.md said was needed to see real divergence, and
review_history alone never had it. Every accepted_dates row is by definition
a successful submission, so each SM-2 replay quality is a flat 5 (no partial-
credit/struggle signal exists at this granularity).

Read-only: does not write to problems.db. Requires the dashboard's deps
(pandas/streamlit) since it reuses dashboard.load_problems().

Run: python3 sm2_vs_heuristic_demo.py
"""

from datetime import date
from pathlib import Path

import sqlite3

import dashboard as dash
from sm2 import replay

DB_PATH = Path(__file__).parent / "problems.db"


def sm2_next_due(problem_url: str, con: sqlite3.Connection):
    """Replay a problem's full accepted-date history through SM-2, in
    chronological order. Returns (state, next_due_date, days_until_due) or
    None if there's no accepted-date history for this problem."""
    rows = con.execute(
        "SELECT accepted_date FROM accepted_dates WHERE problem_url = ? ORDER BY accepted_date ASC",
        (problem_url,),
    ).fetchall()

    if not rows:
        return None

    state = replay([5] * len(rows))
    last_date = date.fromisoformat(rows[-1][0])
    next_due = date.fromordinal(last_date.toordinal() + state.interval)
    days_until_due = (next_due - date.today()).days
    return state, next_due, days_until_due


def main():
    dash.init_db()
    df = dash.load_problems()
    con = sqlite3.connect(str(DB_PATH))

    reviewed = df[df["solved_count"] + df["struggle_count"] > 0].copy()
    reviewed["heuristic_score"] = reviewed.apply(dash.score_row, axis=1)
    reviewed = reviewed.sort_values("heuristic_score", ascending=False)

    print(
        f"{'problem':<45} {'heuristic':>9} {'reps':>5} {'EF':>5} "
        f"{'SM-2 next due':>14} {'SM-2 days':>10}  divergence"
    )
    print("-" * 130)

    divergence_count = 0
    total_count = 0

    for _, row in reviewed.iterrows():
        sm2_result = sm2_next_due(row["url"], con)
        if sm2_result is None:
            continue
        state, next_due, days_until_due = sm2_result
        total_count += 1

        heuristic_says_urgent = row["heuristic_score"] >= 20  # matches the app's "Overdue" tab threshold
        sm2_says_due = days_until_due <= 0
        divergence = ""
        if heuristic_says_urgent and not sm2_says_due:
            divergence = f"heuristic flags it now; SM-2 not due for {days_until_due} more day(s)"
        elif sm2_says_due and not heuristic_says_urgent:
            divergence = "SM-2 says it's due; heuristic doesn't rank it urgent"
        if divergence:
            divergence_count += 1

        print(
            f"{row['name']:<45} {row['heuristic_score']:>9.1f} {state.repetitions:>5} "
            f"{state.ease_factor:>5.2f} {next_due.isoformat():>14} {days_until_due:>10}  {divergence}"
        )

    con.close()
    print("-" * 130)
    print(f"{divergence_count} of {total_count} problems show heuristic/SM-2 divergence.")


if __name__ == "__main__":
    main()
