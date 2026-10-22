"""Shared headless-browser setup.

Two things decide whether a retailer's page loads at all:

* Which Chromium binary. Playwright's default headless shell is fingerprinted
  and turned away with a 403 by chains like Urban Outfitters, so locally we ask
  for the full "chromium" channel instead. On Streamlit Cloud, Chromium comes
  from apt (packages.txt) rather than `playwright install`, so Playwright has
  to be pointed at the system binary.
* Looking like an ordinary desktop browser: a real user agent, a normal
  viewport and locale, and no automation flag.
"""
import os
import shutil
from contextlib import contextmanager

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]


def _launch(playwright):
    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if system_chromium and os.name != "nt":
        return playwright.chromium.launch(
            headless=True,
            executable_path=system_chromium,
            args=_STEALTH_ARGS + ["--no-sandbox", "--disable-dev-shm-usage"],
        )
    try:
        return playwright.chromium.launch(
            headless=True, channel="chromium", args=_STEALTH_ARGS)
    except Exception:
        # No "chromium" channel installed -- fall back to the headless shell,
        # which works on most sites even though strict ones will block it.
        return playwright.chromium.launch(headless=True, args=_STEALTH_ARGS)


@contextmanager
def open_page():
    """Yield a Playwright page that presents as an ordinary desktop Chrome."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            context = browser.new_context(
                user_agent=BROWSER_UA,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            yield context.new_page()
        finally:
            browser.close()
