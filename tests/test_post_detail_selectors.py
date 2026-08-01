"""Tests for the post-permalink selectors used before commenting.

Offline: POST_DETAIL_SELECTORS is a class constant, so no browser or network is
involved. These guard the regression that made posting fail on every post:
LinkedIn moved the permalink page to data-testid attributes while this list
still held only legacy class names."""

from linkedin_automation.comment_poster import LinkedInCommentPoster as Poster


LEGACY = {
    "div.occludable-update",
    "div.feed-shared-update-v2",
    "article.feed-shared-article",
    "div[data-urn*='activity']",
}


def test_has_a_modern_selector():
    """At least one selector must target the current DOM, not only legacy classes."""
    modern = [s for s in Poster.POST_DETAIL_SELECTORS if s not in LEGACY]
    assert modern, "only legacy selectors present; posting will fail on every post"


def test_modern_selectors_come_first():
    """Legacy selectors are fallbacks. If one leads, every post pays a full
    WebDriverWait timeout before the working selector is reached."""
    first = Poster.POST_DETAIL_SELECTORS[0]
    assert first not in LEGACY, f"legacy selector {first!r} is first; costs a timeout per post"


def test_verified_selectors_present():
    """The two confirmed against a live permalink page on 2026-07-30."""
    assert "span[data-testid='expandable-text-box']" in Poster.POST_DETAIL_SELECTORS
    assert "div[role='listitem']" in Poster.POST_DETAIL_SELECTORS


def test_legacy_fallbacks_retained():
    """Kept deliberately: they cost nothing once a modern selector matches first,
    and still help any account served the older layout."""
    assert LEGACY.issubset(set(Poster.POST_DETAIL_SELECTORS))


def test_bare_main_is_not_used():
    """'main' matches on every LinkedIn page including error pages, so it would
    report a post had loaded when nothing had."""
    assert "main" not in Poster.POST_DETAIL_SELECTORS


def test_no_duplicates():
    sels = Poster.POST_DETAIL_SELECTORS
    assert len(sels) == len(set(sels))
