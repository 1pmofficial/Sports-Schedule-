"""
Live sports TV schedule scraper (v2).

Why v1 returned 0 events:
  * parse_events() looked at the text of the *time element only*, so the
    teams/channels (siblings) were never in the same string -> no match.
  * The site is a Next.js app, so the data usually lives in a JSON blob
    (__NEXT_DATA__) or an XHR response, not in neat HTML rows.

v2 tries four sources in order and uses the first that yields events:
  1. __NEXT_DATA__ JSON embedded in the page
  2. JSON responses captured from the network while the page loads
  3. The rendered page text (line-based parser, no CSS selectors needed)
  4. Nothing found -> exit non-zero, keep the old events.json, save diagnostics

Diagnostics are written to output/debug/ and uploaded by the workflow.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

URL = "https://www.livesportsontv.com"
OUTPUT = Path("output/events.json")
DEBUG_DIR = Path("output/debug")
LEAGUE_MAP_FILE = Path("leagues.json")
TIMEZONE = "America/New_York"  # times on the page are shown in this zone

TIME_RE = re.compile(r"^(\d{1,2}:\d{2}\s*[AP]M)\b\s*(.*)$", re.I)
SHOW_MORE_RE = re.compile(r"^Show more from (.+)$", re.I)
VS_RE = re.compile(r"\s+(?:@|vs\.?|v)\s+", re.I)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_page():
    """Render the page. Returns (html, visible_text, captured_json_responses)."""
    from playwright.sync_api import sync_playwright  # imported here so parsers are testable

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id=TIMEZONE,
        )
        page = context.new_page()

        def on_response(resp):
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype:
                    return
                body = resp.text()
                if len(body) < 300:
                    return
                captured.append({"url": resp.url, "status": resp.status, "body": body})
            except Exception:
                pass  # some responses can't be read; ignore

        page.on("response", on_response)

        status = None
        try:
            resp = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp else None
        except Exception as e:
            print(f"[warn] goto problem (continuing with what loaded): {e}")
        print(f"[info] main document status: {status}")

        page.wait_for_timeout(6000)

        # Dismiss ad-block / consent overlays if present (best effort).
        for sel in [
            "text=Continue without supporting us",
            "button:has-text('Accept')",
            "button:has-text('Continue')",
        ]:
            try:
                page.click(sel, timeout=1500)
                print(f"[info] clicked overlay button: {sel}")
                break
            except Exception:
                pass

        # Scroll to trigger lazy-loaded sections.
        for _ in range(6):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(1200)

        html = page.content()
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        try:
            page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=False)
        except Exception:
            pass
        browser.close()

    print(f"[info] html={len(html):,} bytes, text={len(text):,} chars, "
          f"json responses captured={len(captured)}")
    return html, text, captured


# --------------------------------------------------------------------------
# Source 1 + 2: JSON (Next.js data / XHR)
# --------------------------------------------------------------------------

def extract_next_data(html):
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _keys(d):
    return {k.lower(): k for k in d.keys()}


def looks_like_event(d):
    if not isinstance(d, dict) or len(d) < 3:
        return False
    keys = _keys(d)
    has_time = any(any(t in k for t in ("time", "start", "date", "kickoff")) for k in keys)
    has_name = any(
        any(t in k for t in ("team", "home", "away", "title", "name", "event", "match"))
        for k in keys
    )
    return has_time and has_name


def find_event_lists(obj, depth=0):
    """Yield every list in the JSON that looks like a list of events."""
    if depth > 12:
        return
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if len(dicts) >= 3 and sum(looks_like_event(x) for x in dicts) / len(dicts) >= 0.6:
            yield obj
        for x in obj:
            yield from find_event_lists(x, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from find_event_lists(v, depth + 1)


def _first(d, *needles):
    """First non-empty value whose key contains any needle (case-insensitive)."""
    for k, v in d.items():
        kl = k.lower()
        if any(n in kl for n in needles) and v not in (None, "", [], {}):
            return v
    return None


def _flatten_names(v):
    out = []
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        name = _first(v, "name", "title", "label")
        if isinstance(name, str):
            out.append(name)
    elif isinstance(v, list):
        for x in v:
            out.extend(_flatten_names(x))
    return out


def normalize_json_event(d):
    title = _first(d, "title", "event", "name", "match")
    home = _first(d, "home")
    away = _first(d, "away", "visitor")
    home = _flatten_names(home)[0] if _flatten_names(home) else None
    away = _flatten_names(away)[0] if _flatten_names(away) else None
    if not title and home and away:
        title = f"{away} @ {home}"
    return {
        "time": _first(d, "start", "time", "kickoff", "date"),
        "title": title if isinstance(title, str) else None,
        "away": away,
        "home": home,
        "channels": _flatten_names(_first(d, "channel", "network", "broadcast", "tv")),
        "sport": _flatten_names(_first(d, "sport"))[:1] or None,
        "league": _flatten_names(_first(d, "league", "competition", "tournament"))[:1] or None,
        "raw": d,
    }


def events_from_json(obj):
    best = []
    for lst in find_event_lists(obj):
        evs = [normalize_json_event(x) for x in lst if isinstance(x, dict) and looks_like_event(x)]
        if len(evs) > len(best):
            best = evs
    for e in best:
        e["sport"] = e["sport"][0] if e["sport"] else None
        e["league"] = e["league"][0] if e["league"] else None
    return best


# --------------------------------------------------------------------------
# Source 3: visible page text
# --------------------------------------------------------------------------

def events_from_text(text):
    """
    The page reads like:
        6:30 AM
        Centre College vs Millsaps
        FloSports
        ...
        Show more from College Soccer
    A time line starts an event; "Show more from X" closes a group named X.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    events, pending, current = [], [], None

    def close_current():
        nonlocal current
        if current:
            pending.append(current)
            current = None

    for line in lines:
        m = TIME_RE.match(line)
        if m:
            close_current()
            current = {"time": m.group(1).upper(), "lines": [m.group(2)] if m.group(2) else []}
            continue
        h = SHOW_MORE_RE.match(line)
        if h:
            close_current()
            for e in pending:
                e["section"] = h.group(1).strip()
            events.extend(pending)
            pending = []
            continue
        if current is not None and len(current["lines"]) < 14:
            current["lines"].append(line)

    close_current()
    events.extend(pending)

    out = []
    for e in events:
        body = e["lines"]
        title_idx = next((i for i, l in enumerate(body) if VS_RE.search(l)), 0 if body else None)
        if title_idx is None:
            continue
        title = body[title_idx]
        details = body[:title_idx] + body[title_idx + 1:]
        parts = VS_RE.split(title, maxsplit=1)
        away, home = (parts + [None])[:2] if len(parts) == 2 else (None, None)
        out.append({
            "time": e["time"],
            "title": title,
            "away": away,
            "home": home,
            "channels": details,   # channels + notes; split further once real DOM is known
            "sport": None,
            "league": None,
            "section": e.get("section"),
            "raw": " | ".join(body),
        })
    return out


# --------------------------------------------------------------------------
# Categorization
# --------------------------------------------------------------------------

def _norm(s):
    return re.sub(r"\s+", " ", str(s).replace("’", "'")).strip().lower()


# League names too generic to trust when they only appear inside an event title.
GENERIC_NAMES = {
    "championship", "wrestling", "marathon", "boxing", "skeleton",
    "speedway", "curling", "judo", "luge",
}


def load_league_map():
    """
    leagues.json is grouped by sport:
        {"Soccer": {"Premier League": {"country": "England", "gender": "male"}, ...}, ...}
    Flattened here into lookups by league name and by sport name.
    """
    lm = {"by_name": {}, "ordered": [], "sports": {}}
    if not LEAGUE_MAP_FILE.exists():
        print(f"[warn] {LEAGUE_MAP_FILE} not found; everything will be 'Other'")
        return lm
    data = json.loads(LEAGUE_MAP_FILE.read_text(encoding="utf-8"))
    for sport, leagues in data.items():
        lm["sports"][_norm(sport)] = sport
        for league, meta in leagues.items():
            key = _norm(league)
            lm["by_name"][key] = (sport, league, meta)
            lm["ordered"].append((key, league, sport, meta))
    # Longest name first so "Women's College Soccer" beats "College Soccer".
    lm["ordered"].sort(key=lambda r: len(r[0]), reverse=True)
    return lm


def categorize(event, lm):
    """Return (sport, league, meta). meta is {"country", "gender"} or None."""
    by_name = lm["by_name"]
    league_field = event.get("league")
    section = event.get("section")

    # 1. Exact league name from the data (JSON field or page heading).
    for value in (league_field, section):
        if value and _norm(value) in by_name:
            return by_name[_norm(value)]

    # 2. The source itself named a sport and league we don't know: keep them.
    if event.get("sport") and league_field:
        return event["sport"], league_field, None

    # 3. League name appears inside the heading ("Show more from NCAA College Soccer").
    if section:
        s = _norm(section)
        for key, league, sport, meta in lm["ordered"]:
            if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", s):
                return sport, league, meta

    # 4. League name appears in the matchup title (acronyms like NFL, or 5+ characters).
    title = event.get("title")
    if title:
        t = _norm(title)
        for key, league, sport, meta in lm["ordered"]:
            if key in GENERIC_NAMES:
                continue
            if not (len(key) >= 5 or (league.isupper() and len(key) >= 3)):
                continue
            if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", t):
                return sport, league, meta

    # 5. The heading is a sport, not a league ("Show more from Basketball").
    if section and _norm(section) in lm["sports"]:
        sport = lm["sports"][_norm(section)]
        other = f"Other {sport}"
        if _norm(other) in by_name:
            return by_name[_norm(other)]
        return sport, other, None

    # 6. Unknown: keep the heading as the league so nothing is silently lost.
    return event.get("sport") or "Other", league_field or section or "Other", None


def group(events, lm):
    nested = {}   # sport -> league -> {"meta": ..., "events": [...]}
    for e in events:
        sport, league, meta = categorize(e, lm)
        slim = {k: v for k, v in e.items() if k not in ("sport", "league", "section")}
        slim["raw"] = slim["raw"] if isinstance(slim["raw"], str) else None  # keep file small
        entry = nested.setdefault(sport, {}).setdefault(league, {"meta": meta, "events": []})
        entry["events"].append(slim)

    def league_obj(name, entry):
        obj = {"league": name}
        if entry["meta"]:
            obj.update(entry["meta"])   # country, gender
        obj["events"] = entry["events"]
        return obj

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "timezone": TIMEZONE,
        "total_events": len(events),
        "sports": [
            {
                "sport": s,
                "leagues": [league_obj(l, entry) for l, entry in sorted(ls.items())],
            }
            for s, ls in sorted(nested.items())
        ],
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def save_debug(html, text, captured):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / "page.html").write_text(html, encoding="utf-8")
    (DEBUG_DIR / "page.txt").write_text(text, encoding="utf-8")
    (DEBUG_DIR / "network.json").write_text(
        json.dumps(
            [{"url": c["url"], "status": c["status"], "bytes": len(c["body"])} for c in captured],
            indent=2,
        )
    )
    for i, c in enumerate(captured[:10]):
        (DEBUG_DIR / f"response_{i}.json").write_text(c["body"][:500_000], encoding="utf-8")


def main():
    rules = load_league_map()
    html, text, captured = fetch_page()
    save_debug(html, text, captured)

    events, source = [], None

    nd = extract_next_data(html)
    if nd:
        events = events_from_json(nd)
        source = "__NEXT_DATA__" if events else None

    if not events:
        for c in captured:
            try:
                evs = events_from_json(json.loads(c["body"]))
            except json.JSONDecodeError:
                continue
            if len(evs) > len(events):
                events, source = evs, f"xhr:{c['url']}"

    if not events:
        events = events_from_text(text)
        source = "page-text" if events else None

    print(f"[info] source={source} events={len(events)}")

    if not events:
        print("[error] 0 events found. Existing events.json left untouched.")
        print("[error] Check output/debug/ (uploaded as a workflow artifact): "
              "page.txt, page.png, network.json.")
        sys.exit(1)

    result = group(events, rules)
    result["parsed_from"] = source
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[info] wrote {OUTPUT} ({len(events)} events)")


if __name__ == "__main__":
    main()
