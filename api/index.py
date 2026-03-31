#!/usr/bin/env python3
"""
Vercel serverless function: WHAT-IF activity summary.
Env vars required:
  GITHUB_TOKEN  — personal access token with repo read scope
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

# ── config ────────────────────────────────────────────────────────────────────
CALENDAR_ICS = os.environ.get("CALENDAR_ICS_URL", "")
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

GITHUB_USER = os.environ.get("GITHUB_USER", "")
GITHUB_REPOS = [r.strip() for r in os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]


# ── helpers ───────────────────────────────────────────────────────────────────

def load_patterns() -> list[re.Pattern]:
    raw = os.environ.get("MATCH_PATTERNS", "")
    patterns = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def to_local_datetime(dt_val) -> datetime:
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=ZoneInfo("UTC"))
        return dt_val.astimezone(LOCAL_TZ)
    if isinstance(dt_val, date):
        return datetime(dt_val.year, dt_val.month, dt_val.day, tzinfo=LOCAL_TZ)
    raise TypeError(f"Unexpected date type: {type(dt_val)}")


def event_text(component) -> str:
    """Concatenate summary and all attendee/organizer strings for pattern matching."""
    parts = [str(component.get("SUMMARY", ""))]
    for prop_name in ("ATTENDEE", "ORGANIZER"):
        val = component.get(prop_name)
        if val is None:
            continue
        if not isinstance(val, list):
            val = [val]
        parts.extend(str(v) for v in val)
    return " ".join(parts)


def matches(component, patterns: list[re.Pattern]) -> bool:
    text = event_text(component)
    return any(p.search(text) for p in patterns)


def duration_str(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def gh_api_get(path: str, token: str) -> list:
    """Paginate through a GitHub API endpoint and return all results."""
    results = []
    url = f"https://api.github.com/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    page = 1
    while True:
        resp = requests.get(url, headers=headers, params={"per_page": 100, "page": page}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results


# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_calendar(patterns: list[re.Pattern]) -> dict[date, list[tuple[str, int]]]:
    resp = requests.get(CALENDAR_ICS, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)

    days: dict[date, list[tuple[str, int]]] = defaultdict(list)
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        if not matches(component, patterns):
            continue
        summary = str(component.get("SUMMARY", "(no title)")).strip()
        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")
        if dtstart is None:
            continue
        start_dt = to_local_datetime(dtstart.dt)
        end_dt = to_local_datetime(dtend.dt) if dtend else start_dt
        duration_minutes = max(0, int((end_dt - start_dt).total_seconds() // 60))
        days[start_dt.date()].append((summary, duration_minutes))
    return days


def fetch_commits(token: str) -> dict[date, list[datetime]]:
    days: dict[date, list[datetime]] = defaultdict(list)
    for repo in GITHUB_REPOS:
        commits = gh_api_get(f"repos/{repo}/commits?author={GITHUB_USER}", token)
        for c in commits:
            iso = c["commit"]["author"]["date"]
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            days[dt.date()].append(dt)
    return days


# ── HTML rendering ────────────────────────────────────────────────────────────

def render_html(calendar_days, commit_days) -> str:
    all_days = sorted(set(calendar_days) | set(commit_days))

    rows = []
    for day in all_days:
        dow = day.strftime("%A")
        rows.append(f'<tr class="day-header"><td colspan="2"><strong>{day} &nbsp; {dow}</strong></td></tr>')

        if day in calendar_days:
            events = calendar_days[day]
            total_min = sum(m for _, m in events)
            rows.append(
                f'<tr><td class="label">Meetings ({len(events)}, {duration_str(total_min)} total)</td><td></td></tr>'
            )
            for title, mins in events:
                dur = f"[{duration_str(mins)}]" if mins else ""
                rows.append(f'<tr><td class="item">&#x2022; {title}</td><td class="dur">{dur}</td></tr>')

        if day in commit_days:
            times = sorted(commit_days[day])
            n = len(times)
            span_min = int((times[-1] - times[0]).total_seconds() // 60)
            span = duration_str(span_min) if span_min else "&lt;1m"
            rows.append(f'<tr><td class="label">GitHub commits: {n}</td><td class="dur">span: {span}</td></tr>')

        rows.append('<tr class="spacer"><td colspan="2"></td></tr>')

    rows_html = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WHAT-IF activity summary</title>
<style>
  body {{ font-family: monospace; font-size: 14px; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.2rem; border-bottom: 2px solid #333; padding-bottom: .4rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  tr.day-header td {{ padding-top: 1.2em; padding-bottom: .2em; }}
  tr.spacer td {{ height: .4em; }}
  td {{ padding: 1px 4px; vertical-align: top; }}
  td.label {{ color: #444; padding-left: 1em; }}
  td.item {{ padding-left: 2em; }}
  td.dur {{ color: #666; white-space: nowrap; text-align: right; }}
  .generated {{ color: #999; font-size: 12px; margin-top: 2rem; }}
</style>
</head>
<body>
<h1>WHAT-IF activity summary</h1>
<table>
{rows_html}
</table>
<p class="generated">Generated {datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")}</p>
</body>
</html>"""


def render_error(msg: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Error</title>
<style>body{{font-family:monospace;max-width:600px;margin:2rem auto;padding:0 1rem}}</style>
</head><body><h2>Error</h2><pre>{msg}</pre></body></html>"""


# ── Vercel handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        token = os.environ.get("GITHUB_TOKEN", "")
        try:
            if not CALENDAR_ICS:
                raise ValueError("CALENDAR_ICS_URL environment variable is not set.")
            if not token:
                raise ValueError("GITHUB_TOKEN environment variable is not set.")
            if not GITHUB_USER:
                raise ValueError("GITHUB_USER environment variable is not set.")
            if not GITHUB_REPOS:
                raise ValueError("GITHUB_REPOS environment variable is not set.")
            patterns = load_patterns()
            if not patterns:
                raise ValueError("MATCH_PATTERNS environment variable is not set.")
            calendar_days = fetch_calendar(patterns)
            commit_days = fetch_commits(token)
            body = render_html(calendar_days, commit_days)
            status = 200
        except Exception as e:
            body = render_error(str(e))
            status = 500

        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass  # suppress default access logs
