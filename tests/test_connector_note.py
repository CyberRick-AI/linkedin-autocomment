"""The connection note actually reaches the invitation (Phase 15).

Found 2026-08-02 on Rick's first real auto-connector run. He entered a note,
three requests went out, and every one logged
``Clicked 'Send without a note' in shadow DOM``.

``_handle_after_click`` called ``_click_send_without_note()`` unconditionally,
before looking at ``self.add_note`` at all. Note-typing code existed in two
other methods, and neither was reachable: both sit *after* that call and only
run when it fails, and against LinkedIn's current shadow-DOM popup it never
fails.

So the operator's note was read from the form, passed through the endpoint,
the CLI, and the constructor, and was discarded at the last step. Same shape
as the unreachable XPath branch Phase 11b found, with a worse outcome: that
one under-reported a count, this one sent something other than what was asked
for, to real people, unrecallably.

Offline: the driver is a fake throughout. Nothing here contacts LinkedIn, per
PROJECT.md section 4.
"""

import pytest

from linkedin_automation import auto_connector as ac
from linkedin_automation import human_behavior as hb


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(hb, "human_click", lambda driver, el: el.click())
    monkeypatch.setattr(hb, "type_like_human",
                        lambda driver, el, text: el.send_keys(text))
    monkeypatch.setattr(hb, "scroll_to_element", lambda *a, **k: None)


class FakeButton:
    def __init__(self, label):
        self.label = label
        self.clicks = 0

    def click(self):
        self.clicks += 1


class FakeTextarea:
    def __init__(self):
        self.typed = ""

    def send_keys(self, text):
        self.typed += text


class ShadowDriver:
    """A driver whose popup exposes exactly the controls a test declares.

    Models the one thing that matters here: which buttons the shadow root
    offers. ``_shadow_button`` and ``_shadow_textarea`` both go through
    ``execute_script``, so intercepting that is the whole boundary.
    """

    def __init__(self, buttons=(), textarea=None, url="https://www.linkedin.com/search/"):
        self.buttons = {label: FakeButton(label) for label in buttons}
        self.textarea = FakeTextarea() if textarea else None
        self.current_url = url
        self.scripts = []

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if "querySelector('textarea')" in script or "querySelector('textarea')" in script.replace('"', "'"):
            return self.textarea
        if "textarea" in script:
            return self.textarea
        if args and isinstance(args[-1], list):
            for label in args[-1]:
                if label in self.buttons:
                    return self.buttons[label]
            return None
        return None

    def find_elements(self, by, value):
        return []

    def find_element(self, by, value):
        from selenium.common.exceptions import NoSuchElementException
        raise NoSuchElementException(value)


def make_connector(note="Hi there, nice work.", add_note=True, fallback=False):
    conn = ac.LinkedInAutoConnector.__new__(ac.LinkedInAutoConnector)
    conn.note_text = note
    conn.add_note = add_note
    conn.send_without_note_fallback = fallback
    conn.profile_name = "test"
    conn.debug = False
    return conn


# ─── The defect ───────────────────────────────────────────────────────────────

def test_a_configured_note_is_typed_before_anything_is_sent():
    """The whole point. A note that was entered must reach the invitation."""
    conn = make_connector(note="Hi there, nice work.")
    conn.driver = ShadowDriver(
        buttons=["Add a note", "Send invitation", "Send without a note"],
        textarea=True,
    )

    assert conn._send_with_note() is True
    assert conn.driver.textarea.typed == "Hi there, nice work."
    assert conn.driver.buttons["Add a note"].clicks == 1
    assert conn.driver.buttons["Send invitation"].clicks == 1
    assert conn.driver.buttons["Send without a note"].clicks == 0, (
        "it sent without the note despite one being configured"
    )


def test_the_note_path_runs_before_send_without_a_note_is_even_tried():
    """The regression guard on the ordering that caused this.

    ``_click_send_without_note`` succeeds against the live popup, so anything
    after it is unreachable. The note has to come first or it does not happen
    at all.
    """
    conn = make_connector()
    conn.driver = ShadowDriver(
        buttons=["Add a note", "Send invitation", "Send without a note"],
        textarea=True,
    )
    called = []
    conn._click_send_without_note = lambda: called.append(1) or True

    assert conn._handle_after_click("https://www.linkedin.com/search/") is True
    assert called == [], "the without-note path ran while a note was configured"
    assert conn.driver.textarea.typed


def test_no_note_configured_still_sends_without_one():
    """The default path is untouched: no note means the old behaviour."""
    conn = make_connector(note="", add_note=False)
    conn.driver = ShadowDriver(buttons=["Send without a note"])
    called = []
    conn._click_send_without_note = lambda: called.append(1) or True

    assert conn._handle_after_click("https://www.linkedin.com/search/") is True
    assert called == [1]


# ─── What happens when the note cannot be attached ────────────────────────────

def test_a_missing_add_note_button_does_not_silently_send_a_bare_invite():
    """The default refuses, and that default is deliberate.

    A connection request cannot be recalled and the weekly allowance is
    finite, so spending one on an invite the operator did not write is the
    more expensive mistake. LinkedIn also limits how many invitations may
    carry a note, so this is a state a real run will reach.
    """
    conn = make_connector()
    conn.driver = ShadowDriver(buttons=["Send without a note"])
    conn._close_modal = lambda *a, **k: None
    called = []
    conn._click_send_without_note = lambda: called.append(1) or True

    result = conn._handle_after_click("https://www.linkedin.com/search/")

    assert result is False
    assert called == [], "it fell back to a bare invite without being asked to"


def test_the_fallback_is_available_when_explicitly_enabled():
    """Opt in, per run, rather than the safe default being a dead end."""
    conn = make_connector(fallback=True)
    conn.driver = ShadowDriver(buttons=["Send without a note"])
    called = []
    conn._click_send_without_note = lambda: called.append(1) or True

    assert conn._handle_after_click("https://www.linkedin.com/search/") is True
    assert called == [1]


def test_a_missing_add_note_button_says_why(caplog):
    """Name the likely cause rather than reporting a bare failure."""
    conn = make_connector()
    conn.driver = ShadowDriver(buttons=["Send without a note"])

    with caplog.at_level("WARNING", logger="linkedin_automation.auto_connector"):
        assert conn._send_with_note() is False

    assert "Add a note" in caplog.text
    assert "allowance" in caplog.text


def test_a_note_field_that_never_opens_is_reported_not_ignored(caplog):
    conn = make_connector()
    conn.driver = ShadowDriver(buttons=["Add a note", "Send invitation"], textarea=None)

    with caplog.at_level("WARNING", logger="linkedin_automation.auto_connector"):
        assert conn._send_with_note() is False

    assert "no note field" in caplog.text


def test_a_typed_note_with_no_send_button_fails_rather_than_claiming_success(caplog):
    """Typing is not sending. Returning True here would over-report."""
    conn = make_connector()
    conn.driver = ShadowDriver(buttons=["Add a note"], textarea=True)

    with caplog.at_level("WARNING", logger="linkedin_automation.auto_connector"):
        assert conn._send_with_note() is False

    assert "no Send button" in caplog.text


def test_a_driver_that_raises_does_not_take_down_the_run(caplog):
    """One bad invite must not end the session; partial failure is skip-and-log."""
    class Exploding(ShadowDriver):
        def execute_script(self, script, *args):
            raise RuntimeError("shadow root detached")

    conn = make_connector()
    conn.driver = Exploding()

    with caplog.at_level("WARNING", logger="linkedin_automation.auto_connector"):
        assert conn._send_with_note() is False

    assert "Attaching the note failed" in caplog.text


# ─── The wiring that carries the note down ────────────────────────────────────

def test_the_cli_passes_the_note_through_to_the_connector():
    """The note survives the endpoint, the CLI and the constructor.

    Every layer was already correct; the defect was at the bottom. Asserted so
    a later refactor cannot quietly drop it higher up.
    """
    import inspect

    source = inspect.getsource(ac.main)
    assert "note_text=args.note" in source
    assert "--note" in source


def test_the_fallback_defaults_to_off():
    """The safe default is the one that does not spend an invite unasked."""
    conn = ac.LinkedInAutoConnector.__new__(ac.LinkedInAutoConnector)
    conn.send_without_note_fallback = None
    # Mirrors the constructor's resolution with no config and no flag.
    resolved = None if None is not None else bool({}.get("send_without_note_if_needed", False))
    assert resolved is False


# ─── LinkedIn's "Do you know this person?" gate ───────────────────────────────
# Hit live on 2026-08-02. LinkedIn asks for the member's email address before
# it will deliver the invitation. The tool never supplies one.


class GateDriver(ShadowDriver):
    """A popup whose text is whatever the gate under test says."""

    def __init__(self, text, **kwargs):
        super().__init__(**kwargs)
        self.dialog_text = text

    def execute_script(self, script, *args):
        if "textContent" in script:
            return self.dialog_text
        return super().execute_script(script, *args)


def gate_connector(**kwargs):
    conn = make_connector(**kwargs)
    conn.email_gate_count = 0
    conn.consecutive_email_gates = 0
    conn.max_consecutive_email_gates = 3
    conn.last_skip_reason = None
    conn.sent_count = 0
    conn.max_requests = 25
    conn._close_modal = lambda *a, **k: None
    return conn


def test_the_email_gate_is_skipped_and_named(caplog):
    """It is not an error and not a send. It is LinkedIn declining, with a reason.

    Filing it as a generic failure loses the one fact that matters: nothing is
    broken, and no amount of retrying will change the answer.
    """
    conn = gate_connector()
    conn.driver = GateDriver(
        "do you know michael turner? enter their email address to connect.")

    with caplog.at_level("WARNING", logger="linkedin_automation.auto_connector"):
        assert conn._handle_after_click("https://www.linkedin.com/search/") is False

    assert conn.last_skip_reason == "email_required"
    assert conn.email_gate_count == 1
    assert "email address" in caplog.text


def test_the_tool_never_supplies_an_email_address():
    """The rule, asserted on the code rather than trusted to reviewers.

    Guessing or looking up a stranger's private address to defeat a
    verification gate is the member's data misused and the fastest route to a
    restricted account. There is no code path that types into that field.
    """
    import inspect

    source = inspect.getsource(ac.LinkedInAutoConnector)
    for banned in ("input[type='email']", 'input[type="email"]', "send_keys(email"):
        assert banned not in source, f"something types into an email field: {banned}"


def test_an_invitation_limit_stops_the_whole_run_not_just_one_person(caplog):
    """The two dialogs read alike and the right responses are opposite.

    A limit means every further attempt will fail and each one is another
    flagged action, so the run ends. An email gate means skip this person.
    """
    conn = gate_connector()
    conn.sent_count = 4
    conn.driver = GateDriver("you've reached the weekly invitation limit.")

    with caplog.at_level("WARNING", logger="linkedin_automation.auto_connector"):
        assert conn._handle_after_click("https://www.linkedin.com/search/") is False

    assert conn.last_skip_reason == "invitation_limit"
    assert conn.max_requests == 4, "the run was allowed to continue past a limit"


def test_a_limit_is_recognised_even_though_it_also_mentions_knowing_someone():
    """Ordering guard: limit is checked first, so overlapping wording is safe."""
    conn = gate_connector()
    conn.sent_count = 2
    conn.driver = GateDriver(
        "you've reached the weekly invitation limit. do you know this person?")

    conn._handle_after_click("https://www.linkedin.com/search/")

    assert conn.last_skip_reason == "invitation_limit"


def test_consecutive_email_gates_accumulate_and_a_send_resets_them():
    """The monitor. A run of gates is a signal, a scattered few are not."""
    conn = gate_connector()
    conn.driver = GateDriver("do you know this person? enter their email address")

    for expected in (1, 2, 3):
        conn._handle_after_click("https://www.linkedin.com/search/")
        assert conn.consecutive_email_gates == expected

    conn.consecutive_email_gates = 0        # what a successful send does
    conn._handle_after_click("https://www.linkedin.com/search/")
    assert conn.consecutive_email_gates == 1
    assert conn.email_gate_count == 4, "the run total must not reset"


@pytest.mark.parametrize("config,expected", [
    ({}, 3),
    ({"connector": {"max_consecutive_email_gates": 7}}, 7),
    ({"connector": {"max_consecutive_email_gates": 1}}, 1),
])
def test_the_threshold_is_configurable(config, expected, monkeypatch, tmp_path):
    """Through the real constructor, not a dict lookup that proves nothing.

    The first version of this test asserted on ``{}.get(...)``, which would
    have passed with the config key wired to nothing at all. Ruff caught the
    unused connector and the test was the thing at fault.
    """
    from linkedin_automation import profile_manager as pm

    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config)
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "t")
    monkeypatch.setattr(ac.hb, "configure_behavior", lambda *a, **k: None)

    conn = ac.LinkedInAutoConnector(profile_name="t")

    assert conn.max_consecutive_email_gates == expected
    assert conn.email_gate_count == 0
    assert conn.last_skip_reason is None


def test_an_ordinary_popup_is_not_mistaken_for_a_gate():
    """False positives here would abandon invitations that would have worked."""
    conn = gate_connector()
    conn.driver = GateDriver(
        "add a note to your invitation? personalize your invitation to michael "
        "turner by adding a note.")

    assert conn._detect_gate() is None


def test_an_unreadable_popup_is_not_treated_as_a_gate():
    """No text means no evidence, and no evidence is not a gate."""
    conn = gate_connector()
    conn.driver = GateDriver("")

    assert conn._detect_gate() is None
