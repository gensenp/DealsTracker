"""One chain's current deals, filtered and sorted."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from api import queries
from api.deps import PageDep, SessionDep
from api.schemas import (
    ChainDealsResponse, Deal, DealSort, ErrorResponse, Pagination,
)
from api.serializers import chain_summary

router = APIRouter(tags=["chains"])


@router.get("/chains/{chain_key}/deals", response_model=ChainDealsResponse,
            responses={404: {"model": ErrorResponse},
                       400: {"model": ErrorResponse}},
            summary="One chain's current deals")
def chain_deals(
    session: SessionDep,
    page: PageDep,
    chain_key: Annotated[str, Path(
        description="The chain's key, normally its registrable domain "
                    "(hm.com). A full host (www.hm.com) resolves to the same "
                    "chain.")],
    min_discount: Annotated[float, Query(
        ge=0, le=100,
        description="Only items at least this many percent off. Items with no "
                    "original price have no discount to compare and drop out "
                    "once this is above 0.")] = 0,
    sort: Annotated[DealSort, Query(
        description="Ordering. `discount` is deepest markdown first.",
    )] = DealSort.discount,
) -> ChainDealsResponse:
    """Everything this chain had on sale as of its last scrape.

    Filtering and ordering happen in the database, not after the fact, so
    `sort=discount&limit=20` really is the twenty deepest markdowns the chain
    has rather than whichever twenty rows arrived first.

    A chain that exists but has no deals -- blocked, no sale page, or never
    scraped -- returns 200 with an empty list and its `status` saying why. That
    is different from a chain we have never heard of, which is a 404.
    """
    chain = queries.resolve_chain(session, chain_key)
    if chain is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found",
                    "message": f"No chain with key {chain_key!r}.",
                    "detail": "Chains appear once a search has found a shop "
                              "of theirs. Try GET /cities/{city}/deals."})

    items, total = queries.latest_deals(
        session, chain, min_discount=min_discount, sort=sort.value,
        limit=page.limit, offset=page.offset)

    return ChainDealsResponse(
        chain=chain_summary(session, chain),
        promotions=queries.latest_promotions(session, chain),
        items=[Deal(**item) for item in items],
        pagination=Pagination.build(page.limit, page.offset, total),
    )
