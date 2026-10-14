"""
Amazon "Read Sample" scraper.

Usage:
    python scraper.py <amazon-book-url>

Example:
    python scraper.py "https://www.amazon.com/dp/B08N5WRWNW"
    python scraper.py "https://a.co/d/abc123"

Output:
    ./output/<ASIN>/page_001.jpg, page_002.jpg, ...
"""

import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


def extract_asin(url: str) -> str:
    for pattern in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})"]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


async def resolve_and_open(page, url: str):
    print(f"[+] Opening: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(3_000)

    final_url = page.url
    print(f"[+] Resolved URL: {final_url}")

    asin = extract_asin(final_url)
    if not asin:
        canonical = await page.evaluate(
            "() => document.querySelector('link[rel=canonical]')?.href || ''"
        )
        asin = extract_asin(canonical) or "unknown_asin"
    print(f"[+] ASIN: {asin}")

    await page.wait_for_timeout(2_000)

    sample_selectors = [
        '#sitbLogoImg', 'a[id*="sitbLogoImg"]', 'a[id*="sitbLogo"]',
        'a:has-text("Read sample")', 'a:has-text("Look inside")',
        '[data-action="a-sitb"]', 'a[href*="reader.amazon"]',
        'span:has-text("Read sample")',
    ]
    reader_iframe_sel = 'iframe[src*="read.amazon"], iframe[id*="kindle"], #KindleReaderIFrame'

    for sel in sample_selectors:
        try:
            btn = page.locator(sel).first
            if not await btn.is_visible(timeout=2_000):
                continue
            print(f"[+] Clicking sample button ({sel})")
            try:
                async with page.context.expect_page(timeout=5_000) as new_page_info:
                    await btn.click()
                reader_page = await new_page_info.value
                await reader_page.wait_for_load_state("domcontentloaded")
                print("[+] Reader opened in new tab")
                return reader_page, asin
            except PWTimeout:
                pass
            print("[+] Reader opened inline — waiting for iframe...")
            try:
                await page.wait_for_selector(reader_iframe_sel, timeout=15_000)
                print("[+] Found inline reader iframe")
                return page, asin
            except PWTimeout:
                continue
        except Exception:
            continue

    print("[!] Could not open reader")
    return page, asin


def b36_to_int(code: str) -> int:
    """Convert a 3-char base-36 page code (e.g. '00A' -> 10, '03L' -> 129)."""
    return int(code, 36)


async def scrape_reader(reader_page, output_dir: Path, captured: dict):
    output_dir.mkdir(parents=True, exist_ok=True)

    # Wait for the reader to fully render (scroll layout — all pages in DOM at once)
    await reader_page.wait_for_timeout(5_000)

    # ------------------------------------------------------------------ #
    # Scroll to bottom to ensure all images are loaded
    # ------------------------------------------------------------------ #
    print("[+] Scrolling to load all pages...")
    await reader_page.evaluate("""() => {
        const el = document.querySelector('.litb-reading-area') || document.scrollingElement;
        if (el) el.scrollTop = el.scrollHeight;
    }""")
    await reader_page.wait_for_timeout(3_000)

    # ------------------------------------------------------------------ #
    # Read all page images directly from the DOM using data-page attribute
    # ------------------------------------------------------------------ #
    print("[+] Collecting page image URLs from DOM...")
    pages = await reader_page.evaluate("""() => {
        return [...document.querySelectorAll('[data-page] img[src*="cloudfront"]')]
            .map(img => ({
                page: parseInt(img.closest('[data-page]').getAttribute('data-page')),
                src: img.src
            }))
            .filter(p => p.src && p.page > 0);
    }""")

    print(f"[+] Found {len(pages)} pages in DOM")

    # Merge with anything already captured via interception, prefer DOM src
    # (interception may have missed pages 10+ due to base-36 encoding)
    api_ctx = reader_page.context.request
    for item in pages:
        pn = item["page"]
        src = item["src"]
        if pn in captured:
            continue
        try:
            resp = await api_ctx.get(src)
            if resp.ok:
                captured[pn] = await resp.body()
                print(f"[+] Downloaded page {pn} ({len(captured[pn])//1024} KB)")
            else:
                print(f"[!] HTTP {resp.status} for page {pn}")
        except Exception as e:
            print(f"[!] Could not download page {pn}: {e}")

    if not captured:
        print("[!] No pages captured")
        return 0

    for page_num in sorted(captured.keys()):
        (output_dir / f"page_{page_num:03d}.jpg").write_bytes(captured[page_num])

    print(f"[+] Saved {len(captured)} pages to {output_dir}/")
    return len(captured)


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url = sys.argv[1]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,900",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            java_script_enabled=True,
        )

        page = await context.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            window.chrome = {runtime: {}};
            Object.defineProperty(navigator, 'permissions', {
                get: () => ({ query: (p) => Promise.resolve({state: 'granted'}) })
            });
        """)

        # Intercept CloudFront image responses across all pages and frames
        captured: dict[int, bytes] = {}

        async def on_response(response):
            if "cloudfront.net" not in response.url:
                return
            m = re.search(r"\.S([0-9A-Z]{3})\.", response.url)
            if not m:
                return
            pn = b36_to_int(m.group(1))
            if pn in captured:
                return
            try:
                body = await response.body()
                if body:
                    captured[pn] = body
                    print(f"[+] Intercepted page {pn} ({len(body)//1024} KB)")
            except Exception:
                pass

        context.on("response", on_response)

        try:
            reader_page, asin = await resolve_and_open(page, url)
            output_dir = Path("output") / asin
            print(f"[+] Output dir: {output_dir}")
            total = await scrape_reader(reader_page, output_dir, captured)
        except Exception as e:
            print(f"[!] Error: {e}")
            raise
        finally:
            await browser.close()

    print(f"[+] Done! {total} pages saved to output/{asin}/")


if __name__ == "__main__":
    asyncio.run(main())
