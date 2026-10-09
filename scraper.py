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

        # Navigate
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[warn] goto: {e}")

        # Wait for the first event row
        try:
            page.wait_for_selector(
                "div[class*='FixtureItem_container']", timeout=45000
            )
            print("[info] first event rows detected")
        except Exception:
            print("[warn] no FixtureItem_container appeared")

        # ---- KEY FIX: scroll until the page stops growing ----
        last_height = page.evaluate("document.body.scrollHeight")
        print(f"[info] initial scrollHeight: {last_height}px")
        for i in range(40):  # up to 40 scroll steps
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                print(f"[info] page stopped growing after {i+1} scrolls "
                      f"at {new_height}px")
                break
            last_height = new_height
        else:
            print(f"[warn] hit 40 scroll iterations; final height {last_height}px")

        # Give lazy content a final moment
        page.wait_for_timeout(4000)

        # Count event rows on the live DOM
        live_count = page.locator(
            "div[class*='FixtureItem_container']"
        ).count()
        print(f"[info] live DOM has {live_count} FixtureItem rows")

        html = page.content()
        browser.close()

    print(f"[info] fetched {len(html):,} bytes")
    return html
