import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

DB_PATH = Path(__file__).parent / "problems.db"
MAX_PROBLEMS = 10

# Category backfill (sync_categories) gets its own, much larger cap.
# MAX_PROBLEMS exists to stop the accepted-date backfill from hammering
# LeetCode's endpoint on every single ongoing sync run, forever -- category
# backfill is a one-time catch-up for an existing 'Uncategorized' backlog,
# not a recurring drip, so there's no good reason to stretch a few hundred
# problems across dozens of separate tracker.py runs. Comfortably above the
# current backlog size; still a real bound, not "no cap at all", in case a
# much larger backlog shows up later.
MAX_CATEGORY_BACKFILL = 300

# Same reasoning as MAX_CATEGORY_BACKFILL: sync_accepted_dates() is a
# one-time catch-up for problems that already have a last_accepted but were
# never scraped for their *full* accepted-date history (only ever kept the
# single latest date) -- not an ongoing per-run drip, so it's sized to clear
# the whole backlog in one run.
MAX_ACCEPTED_DATES_BACKFILL = 300

def scrape_neetcode_catalog(page):
    """Every NeetCode 150 problem row -- completed or not -- with its
    completed/starred toggle state. get_completed_problems() below is a
    completed-only filter over this; the unfiltered list is also what
    populates the `catalog` table (see save_catalog_row), so the dashboard
    can recommend problems that haven't been attempted yet.

    Deliberately scoped to the curated 150-problem list, not NeetCode's
    full ~973-problem catalog: briefly widened to the full "allNC" list,
    but that list paginates within a category (75 problems/page) where
    this list doesn't need to -- every category here comfortably fits on
    one screen, so the simple single-scroll approach below is enough, and
    trying to handle pagination for a case that doesn't occur here was
    itself a source of bugs, not a robustness improvement."""
    # "networkidle" can hang indefinitely on a page with any persistent
    # background activity (polling, websockets, analytics beacons) --
    # check_problem() below already avoids it for this exact reason.
    # "domcontentloaded" plus the explicit settle-timeout that already
    # follows is more reliable, and a slow/partial load here shouldn't
    # abort the whole run when the fixed wait afterward is likely enough
    # for the SPA to finish rendering anyway.
    try:
        page.goto(
            "https://neetcode.io/practice/practice/neetcode150",
            wait_until="domcontentloaded", timeout=60000,
        )
    except Exception as e:
        print(f"Warning: initial page load didn't fully settle ({type(e).__name__}: {e}) -- continuing anyway.")
    page.wait_for_timeout(3000)

    categories = [
        "Arrays & Hashing",
        "Two Pointers",
        "Sliding Window",
        "Stack",
        "Binary Search",
        "Linked List",
        "Trees",
        "Heap / Priority Queue",
        "Backtracking",
        "Tries",
        "Graphs",
        "Advanced Graphs",
        "1-D Dynamic Programming",
        "2-D Dynamic Programming",
        "Greedy",
        "Intervals",
        "Math & Geometry",
        "Bit Manipulation",
    ]

    problems = []

    def open_category(page, category):
        header = page.get_by_text(category, exact=True).last
        if header.count() == 0:
            # Cheap safety net, no real cost when unused: falls back to a
            # substring match if a header's text node ever carries
            # something extra (a count badge, an icon label) alongside the
            # category name. A genuinely wrong name still fails below.
            header = page.get_by_text(category, exact=False).last
        header.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        header.click(force=True)
        page.wait_for_timeout(1000)

    for category in categories:
        print(f"Opening category: {category}")

        try:
            open_category(page, category)
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(500)
        except Exception as e:
            print(f"Could not open {category}: {type(e).__name__}: {e}")
            continue

        links = page.locator('a[href^="/problems/"]')

        for i in range(links.count()):
            link = links.nth(i)
            href = link.get_attribute("href")
            name = link.inner_text().strip()

            if not href or not name:
                continue

            row = link.locator("xpath=ancestor::*[self::div or self::tr][1]")

            # NeetCode's practice page also has promo "course card" links at
            # the top (e.g. "Python for Coding Interviews") that happen to
            # match a[href^="/problems/"] too, but aren't real problem rows
            # -- they have no status toggle at all. Real rows do, so this
            # doubles as the row-vs-not-a-row filter.
            status_btn = row.locator("button.neeter-status-toggle")
            if status_btn.count() == 0:
                continue

            completed = status_btn.get_attribute("aria-pressed") == "true"

            star_btn = row.locator("button.neeter-star-toggle")
            starred = star_btn.count() > 0 and star_btn.get_attribute("aria-pressed") == "true"

            # Strip the query string (e.g. "?list=neetcode150" or
            # "?list=allNC") -- it reflects which list page happened to
            # link here, not the problem's identity, and url is the
            # primary key. Leaving it in caused a real bug: switching scrape
            # targets between neetcode150 and allNC this session made the
            # same problem get stored under two different "identities".
            full_url = "https://neetcode.io" + href.split("?")[0]

            problems.append({
                "name": name,
                "category": category,
                "url": full_url,
                "completed": completed,
                "starred": starred,
            })

    # remove duplicates
    seen = set()
    unique = []

    for p in problems:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)

    return unique


def get_completed_problems(page):
    """Completed-only view of the NeetCode practice list -- the scrape path
    that feeds the `problems` (solved/reviewed) table."""
    return [p for p in scrape_neetcode_catalog(page) if p["completed"]]

'''
def get_completed_problems(page):
    page.goto(
        "https://neetcode.io/practice/practice/neetcode150",
        wait_until="networkidle"
    )

    page.wait_for_timeout(2000)

    problems = []

    sections = page.locator("h2")

    for i in range(sections.count()):
        category = sections.nth(i).inner_text()

        table = sections.nth(i).locator("xpath=following::table[1]")

        rows = table.locator("tbody tr")

        for r in range(rows.count()):
            row = rows.nth(r)

            links = row.locator("a")

            if links.count() == 0:
                continue

            href = links.first.get_attribute("href")

            name = links.first.inner_text()

            # green checkmark?
            completed = row.locator("svg").count() > 0

            if completed:
                problems.append({
                    "name": name,
                    "category": category,
                    "url": "https://neetcode.io" + href
                })

    return problems'''

SLUG_ALIASES = {
    "contains-duplicate": "duplicate-integer",
    "valid-anagram": "is-anagram",
    "two-sum": "two-integer-sum",
    "group-anagrams": "anagram-groups",
    "top-k-frequent-elements": "top-k-elements-in-list",
    "product-of-array-except-self": "products-of-array-discluding-self",
    "valid-sudoku": "valid-sudoku",
    "longest-consecutive-sequence": "longest-consecutive-sequence",
    "valid-palindrome": "is-palindrome",
    "two-sum-ii-input-array-is-sorted": "two-integer-sum-ii",
    "3sum": "three-integer-sum",
    "container-with-most-water": "max-water-container",
    "trapping-rain-water": "trapping-rain-water",
    "best-time-to-buy-and-sell-stock": "buy-and-sell-crypto",
    "longest-substring-without-repeating-characters": "longest-substring-without-duplicates",
    "longest-repeating-character-replacement": "longest-repeating-substring-with-replacement",
    "permutation-in-string": "permutation-string",

    # Found empirically: every one of these 45 was a NeetCode-catalogued
    # problem with difficulty stuck NULL, because its LeetCode-canonical
    # slug wasn't in this table -- NEETCODE_TO_LEETCODE_SLUG silently fell
    # back to the (wrong) NeetCode spelling, so the difficulty lookup and
    # the leetcode-solved dedup match in sync_leetcode_solved() both missed.
    # 38 of the 45 had already been duplicated into a second
    # source='leetcode' row as a result -- see dedupe_leetcode_duplicates.py.
    "binary-tree-level-order-traversal": "level-order-traversal-of-binary-tree",
    "combination-sum": "combination-target-sum",
    "combination-sum-ii": "combination-target-sum-ii",
    "copy-list-with-random-pointer": "copy-linked-list-with-random-pointer",
    "design-add-and-search-words-data-structure": "design-word-search-data-structure",
    "diameter-of-binary-tree": "binary-tree-diameter",
    "encode-and-decode-strings": "string-encode-and-decode",
    "find-the-duplicate-number": "find-duplicate-integer",
    "graph-valid-tree": "valid-tree",
    "happy-number": "non-cyclical-number",
    "implement-trie-prefix-tree": "implement-prefix-tree",
    "insert-interval": "insert-new-interval",
    "invert-binary-tree": "invert-a-binary-tree",
    "koko-eating-bananas": "eating-bananas",
    "kth-largest-element-in-a-stream": "kth-largest-integer-in-a-stream",
    "kth-smallest-element-in-a-bst": "kth-smallest-integer-in-bst",
    "letter-combinations-of-a-phone-number": "combinations-of-a-phone-number",
    "linked-list-cycle": "linked-list-cycle-detection",
    "lowest-common-ancestor-of-a-binary-search-tree": "lowest-common-ancestor-in-binary-search-tree",
    "maximum-depth-of-binary-tree": "depth-of-binary-tree",
    "meeting-rooms": "meeting-schedule",
    "meeting-rooms-ii": "meeting-schedule-ii",
    "merge-k-sorted-lists": "merge-k-sorted-linked-lists",
    "merge-two-sorted-lists": "merge-two-sorted-linked-lists",
    "min-stack": "minimum-stack",
    "number-of-1-bits": "number-of-one-bits",
    "number-of-connected-components-in-an-undirected-graph": "count-connected-components",
    "number-of-islands": "count-number-of-islands",
    "powx-n": "pow-x-n",
    "remove-nth-node-from-end-of-list": "remove-node-from-end-of-linked-list",
    "reorder-list": "reorder-linked-list",
    "reverse-linked-list": "reverse-a-linked-list",
    "rotate-image": "rotate-matrix",
    "rotting-oranges": "rotting-fruit",
    "same-tree": "same-binary-tree",
    "search-in-rotated-sorted-array": "find-target-in-rotated-sorted-array",
    "search-a-2d-matrix": "search-2d-matrix",
    "set-matrix-zeroes": "set-zeroes-in-matrix",
    "subtree-of-another-tree": "subtree-of-a-binary-tree",
    "task-scheduler": "task-scheduling",
    "unique-paths": "count-paths",
    "valid-parentheses": "validate-parentheses",
    "validate-binary-search-tree": "valid-binary-search-tree",
    "walls-and-gates": "islands-and-treasure",
    "word-search": "search-for-word",

    # Same failure again for these 15 (difficulty NULL because the lookup
    # fell back to the NeetCode spelling). Found by listing catalog rows
    # with no difficulty after a full sync.
    "minimum-window-substring": "minimum-window-with-characters",
    "construct-binary-tree-from-preorder-and-inorder-traversal": "binary-tree-from-preorder-and-inorder-traversal",
    "design-twitter": "design-twitter-feed",
    "find-median-from-data-stream": "find-median-in-a-data-stream",
    "word-search-ii": "search-for-word-ii",
    "reconstruct-itinerary": "reconstruct-flight-path",
    "min-cost-to-connect-all-points": "min-cost-to-connect-points",
    "alien-dictionary": "foreign-dictionary",
    "cheapest-flights-within-k-stops": "cheapest-flight-path",
    "best-time-to-buy-and-sell-stock-with-cooldown": "buy-and-sell-crypto-with-cooldown",
    "longest-increasing-path-in-a-matrix": "longest-increasing-path-in-matrix",
    "distinct-subsequences": "count-subsequences",
    "merge-triplets-to-form-target-triplet": "merge-triplets-to-form-target",
    "minimum-interval-to-include-each-query": "minimum-interval-including-query",
    "detect-squares": "count-squares",
}


def leetcode_slug(url):
    """Extract the problem slug from either URL shape in use:
    NeetCode's .../problems/<slug>/question?list=... or a bare
    LeetCode https://leetcode.com/problems/<slug>/. Taking the last path
    segment alone (the original version of this function) breaks on
    NeetCode URLs, whose last segment is the literal word "question"."""
    parts = urlparse(url).path.strip("/").split("/")
    if "problems" in parts:
        idx = parts.index("problems")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1] if parts else ""


def title_from_slug(slug):
    return slug.replace("-", " ").title()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS problems (
            url           TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            category      TEXT NOT NULL,
            starred       INTEGER NOT NULL DEFAULT 0,
            last_accepted TEXT,
            first_seen    TEXT NOT NULL,
            last_seen     TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'neetcode',
            difficulty    TEXT,
            topics        TEXT
        );
        CREATE TABLE IF NOT EXISTS review_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_url TEXT NOT NULL REFERENCES problems(url) ON DELETE CASCADE,
            reviewed_at TEXT NOT NULL,
            result      TEXT NOT NULL,
            notes       TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS catalog (
            url        TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            category   TEXT NOT NULL,
            difficulty TEXT,
            source     TEXT NOT NULL DEFAULT 'neetcode',
            last_seen  TEXT NOT NULL,
            completed  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS accepted_dates (
            problem_url   TEXT NOT NULL REFERENCES problems(url) ON DELETE CASCADE,
            accepted_date TEXT NOT NULL,
            PRIMARY KEY (problem_url, accepted_date)
        );
    """)

    # Migration path for a problems.db created before the 'source'/'difficulty'
    # columns existed (CREATE TABLE IF NOT EXISTS above is a no-op on an
    # existing table, so older DBs need these ALTER TABLEs run once).
    try:
        con.execute("ALTER TABLE problems ADD COLUMN source TEXT NOT NULL DEFAULT 'neetcode'")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        con.execute("ALTER TABLE problems ADD COLUMN difficulty TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        con.execute("ALTER TABLE problems ADD COLUMN topics TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        con.execute("ALTER TABLE catalog ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    con.commit()
    con.close()


def _mdy_to_iso(mdy: str) -> str:
    return datetime.strptime(mdy, "%m/%d/%Y").strftime("%Y-%m-%d")


_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")


def extract_all_accepted_dates(text):
    """Every distinct date (ISO 'YYYY-MM-DD') near an "Accepted" occurrence
    in the rendered submission-history page text, not just the first one --
    extract_latest_accepted (below) used to return on the first match,
    discarding every earlier Accepted submission's date.

    For each "Accepted" line, this checks nearby lines closest-first (within
    the same +-5/+8 window the original single-date version used), ties
    broken toward the line *after* (matching "Accepted\\n<date>", the
    primary format the original window size was tuned around) -- a plain
    search over the whole joined window would keep matching the same
    (topmost) date for every occurrence once multiple submissions are
    listed close together, which is exactly the bug this replaces."""
    if "Accepted" not in text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    dates = set()
    for i, line in enumerate(lines):
        if line == "Accepted" or "Accepted" in line:
            lo, hi = max(0, i - 5), min(len(lines), i + 8)
            candidates = sorted(range(lo, hi), key=lambda j: (abs(j - i), j <= i))
            for j in candidates:
                match = _DATE_RE.search(lines[j])
                if match:
                    dates.add(_mdy_to_iso(match.group(0)))
                    break

    if dates:
        return sorted(dates)

    # No date found near any specific "Accepted" line -- fall back to
    # whatever date appears anywhere in the text, same as the old
    # single-value behavior.
    match = _DATE_RE.search(text)
    return [_mdy_to_iso(match.group(0))] if match else []


def extract_latest_accepted(text):
    """The single most recent accepted date, in the original MM/DD/YYYY
    format -- kept for save_row()'s last_accepted column. Everything else
    should prefer extract_all_accepted_dates()."""
    dates = extract_all_accepted_dates(text)
    if not dates:
        return None
    return datetime.strptime(max(dates), "%Y-%m-%d").strftime("%m/%d/%Y")


from urllib.parse import urlparse

def check_problem(page, problem):
    """Returns (latest_accepted, all_accepted_dates): latest in
    'MM/DD/YYYY' for save_row()'s last_accepted column, all_accepted_dates
    as the full ISO-date list for accepted_dates (see
    extract_all_accepted_dates -- this page's text already contains every
    Accepted date, previously discarded down to just the latest)."""
    base = problem["url"].split("/question")[0]
    history_url = base + "/history"

    print(history_url)

    try:
        page.goto(history_url, wait_until="domcontentloaded", timeout=60000)

        page.wait_for_function(
            """() => document.body.innerText.includes("Accepted")
                || document.body.innerText.includes("Submissions")
                || document.body.innerText.includes("No submissions")""",
            timeout=15000
        )

    except Exception as e:
        print(f"Could not fully load {history_url}: {e}")

    text = page.locator("body").inner_text()
    all_dates = extract_all_accepted_dates(text)
    latest = datetime.strptime(max(all_dates), "%Y-%m-%d").strftime("%m/%d/%Y") if all_dates else None
    return latest, all_dates


def save_row(row):
    """Upsert a NeetCode-catalogued problem. Never deletes: a partial/failed
    scrape run can't wipe problems that were already recorded. first_seen is
    preserved across runs; everything else reflects the latest scrape.
    source is fixed to 'neetcode' -- this path is only ever used for
    problems read straight off NeetCode's own practice list.
    starred is OR-ed with the stored value rather than overwritten: a star
    set from the dashboard has no NeetCode-side counterpart, so a plain
    overwrite would wipe it on the next crawl."""
    name, category, url, starred, last_accepted, last_seen = row

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("""
        INSERT INTO problems (url, name, category, starred, last_accepted, first_seen, last_seen, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'neetcode')
        ON CONFLICT(url) DO UPDATE SET
            name          = excluded.name,
            category      = excluded.category,
            starred       = MAX(problems.starred, excluded.starred),
            last_accepted = excluded.last_accepted,
            last_seen     = excluded.last_seen
    """, (url, name, category, starred, last_accepted, last_seen, last_seen))

    con.commit()
    con.close()


def save_catalog_row(row):
    """Upsert one row of NeetCode's full practice catalog -- completed or not.
    Independent of `problems` (which only ever holds problems that have
    been solved): this is "everything that exists", so the dashboard can
    diff it against `problems` to recommend problems not yet attempted.
    Never deletes, for the same partial-scrape-safety reason as save_row.
    `completed` is NeetCode's own checkbox state and is overwritten every
    scrape, so unchecking a problem on NeetCode is reflected too."""
    name, category, url, completed, last_seen = row

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("""
        INSERT INTO catalog (url, name, category, source, last_seen, completed)
        VALUES (?, ?, ?, 'neetcode', ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            name      = excluded.name,
            category  = excluded.category,
            last_seen = excluded.last_seen,
            completed = excluded.completed
    """, (url, name, category, last_seen, completed))

    con.commit()
    con.close()


def save_accepted_dates(url, dates):
    """Record every distinct calendar date (ISO 'YYYY-MM-DD') this problem
    has an Accepted submission on -- the full history extract_latest_accepted
    and fetch_leetcode_accepted_date always discarded down to a single
    'latest' value. INSERT OR IGNORE: the (problem_url, accepted_date)
    primary key makes re-saving an already-known date a no-op, so this is
    safe to call repeatedly with overlapping date sets from either source."""
    if not dates:
        return
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.executemany(
        "INSERT OR IGNORE INTO accepted_dates (problem_url, accepted_date) VALUES (?, ?)",
        [(url, d) for d in dates],
    )
    con.commit()
    con.close()


# --- LeetCode cross-check -------------------------------------------------
#
# NeetCode's practice page only knows about problems solved through its own
# editor. Solving directly on leetcode.com never checks NeetCode's box, so
# this section reconciles against LeetCode's own solved-problems record,
# covering both NeetCode-catalogued problems solved elsewhere and problems
# outside NeetCode's catalog entirely (see docs/scheduling-comparison.md's sibling
# concerns -- schema decisions here are recorded in the conversation that
# added this feature, not a separate doc).
#
# NOTE: the two network calls below (LEETCODE_PROBLEMS_API,
# LEETCODE_GRAPHQL) hit LeetCode's own undocumented/legacy endpoints via the
# same CDP-attached browser context used for NeetCode, so they reuse
# whatever leetcode.com login already exists in that Chrome window. These
# have not been exercised against the live site from this environment --
# smoke-test a single run before relying on it.

LEETCODE_PROBLEMS_API = "https://leetcode.com/api/problems/all/"
LEETCODE_GRAPHQL = "https://leetcode.com/graphql/"

# SLUG_ALIASES keys are canonical leetcode.com slugs; values are the
# (occasionally different) slug NeetCode hosts the same problem under.
NEETCODE_TO_LEETCODE_SLUG = {neet: leet for leet, neet in SLUG_ALIASES.items()}


def get_leetcode_solved(context):
    """Every problem the logged-in LeetCode account has an accepted
    submission for. One request, no per-problem timestamp (LeetCode's bulk
    endpoint only exposes ac/notac/unattempted status)."""
    resp = context.request.get(LEETCODE_PROBLEMS_API)
    if not resp.ok:
        raise RuntimeError(f"LeetCode problems API request failed: HTTP {resp.status}")

    data = resp.json()
    if not data.get("user_name"):
        raise RuntimeError(
            "LeetCode API returned no logged-in user -- make sure the Chrome "
            "window attached via CDP (localhost:9222) is logged into leetcode.com."
        )

    solved = []
    for pair in data.get("stat_status_pairs", []):
        if pair.get("status") != "ac":
            continue
        stat = pair["stat"]
        solved.append({
            "slug": stat["question__title_slug"],
            "title": stat["question__title"],
        })
    return solved


def fetch_leetcode_accepted_dates(context, slug):
    """Every distinct calendar date (ISO 'YYYY-MM-DD') this problem has an
    Accepted submission on, via LeetCode's GraphQL submissionList query --
    structured JSON with unix timestamps, more robust than scraping
    rendered submission-history text. Limited to the 20 most recent
    submissions (LeetCode's own query shape); a problem re-submitted more
    than 20 times with a mix of outcomes could have older Accepted dates
    fall outside that window -- an existing limitation, not a new one.
    Returns [] on any failure rather than raising."""
    query = """
        query submissionList($offset: Int!, $limit: Int!, $questionSlug: String!) {
            submissionList(offset: $offset, limit: $limit, questionSlug: $questionSlug) {
                submissions { statusDisplay timestamp }
            }
        }
    """
    resp = context.request.post(
        LEETCODE_GRAPHQL,
        data=json.dumps({
            "query": query,
            "variables": {"offset": 0, "limit": 20, "questionSlug": slug},
        }),
        headers={
            "Content-Type": "application/json",
            "Referer": f"https://leetcode.com/problems/{slug}/",
        },
    )

    if not resp.ok:
        print(f"Could not fetch submission history for {slug}: HTTP {resp.status}")
        return []

    try:
        submissions = resp.json()["data"]["submissionList"]["submissions"]
    except (KeyError, TypeError, ValueError) as e:
        print(f"Unexpected submissionList response for {slug}: {e}")
        return []

    dates = {
        datetime.fromtimestamp(int(s["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d")
        for s in submissions if s.get("statusDisplay") == "Accepted"
    }
    return sorted(dates)


def fetch_leetcode_accepted_date(context, slug):
    """The single most recent accepted date, in 'MM/DD/YYYY' format --
    kept for save_row()'s last_accepted column. Everything else should
    prefer fetch_leetcode_accepted_dates()."""
    dates = fetch_leetcode_accepted_dates(context, slug)
    if not dates:
        return None
    return datetime.strptime(max(dates), "%Y-%m-%d").strftime("%m/%d/%Y")


def sync_leetcode_solved(context):
    """Reconcile LeetCode's solved-problems record into problems.db.

    Two passes:
      1. Upsert every currently-solved LeetCode problem. Ones matching an
         existing NeetCode-catalogued row (by slug, via NEETCODE_TO_LEETCODE_SLUG)
         just get last_seen bumped. New discoveries -- solved on LeetCode but
         outside NeetCode's catalog entirely -- are inserted as source='leetcode',
         category='Uncategorized'. Never overwrites an existing last_accepted
         or touches an existing row's category/source.
      2. Backfill last_accepted for up to MAX_PROBLEMS rows that still have
         none, oldest-touched-last-seen first, via a real per-problem date
         fetch. Capped so one run can't fire off hundreds of extra requests.
    """
    now = datetime.now(timezone.utc).isoformat()
    solved = get_leetcode_solved(context)
    print(f"LeetCode reports {len(solved)} solved problems.")

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")

    existing_neet_slugs = {
        leetcode_slug(url): url
        for (url,) in con.execute("SELECT url FROM problems WHERE source = 'neetcode'")
    }
    known_urls = {url for (url,) in con.execute("SELECT url FROM problems")}

    new_count = 0
    for item in solved:
        mapped_neet_slug = SLUG_ALIASES.get(item["slug"], item["slug"])
        matched_url = existing_neet_slugs.get(mapped_neet_slug)

        if matched_url:
            con.execute(
                "UPDATE problems SET last_seen = ? WHERE url = ?",
                (now, matched_url),
            )
            continue

        candidate_url = f"https://leetcode.com/problems/{item['slug']}/"
        if candidate_url in known_urls:
            con.execute(
                "UPDATE problems SET last_seen = ? WHERE url = ?",
                (now, candidate_url),
            )
            continue

        con.execute("""
            INSERT INTO problems (url, name, category, starred, last_accepted, first_seen, last_seen, source)
            VALUES (?, ?, 'Uncategorized', 0, NULL, ?, ?, 'leetcode')
        """, (candidate_url, item["title"], now, now))
        known_urls.add(candidate_url)
        new_count += 1

    con.commit()
    print(f"{new_count} newly discovered LeetCode-only problems added.")

    to_backfill = con.execute("""
        SELECT url, source FROM problems
        WHERE last_accepted IS NULL
        ORDER BY last_seen DESC
        LIMIT ?
    """, (MAX_PROBLEMS,)).fetchall()

    for url, source in to_backfill:
        if source == "leetcode":
            slug = leetcode_slug(url)
        else:
            slug = NEETCODE_TO_LEETCODE_SLUG.get(leetcode_slug(url), leetcode_slug(url))

        print(f"Backfilling accepted date for {slug} ...")
        accepted = fetch_leetcode_accepted_date(context, slug)
        if accepted:
            con.execute(
                "UPDATE problems SET last_accepted = ?, last_seen = ? WHERE url = ?",
                (accepted, now, url),
            )

    con.commit()
    con.close()


def sync_accepted_dates(context):
    """Backfill full accepted-date history for problems with zero rows in
    accepted_dates yet -- covers problems solved long before this table
    existed (already have a last_accepted, but only the single latest date
    was ever kept) as well as any row sync_leetcode_solved()'s own backfill
    hasn't reached. One GraphQL request per problem, capped at
    MAX_ACCEPTED_DATES_BACKFILL like sync_categories() -- a one-time
    catch-up, not an ongoing per-run drip."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")

    to_backfill = con.execute("""
        SELECT p.url, p.source FROM problems p
        WHERE NOT EXISTS (SELECT 1 FROM accepted_dates a WHERE a.problem_url = p.url)
        ORDER BY p.last_seen DESC
        LIMIT ?
    """, (MAX_ACCEPTED_DATES_BACKFILL,)).fetchall()

    updated = 0
    for url, source in to_backfill:
        raw_slug = leetcode_slug(url)
        slug = raw_slug if source == "leetcode" else NEETCODE_TO_LEETCODE_SLUG.get(raw_slug, raw_slug)

        print(f"Fetching accepted-date history for {slug} ...")
        dates = fetch_leetcode_accepted_dates(context, slug)
        if not dates:
            continue

        con.executemany(
            "INSERT OR IGNORE INTO accepted_dates (problem_url, accepted_date) VALUES (?, ?)",
            [(url, d) for d in dates],
        )
        # Also backfill last_accepted for a row that never had one -- never
        # overwrites an existing value, same rule as every other sync here.
        con.execute(
            "UPDATE problems SET last_accepted = COALESCE(last_accepted, ?) WHERE url = ?",
            (datetime.strptime(max(dates), "%Y-%m-%d").strftime("%m/%d/%Y"), url),
        )
        updated += 1

    con.commit()
    con.close()
    print(f"Accepted-date history backfilled for {updated} of {len(to_backfill)} attempted this run.")


# --- Difficulty tags -------------------------------------------------------
#
# LEETCODE_PROBLEMS_API's stat_status_pairs cover LeetCode's *entire*
# catalogue, not just solved problems, and each pair carries a
# {"difficulty": {"level": 1|2|3}} field alongside "stat"/"status". That
# makes it a one-request source of difficulty for every problem we track,
# regardless of whether it's been solved yet or came from NeetCode vs.
# LeetCode -- unlike get_leetcode_solved(), which only looks at "ac" pairs.

DIFFICULTY_LEVELS = {1: "Easy", 2: "Medium", 3: "Hard"}


def fetch_leetcode_difficulty_map(context):
    """{leetcode_slug: 'Easy'|'Medium'|'Hard'} for every problem LeetCode's
    bulk endpoint knows about. Same endpoint as get_leetcode_solved() but a
    separate request, since that function discards everything except
    currently-solved pairs and we need difficulty for unsolved ones too."""
    resp = context.request.get(LEETCODE_PROBLEMS_API)
    if not resp.ok:
        raise RuntimeError(f"LeetCode problems API request failed: HTTP {resp.status}")

    data = resp.json()
    difficulty_map = {}
    for pair in data.get("stat_status_pairs", []):
        slug = pair.get("stat", {}).get("question__title_slug")
        level = pair.get("difficulty", {}).get("level")
        label = DIFFICULTY_LEVELS.get(level)
        if slug and label:
            difficulty_map[slug] = label
    return difficulty_map


def _apply_difficulty_map(con, table, difficulty_map):
    """Shared UPDATE loop behind sync_difficulties, over whichever table
    (`problems` or `catalog`) is passed in -- both key difficulty off a
    LeetCode slug the same way. Rows LeetCode's map has no entry for (e.g.
    a stale/renamed slug) are left untouched rather than cleared."""
    rows = con.execute(f"SELECT url, source FROM {table}").fetchall()
    updated = 0
    for url, source in rows:
        raw_slug = leetcode_slug(url)
        slug = raw_slug if source == "leetcode" else NEETCODE_TO_LEETCODE_SLUG.get(raw_slug, raw_slug)
        difficulty = difficulty_map.get(slug)
        if difficulty is None:
            continue
        con.execute(
            f"UPDATE {table} SET difficulty = ? WHERE url = ? AND difficulty IS NOT ?",
            (difficulty, url, difficulty),
        )
        updated += 1
    return updated


def sync_difficulties(context):
    """Backfill/refresh difficulty for every row in `problems` and
    `catalog`, matching each problem's URL to a LeetCode slug the same way
    sync_leetcode_solved() does (NeetCode-catalogued rows go through
    NEETCODE_TO_LEETCODE_SLUG first, since NeetCode occasionally hosts a
    problem under a different slug than LeetCode's canonical one)."""
    difficulty_map = fetch_leetcode_difficulty_map(context)

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")

    updated = _apply_difficulty_map(con, "problems", difficulty_map)
    updated += _apply_difficulty_map(con, "catalog", difficulty_map)

    con.commit()
    con.close()
    print(f"Difficulty tagged/refreshed for {updated} problems.")


# --- Category backfill from LeetCode topic tags -----------------------------
#
# Unlike difficulty, topic tags aren't in LEETCODE_PROBLEMS_API's bulk
# response -- they only come back from a per-problem GraphQL query, same
# shape as fetch_leetcode_accepted_date(). And unlike difficulty (a single
# enum value with an obvious 1:1 mapping), a LeetCode problem carries
# *several* tags (e.g. "Two Sum" -> ["Array", "Hash Table"]) against this
# app's one-category-per-problem model. This section never overwrites an
# existing real category -- it only fills the 'Uncategorized' gap, the same
# rule sync_leetcode_solved() and sync_difficulties() already follow.

# High-confidence translations onto NeetCode's own curated bucket names, so
# a LeetCode-only problem lands in the same category a NeetCode-catalogued
# equivalent would use. Anything not listed here (String, Dynamic
# Programming, Design, Union Find, ...) is deliberately left untranslated --
# NeetCode splits or doesn't have an equivalent for these, so guessing wrong
# would be worse than just using LeetCode's own tag name as a new category.
LEETCODE_TAG_TO_CATEGORY = {
    "Array": "Arrays & Hashing",
    "Hash Table": "Arrays & Hashing",
    "Two Pointers": "Two Pointers",
    "Sliding Window": "Sliding Window",
    "Binary Search": "Binary Search",
    "Stack": "Stack",
    "Monotonic Stack": "Stack",
    "Linked List": "Linked List",
    "Tree": "Trees",
    "Binary Tree": "Trees",
    "Binary Search Tree": "Trees",
    "Graph": "Graphs",
    "Topological Sort": "Graphs",
    "Trie": "Tries",
    "Backtracking": "Backtracking",
    "Greedy": "Greedy",
    "Heap (Priority Queue)": "Heap / Priority Queue",
    "Bit Manipulation": "Bit Manipulation",
    "Math": "Math & Geometry",
    "Geometry": "Math & Geometry",
    "Matrix": "Math & Geometry",
    "Interval": "Intervals",
}

# Tags broad enough to show up on a huge fraction of problems regardless of
# the technique that actually defines them -- deprioritized when picking a
# single "primary" tag out of a problem's full list, so e.g. a sliding-window
# problem doesn't get flattened into "Arrays & Hashing" just because "Array"
# happened to be listed first.
GENERIC_LEETCODE_TAGS = {"Array", "String", "Math", "Hash Table", "Sorting", "Simulation", "Counting", "Enumeration"}


def pick_primary_category(tags):
    """One category label out of a problem's full LeetCode tag list: the
    first non-generic tag (see GENERIC_LEETCODE_TAGS), falling back to the
    first tag overall if every tag is generic, translated through
    LEETCODE_TAG_TO_CATEGORY where a confident mapping exists. 'Uncategorized'
    for an empty list (never raises on a problem with zero tags)."""
    if not tags:
        return "Uncategorized"
    specific = [t for t in tags if t not in GENERIC_LEETCODE_TAGS]
    chosen = specific[0] if specific else tags[0]
    return LEETCODE_TAG_TO_CATEGORY.get(chosen, chosen)


def fetch_leetcode_topic_tags(context, slug):
    """This problem's full list of LeetCode topic tag names (e.g.
    ["Array", "Two Pointers"]), via LeetCode's GraphQL question query.
    Returns [] on any failure rather than raising, so one bad slug can't
    abort a whole sync_categories() run."""
    query = """
        query questionTopicTags($titleSlug: String!) {
            question(titleSlug: $titleSlug) {
                topicTags { name }
            }
        }
    """
    resp = context.request.post(
        LEETCODE_GRAPHQL,
        data=json.dumps({
            "query": query,
            "variables": {"titleSlug": slug},
        }),
        headers={
            "Content-Type": "application/json",
            "Referer": f"https://leetcode.com/problems/{slug}/",
        },
    )

    if not resp.ok:
        print(f"Could not fetch topic tags for {slug}: HTTP {resp.status}")
        return []

    try:
        tags = resp.json()["data"]["question"]["topicTags"]
        return [t["name"] for t in tags]
    except (KeyError, TypeError, ValueError) as e:
        print(f"Unexpected topicTags response for {slug}: {e}")
        return []


def sync_categories(context):
    """Backfill category + the full topics tag list for problems still
    stuck at 'Uncategorized' -- almost entirely LeetCode-only discoveries,
    since NeetCode-catalogued rows always arrive with a real category
    already. One GraphQL request per problem (LeetCode's bulk endpoints
    don't expose tags), capped at MAX_CATEGORY_BACKFILL -- a one-time
    catch-up, not an ongoing per-run drip like the accepted-date backfill,
    so the cap is sized to clear the whole backlog in one run rather than
    dozens."""
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")

    to_backfill = con.execute("""
        SELECT url, source FROM problems
        WHERE category = 'Uncategorized' AND topics IS NULL
        ORDER BY last_seen DESC
        LIMIT ?
    """, (MAX_CATEGORY_BACKFILL,)).fetchall()

    updated = 0
    for url, source in to_backfill:
        raw_slug = leetcode_slug(url)
        slug = raw_slug if source == "leetcode" else NEETCODE_TO_LEETCODE_SLUG.get(raw_slug, raw_slug)

        print(f"Fetching topic tags for {slug} ...")
        tags = fetch_leetcode_topic_tags(context, slug)
        if not tags:
            continue

        con.execute(
            "UPDATE problems SET category = ?, topics = ? WHERE url = ? AND category = 'Uncategorized'",
            (pick_primary_category(tags), ", ".join(tags), url),
        )
        updated += 1

    con.commit()
    con.close()
    print(f"Category/topics tagged for {updated} of {len(to_backfill)} attempted this run.")


def main():
    init_db()
    now = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")

        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        catalog = scrape_neetcode_catalog(page)
        completed = [entry for entry in catalog if entry["completed"]]
        #completed = completed[:MAX_PROBLEMS]

        print(f"Found {len(catalog)} NeetCode catalog problems, {len(completed)} completed.")

        for entry in catalog:
            save_catalog_row((entry["name"], entry["category"], entry["url"], int(entry["completed"]), now))

        for i, problem in enumerate(completed, start=1):
            print(f"[{i}/{len(completed)}] {problem['name']}")

            last_accepted, all_dates = check_problem(page, problem)

            save_row((
                problem["name"],
                problem["category"],
                problem["url"],
                int(problem["starred"]),
                last_accepted,
                now,
            ))
            save_accepted_dates(problem["url"], all_dates)

        try:
            sync_leetcode_solved(context)
        except Exception as e:
            # This section hits LeetCode's own (unofficial) endpoints and
            # has not been exercised against the live site from this
            # environment -- if its response shape doesn't match what's
            # expected, fail loudly here without losing the NeetCode
            # scrape that already succeeded above.
            print(f"Skipping LeetCode sync -- {type(e).__name__}: {e}")

        try:
            sync_difficulties(context)
        except Exception as e:
            print(f"Skipping difficulty sync -- {type(e).__name__}: {e}")

        try:
            sync_accepted_dates(context)
        except Exception as e:
            print(f"Skipping accepted-date history sync -- {type(e).__name__}: {e}")

        try:
            sync_categories(context)
        except Exception as e:
            print(f"Skipping category sync -- {type(e).__name__}: {e}")

        browser.close()

    print("\nDone!")


if __name__ == "__main__":
    main()