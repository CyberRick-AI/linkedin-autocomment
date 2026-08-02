"""macapp.py — the macOS app entry point.

**Phase 14a: the subset that needs no GUI toolkit.** It starts the dashboard,
opens it in the browser, and exits, leaving the server running detached in its
own process group. Phase 14b adds the menu bar item and the native window on
top of this, which is where the Cocoa dependency arrives.

That subset is already the point of the phase. ``run.command``'s own comment
says "Closing this Terminal window stops the server", so today the operator is
running a background service inside a window they must not touch. After this,
there is no window to protect: the app hands the server off and gets out of the
way.

What it deliberately does **not** do yet, all of it Phase 14b:

* a menu bar item, so there is currently no Stop from the app
* a native window, so the dashboard opens in the default browser
* Quit, which is the counterpart to Stop

Until then the server is stopped the way it always was, and
:class:`~linkedin_automation.server_supervisor.ServerSupervisor` already
implements the stop that 14b will call.
"""

import logging
import os
import subprocess
import sys

from . import server_supervisor as ss

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1


def repo_root():
    """The project directory, which is this package's parent."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def open_in_browser(url, opener=None):
    """Open ``url`` in the default browser.

    Uses ``/usr/bin/open`` rather than :mod:`webbrowser`, because inside an
    ``LSUIElement`` bundle the Python process has no window server presence of
    its own and ``webbrowser`` has been known to no-op there. ``open`` is the
    supported way to ask Launch Services.
    """
    opener = opener or _open_real
    return opener(url)


def _open_real(url):
    try:
        subprocess.run(["/usr/bin/open", url], check=True, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        logger.error("Could not open %s in a browser: %s", url, e)
        return False


def alert(title, message, runner=None):
    """Show a macOS dialog.

    A GUI app has no stdout anybody reads. Without this, every failure below is
    a Dock bounce with no explanation, which is the silent-surface defect this
    project has now found in dialogs, in job logs, and in a launcher.
    """
    runner = runner or _alert_real
    return runner(title, message)


def _alert_real(title, message):
    script = (f'display alert {_as_applescript(title)} '
              f'message {_as_applescript(message)} as critical')
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       check=False, timeout=60)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        logger.error("Could not show an alert: %s", e)
        return False


def _as_applescript(text):
    """Quote a Python string as an AppleScript string literal."""
    escaped = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def main(argv=None, supervisor=None, opener=None, alerter=None):
    """Start the dashboard if it is not already up, then open it.

    Returns an exit code. Every failure path raises a dialog, because the only
    other output this process has is a log file nobody is watching.
    """
    argv = argv if argv is not None else sys.argv[1:]
    port = ss.DEFAULT_PORT
    if argv and argv[0].isdigit():
        port = int(argv[0])

    supervisor = supervisor or ss.ServerSupervisor(port=port, cwd=repo_root())
    url = f"http://localhost:{port}"

    outcome = supervisor.start()

    if outcome == ss.FAILED:
        alert(
            "LinkedIn Autocomment",
            f"The dashboard did not start.\n\nSomething may already be using "
            f"port {port}, or the project environment may be incomplete. "
            f"Try running ./run.sh in the project folder to see the error.",
            runner=alerter,
        )
        return EXIT_ERROR

    if outcome == ss.ALREADY_RUNNING:
        logger.info("Dashboard was already running; opening it")

    if not open_in_browser(url, opener=opener):
        alert(
            "LinkedIn Autocomment",
            f"The dashboard is running at {url} but the browser could not be "
            f"opened. Go to that address manually.",
            runner=alerter,
        )
        return EXIT_ERROR

    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
