"""server_supervisor.py — own the dashboard's lifecycle from outside it.

The dashboard is a background service whose current off switch is "do not close
that Terminal window". ``run.command`` says so in its own comment. That is the
failure mode ROADMAP Phase 14 exists to remove, and removing it needs something
that can start the server, notice it is already up, and stop it completely.

**Completely is the hard word.** The dashboard spawns browser jobs, and those
spawn `chromedriver`, which spawns Chrome. Killing the server's pid leaves that
subtree running: a headless Chrome holding a LinkedIn session, invisible,
surviving until the machine reboots. So the server is started in its own
process group and stopped by signalling the group.

Everything here is plain Python with no GUI and no Cocoa, which is why it is a
separate module from the app shell. It is the half of Phase 14 that can be
tested offline, and per PROJECT.md section 8 the suite never starts a real
server: the tests inject a fake spawner and a fake prober.
"""

import logging
import os
import signal
import socket
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

DEFAULT_PORT = 6500
HOST = "127.0.0.1"

# How long to wait for the port to answer after a start, and for the process to
# die after a stop. Both are generous: a cold start imports Flask, selenium and
# the provider SDKs, and a stop has to let the scheduler thread notice.
START_TIMEOUT = 30.0
STOP_TIMEOUT = 10.0
POLL_INTERVAL = 0.2

# Outcomes of start(). Distinguished rather than collapsed into a bool because
# "it was already running" is a success the caller reports differently from "I
# started it", and an operator pressing Start twice must not see an error.
STARTED = "started"
ALREADY_RUNNING = "already_running"
FAILED = "failed"

# SIGKILL is POSIX. On Windows there is no uncatchable signal and SIGTERM is
# already the abrupt one, so the escalation collapses to a single step there
# rather than raising AttributeError on the error path, which is the worst
# possible place to discover a missing constant.
TERM_SIGNAL = signal.SIGTERM
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def port_is_serving(port=DEFAULT_PORT, host=HOST, timeout=0.5):
    """True when something accepts a TCP connection on ``host:port``.

    Deliberately not an HTTP request. This answers "is the port taken", which
    is the question that decides whether starting a second server would fail to
    bind. Whether the thing listening is our dashboard is a separate question
    with a separate answer; see :func:`identify_listener`.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


class ServerSupervisor:
    """Start, inspect and stop the dashboard server process.

    Args:
        port: The port the dashboard binds.
        python: Interpreter to launch it with. Defaults to the one running
            this code, which inside the .app bundle is the project venv.
        cwd: Working directory for the server process.
        spawn: Injection point for the process spawner, so tests never start a
            real server.
        probe: Injection point for the port check.
        now: Injection point for the clock, so timeout behaviour is testable
            without waiting.
    """

    def __init__(self, port=DEFAULT_PORT, python=None, cwd=None,
                 spawn=None, probe=None, now=None, sleep=None):
        self.port = port
        self.python = python or sys.executable
        self.cwd = cwd
        self._spawn = spawn or self._spawn_real
        self._probe = probe or port_is_serving
        self._now = now or time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self.process = None

    # ── inspection ────────────────────────────────────────────────────────────

    def is_serving(self):
        """True when the dashboard's port answers."""
        return self._probe(self.port)

    def owns_process(self):
        """True when this supervisor started the server that is still alive.

        False after an external start, which is the normal case when the
        operator launched ``run.command`` first and then opened the app. The
        app can still see and open that server; it just must not claim to be
        able to stop something it did not start.
        """
        return self.process is not None and self.process.poll() is None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, timeout=START_TIMEOUT):
        """Start the dashboard unless the port is already answering.

        Returns one of :data:`STARTED`, :data:`ALREADY_RUNNING`, :data:`FAILED`.

        The port check comes first and is the whole single-instance mechanism.
        Two dashboards on one port is not a degraded state: the second fails to
        bind and dies, and if it did bind it would run a second scheduler
        thread posting from the same queue.
        """
        if self.is_serving():
            logger.info("Dashboard already serving on %s:%s", HOST, self.port)
            return ALREADY_RUNNING

        logger.info("Starting the dashboard on %s:%s", HOST, self.port)
        self.process = self._spawn()

        deadline = self._now() + timeout
        while self._now() < deadline:
            if self.is_serving():
                logger.info("Dashboard is serving")
                return STARTED
            if self.process.poll() is not None:
                # It exited before binding. Almost always the port being taken
                # by something else, or a missing dependency at import time.
                logger.error(
                    "The dashboard exited with code %s before it began serving. "
                    "Check whether port %s is in use by another program.",
                    self.process.returncode, self.port)
                return FAILED
            self._sleep(POLL_INTERVAL)

        logger.error("The dashboard did not begin serving within %.0fs", timeout)
        return FAILED

    def stop(self, timeout=STOP_TIMEOUT):
        """Stop the server and everything it spawned. True when nothing remains.

        Signals the process *group*, not the process. The dashboard's browser
        jobs spawn chromedriver, which spawns Chrome; a plain ``terminate()``
        on the server leaves that subtree alive, holding a logged-in LinkedIn
        session, with nothing on screen to reveal it.

        SIGTERM first so Flask and the scheduler can unwind, SIGKILL only if
        the group is still there at the deadline.
        """
        if self.process is None:
            logger.info("Nothing to stop: this app did not start the server")
            return not self.is_serving()

        if self.process.poll() is not None:
            self.process = None
            return True

        self._signal_group(TERM_SIGNAL)

        deadline = self._now() + timeout
        while self._now() < deadline:
            if self.process.poll() is not None:
                self.process = None
                logger.info("Dashboard stopped")
                return True
            self._sleep(POLL_INTERVAL)

        logger.warning(
            "The dashboard did not stop within %.0fs; killing the process group",
            timeout)
        self._signal_group(KILL_SIGNAL)
        self.process.poll()
        stopped = self.process.poll() is not None
        self.process = None
        return stopped

    def restart(self, timeout=START_TIMEOUT):
        """Stop then start. Used by nothing yet; the menu offers Start and Stop."""
        self.stop()
        return self.start(timeout=timeout)

    # ── the real implementations, replaced in tests ───────────────────────────

    def _spawn_real(self):
        """Launch the dashboard in its own process group.

        ``start_new_session`` is what makes group signalling possible later. It
        also detaches the server from the launching terminal, so the app does
        not inherit ``run.command``'s "closing this window stops the server"
        behaviour, which is the entire point of the phase.
        """
        return subprocess.Popen(
            [self.python, "-m", "linkedin_automation.dashboard"],
            cwd=self.cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _signal_group(self, sig):
        """Signal the server's whole process group, tolerating a dead group.

        Process groups are POSIX. Windows has an equivalent (job objects, or
        ``taskkill /T``) and this does not implement it: Phase 14 is a macOS
        app and Windows is a CI platform for this project, not a target.

        The Windows path therefore terminates the server process only, and
        **says so**, because the difference is that a chromedriver subtree can
        survive. Degrading quietly here would recreate the exact failure the
        group signalling exists to prevent, just on a platform nobody watches.
        """
        if not hasattr(os, "killpg"):
            logger.warning(
                "This platform has no process groups, so only the dashboard "
                "process is being stopped. A chromedriver started by a browser "
                "job may survive and need closing by hand.")
            try:
                self.process.send_signal(sig)
            except (ProcessLookupError, OSError, ValueError):
                pass
            return

        try:
            os.killpg(os.getpgid(self.process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError) as e:
            # Already gone, or never got its own group. Fall back to the single
            # process rather than giving up: a stop that half-works is still
            # better than a stop that raises.
            logger.debug("Group signal failed (%s); signalling the process", e)
            try:
                self.process.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass
