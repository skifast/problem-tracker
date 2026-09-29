"""Run: python3 -m unittest test_tracker

Requires playwright to be installed (it's a module-level import in
tracker.py) even though these tests never launch a browser --
see requirements.txt.
"""

import unittest

from tracker import extract_all_accepted_dates, extract_latest_accepted, leetcode_slug


class TestLeetcodeSlug(unittest.TestCase):
    def test_neetcode_question_url(self):
        # Regression test: the original implementation took the URL's last
        # path segment, which for every real NeetCode URL is the literal
        # word "question" -- not the problem slug.
        url = "https://neetcode.io/problems/reverse-linked-list/question?list=neetcode150"
        self.assertEqual(leetcode_slug(url), "reverse-linked-list")

    def test_bare_leetcode_url(self):
        self.assertEqual(leetcode_slug("https://leetcode.com/problems/word-ladder/"), "word-ladder")

    def test_leetcode_url_without_trailing_slash(self):
        self.assertEqual(leetcode_slug("https://leetcode.com/problems/two-sum"), "two-sum")


class TestExtractLatestAccepted(unittest.TestCase):
    def test_no_accepted_anywhere_returns_none(self):
        text = "Submissions\nNo submissions yet"
        self.assertIsNone(extract_latest_accepted(text))

    def test_date_immediately_after_accepted(self):
        text = "History\nAccepted\n07/12/2026\nRuntime: 40ms"
        self.assertEqual(extract_latest_accepted(text), "07/12/2026")

    def test_date_immediately_before_accepted_within_window(self):
        text = "07/12/2026\nAccepted\nRuntime: 40ms"
        self.assertEqual(extract_latest_accepted(text), "07/12/2026")

    def test_accepted_on_same_line_as_date(self):
        text = "Status: Accepted on 07/12/2026"
        self.assertEqual(extract_latest_accepted(text), "07/12/2026")

    def test_accepted_present_but_no_date_anywhere_returns_none(self):
        text = "Accepted\nRuntime: 40ms\nMemory: 16MB"
        self.assertIsNone(extract_latest_accepted(text))

    def test_skips_accepted_occurrence_with_no_nearby_date_falls_back_to_global_search(self):
        # First "Accepted" has no date within its +-window, but a date
        # exists elsewhere in the text -- the function should still find it
        # via the final whole-text fallback regex rather than giving up.
        far_lines = ["line"] * 10
        text = "\n".join(["Accepted", *far_lines, "05/01/2026"])
        self.assertEqual(extract_latest_accepted(text), "05/01/2026")

    def test_empty_text_returns_none(self):
        self.assertIsNone(extract_latest_accepted(""))


class TestExtractAllAcceptedDates(unittest.TestCase):
    def test_no_accepted_anywhere_returns_empty_list(self):
        self.assertEqual(extract_all_accepted_dates("Submissions\nNo submissions yet"), [])

    def test_finds_every_distinct_date_not_just_the_first(self):
        # Regression scenario: extract_latest_accepted used to return on the
        # first "Accepted" match, discarding every earlier submission's date.
        text = "\n".join([
            "Accepted", "05/01/2026", "Runtime: 40ms",
            "Wrong Answer", "04/15/2026",
            "Accepted", "03/02/2026", "Runtime: 38ms",
        ])
        self.assertEqual(extract_all_accepted_dates(text), ["2026-03-02", "2026-05-01"])

    def test_same_date_on_multiple_accepted_occurrences_is_deduped(self):
        text = "\n".join(["Accepted", "05/01/2026"] * 3)
        self.assertEqual(extract_all_accepted_dates(text), ["2026-05-01"])

    def test_falls_back_to_global_search_when_no_date_near_any_accepted(self):
        far_lines = ["line"] * 10
        text = "\n".join(["Accepted", *far_lines, "05/01/2026"])
        self.assertEqual(extract_all_accepted_dates(text), ["2026-05-01"])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(extract_all_accepted_dates(""), [])


class TestExtractLatestAcceptedBuildsOnAllDates(unittest.TestCase):
    def test_latest_is_the_max_of_all_extracted_dates(self):
        text = "\n".join([
            "Accepted", "03/02/2026", "Runtime: 38ms",
            "Accepted", "05/01/2026", "Runtime: 40ms",
        ])
        self.assertEqual(extract_latest_accepted(text), "05/01/2026")


if __name__ == "__main__":
    unittest.main()
