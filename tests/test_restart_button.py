"""Restarting from the dashboard (2026-08-03).

A running dashboard does not pick up code changes. Flask's reloader only runs
in debug mode, and debug mode is off by default because it ships the Werkzeug
debugger. So every update needs a restart, and there is no symptom when one is
missed: the app keeps working exactly as it did before, which reads as the fix
not working.

Restart existed in the menu bar app first. That is the one surface that does
not reliably appear, so in practice it was unreachable and Rick was pkilling
from a terminal several times a day.

Offline: the helper's spawn and clock are faked, and the endpoint's process
exit is patched out.
"""

import subprocess

import pytest

from linkedin_automation import dashboard as dash
from linkedin_automation import restart_helper as rh


@pytest.fixture(autouse=True)
def never_actually_exit(monkeypatch):
    """os._exit in a test run would take pytest with it."""
    exits = []
    monkeypatch.setattr(dash.os, "_exit", lambda code: exits.append(code))
    # Shorten the grace period rather than patching time.sleep, which is the
    # shared module and would also disable this test's own waiting loop. That
    # is exactly how the first version of this test failed.
    monkeypatch.setattr(dash, "RESTART_GRACE_SECONDS", 0)
    return exits


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(dash.subprocess, "Popen",
                        lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc())
    return calls


# ─── The endpoint ─────────────────────────────────────────────────────────────

def test_restart_launches_a_detached_helper(api_client, spawned):
    """The helper has to outlive the server, or nothing starts the replacement."""
    res = api_client.post("/api/restart")

    assert res.status_code == 200
    assert res.get_json()["restarting"] is True

    (cmd, kwargs), = spawned
    assert "linkedin_automation.restart_helper" in cmd
    assert kwargs["start_new_session"] is True, (
        "killing the dashboard would take its own restarter with it"
    )


def test_the_helper_is_told_which_process_and_port(api_client, spawned):
    """It waits on both: a pid can die while the socket lingers."""
    import os

    api_client.post("/api/restart")
    (cmd, _), = spawned

    assert cmd[-2] == str(os.getpid())
    assert cmd[-1] == str(dash.get_port())


def test_restart_is_refused_while_a_browser_job_runs(api_client, spawned):
    """The guard that matters.

    Killing the server mid-scrape orphans a Chrome holding the profile, which
    then blocks every later run and every login with a message naming none of
    that. Rick lost a day to exactly that failure on 2026-08-02.
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


def test_a_helper_that_will_not_start_is_reported(api_client, monkeypatch):
    """Exiting anyway would leave nothing to bring the server back."""
    def boom(cmd, **kw):
        raise OSError("no such executable")

    monkeypatch.setattr(dash.subprocess, "Popen", boom)

    res = api_client.post("/api/restart")

    assert res.status_code == 500
    assert "helper" in res.get_json()["error"]


def test_the_server_only_exits_after_the_helper_is_launched(api_client, spawned,
                                                            never_actually_exit):
    """Ordering. Exiting first leaves nothing running to start the replacement."""
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


def test_the_helper_refuses_to_start_if_the_old_one_survives(monkeypatch, capsys):
    monkeypatch.setattr(rh, "wait_for_exit", lambda pid, **k: False)
    started = []
    monkeypatch.setattr(rh.subprocess, "Popen",
                        lambda cmd, **kw: started.append(cmd))

    assert rh.main(["123", "6500"]) == 1
    assert started == []


def test_the_helper_starts_the_dashboard_detached(monkeypatch):
    monkeypatch.setattr(rh, "wait_for_exit", lambda pid, **k: True)
    monkeypatch.setattr(rh, "wait_for_port_free", lambda port, **k: True)
    monkeypatch.setattr(rh.ss, "port_is_serving", lambda p, **k: True)
    started = []
    monkeypatch.setattr(rh.subprocess, "Popen",
                        lambda cmd, **kw: started.append((cmd, kw)))

    assert rh.main(["123", "6501"]) == 0

    (cmd, kwargs), = started
    assert cmd[1:] == ["-m", "linkedin_automation.dashboard"]
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["LINKEDIN_DASHBOARD_PORT"] == "6501"


def test_bad_arguments_are_refused():
    assert rh.main([]) == 2
    assert rh.main(["notapid", "6500"]) == 2


def test_process_alive_reports_a_dead_pid():
    """Signal 0 checks existence without delivering anything."""
    proc = subprocess.Popen([__import__("sys").executable, "-c", "pass"])
    proc.wait()

    assert rh.process_alive(proc.pid) is False


# ─── The button ───────────────────────────────────────────────────────────────

def test_the_button_is_in_the_header_not_a_tab():
    """It was in the menu bar, which does not reliably appear. It belongs on
    the screen the operator is already looking at."""
    import pathlib
    import re

    html = (pathlib.Path(dash.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")
    header = re.search(r"<header.*?</header>", html, re.S)

    assert header, "no header block found"
    assert "restartDashboard()" in header.group(0)


def test_the_handler_survives_the_server_exiting_mid_request():
    """The server may exit before the response lands. That is a successful
    restart, and reporting it as an error would send the operator looking for
    a problem that does not exist."""
    import pathlib

    html = (pathlib.Path(dash.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")
    handler = html.split("async function restartDashboard", 1)[1].split("\n}", 1)[0]

    assert "catch" in handler
    assert "waitForDashboard" in handler
