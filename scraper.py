"""
Live Sports TV scraper — v5 (league-driven categorization).

Approach:
  1. Fetch the rendered homepage.
  2. Walk the DOM once, collecting every event along with its league header.
  3. Categorize each event using the official league→sport mapping
     (leagues.json) rather than guessing from the sport section header.

This makes categorization authoritative and independent of page layout.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

URL = "https://www.livesportsontv.com"
OUTPUT = Path("output/events.json")
DEBUG_HTML = Path("output/debug.html")
LEAGUES_FILE = Path("leagues.json")


# ---------------------------------------------------------------- league map

def load_league_map() -> dict:
    """Load the official league → {sport, country, gender} mapping."""
    if not LEAGUES_FILE.exists():
        print(f"[warn] {LEAGUES_FILE} not found — categorization will be empty")
        return {}
    with LEAGUES_FILE.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- fetch

def fetch_html() -> str:
    """Render the page in a headless browser and return the HTML."""
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

        # Navigate — don't wait for networkidle (never fires on ad-heavy sites)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[warn] goto: {e}")

        # Wait for the first event row
        try:
            page.wait_for_selector(
                "div[class*='FixtureItem_container']", timeout=45000
            )
            print("[info] event rows detected")
        except Exception:
            print("[warn] no FixtureItem_container appeared; dumping anyway")

        # Let React render all leagues
        page.wait_for_timeout(5000)

        # Scroll to trigger lazy loads
        for _ in range(12):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(600)
        page.mouse.wheel(0, -50000)
        page.wait_for_timeout(2000)

        html = page.content()
        browser.close()

    print(f"[info] fetched {len(html):,} bytes")
    return html


# ---------------------------------------------------------------- helpers

def _text(el) -> str:
    if el is None:
        return ""
    return el.get_text(strip=True)


# ---------------------------------------------------------------- extract

def extract_event(row) -> dict | None:
    """Extract a single event from a FixtureItem_container div."""
    # ---- time ----
    time_el = row.select_one(
        "span[class*='FixtureItem_time__'][data-fixture-time]"
    )
    time_str = _text(time_el) or None

    # ---- teams / title ----
    title_el = row.select_one("span[class*='FixtureItem_titleOnly']")
    if title_el:
        title = _text(title_el)
        away, home = None, None
    else:
        team_els = row.select("span[class*='FixtureItem_teamName']")
        if len(team_els) >= 2:
            away = _text(team_els[0])
            home = _text(team_els[1])
            title = f"{away} vs {home}"
        elif len(team_els) == 1:
            away = _text(team_els[0])
            home = None
            title = away
        else:
            link = row.select_one("a[class*='FixtureItem_rowLink']")
            if link and link.get("aria-label"):
                title = link["aria-label"]
                parts = [p.strip() for p in title.split(" - ", 1)]
                away = parts[0] if parts else None
                home = parts[1] if len(parts) > 1 else None
            else:
                return None

    # ---- notes ----
    notes_el = row.select_one("p[class*='FixtureItem_restrictions']")
    notes = _text(notes_el) or None

    # ---- channels ----
    channels: list[dict] = []
    for chip in row.select("[class*='FixtureItem_channelChip']"):
        chip_classes = " ".join(chip.get("class", []))

        # Skip "Show more"
        if "FixtureItem_showMore" in chip_classes:
            continue

        name_el = chip.select_one(
            "span[class*='FixtureItem_channelChipText']"
        )
        name = _text(name_el)
        if not name:
            continue

        img = chip.select_one("img[class*='ChannelIcon_image']")
        logo = img["src"] if img and img.get("src") else None

        channels.append(
            {
                "name": name,
                "streaming": "FixtureItem_streaming" in chip_classes,
                "logo": logo,
            }
        )

    return {
        "time": time_str,
        "title": title,
        "away": away,
        "home": home,
        "notes": notes,
        "channels": channels,
    }


# ---------------------------------------------------------------- parse

def parse_events(html: str, league_map: dict) -> list[dict]:
    """
    Walk the DOM. For each event, look up the closest enclosing league
    header and use league_map to assign sport / country / gender.
    """
    soup = BeautifulSoup(html, "lxml")
    events: list[dict] = []

    # Every event row
    rows = soup.select("div[class*='FixtureItem_container']")
    print(f"[info] found {len(rows)} raw event rows")

    for row in rows:
        # Find the closest LeagueCard ancestor (if any)
        league_card = row.find_parent(
            lambda tag: tag.name == "div"
            and any("Card_card" in c for c in tag.get("class", []))
        )

        league_name = None
        if league_card:
            link = league_card.select_one(
                "a[class*='LeagueCard_cardTitleLink']"
            )
            league_name = _text(link) if link else None

        event = extract_event(row)
        if not event:
            continue

        # Look up official metadata
        meta = league_map.get(league_name or "", {})
        event["league"] = league_name or "Unknown"
        event["sport"] = meta.get("sport", "Unknown")
        event["country"] = meta.get("country", "Unknown")
        event["gender"] = meta.get("gender", "mixed")

        events.append(event)

    print(f"[info] parsed {len(events)} events")
    return events


# ---------------------------------------------------------------- group

def group(events: list[dict]) -> dict:
    """Group events by sport → league."""
    nested: dict[str, dict[str, list]] = {}
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
                "total_events": sum(len(v) for v in leagues.values()),
                "leagues": [
                    {
                        "league": league,
                        "country": (
                            events[0].get("country")
                            if (events := next(
                                (
                                    evs
                                    for evs in leagues.values()
                                    if evs
                                ),
                                [],
                            ))
                            else None
                        ),
                        "gender": (
                            next(
                                (ev.get("gender") for ev in evs if ev),
                                "mixed",
                            )
                        ),
                        "total_events": len(evs),
                        "events": evs,
                    }
                    for league, evs in sorted(leagues.items())
                ],
            }
            for sport, leagues in sorted(nested.items())
        ],
    }


# ---------------------------------------------------------------- main

def main():
    league_map = load_league_map()
    print(f"[info] loaded {len(league_map)} leagues from {LEAGUES_FILE}")

    try:
        html = fetch_html()
    except Exception as e:
        print(f"[error] fetch_html failed: {e}")
        sys.exit(1)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_HTML.write_text(html, encoding="utf-8")
    print(f"[info] saved raw HTML to {DEBUG_HTML}")

    events = parse_events(html, league_map)

    if not events:
        print("[error] parsed 0 events — check output/debug.html")
        grouped = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": URL,
            "total_events": 0,
            "sports": [],
        }
    else:
        grouped = group(events)

    OUTPUT.write_text(
        json.dumps(grouped, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[info] wrote {OUTPUT}")
    print(f"[info] total events: {grouped['total_events']}")


if __name__ == "__main__":
    main()
