"""The selector registry covers every path the tool can break on.

ROADMAP Phase 11, closing AUDIT G3. Offline: pure registry inspection, no
browser and no LinkedIn session.

**Why this file exists.** On 2026-07-31 a posting run placed zero of three
comments and `selector_health` reported HEALTHY minutes later. Its registry held
only feed-scraping selectors, so it gave a confident all-clear on the path that
had just failed. A monitor that does not cover the risky path is worse than no
monitor, because it converts a loud failure into a green light.

The structural cause was that the posting selectors were inline literals inside
methods, while the registry is built from class constants. These tests guard
both halves: that the posting path is registered, and that the registry keeps
reading the same constants the code uses.
"""

import pytest

from linkedin_automation.comment_poster import LinkedInCommentPoster
from linkedin_automation.selector_health import SELECTOR_REGISTRY


POSTING_KEYS = [
    "post_detail",
    "post_like_button",
    "post_comment_button",
    "post_comment_input",
    "post_submit_button",
]


# ─── G3: the posting path is covered at all ───────────────────────────────────

@pytest.mark.parametrize("key", POSTING_KEYS)
def test_every_posting_selector_is_registered(key):
    """The finding itself: none of these were in the registry before Phase 11."""
    assert key in SELECTOR_REGISTRY


def test_the_registry_covers_all_three_pages():
    pages = {spec.get("page", "feed") for spec in SELECTOR_REGISTRY.values()}
    assert pages == {"feed", "search", "post"}


def test_the_posting_path_has_critical_entries():
    """A path where every entry is optional cannot report BROKEN."""
    critical = [k for k in POSTING_KEYS if SELECTOR_REGISTRY[k].get("critical")]
    assert "post_detail" in critical
    assert "post_comment_button" in critical
    assert "post_submit_button" in critical


def test_the_like_button_is_not_critical():
    """It is absent when the post is already liked, so a zero is ambiguous."""
    assert SELECTOR_REGISTRY["post_like_button"]["critical"] is False


# ─── The structural cause: registry must read the code's own constants ────────

def test_registry_selectors_are_the_constants_the_poster_actually_uses():
    """The guard on the root cause of G3.

    The registry is built from class constants precisely so it cannot drift
    from what the code does. If someone re-inlines a selector list into a method
    body, this fails, because the registry entry stops matching the constant.
    """
    assert SELECTOR_REGISTRY["post_detail"]["selectors"] == \
        list(LinkedInCommentPoster.POST_DETAIL_SELECTORS)
    assert SELECTOR_REGISTRY["post_like_button"]["selectors"] == \
        list(LinkedInCommentPoster.LIKE_BUTTON_SELECTORS)
    assert SELECTOR_REGISTRY["post_comment_input"]["selectors"] == \
        list(LinkedInCommentPoster.COMMENT_INPUT_SELECTORS)
    assert SELECTOR_REGISTRY["post_submit_button"]["selectors"] == \
        [LinkedInCommentPoster.SUBMIT_BUTTON_XPATH]


def test_the_posting_selectors_are_class_constants_not_method_literals():
    """Hoisting them out of the method bodies is what made coverage possible."""
    for name in ("POST_DETAIL_SELECTORS", "LIKE_BUTTON_SELECTORS",
                 "LIKED_STATE_SELECTORS", "COMMENT_BUTTON_LABEL_SELECTORS",
                 "COMMENT_INPUT_SELECTORS", "SUBMIT_BUTTON_XPATH"):
        assert hasattr(LinkedInCommentPoster, name), \
            f"{name} was re-inlined into a method; the registry can no longer see it"


# ─── Honesty about what a passive check cannot see ────────────────────────────

def test_interaction_gated_selectors_are_marked_as_such():
    """The comment box and submit button do not exist until the box is opened.

    Marking them is what lets the report say "not checked" instead of implying a
    clean result. Silently counting zero and calling it fine is the same class of
    lie that produced the original HEALTHY-after-failure.
    """
    assert SELECTOR_REGISTRY["post_comment_input"]["requires_interaction"] is True
    assert SELECTOR_REGISTRY["post_submit_button"]["requires_interaction"] is True


def test_selectors_that_a_page_load_can_see_are_not_marked_interaction_gated():
    for key in ("post_detail", "post_comment_button"):
        assert not SELECTOR_REGISTRY[key].get("requires_interaction")


def test_the_submit_button_entry_is_flagged_as_xpath():
    """It is matched by visible text, so a CSS counter would silently miss it."""
    assert SELECTOR_REGISTRY["post_submit_button"]["xpath"] is True
    assert SELECTOR_REGISTRY["post_submit_button"]["selectors"][0].startswith("//")


# ─── Every entry stays well-formed ────────────────────────────────────────────

@pytest.mark.parametrize("key", sorted(SELECTOR_REGISTRY))
def test_every_registry_entry_names_the_symbol_a_fixer_should_edit(key):
    """A finding with no repair target makes the human hunt for it."""
    assert SELECTOR_REGISTRY[key]["fix_symbol"]


@pytest.mark.parametrize("key", sorted(SELECTOR_REGISTRY))
def test_every_registry_entry_has_selectors_and_a_threshold(key):
    spec = SELECTOR_REGISTRY[key]
    assert spec["selectors"], f"{key} has no selectors"
    assert all(isinstance(s, str) and s for s in spec["selectors"])
    assert isinstance(spec["min_expected"], int)
    assert isinstance(spec["critical"], bool)
