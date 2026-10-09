"""
Live Sports TV scraper.
Fetches https://www.livesportsontv.com and writes a categorized JSON file.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

URL = "https://www.livesportsontv.com"
OUTPUT = Path("output/events.json")
DEBUG_HTML = Path("output/debug.html")
LEAGUE_MAP_FILE = Path("leagues.json")

# ---------- helpers ----------

def load_league_map():
    """Load the league/sport categorization map. Return {} if missing."""
    if not LEAGUE_MAP_FILE.exists():
        print(f"[warn] {LEAGUE_MAP_FILE} not found — categorization will be 'Other'")
        return {}
    with LEAGUE_MAP_FILE.open() as f:
        return json.load(f)


def fetch_html():
    """
    Render the page with a headless browser.
    Never raises on navigation timeout — returns whatever HTML we got.
    """
    print(f"[info] fetching {URL}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        # ---- 1. Navigate. Don't wait for networkidle — it never fires on this site.
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[warn] goto timed out or failed: {e} (continuing anyway)")

        # ---- 2. Give JS time to render the schedule.
        page.wait_for_timeout(10000)

        # ---- 3. Dismiss the ad-blocker modal if present.
        for sel in [
            "text=Continue without supporting us",
            "button:has-text('Continue')",
            "text=×",
        ]:
            try:
                page.click(sel, timeout=1500)
                print(f"[info] dismissed modal via selector: {sel}")
                break
            except Exception:
                pass

        # ---- 4. Wait a bit more for lazy-loaded rows.
        page.wait_for_timeout(5000)

        html = page.content()
        browser.close()

    print(f"[info] fetched {len(html):,} bytes of HTML")
    return html


def categorize(event_text, league_map):
    """Return (sport, league) based on keyword matching."""
    text = event_text.lower()
    for league, meta in league_map.items():
        for keyword in meta.get("keywords", []):
            if keyword.lower() in text:
                return meta.get("sport", "Other"), league
    return "Other", "Other"


def parse_events(html, league_map):
    """
    Parse event rows out of the rendered HTML.
    Uses a broad text-pattern approach so it survives minor DOM changes.
    """
    soup = BeautifulSoup(html, "lxml")
    events = []

    # Strategy: find every element whose text looks like
    #   "TIME  TEAM_A @ TEAM_B  CHANNEL1 CHANNEL2 ..."
    #   or
    #   "TIME  TEAM_A vs. TEAM_B  CHANNEL1 ..."
    time_re = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b")
    matchup_re = re.compile(
        r"([A-Za-z0-9 .\-'&()]+?)\s*(?:@|vs\.?)\s*([A-Za-z0-9 .\-'&()]+)"
    )

    seen = set()
    for el in soup.find_all(string=time_re):
        parent = el.parent
        if parent is None:
            continue
        text = parent.get_text(" ", strip=True)

        time_match = time_re.search(text)
        if not time_match:
            continue
        time_str = time_match.group(1)

        matchup = matchup_re.search(text)
        if not matchup:
            continue

        away = matchup.group(1).strip(" -–—")
        home = matchup.group(2).strip(" -–—")
        if not away or not home or len(away) > 60 or len(home) > 60:
            continue

        dedupe_key = (time_str, away.lower(), home.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        # Channels = the rest of the text after the matchup
        rest = text[matchup.end():].strip()
        channels = [c.strip() for c in re.split(r"\s{2,}|,", rest) if c.strip()]

        sport, league = categorize(f"{away} {home} {rest}", league_map)

        events.append(
            {
                "time": time_str,
                "away": away,
                "home": home,
                "channels": channels,
                "sport": sport,
                "league": league,
                "raw": text,
            }
        )

    print(f"[info] parsed {len(events)} events")
    return events


def group_by_sport_and_league(events):
    """Restructure the flat event list into sport → league → events."""
    nested = {}
    for e in events:
        nested.setdefault(e["sport"], {}).setdefault(e["league"], []).append(
            {k: v for k, v in e.items() if k not in ("sport", "league")}
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "total_events": len(events),
        "sports": [
            {
                "sport": sport,
                "leagues": [
                    {"league": league, "events": evs}
                    for league, evs in sorted(leagues.items())
                ],
            }
            for sport, leagues in sorted(nested.items())
        ],
    }


def main():
    league_map = load_league_map()

    try:
        html = fetch_html()
    except Exception as e:
        print(f"[error] fetch_html failed: {e}")
        sys.exit(1)

    # Always save raw HTML so you can inspect it from the Actions artifacts
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_HTML.write_text(html, encoding="utf-8")
    print(f"[info] saved raw HTML to {DEBUG_HTML}")

    events = parse_events(html, league_map)
    if not events:
        print("[error] 0 events parsed — selectors likely wrong. See output/debug.html")
        # Still write an empty structure so the workflow doesn't fail hard
        grouped = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": URL,
            "total_events": 0,
            "sports": [],
        }
    else:
        grouped = group_by_sport_and_league(events)

    OUTPUT.write_text(json.dumps(grouped, indent=2, ensure_ascii=False))
    print(f"[info] wrote {OUTPUT}")


if __name__ == "__main__":
    main()
