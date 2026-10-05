"""Dispatch 24: every Like verdict needs positive proof, not just a miss.

The already-liked branch in ``like_post`` used to return True with zero
evidence. On 2026-09-24, four posts the tool had never touched all reported
"already liked" after the 3s Like poll found nothing - and because that
branch wrote nothing, there was no way to tell a correct read from a miss
that slipped past ``note_like_miss``'s capture entirely.

This instruments all three outcomes - placed, miss, already_liked -
uniformly, behind ``LIKE_STATE_CAPTURE`` (default off, since capturing a
screenshot/DOM/JSON triple on every ordinary "placed" like would be noise,
not diagnosis). Gated in both directions: the flag on must write a
``failure_like_state_*`` triple for every outcome with the deciding element
recorded; the flag off (the shipped default) must write nothing at all.

Leaves the Like selectors and the 3s budget untouched - this is capture only.
"""

import glob
import json
import logging
import os

import pytest

from linkedin_automation import comment_poster as cpm
from linkedin_automation import profile_manager as pm

from fake_post_page import FakePostPage

URL = "https://www.linkedin.com/feed/update/urn:li:activity:1101101101101101/"


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
    return data / "failures"


def _poster(page):
    poster = cpm.LinkedInCommentPoster(profile_name="t")
    poster.driver = page
    poster.LIKE_WAIT_SECONDS = 0.05
    poster.LIKE_POLL_SECONDS = 0.01
    return poster


def _state_files(failures):
    return sorted(os.path.basename(p) for p in
                  glob.glob(str(failures / "failure_like_state_*")))


def _already_liked_page():
    """A page where the Like control is absent but the reacted-state
    selector matches - the shape that made the 2026-09-24 reports."""
    page = FakePostPage(like_absent_urls={URL})
    page.get(URL)
    page._like()
    orig = page.find_elements

    def with_liked_state(by, selector):
        if selector == cpm.LinkedInCommentPoster.LIKED_STATE_SELECTORS[0]:
            return [page.like_button]
        return orig(by, selector)
    page.find_elements = with_liked_state
    return page


# ─── flag ON: a capture for every one of the three outcomes ─────────────────

def test_placed_is_captured_with_the_deciding_selector_and_element(failures):
    page = FakePostPage()
    page.get(URL)
    poster = _poster(page)
    poster.LIKE_STATE_CAPTURE = True

    assert poster.like_post() is True

    files = _state_files(failures)
    assert any(f.endswith(".png") for f in files), files
    assert any(f.endswith(".html") for f in files), files
    [dom_path] = [f for f in files if f.endswith("_likedom.json")]
    with open(failures / dom_path, encoding="utf-8") as f:
        dom = json.load(f)

    assert dom["outcome"] == "placed"
    assert dom["decided_by"] == cpm.LinkedInCommentPoster.LIKE_BUTTON_SELECTORS[0]
    # Read AFTER the click, so the fake (like the real DOM) already shows the
    # post-click reacted state - evidence the like actually took.
    assert dom["deciding_element"]["aria_label"] == \
        "Reaction button state: Liked"


def test_miss_is_captured_with_no_deciding_element(failures):
    page = FakePostPage(like_absent_urls={URL})
    page.get(URL)
    poster = _poster(page)
    poster.LIKE_STATE_CAPTURE = True

    assert poster.like_post() is False

    files = _state_files(failures)
    assert any(f.endswith(".png") for f in files), files
    assert any(f.endswith(".html") for f in files), files
    [dom_path] = [f for f in files if f.endswith("_likedom.json")]
    with open(failures / dom_path, encoding="utf-8") as f:
        dom = json.load(f)

    assert dom["outcome"] == "miss"
    assert dom["decided_by"] is None
    assert dom["deciding_element"] is None


def test_already_liked_is_captured_with_the_deciding_selector_and_element(
        failures):
    page = _already_liked_page()
    poster = _poster(page)
    poster.LIKE_STATE_CAPTURE = True

    assert poster.like_post() is True

    files = _state_files(failures)
    assert any(f.endswith(".png") for f in files), files
    assert any(f.endswith(".html") for f in files), files
    [dom_path] = [f for f in files if f.endswith("_likedom.json")]
    with open(failures / dom_path, encoding="utf-8") as f:
        dom = json.load(f)

    assert dom["outcome"] == "already_liked"
    assert dom["decided_by"] == \
        cpm.LinkedInCommentPoster.LIKED_STATE_SELECTORS[0]
    # The fake's like_button was flipped to "Liked" by page._like() before
    # with_liked_state exposed it - the shape a real reacted post renders.
    assert dom["deciding_element"]["aria_label"] == \
        "Reaction button state: Liked"


def test_a_decided_verdict_logs_the_outcome_and_deciding_element(failures,
                                                                 caplog):
    caplog.set_level(logging.INFO)
    page = FakePostPage()
    page.get(URL)
    poster = _poster(page)
    poster.LIKE_STATE_CAPTURE = True

    poster.like_post()

    logged = [r for r in caplog.records if "LIKE STATE" in r.getMessage()]
    assert logged, "no LIKE STATE log line was emitted"
    msg = logged[0].getMessage()
    assert "outcome=placed" in msg
    assert "Reaction button state: Like" in msg


# ─── flag OFF (the shipped default): nothing written, for any outcome ───────

def _placed_page():
    page = FakePostPage()
    page.get(URL)
    return page


def _miss_page():
    page = FakePostPage(like_absent_urls={URL})
    page.get(URL)
    return page


@pytest.mark.parametrize("make_page",
                         [_placed_page, _miss_page, _already_liked_page])
def test_flag_off_writes_nothing_for_any_outcome(failures, make_page):
    poster = _poster(make_page())
    assert poster.LIKE_STATE_CAPTURE is False       # the shipped default

    poster.like_post()

    assert _state_files(failures) == []
