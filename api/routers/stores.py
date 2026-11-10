"""Store locations with coordinates, for the map."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from api import queries
from api.deps import PageDep, SessionDep
from api.schemas import ErrorResponse, Pagination, StoresResponse
from api.serializers import store_with_chain

router = APIRouter(tags=["stores"])


@router.get("/stores", response_model=StoresResponse,
            responses={400: {"model": ErrorResponse}},
            summary="Store locations for plotting")
def stores(
    session: SessionDep,
    page: PageDep,
    city: Annotated[str | None, Query(
        description="City name. Omit for every location on record.")] = None,
    state: Annotated[str | None, Query(
        description="Two-letter state, to separate same-named cities.")] = None,
    zip: Annotated[str | None, Query(
        alias="zip", description="Postal code, to narrow further.")] = None,
    chain_key: Annotated[str | None, Query(
        description="Only this retailer's locations.")] = None,
    with_coordinates: Annotated[bool, Query(
        description="Only locations that have a position. Coordinates are "
                    "cleared once Google's caching window is up, and a pin "
                    "needs somewhere to go, so this defaults on.")] = True,
) -> StoresResponse:
    """Known locations, each carrying the chain it belongs to.

    The chain travels with every row because that is what the map needs to
    colour or group its pins, and fetching it per store from another endpoint
    would be one request per marker.
    """
    stmt = queries.stores_query(city=city, state=state, postal_code=zip,
                                key=chain_key,
                                with_coordinates=with_coordinates)
    total = queries.count_of(session, stmt)
    rows = session.execute(stmt.offset(page.offset).limit(page.limit)).all()

    return StoresResponse(
        items=[store_with_chain(store, chain) for store, chain in rows],
        pagination=Pagination.build(page.limit, page.offset, total),
    )
