"""Phase 5b — the operator surfaces must not fail silently.

Every defect covered here was found by using the dashboard, not by review and
not by the 787 tests that were green at the time. They share one shape: a
control declines to act and says nothing, so the operator cannot tell a refusal
from a hang from a success.
"""

import re
from pathlib import Path

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import providers
from tools import login_check


TEMPLATE = (Path(__file__).parent.parent / "linkedin_automation"
            / "templates" / "dashboard.html").read_text(encoding="utf-8")

# Real calls always carry an argument. The prose in the comments writes bare
# `alert()` / `confirm()` when naming what was removed, and that is not a call.
NATIVE_DIALOG_CALL = re.compile(r"(?<![.\w])(alert|confirm)\(\s*[^)\s]")


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


# ─── No native dialogs anywhere in the dashboard ──────────────────────────────

def test_the_dashboard_calls_no_native_dialogs():
    """The gate that stops this regressing one line at a time.

    An embedded browser suppresses alert() and confirm() and hands the page
    `false`. Observed 2026-08-01: 25 consecutive suppressed confirm() calls on
    Test Connection, with no output of any kind. The same suppression silences
    every error message and defeats the guard on publishing to LinkedIn.
    """
    offenders = [m.group(0) for m in NATIVE_DIALOG_CALL.finditer(TEMPLATE)]
    assert offenders == []


def test_the_replacements_are_actually_present():
    """Zero native calls is also what deleting every message would achieve."""
    assert "function showToast(" in TEMPLATE
    assert "function askConfirm(" in TEMPLATE
    assert TEMPLATE.count("showToast(") >= 33
    assert TEMPLATE.count("askConfirm(") >= 4


def test_every_guard_awaits_its_confirmation():
    """askConfirm returns a Promise. Used without await it is always truthy,
    which would turn each guard into an unconditional proceed — worse than the
    silent refusal it replaced, because one of these publishes to LinkedIn."""
    for match in re.finditer(r".*askConfirm\(.*", TEMPLATE):
        line = match.group(0)
        if "function askConfirm" in line:
            continue
        assert "await askConfirm(" in line, line.strip()


def test_the_guarded_actions_are_still_guarded():
    """The four things that must never happen on a stray click."""
    guarded = re.findall(r"await askConfirm\(\s*\n?\s*`?([^`\n]{0,60})", TEMPLATE)
    joined = " ".join(guarded).lower()
    assert "publish post" in joined          # posts to LinkedIn
    assert "send one small test prompt" in joined  # spends money
    assert "forget the stored" in joined     # destroys a stored key
    assert "remove post" in joined


def test_a_dismissed_confirmation_resolves_false():
    """Escape, Cancel and a backdrop click must all mean no. A confirmation
    that defaults to yes when unanswered is not a guard."""
    body = TEMPLATE.split("function askConfirm(")[1].split("\n// ")[0]
    assert "settle(false)" in body
    assert "e.key === 'Escape'" in body
    assert "cancelBtn.onclick = () => settle(false)" in body
    # And exactly one path says yes.
    assert body.count("settle(true)") == 2  # the OK button and Enter


def test_failures_are_styled_as_failures():
    """A toast that looks like every other toast is a message nobody reads."""
    assert "showToast('Select a profile first', 'error')" in TEMPLATE
    assert "showToast('Paste a key first', 'error')" in TEMPLATE
    assert ".toast.error" in TEMPLATE


# ─── login_check no longer needs a terminal ───────────────────────────────────

class FakeDriver:
    """Reports logged-out for the first ``flips_after`` checks, then logged in."""

    def __init__(self, flips_after=None):
        self.flips_after = flips_after
        self.checks = 0


def _patch_login(monkeypatch, driver_state):
    def fake_is_logged_in(driver):
        driver.checks += 1
        if driver.flips_after is None:
            return False
        return driver.checks > driver.flips_after
    monkeypatch.setattr(pm, "is_logged_in_on_page", fake_is_logged_in)
    return driver_state


def test_the_wait_polls_and_needs_no_stdin(monkeypatch):
    driver = _patch_login(monkeypatch, FakeDriver(flips_after=3))
    slept = []
    clock = iter([0, 1, 2, 3, 4, 5, 6, 7, 8])

    result = login_check.wait_for_manual_login(
        driver, timeout=60, poll=5, sleep=slept.append, clock=lambda: next(clock))

    assert result is True
    assert driver.checks == 4
    assert slept == [5, 5, 5]


def test_the_wait_gives_up_rather_than_hanging(monkeypatch):
    """The fix must not replace a silent forever-block with a quiet one."""
    driver = _patch_login(monkeypatch, FakeDriver(flips_after=None))
    ticks = iter([0] + [i * 10 for i in range(1, 20)])

    result = login_check.wait_for_manual_login(
        driver, timeout=30, poll=10, sleep=lambda s: None, clock=lambda: next(ticks))

    assert result is False


def test_an_already_logged_in_session_returns_at_once(monkeypatch):
    driver = _patch_login(monkeypatch, FakeDriver(flips_after=0))
    slept = []
    result = login_check.wait_for_manual_login(
        driver, timeout=60, poll=5, sleep=slept.append, clock=lambda: 0)
    assert result is True
    assert slept == []          # never waits when there is nothing to wait for


def test_no_bare_input_remains_in_the_tool():
    """The last instance of B7 from Phase 3, which converted four others."""
    source = (Path(__file__).parent.parent / "tools" / "login_check.py").read_text(
        encoding="utf-8")
    # A real call has an argument or is bare `input()` awaiting a keypress; the
    # docstring names ``input()`` when describing what was removed. Match the
    # statement form, not the prose.
    assert not re.search(r"^\s*(?:\w+\s*=\s*)?input\(", source, re.M)


def test_the_wait_does_not_reload_the_page(monkeypatch):
    """A reload mid-login wipes a half-typed form or a verification challenge."""
    class Strict(FakeDriver):
        def get(self, url):  # pragma: no cover - the assertion is that it is never called
            raise AssertionError("the wait re-navigated while the user was logging in")

    driver = _patch_login(monkeypatch, Strict(flips_after=1))
    assert login_check.wait_for_manual_login(
        driver, timeout=60, poll=1, sleep=lambda s: None, clock=lambda: 0) is True


# ─── The key field names the provider it will save to ─────────────────────────

def test_the_key_field_label_is_driven_by_the_dropdown():
    assert 'id="setApiKeyLabel"' in TEMPLATE
    assert "Paste a key for ${(p && p.label) || name}" in TEMPLATE


def test_the_key_list_says_it_is_status_only():
    """It reads as a chooser and is not. A key pasted while the dropdown still
    said OpenAI is stored as the OpenAI key."""
    assert "status only" in TEMPLATE


def test_every_provider_has_a_label_to_show(api_client):
    """The label falls back to the raw name, but a blank one would render
    'Paste a key for '. Assert every shipped provider actually has one."""
    rows = api_client.get("/api/settings/providers").get_json()
    rows = rows["providers"] if isinstance(rows, dict) else rows
    assert len(rows) == len(providers.SPECS)
    for row in rows:
        assert row.get("label"), row.get("name")


# ─── A placeholder key must not read as configured ────────────────────────────

def test_the_env_template_ships_no_placeholder_key():
    """A copied .env reported OpenAI as `set` on first boot, which suppresses
    the actionable missing-key error and turns it into a 401 mid-generation."""
    template = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "_API_KEY=" not in stripped:
            continue
        _, _, value = stripped.partition("=")
        assert value.strip() == "", line


def test_the_xai_default_model_is_marked_verified():
    """Confirmed live 2026-08-01: grok-4 answered, temperature accepted."""
    assert providers.SPECS["xai"].default_model == "grok-4"
    assert providers.SPECS["xai"].default_model_verified is True


def test_an_unverified_default_still_says_so():
    """The flag must stay meaningful. If everything were marked verified the
    UI's warning would be decoration."""
    unverified = [s.name for s in providers.SPECS.values()
                  if s.default_model and not s.default_model_verified]
    assert unverified, "no spec is unverified any more; the warning is now dead UI"


# ─── The credential self-check ────────────────────────────────────────────────

def test_the_check_reports_storage_and_never_the_password(api_client, monkeypatch):
    api_client.post("/api/profiles", json={
        "name": "rick", "username": "someone@example.com", "password": "pa55w0rd-secret",
    })
    resp = api_client.get("/api/profiles/rick/credential-check")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["username"] == "someone@example.com"
    assert data["roundtrip_ok"] is True
    assert data["plaintext_in_file"] is False
    # Sweep the WHOLE body, not just the fields we expect to be clean.
    assert "pa55w0rd-secret" not in resp.get_data(as_text=True)


def test_the_check_says_what_it_did_not_check(api_client):
    """The point of the endpoint. Listing what passed while staying quiet about
    what was never tested is how a partial answer gets read as a whole one."""
    api_client.post("/api/profiles", json={
        "name": "rick", "username": "a@b.com", "password": "x",
    })
    data = api_client.get("/api/profiles/rick/credential-check").get_json()
    assert data["checked"]
    joined = " ".join(data["not_checked"]).lower()
    assert "correct on linkedin" in joined
    assert "will not sign in" in joined


def test_a_credential_that_cannot_be_read_back_is_reported(api_client, monkeypatch):
    api_client.post("/api/profiles", json={
        "name": "rick", "username": "a@b.com", "password": "x",
    })
    monkeypatch.setattr(pm, "get_profile_password", lambda profile, name=None: "")
    data = api_client.get("/api/profiles/rick/credential-check").get_json()
    assert data["roundtrip_ok"] is False
    assert "no password to use" in data["detail"]


def test_a_keychain_error_is_reported_not_swallowed(api_client, monkeypatch):
    api_client.post("/api/profiles", json={
        "name": "rick", "username": "a@b.com", "password": "x",
    })

    def boom(profile, name=None):
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(pm, "get_profile_password", boom)
    data = api_client.get("/api/profiles/rick/credential-check").get_json()
    assert data["roundtrip_ok"] is False
    assert "RuntimeError" in data["detail"]


def test_an_unknown_profile_is_a_404(api_client):
    resp = api_client.get("/api/profiles/nobody/credential-check")
    assert resp.status_code == 404
    assert "nobody" in resp.get_json()["error"]


def test_the_check_never_touches_the_browser(api_client, monkeypatch):
    """It must be usable when there is no session at all, which is exactly when
    someone reaches for it."""
    def explode(*a, **k):  # pragma: no cover - asserted by never being hit
        raise AssertionError("the credential check opened a browser")

    monkeypatch.setattr(pm, "create_driver", explode)
    api_client.post("/api/profiles", json={
        "name": "rick", "username": "a@b.com", "password": "x",
    })
    assert api_client.get("/api/profiles/rick/credential-check").status_code == 200
