"""Store locations with coordinates, for the map."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

import geo
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
    lat: Annotated[float | None, Query(
        ge=-90, le=90,
        description="Latitude to measure from. Give with `lng` and "
                    "`radius_miles`.")] = None,
    lng: Annotated[float | None, Query(
        ge=-180, le=180,
        description="Longitude to measure from.")] = None,
    radius_miles: Annotated[float | None, Query(
        gt=0, le=geo.MAX_RADIUS_MILES,
        description=f"Only locations within this many miles of `lat`/`lng`, "
                    f"nearest first, each carrying `distance_miles`. "
                    f"Up to {geo.MAX_RADIUS_MILES:.0f}.")] = None,
    with_coordinates: Annotated[bool, Query(
        description="Only locations that have a position. Coordinates are "
                    "cleared once Google's caching window is up, and a pin "
                    "needs somewhere to go, so this defaults on.")] = True,
) -> StoresResponse:
    """Known locations, each carrying the chain it belongs to.

    The chain travels with every row because that is what the map needs to
    colour or group its pins, and fetching it per store from another endpoint
    would be one request per marker.

    Give `lat`, `lng` and `radius_miles` together for a "near me" search. The
    results come back nearest first with `distance_miles` on each, and the
    ordering is worked out across every store in range before the page is cut,
    so `radius_miles=5&limit=10` really is the ten nearest within five miles.
    Distance is straight-line, not driving distance.
    """
    radius_parts = {"lat": lat, "lng": lng, "radius_miles": radius_miles}
    given = [name for name, value in radius_parts.items() if value is not None]
    if given and len(given) != 3:
        # Silently ignoring a lone `lat` would answer a different question from
        # the one that was asked, and look like the radius filter had no effect.
        missing = sorted(set(radius_parts) - set(given))
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request",
                    "message": "A radius search needs lat, lng and "
                               "radius_miles together.",
                    "detail": [{"field": f"query.{name}", "problem": "required "
                                "when the others are given"} for name in missing]})

    if given:
        measured = queries.stores_within_radius(
            session, lat, lng, radius_miles, city=city, state=state,
            postal_code=zip, key=chain_key)
        total = len(measured)
        window = measured[page.offset:page.offset + page.limit]
        items = [store_with_chain(store, chain, distance)
                 for store, chain, distance in window]
    else:
        stmt = queries.stores_query(city=city, state=state, postal_code=zip,
                                    key=chain_key,
                                    with_coordinates=with_coordinates)
        total = queries.count_of(session, stmt)
        rows = session.execute(
            stmt.offset(page.offset).limit(page.limit)).all()
        items = [store_with_chain(store, chain) for store, chain in rows]

    return StoresResponse(
        items=items,
        pagination=Pagination.build(page.limit, page.offset, total),
    )
