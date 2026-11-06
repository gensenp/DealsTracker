"""The one shape every deal source returns.

The LLM extractor and the per-chain API scrapers (Nike, Uniqlo) all hand back:

    {"items": [{"name": str,
                "sale_price": float | None,
                "original_price": float | None,
                "product_url": str | None}, ...],
     "promotions": [str, ...]}

`original_price` is nullable because not every source exposes one -- Uniqlo's
API returns the same value for its base and promo prices -- and an item without
it is excluded from discount ranking rather than shown as 0% off.

Nothing here touches the network or the database, so it stays cheap to test.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Guards against a model that ignores instructions and returns a whole catalog.
MAX_ITEMS = 200
MAX_PROMOTIONS = 20

# A "price" outside this range is a phone number, a postcode or a year rather
# than money. The ceiling is set well clear of real luxury retail -- Saks and
# Nordstrom Rack list coats and handbags in the low thousands -- so only
# obvious junk is dropped.
MIN_PLAUSIBLE_PRICE = 0.01
MAX_PLAUSIBLE_PRICE = 100_000.0

_PRICE_CHARS = re.compile(r"[^\d.\-]")


@dataclass
class CoercionStats:
    """What normalizing a source's output had to throw away or fix.

    Stored per scrape so extraction quality is visible over time: a chain whose
    rejected count suddenly climbs is usually a page redesign, not a sale that
    ended."""
    items_seen: int = 0
    items_kept: int = 0
    items_rejected: int = 0        # no usable name, or not a dict
    duplicates_dropped: int = 0    # same product, one row per colourway
    originals_corrected: int = 0   # original_price <= sale_price, so discarded
    promotions_rejected: int = 0
    truncated: bool = False        # hit MAX_ITEMS / MAX_PROMOTIONS

    def as_dict(self) -> dict:
        return asdict(self)


def empty_deals() -> dict:
    return {"items": [], "promotions": []}


def _coerce_price(value) -> float | None:
    """Accept 29.99, "29.99", "$29.99", "USD 29.99", "1,299" -> float.

    A range ("$4.95-$19.99") keeps its low end, which is the price a shopper
    actually sees first on the tile."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.replace(",", "")
        # Take the first number in a range or a "from $X" string.
        match = re.search(r"\d+(?:\.\d+)?", _PRICE_CHARS.sub(" ", text).replace("-", " "))
        if not match:
            return None
        try:
            price = float(match.group())
        except ValueError:
            return None
    if price < MIN_PLAUSIBLE_PRICE or price > MAX_PLAUSIBLE_PRICE:
        return None
    return round(price, 2)


def _coerce_item(raw, stats: CoercionStats) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None

    sale = _coerce_price(raw.get("sale_price"))
    original = _coerce_price(raw.get("original_price"))
    # An "original" at or below the sale price is noise, not a markdown.
    if sale is not None and original is not None and original <= sale:
        original = None
        stats.originals_corrected += 1

    url = raw.get("product_url") or raw.get("url")
    url = str(url).strip() if url else None
    if url and not url.startswith(("http://", "https://")):
        url = None

    return {"name": name[:500], "sale_price": sale,
            "original_price": original, "product_url": url}


def coerce_deals_with_stats(raw) -> tuple[dict, CoercionStats]:
    """Normalize whatever a source produced, and report what was discarded.

    Written to never raise: a scraper returning nonsense should look like a
    chain with no deals, not take down the search."""
    stats = CoercionStats()
    if not isinstance(raw, dict):
        return empty_deals(), stats

    raw_items = raw.get("items") or []
    if not isinstance(raw_items, (list, tuple)):
        raw_items = []
    stats.items_seen = len(raw_items)

    items, seen = [], set()
    for entry in raw_items:
        if len(items) >= MAX_ITEMS:
            stats.truncated = True
            break
        item = _coerce_item(entry, stats)
        if not item:
            stats.items_rejected += 1
            continue
        key = item["name"].lower()
        if key in seen:  # same product listed once per colourway
            stats.duplicates_dropped += 1
            continue
        seen.add(key)
        items.append(item)
    stats.items_kept = len(items)

    raw_promos = raw.get("promotions") or []
    if not isinstance(raw_promos, (list, tuple)):
        raw_promos = []

    promotions, seen_promos = [], set()
    for entry in raw_promos:
        if len(promotions) >= MAX_PROMOTIONS:
            stats.truncated = True
            break
        text = str(entry or "").strip()
        if not text or text.lower() in seen_promos:
            stats.promotions_rejected += 1
            continue
        seen_promos.add(text.lower())
        promotions.append(text[:500])

    return {"items": items, "promotions": promotions}, stats


def coerce_deals(raw) -> dict:
    """coerce_deals_with_stats, for callers that don't need the counts."""
    return coerce_deals_with_stats(raw)[0]


def has_deals(deals: dict) -> bool:
    return bool(deals.get("items") or deals.get("promotions"))


def discount_pct(item: dict) -> float | None:
    """Percent off, or None when the source gave no original price."""
    sale, original = item.get("sale_price"), item.get("original_price")
    if not sale or not original or original <= sale:
        return None
    return round((1 - sale / original) * 100)


def _format_price(value) -> str:
    return f"${value:,.2f}" if value is not None else "?"


def format_deals(deals: dict, limit: int = 15) -> str:
    """Human-readable summary, as the CLI and the old UI displayed it."""
    if not has_deals(deals):
        return "No deals found"

    lines = []
    for promo in deals.get("promotions") or []:
        lines.append(f"- {promo}")

    items = deals.get("items") or []
    # Deepest markdown first; items with no original price trail behind.
    items = sorted(items, key=lambda it: (discount_pct(it) is None,
                                          -(discount_pct(it) or 0)))
    for item in items[:limit]:
        pct = discount_pct(item)
        price = _format_price(item.get("sale_price"))
        if item.get("original_price") is not None:
            lines.append(f"- {item['name']} — {price} "
                         f"(was {_format_price(item['original_price'])}, {pct:.0f}% off)")
        else:
            lines.append(f"- {item['name']} — {price}")

    remaining = len(items) - limit
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return "\n".join(lines) if lines else "No deals found"
