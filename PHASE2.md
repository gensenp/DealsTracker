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
