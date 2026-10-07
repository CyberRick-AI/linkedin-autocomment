"""Phase 4: the browser first-comment, offline.

The browser is a fake throughout. What matters here is not the DOM work — that
is the comment path's own, already covered — but the rules around it, all of
which exist because THE POST IS ALREADY PUBLISHED AND CANNOT BE UNPUBLISHED:

  * the post is never re-published, whatever the comment does
  * a permalink gets exactly one first comment
  * a failure comes back as data, so no caller can read it as "redo the row"
  * we never like our own post
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import first_comment as fc  # noqa: E402

PERMALINK = "https://www.linkedin.com/feed/update/urn:li:share:0000000000000000000"
LINK = "https://example.com/the-reach-link"


class FakePoster:
    """Stands in for LinkedInCommentPoster's primitives."""

    def __init__(self, navigate_ok=True, comment_ok=True, login_ok=True,
                already_commented=False,
                already_commented_why="no comment of ours found on this thread"):
        self.navigate_ok, self.comment_ok, self.login_ok = (
            navigate_ok, comment_ok, login_ok)
        self.already_commented = already_commented
        self.already_commented_why = already_commented_why
        self.navigated = []
        self.comments = []
        self.already_commented_calls = []
        self.liked = 0
        self.driver = None

    def setup_driver(self):
        self.driver = object()

    def login(self):
        return self.login_ok

    def navigate_to_post(self, url):
        self.navigated.append(url)
        return self.navigate_ok

    def already_commented_here(self, comment_text=None):
        self.already_commented_calls.append(comment_text)
        if self.already_commented:
            return True, self.already_commented_why
        return False, self.already_commented_why

    def post_comment(self, text):
        self.comments.append(text)
        return self.comment_ok

    def like_post(self):
        self.liked += 1
        return True


@pytest.fixture
def ledger(tmp_path):
    return fc.FirstCommentLedger(path=str(tmp_path / "ledger.json"))


# --- the happy path ---------------------------------------------------------

def test_it_navigates_to_the_permalink_and_comments_the_link(ledger):
    p = FakePoster()
    out = fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    assert out["status"] == fc.POSTED
    assert out.ok
    assert p.navigated == [PERMALINK]
    assert p.comments == [LINK]


def test_it_never_likes_our_own_post(ledger):
    """like=False is the requirement; self-liking is not the behaviour we want."""
    p = FakePoster()
    fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    assert p.liked == 0


# --- idempotency: one first comment per permalink ---------------------------

def test_a_second_run_does_not_comment_again(ledger):
    p = FakePoster()
    first = fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    second = fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    assert first["status"] == fc.POSTED
    assert second["status"] == fc.ALREADY
    assert p.comments == [LINK], "commented twice on the same post"


def test_idempotency_survives_a_new_process(tmp_path):
    """The ledger is on disk, so a re-run tomorrow still knows."""
    path = str(tmp_path / "ledger.json")
    p1 = FakePoster()
    fc.post_first_comment(PERMALINK, LINK, poster=p1,
                          ledger=fc.FirstCommentLedger(path=path))
    p2 = FakePoster()
    out = fc.post_first_comment(PERMALINK, LINK, poster=p2,
                                ledger=fc.FirstCommentLedger(path=path))
    assert out["status"] == fc.ALREADY
    assert p2.comments == []


def test_a_different_post_is_not_blocked_by_an_earlier_one(ledger):
    p = FakePoster()
    fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    # A second fabricated permalink. Derived explicitly rather than by
    # patching digits in PERMALINK, so normalising those ids cannot
    # silently make the two identical and the test vacuous.
    other = PERMALINK.replace("share:0000000000000000000",
                              "share:0000000000000000001")
    out = fc.post_first_comment(other, LINK, poster=p, ledger=ledger)
    assert out["status"] == fc.POSTED
    assert len(p.comments) == 2


def test_a_failed_comment_is_NOT_recorded_so_it_can_be_retried(ledger):
    """Only a comment that actually landed may be marked done."""
    fail = FakePoster(comment_ok=False)
    out = fc.post_first_comment(PERMALINK, LINK, poster=fail, ledger=ledger)
    assert out["status"] == fc.FAILED
    assert not ledger.already_posted(PERMALINK)

    ok = FakePoster()
    retry = fc.post_first_comment(PERMALINK, LINK, poster=ok, ledger=ledger)
    assert retry["status"] == fc.POSTED


def test_a_corrupt_ledger_is_not_read_as_empty(tmp_path):
    """Treating it as empty would double-comment every post it recorded."""
    path = tmp_path / "ledger.json"
    path.write_text("{ this is not json")
    with pytest.raises(ValueError):
        fc.FirstCommentLedger(path=str(path))


def test_the_ledger_records_the_link_and_a_timestamp(ledger):
    fc.post_first_comment(PERMALINK, LINK, poster=FakePoster(), ledger=ledger)
    saved = json.loads(open(ledger.path, encoding="utf-8").read())
    entry = saved["commented"][PERMALINK]
    assert entry["link"] == LINK
    assert entry["commented_at"]


# --- rows that get no comment ----------------------------------------------

def test_no_link_means_no_comment_and_that_is_not_a_failure(ledger):
    p = FakePoster()
    out = fc.post_first_comment(PERMALINK, "", poster=p, ledger=ledger)
    assert out["status"] == fc.SKIPPED
    assert out.ok
    assert not out.needs_human
    assert p.navigated == [] and p.comments == []


def test_a_whitespace_only_link_is_also_a_skip(ledger):
    out = fc.post_first_comment(PERMALINK, "   ", poster=FakePoster(),
                                ledger=ledger)
    assert out["status"] == fc.SKIPPED


# --- partial failure: the post is live and must never be re-published -------

def test_a_navigation_failure_reports_rather_than_raises(ledger):
    out = fc.post_first_comment(PERMALINK, LINK,
                                poster=FakePoster(navigate_ok=False),
                                ledger=ledger)
    assert out["status"] == fc.FAILED
    assert out.needs_human
    assert "could not open" in out["error"]


def test_a_comment_failure_says_the_post_is_live(ledger):
    """The message has to make the situation unambiguous: there is nothing to
    redo, only a comment to add by hand."""
    out = fc.post_first_comment(PERMALINK, LINK,
                                poster=FakePoster(comment_ok=False),
                                ledger=ledger)
    assert out["status"] == fc.FAILED
    assert "LIVE" in out["error"]


def test_an_unexpected_exception_becomes_a_result_not_a_raise(ledger):
    """An escaping exception could be caught by a caller whose idea of handling
    a row failure is to redo the row - and the row's post is already published."""
    class Exploding(FakePoster):
        def navigate_to_post(self, url):
            raise RuntimeError("browser died")

    out = fc.post_first_comment(PERMALINK, LINK, poster=Exploding(),
                                ledger=ledger)
    assert out["status"] == fc.FAILED
    assert "browser died" in out["error"]


def test_every_failure_carries_what_a_human_needs_to_fix_it(ledger):
    for poster in (FakePoster(navigate_ok=False), FakePoster(comment_ok=False)):
        out = fc.post_first_comment(PERMALINK, LINK, poster=poster,
                                    ledger=ledger)
        assert out["permalink"] == PERMALINK    # which post
        assert out["link"] == LINK              # what should have been commented
        assert out["error"]                     # why
        assert out["at"]                        # when


def test_a_missing_permalink_fails_without_touching_the_browser(ledger):
    p = FakePoster()
    out = fc.post_first_comment("", LINK, poster=p, ledger=ledger)
    assert out["status"] == fc.FAILED
    assert p.navigated == []


# --- the duplicate guard (Dispatch 31, MAINTENANCE §6.6 reused) -------------
#
# post_first_comment used to go straight from navigate_to_post to
# post_comment, relying only on the FirstCommentLedger. A retry of a row that
# went COMMENT_FAILED on a false-negative verify (the comment actually landed,
# but post_comment() reported False) had nothing stopping a second comment on
# our own post. already_commented_here() - the same §6.6 check the main
# engagement path runs - is now asked first, reusing it rather than writing a
# second one.

def test_already_on_thread_skips_post_comment_and_reaches_already(ledger):
    """Assert on the post_comment call, not the status - the gate's own
    wording, because a status-only assertion would pass even if post_comment
    were still called and the duplicate landed anyway."""
    p = FakePoster(already_commented=True, already_commented_why="a comment "
                   "of ours is already on this thread ('• You')")
    out = fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    assert out["status"] == fc.ALREADY
    assert p.comments == [], "post_comment must not be called when already on thread"
    assert ledger.already_posted(PERMALINK), "the ledger must still gain its entry"


def test_the_duplicate_check_reuses_already_commented_here_with_the_link(ledger):
    """Reuse, not a second check: the same method, called with the comment
    link as the text to look for."""
    p = FakePoster()
    fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    assert p.already_commented_calls == [LINK]


def test_a_clean_thread_is_commented_as_before(ledger):
    p = FakePoster(already_commented=False)
    out = fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    assert out["status"] == fc.POSTED
    assert p.comments == [LINK]


def test_an_unreadable_thread_does_not_block_posting(ledger):
    """§6.6's own conservative direction: unreadable must read as False, not
    True - a DOM change must not silently stop the tool."""
    p = FakePoster(already_commented=False,
                   already_commented_why="thread not readable")
    out = fc.post_first_comment(PERMALINK, LINK, poster=p, ledger=ledger)
    assert out["status"] == fc.POSTED, "an unreadable thread must not block posting"
    assert p.comments == [LINK]


def test_a_retry_after_a_false_negative_verify_does_not_double_comment(ledger):
    """The exact failure mode this dispatch exists for: a comment lands, but
    post_comment() reports False (a false-negative verify, MAINTENANCE §6.5),
    so the row goes COMMENT_FAILED with no ledger entry. A retry must not
    comment a second time - the live thread is what catches it, since the
    ledger alone has nothing recorded."""
    false_negative = FakePoster(comment_ok=False)
    first = fc.post_first_comment(PERMALINK, LINK, poster=false_negative,
                                  ledger=ledger)
    assert first["status"] == fc.FAILED
    assert not ledger.already_posted(PERMALINK), \
        "a false-negative verify leaves no ledger entry, by design"

    # The retry's browser reads the thread fresh and finds the comment that
    # actually landed on the first attempt.
    retry = FakePoster(already_commented=True)
    second = fc.post_first_comment(PERMALINK, LINK, poster=retry, ledger=ledger)
    assert second["status"] == fc.ALREADY
    assert retry.comments == [], "a second comment would be the duplicate this guards against"
    assert ledger.already_posted(PERMALINK)


def test_the_result_never_suggests_republishing():
    """Nothing in this module's vocabulary should invite a re-post."""
    import inspect
    src = inspect.getsource(fc)
    for word in ("create_post", "schedule_row", "republish", "repost"):
        assert word not in src, "%r appears in the first-comment path" % word
