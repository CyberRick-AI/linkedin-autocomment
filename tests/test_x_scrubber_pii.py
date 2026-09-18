"""Phase B.0: prove the X scrubber strips handles before any fixture is saved.

Phase B saves live X DOM and hand-authors fixtures from its shape. X puts real
handles INSIDE ``data-testid`` values — a single authenticated timeline carried
about twelve, ``UserAvatar-Container-<handle>`` for every avatar on screen — and
the original scrubber copied ``data-testid`` verbatim while ``pii_residue`` only
matched @handles, emails and long digit runs against the serialized text. None of
those shapes match ``UserAvatar-Container-exampleperson``, so the gate would have
passed a document carrying a dozen real people's handles.

A scrubber that leaks is worse than no scrubber, because the gate makes it look
checked. So these tests prove the failure direction too: each one shows the check
FIRING on planted PII, not merely passing on clean input.

**All markup here is FABRICATED.** Handles are named ``fakehandle*`` /
``notarealuser*`` so nothing in this file can be mistaken for recon output.
"""

import json
import re
import subprocess
import sys

import pytest

from tools import x_dump


# ─── Fabricated markup ────────────────────────────────────────────────────────

DIRTY = """
<div data-testid="cellInnerDiv">
  <article data-testid="tweet">
    <div data-testid="UserAvatar-Container-fakehandle123"></div>
    <div data-testid="UserAvatar-Container-notarealuser_two"></div>
    <span data-testid="User-Name">Fabricated Name</span>
    <button data-testid="1234567890123456789-follow"></button>
    <div data-testid="news_sidebar_article_RkFCUklDQVRFRElEMDAx"></div>
    <button data-testid="reply"></button>
    <button data-testid="like"></button>
  </article>
</div>
"""


def _testids_in(html):
    """Every data-testid value present, via the offline DOM engine."""
    root = x_dump.dom_probe.parse_html(html)
    return {n.attrs["data-testid"] for n in root.walk() if n.attrs.get("data-testid")}


# ─── 1. The scrubber strips the handle and keeps the stem ─────────────────────

def test_the_handle_is_gone_from_every_testid():
    scrubbed = x_dump.scrub_html(DIRTY)
    for handle in ("fakehandle123", "notarealuser_two"):
        assert handle not in scrubbed, f"{handle} survived scrubbing"


def test_the_structural_stem_survives_so_selectors_still_work():
    """The adapter matches [data-testid^='UserAvatar-Container-']; keep that."""
    values = _testids_in(x_dump.scrub_html(DIRTY))
    stems = [v for v in values if v.startswith("UserAvatar-Container-")]
    assert len(stems) == 2, values
    for v in stems:
        assert re.fullmatch(r"UserAvatar-Container-user\d+", v), v


def test_two_different_handles_get_two_different_placeholders():
    """Flattening them would make 'one avatar per card' unobservable."""
    values = _testids_in(x_dump.scrub_html(DIRTY))
    stems = {v for v in values if v.startswith("UserAvatar-Container-")}
    assert len(stems) == 2


def test_a_numeric_id_suffix_keeps_its_shape():
    """`[data-testid$='-follow']` must still look like what it looks like live."""
    values = _testids_in(x_dump.scrub_html(DIRTY))
    follow = [v for v in values if v.endswith("-follow")]
    assert len(follow) == 1
    assert "1234567890123456789" not in follow[0]
    assert re.fullmatch(r"9\d*-follow", follow[0]), follow[0]


def test_purely_structural_testids_are_untouched():
    values = _testids_in(x_dump.scrub_html(DIRTY))
    for keep in ("cellInnerDiv", "tweet", "reply", "like", "User-Name"):
        assert keep in values, f"{keep} was mangled; selector discovery needs it"


def test_an_unrecognized_testid_is_reported_not_silently_kept():
    """A new handle-bearing shape must surface, not pass.

    It is deliberately NOT mutated — mangling a genuinely structural hook would
    break the harvest silently. Instead it is reported, and the residue gate
    below refuses to save the dump until a human triages it.
    """
    report = {}
    x_dump.scrub_html('<div data-testid="SomeBrandNewShape-fakehandle999"></div>',
                      report)
    assert report["unknown_testids"] == ["SomeBrandNewShape-fakehandle999"]


def test_the_scrub_report_names_what_it_sanitized():
    report = {}
    x_dump.scrub_html(DIRTY, report)
    assert "UserAvatar-Container-fakehandle123" in report["sanitized_testids"]
    assert report["sanitized_testids"]["UserAvatar-Container-fakehandle123"] \
        .startswith("UserAvatar-Container-user")


# ─── 2. pii_residue scans INSIDE attribute values ─────────────────────────────
#
# The failure direction first: each of these proves the gate FIRES. A gate only
# ever exercised on clean input is a gate nobody has tested.

@pytest.mark.parametrize("planted", [
    '<div data-testid="UserAvatar-Container-fakehandle123"></div>',
    '<div data-testid="UserCell-notarealuser"></div>',
    '<div data-testid="1234567890123456789-follow"></div>',
    '<div data-testid="news_sidebar_article_RkFCUklDQVRFRA"></div>',
])
def test_residue_detects_identity_inside_a_testid_value(planted):
    """THE REGRESSION GUARD: none of these match @handle / email / digit-run,
    which is exactly why the old text-only gate passed them."""
    found = x_dump.pii_residue(planted)
    assert found, f"gate did not fire on planted PII: {planted}"
    assert "identifying_testids" in found or "long_digit_runs" in found, found


def test_the_old_text_only_check_would_have_missed_it():
    """Documents the hole this closes, so nobody reopens it.

    ``UserAvatar-Container-fakehandle123`` contains no '@', no email and no 6+
    digit run — the three things the original gate looked for.
    """
    value = "UserAvatar-Container-fakehandle123"
    assert not re.search(r"@[A-Za-z0-9_]{2,15}", value)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", value)
    assert not re.search(r"\d{6,}", value)
    # ...and yet:
    assert x_dump.pii_residue('<div data-testid="' + value + '"></div>')


def test_residue_is_clean_once_the_scrubber_has_run():
    """The other direction: planted PII in, nothing flagged out."""
    assert x_dump.pii_residue(DIRTY)                              # dirty fires
    assert x_dump.pii_residue(x_dump.scrub_html(DIRTY)) == {}     # clean passes


def test_an_unrecognized_testid_blocks_the_write():
    """Unknown shape => residue non-empty => observe() refuses to save."""
    found = x_dump.pii_residue(
        '<div data-testid="SomeBrandNewShape-fakehandle999"></div>')
    assert "unrecognized_testids" in found


# ─── 3. The [:60] cap is gone ─────────────────────────────────────────────────

def _fabricated_page_with(n_testids):
    parts = [f'<div data-testid="fabricatedHook{i:03d}"></div>'
             for i in range(n_testids)]
    return "<html><body>" + "".join(parts) + "</body></html>"


def test_all_testids_are_captured_not_the_first_sixty():
    """The spike capped at [:60] of 87 and lost `reply`, `retweet`, `tweet`,
    `tweetTextarea_0` to an alphabetical slice — the exact hooks Phase C/D need."""
    counts = x_dump.testid_counts(_fabricated_page_with(87))
    assert len(counts) == 87


def test_the_alphabetically_last_testids_survive():
    """Names chosen to sort AFTER the old cut-off point."""
    html = ('<html><body>'
            + "".join(f'<div data-testid="aaaHook{i:03d}"></div>' for i in range(70))
            + '<div data-testid="reply"></div><div data-testid="retweet"></div>'
            + '<div data-testid="tweetTextarea_0"></div></body></html>')
    names = set(x_dump.testid_counts(html))
    assert {"reply", "retweet", "tweetTextarea_0"} <= names
    assert len(names) == 73


def test_summarize_does_not_truncate_candidate_selectors():
    summary = x_dump.summarize(_fabricated_page_with(87))
    assert len(summary["candidate_selectors"]) >= 87
    assert summary["data_testid_count"] == 87


# ─── 4. Phase B needs the status and profile pages ────────────────────────────

def test_status_and_profile_are_reachable_page_targets():
    """B.1's permalink measurement happens on a status page; the follow button
    lives on a profile. Neither was a target in the spike."""
    assert set(x_dump.PARAMETERIZED_PAGES) == {"status", "profile"}
    out = subprocess.run([sys.executable, "-m", "tools.x_dump", "--help"],
                         capture_output=True, text=True).stdout
    assert "--status-url" in out
    assert "--profile-url" in out


def test_the_signal_harvest_uses_the_scrubbed_copy():
    """The vector is written to disk, so it must not carry raw handles.

    On a live timeline the raw harvest put ~12 `UserAvatar-Container-<handle>`
    values straight into `data/<profile>/x_spike/*.json`.
    """
    import inspect
    src = inspect.getsource(x_dump.probe_login_signals)
    assert "scrub_html(driver.page_source)" in src
    assert "page = driver.page_source" not in src


# ─── The first live dump: structural chrome must not block the save ───────────
#
# The B.0 gate is fail-safe — an unrecognized testid blocks the write — and the
# first live home-timeline dump duly refused to save. It was a FALSE POSITIVE:
# every handle-bearing testid had been scrubbed correctly (UserAvatar-Container-
# user10, user11...; <id>-follow numeric; news_sidebar_article_ scrubbed) and
# the block came entirely from UI chrome the allowlist had never seen.
#
# Widening an allowlist is the dangerous direction, so `classify_testid` now
# checks identity BEFORE any allowlist. Nothing below can shadow a handle.

LIVE_STRUCTURAL_TESTIDS = [
    "premium-signup-tab", "progressBar-bar", "right-impression-pixel",
    "top-impression-pixel", "scheduleOption", "toolBar", "trend",
    "tweet-text-show-more-link", "tweetPhoto",
    "tweetTextarea_0RichTextInputContainer", "scrubber",
    # Second live load: X served a video and a repost, surfacing two more.
    "videoComponent", "socialContext",
    # Third load: the scrolled page carried a condensed media preview.
    "testCondensedMedia",
    # Fourth load: the boost/promote CTA button, the last thing blocking a save.
    "boostCta",
    # First status-page capture: chrome the timeline never renders.
    "app-bar-back", "inline_reply_offscreen",
    # Timeline scroll: a video with captions, and a preview interstitial.
    "captions", "previewInterstitial",
    # First search-page capture: the search filter sidebar and its overflow.
    "radioGroupLocation", "radioGroupPeople",
    "searchBoxOverflowButton", "searchFiltersAdvancedSearch",
]


@pytest.mark.parametrize("value", LIVE_STRUCTURAL_TESTIDS)
def test_live_structural_testids_are_recognized(value):
    """Confirmed chrome from the live dump. None carries user data."""
    status, _ = x_dump.classify_testid(value)
    assert status == "safe", f"{value} would block a save as {status}"


@pytest.mark.parametrize("value", LIVE_STRUCTURAL_TESTIDS)
def test_live_structural_testids_do_not_trigger_residue(value):
    """The regression guard for the false positive: these must never re-block."""
    html = '<div data-testid="' + value + '"></div>'
    assert x_dump.pii_residue(html) == {}


@pytest.mark.parametrize("variant", [
    "bottom-impression-pixel", "left-impression-pixel", "top-impression-pixel",
    "right-impression-pixel", "tweetTextarea_1", "tweetTextarea_0", "tweetPhoto-1",
])
def test_structural_families_generalize(variant):
    """Allowlisted as patterns, not literals.

    X numbers and positions these, so a variant appearing on the next scroll
    would otherwise re-block a save that the previous scroll allowed.
    """
    assert x_dump.classify_testid(variant)[0] == "safe"


def test_widening_the_allowlist_cannot_shadow_a_handle():
    """Identity is classified BEFORE any allowlist, so ordering cannot regress.

    Proven adversarially: even if a handle-bearing value were added to the safe
    set by mistake, the stem check runs first and still wins.
    """
    poisoned = frozenset(x_dump._TESTID_SAFE_EXACT | {"UserAvatar-Container-fakehandle123"})
    original = x_dump._TESTID_SAFE_EXACT
    try:
        x_dump._TESTID_SAFE_EXACT = poisoned
        status, stem = x_dump.classify_testid("UserAvatar-Container-fakehandle123")
        assert status == "handle_stem"
        assert stem == "UserAvatar-Container-"
    finally:
        x_dump._TESTID_SAFE_EXACT = original


def test_a_page_of_live_chrome_plus_a_handle_still_blocks():
    """The widening is scoped: chrome passes, a handle in the same page does not."""
    chrome = "".join('<div data-testid="' + v + '"></div>'
                     for v in LIVE_STRUCTURAL_TESTIDS)
    assert x_dump.pii_residue("<body>" + chrome + "</body>") == {}

    with_handle = chrome + '<div data-testid="UserAvatar-Container-fakehandle123"></div>'
    found = x_dump.pii_residue("<body>" + with_handle + "</body>")
    assert "identifying_testids" in found


def test_social_context_is_structural_bare_but_identity_when_suffixed():
    """The deliberate asymmetry, asserted so nobody "simplifies" it away.

    `socialContext` is the "reposted" / "you follow X" label. Its VISIBLE TEXT
    names a person — but text is dropped wholesale by the scrubber, and the
    testid itself is a fixed name. A suffixed variant would be a different
    matter, so the handle stem stays and, because identity is checked first,
    still wins.
    """
    assert x_dump.classify_testid("socialContext")[0] == "safe"
    assert x_dump.classify_testid("socialContext-somehandle")[0] == "handle_stem"
    assert x_dump.pii_residue('<div data-testid="socialContext"></div>') == {}
    assert "identifying_testids" in x_dump.pii_residue(
        '<div data-testid="socialContext-fakehandle123"></div>')


def test_the_video_family_generalizes():
    """videoComponent and videoPlayer are a pair; suffixed variants are chrome."""
    for v in ("videoComponent", "videoComponent-1", "videoPlayer", "videoPlayer-0"):
        assert x_dump.classify_testid(v)[0] == "safe", v


def test_the_condensed_media_family_generalizes():
    """Allowlisted as a prefix: X suffixes and indexes these preview elements."""
    for v in ("testCondensedMedia", "testCondensedMedia-0", "testCondensedMediaGroup"):
        assert x_dump.classify_testid(v)[0] == "safe", v
    assert x_dump.pii_residue('<div data-testid="testCondensedMedia"></div>') == {}


def test_boost_cta_is_allowlisted_exactly_not_as_a_family():
    """`boostCta` is the boost/promote button: one per page, no variants seen.

    Exact rather than a prefix, so the fail-safe direction is preserved — a
    hypothetical `boostCta-<tail>` blocks the save for triage instead of being
    waved through by a family rule nothing has justified yet.
    """
    assert x_dump.classify_testid("boostCta")[0] == "safe"
    assert x_dump.pii_residue('<div data-testid="boostCta"></div>') == {}
    assert x_dump.classify_testid("boostCta-somehandle")[0] != "safe"
    assert "unrecognized_testids" in x_dump.pii_residue(
        '<div data-testid="boostCta-somehandle"></div>')


def test_boost_cta_cannot_shadow_a_handle_and_reverts_cleanly():
    """Reversion proof, both directions.

    Forward: with the entry present, `boostCta` no longer blocks a save.
    Backward: with the entry removed, it blocks again exactly as before — so the
    allowlist is the only thing that changed, and identity classification is
    untouched by it.
    """
    html = '<div data-testid="boostCta"></div>'
    assert x_dump.pii_residue(html) == {}

    original = x_dump._TESTID_SAFE_EXACT
    try:
        x_dump._TESTID_SAFE_EXACT = frozenset(original - {"boostCta"})
        assert x_dump.classify_testid("boostCta")[0] == "unknown"
        assert "unrecognized_testids" in x_dump.pii_residue(html)
        # And the scrub of a real handle is identical with or without the entry.
        assert x_dump.classify_testid("UserAvatar-Container-realperson") == (
            "handle_stem", "UserAvatar-Container-")
    finally:
        x_dump._TESTID_SAFE_EXACT = original
    assert x_dump.classify_testid("boostCta")[0] == "safe"


def test_the_status_page_chrome_is_allowlisted_exactly():
    """Both status-page testids are exact, and X's source is why.

    `inline_reply_offscreen` is a hardcoded literal in the bundle; there is no
    indexed variant to generalize to. `app-bar-back` comes from a template whose
    tail is a two-member enum, so the family is enumerated rather than opened —
    which keeps a tail X cannot emit on the blocking side of the gate.
    """
    for value in ("app-bar-back", "app-bar-close", "inline_reply_offscreen"):
        assert x_dump.classify_testid(value)[0] == "safe", value
        assert x_dump.pii_residue(
            '<div data-testid="' + value + '"></div>') == {}

    for tail in ("app-bar-somehandle", "inline_reply_offscreen-0",
                 "inline_reply_offscreen_somehandle"):
        assert x_dump.classify_testid(tail)[0] != "safe", tail
        assert "unrecognized_testids" in x_dump.pii_residue(
            '<div data-testid="' + tail + '"></div>'), tail


def test_the_status_page_chrome_reverts_cleanly_and_shadows_nothing():
    """Reversion proof, both directions, plus the poison check for these two.

    Forward: with the entries present, neither blocks a save. Backward: removed,
    both block again exactly as before. And even if one were mistakenly written
    with a handle-bearing tail, identity is classified first and still wins.
    """
    added = {"app-bar-back", "app-bar-close", "inline_reply_offscreen"}
    html = "".join('<div data-testid="' + v + '"></div>' for v in sorted(added))
    assert x_dump.pii_residue("<body>" + html + "</body>") == {}

    original = x_dump._TESTID_SAFE_EXACT
    try:
        x_dump._TESTID_SAFE_EXACT = frozenset(original - added)
        for v in sorted(added):
            assert x_dump.classify_testid(v)[0] == "unknown", v
        assert "unrecognized_testids" in x_dump.pii_residue(
            "<body>" + html + "</body>")

        # Poisoned the other way: a handle-bearing value in the safe set loses
        # to the stem check, which runs first.
        x_dump._TESTID_SAFE_EXACT = frozenset(
            original | {"UserName-fakehandle123"})
        assert x_dump.classify_testid("UserName-fakehandle123") == (
            "handle_stem", "UserName-")
    finally:
        x_dump._TESTID_SAFE_EXACT = original

    for v in sorted(added):
        assert x_dump.classify_testid(v)[0] == "safe", v


def test_status_page_chrome_plus_a_handle_still_blocks():
    """The widening is scoped: a real status page with a handle still refuses."""
    chrome = ('<div data-testid="app-bar-back"></div>'
              '<div data-testid="inline_reply_offscreen"></div>')
    assert x_dump.pii_residue("<body>" + chrome + "</body>") == {}
    found = x_dump.pii_residue(
        "<body>" + chrome +
        '<div data-testid="UserAvatar-Container-fakehandle123"></div></body>')
    assert "identifying_testids" in found


def test_the_video_caption_and_interstitial_chrome_are_exact():
    """Both exact, and the bundle is why.

    `captions` is a hardcoded `testID:"captions"` on the ControlBar toggle.
    `previewInterstitial` appears only as a standalone literal. Neither has an
    indexed variant, so a prefix would widen the allowlist for a shape that does
    not exist — and X's *actual* indexed captions family is a different name
    whose tail is a post id, which must keep blocking.
    """
    for value in ("captions", "previewInterstitial"):
        assert x_dump.classify_testid(value)[0] == "safe", value
        assert x_dump.pii_residue('<div data-testid="' + value + '"></div>') == {}

    for tail in ("captions-0", "previewInterstitial-1",
                 "immersive-tweet-add-captions-icon-1011001100110011001"):
        assert x_dump.classify_testid(tail)[0] != "safe", tail


def test_the_caption_chrome_reverts_cleanly_and_shadows_nothing():
    """Reversion both ways, plus the poison check for these two."""
    added = {"captions", "previewInterstitial"}
    html = "".join('<div data-testid="' + v + '"></div>' for v in sorted(added))
    assert x_dump.pii_residue(html) == {}

    original = x_dump._TESTID_SAFE_EXACT
    try:
        x_dump._TESTID_SAFE_EXACT = frozenset(original - added)
        for v in sorted(added):
            assert x_dump.classify_testid(v)[0] == "unknown", v
        assert "unrecognized_testids" in x_dump.pii_residue(html)

        x_dump._TESTID_SAFE_EXACT = frozenset(original | {"UserCell-fakehandle"})
        assert x_dump.classify_testid("UserCell-fakehandle") == (
            "handle_stem", "UserCell-")
    finally:
        x_dump._TESTID_SAFE_EXACT = original
    for v in sorted(added):
        assert x_dump.classify_testid(v)[0] == "safe", v


# ─── The url_after_nav leak: URLs on disk were never behind the gate ──────────
#
# The testid gate only ever guarded the HTML. The observation record itself
# wrote the navigated URL as observed, so a status dump put a real handle AND a
# real post id into `data/<profile>/x_spike/x_probe_*.json` — the same class of
# leak the scrubber exists to prevent, just outside the thing being checked.

REAL_STATUS_URL = "https://x.com/EXAMPLECO/status/1011001100110011001"
REAL_PROFILE_URL = "https://x.com/exampleperson"


def test_a_navigated_status_url_keeps_its_shape_and_loses_its_identity():
    """Structure is why the field is kept at all, so structure must survive."""
    out = x_dump.scrub_url(REAL_STATUS_URL)
    assert out.startswith("https://x.com/")
    assert "/status/" in out
    assert "EXAMPLECO" not in out
    assert "1011001100110011001" not in out

    m = re.match(r"^https://x\.com/(user\d+)/status/(\d+)$", out)
    assert m, out
    # Same synthesis as everywhere else: user<N>, and an all-9s id of the same
    # length, so length-sensitive selector work still applies.
    assert len(m.group(2)) == len("1011001100110011001")
    assert m.group(2).startswith("9")


def test_a_profile_url_becomes_a_synthetic_profile_url():
    out = x_dump.scrub_url(REAL_PROFILE_URL)
    assert re.fullmatch(r"https://x\.com/user\d+", out), out


def test_two_different_handles_get_two_different_synthetic_urls():
    """Distinctness is preserved, exactly as it is for handles in the HTML."""
    scrubber = x_dump._Scrubber()
    a = x_dump.scrub_url(REAL_PROFILE_URL, scrubber)
    b = x_dump.scrub_url("https://x.com/exampleuser", scrubber)
    assert a != b
    assert x_dump.scrub_url(REAL_PROFILE_URL, scrubber) == a


@pytest.mark.parametrize("value", [
    "https://x.com/home", "https://x.com/i/flow/login", "https://x.com/explore",
    "/account/access", "/suspended", "/login", "/challenge",
    # A bare `/<handle>` is left alone here too — indistinguishable from the
    # markers above, and the HTML scrubber already redacts it where it appears
    # as an href. Only `/<handle>/status/<id>` is unambiguous.
    "/exampleperson",
    r"data\xtest\x_spike\timeline_foryou.scrubbed.html",
    "timeline_foryou", "logged_in",
])
def test_the_url_scrub_leaves_non_identity_strings_alone(value):
    """Scoped narrowly on purpose.

    X's own routes are not handles, `blocked_url_markers` holds diagnostics like
    `/account/access`, and `saved_to` holds a Windows path. A blanket
    "starts with a slash" rule would rewrite `/account/access` into
    `/user1/access` and quietly destroy a block signal.
    """
    assert x_dump.scrub_url(value) == value


def test_a_whole_probe_record_has_no_real_handle_or_post_id_on_disk():
    """The end-to-end assertion: a status dump's record, as written."""
    record = {
        "label": "status",
        "url_after_nav": REAL_STATUS_URL,
        "saved_to": r"data\xtest\x_spike\status.scrubbed.html",
        "blocked_url_markers": ["/account/access"],
        "login_signals": {"_url": REAL_STATUS_URL, "nav_avatar": True},
        "navigations": [{"label": "status", "url": REAL_STATUS_URL},
                        {"label": "profile", "url": REAL_PROFILE_URL}],
    }
    assert x_dump.disk_pii_residue(record)          # the leak, before

    on_disk = x_dump.scrub_for_disk(record)
    blob = json.dumps(on_disk)
    assert "EXAMPLECO" not in blob
    assert "exampleperson" not in blob
    assert "1011001100110011001" not in blob
    assert x_dump.disk_pii_residue(on_disk) == {}

    # Structure preserved, and the untouched fields are genuinely untouched.
    assert "/status/" in on_disk["url_after_nav"]
    assert on_disk["saved_to"] == record["saved_to"]
    assert on_disk["blocked_url_markers"] == ["/account/access"]
    assert on_disk["login_signals"]["nav_avatar"] is True
    # One shared scrubber per record: the same real handle numbers the same way
    # in every field it appears in.
    assert on_disk["login_signals"]["_url"] == on_disk["url_after_nav"]


def test_removing_the_url_scrub_puts_the_handle_straight_back():
    """Reversion proof: without the scrub the record leaks, with it it does not.

    This is the test that would have failed before the fix — it is written
    against the leak, not against the patch.
    """
    record = {"url_after_nav": REAL_STATUS_URL}
    assert "EXAMPLECO" in json.dumps(record)
    assert x_dump.disk_pii_residue(record) == {
        "identifying_urls": [REAL_STATUS_URL]}

    assert "EXAMPLECO" not in json.dumps(x_dump.scrub_for_disk(record))


def test_every_json_dump_in_the_tool_goes_through_the_disk_scrub():
    """A field added later must not silently reopen the leak.

    The scrub is applied at the WRITE boundary, so this asserts the boundary
    itself rather than the three field names that happen to exist today.
    """
    import inspect
    writers = [fn for name, fn in vars(x_dump).items()
               if callable(fn) and getattr(fn, "__module__", "") == x_dump.__name__
               and "json.dump(" in inspect.getsource(fn)]
    assert writers, "no function writes JSON — did the writer move?"
    for fn in writers:
        assert "scrub_for_disk" in inspect.getsource(fn), fn.__name__


# ─── The search page: four testids, and the one prefix that was refused ───────


def test_the_search_ui_chrome_is_allowlisted_exactly():
    """All four exact. Two are literals; two come from a template, deliberately
    enumerated rather than turned into a prefix."""
    for value in ("radioGroupLocation", "radioGroupPeople",
                  "searchBoxOverflowButton", "searchFiltersAdvancedSearch"):
        assert x_dump.classify_testid(value)[0] == "safe", value
        assert x_dump.pii_residue('<div data-testid="' + value + '"></div>') == {}


def test_the_radio_group_prefix_was_refused_on_purpose():
    """The reason a `radioGroup` family prefix is NOT in the allowlist.

    X builds these as `radioGroup${name}` on a generic component used app-wide.
    The family allowlist is checked BEFORE the embedded-id sweep, so a prefix
    would return "safe" for `radioGroup<id>` without the sweep ever seeing it.
    Enumerating the two search-filter groups keeps every other tail blocking.
    """
    with_id = "radioGroup1011001100110011001"
    assert not any(p == "radioGroup" for p in x_dump._TESTID_SAFE_FAMILY_PREFIXES)
    assert x_dump.classify_testid("radioGroupLocation")[0] == "safe"
    assert x_dump.classify_testid("radioGroupSomethingNew")[0] == "unknown"
    # Blocks today, and that is the property being protected.
    assert x_dump.classify_testid(with_id)[0] != "safe"
    assert x_dump.pii_residue('<div data-testid="' + with_id + '"></div>')

    # With the prefix wrongly added, the classifier IS shadowed. Two different
    # tails show how far that goes, and they do not end the same way.
    with_handle = "radioGroupchrmanning"
    assert x_dump.classify_testid(with_handle)[0] == "unknown"
    assert x_dump.pii_residue('<div data-testid="' + with_handle + '"></div>')

    original = x_dump._TESTID_SAFE_FAMILY_PREFIXES
    try:
        x_dump._TESTID_SAFE_FAMILY_PREFIXES = original + ("radioGroup",)
        assert x_dump.classify_testid(with_id)[0] == "safe"
        assert x_dump.classify_testid(with_handle)[0] == "safe"

        # A numeric tail would still be caught, but by the residue gate's blunt
        # digit sweep rather than by the classifier — the backstop, not the plan.
        assert "long_digit_runs" in x_dump.pii_residue(
            '<div data-testid="' + with_id + '"></div>')
        # A handle-shaped tail has no digits, so nothing catches it and the page
        # saves silently. THAT is why the prefix was refused.
        assert x_dump.pii_residue(
            '<div data-testid="' + with_handle + '"></div>') == {}
    finally:
        x_dump._TESTID_SAFE_FAMILY_PREFIXES = original

    assert x_dump.classify_testid(with_id)[0] != "safe"
    assert x_dump.pii_residue('<div data-testid="' + with_handle + '"></div>')


def test_the_search_chrome_reverts_cleanly_and_shadows_nothing():
    """Reversion both ways, plus the poison check."""
    added = {"radioGroupLocation", "radioGroupPeople",
             "searchBoxOverflowButton", "searchFiltersAdvancedSearch"}
    html = "".join('<div data-testid="' + v + '"></div>' for v in sorted(added))
    assert x_dump.pii_residue(html) == {}

    original = x_dump._TESTID_SAFE_EXACT
    try:
        x_dump._TESTID_SAFE_EXACT = frozenset(original - added)
        for v in sorted(added):
            assert x_dump.classify_testid(v)[0] == "unknown", v
        assert "unrecognized_testids" in x_dump.pii_residue(html)

        x_dump._TESTID_SAFE_EXACT = frozenset(
            original | {"UserName-fakehandle"})
        assert x_dump.classify_testid("UserName-fakehandle") == (
            "handle_stem", "UserName-")
    finally:
        x_dump._TESTID_SAFE_EXACT = original
    for v in sorted(added):
        assert x_dump.classify_testid(v)[0] == "safe", v

    # Scoped: search chrome passes, a handle on the same page still blocks.
    with_handle = html + '<div data-testid="UserCell-fakehandle123"></div>'
    assert "identifying_testids" in x_dump.pii_residue(with_handle)


# ─── The _title leak: the second field that was never behind the gate ─────────
#
# `signals["_title"]` is `driver.title`. On a status page that is the whole
# card, so `x_probe_*.json` carried a real account name and a sentence of real
# post text long after the URLs were cleaned up.

REAL_TITLE = ('(1) EXAMPLECO on X: "Researchers found that a metabolite produced by '
              'gut bacteria can weaken the barrier" / X')


def test_redact_alone_does_not_close_the_title_leak():
    """Why the title needs the text rule and not just `redact()`.

    This is the assumption worth pinning down: `redact()` matches @handles,
    emails and long digit runs, and a display name plus a sentence of post text
    contain none of those. Run over a real title it changes nothing at all.
    """
    assert x_dump._Scrubber().redact(REAL_TITLE) == REAL_TITLE
    assert "EXAMPLECO" in x_dump._Scrubber().redact(REAL_TITLE)


def test_a_status_title_loses_every_word_but_keeps_its_shape():
    """Same rule as every text node in the document: one block char per word."""
    out = x_dump.scrub_title(REAL_TITLE)
    assert "EXAMPLECO" not in out
    assert "metabolite" not in out
    assert "Researchers" not in out
    assert out.startswith("(1) ")
    assert out.endswith(" / X")
    body = out[len("(1) "):-len(" / X")]
    assert set(body.split()) == {x_dump.TEXT_PLACEHOLDER}
    # Word count survives, so the title is still readable AS a shape.
    assert len(body.split()) == len(
        REAL_TITLE[len("(1) "):-len(" / X")].split())


def test_a_route_title_is_not_a_person_and_survives():
    """`Home` is X's own route, not somebody's name — blocking it would throw
    away the only signal the field carries."""
    for title in ("(1) Home / X", "Home / X", "Explore / X",
                  "(12) Notifications / X"):
        assert x_dump.scrub_title(title) == title
        assert x_dump.disk_pii_residue({"_title": title}) == {}


def test_a_profile_title_keeps_the_synthetic_handle_only():
    """The display name goes; the synthesized handle stays, parens and all."""
    out = x_dump.scrub_title("Example Person (@exampleperson) / X")
    assert "Person" not in out
    assert "exampleperson" not in out
    assert re.search(r"@user\d+", out), out
    assert x_dump.disk_pii_residue({"_title": out}) == {}


def test_a_whole_record_is_clean_on_disk_urls_and_titles_together():
    """The end-to-end assertion, both leaks at once."""
    record = {
        "label": "status",
        "url_after_nav": REAL_STATUS_URL,
        "saved_to": r"data\xtest\x_spike\status.scrubbed.html",
        "login_signals": {"_url": REAL_STATUS_URL, "_title": REAL_TITLE,
                          "nav_avatar": True},
    }
    assert set(x_dump.disk_pii_residue(record)) == {
        "identifying_urls", "identifying_titles"}

    on_disk = x_dump.scrub_for_disk(record)
    blob = json.dumps(on_disk)
    assert "EXAMPLECO" not in blob
    assert "metabolite" not in blob
    assert "1011001100110011001" not in blob
    assert x_dump.disk_pii_residue(on_disk) == {}
    assert on_disk["login_signals"]["nav_avatar"] is True
    assert "/status/" in on_disk["url_after_nav"]


def test_removing_the_title_scrub_puts_the_post_text_straight_back():
    """Reversion proof for the title, written against the leak."""
    record = {"login_signals": {"_title": REAL_TITLE}}
    assert "EXAMPLECO" in json.dumps(record)
    assert "identifying_titles" in x_dump.disk_pii_residue(record)

    assert "EXAMPLECO" not in json.dumps(x_dump.scrub_for_disk(record))


def test_the_title_scrub_only_fires_on_title_keys():
    """Scoped: the walker is key-aware, so a title rule cannot eat a URL field
    or a label, and a URL rule cannot eat a title."""
    out = x_dump.scrub_for_disk({"label": "timeline_foryou",
                                "login_state": "logged_in",
                                "_title": "(1) Home / X"})
    assert out["label"] == "timeline_foryou"
    assert out["login_state"] == "logged_in"
    assert out["_title"] == "(1) Home / X"


@pytest.mark.xfail(strict=True, reason=(
    "PRE-EXISTING BUG, found 2026-08-28, logged in .dev/BACKLOG.md. "
    "_TESTID_DIGIT_ID_RE and _TESTID_LONG_ID_RE contain literal backspace "
    "bytes (0x08) where \\b word boundaries were intended, so neither ever "
    "matches and classify_testid never returns 'embedded_id'. FAIL-SAFE: such "
    "a value falls through to 'unknown', which still blocks the save, so "
    "nothing leaks — but the auto-sanitize branch in _Scrubber._testid is dead "
    "code. Fixing it moves those values from 'block for triage' to 'silently "
    "sanitized', which is a gate behaviour change and wants a human's call. "
    "This test flips to a failure the moment the regexes are repaired, which "
    "is the reminder to re-read that branch."))
def test_the_embedded_id_sweep_is_currently_dead():
    assert x_dump.classify_testid("foo-1011001100110011001")[0] == "embedded_id"


# ─── A handle nested inside another URL's query string ────────────────────────
#
# Found while verifying the url_after_nav fix against the real dump: X embeds a
# whole permalink inside an oembed link. `_href` was front-anchored, so the post
# id was synthesized correctly while the handle beside it rode through — and
# `pii_residue` never caught it, because a bare `EXAMPLECO` is not `@EXAMPLECO`. Once
# the allowlist stopped blocking these pages, that would have SAVED.

NESTED = ("https://publish.x.com/oembed?url="
          "https://x.com/EXAMPLECO/status/1011001100110011001")


def test_a_handle_nested_in_a_query_string_is_redacted():
    out = x_dump.scrub_html('<a href="' + NESTED + '">t</a>')
    assert "EXAMPLECO" not in out
    assert "1011001100110011001" not in out
    # Structure survives: still an oembed link to a status permalink.
    assert "publish.x.com/oembed?url=" in out
    assert re.search(r"x\.com/user\d+/status/9\d+", out), out


def test_the_api_path_is_not_mistaken_for_a_person():
    """Host-scoped on purpose: `publish.x.com/oembed` is an API path, and a
    host-blind rule would rename `oembed` to user1 and break the shape."""
    assert "oembed" in x_dump.scrub_html('<a href="' + NESTED + '">t</a>')


def test_the_nested_handle_was_invisible_to_the_old_text_gate():
    """Why this needed a scrub and not just the gate: `EXAMPLECO` has no @, and the
    id beside it is already synthetic, so nothing in the old residue check
    flagged the string at all."""
    already_scrubbed_id = NESTED.replace("1011001100110011001",
                                         "9999999999999990001")
    assert x_dump._HANDLE_RE.findall(already_scrubbed_id) == []
    assert [d for d in x_dump._LONG_DIGITS_RE.findall(already_scrubbed_id)
            if not d.startswith("9")] == []
    # The disk-side gate does see it now.
    assert "identifying_urls" in x_dump.disk_pii_residue(
        {"url_after_nav": already_scrubbed_id})


def test_removing_the_nested_rule_puts_the_handle_back():
    """Reversion proof, written against the leak."""
    original = x_dump._PROFILE_IN_URL_RE
    try:
        # A pattern that cannot match anything = the old front-anchored behaviour.
        x_dump._PROFILE_IN_URL_RE = re.compile(r"(?!x)x")
        out = x_dump.scrub_html('<a href="' + NESTED + '">t</a>')
        assert "EXAMPLECO" in out
    finally:
        x_dump._PROFILE_IN_URL_RE = original
    assert "EXAMPLECO" not in x_dump.scrub_html('<a href="' + NESTED + '">t</a>')


def test_the_real_dump_that_leaked_is_clean_end_to_end():
    """The record that actually leaked, run through the disk scrub.

    Not a synthetic fixture: this is the shape of `x_probe_20260823_161734.json`,
    which carried a real handle in three different fields at once.
    """
    record = {
        "url_after_nav": REAL_STATUS_URL,
        "login_signals": {"_url": REAL_STATUS_URL, "_title": REAL_TITLE},
        "summary": {"status_hrefs_sample": [NESTED, "/EXAMPLECO/status/1011001100110011001"]},
    }
    on_disk = x_dump.scrub_for_disk(record)
    blob = json.dumps(on_disk, ensure_ascii=False)
    for leak in ("EXAMPLECO", "metabolite", "1011001100110011001"):
        assert leak not in blob, leak
    assert x_dump.disk_pii_residue(on_disk) == {}
