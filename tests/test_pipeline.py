"""Tests for chain grouping, caching and the scrape log.

Nothing here touches the network: scraping is stubbed, so these exercise the
decisions pipeline makes rather than the sites it makes them about.
"""
from datetime import timedelta

import pytest

import db
import pipeline
# Captured before the FakeScraper fixture replaces dt.page_text, so one test
# below can put the real implementation back and drive it end to end.
from DealsTracker import page_text as real_page_text
from db import (
    Chain, PriceSnapshot, Product, Promotion, ScrapeRun, Store,
    STAGE_CACHED, STAGE_FETCH,
    STATUS_BLOCKED, STATUS_ERROR, STATUS_NO_SALE_PAGE, STATUS_OK, utcnow,
)
from deals import empty_deals

NOW = utcnow()


def store(website, name="Shop", place_id=None, address="1 Main St"):
    return {"place_id": place_id or f"pid-{website}-{name}",
            "name": name, "website": website, "address": address,
            "lat": 42.3, "lng": -71.1}


def some_deals(n=2):
    return {"items": [{"name": f"Item {i}", "sale_price": 10.0 + i,
                       "original_price": 40.0, "product_url": None}
                      for i in range(n)],
            "promotions": ["20% off everything"]}


# --- Grouping ---------------------------------------------------------------
class TestGroupByChain:
    def test_locations_of_one_chain_are_scraped_once(self):
        grouped = pipeline.group_by_chain([
            store("https://www2.hm.com/en_us/.../US0100.html", "H&M"),
            store("https://www.hm.com/en_us/another-store", "H&M"),
        ])
        assert list(grouped) == ["hm.com"]
        assert len(grouped["hm.com"]) == 2

    def test_locator_subdomain_joins_the_main_site(self):
        grouped = pipeline.group_by_chain([
            store("https://stores.nordstromrack.com/us/ma/boston", "Nordstrom Rack"),
            store("https://www.nordstromrack.com/", "Nordstrom Rack"),
        ])
        assert list(grouped) == ["nordstromrack.com"]

    def test_sibling_brands_are_not_merged(self):
        grouped = pipeline.group_by_chain([
            store("https://oldnavy.gap.com/stores/x", "Old Navy"),
            store("https://www.gap.com/", "Gap"),
        ])
        assert set(grouped) == {"oldnavy.gap.com", "gap.com"}

    def test_stores_without_a_website_are_ignored(self):
        assert pipeline.group_by_chain([store("", "No site")]) == {}


class TestChainDisplayName:
    def test_most_common_name_wins(self):
        name = pipeline._chain_display_name(
            [store("https://x.com", "Gap"), store("https://x.com", "Gap"),
             store("https://x.com", "Gap Factory")])
        assert name == "Gap"


# --- Caching decisions ------------------------------------------------------
class FakeScraper:
    """Stands in for the network, and counts how often it was asked to work."""

    def __init__(self, deals=None, sale_url="https://x.com/sale", text="page text"):
        self.deals = deals if deals is not None else some_deals()
        self.sale_url = sale_url
        self.text = text
        self.discovery_calls = 0
        self.page_calls = 0
        self.extract_calls = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(pipeline.dt, "find_clothing_stores",
                            lambda *a, **k: self.stores)
        monkeypatch.setattr(pipeline.dt, "custom_scraper_for", lambda url: None)

        def discover(url):
            self.discovery_calls += 1
            return self.sale_url, STAGE_FETCH

        def page_text(url):
            self.page_calls += 1
            return self.text

        def extract(text, url):
            self.extract_calls += 1
            return self.deals

        monkeypatch.setattr(pipeline.dt, "discover_deals_page", discover)
        monkeypatch.setattr(pipeline.dt, "page_text", page_text)
        monkeypatch.setattr(pipeline.dt, "extract_deals", extract)
        monkeypatch.setattr(pipeline.dt, "build_location", lambda *a, **k: "Boston, MA")
        return self

    stores = [store("https://www.example.com/shop", "Example")]


@pytest.fixture
def fake(monkeypatch):
    return FakeScraper().install(monkeypatch)


@pytest.fixture
def memory_db(monkeypatch):
    """pipeline opens its own sessions, so point the module at a shared
    in-memory database that survives between them."""
    from sqlalchemy import StaticPool, create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_Session",
                        sessionmaker(bind=engine, expire_on_commit=False, future=True))
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    db.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


class TestFreshness:
    def test_first_search_scrapes(self, memory_db, fake):
        result = pipeline.search_deals("Boston", "MA")
        assert result.scraped == 1 and result.cached == 0
        assert fake.extract_calls == 1
        assert len(result.chains[0].deals["items"]) == 2

    def test_second_search_within_the_window_reads_the_database(self, memory_db, fake):
        first = pipeline.search_deals("Boston", "MA")
        second = pipeline.search_deals("Boston", "MA")

        assert second.cached == 1 and second.scraped == 0
        assert fake.extract_calls == 1, "should not have scraped again"
        assert second.chains[0].from_cache
        # Cached deals match what was scraped.
        assert ({i["name"] for i in second.chains[0].deals["items"]}
                == {i["name"] for i in first.chains[0].deals["items"]})
        assert second.chains[0].deals["promotions"] == ["20% off everything"]

    def test_stale_chain_is_rescraped(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            chain = s.query(Chain).one()
            chain.last_scraped_at = utcnow() - db.DEALS_FRESH_FOR - timedelta(minutes=1)

        result = pipeline.search_deals("Boston", "MA")
        assert result.scraped == 1
        assert fake.extract_calls == 2

    def test_force_refresh_ignores_freshness(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        result = pipeline.search_deals("Boston", "MA", force_keys={"example.com"})
        assert result.scraped == 1
        assert fake.extract_calls == 2


class TestSaleUrlCache:
    def test_discovery_is_skipped_when_the_url_is_trusted(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        assert fake.discovery_calls == 1

        # Force a re-scrape; the saved URL is still within its trust window.
        pipeline.search_deals("Boston", "MA", force_keys={"example.com"})
        assert fake.discovery_calls == 1, "should have reused the saved sale URL"
        assert fake.page_calls == 2

        with db.session_scope() as s:
            assert s.query(ScrapeRun).order_by(ScrapeRun.id.desc()).first().stage_used \
                == STAGE_CACHED

    def test_expired_url_triggers_rediscovery(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            chain = s.query(Chain).one()
            chain.sale_url_verified_at = (
                utcnow() - db.SALE_URL_TRUSTED_FOR - timedelta(hours=1))

        pipeline.search_deals("Boston", "MA", force_keys={"example.com"})
        assert fake.discovery_calls == 2

    def test_cached_url_that_yields_nothing_is_dropped(self, memory_db, fake):
        """A saved URL can go stale when a sale ends. Clear it so the next run
        rediscovers, rather than caching a failure against a dead URL."""
        pipeline.search_deals("Boston", "MA")
        fake.deals = empty_deals()

        result = pipeline.search_deals("Boston", "MA", force_keys={"example.com"})
        assert result.chains[0].status == STATUS_ERROR
        with db.session_scope() as s:
            chain = s.query(Chain).one()
            assert chain.sale_url is None
            assert chain.sale_url_verified_at is None


class TestNegativeCaching:
    def test_chain_with_no_sale_page_is_recorded(self, memory_db, fake):
        fake.sale_url = None        # discovery found no sale link (Primark)
        result = pipeline.search_deals("Boston", "MA")
        assert result.chains[0].status == STATUS_NO_SALE_PAGE

    def test_blocked_chain_is_recorded(self, memory_db, fake):
        fake.text = ""              # rendered, but nothing readable (bot wall)
        result = pipeline.search_deals("Boston", "MA")
        assert result.chains[0].status == STATUS_BLOCKED

    def test_a_wall_served_as_http_200_is_blocked_not_no_sale_page(
            self, memory_db, fake, monkeypatch):
        """End to end, with the real page_text: loft.com answers 200 with a few
        hundred characters of PerimeterX prose. That cleared every length check,
        so the chain used to be filed as having no sale section at all."""
        wall = ("Access to this page has been denied. You have been blocked "
                "because we believe you are using automation tools to browse "
                "the website. Reference ID: a3f29c81-4e7b-11ef-9c2d")

        class Response:
            ok = True
            text = f"<html><body>{wall}</body></html>"

        monkeypatch.setattr(pipeline.dt, "page_text", real_page_text)
        monkeypatch.setattr(pipeline.dt.requests, "get",
                            lambda url, **kwargs: Response())
        monkeypatch.setattr(pipeline.dt, "render_text", lambda url, **kw: wall)

        result = pipeline.search_deals("Boston", "MA")
        assert result.chains[0].status == STATUS_BLOCKED
        assert fake.extract_calls == 0, (
            "a wall should not cost an LLM call -- there are no prices in it")

    def test_dead_end_is_skipped_until_the_recheck_window_passes(self, memory_db, fake):
        fake.sale_url = None
        pipeline.search_deals("Boston", "MA")
        calls_after_first = fake.discovery_calls

        second = pipeline.search_deals("Boston", "MA")
        assert second.skipped == 1
        assert fake.discovery_calls == calls_after_first, "should not have retried"
        assert second.chains[0].skipped

    def test_dead_end_is_rechecked_once_the_window_passes(self, memory_db, fake):
        fake.sale_url = None
        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            chain = s.query(Chain).one()
            chain.status_checked_at = (
                utcnow() - db.NEGATIVE_RECHECK_AFTER - timedelta(hours=1))

        result = pipeline.search_deals("Boston", "MA")
        assert result.skipped == 0 and result.scraped == 1

    def test_refresh_overrides_a_skip(self, memory_db, fake):
        fake.sale_url = None
        pipeline.search_deals("Boston", "MA")
        result = pipeline.search_deals("Boston", "MA", force_keys={"example.com"})
        assert result.skipped == 0 and result.scraped == 1


# --- Logging ----------------------------------------------------------------
class TestScrapeRunLog:
    def test_successful_run_is_logged_with_timing(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            run = s.query(ScrapeRun).one()
            assert run.status == STATUS_OK
            assert run.deals_found == 2
            assert run.promotions_found == 1
            assert run.stage_used == STAGE_FETCH
            assert run.duration_seconds is not None and run.duration_seconds >= 0

    def test_failure_is_logged_too(self, memory_db, fake, monkeypatch):
        def boom(url):
            raise RuntimeError("renderer died")
        monkeypatch.setattr(pipeline.dt, "page_text", boom)

        result = pipeline.search_deals("Boston", "MA")
        assert result.chains[0].status == STATUS_ERROR
        with db.session_scope() as s:
            run = s.query(ScrapeRun).one()
            assert run.status == STATUS_ERROR
            assert "renderer died" in run.error_message
            assert run.duration_seconds is not None

    def test_discard_counts_are_recorded(self, memory_db, fake):
        """Extraction quality is tracked per run, so a page redesign shows up
        as a jump in rejects rather than a quiet drop in deals."""
        fake.deals = {"items": [
            {"name": "Good", "sale_price": 10, "original_price": 40},
            {"name": "Good", "sale_price": 10, "original_price": 40},   # duplicate
            {"name": "", "sale_price": 10},                             # rejected
            {"name": "Echoed", "sale_price": 10, "original_price": 10}, # corrected
        ], "promotions": []}

        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            run = s.query(ScrapeRun).one()
            assert run.items_rejected == 1
            assert run.duplicates_dropped == 1
            assert run.originals_corrected == 1
            assert run.deals_found == 2


# --- Persistence ------------------------------------------------------------
class TestPersistence:
    def test_rescraping_appends_price_history(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        fake.deals = {"items": [{"name": "Item 0", "sale_price": 5.0,
                                 "original_price": 40.0}], "promotions": []}
        pipeline.search_deals("Boston", "MA", force_keys={"example.com"})

        with db.session_scope() as s:
            product = s.query(Product).filter_by(normalized_name="item 0").one()
            prices = [snap.sale_price for snap in
                      s.query(PriceSnapshot).filter_by(product_id=product.id)
                       .order_by(PriceSnapshot.scraped_at).all()]
            assert prices == [10.0, 5.0], "history should grow, not be overwritten"

    def test_locations_are_saved_against_the_chain(self, memory_db, fake):
        fake.stores = [store("https://www.example.com/a", "Example", "pid-a"),
                       store("https://www.example.com/b", "Example", "pid-b")]
        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            chain = s.query(Chain).one()
            stores = s.query(Store).all()
            assert len(stores) == 2
            assert {st.chain_id for st in stores} == {chain.id}
            assert all(st.last_refreshed_at is not None for st in stores)

    def test_cached_deals_exclude_products_from_older_scrapes(self, memory_db, fake):
        """An item that has left the sale shouldn't reappear from history."""
        pipeline.search_deals("Boston", "MA")

        # Push that scrape into the past before re-scraping. Everything here is
        # stubbed, so two searches finish well inside one tick of the clock --
        # about a millisecond on Windows -- and the older scrape would
        # otherwise carry the same timestamp as the newer one, leaving no way
        # for either this code or the assertion to tell them apart. Real
        # scrapes are hours apart; see DEALS_FRESH_FOR.
        earlier = utcnow() - timedelta(hours=1)
        with db.session_scope() as s:
            for snapshot in s.query(PriceSnapshot).all():
                snapshot.scraped_at = earlier
            for promotion in s.query(Promotion).all():
                promotion.scraped_at = earlier
            s.query(Chain).one().last_scraped_at = earlier

        fake.deals = {"items": [{"name": "Item 0", "sale_price": 9.0,
                                 "original_price": 40.0}], "promotions": []}
        pipeline.search_deals("Boston", "MA", force_keys={"example.com"})

        result = pipeline.search_deals("Boston", "MA")   # served from cache
        assert result.cached == 1
        assert [i["name"] for i in result.chains[0].deals["items"]] == ["Item 0"]


class TestPlacesRetention:
    def test_details_older_than_the_window_are_cleared(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            s.query(Store).one().last_refreshed_at = (
                utcnow() - db.PLACES_DETAILS_REFRESH_AFTER - timedelta(days=1))

        with db.session_scope() as s:
            assert pipeline.clear_stale_place_details(s) == 1

        with db.session_scope() as s:
            store_row = s.query(Store).one()
            assert store_row.place_id          # id may be kept indefinitely
            assert store_row.address is None   # details may not
            assert store_row.lat is None

    def test_recent_details_are_untouched(self, memory_db, fake):
        pipeline.search_deals("Boston", "MA")
        with db.session_scope() as s:
            assert pipeline.clear_stale_place_details(s) == 0
            assert s.query(Store).one().address == "1 Main St"
