"""Tests for the Streamlit UI, run headless through streamlit's AppTest.

The pipeline is stubbed, so these check what the page does with a result --
grouping, provenance lines, the per-chain Refresh button -- not the scraping
behind it.
"""
from datetime import timedelta
from pathlib import Path

import pytest

import db
import pipeline
import ui
from db import STATUS_BLOCKED, STATUS_NO_SALE_PAGE, STATUS_OK, utcnow
from pipeline import ChainResult, SearchResult

APP = str(Path(__file__).parent.parent / "app.py")

pytest.importorskip("streamlit.testing.v1", reason="needs streamlit's AppTest")
from streamlit.testing.v1 import AppTest  # noqa: E402


@pytest.fixture(autouse=True)
def needs_keys():
    """app.py imports DealsTracker, which refuses to load without API keys."""
    try:
        import DealsTracker  # noqa: F401
    except RuntimeError as exc:
        pytest.skip(f"app.py needs API keys: {exc}")


def chain(key="hm.com", name="H&M", items=2, promos=1, **kwargs):
    deals = {
        "items": [{"name": f"Item {i}", "sale_price": 10.0 + i,
                   "original_price": 40.0, "product_url": None}
                  for i in range(items)],
        "promotions": ["Buy 2 Get 1 Free"][:promos],
    }
    defaults = dict(
        key=key, name=name, deals=deals,
        stores=[{"name": name, "address": "100 Newbury St, Boston, MA"}],
        sale_url="https://www2.hm.com/sale", status=STATUS_OK,
        last_scraped_at=utcnow() - timedelta(minutes=4), duration_s=3.2,
    )
    defaults.update(kwargs)
    return ChainResult(**defaults)


def result(chains=None, **kwargs):
    defaults = dict(location="Boston, MA", chains=chains or [chain()],
                    total_seconds=62.0, scraped=1, cached=0, skipped=0)
    defaults.update(kwargs)
    return SearchResult(**defaults)


def run_with(monkeypatch, search_result, **session):
    """Render the app with a canned pipeline result already in session."""
    monkeypatch.setattr(pipeline, "search_deals",
                        lambda *a, **k: search_result)
    app = AppTest.from_file(APP, default_timeout=30)
    app.session_state["result"] = search_result
    app.session_state["params"] = ("Boston", "MA", "")
    for key, value in session.items():
        app.session_state[key] = value
    return app.run()


def text_of(app):
    """Everything the page rendered, as one searchable string."""
    parts = []
    for collection in (app.markdown, app.caption, app.subheader, app.success,
                       app.warning, app.error, app.info):
        parts.extend(element.value for element in collection)
    return "\n".join(parts)


class TestEmptyState:
    def test_nothing_is_rendered_before_a_search(self, monkeypatch):
        monkeypatch.setattr(pipeline, "search_deals", lambda *a, **k: result())
        app = AppTest.from_file(APP, default_timeout=30).run()
        assert not app.exception
        assert "retailers have deals" not in text_of(app)

    def test_search_form_is_present(self, monkeypatch):
        app = AppTest.from_file(APP, default_timeout=30).run()
        assert [i.label for i in app.text_input] == ["City", "ZIP"]
        assert app.selectbox[0].label == "State"


class TestResultsSummary:
    def test_counts_and_timing_are_shown(self, monkeypatch):
        app = run_with(monkeypatch, result(scraped=2, cached=7, skipped=9,
                                           total_seconds=62.0))
        page = text_of(app)
        assert "2 scraped" in page
        assert "7 from saved results" in page
        assert "9 skipped" in page
        assert "62s" in page

    def test_in_store_prices_caveat_is_shown(self, monkeypatch):
        """Deals come from the website; a shopper standing in the shop needs
        to know the price on the shelf may not match."""
        app = run_with(monkeypatch, result())
        assert "in-store prices may differ" in text_of(app).lower()

    def test_headline_counts_retailers_with_deals(self, monkeypatch):
        app = run_with(monkeypatch, result(chains=[
            chain(key="hm.com", name="H&M"),
            chain(key="primark.com", name="Primark", items=0, promos=0,
                  status=STATUS_NO_SALE_PAGE),
        ]))
        assert "1 of 2 retailers have deals" in text_of(app)


class TestChainRendering:
    def test_deals_and_promotions_are_listed(self, monkeypatch):
        app = run_with(monkeypatch, result())
        page = text_of(app)
        assert "H&M" in page
        assert "Buy 2 Get 1 Free" in page
        assert "Item 0" in page
        assert "75% off" in page          # 10.00 off a 40.00 original

    def test_locations_are_grouped_under_the_chain(self, monkeypatch):
        """Six Gap shops share one scrape, so the page shows one card with the
        locations beneath it rather than six identical cards."""
        many = chain(key="gap.com", name="Gap", stores=[
            {"name": "Gap", "address": f"{n} Example St"} for n in range(6)])
        app = run_with(monkeypatch, result(chains=[many]))
        page = text_of(app)
        assert "6 locations" in page
        assert "5 Example St" in page
        assert page.count("### Gap") == 1

    def test_single_location_is_not_pluralized(self, monkeypatch):
        page = text_of(run_with(monkeypatch, result()))
        assert "· 1 location" in page
        assert "1 locations" not in page

    def test_freshly_scraped_chain_says_so(self, monkeypatch):
        app = run_with(monkeypatch, result(chains=[chain(from_cache=False)]))
        assert "scraped just now" in text_of(app)

    def test_cached_chain_shows_its_age(self, monkeypatch):
        app = run_with(monkeypatch, result(chains=[
            chain(from_cache=True,
                  last_scraped_at=utcnow() - timedelta(hours=2))]))
        page = text_of(app)
        assert "from saved results" in page
        assert "updated 2 hours ago" in page

    def test_blocked_chain_explains_itself(self, monkeypatch):
        app = run_with(monkeypatch, result(chains=[
            chain(key="tjmaxx.com", name="T.J. Maxx", items=0, promos=0,
                  status=STATUS_BLOCKED)]))
        assert "blocks automated access" in text_of(app)

    def test_skipped_dead_end_says_when_it_is_due(self, monkeypatch):
        app = run_with(monkeypatch, result(chains=[
            chain(key="primark.com", name="Primark", items=0, promos=0,
                  status=STATUS_NO_SALE_PAGE, from_cache=False,
                  last_scraped_at=utcnow() - timedelta(days=2))]))
        page = text_of(app)
        assert "not re-checked" in page
        assert "next look in 5 days" in page

    def test_long_lists_are_partly_folded_away(self, monkeypatch):
        app = run_with(monkeypatch, result(chains=[chain(items=30)]))
        assert any("more" in e.label for e in app.expander)


class TestRefreshButton:
    def test_every_chain_gets_its_own_refresh(self, monkeypatch):
        app = run_with(monkeypatch, result(chains=[
            chain(key="hm.com", name="H&M"),
            chain(key="gap.com", name="Gap"),
        ]))
        assert len([b for b in app.button if b.label == "Refresh"]) == 2

    def test_refresh_rescrapes_only_that_chain(self, monkeypatch):
        """The button must force one retailer, not re-run the whole search."""
        calls = {}

        def fake_search(city, state=None, postal_code=None, force_keys=None,
                        on_progress=None):
            calls["force_keys"] = force_keys
            return result()

        monkeypatch.setattr(pipeline, "search_deals", fake_search)
        app = AppTest.from_file(APP, default_timeout=30)
        app.session_state["result"] = result(chains=[
            chain(key="hm.com", name="H&M"), chain(key="gap.com", name="Gap")])
        app.session_state["params"] = ("Boston", "MA", "")
        app.run()

        app.button(key="refresh-gap.com").click().run()
        assert calls["force_keys"] == {"gap.com"}

    def test_page_survives_a_failing_search(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("Places API 403")

        monkeypatch.setattr(pipeline, "search_deals", boom)
        app = AppTest.from_file(APP, default_timeout=30)
        app.session_state["params"] = ("Boston", "MA", "")
        app.session_state["force_key"] = "hm.com"
        app.run()
        assert not app.exception
        assert "Places API 403" in text_of(app)


class TestHelpers:
    """The pure presentation helpers, tested without rendering a page."""

    @pytest.mark.parametrize("delta,expected", [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=4), "4 minutes ago"),
        (timedelta(minutes=1, seconds=40), "1 minute ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=3), "3 days ago"),
    ])
    def test_humanize_age(self, delta, expected):
        assert ui.humanize_age(utcnow() - delta) == expected

    def test_humanize_age_without_a_timestamp(self):
        assert ui.humanize_age(None) == "never"

    @pytest.mark.parametrize("checked_days_ago,expected", [
        (2, "in 5 days"),      # 4d23h left must not read as 4 days
        (6.5, "in 1 day"),
        (7.5, "today"),        # already overdue
    ])
    def test_recheck_due_rounds_up(self, checked_days_ago, expected):
        when = utcnow() - timedelta(days=checked_days_ago)
        assert ui.recheck_due_in(when) == expected

    def test_recheck_without_a_timestamp(self):
        assert ui.recheck_due_in(None) == "soon"

    def test_items_are_ordered_by_deepest_markdown(self):
        lines = ui.render_items([
            {"name": "Shallow", "sale_price": 90, "original_price": 100},
            {"name": "Deep", "sale_price": 10, "original_price": 100},
            {"name": "Unknown", "sale_price": 5, "original_price": None},
        ])
        assert "Deep" in lines[0]
        assert "Shallow" in lines[1]
        assert "Unknown" in lines[2], "items with no original price go last"

    def test_item_row_shows_both_prices(self):
        line = ui.render_items(
            [{"name": "Tee", "sale_price": 10, "original_price": 40}])[0]
        assert "10.00" in line and "40.00" in line and "75% off" in line

    def test_item_without_an_original_price_shows_only_one(self):
        line = ui.render_items(
            [{"name": "Tee", "sale_price": 10, "original_price": None}])[0]
        assert "10.00" in line and "% off" not in line

    def test_dollar_signs_are_escaped(self):
        r"""Two bare $ on one line make streamlit render LaTeX, which turned
        every "$10.00 was $40.00" row into mangled algebra."""
        line = ui.render_items(
            [{"name": "Tee", "sale_price": 10, "original_price": 40}])[0]
        assert "\\$10.00" in line and "\\$40.00" in line
        assert "$" not in line.replace("\\$", "")

    @pytest.mark.parametrize("count,expected", [
        (1, "1 location"), (0, "0 locations"), (6, "6 locations")])
    def test_pluralize(self, count, expected):
        assert ui.pluralize(count, "location") == expected
