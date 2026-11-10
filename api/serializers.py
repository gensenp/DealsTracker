"""Turning database rows into response models.

Shared so the city listing and the single-chain endpoint describe a chain the
same way, and so the freshness fields come from db's own rules rather than
being recomputed here. If DEALS_FRESH_FOR or NEGATIVE_RECHECK_AFTER changes,
the API's answers change with it -- there is no second copy of those rules.

Formatting stays out. ui.py's humanize_age ("4 minutes ago") and money ("$10.00")
are for a Streamlit page; this hands back ISO-8601 timestamps and plain numbers
and lets the client decide how to read them to a shopper.
"""
from __future__ import annotations

import db
from api import queries
from api.schemas import (
    ChainSummary, ChainWithDeals, Deal, StoreSummary, StoreWithChain,
)


def recheck_due_at(chain: db.Chain):
    """When a dead end comes up for another look, or None if it isn't one.

    Keyed on status_checked_at, which is what db.should_skip_chain actually
    tests -- ui.py reads the same idea off last_scraped_at, which is close
    enough for a caption but not for a timestamp a client might count down to.
    """
    if chain.status not in db.NEGATIVE_STATUSES:
        return None
    checked = db.as_utc(chain.status_checked_at)
    if checked is None:
        return None
    return checked + db.NEGATIVE_RECHECK_AFTER


def chain_summary(session, chain: db.Chain, store_count: int | None = None
                  ) -> ChainSummary:
    """A chain and the state of what we know about it."""
    return ChainSummary(
        key=chain.key,
        name=chain.name,
        domain=chain.domain,
        status=chain.status,
        sale_url=chain.sale_url,
        stage=chain.discovery_stage,
        last_scraped_at=db.as_utc(chain.last_scraped_at),
        is_fresh=db.deals_are_fresh(chain),
        recheck_due_at=recheck_due_at(chain),
        store_count=(queries.store_count(session, chain)
                     if store_count is None else store_count),
    )


def store_summary(store: db.Store) -> StoreSummary:
    return StoreSummary(place_id=store.place_id, name=store.name,
                        address=store.address, lat=store.lat, lng=store.lng,
                        website=store.website)


def store_with_chain(store: db.Store, chain: db.Chain) -> StoreWithChain:
    return StoreWithChain(place_id=store.place_id, name=store.name,
                          address=store.address, lat=store.lat, lng=store.lng,
                          website=store.website, chain_key=chain.key,
                          chain_name=chain.name, chain_status=chain.status)


def chain_with_deals(session, chain: db.Chain, deals_per_chain: int
                     ) -> ChainWithDeals:
    """A chain plus a sample of its deals and its locations."""
    items, total = queries.latest_deals(session, chain,
                                        limit=deals_per_chain)
    stores = queries.stores_for_chain(session, chain, limit=25)
    summary = chain_summary(session, chain,
                            store_count=queries.store_count(session, chain))
    return ChainWithDeals(
        **summary.model_dump(),
        stores=[store_summary(s) for s in stores],
        promotions=queries.latest_promotions(session, chain),
        deals=[Deal(**item) for item in items],
        deal_count=total,
    )
