# Flight price watcher — apollorejser.dk

Polls three candidate departure dates (Aug 1 / 8 / 15, 2026) for a
CPH 7-day round-trip on apollorejser.dk and emails when any drops
below a configurable DKK threshold.

## How it works

- A GitHub Actions cron runs `check.py` every 10 min.
- `check.py` opens each URL in headless Chromium (Playwright), waits
  for the SPA to render, scrapes the lowest plausible DKK price, and
  emails via Gmail SMTP if any price < `THRESHOLD_DKK` (default 2000).
- `state.json` records the last alerted price per date so you don't
  get spammed every 10 min while a price is below threshold — you only
  get re-alerted if the price drops *further*. If the price recovers
  above threshold, the next dip re-alerts.

## Important caveats

- **Make the repo public** if you keep the 10-min cadence. Headless
  Chromium runs take ~1–2 min, and `*/10 * * * *` × 144 runs/day eats
  more than the 2000 free private-repo minutes/month. Public repos are
  unlimited.
- **GitHub cron is best-effort.** It can be delayed 10–30 min under
  load. Treat "every 10 min" as "roughly every 10–40 min".
- **Polling that often looks bot-ish.** If apollorejser.dk starts
  serving empty results / blocking, slow to `*/30 * * * *` in
  `.github/workflows/check.yml`.

## One-time setup

### 1. Push this folder as its own GitHub repo

The workflow file MUST be at the repo's root in `.github/workflows/`,
so push `flights_check/` as the repo root — don't nest it.

```powershell
cd c:\Users\kspyr\Desktop\flights_check
git init
git add .
git commit -m "Initial flight price watcher"
gh repo create flights_check --public --source=. --push
```

(Or do it through the GitHub web UI — make sure to set it **public**.)

### 2. Generate a Gmail app password

1. Go to https://myaccount.google.com/security and turn on
   **2-Step Verification** if you haven't already.
2. Go to https://myaccount.google.com/apppasswords.
3. Create a new app password (name it e.g. "flight-price-watcher").
4. Copy the 16-character password.

### 3. Add repo secrets

In GitHub → your repo → **Settings → Secrets and variables → Actions
→ New repository secret**, add:

| Name                  | Value                                  |
| --------------------- | -------------------------------------- |
| `GMAIL_USER`          | your Gmail address (the sender)        |
| `GMAIL_APP_PASSWORD`  | the 16-char app password from step 2   |
| `EMAIL_TO`            | recipient address (often the same one) |

### 4. Test it

In the **Actions** tab, find the **Flight price check** workflow and
click **Run workflow**. Watch the logs:

- The "Run price check" step should print three lines like
  `2026-08-01: 2399`. If you see `None`, the price selector or wait
  logic needs tweaking — see "Debugging" below.
- If any price < 2000 DKK, you should get an email within seconds.

To force a test email regardless of price, temporarily set
`THRESHOLD_DKK: "100000"` in the workflow's `env:` block, run once,
then revert.

## Tuning

- **Threshold**: change `THRESHOLD_DKK: "2000"` in
  `.github/workflows/check.yml`.
- **Cadence**: change the `cron:` line. Common values:
  - `"*/10 * * * *"` — every 10 min (default; aggressive)
  - `"*/30 * * * *"` — every 30 min (recommended)
  - `"0 */6 * * *"` — every 6 hours (gentle)
- **Add/remove dates**: edit the `DATES` list in `check.py`.

## Debugging

If `check.py` consistently returns `None`:

1. Run locally:
   ```powershell
   cd c:\Users\kspyr\Desktop\flights_check
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python -m playwright install chromium
   $env:GMAIL_USER="your-gmail@gmail.com"
   $env:GMAIL_APP_PASSWORD="your-app-password"
   $env:THRESHOLD_DKK="100000"   # force email
   python check.py
   ```
2. To see the browser, change `browser = await p.chromium.launch(...)`
   in `check.py` to `launch(headless=False, ...)` and run locally.
3. If the cookie banner blocks rendering, inspect it in the visible
   browser and add the right selector to the `for sel in [...]` loop.
4. If prices render but the regex doesn't match them, copy a price
   string from the rendered page and adjust `PRICE_RE`.

## Files

- `check.py` — scraper + email logic
- `requirements.txt` — `playwright`
- `.github/workflows/check.yml` — cron + secrets wiring
- `state.json` — last-alerted price per date (committed by the
  workflow on each run)
