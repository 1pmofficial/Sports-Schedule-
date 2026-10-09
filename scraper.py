"""
Live Sports TV scraper — v4 (final).

Fetches https://www.livesportsontv.com, extracts every event, and writes
a categorized JSON grouped by sport → league → events.

Verified selectors from the live site (CSS Modules class names):
  - Event row:        div.FixtureItem_container__kejZk
  - Event time:       span.FixtureItem_time__2roux[data-fixture-time]
  - Team names:       span.FixtureItem_teamName__AwoHS
  - "at" sign:        span.FixtureItem_atSign__jX5PJ  (@ or vs.)
  - Notes/round:      p.FixtureItem_restrictions__quJfN
  - Title-only events span.FixtureItem_titleOnly__j53oU  (races, F1, golf)
  - Channel chips:    [class*='FixtureItem_channelChip']
      * name:         span.FixtureItem_channelChipText__85Ksi
      * streaming:    class FixtureItem_streaming__AVFrm
      * TV:           class FixtureItem_nonStreaming__zrRAs
      * show-more:    class FixtureItem_showMore__i_kKo (skipped)
      * logo img:     img.ChannelIcon_image__TkRf3[src]
  - League header:    a.LeagueCard_cardTitleLink__S1OJM
  - Sport header:     a.SectionDivider_content__2mTk3[href^='/sport/']
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

        # Navigate — don't wait for networkidle, it never fires on ad-heavy sites
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[warn] goto: {e}")

        # Wait until at least one event row exists
        try:
            page.wait_for_selector(
                "div[class*='FixtureItem_container']", timeout=45000
            )
            print("[info] event rows detected")
        except Exception:
            print("[warn] no FixtureItem_container appeared; dumping anyway")

        # Let React render remaining leagues
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
    """Safely extract trimmed text from a BS4 element."""
    if el is None:
        return ""
    return el.get_text(strip=True)


# ---------------------------------------------------------------- parse

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
        # Title-only event (horse race, F1 practice, golf round, etc.)
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
            # Fallback: read aria-label on the row link (e.g. "A - B")
            link = row.select_one("a[class*='FixtureItem_rowLink']")
            if link and link.get("aria-label"):
                title = link["aria-label"]
                parts = [p.strip() for p in title.split(" - ", 1)]
                away = parts[0] if parts else None
                home = parts[1] if len(parts) > 1 else None
            else:
                return None

    # ---- notes / round info ----
    notes_el = row.select_one("p[class*='FixtureItem_restrictions']")
    notes = _text(notes_el) or None

    # ---- channels ----
    channels: list[dict] = []
    for chip in row.select("[class*='FixtureItem_channelChip']"):
        chip_classes = " ".join(chip.get("class", []))

        # Skip the "Show more" pseudo-channel
        if "FixtureItem_showMore" in chip_classes:
            continue

        name_el = chip.select_one(
            "span[class*='FixtureItem_channelChipText']"
        )
        name = _text(name_el)
        if not name:
            continue

        is_streaming = "FixtureItem_streaming" in chip_classes

        # Optional: channel logo URL
        img = chip.select_one("img[class*='ChannelIcon_image']")
        logo = img["src"] if img and img.get("src") else None

        channels.append(
            {
                "name": name,
                "streaming": is_streaming,
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


def parse_events(html: str) -> list[dict]:
    """Walk sport → league → event structure in the rendered HTML."""
    soup = BeautifulSoup(html, "lxml")
    events: list[dict] = []

    # 1. Find all sport section headers
    sport_headers = soup.select(
        "a[class*='SectionDivider_content'][href^='/sport/']"
    )
    print(f"[info] found {len(sport_headers)} sport sections")

    for sport_header in sport_headers:
        # Sport name comes from the <img alt="...">
        sport_img = sport_header.find("img")
        sport = (
            sport_img["alt"].strip()
            if sport_img and sport_img.get("alt")
            else _text(sport_header)
        )
        if not sport:
            sport = "Unknown"

        # Find the wrapper that contains this sport's leagues
        sport_block = sport_header.find_parent(
            lambda tag: tag.name == "div"
            and "FixtureListBySport_sport" in " ".join(tag.get("class", []))
        )
        if not sport_block:
            continue

        # 2. Iterate over league headers inside this sport block
        for league_header in sport_block.select(
            "a[class*='LeagueCard_cardTitleLink']"
        ):
            league = _text(league_header) or "Unknown"

            # The card that contains this league's events
            league_card = league_header.find_parent(
                lambda tag: tag.name == "div"
                and any("Card_card" in c for c in tag.get("class", []))
            )
            if not league_card:
                continue

            # 3. Extract each event row
            for row in league_card.select(
                "div[class*='FixtureItem_container']"
            ):
                event = extract_event(row)
                if not event:
                    continue
                event["sport"] = sport
                event["league"] = league
                events.append(event)

    print(f"[info] parsed {len(events)} events")
    return events


# ---------------------------------------------------------------- group

def group(events: list[dict]) -> dict:
    """Group events: sport → league → events."""
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
    # Fetch
    try:
        html = fetch_html()
    except Exception as e:
        print(f"[error] fetch_html failed: {e}")
        sys.exit(1)

    # Always save the raw HTML so we can debug from the Actions artifact
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_HTML.write_text(html, encoding="utf-8")
    print(f"[info] saved raw HTML to {DEBUG_HTML}")

    # Parse
    events = parse_events(html)

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

    # Write JSON
    OUTPUT.write_text(
        json.dumps(grouped, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[info] wrote {OUTPUT}")
    print(f"[info] total events: {grouped['total_events']}")


if __name__ == "__main__":
    main()
