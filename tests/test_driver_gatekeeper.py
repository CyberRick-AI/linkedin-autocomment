"""A freshly downloaded chromedriver has to actually run (2026-08-03).

Rick's scrape failed at browser startup, having never reached LinkedIn:

    Service .../chromedriver unexpectedly exited. Status code was: -9

-9 is SIGKILL. Chrome had auto-updated overnight, webdriver_manager downloaded
a matching chromedriver at 03:10, and macOS killed the new binary before it
ran a line. ``spctl`` reported it rejected; running it by hand exited 137 with
no output at all.

The cause is a code signature macOS will not accept, not the quarantine
attribute: the attribute present was ``com.apple.provenance``, which is
protected and cannot be removed with ``xattr``. An ad-hoc ``codesign`` fixes
it, and that is what a person would do by hand.

Offline: subprocess is faked throughout. Nothing here signs or runs a real
binary.
"""

import subprocess

import pytest

from linkedin_automation import platform_compat as pc


class FakeRun:
    """Stands in for subprocess.run, keyed on the command being invoked."""

    def __init__(self, version_codes, codesign_code=0):
        # One entry per --version probe, consumed in order.
        self.version_codes = list(version_codes)
        self.codesign_code = codesign_code
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == "codesign":
            return subprocess.CompletedProcess(cmd, self.codesign_code, b"", b"")
        code = self.version_codes.pop(0) if self.version_codes else 0
        return subprocess.CompletedProcess(cmd, code, b"", b"")


@pytest.fixture
def on_macos(monkeypatch):
    monkeypatch.setattr(pc, "IS_MACOS", True)


DRIVER = "/fake/.wdm/chromedriver"


def test_a_working_driver_is_left_alone(on_macos, monkeypatch):
    """Do not re-sign something that already runs. This is on every startup."""
    fake = FakeRun(version_codes=[0])
    monkeypatch.setattr(pc.subprocess, "run", fake)

    assert pc.ensure_driver_runnable(DRIVER) is True
    assert not any(c[0] == "codesign" for c in fake.calls), (
        "it re-signed a driver that was already fine"
    )


def test_a_killed_driver_is_resigned_and_rechecked(on_macos, monkeypatch, caplog):
    """The actual repair: probe fails, sign, probe again."""
    fake = FakeRun(version_codes=[137, 0])   # SIGKILL, then fine after signing
    monkeypatch.setattr(pc.subprocess, "run", fake)

    with caplog.at_level("WARNING", logger="linkedin_automation.platform_compat"):
        assert pc.ensure_driver_runnable(DRIVER) is True

    signed = [c for c in fake.calls if c[0] == "codesign"]
    assert signed == [["codesign", "--force", "--sign", "-", DRIVER]]
    assert "Gatekeeper" in caplog.text, "the log does not name the likely cause"


def test_a_driver_that_stays_broken_says_what_to_try(on_macos, monkeypatch, caplog):
    """Never silently give up on the thing that stops every browser run."""
    fake = FakeRun(version_codes=[137, 137])   # still dead after signing
    monkeypatch.setattr(pc.subprocess, "run", fake)

    with caplog.at_level("ERROR", logger="linkedin_automation.platform_compat"):
        assert pc.ensure_driver_runnable(DRIVER) is False

    assert ".wdm" in caplog.text, "it does not suggest clearing the driver cache"
    assert "codesign" in caplog.text, "it does not give the manual command"


def test_a_failing_codesign_is_reported_not_swallowed(on_macos, monkeypatch, caplog):
    fake = FakeRun(version_codes=[137, 137], codesign_code=1)
    monkeypatch.setattr(pc.subprocess, "run", fake)

    with caplog.at_level("ERROR", logger="linkedin_automation.platform_compat"):
        assert pc.ensure_driver_runnable(DRIVER) is False

    assert "codesign failed" in caplog.text


def test_it_never_raises_into_the_browser_startup(on_macos, monkeypatch):
    """A repair that throws is worse than the problem it repairs.

    Selenium's own error is at least about the browser. An exception from here
    would replace it with one about signing.
    """
    def boom(*a, **k):
        raise OSError("codesign not found")

    monkeypatch.setattr(pc.subprocess, "run", boom)

    assert pc.ensure_driver_runnable(DRIVER) is False


def test_nothing_happens_off_macos(monkeypatch):
    """Gatekeeper is macOS. Linux and Windows must not run codesign."""
    monkeypatch.setattr(pc, "IS_MACOS", False)
    calls = []
    monkeypatch.setattr(pc.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    assert pc.ensure_driver_runnable(DRIVER) is True
    assert calls == []


def test_an_empty_path_is_not_probed(on_macos, monkeypatch):
    calls = []
    monkeypatch.setattr(pc.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    assert pc.ensure_driver_runnable("") is True
    assert calls == []


def test_the_probe_treats_a_timeout_as_broken(on_macos, monkeypatch):
    """A driver that hangs is not a driver that works."""
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="chromedriver", timeout=15)

    monkeypatch.setattr(pc.subprocess, "run", hang)

    assert pc.driver_runs(DRIVER) is False


def test_driver_creation_repairs_before_handing_it_to_selenium():
    """The wiring. Repairing after Selenium has already failed is no use."""
    import inspect

    from linkedin_automation import profile_manager as pm

    source = inspect.getsource(pm.create_driver)
    repair = source.index("ensure_driver_runnable")
    service = source.index("Service(driver_path)")
    assert repair < service, (
        "the driver is handed to Selenium before it is checked"
    )
