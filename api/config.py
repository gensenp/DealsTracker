"""Settings, read from the environment.

Deliberately plain os.environ rather than pydantic-settings: the API needs two
values, and adding a dependency to read them would be the only reason that
package appears in requirements.txt.
"""
from __future__ import annotations

import os

# The Vite dev server's default. Overridden in deployment by CORS_ORIGINS.
DEFAULT_DEV_ORIGIN = "http://localhost:5173"

API_TITLE = "DealsTracker API"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "Read-only access to saved deals, chains and store locations.\n\n"
    "Everything here is served from the database. Deals appear once the "
    "Streamlit app (or a later scrape endpoint) has searched that city -- this "
    "API never scrapes, so an empty result means nothing has been collected "
    "yet, not that a shop has no sale on."
)


def cors_origins() -> list[str]:
    """Allowed browser origins, comma-separated in CORS_ORIGINS.

    Read at call time so a test or a deploy can set the variable after import.
    Unset means the local React dev server alone, which is the right default
    for a checkout: a misconfigured deploy then fails closed rather than
    quietly accepting every origin on the internet.
    """
    raw = (os.environ.get("CORS_ORIGINS") or "").strip()
    if not raw:
        return [DEFAULT_DEV_ORIGIN]
    origins = [part.strip() for part in raw.split(",")]
    return [origin for origin in origins if origin]
