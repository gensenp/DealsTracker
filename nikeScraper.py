"""Nike sale scraper.

Nike exposes its sale catalog through an internal "product wall" API. We open
the sale page in a headless browser and intercept those JSON responses (the
original approach), which gives exact current/original prices -- far cleaner
than reading rendered text.

Two ways to use it:
  - as a DealsTracker plugin:  scrape(store) -> short summary string
  - standalone:                `python nikeScraper.py` -> dumps full data.csv
"""

API_MARKER = "api.nike.com/discover/product_wall"
SALE_URL = "https://www.nike.com/w/mens-sale-clothing-3yaepz6ymx6"


def _collect(max_items=None, full_scroll=False):
    """Open the Nike sale page and intercept product-wall API responses,
    returning a list of product dicts. Playwright is imported lazily (inside
    browser.open_page) so the rest of DealsTracker still works without it."""
    from browser import open_page

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

    with open_page() as page:
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
    return results


def scrape(store=None, max_items=150):
    """DealsTracker plugin entry point: deals in the shared shape.

    Nike's API gives exact current/original pairs, so every item carries a real
    original_price -- no LLM reading prices off rendered text."""
    items = _collect(max_items=max_items)

    best = {}  # name -> item, keeping the deepest discount per product
    for it in items:
        if not it["original_price"] or it["price"] >= it["original_price"]:
            continue
        current = best.get(it["name"])
        if current is None or it["price"] < current["sale_price"]:
            best[it["name"]] = {
                "name": it["name"],
                "sale_price": it["price"],
                "original_price": it["original_price"],
                "product_url": it.get("pdp_url"),
            }

    return {"items": list(best.values()), "promotions": []}


if __name__ == "__main__":
    import pandas as pd
    items = _collect(full_scroll=True)
    df = pd.DataFrame(items)
    print(df)
    df.to_csv("data.csv", index=False)
    print(f"\nSaved {len(df)} rows to data.csv")
