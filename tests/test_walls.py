"""Tests for detecting a bot wall served as a normal page.

The failure these guard against is quiet, which is what makes it worth tests:
a wall answered as HTTP 200 with a few hundred characters of prose passed every
length check, was handed to the extractor, produced no items, and the chain was
filed as having no sale page -- "this retailer has no sale section" -- when the
truth was that we were turned away.

Nothing here touches the network: requests and the browser are both stubbed.
"""
from pathlib import Path

import pytest

import DealsTracker as dt

FIXTURES = Path(__file__).parent / "fixtures"

# The three wordings that prompted this, as the sites actually phrase them.
PERIMETERX_WALL = (
    "Access to this page has been denied. You have been blocked because we "
    "believe you are using automation tools to browse the website. Please "
    "make sure your browser does not have any add-ons or plugins running. "
    "Reference ID: a3f29c81-4e7b-11ef-9c2d-0242ac120002")
HUMAN_CHECK_WALL = (
    "Please verify you are a human. Complete the security check to access "
    "loft.com. This process is automatic. Your browser will redirect to the "
    "requested content shortly.")
DATADOME_WALL = (
    "Pardon our interruption. As you were browsing, something about your "
    "browser made us think you were a bot. Powered by DataDome.")


class FakeResponse:
    def __init__(self, text, ok=True):
        self.text = text
        self.ok = ok


@pytest.fixture
def no_browser(monkeypatch):
    """Fail loudly if a test renders without meaning to."""
    def unexpected(url, **kwargs):
        raise AssertionError(f"render_text should not have been called for {url}")

    monkeypatch.setattr(dt, "render_text", unexpected)


def stub_fetch(monkeypatch, body, ok=True):
    monkeypatch.setattr(dt.requests, "get",
                        lambda url, **kwargs: FakeResponse(body, ok=ok))


def stub_render(monkeypatch, body):
    calls = []

    def render(url, **kwargs):
        calls.append(url)
        return body

    monkeypatch.setattr(dt, "render_text", render)
    return calls


class TestLooksBlocked:
    @pytest.mark.parametrize("wall", [PERIMETERX_WALL, HUMAN_CHECK_WALL,
                                      DATADOME_WALL])
    def test_recognises_a_wall(self, wall):
        assert dt.looks_blocked(wall)

    @pytest.mark.parametrize("wall", [
        "Access Denied",
        "403 Forbidden",
        "Request blocked. We are sorry.",
        "Unusual traffic from your network.",
        "Please enable JavaScript to continue.",
        "Please disable any ad blocker and try again.",
        "Are you a human? Confirm below.",
        "Complete the CAPTCHA to continue.",
    ])
    def test_recognises_the_older_wordings_too(self, wall):
        assert dt.looks_blocked(wall)

    def test_an_empty_page_is_not_a_wall(self):
        """Empty means nothing readable, which page_text already reports. It is
        not evidence of being blocked, and saying otherwise would turn every
        dead link into a bot wall."""
        assert dt.looks_blocked("") is False
        assert dt.looks_blocked("   ") is False
        assert dt.looks_blocked(None) is False

    def test_a_real_sale_page_is_not_a_wall(self):
        """The recorded H&M page, so a false positive here fails the suite."""
        text = (FIXTURES / "hm_sale_page.txt").read_text(encoding="utf-8")
        assert dt.looks_blocked(text) is False

    def test_a_long_page_mentioning_a_captcha_is_not_a_wall(self):
        """A shop's help or privacy copy can mention these words in passing.
        Only short pages are judged on wording."""
        page = ("Sale " * 50) + " We use a captcha to protect our forms. " + \
               ("Jeans 29.99 was 59.99 " * 200)
        assert len(page) > dt.WALL_MAX_CHARS
        assert dt.looks_blocked(page) is False

    def test_the_length_guard_is_what_draws_the_line(self):
        """A wall's wording inside a short body still counts."""
        short = "Access to this page has been denied."
        assert len(short) < dt.WALL_MAX_CHARS
        assert dt.looks_blocked(short) is True


class TestPageText:
    def test_a_normal_page_is_returned_without_rendering(self, monkeypatch,
                                                         no_browser):
        body = "<html><body>" + ("Jeans 29.99 was 59.99 " * 40) + "</body></html>"
        stub_fetch(monkeypatch, body)
        text = dt.page_text("https://example.com/sale")
        assert "Jeans" in text

    def test_a_wall_served_as_http_200_returns_nothing_readable(self, monkeypatch):
        """The case this change exists for: status 200, enough prose to clear
        the length check, and no sale on the page at all."""
        stub_fetch(monkeypatch, f"<html><body>{PERIMETERX_WALL}</body></html>")
        rendered = stub_render(monkeypatch, PERIMETERX_WALL)

        assert dt.page_text("https://www.loft.com/sale") == ""
        assert rendered, "a wall deserves one rendered attempt"

    def test_a_wall_the_browser_gets_past_is_kept(self, monkeypatch):
        """Some walls are shown only to clients that don't look like browsers,
        so the rendered attempt is not a formality."""
        stub_fetch(monkeypatch, f"<html><body>{PERIMETERX_WALL}</body></html>")
        stub_render(monkeypatch, "Womens Sale " + ("Dress 19.99 was 49.99 " * 20))

        text = dt.page_text("https://www.loft.com/sale")
        assert "Dress" in text

    def test_a_blank_shell_still_renders_as_before(self, monkeypatch):
        stub_fetch(monkeypatch, "<html><body></body></html>")
        rendered = stub_render(monkeypatch, "Sale " + ("Top 9.99 was 29.99 " * 20))

        assert "Top" in dt.page_text("https://example.com/sale")
        assert rendered

    def test_a_page_too_short_to_be_useful_is_still_nothing(self, monkeypatch):
        stub_fetch(monkeypatch, "<html><body>Hi</body></html>")
        stub_render(monkeypatch, "Hi")
        assert dt.page_text("https://example.com/sale") == ""
