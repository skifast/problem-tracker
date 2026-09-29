"""
Sends a daily email with 3 problem recommendations: the 2 top review picks
from the same logic behind Today's Queue (build_queue) plus 1 new problem
from the same logic behind Recommended (recommend_new_problems).

No live browser needed at send time -- this reads problems.db directly, off
whatever tracker.py last scraped. Reuses dashboard.py's functions rather
than reimplementing selection logic, so this stays in sync with the
dashboard automatically as that logic changes.

One-time setup:
  1. Generate a Gmail App Password and fill it into email_config.py --
     see that file's docstring for the exact steps.
  2. Test without sending anything:  python3 send_daily_email.py --dry-run
  3. Send for real once:             python3 send_daily_email.py
  4. Schedule it daily: see the launchd .plist and setup notes shipped
     alongside this file (com.problemtracker.dailyemail.plist).

Run: python3 send_daily_email.py [--dry-run]
"""

import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd

import dashboard as dash
import email_config as cfg

REVIEW_CAPACITY = 2
NEW_PICK_COUNT = 1


def pick_problems():
    """Returns (review_df, new_df) -- review_df from the same build_queue()
    logic as Today's Queue (capacity=2, so just the top 2 by urgency), new_df
    from the same recommend_new_problems() logic as the Recommended tab
    (gap-category first, then weakest, capped to the single top pick).
    new_df is None if catalog is empty (tracker.py hasn't populated it)."""
    dash.init_db()
    df = dash.load_problems()
    done = dash.reviewed_today()

    review_df = dash.build_queue(df, REVIEW_CAPACITY, done)

    catalog_df = dash.load_catalog()
    if catalog_df.empty:
        return review_df, None

    weak_df = dash.weak_categories(df)
    new_df = dash.recommend_new_problems(catalog_df, df, weak_df, per_category=NEW_PICK_COUNT)
    return review_df, new_df.head(NEW_PICK_COUNT)


def _difficulty_label(row) -> str:
    difficulty = row.get("difficulty")
    return difficulty if difficulty and not pd.isna(difficulty) else ""


def render_problem_html(row, reason: str = "") -> str:
    diff = _difficulty_label(row)
    diff_html = f' <span style="color:#666;">({diff})</span>' if diff else ""
    reason_html = (
        f'<div style="color:#888;font-size:13px;margin-top:2px;">\U0001f4a1 {reason}</div>'
        if reason else ""
    )
    return f"""
    <div style="margin-bottom:14px;padding:10px 14px;border:1px solid #e0e0e0;border-radius:6px;">
      <div style="font-weight:600;font-size:15px;">
        <a href="{row['url']}" style="color:#1a5fa8;text-decoration:none;">{row['name']}</a>{diff_html}
      </div>
      <div style="color:#666;font-size:13px;">{row['category']}</div>
      {reason_html}
    </div>
    """


def build_email_html(review_df: pd.DataFrame, new_df) -> str:
    parts = ['<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:520px;">']
    parts.append("<h2 style=\"margin-bottom:4px;\">\U0001f9e0 Today's 3 Problems</h2>")

    parts.append('<h3 style="margin:16px 0 8px;color:#1a2744;">\U0001f4cb Review</h3>')
    if review_df is None or review_df.empty:
        parts.append('<p style="color:#888;">Nothing due for review today — nice work.</p>')
    else:
        for _, row in review_df.iterrows():
            parts.append(render_problem_html(row))

    parts.append('<h3 style="margin:16px 0 8px;color:#1a2744;">\U0001f195 New</h3>')
    if new_df is None or new_df.empty:
        parts.append(
            '<p style="color:#888;">No new recommendation available — run tracker.py to '
            'refresh the catalog.</p>'
        )
    else:
        for _, row in new_df.iterrows():
            parts.append(render_problem_html(row, reason=row.get("reason", "")))

    parts.append(
        '<p style="color:#aaa;font-size:12px;margin-top:24px;">Sent by your Problem Tracker.</p>'
    )
    parts.append("</div>")
    return "".join(parts)


def send_email(html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "\U0001f9e0 Today's 3 problems"
    msg["From"] = cfg.EMAIL_ADDRESS
    msg["To"] = cfg.SEND_TO
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(cfg.EMAIL_ADDRESS, cfg.EMAIL_APP_PASSWORD)
        server.sendmail(cfg.EMAIL_ADDRESS, [cfg.SEND_TO], msg.as_string())


def _print_dry_run(review_df, new_df):
    print("--- DRY RUN: nothing sent ---")
    print(f"From: {cfg.EMAIL_ADDRESS}  To: {cfg.SEND_TO}\n")
    print("Review:")
    if review_df is None or review_df.empty:
        print("  (nothing due)")
    else:
        for _, row in review_df.iterrows():
            print(f"  - {row['name']} ({row['category']}) {row['url']}")
    print("New:")
    if new_df is None or new_df.empty:
        print("  (none available)")
    else:
        for _, row in new_df.iterrows():
            print(f"  - {row['name']} ({row['category']}) {row['url']}  -- {row.get('reason', '')}")


def main():
    dry_run = "--dry-run" in sys.argv
    review_df, new_df = pick_problems()

    if dry_run:
        _print_dry_run(review_df, new_df)
        return

    if not cfg.EMAIL_APP_PASSWORD:
        raise SystemExit(
            "EMAIL_APP_PASSWORD is empty in email_config.py -- generate a Gmail App Password "
            "(https://myaccount.google.com/apppasswords) and fill it in first. "
            "Use --dry-run to preview without needing that yet."
        )

    send_email(build_email_html(review_df, new_df))
    print("Email sent.")


if __name__ == "__main__":
    main()
