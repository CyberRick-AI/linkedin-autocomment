"""platform_compat.py — the few things that genuinely differ per operating system.

The rest of the package is platform-neutral: paths are built with ``os.path``,
child processes are spawned with ``sys.executable``, and the Chrome driver is
resolved by ``webdriver-manager``. Only two behaviors are OS-specific, and both
live here so no other module needs a platform check:

* the modifier key that submits a LinkedIn comment box, and
* reading the OS clipboard (a fallback path only — see
  ``post_finder.extract_url_via_clipboard``, which intercepts the copy in-page).

Everything degrades to a safe default rather than raising, because both callers
treat a miss as "try the next approach" rather than an error.
"""

import logging
import subprocess
import sys
from typing import List, Optional

from selenium.webdriver.common.keys import Keys

logger = logging.getLogger(__name__)

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform in ("win32", "cygwin")

# Clipboard readers, in preference order per platform. Each entry is an argv
# list that prints the clipboard to stdout. Linux ships neither tool by default,
# hence two candidates.
_CLIPBOARD_COMMANDS = {
    "darwin": [["pbpaste"]],
    "windows": [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]],
    "other": [
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ],
}


def submit_modifier() -> str:
    """Return the modifier key that submits a LinkedIn comment box.

    macOS uses Command: Chrome on macOS does not route Ctrl+Enter to the page's
    submit shortcut, so Ctrl+Enter is silently swallowed there. Every other
    platform uses Control.
    """
    return Keys.COMMAND if IS_MACOS else Keys.CONTROL


def clipboard_commands() -> List[List[str]]:
    """Return the candidate clipboard-read commands for the current platform."""
    if IS_MACOS:
        return _CLIPBOARD_COMMANDS["darwin"]
    if IS_WINDOWS:
        return _CLIPBOARD_COMMANDS["windows"]
    return _CLIPBOARD_COMMANDS["other"]


def read_clipboard() -> str:
    """Return the OS clipboard as text, or ``""`` if it cannot be read.

    Tries each candidate command for the platform and returns the first
    non-empty result. A missing helper binary (common on Linux, where neither
    xclip nor xsel is installed by default) is not an error: the only caller
    uses this as a fallback and treats an empty string as "no URL found".
    """
    for argv in clipboard_commands():
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            logger.debug("Clipboard helper not installed: %s", argv[0])
            continue
        except (subprocess.SubprocessError, OSError) as e:
            logger.debug("Clipboard read via %s failed: %s", argv[0], e)
            continue

        text = (result.stdout or "").strip()
        if text:
            return text

    logger.debug("No clipboard content available on this platform")
    return ""


def describe() -> Optional[str]:
    """Return a short human-readable platform label, for logs and diagnostics."""
    if IS_MACOS:
        return "macOS"
    if IS_WINDOWS:
        return "Windows"
    return sys.platform


# ─── Termination ──────────────────────────────────────────────────────────────

# Exit code for "terminated by SIGTERM", by the shell's 128+signal convention.
# Distinguishable from a crash, which is what lets a caller tell a stop the
# operator asked for from a failure they did not.
EXIT_TERMINATED = 143


def exit_cleanly_on_termination(signals=None) -> List[str]:
    """Make SIGTERM and SIGHUP raise ``SystemExit`` so ``finally`` blocks run.

    **Python does not run ``finally`` blocks when the process is killed by a
    signal.** The default SIGTERM disposition terminates immediately, so every
    cleanup handler in the call stack is skipped.

    For the browser-driving scripts that means ``driver.quit()`` never runs and
    Chrome is orphaned, still holding the profile's ``user-data-dir``. Nothing
    can then open that profile: the next run, and every login attempt, fails
    because a process nobody can see owns the lock.

    Observed 2026-08-02. Rick pressed Stop during a connector run. The
    dashboard called ``terminate()``, the connector died without unwinding, and
    the orphaned Chrome held the profile for the next twenty-three hours. The
    only visible symptom was "The login check could not run".

    ``sys.exit`` raises ``SystemExit``, which is a ``BaseException``: it
    unwinds the stack, runs every ``finally``, and is not swallowed by the
    ``except Exception`` handlers these scripts use around their main loops.

    Returns the names of the signals actually installed, which varies by
    platform: SIGHUP does not exist on Windows.
    """
    import signal

    if signals is None:
        names = ("SIGTERM", "SIGHUP")
    else:
        names = tuple(signals)

    def _bail(signum, _frame):
        logger.warning(
            "Received signal %s; shutting down and closing the browser. "
            "Interrupting again may orphan Chrome and lock the profile.", signum)
        sys.exit(EXIT_TERMINATED)

    installed = []
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _bail)
            installed.append(name)
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or the platform refuses this signal. Not
            # fatal: the script simply keeps the default disposition.
            logger.debug("Could not install a handler for %s", name)
    return installed
