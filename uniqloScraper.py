"""Uniqlo sale scraper.

Uniqlo's public commerce API lists its sale items (`flagCodes=discount`) with
prices, so no browser is needed -- a single requests call gets the full catalog,
which is faster and more complete than rendering the page.

  - as a DealsTracker plugin:  scrape(store) -> short summary string
  - standalone:                `python uniqloScraper.py`
"""
import requests

API = "https://www.uniqlo.com/us/api/commerce/v5/en/products"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


PRODUCT_URL = "https://www.uniqlo.com/us/en/products/{product_id}"


def scrape(store=None, limit=100):
    """DealsTracker plugin entry point: deals in the shared shape.

    original_price is always None here. The endpoint exposes `prices.base` and
    `prices.promo`, but across every discounted item the two carry the same
    value and `isDualPrice` is false, so there is no crossed-out price to
    report. Those items are excluded from discount ranking rather than being
    shown as 0% off."""
    try:
        r = requests.get(API, params={"flagCodes": "discount", "limit": str(limit),
                                       "offset": "0", "httpFailure": "true"},
                         headers={"User-Agent": UA}, timeout=15)
        result = r.json().get("result", {})
    except Exception:
        return {"items": [], "promotions": []}

    items = []
    for it in result.get("items") or []:
        name = it.get("name")
        value = ((it.get("prices") or {}).get("base") or {}).get("value")
        if not name or not value:
            continue
        product_id = it.get("productId")
        items.append({
            "name": name,
            "sale_price": value,
            "original_price": None,
            "product_url": PRODUCT_URL.format(product_id=product_id) if product_id else None,
        })

    return {"items": items, "promotions": []}


if __name__ == "__main__":
    import json
    print(json.dumps(scrape(), indent=2)[:1200])
