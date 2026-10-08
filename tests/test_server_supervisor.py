"""The dashboard's lifecycle, owned from outside it (Phase 14a).

ROADMAP Phase 14's gate asks for three things this module has to answer:
launching twice must not double-bind, closing the window must not stop the
server, and Quit must leave zero python and zero chromedriver processes.

Offline, per PROJECT.md section 8: the spawner, the port prober and the clock
are all injected, so nothing here starts a real server, binds a real port, or
waits real seconds.
"""

import os
import signal

import pytest

from linkedin_automation import server_supervisor as ss


# Process groups are POSIX. Phase 14 is a macOS app and Windows is a CI
# platform for this project rather than a target, so the group-signalling
# tests are skipped there rather than the behaviour being watered down to
# something that passes everywhere. The Windows fallback has its own test.
posix_only = pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="process groups are POSIX; Windows uses the documented fallback",
)


class FakeProcess:
    """A ``Popen`` stand-in whose lifetime the test controls explicitly.

    Deliberately not "exits after N polls". That version coupled every test to
    how many times the implementation happens to call ``poll()``, so adding one
    poll to the supervisor broke five unrelated tests. The test says when the
    process dies; the supervisor is free to look as often as it likes.
    """

    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def exit(self, code=0):
        """Mark the process dead, as the OS would."""
        self.returncode = code

    def send_signal(self, sig):
        self.signals.append(sig)


class Clock:
    """Monotonic clock that only advances when the code sleeps."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


def make(port_states, process=None, clock=None, killpg=None, monkeypatch=None):
    """Build a supervisor whose port answers according to ``port_states``.

    ``port_states`` is a list consumed one entry per probe, then the last value
    repeats. That is how a start is modelled: not serving, not serving, then
    serving.
    """
    states = list(port_states)

    def probe(port, **kwargs):
        return states.pop(0) if len(states) > 1 else states[0]

    proc = process if process is not None else FakeProcess()
    clk = clock or Clock()
    sup = ss.ServerSupervisor(
        spawn=lambda: proc, probe=probe, now=clk, sleep=clk.sleep)
    return sup, proc


# ─── Single instance: the double-bind the gate names ──────────────────────────

def test_starting_when_the_port_already_answers_does_not_spawn():
    """Launching a second time must surface the existing server, not a second one.

    Two dashboards on one port is not a degraded state. The second fails to
    bind and dies; if it somehow bound, it would run a second scheduler thread
    publishing from the same queue. The port check is the whole mechanism.
    """
    spawned = []

    sup = ss.ServerSupervisor(
        spawn=lambda: spawned.append(1) or FakeProcess(),
        probe=lambda port, **kw: True,
    )

    assert sup.start() == ss.ALREADY_RUNNING
    assert spawned == [], "a second server was spawned onto a bound port"


def test_a_normal_start_spawns_once_and_waits_for_the_port():
    """The happy path: nothing listening, spawn, poll until it answers."""
    sup, proc = make([False, False, True])

    assert sup.start() == ss.STARTED
    assert sup.owns_process()


def test_a_server_that_dies_before_binding_is_reported_as_failed(caplog):
    """The port being taken by something else is the common cause, and it is named.

    Waiting the full timeout on a process that has already exited would turn a
    five-second answer into a thirty-second one.
    """
    proc = FakeProcess()
    proc.exit(1)                       # died at import, before binding
    sup, _ = make([False], process=proc)

    with caplog.at_level("ERROR", logger="linkedin_automation.server_supervisor"):
        assert sup.start() == ss.FAILED

    assert "in use" in caplog.text


def test_a_server_that_never_binds_times_out_rather_than_hanging():
    """A start that cannot be verified must end, not block the menu forever."""
    clock = Clock()
    sup, _ = make([False], clock=clock)

    assert sup.start(timeout=5.0) == ss.FAILED
    assert clock.t >= 5.0, "it returned without actually waiting"


# ─── Stop: the "zero chromedriver processes" criterion ────────────────────────

@posix_only
def test_stop_signals_the_process_group_not_the_process(monkeypatch):
    """The criterion that makes Quit mean Quit.

    The dashboard spawns browser jobs, which spawn chromedriver, which spawns
    Chrome. Terminating the server's pid alone leaves that subtree alive,
    holding a logged-in LinkedIn session, with nothing on screen to show it.
    """
    signalled = []
    proc = FakeProcess(pid=4242)

    def killpg(pgid, sig):
        signalled.append((pgid, sig))
        proc.exit()                    # the group dies, as it would

    monkeypatch.setattr(ss.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(ss.os, "killpg", killpg)

    sup, _ = make([False, True], process=proc)
    sup.start()

    assert sup.stop() is True
    assert signalled == [(4242, signal.SIGTERM)], (
        f"expected one SIGTERM to the group, got {signalled}"
    )
    assert proc.signals == [], "it signalled the bare process instead of the group"


@posix_only
def test_a_process_that_ignores_sigterm_is_killed(monkeypatch):
    """Escalation, bounded. A stop that gives up silently is not a stop."""
    signalled = []
    proc = FakeProcess(pid=99)          # ignores SIGTERM entirely

    def killpg(pgid, sig):
        signalled.append(sig)
        if sig == signal.SIGKILL:
            proc.exit(-9)

    monkeypatch.setattr(ss.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(ss.os, "killpg", killpg)

    clock = Clock()
    sup, _ = make([False, True], process=proc, clock=clock)
    sup.start()

    sup.stop(timeout=3.0)

    assert signalled == [signal.SIGTERM, signal.SIGKILL]
    assert clock.t >= 3.0, "it escalated to SIGKILL without waiting"


@posix_only
def test_stopping_falls_back_to_the_process_when_the_group_is_gone(monkeypatch):
    """A stop that half-works beats a stop that raises into the menu handler."""
    def boom(pid):
        raise ProcessLookupError("no such group")

    monkeypatch.setattr(ss.os, "getpgid", boom)

    proc = FakeProcess()
    proc.send_signal = lambda sig: (proc.signals.append(sig), proc.exit())[0]
    sup, _ = make([False, True], process=proc)
    sup.start()

    assert sup.stop() is True
    assert proc.signals == [signal.SIGTERM]


def test_stopping_a_server_this_app_did_not_start_is_honest(caplog):
    """The operator may have launched run.command first. Say so; do not pretend.

    Claiming to have stopped a server we never owned is the same class of lie
    as reporting HEALTHY on an unchecked path.
    """
    sup = ss.ServerSupervisor(spawn=lambda: FakeProcess(),
                              probe=lambda port, **kw: True)

    with caplog.at_level("INFO", logger="linkedin_automation.server_supervisor"):
        result = sup.stop()

    assert result is False, "it claimed to have stopped a server it does not own"
    assert "did not start" in caplog.text


def test_stopping_when_nothing_is_running_succeeds():
    """Idempotent. Quit on an already-stopped server is not an error."""
    sup = ss.ServerSupervisor(spawn=lambda: FakeProcess(),
                              probe=lambda port, **kw: False)

    assert sup.stop() is True


def test_stop_is_idempotent_after_the_process_has_exited():
    """Called twice, the second call must not signal a reaped pid."""
    proc = FakeProcess()
    # The port answers exactly while the process is alive, which is the only
    # world that can actually happen. A fixed "always serving" probe made the
    # second stop() report False, and it was right to: something is still
    # listening on that port and this app does not own it.
    sup = ss.ServerSupervisor(
        spawn=lambda: proc,
        probe=lambda port, **kw: proc.poll() is None,
    )
    sup.start()
    proc.exit()                        # it died on its own

    assert sup.stop() is True
    assert sup.stop() is True


# ─── Ownership: closing the window must not stop the server ───────────────────

def test_a_supervisor_that_started_nothing_does_not_claim_ownership():
    sup = ss.ServerSupervisor(spawn=lambda: FakeProcess(),
                              probe=lambda port, **kw: True)

    assert sup.is_serving() is True
    assert sup.owns_process() is False


def test_ownership_ends_when_the_process_dies():
    proc = FakeProcess()
    sup, _ = make([False, True], process=proc)
    sup.start()
    assert sup.owns_process() is True

    proc.exit()

    assert sup.owns_process() is False


# ─── The port probe itself ────────────────────────────────────────────────────

def test_the_probe_reports_false_for_a_port_nothing_listens_on():
    """A real socket call, to a port chosen to be closed. No server started."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    # The socket is closed now, so nothing listens on free_port.

    assert ss.port_is_serving(free_port, timeout=0.2) is False


def test_the_probe_reports_true_for_a_port_something_listens_on():
    """The positive case, proven against a real listener that serves nothing.

    Deliberately a bare socket rather than the dashboard: the probe answers
    "is this port taken", which is what decides whether a start would collide.
    """
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert ss.port_is_serving(port, timeout=0.5) is True
    finally:
        listener.close()


def test_the_probe_survives_a_nonsense_port():
    """Malformed input must return False, not raise into a menu handler."""
    assert ss.port_is_serving(-1) is False
    assert ss.port_is_serving(999999) is False


# ─── The spawn command ────────────────────────────────────────────────────────

def test_the_server_is_started_in_its_own_session(monkeypatch):
    """``start_new_session`` is what makes group signalling possible at all.

    It also detaches the server from the launching terminal, which is the
    behaviour Phase 14 exists to remove: run.command's own comment says closing
    its window stops the server.
    """
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            self.pid = 1
            self.returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(ss.subprocess, "Popen", FakePopen)

    sup = ss.ServerSupervisor(python="/fake/python", probe=lambda port, **kw: False)
    sup._spawn_real()

    assert captured["cmd"] == ["/fake/python", "-m", "linkedin_automation.dashboard"]
    assert captured["kwargs"]["start_new_session"] is True, (
        "without its own session, stop() cannot signal the chromedriver subtree"
    )


# ─── The platform the app does not target ─────────────────────────────────────

@pytest.mark.skipif(hasattr(os, "killpg"), reason="POSIX has process groups")
def test_on_windows_the_fallback_stops_the_process_and_warns(caplog):
    """Degrade loudly, not quietly.

    Windows has no process groups, so only the dashboard process is stopped
    and a chromedriver subtree can survive. That difference is exactly what
    the group signalling exists to prevent, so it is stated rather than left
    for someone to discover as a stray Chrome holding a LinkedIn session.
    """
    proc = FakeProcess()
    proc.send_signal = lambda sig: (proc.signals.append(sig), proc.exit())[0]
    sup, _ = make([False, True], process=proc)
    sup.start()

    with caplog.at_level("WARNING", logger="linkedin_automation.server_supervisor"):
        assert sup.stop() is True

    assert proc.signals == [ss.TERM_SIGNAL]
    assert "by hand" in caplog.text, "the surviving-subtree caveat was not stated"


def test_the_kill_signal_exists_on_every_platform():
    """The escalation path must not raise AttributeError when it is needed.

    ``signal.SIGKILL`` is POSIX-only, and the one place it is referenced is
    the error path of a stop that is already going badly.
    """
    assert ss.TERM_SIGNAL is not None
    assert ss.KILL_SIGNAL is not None
