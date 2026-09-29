"""Coding Problem Spaced Repetition Dashboard — run: streamlit run dashboard.py"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

DESKTOP   = Path(__file__).parent
DB_PATH   = DESKTOP / "problems.db"
CAPACITY_OPTIONS = [3, 5, 8, 12, 20]
OVERDUE_DISPLAY_OPTIONS = [10, 20, 30, 50, 100]
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
    columns = ["url", "name", "category", "difficulty", "source"]
    with db() as con:
        try:
            return pd.read_sql_query(f"SELECT {', '.join(columns)} FROM catalog", con)
        except (sqlite3.OperationalError, pd.errors.DatabaseError):
            return pd.DataFrame(columns=columns)

def reviewed_today() -> set:
    today = date.today().isoformat()
    with db() as con:
        rows = con.execute(
            "SELECT DISTINCT problem_url FROM review_history WHERE date(reviewed_at)=?",
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

def record_review(url: str, result: str, notes: str = ""):
    with db() as con:
        con.execute(
            "INSERT INTO review_history (problem_url, reviewed_at, result, notes) VALUES (?,?,?,?)",
            (url, datetime.utcnow().isoformat(), result, notes),
        )

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

def score_row(row) -> float:
    ref = parse_date(row.get("last_reviewed")) or parse_date(row.get("last_accepted"))
    age = (date.today() - ref).days if ref else 999

    s = float(age) * 2.0

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
    link_label = "Open on LeetCode ↗" if source == "leetcode" else "Open in NeetCode ↗"
    st.markdown(f"[{link_label}]({url})")

    with st.form(key=f"form_{key_suffix}_{url}"):
        notes = st.text_input("Notes", placeholder="What tripped you up? (optional)")
        c1, c2, c3, c4 = st.columns(4)
        solved_btn = c1.form_submit_button("✅ Solved")
        hint_btn   = c2.form_submit_button("💡 Hint")
        failed_btn = c3.form_submit_button("❌ Failed")
        skip_btn   = c4.form_submit_button("⏭ Skip")

    if solved_btn or hint_btn or failed_btn or skip_btn:
        # Cards can come from `catalog` (a recommended problem never before
        # tracked) as well as `problems` -- ensure_tracked is a no-op for
        # anything already tracked, so this is safe unconditionally.
        ensure_tracked(url, name, cat, source)
    if solved_btn:
        record_review(url, "Solved", notes)
        st.experimental_rerun()
    if hint_btn:
        record_review(url, "Hint", notes)
        st.experimental_rerun()
    if failed_btn:
        record_review(url, "Failed", notes)
        st.experimental_rerun()
    if skip_btn:
        record_review(url, "Skip", notes)
        st.experimental_rerun()

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
            new_recs = recommend_new_problems(catalog_df, df, weak_df)
            if new_recs.empty:
                st.success("No gaps found — every catalogued problem is already tracked!")
            else:
                st.write(f"**{len(new_recs)} suggestions**, prioritizing untouched and weak patterns")
                for _, row in new_recs.iterrows():
                    render_card(row, "rec", reason=row.get("reason", ""))

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
                "SELECT COUNT(*) FROM review_history WHERE date(reviewed_at)=?", (today_str,)
            ).fetchone()[0]
            week_n  = con.execute(
                "SELECT COUNT(*) FROM review_history WHERE date(reviewed_at)>=?", (week_ago,)
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
