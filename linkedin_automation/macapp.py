"""macapp.py — the macOS app entry point.

``run.command``'s own comment says "Closing this Terminal window stops the
server", so before this the operator was running a background service inside a
window they must not touch. This removes the window.

Two modes, and which one runs depends only on whether PyObjC is installed:

* **With it** (``requirements-macos.txt``): the full app. A menu bar item and
  a window hosting the dashboard, driven by :mod:`macapp_ui` and
  :class:`~app_controller.AppController`. Hands off to the Cocoa run loop.
* **Without it**: start the dashboard, open it in the browser, exit, leaving
  the server running detached in its own process group. Still better than a
  terminal window you must not close, so a missing optional dependency
  degrades rather than fails.

Nothing here imports PyObjC at module scope, so this file, and the rest of the
package, work on a machine that never installed it — including Windows in CI.
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


# Sentinel for "work out whether the UI is available". Distinct from None,
# which a caller passes to mean "definitely no UI" — a test that wants the
# fallback path must be able to say so without relying on PyObjC being absent
# from the machine running it.
_AUTO = object()


def load_ui():
    """Return the Cocoa UI module, or None when PyObjC is not installed.

    Kept as a lookup rather than a top-level import so that everything else in
    this module — and the whole package — works on a machine without PyObjC.
    Only ``requirements-macos.txt`` installs it.
    """
    try:
        from . import macapp_ui
        return macapp_ui
    except ImportError as e:
        logger.info("The menu bar UI is unavailable (%s); "
                    "starting the dashboard and opening a browser instead", e)
        return None


def main(argv=None, supervisor=None, opener=None, alerter=None, ui=_AUTO):
    """Run the menu bar app, or fall back to start-and-open-a-browser.

    Returns an exit code. Every failure path raises a dialog, because the only
    other output this process has is a log file nobody is watching.

    With PyObjC present this hands off to the Cocoa run loop and does not
    return. Without it, the Phase 14a behaviour remains: start the dashboard,
    open it, exit, leaving the server running detached. That is still better
    than a terminal window you must not close, so a missing optional
    dependency degrades rather than fails.
    """
    argv = argv if argv is not None else sys.argv[1:]
    port = ss.DEFAULT_PORT
    if argv and argv[0].isdigit():
        port = int(argv[0])

    supervisor = supervisor or ss.ServerSupervisor(port=port, cwd=repo_root())
    url = f"http://localhost:{port}"

    if ui is _AUTO:
        ui = None if "--no-ui" in argv else load_ui()
    if ui is not None:
        ui.run(supervisor=supervisor)
        return EXIT_OK

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
