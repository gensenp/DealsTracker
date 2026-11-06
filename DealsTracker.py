import json
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import time
from dotenv import load_dotenv
from groq import Groq

from browser import BROWSER_UA, open_page
from db import STAGE_FETCH, STAGE_HOVER, STAGE_RENDER
from deals import coerce_deals, empty_deals, format_deals
import nikeScraper
import uniqloScraper

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_PLACES_API_KEY = (os.environ.get("GOOGLE_PLACES_API_KEY") or "").strip().strip('"\'')

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY not set in .env (get one at console.groq.com)")
if not GOOGLE_PLACES_API_KEY:
    raise RuntimeError("GOOGLE_PLACES_API_KEY not set in .env (get one at console.cloud.google.com)")

client = Groq(api_key=GROQ_API_KEY)

LLM_MODEL = "openai/gpt-oss-120b"

# How much page text the model reads, and how much JSON it may write back.
# Groq's free tier allows 8000 tokens per minute, and ~1.8 chars make a token,
# so 20k characters of page text is rejected outright with a 413. Staying under
# the limit costs a little of the longest pages but avoids a guaranteed failure
# on every one of them; raise this if the account moves to a paid tier.
MAX_PROMPT_CHARS = 12000
# A full sale page runs to 70+ items, and the default budget cuts the model off
# mid-array: Groq then rejects the truncated JSON with a 400 rather than
# returning it. Urban Outfitters' sale page needs ~10k tokens to come back
# whole, and failed outright until this was raised.
MAX_COMPLETION_TOKENS = 16000

_EXTRACTION_PROMPT = """You are reading a clothing store web page: {url}

Extract every deal on the page as JSON matching exactly this shape:

{{"items": [{{"name": "Hazel Bubble Top", "sale_price": 68, "original_price": 138, "product_url": null}}],
 "promotions": ["20% off jeans", "Free shipping over $50"]}}

Rules:
- "items" holds individual products that are marked down: the price shown is
  below a regular/original price, or the item is labelled Final Sale,
  Clearance or "Save $X".
- "promotions" holds storewide or category offers and coupon codes -- anything
  not tied to one product. Plain strings.
- sale_price is the price a shopper pays now. original_price is the crossed-out
  price, or null if the page doesn't show one. Never invent an original price,
  and never copy the sale price into it.
- For a price range, use the lowest number.
- Numbers only: no currency symbols, no commas.
- Omit anything that isn't a real markdown. If the page has no deals at all,
  return {{"items": [], "promotions": []}}.

Web page text:
{text}"""


def _retry_on_less_text(exc):
    """Errors a smaller slice of the page can fix.

    json_validate_failed: Groq rejects its own output when the model was cut
    off mid-JSON. rate_limit_exceeded: the request alone is over the account's
    tokens-per-minute allowance."""
    message = str(exc)
    return "json_validate_failed" in message or "rate_limit_exceeded" in message


def extract_deals(text, url):
    """Ask the model for structured deals. Returns the shared deals shape.

    If a request fails for a reason less text can fix, retry on less of the
    page: fewer items fit in the reply, which is worse than a complete read but
    much better than losing the chain entirely."""
    last_error = None
    for attempt, chars in enumerate(
            (MAX_PROMPT_CHARS, MAX_PROMPT_CHARS // 2, MAX_PROMPT_CHARS // 4)):
        if attempt and "rate_limit_exceeded" in str(last_error):
            time.sleep(2)  # let the per-minute allowance recover
        try:
            res = client.chat.completions.create(
                model=LLM_MODEL,
                temperature=0,  # extraction, not writing -- same page, same rows
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                response_format={"type": "json_object"},
                messages=[{"role": "user",
                           "content": _EXTRACTION_PROMPT.format(
                               url=url, text=text[:chars])}],
            )
        except Exception as exc:
            if not _retry_on_less_text(exc):
                raise
            last_error = exc
            continue

        try:
            return coerce_deals(json.loads(res.choices[0].message.content))
        except (json.JSONDecodeError, TypeError):
            return empty_deals()

    raise last_error

def build_location(city, state=None, postal_code=None):
    """Join the location parts into the form Places understands best:
    "San Jose, CA 95113". Any part may be blank -- a bare ZIP is enough to
    pin down a city, and a bare city still works as it always did."""
    city = (city or "").strip()
    state = (state or "").strip()
    postal_code = (postal_code or "").strip()

    location = ", ".join(p for p in (city, state) if p)
    return f"{location} {postal_code}".strip() if postal_code else location

def find_clothing_stores(city, state=None, postal_code=None):
    location = build_location(city, state, postal_code)
    if not location:
        raise ValueError("Enter a city or a ZIP code.")

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        # places.id and places.location come from the Essentials SKU, which is
        # already covered by the Pro fields below -- no change in billing tier.
        "X-Goog-FieldMask": ("places.id,places.displayName,places.websiteUri,"
                             "places.formattedAddress,places.location"),
    }
    body = {
        "textQuery": f"clothing stores in {location}",
        "includedType": "clothing_store",
        "pageSize": 20,
    }
    res = requests.post(url, headers=headers, json=body, timeout=15)
    if not res.ok:
        # Show a fingerprint of the key in use (never the full key) so a
        # mismatched deploy secret is easy to spot.
        k = GOOGLE_PLACES_API_KEY
        raise RuntimeError(f"Places API {res.status_code} (key len={len(k)}, "
                           f"ends ...{k[-4:]}): {res.text}")
    stores = []
    for place in res.json().get("places", []):
        website = place.get("websiteUri")
        if not website:
            continue
        location = place.get("location") or {}
        stores.append({
            "place_id": place.get("id"),
            "name": place.get("displayName", {}).get("text", "(unknown)"),
            "website": website,
            "address": place.get("formattedAddress", ""),
            "lat": location.get("latitude"),
            "lng": location.get("longitude"),
        })
    return stores

# Ordered strongest -> weakest deal signal; we return the highest-ranked match
# so e.g. a "Current Promotions" link wins over an incidental keyword elsewhere.
# "clearance"/"sale" name an actual catalog section, while "deal"/"promo" tend
# to label partner or campaign pages (H&M files a student-discount offer under
# /home/deals/), so they rank lower.
# "special" and "event" are deliberately absent: in retail they catch
# "Partywear & Special Occasions" and in-store events far more often than they
# catch a deal, and a false hit is worse than none -- it sends the scraper off
# to summarize a page with no prices on it.
DEAL_KEYWORDS = ["clearance", "sale", "promotion", "promo", "deal",
                 "coupon", "discount", "offer"]

# Keywords good enough to stop hunting: a real sale section, not a campaign page.
_STRONG_RANK = 1

# Markers for the "everything in this section" page. Without this we'd happily
# take H&M's /men/sale/pants.html over its /men/sale/view-all.html and report
# only the trousers on sale.
_BROAD_PATTERN = re.compile(r"(?<![a-z])(view[-_]?all|shop[-_]?all|all)(?![a-z])")

# Whole-word (optionally plural) matching, so "wholesale", "Salem" and "dealer"
# don't register as deals. Hyphens, slashes and the "--"/"__" in BEM class names
# all read as boundaries, which is what lets both "/on-sale" and
# "c-pwa-header-navigation__link--sale" match.
_KEYWORD_PATTERNS = [(rank, re.compile(rf"(?<![a-z]){re.escape(k)}s?(?![a-z])"))
                     for rank, k in enumerate(DEAL_KEYWORDS)]

# Attributes carrying the nav's own labelling of a link, e.g. Urban Outfitters'
# class="...header-navigation__link--sale" id="l1-sale" on an otherwise plain
# <a href="/sale"><span>Sale</span></a>.
_SIGNAL_ATTRS = ("class", "id", "aria-label", "title", "data-qa")

def _host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host

# Multi-part public suffixes we're likely to meet, so that "foo.co.uk" and
# "bar.co.uk" aren't mistaken for one site.
_MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.jp", "co.nz", "co.za", "com.br", "com.mx", "co.in", "com.sg",
}

def _registrable(host):
    """The part of a hostname that identifies the site -- nordstromrack.com for
    both www.nordstromrack.com and stores.nordstromrack.com. Chains often park
    their store locator on a separate subdomain and link back to the main shop,
    so comparing full hostnames would discard the very link we want."""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])

def _same_site(host, base_host):
    return _registrable(host) == _registrable(base_host)

def _attr_blob(link):
    parts = []
    for attr in _SIGNAL_ATTRS:
        val = link.get(attr)
        if isinstance(val, list):  # bs4 returns class as a list
            val = " ".join(val)
        if val:
            parts.append(val)
    return " ".join(parts).lower()

def _in_nav(link):
    """True if the link belongs to the site's main navigation -- either inside
    a <nav>, or labelled as nav by its own classes. Nav links point at the real
    section; promo banners sitting in the same header point at one-off landing
    pages with far less on them."""
    return bool(link.find_parent("nav")) or "nav" in _attr_blob(link)

def _rank_link(link):
    """Lowest-is-best sort key for a candidate link, or None if it shows no
    deal signal at all."""
    parsed = urlparse(link["href"].lower())
    # Match the path only, never the query string: tracking params are full of
    # words like "promo" and "offer" that have nothing to do with the target
    # page (Gap's credit-card signup carries "..._CARD_RET_PROMO").
    path = parsed.path
    text = link.get_text(" ", strip=True).lower()
    attrs = _attr_blob(link)

    for rank, pattern in _KEYWORD_PATTERNS:
        if pattern.search(path) or pattern.search(text) or pattern.search(attrs):
            return (
                0 if _in_nav(link) else 1,      # nav link beats banner or body link
                rank,                           # strongest keyword
                1 if parsed.query else 0,       # "/sale" beats "/promo-sale?html_banner=1"
                0 if _BROAD_PATTERN.search(path) else 1,  # "view all" beats one subcategory
                len(parsed.path),               # section root beats a subcategory
            )
    return None

def _best_deal_link_keyed(html, base_url):
    """Best deal link in this HTML as (url, sort_key), or (None, None)."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = _host(base_url)
    best, best_key = None, None
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target = urljoin(base_url, href)
        # Stay on the store's own site; outbound "deals" links are affiliate or
        # social noise.
        if not _same_site(_host(target), base_host):
            continue
        key = _rank_link(link)
        if key is not None and (best_key is None or key < best_key):
            best, best_key = target, key
    return best, best_key

def _best_deal_link(html, base_url):
    return _best_deal_link_keyed(html, base_url)[0]

# Top-level nav items worth pointing at, and how many to try before giving up.
_NAV_ITEM_SELECTOR = "nav a, nav button, header a, header button"
_MAX_HOVERS = 12

# Hovering is a last resort, and a slow site can make it crawl -- Primark spent
# 110s working through its menus. Cap the whole rendered phase so one store
# can't stall a search over a dozen of them; we return the best link found so
# far when the budget runs out.
_RENDER_BUDGET_S = 25

def _find_deal_link_rendered(base_url, timeout_ms=20000):
    """Render the page and hunt for a deal link, hovering top-level nav items
    if the visible nav has nothing.

    Mega-menus build their submenu only once the parent is pointed at, so the
    Sale link genuinely isn't in the DOM until then -- H&M files its sale pages
    under Women/Men this way, and hovering "WOMEN" takes the page from 82 links
    to over 1100."""
    best, best_key = None, None
    stage = STAGE_RENDER
    deadline = time.monotonic() + _RENDER_BUDGET_S
    with open_page() as page:
        page.goto(base_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)  # let the nav hydrate
        best, best_key = _best_deal_link_keyed(page.content(), base_url)
        if best_key and best_key[1] <= _STRONG_RANK:
            return best, stage

        hovered = 0
        for item in page.query_selector_all(_NAV_ITEM_SELECTOR):
            if hovered >= _MAX_HOVERS or time.monotonic() > deadline:
                break
            try:
                if not item.is_visible():
                    continue
                item.hover(timeout=2500)
                hovered += 1
                page.wait_for_timeout(700)  # let the submenu render
                candidate, key = _best_deal_link_keyed(page.content(), base_url)
            except Exception:
                continue  # element detached or moved mid-hover -- try the next
            if key is not None and (best_key is None or key < best_key):
                best, best_key = candidate, key
                stage = STAGE_HOVER
            # A real sale section is as good as it gets; stop opening menus.
            if best_key and best_key[1] <= _STRONG_RANK:
                break
    return best, stage

def discover_deals_page(base_url):
    """Find a store's sale/deals page, as (url, stage) -- stage naming which
    tier of the escalation found it, which the scrape log records.

    Starts from whatever URL Places gave us, often a store-locator page rather
    than a homepage. Tries a plain fetch first, then a rendered one, because big
    chains build their nav in JavaScript and turn away non-browser clients, so
    the <a href="/sale"> we want frequently only exists once the page has run."""
    html = ""
    try:
        res = requests.get(base_url, headers={"User-Agent": BROWSER_UA}, timeout=10)
        if res.ok:
            html = res.text
    except requests.RequestException:
        pass

    best = _best_deal_link(html, base_url) if html else None
    if best:
        return best, STAGE_FETCH
    try:
        return _find_deal_link_rendered(base_url)
    except Exception:
        return None, None

def find_deals_page(base_url):
    """The discovered sale page URL, or None."""
    return discover_deals_page(base_url)[0]

def render_html(url, timeout_ms=45000):
    """Rendered DOM of a page, once its JavaScript has built the nav."""
    with open_page() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)  # let the nav hydrate
        return page.content()

def render_text(url, timeout_ms=45000):
    """Render a JavaScript-heavy page in a headless browser and return its
    visible text. Used as a fallback when plain requests gets a blank shell
    (e.g. React/SPA sites like Nordstrom)."""
    with open_page() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        for _ in range(6):  # scroll to trigger lazy-loaded content
            page.evaluate("window.scrollBy(0, 1500)")
            page.wait_for_timeout(500)
        page.wait_for_timeout(1500)
        return page.inner_text("body")

def page_text(url):
    """Readable text of a page: plain fetch first, browser if that gives a
    shell. Returns "" when nothing readable came back (bot wall, dead page)."""
    text = ""
    try:
        res = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=10)
        if res.ok:
            soup = BeautifulSoup(res.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
    except requests.RequestException:
        pass
    # Blank shell (JS-rendered) or blocked -> render in a real browser.
    if len(text) < 200:
        text = render_text(url)
    return text if len(text.strip()) >= 50 else ""


def scrape_and_clean(url):
    """Scrape one page into the shared deals shape."""
    try:
        text = page_text(url)
        return extract_deals(text, url) if text else empty_deals()
    except Exception:
        return empty_deals()

# Per-chain scraper plugins, keyed by website domain. A chain with an
# accessible API/feed gets a custom scraper here for cleaner, structured data;
# everything else falls through to the generic find-page + render + LLM path.
CUSTOM_SCRAPERS = {
    "nike.com": nikeScraper.scrape,
    "uniqlo.com": uniqloScraper.scrape,
}

def custom_scraper_for(website):
    """The per-chain scraper registered for this site, or None."""
    host = _host(website or "")
    for domain, scraper in CUSTOM_SCRAPERS.items():
        if host == domain or host.endswith("." + domain):
            return scraper
    return None

def get_store_deals(store):
    """Deals for one store, in the shared shape. Tries a registered per-chain
    plugin first (matched on the website's domain), otherwise discovers the
    sale page and scrapes it."""
    scraper = custom_scraper_for(store.get("website", ""))
    if scraper:
        try:
            return coerce_deals(scraper(store))
        except Exception:
            return empty_deals()
    target_url = find_deals_page(store["website"]) or store["website"]
    return scrape_and_clean(target_url)

if __name__ == "__main__":
    city = "San Jose"
    print(f"Searching for clothing stores in {city}...")
    stores = find_clothing_stores(city)
    print(f"Scanning {len(stores)} stores...\n")

    for store in stores:
        deals = get_store_deals(store)
        print(f"🛍️ {store['name']} — {store['address']}\n   {store['website']}")
        print(f"  💰 {format_deals(deals)}")
        time.sleep(1)
