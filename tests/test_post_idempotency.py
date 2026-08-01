"""Phase 7 — re-running a step must not repeat work that already happened.

The consequence is asymmetric and that shapes every decision here. A comment
that failed to post can be posted again by hand. A comment posted twice cannot
be unposted, and it lands on somebody else's post under Rick's name.
"""

import json

import pytest

from linkedin_automation import atomic_io
from linkedin_automation import comment_poster as cp
from linkedin_automation import profile_manager as pm


URL_A = "https://www.linkedin.com/feed/update/urn:li:activity:1001/"
URL_B = "https://www.linkedin.com/feed/update/urn:li:activity:1002/"


@pytest.fixture
def poster(tmp_path, monkeypatch):
    """A poster whose storage is in tmp and whose browser is never built."""
    comments_dir = tmp_path / "quality_comments"
    shots = comments_dir / "debug_screenshots"
    shots.mkdir(parents=True)
    progress = comments_dir / "posting_progress.json"

    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(comments_dir))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda profile_name=None: str(shots))
    monkeypatch.setattr(pm, "get_progress_file", lambda profile_name=None: str(progress))

    def build():
        p = cp.LinkedInCommentPoster(profile_name="t")
        p.driver = object()
        return p

    return build, progress


def _comment(url):
    return {"url": url, "comment": "A considered reply.", "preview": "a post"}


def _drive(poster_obj, posted_urls, submit_result=True):
    """Stub every browser step so post_single_comment exercises only the ledger."""
    poster_obj.navigate_to_post = lambda url: True
    poster_obj.like_post = lambda: True
    poster_obj.post_comment = lambda text: (posted_urls.append(text) or submit_result
                                            if submit_result else False)


# ─── The poster does not post twice ───────────────────────────────────────────

def test_a_second_run_over_the_same_file_posts_nothing_again(poster, monkeypatch):
    build, progress_path = poster
    monkeypatch.setattr(cp.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cp.hb, "simulate_reading_for_text", lambda *a, **k: None)

    submissions = []
    first = build()
    _drive(first, submissions)
    assert first.post_single_comment(_comment(URL_A)) is True
    assert len(submissions) == 1

    # A fresh process reading the same ledger.
    second = build()
    _drive(second, submissions)
    assert second.post_single_comment(_comment(URL_A)) is True
    assert len(submissions) == 1, "the comment was submitted to LinkedIn twice"

    ledger = json.loads(progress_path.read_text(encoding="utf-8"))
    assert ledger["posted_comments"].count(URL_A) == 1


def test_a_failed_post_is_not_recorded_as_posted(poster, monkeypatch):
    build, progress_path = poster
    monkeypatch.setattr(cp.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cp.hb, "simulate_reading_for_text", lambda *a, **k: None)

    p = build()
    p.navigate_to_post = lambda url: True
    p.like_post = lambda: True
    p.post_comment = lambda text: False

    assert p.post_single_comment(_comment(URL_A)) is False
    ledger = json.loads(progress_path.read_text(encoding="utf-8"))
    assert URL_A not in ledger["posted_comments"]
    assert ledger["in_flight"] == []          # and the intent record was cleared


# ─── The window atomic writes cannot close ────────────────────────────────────

def test_a_crash_between_posting_and_recording_is_not_silently_lost(poster, monkeypatch):
    """The gap the intent record exists for.

    LinkedIn accepts the comment, then the process dies before the ledger is
    written. Atomic writes do not help: the write is not what is interrupted,
    the gap between two of them is. Without an intent record there is no
    evidence the attempt ever happened, and the next run posts it again.
    """
    build, progress_path = poster
    monkeypatch.setattr(cp.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cp.hb, "simulate_reading_for_text", lambda *a, **k: None)

    submissions = []
    first = build()
    first.navigate_to_post = lambda url: True
    first.like_post = lambda: True

    def submit_then_die(text):
        submissions.append(text)
        raise KeyboardInterrupt("killed after LinkedIn accepted the comment")

    first.post_comment = submit_then_die
    with pytest.raises(KeyboardInterrupt):
        first.post_single_comment(_comment(URL_A))

    # The intent survived the crash.
    assert json.loads(progress_path.read_text(encoding="utf-8"))["in_flight"] == [URL_A]

    # The next run refuses rather than guessing in either direction.
    second = build()
    _drive(second, submissions)
    assert second.post_single_comment(_comment(URL_A)) is False
    assert len(submissions) == 1, "it re-posted a comment that may already be live"

    ledger = json.loads(progress_path.read_text(encoding="utf-8"))
    assert ledger["needs_review"] == [URL_A]
    assert ledger["in_flight"] == []
    assert URL_A not in ledger["posted_comments"]


def test_the_unresolved_state_is_named_in_the_log_not_just_recorded(poster, caplog):
    """A skip nobody is told about is the silent failure this project keeps
    finding. The URL has to appear, so it can be checked by eye."""
    build, progress_path = poster
    atomic_io.write_json_atomic(str(progress_path),
                                {"posted_comments": [], "in_flight": [URL_A]})

    with caplog.at_level("WARNING"):
        build()

    assert URL_A in caplog.text
    assert "needs review" in caplog.text
    assert "--force" in caplog.text


def test_force_is_the_deliberate_override(poster, monkeypatch):
    """Recovery has to be possible, or the safe default becomes a dead end."""
    build, progress_path = poster
    monkeypatch.setattr(cp.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cp.hb, "simulate_reading_for_text", lambda *a, **k: None)
    atomic_io.write_json_atomic(str(progress_path),
                                {"posted_comments": [], "in_flight": [URL_A]})

    submissions = []
    p = build()
    _drive(p, submissions)
    assert p.post_single_comment(_comment(URL_A), force=True) is True
    assert len(submissions) == 1
    assert URL_A in json.loads(progress_path.read_text(encoding="utf-8"))["posted_comments"]


def test_an_unresolved_url_does_not_block_the_others(poster, monkeypatch):
    build, progress_path = poster
    monkeypatch.setattr(cp.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cp.hb, "simulate_reading_for_text", lambda *a, **k: None)
    atomic_io.write_json_atomic(str(progress_path),
                                {"posted_comments": [], "in_flight": [URL_A]})

    submissions = []
    p = build()
    _drive(p, submissions)
    assert p.post_single_comment(_comment(URL_A)) is False
    assert p.post_single_comment(_comment(URL_B)) is True
    assert len(submissions) == 1


# ─── The ledger survives a truncated write ────────────────────────────────────

def test_the_ledger_is_written_atomically(poster, monkeypatch):
    build, progress_path = poster
    p = build()
    p.progress["posted_comments"] = [URL_A]
    p.save_progress()
    original = progress_path.read_bytes()

    monkeypatch.setattr(atomic_io.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    p.progress["posted_comments"] = [URL_A, URL_B]
    with pytest.raises(OSError):
        p.save_progress()

    # The record of what was already posted is exactly as it was.
    assert progress_path.read_bytes() == original
    assert json.loads(progress_path.read_text(encoding="utf-8"))["posted_comments"] == [URL_A]


def test_an_old_ledger_without_the_new_fields_still_loads(poster):
    """Existing installs have a progress file with only posted_comments."""
    build, progress_path = poster
    atomic_io.write_json_atomic(str(progress_path), {"posted_comments": [URL_A]})

    p = build()
    assert p.progress["posted_comments"] == [URL_A]
    assert p.progress["in_flight"] == []
    assert p.progress["needs_review"] == []
    assert p.post_single_comment(_comment(URL_A)) is True   # still recognised as done


# ─── The generator does not regenerate what is already posted ─────────────────

def test_the_generator_skips_urls_already_in_the_ledger(tmp_path, monkeypatch):
    """Second half of the idempotency criterion: running generate twice over the
    same NEW posts must not produce a second draft for something already
    commented on."""
    from linkedin_automation import comment_generator as cg

    progress = tmp_path / "posting_progress.json"
    atomic_io.write_json_atomic(str(progress), {"posted_comments": [URL_A]})
    monkeypatch.setattr(pm, "get_progress_file", lambda profile_name=None: str(progress))
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))

    import logging
    stub = type("S", (), {"progress_file": str(progress),
                          "logger": logging.getLogger("t")})()
    loaded = cg.AuthenticLinkedInCommentGenerator.load_posted_urls(stub)
    assert URL_A in loaded
