"""Dispatch 25: a reaction verdict needs POSITIVE proof, never a negation.

MAINTENANCE §6.9. The already-liked check used to be a NEGATION - "anything
in the `Reaction button state:` family that is not the literal unliked
label `...state: Like`". On 2026-10-03 a live batch against 'dev' showed
LinkedIn's own unliked label had drifted to `...state: no reaction` (button
text still "Like") - and the negation read every one of those as
already-liked, silently skipping the Like on all three posts
(data/dev/failures/failure_like_state_20261003_124153 and two siblings).

A negation can only ever repeat that failure: any label it has not seen
becomes a false already-liked by construction. This asserts the three-way
fix: the new unliked shape resolves UNLIKED, a KNOWN reacted label resolves
ALREADY_LIKED (positive proof only), and anything else - a label that is
neither - resolves UNKNOWN and takes the miss path (logged, counted,
captured, never halts commenting). The old `...state: Like` shape is kept
as a fallback per MAINTENANCE step 4, not deleted.

Every decision selector here is run through REAL CSS matching
(linkedin_automation.dom_probe, the same engine the offline selector-health
gate uses) against a single-button fragment carrying the label under test -
never a hand-picked stand-in that decides membership itself. That is what
lets test_the_already_liked_check_does_not_fire_on_the_unliked_shape
actually exercise the OLD negation pattern's real bug when reverted,
instead of a fake that merely asserts what it was told to assert.
"""

import glob
import json
import os
import time

import pytest

from linkedin_automation import comment_poster as cpm
from linkedin_automation import dom_probe
from linkedin_automation import profile_manager as pm

from fake_post_page import FakePostPage

URL = "https://www.linkedin.com/feed/update/urn:li:activity:1011011011011011/"

P = cpm.LinkedInCommentPoster


@pytest.fixture
def failures(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "failures").mkdir(parents=True)
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "t")
    monkeypatch.setattr(pm, "get_comments_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda n=None: str(data / "progress.json"))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda n=None: str(data))
    monkeypatch.setattr(pm, "get_profile_config",
                        lambda n=None: {"behavior": {}})
    monkeypatch.setattr(pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(
                            data / (subdir or "")))
    monkeypatch.setattr(pm, "login", lambda d, p: True)
    # The real human_behavior code runs; only the waiting is removed.
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return data / "failures"


def _poster(page):
    poster = P(profile_name="t")
    poster.driver = page
    poster.LIKE_STATE_CAPTURE = True
    poster.LIKE_WAIT_SECONDS = 0.05
    poster.LIKE_POLL_SECONDS = 0.01
    return poster


def _matches(aria_label, selector):
    """Does ``selector`` really match a lone button carrying ``aria_label``,
    per the same CSS engine the offline selector-health gate uses?"""
    fragment = dom_probe.parse_html(
        "<div role='listitem'><button type='button' aria-label=\"%s\">"
        "Like</button></div>" % aria_label)
    try:
        return bool(dom_probe.select_css(fragment, selector))
    except Exception:
        return False


def _page_with_reaction_label(aria_label):
    """A FakePostPage whose Like-region button carries exactly
    ``aria_label``. Every query against a LIKE_BUTTON_SELECTORS /
    LIKED_STATE_SELECTORS / diagnostic-probe selector is answered by REAL
    CSS matching against that label, not a selector-shaped stand-in - so a
    reverted (negation-based) LIKED_STATE_SELECTORS genuinely misfires here
    exactly as it did live, and a fixed one genuinely does not.
    """
    page = FakePostPage()
    page.get(URL)
    page.like_button.attrs["aria-label"] = aria_label
    orig = page.find_elements

    def patched(by, selector):
        if selector in P.LIKE_BUTTON_SELECTORS or \
                selector in P.LIKED_STATE_SELECTORS or \
                selector == getattr(P, "ANY_REACTION_STATE_SELECTOR", None):
            return [page.like_button] if _matches(aria_label, selector) else []
        return orig(by, selector)
    page.find_elements = patched
    return page


def _state_files(failures):
    return sorted(os.path.basename(p) for p in
                  glob.glob(str(failures / "failure_like_state_*")))


# ─── 1. the measured "no reaction" shape resolves UNLIKED, like attempted ──

def test_no_reaction_resolves_unliked_and_the_like_is_attempted(failures):
    page = _page_with_reaction_label("Reaction button state: no reaction")
    poster = _poster(page)

    assert poster.like_post() is True
    assert page.liked == [URL]          # the click actually fired

    [dom] = [f for f in _state_files(failures) if f.endswith("_likedom.json")]
    with open(failures / dom, encoding="utf-8") as f:
        captured = json.load(f)
    assert captured["outcome"] == "placed"
    assert captured["decided_by"] == \
        "button[aria-label='Reaction button state: no reaction']"


# ─── 2. a known reacted label resolves ALREADY_LIKED (positive proof) ──────

def test_a_known_reaction_label_resolves_already_liked(failures):
    page = _page_with_reaction_label("Reaction button state: Liked")
    poster = _poster(page)

    assert poster.like_post() is True
    assert page.liked == []             # recognised, never clicked again
    assert poster.like_misses == 0

    [dom] = [f for f in _state_files(failures) if f.endswith("_likedom.json")]
    with open(failures / dom, encoding="utf-8") as f:
        captured = json.load(f)
    assert captured["outcome"] == "already_liked"
    assert captured["decided_by"] == \
        "button[aria-label='Reaction button state: Liked']"
    assert captured["deciding_element"]["aria_label"] == \
        "Reaction button state: Liked"


# ─── the matching False case (§6.5): the SAME positive check must also be
#     provably able to say no, not just yes ───────────────────────────────

def test_the_already_liked_check_does_not_fire_on_the_unliked_shape(failures):
    """A verifier that can only return True is a rubber stamp (§6.5). Drive
    the unliked ("no reaction") shape through the SAME LIKED_STATE_SELECTORS
    check that test_a_known_reaction_label_resolves_already_liked proves can
    return True, and confirm it correctly returns False here - using real
    CSS matching, so a reverted negation selector is caught matching this
    exactly as it did on 2026-10-03, not waved through by the fake."""
    for selector in P.LIKED_STATE_SELECTORS:
        assert not _matches("Reaction button state: no reaction", selector), (
            "LIKED_STATE_SELECTORS matched the UNLIKED shape: %s" % selector)

    page = _page_with_reaction_label("Reaction button state: no reaction")
    poster = _poster(page)
    poster.like_post()
    [dom] = [f for f in _state_files(failures) if f.endswith("_likedom.json")]
    with open(failures / dom, encoding="utf-8") as f:
        captured = json.load(f)
    assert captured["outcome"] != "already_liked"


# ─── 3. an unrecognised label is UNKNOWN: miss path, logged, counted, ──────
#        captured, and the comment still posts ──────────────────────────────

def test_an_unrecognised_label_is_unknown_not_already_liked(failures):
    page = _page_with_reaction_label("Reaction button state: Thumbs Sideways")
    poster = _poster(page)

    assert poster.like_post() is False          # a miss, not already-liked
    assert poster.like_misses == 1              # counted

    # captured - both the always-on miss capture and the new like_state one
    assert any(f.startswith("failure_like_miss_") for f in
              os.listdir(failures))
    [dom] = [f for f in _state_files(failures) if f.endswith("_likedom.json")]
    with open(failures / dom, encoding="utf-8") as f:
        captured = json.load(f)
    assert captured["outcome"] == "miss"
    assert captured["decided_by"] == "unrecognised_reaction_label"
    assert captured["deciding_element"]["aria_label"] == \
        "Reaction button state: Thumbs Sideways"


def test_an_unrecognised_label_never_blocks_the_comment(failures):
    """Liking is optional (MAINTENANCE §6.8) - UNKNOWN must be exactly as
    soft a skip as the plain "button not found" miss always was."""
    page = _page_with_reaction_label("Reaction button state: Thumbs Sideways")
    poster = _poster(page)

    comment = {"url": URL, "comment": "A careful point about eval drift.",
               "preview": "preview"}
    assert poster.post_single_comment(dict(comment)) is True
    assert page.published == [(URL, comment["comment"])]


# ─── 4. the old "state: Like" shape still resolves, via the fallback ───────

def test_the_old_state_like_shape_still_resolves_via_the_fallback(failures):
    assert P.LIKE_BUTTON_SELECTORS[0] != \
        "button[aria-label='Reaction button state: Like']", (
            "this test needs the OLD shape to be a fallback, not the "
            "primary selector")
    page = _page_with_reaction_label("Reaction button state: Like")
    poster = _poster(page)

    assert poster.like_post() is True
    assert page.liked == [URL]

    [dom] = [f for f in _state_files(failures) if f.endswith("_likedom.json")]
    with open(failures / dom, encoding="utf-8") as f:
        captured = json.load(f)
    assert captured["outcome"] == "placed"
    assert captured["decided_by"] == \
        "button[aria-label='Reaction button state: Like']"
