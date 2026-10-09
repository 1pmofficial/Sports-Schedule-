"""
Live Sports TV scraper — v6 (full pass, verified).

- Scrolls until the page stops growing (fixes "only 85 events" issue)
- Logs the live DOM count so we can verify against the site's own 307
- Robust league lookup with fallbacks for ambiguous names
- Skips the "Show more" pseudo-channel
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


# ---------------------------------------------------------------- config

def load_league_map() -> dict:
    if not LEAGUES_FILE.exists():
        print(f"[warn] {LEAGUES_FILE} not found")
        return {}
    with LEAGUES_FILE.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- fetch

def fetch_html() -> str:
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

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[warn] goto: {e}")

        try:
            page.wait_for_selector(
                "div[class*='FixtureItem_container']", timeout=45000
            )
            print("[info] first event rows detected")
        except Exception:
            print("[warn] no FixtureItem_container appeared")

        # ---- Scroll until scrollHeight stops growing ----
        last_height = page.evaluate("document.body.scrollHeight")
        print(f"[info] initial scrollHeight: {last_height}px")
        for i in range(60):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                print(f"[info] page stopped growing after {i+1} scrolls "
                      f"at {new_height}px")
                break
            last_height = new_height
        else:
            print(f"[warn] hit 60 scroll iterations; final {last_height}px")

        page.wait_for_timeout(4000)

        # ---- Verify we have all events in the live DOM ----
        live_count = page.locator(
            "div[class*='FixtureItem_container']"
        ).count()
        print(f"[info] live DOM has {live_count} FixtureItem rows")

        html = page.content()
        browser.close()

    print(f"[info] fetched {len(html):,} bytes")
    return html


# ---------------------------------------------------------------- helpers

def _text(el) -> str:
    return "" if el is None else el.get_text(strip=True)


def _lookup_league(name: str, league_map: dict) -> dict:
    """
    Look up a league's metadata, trying several disambiguation suffixes.
    Returns {} when nothing matches.
    """
    if not name:
        return {}

    # exact
    if name in league_map:
        return league_map[name]

    # <name> <Sport>
    for suffix in (
        "Football", "Basketball", "Ice Hockey", "American Football",
        "Rugby Union", "Rugby League", "Tennis", "Baseball", "Volleyball",
    ):
        key = f"{name} {suffix}"
        if key in league_map:
            return league_map[key]

    # <name> <Country>
    for suffix in (
        "Argentina", "Chile", "Guatemala", "Honduras", "Australia",
        "New Zealand", "Belgium", "Saudi Arabia", "Spain", "Italy",
        "Portugal", "USA", "Canada", "Mexico", "Japan", "Germany",
        "England", "France", "Netherlands", "Scotland", "Sweden",
        "Denmark", "Poland", "Greece", "Turkiye", "Brazil", "India",
        "Ireland", "Puerto Rico", "Venezuela", "Dominican Republic",
    ):
        key = f"{name} {suffix}"
        if key in league_map:
            return league_map[key]

    return {}


# ---------------------------------------------------------------- extract

def extract_event(row) -> dict | None:
    time_el = row.select_one(
        "span[class*='FixtureItem_time__'][data-fixture-time]"
    )
    time_str = _text(time_el) or None

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

    notes_el = row.select_one("p[class*='FixtureItem_restrictions']")
    notes = _text(notes_el) or None

    channels: list[dict] = []
    for chip in row.select("[class*='FixtureItem_channelChip']"):
        chip_classes = " ".join(chip.get("class", []))
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
        channels.append({
            "name": name,
            "streaming": "FixtureItem_streaming" in chip_classes,
            "logo": logo,
        })

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
    soup = BeautifulSoup(html, "lxml")
    events: list[dict] = []

    rows = soup.select("div[class*='FixtureItem_container']")
    print(f"[info] parsed HTML contains {len(rows)} FixtureItem rows")

    for row in rows:
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

        meta = _lookup_league(league_name or "", league_map)
        event["league"] = league_name or "Unknown"
        event["sport"] = meta.get("sport", "Unknown")
        event["country"] = meta.get("country", "Unknown")
        event["gender"] = meta.get("gender", "mixed")
        events.append(event)

    # ---- Diagnostics: count by league ----
    from collections import Counter
    counts = Counter(e["league"] for e in events)
    print("[info] events per league (top 30):")
    for league, n in counts.most_common(30):
        print(f"        {n:>4}  {league}")
    unknown = counts.get("Unknown", 0)
    if unknown:
        print(f"[warn] {unknown} events have league='Unknown' "
              f"(couldn't match a header)")

    print(f"[info] parsed {len(events)} events")
    return events


# ---------------------------------------------------------------- group

def group(events: list[dict]) -> dict:
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
