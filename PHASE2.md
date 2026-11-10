# Phase 2 backlog

Things phase 1 deliberately left alone. Each entry says what is wrong now and
what "done" looks like, so none of it has to be rediscovered later.

## Cleanup

### Move `build_location` off the scraping module

`api/routers/cities.py` imports `build_location` from `DealsTracker.py`, which
means starting the read-only API pulls in `groq`, the Playwright-facing
`browser` module and both per-chain scrapers — none of which it can use, since
it never scrapes.

It is real reuse rather than a reimplementation, and it became safe once
credentials moved to being read on use, so it was kept as-is in phase 1. But it
is a heavier import graph than a read layer needs, and it slows API startup for
no benefit.

**Done looks like:** `build_location` (and anything else the API borrows that
is pure string or location handling) lives in a module with no network or LLM
dependencies — a `locations.py` alongside `deals.py`, say. `DealsTracker.py`
and the API both import it from there. No behaviour change, and the Streamlit
app keeps working.

## Data model

### Store parsed city, state and postal code

`Store` holds Google's `formattedAddress` ("123 Newbury St, Boston, MA 02116,
USA") and nothing broken out of it, so every location filter in the API matches
inside that string — `api/queries.py:address_filters`.

That is approximate in both directions. "Boston" also matches "South Boston",
and a city whose name turns up in a street name can match the street instead of
the city. Narrowing with `state` or `zip` settles most of it, which is why the
parameters exist, but a shopper searching a city should not have to.

The data is already available and being thrown away: the Places API returns
`addressComponents` alongside `formattedAddress`, and `find_clothing_stores`
asks for neither beyond the formatted string.

This is the first real migration. Alembic is already set up and the initial
revision matches the current schema, so this starts with `alembic revision
--autogenerate -m "parsed address columns on stores"` once the model is
changed — and `tests/test_migrations.py` fails until that migration exists.

**Done looks like:** `Store` gains nullable `city`, `state` and `postal_code`
columns. `find_clothing_stores` requests `places.addressComponents` and
`upsert_stores` writes the parsed values; the Places field mask change needs
checking against the billing tier, since the existing mask is deliberately
scoped to Essentials-plus-Pro. `address_filters` switches to exact column
matches, falling back to the current `ILIKE` while rows predate the change.
Existing rows backfill on their next sighting, since `upsert_stores` already
rewrites every detail each time a search finds a shop.

Two things to decide when doing it: whether a one-off backfill is worth writing
for rows that may not be searched again soon, and whether `GET /cities` becomes
worth having once cities are a real column rather than a substring.
