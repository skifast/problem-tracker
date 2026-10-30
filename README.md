# Problem Tracker

A spaced-repetition tracker for LeetCode / NeetCode practice. It scrapes what you've
solved, stores it in a local SQLite database, and gives you a dashboard that tells you
what to review today and what to try next. Optional AI features (Claude) coach you on
mistakes, write a weekly review, and rank your next problems.

Everything runs on your own machine. Your data lives in `problems.db` and is never
committed to git.

## What's in it

| Piece | File | What it does |
|---|---|---|
| Crawler | `tracker.py` | Scrapes your NeetCode 150 progress (including the completed checkbox), syncs your LeetCode solves, difficulties, accepted-date history and topic tags into `problems.db`. |
| Dashboard | `dashboard.py` | Streamlit app: daily review queue, recommendations, overdue list, browse by pattern, statistics. |
| Daily email | `send_daily_email.py` | Emails you 2 problems to review and 1 new one each morning, plus the AI weekly review once a week. |
| AI layer | `coach.py` | The Claude calls: attempt coaching, weekly report, next-problem ranking. |
| Problem lists | `blind75.py` | The Blind 75 list. |

## Setup (once)

You need macOS, Python 3.11+, and Google Chrome.

```bash
cd "Problem Tracker Application"
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

Use `venv/bin/python` for everything below. It avoids needing to activate the venv, and it
sidesteps a conda `(base)` environment hijacking `python` (the symptom is an error
mentioning `mambaforge` or Python 3.9). If you prefer, `source venv/bin/activate` works
too, but check that `which python` ends in `venv/bin/python`.

### Optional: Anthropic API key (for the AI features)

Create a key at https://console.anthropic.com, then:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."     # for the dashboard, in the terminal you launch it from
```

The scheduled daily email does not see your shell's environment, so for the weekly report
put the key in `email_config.py` instead (see [Daily email](#daily-email)). Never commit a
key. `email_config.py` is gitignored.

Without a key everything still works; the AI features just show a short "unavailable" note.
The model defaults to `claude-opus-5-5`. Set `COACH_MODEL=claude-sonnet-5-5` (or
`claude-haiku-5-5`) to spend less.

## Running the crawler

The crawler drives your real, logged-in Chrome through its remote-debugging port, so it can
see your NeetCode and LeetCode sessions.

1. **Quit Chrome completely** (Cmd+Q), then start it with a dedicated profile and the
   debugging port. Leave this terminal open:

   ```bash
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
     --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"
   ```

   Chrome refuses the debugging port on your default profile, which is why
   `--user-data-dir` is required.

2. In that Chrome window, **log into neetcode.io and leetcode.com**. You only do this once;
   the profile remembers you.

3. Check the port is up (you should get JSON back):

   ```bash
   curl http://localhost:9222/json/version
   ```

4. In a second terminal, run the crawler:

   ```bash
   cd "Problem Tracker Application"
   venv/bin/python tracker.py
   ```

It takes a few minutes. It never deletes rows, so an interrupted run can't wipe your
history. Run it whenever you want fresh data, for example after a solving session.

## Running the dashboard

```bash
venv/bin/python -m streamlit run dashboard.py
```

It opens at http://localhost:8501. If `problems.db` doesn't exist yet, run the crawler first.

| Tab | What you get |
|---|---|
| **Today's Queue** | The problems most worth reviewing today, capped by your daily capacity. |
| **Recommended** | New problems to try. Toggle **All / NeetCode 150 / Blind 75** to see the next unsolved problems in a list, with progress. **✨ Rank with AI** reorders them by your weak spots and prerequisites. |
| **Overdue** | Everything that's gone stale, most urgent first. |
| **Browse by Pattern** | Search and filter your whole history by category and difficulty. |
| **Statistics** | Review activity and per-pattern numbers. |

On every problem card: **✅ Solved, 💡 Hint, ❌ Failed, ⏭ Skip** log an attempt, and
**⭐ Star** flags it (treated the same as a NeetCode star). Anything you attempt today is
hidden until tomorrow.

In the NeetCode 150 and Blind 75 views, a problem counts as done when its checkbox is ticked
on NeetCode (picked up the next time the crawler runs) or you clicked **Solved** here.
LeetCode submission history alone does not count.

## AI features

All need `ANTHROPIC_API_KEY`.

- **Attempt coaching.** Type what tripped you up in the Notes box and click Hint or Failed.
  Claude replies with a short diagnosis and a drill, using your earlier attempts at that
  problem. It's shown on the card. No note means no API call.
- **Weekly review.** Once a week the daily email includes a short review of your last 7
  days: recurring mistakes, weak patterns, and one focus for next week.
- **Next-problem ranking.** In the Blind 75 / NeetCode 150 views, **✨ Rank with AI** orders
  your next 15 candidates. Claude can only reorder the problems it's given.

Calls happen only when you click or when the email runs, so cost is small.

## Daily email

1. Create `email_config.py` in the project folder (it is gitignored):

   ```python
   EMAIL_ADDRESS = "you@gmail.com"
   EMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"   # Gmail App Password, see below
   SEND_TO = EMAIL_ADDRESS                      # who receives it

   # Optional
   ANTHROPIC_API_KEY = "sk-ant-..."             # enables the weekly AI review in scheduled runs
   WEEKLY_REPORT_WEEKDAY = 6                    # 0 = Monday ... 6 = Sunday (default)
   ```

2. **Get a Gmail App Password.** It's a separate 16-character password for this app, not
   your real one. Turn on 2-Step Verification
   (https://myaccount.google.com/signinoptions/two-step-verification), then create one at
   https://myaccount.google.com/apppasswords. You can revoke it any time.

3. Preview without sending, then send one for real:

   ```bash
   venv/bin/python send_daily_email.py --dry-run
   venv/bin/python send_daily_email.py
   ```

   Add `--weekly` to force the AI weekly review regardless of the day (it needs at least 3
   reviews in the last 7 days).

4. **Schedule it** (7:00 AM daily) with launchd. The job only fires if the Mac is awake
   at that time.

   ```bash
   cp com.problemtracker.dailyemail.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.problemtracker.dailyemail.plist
   ```

   The plist points at this folder's `venv/bin/python3`. If you move the project, edit the
   paths in the plist. To stop it:

   ```bash
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.problemtracker.dailyemail.plist
   ```

   Output goes to `daily_email.log` and `daily_email.err.log`.

## Tests

```bash
venv/bin/python -m unittest test_coach test_dashboard test_tracker test_leetcode_sync test_sm2
```

The AI tests use a fake client, so they need no network or API key.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `connect ECONNREFUSED ... 9222` | Chrome isn't running with the debugging port. Quit it fully and relaunch with the command above, then check `curl http://localhost:9222/json/version`. |
| `DevTools remote debugging requires a non-default data directory` | Add `--user-data-dir="$HOME/chrome-debug-profile"`. |
| Errors mentioning `mambaforge` or Python 3.9 | conda's Python is being picked up. Use `venv/bin/python ...`. |
| "Coach unavailable: No Anthropic credentials found" | `ANTHROPIC_API_KEY` isn't set in the terminal that launched Streamlit. |
| Progress shows 0 done in NeetCode 150 / Blind 75 | The crawler hasn't recorded your NeetCode checkboxes yet. Run `tracker.py`. |
| A problem has no difficulty | Run `tracker.py`. If NeetCode and LeetCode name the problem differently, add it to `SLUG_ALIASES` in `tracker.py`. |

## More

- `docs/scheduling-comparison.md` explains the review-priority heuristic and how it compares
  with SM-2 (`sm2.py`).
- `dedupe_*.py` and `migrate_to_unified_db.py` are one-off maintenance scripts. They change
  data, so read them before running one.
