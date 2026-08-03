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


# ─── ChromeDriver on macOS ────────────────────────────────────────────────────

# How long to allow for a `chromedriver --version` probe and for `codesign`.
# Both are near-instant; these bounds exist so a wedged tool cannot hang a run.
DRIVER_PROBE_TIMEOUT = 15
CODESIGN_TIMEOUT = 60


def driver_runs(driver_path: str) -> bool:
    """True when ``chromedriver --version`` actually executes.

    Cheap, and the only reliable test. A driver that macOS will refuse is
    indistinguishable from a working one by looking at the file: right size,
    right permissions, executable bit set.
    """
    try:
        result = subprocess.run(
            [driver_path, "--version"],
            capture_output=True, timeout=DRIVER_PROBE_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("chromedriver probe failed: %s", e)
        return False


def ensure_driver_runnable(driver_path: str) -> bool:
    """Make a freshly downloaded chromedriver executable on macOS.

    **The failure this exists for.** Chrome updates itself, `webdriver_manager`
    downloads a matching chromedriver, and macOS kills the new binary with
    SIGKILL before a single line of it runs. Selenium reports:

        Service .../chromedriver unexpectedly exited. Status code was: -9

    which names neither the cause nor the fix. Observed 2026-08-03: a scrape
    that had worked the day before failed at browser startup, having never
    reached LinkedIn, minutes after Chrome auto-updated overnight.

    The remedy is an ad-hoc code signature. `xattr` does not help: the
    attribute involved is `com.apple.provenance`, which is protected and
    cannot be removed.

    **This does replace a code signature, so it is worth being plain about it.**
    The binary comes from Google's own `chrome-for-testing` endpoint over
    HTTPS, fetched by a pinned dependency, and it is re-signed locally rather
    than trusted from anywhere new. It is also exactly what the operator would
    do by hand, and the alternative is a tool that stops working every time
    Chrome updates with an error nobody can act on.

    Returns True when the driver runs afterwards. Never raises: a driver that
    cannot be repaired should fail at the browser with Selenium's own message
    rather than here.
    """
    if not IS_MACOS or not driver_path:
        return True

    if driver_runs(driver_path):
        return True

    logger.warning(
        "chromedriver at %s will not start, which on macOS is normally a "
        "freshly downloaded driver being refused by Gatekeeper. Re-signing it "
        "locally.", driver_path)

    try:
        result = subprocess.run(
            ["codesign", "--force", "--sign", "-", driver_path],
            capture_output=True, timeout=CODESIGN_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.error("Could not run codesign on chromedriver: %s", e)
        return False

    if result.returncode != 0:
        logger.error(
            "codesign failed on chromedriver (exit %s): %s",
            result.returncode, (result.stderr or b"").decode("utf-8", "replace").strip())
        return False

    if driver_runs(driver_path):
        logger.info("chromedriver re-signed and now starts normally")
        return True

    logger.error(
        "chromedriver still will not start after re-signing. Try deleting "
        "~/.wdm so it downloads again, or run: codesign --force --sign - %s",
        driver_path)
    return False
