"""Coding Problem Spaced Repetition Dashboard — run: streamlit run dashboard.py"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

import coach
from blind75 import BLIND_75, BLIND_75_EXTRAS

DESKTOP   = Path(__file__).parent
DB_PATH   = DESKTOP / "problems.db"
CAPACITY_OPTIONS = [3, 5, 8, 12, 20]
OVERDUE_DISPLAY_OPTIONS = [10, 20, 30, 50, 100]
SET_DISPLAY_OPTIONS = [3, 5, 10, 20]
MAX_PER_CATEGORY_PCT_OPTIONS = [20, 40, 60, 80, 100]
DEFAULT_MAX_PER_CATEGORY_PCT = 40

# ── Database ──────────────────────────────────────────────────────────────────
# Schema is owned by tracker.py (the scraper), which creates/upserts
# the `problems` table. This dashboard only adds review_history on top of it.

def db():
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH.name} not found. Run tracker.py at least once to scrape "
            "and create the problems table before launching the dashboard."
        )
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS review_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_url TEXT NOT NULL REFERENCES problems(url) ON DELETE CASCADE,
                reviewed_at TEXT NOT NULL,
                result      TEXT NOT NULL,
                notes       TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS weekly_reports (
                week       TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                report     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coach_notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_url TEXT NOT NULL REFERENCES problems(url) ON DELETE CASCADE,
                created_at  TEXT NOT NULL,
                diagnosis   TEXT NOT NULL
            );
        """)

# ── Queries ───────────────────────────────────────────────────────────────────

def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None

def load_problems() -> pd.DataFrame:
    """solved_count counts distinct calendar dates the problem was solved
    on, combining two signals that are each incomplete alone: LeetCode/
    NeetCode's own accepted-submission history (accepted_dates, backfilled
    by tracker.py -- a problem solved on the platform but never reviewed
    through this app would otherwise show 0 solves) and this app's own
    in-review "Solved" clicks (review_history -- so marking a problem
    Solved just now counts immediately, without waiting on tracker.py's
    next sync to catch up). Falls back to review_history alone on a DB
    from before accepted_dates existed."""
    with db() as con:
        if _table_exists(con, "accepted_dates"):
            solved_subquery = """
                SELECT problem_url, COUNT(*) AS solved_count FROM (
                    SELECT problem_url, accepted_date AS d FROM accepted_dates
                    UNION
                    SELECT problem_url, date(reviewed_at) AS d FROM review_history WHERE result='Solved'
                ) combined
                GROUP BY problem_url
            """
        else:
            solved_subquery = """
                SELECT problem_url, COUNT(*) AS solved_count
                FROM review_history WHERE result='Solved' GROUP BY problem_url
            """
        return pd.read_sql_query(
            f"""
            SELECT
                p.url, p.name, p.category, p.starred, p.last_accepted, p.source, p.difficulty,
                COALESCE(s.solved_count,   0) AS solved_count,
                COALESCE(st.struggle_count,0) AS struggle_count,
                last_r.result                 AS last_result,
                last_r.reviewed_at            AS last_reviewed,
                last_r.notes                  AS last_notes
            FROM problems p
            LEFT JOIN ({solved_subquery}) s ON s.problem_url = p.url
            LEFT JOIN (
                SELECT problem_url, COUNT(*) AS struggle_count
                FROM review_history WHERE result IN ('Hint','Failed') GROUP BY problem_url
            ) st ON st.problem_url = p.url
            LEFT JOIN (
                SELECT problem_url, result, reviewed_at, notes
                FROM review_history
                WHERE id IN (SELECT MAX(id) FROM review_history GROUP BY problem_url)
            ) last_r ON last_r.problem_url = p.url
            """,
            con,
        )

def load_catalog() -> pd.DataFrame:
    """Every problem in NeetCode's practice catalog tracker.py has ever seen, completed or
    not -- the pool "recommend new problems" draws from. Tolerates a
    problems.db from before this table existed (or one whose catalog scrape
    just hasn't run yet): returns empty rather than crashing the dashboard."""
    columns = ["url", "name", "category", "difficulty", "source", "completed"]
    with db() as con:
        try:
            # `completed` (NeetCode's checkbox) only exists once tracker.py
            # has run since it was added; default to 0 until then.
            present = {r[1] for r in con.execute("PRAGMA table_info(catalog)")}
            select = ", ".join(c if c in present else f"0 AS {c}" for c in columns)
            return pd.read_sql_query(f"SELECT {select} FROM catalog ORDER BY rowid", con)
        except (sqlite3.OperationalError, pd.errors.DatabaseError):
            return pd.DataFrame(columns=columns)

def reviewed_today() -> set:
    today = date.today().isoformat()
    with db() as con:
        rows = con.execute(
            "SELECT DISTINCT problem_url FROM review_history WHERE date(reviewed_at, 'localtime')=?",
            (today,),
        ).fetchall()
    return {r[0] for r in rows}

def ensure_tracked(url: str, name: str, category: str, source: str = "neetcode"):
    """Upsert a bare `problems` row before recording a review, so cards for
    a recommended-but-never-attempted problem (from `catalog`, not yet in
    `problems`) don't violate review_history's FK on first click. No-op if
    already tracked -- never overwrites an existing row's fields."""
    now = datetime.utcnow().isoformat()
    with db() as con:
        con.execute(
            """
            INSERT INTO problems (url, name, category, starred, last_accepted, first_seen, last_seen, source)
            VALUES (?, ?, ?, 0, NULL, ?, ?, ?)
            ON CONFLICT(url) DO NOTHING
            """,
            (url, name, category, now, now, source),
        )

def set_starred(url: str, name: str, category: str, source: str, starred: bool):
    """Star/unstar from the dashboard. Writes the same problems.starred column
    NeetCode's own star populates, so scoring and every query treat both alike.
    ensure_tracked first, since cards can come from the untracked catalog."""
    ensure_tracked(url, name, category, source)
    with db() as con:
        con.execute("UPDATE problems SET starred=? WHERE url=?", (int(starred), url))

def record_review(url: str, result: str, notes: str = ""):
    with db() as con:
        con.execute(
            "INSERT INTO review_history (problem_url, reviewed_at, result, notes) VALUES (?,?,?,?)",
            (url, datetime.utcnow().isoformat(), result, notes),
        )

def problem_history(url: str, n: int = coach.HISTORY_LIMIT) -> list:
    """Most recent reviews of one problem, newest first."""
    with db() as con:
        rows = con.execute(
            "SELECT reviewed_at, result, notes FROM review_history "
            "WHERE problem_url=? ORDER BY id DESC LIMIT ?", (url, n),
        ).fetchall()
    return [{"reviewed_at": r[0], "result": r[1], "notes": r[2]} for r in rows]

def save_coach_note(url: str, diagnosis: str):
    with db() as con:
        con.execute(
            "INSERT INTO coach_notes (problem_url, created_at, diagnosis) VALUES (?,?,?)",
            (url, datetime.utcnow().isoformat(), diagnosis),
        )

def latest_coach_note(url: str) -> Optional[tuple]:
    """(date, diagnosis) of the newest coaching note for a problem, or None."""
    with db() as con:
        row = con.execute(
            "SELECT created_at, diagnosis FROM coach_notes WHERE problem_url=? "
            "ORDER BY id DESC LIMIT 1", (url,),
        ).fetchone()
    return (row[0][:10], row[1]) if row else None

def run_coach(url: str, name: str, category: str, difficulty, result: str, note: str) -> Optional[str]:
    """Diagnose a Hint/Failed attempt and store the result. Returns an error
    message to show the user, or None on success. Call after record_review so
    the attempt itself is excluded from the earlier-attempts context."""
    history = problem_history(url, coach.HISTORY_LIMIT + 1)[1:]
    try:
        diagnosis = coach.diagnose(name, category, difficulty, result, note, history)
    except coach.CoachError as e:
        return str(e)
    save_coach_note(url, diagnosis)
    return None

def week_key(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"

def get_weekly_report(week: str) -> Optional[str]:
    with db() as con:
        row = con.execute("SELECT report FROM weekly_reports WHERE week=?", (week,)).fetchone()
    return row[0] if row else None

def save_weekly_report(week: str, report: str):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO weekly_reports (week, created_at, report) VALUES (?,?,?)",
            (week, datetime.utcnow().isoformat(), report),
        )

def reviews_since(days: int = 7) -> list:
    """Review attempts from the last `days` days, newest first, with the
    problem's name/category attached."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db() as con:
        rows = con.execute(
            """
            SELECT h.reviewed_at, h.result, h.notes, p.name, p.category
            FROM review_history h JOIN problems p ON p.url = h.problem_url
            WHERE date(h.reviewed_at, 'localtime') >= ? ORDER BY h.id DESC
            """,
            (cutoff,),
        ).fetchall()
    keys = ("reviewed_at", "result", "notes", "name", "category")
    return [dict(zip(keys, r)) for r in rows]

def recent_history(n: int = 30) -> pd.DataFrame:
    with db() as con:
        return pd.read_sql_query(
            """
            SELECT rh.reviewed_at, p.name, p.category, rh.result, rh.notes
            FROM review_history rh
            JOIN problems p ON p.url = rh.problem_url
            ORDER BY rh.reviewed_at DESC LIMIT ?
            """,
            con,
            params=(n,),
        )

# ── Scheduling ────────────────────────────────────────────────────────────────

def parse_date(val) -> Optional[date]:
    if val is None or (isinstance(val, float)):
        return None
    s = str(val).strip()
    if not s or s == "nan":
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None

# Problems last touched before this are a weaker signal of current skill, so
# their staleness counts for less. Age is uncapped, so a flat bonus for recent
# problems would be swamped by a years-old problem's age term; scaling the age
# term down lets recent work compete.
RECENT_CUTOFF = date(2024, 1, 1)
OLD_AGE_WEIGHT = 0.25

def score_row(row) -> float:
    ref = parse_date(row.get("last_reviewed")) or parse_date(row.get("last_accepted"))
    age = (date.today() - ref).days if ref else 999

    dates = [d for d in (parse_date(row.get("last_reviewed")), parse_date(row.get("last_accepted"))) if d]
    is_recent = bool(dates) and max(dates) >= RECENT_CUTOFF

    s = float(age) * 2.0 * (1.0 if is_recent else OLD_AGE_WEIGHT)

    if row.get("starred"):
        s += 10

    last = row.get("last_result") or ""
    if last == "Failed":
        s += 20
    elif last == "Hint":
        s += 10

    # Single discount for cumulative mastery, independent of whether the
    # most recent review specifically was "Solved" -- previously this was
    # applied twice (once here, once again inside an `elif last == "Solved"`
    # branch), so a problem's discount depended on its last outcome rather
    # than purely on how many times it's been solved. See
    # docs/scheduling-comparison.md / commit history for context.
    s -= min(int(row.get("solved_count", 0)) * 2, 10)
    s += int(row.get("struggle_count", 0)) * 5
    return s

def build_queue(
    df: pd.DataFrame, capacity: int, done: set,
    max_per_cat_frac: float = DEFAULT_MAX_PER_CATEGORY_PCT / 100,
) -> pd.DataFrame:
    candidates = df[~df["url"].isin(done)].copy()
    if candidates.empty:
        return candidates
    candidates["score"] = candidates.apply(score_row, axis=1)
    candidates = candidates.sort_values("score", ascending=False)

    # Soft cap, not a hard one: this first pass fills at most max_per_cat
    # slots per category; the backfill pass below fills any remaining
    # capacity uncapped, so a single-category queue is still possible when
    # too few other categories have anything eligible.
    max_per_cat = max(1, round(capacity * max_per_cat_frac))
    selected, cat_counts, in_sel = [], {}, set()

    for _, row in candidates.iterrows():
        cat = row["category"]
        if cat_counts.get(cat, 0) < max_per_cat:
            selected.append(row)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            in_sel.add(row["url"])
        if len(selected) >= capacity:
            break

    if len(selected) < capacity:
        for _, row in candidates.iterrows():
            if row["url"] not in in_sel:
                selected.append(row)
                in_sel.add(row["url"])
            if len(selected) >= capacity:
                break

    return pd.DataFrame(selected) if selected else pd.DataFrame(columns=candidates.columns)

# ── Recommendations ──────────────────────────────────────────────────────────

MIN_ATTEMPTS_FOR_WEAKNESS = 3  # below this, one bad problem would brand a whole category "weak"
DIFFICULTY_RANK = {"Easy": 0, "Medium": 1, "Hard": 2}
RECS_PER_CATEGORY = 3

def weak_categories(df: pd.DataFrame) -> pd.DataFrame:
    """Per-category struggle rate among tracked (attempted) problems, most
    struggled-with first. Categories with too few attempts are dropped
    rather than mislabeled "weak" off a single data point, and a category
    with zero recorded struggles is dropped outright -- 0% struggle rate
    isn't "weak," it's just not what this function is for."""
    attempted = df[(df["solved_count"] > 0) | (df["struggle_count"] > 0)]
    if attempted.empty:
        return pd.DataFrame(columns=["category", "attempts", "weakness"])

    grouped = attempted.groupby("category").agg(
        solved=("solved_count", "sum"), struggled=("struggle_count", "sum")
    )
    grouped["attempts"] = grouped["solved"] + grouped["struggled"]
    grouped = grouped[(grouped["attempts"] >= MIN_ATTEMPTS_FOR_WEAKNESS) & (grouped["struggled"] > 0)]
    grouped["weakness"] = grouped["struggled"] / grouped["attempts"]
    return (
        grouped[["attempts", "weakness"]]
        .sort_values("weakness", ascending=False)
        .reset_index()
    )

def recommend_new_problems(
    catalog_df: pd.DataFrame, problems_df: pd.DataFrame, weak_df: pd.DataFrame,
    per_category: int = RECS_PER_CATEGORY,
) -> pd.DataFrame:
    """Untracked catalog problems to try next, prioritizing:
    1. categories with zero tracked attempts (coverage gaps -- no signal
       yet, so likely the biggest blind spot), then
    2. categories in weak_categories(), most struggled-with first.
    Categories in neither list are left out -- this fills gaps, it's not a
    full catalog browser (Browse by Pattern already covers that). Within a
    category, easier difficulties are suggested before harder ones so
    recommendations build up rather than front-loading Hards."""
    out_cols = list(catalog_df.columns) + ["reason"]
    if catalog_df.empty:
        return pd.DataFrame(columns=out_cols)

    untracked = catalog_df[~catalog_df["url"].isin(set(problems_df["url"]))].copy()
    if untracked.empty:
        return pd.DataFrame(columns=out_cols)

    attempted_categories = set(problems_df["category"])
    gap_categories = [c for c in untracked["category"].unique() if c not in attempted_categories]
    weak_order = list(weak_df["category"]) if not weak_df.empty else []
    priority = gap_categories + [c for c in weak_order if c not in gap_categories]
    if not priority:
        return pd.DataFrame(columns=out_cols)

    untracked["_diff_rank"] = untracked["difficulty"].map(DIFFICULTY_RANK).fillna(1)

    selected = []
    for cat in priority:
        picks = untracked[untracked["category"] == cat].sort_values("_diff_rank").head(per_category).copy()
        picks["reason"] = (
            "No attempts tracked yet in this pattern" if cat in gap_categories
            else "One of your weaker patterns by struggle rate"
        )
        selected.append(picks)

    return pd.concat(selected, ignore_index=True).drop(columns="_diff_rank")

PROBLEM_SETS = ["All", "NeetCode 150", "Blind 75"]

def _norm_name(name) -> str:
    """Lowercase alphanumerics only, so 'Best Time to Buy And Sell Stock' (NeetCode)
    matches 'Best Time To Buy And Sell Stock' (title-cased from a LeetCode slug)."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())

def problem_set_catalog(catalog_df: pd.DataFrame, problem_set: str) -> pd.DataFrame:
    """Catalog rows belonging to a curated list, in curriculum (catalog) order.
    NeetCode 150 is the scraped NeetCode catalog itself; Blind 75 is picked out
    of it by name, with BLIND_75_EXTRAS appended for the problems NeetCode 150
    doesn't carry."""
    if problem_set == "NeetCode 150":
        return catalog_df[catalog_df["source"] == "neetcode"].reset_index(drop=True)
    if problem_set == "Blind 75":
        wanted = {_norm_name(n) for n in BLIND_75}
        picked = catalog_df[catalog_df["name"].map(_norm_name).isin(wanted)]
        have = set(picked["name"].map(_norm_name))
        extras = [
            {"url": url, "name": name, "category": cat, "difficulty": diff, "source": "leetcode"}
            for name, (url, cat, diff) in BLIND_75_EXTRAS.items()
            if _norm_name(name) not in have
        ]
        return pd.concat([picked, pd.DataFrame(extras, columns=catalog_df.columns)], ignore_index=True)
    return catalog_df.reset_index(drop=True)

def hide_reviewed_today(todo: pd.DataFrame, reviewed: set) -> tuple:
    """(visible, hidden_count): drops anything attempted today (Failed, Hint or
    Skip, not just Solved) so it comes back tomorrow, same as Today's Queue
    and Overdue treat reviewed_today()."""
    if todo.empty:
        return todo, 0
    attempted = todo["url"].isin(reviewed)
    return todo[~attempted].reset_index(drop=True), int(attempted.sum())

RANK_POOL = 15

def apply_ranking(todo: pd.DataFrame, ranking: list) -> pd.DataFrame:
    """Reorder `todo` by a stored AI ranking [{"url", "reason"}, ...] and attach
    each reason. Ranked problems that are no longer to-do (solved or attempted
    since) drop out; anything the ranking never saw keeps its list order at the end."""
    if todo.empty or not ranking:
        return todo
    by_url = {r["url"]: r["reason"] for r in ranking}
    order = {r["url"]: i for i, r in enumerate(ranking)}
    out = todo.copy()
    out["reason"] = out["url"].map(by_url).fillna("")
    out["_rank"] = out["url"].map(order).fillna(len(order))
    return out.sort_values("_rank", kind="stable").drop(columns="_rank").reset_index(drop=True)

def ai_rank(todo: pd.DataFrame, set_df: pd.DataFrame, weak_df: pd.DataFrame) -> tuple:
    """(ranking, error): Claude's order for the next RANK_POOL to-do problems,
    as stored-friendly dicts. error is a user-facing message, or None."""
    pool = todo.head(RANK_POOL)
    if pool.empty:
        return [], None
    remaining = todo.groupby("category").size().to_dict()
    totals = set_df.groupby("category").size().to_dict()
    progress = {c: (totals[c] - remaining.get(c, 0), totals[c]) for c in totals}
    candidates = pool[["name", "category", "difficulty"]].to_dict("records")
    weak_rows = weak_df.head(3).to_dict("records") if not weak_df.empty else []
    try:
        ranked = coach.rank_candidates(candidates, weak_rows, progress)
    except coach.CoachError as e:
        return [], str(e)
    urls = list(pool["url"])
    return [{"url": urls[i], "reason": reason} for i, reason in ranked], None

def app_solved_urls() -> set:
    """URLs you explicitly clicked Solved on in this app."""
    with db() as con:
        rows = con.execute("SELECT DISTINCT problem_url FROM review_history WHERE result='Solved'").fetchall()
    return {r[0] for r in rows}

def next_in_set(set_df: pd.DataFrame, problems_df: pd.DataFrame, app_solved: frozenset = frozenset()) -> tuple:
    """(todo, done_count) for a curated list, todo in list order. NeetCode
    problems are done when their checkbox on NeetCode is ticked (catalog.completed,
    refreshed by tracker.py) or you clicked Solved on them here -- but not merely
    because of passive solve history (e.g. LeetCode submissions). Rows from elsewhere
    (e.g. Blind 75's LeetCode-only extras) have no checkbox, so they fall back to
    a recorded solve, matched by URL or normalized name."""
    if set_df.empty:
        return set_df, 0
    solved = problems_df[(problems_df["solved_count"] > 0) | problems_df["last_accepted"].notna()]
    solved_by_history = set_df["url"].isin(set(solved["url"])) | set_df["name"].map(_norm_name).isin(
        {_norm_name(n) for n in solved["name"]}
    )
    checked = set_df["completed"].fillna(0).astype(int) == 1 if "completed" in set_df else False
    is_done = (
        (set_df["source"] == "neetcode") & (checked | set_df["url"].isin(app_solved))
        | (set_df["source"] != "neetcode") & solved_by_history
    )
    return set_df[~is_done].reset_index(drop=True), int(is_done.sum())

# ── UI helpers ────────────────────────────────────────────────────────────────

def show_dataframe(data: pd.DataFrame, **kwargs):
    """st.dataframe wrapper that degrades gracefully on a Streamlit older
    than 1.19 (hide_index) / 1.18 (use_container_width) -- this app has
    been run against a global interpreter's ancient Streamlit shadowing the
    project's own pinned venv (see requirements.txt), which otherwise
    crashes the whole script with a bare TypeError."""
    try:
        st.dataframe(data, **kwargs)
    except TypeError:
        if kwargs.pop("hide_index", False):
            data = data.reset_index(drop=True)
        kwargs.pop("use_container_width", None)
        st.dataframe(data, **kwargs)

BADGE = {
    "Solved": ("✅", "#2e7d32"),
    "Hint":   ("💡", "#e65100"),
    "Failed": ("❌", "#c62828"),
    "Skip":   ("⏭",  "#757575"),
}

DIFFICULTY_COLOR = {
    "Easy":   "#2e7d32",
    "Medium": "#e65100",
    "Hard":   "#c62828",
}

def difficulty_badge_html(difficulty) -> str:
    if not difficulty or pd.isna(difficulty):
        return ""
    color = DIFFICULTY_COLOR.get(difficulty, "#555")
    return (
        f'<span style="background:{color};color:white;padding:1px 8px;'
        f'border-radius:10px;font-size:0.8em">{difficulty}</span>'
    )

def render_card(row, key_suffix: str, reason: str = ""):
    url       = row["url"]
    name      = row["name"]
    cat       = row["category"]
    starred   = row.get("starred", 0)
    source    = row.get("source", "neetcode")
    solved    = int(row.get("solved_count",   0))
    struggled = int(row.get("struggle_count", 0))
    last_res  = row.get("last_result") or ""
    last_rev  = str(row.get("last_reviewed") or row.get("last_accepted") or "—")[:10]

    star = "⭐ " if starred else ""
    emoji, color = BADGE.get(last_res, ("", "#555"))
    badge_html = (
        f'<span style="background:{color};color:white;padding:1px 8px;'
        f'border-radius:10px;font-size:0.8em">{emoji} {last_res}</span>'
    ) if last_res else ""
    source_html = (
        '<span style="background:#333;color:white;padding:1px 8px;'
        'border-radius:10px;font-size:0.8em">LeetCode-only</span>'
    ) if source == "leetcode" else ""
    diff_html = difficulty_badge_html(row.get("difficulty"))

    st.markdown(
        f"**{star}{name}** &nbsp; "
        f'<code style="font-size:0.8em">{cat}</code> &nbsp; {diff_html} &nbsp; {badge_html} &nbsp; {source_html}',
        unsafe_allow_html=True,
    )
    if reason:
        st.caption(f"💡 {reason}")
    st.caption(f"Last seen: {last_rev} · Solved {solved}× · Struggled {struggled}×")
    coach_note = latest_coach_note(url)
    if coach_note:
        st.info(f"🧑‍🏫 **Coach ({coach_note[0]}):** {coach_note[1]}")
    link_label = "Open on LeetCode ↗" if source == "leetcode" else "Open in NeetCode ↗"
    st.markdown(f"[{link_label}]({url})")

    with st.form(key=f"form_{key_suffix}_{url}"):
        notes = st.text_input("Notes", placeholder="What tripped you up? (optional)")
        c1, c2, c3, c4, c5 = st.columns(5)
        solved_btn = c1.form_submit_button("✅ Solved")
        hint_btn   = c2.form_submit_button("💡 Hint")
        failed_btn = c3.form_submit_button("❌ Failed")
        skip_btn   = c4.form_submit_button("⏭ Skip")
        star_btn   = c5.form_submit_button("☆ Unstar" if starred else "⭐ Star")

    if star_btn:
        set_starred(url, name, cat, source, not starred)
        st.rerun()

    if solved_btn or hint_btn or failed_btn or skip_btn:
        # Cards can come from `catalog` (a recommended problem never before
        # tracked) as well as `problems` -- ensure_tracked is a no-op for
        # anything already tracked, so this is safe unconditionally.
        ensure_tracked(url, name, cat, source)
    if solved_btn:
        record_review(url, "Solved", notes)
        st.rerun()
    for clicked, result in ((hint_btn, "Hint"), (failed_btn, "Failed")):
        if clicked:
            record_review(url, result, notes)
            # The note is the signal worth sending; skip the API call without one.
            if notes.strip():
                with st.spinner("Coach is reviewing your attempt…"):
                    err = run_coach(url, name, cat, row.get("difficulty"), result, notes)
                if err:
                    st.session_state["coach_error"] = err
            st.rerun()
    if skip_btn:
        record_review(url, "Skip", notes)
        st.rerun()

    st.markdown("---")

# ── App ───────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="NeetCode Review", page_icon="🧠", layout="wide")
    init_db()

    df   = load_problems()
    done = reviewed_today()
    weak_df  = weak_categories(df)
    weak_top = set(weak_df["category"].head(3))  # only tag the worst few, not every weak-ish category

    def weak_reason(category: str) -> str:
        return "One of your weaker patterns by struggle rate" if category in weak_top else ""

    st.title("🧠 NeetCode Spaced Repetition")
    if coach_error := st.session_state.pop("coach_error", None):
        st.warning(f"Coach unavailable: {coach_error}")
    st.caption(f"{len(df)} problems tracked · {len(done)} reviewed today")

    tab_queue, tab_recommend, tab_overdue, tab_browse, tab_stats = st.tabs([
        "📋 Today's Queue", "🎯 Recommended", "🚨 Overdue", "📂 Browse by Pattern", "📊 Statistics",
    ])

    # ── Today's Queue ────────────────────────────────────────────────────────
    with tab_queue:
        c1, c2 = st.columns(2)
        capacity = c1.select_slider("Daily review capacity", options=CAPACITY_OPTIONS, value=5)
        max_per_cat_pct = c2.select_slider(
            "Max per category (% of capacity)",
            options=MAX_PER_CATEGORY_PCT_OPTIONS, value=DEFAULT_MAX_PER_CATEGORY_PCT,
        )
        queue = build_queue(df, capacity, done, max_per_cat_frac=max_per_cat_pct / 100)

        if queue.empty:
            st.success("🎉 You've completed today's review queue!")
        else:
            st.write(f"**{len(queue)} problems queued** (capacity: {capacity})")
            for _, row in queue.iterrows():
                render_card(row, "q", reason=weak_reason(row["category"]))

    # ── Recommended ──────────────────────────────────────────────────────────
    with tab_recommend:
        st.subheader("🆕 New problems to try")
        catalog_df = load_catalog()
        if catalog_df.empty:
            st.info(
                "No catalog data yet — run tracker.py to scrape NeetCode's full practice "
                "catalog (not just problems you've solved), then reload."
            )
        else:
            problem_set = st.radio("Problem set", PROBLEM_SETS, horizontal=True)
            if problem_set == "All":
                new_recs = recommend_new_problems(catalog_df, df, weak_df)
                if new_recs.empty:
                    st.success("No gaps found — every catalogued problem is already tracked!")
                else:
                    st.write(f"**{len(new_recs)} suggestions**, prioritizing untouched and weak patterns")
                    for _, row in new_recs.iterrows():
                        render_card(row, "rec", reason=row.get("reason", ""))
            else:
                set_df = problem_set_catalog(catalog_df, problem_set)
                todo, done_n = next_in_set(set_df, df, app_solved_urls())
                todo, hidden_n = hide_reviewed_today(todo, done)

                rank_key = f"ai_rank_{problem_set}"
                if st.button("✨ Rank with AI", help="Order the next problems by your weak spots and prerequisites"):
                    with st.spinner("Claude is ranking your next problems…"):
                        ranking, err = ai_rank(todo, set_df, weak_df)
                    if err:
                        st.warning(f"AI ranking unavailable: {err}")
                    else:
                        st.session_state[rank_key] = ranking
                if st.session_state.get(rank_key):
                    todo = apply_ranking(todo, st.session_state[rank_key])
                st.progress(done_n / len(set_df) if len(set_df) else 0.0)
                st.caption(f"{done_n} of {len(set_df)} {problem_set} problems solved")
                if hidden_n:
                    st.caption(f"{hidden_n} attempted today — back tomorrow")
                if todo.empty:
                    if hidden_n:
                        st.success("Nothing left for today — attempted problems return tomorrow.")
                    else:
                        st.success(f"🎉 You've solved every {problem_set} problem!")
                else:
                    # Same widget-count concern as the overdue tab: each card is a
                    # full form, so only render the next few, in list order.
                    show_n = st.select_slider("Show next N", options=SET_DISPLAY_OPTIONS, value=5)
                    st.write(f"**Next up in {problem_set}** — {len(todo)} to go")
                    for _, row in todo.head(show_n).iterrows():
                        render_card(row, f"set_{problem_set}", reason=row.get("reason", ""))

        st.subheader("📉 Your weakest patterns")
        if weak_df.empty:
            st.info(f"Not enough review history yet (need ≥{MIN_ATTEMPTS_FOR_WEAKNESS} attempts in a pattern).")
        else:
            display = weak_df.copy()
            display["struggle rate"] = (display["weakness"] * 100).round(0).astype(int).astype(str) + "%"
            show_dataframe(
                display[["category", "attempts", "struggle rate"]],
                use_container_width=True, hide_index=True,
            )

    # ── Overdue ──────────────────────────────────────────────────────────────
    with tab_overdue:
        candidates = df[~df["url"].isin(done)].copy()
        if not candidates.empty:
            candidates["score"] = candidates.apply(score_row, axis=1)
            overdue = candidates[candidates["score"] >= 20].sort_values("score", ascending=False)
        else:
            overdue = pd.DataFrame()

        if overdue.empty:
            st.info("No overdue problems — you're on top of things!")
        else:
            # Rendering every overdue row unbounded (one full form + 4
            # buttons each) can mean hundreds of widgets on a well-used
            # tracker -- easily enough to freeze the browser tab. Cap what's
            # actually rendered and let the worst-scoring ones surface first.
            show_n = st.select_slider(
                "Show top N overdue", options=OVERDUE_DISPLAY_OPTIONS, value=20
            )
            st.write(f"**{len(overdue)} overdue problems** — showing the {min(show_n, len(overdue))} most urgent")
            for _, row in overdue.head(show_n).iterrows():
                render_card(row, "od", reason=weak_reason(row["category"]))

    # ── Browse by Pattern ────────────────────────────────────────────────────
    with tab_browse:
        search = st.text_input("🔍 Search", placeholder="problem name or category…")
        diffs_present = [d for d in ["Easy", "Medium", "Hard"] if d in df["difficulty"].unique()]
        difficulty_filter = st.multiselect("Difficulty", diffs_present, default=diffs_present)

        filtered = (
            df[
                df["name"].str.contains(search, case=False, na=False)
                | df["category"].str.contains(search, case=False, na=False)
            ]
            if search else df
        )
        if difficulty_filter:
            filtered = filtered[
                filtered["difficulty"].isin(difficulty_filter) | filtered["difficulty"].isna()
            ]

        for cat in sorted(filtered["category"].unique()):
            cat_df = filtered[filtered["category"] == cat]
            with st.expander(f"{cat}  ({len(cat_df)})"):
                for _, row in cat_df.iterrows():
                    last_res = row.get("last_result") or ""
                    last_rev = str(row.get("last_reviewed") or row.get("last_accepted") or "—")[:10]
                    star     = "⭐ " if row["starred"] else ""
                    emoji    = BADGE.get(last_res, ("",))[0]
                    source_tag = "🔶 " if row.get("source") == "leetcode" else ""
                    diff_html  = difficulty_badge_html(row.get("difficulty"))
                    st.markdown(
                        f"{star}{source_tag}[{row['name']}]({row['url']})  "
                        f"&nbsp; {diff_html} &nbsp; {emoji} `{last_res or 'not reviewed'}` &nbsp; `{last_rev}`",
                        unsafe_allow_html=True,
                    )

    # ── Statistics ───────────────────────────────────────────────────────────
    with tab_stats:
        today_str = date.today().isoformat()
        week_ago  = (date.today() - timedelta(days=7)).isoformat()

        with db() as con:
            today_n = con.execute(
                "SELECT COUNT(*) FROM review_history WHERE date(reviewed_at, 'localtime')=?", (today_str,)
            ).fetchone()[0]
            week_n  = con.execute(
                "SELECT COUNT(*) FROM review_history WHERE date(reviewed_at, 'localtime')>=?", (week_ago,)
            ).fetchone()[0]
            total_n = con.execute("SELECT COUNT(*) FROM review_history").fetchone()[0]

        # Distinct problems ever solved, combining accepted_dates (LeetCode/
        # NeetCode's own history, independent of this app) with in-app
        # Solved clicks -- see load_problems()'s solved_count. Deliberately
        # separate from today_n/week_n/total_n above, which count in-app
        # review *activity*, not lifetime solves; conflating the two would
        # make "reviewed" claim credit for solves that happened long before
        # this app existed.
        lifetime_solves = int((df["solved_count"] > 0).sum())

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Reviewed today",  today_n)
        c2.metric("This week",       week_n)
        c3.metric("Reviews all-time", total_n)
        c4.metric("Lifetime solves", lifetime_solves)

        st.subheader("Avg days since last review — by pattern")
        df2          = df.copy()
        df2["ref"]   = df2.apply(
            lambda r: parse_date(r["last_reviewed"]) or parse_date(r["last_accepted"]), axis=1
        )
        df2["days"]  = df2["ref"].apply(
            lambda d: (date.today() - d).days if d else None
        )
        pattern_avg  = (
            df2.groupby("category")["days"]
            .mean()
            .dropna()
            .sort_values(ascending=False)
            .round(1)
            .rename("avg days since last review")
        )
        st.bar_chart(pattern_avg)

        st.subheader("Outcome breakdown")
        hist = recent_history(200)
        if not hist.empty:
            counts = hist["result"].value_counts().rename("count")
            st.bar_chart(counts)

        st.subheader("Recent activity (last 30)")
        hist30 = recent_history(30)
        if hist30.empty:
            st.info("No review history yet — complete some problems to see activity here.")
        else:
            hist30["reviewed_at"] = hist30["reviewed_at"].str[:16]
            show_dataframe(hist30, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
