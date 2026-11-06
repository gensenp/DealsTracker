"""Chain-level orchestration: what to scrape, what to read from the database.

A search used to scrape every store live, which meant minutes of waiting and a
different answer each run. Here the work is done per *chain* instead -- all six
Gap locations in a city share one scrape of gap.com -- and a chain is only
scraped when its saved deals have gone stale.

Three caches do the heavy lifting:

* deals, fresh for DEALS_FRESH_FOR (6h)
* the discovered sale URL, trusted for SALE_URL_TRUSTED_FOR (7d), so a repeat
  scrape skips find_deals_page entirely
* dead ends, remembered for NEGATIVE_RECHECK_AFTER (7d) -- Primark has no sale
  page and T.J. Maxx sits behind a bot wall, and neither is worth re-checking
  on every search. We record the status; we never try to get around it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

import DealsTracker as dt
import db
from db import (
    Chain, PriceSnapshot, Product, Promotion, ScrapeRun, Store,
    STAGE_CACHED, STAGE_CUSTOM_API,
    STATUS_BLOCKED, STATUS_ERROR, STATUS_NO_SALE_PAGE, STATUS_OK,
    chain_key, deals_are_fresh, normalize_name, registrable_domain,
    sale_url_is_trusted, session_scope, should_skip_chain, utcnow,
)
from deals import CoercionStats, coerce_deals_with_stats, empty_deals, has_deals

log = logging.getLogger(__name__)


@dataclass
class ChainResult:
    """What the UI needs to render one retailer."""
    key: str
    name: str
    deals: dict = field(default_factory=empty_deals)
    stores: list[dict] = field(default_factory=list)
    sale_url: str | None = None
    status: str = STATUS_OK
    stage: str | None = None
    last_scraped_at: datetime | None = None
    from_cache: bool = False
    duration_s: float | None = None
    error: str | None = None

    @property
    def skipped(self) -> bool:
        """A known dead end we deliberately didn't re-check."""
        return self.status in db.NEGATIVE_STATUSES and not self.from_cache


@dataclass
class SearchResult:
    location: str
    chains: list[ChainResult]
    total_seconds: float
    scraped: int = 0   # chains we hit the network for
    cached: int = 0    # chains served from the database
    skipped: int = 0   # known dead ends inside their recheck window


# --- Grouping ---------------------------------------------------------------
def group_by_chain(stores: list[dict]) -> dict[str, list[dict]]:
    """Places results keyed by chain, so each retailer is scraped once.

    Every H&M in a city shows the same online sale, so scraping per location
    would repeat identical work and produce identical rows."""
    grouped: dict[str, list[dict]] = {}
    for store in stores:
        website = store.get("website") or ""
        key = chain_key(website)
        if not key:
            continue
        grouped.setdefault(key, []).append(store)
    return grouped


def _chain_display_name(stores: list[dict]) -> str:
    """The name shoppers know. Locations of one chain usually share a name;
    when they differ ("Gap", "Gap Factory") the most common one wins."""
    counts: dict[str, int] = {}
    for store in stores:
        name = (store.get("name") or "").strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


# --- Persistence helpers ----------------------------------------------------
def get_or_create_chain(session, key: str, domain: str, name: str) -> Chain:
    chain = session.scalar(select(Chain).where(Chain.key == key))
    if chain is None:
        chain = Chain(key=key, domain=domain, name=name)
        session.add(chain)
        session.flush()
    elif name and chain.name != name:
        chain.name = name
    return chain


def upsert_stores(session, chain: Chain, stores: list[dict]) -> None:
    """Record each physical location, refreshing the Places details we hold.

    Google allows caching place_id indefinitely but not the rest, so every
    sighting rewrites the details and stamps last_refreshed_at."""
    now = utcnow()
    for raw in stores:
        place_id = raw.get("place_id")
        if not place_id:
            continue
        store = session.scalar(select(Store).where(Store.place_id == place_id))
        if store is None:
            store = Store(place_id=place_id)
            session.add(store)
        store.name = raw.get("name") or ""
        store.address = raw.get("address")
        store.lat = raw.get("lat")
        store.lng = raw.get("lng")
        store.website = raw.get("website")
        store.chain_id = chain.id
        store.last_refreshed_at = now


def clear_stale_place_details(session) -> int:
    """Drop Places details we've held longer than Google's terms allow.

    place_id may be kept indefinitely; name, address and coordinates may not,
    so a location we haven't seen in 30 days keeps its row and loses its
    details until a later search refreshes them."""
    cutoff = utcnow() - db.PLACES_DETAILS_REFRESH_AFTER
    stale = session.scalars(
        select(Store).where(Store.last_refreshed_at.is_not(None),
                            Store.last_refreshed_at < cutoff)).all()
    for store in stale:
        store.name = ""
        store.address = None
        store.lat = None
        store.lng = None
        store.last_refreshed_at = None
    return len(stale)


def save_deals(session, chain: Chain, deals: dict, run: ScrapeRun) -> None:
    """Persist a scrape: upsert products, append price snapshots, log promos.

    Snapshots are append-only, so re-scraping extends a product's price history
    rather than overwriting what came before."""
    now = utcnow()
    for item in deals.get("items") or []:
        key = normalize_name(item["name"])
        if not key:
            continue
        product = session.scalar(
            select(Product).where(Product.chain_id == chain.id,
                                  Product.normalized_name == key))
        if product is None:
            product = Product(chain_id=chain.id, name=item["name"],
                              normalized_name=key,
                              product_url=item.get("product_url"),
                              first_seen_at=now, last_seen_at=now)
            session.add(product)
            session.flush()
        else:
            product.last_seen_at = now
            if item.get("product_url"):
                product.product_url = item["product_url"]
        session.add(PriceSnapshot(product_id=product.id,
                                  sale_price=item.get("sale_price"),
                                  original_price=item.get("original_price"),
                                  scraped_at=now))

    for text in deals.get("promotions") or []:
        session.add(Promotion(chain_id=chain.id, scrape_run_id=run.id,
                              text=text, scraped_at=now))


def load_cached_deals(session, chain: Chain) -> dict:
    """The most recent scrape's deals, rebuilt from the database.

    Takes each product's latest snapshot, and the promotions from the run that
    produced it, so a cached result matches what the last scrape saw."""
    last = db.as_utc(chain.last_scraped_at)
    if last is None:
        return empty_deals()

    items = []
    products = session.scalars(
        select(Product).where(Product.chain_id == chain.id)).all()
    for product in products:
        snapshot = session.scalar(
            select(PriceSnapshot)
            .where(PriceSnapshot.product_id == product.id)
            .order_by(PriceSnapshot.scraped_at.desc())
            .limit(1))
        # Only products seen in the latest scrape, not everything ever listed.
        if snapshot is None or db.as_utc(snapshot.scraped_at) < last:
            continue
        items.append({"name": product.name,
                      "sale_price": snapshot.sale_price,
                      "original_price": snapshot.original_price,
                      "product_url": product.product_url})

    promos = session.scalars(
        select(Promotion)
        .where(Promotion.chain_id == chain.id, Promotion.scraped_at >= last)
        .order_by(Promotion.scraped_at.desc())).all()

    return {"items": items, "promotions": [p.text for p in promos]}


# --- Scraping ---------------------------------------------------------------
def _classify_failure(sale_url: str | None, text_found: bool) -> str:
    """Why a chain produced nothing: no sale page at all, or a wall."""
    if sale_url is None:
        return STATUS_NO_SALE_PAGE   # discovery ran and found no such link
    return STATUS_BLOCKED if not text_found else STATUS_NO_SALE_PAGE


def scrape_chain(session, chain: Chain, stores: list[dict]) -> ChainResult:
    """Scrape one chain and write everything the run produced.

    A scrape_runs row is written whatever happens, so a failure is as visible
    as a success."""
    started = time.monotonic()
    run = ScrapeRun(chain_id=chain.id, started_at=utcnow())
    session.add(run)
    session.flush()

    result = ChainResult(key=chain.key, name=chain.name or chain.key,
                         stores=stores)
    website = stores[0].get("website") if stores else None
    deals, stats = empty_deals(), CoercionStats()
    stage = None

    try:
        scraper = dt.custom_scraper_for(website or "")
        if scraper:
            stage = STAGE_CUSTOM_API
            deals, stats = coerce_deals_with_stats(scraper({"website": website}))
            sale_url = chain.sale_url
            text_found = True
        else:
            # Reuse a trusted sale URL; otherwise re-run discovery.
            if sale_url_is_trusted(chain):
                sale_url, stage = chain.sale_url, STAGE_CACHED
            else:
                sale_url, stage = dt.discover_deals_page(website or "")

            if sale_url:
                text = dt.page_text(sale_url)
                text_found = bool(text)
                if text:
                    deals, stats = coerce_deals_with_stats(
                        dt.extract_deals(text, sale_url))
            else:
                text_found = False

        if has_deals(deals):
            chain.status = STATUS_OK
            chain.sale_url = sale_url
            chain.discovery_stage = stage
            chain.sale_url_verified_at = utcnow()
            result.status = STATUS_OK
        else:
            # A cached URL that yielded nothing may simply be out of date, so
            # drop it and let the next run rediscover rather than caching the
            # failure against a URL we no longer trust.
            if stage == STAGE_CACHED:
                chain.sale_url = None
                chain.sale_url_verified_at = None
                result.status = STATUS_ERROR
                chain.status = STATUS_ERROR
            else:
                status = _classify_failure(sale_url, text_found)
                chain.status = result.status = status
            chain.status_checked_at = utcnow()

        chain.last_scraped_at = utcnow()
        save_deals(session, chain, deals, run)

    except Exception as exc:  # a chain failing must not end the search
        log.exception("scrape failed for %s", chain.key)
        chain.status = result.status = STATUS_ERROR
        chain.status_checked_at = utcnow()
        result.error = f"{type(exc).__name__}: {exc}"
        run.error_message = result.error[:1000]

    duration = time.monotonic() - started
    run.duration_seconds = round(duration, 2)
    run.stage_used = stage
    run.status = result.status
    run.deals_found = len(deals.get("items") or [])
    run.promotions_found = len(deals.get("promotions") or [])
    run.items_rejected = stats.items_rejected
    run.duplicates_dropped = stats.duplicates_dropped
    run.originals_corrected = stats.originals_corrected

    result.deals = deals
    result.sale_url = chain.sale_url
    result.stage = stage
    result.duration_s = round(duration, 2)
    result.last_scraped_at = chain.last_scraped_at
    return result


# --- Entry point ------------------------------------------------------------
def search_deals(city, state=None, postal_code=None, force_keys=None,
                 on_progress=None) -> SearchResult:
    """Find stores, then serve each chain's deals from cache or scrape them.

    `force_keys` re-scrapes those chains regardless of freshness, which is what
    the per-chain Refresh button sends.
    """
    started = time.monotonic()
    force_keys = set(force_keys or ())
    db.init_db()

    stores = dt.find_clothing_stores(city, state, postal_code)
    grouped = group_by_chain(stores)
    results: list[ChainResult] = []
    scraped = cached = skipped = 0

    with session_scope() as session:
        for key, chain_stores in grouped.items():
            name = _chain_display_name(chain_stores)
            if on_progress:
                on_progress(name or key, len(results) + 1, len(grouped))

            chain = get_or_create_chain(
                session, key, registrable_domain(key), name)
            upsert_stores(session, chain, chain_stores)
            forced = key in force_keys
            # A known dead end is decided by its recheck window alone. Deal
            # freshness doesn't apply: there are no deals to be fresh, and
            # consulting it would keep a chain "cached" past the point where we
            # meant to look at it again.
            if not forced and chain.status in db.NEGATIVE_STATUSES:
                if should_skip_chain(chain):
                    results.append(ChainResult(
                        key=key, name=name or key, stores=chain_stores,
                        status=chain.status, sale_url=chain.sale_url,
                        last_scraped_at=chain.last_scraped_at))
                    skipped += 1
                    continue
            elif not forced and deals_are_fresh(chain):
                results.append(ChainResult(
                    key=key, name=name or key, stores=chain_stores,
                    deals=load_cached_deals(session, chain),
                    status=chain.status, sale_url=chain.sale_url,
                    stage=chain.discovery_stage, from_cache=True,
                    last_scraped_at=chain.last_scraped_at))
                cached += 1
                continue

            results.append(scrape_chain(session, chain, chain_stores))
            scraped += 1

        clear_stale_place_details(session)

    # Chains with deals first, then by name -- empty results sink.
    results.sort(key=lambda r: (not has_deals(r.deals), r.name.lower()))
    return SearchResult(
        location=dt.build_location(city, state, postal_code),
        chains=results, total_seconds=round(time.monotonic() - started, 2),
        scraped=scraped, cached=cached, skipped=skipped)
