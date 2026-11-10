"""Distance maths for "shops near me".

Ported from the distance half of storeLocations.py on the extraction branch.
The geocoding half did not come with it and is not needed: that module existed
because the old flat scripts only had addresses as text, so every address had
to be sent to Nominatim and cached on disk before anything could be measured.
Store rows here already carry lat/lng from the Places lookup, so there is
nothing to geocode and no rate limit to respect -- which is also why geopy is
not a dependency of this project.

Nothing here touches the network or the database, so it stays cheap to test.
"""
from __future__ import annotations

import math

# Mean Earth radius. The figure matters less than using one consistently: over
# the tens of miles a shopper cares about, the difference between radii is a
# few feet.
EARTH_RADIUS_MILES = 3958.7613

# One degree of latitude, anywhere. Longitude is not constant, which is the
# whole difficulty in bounding_box below.
MILES_PER_DEGREE_LAT = EARTH_RADIUS_MILES * math.pi / 180

# How far a radius search may reach. A cap keeps one request from asking for
# every store in the database by way of a 10,000-mile circle.
MAX_RADIUS_MILES = 500.0


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    # Clamped because floating point can hand asin a value a hair above 1 for
    # two points at opposite ends of the Earth, which would raise instead of
    # returning half a circumference.
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, a)))


def bounding_box(lat: float, lng: float, miles: float
                 ) -> tuple[float, float, float, float]:
    """A latitude/longitude box that fully contains the circle of that radius.

    Returned as (min_lat, max_lat, min_lng, max_lng), for narrowing a query
    before exact distances are worked out. Plain arithmetic on purpose: the
    comparison goes into SQL, and trigonometry in SQL is not portable --
    PostgreSQL always has sin/cos/radians, but SQLite only has them when it was
    compiled with SQLITE_ENABLE_MATH_FUNCTIONS, which not every build is.

    The box is deliberately a superset, never a tight fit. A degree of
    longitude shrinks towards the poles, so the width is taken at whichever
    edge of the box lies nearest a pole; using the centre latitude instead
    would make the box too narrow at that edge and lose shops inside the
    circle. Everything the box lets through is measured exactly afterwards, so
    being generous costs a few extra rows and nothing else.
    """
    d_lat = miles / MILES_PER_DEGREE_LAT

    worst_lat = min(89.9, abs(lat) + d_lat)
    cos_lat = math.cos(math.radians(worst_lat))
    if cos_lat < 1e-6:
        d_lng = 180.0  # the circle reaches a pole; every longitude is in it
    else:
        d_lng = min(180.0, miles / (MILES_PER_DEGREE_LAT * cos_lat))

    return (max(-90.0, lat - d_lat), min(90.0, lat + d_lat),
            max(-180.0, lng - d_lng), min(180.0, lng + d_lng))
