# WHAT-IF Activity Summary

Generates a daily overview of WHAT-IF project activity from:
- **Outlook calendar** — meetings matching configurable patterns (title keywords or attendee domains)
- **GitHub** — commits by a given user across configured repositories

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `CALENDAR_ICS_URL` | Outlook published calendar ICS URL (see `.env.example` for how to get this) |
| `GITHUB_TOKEN` | GitHub personal access token with `repo` read scope |
| `GITHUB_USER` | GitHub username whose commits to scan |
| `GITHUB_REPOS` | Comma-separated list of repos to scan (`owner/repo` format) |
| `MATCH_PATTERNS` | Pipe-separated (`\|`) regex patterns matched against event titles and attendee addresses |

## Local usage

**Requirements:** Python 3.11+

Install dependencies:

```bash
pip install icalendar requests
```

Run the development server:

```bash
python3 api/index.py
```

Then open http://localhost:8000. The `.env` file is loaded automatically.

To use a different port:

```bash
python3 api/index.py 9000
```

## Deploying to Vercel

1. Push this repository to GitHub
2. Go to [vercel.com/new](https://vercel.com/new) and import the repository
3. Leave **Root Directory** as `/` (no change needed)
4. Under **Environment Variables**, add all five variables from your `.env`
   - `MATCH_PATTERNS` is a single-line pipe-separated string, e.g. `what-?if|WP[1-8]|@example\.com`
5. Click **Deploy**

On each visit to the deployed URL, the page is regenerated live from the calendar and GitHub APIs.

> **Note:** Vercel's hobby plan has a 10-second function timeout. The page typically loads in 3–5 seconds, but if your calendar is very large or you have many repos it may be worth upgrading to Pro (60-second limit).
