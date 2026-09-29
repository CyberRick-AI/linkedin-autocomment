"""Phase 4a: X through the scheduled-posting write path.

Entirely offline — every Buffer boundary is injected. The claim that matters
most is the one about the OTHER platform: LinkedIn's schedule, comment and key
resolution must be byte-identical, and that is asserted here rather than
assumed, because this phase changed the functions LinkedIn runs through.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402
from linkedin_automation import platform_policy as pp  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402

NOW = cp.datetime(2036, 11, 1, 9, 0, tzinfo=cp.timezone.utc)


def row(**kw):
    r = {"date": "2036-11-02", "time_window": "morning",
         "post_text": "A post about llm evaluation harnesses.", "topic": "t",
         "tags": "", "image_path": "", "first_comment_link": ""}
    r.update(kw)
    return r


@pytest.fixture
def state(tmp_path):
    return cp.PipelineState(path=str(tmp_path / "state.json"))


def channel(name="AI_Fun_times"):
    return {"id": "chan_x", "name": name,
            "externalLink": "https://x.com/%s" % name}


def fetch_ok(channel_id, key=None, session=None):
    return channel()


# ─── the link rides in the body on X, and only on X ──────────────────────────

def test_an_x_row_puts_the_link_in_the_post_body():
    """One tweet: image + text + link.

    Phase 1 proved metadata.twitter.thread is FREE but DROPS THE BASE IMAGE.
    Trading the image for a threaded reply is the wrong trade for these posts,
    so the link goes in the body instead.
    """
    body = cp.compose_for_platform("Some text", "#tag",
                                   "https://example.com/a", "x")
    assert body.endswith("https://example.com/a")
    assert "Some text" in body and "#tag" in body


def test_a_linkedin_row_never_puts_the_link_in_the_body():
    """LinkedIn's link belongs in the first comment, posted by the browser."""
    body = cp.compose_for_platform("Some text", "#tag",
                                   "https://example.com/a", "linkedin")
    assert "https://example.com/a" not in body
    assert body == cp.bc.compose_text("Some text", "#tag")


def test_the_placement_is_chosen_by_platform_not_by_a_new_column():
    """Same row, two platforms, two placements."""
    r = row(first_comment_link="https://example.com/a", tags="#t")
    x_body = cp.compose_for_platform(r["post_text"], r["tags"],
                                     r["first_comment_link"], "x")
    li_body = cp.compose_for_platform(r["post_text"], r["tags"],
                                      r["first_comment_link"], "linkedin")
    assert x_body != li_body
    assert "https://example.com/a" in x_body
    assert "https://example.com/a" not in li_body


def test_no_thread_metadata_is_ever_built():
    """build_post_input has no metadata block, and X must not gain one."""
    payload = cp.bc.build_post_input("chan_x", "text", None,
                                     "2036-11-02T09:00:00.000Z")
    assert "metadata" not in payload


# ─── the 280 gate, with X's own arithmetic ───────────────────────────────────

def test_x_counts_every_url_as_23_characters():
    """t.co rewrites every link to a fixed width.

    Counting the raw URL would reject posts that fit and accept ones that do
    not - the error in both directions, from the same bug.
    """
    short = "x " + "https://a.co/1"
    long_ = "x " + "https://example.com/" + "a" * 300
    assert cp.x_counted_length(short) == cp.x_counted_length(long_) == 2 + 23


def test_the_limit_comes_from_platform_policy_not_a_literal():
    assert pp.policy_for("x").length.hard_max == 280
    assert pp.policy_for("x").length.hard_max_is_postable_limit is True


def test_an_over_length_x_row_is_rejected_at_validate_time():
    problems = cp.validate_row(row(post_text="a" * 300), now=NOW, platform="x")
    assert any("too long for X" in p for p in problems)
    assert any("280" in p for p in problems)


def test_the_link_counts_toward_the_limit():
    """The link is appended to the body on X, so it is part of the post."""
    text = "b" * 260
    assert cp.validate_row(row(post_text=text), now=NOW, platform="x") == []
    problems = cp.validate_row(
        row(post_text=text, first_comment_link="https://example.com/a"),
        now=NOW, platform="x")
    assert any("too long for X" in p for p in problems)


def test_an_over_length_row_is_never_truncated(state):
    """Rejected, not shortened. The link is at the END, so truncating would
    silently drop the thing the row exists to share."""
    created = []
    results = cp.schedule_pass(
        [row(post_text="a" * 300)], "chan_x", state, now=NOW, platform="x",
        identity_slug="AI_Fun_times", fetch_channel=fetch_ok,
        upload=lambda p: "https://img/x.png")
    assert created == []
    assert results[0]["status"] == cp.FAILED
    assert results[0]["stage"] == "validate"


def test_linkedin_has_no_length_limit_at_validate_time():
    """Its hard_max is 70 WORDS with hard_max_is_postable_limit False - a style
    guide, not something Buffer enforces. Bounding it here would reject posts
    that publish fine today."""
    assert pp.policy_for("linkedin").length.hard_max_is_postable_limit is False
    assert cp.validate_row(row(post_text="a" * 5000), now=NOW) == []
    assert cp.validate_row(row(post_text="a" * 5000), now=NOW,
                           platform="linkedin") == []


# ─── the guard at the chokepoint, not at one caller ──────────────────────────

def test_schedule_pass_itself_refuses_a_wrong_identity(state):
    """Phase 3 put this in the CLI. It belongs where every caller passes."""
    with pytest.raises(cp.IdentityRefused) as exc:
        cp.schedule_pass([row()], "chan_x", state, now=NOW, platform="x",
                         identity_slug="someone_else", fetch_channel=fetch_ok)
    assert "someone_else" in str(exc.value)
    assert "AI_Fun_times" in str(exc.value)


def test_the_refusal_happens_before_any_post_is_created(state):
    created = []

    def create(*a, **k):
        created.append(a)
        return {"id": "p1"}

    with pytest.raises(cp.IdentityRefused):
        cp.schedule_pass([row(), row(post_text="second post about llms")],
                         "chan_x", state, now=NOW, platform="x",
                         identity_slug="wrong", fetch_channel=fetch_ok)
    assert created == []


def test_the_guard_runs_once_per_call_not_once_per_row(state, monkeypatch):
    """One request, however many rows."""
    calls = []

    def counting_fetch(cid, key=None, session=None):
        calls.append(cid)
        return channel()

    monkeypatch.setattr(cp.bc, "create_post",
                        lambda *a, **k: {"id": "p%d" % len(calls),
                                         "status": "scheduled"})
    cp.schedule_pass([row(), row(post_text="another llm post"),
                      row(post_text="a third llm post")],
                     "chan_x", state, now=NOW, platform="x",
                     identity_slug="AI_Fun_times", fetch_channel=counting_fetch)
    assert len(calls) == 1


def test_an_empty_slug_refuses_the_x_run(state):
    """Dispatch 20: pm.check_declared_production now runs BEFORE
    verify_identity inside schedule_pass, and an identity that resolves to
    NOTHING is unconditionally its refusal (ProductionAccessRefused), not
    verify_identity's own empty-slug check (IdentityRefused) - see
    docs/ARCHITECTURE.md §9 and tests/test_identity_guard.py's
    test_an_unresolvable_x_identity_refuses_even_with_the_flag."""
    with pytest.raises(pm.ProductionAccessRefused):
        cp.schedule_pass([row()], "chan_x", state, now=NOW, platform="x",
                         identity_slug="", fetch_channel=fetch_ok)


def test_a_linkedin_schedule_never_calls_the_guard(state, monkeypatch):
    """The production path gains no gate. Out of scope, logged in BACKLOG."""
    asked = []
    monkeypatch.setattr(cp.bc, "get_channel",
                        lambda *a, **k: asked.append(a) or channel())
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda *a, **k: {"id": "p1", "status": "scheduled"})
    cp.schedule_pass([row()], "chan_li", state, now=NOW)
    assert asked == []


# ─── publish outcome: create-success is not publish-success on X ─────────────

def _published_state(tmp_path, status=cp.SCHEDULED):
    st = cp.PipelineState(path=str(tmp_path / "s.json"))
    st.update("k1", status=status, post_id="p1", text="A post.")
    return st


def test_an_x_post_that_errored_is_marked_failed_not_published(tmp_path):
    """Phase 1, live: createPost succeeded and the publish still failed with
    HTTP 405. A reconcile that only asks about `sent` never learns that."""
    st = _published_state(tmp_path)

    def fetch(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        assert "error" in statuses, "X must ask about failures too"
        return {"p1": {"id": "p1", "status": "error",
                       "error": {"message": "Media URL not publicly accessible"}}}

    changed = cp.reconcile_published(st, "chan_x", fetch=fetch, platform="x")
    assert st.rows["k1"]["status"] == cp.FAILED
    assert changed[0]["status"] == cp.FAILED
    assert "Media URL not publicly accessible" in changed[0]["error"]
    assert any("Media URL" in e for e in st.rows["k1"]["errors"])


def test_an_x_post_that_sent_is_still_published(tmp_path):
    st = _published_state(tmp_path)

    def fetch(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        return {"p1": {"id": "p1", "status": "sent",
                       "sentAt": "2036-11-02T09:00:00.000Z",
                       "externalLink": "https://x.com/AI_Fun_times/status/1"}}

    cp.reconcile_published(st, "chan_x", fetch=fetch, platform="x")
    assert st.rows["k1"]["status"] == cp.PUBLISHED
    assert st.rows["k1"]["permalink"].endswith("/status/1")


def test_linkedins_reconcile_still_asks_only_about_sent(tmp_path):
    """Adding a status would change which posts the production path reconciles."""
    st = _published_state(tmp_path)
    seen = {}

    def fetch(channel_id, statuses=("sent",), limit=100, key=None, session=None):
        seen["statuses"] = tuple(statuses)
        return {}

    cp.reconcile_published(st, "chan_li", fetch=fetch)
    assert seen["statuses"] == ("sent",)


# ─── per-(profile, platform) state ───────────────────────────────────────────

def test_x_and_linkedin_keep_separate_state_files(monkeypatch, tmp_path):
    """A row keyed the same way on both would let an X run read LinkedIn's
    progress and conclude a post already exists."""
    monkeypatch.setattr(cp.pm, "get_data_dir",
                        lambda profile_name=None, subdir=None: str(
                            tmp_path / (subdir or "root")))
    (tmp_path / "root").mkdir()
    (tmp_path / "x").mkdir()
    li = cp.PipelineState(profile_name="p")
    x = cp.PipelineState(profile_name="p", platform="x")
    assert li.path != x.path
    assert x.path.endswith(os.path.join("x", cp.STATE_NAME))
    assert li.path.endswith(os.path.join("root", cp.STATE_NAME))


def test_linkedins_state_path_is_unchanged(monkeypatch, tmp_path):
    """One argument, exactly as before - so nothing migrates."""
    seen = []

    def get_data_dir(profile_name=None):        # the OLD arity
        seen.append(profile_name)
        return str(tmp_path)

    monkeypatch.setattr(cp.pm, "get_data_dir", get_data_dir)
    st = cp.PipelineState(profile_name="p")
    assert st.path == os.path.join(str(tmp_path), cp.STATE_NAME)
    assert seen == ["p"]


def test_a_done_x_row_is_not_recreated(state, monkeypatch):
    """Re-running a finished row must create nothing."""
    created = []
    monkeypatch.setattr(cp.bc, "create_post",
                        lambda *a, **k: created.append(a) or {
                            "id": "p1", "status": "scheduled"})
    r = row()
    first = cp.schedule_pass([r], "chan_x", state, now=NOW, platform="x",
                             identity_slug="AI_Fun_times",
                             fetch_channel=fetch_ok)
    assert first[0]["status"] == cp.SCHEDULED
    assert len(created) == 1

    second = cp.schedule_pass([r], "chan_x", state, now=NOW, platform="x",
                              identity_slug="AI_Fun_times",
                              fetch_channel=fetch_ok)
    assert len(created) == 1, "it re-created a row that already had a post"
    assert "already has a post" in (second[0].get("note") or "")


# ─── the key reaches Buffer ──────────────────────────────────────────────────

def test_the_resolved_key_is_threaded_into_create_post(state, monkeypatch):
    seen = {}

    def create(channel_id, body, image_url, when, key=None, session=None):
        seen["key"] = key
        return {"id": "p1", "status": "scheduled"}

    monkeypatch.setattr(cp.bc, "create_post", create)
    cp.schedule_pass([row()], "chan_x", state, now=NOW, platform="x",
                     identity_slug="AI_Fun_times", fetch_channel=fetch_ok,
                     key="x-key")
    assert seen["key"] == "x-key"


def test_linkedin_passing_no_key_still_falls_through_to_the_env(state,
                                                                monkeypatch):
    """`key or api_key()` is the fallback, so LinkedIn's None is unchanged."""
    seen = {}

    def create(channel_id, body, image_url, when, key=None, session=None):
        seen["key"] = key
        return {"id": "p1", "status": "scheduled"}

    monkeypatch.setattr(cp.bc, "create_post", create)
    cp.schedule_pass([row()], "chan_li", state, now=NOW)
    assert seen["key"] is None
