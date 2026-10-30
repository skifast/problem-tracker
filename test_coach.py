"""Tests for the AI coaching layer. The Anthropic client is faked -- no network."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pandas as pd

import coach
import dashboard as dash
import tracker as t


def fake_client(text="Review hash-map lookups. Drill: redo it with a dict.", stop_reason="end_turn", raises=None):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if raises:
            raise raises
        return SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    client.calls = calls
    return client


def status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


class TestBuildPrompt(unittest.TestCase):
    def test_includes_problem_result_and_note(self):
        p = coach.build_prompt("Two Sum", "Arrays & Hashing", "Easy", "Failed", "forgot the complement", [])
        for expected in ("Two Sum", "Arrays & Hashing", "Easy", "Failed", "forgot the complement"):
            self.assertIn(expected, p)

    def test_history_is_listed_newest_first_and_capped(self):
        history = [{"reviewed_at": f"2026-01-0{i}T00:00", "result": "Hint", "notes": f"n{i}"} for i in range(1, 9)]
        p = coach.build_prompt("X", "C", None, "Hint", "note", history)
        self.assertIn("2026-01-01: Hint -- n1", p)
        self.assertNotIn("n6", p)  # beyond HISTORY_LIMIT

    def test_no_history_section_when_empty(self):
        self.assertNotIn("Earlier attempts", coach.build_prompt("X", "C", "Easy", "Hint", "note", []))


class TestDiagnose(unittest.TestCase):
    def test_returns_text_block_and_skips_thinking(self):
        client = fake_client()
        out = coach.diagnose("Two Sum", "Arrays", "Easy", "Failed", "note", [], client=client)
        self.assertEqual(out, "Review hash-map lookups. Drill: redo it with a dict.")

    def test_request_shape(self):
        client = fake_client()
        coach.diagnose("Two Sum", "Arrays", "Easy", "Failed", "note", [], client=client)
        call = client.calls[0]
        self.assertEqual(call["model"], coach.MODEL)
        self.assertEqual(call["output_config"], {"effort": "low"})
        self.assertNotIn("temperature", call)
        self.assertEqual(call["system"], coach.SYSTEM_PROMPT)

    def test_refusal_becomes_coach_error(self):
        with self.assertRaises(coach.CoachError):
            coach.diagnose("X", "C", "Easy", "Failed", "n", [], client=fake_client(stop_reason="refusal"))

    def test_empty_response_becomes_coach_error(self):
        with self.assertRaises(coach.CoachError):
            coach.diagnose("X", "C", "Easy", "Failed", "n", [], client=fake_client(text=""))

    def test_auth_error_mentions_api_key(self):
        err = status_error(anthropic.AuthenticationError, 401)
        with self.assertRaises(coach.CoachError) as cm:
            coach.diagnose("X", "C", "Easy", "Failed", "n", [], client=fake_client(raises=err))
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))

    def test_server_error_becomes_coach_error(self):
        err = status_error(anthropic.InternalServerError, 500)
        with self.assertRaises(coach.CoachError) as cm:
            coach.diagnose("X", "C", "Easy", "Failed", "n", [], client=fake_client(raises=err))
        self.assertIn("500", str(cm.exception))


class TestCoachStorage(unittest.TestCase):
    URL = "https://neetcode.io/problems/two-integer-sum/question"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._orig = (t.DB_PATH, dash.DB_PATH)
        t.DB_PATH = dash.DB_PATH = self.db_path
        t.init_db()
        dash.init_db()
        dash.ensure_tracked(self.URL, "Two Sum", "Arrays & Hashing")

    def tearDown(self):
        t.DB_PATH, dash.DB_PATH = self._orig
        self.tmpdir.cleanup()

    def test_latest_coach_note_none_before_any(self):
        self.assertIsNone(dash.latest_coach_note(self.URL))

    def test_save_and_latest_returns_newest(self):
        dash.save_coach_note(self.URL, "first")
        dash.save_coach_note(self.URL, "second")
        date_str, text = dash.latest_coach_note(self.URL)
        self.assertEqual(text, "second")
        self.assertEqual(date_str, datetime.utcnow().date().isoformat())

    def test_problem_history_newest_first_and_limited(self):
        for i in range(4):
            dash.record_review(self.URL, "Hint", f"n{i}")
        got = dash.problem_history(self.URL, 2)
        self.assertEqual([h["notes"] for h in got], ["n3", "n2"])

    def test_run_coach_excludes_current_attempt_and_stores_diagnosis(self):
        dash.record_review(self.URL, "Failed", "older")
        dash.record_review(self.URL, "Hint", "current")
        seen = {}
        orig = coach.diagnose
        coach.diagnose = lambda *a, **k: seen.update(history=a[5]) or "stored diagnosis"
        try:
            err = dash.run_coach(self.URL, "Two Sum", "Arrays & Hashing", "Easy", "Hint", "current")
        finally:
            coach.diagnose = orig
        self.assertIsNone(err)
        self.assertEqual([h["notes"] for h in seen["history"]], ["older"])
        self.assertEqual(dash.latest_coach_note(self.URL)[1], "stored diagnosis")

    def test_run_coach_returns_error_and_stores_nothing_on_failure(self):
        orig = coach.diagnose

        def boom(*a, **k):
            raise coach.CoachError("no key")

        coach.diagnose = boom
        try:
            err = dash.run_coach(self.URL, "Two Sum", "Arrays & Hashing", "Easy", "Failed", "n")
        finally:
            coach.diagnose = orig
        self.assertEqual(err, "no key")
        self.assertIsNone(dash.latest_coach_note(self.URL))


class TestWeeklyPrompt(unittest.TestCase):
    REVIEWS = [
        {"reviewed_at": "2026-10-28T10:00", "result": "Failed", "notes": "off by one in window", "name": "Minimum Window Substring", "category": "Sliding Window"},
        {"reviewed_at": "2026-10-27T10:00", "result": "Solved", "notes": "", "name": "Two Sum", "category": "Arrays & Hashing"},
    ]

    def test_prompt_has_counts_weak_patterns_and_notes(self):
        weak = [{"category": "Sliding Window", "attempts": 8, "weakness": 0.625}]
        p = coach.build_weekly_prompt(self.REVIEWS, weak)
        self.assertIn("2 (1 Failed, 1 Solved)", p)
        self.assertIn("Sliding Window: 62% over 8 attempts", p)
        self.assertIn("off by one in window", p)

    def test_prompt_caps_attempt_lines(self):
        many = [dict(self.REVIEWS[0], name=f"P{i}") for i in range(coach.WEEKLY_MAX_REVIEWS + 10)]
        p = coach.build_weekly_prompt(many, [])
        self.assertEqual(p.count("Failed:"), coach.WEEKLY_MAX_REVIEWS)

    def test_weekly_report_uses_medium_effort(self):
        client = fake_client(text="- a")
        self.assertEqual(coach.weekly_report(self.REVIEWS, [], client=client), "- a")
        self.assertEqual(client.calls[0]["output_config"], {"effort": "medium"})
        self.assertEqual(client.calls[0]["system"], coach.WEEKLY_SYSTEM_PROMPT)


def json_client(payload):
    import json
    return fake_client(text=json.dumps(payload))


class TestRankCandidates(unittest.TestCase):
    CANDS = [
        {"name": "A", "category": "Trees", "difficulty": "Easy"},
        {"name": "B", "category": "Graphs", "difficulty": "Medium"},
        {"name": "C", "category": "Trees", "difficulty": "Hard"},
    ]

    def rank(self, order):
        return coach.rank_candidates(self.CANDS, [], {}, client=json_client({"order": order}))

    def test_returns_models_order_with_reasons(self):
        got = self.rank([{"id": 2, "reason": "weak"}, {"id": 0, "reason": "warmup"}, {"id": 1, "reason": "later"}])
        self.assertEqual(got, [(2, "weak"), (0, "warmup"), (1, "later")])

    def test_omitted_candidates_are_appended_in_original_order(self):
        got = self.rank([{"id": 2, "reason": "x"}])
        self.assertEqual([i for i, _ in got], [2, 0, 1])

    def test_unknown_and_repeated_ids_are_dropped(self):
        got = self.rank([{"id": 9, "reason": "?"}, {"id": 1, "reason": "a"}, {"id": 1, "reason": "dup"}, {"id": -1, "reason": "?"}])
        self.assertEqual([i for i, _ in got], [1, 0, 2])
        self.assertEqual(got[0][1], "a")

    def test_unparseable_reply_becomes_coach_error(self):
        with self.assertRaises(coach.CoachError):
            coach.rank_candidates(self.CANDS, [], {}, client=fake_client(text="not json"))

    def test_request_is_schema_constrained(self):
        client = json_client({"order": []})
        coach.rank_candidates(self.CANDS, [], {}, client=client)
        fmt = client.calls[0]["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["schema"], coach.RANK_SCHEMA)
        self.assertEqual(client.calls[0]["output_config"]["effort"], "medium")

    def test_prompt_lists_ids_weak_patterns_and_progress(self):
        p = coach.build_rank_prompt(
            self.CANDS, [{"category": "Trees", "attempts": 6, "weakness": 0.5}], {"Trees": (1, 4)}
        )
        self.assertIn("0: A [Trees, Easy]", p)
        self.assertIn("Trees: 50% over 6 attempts", p)
        self.assertIn("Trees: 1/4", p)


class TestDashboardRanking(unittest.TestCase):
    def todo(self):
        return pd.DataFrame([
            {"url": f"u{i}", "name": n, "category": c, "difficulty": "Easy", "source": "neetcode"}
            for i, (n, c) in enumerate([("A", "Trees"), ("B", "Graphs"), ("C", "Trees")])
        ])

    def test_apply_ranking_reorders_and_attaches_reasons(self):
        out = dash.apply_ranking(self.todo(), [{"url": "u2", "reason": "weak"}, {"url": "u0", "reason": "easy"}])
        self.assertEqual(list(out["url"]), ["u2", "u0", "u1"])
        self.assertEqual(list(out["reason"]), ["weak", "easy", ""])

    def test_apply_ranking_drops_ranked_problems_no_longer_todo(self):
        todo = self.todo().iloc[1:]  # u0 was solved since ranking
        out = dash.apply_ranking(todo, [{"url": "u0", "reason": "x"}, {"url": "u2", "reason": "y"}])
        self.assertEqual(list(out["url"]), ["u2", "u1"])

    def test_apply_ranking_without_ranking_is_unchanged(self):
        self.assertEqual(list(dash.apply_ranking(self.todo(), [])["url"]), ["u0", "u1", "u2"])

    def test_ai_rank_maps_model_indexes_back_to_urls_and_computes_progress(self):
        seen = {}
        orig = coach.rank_candidates

        def fake(cands, weak, progress):
            seen.update(cands=cands, progress=progress)
            return [(1, "weak"), (0, "")]

        coach.rank_candidates = fake
        try:
            set_df = self.todo()                      # 3 in list: 2 Trees, 1 Graphs
            ranking, err = dash.ai_rank(self.todo().iloc[1:], set_df, pd.DataFrame())  # A solved
        finally:
            coach.rank_candidates = orig
        self.assertIsNone(err)
        self.assertEqual(seen["progress"], {"Trees": (1, 2), "Graphs": (0, 1)})
        self.assertEqual(ranking, [{"url": "u2", "reason": "weak"}, {"url": "u1", "reason": ""}])

    def test_ai_rank_returns_error_message_on_coach_failure(self):
        orig = coach.rank_candidates

        def boom(*a, **k):
            raise coach.CoachError("no key")

        coach.rank_candidates = boom
        try:
            ranking, err = dash.ai_rank(self.todo(), self.todo(), pd.DataFrame())
        finally:
            coach.rank_candidates = orig
        self.assertEqual((ranking, err), ([], "no key"))


class TestWeeklyEmail(unittest.TestCase):
    SUNDAY = date(2026, 11, 1)   # weekday() == 6
    MONDAY = date(2026, 11, 2)

    def setUp(self):
        import send_daily_email as sde
        self.sde = sde
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._orig = (t.DB_PATH, dash.DB_PATH, coach.weekly_report, sde.WEEKLY_REPORT_WEEKDAY)
        t.DB_PATH = dash.DB_PATH = self.db_path
        sde.WEEKLY_REPORT_WEEKDAY = 6
        t.init_db()
        dash.init_db()
        self.calls = []
        coach.weekly_report = lambda reviews, weak: self.calls.append(reviews) or "- be careful\n- practice windows"

    def tearDown(self):
        t.DB_PATH, dash.DB_PATH, coach.weekly_report, self.sde.WEEKLY_REPORT_WEEKDAY = self._orig
        self.tmpdir.cleanup()

    def _seed_reviews(self, n):
        url = "https://neetcode.io/problems/x/question"
        dash.ensure_tracked(url, "X", "Trees")
        for _ in range(n):
            dash.record_review(url, "Failed", "note")

    def test_not_report_day_returns_none_without_api_call(self):
        self._seed_reviews(5)
        self.assertIsNone(self.sde.weekly_report_text(self.MONDAY))
        self.assertEqual(self.calls, [])

    def test_too_few_reviews_returns_none_without_api_call(self):
        self._seed_reviews(2)
        self.assertIsNone(self.sde.weekly_report_text(self.SUNDAY))
        self.assertEqual(self.calls, [])

    def test_generates_stores_and_reuses_within_the_week(self):
        self._seed_reviews(4)
        first = self.sde.weekly_report_text(self.SUNDAY)
        second = self.sde.weekly_report_text(self.SUNDAY)
        self.assertEqual(first, "- be careful\n- practice windows")
        self.assertEqual(second, first)
        self.assertEqual(len(self.calls), 1)

    def test_force_ignores_weekday(self):
        self._seed_reviews(4)
        self.assertIsNotNone(self.sde.weekly_report_text(self.MONDAY, force=True))

    def test_coach_failure_skips_report_and_stores_nothing(self):
        self._seed_reviews(4)

        def boom(reviews, weak):
            raise coach.CoachError("no key")

        coach.weekly_report = boom
        self.assertIsNone(self.sde.weekly_report_text(self.SUNDAY))
        self.assertIsNone(dash.get_weekly_report(dash.week_key(self.SUNDAY)))

    def test_render_escapes_html_and_makes_bullets(self):
        out = self.sde.render_weekly_html("- watch <script>alert(1)</script>\nplain line")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<li", out)
        self.assertIn("plain line", out)

    def test_email_html_includes_report_only_when_given(self):
        empty = pd.DataFrame()
        self.assertNotIn("Your week", self.sde.build_email_html(empty, None))
        self.assertIn("Your week", self.sde.build_email_html(empty, None, "- hi"))


if __name__ == "__main__":
    unittest.main()
