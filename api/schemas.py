"""Request and response shapes.

These are what makes /docs worth reading, so the field descriptions are part of
the deliverable rather than decoration -- a shopper-facing frontend needs to
know that `discount_pct` can be null and why.

The status enum is built from db's constants instead of being retyped, so a new
status cannot be added to the scraper and quietly go missing from the API's
documentation.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

import db
from api import queries


class ChainStatus(str, Enum):
    """Why a chain does or doesn't have deals to show.

    Mirrors db.STATUS_*. `error` is included deliberately: a transient failure
    is not the same as a shop with no sale page, and a frontend that collapses
    the two tells shoppers a retailer has nothing on when the truth is we could
    not read it.
    """
    ok = db.STATUS_OK
    no_sale_page = db.STATUS_NO_SALE_PAGE
    blocked = db.STATUS_BLOCKED
    error = db.STATUS_ERROR


class DealSort(str, Enum):
    """Ordering for a chain's deals."""
    discount = queries.SORT_DISCOUNT
    price_asc = queries.SORT_PRICE_ASC
    price_desc = queries.SORT_PRICE_DESC
    newest = queries.SORT_NEWEST
    name = queries.SORT_NAME


class Pagination(BaseModel):
    """The same envelope on every list, so a client writes paging once."""
    limit: int = Field(description="Rows requested.")
    offset: int = Field(description="Rows skipped before this page.")
    total: int = Field(description="Rows matching the filters, before paging.")
    has_more: bool = Field(
        description="Whether another page follows this one.")

    @classmethod
    def build(cls, limit: int, offset: int, total: int) -> "Pagination":
        return cls(limit=limit, offset=offset, total=total,
                   has_more=offset + limit < total)


class Deal(BaseModel):
    """One marked-down product, at the price of the latest scrape."""
    name: str
    sale_price: float | None = Field(
        default=None, description="What a shopper pays now.")
    original_price: float | None = Field(
        default=None,
        description="The crossed-out price. Null when the source never gave "
                    "one -- Uniqlo's API returns the same value for base and "
                    "promo price, for instance.")
    discount_pct: float | None = Field(
        default=None,
        description="Percent off, or null when there is no original price to "
                    "compare against. A null is not 0%: such items are left "
                    "out of discount filters and sort last in a discount "
                    "ranking.")
    product_url: str | None = None
    first_seen_at: datetime | None = Field(
        default=None, description="When this product first appeared on sale.")
    last_seen_at: datetime | None = Field(
        default=None, description="When it was last seen on the sale page.")
    scraped_at: datetime | None = Field(
        default=None, description="When this price was recorded.")


class StoreSummary(BaseModel):
    """One physical location."""
    place_id: str
    name: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    website: str | None = None


class StoreWithChain(StoreSummary):
    """A location plus the retailer it belongs to, for the map."""
    chain_key: str
    chain_name: str | None = None
    chain_status: ChainStatus


class ChainSummary(BaseModel):
    """A retailer and the state of what we know about it."""
    key: str = Field(description="Identity used to group shops into one "
                                 "retailer, normally the registrable domain.")
    name: str | None = None
    domain: str
    status: ChainStatus
    sale_url: str | None = Field(
        default=None, description="The sale page deals were read from.")
    stage: str | None = Field(
        default=None,
        description="How that page was reached: fetch, render, hover, "
                    "custom_api or cached.")
    last_scraped_at: datetime | None = None
    is_fresh: bool = Field(
        description="Whether these deals are inside the freshness window and "
                    "so would be served from cache rather than re-scraped.")
    recheck_due_at: datetime | None = Field(
        default=None,
        description="For a chain with no sale page or behind a bot wall, when "
                    "it comes up for another look. Null for a healthy chain.")
    store_count: int = Field(
        default=0, description="Locations of this chain that we know about.")


class ChainWithDeals(ChainSummary):
    """A retailer with a sample of its current deals, for a city listing."""
    stores: list[StoreSummary] = []
    promotions: list[str] = Field(
        default=[],
        description="Storewide offers, which belong to no single product.")
    deals: list[Deal] = Field(
        default=[],
        description="Up to `deals_per_chain` items. Use "
                    "/chains/{chain_key}/deals for the full, sortable list.")
    deal_count: int = Field(
        default=0,
        description="How many deals this chain has in total, which may be more "
                    "than the `deals` shown here.")


class CityDealsResponse(BaseModel):
    """Chains in one city, each with a sample of its deals."""
    city: str
    location: str = Field(
        description='The parts joined as Places reads them: "Boston, MA 02116".')
    items: list[ChainWithDeals]
    pagination: Pagination


class ChainDealsResponse(BaseModel):
    """One chain's current deals, filtered and sorted."""
    chain: ChainSummary
    promotions: list[str] = []
    items: list[Deal]
    pagination: Pagination


class StoresResponse(BaseModel):
    """Store locations with coordinates, for plotting."""
    items: list[StoreWithChain]
    pagination: Pagination


class HealthResponse(BaseModel):
    """Whether the API is up and whether it can see its database."""
    status: str = Field(description='"ok", or "degraded" if the database is '
                                    "unreachable.")
    database: str = Field(description='"ok" or "unreachable".')
    chains: int | None = Field(
        default=None, description="Chains on record. Null if unreachable.")
    stores: int | None = Field(
        default=None, description="Locations on record. Null if unreachable.")
    last_scrape_at: datetime | None = Field(
        default=None, description="The most recent scrape of any chain.")


class ErrorDetail(BaseModel):
    code: str = Field(description='Machine-readable: "not_found", '
                                  '"bad_request", "internal_error".')
    message: str = Field(description="What went wrong, in a sentence.")
    detail: object | None = Field(
        default=None,
        description="Which parameters were rejected, when that applies.")


class ErrorResponse(BaseModel):
    """The one error shape every failure uses, so a client parses it once."""
    error: ErrorDetail
