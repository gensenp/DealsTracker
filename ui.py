"""Presentation helpers for the Streamlit page.

Kept free of streamlit so they can be tested without rendering a page.
"""
from __future__ import annotations

import math
from datetime import timedelta

import db
from db import STATUS_BLOCKED, STATUS_ERROR, STATUS_NO_SALE_PAGE, as_utc, utcnow
from deals import discount_pct

# How many items to show before the rest go behind a toggle.
ITEMS_SHOWN = 12

STATUS_NOTES = {
    STATUS_NO_SALE_PAGE: ("🚫", "No sale section on this retailer's site."),
    STATUS_BLOCKED: ("🛡️", "This site blocks automated access, so its deals "
                            "can't be read."),
    STATUS_ERROR: ("⚠️", "Couldn't read this site's deals on the last try."),
}


def humanize_age(moment) -> str:
    """"4 minutes ago" -- how long since a chain's deals were scraped."""
    moment = as_utc(moment)
    if moment is None:
        return "never"
    seconds = max(0, (utcnow() - moment).total_seconds())
    if seconds < 90:
        return "just now"
    for limit, size, unit in ((3600, 60, "minute"),
                              (86400, 3600, "hour"),
                              (float("inf"), 86400, "day")):
        if seconds < limit:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "never"  # unreachable; keeps the return type honest


def recheck_due_in(last_checked) -> str:
    """When a skipped dead end comes up for another look.

    Rounded up, so a wait of 4 days and 23 hours reads as 5 days rather than
    promising a look a day earlier than it will happen."""
    checked = as_utc(last_checked)
    if checked is None:
        return "soon"
    remaining = (checked + db.NEGATIVE_RECHECK_AFTER) - utcnow()
    if remaining <= timedelta(0):
        return "today"
    days = math.ceil(remaining.total_seconds() / 86400)
    return f"in {days} day{'s' if days != 1 else ''}"


def pluralize(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def money(value) -> str:
    r"""A price as markdown. The backslash matters: streamlit reads a pair of
    unescaped dollar signs on a line as LaTeX delimiters, so "$10.00 was
    $40.00" renders as mangled algebra instead of two prices."""
    return "—" if value is None else f"\\${value:,.2f}"


def render_items(items) -> list[str]:
    """Markdown rows for a chain's items, deepest markdown first."""
    ordered = sorted(items, key=lambda i: (discount_pct(i) is None,
                                           -(discount_pct(i) or 0)))
    lines = []
    for item in ordered:
        price = item.get("sale_price")
        price_text = f"**{money(price)}**" if price is not None else "—"
        pct = discount_pct(item)
        if pct:
            lines.append(f"- {item['name']} — {price_text} "
                         f"~~{money(item['original_price'])}~~ · {pct:.0f}% off")
        else:
            lines.append(f"- {item['name']} — {price_text}")
    return lines


def provenance(chain) -> str:
    """The "updated 4 minutes ago · from saved results · 2 locations" line."""
    bits = []
    if chain.skipped:
        bits.append(f"not re-checked — next look "
                    f"{recheck_due_in(chain.last_scraped_at)}")
    else:
        bits.append(f"updated {humanize_age(chain.last_scraped_at)}")
        bits.append("from saved results" if chain.from_cache
                    else f"scraped just now ({chain.duration_s or 0:.0f}s)")
    bits.append(pluralize(len(chain.stores), "location"))
    return " · ".join(bits)
