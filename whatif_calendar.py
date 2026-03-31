#!/usr/bin/env python3
"""
Summarize WHAT-IF related calendar events and GitHub commits per day.

Calendar: fetches the Outlook ICS feed, filters events that:
  - contain "WHAT-IF" (case-insensitive) in the title, OR
  - have any attendee / organizer matching an email from emails.txt

GitHub: fetches commits authored by GITHUB_USER from GITHUB_REPOS,
  grouped by day (time = span between first and last commit that day).

Outputs a combined per-day summary.
"""

import subprocess
import json
import sys
import re
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import os

import requests
from icalendar import Calendar, vDatetime

# ── config ────────────────────────────────────────────────────────────────────
CALENDAR_ICS = os.environ["CALENDAR_ICS_URL"]  # set in environment or .env
EMAILS_FILE = Path(__file__).parent / "emails.txt"
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

GITHUB_USER = os.environ["GITHUB_USER"]
GITHUB_REPOS = [r.strip() for r in os.environ["GITHUB_REPOS"].split(",") if r.strip()]


def load_emails(path: Path) -> set[str]:
    emails = set()
    for line in path.read_text().splitlines():
        line = line.strip().lower()
        if "@" in line:
            emails.add(line)
    return emails


def to_local_datetime(dt_val, tzinfo_fallback=None) -> datetime:
    """Convert a vDatetime / date / datetime to a timezone-aware local datetime."""
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            if tzinfo_fallback:
                dt_val = dt_val.replace(tzinfo=tzinfo_fallback)
            else:
                dt_val = dt_val.replace(tzinfo=ZoneInfo("UTC"))
        return dt_val.astimezone(LOCAL_TZ)
    if isinstance(dt_val, date):
        # All-day event: treat as midnight local time
        return datetime(dt_val.year, dt_val.month, dt_val.day, tzinfo=LOCAL_TZ)
    raise TypeError(f"Unexpected date type: {type(dt_val)}")


def event_emails(component) -> set[str]:
    """Return all email addresses mentioned in an event (attendees + organizer)."""
    emails = set()
    for prop_name in ("ATTENDEE", "ORGANIZER"):
        val = component.get(prop_name)
        if val is None:
            continue
        # Can be a list (multiple attendees) or a single value
        if not isinstance(val, list):
            val = [val]
        for v in val:
            addr = str(v)
            # mailto:someone@example.com  or  CN=Name:MAILTO:someone@example.com
            m = re.search(r"mailto:([^\s;>]+)", addr, re.IGNORECASE)
            if m:
                emails.add(m.group(1).lower())
    return emails


def matches(component, target_emails: set[str]) -> bool:
    summary = str(component.get("SUMMARY", ""))
    if "what-if" in summary.lower() or "whatif" in summary.lower():
        return True
    if event_emails(component) & target_emails:
        return True
    return False


def duration_str(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if m:
        return f"{h}h{m:02d}m"
    return f"{h}h"


def gh_api(path: str) -> list:
    """Call `gh api` with pagination, return combined list of results."""
    results = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        cmd = ["gh", "api", f"{path}{sep}per_page=100&page={page}"]
        out = subprocess.check_output(cmd)
        data = json.loads(out)
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results


def fetch_github_commits() -> dict[date, list[datetime]]:
    """Return {day: [commit_datetimes]} for all WHAT-IF repos."""
    print("Fetching GitHub commits…", file=sys.stderr)
    days: dict[date, list[datetime]] = defaultdict(list)
    for repo in GITHUB_REPOS:
        commits = gh_api(f"repos/{repo}/commits?author={GITHUB_USER}")
        for c in commits:
            iso = c["commit"]["author"]["date"]  # e.g. 2026-03-25T10:56:49Z
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            days[dt.date()].append(dt)
    return days


def main():
    target_emails = load_emails(EMAILS_FILE)
    print(f"Loaded {len(target_emails)} target email addresses.", file=sys.stderr)

    print("Fetching calendar…", file=sys.stderr)
    resp = requests.get(CALENDAR_ICS, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    cal = Calendar.from_ical(resp.content)

    # day → list of (title, duration_minutes)
    days: dict[date, list[tuple[str, int]]] = defaultdict(list)

    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        if not matches(component, target_emails):
            continue

        summary = str(component.get("SUMMARY", "(no title)")).strip()
        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")
        if dtstart is None:
            continue

        start_dt = to_local_datetime(dtstart.dt)
        if dtend is not None:
            end_dt = to_local_datetime(dtend.dt)
        else:
            end_dt = start_dt  # no end means zero duration

        duration_minutes = max(0, int((end_dt - start_dt).total_seconds() // 60))
        event_date = start_dt.date()
        days[event_date].append((summary, duration_minutes))

    commit_days = fetch_github_commits()

    all_days = sorted(set(days) | set(commit_days))

    if not all_days:
        print("No matching events or commits found.")
        return

    print(f"\n{'='*60}")
    print("WHAT-IF activity summary")
    print(f"{'='*60}\n")

    for day in all_days:
        dow = day.strftime("%A")
        print(f"{day}  {dow}")

        # Calendar events
        if day in days:
            events = days[day]
            total_min = sum(m for _, m in events)
            print(f"  Meetings ({len(events)}, {duration_str(total_min)} total):")
            for title, mins in events:
                dur = f"  [{duration_str(mins)}]" if mins else ""
                print(f"    • {title}{dur}")

        # GitHub commits
        if day in commit_days:
            commit_times = sorted(commit_days[day])
            n = len(commit_times)
            span_min = int((commit_times[-1] - commit_times[0]).total_seconds() // 60)
            span = duration_str(span_min) if span_min else "<1m"
            print(f"  GitHub commits: {n}  (span: {span})")

        print()


if __name__ == "__main__":
    main()
