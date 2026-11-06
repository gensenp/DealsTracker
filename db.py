"""Persistence for DealsTracker.

SQLite locally, PostgreSQL (Supabase) when deployed -- same code either way.
Set DATABASE_URL to switch; with it unset we use a file next to this module.

The shape of the data follows how scraping actually works: deals belong to a
*chain*, not to a shop. Every H&M on Newbury Street shows the same online sale,
so we scrape hm.com once and read those deals for each of its locations. Prices
are append-only (`price_snapshots`), so a product's history survives re-scrapes.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker,
)

# --- Tuning -----------------------------------------------------------------
# How long a chain's scraped deals count as current before we re-scrape.
DEALS_FRESH_FOR = timedelta(hours=6)
# How long a discovered sale URL is trusted before we re-run discovery.
SALE_URL_TRUSTED_FOR = timedelta(days=7)
# How long we leave a dead end alone (no sale page, or bot-walled).
NEGATIVE_RECHECK_AFTER = timedelta(days=7)
# Google's terms allow caching place_id indefinitely, but the rest of a place's
# details must be refreshed periodically rather than kept forever.
PLACES_DETAILS_REFRESH_AFTER = timedelta(days=30)

# --- Enumerations (plain strings; kept here so callers never spell them out) --
STAGE_FETCH = "fetch"            # found in the plain requests HTML
STAGE_RENDER = "render"          # needed a headless browser
STAGE_HOVER = "hover"            # needed a mega-menu opened
STAGE_CUSTOM_API = "custom_api"  # a per-chain scraper handled it
STAGE_CACHED = "cached"          # reused a saved sale_url, no discovery run

STATUS_OK = "ok"
STATUS_NO_SALE_PAGE = "no_sale_page"  # genuinely has none (Primark)
STATUS_BLOCKED = "blocked"            # bot wall (T.J. Maxx / Marshalls)
STATUS_ERROR = "error"                # transient failure; retry sooner

# Statuses we cache negatively -- skip the chain until the recheck window is up.
NEGATIVE_STATUSES = (STATUS_NO_SALE_PAGE, STATUS_BLOCKED)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes while PostgreSQL returns aware ones.
    Treat a naive value as UTC so freshness maths works on both backends."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


_WHITESPACE = re.compile(r"\s+")

def normalize_name(name: str) -> str:
    """Key for deduplicating a product within a chain. Retailers re-case and
    re-punctuate titles between runs, which would otherwise create a new
    product row -- and a broken price history -- on every scrape."""
    cleaned = _WHITESPACE.sub(" ", (name or "").strip().lower())
    return cleaned.strip(" -–—.,")


# --- Models -----------------------------------------------------------------
class Base(DeclarativeBase):
    pass


class Chain(Base):
    """One retailer. `key` identifies it for grouping: normally the registrable
    domain (hm.com), but kept separate from `domain` so a multi-brand domain can
    be split later -- oldnavy.gap.com and gap.com share a registrable domain but
    are different shops with different sales."""
    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str | None] = mapped_column(String(255))

    sale_url: Mapped[str | None] = mapped_column(Text)
    discovery_stage: Mapped[str | None] = mapped_column(String(32))
    sale_url_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(32), default=STATUS_OK)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    stores: Mapped[list["Store"]] = relationship(back_populates="chain")
    products: Mapped[list["Product"]] = relationship(back_populates="chain")

    def __repr__(self) -> str:
        return f"<Chain {self.key} status={self.status}>"


class Store(Base):
    """One physical location, as Google Places knows it."""
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    website: Mapped[str | None] = mapped_column(Text)

    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id"), index=True)
    chain: Mapped[Chain | None] = relationship(back_populates="stores")

    # When the Places details (not the id) were last re-fetched.
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Store {self.name!r} {self.place_id}>"


class Product(Base):
    """A product seen on a chain's sale page. Prices live in snapshots."""
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("chain_id", "normalized_name", name="uq_product_chain_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    chain: Mapped[Chain] = relationship(back_populates="products")

    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(String(500))
    product_url: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="product", order_by="PriceSnapshot.scraped_at")

    def __repr__(self) -> str:
        return f"<Product {self.name[:40]!r}>"


class PriceSnapshot(Base):
    """Append-only price observation. Never updated, so history is intact.

    `original_price` is nullable on purpose: some sources give only the current
    price (Uniqlo's API returns the same value for base and promo), and those
    items are excluded from discount ranking rather than shown as 0% off."""
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    product: Mapped[Product] = relationship(back_populates="snapshots")

    sale_price: Mapped[float | None] = mapped_column(Float)
    original_price: Mapped[float | None] = mapped_column(Float)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)

    @property
    def discount_pct(self) -> float | None:
        """Percent off, or None when the source didn't give an original price."""
        if not self.original_price or not self.sale_price:
            return None
        if self.original_price <= self.sale_price:
            return None
        return round((1 - self.sale_price / self.original_price) * 100)

    def __repr__(self) -> str:
        return f"<PriceSnapshot {self.sale_price} was {self.original_price}>"


class ScrapeRun(Base):
    """One attempt at scraping one chain -- success or failure, always logged."""
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    stage_used: Mapped[str | None] = mapped_column(String(32))
    deals_found: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default=STATUS_OK)
    error_message: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<ScrapeRun chain={self.chain_id} {self.status} {self.deals_found} deals>"


class Promotion(Base):
    """A storewide offer ("20% off jeans"), which belongs to no single product."""
    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    scrape_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"))
    text: Mapped[str] = mapped_column(Text)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)

    def __repr__(self) -> str:
        return f"<Promotion {self.text[:40]!r}>"


# --- Freshness rules --------------------------------------------------------
def deals_are_fresh(chain: Chain, now: datetime | None = None) -> bool:
    """True if this chain was scraped recently enough to show cached deals."""
    last = as_utc(chain.last_scraped_at)
    if last is None:
        return False
    return (now or utcnow()) - last < DEALS_FRESH_FOR


def sale_url_is_trusted(chain: Chain, now: datetime | None = None) -> bool:
    """True if we can fetch the saved sale_url directly and skip discovery."""
    if not chain.sale_url or chain.status == STATUS_ERROR:
        return False
    verified = as_utc(chain.sale_url_verified_at)
    if verified is None:
        return False
    return (now or utcnow()) - verified < SALE_URL_TRUSTED_FOR


def should_skip_chain(chain: Chain, now: datetime | None = None) -> bool:
    """True for a known dead end still inside its recheck window: a chain with
    no sale page at all, or one behind a bot wall. We record the status and
    leave it alone rather than hammering it on every search."""
    if chain.status not in NEGATIVE_STATUSES:
        return False
    checked = as_utc(chain.status_checked_at)
    if checked is None:
        return False
    return (now or utcnow()) - checked < NEGATIVE_RECHECK_AFTER


def store_details_are_stale(store: Store, now: datetime | None = None) -> bool:
    """True if this store's Places details are due a refresh. place_id may be
    kept indefinitely; the rest may not."""
    last = as_utc(store.last_refreshed_at)
    if last is None:
        return True
    return (now or utcnow()) - last >= PLACES_DETAILS_REFRESH_AFTER


# --- Engine / session -------------------------------------------------------
_DEFAULT_SQLITE = "sqlite:///" + os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dealstracker.db")


def get_database_url() -> str:
    """Connection string from DATABASE_URL, defaulting to a local SQLite file.

    Supabase and Heroku hand out `postgres://` / `postgresql://` URLs; both are
    rewritten to psycopg 3, which is the driver we depend on."""
    url = (os.environ.get("DATABASE_URL") or "").strip().strip('"\'')
    if not url:
        return _DEFAULT_SQLITE
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


_engine = None
_Session = None


def get_engine():
    """Lazily built engine, shared for the process."""
    global _engine, _Session
    if _engine is not None:
        return _engine

    url = get_database_url()
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # Streamlit touches the DB from more than one thread.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Supabase's pooled connection string runs pgbouncer in transaction
        # mode, which cannot carry server-side prepared statements between
        # queries; psycopg 3 creates those by default after a few repeats.
        kwargs["connect_args"] = {"prepare_threshold": None}

    _engine = create_engine(url, **kwargs)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    """Create any missing tables. Safe to call on every startup."""
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope():
    """Transactional session: commits on success, rolls back on error."""
    get_engine()  # ensures _Session exists
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_testing(url: str | None = None) -> None:
    """Point the module at a different database (tests use in-memory SQLite)."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
    if url is not None:
        os.environ["DATABASE_URL"] = url
    else:
        os.environ.pop("DATABASE_URL", None)
