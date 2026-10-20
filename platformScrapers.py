"""Platform-level sale extraction.

The per-domain plugins (nikeScraper, uniqloScraper) only help the one chain they
were written for. Most store sites, though, state their own markdowns directly
in the markup -- either as a Shopify product feed or as product tiles carrying a
sale price next to a struck-through original. Reading that markup is exact,
free, and has no text-truncation ceiling, so we do it instead of asking the LLM
to spot prices in a truncated page dump.

  - Shopify -> /collections/<handle>/products.json exposes compare_at_price
  - Everything else -> parse product tiles for a sale/original price pair
    (works on Salesforce Commerce Cloud, BigCommerce, Magento and most custom
    themes, since they all mark the original price with <s>/<del> or a class
    like "strike", "was", "compare-at" or "original")
"""
import re
import requests

# Deliberately the bare token, not a full Chrome string. Some chains gate on a
# browser-like UA and answer it with a JS bot-check shell: loft.com returns its
# whole 3.6MB catalog to "Mozilla/5.0" but only a 4.7KB stub to a realistic
# Chrome UA. The sparse UA gets served the server-rendered HTML we want.
UA = "Mozilla/5.0"

# Shopify collection handles to try, most-specific first.
SHOPIFY_HANDLES = ["sale", "sale-items", "clearance", "on-sale", "all-sale", "markdowns"]

# A tile's original ("was") price is marked up differently from its current
# price. These are the conventions themes use; anything else in the tile that
# looks like money is treated as the current price.
ORIGINAL_CLASS_RE = re.compile(
    r"strike|through|original|[-_]was|was[-_]|compare|regular|list[-_]?price|"
    r"standard|old[-_]?price|msrp", re.I)
# Guards against reading a tile's loyalty blurb ("$10 reward") as a price.
MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")

# Tile text runs the product name together with badges and button labels
# ("The Pencil Jean $148 $59.99 (59% off) Add to Bag"), so strip the furniture.
NAME_NOISE_RE = re.compile(
    r"\b(add to (bag|cart)|quick (view|shop|add)|new arrival|pre-?order|"
    r"best ?seller|matching set|sold out|final sale|on sale|sale|new)\b", re.I)
# Screen-reader price labels, e.g. J.Crew's "… necklace Original Price:". Only
# stripped from the end, so a product genuinely called "Price Perfect Tee" keeps
# its name.
PRICE_LABEL_RE = re.compile(
    r"\s*(original|sale|now|current|regular|reduced)?\s*price\s*:?\s*$", re.I)


def _clean_name(text):
    """Trim badge and button text off a product name, or return None."""
    name = text.split("$")[0]  # prices always follow the name in tile markup
    name = NAME_NOISE_RE.sub(" ", name)
    name = PRICE_LABEL_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" -–—|·,:")
    # A tile can repeat its name (image alt + heading); collapse an exact echo.
    half = len(name) // 2
    if half and name[:half].strip() == name[half:].strip():
        name = name[:half].strip()
    return name if 2 < len(name) <= 120 else None


def _money(text):
    """First dollar amount in `text` as a float, or None."""
    m = MONEY_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def detect_platform(html):
    """Identify the commerce platform from page markup, or None if unknown."""
    if "cdn.shopify.com" in html or "/cdn/shop/" in html or "Shopify.theme" in html:
        return "shopify"
    if "demandware" in html.lower():
        return "sfcc"
    return None


def shopify_sale(base_url, handles=None, timeout=15):
    """Pull marked-down items from a Shopify store's public product feed.

    `handles` are collection handles to try first -- the caller passes the ones
    it actually found linked on the site, since stores name their sale
    collection all sorts of things ("womens-sale", "final-cut") that no fixed
    list would cover. SHOPIFY_HANDLES is the fallback guess.
    Returns a list of item dicts (possibly empty)."""
    base = base_url.rstrip("/")
    items = []
    for handle in list(handles or []) + SHOPIFY_HANDLES:
        try:
            r = requests.get(f"{base}/collections/{handle}/products.json",
                             params={"limit": 250}, headers={"User-Agent": UA},
                             timeout=timeout)
            if not r.ok:
                continue
            products = r.json().get("products") or []
        except Exception:
            continue
        for p in products:
            for v in p.get("variants") or []:
                # Shopify sends price/compare_at_price as plain numbers, so parse
                # them directly rather than through the money regex.
                try:
                    price = float(v["price"]) if v.get("price") else None
                    compare = float(v["compare_at_price"]) if v.get("compare_at_price") else None
                except (TypeError, ValueError):
                    continue
                if price and compare and compare > price:
                    items.append({"name": p.get("title") or "(unnamed)",
                                  "price": price, "original": compare})
                    break  # one entry per product, not per size
        if items:
            break  # first handle that yields markdowns wins
    return items


def parse_markdown_tiles(soup):
    """Find product tiles in a parsed page and return the ones whose current
    price is below their original price.

    Tiles are matched on the near-universal "product-tile"/"product-card"/
    "product-item" class naming. Prices inside a tile are split into current vs
    original by markup convention (see ORIGINAL_CLASS_RE), which avoids relying
    on any one theme's class names."""
    # The `i` flag matters: CSS substring matching is case-sensitive by default,
    # so a bare [class*=product-tile] misses J.Crew's "ProductTileRevamped__…".
    tiles = soup.select(
        '[class*="product-tile" i], [class*="producttile" i], '
        '[class*="product-card" i], [class*="productcard" i], '
        '[class*="product-item" i], li[class*="product" i]')
    items, seen = [], set()
    for tile in tiles:
        price_nodes = tile.select('[class*="price" i], s, del')
        node_ids = {id(n) for n in price_nodes}
        wrappers = {id(node) for node in price_nodes
                    if any(id(d) in node_ids for d in node.find_all(True))}

        currents, originals = [], []
        for node in price_nodes:
            # Skip a node only if it wraps other price nodes. Checking for any
            # child element instead would drop J.Crew's markup, where the amount
            # sits in a plain <span> inside the price div.
            if id(node) in wrappers:
                continue
            amount = _money(node.get_text(" ", strip=True))
            if amount is None:
                continue
            classes = " ".join(node.get("class") or [])
            parent_classes = " ".join(node.parent.get("class") or []) if node.parent else ""
            is_original = (node.name in ("s", "del")
                           or ORIGINAL_CLASS_RE.search(classes + " " + parent_classes))
            (originals if is_original else currents).append(amount)

        if not currents or not originals:
            continue
        price, original = min(currents), max(originals)
        if original <= price:
            continue

        name = None
        for sel in ('[class*="product-name" i]', '[class*="product-title" i]',
                    '[class*="productname" i]', '[class*="tile-name" i]',
                    '[class*="name" i] a', '[class*="name" i]', '[class*="title" i]'):
            node = tile.select_one(sel)
            if node:
                name = _clean_name(node.get_text(" ", strip=True))
                if name:
                    break
        if not name:
            link = tile.find("a", href=True)
            name = _clean_name(link.get_text(" ", strip=True)) if link else None
        if not name:
            continue  # no usable name -> can't report it usefully

        # Themes nest tile wrappers inside tile wrappers, so the same product
        # matches several times. Dedupe on the extracted values rather than the
        # product link, since the nested copies don't always carry one.
        key = (name, price, original)
        if key in seen:
            continue
        seen.add(key)
        items.append({"name": name, "price": price, "original": original})
    return items


def summarize_items(items, label="Sale", limit=5):
    """Render extracted markdowns the way the per-chain plugins do, so the
    Streamlit and API callers keep getting one display string."""
    if not items:
        return None
    best = {}  # name -> (price, original, pct), keeping the deepest discount
    for it in items:
        pct = round((1 - it["price"] / it["original"]) * 100)
        if it["name"] not in best or pct > best[it["name"]][2]:
            best[it["name"]] = (it["price"], it["original"], pct)

    max_pct = max(v[2] for v in best.values())
    min_price = min(v[0] for v in best.values())
    top = sorted(best.items(), key=lambda kv: kv[1][2], reverse=True)[:limit]

    lines = [f"{label}: {len(best)} items marked down, up to {max_pct}% off, "
             f"from ${min_price:.2f}. Top markdowns:"]
    for name, (cur, orig, pct) in top:
        lines.append(f"- {name} — ${cur:.2f} (was ${orig:.2f}, {pct}% off)")
    return "\n".join(lines)
