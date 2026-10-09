import json
import re
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

URL = "https://www.livesportsontv.com"
OUTPUT = Path("output/events.json")

# Load league mapping (see leagues.json)
with open("leagues.json") as f:
    LEAGUE_MAP = json.load(f)


def fetch_html():
    """Render the page with a headless browser to get the full schedule."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        )
        page.goto(URL, wait_until="networkidle", timeout=60000)
        # Give dynamic event lists time to render
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
        return html


def categorize(event_text):
    """Return (sport, league) based on keywords. Extend this mapping as needed."""
    text = event_text.lower()
    for league, meta in LEAGUE_MAP.items():
        for keyword in meta["keywords"]:
            if keyword.lower() in text:
                return meta["sport"], league
    return "Other", "Other"


def parse_events(html):
    """Very rough example parser – you WILL need to inspect the DOM to tune selectors."""
    soup = BeautifulSoup(html, "lxml")
    events = []

    # The site groups events inside headings like "Show more from College Soccer"
    # Adapt these selectors after inspecting the live DOM in DevTools.
    for block in soup.select("section, div.event-row, li.event"):
        text = block.get_text(" ", strip=True)
        if "@" not in text and "vs." not in text:
            continue

        time_match = re.match(r"(\d{1,2}:\d{2}\s*(?:AM|PM))", text)
        time_str = time_match.group(1) if time_match else None

        # Teams are usually the part containing "@" or "vs."
        teams_match = re.search(
            r"([A-Za-z0-9 .\-']+)\s*(?:@|vs\.)\s*([A-Za-z0-9 .\-']+)", text
        )
        if not teams_match:
            continue

        away, home = teams_match.group(1).strip(), teams_match.group(2).strip()

        # Anything after the teams is usually channels / notes
        rest = text[teams_match.end():].strip()
        channels = [c.strip() for c in re.split(r"\s{2,}", rest) if c.strip()]

        sport, league = categorize(f"{away} {home} {rest}")

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
    return events


def group_by_sport_and_league(events):
    result = {}
    for e in events:
        result.setdefault(e["sport"], {}).setdefault(e["league"], []).append(
            {k: v for k, v in e.items() if k not in ("sport", "league")}
        )
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": URL,
        "sports": [
            {
                "sport": sport,
                "leagues": [
                    {"league": league, "events": evs}
                    for league, evs in leagues.items()
                ],
            }
            for sport, leagues in result.items()
        ],
    }


def main():
    html = fetch_html()
    events = parse_events(html)
    grouped = group_by_sport_and_league(events)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(grouped, indent=2))
    print(f"Wrote {len(events)} events to {OUTPUT}")


if __name__ == "__main__":
    main()
