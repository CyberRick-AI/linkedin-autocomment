"""Tests for three-state login status and the no-terminal guard (phase-3).

Offline: the driver is a stub exposing only ``current_url``, and stdin is
faked. Nothing launches a browser."""

import logging
import types

import pytest

from linkedin_automation import profile_manager as pm


class FakeDriver:
    """Minimal driver stub: a URL, or an exception when asked for one."""

    def __init__(self, url=None, raises=None, get_raises=None):
        self._url = url
        self._raises = raises
        self._get_raises = get_raises
        self.visited = []

    @property
    def current_url(self):
        if self._raises:
            raise self._raises
        return self._url

    def get(self, url):
        if self._get_raises:
            raise self._get_raises
        self.visited.append(url)


# ─── the third state ─────────────────────────────────────────────────────────

def test_feed_url_is_logged_in():
    d = FakeDriver("https://www.linkedin.com/feed/")
    assert pm.login_status_on_page(d) is pm.LoginStatus.LOGGED_IN


def test_authwall_is_logged_out():
    d = FakeDriver("https://www.linkedin.com/authwall?trk=x")
    assert pm.login_status_on_page(d) is pm.LoginStatus.LOGGED_OUT


def test_unrecognised_url_is_unknown_not_logged_out():
    """The whole point of the change.

    An unrecognised URL used to return False, which the caller read as
    "logged out" and answered by typing the user's password.
    """
    d = FakeDriver("https://www.linkedin.com/some/page/nobody/has/seen")
    assert pm.login_status_on_page(d) is pm.LoginStatus.UNKNOWN


def test_blank_url_is_unknown():
    """A page that never loaded reports an empty URL. Not evidence of logout."""
    assert pm.login_status_on_page(FakeDriver("")) is pm.LoginStatus.UNKNOWN


def test_dead_driver_is_unknown_not_logged_out():
    d = FakeDriver(raises=RuntimeError("no such window"))
    assert pm.login_status_on_page(d) is pm.LoginStatus.UNKNOWN


def test_failed_navigation_is_unknown_not_logged_out():
    """A navigation timeout must never be reported as an expired session."""
    d = FakeDriver(get_raises=TimeoutError("page load timed out"))
    assert pm.login_status(d) is pm.LoginStatus.UNKNOWN


def test_unknown_is_logged_with_the_offending_url(caplog):
    """8.2: the failure must be diagnosable from logs alone."""
    d = FakeDriver("https://www.linkedin.com/unrecognised-thing")
    with caplog.at_level(logging.WARNING):
        pm.login_status_on_page(d)
    assert "UNKNOWN" in caplog.text
    assert "unrecognised-thing" in caplog.text


# ─── the bool wrapper still behaves for existing callers ─────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/feed/", True),
    ("https://www.linkedin.com/authwall", False),
    ("https://www.linkedin.com/mystery", False),
])
def test_is_logged_in_on_page_still_returns_bool(url, expected):
    assert pm.is_logged_in_on_page(FakeDriver(url)) is expected


# ─── login() must not type the password on an ambiguous result ───────────────

def _profile():
    return {"name": "t", "username": "u@example.com", "password": "pw",
            "password_location": pm.LOCATION_FILE}


def test_login_refuses_credential_entry_when_status_is_unknown(monkeypatch, caplog):
    """The core safety property of this phase.

    UNKNOWN must not reach the branch that fills the login form. Automated
    credential entry is the most heavily flagged action available, and it was
    previously triggered by a page that simply had not loaded.
    """
    monkeypatch.setattr(pm, "login_status", lambda d: pm.LoginStatus.UNKNOWN)

    typed = []
    driver = types.SimpleNamespace(
        find_element=lambda *a, **k: typed.append(a) or types.SimpleNamespace(
            send_keys=lambda *_: None, click=lambda: None),
        get=lambda url: None,
        current_url="",
    )

    with caplog.at_level(logging.ERROR):
        assert pm.login(driver, _profile()) is False

    assert typed == [], "login() touched the form on an ambiguous status"
    assert "ambiguous" in caplog.text.lower()


def test_login_short_circuits_when_already_logged_in(monkeypatch):
    monkeypatch.setattr(pm, "login_status", lambda d: pm.LoginStatus.LOGGED_IN)
    assert pm.login(object(), _profile()) is True


# ─── no terminal means do not block ──────────────────────────────────────────

def test_wait_for_human_returns_false_without_a_tty(monkeypatch):
    """Dashboard subprocesses have no stdin. Blocking there hangs forever."""
    monkeypatch.setattr(pm.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert pm.wait_for_human("prompt") is False


def test_wait_for_human_returns_false_when_stdin_is_absent(monkeypatch):
    monkeypatch.setattr(pm.sys, "stdin", None)
    assert pm.wait_for_human("prompt") is False


def test_wait_for_human_prompts_when_a_tty_is_present(monkeypatch):
    monkeypatch.setattr(pm.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert pm.wait_for_human("prompt") is True


def test_wait_for_human_survives_eof(monkeypatch):
    """Ctrl+D or a closed pipe must return, not raise."""
    monkeypatch.setattr(pm.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))

    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert pm.wait_for_human("prompt") is False


# ─── BOM tolerance ───────────────────────────────────────────────────────────

def test_profiles_file_with_a_bom_still_loads(profiles_store):
    """PowerShell's Set-Content -Encoding UTF8 writes a BOM by default.

    That used to raise UnicodeDecodeError, get swallowed, and silently report
    zero profiles.
    """
    import json
    profiles_store.write_text(
        "﻿" + json.dumps({"profiles": {"rick": {"username": "u@example.com"}},
                               "default": "rick"}),
        encoding="utf-8",
    )
    data = pm.load_profiles()
    assert "rick" in data["profiles"], "a BOM silently emptied the profile store"
    assert data["default"] == "rick"


def test_profiles_file_without_a_bom_still_loads(profiles_store):
    import json
    profiles_store.write_text(
        json.dumps({"profiles": {"rick": {"username": "u@example.com"}}, "default": "rick"}),
        encoding="utf-8",
    )
    assert "rick" in pm.load_profiles()["profiles"]


# ─── explicit page load ceiling ──────────────────────────────────────────────

def test_page_load_timeout_is_set_and_bounded():
    """Selenium's 300s default turns a hang into a multi-minute stall."""
    assert 0 < pm.PAGE_LOAD_TIMEOUT_SECONDS < 300
