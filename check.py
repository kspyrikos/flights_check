"""Apollorejser.dk flight price watcher.

Opens each candidate departure date in a headless Chromium, scrapes the
lowest plausible DKK price visible, and emails the configured recipient
via Gmail SMTP if any price drops below THRESHOLD_DKK. Sender, app
password, and recipient are read from env vars (GMAIL_USER,
GMAIL_APP_PASSWORD, EMAIL_TO) so no addresses live in the repo.

State is persisted in state.json so the same low price isn't emailed on
every run; we only re-alert when the price goes lower than what we last
alerted for that date, or after a price has recovered above the
threshold and dropped again.
"""

import asyncio
import json
import os
import random
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

# Each tuple: (destination, departure date, search URL, alert threshold in DKK).
DATES = [
    (
        "Samos",
        "2026-08-01",
        "https://www.apollorejser.dk/booking-guide/flight/list?departureAirportCode=CPH&travelAreaUri=der%3Aairport%3Adtno%3A1206265&paxAges=18&searchProductCategoryCodes=TwoWayFlightOnly&departureDate=2026-08-01&endDate=&duration=7&searchType=NotSet&abTestVisualDestinations=true",
        2000,
    ),
    (
        "Samos",
        "2026-08-08",
        "https://www.apollorejser.dk/booking-guide/flight/list?departureAirportCode=CPH&travelAreaUri=der%3Aairport%3Adtno%3A1206265&paxAges=18&searchProductCategoryCodes=TwoWayFlightOnly&departureDate=2026-08-08&endDate=&duration=7&searchType=NotSet&abTestVisualDestinations=true",
        2000,
    ),
    (
        "Samos",
        "2026-08-15",
        "https://www.apollorejser.dk/booking-guide/flight/list?departureAirportCode=CPH&travelAreaUri=der%3Aairport%3Adtno%3A1206265&paxAges=18&searchProductCategoryCodes=TwoWayFlightOnly&departureDate=2026-08-15&endDate=&duration=7&searchType=NotSet&abTestVisualDestinations=true",
        2000,
    ),
    (
        "Ioannina",
        "2026-08-03",
        "https://www.apollorejser.dk/booking-guide/flight/list?departureAirportCode=CPH&travelAreaUri=der%3Aairport%3Adtno%3A1206225&paxAges=18&searchProductCategoryCodes=TwoWayFlightOnly&departureDate=2026-08-03&duration=7&searchType=NotSet&abTestVisualDestinations=true",
        2500,
    ),
    (
        "Ioannina",
        "2026-08-10",
        "https://www.apollorejser.dk/booking-guide/flight/list?departureAirportCode=CPH&travelAreaUri=der%3Aairport%3Adtno%3A1206225&paxAges=18&searchProductCategoryCodes=TwoWayFlightOnly&departureDate=2026-08-10&duration=7&searchType=NotSet&abTestVisualDestinations=true",
        2500,
    ),
    (
        "Ioannina",
        "2026-08-17",
        "https://www.apollorejser.dk/booking-guide/flight/list?departureAirportCode=CPH&travelAreaUri=der%3Aairport%3Adtno%3A1206225&paxAges=18&searchProductCategoryCodes=TwoWayFlightOnly&departureDate=2026-08-17&duration=7&searchType=NotSet&abTestVisualDestinations=true",
        2500,
    ),
]


def trip_key(destination: str, date: str) -> str:
    return f"{destination} {date}"


# Per-trip thresholds. THRESHOLD_DKK env var, if set, overrides all of these
# (useful for testing the email path — set it to something huge to force alerts).
THRESHOLDS = {trip_key(dest, date): th for dest, date, _url, th in DATES}
_OVERRIDE = os.environ.get("THRESHOLD_DKK", "").strip()
GLOBAL_OVERRIDE: int | None = int(_OVERRIDE) if _OVERRIDE else None


def threshold_for(key: str) -> int:
    return GLOBAL_OVERRIDE if GLOBAL_OVERRIDE is not None else THRESHOLDS[key]


STATE_FILE = Path(__file__).parent / "state.json"

# Matches Danish-formatted prices. Apollorejser uses "2.398,-" (no kr/DKK
# suffix); we also accept "kr"/"DKK" defensively in case the markup varies.
PRICE_RE = re.compile(r"(\d{1,2}[.\s]?\d{3}|\d{3,5})\s*(?:,-|kr\.?|DKK)", re.IGNORECASE)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def fetch_lowest_price(page, url: str) -> int | None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    # Cookie banner — Apollorejser uses Cookiebot. Try a few variants.
    for sel in [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Tillad alle')",
        "button:has-text('Acceptér alle')",
        "button:has-text('Accepter alle')",
        "button:has-text('OK')",
    ]:
        try:
            await page.locator(sel).first.click(timeout=2_000)
            break
        except Exception:
            continue

    # Let the SPA render results.
    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    await page.wait_for_timeout(4_000)

    # Scroll once in case results lazy-load on intersection.
    try:
        await page.mouse.wheel(0, 2_000)
        await page.wait_for_timeout(2_000)
    except Exception:
        pass

    text = await page.evaluate("() => document.body.innerText")
    prices: list[int] = []
    for m in PRICE_RE.findall(text):
        n = int(m.replace(".", "").replace(" ", ""))
        # Filter to plausible round-trip flight prices.
        if 500 <= n <= 30_000:
            prices.append(n)
    return min(prices) if prices else None


async def collect_prices() -> dict[str, int | None]:
    results: dict[str, int | None] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(locale="da-DK", user_agent=USER_AGENT)
        page = await ctx.new_page()
        for destination, date, url, _threshold in DATES:
            key = trip_key(destination, date)
            try:
                price = await fetch_lowest_price(page, url)
            except Exception as exc:
                print(f"ERROR scraping {key}: {exc}", file=sys.stderr)
                price = None
            results[key] = price
            print(f"{key}: {price}")
            # Jitter between requests so we don't hammer the site predictably.
            await page.wait_for_timeout(random.randint(2_000, 6_000))
        await browser.close()
    return results


def load_state() -> dict[str, int | None]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict[str, int | None]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def decide_alerts(
    results: dict[str, int | None],
    state: dict[str, int | None],
) -> tuple[list[tuple[str, int]], dict[str, int | None]]:
    """Return (alerts_to_send, new_state).

    Alert when price < THRESHOLD AND (no prior alert OR new price is lower
    than the last alerted price). Reset the per-date watermark when price
    recovers above the threshold so the next dip re-alerts.
    """
    alerts: list[tuple[str, int]] = []
    # Drop any state keys not in this run's results so old trips don't
    # leave orphaned watermarks in state.json forever.
    new_state = {k: v for k, v in state.items() if k in results}
    for key, price in results.items():
        if price is None:
            continue
        last_alerted = state.get(key)
        threshold = threshold_for(key)
        if price < threshold:
            if last_alerted is None or price < last_alerted:
                alerts.append((key, price))
                new_state[key] = price
        else:
            if last_alerted is not None:
                new_state[key] = None
    return alerts, new_state


def send_email(alerts: list[tuple[str, int]], all_results: dict[str, int | None]) -> None:
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("EMAIL_TO", user)

    lowest = min(p for _, p in alerts)
    override_note = f" (overridden to {GLOBAL_OVERRIDE} DKK)" if GLOBAL_OVERRIDE else ""
    lines = [
        "Flight price alert — apollorejser.dk, CPH round-trip, 7 days.",
        "",
        f"Per-trip thresholds active{override_note}.",
        "",
        "Triggered:",
    ]
    url_by_key = {trip_key(dest, date): url for dest, date, url, _ in DATES}
    for key, price in alerts:
        lines.append(f"  {key}: {price} DKK (threshold {threshold_for(key)})  ->  {url_by_key[key]}")
    lines.append("")
    lines.append("All prices this check:")
    for key, price in all_results.items():
        shown = f"{price} DKK" if price is not None else "unknown"
        lines.append(f"  {key}: {shown} (threshold {threshold_for(key)})")

    msg = MIMEText("\n".join(lines))
    msg["Subject"] = f"Flight price drop: {lowest} DKK"
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, pw)
        smtp.send_message(msg)
    print(f"Email sent to {to}")


async def main() -> int:
    results = await collect_prices()
    state = load_state()
    alerts, new_state = decide_alerts(results, state)
    save_state(new_state)
    if alerts:
        send_email(alerts, results)
    else:
        print("No alerts to send.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
