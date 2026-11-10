"""Tests for the read-only API.

Nothing here touches the network or needs an API key: the app is served the
in-memory database from tests/conftest.py, and these exercise what the
endpoints decide rather than the sites the data came from.

The seeding helpers build a small city on purpose -- two chains that worked,
one behind a bot wall, one with no sale page, one never scraped -- because the
interesting answers are the ones about chains that produced nothing.
"""
from datetime import timedelta

import pytest

import db
from db import (
    Chain, PriceSnapshot, Product, Promotion, Store,
    STAGE_FETCH, STATUS_BLOCKED, STATUS_ERROR, STATUS_NO_SALE_PAGE, STATUS_OK,
    normalize_name, utcnow,
)

BOSTON = "100 Newbury St, Boston, MA 02116, USA"
CAMBRIDGE = "1 Brattle Sq, Cambridge, MA 02138, USA"


def make_chain(session, key, name=None, status=STATUS_OK, scraped_ago=None,
               sale_url="https://example.com/sale", stage=STAGE_FETCH,
               status_checked_ago=None):
    """A chain, optionally scraped some time ago."""
    now = utcnow()
    chain = Chain(
        key=key, domain=key, name=name or key.split(".")[0].title(),
        sale_url=sale_url, discovery_stage=stage, status=status,
        last_scraped_at=None if scraped_ago is None else now - scraped_ago,
        sale_url_verified_at=None if scraped_ago is None else now - scraped_ago,
        status_checked_at=(None if status_checked_ago is None
                           else now - status_checked_ago),
    )
    session.add(chain)
    session.flush()
    return chain


def add_store(session, chain, name=None, address=BOSTON, lat=42.35, lng=-71.08,
              place_id=None):
    store = Store(place_id=place_id or f"pid-{chain.key}-{name or chain.name}",
                  name=name or chain.name, address=address, lat=lat, lng=lng,
                  website=f"https://{chain.key}/", chain_id=chain.id,
                  last_refreshed_at=utcnow())
    session.add(store)
    session.flush()
    return store


def add_product(session, chain, name, sale, original, scraped_at=None,
                url=None):
    """A product with one price snapshot, stamped with the chain's scrape."""
    when = scraped_at or chain.last_scraped_at or utcnow()
    product = Product(chain_id=chain.id, name=name,
                      normalized_name=normalize_name(name), product_url=url,
                      first_seen_at=when, last_seen_at=when)
    session.add(product)
    session.flush()
    session.add(PriceSnapshot(product_id=product.id, sale_price=sale,
                              original_price=original, scraped_at=when))
    session.flush()
    return product


@pytest.fixture
def city(session):
    """One city's worth of chains, covering every status."""
    chains = {}

    hm = make_chain(session, "hm.com", "H&M", scraped_ago=timedelta(hours=1))
    add_store(session, hm, "H&M Newbury")
    add_store(session, hm, "H&M Downtown", place_id="pid-hm-2")
    add_product(session, hm, "Linen Shirt", 20.0, 80.0)       # 75% off
    add_product(session, hm, "Wool Coat", 60.0, 100.0)        # 40% off
    add_product(session, hm, "Plain Socks", 5.0, None)        # no discount
    session.add(Promotion(chain_id=hm.id, text="20% off everything",
                          scraped_at=hm.last_scraped_at))
    chains["hm"] = hm

    gap = make_chain(session, "gap.com", "Gap", scraped_ago=timedelta(hours=2))
    add_store(session, gap, "Gap Boston")
    add_product(session, gap, "Denim Jacket", 45.0, 90.0)     # 50% off
    chains["gap"] = gap

    tjx = make_chain(session, "tjmaxx.com", "T.J. Maxx", status=STATUS_BLOCKED,
                     scraped_ago=timedelta(days=1),
                     status_checked_ago=timedelta(days=1), sale_url=None,
                     stage=None)
    add_store(session, tjx, "T.J. Maxx Boston")
    chains["tjx"] = tjx

    primark = make_chain(session, "primark.com", "Primark",
                         status=STATUS_NO_SALE_PAGE,
                         scraped_ago=timedelta(days=2),
                         status_checked_ago=timedelta(days=2), sale_url=None,
                         stage=None)
    add_store(session, primark, "Primark Downtown Crossing")
    chains["primark"] = primark

    # Found by a search but never scraped: no last_scraped_at at all.
    fresh = make_chain(session, "zara.com", "Zara", sale_url=None, stage=None)
    add_store(session, fresh, "Zara Boston")
    chains["zara"] = fresh

    # A different city, to prove the location filter actually filters.
    other = make_chain(session, "uniqlo.com", "Uniqlo",
                       scraped_ago=timedelta(hours=1))
    add_store(session, other, "Uniqlo Cambridge", address=CAMBRIDGE)
    add_product(session, other, "Fleece", 15.0, 30.0)
    chains["uniqlo"] = other

    session.flush()
    return chains


# --- /health ----------------------------------------------------------------
class TestHealth:
    def test_reports_ok_and_counts_rows(self, client, city):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        assert body["chains"] == 6
        assert body["stores"] == 7
        assert body["last_scrape_at"] is not None

    def test_empty_database_is_healthy_but_says_it_is_empty(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["chains"] == 0
        assert body["last_scrape_at"] is None


# --- /cities/{city}/deals -----------------------------------------------------
class TestCityDeals:
    def test_returns_chains_with_their_deals_and_locations(self, client, city):
        body = client.get("/cities/Boston/deals").json()
        keys = [c["key"] for c in body["items"]]
        assert "hm.com" in keys and "gap.com" in keys
        assert "uniqlo.com" not in keys, "Cambridge is a different city"

        hm = next(c for c in body["items"] if c["key"] == "hm.com")
        assert hm["name"] == "H&M"
        assert hm["status"] == "ok"
        assert hm["store_count"] == 2
        assert len(hm["stores"]) == 2
        assert hm["promotions"] == ["20% off everything"]
        assert hm["deal_count"] == 3
        assert hm["last_scraped_at"] is not None

    def test_chains_with_deals_sort_ahead_of_empty_ones(self, client, city):
        body = client.get("/cities/Boston/deals").json()
        with_deals = [bool(c["deals"] or c["promotions"]) for c in body["items"]]
        assert with_deals == sorted(with_deals, reverse=True), (
            "chains with nothing to show should sink to the bottom")

    def test_every_status_is_reported(self, client, city):
        body = client.get("/cities/Boston/deals").json()
        statuses = {c["key"]: c["status"] for c in body["items"]}
        assert statuses["tjmaxx.com"] == "blocked"
        assert statuses["primark.com"] == "no_sale_page"
        assert statuses["hm.com"] == "ok"

    def test_a_dead_end_says_when_it_is_next_checked(self, client, city):
        body = client.get("/cities/Boston/deals").json()
        tjx = next(c for c in body["items"] if c["key"] == "tjmaxx.com")
        assert tjx["recheck_due_at"] is not None
        hm = next(c for c in body["items"] if c["key"] == "hm.com")
        assert hm["recheck_due_at"] is None, "a healthy chain has none pending"

    def test_freshness_follows_the_database_rule(self, client, session, city):
        body = client.get("/cities/Boston/deals").json()
        hm = next(c for c in body["items"] if c["key"] == "hm.com")
        assert hm["is_fresh"] is True, "scraped an hour ago, window is 6h"

        city["hm"].last_scraped_at = utcnow() - db.DEALS_FRESH_FOR - timedelta(minutes=1)
        session.flush()
        body = client.get("/cities/Boston/deals").json()
        hm = next(c for c in body["items"] if c["key"] == "hm.com")
        assert hm["is_fresh"] is False

    def test_include_empty_false_drops_chains_with_nothing(self, client, city):
        body = client.get("/cities/Boston/deals?include_empty=false").json()
        keys = {c["key"] for c in body["items"]}
        assert keys == {"hm.com", "gap.com"}
        assert body["pagination"]["total"] == 2, (
            "total must count the filtered set, not the unfiltered one")

    def test_deals_per_chain_caps_the_sample(self, client, city):
        body = client.get("/cities/Boston/deals?deals_per_chain=1").json()
        hm = next(c for c in body["items"] if c["key"] == "hm.com")
        assert len(hm["deals"]) == 1
        assert hm["deal_count"] == 3, "the cap must not hide the real count"

    def test_state_and_zip_narrow_the_search(self, client, city):
        assert client.get("/cities/Boston/deals?state=MA").json()["items"]
        assert client.get("/cities/Boston/deals?state=CA").json()["items"] == []
        assert client.get("/cities/Boston/deals?zip=02116").json()["items"]
        assert client.get("/cities/Boston/deals?zip=99999").json()["items"] == []

    def test_unknown_city_is_an_empty_list_not_a_404(self, client, city):
        res = client.get("/cities/Nowhere/deals")
        assert res.status_code == 200
        assert res.json()["items"] == []
        assert res.json()["pagination"]["total"] == 0

    def test_location_echoes_the_parts_as_places_reads_them(self, client, city):
        body = client.get("/cities/Boston/deals?state=MA&zip=02116").json()
        assert body["location"] == "Boston, MA 02116"

    def test_pagination_walks_the_chains(self, client, city):
        first = client.get("/cities/Boston/deals?limit=2&offset=0").json()
        second = client.get("/cities/Boston/deals?limit=2&offset=2").json()
        assert len(first["items"]) == 2
        assert first["pagination"]["total"] == 5
        assert first["pagination"]["has_more"] is True
        assert {c["key"] for c in first["items"]}.isdisjoint(
            {c["key"] for c in second["items"]})

    def test_a_wildcard_in_the_city_is_not_a_wildcard(self, client, city):
        assert client.get("/cities/%/deals").json()["items"] == []

    def test_bad_parameters_are_400(self, client, city):
        for url in ("/cities/Boston/deals?limit=0",
                    "/cities/Boston/deals?limit=5000",
                    "/cities/Boston/deals?offset=-1",
                    "/cities/Boston/deals?deals_per_chain=-1"):
            res = client.get(url)
            assert res.status_code == 400, url
            assert res.json()["error"]["code"] == "bad_request"
            assert res.json()["error"]["detail"], "should name the bad field"


# --- /chains/{chain_key}/deals -------------------------------------------------
class TestChainDeals:
    def test_returns_the_chains_current_deals(self, client, city):
        body = client.get("/chains/hm.com/deals").json()
        assert body["chain"]["key"] == "hm.com"
        assert body["chain"]["status"] == "ok"
        assert body["promotions"] == ["20% off everything"]
        assert {item["name"] for item in body["items"]} == {
            "Linen Shirt", "Wool Coat", "Plain Socks"}

    def test_discount_is_computed_and_null_without_an_original_price(
            self, client, city):
        body = client.get("/chains/hm.com/deals").json()
        by_name = {item["name"]: item for item in body["items"]}
        assert by_name["Linen Shirt"]["discount_pct"] == 75
        assert by_name["Wool Coat"]["discount_pct"] == 40
        assert by_name["Plain Socks"]["discount_pct"] is None
        assert by_name["Plain Socks"]["original_price"] is None

    def test_sorts_by_discount_with_undiscounted_items_last(self, client, city):
        body = client.get("/chains/hm.com/deals?sort=discount").json()
        assert [i["name"] for i in body["items"]] == [
            "Linen Shirt", "Wool Coat", "Plain Socks"]

    def test_sorts_by_price(self, client, city):
        asc = client.get("/chains/hm.com/deals?sort=price_asc").json()
        assert [i["sale_price"] for i in asc["items"]] == [5.0, 20.0, 60.0]
        desc = client.get("/chains/hm.com/deals?sort=price_desc").json()
        assert [i["sale_price"] for i in desc["items"]] == [60.0, 20.0, 5.0]

    def test_min_discount_filters_and_excludes_items_without_one(
            self, client, city):
        body = client.get("/chains/hm.com/deals?min_discount=50").json()
        assert [i["name"] for i in body["items"]] == ["Linen Shirt"]
        assert body["pagination"]["total"] == 1, (
            "an item with no original price is not 0% off, it is unrankable")

    def test_filtering_and_sorting_happen_before_paging(self, client, city):
        """The deepest markdown must survive a limit of one."""
        body = client.get("/chains/hm.com/deals?sort=discount&limit=1").json()
        assert [i["name"] for i in body["items"]] == ["Linen Shirt"]
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["has_more"] is True

    def test_only_the_latest_scrape_counts(self, client, session, city):
        """A product that stopped appearing is no longer a deal."""
        hm = city["hm"]
        old = add_product(session, hm, "Last Season Parka", 10.0, 100.0,
                          scraped_at=hm.last_scraped_at - timedelta(days=3))
        session.flush()
        names = [i["name"] for i in client.get("/chains/hm.com/deals").json()["items"]]
        assert old.name not in names

    def test_a_re_scrape_shows_the_newest_price(self, client, session, city):
        hm = city["hm"]
        product = session.query(Product).filter_by(name="Wool Coat").one()
        session.add(PriceSnapshot(product_id=product.id, sale_price=30.0,
                                  original_price=100.0,
                                  scraped_at=hm.last_scraped_at + timedelta(minutes=1)))
        session.flush()
        body = client.get("/chains/hm.com/deals").json()
        coat = next(i for i in body["items"] if i["name"] == "Wool Coat")
        assert coat["sale_price"] == 30.0
        assert coat["discount_pct"] == 70

    def test_a_full_host_resolves_to_the_same_chain(self, client, city):
        assert client.get("/chains/www.hm.com/deals").json()["chain"]["key"] == "hm.com"
        assert client.get(
            "/chains/https:%2F%2Fwww.hm.com%2F/deals").status_code in (200, 404)

    def test_unknown_chain_is_404_with_a_clear_error(self, client, city):
        res = client.get("/chains/nosuchshop.com/deals")
        assert res.status_code == 404
        body = res.json()
        assert body["error"]["code"] == "not_found"
        assert "nosuchshop.com" in body["error"]["message"]
        assert "error" in body and "detail" not in body["error"].get("message", "")

    def test_a_blocked_chain_is_200_with_no_deals_and_a_reason(self, client, city):
        res = client.get("/chains/tjmaxx.com/deals")
        assert res.status_code == 200
        body = res.json()
        assert body["items"] == []
        assert body["chain"]["status"] == "blocked"
        assert body["chain"]["recheck_due_at"] is not None

    def test_a_never_scraped_chain_is_200_with_no_deals(self, client, city):
        body = client.get("/chains/zara.com/deals").json()
        assert body["items"] == []
        assert body["chain"]["last_scraped_at"] is None
        assert body["chain"]["is_fresh"] is False

    def test_bad_parameters_are_400(self, client, city):
        for url in ("/chains/hm.com/deals?sort=sideways",
                    "/chains/hm.com/deals?min_discount=-5",
                    "/chains/hm.com/deals?min_discount=101",
                    "/chains/hm.com/deals?limit=0"):
            res = client.get(url)
            assert res.status_code == 400, url
            assert res.json()["error"]["code"] == "bad_request"

    def test_an_unknown_chain_with_bad_parameters_reports_the_parameters(
            self, client, city):
        """Validation runs before the handler, so the 400 wins over the 404."""
        res = client.get("/chains/nosuchshop.com/deals?sort=sideways")
        assert res.status_code == 400


# --- /stores -------------------------------------------------------------------
class TestStores:
    def test_returns_locations_with_coordinates_and_their_chain(self, client, city):
        body = client.get("/stores?city=Boston").json()
        assert body["items"], "expected Boston stores"
        for item in body["items"]:
            assert item["lat"] is not None and item["lng"] is not None
            assert item["chain_key"]
            assert item["chain_status"] in {"ok", "blocked", "no_sale_page",
                                            "error"}

    def test_filters_by_city_and_by_chain(self, client, city):
        boston = client.get("/stores?city=Boston").json()
        assert "uniqlo.com" not in {i["chain_key"] for i in boston["items"]}

        cambridge = client.get("/stores?city=Cambridge").json()
        assert {i["chain_key"] for i in cambridge["items"]} == {"uniqlo.com"}

        one_chain = client.get("/stores?chain_key=hm.com").json()
        assert {i["chain_key"] for i in one_chain["items"]} == {"hm.com"}
        assert len(one_chain["items"]) == 2

    def test_stores_without_coordinates_are_left_out_by_default(
            self, client, session, city):
        """clear_stale_place_details nulls coordinates once Google's caching
        window is up, and a map pin needs somewhere to go."""
        add_store(session, city["gap"], "Gap Unplaced", lat=None, lng=None,
                  place_id="pid-gap-unplaced")
        session.flush()

        default = client.get("/stores?city=Boston").json()
        assert "Gap Unplaced" not in {i["name"] for i in default["items"]}

        everything = client.get(
            "/stores?city=Boston&with_coordinates=false").json()
        assert "Gap Unplaced" in {i["name"] for i in everything["items"]}

    def test_pagination_reports_the_full_total(self, client, city):
        body = client.get("/stores?limit=2").json()
        assert len(body["items"]) == 2
        assert body["pagination"]["total"] == 7
        assert body["pagination"]["has_more"] is True

        last = client.get("/stores?limit=2&offset=6").json()
        assert last["pagination"]["has_more"] is False

    def test_no_filters_returns_every_placed_store(self, client, city):
        body = client.get("/stores").json()
        assert body["pagination"]["total"] == 7

    def test_bad_parameters_are_400(self, client, city):
        res = client.get("/stores?limit=-1")
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "bad_request"


class TestStoresWithinRadius:
    """Coordinates are seeded at real places so the distances mean something.

    The `city` fixture puts every Boston store at the same point, which would
    hide an ordering bug, so these seed their own spread of locations.
    """

    @pytest.fixture
    def pins(self, session, city):
        """Four stores at known distances from Boston Common."""
        from tests.test_api import add_store  # the module's own helper

        places = {
            "Common": (42.3550, -71.0656, 0.0),      # the origin itself
            "Fenway": (42.3467, -71.0972, 1.7),
            "Harvard": (42.3736, -71.1190, 3.0),
            "Providence": (41.8240, -71.4128, 41.0),
        }
        for name, (lat, lng, _expected) in places.items():
            add_store(session, city["gap"], f"Gap {name}", lat=lat, lng=lng,
                      place_id=f"pid-pin-{name}")
        session.flush()
        return places

    ORIGIN = "lat=42.3550&lng=-71.0656"

    def test_returns_only_stores_inside_the_circle(self, client, pins):
        body = client.get(f"/stores?{self.ORIGIN}&radius_miles=5").json()
        names = {i["name"] for i in body["items"]}
        assert "Gap Common" in names and "Gap Fenway" in names
        assert "Gap Harvard" in names
        assert "Gap Providence" not in names, "41 miles away, radius was 5"

    def test_a_wider_radius_reaches_further(self, client, pins):
        body = client.get(f"/stores?{self.ORIGIN}&radius_miles=50").json()
        assert "Gap Providence" in {i["name"] for i in body["items"]}

    def test_results_are_nearest_first(self, client, pins):
        body = client.get(f"/stores?{self.ORIGIN}&radius_miles=50").json()
        distances = [i["distance_miles"] for i in body["items"]]
        assert distances == sorted(distances)

    def test_distance_miles_is_reported_and_about_right(self, client, pins):
        body = client.get(f"/stores?{self.ORIGIN}&radius_miles=50").json()
        measured = {i["name"]: i["distance_miles"] for i in body["items"]}
        for name, (_lat, _lng, expected) in pins.items():
            actual = measured[f"Gap {name}"]
            tolerance = 1.0 if expected < 10 else 2.0
            assert abs(actual - expected) < tolerance, (
                f"Gap {name}: expected about {expected} mi, got {actual}")

    def test_distance_is_null_without_a_radius_search(self, client, pins):
        body = client.get("/stores?city=Boston").json()
        assert all(i["distance_miles"] is None for i in body["items"])

    def test_the_nearest_page_is_the_nearest_overall(self, client, pins):
        """Ordering has to be decided across every store in range, not within
        whichever page came back first. Asserted against the unpaged answer
        rather than named stores, so seeding another shop into the fixture
        cannot quietly turn this into a tautology."""
        everything = client.get(
            f"/stores?{self.ORIGIN}&radius_miles=50&limit=200").json()
        first_page = client.get(
            f"/stores?{self.ORIGIN}&radius_miles=50&limit=2").json()

        assert [i["place_id"] for i in first_page["items"]] ==             [i["place_id"] for i in everything["items"]][:2]
        assert first_page["items"][0]["name"] == "Gap Common", (
            "the store at the origin itself must come first")
        assert first_page["pagination"]["total"] == everything["pagination"]["total"]
        assert first_page["pagination"]["has_more"] is True

    def test_paging_walks_outwards_without_repeating(self, client, pins):
        first = client.get(f"/stores?{self.ORIGIN}&radius_miles=50&limit=2").json()
        second = client.get(
            f"/stores?{self.ORIGIN}&radius_miles=50&limit=2&offset=2").json()
        assert {i["place_id"] for i in first["items"]}.isdisjoint(
            {i["place_id"] for i in second["items"]})
        assert (max(i["distance_miles"] for i in first["items"])
                <= min(i["distance_miles"] for i in second["items"]))

    def test_total_counts_the_circle_not_the_bounding_box(self, client, pins):
        """A store in the box's corner but outside the circle must not be
        counted. At 42N a 5-mile box corner is about 7 miles out."""
        body = client.get(f"/stores?{self.ORIGIN}&radius_miles=5").json()
        assert body["pagination"]["total"] == len(
            [i for i in body["items"] if i["distance_miles"] <= 5])
        assert all(i["distance_miles"] <= 5 for i in body["items"])

    def test_a_corner_of_the_bounding_box_is_excluded(self, client, session,
                                                      city, pins):
        """Belt and braces for the box being a superset: a point diagonally
        out at 1.4x the radius sits inside the box and outside the circle."""
        add_store(session, city["gap"], "Gap Corner", lat=42.3550 + 0.0724,
                  lng=-71.0656 + 0.0978, place_id="pid-pin-corner")
        session.flush()
        body = client.get(f"/stores?{self.ORIGIN}&radius_miles=5").json()
        assert "Gap Corner" not in {i["name"] for i in body["items"]}

    def test_it_combines_with_the_other_filters(self, client, pins):
        body = client.get(
            f"/stores?{self.ORIGIN}&radius_miles=50&chain_key=hm.com").json()
        assert {i["chain_key"] for i in body["items"]} == {"hm.com"}

    def test_stores_without_coordinates_cannot_be_measured(self, client,
                                                           session, city, pins):
        add_store(session, city["gap"], "Gap Unplaced", lat=None, lng=None,
                  place_id="pid-pin-unplaced")
        session.flush()
        body = client.get(f"/stores?{self.ORIGIN}&radius_miles=500").json()
        assert "Gap Unplaced" not in {i["name"] for i in body["items"]}

    @pytest.mark.parametrize("query", [
        "lat=42.355",
        "lng=-71.0656",
        "radius_miles=5",
        "lat=42.355&lng=-71.0656",
        "lat=42.355&radius_miles=5",
    ])
    def test_a_partial_radius_search_is_400(self, client, pins, query):
        """Ignoring a lone lat would answer a different question and look like
        the filter had simply done nothing."""
        res = client.get(f"/stores?{query}")
        assert res.status_code == 400
        body = res.json()
        assert body["error"]["code"] == "bad_request"
        assert "radius" in body["error"]["message"].lower()

    @pytest.mark.parametrize("query", [
        "lat=91&lng=0&radius_miles=5",
        "lat=0&lng=181&radius_miles=5",
        "lat=42.355&lng=-71.0656&radius_miles=0",
        "lat=42.355&lng=-71.0656&radius_miles=-5",
        "lat=42.355&lng=-71.0656&radius_miles=100000",
    ])
    def test_out_of_range_coordinates_are_400(self, client, pins, query):
        res = client.get(f"/stores?{query}")
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "bad_request"

    def test_an_empty_circle_is_an_empty_list(self, client, pins):
        """Mid-Atlantic, far from anything seeded."""
        body = client.get("/stores?lat=30&lng=-40&radius_miles=50").json()
        assert body["items"] == []
        assert body["pagination"]["total"] == 0


# --- Cross-cutting ---------------------------------------------------------------
class TestApiContract:
    def test_cors_allows_the_react_dev_server(self, client, city):
        res = client.get("/stores", headers={"Origin": "http://localhost:5173"})
        assert res.headers.get("access-control-allow-origin") == \
            "http://localhost:5173"

    def test_cors_origins_come_from_the_environment(self, monkeypatch):
        from api import config

        monkeypatch.setenv("CORS_ORIGINS",
                           "https://deals.example.com, http://localhost:3000")
        assert config.cors_origins() == ["https://deals.example.com",
                                         "http://localhost:3000"]
        monkeypatch.delenv("CORS_ORIGINS")
        assert config.cors_origins() == [config.DEFAULT_DEV_ORIGIN]

    def test_the_api_never_writes(self, client, session, city):
        """Every endpoint, then a check that nothing was added or changed."""
        before = (session.query(Chain).count(), session.query(Store).count(),
                  session.query(Product).count(),
                  session.query(PriceSnapshot).count())
        for url in ("/health", "/cities/Boston/deals", "/chains/hm.com/deals",
                    "/stores?city=Boston"):
            assert client.get(url).status_code == 200
        after = (session.query(Chain).count(), session.query(Store).count(),
                 session.query(Product).count(),
                 session.query(PriceSnapshot).count())
        assert before == after

    def test_docs_and_schema_are_served(self, client):
        assert client.get("/docs").status_code == 200
        spec = client.get("/openapi.json").json()
        assert set(spec["paths"]) == {
            "/health", "/cities/{city}/deals", "/chains/{chain_key}/deals",
            "/stores"}

    def test_every_status_the_scraper_can_set_is_in_the_documented_enum(
            self, client):
        """A status added to db.py must not go missing from the API's docs."""
        from api.schemas import ChainStatus

        documented = {member.value for member in ChainStatus}
        assert documented == {STATUS_OK, STATUS_NO_SALE_PAGE, STATUS_BLOCKED,
                              STATUS_ERROR}

    def test_an_unexpected_failure_is_not_a_stack_trace(self, session, city,
                                                        monkeypatch):
        """A client of its own, because letting the exception through to the
        handler means telling TestClient not to re-raise it at the test."""
        from fastapi.testclient import TestClient

        from api import queries
        from api.deps import get_session
        from api.main import app

        def boom(*args, **kwargs):
            raise RuntimeError("database on fire")

        monkeypatch.setattr(queries, "count_of", boom)
        app.dependency_overrides[get_session] = lambda: session
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                res = client.get("/stores")
        finally:
            app.dependency_overrides.clear()

        assert res.status_code == 500
        assert res.json()["error"]["code"] == "internal_error"
        assert "fire" not in res.text and "Traceback" not in res.text
