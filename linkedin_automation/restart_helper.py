"""restart_helper.py — outlive the dashboard so it can be started again.

A server cannot restart itself. Something has to still be running after it
exits in order to start the replacement, and that is all this is: wait for the
old process(es) to go, wait for the port to free, start a new one, confirm it
answers.

**Why this exists at all.** A running dashboard does not pick up an update to
the code it already imported, and on the macOS app or a double-clicked
``run.command`` there is no terminal in front of the operator to Ctrl+C and
start again. Missing a restart has no symptom of its own: the app keeps working
exactly as it did before, which reads as "the update did not work". So it is a
button in the dashboard header, and this module is the half of it that has to
run after the dashboard is gone.

**The dashboard can be two processes, and both must be gone.** ``dashboard.py``
runs ``app.run(debug=True)``, which turns on the Werkzeug reloader. The process
started from the command line becomes a *monitor*: it binds the listening
socket itself, then spawns a child (``WERKZEUG_RUN_MAIN=true``) that inherits
that socket and actually serves requests. The Restart request is handled by
the child, so the child's pid is not enough: the monitor still holds the socket
open after the child exits, and the port keeps accepting connections until the
monitor exits too (which it does as soon as it sees the child exit with a code
other than 3). The dashboard therefore passes BOTH pids — its own, and its
parent's when it is the reloader child — and this waits for every one of them,
and then for the port, before starting anything.

Launched detached (its own session on POSIX, detached from the console on
Windows), so the dashboard exiting — or the terminal window it was started from
closing — does not take its own restarter with it.

Usage (not meant to be run by hand):
    python -m linkedin_automation.restart_helper <pid> <port> [<pid> ...]
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

#: The replacement server's stdout/stderr, under the project's logs/ directory.
#: The old server's output went to whatever terminal or pipe started it; the
#: replacement is detached from both, so without a file a failure to come back
#: up would leave nothing to read.
RESTART_LOG_NAME = "dashboard_restart.log"

# Windows process-creation flags (subprocess exposes these only on Windows, so
# they are spelled out here rather than read off the module on every platform).
# DETACHED_PROCESS: no console, so closing the console the dashboard was started
# from (run.bat) does not deliver CTRL_CLOSE_EVENT and kill the helper or the
# replacement server. CREATE_NEW_PROCESS_GROUP: a Ctrl+C in that console is not
# delivered to them either.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

# GetExitCodeProcess's "has not exited" sentinel, and the access right that is
# enough to ask for it (Vista+). See _process_alive_windows.
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def detached_popen_kwargs() -> dict:
    """``subprocess.Popen`` keyword arguments that detach a child from us.

    POSIX: a new session, so a signal to our process group (the dashboard's, or
    the menu bar app's supervisor stopping it) does not reach the child.
    Windows has no sessions; ``start_new_session`` is silently ignored there,
    so it needs creation flags instead or the child dies with the console.
    """
    if sys.platform == "win32":
        return {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _process_alive_windows(pid: int, kernel32=None, get_last_error=None) -> bool:
    """Windows: ask the kernel for the process's exit code.

    ``os.kill(pid, 0)`` is NOT an existence check on Windows. Any signal other
    than CTRL_C/CTRL_BREAK becomes ``TerminateProcess(handle, sig)`` — so the
    POSIX idiom would *kill* a live process, with exit code 0. And for one that
    has already exited, the process object survives for as long as anyone holds
    a handle to it (the ``Popen`` that started it, until it is collected), so
    ``TerminateProcess`` on it fails with access denied, which the POSIX branch
    reads as "alive". That is exactly how this function failed on Windows CI:
    a child that had exited and been waited on still reported alive.

    The correct question on Windows is "has it exited", and the answer is
    ``GetExitCodeProcess`` returning something other than STILL_ACTIVE:

    * ``OpenProcess`` fails with ERROR_INVALID_PARAMETER  -> no such pid -> dead.
    * ``OpenProcess`` fails with ERROR_ACCESS_DENIED      -> exists, not ours ->
      alive (cannot happen for our own dashboard; conservative regardless,
      since the caller treats "alive" as "do not start a second server").
    * opened, exit code == STILL_ACTIVE (259)            -> alive.
    * opened, any other exit code                        -> dead, even though
      the process object still exists because a handle is held somewhere.

    The one ambiguity is a process that genuinely exits *with* code 259; it
    would read as alive until the wait times out. The dashboard exits with 0.

    ``kernel32`` and ``get_last_error`` are injectable so the decision table
    above is unit-tested on every platform, not only on a Windows runner.
    """
    import ctypes
    from ctypes import wintypes

    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    if get_last_error is None:
        get_last_error = ctypes.get_last_error

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            # Could not ask. Conservative, for the same reason as above.
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    """True while ``pid`` has not exited.

    POSIX: signal 0 checks existence without delivering anything. Windows: see
    :func:`_process_alive_windows` — the POSIX idiom would terminate the
    process there, and misreads one that has exited.

    A POSIX zombie (exited, not yet reaped by its parent) still answers signal
    0. That never applies to the pids this waits on: the dashboard is not our
    child, and its parent (a shell, the menu bar app, or the reloader monitor,
    which reaps it via ``subprocess.call``) does the reaping.
    """
    if sys.platform == "win32":
        return _process_alive_windows(pid)
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

    Separate from waiting for the pids, because a socket can linger briefly
    after the process holding it exits, and starting into that window gives an
    address-already-in-use failure that looks like a broken restart. It is also
    the backstop for a holder of the socket that no pid named.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ss.port_is_serving(port):
            return True
        time.sleep(POLL)
    return False


def _open_restart_log():
    """Append-mode handle on logs/dashboard_restart.log, or DEVNULL on failure.

    A log that cannot be opened must not stop the restart: the server coming
    back matters more than its output being kept.
    """
    try:
        from . import run_log
        log_dir = run_log.logs_dir()
        os.makedirs(log_dir, exist_ok=True)
        return open(os.path.join(log_dir, RESTART_LOG_NAME), "a",
                    encoding="utf-8")
    except Exception:
        logger.debug("Could not open the restart log", exc_info=True)
        return subprocess.DEVNULL


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print("usage: python -m linkedin_automation.restart_helper "
              "<pid> <port> [<pid> ...]", file=sys.stderr)
        return 2

    try:
        old_pid, port = int(argv[0]), int(argv[1])
        more_pids = [int(p) for p in argv[2:]]
    except ValueError:
        print("pids and port must be integers", file=sys.stderr)
        return 2

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for pid in [old_pid] + more_pids:
        if not wait_for_exit(pid):
            logger.error("The old dashboard (pid %s) did not exit; not starting "
                         "a second one on the same port", pid)
            return 1

    if not wait_for_port_free(port):
        logger.error("Port %s is still in use after the dashboard exited; "
                     "something else is holding it", port)
        return 1

    env = dict(os.environ)
    # Whatever the reloader set in OUR environment (we were started by its
    # child) must not leak into the replacement: WERKZEUG_RUN_MAIN would make
    # the new process believe it is already the reloader's child and try to
    # adopt a socket fd (WERKZEUG_SERVER_FD) that does not exist in it.
    env.pop("WERKZEUG_RUN_MAIN", None)
    env.pop("WERKZEUG_SERVER_FD", None)
    env["PYTHONUNBUFFERED"] = "1"

    log = _open_restart_log()
    try:
        subprocess.Popen(
            [sys.executable, "-m", "linkedin_automation.dashboard"],
            cwd=repo, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            **detached_popen_kwargs(),
        )
    finally:
        # The child has its own copy of the descriptor; ours is not needed.
        if log is not subprocess.DEVNULL:
            log.close()

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if ss.port_is_serving(port):
            return 0
        time.sleep(POLL)

    logger.error("The dashboard did not come back up on port %s", port)
    return 1


def _configure_logging():
    """Send this helper's own log lines to the restart log too.

    It runs detached with no terminal, so an error it reports ("the old
    dashboard did not exit", "port still in use") would otherwise go nowhere,
    and a restart that silently never came back would have no explanation.
    """
    try:
        from . import run_log
        log_dir = run_log.logs_dir()
        os.makedirs(log_dir, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            filename=os.path.join(log_dir, RESTART_LOG_NAME), encoding="utf-8",
            format="%(asctime)s - restart_helper - %(levelname)s - %(message)s")
    except Exception:
        logging.basicConfig(level=logging.INFO)


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
