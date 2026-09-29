"""
Tests sync_leetcode_solved()'s matching/upsert logic against a mocked
LeetCode context (no network, no browser). This does NOT verify that
LEETCODE_PROBLEMS_API / LEETCODE_GRAPHQL actually work against the live
site -- only that, given a plausible response shape, the DB reconciliation
logic (slug matching via SLUG_ALIASES, never overwriting existing data,
capped date backfill) behaves correctly.

Run: python3 -m unittest test_leetcode_sync
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import tracker as t


def make_response(json_data, ok=True, status=200):
    resp = MagicMock()
    resp.ok = ok
    resp.status = status
    resp.json.return_value = json_data
    return resp


def make_leetcode_all_response(pairs):
    return {"user_name": "testuser", "stat_status_pairs": pairs}


def solved_pair(slug, title):
    return {
        "status": "ac",
        "stat": {"question__title_slug": slug, "question__title": title},
    }


def unsolved_pair(slug, title):
    return {
        "status": None,
        "stat": {"question__title_slug": slug, "question__title": title},
    }


def pair_with_difficulty(slug, title, level, status="ac"):
    return {
        "status": status,
        "stat": {"question__title_slug": slug, "question__title": title},
        "difficulty": {"level": level},
    }


class TestSyncLeetcodeSolved(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._orig_db_path = t.DB_PATH
        t.DB_PATH = self.db_path
        t.init_db()

    def tearDown(self):
        t.DB_PATH = self._orig_db_path
        self.tmpdir.cleanup()

    def _seed_neetcode_problem(self, url, name, category, last_accepted=None):
        now = datetime.now(timezone.utc).isoformat()
        t.save_row((name, category, url, 0, last_accepted, now))

    def _con(self):
        return sqlite3.connect(str(self.db_path))

    def test_matches_existing_neetcode_problem_by_identical_slug(self):
        # "climbing-stairs" is not in SLUG_ALIASES -- identical slug on both sites.
        # (Was "reverse-linked-list" -- turned out NOT to be identical after
        # all: NeetCode actually hosts it as "reverse-a-linked-list", now a
        # real SLUG_ALIASES entry, so it stopped exercising this "no alias
        # needed" case and started exercising the alias-lookup path instead.)
        self._seed_neetcode_problem(
            "https://neetcode.io/problems/climbing-stairs/question?list=neetcode150",
            "Climbing Stairs", "1-D Dynamic Programming", last_accepted="07/01/2026",
        )
        context = MagicMock()
        context.request.get.return_value = make_response(
            make_leetcode_all_response([solved_pair("climbing-stairs", "Climbing Stairs")])
        )

        t.sync_leetcode_solved(context)

        con = self._con()
        rows = con.execute("SELECT url, source, last_accepted FROM problems").fetchall()
        con.close()
        self.assertEqual(len(rows), 1)  # no duplicate row created
        self.assertEqual(rows[0][1], "neetcode")
        self.assertEqual(rows[0][2], "07/01/2026")  # untouched, was already set

    def test_matches_existing_neetcode_problem_via_slug_alias(self):
        # "contains-duplicate" (leetcode slug) maps to "duplicate-integer" (neetcode slug).
        self._seed_neetcode_problem(
            "https://neetcode.io/problems/duplicate-integer/question?list=neetcode150",
            "Contains Duplicate", "Arrays & Hashing",
        )
        context = MagicMock()
        context.request.get.return_value = make_response(
            make_leetcode_all_response([solved_pair("contains-duplicate", "Contains Duplicate")])
        )
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Accepted", "timestamp": 1751328000},  # 07/01/2025-ish
            ]}}
        })

        t.sync_leetcode_solved(context)

        con = self._con()
        rows = con.execute("SELECT url, source FROM problems").fetchall()
        con.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "https://neetcode.io/problems/duplicate-integer/question?list=neetcode150")

    def test_new_discovery_outside_catalogue_is_inserted_uncategorized(self):
        context = MagicMock()
        context.request.get.return_value = make_response(
            make_leetcode_all_response([solved_pair("word-ladder", "Word Ladder")])
        )
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Accepted", "timestamp": 1751328000},
            ]}}
        })

        t.sync_leetcode_solved(context)

        con = self._con()
        row = con.execute(
            "SELECT name, category, source, last_accepted FROM problems WHERE url = ?",
            ("https://leetcode.com/problems/word-ladder/",),
        ).fetchone()
        con.close()
        self.assertIsNotNone(row)
        name, category, source, last_accepted = row
        self.assertEqual(name, "Word Ladder")
        self.assertEqual(category, "Uncategorized")
        self.assertEqual(source, "leetcode")
        self.assertIsNotNone(last_accepted)  # backfilled since under MAX_PROBLEMS cap

    def test_unsolved_problems_are_ignored(self):
        context = MagicMock()
        context.request.get.return_value = make_response(
            make_leetcode_all_response([unsolved_pair("two-sum", "Two Sum")])
        )
        t.sync_leetcode_solved(context)

        con = self._con()
        count = con.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_not_logged_in_raises_clear_error(self):
        context = MagicMock()
        context.request.get.return_value = make_response({"user_name": "", "stat_status_pairs": []})
        with self.assertRaises(RuntimeError):
            t.get_leetcode_solved(context)

    def test_backfill_is_capped_at_max_problems(self):
        # Seed more NULL-last_accepted rows than MAX_PROBLEMS, all as new
        # LeetCode-only discoveries in one run, and confirm only
        # MAX_PROBLEMS of them get an actual date fetch attempt.
        pairs = [solved_pair(f"problem-{i}", f"Problem {i}") for i in range(t.MAX_PROBLEMS + 5)]
        context = MagicMock()
        context.request.get.return_value = make_response(make_leetcode_all_response(pairs))
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Accepted", "timestamp": 1751328000},
            ]}}
        })

        t.sync_leetcode_solved(context)

        self.assertEqual(context.request.post.call_count, t.MAX_PROBLEMS)

        con = self._con()
        total = con.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
        with_date = con.execute("SELECT COUNT(*) FROM problems WHERE last_accepted IS NOT NULL").fetchone()[0]
        con.close()
        self.assertEqual(total, t.MAX_PROBLEMS + 5)  # nothing dropped
        self.assertEqual(with_date, t.MAX_PROBLEMS)  # only the capped batch got a date

    def test_never_overwrites_existing_last_accepted(self):
        self._seed_neetcode_problem(
            "https://neetcode.io/problems/two-integer-sum/question?list=neetcode150",
            "Two Sum", "Arrays & Hashing", last_accepted="01/01/2020",
        )
        context = MagicMock()
        context.request.get.return_value = make_response(
            make_leetcode_all_response([solved_pair("two-sum", "Two Sum")])
        )
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Accepted", "timestamp": 1751328000},
            ]}}
        })

        t.sync_leetcode_solved(context)

        con = self._con()
        last_accepted = con.execute(
            "SELECT last_accepted FROM problems WHERE url = ?",
            ("https://neetcode.io/problems/two-integer-sum/question?list=neetcode150",),
        ).fetchone()[0]
        con.close()
        self.assertEqual(last_accepted, "01/01/2020")


class TestSyncDifficulties(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._orig_db_path = t.DB_PATH
        t.DB_PATH = self.db_path
        t.init_db()

    def tearDown(self):
        t.DB_PATH = self._orig_db_path
        self.tmpdir.cleanup()

    def _seed_neetcode_problem(self, url, name, category):
        now = datetime.now(timezone.utc).isoformat()
        t.save_row((name, category, url, 0, None, now))

    def _con(self):
        return sqlite3.connect(str(self.db_path))

    def test_fetch_leetcode_difficulty_map_parses_labels(self):
        context = MagicMock()
        context.request.get.return_value = make_response({
            "user_name": "testuser",
            "stat_status_pairs": [
                pair_with_difficulty("two-sum", "Two Sum", 1),
                pair_with_difficulty("word-ladder", "Word Ladder", 3, status=None),
            ],
        })
        result = t.fetch_leetcode_difficulty_map(context)
        self.assertEqual(result, {"two-sum": "Easy", "word-ladder": "Hard"})

    def test_sync_tags_difficulty_for_leetcode_sourced_problem(self):
        context = MagicMock()
        context.request.get.return_value = make_response({
            "user_name": "testuser",
            "stat_status_pairs": [pair_with_difficulty("word-ladder", "Word Ladder", 3)],
        })
        con = self._con()
        con.execute("""
            INSERT INTO problems (url, name, category, starred, first_seen, last_seen, source)
            VALUES ('https://leetcode.com/problems/word-ladder/', 'Word Ladder', 'Uncategorized', 0, '', '', 'leetcode')
        """)
        con.commit()
        con.close()

        t.sync_difficulties(context)

        con = self._con()
        difficulty = con.execute(
            "SELECT difficulty FROM problems WHERE url = ?",
            ("https://leetcode.com/problems/word-ladder/",),
        ).fetchone()[0]
        con.close()
        self.assertEqual(difficulty, "Hard")

    def test_sync_tags_difficulty_for_neetcode_sourced_problem_via_slug_alias(self):
        # NeetCode hosts "Contains Duplicate" under "duplicate-integer";
        # LeetCode's canonical slug is "contains-duplicate".
        self._seed_neetcode_problem(
            "https://neetcode.io/problems/duplicate-integer/question?list=neetcode150",
            "Contains Duplicate", "Arrays & Hashing",
        )
        context = MagicMock()
        context.request.get.return_value = make_response({
            "user_name": "testuser",
            "stat_status_pairs": [pair_with_difficulty("contains-duplicate", "Contains Duplicate", 1)],
        })

        t.sync_difficulties(context)

        con = self._con()
        difficulty = con.execute(
            "SELECT difficulty FROM problems WHERE url = ?",
            ("https://neetcode.io/problems/duplicate-integer/question?list=neetcode150",),
        ).fetchone()[0]
        con.close()
        self.assertEqual(difficulty, "Easy")

    def test_sync_leaves_unmatched_rows_untouched(self):
        self._seed_neetcode_problem(
            "https://neetcode.io/problems/reverse-linked-list/question?list=neetcode150",
            "Reverse Linked List", "Linked List",
        )
        context = MagicMock()
        context.request.get.return_value = make_response({
            "user_name": "testuser",
            "stat_status_pairs": [],  # LeetCode's map has nothing for this slug
        })

        t.sync_difficulties(context)

        con = self._con()
        difficulty = con.execute(
            "SELECT difficulty FROM problems WHERE url = ?",
            ("https://neetcode.io/problems/reverse-linked-list/question?list=neetcode150",),
        ).fetchone()[0]
        con.close()
        self.assertIsNone(difficulty)


class TestPickPrimaryCategory(unittest.TestCase):
    def test_empty_list_returns_uncategorized(self):
        self.assertEqual(t.pick_primary_category([]), "Uncategorized")

    def test_single_tag_translated_to_neetcode_category(self):
        self.assertEqual(t.pick_primary_category(["Hash Table"]), "Arrays & Hashing")

    def test_generic_tag_deprioritized_in_favor_of_specific_tag(self):
        # "Array" is generic and listed first; "Two Pointers" is specific
        # and should win even though it's second in the list.
        self.assertEqual(t.pick_primary_category(["Array", "Two Pointers"]), "Two Pointers")

    def test_falls_back_to_first_tag_when_every_tag_is_generic(self):
        self.assertEqual(t.pick_primary_category(["Array", "Math"]), "Arrays & Hashing")

    def test_untranslated_tag_used_as_is(self):
        # "Design" has no NeetCode-equivalent bucket in LEETCODE_TAG_TO_CATEGORY.
        self.assertEqual(t.pick_primary_category(["Design"]), "Design")


class TestFetchLeetcodeTopicTags(unittest.TestCase):
    def test_returns_tag_names_list(self):
        context = MagicMock()
        context.request.post.return_value = make_response({
            "data": {"question": {"topicTags": [{"name": "Array"}, {"name": "Two Pointers"}]}}
        })
        self.assertEqual(t.fetch_leetcode_topic_tags(context, "two-sum"), ["Array", "Two Pointers"])

    def test_http_error_returns_empty_list(self):
        context = MagicMock()
        context.request.post.return_value = make_response({}, ok=False, status=500)
        self.assertEqual(t.fetch_leetcode_topic_tags(context, "two-sum"), [])

    def test_malformed_response_returns_empty_list(self):
        context = MagicMock()
        context.request.post.return_value = make_response({"data": {"question": None}})
        self.assertEqual(t.fetch_leetcode_topic_tags(context, "two-sum"), [])


class TestSyncCategories(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._orig_db_path = t.DB_PATH
        t.DB_PATH = self.db_path
        t.init_db()

    def tearDown(self):
        t.DB_PATH = self._orig_db_path
        self.tmpdir.cleanup()

    def _con(self):
        return sqlite3.connect(str(self.db_path))

    def _seed_uncategorized(self, url, name):
        now = datetime.now(timezone.utc).isoformat()
        con = self._con()
        con.execute("""
            INSERT INTO problems (url, name, category, starred, first_seen, last_seen, source)
            VALUES (?, ?, 'Uncategorized', 0, ?, ?, 'leetcode')
        """, (url, name, now, now))
        con.commit()
        con.close()

    def _tags_response(self, *names):
        return make_response({"data": {"question": {"topicTags": [{"name": n} for n in names]}}})

    def test_fills_uncategorized_row_with_primary_category_and_topics(self):
        self._seed_uncategorized("https://leetcode.com/problems/happy-number/", "Happy Number")
        context = MagicMock()
        context.request.post.return_value = self._tags_response("Hash Table", "Math", "Two Pointers")

        t.sync_categories(context)

        con = self._con()
        category, topics = con.execute(
            "SELECT category, topics FROM problems WHERE url = ?",
            ("https://leetcode.com/problems/happy-number/",),
        ).fetchone()
        con.close()
        self.assertEqual(category, "Two Pointers")  # first non-generic tag
        self.assertEqual(topics, "Hash Table, Math, Two Pointers")  # full list preserved

    def test_never_overwrites_an_existing_real_category(self):
        now = datetime.now(timezone.utc).isoformat()
        con = self._con()
        con.execute("""
            INSERT INTO problems (url, name, category, starred, first_seen, last_seen, source)
            VALUES ('https://neetcode.io/problems/two-integer-sum/question?list=neetcode150',
                    'Two Sum', 'Arrays & Hashing', 0, ?, ?, 'neetcode')
        """, (now, now))
        con.commit()
        con.close()

        context = MagicMock()
        context.request.post.return_value = self._tags_response("Design")  # would be wrong if applied

        t.sync_categories(context)

        con = self._con()
        category = con.execute(
            "SELECT category FROM problems WHERE url = ?",
            ("https://neetcode.io/problems/two-integer-sum/question?list=neetcode150",),
        ).fetchone()[0]
        con.close()
        self.assertEqual(category, "Arrays & Hashing")  # untouched
        self.assertEqual(context.request.post.call_count, 0)  # never even selected for backfill

    def test_capped_at_max_category_backfill_per_run(self):
        # Own cap, decoupled from MAX_PROBLEMS: this is a one-time backlog
        # catch-up, not a per-run drip, so it's sized to clear a real
        # backlog in one run rather than dozens -- monkeypatch it small
        # here rather than actually seeding hundreds of rows.
        orig_cap = t.MAX_CATEGORY_BACKFILL
        t.MAX_CATEGORY_BACKFILL = 3
        try:
            for i in range(t.MAX_CATEGORY_BACKFILL + 5):
                self._seed_uncategorized(f"https://leetcode.com/problems/problem-{i}/", f"Problem {i}")
            context = MagicMock()
            context.request.post.return_value = self._tags_response("Greedy")

            t.sync_categories(context)

            self.assertEqual(context.request.post.call_count, 3)
            con = self._con()
            tagged = con.execute("SELECT COUNT(*) FROM problems WHERE topics IS NOT NULL").fetchone()[0]
            con.close()
            self.assertEqual(tagged, 3)
        finally:
            t.MAX_CATEGORY_BACKFILL = orig_cap

    def test_failed_tag_fetch_leaves_row_retryable(self):
        # topics stays NULL on failure, so the row is picked up again by a
        # later run instead of being silently skipped forever.
        self._seed_uncategorized("https://leetcode.com/problems/some-slug/", "Some Problem")
        context = MagicMock()
        context.request.post.return_value = make_response({}, ok=False, status=500)

        t.sync_categories(context)

        con = self._con()
        category, topics = con.execute(
            "SELECT category, topics FROM problems WHERE url = ?",
            ("https://leetcode.com/problems/some-slug/",),
        ).fetchone()
        con.close()
        self.assertEqual(category, "Uncategorized")
        self.assertIsNone(topics)


class TestFetchLeetcodeAcceptedDate(unittest.TestCase):
    def test_returns_latest_accepted_date_formatted(self):
        context = MagicMock()
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Wrong Answer", "timestamp": 1751500000},
                {"statusDisplay": "Accepted", "timestamp": 1751328000},
                {"statusDisplay": "Accepted", "timestamp": 1700000000},  # older
            ]}}
        })
        result = t.fetch_leetcode_accepted_date(context, "two-sum")
        expected = datetime.fromtimestamp(1751328000, tz=timezone.utc).strftime("%m/%d/%Y")
        self.assertEqual(result, expected)

    def test_no_accepted_submissions_returns_none(self):
        context = MagicMock()
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Wrong Answer", "timestamp": 1751500000},
            ]}}
        })
        self.assertIsNone(t.fetch_leetcode_accepted_date(context, "two-sum"))

    def test_http_error_returns_none(self):
        context = MagicMock()
        context.request.post.return_value = make_response({}, ok=False, status=500)
        self.assertIsNone(t.fetch_leetcode_accepted_date(context, "two-sum"))

    def test_malformed_response_returns_none(self):
        context = MagicMock()
        context.request.post.return_value = make_response({"unexpected": "shape"})
        self.assertIsNone(t.fetch_leetcode_accepted_date(context, "two-sum"))


class TestFetchLeetcodeAcceptedDates(unittest.TestCase):
    def test_returns_every_distinct_accepted_date(self):
        context = MagicMock()
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Wrong Answer", "timestamp": 1751500000},
                {"statusDisplay": "Accepted", "timestamp": 1751328000},
                {"statusDisplay": "Accepted", "timestamp": 1700000000},  # older, distinct date
            ]}}
        })
        result = t.fetch_leetcode_accepted_dates(context, "two-sum")
        expected = sorted({
            datetime.fromtimestamp(1751328000, tz=timezone.utc).strftime("%Y-%m-%d"),
            datetime.fromtimestamp(1700000000, tz=timezone.utc).strftime("%Y-%m-%d"),
        })
        self.assertEqual(result, expected)

    def test_same_calendar_date_deduped(self):
        # Two Accepted submissions on the same UTC day -> one distinct date.
        context = MagicMock()
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Accepted", "timestamp": 1751328000},
                {"statusDisplay": "Accepted", "timestamp": 1751328600},  # 10 min later
            ]}}
        })
        result = t.fetch_leetcode_accepted_dates(context, "two-sum")
        self.assertEqual(len(result), 1)

    def test_no_accepted_submissions_returns_empty_list(self):
        context = MagicMock()
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Wrong Answer", "timestamp": 1751500000},
            ]}}
        })
        self.assertEqual(t.fetch_leetcode_accepted_dates(context, "two-sum"), [])

    def test_http_error_returns_empty_list(self):
        context = MagicMock()
        context.request.post.return_value = make_response({}, ok=False, status=500)
        self.assertEqual(t.fetch_leetcode_accepted_dates(context, "two-sum"), [])

    def test_malformed_response_returns_empty_list(self):
        context = MagicMock()
        context.request.post.return_value = make_response({"unexpected": "shape"})
        self.assertEqual(t.fetch_leetcode_accepted_dates(context, "two-sum"), [])

    def test_fetch_leetcode_accepted_date_is_the_max_of_fetch_leetcode_accepted_dates(self):
        context = MagicMock()
        context.request.post.return_value = make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Accepted", "timestamp": 1700000000},
                {"statusDisplay": "Accepted", "timestamp": 1751328000},
            ]}}
        })
        latest = t.fetch_leetcode_accepted_date(context, "two-sum")
        all_dates = t.fetch_leetcode_accepted_dates(context, "two-sum")
        self.assertEqual(latest, datetime.strptime(max(all_dates), "%Y-%m-%d").strftime("%m/%d/%Y"))


class TestSyncAcceptedDates(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self._orig_db_path = t.DB_PATH
        t.DB_PATH = self.db_path
        t.init_db()

    def tearDown(self):
        t.DB_PATH = self._orig_db_path
        self.tmpdir.cleanup()

    def _con(self):
        return sqlite3.connect(str(self.db_path))

    def _seed_problem(self, url, name, source, last_accepted=None):
        now = datetime.now(timezone.utc).isoformat()
        con = self._con()
        con.execute("""
            INSERT INTO problems (url, name, category, starred, last_accepted, first_seen, last_seen, source)
            VALUES (?, ?, 'Arrays & Hashing', 0, ?, ?, ?, ?)
        """, (url, name, last_accepted, now, now, source))
        con.commit()
        con.close()

    def _submissions_response(self, *timestamps):
        return make_response({
            "data": {"submissionList": {"submissions": [
                {"statusDisplay": "Accepted", "timestamp": ts} for ts in timestamps
            ]}}
        })

    def test_backfills_full_history_for_a_problem_that_already_had_last_accepted(self):
        # The whole point: a problem solved long before accepted_dates
        # existed already has a last_accepted, but only ever the one date.
        self._seed_problem(
            "https://leetcode.com/problems/two-sum/", "Two Sum", "leetcode",
            last_accepted="06/30/2025",
        )
        context = MagicMock()
        context.request.post.return_value = self._submissions_response(1700000000, 1751328000)

        t.sync_accepted_dates(context)

        con = self._con()
        dates = sorted(r[0] for r in con.execute(
            "SELECT accepted_date FROM accepted_dates WHERE problem_url = ?",
            ("https://leetcode.com/problems/two-sum/",),
        ).fetchall())
        con.close()
        self.assertEqual(len(dates), 2)

    def test_never_overwrites_an_existing_last_accepted(self):
        self._seed_problem(
            "https://leetcode.com/problems/two-sum/", "Two Sum", "leetcode",
            last_accepted="06/30/2025",
        )
        context = MagicMock()
        context.request.post.return_value = self._submissions_response(1751328000)

        t.sync_accepted_dates(context)

        con = self._con()
        last_accepted = con.execute(
            "SELECT last_accepted FROM problems WHERE url = ?",
            ("https://leetcode.com/problems/two-sum/",),
        ).fetchone()[0]
        con.close()
        self.assertEqual(last_accepted, "06/30/2025")

    def test_backfills_last_accepted_when_missing(self):
        self._seed_problem("https://leetcode.com/problems/two-sum/", "Two Sum", "leetcode")
        context = MagicMock()
        context.request.post.return_value = self._submissions_response(1751328000)

        t.sync_accepted_dates(context)

        con = self._con()
        last_accepted = con.execute(
            "SELECT last_accepted FROM problems WHERE url = ?",
            ("https://leetcode.com/problems/two-sum/",),
        ).fetchone()[0]
        con.close()
        expected = datetime.fromtimestamp(1751328000, tz=timezone.utc).strftime("%m/%d/%Y")
        self.assertEqual(last_accepted, expected)

    def test_skips_problems_that_already_have_accepted_dates_rows(self):
        self._seed_problem("https://leetcode.com/problems/two-sum/", "Two Sum", "leetcode")
        con = self._con()
        con.execute(
            "INSERT INTO accepted_dates (problem_url, accepted_date) VALUES (?, '2025-06-30')",
            ("https://leetcode.com/problems/two-sum/",),
        )
        con.commit()
        con.close()

        context = MagicMock()
        context.request.post.return_value = self._submissions_response(1751328000)

        t.sync_accepted_dates(context)

        self.assertEqual(context.request.post.call_count, 0)

    def test_capped_at_max_accepted_dates_backfill_per_run(self):
        orig_cap = t.MAX_ACCEPTED_DATES_BACKFILL
        t.MAX_ACCEPTED_DATES_BACKFILL = 3
        try:
            for i in range(t.MAX_ACCEPTED_DATES_BACKFILL + 5):
                self._seed_problem(f"https://leetcode.com/problems/problem-{i}/", f"Problem {i}", "leetcode")
            context = MagicMock()
            context.request.post.return_value = self._submissions_response(1751328000)

            t.sync_accepted_dates(context)

            self.assertEqual(context.request.post.call_count, 3)
        finally:
            t.MAX_ACCEPTED_DATES_BACKFILL = orig_cap


if __name__ == "__main__":
    unittest.main()
