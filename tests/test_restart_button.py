"""Restarting from the dashboard.

A running dashboard does not pick up an update to code it has already
imported, and there is no symptom when a restart is missed: the app keeps
working exactly as it did before, which reads as the fix not working. On the
macOS app or a double-clicked launcher there is no terminal to restart it from.

Ported from the beta branch (34732ca), adapted to this dashboard: it runs
``app.run(debug=True)``, so the Werkzeug reloader makes it two processes, and
the restart has to wait for both. The Windows liveness check is new; the beta
version of ``process_alive`` failed on Windows CI (see the tests at the bottom).

Offline: the helper's spawn and clock are faked, the endpoint's process exit is
patched out, and nothing binds a port.
"""

import os
import subprocess
import sys

import pytest

from linkedin_automation import dashboard as dash
from linkedin_automation import restart_helper as rh


@pytest.fixture(autouse=True)
def never_actually_exit(monkeypatch):
    """os._exit in a test run would take pytest with it."""
    exits = []
    monkeypatch.setattr(dash.os, "_exit", lambda code: exits.append(code))
    # Shorten the grace period rather than patching time.sleep, which is the
    # shared module and would also disable this test's own waiting loop.
    monkeypatch.setattr(dash, "RESTART_GRACE_SECONDS", 0)
    return exits


@pytest.fixture(autouse=True)
def not_under_the_reloader(monkeypatch):
    """The suite is not the reloader's child; tests that need it opt in."""
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(dash.subprocess, "Popen",
                        lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc())
    return calls


def _helper_args(cmd):
    """The arguments after ``-m linkedin_automation.restart_helper``."""
    return cmd[cmd.index("linkedin_automation.restart_helper") + 1:]


# ─── The endpoint ─────────────────────────────────────────────────────────────

def test_restart_launches_a_detached_helper(api_client, spawned):
    """The helper has to outlive the server, or nothing starts the replacement."""
    res = api_client.post("/api/restart")

    assert res.status_code == 200
    assert res.get_json()["restarting"] is True

    (cmd, kwargs), = spawned
    assert "linkedin_automation.restart_helper" in cmd
    for key, value in rh.detached_popen_kwargs().items():
        assert kwargs[key] == value, (
            "killing the dashboard would take its own restarter with it")


def test_the_helper_does_not_inherit_the_listening_socket(api_client, spawned):
    """The reloader marks the socket inheritable. A helper holding it would keep
    the port open and wait on itself until it timed out."""
    api_client.post("/api/restart")
    (_cmd, kwargs), = spawned
    assert kwargs["close_fds"] is True


def test_the_helper_is_told_which_process_and_port(api_client, spawned):
    """It waits on both: a pid can die while the socket lingers."""
    api_client.post("/api/restart")
    (cmd, _), = spawned

    assert _helper_args(cmd) == [str(os.getpid()), str(dash.DASHBOARD_PORT)]


def test_under_the_reloader_the_monitor_pid_is_waited_on_too(api_client, spawned,
                                                             monkeypatch):
    """app.run(debug=True) runs the Werkzeug reloader. Its monitor process owns
    the listening socket and keeps it open after the serving child exits, so a
    helper that only waited on the child would race the monitor for the port."""
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")

    res = api_client.post("/api/restart")
    (cmd, _), = spawned

    assert _helper_args(cmd) == [str(os.getpid()), str(dash.DASHBOARD_PORT),
                                 str(os.getppid())]
    assert res.get_json()["waiting_on"] == [os.getpid(), os.getppid()]


def test_the_port_is_the_one_the_dashboard_binds():
    """The helper restarts onto DASHBOARD_PORT; app.run must bind the same one."""
    source = open(dash.__file__, encoding="utf-8").read()
    assert "app.run(debug=True, port=DASHBOARD_PORT)" in source


def test_restart_is_refused_while_a_browser_job_runs(api_client, spawned):
    """The guard that matters.

    Killing the server mid-scrape orphans a Chrome holding the profile, which
    then blocks every later run and every login with a message naming none of
    that.
    """
    dash.jobs["scrape_test"] = {
        "status": "running", "task_type": "browser",
        "category": "scrape", "profile": "t", "log": [],
    }
    try:
        res = api_client.post("/api/restart")
    finally:
        dash.jobs.pop("scrape_test", None)

    assert res.status_code == 409
    assert "scrape" in res.get_json()["error"]
    assert spawned == [], "it started a restart while a browser job was running"


def test_an_api_job_does_not_block_a_restart(api_client, spawned):
    """Only browser jobs orphan a Chrome. Generation is a subprocess that dies
    cleanly, and blocking on it would make the button feel broken."""
    dash.jobs["gen_test"] = {
        "status": "running", "task_type": "api",
        "category": "generate", "profile": "t", "log": [],
    }
    try:
        res = api_client.post("/api/restart")
    finally:
        dash.jobs.pop("gen_test", None)

    assert res.status_code == 200


def test_a_helper_that_will_not_start_is_reported(api_client, monkeypatch,
                                                  never_actually_exit):
    """Exiting anyway would leave nothing to bring the server back."""
    def boom(cmd, **kw):
        raise OSError("no such executable")

    monkeypatch.setattr(dash.subprocess, "Popen", boom)

    res = api_client.post("/api/restart")

    assert res.status_code == 500
    assert "helper" in res.get_json()["error"]
    assert never_actually_exit == []


def test_the_server_only_exits_after_the_helper_is_launched(api_client, spawned,
                                                            never_actually_exit):
    """Ordering. Exiting first leaves nothing running to start the replacement.
    Exit code 0, because the reloader monitor respawns only on 3."""
    api_client.post("/api/restart")

    import time as _t
    for _ in range(50):
        if never_actually_exit:
            break
        _t.sleep(0.02)

    assert spawned, "the helper was never launched"
    assert never_actually_exit == [0]


# ─── The helper ───────────────────────────────────────────────────────────────

def test_the_helper_waits_for_the_old_process_to_go(monkeypatch):
    """Starting while the old server lives means the new one cannot bind."""
    alive = iter([True, True, False])
    monkeypatch.setattr(rh, "process_alive", lambda pid: next(alive, False))
    monkeypatch.setattr(rh.time, "sleep", lambda s: None)

    assert rh.wait_for_exit(123, timeout=5) is True


def test_the_helper_gives_up_rather_than_starting_a_second_server(monkeypatch):
    """Two dashboards on one port is worse than none."""
    monkeypatch.setattr(rh, "process_alive", lambda pid: True)
    monkeypatch.setattr(rh.time, "sleep", lambda s: None)
    monkeypatch.setattr(rh.time, "monotonic", iter(range(1000)).__next__)

    assert rh.wait_for_exit(123, timeout=5) is False


def test_the_helper_also_waits_for_the_socket_to_free(monkeypatch):
    """A socket can linger after its process exits, and starting into that
    window fails with address-already-in-use, which looks like a broken
    restart rather than a timing one."""
    serving = iter([True, False])
    monkeypatch.setattr(rh.ss, "port_is_serving", lambda p, **k: next(serving, False))
    monkeypatch.setattr(rh.time, "sleep", lambda s: None)

    assert rh.wait_for_port_free(6500, timeout=5) is True


def test_the_helper_refuses_to_start_if_the_old_one_survives(monkeypatch):
    monkeypatch.setattr(rh, "wait_for_exit", lambda pid, **k: False)
    started = []
    monkeypatch.setattr(rh.subprocess, "Popen",
                        lambda cmd, **kw: started.append(cmd))

    assert rh.main(["123", "6500"]) == 1
    assert started == []


def test_the_helper_waits_for_every_pid_it_is_given(monkeypatch):
    """The reloader monitor as well as the child. If the monitor outlives the
    wait, starting a new server would fail to bind."""
    waited = []

    def fake_wait(pid, **k):
        waited.append(pid)
        return pid != 456          # the monitor never exits

    monkeypatch.setattr(rh, "wait_for_exit", fake_wait)
    started = []
    monkeypatch.setattr(rh.subprocess, "Popen",
                        lambda cmd, **kw: started.append(cmd))

    assert rh.main(["123", "6500", "456"]) == 1
    assert waited == [123, 456]
    assert started == []


def test_the_helper_starts_the_dashboard_detached(monkeypatch):
    monkeypatch.setattr(rh, "wait_for_exit", lambda pid, **k: True)
    monkeypatch.setattr(rh, "wait_for_port_free", lambda port, **k: True)
    monkeypatch.setattr(rh.ss, "port_is_serving", lambda p, **k: True)
    # What the helper inherits when the dashboard that launched it was the
    # reloader's child.
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    monkeypatch.setenv("WERKZEUG_SERVER_FD", "7")
    started = []
    monkeypatch.setattr(rh.subprocess, "Popen",
                        lambda cmd, **kw: started.append((cmd, kw)))

    assert rh.main(["123", "6500", "456"]) == 0

    (cmd, kwargs), = started
    assert cmd[1:] == ["-m", "linkedin_automation.dashboard"]
    for key, value in rh.detached_popen_kwargs().items():
        assert kwargs[key] == value
    # Inherited reloader state would make the replacement think it is already
    # the reloader's child and adopt a socket fd that does not exist in it.
    assert "WERKZEUG_RUN_MAIN" not in kwargs["env"]
    assert "WERKZEUG_SERVER_FD" not in kwargs["env"]


def test_the_replacement_output_goes_to_the_restart_log(monkeypatch):
    """Detached from any terminal, a replacement that fails to come up would
    otherwise leave nothing to read."""
    from linkedin_automation import run_log

    monkeypatch.setattr(rh, "wait_for_exit", lambda pid, **k: True)
    monkeypatch.setattr(rh, "wait_for_port_free", lambda port, **k: True)
    monkeypatch.setattr(rh.ss, "port_is_serving", lambda p, **k: True)
    started = []
    monkeypatch.setattr(rh.subprocess, "Popen",
                        lambda cmd, **kw: started.append(kw))

    rh.main(["123", "6500"])

    (kwargs,) = started
    assert kwargs["stdout"].name == os.path.join(run_log.logs_dir(),
                                                 rh.RESTART_LOG_NAME)
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_bad_arguments_are_refused():
    assert rh.main([]) == 2
    assert rh.main(["notapid", "6500"]) == 2
    assert rh.main(["123", "6500", "notapid"]) == 2


# ─── process_alive ────────────────────────────────────────────────────────────
#
# The beta version failed on Windows CI: ``process_alive(proc.pid)`` returned
# True for a child that had exited and been waited on. On Windows ``os.kill(pid,
# 0)`` is not an existence check at all — it is TerminateProcess — and on an
# exited process whose object is still referenced it fails with access denied,
# which read as "alive". The Windows branch now asks GetExitCodeProcess.

def test_process_alive_reports_a_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()

    assert rh.process_alive(proc.pid) is False


def test_process_alive_reports_a_live_pid_and_does_not_harm_it():
    """On Windows the old idiom would have *killed* this process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert rh.process_alive(proc.pid) is True
        assert proc.poll() is None, "checking liveness terminated the process"
    finally:
        proc.kill()
        proc.wait()
    assert rh.process_alive(proc.pid) is False


class _FakeKernel32:
    """Just enough of kernel32 to drive the Windows decision table."""

    def __init__(self, handle=1, exit_code=None, query_ok=True):
        self.handle = handle
        self.exit_code = exit_code
        self.query_ok = query_ok
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        assert access == rh._PROCESS_QUERY_LIMITED_INFORMATION
        return self.handle

    def GetExitCodeProcess(self, handle, code_ref):
        if not self.query_ok:
            return 0
        code_ref._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)


@pytest.mark.parametrize("kernel32, last_error, expected", [
    # Running: GetExitCodeProcess says STILL_ACTIVE.
    (_FakeKernel32(exit_code=259), 0, True),
    # Exited (the CI failure: exited, handle still referenced) -> dead.
    (_FakeKernel32(exit_code=0), 0, False),
    (_FakeKernel32(exit_code=1), 0, False),
    # No such process: OpenProcess fails with ERROR_INVALID_PARAMETER.
    (_FakeKernel32(handle=0), 87, False),
    # Exists but not ours: conservative, never start a second server.
    (_FakeKernel32(handle=0), 5, True),
    # Could not read the exit code: also conservative.
    (_FakeKernel32(query_ok=False), 0, True),
])
def test_the_windows_liveness_decision_table(kernel32, last_error, expected):
    assert rh._process_alive_windows(
        1234, kernel32=kernel32, get_last_error=lambda: last_error) is expected
    if kernel32.handle:
        assert kernel32.closed == [kernel32.handle], "the handle leaked"


def test_windows_uses_the_exit_code_check_not_os_kill(monkeypatch):
    """The dispatch itself, exercised on any platform."""
    monkeypatch.setattr(rh.sys, "platform", "win32")
    seen = []
    monkeypatch.setattr(rh, "_process_alive_windows",
                        lambda pid: seen.append(pid) or False)

    def no_kill(pid, sig):  # pragma: no cover - asserted by never being hit
        raise AssertionError("os.kill on Windows is TerminateProcess")

    monkeypatch.setattr(rh.os, "kill", no_kill)

    assert rh.process_alive(99) is False
    assert seen == [99]


def test_windows_detaches_with_creation_flags(monkeypatch):
    """start_new_session is silently ignored on Windows; without the flags the
    helper and the replacement die when run.bat's console closes."""
    monkeypatch.setattr(rh.sys, "platform", "win32")
    kwargs = rh.detached_popen_kwargs()
    assert kwargs == {"creationflags": rh._DETACHED_PROCESS
                      | rh._CREATE_NEW_PROCESS_GROUP}

    monkeypatch.setattr(rh.sys, "platform", "darwin")
    assert rh.detached_popen_kwargs() == {"start_new_session": True}


# ─── The button ───────────────────────────────────────────────────────────────

def _template():
    import pathlib

    return (pathlib.Path(dash.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")


def test_the_button_is_in_the_header_not_a_tab():
    """It belongs on the screen the operator is already looking at."""
    import re

    header = re.search(r"<header.*?</header>", _template(), re.S)

    assert header, "no header block found"
    assert "restartDashboard()" in header.group(0)


def test_the_button_confirms_first():
    handler = _template().split("async function restartDashboard", 1)[1].split("\n}", 1)[0]
    assert "await askConfirm(" in handler
    assert "if (!ok) return;" in handler


def test_the_handler_survives_the_server_exiting_mid_request():
    """The server may exit before the response lands. That is a successful
    restart, and reporting it as an error would send the operator looking for
    a problem that does not exist."""
    handler = _template().split("async function restartDashboard", 1)[1].split("\n}", 1)[0]

    assert "catch" in handler
    assert "waitForDashboard" in handler
