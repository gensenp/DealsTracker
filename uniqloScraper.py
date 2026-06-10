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


def scrape(store=None, limit=100):
    """DealsTracker plugin entry point: return a short sale summary, or
    'No deals found'."""
    try:
        r = requests.get(API, params={"flagCodes": "discount", "limit": str(limit),
                                       "offset": "0", "httpFailure": "true"},
                         headers={"User-Agent": UA}, timeout=15)
        result = r.json().get("result", {})
    except Exception:
        return "No deals found"

    items = result.get("items") or []
    total = (result.get("pagination") or {}).get("total") or len(items)
    priced = []
    for it in items:
        name = it.get("name")
        value = ((it.get("prices") or {}).get("base") or {}).get("value")
        if name and value:
            priced.append((name, value))
    if not priced:
        return "No deals found"

    min_price = min(v for _, v in priced)
    examples = sorted(priced, key=lambda nv: nv[1])[:5]
    lines = [f"Uniqlo sale: {total} items on sale, from ${min_price:.2f}. Examples:"]
    for name, value in examples:
        lines.append(f"- {name} — ${value:.2f}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(scrape())
