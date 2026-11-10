"""Chains and their deals for one city."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from DealsTracker import build_location
from api import queries
from api.deps import MAX_DEALS_PER_CHAIN, PageDep, SessionDep
from api.schemas import CityDealsResponse, ErrorResponse, Pagination
from api.serializers import chain_with_deals

router = APIRouter(tags=["cities"])


@router.get("/cities/{city}/deals", response_model=CityDealsResponse,
            responses={400: {"model": ErrorResponse}},
            summary="Chains with deals in a city")
def city_deals(
    session: SessionDep,
    page: PageDep,
    city: Annotated[str, Path(description="City name, e.g. Boston.")],
    state: Annotated[str | None, Query(
        description="Two-letter state, to separate same-named cities.")] = None,
    zip: Annotated[str | None, Query(
        alias="zip", description="Postal code, to narrow further.")] = None,
    deals_per_chain: Annotated[int, Query(
        ge=0, le=MAX_DEALS_PER_CHAIN,
        description="Deals to include per chain. The full, sortable list "
                    "lives at /chains/{chain_key}/deals.")] = 12,
    include_empty: Annotated[bool, Query(
        description="Keep chains with no deals -- those blocked, without a "
                    "sale page, or not yet scraped. Off hides them.")] = True,
) -> CityDealsResponse:
    """Retailers with a known shop in this city, and what each has on sale.

    Served entirely from saved data. A city nobody has searched yet comes back
    with an empty list and `total: 0` rather than a 404: the city is not a
    resource that can be missing, and "we hold nothing for Boston" is a real,
    correct answer that a frontend should render as an empty state. Scraping it
    on demand is phase 2's job.

    Paging is over chains, with each chain's deals capped by `deals_per_chain`.
    A city can hold dozens of retailers with up to 200 products apiece, and
    serving all of that in one response would make the endpoint unusable on a
    phone.
    """
    if not city or not city.strip():
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": "City must not be empty."})

    stmt = queries.chains_in_location(city, state, zip,
                                      only_with_deals=not include_empty)
    total = queries.count_of(session, stmt)
    chains = list(session.scalars(
        stmt.offset(page.offset).limit(page.limit)).all())

    items = [chain_with_deals(session, chain, deals_per_chain)
             for chain in chains]
    # Chains with something to show first, then by name -- the order the
    # Streamlit page already sorts into, so both frontends agree.
    items.sort(key=lambda c: (not (c.deals or c.promotions),
                              (c.name or c.key).lower()))

    return CityDealsResponse(
        city=city.strip(),
        location=build_location(city, state, zip),
        items=items,
        pagination=Pagination.build(page.limit, page.offset, total),
    )
