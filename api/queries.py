"""The read-only SQL the API runs.

Kept apart from `pipeline.py` on purpose. pipeline orchestrates *writing*: it
decides what to scrape, calls out to the network and records what came back.
Nothing here touches the network, and nothing here writes -- these are the
queries that answer "what do we already know?".

The one that matters is `latest_deals`: a chain's products as of its most
recent scrape. `pipeline.load_cached_deals` used to answer that with one query
per product, which is fine for a single chain on a Streamlit page but cannot
support `?sort=discount&limit=20` -- ordering by discount means ranking every
product the chain has, not the twenty that happen to come back first. So the
ranking, filtering and limiting all happen in SQL, and load_cached_deals now
calls the same function.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from db import Chain, PriceSnapshot, Product, Promotion, Store, chain_key

# Sort keys accepted by /chains/{chain_key}/deals.
SORT_DISCOUNT = "discount"
SORT_PRICE_ASC = "price_asc"
SORT_PRICE_DESC = "price_desc"
SORT_NEWEST = "newest"
SORT_NAME = "name"
SORTS = (SORT_DISCOUNT, SORT_PRICE_ASC, SORT_PRICE_DESC, SORT_NEWEST, SORT_NAME)

# LIKE reads these as wildcards, so a city typed with one in it would quietly
# match far more than it should.
_LIKE_ESCAPE = "\\"


def _like_literal(value: str) -> str:
    """User text as a LIKE fragment, wildcards defanged."""
    for char in (_LIKE_ESCAPE, "%", "_"):
        value = value.replace(char, _LIKE_ESCAPE + char)
    return value


def address_filters(city: str | None = None, state: str | None = None,
                    postal_code: str | None = None) -> list:
    """Clauses matching a location against Store.address.

    Stores carry Google's `formattedAddress` ("123 Newbury St, Boston, MA
    02116, USA") and no parsed city column, so this matches inside that string.
    It is approximate by nature: "Boston" also matches "South Boston", and a
    city whose name turns up in a street name can match the street instead.
    Narrowing with state or ZIP settles nearly all of it.

    An exact version wants parsed city/state/postal_code columns on Store,
    filled in when a scrape records a location -- a schema change and a
    write-path change, so not part of a read-only phase.
    """
    clauses = []
    if city and city.strip():
        clauses.append(Store.address.ilike(
            f"%{_like_literal(city.strip())}%", escape=_LIKE_ESCAPE))
    if state and state.strip():
        # In a formatted address the state sits between city and ZIP, so
        # ", MA " pins it down where "%MA%" would also hit "Marshalls" and
        # "Massachusetts Ave". The second form catches an address that ends
        # at the state with no ZIP after it.
        token = _like_literal(state.strip())
        clauses.append(or_(
            Store.address.ilike(f"%, {token} %", escape=_LIKE_ESCAPE),
            Store.address.ilike(f"%, {token}", escape=_LIKE_ESCAPE)))
    if postal_code and postal_code.strip():
        clauses.append(Store.address.ilike(
            f"%{_like_literal(postal_code.strip())}%", escape=_LIKE_ESCAPE))
    return clauses


def has_something_to_show():
    """Clause: this chain's last scrape produced a product or a promotion.

    Correlated on Chain, so it can narrow a chain listing before paging rather
    than after. Filtering the page afterwards would leave `total` counting
    chains that were then dropped, and a client paging through such a list
    would see short pages and a total it can never reach.
    """
    products = (
        select(1).select_from(Product)
        .join(PriceSnapshot, PriceSnapshot.product_id == Product.id)
        .where(Product.chain_id == Chain.id,
               PriceSnapshot.scraped_at >= Chain.last_scraped_at)
        .exists())
    promotions = (
        select(1).select_from(Promotion)
        .where(Promotion.chain_id == Chain.id,
               Promotion.scraped_at >= Chain.last_scraped_at)
        .exists())
    return or_(products, promotions)


def chains_in_location(city: str | None = None, state: str | None = None,
                       postal_code: str | None = None,
                       only_with_deals: bool = False) -> Select:
    """Chains with at least one known store in this location.

    Returns a Select rather than rows so the caller can count it for pagination
    and then page it without the filter being written out twice.
    """
    stmt = select(Chain).join(Store, Store.chain_id == Chain.id)
    for clause in address_filters(city, state, postal_code):
        stmt = stmt.where(clause)
    if only_with_deals:
        stmt = stmt.where(Chain.last_scraped_at.is_not(None),
                          has_something_to_show())
    # One row per chain however many of its shops matched.
    return stmt.distinct().order_by(Chain.name, Chain.key)


def resolve_chain(session: Session, key: str) -> Chain | None:
    """A chain by key, accepting any form of the host that names it.

    `/chains/www.hm.com/deals` and `/chains/hm.com/deals` mean the same chain:
    db.chain_key is what grouped the stores in the first place, so it is what
    normalizes the path parameter too. An already-canonical key is looked up as
    given, which is why the exact match is tried first.
    """
    raw = (key or "").strip()
    if not raw:
        return None
    chain = session.scalar(select(Chain).where(Chain.key == raw))
    if chain is not None:
        return chain
    normalized = chain_key(raw)
    if normalized and normalized != raw:
        return session.scalar(select(Chain).where(Chain.key == normalized))
    return None


def _latest_snapshots():
    """Subquery holding each product's most recent price snapshot.

    Snapshots are append-only, so a product has one row per scrape it appeared
    in and "the price now" is the newest of them. A window function picks that
    row per product in one pass; the id tie-break keeps the choice deterministic
    when two snapshots share a timestamp, which they do whenever a single scrape
    writes them all.
    """
    ranked = select(
        PriceSnapshot.product_id.label("product_id"),
        PriceSnapshot.sale_price.label("sale_price"),
        PriceSnapshot.original_price.label("original_price"),
        PriceSnapshot.scraped_at.label("scraped_at"),
        PriceSnapshot.discount_pct.label("discount_pct"),
        func.row_number().over(
            partition_by=PriceSnapshot.product_id,
            order_by=(PriceSnapshot.scraped_at.desc(), PriceSnapshot.id.desc()),
        ).label("rank"),
    ).subquery()
    return select(ranked).where(ranked.c.rank == 1).subquery()


def latest_deals(session: Session, chain: Chain,
                 min_discount: float | None = None,
                 sort: str = SORT_DISCOUNT, limit: int | None = None,
                 offset: int = 0) -> tuple[list[dict], int]:
    """A chain's products as of its last scrape, and the total before paging.

    Only products seen in the most recent scrape, not everything the chain has
    ever listed -- an item pulled from the sale page should stop being a deal.
    The cutoff is `chain.last_scraped_at` exactly as the database handed it
    over, not a timezone-normalized copy: SQLite returns these naive and
    PostgreSQL returns them aware, and re-binding the value we were given keeps
    the comparison in whichever form that backend stores.
    """
    if chain.last_scraped_at is None:
        return [], 0

    snaps = _latest_snapshots()
    stmt = (
        select(Product.name, Product.product_url, Product.first_seen_at,
               Product.last_seen_at, snaps.c.sale_price, snaps.c.original_price,
               snaps.c.discount_pct, snaps.c.scraped_at)
        .join(snaps, snaps.c.product_id == Product.id)
        .where(Product.chain_id == chain.id,
               snaps.c.scraped_at >= chain.last_scraped_at)
    )

    if min_discount is not None and min_discount > 0:
        # A NULL discount never satisfies this, so items with no original price
        # drop out of a filtered search rather than counting as 0% off.
        stmt = stmt.where(snaps.c.discount_pct >= min_discount)

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    # nulls_last() is spelled out because the backends disagree by default:
    # PostgreSQL sorts NULLs first on DESC, SQLite sorts them last. An item with
    # no original price belongs at the bottom of a discount ranking on both.
    orderings = {
        SORT_DISCOUNT: (snaps.c.discount_pct.desc().nulls_last(), Product.name),
        SORT_PRICE_ASC: (snaps.c.sale_price.asc().nulls_last(), Product.name),
        SORT_PRICE_DESC: (snaps.c.sale_price.desc().nulls_last(), Product.name),
        SORT_NEWEST: (Product.first_seen_at.desc(), Product.name),
        SORT_NAME: (Product.name.asc(),),
    }
    stmt = stmt.order_by(*orderings.get(sort, orderings[SORT_DISCOUNT]))

    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)

    items = [{"name": row.name,
              "sale_price": row.sale_price,
              "original_price": row.original_price,
              "discount_pct": row.discount_pct,
              "product_url": row.product_url,
              "first_seen_at": row.first_seen_at,
              "last_seen_at": row.last_seen_at,
              "scraped_at": row.scraped_at}
             for row in session.execute(stmt).all()]
    return items, total


def latest_promotions(session: Session, chain: Chain) -> list[str]:
    """Storewide offers from the chain's most recent scrape."""
    if chain.last_scraped_at is None:
        return []
    rows = session.scalars(
        select(Promotion)
        .where(Promotion.chain_id == chain.id,
               Promotion.scraped_at >= chain.last_scraped_at)
        .order_by(Promotion.scraped_at.desc(), Promotion.id.desc())).all()
    return [row.text for row in rows]


def stores_query(city: str | None = None, state: str | None = None,
                 postal_code: str | None = None, key: str | None = None,
                 with_coordinates: bool = True) -> Select:
    """Store locations, joined to the chain that owns each.

    Coordinates are nullable in the schema and get cleared by
    `clear_stale_place_details` once Google's caching window is up, so by
    default rows without them are left out: this endpoint exists to put pins on
    a map, and a pin needs a position. Pass with_coordinates=False for the lot.
    """
    stmt = select(Store, Chain).join(Chain, Store.chain_id == Chain.id)
    for clause in address_filters(city, state, postal_code):
        stmt = stmt.where(clause)
    if key:
        stmt = stmt.where(Chain.key == key)
    if with_coordinates:
        stmt = stmt.where(Store.lat.is_not(None), Store.lng.is_not(None))
    return stmt.order_by(Chain.key, Store.name, Store.id)


def count_of(session: Session, stmt: Select) -> int:
    """How many rows a Select would return, for a pagination envelope."""
    return session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())) or 0


def store_count(session: Session, chain: Chain) -> int:
    """How many locations of this chain we know about."""
    return session.scalar(
        select(func.count()).select_from(Store)
        .where(Store.chain_id == chain.id)) or 0


def stores_for_chain(session: Session, chain: Chain, limit: int) -> list[Store]:
    """A few of a chain's locations, to show under its deals."""
    return list(session.scalars(
        select(Store).where(Store.chain_id == chain.id)
        .order_by(Store.name, Store.id).limit(limit)).all())


def last_scrape_time(session: Session) -> datetime | None:
    """The most recent scrape across all chains, for /health."""
    return session.scalar(select(func.max(Chain.last_scraped_at)))
