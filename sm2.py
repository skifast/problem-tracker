"""
Canonical SM-2 (SuperMemo-2) spaced-repetition algorithm.

Implemented as a pure function over an explicit per-item state -- the same
core algorithm used by Anki and the original SuperMemo. This module is
deliberately standalone: it has no dependency on the rest of the app and
does not touch problems.db.

It exists to compare against the ad-hoc priority heuristic in
dashboard.py (score_row / build_queue). See
docs/scheduling-comparison.md for the write-up, and sm2_vs_heuristic_demo.py
for both algorithms run side by side over real review history.
"""

from dataclasses import dataclass

MIN_EASE_FACTOR = 1.3
DEFAULT_EASE_FACTOR = 2.5


@dataclass(frozen=True)
class SM2State:
    repetitions: int = 0                     # consecutive passing recalls
    interval: int = 0                        # days until the next review
    ease_factor: float = DEFAULT_EASE_FACTOR  # SM-2's "EF"; starts at 2.5, floor 1.3


def sm2_update(state: SM2State, quality: int) -> SM2State:
    """
    Advance SM-2 state by one review.

    quality: 0-5 self-rated recall quality, per the original SM-2 spec:
        5 - perfect recall
        4 - correct, after some hesitation
        3 - correct, with serious difficulty
        2 - incorrect, but the correct answer felt familiar
        1 - incorrect, the correct answer is remembered on seeing it
        0 - complete blackout

    quality < 3 counts as a failed recall: repetitions and interval reset
    to the beginning of the schedule, but the ease factor is still updated
    (a failure still makes future items harder, even after the reset).
    """
    if not 0 <= quality <= 5:
        raise ValueError("quality must be between 0 and 5")

    ef = state.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ef = max(ef, MIN_EASE_FACTOR)

    # Schedule restarts, EF carries forward
    if quality < 3:
        return SM2State(repetitions=0, interval=1, ease_factor=ef)

    repetitions = state.repetitions + 1
    if repetitions == 1:
        interval = 1
    elif repetitions == 2:
        interval = 6
    else:
        interval = round(state.interval * ef)

    return SM2State(repetitions=repetitions, interval=interval, ease_factor=ef)


def replay(qualities: list[int]) -> SM2State:
    """Fold a chronological list of quality ratings into a final SM2State."""
    state = SM2State()
    for q in qualities:
        state = sm2_update(state, q)
    return state


# --- Mapping our review outcomes onto SM-2's 0-5 quality scale -------------
#
# Our app records one of four discrete outcomes per review (see
# dashboard.py: record_review). SM-2 expects a continuous 0-5
# self-rating, so this mapping is itself a design decision -- SM-2 does not
# define it for us. See docs/scheduling-comparison.md for the reasoning.
#
# "Skip" has no SM-2 analogue: no attempt was made, so it should neither
# advance nor penalize the schedule. Callers must filter Skip events out
# before replaying history through sm2_update/replay.

OUTCOME_TO_QUALITY = {
    "Solved": 5,   # correct, no hesitation reported
    "Hint":   3,   # correct, but needed a nudge -- SM-2's floor for "still passing"
    "Failed": 1,   # incorrect, but recognized the solution once shown
}


def quality_for_outcome(outcome: str) -> int:
    if outcome not in OUTCOME_TO_QUALITY:
        raise ValueError(
            f"No SM-2 quality mapping for outcome {outcome!r} "
            "('Skip' is intentionally excluded -- filter it out before replaying)"
        )
    return OUTCOME_TO_QUALITY[outcome]
