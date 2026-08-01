"""login_check.py — open a profile's Chrome session and report LinkedIn login status.

Manual utility. Launches Chrome with the profile's persistent ``user-data-dir``,
navigates to the LinkedIn feed, and reports whether you are logged in. If you are
not, it prints instructions and waits while you log in manually, then re-checks.
Your session persists, so subsequent scrape/post/connect runs reuse it without
prompting.

The wait needs no terminal: it polls the browser rather than prompting on stdin,
so it works from an editor's run button, the dashboard, or a menu bar app.

Usage:
    python tools/login_check.py --profile jeff
    python tools/login_check.py                 # uses the default profile
    python tools/login_check.py --profile jeff --no-wait   # report and exit
    python tools/login_check.py --profile jeff --timeout 600

Exit codes:
    0  logged in
    1  error (no/unknown profile, browser failure)
    2  not logged in (login required)
"""

import argparse
import logging
import sys
import time

# Make `import linkedin_automation` resolve when run as `python tools/login_check.py`
# (project root is this file's grandparent directory).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    WebDriverException,
)

from linkedin_automation import profile_manager as pm

logger = logging.getLogger(__name__)


def status_report(logged_in: bool, profile_name: str):
    """Return ``(human_message, exit_code)`` for a login-status result.

    Pure function (no browser), so it is unit-testable on its own.
    """
    name = profile_name or "default"
    if logged_in:
        return (
            f"✅ Profile '{name}' is logged in to LinkedIn. "
            f"Scrape / post / connect runs will reuse this session.",
            pm.EXIT_OK,
        )
    return (
        f"❌ Profile '{name}' is NOT logged in to LinkedIn.\n"
        f"   Log in manually in the Chrome window, then close it — your\n"
        f"   session will persist. Re-run to confirm:\n"
        f"   python tools/login_check.py --profile {name}",
        pm.EXIT_LOGIN_REQUIRED,
    )


LOGIN_POLL_SECONDS = 5
LOGIN_WAIT_TIMEOUT_SECONDS = 300


def wait_for_manual_login(driver, timeout: float = LOGIN_WAIT_TIMEOUT_SECONDS,
                          poll: float = LOGIN_POLL_SECONDS,
                          sleep=time.sleep, clock=time.monotonic) -> bool:
    """Hold the browser open and poll until the session is logged in.

    Replaces a bare ``input()``. That prompt assumed a terminal, and this script
    is increasingly launched from things that have none: the Run button in an
    editor, the dashboard, and shortly the menu bar app. With no stdin it either
    raised EOFError into a generic handler or blocked forever showing nothing,
    which is the same hang class as B7 in Phase 3.

    Deliberately does NOT re-navigate while waiting. LinkedIn redirects to the
    feed on a successful login, so reading the current page is enough, and a
    reload would wipe a half-filled login form or a verification challenge.

    ``sleep`` and ``clock`` are injectable so this is testable without a
    real wait. Returns True on login, False at the timeout.
    """
    deadline = clock() + timeout
    while True:
        if pm.is_logged_in_on_page(driver):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        print(f"   waiting for login... {int(remaining)}s left", flush=True)
        sleep(min(poll, remaining))


def check_login(profile_name: str = None, wait_for_manual: bool = True,
                timeout: float = LOGIN_WAIT_TIMEOUT_SECONDS) -> int:
    """Open the profile's Chrome session, report status, and return an exit code."""
    driver = None
    try:
        pm.auto_migrate_from_env()
        driver, _profile = pm.create_driver(profile_name)
        # URL-based detection (is_logged_in_on_page) after a single navigation —
        # avoids the false "not logged in" the old element wait produced.
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(4)
        logged_in = pm.is_logged_in_on_page(driver)
        message, code = status_report(logged_in, profile_name)
        print(message)

        if not logged_in and wait_for_manual:
            print(
                f"\nLog in in the Chrome window that just opened. This will "
                f"detect it on its own, checking every {LOGIN_POLL_SECONDS}s "
                f"for up to {int(timeout)}s.\nNothing to press. Closing the "
                f"window early also ends the wait; your session still persists."
            )
            logged_in = wait_for_manual_login(driver, timeout=timeout)
            if not logged_in:
                print(f"\n⏱  Gave up after {int(timeout)}s without a login.")
            message, code = status_report(logged_in, profile_name)
            print(message)

        return code

    except ValueError as e:
        # Raised by create_driver for missing/unknown profile.
        print(f"❌ {e}")
        return pm.EXIT_ERROR
    except (NoSuchWindowException, InvalidSessionIdException, WebDriverException) as e:
        # The browser is gone. Overwhelmingly this means the user closed the
        # Chrome window instead of pressing Enter, which the tool's own message
        # used to tell them to do. Reporting that as a generic error made a
        # successful login look like a failure.
        message = str(e).lower()
        if any(m in message for m in ("no such window", "target window already closed",
                                      "web view not found", "invalid session id",
                                      "disconnected", "not connected")):
            print(
                "\n⚠️  The Chrome window was closed before the re-check.\n"
                "   Your session is almost certainly saved: closing the browser\n"
                "   does not discard it. Confirm with:\n"
                f"   python tools/login_check.py --profile {profile_name or 'default'} --no-wait"
            )
            return pm.EXIT_LOGIN_REQUIRED
        print(f"❌ Browser error while checking login status: {e}")
        return pm.EXIT_ERROR
    except Exception as e:
        print(f"❌ Could not check login status: {e}")
        return pm.EXIT_ERROR
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                logger.debug("Driver already closed", exc_info=True)


def main(argv=None) -> int:
    """Parse args and run the login check. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Check LinkedIn login status for a Chrome-session profile"
    )
    parser.add_argument(
        "--profile", default=None,
        help="Profile name (uses the default profile if omitted)",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="Report status and exit; do not wait for manual login",
    )
    parser.add_argument(
        "--timeout", type=float, default=LOGIN_WAIT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for a manual login (default {int(LOGIN_WAIT_TIMEOUT_SECONDS)})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    return check_login(args.profile, wait_for_manual=not args.no_wait,
                       timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
