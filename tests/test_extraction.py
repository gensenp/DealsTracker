"""Regression tests for deal extraction, against a captured H&M sale page.

Two layers, because the LLM is the expensive and non-deterministic part:

* The offline tests replay a recorded extraction through coerce_deals. They run
  everywhere, in CI included, and catch a change to normalization.
* The live test re-extracts from the saved page text and checks the properties
  that must hold. It needs a Groq key and costs a call, so it is opt-in:

      RUN_LLM_TESTS=1 pytest tests/test_extraction.py

Refresh the fixtures with tests/fixtures/capture.py when the page changes.
"""
import json
import os
from pathlib import Path

import pytest

from deals import coerce_deals_with_stats, discount_pct, has_deals

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_TEXT = FIXTURES / "hm_sale_page.txt"
EXTRACTION = FIXTURES / "hm_extraction.json"


@pytest.fixture(scope="module")
def recorded():
    return json.loads(EXTRACTION.read_text(encoding="utf-8"))


class TestRecordedExtraction:
    """What the model returned for a real H&M sale page, replayed offline."""

    def test_fixtures_are_present(self):
        assert PAGE_TEXT.exists() and EXTRACTION.exists()
        assert len(PAGE_TEXT.read_text(encoding="utf-8")) > 2000

    def test_recorded_deals_survive_coercion_unchanged(self, recorded):
        """The recording is already coerced, so re-coercing must be a no-op --
        otherwise normalization has changed under us."""
        deals, stats = coerce_deals_with_stats(recorded["deals"])
        assert deals == recorded["deals"]
        assert stats.items_rejected == 0
        assert stats.duplicates_dropped == 0
        assert stats.originals_corrected == 0

    def test_page_yields_a_full_sale_list(self, recorded):
        assert len(recorded["deals"]["items"]) >= 20

    def test_every_item_has_a_real_markdown(self, recorded):
        """H&M shows crossed-out prices, so each item should carry both."""
        for item in recorded["deals"]["items"]:
            assert item["sale_price"] > 0
            assert item["original_price"] > item["sale_price"], item["name"]
            assert 0 < discount_pct(item) < 100

    def test_storewide_offer_is_a_promotion_not_an_item(self, recorded):
        """"Buy 2 Get 1 Free" belongs in promotions; as an item it would have
        no price and pollute the price history."""
        promos = [p.lower() for p in recorded["deals"]["promotions"]]
        assert any("buy 2" in p or "get 1" in p for p in promos)
        names = [i["name"].lower() for i in recorded["deals"]["items"]]
        assert not any("buy 2 get 1" in n for n in names)

    def test_known_item_is_extracted_with_both_prices(self, recorded):
        by_name = {i["name"].upper(): i for i in recorded["deals"]["items"]}
        shorts = by_name.get("REGULAR-FIT TRACK SHORTS")
        assert shorts is not None, "expected item missing from the capture"
        assert shorts["sale_price"] == 10.99
        assert shorts["original_price"] == 39.99


@pytest.fixture
def dt_module():
    """DealsTracker imports without API keys: they are resolved on use, so the
    offline tests below exercise the retry ladder without a Groq account."""
    import DealsTracker
    return DealsTracker


class FakeGroqClient:
    """Stands in for the Groq client so no key and no call are needed. Mirrors
    only the one path extract_deals uses: client.chat.completions.create."""

    def __init__(self, create):
        completions = type("Completions", (), {"create": staticmethod(create)})()
        self.chat = type("Chat", (), {"completions": completions})()


class FakeCompletion:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})})]


class TestRetryLadder:
    """Both failures here were found in live runs, not in review: a long sale
    page cut the model off mid-JSON, and a 20k-char prompt exceeded the free
    tier's per-minute token allowance outright."""

    def _install(self, dt_module, monkeypatch, failures, error_text):
        calls = []

        def create(**kwargs):
            text = kwargs["messages"][0]["content"]
            calls.append(len(text))
            if len(calls) <= failures:
                raise RuntimeError(error_text)
            return FakeCompletion('{"items": [{"name": "Tee", "sale_price": 9}],'
                                  ' "promotions": []}')

        monkeypatch.setattr(dt_module, "groq_client", lambda: FakeGroqClient(create))
        monkeypatch.setattr(dt_module.time, "sleep", lambda s: None)
        return calls

    @pytest.mark.parametrize("error_text", [
        "Error code: 400 - {'code': 'json_validate_failed'}",
        "Error code: 413 - {'code': 'rate_limit_exceeded'}",
    ])
    def test_retries_on_a_smaller_slice_of_the_page(self, dt_module, monkeypatch,
                                                    error_text):
        calls = self._install(dt_module, monkeypatch, failures=1, error_text=error_text)
        deals = dt_module.extract_deals("x" * 40000, "https://example.com/sale")

        assert len(deals["items"]) == 1
        assert len(calls) == 2
        assert calls[1] < calls[0], "retry should send less page text"

    def test_gives_up_after_the_ladder_and_reports_the_failure(self, dt_module,
                                                               monkeypatch):
        """Exhausted retries must raise so the chain is logged as an error,
        rather than quietly recorded as having no sale page."""
        self._install(dt_module, monkeypatch, failures=99,
                      error_text="Error code: 400 - {'code': 'json_validate_failed'}")
        with pytest.raises(RuntimeError, match="json_validate_failed"):
            dt_module.extract_deals("x" * 40000, "https://example.com/sale")

    def test_unrelated_errors_are_not_retried(self, dt_module, monkeypatch):
        calls = self._install(dt_module, monkeypatch, failures=99,
                              error_text="Error code: 401 - invalid api key")
        with pytest.raises(RuntimeError, match="invalid api key"):
            dt_module.extract_deals("x" * 40000, "https://example.com/sale")
        assert len(calls) == 1, "an auth failure shouldn't be retried three times"


@pytest.mark.skipif(not os.environ.get("RUN_LLM_TESTS"),
                    reason="set RUN_LLM_TESTS=1 to call Groq")
class TestLiveExtraction:
    """Re-extract from the saved page text. Opt-in: needs a key and a call."""

    def test_extraction_still_finds_the_sale(self):
        import DealsTracker as dt

        text = PAGE_TEXT.read_text(encoding="utf-8")
        deals = dt.extract_deals(text, "https://www2.hm.com/en_us/men/sale/view-all.html")

        assert has_deals(deals)
        assert len(deals["items"]) >= 15, "extraction found far fewer items than before"
        with_originals = [i for i in deals["items"] if i["original_price"]]
        assert len(with_originals) >= len(deals["items"]) * 0.8
        for item in deals["items"]:
            if item["original_price"]:
                assert item["original_price"] > item["sale_price"]
