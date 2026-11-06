"""Tests for the shared deal shape: coercion, discount maths and formatting.

coerce_deals() is the boundary between untrusted output (an LLM, a third-party
API) and the database, so it has to be total -- never raising, whatever it is
handed.
"""
import pytest

from deals import (
    MAX_ITEMS, MAX_PROMOTIONS, coerce_deals, discount_pct, empty_deals,
    format_deals, has_deals,
)


class TestCoercePrices:
    @pytest.mark.parametrize("given,expected", [
        (29.99, 29.99),
        (30, 30.0),
        ("29.99", 29.99),
        ("$29.99", 29.99),
        ("USD 29.99", 29.99),
        ("1,299.00", 1299.0),
        ("$4.95-$19.99", 4.95),     # range -> the price shown first
        ("from $17.97", 17.97),
    ])
    def test_prices_are_parsed(self, given, expected):
        out = coerce_deals({"items": [{"name": "X", "sale_price": given}]})
        assert out["items"][0]["sale_price"] == expected

    @pytest.mark.parametrize("given", [None, "", "  ", "free", "N/A", 0, -5, True])
    def test_non_prices_become_none(self, given):
        out = coerce_deals({"items": [{"name": "X", "sale_price": given}]})
        assert out["items"][0]["sale_price"] is None

    def test_absurd_price_is_rejected(self):
        """A stray phone number or postcode must not land in price history."""
        out = coerce_deals({"items": [{"name": "X", "sale_price": "8889666283"}]})
        assert out["items"][0]["sale_price"] is None


class TestCoerceItems:
    def test_unnamed_items_are_dropped(self):
        out = coerce_deals({"items": [{"sale_price": 10}, {"name": "  ", "sale_price": 10}]})
        assert out["items"] == []

    def test_original_at_or_below_sale_is_discarded(self):
        """Models like to echo the sale price into original_price; that would
        read as a 0% markdown instead of an unknown one."""
        out = coerce_deals({"items": [
            {"name": "A", "sale_price": 10, "original_price": 10},
            {"name": "B", "sale_price": 10, "original_price": 8},
        ]})
        assert [i["original_price"] for i in out["items"]] == [None, None]

    def test_genuine_markdown_is_kept(self):
        out = coerce_deals({"items": [{"name": "A", "sale_price": 10, "original_price": 40}]})
        assert out["items"][0] == {"name": "A", "sale_price": 10.0,
                                   "original_price": 40.0, "product_url": None}

    def test_duplicate_names_collapse(self):
        """The same product appears once per colourway on most sale pages."""
        out = coerce_deals({"items": [{"name": "Tee", "sale_price": 9},
                                      {"name": "tee", "sale_price": 9}]})
        assert len(out["items"]) == 1

    def test_relative_product_url_is_dropped(self):
        out = coerce_deals({"items": [{"name": "A", "product_url": "/shop/a"}]})
        assert out["items"][0]["product_url"] is None

    def test_absolute_product_url_is_kept(self):
        out = coerce_deals({"items": [{"name": "A", "product_url": "https://x.com/a"}]})
        assert out["items"][0]["product_url"] == "https://x.com/a"


class TestCoerceIsTotal:
    @pytest.mark.parametrize("given", [
        None, [], "No deals found", 42, {"items": None}, {"items": "nope"},
        {"items": [None, 5, "x"]}, {"promotions": None}, {},
    ])
    def test_junk_input_yields_empty_deals(self, given):
        """A broken source should look like a chain with no deals, not an
        exception that takes down the search."""
        assert coerce_deals(given) == empty_deals()

    def test_runaway_output_is_capped(self):
        out = coerce_deals({
            "items": [{"name": f"item {i}", "sale_price": 1} for i in range(MAX_ITEMS * 3)],
            "promotions": [f"promo {i}" for i in range(MAX_PROMOTIONS * 3)],
        })
        assert len(out["items"]) == MAX_ITEMS
        assert len(out["promotions"]) == MAX_PROMOTIONS


class TestPromotions:
    def test_promotions_are_deduplicated_and_trimmed(self):
        out = coerce_deals({"promotions": ["  20% off  ", "20% OFF", "", None, "BOGO"]})
        assert out["promotions"] == ["20% off", "BOGO"]

    def test_promotions_alone_still_count_as_deals(self):
        assert has_deals(coerce_deals({"promotions": ["Free shipping"]}))


class TestDiscountPct:
    def test_percentage_off(self):
        assert discount_pct({"sale_price": 10, "original_price": 40}) == 75

    def test_missing_original_has_no_percentage(self):
        """Uniqlo items carry no original price and must not read as 0% off."""
        assert discount_pct({"sale_price": 10, "original_price": None}) is None


class TestFormatDeals:
    def test_no_deals_message(self):
        assert format_deals(empty_deals()) == "No deals found"

    def test_deepest_discount_leads(self):
        text = format_deals({"items": [
            {"name": "Small cut", "sale_price": 90, "original_price": 100},
            {"name": "Deep cut", "sale_price": 10, "original_price": 100},
        ], "promotions": []})
        assert text.index("Deep cut") < text.index("Small cut")

    def test_items_without_an_original_price_are_listed_last(self):
        text = format_deals({"items": [
            {"name": "Unknown", "sale_price": 5, "original_price": None},
            {"name": "Known", "sale_price": 50, "original_price": 100},
        ], "promotions": []})
        assert text.index("Known") < text.index("Unknown")

    def test_promotions_appear_before_items(self):
        text = format_deals({"items": [{"name": "Tee", "sale_price": 5,
                                        "original_price": 10}],
                             "promotions": ["20% off everything"]})
        assert text.index("20% off everything") < text.index("Tee")

    def test_overflow_is_summarized(self):
        deals = coerce_deals({"items": [{"name": f"i{i}", "sale_price": 5,
                                         "original_price": 10} for i in range(30)]})
        assert "...and 15 more" in format_deals(deals, limit=15)
