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
