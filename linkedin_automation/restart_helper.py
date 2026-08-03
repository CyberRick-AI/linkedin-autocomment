"""restart_helper.py — outlive the dashboard so it can be started again.

A server cannot restart itself. Something has to still be running after it
exits in order to start the replacement, and that is all this is: wait for the
old process to go, wait for its port to free, start a new one, confirm it
answers.

**Why this exists at all.** A running dashboard does not pick up code changes.
Flask's reloader only runs in debug mode, and debug mode is off by default
because it ships the Werkzeug debugger. So every update needs a restart, and
until now that meant a terminal or a menu bar item the operator could not
always see. Enough restarts happened in one week of real use to make it a
button.

Launched detached, in its own session, so killing the dashboard does not take
its own restarter with it.

Usage (not meant to be run by hand):
    python -m linkedin_automation.restart_helper <old_pid> <port>
"""

import logging
import os
import subprocess
import sys
import time

from . import server_supervisor as ss

logger = logging.getLogger(__name__)

# How long to wait for the old server to exit, and for the new one to answer.
# Generous: a cold start imports Flask, selenium and the provider SDKs.
EXIT_TIMEOUT = 30.0
PORT_FREE_TIMEOUT = 30.0
START_TIMEOUT = 60.0
POLL = 0.25


def process_alive(pid: int) -> bool:
    """True while ``pid`` exists. Signal 0 checks without delivering anything."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists and belongs to someone else, which cannot happen here but
        # is still "alive" as far as this is concerned.
        return True
    except OSError:
        return False


def wait_for_exit(pid: int, timeout: float = EXIT_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(POLL)
    return False


def wait_for_port_free(port: int, timeout: float = PORT_FREE_TIMEOUT) -> bool:
    """Wait until nothing answers on the port.

    Separate from waiting for the pid, because a socket can linger briefly
    after the process holding it exits, and starting into that window gives an
    address-already-in-use failure that looks like a broken restart.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ss.port_is_serving(port):
            return True
        time.sleep(POLL)
    return False


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print("usage: python -m linkedin_automation.restart_helper <pid> <port>",
              file=sys.stderr)
        return 2

    try:
        old_pid, port = int(argv[0]), int(argv[1])
    except ValueError:
        print("pid and port must be integers", file=sys.stderr)
        return 2

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if not wait_for_exit(old_pid):
        logger.error("The old dashboard (pid %s) did not exit; not starting a "
                     "second one on the same port", old_pid)
        return 1

    if not wait_for_port_free(port):
        logger.error("Port %s is still in use after the dashboard exited; "
                     "something else is holding it", port)
        return 1

    env = dict(os.environ)
    env["LINKEDIN_DASHBOARD_PORT"] = str(port)

    subprocess.Popen(
        [sys.executable, "-m", "linkedin_automation.dashboard"],
        cwd=repo, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if ss.port_is_serving(port):
            return 0
        time.sleep(POLL)

    logger.error("The dashboard did not come back up on port %s", port)
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
