"""The read-only JSON API over DealsTracker's saved deals.

Phase 1 serves everything from the database: no scraping, no Places lookups,
no LLM calls. The scraping pipeline stays where it is -- `pipeline.py` remains
the only thing that writes -- so this package never needs scraping credentials
to start.
"""
