from datetime import date

from src.opportunities.scan import (
    discover_profile_posts,
    _draft_is_expired,
    opportunity_relevance,
)


def test_relevance_keeps_explicit_opportunities_and_skips_retrospectives():
    assert opportunity_relevance("Applications open for our research assistant team", False) == "collect"
    assert opportunity_relevance("Join us for our workshop next week", False) == "collect"
    assert opportunity_relevance("Registration link in bio for the summit", False) == "collect"
    assert opportunity_relevance(
        "Career panel invites you to apply today; registration is required",
        False,
    ) == "collect"
    assert opportunity_relevance("MESA x HRA Sports Day is here", True) == "inspect_image"
    assert opportunity_relevance("Event recap: thank you for joining", False) == "exclude"
    assert opportunity_relevance(
        "Thank you to everyone who joined the program. Stay tuned for upcoming events.",
        True,
    ) == "exclude"
    assert opportunity_relevance("Congratulations to our graduating members", False) == "exclude"
    assert opportunity_relevance("Packing for Hack the Valley 11?", True) == "exclude"
    assert opportunity_relevance("Welcome back to our conference sponsor!", True) == "exclude"
    assert opportunity_relevance("The AMACSS Lounge is launching!", True) == "exclude"
    assert opportunity_relevance(
        "HTV attendees get 20% OFF at our partner locations October 16-23",
        True,
    ) == "exclude"


def test_missing_caption_is_only_a_bounded_image_ocr_fallback():
    assert opportunity_relevance(None, True) == "inspect_image"
    assert opportunity_relevance("", True) == "inspect_image"
    assert opportunity_relevance(None, False) == "exclude"


def test_draft_date_filter_skips_expired_events_and_deadlines():
    assert _draft_is_expired(
        {"opportunity": {"kind": "event", "start_date": "2026-10-19"}},
        today=date(2026, 10, 28),
    )
    assert _draft_is_expired(
        {"opportunity": {"kind": "ongoing_opportunity", "deadline_date": "2026-10-27"}},
        today=date(2026, 10, 28),
    )
    assert not _draft_is_expired(
        {"opportunity": {"kind": "event", "start_date": "2026-11-02"}},
        today=date(2026, 10, 28),
    )
    assert _draft_is_expired(
        {
            "opportunity": {
                "kind": "event",
                "start_date": None,
                "description": "Review seminar\nDate: Tuesday, October 13",
            }
        },
        today=date(2026, 10, 28),
    )
    assert not _draft_is_expired(
        {
            "opportunity": {
                "kind": "event",
                "start_date": None,
                "description": "Career panel\nDate: November 2",
            }
        },
        today=date(2026, 10, 28),
    )
    assert _draft_is_expired(
        {
            "opportunity": {
                "kind": "event",
                "start_date": "2026-11-02",
                "deadline_date": "2026-10-22",
                "description": "Career panel\nApply by October 22",
            }
        },
        today=date(2026, 10, 28),
    )
    assert _draft_is_expired(
        {"opportunity": {"kind": "event", "description": "Newsletter opportunity"}},
        today=date(2026, 10, 28),
        source_text="Delegate 101 OCT 21\nDelegate Registration Deadline OCT 23",
    )
    assert _draft_is_expired(
        {"opportunity": {"kind": "event", "description": "Newsletter opportunity"}},
        today=date(2026, 10, 28),
        source_text=(
            "Delegate 101:\n21\nNationals Winners Panel\nOCT\n"
            "23\nDelegate Registration Deadline\nOCT"
        ),
    )
    assert _draft_is_expired(
        {
            "opportunity": {
                "kind": "event",
                "start_date": "2026-11-02",
                "deadline_date": None,
                "description": "Career panel\nApply today through link by October 22",
            }
        },
        today=date(2026, 10, 28),
    )


class _Locator:
    def __init__(self, values):
        self.values = values

    def evaluate_all(self, _script):
        return self.values


class _ProfilePage:
    def __init__(self, links, heights):
        self.links = links
        self.heights = iter(heights)

    def goto(self, _url, **_kwargs):
        pass

    def wait_for_timeout(self, _milliseconds):
        pass

    def locator(self, _selector):
        return _Locator(self.links)

    def evaluate(self, script):
        if script.startswith("document.documentElement.scrollHeight"):
            return next(self.heights)
        return None


def test_profile_discovery_deduplicates_and_reports_end_of_feed(monkeypatch):
    monkeypatch.setattr("src.opportunities.scan._detect_access_block", lambda _page: None)
    monkeypatch.setattr("src.opportunities.scan._wait_for_signup_prompt_dismissal", lambda *_args: None)
    links = [
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/p/ABC123/?utm_source=profile",
        "https://www.instagram.com/reel/XYZ789/",
    ]
    page = _ProfilePage(links, [100, 100, 100, 100])

    found, reached_end = discover_profile_posts(
        page,
        "https://www.instagram.com/club/",
        max_posts=10,
        max_scrolls=4,
    )

    assert found == [
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/reel/XYZ789/",
    ]
    assert reached_end is True


def test_profile_discovery_marks_post_limit_as_partial(monkeypatch):
    monkeypatch.setattr("src.opportunities.scan._detect_access_block", lambda _page: None)
    monkeypatch.setattr("src.opportunities.scan._wait_for_signup_prompt_dismissal", lambda *_args: None)
    page = _ProfilePage(
        [
            "https://www.instagram.com/p/ABC123/",
            "https://www.instagram.com/p/DEF456/",
        ],
        [100],
    )

    found, reached_end = discover_profile_posts(
        page,
        "https://www.instagram.com/club/",
        max_posts=1,
        max_scrolls=2,
    )

    assert found == ["https://www.instagram.com/p/ABC123/"]
    assert reached_end is False
