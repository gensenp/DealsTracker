# DealsTracker

Find clothing stores in a city and see what's actually on sale.

Enter a city (optionally narrowed by state or ZIP), and DealsTracker looks up
nearby clothing stores through Google Places, finds each retailer's sale
section, and extracts the markdowns — item names with both prices, plus any
storewide promotions. Results are saved, so searching the same city again reads
from the database instead of scraping the web a second time.

```
H&M                                    updated 4 minutes ago · 2 locations
  Buy 2 Get 1 Free
  REGULAR-FIT TRACK SHORTS — $10.99  $39.99 · 73% off
  OVERSIZED FIT PRINTED HOODIE — $17.99  $49.99 · 64% off
```

A first search of a city takes a few minutes. The next one takes about a second.

## Quick start

```bash
git clone https://github.com/gensenp/DealsTracker.git
cd DealsTracker

python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env             # then fill in your two API keys
python -m streamlit run app.py
```

Python 3.11 or newer. `playwright install chromium` is required and is **not**
covered by `pip install` — without it, every site that needs a browser silently
returns no deals.

### API keys

Both are required, in `.env` (which is gitignored):

| Key | Where | Notes |
|---|---|---|
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) | Free tier works, with one caveat — see [Known limits](#known-limits) |
| `GOOGLE_PLACES_API_KEY` | [console.cloud.google.com](https://console.cloud.google.com) | Enable **Places API (New)**, not the legacy Places API. Needs a billing account even within free usage. |

If Places returns `403 PERMISSION_DENIED`, it's almost always one of: the New
API isn't enabled, billing isn't set up, or the key has **Application
restrictions** blocking the machine making the call.

### Database

`DATABASE_URL` is optional. Leave it unset and the app uses a local SQLite file
(`dealstracker.db`), which is right for development — tables are created on
first run, so there's no migration step.

Set it to a PostgreSQL URL for anything that needs to persist. `postgres://`
and `postgresql://` URLs are both accepted and rewritten to psycopg 3.

## How it works

```
city + state/ZIP
      ↓  Google Places (searchText)
stores, grouped by chain
      ↓  per chain, only if its saved deals are stale
discover sale page  →  scrape  →  extract
      ↓
database  →  what the page renders
```

### Chains, not shops

Every H&M in a city shows the same online sale, so scraping per location would
repeat identical work. Stores are grouped by chain and each chain is scraped
once; the UI shows one card per retailer with its locations underneath.

Grouping uses the registrable domain, so `stores.nordstromrack.com` and
`www.nordstromrack.com` are one chain. A brand subdomain is kept, though —
`oldnavy.gap.com`, `bananarepublic.gap.com` and `athleta.gap.com` share a domain
but are different shops with different sales. `CHAIN_KEY_OVERRIDES` in `db.py`
fixes any case the rule gets wrong.

### Finding the sale page

Google Places usually returns a **store-locator page**, not a homepage —
something like `stores.nordstromrack.com/us/ma/boston/497-boylston-st`. Getting
from there to the sale page is most of the work, so discovery escalates through
three stages and stops at the first that succeeds:

1. **Plain fetch** — `requests` + BeautifulSoup, scoring every link. ~0.5s, and
   handles most sites.
2. **Render** — headless Chromium, for sites that build their nav in JavaScript
   or refuse non-browser clients. Urban Outfitters returns 403 to `requests`.
3. **Hover** — mega-menus build their submenu only once the parent is pointed
   at, so the Sale link genuinely isn't in the DOM until then. Hovering "WOMEN"
   on an H&M store page takes it from 82 links to over 1,100.

Link scoring prefers nav links over banners, `clearance`/`sale` over
`deal`/`promo` (which tend to label campaign pages), clean URLs over tracking
ones, and view-all pages over a single subcategory. Keywords match whole words
in the URL path only — otherwise "whole**sale**", "**Salem**" and tracking
params like `_CARD_RET_PROMO` all read as deals.

### Extraction

Page text goes to an LLM (`openai/gpt-oss-120b` via Groq) in JSON mode at
temperature 0, which returns items with both prices plus storewide promotions.
Everything passes through `coerce_deals()` in `deals.py` before it reaches the
database: prices are parsed from whatever form they arrive in, an
`original_price` at or below the sale price is discarded rather than stored as
a 0% markdown, duplicates collapse, and absurd values are rejected. It never
raises — a broken source looks like a chain with no deals.

### Caching

| What | Fresh for | Why |
|---|---|---|
| A chain's deals | 6 hours | Repeat searches read the database |
| A discovered sale URL | 7 days | Skips discovery entirely on re-scrape |
| A dead end | 7 days | Primark has no sale page; T.J. Maxx is bot-walled |

Dead ends are recorded, not worked around. Every scrape writes a `scrape_runs`
row — duration, stage used, deals found, and what extraction discarded — so a
page redesign shows up as a jump in rejects rather than a quiet drop in deals.

Price history is append-only: re-scraping adds a `price_snapshots` row rather
than overwriting, so a product's price over time survives.

### Custom scrapers

`CUSTOM_SCRAPERS` in `DealsTracker.py` maps a domain to a scraper that bypasses
the generic path. Two exist, both because the chain exposes an API that beats
rendering:

- **`nikeScraper.py`** intercepts Nike's product-wall JSON — exact
  current/original pairs and product URLs.
- **`uniqloScraper.py`** calls Uniqlo's commerce API — one request, no browser.

Most sites don't need one. The generic path handles Urban Outfitters, H&M,
J.Crew, Gap, Macy's, Saks and Nordstrom Rack with no site-specific code. Add a custom scraper only when you find an API worth using,
not because a site is awkward — hand-written selectors break on the next
redesign.

## The API

A read-only JSON API over the saved deals, for the React frontend. It serves
everything from the database and never scrapes, so it needs no API keys to run:

```bash
pip install -r requirements.txt
uvicorn api.main:app --reload
```

Run it from the project root — `api/` imports `db` and `pipeline` as top-level
modules, which resolve because the root is on `sys.path` when uvicorn starts
there. Interactive docs are at <http://127.0.0.1:8000/docs>.

| Endpoint | Returns |
|---|---|
| `GET /health` | Whether the database is reachable, and how many chains and stores are on record |
| `GET /cities/{city}/deals?state=&zip=` | Chains with a shop in that city, each with its status, locations and a sample of its deals |
| `GET /chains/{chain_key}/deals` | One chain's deals, with `min_discount` and `sort=discount\|price_asc\|price_desc\|newest\|name` |
| `GET /stores?city=&chain_key=` | Store locations with coordinates, for the map |
| `GET /stores?lat=&lng=&radius_miles=` | Locations within that radius, nearest first, each with `distance_miles` |

Every list response carries the same envelope:

```json
{"items": [], "pagination": {"limit": 50, "offset": 0, "total": 0, "has_more": false}}
```

and every failure the same one:

```json
{"error": {"code": "not_found", "message": "No chain with key 'nope.com'.", "detail": null}}
```

An unknown chain is a 404. Bad parameters are a **400** — not FastAPI's default
422, which is remapped so a client has one error path rather than two codes
meaning the same thing. A chain that exists but has no deals is a 200 whose
`status` says why: `ok`, `no_sale_page`, `blocked` or `error`. A city nobody has
searched yet is a 200 with an empty list, not a 404 — nothing has been collected
for it, which a frontend should render as an empty state.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated browser origins. Unset allows the Vite dev server alone, so a misconfigured deploy fails closed rather than accepting every origin. |
| `DATABASE_URL` | local SQLite | Same variable the Streamlit app uses. Point both at the same database and the API serves what a search saved. |

```bash
CORS_ORIGINS="https://deals.example.com,http://localhost:5173" uvicorn api.main:app
```

### Distance search

`lat`, `lng` and `radius_miles` go together; any one of them alone is a 400,
because quietly ignoring a lone `lat` would answer a different question from
the one asked. Results come back nearest first with `distance_miles` on each,
and the ordering is settled across every store in range before the page is cut
— `radius_miles=5&limit=10` is the ten nearest within five miles, not ten
arbitrary ones. Distances are straight-line, not driving distance.

It runs in two steps: SQL narrows to a bounding box, then `geo.py` measures
what is left and sorts it. The box is deliberately a superset of the circle, so
the exact check afterwards is what decides the answer. Trigonometry stays out of
SQL because it is not portable — PostgreSQL always has `sin`/`cos`/`radians`,
SQLite only when built with `SQLITE_ENABLE_MATH_FUNCTIONS`.

### What it reuses

The API is a read layer, not a second copy of the app. Freshness comes from
`db.deals_are_fresh` and `db.NEGATIVE_RECHECK_AFTER`, so `is_fresh` and
`recheck_due_at` follow the same rules the scraper obeys; the status enum is
built from `db.STATUS_*`, so a status added there cannot go missing from
`/docs`; and `chain_key` normalizes the path parameter, so `/chains/www.hm.com`
and `/chains/hm.com` are the same chain. What it deliberately does not reuse is
`ui.py` — `humanize_age` and `money` are for a Streamlit page, and the API hands
back ISO-8601 timestamps and plain numbers instead.

`api/queries.py` holds the SQL. It exists because ordering by discount has to
happen in the database: `?sort=discount&limit=20` must mean the twenty deepest
markdowns the chain has, not the first twenty rows that arrived. The same
window-function query now also backs `pipeline.load_cached_deals`, which used to
run one query per product — a chain with 200 of them made 200 round trips to
Supabase to render one Streamlit page.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit page |
| `ui.py` | Presentation helpers, kept free of streamlit so they're testable |
| `pipeline.py` | Chain grouping, caching decisions, scrape logging |
| `db.py` | SQLAlchemy models, chain identity, freshness rules |
| `deals.py` | The shared deal shape every source returns |
| `DealsTracker.py` | Places lookup, sale-page discovery, scraping, extraction |
| `browser.py` | Playwright setup — which Chromium, and looking like a real browser |
| `nikeScraper.py`, `uniqloScraper.py` | Per-chain API scrapers |
| `packages.txt` | Installs Chromium on Streamlit Cloud |
| `api/` | The read-only FastAPI app — see [The API](#the-api) |
| `api/queries.py` | The read-only SQL, shared with `pipeline.load_cached_deals` |
| `geo.py` | Haversine distance and bounding boxes, for "shops near me" |

`DealsTracker.py` also runs standalone (`python DealsTracker.py`) for a San Jose
scan that skips the database entirely.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

250 tests, no network and no API keys needed — scraping is stubbed and the
database is in-memory SQLite. UI tests run the real page headless through
streamlit's `AppTest`, and the API tests go through FastAPI's `TestClient`
against that same in-memory database.

Extraction has a recorded H&M sale page in `tests/fixtures/`, replayed offline.
To re-extract it live instead (needs a Groq key and costs a call):

```bash
RUN_LLM_TESTS=1 pytest tests/test_extraction.py
```

Refresh the fixtures with `python tests/fixtures/capture.py` when the page
changes enough that the recording no longer reflects reality.

## Deploying

Set up for [Streamlit Community Cloud](https://share.streamlit.io):

1. Point it at this repo, branch `main`, main file `app.py`.
2. Set **Python version** to 3.12 under Advanced settings.
3. Add your keys under **Secrets**:
   ```toml
   GROQ_API_KEY = "gsk_..."
   GOOGLE_PLACES_API_KEY = "AIza..."
   DATABASE_URL = "postgresql://postgres.PROJECT:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres"
   ```

**Use a hosted database when deployed.** Streamlit Cloud's filesystem is
ephemeral, so a SQLite file there is wiped on every restart and redeploy — the
cache would reset constantly and price history would never accumulate. Supabase's
free tier is enough; its *pooled* connection string (port 6543) is the one to
use, and `db.py` already disables prepared statements, which pgbouncer's
transaction mode can't carry.

Streamlit Cloud has no `playwright install` step — it gets Chromium from
`packages.txt`, and `browser.py` points Playwright at that system binary. Keep
`packages.txt` if you want the browser-dependent sites to work when deployed.

## Known limits

- **The free Groq tier costs you deals.** It allows 8,000 tokens per minute, and
  a 20k-character page is ~11k tokens, so long pages are rejected outright. The
  prompt window is capped at 12k characters to stay under it, which means fewer
  items on the longest pages — Nordstrom Rack returns 61 items at 20k characters
  but 24 at 12k. On a paid tier, raise `MAX_PROMPT_CHARS` in `DealsTracker.py`.
- **Some chains can't be scraped at all.** T.J. Maxx and Marshalls serve an
  Akamai challenge page even to a real browser. They're recorded as `blocked`
  and skipped for a week rather than retried.
- **Hammering a site gets you rate-limited.** Nordstrom Rack started refusing
  requests after repeated testing from one IP. It clears on its own.
- **Not every store has a sale page.** Primark genuinely has none, and spends
  ~26s looking before giving up. That's a correct result, not a bug.
- **A first search is slow** — roughly 8s per chain, so a city with 18 retailers
  takes about three minutes. Chains are scraped one at a time; the rendered
  phase is capped at 25s each so one slow site can't stall a whole search.
  Repeat searches of the same city take about a second.
- **Prices are as accurate as the page is readable.** The LLM reads rendered
  text; the Nike and Uniqlo scrapers return structured data and don't share that
  caveat. Uniqlo exposes no original price at all, so its items carry only a
  current price and are left out of discount ranking.
- **Deals come from each retailer's website.** In-store prices may differ.
