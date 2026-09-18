"""The X selector registry, and the offline gate that checks it.

The X half of test_selector_health.py + test_selector_fixture_gate.py. Same two
jobs: prove the registry stays in sync with the constants a finder will actually
import, and prove the offline gate can fail.

No browser, no network, no X session — every check here runs against the
hand-authored fixtures in tests/fixtures/x_*.html.

WHAT THESE FIXTURES ARE AND ARE NOT. They freeze the shapes the live captures
proved (four scrubbed pages, summarised in .dev/AUDIT_x_harvest_state.md).
Passing against them means *the selectors still match what they were written
for*. It says nothing about what X serves today — the same split the fixture
README draws for LinkedIn. Neither check substitutes for the other.
"""

import io
import os

import pytest

from linkedin_automation import dom_probe
from linkedin_automation import selector_health as shc
from linkedin_automation import x_selectors as xs

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

X_PAGES = ("x_timeline", "x_status", "x_search")


def fixture(name: str) -> str:
    with io.open(os.path.join(FIXTURE_DIR, name + ".html"), encoding="utf-8") as f:
        return f.read()


def x_entries():
    return {k: v for k, v in shc.SELECTOR_REGISTRY.items()
            if v.get("page", "feed") in X_PAGES}


# ─── The registry stays in sync with the constants ────────────────────────────

def test_registry_pulls_selectors_from_x_selectors():
    """The same invariant the LinkedIn registry holds: one source of truth.

    A finder that hard-codes its own copy is how a registry starts reporting on
    selectors nothing uses.
    """
    reg = shc.SELECTOR_REGISTRY
    assert reg["x_timeline_row"]["selectors"] == list(xs.TWEET_ROW_SELECTORS)
    assert reg["x_tweet_card"]["selectors"] == list(xs.TWEET_CARD_SELECTORS)
    assert reg["x_tweet_text"]["selectors"] == list(xs.TWEET_TEXT_SELECTORS)
    assert reg["x_tweet_author"]["selectors"] == list(xs.AUTHOR_SELECTORS)
    assert reg["x_tweet_permalink"]["selectors"] == list(xs.PERMALINK_SELECTORS)
    assert reg["x_reply_editor"]["selectors"] == list(xs.REPLY_EDITOR_SELECTORS)
    assert reg["x_reply_submit"]["selectors"] == list(xs.REPLY_SUBMIT_SELECTORS)
    assert reg["x_search_input"]["selectors"] == list(xs.SEARCH_INPUT_SELECTORS)


def test_every_x_entry_names_the_symbol_a_fixer_would_edit():
    for key, spec in x_entries().items():
        assert spec.get("fix_symbol"), f"{key} has no fix_symbol"
        assert spec["fix_symbol"].startswith("x_selectors."), key


def test_the_read_and_write_paths_are_critical():
    """A dead hook on either path must read BROKEN, not DEGRADED."""
    reg = shc.SELECTOR_REGISTRY
    # Read: without these there is no post to address.
    for key in ("x_timeline_row", "x_tweet_card", "x_tweet_text",
                "x_tweet_author", "x_tweet_permalink"):
        assert reg[key]["critical"] is True, key
    # Write: without these no reply can be posted.
    for key in ("x_reply_editor", "x_reply_submit"):
        assert reg[key]["critical"] is True, key


# ─── LinkedIn isolation ───────────────────────────────────────────────────────
#
# X entries must be invisible to every LinkedIn path and vice versa. The
# mechanism is the `page` key, and `page` DEFAULTS to "feed" — so an X entry
# that forgot its page would silently join the LinkedIn feed check and fail
# every live run against a real feed.

def test_no_x_entry_leaks_into_a_linkedin_page():
    for key, spec in x_entries().items():
        assert spec.get("page") in X_PAGES, f"{key} would default onto the feed"


@pytest.mark.parametrize("page_filter,label", [
    (lambda v: not v.get("requires_menu_open") and v.get("page", "feed") == "feed",
     "live feed run"),
    (lambda v: v.get("page") == "search" and not v.get("modal_only"),
     "live search run"),
    (lambda v: v.get("page") == "post" and not v.get("requires_comment_box"),
     "live post run"),
])
def test_the_linkedin_live_runs_never_see_an_x_selector(page_filter, label):
    """Mirrors the exact filters the three live runs build."""
    selected = {k for k, v in shc.SELECTOR_REGISTRY.items() if page_filter(v)}
    leaked = selected & set(x_entries())
    assert not leaked, f"{label} would check X selectors: {sorted(leaked)}"


def test_the_x_pages_never_see_a_linkedin_selector():
    linkedin = {k for k, v in shc.SELECTOR_REGISTRY.items()
                if v.get("page", "feed") not in X_PAGES}
    for page in X_PAGES:
        on_page = {k for k, v in shc.SELECTOR_REGISTRY.items()
                   if v.get("page") == page}
        assert not (on_page & linkedin)


def test_the_linkedin_feed_registry_is_unchanged_by_the_x_additions():
    """The keys the LinkedIn feed check runs are exactly what they were."""
    feed = {k for k, v in shc.SELECTOR_REGISTRY.items()
            if v.get("page", "feed") == "feed" and not v.get("requires_menu_open")}
    assert feed == {"feed_container", "post_text", "author", "overflow_menu",
                    "scroll_container", "composer_trigger"}


# ─── The offline gate ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,page", [
    ("x_timeline_healthy", "x_timeline"),
    ("x_status_healthy", "x_status"),
    ("x_search_healthy", "x_search"),
])
def test_a_healthy_x_fixture_reports_healthy(name, page):
    report = shc.fixture_report(fixture(name), page=page)
    assert report["status"] == "HEALTHY", report["failed"]
    assert report["failed"] == []


def test_a_renamed_card_container_reports_broken():
    """The break shape: one hook renamed, nothing raises, the code finds nothing."""
    report = shc.fixture_report(fixture("x_timeline_broken"), page="x_timeline")
    assert report["status"] == "BROKEN"
    assert "x_tweet_card" in report["failed"]


def test_the_broken_fixture_only_breaks_the_one_entry():
    """A report that failed everything would hide which selector actually died.

    X's permalinks live OUTSIDE the card hook, so renaming the card must not
    take the permalink check down with it.
    """
    report = shc.fixture_report(fixture("x_timeline_broken"), page="x_timeline")
    assert report["failed"] == ["x_tweet_card"]
    checks = report["checks"]
    assert checks["x_timeline_row"]["count"] == 3
    assert checks["x_tweet_permalink"]["count"] >= 1


@pytest.mark.parametrize("name,page", [
    ("x_timeline_healthy", "x_timeline"),
    ("x_status_healthy", "x_status"),
    ("x_search_healthy", "x_search"),
])
def test_every_registry_entry_for_the_page_appears_in_the_report(name, page):
    """Nothing is silently absent — the honesty rule, applied to X."""
    report = shc.fixture_report(fixture(name), page=page)
    expected = {k for k, v in shc.SELECTOR_REGISTRY.items()
                if v.get("page") == page}
    assert set(report["checks"]) == expected


def test_the_gate_is_deterministic():
    reports = [shc.fixture_report(fixture("x_timeline_healthy"),
                                  page="x_timeline")["checks"]
               for _ in range(3)]
    assert reports[0] == reports[1] == reports[2]


def test_every_x_selector_is_readable_by_the_offline_engine():
    """A selector dom_probe cannot parse is worse than one that fails.

    check_registry lets UnsupportedSelector propagate on purpose: scoring an
    unreadable selector 0 would manufacture a false all-clear. So every X
    selector has to be inside the grammar, including the $= suffix match the
    follow button needs.
    """
    count = dom_probe.make_counter(fixture("x_timeline_healthy"))
    for key, spec in x_entries().items():
        for sel in spec["selectors"]:
            try:
                count(sel)
            except dom_probe.UnsupportedSelector as exc:
                pytest.fail(f"{key}: {sel!r} is outside the grammar ({exc})")


# ─── The findings the harvest paid for ────────────────────────────────────────

def test_the_permalink_is_a_plain_href_needing_no_interaction():
    """The single most consequential finding of the X harvest.

    LinkedIn needs the overflow menu opened and the clipboard read to learn a
    post's URL. X puts it in an anchor. If this ever stops being true the whole
    X read path has to be redesigned, so it is pinned here rather than left as
    a comment.
    """
    count = dom_probe.make_counter(fixture("x_timeline_healthy"))
    assert count("a[href*='/status/']") >= 3
    # Nothing had to be clicked: the cards are not inside an opened menu.
    assert count("[role='menu']") == 0


def test_the_analytics_variant_sits_beside_the_canonical_permalink():
    """Both were in the captures, so a caller must not assume one per card."""
    count = dom_probe.make_counter(fixture("x_timeline_healthy"))
    assert count("a[href*='/analytics']") >= 1
    assert count("a[href*='/status/']") > count("a[href*='/analytics']")


def test_the_reply_composer_needs_no_click_on_a_status_page():
    """`status` and `composer_open` captured identical structural testids.

    So none of the x_status entries is gated, and a page-load check sees the
    editor and the submit button. Mirroring LinkedIn's requires_comment_box
    here would report "not checked" for something in plain sight.
    """
    report = shc.fixture_report(fixture("x_status_healthy"), page="x_status")
    assert report["not_checked"] == []
    assert report["checks"]["x_reply_editor"]["count"] >= 1
    assert report["checks"]["x_reply_submit"]["count"] >= 1


def test_nothing_on_the_x_side_is_gated_behind_an_interaction():
    for key, spec in x_entries().items():
        assert shc.gate_reason(spec) is None, f"{key} claims an interaction gate"


def test_the_timeline_also_mounts_a_composer_so_the_lookup_must_be_scoped():
    """The trap a future X finder would otherwise walk into.

    tweetTextarea_0 is NOT unique to a status page — X mounts an inline composer
    on the timeline too. A finder that looks document-wide for the reply editor
    will find one on a page where replying is not what the caller meant, which
    is why the entry is filed under page="x_status".
    """
    timeline = dom_probe.make_counter(fixture("x_timeline_healthy"))
    status = dom_probe.make_counter(fixture("x_status_healthy"))
    search = dom_probe.make_counter(fixture("x_search_healthy"))

    assert timeline("[data-testid='tweetTextarea_0']") >= 1, (
        "the timeline composer is the whole point of this test")
    assert status("[data-testid='tweetTextarea_0']") >= 1
    assert search("[data-testid='tweetTextarea_0']") == 0

    assert shc.SELECTOR_REGISTRY["x_reply_editor"]["page"] == "x_status"


def test_only_the_status_page_carries_the_witness_hook():
    """inline_reply_offscreen is the one captured hook unique to a status page.

    app-bar-back is deliberately NOT used for this: search has it too, so it
    cannot tell a status page from a search page.
    """
    sel = "[data-testid='inline_reply_offscreen']"
    assert dom_probe.make_counter(fixture("x_status_healthy"))(sel) >= 1
    assert dom_probe.make_counter(fixture("x_timeline_healthy"))(sel) == 0
    assert dom_probe.make_counter(fixture("x_search_healthy"))(sel) == 0

    back = "[data-testid='app-bar-back']"
    assert dom_probe.make_counter(fixture("x_search_healthy"))(back) >= 1


def test_search_renders_the_same_card_as_the_timeline():
    """No search-specific card shape, so one card parser serves both."""
    search = dom_probe.make_counter(fixture("x_search_healthy"))
    for sel in (xs.TWEET_CARD_SELECTORS + xs.TWEET_TEXT_SELECTORS
                + xs.AUTHOR_SELECTORS + xs.TWEET_ROW_SELECTORS
                + xs.REPLY_BUTTON_SELECTORS + xs.LIKE_BUTTON_SELECTORS):
        assert search(sel) >= 1, sel
    assert search("a[href*='/status/']") >= 1


def test_the_virtualized_row_expects_more_than_one():
    """Rows leave the DOM as they scroll, so a min of 1 would pass on a stub.

    The captures went 81 -> 90 distinct testids and 9 -> 18 permalinks across
    three scrolls. A harvester must read as it scrolls; the registry's min of 3
    is the floor that makes a nearly-empty timeline fail.
    """
    assert shc.SELECTOR_REGISTRY["x_timeline_row"]["min_expected"] == 3
    assert shc.SELECTOR_REGISTRY["x_tweet_card"]["min_expected"] == 3


def test_the_follow_button_is_matched_by_suffix_and_is_not_profile_coverage():
    """X names these `<account-id>-follow`, so there is no fixed string.

    It stays non-critical because THE PROFILE PAGE HAS NEVER BEEN DUMPED — the
    follow button there is presumed to share this shape, which is an inference,
    not an observation. A passing check here must not read as profile coverage.
    """
    spec = shc.SELECTOR_REGISTRY["x_follow_button"]
    assert spec["critical"] is False
    assert any(s.endswith("$='-follow']") for s in spec["selectors"])

    count = dom_probe.make_counter(fixture("x_timeline_healthy"))
    assert count("[data-testid$='-follow']") >= 1
    # The suffix match must not also catch the unfollow state.
    assert count("[data-testid$='-unfollow']") == 0
    assert dom_probe.make_counter(
        fixture("x_status_healthy"))("[data-testid$='-unfollow']") >= 1


def test_no_x_page_is_registered_for_a_capture_that_was_never_taken():
    """The profile page has no entries, because it has no dump.

    Registering selectors for it would be guessing, and a registry entry that
    was guessed reports confidence the harvest never earned.
    """
    pages = {v.get("page") for v in x_entries().values()}
    assert pages == set(X_PAGES)
    assert "x_profile" not in pages
