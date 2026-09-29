"""One-off diagnostic: connect to the already-open, CDP-debuggable Chrome,
open the Arrays & Hashing category on NeetCode's practice page, then find a
REAL problem row (filtered by having a difficulty label like Easy/Medium/
Hard nearby, to skip the unrelated course-card promo links) and dump its
exact markup -- so the checkbox/star selectors can be fixed against ground
truth instead of guessed at.

Run: ./venv/bin/python3 debug_dom.py
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

OUT_DIR = Path(__file__).parent / "debug_out"
OUT_DIR.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()

    page.goto("https://neetcode.io/practice/practice/neetcode150", wait_until="networkidle")
    page.wait_for_timeout(3000)

    body_text = page.locator("body").inner_text()
    print("Logged in (no Sign In text):", "Sign In" not in body_text)

    try:
        header = page.get_by_text("Arrays & Hashing", exact=True).last
        header.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        header.click(force=True)
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"Could not click category header: {e}")

    page.mouse.wheel(0, 600)
    page.wait_for_timeout(800)
    page.screenshot(path=str(OUT_DIR / "03_logged_in.png"), full_page=False)

    links = page.locator('a[href^="/problems/"]')
    count = links.count()
    print(f"\nTotal a[href^='/problems/'] links on page: {count}")

    real_row = None
    real_link = None
    for i in range(count):
        link = links.nth(i)
        href = link.get_attribute("href")
        row = link.locator("xpath=ancestor::*[self::div or self::tr][1]")
        row_text = row.inner_text()
        is_real = any(diff in row_text for diff in ("Easy", "Medium", "Hard"))
        print(f"  [{i}] href={href!r} looks_like_real_row={is_real}")
        if is_real and real_row is None:
            real_row = row
            real_link = link

    if real_row is None:
        print("\nNo row with a difficulty label found near any link -- need a wider ancestor search.")
        browser.close()
        raise SystemExit(1)

    print("\n=== Found a real problem row ===")
    print("Problem name:", real_link.inner_text().strip())
    print("\n--- outerHTML of the row ---")
    print(real_row.evaluate("el => el.outerHTML"))

    # How many candidate "checkbox-like" controls exist in this row, under
    # several possible implementations NeetCode might use today.
    print("\n--- control counts inside this row ---")
    print("input[type=checkbox]:", real_row.locator("input[type='checkbox']").count())
    print("[role=checkbox]:", real_row.locator("[role='checkbox']").count())
    print("button:", real_row.locator("button").count())
    print("svg:", real_row.locator("svg").count())
    print("input (any type):", real_row.locator("input").count())

    browser.close()
