# Scheduling: the app's heuristic vs. canonical SM-2

This app schedules daily review problems with a hand-written priority
function, `score_row()` in [dashboard.py](../dashboard.py).
This doc compares that heuristic against SM-2 (SuperMemo-2), the algorithm
Anki and most flashcard software use, and explains why the heuristic was
kept. [sm2.py](../sm2.py) is a real, tested implementation of SM-2 — not
just a description — and [sm2_vs_heuristic_demo.py](../sm2_vs_heuristic_demo.py)
runs both against real accepted-submission history from LeetCode/NeetCode
(`accepted_dates` — see [data-model-script.pdf](data-model-script.pdf)),
not just this app's own thin review log.

## How SM-2 works

Each item carries three numbers, updated after every review:

- **repetitions** — consecutive passing recalls
- **interval** — days until the item is next due
- **ease_factor (EF)** — how quickly the interval grows; starts at 2.5, floor 1.3

A review is graded 0–5 ("quality"). If quality < 3, the schedule resets
(repetitions → 0, interval → 1 day). Otherwise the interval grows:
1st pass → 1 day, 2nd → 6 days, 3rd+ → `round(previous_interval * EF)`. EF
itself moves up or down slightly based on how easy the recall felt. The
result is a **due date per item** — SM-2 answers "is this item due yet?",
not "which items should I study today?".

## How `score_row()` works

It computes a single continuous urgency number per problem, every time the
dashboard loads, from signals SM-2 doesn't have:

- age since last review (linear, `days * 2`)
- `+10` if starred
- `+20` Failed / `+10` Hint / `-≤10` Solved, scaled by how many times it's been solved
- `+5` per past struggle (Hint or Failed)

`build_queue()` then sorts by that score and caps how many problems per
category can appear in one day's queue, so a review session isn't ten
Trees problems in a row. SM-2 has no equivalent to either the starred boost
or the category cap — both were bolted onto the heuristic because the app
needed them, not because a scheduling algorithm handed them out.

## The outcome → quality mapping is itself a design decision

The app records one of four discrete outcomes (`Solved`, `Hint`, `Failed`,
`Skip`); SM-2 wants a continuous 0–5 rating. Translating between them isn't
free — `sm2.py`'s `OUTCOME_TO_QUALITY` makes an explicit call:

| App outcome | SM-2 quality | Rationale |
|---|---|---|
| Solved | 5 | correct, no reported hesitation |
| Hint   | 3 | correct, but needed help — SM-2's own floor for "still passing" |
| Failed | 1 | incorrect, but recognized the solution once shown |
| Skip   | *(excluded)* | no attempt was made — advancing or penalizing the schedule for a non-attempt would be wrong, so `quality_for_outcome()` raises rather than guess |

This is a real information loss: SM-2 was designed around a self-rated
5-point scale, and squeezing 4 coarse buckets into it discards nuance a
real flashcard app would capture directly.

## Running both against real data

`sm2_vs_heuristic_demo.py` originally replayed `review_history` — but that
log only spans the few weeks since this app started recording in-app
reviews (22 rows, total, mostly 1 per problem), so on that data the two
algorithms never visibly disagreed: everything was uniformly overdue under
both. That absence of divergence was an honest finding, but it was a
finding about the dataset being too young, not about the algorithms being
equivalent.

`accepted_dates` changed that. It's LeetCode/NeetCode's own record of every
distinct date a problem was accepted — independent of this app entirely —
and it turned out to hold genuine multi-year repetition history: 149 of 354
problems have 2+ distinct accepted dates, some spanning 2020 through 2026,
one with 11 separate accepted dates. That's exactly the "mature dataset"
this doc used to say didn't exist yet. The demo now replays *that* history
instead (every accepted_dates row is a successful submission by definition,
so each replay quality is a flat 5):

```
$ python3 sm2_vs_heuristic_demo.py
problem                                       heuristic  reps    EF  SM-2 next due  SM-2 days  divergence
----------------------------------------------------------------------------------------------------------------------------------
Container With Most Water                        129.0     7  3.20     2030-04-10       1306  heuristic flags it now; SM-2 not due for 1306 more day(s)
Coin Change                                       206.0    10  3.50     2183-04-18      57196  heuristic flags it now; SM-2 not due for 57196 more day(s)
Subsets                                           126.0    11  3.60     2591-04-16     206213  heuristic flags it now; SM-2 not due for 206213 more day(s)
...
------------------------------------------------------------------------------------------------------------------------------------
59 of 354 problems show heuristic/SM-2 divergence.
```

**Real finding now, not a constructed one: 59 of 354 problems genuinely
diverge.** Every divergence in this direction — the heuristic says review
it now, SM-2 says it's not due for anywhere from a few days to (in the most
extreme cases) centuries — because the heuristic's per-solve discount is
capped and linear (`min(solved_count*2, 10)`, so a problem solved 3 times
scores identically to one solved 30 times), while SM-2's interval grows
*multiplicatively* through the ease factor. After 7+ genuine repetitions,
that compounding produces intervals the heuristic has no way to express.

**A second, unplanned finding: canonical SM-2 has no ceiling.** `Subsets`
(11 accepted dates, EF 3.6) comes out to a next-due date of **2591** —
206,213 days out. That's not a bug in this implementation; it's what the
original SM-2 spec actually does with enough successful repetitions and no
cap. Real spaced-repetition tools (Anki included) impose a maximum interval
in practice for exactly this reason — canonical SM-2 on its own will
confidently schedule something for the 26th century. Worth having ready if
asked "would you actually deploy raw SM-2": no, not without adding the cap
everyone else adds too.

## Why the heuristic was kept

1. **The app needs a ranked top-N, not a due/not-due flag.** Given a
   review capacity of 5–20 problems, it needs to pick the *most valuable*
   items across everything not-yet-reviewed today. SM-2 alone answers "is
   X due?" per item; turning that into "which 8 of my 40 due items today"
   still needs a secondary ranking step — which is exactly what
   `score_row()` already does, natively.
2. **Category balance is a hard requirement here, and SM-2 has no concept
   of it.** `build_queue()`'s per-category cap would have to be bolted onto
   SM-2 regardless of which scheduler ran underneath — so switching
   wouldn't remove that piece of custom logic, only add SM-2 alongside it.
3. **No longer cold-start, but no ceiling either.** This used to be "the
   dataset is too young to tell" — it isn't anymore (see above, 60 real
   divergences). The reason to still not adopt raw SM-2 changed accordingly:
   it has no maximum interval, and on real multi-year data it produces
   schedules like "next due in the 26th century." Adopting it would mean
   adopting that cap-the-interval fix too, which is itself extra logic on
   top of SM-2, not a reason SM-2 alone is simpler.
4. **State cost.** SM-2 needs mutable per-item state (`repetitions`,
   `interval`, `ease_factor`) persisted and updated transactionally on
   every review. The heuristic derives its score fresh, statelessly, from
   `review_history` — the log we already keep for the notes field — so
   there's no separate state to keep in sync or get wrong.

## Where SM-2 would clearly win

If the goal shifts from "structured breadth of practice across topics" to
"retain each problem's pattern in long-term memory with minimal repetition,"
SM-2's EF-based interval growth is a validated model of the forgetting
curve; the heuristic's fixed weights (`+10` starred, `-2`/solve, `+5`/struggle)
are hand-tuned constants with no research behind them, and its linear
age term doesn't reflect how memory actually decays. That's the trade being
made deliberately here, and `sm2.py` exists so that trade is a real,
demonstrable comparison — not just an assertion.
