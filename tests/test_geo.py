"""Tests for the distance maths behind "shops near me".

Distances are checked against known real-world pairs rather than against the
formula restated, which would only prove the code matches itself. A few tenths
of a mile of tolerance covers the choice of Earth radius.
"""
import math

import pytest

import geo

# Landmarks with well-known separations, as (lat, lng).
BOSTON_COMMON = (42.3550, -71.0656)
FENWAY_PARK = (42.3467, -71.0972)          # ~1.7 mi from the Common
HARVARD_SQUARE = (42.3736, -71.1190)       # ~3.0 mi
PROVIDENCE_RI = (41.8240, -71.4128)        # ~41 mi
SAN_FRANCISCO = (37.7749, -122.4194)       # ~2700 mi


class TestHaversine:
    @pytest.mark.parametrize("origin,target,expected,tolerance", [
        (BOSTON_COMMON, FENWAY_PARK, 1.7, 0.2),
        (BOSTON_COMMON, HARVARD_SQUARE, 3.0, 0.2),
        (BOSTON_COMMON, PROVIDENCE_RI, 41.0, 1.0),
        (BOSTON_COMMON, SAN_FRANCISCO, 2700.0, 25.0),
    ])
    def test_matches_known_distances(self, origin, target, expected, tolerance):
        actual = geo.haversine_miles(*origin, *target)
        assert abs(actual - expected) < tolerance, (
            f"expected about {expected} miles, got {actual:.1f}")

    def test_a_point_is_no_distance_from_itself(self):
        assert geo.haversine_miles(*BOSTON_COMMON, *BOSTON_COMMON) == 0

    def test_it_is_symmetric(self):
        there = geo.haversine_miles(*BOSTON_COMMON, *PROVIDENCE_RI)
        back = geo.haversine_miles(*PROVIDENCE_RI, *BOSTON_COMMON)
        assert there == pytest.approx(back)

    def test_antipodes_do_not_raise(self):
        """Floating point can hand asin a value a hair over 1 for two points on
        opposite sides of the Earth, which would raise a ValueError."""
        distance = geo.haversine_miles(0.0, 0.0, 0.0, 180.0)
        half_circumference = math.pi * geo.EARTH_RADIUS_MILES
        assert distance == pytest.approx(half_circumference, rel=1e-6)


class TestBoundingBox:
    def test_contains_the_circle_it_describes(self):
        """Every point exactly at the radius must fall inside the box, in all
        directions -- that is the box's whole job."""
        lat, lng = BOSTON_COMMON
        miles = 25.0
        min_lat, max_lat, min_lng, max_lng = geo.bounding_box(lat, lng, miles)

        for bearing in range(0, 360, 5):
            # Step out to the radius along this bearing.
            theta = math.radians(bearing)
            d_lat = (miles / geo.MILES_PER_DEGREE_LAT) * math.cos(theta)
            d_lng = ((miles / geo.MILES_PER_DEGREE_LAT)
                     / math.cos(math.radians(lat))) * math.sin(theta)
            point_lat, point_lng = lat + d_lat, lng + d_lng
            assert min_lat <= point_lat <= max_lat, f"bearing {bearing}"
            assert min_lng <= point_lng <= max_lng, f"bearing {bearing}"

    def test_a_bigger_radius_makes_a_bigger_box(self):
        small = geo.bounding_box(42.35, -71.06, 5)
        large = geo.bounding_box(42.35, -71.06, 50)
        assert large[0] < small[0] and large[1] > small[1]
        assert large[2] < small[2] and large[3] > small[3]

    def test_longitude_widens_towards_the_poles(self):
        """A degree of longitude is ~69 miles at the equator and ~35 at 60
        degrees north, so the same radius needs a wider box further north."""
        equator = geo.bounding_box(0.0, 0.0, 50)
        northern = geo.bounding_box(60.0, 0.0, 50)
        assert (northern[3] - northern[2]) > (equator[3] - equator[2])

    def test_it_never_leaves_valid_coordinates(self):
        for lat, lng, miles in [(89.5, 0.0, 200), (-89.5, 0.0, 200),
                                (0.0, 179.5, 200), (0.0, -179.5, 200)]:
            min_lat, max_lat, min_lng, max_lng = geo.bounding_box(lat, lng, miles)
            assert -90.0 <= min_lat <= max_lat <= 90.0
            assert -180.0 <= min_lng <= max_lng <= 180.0

    def test_a_circle_reaching_the_pole_spans_every_longitude(self):
        _, _, min_lng, max_lng = geo.bounding_box(89.9, 0.0, 100)
        assert min_lng == -180.0 and max_lng == 180.0
