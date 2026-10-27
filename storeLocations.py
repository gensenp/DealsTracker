"""Geocoding and distance helpers for the map view.

Addresses come back from the Places lookup as text, so they have to be turned
into coordinates before anything can be plotted or filtered by distance. The
results are cached to disk (store_locations.json) and looked up at most once per
address, for two reasons: Nominatim's usage policy allows roughly one request a
second and asks that results be cached, and a city search can return twenty
stores whose addresses never change between runs.

Nothing here scrapes or touches the deal pipeline.
"""
import json
import math
import os
import threading
import time

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "store_locations.json")

# Nominatim asks for an identifying user agent and at most one request per
# second. geopy ships a RateLimiter for this, but the cache means we rarely hit
# the network at all, so a plain lock plus a sleep keeps the dependency surface
# small and the policy obviously satisfied.
USER_AGENT = "DealsTracker/1.0 (github.com/gensenp/DealsTracker)"
MIN_SECONDS_BETWEEN_CALLS = 1.1

# Bare ZIP codes are wildly ambiguous worldwide -- unbiased, Nominatim places
# "02119" (Roxbury, MA) in Vilnius, Lithuania. The store search is US-based, so
# constrain geocoding to match. Widen this if the app ever covers other countries.
COUNTRY_CODES = "us"

EARTH_RADIUS_MILES = 3958.7613

_lock = threading.Lock()
_last_call = 0.0
_cache = None


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def _load_cache():
    """Address -> {"lat": float, "lon": float} or None for a known failure."""
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                _cache = json.load(fh)
        except (OSError, ValueError):
            _cache = {}  # missing or corrupt -> start fresh rather than crash
    return _cache


def _save_cache():
    tmp = CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_cache, fh, indent=2, sort_keys=True)
        os.replace(tmp, CACHE_PATH)  # atomic, so a crash can't truncate the cache
    except OSError:
        pass  # a cache we cannot persist is still usable in memory


def geocode(address, timeout=10):
    """Coordinates for `address` as (lat, lon), or None if it can't be placed.

    Cached permanently, including failures: a street address that Nominatim
    cannot resolve this time will not resolve on the next page load either, and
    retrying it would spend the rate limit for nothing."""
    global _last_call
    address = (address or "").strip()
    if not address:
        return None

    cache = _load_cache()
    if address in cache:
        hit = cache[address]
        return (hit["lat"], hit["lon"]) if hit else None

    from geopy.geocoders import Nominatim  # imported lazily; only the map needs it
    from geopy.exc import GeocoderServiceError

    with _lock:
        wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            location = Nominatim(user_agent=USER_AGENT).geocode(
                address, timeout=timeout, country_codes=COUNTRY_CODES)
        except (GeocoderServiceError, OSError):
            return None  # transient: not cached, so a later run can retry
        finally:
            _last_call = time.monotonic()

    cache[address] = ({"lat": location.latitude, "lon": location.longitude}
                      if location else None)
    _save_cache()
    return (location.latitude, location.longitude) if location else None


def locate_stores(stores, progress=None):
    """Split `stores` into those that can be placed on a map and those that
    can't.

    Returns (located, unlocated). Each located store gains "lat" and "lon".
    Unlocated ones are the online-only or unresolvable addresses, which the
    caller lists separately rather than silently dropping."""
    located, unlocated = [], []
    for i, store in enumerate(stores):
        coords = geocode(store.get("address", ""))
        if progress:
            progress(i + 1, len(stores), store.get("name", ""))
        if coords:
            located.append({**store, "lat": coords[0], "lon": coords[1]})
        else:
            unlocated.append(store)
    return located, unlocated


def within_radius(stores, origin, miles):
    """Stores within `miles` of (lat, lon), each with "distance_miles" set,
    nearest first."""
    lat, lon = origin
    near = []
    for store in stores:
        distance = haversine(lat, lon, store["lat"], store["lon"])
        if distance <= miles:
            near.append({**store, "distance_miles": distance})
    return sorted(near, key=lambda s: s["distance_miles"])
