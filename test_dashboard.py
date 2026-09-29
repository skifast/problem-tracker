"""Run: python3 -m unittest test_dashboard

Requires the dashboard's real dependencies (pandas/streamlit) since it
imports dashboard directly -- see requirements.txt.
"""

import math
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import dashboard as dash
import tracker as t


def make_row(**overrides):
    """A row shaped like what load_problems()/build_queue() produce, with
    sane defaults so each test only needs to set the fields it cares about."""
    row = {
        "url": "https://neetcode.io/problems/example",
        "name": "Example Problem",
        "category": "Arrays & Hashing",
        "starred": 0,
        "last_accepted": None,
        "solved_count": 0,
        "struggle_count": 0,
        "last_result": None,
        "last_reviewed": None,
    }
    row.update(overrides)
    return row


class TestParseDate(unittest.TestCase):
    def test_mdy_format(self):
        self.assertEqual(dash.parse_date("07/12/2026"), date(2026, 7, 12))

    def test_iso_format(self):
        self.assertEqual(dash.parse_date("2026-07-12"), date(2026, 7, 12))

    def test_full_isoformat_timestamp_is_truncated_to_date(self):
        self.assertEqual(dash.parse_date("2026-07-12T21:41:28.474184+00:00"), date(2026, 7, 12))

    def test_none_returns_none(self):
        self.assertIsNone(dash.parse_date(None))

    def test_nan_float_returns_none(self):
        self.assertIsNone(dash.parse_date(float("nan")))

    def test_string_nan_returns_none(self):
        self.assertIsNone(dash.parse_date("nan"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(dash.parse_date(""))

    def test_unparseable_string_returns_none(self):
        self.assertIsNone(dash.parse_date("not a date"))


class TestScoreRow(unittest.TestCase):
    def test_never_reviewed_uses_default_age_of_999(self):
        row = make_row()
        self.assertEqual(dash.score_row(row), 999 * 2.0)

    def test_reviewed_today_has_zero_age_component(self):
        row = make_row(last_reviewed=date.today().isoformat())
        self.assertEqual(dash.score_row(row), 0.0)

    def test_last_reviewed_takes_priority_over_last_accepted(self):
        # last_reviewed = today, last_accepted = long ago -- should use today.
        row = make_row(
            last_reviewed=date.today().isoformat(),
            last_accepted=(date.today() - timedelta(days=100)).strftime("%m/%d/%Y"),
        )
        self.assertEqual(dash.score_row(row), 0.0)

    def test_falls_back_to_last_accepted_when_never_reviewed_in_app(self):
        ten_days_ago = (date.today() - timedelta(days=10)).strftime("%m/%d/%Y")
        row = make_row(last_accepted=ten_days_ago)
        self.assertEqual(dash.score_row(row), 10 * 2.0)

    def test_age_scales_linearly(self):
        thirty_days_ago = (date.today() - timedelta(days=30)).isoformat()
        row = make_row(last_reviewed=thirty_days_ago)
        self.assertEqual(dash.score_row(row), 30 * 2.0)

    def test_starred_adds_flat_bonus(self):
        base = make_row(last_reviewed=date.today().isoformat())
        starred = make_row(last_reviewed=date.today().isoformat(), starred=1)
        self.assertEqual(dash.score_row(starred) - dash.score_row(base), 10)

    def test_failed_adds_twenty(self):
        row = make_row(last_reviewed=date.today().isoformat(), last_result="Failed")
        self.assertEqual(dash.score_row(row), 20)

    def test_hint_adds_ten(self):
        row = make_row(last_reviewed=date.today().isoformat(), last_result="Hint")
        self.assertEqual(dash.score_row(row), 10)

    def test_solved_result_no_longer_double_discounts_solved_count(self):
        # Regression test: score_row previously subtracted for solved_count
        # twice when last_result == "Solved" (once at 3x/cap15 inside the
        # Solved branch, once more unconditionally at 2x/cap10). Fixed to a
        # single, unconditional discount -- so "last review was Solved"
        # should score identically to any other last_result, given the same
        # solved_count.
        solved_last = make_row(
            last_reviewed=date.today().isoformat(), last_result="Solved", solved_count=3
        )
        unspecified_last = make_row(
            last_reviewed=date.today().isoformat(), last_result=None, solved_count=3
        )
        self.assertEqual(dash.score_row(solved_last), dash.score_row(unspecified_last))

    def test_solved_count_discount_caps_at_ten(self):
        row = make_row(last_reviewed=date.today().isoformat(), solved_count=100)
        self.assertEqual(dash.score_row(row), -10)

    def test_solved_count_discount_below_cap(self):
        row = make_row(last_reviewed=date.today().isoformat(), solved_count=2)
        self.assertEqual(dash.score_row(row), -4)  # min(2*2, 10) == 4

    def test_struggle_count_adds_five_each_uncapped(self):
        row = make_row(last_reviewed=date.today().isoformat(), struggle_count=4)
        self.assertEqual(dash.score_row(row), 20)  # 4 * 5, no cap

    def test_all_signals_combine_additively(self):
        row = make_row(
            last_reviewed=date.today().isoformat(),
            starred=1,
            last_result="Hint",
            solved_count=1,
            struggle_count=2,
        )
        # age=0 -> 0; +10 starred; +10 hint; -2 solved discount; +10 struggle
        self.assertEqual(dash.score_row(row), 0 + 10 + 10 - 2 + 10)


def make_catalog_row(**overrides):
    """A row shaped like what load_catalog() produces."""
    row = {
        "url": "https://neetcode.io/problems/example",
        "name": "Example Problem",
        "category": "Arrays & Hashing",
        "difficulty": "Easy",
        "source": "neetcode",
    }
    row.update(overrides)
    return row


class TestWeakCategories(unittest.TestCase):
    def _df(self, rows):
        return pd.DataFrame([make_row(**r) for r in rows])

    def test_no_attempted_problems_returns_empty(self):
        df = self._df([{"url": "u0", "category": "Trees"}])
        self.assertTrue(dash.weak_categories(df).empty)

    def test_category_below_min_attempts_is_dropped(self):
        # 2 attempts total, one below MIN_ATTEMPTS_FOR_WEAKNESS (3).
        df = self._df([
            {"url": "u0", "category": "Trees", "solved_count": 1},
            {"url": "u1", "category": "Trees", "struggle_count": 1},
        ])
        self.assertTrue(dash.weak_categories(df).empty)

    def test_ranks_by_struggle_rate_descending(self):
        df = self._df([
            # Trees: 1 solved, 2 struggled -> weakness 2/3
            {"url": "t0", "category": "Trees", "solved_count": 1},
            {"url": "t1", "category": "Trees", "struggle_count": 2},
            # Arrays: 2 solved, 1 struggled -> weakness 1/3
            {"url": "a0", "category": "Arrays", "solved_count": 2},
            {"url": "a1", "category": "Arrays", "struggle_count": 1},
        ])
        result = dash.weak_categories(df)
        self.assertEqual(list(result["category"]), ["Trees", "Arrays"])
        self.assertAlmostEqual(result.iloc[0]["weakness"], 2 / 3)
        self.assertAlmostEqual(result.iloc[1]["weakness"], 1 / 3)

    def test_zero_struggle_rate_category_is_excluded(self):
        # 3 solved, 0 struggled -- meets the attempt floor but was never
        # actually struggled with, so it shouldn't be reported as "weak".
        df = self._df([{"url": f"g{i}", "category": "Graphs", "solved_count": 1} for i in range(3)])
        self.assertTrue(dash.weak_categories(df).empty)


class TestRecommendNewProblems(unittest.TestCase):
    def _catalog(self, rows):
        return pd.DataFrame([make_catalog_row(**r) for r in rows])

    def _problems(self, rows):
        return pd.DataFrame([make_row(**r) for r in rows]) if rows else pd.DataFrame(
            columns=["url", "category"]
        )

    def _weak(self, rows):
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["category", "attempts", "weakness"])

    def test_empty_catalog_returns_empty(self):
        result = dash.recommend_new_problems(self._catalog([]), self._problems([]), self._weak([]))
        self.assertTrue(result.empty)

    def test_already_tracked_urls_are_excluded(self):
        catalog = self._catalog([{"url": "u0", "category": "Trees"}])
        problems = self._problems([{"url": "u0", "category": "Trees"}])
        result = dash.recommend_new_problems(catalog, problems, self._weak([]))
        self.assertTrue(result.empty)

    def test_categories_outside_gaps_and_weak_are_excluded(self):
        # "Trees" has been attempted and isn't in weak_df -> not a candidate.
        catalog = self._catalog([{"url": "u0", "category": "Trees"}])
        problems = self._problems([{"url": "already_done", "category": "Trees"}])
        result = dash.recommend_new_problems(catalog, problems, self._weak([]))
        self.assertTrue(result.empty)

    def test_coverage_gap_category_is_recommended(self):
        catalog = self._catalog([{"url": "u0", "category": "Tries"}])
        problems = self._problems([{"url": "other", "category": "Trees"}])
        result = dash.recommend_new_problems(catalog, problems, self._weak([]))
        self.assertEqual(list(result["url"]), ["u0"])
        self.assertIn("No attempts tracked", result.iloc[0]["reason"])

    def test_weak_category_is_recommended_with_different_reason(self):
        catalog = self._catalog([{"url": "u0", "category": "Trees"}])
        problems = self._problems([{"url": "already_done", "category": "Trees"}])
        weak = self._weak([{"category": "Trees", "attempts": 5, "weakness": 0.6}])
        result = dash.recommend_new_problems(catalog, problems, weak)
        self.assertEqual(list(result["url"]), ["u0"])
        self.assertIn("weaker pattern", result.iloc[0]["reason"])

    def test_gap_categories_prioritized_before_weak_categories(self):
        catalog = self._catalog([
            {"url": "weak0", "category": "Trees"},
            {"url": "gap0", "category": "Tries"},
        ])
        problems = self._problems([{"url": "already_done", "category": "Trees"}])
        weak = self._weak([{"category": "Trees", "attempts": 5, "weakness": 0.6}])
        result = dash.recommend_new_problems(catalog, problems, weak)
        self.assertEqual(list(result["url"]), ["gap0", "weak0"])

    def test_easier_difficulty_recommended_before_harder_in_same_category(self):
        catalog = self._catalog([
            {"url": "hard0", "category": "Tries", "difficulty": "Hard"},
            {"url": "easy0", "category": "Tries", "difficulty": "Easy"},
        ])
        result = dash.recommend_new_problems(catalog, self._problems([]), self._weak([]))
        self.assertEqual(list(result["url"]), ["easy0", "hard0"])

    def test_per_category_cap_limits_recommendations(self):
        catalog = self._catalog([
            {"url": f"u{i}", "category": "Tries"} for i in range(5)
        ])
        result = dash.recommend_new_problems(
            catalog, self._problems([]), self._weak([]), per_category=2
        )
        self.assertEqual(len(result), 2)


class TestBuildQueue(unittest.TestCase):
    def _df(self, rows):
        return pd.DataFrame([make_row(**r) for r in rows])

    def test_respects_capacity(self):
        rows = [{"url": f"u{i}", "category": "Cat"} for i in range(10)]
        df = self._df(rows)
        queue = dash.build_queue(df, capacity=3, done=set())
        self.assertEqual(len(queue), 3)

    def test_excludes_already_done_urls(self):
        rows = [{"url": f"u{i}", "category": "Cat"} for i in range(5)]
        df = self._df(rows)
        queue = dash.build_queue(df, capacity=5, done={"u0", "u1"})
        self.assertNotIn("u0", queue["url"].values)
        self.assertNotIn("u1", queue["url"].values)
        self.assertEqual(len(queue), 3)

    def test_empty_candidates_returns_empty(self):
        rows = [{"url": "u0", "category": "Cat"}]
        df = self._df(rows)
        queue = dash.build_queue(df, capacity=5, done={"u0"})
        self.assertTrue(queue.empty)

    def test_category_cap_limits_first_pass_selection(self):
        # capacity=5 -> max_per_cat = max(1, round(5*0.4)) = 2.
        # 10 candidates all in one high-scoring category, 1 in another.
        # The single-category cap should stop the first pass at 2 from
        # "Trees" before backfill kicks in.
        overdue = (date.today() - timedelta(days=100)).isoformat()
        rows = [
            {"url": f"trees{i}", "category": "Trees", "last_reviewed": overdue}
            for i in range(10)
        ]
        rows.append({"url": "other0", "category": "Graphs", "last_reviewed": overdue})
        df = self._df(rows)

        queue = dash.build_queue(df, capacity=5, done=set())
        self.assertEqual(len(queue), 5)

        # Backfill (second, uncapped pass) makes up the remaining 3 slots,
        # so more than max_per_cat Trees problems can still end up in the
        # queue once there aren't enough other categories to fill capacity.
        trees_in_queue = (queue["category"] == "Trees").sum()
        self.assertGreaterEqual(trees_in_queue, 2)
        self.assertIn("other0", queue["url"].values)

    def test_higher_score_rows_preferred_within_capacity(self):
        recent = date.today().isoformat()
        old = (date.today() - timedelta(days=200)).isoformat()
        rows = [
            {"url": "high_urgency", "category": "A", "last_reviewed": old, "last_result": "Failed"},
            {"url": "low_urgency", "category": "B", "last_reviewed": recent},
        ]
        df = self._df(rows)
        queue = dash.build_queue(df, capacity=1, done=set())
        self.assertEqual(list(queue["url"]), ["high_urgency"])

    def test_max_per_cat_frac_is_configurable(self):
        # Same setup as test_category_cap_limits_first_pass_selection, but
        # with the cap fraction turned all the way up -- max_per_cat becomes
        # capacity itself, so the first pass alone can fill the queue from
        # a single category (no need for backfill to reach 5).
        overdue = (date.today() - timedelta(days=100)).isoformat()
        rows = [
            {"url": f"trees{i}", "category": "Trees", "last_reviewed": overdue}
            for i in range(10)
        ]
        rows.append({"url": "other0", "category": "Graphs", "last_reviewed": overdue})
        df = self._df(rows)

        queue = dash.build_queue(df, capacity=5, done=set(), max_per_cat_frac=1.0)
        self.assertEqual(len(queue), 5)
        self.assertEqual((queue["category"] == "Trees").sum(), 5)

    def test_max_per_cat_frac_default_matches_prior_hardcoded_40_percent(self):
        # Guards against silently changing behavior for existing callers
        # that don't pass max_per_cat_frac.
        overdue = (date.today() - timedelta(days=100)).isoformat()
        rows = [
            {"url": f"trees{i}", "category": "Trees", "last_reviewed": overdue}
            for i in range(10)
        ]
        rows.append({"url": "other0", "category": "Graphs", "last_reviewed": overdue})
        df = self._df(rows)

        default_queue = dash.build_queue(df, capacity=5, done=set())
        explicit_queue = dash.build_queue(df, capacity=5, done=set(), max_per_cat_frac=0.4)
        self.assertEqual(sorted(default_queue["url"]), sorted(explicit_queue["url"]))


class TestLoadProblemsSolvedCount(unittest.TestCase):
    """Integration-level: load_problems() runs real SQL against a real
    (temp) SQLite file, unlike the rest of this file which operates on
    pre-shaped DataFrames -- the combined accepted_dates + review_history
    solved_count logic is exactly the kind of thing that's easy to get
    subtly wrong in the query itself."""

    URL = "https://leetcode.com/problems/two-sum/"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._orig_tracker_db_path = t.DB_PATH
        self._orig_dash_db_path = dash.DB_PATH
        t.DB_PATH = self.db_path
        dash.DB_PATH = self.db_path
        t.init_db()  # owns problems/catalog/accepted_dates schema
        dash.init_db()  # adds review_history on top, per its own docstring

    def tearDown(self):
        t.DB_PATH = self._orig_tracker_db_path
        dash.DB_PATH = self._orig_dash_db_path
        self.tmpdir.cleanup()

    def _con(self):
        return sqlite3.connect(str(self.db_path))

    def _seed_problem(self):
        now = datetime.now().isoformat()
        con = self._con()
        con.execute("""
            INSERT INTO problems (url, name, category, starred, last_accepted, first_seen, last_seen, source)
            VALUES (?, 'Two Sum', 'Arrays & Hashing', 0, NULL, ?, ?, 'leetcode')
        """, (self.URL, now, now))
        con.commit()
        con.close()

    def _row_for_url(self, df):
        return df[df["url"] == self.URL].iloc[0]

    def test_platform_accepted_date_counts_even_with_zero_app_reviews(self):
        # The exact bug report this fixes: solved on LeetCode/NeetCode,
        # never clicked "Solved" in the app -- should not show 0.
        self._seed_problem()
        con = self._con()
        con.execute(
            "INSERT INTO accepted_dates (problem_url, accepted_date) VALUES (?, '2026-06-30')",
            (self.URL,),
        )
        con.commit()
        con.close()

        df = dash.load_problems()
        self.assertEqual(self._row_for_url(df)["solved_count"], 1)

    def test_app_solved_click_counts_immediately_without_a_platform_date(self):
        self._seed_problem()
        dash.record_review(self.URL, "Solved", "")

        df = dash.load_problems()
        self.assertEqual(self._row_for_url(df)["solved_count"], 1)

    def test_same_calendar_date_from_both_sources_is_not_double_counted(self):
        self._seed_problem()
        # record_review() timestamps with datetime.utcnow(), not local time --
        # use the same clock here, or this flakes whenever local time and UTC
        # fall on different calendar dates (any evening in a negative-UTC-offset
        # timezone).
        today_iso = datetime.utcnow().date().isoformat()
        con = self._con()
        con.execute(
            "INSERT INTO accepted_dates (problem_url, accepted_date) VALUES (?, ?)",
            (self.URL, today_iso),
        )
        con.commit()
        con.close()
        dash.record_review(self.URL, "Solved", "")  # same day as the accepted_dates row above

        df = dash.load_problems()
        self.assertEqual(self._row_for_url(df)["solved_count"], 1)

    def test_distinct_dates_from_both_sources_both_count(self):
        self._seed_problem()
        con = self._con()
        con.execute(
            "INSERT INTO accepted_dates (problem_url, accepted_date) VALUES (?, '2026-01-01')",
            (self.URL,),
        )
        con.commit()
        con.close()
        dash.record_review(self.URL, "Solved", "")  # today -- a distinct date from 2026-01-01

        df = dash.load_problems()
        self.assertEqual(self._row_for_url(df)["solved_count"], 2)

    def test_no_solves_from_either_source_is_zero(self):
        self._seed_problem()
        df = dash.load_problems()
        self.assertEqual(self._row_for_url(df)["solved_count"], 0)

    def test_falls_back_to_review_history_alone_without_accepted_dates_table(self):
        self._seed_problem()
        dash.record_review(self.URL, "Solved", "")
        con = self._con()
        con.execute("DROP TABLE accepted_dates")
        con.commit()
        con.close()

        df = dash.load_problems()  # must not raise on the missing table
        self.assertEqual(self._row_for_url(df)["solved_count"], 1)


if __name__ == "__main__":
    unittest.main()
