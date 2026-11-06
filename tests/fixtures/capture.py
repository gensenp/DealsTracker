"""Re-capture the extraction fixtures.

Run from the project root when the sale page has changed enough that
tests/test_extraction.py no longer reflects reality:

    python tests/fixtures/capture.py [url]

Writes hm_sale_page.txt (the scraped text the model reads) and
hm_extraction.json (what it returned). Both are committed, so the offline
tests stay runnable without a network or an API key.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_URL = "https://www2.hm.com/en_us/men/sale/view-all.html"


def main(url=DEFAULT_URL):
    import DealsTracker as dt

    here = Path(__file__).parent
    text = dt.page_text(url)
    if not text:
        raise SystemExit(f"no readable text at {url} -- blocked, or page moved")

    (here / "hm_sale_page.txt").write_text(text[:20000], encoding="utf-8")
    deals = dt.extract_deals(text, url)
    (here / "hm_extraction.json").write_text(
        json.dumps({"url": url, "deals": deals}, indent=1), encoding="utf-8")

    print(f"captured {len(text)} chars, {len(deals['items'])} items, "
          f"{len(deals['promotions'])} promotions")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)
