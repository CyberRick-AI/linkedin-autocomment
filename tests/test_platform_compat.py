"""Tests for the per-OS shims (macos-support).

Offline: the platform flags are module constants and the clipboard reader is
exercised with subprocess.run faked out, so nothing shells out or touches a
real clipboard."""

import subprocess
import sys
import types

import pytest
from selenium.webdriver.common.keys import Keys

from linkedin_automation import platform_compat as pc


def _reload_as(monkeypatch, platform_name):
    """Reimport platform_compat as if running on ``platform_name``."""
    import importlib

    monkeypatch.setattr(sys, "platform", platform_name)
    return importlib.reload(pc)


@pytest.fixture(autouse=True)
def _restore_module():
    """Leave the real module in place for every other test in the suite."""
    yield
    import importlib

    importlib.reload(pc)


# ─── modifier key ────────────────────────────────────────────────────────────

def test_macos_submits_with_command(monkeypatch):
    mod = _reload_as(monkeypatch, "darwin")
    assert mod.submit_modifier() == Keys.COMMAND


def test_windows_submits_with_control(monkeypatch):
    mod = _reload_as(monkeypatch, "win32")
    assert mod.submit_modifier() == Keys.CONTROL


def test_linux_submits_with_control(monkeypatch):
    mod = _reload_as(monkeypatch, "linux")
    assert mod.submit_modifier() == Keys.CONTROL


# ─── clipboard command selection ─────────────────────────────────────────────

def test_macos_uses_pbpaste(monkeypatch):
    mod = _reload_as(monkeypatch, "darwin")
    assert mod.clipboard_commands() == [["pbpaste"]]


def test_windows_uses_powershell(monkeypatch):
    mod = _reload_as(monkeypatch, "win32")
    assert mod.clipboard_commands() == [
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
    ]


def test_linux_offers_xclip_then_xsel(monkeypatch):
    mod = _reload_as(monkeypatch, "linux")
    assert [argv[0] for argv in mod.clipboard_commands()] == ["xclip", "xsel"]


# ─── clipboard reading ───────────────────────────────────────────────────────

def _fake_run(stdout="", raises=None, seen=None):
    def run(argv, **kwargs):
        if seen is not None:
            seen.append(argv[0])
        if raises is not None:
            raise raises
        return types.SimpleNamespace(stdout=stdout)

    return run


def test_read_clipboard_returns_stripped_text(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="  https://x  \n"))
    assert pc.read_clipboard() == "https://x"


def test_read_clipboard_missing_helper_returns_empty(monkeypatch):
    # Linux without xclip/xsel installed: FileNotFoundError, not a crash.
    monkeypatch.setattr(subprocess, "run", _fake_run(raises=FileNotFoundError()))
    assert pc.read_clipboard() == ""


def test_read_clipboard_survives_subprocess_error(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_run(raises=subprocess.TimeoutExpired("pbpaste", 10))
    )
    assert pc.read_clipboard() == ""


def test_read_clipboard_falls_through_to_second_candidate(monkeypatch):
    """On Linux an empty xclip result should still try xsel."""
    mod = _reload_as(monkeypatch, "linux")
    seen = []

    def run(argv, **kwargs):
        seen.append(argv[0])
        # xclip returns nothing; xsel has the value.
        return types.SimpleNamespace(stdout="" if argv[0] == "xclip" else "found")

    monkeypatch.setattr(subprocess, "run", run)
    assert mod.read_clipboard() == "found"
    assert seen == ["xclip", "xsel"]


def test_read_clipboard_stops_at_first_hit(monkeypatch):
    mod = _reload_as(monkeypatch, "linux")
    seen = []
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="first", seen=seen))
    assert mod.read_clipboard() == "first"
    assert seen == ["xclip"]  # xsel never invoked


# ─── wiring ──────────────────────────────────────────────────────────────────

def test_scraper_get_clipboard_delegates(monkeypatch):
    """post_finder's fallback must route through the shim, not PowerShell."""
    import logging

    from linkedin_automation.post_finder import LinkedInScraper

    monkeypatch.setattr(pc, "read_clipboard", lambda: "delegated")
    scraper = LinkedInScraper(driver=None, logger=logging.getLogger("test"))
    assert scraper._get_clipboard() == "delegated"


def test_describe_labels_known_platforms(monkeypatch):
    assert _reload_as(monkeypatch, "darwin").describe() == "macOS"
    assert _reload_as(monkeypatch, "win32").describe() == "Windows"
