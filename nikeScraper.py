"""Nike sale scraper.

Nike exposes its sale catalog through an internal "product wall" API. We open
the sale page in a headless browser and intercept those JSON responses (the
original approach), which gives exact current/original prices -- far cleaner
than reading rendered text.

Two ways to use it:
  - as a DealsTracker plugin:  scrape(store) -> short summary string
  - standalone:                `python nikeScraper.py` -> dumps full data.csv
"""

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
API_MARKER = "api.nike.com/discover/product_wall"
SALE_URL = "https://www.nike.com/w/mens-sale-clothing-3yaepz6ymx6"


def _collect(max_items=None, full_scroll=False):
    """Open the Nike sale page and intercept product-wall API responses,
    returning a list of product dicts. Playwright is imported lazily so the
    rest of DealsTracker still works if it isn't installed."""
    from playwright.sync_api import sync_playwright

    results = []

    def on_response(resp):
        if API_MARKER not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        for group in data.get("productGroupings") or []:
            for prod in group.get("products") or []:
                prices = prod.get("prices") or {}
                cur, orig = prices.get("currentPrice"), prices.get("initialPrice")
                title = (prod.get("copy") or {}).get("title")
                if title and cur is not None and orig:
                    results.append({
                        "name": title,
                        "subtitle": (prod.get("copy") or {}).get("subTitle"),
                        "price": cur,
                        "original_price": orig,
                        "product_code": prod.get("productCode"),
                        "pdp_url": (prod.get("pdpUrl") or {}).get("url"),
                    })

    # On Streamlit Cloud, Chromium comes from apt (packages.txt) rather than
    # `playwright install`, so point Playwright at it when it exists.
    import os, shutil
    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    launch_args = {"headless": True}
    if system_chromium and os.name != "nt":
        launch_args["executable_path"] = system_chromium
        launch_args["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        try:
            page = browser.new_page(user_agent=BROWSER_UA)
            page.on("response", on_response)
            page.goto(SALE_URL, wait_until="domcontentloaded", timeout=45000)
            # Scroll to trigger the product-wall API calls. The plugin only needs
            # a sample to summarize; the standalone dump walks the whole catalog.
            for _ in range(60 if full_scroll else 8):
                if max_items and len(results) >= max_items:
                    break
                try:
                    page.evaluate("window.scrollBy(0, 2000)")
                except Exception:
                    break  # renderer hiccup -> stop with what we have
                page.wait_for_timeout(700)
        finally:
            browser.close()
    return results


def scrape(store=None, max_items=150):
    """DealsTracker plugin entry point: return a short sale summary, or
    'No deals found'."""
    items = _collect(max_items=max_items)
    on_sale = [it for it in items if it["price"] < it["original_price"]]
    if not on_sale:
        return "No deals found"

    best = {}  # name -> (price, original, pct), keeping deepest discount
    for it in on_sale:
        pct = round((1 - it["price"] / it["original_price"]) * 100)
        if it["name"] not in best or pct > best[it["name"]][2]:
            best[it["name"]] = (it["price"], it["original_price"], pct)

    max_pct = max(v[2] for v in best.values())
    min_price = min(v[0] for v in best.values())
    top = sorted(best.items(), key=lambda kv: kv[1][2], reverse=True)[:5]

    lines = [f"Nike sale: {len(best)}+ items on sale, up to {max_pct}% off, "
             f"from ${min_price:.2f}. Top markdowns:"]
    for name, (cur, orig, pct) in top:
        lines.append(f"- {name} — ${cur:.2f} (was ${orig:.2f}, {pct}% off)")
    return "\n".join(lines)


if __name__ == "__main__":
    import pandas as pd
    items = _collect(full_scroll=True)
    df = pd.DataFrame(items)
    print(df)
    df.to_csv("data.csv", index=False)
    print(f"\nSaved {len(df)} rows to data.csv")
