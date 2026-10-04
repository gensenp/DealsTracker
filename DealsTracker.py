import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import time
from dotenv import load_dotenv
from groq import Groq

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

def ask_groq(text, url):
    res = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": f"""You are looking at a clothing store web page: {url}

List every deal, discount, or sale item on the page. ALL of these count as deals:
- Storewide / category promotions (e.g. "20% off jeans", "buy one get one free", "free shipping over $50")
- Coupon or promo codes
- Individual items marked down: any item whose sale price is below its regular/original price, or labeled "Final sale", "Clearance", or "Save $X"

For marked-down items, give the item name with both prices, e.g. "Hazel Bubble Top — $68 (was $138)".

If there are genuinely no deals or sale items anywhere on the page, reply with exactly: No deals found

Web page text:
{text[:20000]}"""}]
    )
    return res.choices[0].message.content

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
        stores.append({
            "name": place.get("displayName", {}).get("text", "(unknown)"),
            "website": website,
            "address": place.get("formattedAddress", ""),
        })
    return stores

def find_deals_page(base_url):
    headers = {"User-Agent": "Mozilla/5.0"}
    # Ordered strongest -> weakest deal signal; we return the highest-ranked match
    # so e.g. a "Current Promotions" link wins over an incidental keyword elsewhere.
    keywords = ["clearance", "promotion", "promo", "deal", "sale",
                "coupon", "special", "discount", "offer", "event"]
    try:
        res = requests.get(base_url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        best, best_rank = None, len(keywords)
        for link in soup.find_all("a", href=True):
            href = link["href"].lower()
            text = link.text.lower()
            for rank, k in enumerate(keywords):
                if k in href or k in text:
                    if rank < best_rank:
                        best, best_rank = urljoin(base_url, link["href"]), rank
                    break
        return best
    except Exception:
        pass
    return None

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

def render_text(url, timeout_ms=45000):
    """Render a JavaScript-heavy page in a headless browser and return its
    visible text. Used as a fallback when plain requests gets a blank shell
    (e.g. React/SPA sites like Nordstrom). Imported lazily so the module still
    works if Playwright isn't installed."""
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
            return page.inner_text("body")
        finally:
            browser.close()

def scrape_and_clean(url):
    try:
        text = ""
        try:
            res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
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
        # Still nothing readable (bot-walled chain, dead page) -> treat as no
        # deals rather than surfacing an error to the user.
        if len(text.strip()) < 50:
            return "No deals found"
        return ask_groq(text, url)
    except Exception:
        return "No deals found"

# Per-chain scraper plugins, keyed by website domain. A chain with an
# accessible API/feed gets a custom scraper here for cleaner, structured data;
# everything else falls through to the generic find-page + render + LLM path.
CUSTOM_SCRAPERS = {
    "nike.com": nikeScraper.scrape,
    "uniqlo.com": uniqloScraper.scrape,
}

def get_store_deals(store):
    """Get deal text for one store. Tries a registered per-chain plugin first
    (matched on the website's domain), otherwise uses the generic pipeline.
    Returns a deal-summary string, or 'No deals found'."""
    host = urlparse(store.get("website", "")).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, scraper in CUSTOM_SCRAPERS.items():
        if host == domain or host.endswith("." + domain):
            try:
                return scraper(store)
            except Exception:
                return "No deals found"
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
        print("  No deals" if "no deals found" in deals.lower() else f"  💰 {deals}")
        time.sleep(1)
