"""Run: python3 -m unittest test_sm2"""

import unittest

from sm2 import SM2State, sm2_update, replay, quality_for_outcome, MIN_EASE_FACTOR


class TestSM2Update(unittest.TestCase):
    def test_first_pass_interval_is_one_day(self):
        state = sm2_update(SM2State(), quality=5)
        self.assertEqual(state.repetitions, 1)
        self.assertEqual(state.interval, 1)

    def test_second_consecutive_pass_interval_is_six_days(self):
        state = sm2_update(SM2State(), quality=5)
        state = sm2_update(state, quality=5)
        self.assertEqual(state.repetitions, 2)
        self.assertEqual(state.interval, 6)

    def test_third_pass_interval_scales_by_ease_factor(self):
        state = SM2State()
        state = sm2_update(state, quality=5)
        state = sm2_update(state, quality=5)
        state = sm2_update(state, quality=5)
        self.assertEqual(state.repetitions, 3)
        self.assertEqual(state.interval, round(6 * state.ease_factor))

    def test_failure_resets_repetitions_and_interval(self):
        state = sm2_update(SM2State(), quality=5)
        state = sm2_update(state, quality=5)  # repetitions=2, interval=6
        state = sm2_update(state, quality=1)  # fail
        self.assertEqual(state.repetitions, 0)
        self.assertEqual(state.interval, 1)

    def test_ease_factor_has_a_floor(self):
        state = SM2State()
        for _ in range(20):
            state = sm2_update(state, quality=0)
        self.assertGreaterEqual(state.ease_factor, MIN_EASE_FACTOR)

    def test_perfect_recall_increases_ease_factor(self):
        state = sm2_update(SM2State(), quality=5)
        self.assertGreater(state.ease_factor, 2.5)

    def test_mediocre_recall_decreases_ease_factor(self):
        # quality=3 is the lowest "passing" grade; SM-2's own formula makes
        # this a slight ease-factor penalty even though it still counts as
        # a correct recall.
        state = sm2_update(SM2State(), quality=3)
        self.assertLess(state.ease_factor, 2.5)

    def test_quality_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            sm2_update(SM2State(), quality=6)
        with self.assertRaises(ValueError):
            sm2_update(SM2State(), quality=-1)


class TestReplay(unittest.TestCase):
    def test_replay_matches_manual_folding(self):
        qualities = [5, 5, 5, 1, 5]
        expected = SM2State()
        for q in qualities:
            expected = sm2_update(expected, q)
        self.assertEqual(replay(qualities), expected)

    def test_empty_replay_is_initial_state(self):
        self.assertEqual(replay([]), SM2State())


class TestOutcomeMapping(unittest.TestCase):
    def test_known_outcomes_map_to_expected_quality(self):
        self.assertEqual(quality_for_outcome("Solved"), 5)
        self.assertEqual(quality_for_outcome("Hint"), 3)
        self.assertEqual(quality_for_outcome("Failed"), 1)

    def test_skip_is_explicitly_unsupported(self):
        with self.assertRaises(ValueError):
            quality_for_outcome("Skip")

    def test_unknown_outcome_raises(self):
        with self.assertRaises(ValueError):
            quality_for_outcome("Whatever")


if __name__ == "__main__":
    unittest.main()
