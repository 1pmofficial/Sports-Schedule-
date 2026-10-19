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
import os
import re
import sys
from urllib.parse import urljoin
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

# How many individual match pages to open per run (each one lists every broadcaster).
# Override in the workflow with an env var, e.g. MAX_MATCH_PAGES=100.
MAX_MATCH_PAGES = int(os.environ.get("MAX_MATCH_PAGES", "60"))

MATCH_LINK_RE = re.compile(
    r'href=["\']((?:https?://(?:www\.)?livesportsontv\.com)?/match/[^"\'#?\s]+)(?:[?#][^"\']*)?["\']',
    re.I,
)
LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)(?:</script>|\Z)', re.S | re.I
)

# The site calls American football just "Football" (breadcrumb: Football > NFL).
SITE_SPORT_ALIASES = {"football": "American Football"}

# Channel logos are usually <img> tags with no visible text. fetch_page() writes each
# logo's alt text on its own line, prefixed with this marker, so the parser can use it.
IMG_MARK = "[[img]] "

# Lines that describe the event rather than where to watch it.
NOTE_RE = re.compile(
    r"\b(preseason|regular season|postseason|playoffs?|quarterfinals?|semifinals?|finals?|"
    r"round \d+|game \d+|race|laps?|qualifying|practice|sprint|match ?day|group [a-z]\b)",
    re.I,
)

# Runs in the page: grabs sample row HTML for debugging, then exposes logo alt text.
PAGE_JS = """
() => {
  const samples = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode()) && samples.length < 3) {
    if (/^\\s*\\d{1,2}:\\d{2}\\s*[AP]M\\s*$/i.test(n.nodeValue)) {
      let el = n.parentElement;
      for (let i = 0; i < 3 && el && el.parentElement; i++) el = el.parentElement;
      if (el) samples.push(el.outerHTML.slice(0, 6000));
    }
  }
  let added = 0;
  document.querySelectorAll('img[alt], [role="img"][aria-label], svg title').forEach(el => {
    const label = (el.getAttribute('alt') || el.getAttribute('aria-label') || el.textContent || '').trim();
    if (!label || label.length > 60 || /^(logo|image|icon|channel)$/i.test(label)) return;
    const host = el.tagName.toLowerCase() === 'title' ? el.closest('svg') : el;
    if (!host || !host.parentNode) return;
    const d = document.createElement('div');
    d.style.display = 'block';
    d.textContent = '[[img]] ' + label;
    host.parentNode.insertBefore(d, host.nextSibling);
    added++;
  });
  return {added: added, samples: samples};
}
"""


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_page():
    """Render the page. Returns (html, visible_text, captured_json_responses, match_pages)."""
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

        try:
            info = page.evaluate(PAGE_JS)
            print(f"[info] logo labels exposed: {info['added']}, row samples: {len(info['samples'])}")
            (DEBUG_DIR / "event_samples.html").write_text(
                "\n\n<!-- ===== next sample ===== -->\n\n".join(info["samples"]), encoding="utf-8"
            )
        except Exception as e:
            print(f"[warn] page script failed: {e}")

        html = page.content()
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        try:
            page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=False)
        except Exception:
            pass
        # Each match page carries schema.org JSON-LD with the full broadcaster list.
        match_pages = {}
        urls = extract_match_urls(html)
        print(f"[info] match links on page: {len(urls)} (opening up to {MAX_MATCH_PAGES})")
        for u in urls[:MAX_MATCH_PAGES]:
            try:
                r = context.request.get(u, timeout=30000, headers={"Referer": URL})
                if r.status == 200:
                    match_pages[u] = r.text()
                else:
                    print(f"[warn] HTTP {r.status} for {u}")
            except Exception as e:
                print(f"[warn] could not fetch {u}: {e}")
            page.wait_for_timeout(700)   # be gentle with the site
        print(f"[info] match pages fetched: {len(match_pages)}")

        browser.close()

    print(f"[info] html={len(html):,} bytes, text={len(text):,} chars, "
          f"json responses captured={len(captured)}")
    return html, text, captured, match_pages


# --------------------------------------------------------------------------
# Source 0: schema.org JSON-LD on match pages (best: has every channel)
# --------------------------------------------------------------------------

def extract_match_urls(html):
    seen, out = set(), []
    for m in MATCH_LINK_RE.finditer(html):
        u = urljoin(URL, m.group(1))
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _walk_ld(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_ld(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk_ld(x)


def _has_type(node, name):
    t = node.get("@type")
    return name in (t if isinstance(t, list) else [t])


def _ld_name(v):
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, list):
        return _ld_name(v[0]) if v else None
    return v if isinstance(v, str) else None


def events_from_ldjson(html, url=None):
    """
    Parse <script type="application/ld+json"> blocks. A match page contains:
      SportsEvent   -> name, startDate (ISO with UTC offset), homeTeam, awayTeam, location
        subEvent[]  -> BroadcastEvent.publishedOn.name  = one channel / streaming service
      BreadcrumbList -> Home > <Sport> > <League> > <match>
    """
    out = []
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        nodes = list(_walk_ld(data))

        crumbs = []
        for n in nodes:
            if _has_type(n, "BreadcrumbList"):
                items = [i for i in n.get("itemListElement", []) if isinstance(i, dict)]
                items.sort(key=lambda i: i.get("position", 0))
                crumbs = [i.get("name") for i in items]
                break

        for n in nodes:
            if not _has_type(n, "SportsEvent"):
                continue
            home, away = _ld_name(n.get("homeTeam")), _ld_name(n.get("awayTeam"))
            title = f"{away} @ {home}" if home and away else n.get("name")

            subs = n.get("subEvent") or []
            if isinstance(subs, dict):
                subs = [subs]
            channels = []
            for sub in subs:
                name = _ld_name(sub.get("publishedOn")) if isinstance(sub, dict) else None
                if name and name not in channels:
                    channels.append(name)

            sport = crumbs[1] if len(crumbs) >= 3 else n.get("sport")
            if isinstance(sport, str):
                sport = SITE_SPORT_ALIASES.get(sport.strip().lower(), sport)
            league = (crumbs[2] if len(crumbs) >= 4 else None) or _ld_name(n.get("organizer"))

            out.append({
                "time": n.get("startDate"),
                "title": title,
                "away": away,
                "home": home,
                "channels": channels,
                "notes": [],
                "venue": _ld_name(n.get("location")),
                "sport": sport,
                "league": league,
                "section": None,
                "raw": url or n.get("url"),
            })
    return out


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
        "channels": _flatten_names(
            _first(d, "channel", "network", "broadcast", "stream", "watch", "provider", "tv")
        ),
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
        all_lines = e["lines"]
        logo_labels = [l[len(IMG_MARK):].strip() for l in all_lines if l.startswith(IMG_MARK)]
        body = [l for l in all_lines if not l.startswith(IMG_MARK)]
        title_idx = next((i for i, l in enumerate(body) if VS_RE.search(l)), 0 if body else None)
        if title_idx is None:
            continue
        title = body[title_idx]
        details = body[:title_idx] + body[title_idx + 1:]
        parts = VS_RE.split(title, maxsplit=1)
        away, home = (parts + [None])[:2] if len(parts) == 2 else (None, None)

        notes = [l for l in details if NOTE_RE.search(l)]
        channels = []
        for name in [l for l in details if l not in notes] + logo_labels:
            for piece in re.split(r"\s*[,|]\s*", name):
                piece = piece.strip()
                # skip blanks, repeats, and team-logo alt text (a team already in the title)
                if piece and piece not in channels and piece.lower() not in title.lower():
                    channels.append(piece)

        out.append({
            "time": e["time"],
            "title": title,
            "away": away,
            "home": home,
            "channels": channels,
            "notes": notes,
            "sport": None,
            "league": None,
            "section": e.get("section"),
            "raw": " | ".join(all_lines),
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

def save_debug(html, text, captured, match_pages=None):
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
    if match_pages:
        first_url, first_html = next(iter(match_pages.items()))
        blocks = [m.group(1) for m in LD_RE.finditer(first_html)]
        (DEBUG_DIR / "match_sample_jsonld.json").write_text(
            f"// {first_url}\n" + "\n\n".join(blocks), encoding="utf-8"
        )


def main():
    rules = load_league_map()
    html, text, captured, match_pages = fetch_page()
    save_debug(html, text, captured, match_pages)

    events, source = [], None

    # 0. JSON-LD on the individual match pages (full channel lists), then on the homepage.
    for url, body in match_pages.items():
        events.extend(events_from_ldjson(body, url))
    if events:
        source = f"jsonld:{len(match_pages)} match pages"
    else:
        events = events_from_ldjson(html)
        source = "jsonld:homepage" if events else None

    if not events:
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
    unknown = sorted({
        (s["sport"], l["league"])
        for s in result["sports"] for l in s["leagues"] if "country" not in l
    })
    if unknown:
        print(f"[info] leagues not in leagues.json (add them to categorize properly): {unknown}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[info] wrote {OUTPUT} ({len(events)} events)")


if __name__ == "__main__":
    main()
