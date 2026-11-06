"""Tests for the persistence layer: freshness windows, sale-URL caching and
the negative caching that keeps us off dead ends."""
from datetime import timedelta

import pytest

import db
from db import (
    Chain, PriceSnapshot, Product, Store,
    STATUS_BLOCKED, STATUS_ERROR, STATUS_NO_SALE_PAGE, STATUS_OK,
    deals_are_fresh, normalize_name, sale_url_is_trusted, should_skip_chain,
    store_details_are_stale, utcnow,
)

NOW = utcnow()


def make_chain(**kwargs):
    defaults = dict(key="hm.com", domain="hm.com", status=STATUS_OK)
    defaults.update(kwargs)
    return Chain(**defaults)


# --- Freshness --------------------------------------------------------------
class TestDealsAreFresh:
    def test_never_scraped_is_stale(self):
        assert not deals_are_fresh(make_chain(last_scraped_at=None), NOW)

    def test_just_scraped_is_fresh(self):
        chain = make_chain(last_scraped_at=NOW - timedelta(minutes=5))
        assert deals_are_fresh(chain, NOW)

    def test_inside_window_is_fresh(self):
        chain = make_chain(last_scraped_at=NOW - db.DEALS_FRESH_FOR + timedelta(minutes=1))
        assert deals_are_fresh(chain, NOW)

    def test_outside_window_is_stale(self):
        chain = make_chain(last_scraped_at=NOW - db.DEALS_FRESH_FOR - timedelta(minutes=1))
        assert not deals_are_fresh(chain, NOW)

    def test_naive_timestamp_from_sqlite_is_treated_as_utc(self):
        """SQLite returns naive datetimes; comparing them without attaching a
        timezone raises TypeError instead of answering the question."""
        chain = make_chain(last_scraped_at=(NOW - timedelta(hours=1)).replace(tzinfo=None))
        assert deals_are_fresh(chain, NOW)


# --- Sale URL caching -------------------------------------------------------
class TestSaleUrlIsTrusted:
    def test_no_url_means_discovery(self):
        assert not sale_url_is_trusted(make_chain(sale_url=None), NOW)

    def test_recently_verified_url_is_reused(self):
        chain = make_chain(sale_url="https://www2.hm.com/en_us/men/sale/view-all.html",
                           sale_url_verified_at=NOW - timedelta(days=1))
        assert sale_url_is_trusted(chain, NOW)

    def test_url_older_than_window_is_rediscovered(self):
        chain = make_chain(sale_url="https://example.com/sale",
                           sale_url_verified_at=NOW - db.SALE_URL_TRUSTED_FOR - timedelta(hours=1))
        assert not sale_url_is_trusted(chain, NOW)

    def test_errored_chain_is_rediscovered_even_if_recent(self):
        """A saved URL that just failed shouldn't be handed out again."""
        chain = make_chain(sale_url="https://example.com/sale",
                           sale_url_verified_at=NOW - timedelta(minutes=1),
                           status=STATUS_ERROR)
        assert not sale_url_is_trusted(chain, NOW)

    def test_url_never_verified_is_rediscovered(self):
        chain = make_chain(sale_url="https://example.com/sale", sale_url_verified_at=None)
        assert not sale_url_is_trusted(chain, NOW)


# --- Negative caching -------------------------------------------------------
class TestShouldSkipChain:
    @pytest.mark.parametrize("status", [STATUS_NO_SALE_PAGE, STATUS_BLOCKED])
    def test_recent_dead_end_is_skipped(self, status):
        """Primark has no sale page; T.J. Maxx is bot-walled. Neither is worth
        re-checking on every search."""
        chain = make_chain(status=status, status_checked_at=NOW - timedelta(days=1))
        assert should_skip_chain(chain, NOW)

    @pytest.mark.parametrize("status", [STATUS_NO_SALE_PAGE, STATUS_BLOCKED])
    def test_stale_dead_end_is_rechecked(self, status):
        chain = make_chain(status=status,
                           status_checked_at=NOW - db.NEGATIVE_RECHECK_AFTER - timedelta(hours=1))
        assert not should_skip_chain(chain, NOW)

    def test_healthy_chain_is_never_skipped(self):
        chain = make_chain(status=STATUS_OK, status_checked_at=NOW)
        assert not should_skip_chain(chain, NOW)

    def test_transient_error_is_retried_immediately(self):
        """STATUS_ERROR is not a dead end -- it should be retried, not cached."""
        chain = make_chain(status=STATUS_ERROR, status_checked_at=NOW)
        assert not should_skip_chain(chain, NOW)


# --- Places data retention --------------------------------------------------
class TestStoreDetailsAreStale:
    def test_new_store_needs_details(self):
        assert store_details_are_stale(Store(place_id="x", name="A"), NOW)

    def test_recent_details_are_kept(self):
        store = Store(place_id="x", name="A", last_refreshed_at=NOW - timedelta(days=5))
        assert not store_details_are_stale(store, NOW)

    def test_details_past_retention_window_are_refreshed(self):
        store = Store(place_id="x", name="A",
                      last_refreshed_at=NOW - db.PLACES_DETAILS_REFRESH_AFTER - timedelta(days=1))
        assert store_details_are_stale(store, NOW)


# --- Name normalization -----------------------------------------------------
class TestNormalizeName:
    def test_case_and_spacing_collapse_to_one_key(self):
        assert normalize_name("  BDG  Logan   JEANS ") == normalize_name("bdg logan jeans")

    def test_trailing_punctuation_is_stripped(self):
        assert normalize_name("Nike Tee —") == "nike tee"

    def test_distinct_products_stay_distinct(self):
        assert normalize_name("Slim Jeans") != normalize_name("Straight Jeans")


# --- Connection string ------------------------------------------------------
class TestGetDatabaseUrl:
    def test_defaults_to_local_sqlite(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert db.get_database_url().startswith("sqlite:///")

    @pytest.mark.parametrize("given", [
        "postgres://user:pw@host:6543/postgres",      # Heroku-style
        "postgresql://user:pw@host:6543/postgres",    # Supabase-style
    ])
    def test_postgres_urls_are_pinned_to_psycopg3(self, monkeypatch, given):
        """SQLAlchemy would otherwise reach for psycopg2, which we don't ship."""
        monkeypatch.setenv("DATABASE_URL", given)
        url = db.get_database_url()
        assert url.startswith("postgresql+psycopg://")
        assert url.endswith("@host:6543/postgres")

    def test_surrounding_quotes_and_space_are_stripped(self, monkeypatch):
        """Pasting into Streamlit Secrets or .env often brings quotes along."""
        monkeypatch.setenv("DATABASE_URL", '  "sqlite:///x.db"  ')
        assert db.get_database_url() == "sqlite:///x.db"


# --- Persistence round-trip -------------------------------------------------
class TestPersistence:
    def test_price_history_accumulates_rather_than_overwrites(self, session):
        chain = make_chain()
        session.add(chain)
        session.flush()

        product = Product(chain_id=chain.id, name="Track Shorts",
                          normalized_name=normalize_name("Track Shorts"))
        session.add(product)
        session.flush()

        session.add_all([
            PriceSnapshot(product_id=product.id, sale_price=19.99, original_price=39.99),
            PriceSnapshot(product_id=product.id, sale_price=10.99, original_price=39.99),
        ])
        session.flush()

        snapshots = session.query(PriceSnapshot).filter_by(product_id=product.id).all()
        assert len(snapshots) == 2
        assert {s.sale_price for s in snapshots} == {19.99, 10.99}

    def test_same_product_cannot_be_stored_twice_for_one_chain(self, session):
        from sqlalchemy.exc import IntegrityError

        chain = make_chain()
        session.add(chain)
        session.flush()
        for _ in range(2):
            session.add(Product(chain_id=chain.id, name="Tee",
                                normalized_name=normalize_name("Tee")))
        with pytest.raises(IntegrityError):
            session.flush()
        # Leave the session usable so the fixture can close it cleanly.
        session.rollback()

    def test_discount_pct_needs_both_prices(self):
        assert PriceSnapshot(sale_price=10.0, original_price=40.0).discount_pct == 75
        # Uniqlo gives no original price; such items must not read as 0% off.
        assert PriceSnapshot(sale_price=10.0, original_price=None).discount_pct is None
        assert PriceSnapshot(sale_price=10.0, original_price=10.0).discount_pct is None
