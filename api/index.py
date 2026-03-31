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
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

# ── config ────────────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


# ── helpers ───────────────────────────────────────────────────────────────────

def load_patterns() -> list[re.Pattern]:
    raw = os.environ.get("MATCH_PATTERNS", "")
    patterns = []
    for part in raw.split("|"):
        part = part.strip()
        if part:
            patterns.append(re.compile(part, re.IGNORECASE))
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

def fetch_calendar(patterns: list[re.Pattern], calendar_ics: str) -> dict[date, list[tuple[str, int]]]:
    resp = requests.get(calendar_ics, timeout=30, allow_redirects=True)
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


def fetch_commits(token: str, github_user: str, github_repos: list[str]) -> dict[date, list[datetime]]:
    days: dict[date, list[datetime]] = defaultdict(list)
    for repo in github_repos:
        commits = gh_api_get(f"repos/{repo}/commits?author={github_user}", token)
        for c in commits:
            iso = c["commit"]["author"]["date"]
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            days[dt.date()].append(dt)
    return days


# ── HTML rendering ────────────────────────────────────────────────────────────

def iso_week_label(d: date) -> str:
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def group_by_week(all_days: list[date]) -> list[tuple[str, date, list[date]]]:
    """Return [(week_label, monday, [days])] sorted most-recent-first."""
    weeks: dict[str, tuple[date, list[date]]] = {}
    for d in all_days:
        label = iso_week_label(d)
        if label not in weeks:
            weeks[label] = (week_monday(d), [])
        weeks[label][1].append(d)
    return [(label, mon, days) for label, (mon, days) in sorted(weeks.items(), reverse=True)]


def render_day(day: date, calendar_days, commit_days) -> str:
    dow = day.strftime("%a")
    lines = []

    meeting_min = 0
    if day in calendar_days:
        events = calendar_days[day]
        meeting_min = sum(m for _, m in events)
        meeting_items = "".join(
            f'<div class="ms-3 text-body-secondary small">{title}'
            f' <span class="text-muted">[{duration_str(mins)}]</span></div>'
            if mins else
            f'<div class="ms-3 text-body-secondary small">{title}</div>'
            for title, mins in events
        )
        lines.append(
            f'<div>Meetings: {len(events)}, {duration_str(meeting_min)}</div>'
            f'{meeting_items}'
        )

    commit_min = 0
    if day in commit_days:
        times = sorted(commit_days[day])
        n = len(times)
        commit_min = int((times[-1] - times[0]).total_seconds() // 60)
        span = duration_str(commit_min) if commit_min else "&lt;1m"
        lines.append(f'<div>GitHub commits: {n} (span: {span})</div>')

    day_total = meeting_min + commit_min
    content = "\n".join(lines)
    total_badge = f'<span class="badge bg-secondary-subtle text-secondary-emphasis rounded-pill">{duration_str(day_total)}</span>' if day_total else ''

    return f"""<div class="d-flex align-items-start gap-2 mb-2">
  <div style="min-width:110px"><strong>{day}</strong> <span class="text-muted">{dow}</span></div>
  <div class="flex-grow-1">{content}</div>
  <div>{total_badge}</div>
</div>"""


def render_html(calendar_days, commit_days) -> str:
    all_days = sorted(set(calendar_days) | set(commit_days))
    weeks = group_by_week(all_days)

    accordion_items = []
    for i, (label, monday, days) in enumerate(weeks):
        week_id = label.replace("-", "").replace("W", "w")
        friday = monday + timedelta(days=4)

        # Week totals
        total_meeting_min = sum(
            sum(m for _, m in calendar_days[d]) for d in days if d in calendar_days
        )
        total_commit_min = sum(
            int((sorted(commit_days[d])[-1] - sorted(commit_days[d])[0]).total_seconds() // 60)
            for d in days if d in commit_days
        )
        total_min = total_meeting_min + total_commit_min
        n_days = len(days)

        day_html = "\n".join(render_day(d, calendar_days, commit_days) for d in sorted(days))

        expanded = "true" if i == 0 else "false"
        show = " show" if i == 0 else ""
        collapsed = "" if i == 0 else " collapsed"

        accordion_items.append(f"""
<div class="accordion-item">
  <h2 class="accordion-header">
    <button class="accordion-button{collapsed}" type="button"
            data-bs-toggle="collapse" data-bs-target="#{week_id}"
            aria-expanded="{expanded}" aria-controls="{week_id}">
      <div class="d-flex w-100 justify-content-between align-items-center pe-2">
        <span><strong>{label}</strong>
          <span class="text-muted ms-2">{monday.strftime("%d %b")} &ndash; {friday.strftime("%d %b %Y")}</span>
        </span>
        <span>
          <span class="badge bg-primary rounded-pill">{duration_str(total_min)}</span>
          <span class="badge bg-secondary rounded-pill">{n_days}d</span>
        </span>
      </div>
    </button>
  </h2>
  <div id="{week_id}" class="accordion-collapse collapse{show}"
       data-bs-parent="#weekAccordion">
    <div class="accordion-body">
      {day_html}
    </div>
  </div>
</div>""")

    accordion_html = "\n".join(accordion_items)
    generated = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")

    return f"""<!DOCTYPE html>
<html lang="en" data-bs-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WHAT-IF activity summary</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
      rel="stylesheet" crossorigin="anonymous">
<style>
  .accordion-button::after {{ flex-shrink: 0; margin-left: 0.5rem; }}
  .accordion-button {{ font-size: 0.95rem; }}
  .accordion-body {{ font-size: 0.9rem; }}
</style>
</head>
<body>
<div class="container py-4" style="max-width: 850px;">
  <h1 class="h4 mb-3">WHAT-IF activity summary</h1>
  <div class="accordion" id="weekAccordion">
    {accordion_html}
  </div>
  <p class="text-muted small mt-3">Generated {generated}</p>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
        crossorigin="anonymous"></script>
</body>
</html>"""


def render_error(msg: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Error</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
      rel="stylesheet" crossorigin="anonymous">
</head><body>
<div class="container py-4"><div class="alert alert-danger"><h5>Error</h5><pre class="mb-0">{msg}</pre></div></div>
</body></html>"""


# ── Vercel handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        calendar_ics = os.environ.get("CALENDAR_ICS_URL", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        github_user = os.environ.get("GITHUB_USER", "")
        github_repos = [r.strip() for r in os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]
        try:
            if not calendar_ics:
                raise ValueError("CALENDAR_ICS_URL environment variable is not set.")
            if not token:
                raise ValueError("GITHUB_TOKEN environment variable is not set.")
            if not github_user:
                raise ValueError("GITHUB_USER environment variable is not set.")
            if not github_repos:
                raise ValueError("GITHUB_REPOS environment variable is not set.")
            patterns = load_patterns()
            if not patterns:
                raise ValueError("MATCH_PATTERNS environment variable is not set.")
            calendar_days = fetch_calendar(patterns, calendar_ics)
            commit_days = fetch_commits(token, github_user, github_repos)
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


if __name__ == "__main__":
    from http.server import HTTPServer
    from pathlib import Path
    import sys

    # Load .env from project root if present
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ[key.strip()] = value.strip()

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Serving on http://localhost:{port}")
    HTTPServer(("", port), handler).serve_forever()
