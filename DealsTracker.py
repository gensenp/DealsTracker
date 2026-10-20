import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import time
from dotenv import load_dotenv
from groq import Groq, APIConnectionError, AuthenticationError, PermissionDeniedError

import nikeScraper
import uniqloScraper
import platformScrapers

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY not set in .env (get one at console.groq.com)")
if not GOOGLE_PLACES_API_KEY:
    raise RuntimeError("GOOGLE_PLACES_API_KEY not set in .env (get one at console.cloud.google.com)")

# The SDK defaults to a 60s timeout with retries, so an unreachable API costs
# minutes before it reports failure. Promos are a nice-to-have next to the
# markdowns we parse ourselves, so fail fast instead.
client = Groq(api_key=GROQ_API_KEY, timeout=20.0, max_retries=1)

def ask_groq(text, url, today=None):
    """Ask the model for storewide promotions only.

    Individual markdowns are read straight out of the markup by
    platformScrapers, which is exact and has no length limit, so the model's job
    is narrowed to the thing it is actually better at: banner and coupon copy.
    That also lets us send a much smaller slice of text, so the promo wording no
    longer competes with thousands of product prices for the context budget."""
    today = today or time.strftime("%Y-%m-%d")
    res = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": f"""You are reading banner and offer text from a clothing store site: {url}
Today's date is {today}.

List only storewide or category-wide promotions, such as "40% off everything",
"20% off jeans", "buy one get one free", "free shipping over $50", or coupon
codes. Do not list individual product prices; those are collected separately.

Ignore any offer whose stated end date is before today — these pages often keep
expired coupons online. If an offer has no date, assume it is current.

Reply with one short line per promotion, and nothing else. If there are no
current storewide promotions, reply with exactly: No deals found

Page text:
{text[:8000]}"""}]
    )
    return (res.choices[0].message.content or "").strip()

def find_clothing_stores(city):
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.websiteUri,places.formattedAddress",
    }
    body = {
        "textQuery": f"clothing stores in {city}",
        "includedType": "clothing_store",
        "pageSize": 20,
    }
    res = requests.post(url, headers=headers, json=body, timeout=15)
    if not res.ok:
        raise RuntimeError(f"Places API {res.status_code}: {res.text}")
    stores = []
    for place in res.json().get("places", []):
        website = place.get("websiteUri")
        if not website:
            continue
        stores.append({
            "name": place.get("displayName", {}).get("text", "(unknown)"),
            "website": website,
            "address": place.get("formattedAddress", ""),
        })
    return stores

# Deal-page candidates, strongest signal first. Tier 0 is the actual markdown
# catalog ("shop all sale"), which is what we want to parse for items; promo-code
# pages rank last because chains leave them up long after the offer expires
# (loft.com's /sale/current-offers/ still served codes that ended in 2023).
PAGE_TIERS = [
    re.compile(r"(sale|clearance).*(shop[-_]?all|view[-_]?all|all[-_]?sale)"),
    re.compile(r"/(sale|clearance|outlet|markdowns?)(/|$|\?)"),
    re.compile(r"sale|clearance|outlet|markdown"),
    re.compile(r"deal|discount|special"),
    re.compile(r"promo|offer|coupon"),
]
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "#")


def find_deal_pages(base_url, limit=3, html=None):
    """Return up to `limit` candidate deal-page URLs on the store's own domain,
    best first.

    The old version kept a single best match and broke ties by DOM order, which
    on loft.com meant picking the promo-code page out of 33 links that all tied
    on "sale" -- the real sale catalog was never visited. We now score on the URL
    path (more reliable than link text), keep several candidates, and skip
    mailto:/tel: links, which otherwise matched on substrings like
    "directsales@...".
    """
    try:
        if html is None:
            res = requests.get(base_url, headers={"User-Agent": platformScrapers.UA},
                               timeout=10)
            html = res.text
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    scored, seen = [], set()
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href or href.lower().startswith(SKIP_SCHEMES):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).scheme not in ("http", "https"):
            continue
        if urlparse(full).netloc.lower().removeprefix("www.") != host:
            continue  # stay on the store's own site
        full = full.split("#")[0]
        if full in seen:
            continue
        seen.add(full)

        path = urlparse(full).path.lower()
        text = link.get_text(" ", strip=True).lower()
        for tier, pattern in enumerate(PAGE_TIERS):
            if pattern.search(path):
                scored.append((tier, full))
                break
            # Link text is a weaker signal than the path, so a text-only match
            # sorts below any path match at the same tier.
            if pattern.search(text):
                scored.append((tier + 0.5, full))
                break

    scored.sort(key=lambda ts: ts[0])
    return [url for _, url in scored[:limit]]

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

def render_html(url, timeout_ms=45000):
    """Render a JavaScript-heavy page in a headless browser and return its HTML.
    Used as a fallback when plain requests gets a blank shell (e.g. React/SPA
    sites like Nordstrom). Imported lazily so the module still works if
    Playwright isn't installed.

    Returns markup rather than visible text so the caller can still parse
    product tiles out of it -- the old text-only version meant JS-rendered
    stores lost every markdown and fell back to promo detection alone."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=BROWSER_UA)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            for _ in range(6):  # scroll to trigger lazy-loaded content
                page.evaluate("window.scrollBy(0, 1500)")
                page.wait_for_timeout(500)
            page.wait_for_timeout(1500)
            return page.content()
        finally:
            browser.close()

# Short pages that are really bot walls or error stubs. These used to slip past
# the old length check (H&M/Levi's/Madewell all return ~210-char "Access Denied"
# bodies, just over the 200-char threshold) and get summarized as "no deals".
BLOCK_MARKERS = ("access denied", "forbidden", "are you a human", "unusual traffic",
                 "enable javascript", "bot detection", "request blocked",
                 "pardon our interruption")


def fetch_page(url, timeout=15):
    """Fetch a page and return its HTML, falling back to a headless browser for
    JS-rendered shells. Raises on failure so callers can report why a store came
    back empty instead of silently reporting 'No deals found'."""
    try:
        res = requests.get(url, headers={"User-Agent": platformScrapers.UA},
                           timeout=timeout)
        html = res.text if res.status_code < 400 else ""
        status = res.status_code
    except requests.RequestException as e:
        html, status = "", type(e).__name__

    body = BeautifulSoup(html, "html.parser").get_text(" ", strip=True) if html else ""
    blocked = any(m in body[:2000].lower() for m in BLOCK_MARKERS)
    if not html or len(body) < 400 or blocked:
        # Blank shell, bot wall, or an outright 403/429 -> retry in a real
        # browser, which runs the JS challenge these sites gate on. H&M,
        # Levi's and Madewell all refuse plain requests this way.
        try:
            return render_html(url)
        except Exception as e:
            raise RuntimeError(f"HTTP {status}, browser fallback failed "
                               f"({type(e).__name__})") from e
    return html


def page_text(html):
    """Visible text for promo detection.

    Unlike the old cleaner this keeps <header> and <nav>: sitewide promos live
    in the announcement bar, so stripping those tags threw away most of what we
    were trying to find (on an Everlane sale page, 66 of 85 deal phrases)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)

# Per-chain scraper plugins, keyed by website domain. A chain with an
# accessible API/feed gets a custom scraper here for cleaner, structured data;
# everything else falls through to the generic find-page + render + LLM path.
CUSTOM_SCRAPERS = {
    "nike.com": nikeScraper.scrape,
    "uniqlo.com": uniqloScraper.scrape,
}

def get_store_deals(store):
    """Get deal text for one store.

    Order of attack:
      1. a per-chain plugin, if one is registered for this domain
      2. the Shopify product feed, if the site runs on Shopify
      3. parse markdown tiles out of the store's sale pages
      4. ask the model about storewide promos in the page's banner text

    Steps 3 and 4 are complementary -- a store can have both "40% off
    everything" and a rack of marked-down items -- so both are reported.
    Returns a deal-summary string, 'No deals found', or 'Error: ...'."""
    website = store.get("website", "")
    host = urlparse(website).netloc.lower().removeprefix("www.")
    for domain, scraper in CUSTOM_SCRAPERS.items():
        if host == domain or host.endswith("." + domain):
            try:
                return scraper(store)
            except Exception as e:
                return f"Error: {domain} scraper failed ({type(e).__name__})"

    try:
        home_html = fetch_page(website)
    except Exception as e:
        return f"Error: could not load site ({type(e).__name__}: {str(e)[:80]})"

    # Ranked sale-page candidates double as Shopify collection hints below.
    pages = find_deal_pages(website, html=home_html)

    if platformScrapers.detect_platform(home_html) == "shopify":
        handles = [m.group(1) for m in
                   (re.search(r"/collections/([^/?#]+)", p) for p in pages) if m]
        items = platformScrapers.shopify_sale(website, handles=handles)
        if items:
            summary = platformScrapers.summarize_items(items, label="Sale")
            promos, _ = _promo_text(home_html, website)
            return "\n".join(filter(None, [promos, summary]))

    # Walk the best few sale pages and pool their markdowns. Visiting more than
    # one matters: a chain's "shop all sale" page holds the items while its
    # promo page holds the codes.
    items, errors = [], []
    for url in pages:
        try:
            html = fetch_page(url)
        except Exception as e:
            errors.append(f"{type(e).__name__} on {url}")
            continue
        items.extend(platformScrapers.parse_markdown_tiles(BeautifulSoup(html, "html.parser")))
        if items:
            break  # first page with real markdowns is enough

    summary = platformScrapers.summarize_items(items, label="Sale")
    promos, promo_error = _promo_text(home_html, website)
    combined = "\n".join(filter(None, [promos, summary]))
    if combined:
        return combined
    # Nothing found -- say why, so a bot wall or a broken API key is
    # distinguishable from a store that genuinely has no sale on.
    if promo_error:
        return f"Error: {promo_error}"
    if errors:
        return f"Error: {errors[0]}"
    return "No deals found"


# Failures that will hit every remaining store, not just this one: a blocked
# network (corporate firewalls often filter AI API domains) or a bad key. Once
# we see one, stop calling the API for a while -- otherwise a scan of 20 stores
# pays 20 connection timeouts to learn the same thing. The cooldown expires on
# its own, so the app recovers without a restart when the network changes.
PROMO_FATAL_ERRORS = (APIConnectionError, AuthenticationError, PermissionDeniedError)
PROMO_COOLDOWN_SECONDS = 300
_promo_cooldown_until = 0.0
_promo_cooldown_reason = ""


def _promo_text(home_html, url):
    """Storewide promos from the homepage banner.

    Returns (promos, error), either of which may be None. The error is kept
    separate so a failed model call (an unreachable API, a rate limit) doesn't
    get glued onto an otherwise good list of markdowns -- but is still available
    to report when there is nothing else to show."""
    global _promo_cooldown_until, _promo_cooldown_reason
    if time.monotonic() < _promo_cooldown_until:
        return None, _promo_cooldown_reason

    try:
        text = page_text(home_html)
        if len(text.strip()) < 50:
            return None, "page had no readable text"
        promos = ask_groq(text, url)
    except PROMO_FATAL_ERRORS as e:
        _promo_cooldown_until = time.monotonic() + PROMO_COOLDOWN_SECONDS
        _promo_cooldown_reason = (f"promo lookup unavailable ({type(e).__name__}) -- "
                                  f"skipping promos for the next "
                                  f"{PROMO_COOLDOWN_SECONDS // 60} min")
        return None, _promo_cooldown_reason
    except Exception as e:
        return None, f"promo lookup failed ({type(e).__name__})"
    if not promos or "no deals found" in promos.lower():
        return None, None
    return promos, None

if __name__ == "__main__":
    city = "San Jose"
    print(f"Searching for clothing stores in {city}...")
    stores = find_clothing_stores(city)
    print(f"Scanning {len(stores)} stores...\n")

    for store in stores:
        deals = get_store_deals(store)
        print(f"🛍️ {store['name']} — {store['address']}\n   {store['website']}")
        print("  No deals" if "no deals found" in deals.lower() else f"  💰 {deals}")
        time.sleep(1)
