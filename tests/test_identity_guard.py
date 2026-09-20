"""The identity guard: the one control between this tool and the wrong account.

It used to ask whether the configured slug appeared ANYWHERE in the resolved
profile URL. That is a substring test, so `in` matched every profile on
LinkedIn and a truncated vanity name matched a different human with a similar
one. Commenting as the wrong real person on a live post cannot be undone, so
this compares the `/in/<slug>` segment exactly and fails closed on anything it
cannot read.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import csv_pipeline as cp  # noqa: E402

REAL = "https://www.linkedin.com/in/example-person-one/?isSelfProfile=true"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _poster(url, raises=None):
    class Driver:
        current_url = url

        def get(self, u):
            if raises:
                raise raises

    class Poster:
        driver = Driver()

    return Poster()


# ─── the slug parser ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/in/abc-123/", "abc-123"),
    ("https://www.linkedin.com/in/abc-123", "abc-123"),
    ("https://www.linkedin.com/in/abc-123/?isSelfProfile=true", "abc-123"),
    ("https://www.linkedin.com/in/ABC-123/", "abc-123"),
    ("https://www.linkedin.com/in/abc-123/detail/contact-info/", "abc-123"),
    ("https://www.linkedin.com/feed/", None),
    ("https://www.linkedin.com/login", None),
    ("https://www.linkedin.com/checkpoint/challenge/", None),
    ("", None),
    (None, None),
])
def test_the_slug_parser(url, expected):
    assert cp.profile_slug(url) == expected


# ─── exact matching ───────────────────────────────────────────────────────────

def test_the_right_account_passes():
    ok, detail = cp.verify_identity("linkedin", "example-person-one", poster=_poster(REAL))
    assert ok is True
    assert "example-person-one" in detail


def test_case_does_not_matter():
    ok, _ = cp.verify_identity("linkedin", "EXAMPLE-Person-One", poster=_poster(REAL))
    assert ok is True


def test_a_whole_url_in_the_config_also_works():
    ok, _ = cp.verify_identity(
        "linkedin", "https://www.linkedin.com/in/example-person-one/",
        poster=_poster(REAL))
    assert ok is True


@pytest.mark.parametrize("slug", [
    "example-person",       # a truncated vanity name: a DIFFERENT human
    "example",
    "person-one",           # a suffix
    "in",                   # matched every profile on the site
    "linkedin",
    "e",
    "example-person-one-x",  # a superset
])
def test_a_slug_that_is_merely_similar_is_refused(slug):
    ok, detail = cp.verify_identity("linkedin", slug, poster=_poster(REAL))
    assert ok is False, "%r was accepted for %s" % (slug, REAL)
    assert "refusing to comment" in detail


def test_a_different_account_is_refused_and_names_both():
    ok, detail = cp.verify_identity("linkedin", "someone-else", poster=_poster(REAL))
    assert ok is False
    assert "example-person-one" in detail
    assert "someone-else" in detail


# ─── failing closed ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/login",
    "https://www.linkedin.com/checkpoint/challenge/",
    "",
])
def test_anything_that_is_not_a_profile_page_is_refused(url):
    """Not logged in, or bounced to a checkpoint, is not an identity."""
    ok, detail = cp.verify_identity("linkedin", "example-person-one", poster=_poster(url))
    assert ok is False
    assert "refusing to comment" in detail


def test_a_browser_error_is_refused_not_assumed():
    ok, detail = cp.verify_identity(
        "linkedin", "example-person-one",
        poster=_poster(REAL, raises=RuntimeError("session died")))
    assert ok is False
    assert "could not resolve" in detail


def test_no_configured_identity_now_FAILS_CLOSED():
    """It used to return True: "legal, but it disables the guard".

    That was an open default on the one control standing between this tool and
    posting as the wrong real person. The CLI's own refusal was the only thing
    making it survivable, and that covered exactly one caller — anything else
    reaching verify_identity with a blank slug was waved through.

    A guard whose default is "allow" is a guard that is off.
    """
    ok, detail = cp.verify_identity("linkedin", "", poster=_poster(REAL))
    assert ok is False
    assert "no expected identity configured" in detail
    assert "refusing" in detail


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_every_blank_shape_fails_closed_on_both_platforms(blank):
    """Whitespace is not a configured identity, and neither is None."""
    for platform in ("linkedin", "x"):
        ok, detail = cp.verify_identity(platform, blank, poster=_poster(REAL),
                                        channel_id="chan1")
        assert ok is False, (platform, blank)
        assert "refusing" in detail


def test_the_linkedin_path_refuses_when_it_has_no_browser():
    """Fail closed on a missing precondition, not just a mismatch."""
    ok, detail = cp.verify_identity("linkedin", "example-person-one")
    assert ok is False
    assert "no browser session" in detail


# ─── X: the guard for a platform that never opens a browser ──────────────────
#
# LinkedIn acts when it comments, so its guard fires at comment time against a
# live browser session. X's only action is the scheduled post, so its guard
# fires at SCHEDULE time against the Buffer channel — before any createPost, and
# with no browser anywhere.
#
# Everything here is offline: get_channel is injected.

def _channel(**kw):
    ch = {"id": "chan_x", "name": "AI_Fun_times", "displayName": "AI Fun Times",
          "service": "twitter", "externalLink": "https://x.com/AI_Fun_times",
          "organizationId": "org1"}
    ch.update(kw)
    return ch


def _fetch(channel):
    def fetch(channel_id, key=None, session=None):
        return channel
    return fetch


def test_the_matching_handle_is_confirmed():
    ok, detail = cp.verify_identity("x", "AI_Fun_times", channel_id="chan_x",
                                    fetch_channel=_fetch(_channel()))
    assert ok is True
    assert detail == "AI_Fun_times"


def test_x_handles_compare_case_insensitively():
    """X handles are case-insensitive, so @ai_fun_times IS the same account."""
    ok, _ = cp.verify_identity("x", "ai_FUN_times", channel_id="chan_x",
                               fetch_channel=_fetch(_channel()))
    assert ok is True


def test_a_leading_at_sign_is_tolerated_on_both_sides():
    """People write handles with an @; the config should not care."""
    ok, _ = cp.verify_identity("x", "@AI_Fun_times", channel_id="chan_x",
                               fetch_channel=_fetch(_channel(name="@AI_Fun_times")))
    assert ok is True


def test_a_different_handle_is_refused_and_names_both():
    """The message has to say what it found AND what it wanted.

    "identity mismatch" sends someone to read config; naming both tells them
    immediately which of the two is wrong.
    """
    ok, detail = cp.verify_identity("x", "someone_else", channel_id="chan_x",
                                    fetch_channel=_fetch(_channel()))
    assert ok is False
    assert "AI_Fun_times" in detail          # what the channel actually is
    assert "someone_else" in detail          # what was configured
    assert "refusing" in detail


@pytest.mark.parametrize("slug", [
    "AI_Fun_time",        # truncated
    "AI_Fun_times_2",     # extended
    "Fun_times",          # substring
    "AI",                 # short substring
])
def test_a_merely_similar_handle_is_refused(slug):
    """Exact, not substring. The LinkedIn guard learned this the hard way when
    `example-person` matched `example-person-011011`, a different human."""
    ok, _ = cp.verify_identity("x", slug, channel_id="chan_x",
                               fetch_channel=_fetch(_channel()))
    assert ok is False


def test_an_empty_slug_refuses_before_any_request_is_made():
    """Fail closed, and do not even ask Buffer."""
    called = []

    def fetch(channel_id, key=None, session=None):
        called.append(channel_id)
        return _channel()

    ok, detail = cp.verify_identity("x", "", channel_id="chan_x",
                                    fetch_channel=fetch)
    assert ok is False
    assert called == [], "it asked Buffer despite having nothing to compare"
    assert "no expected identity configured" in detail


def test_a_channel_with_no_readable_handle_is_refused():
    """Fail closed on "cannot tell", never open."""
    ok, detail = cp.verify_identity(
        "x", "AI_Fun_times", channel_id="chan_x",
        fetch_channel=_fetch(_channel(name="", externalLink="")))
    assert ok is False
    assert "did not say which account" in detail


def test_the_handle_falls_back_to_the_profile_url():
    """Buffer may populate externalLink rather than name."""
    ok, _ = cp.verify_identity(
        "x", "AI_Fun_times", channel_id="chan_x",
        fetch_channel=_fetch(_channel(name="")))
    assert ok is True


def test_the_display_name_is_never_accepted_as_an_identity():
    """`displayName` is a human label, not an account anyone posts as."""
    ok, _ = cp.verify_identity(
        "x", "AI Fun Times", channel_id="chan_x",
        fetch_channel=_fetch(_channel(name="", externalLink="")))
    assert ok is False


def test_buffer_failing_is_refused_not_assumed():
    """An outage must not read as a confirmed identity."""
    def boom(channel_id, key=None, session=None):
        raise RuntimeError("buffer is down")

    ok, detail = cp.verify_identity("x", "AI_Fun_times", channel_id="chan_x",
                                    fetch_channel=boom)
    assert ok is False
    assert "could not confirm" in detail


def test_no_channel_id_is_refused():
    ok, detail = cp.verify_identity("x", "AI_Fun_times")
    assert ok is False
    assert "no Buffer channel" in detail


def test_the_x_path_needs_no_browser():
    """The whole point: X never drives one, so the guard must not want one."""
    ok, _ = cp.verify_identity("x", "AI_Fun_times", channel_id="chan_x",
                               poster=None, fetch_channel=_fetch(_channel()))
    assert ok is True


def test_the_buffer_check_is_reusable_on_its_own():
    """Kept separate from fetching so any Buffer platform can adopt it —
    LinkedIn included, once its prod channel name has been read."""
    ok, got = cp.verify_channel_identity("AI_Fun_times", _channel())
    assert ok is True and got == "AI_Fun_times"
    assert cp.channel_handle(_channel()) == "AI_Fun_times"


# ─── the guard where it actually fires: the X schedule run ───────────────────

def _cli():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "run_scheduled_posts.py")
    spec = importlib.util.spec_from_file_location("run_scheduled_posts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def x_run(tmp_path, monkeypatch):
    """A real X schedule run with only the BUFFER boundary mocked.

    Phase 3 wired the guard into the CLI, and these tests mocked schedule_pass
    away — which proved the CLI called a guard, not that the guard fires on the
    path that creates posts. Phase 4a moved it into schedule_pass, the
    chokepoint every caller shares, so these now run the REAL schedule_pass and
    watch createPost instead.
    """
    cli = _cli()
    csv_path = tmp_path / "calendar.csv"
    csv_path.write_text(
        "date,time_window,post_text,topic,tags,image_path,first_comment_link\n"
        "2036-11-02,morning,A short post about llm evals.,t,,,"
        "https://example.com/x\n", encoding="utf-8")

    created = []

    def fake_create(channel_id, body, image_url, when, key=None, session=None):
        created.append({"channel_id": channel_id, "body": body,
                        "image": image_url, "key": key})
        return {"id": "p1", "status": "scheduled", "dueAt": when}

    monkeypatch.setattr(cli.cp.bc, "create_post", fake_create)

    def run(identity_slug, channel_name="AI_Fun_times", platform="x"):
        monkeypatch.setattr(cli.pm, "resolve_scheduled", lambda p, plat: {
            "platform": plat, "api_key": "x-key", "channel_id": "chan_x",
            "api_key_env": "BUFFER_API_KEY_X", "identity_slug": identity_slug,
            "drain": None})
        monkeypatch.setattr(cli.cp.bc, "get_channel",
                            lambda cid, key=None, session=None: {
                                "id": cid, "name": channel_name,
                                "externalLink": "https://x.com/%s" % channel_name})
        monkeypatch.setattr(cli.cp.pm, "get_data_dir",
                            lambda profile_name=None, subdir=None: str(tmp_path))
        argv = ["run_scheduled_posts.py", "schedule", "--profile", "p",
                "--csv", str(csv_path)]
        if platform:
            argv += ["--platform", platform]
        monkeypatch.setattr("sys.argv", argv)
        return cli.main()

    run.created = created
    return run


def test_a_wrong_identity_aborts_the_x_run_before_anything_is_created(x_run,
                                                                      capsys):
    """The acceptance case, now against the REAL schedule_pass."""
    rc = x_run(identity_slug="someone_else")
    out = capsys.readouterr().out

    assert rc == 2
    assert x_run.created == [], "it created a post despite the mismatch"
    assert "REFUSING" in out
    assert "someone_else" in out and "AI_Fun_times" in out
    assert "Nothing was created" in out


def test_an_empty_identity_aborts_the_x_run(x_run, capsys):
    rc = x_run(identity_slug="")
    assert rc == 2
    assert x_run.created == []
    assert "REFUSING" in capsys.readouterr().out


def test_a_matching_identity_lets_the_x_run_create_the_post(x_run):
    """The guard must not be so strict it blocks the correct account."""
    rc = x_run(identity_slug="AI_Fun_times")
    assert rc == 0
    assert len(x_run.created) == 1
    assert x_run.created[0]["channel_id"] == "chan_x"
    # The resolved X key reached Buffer, not LinkedIn's.
    assert x_run.created[0]["key"] == "x-key"


def test_the_x_post_carries_the_link_in_its_body(x_run):
    """Phase 1: the thread drops the image, so X puts the link in the body."""
    x_run(identity_slug="AI_Fun_times")
    assert "https://example.com/x" in x_run.created[0]["body"]


def test_the_linkedin_schedule_path_is_not_gated_at_schedule_time(x_run):
    """Explicitly out of scope this phase.

    A schedule-time channel check on the production LinkedIn flow would break
    it if Buffer names that channel anything but the exact vanity slug, and
    that has not been verified against prod. Logged in BACKLOG.

    A slug that would FAIL the X guard must still schedule fine on LinkedIn.
    """
    rc = x_run(identity_slug="not-the-channel-name", platform=None)
    assert rc == 0
    assert len(x_run.created) == 1, "LinkedIn should have scheduled"
    # LinkedIn's body must NOT carry the link - its first comment does.
    assert "https://example.com/x" not in x_run.created[0]["body"]
