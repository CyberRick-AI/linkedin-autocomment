"""app_controller.py — what the menu bar app does, with no Cocoa in it.

Phase 14b puts a menu bar item and a window on top of Phase 14a's supervisor.
Cocoa cannot be exercised in a headless test suite, so everything that decides
*what happens* lives here and the Cocoa layer in :mod:`macapp_ui` only binds
widgets to these methods.

That split is the point. A menu handler with logic inside it is a handler
nobody can test, and this project has already been bitten twice by code that
was correct and unreachable.
"""

import logging
import webbrowser

from . import server_supervisor as ss

logger = logging.getLogger(__name__)

# Menu item identifiers. Strings rather than an enum so the Cocoa layer can
# carry one in an NSMenuItem's representedObject without a bridge conversion.
START = "start"
STOP = "stop"
RESTART = "restart"
SHOW_WINDOW = "show_window"
OPEN_IN_BROWSER = "open_in_browser"
HEALTH = "health"
QUIT = "quit"
SEPARATOR = "-"

RUNNING = "running"
STOPPED = "stopped"
BUSY = "busy"


class MenuItem:
    """One row in the menu: a label, an action, and whether it is clickable."""

    def __init__(self, action, label, enabled=True):
        self.action = action
        self.label = label
        self.enabled = enabled

    def __repr__(self):
        state = "" if self.enabled else " (disabled)"
        return f"<MenuItem {self.action}: {self.label!r}{state}>"

    def __eq__(self, other):
        return (isinstance(other, MenuItem)
                and (self.action, self.label, self.enabled)
                == (other.action, other.label, other.enabled))


class AppController:
    """The app's behaviour: server lifecycle, window visibility, menu state.

    Args:
        supervisor: a :class:`~server_supervisor.ServerSupervisor`.
        window: anything with ``show()``/``hide()``/``is_visible()``/``load(url)``.
            None until the Cocoa layer supplies one, so the controller is
            constructible and testable with no window at all.
        notify: ``notify(title, message)`` for user-visible failures. A menu
            bar app has no stdout anybody reads.
        open_url: browser opener, injected so tests never launch one.
    """

    def __init__(self, supervisor=None, window=None, notify=None, open_url=None):
        self.supervisor = supervisor or ss.ServerSupervisor()
        self.window = window
        self._notify = notify or (lambda title, message: None)
        self._open_url = open_url or webbrowser.open
        self.busy = False

    # ── state ─────────────────────────────────────────────────────────────────

    @property
    def url(self):
        return f"http://localhost:{self.supervisor.port}"

    def state(self):
        """One of RUNNING, STOPPED, BUSY."""
        if self.busy:
            return BUSY
        return RUNNING if self.supervisor.is_serving() else STOPPED

    def status_title(self):
        """The line at the top of the menu. Never a bare colour word.

        It also says whether *this app* started the server, because that
        decides whether Stop can do anything, and an operator who launched
        ./run.sh separately needs to know why Stop is greyed out.
        """
        state = self.state()
        if state == BUSY:
            return "Working…"
        if state == STOPPED:
            return "Dashboard: stopped"
        if self.supervisor.owns_process():
            return f"Dashboard: running on {self.supervisor.port}"
        return f"Dashboard: running on {self.supervisor.port} (started elsewhere)"

    def menu(self):
        """The whole menu, as data. The Cocoa layer renders exactly this."""
        state = self.state()
        running = state == RUNNING
        busy = state == BUSY
        # Stop and Restart act on a process this app owns. A server started by
        # ./run.sh is visible and openable, but not ours to kill: claiming
        # otherwise is the same lie as reporting a health check that never ran.
        owned = running and self.supervisor.owns_process()

        return [
            MenuItem(None, self.status_title(), enabled=False),
            MenuItem(SEPARATOR, "", enabled=False),
            MenuItem(START, "Start", enabled=not running and not busy),
            MenuItem(STOP, "Stop", enabled=owned and not busy),
            MenuItem(RESTART, "Restart", enabled=owned and not busy),
            MenuItem(SEPARATOR, "", enabled=False),
            MenuItem(SHOW_WINDOW, "Show Dashboard", enabled=running and not busy),
            MenuItem(OPEN_IN_BROWSER, "Open in Browser", enabled=running and not busy),
            MenuItem(HEALTH, "Check Selectors…", enabled=running and not busy),
            MenuItem(SEPARATOR, "", enabled=False),
            MenuItem(QUIT, "Quit", enabled=not busy),
        ]

    # ── actions ───────────────────────────────────────────────────────────────

    def handle(self, action):
        """Dispatch a menu action by identifier. Returns True when it worked."""
        handlers = {
            START: self.start,
            STOP: self.stop,
            RESTART: self.restart,
            SHOW_WINDOW: self.show_window,
            OPEN_IN_BROWSER: self.open_in_browser,
            HEALTH: self.open_health,
            QUIT: self.quit,
        }
        handler = handlers.get(action)
        if handler is None:
            logger.warning("Unknown menu action %r", action)
            return False
        return handler()

    def start(self):
        """Start the dashboard, then show it. Already-running is success."""
        self.busy = True
        try:
            outcome = self.supervisor.start()
        finally:
            self.busy = False

        if outcome == ss.FAILED:
            self._notify(
                "LinkedIn Autocomment",
                f"The dashboard did not start. Something may already be using "
                f"port {self.supervisor.port}. Run ./run.sh in the project "
                f"folder to see the error.")
            return False

        self.show_window()
        return True

    def stop(self):
        """Stop the server this app started."""
        if not self.supervisor.owns_process():
            self._notify(
                "LinkedIn Autocomment",
                "This app did not start the dashboard, so it cannot stop it. "
                "Close the terminal window it was started from.")
            return False

        self.busy = True
        try:
            stopped = self.supervisor.stop()
        finally:
            self.busy = False

        if not stopped:
            self._notify("LinkedIn Autocomment",
                         "The dashboard did not stop cleanly. Check for leftover "
                         "python or chromedriver processes.")
            return False

        if self.window is not None:
            self.window.hide()
        return True

    def restart(self):
        """Stop then start.

        The reason this exists: a running dashboard does not pick up code
        changes, Flask's reloader is off by default because it ships the
        Werkzeug debugger, and relaunching the app only opens the window
        because the port is already answering. Without Restart in this menu,
        updating means finding a terminal.
        """
        if not self.supervisor.owns_process():
            self._notify(
                "LinkedIn Autocomment",
                "This app did not start the dashboard, so it cannot restart it. "
                "Stop it where it was started, then press Start here.")
            return False
        if not self.stop():
            return False
        return self.start()

    def show_window(self):
        """Bring up the window on the dashboard."""
        if self.window is None:
            return self.open_in_browser()
        self.window.load(self.url)
        self.window.show()
        return True

    def open_in_browser(self):
        """Escape hatch: the real browser, with its devtools and extensions."""
        try:
            self._open_url(self.url)
            return True
        except Exception as e:
            logger.error("Could not open %s: %s", self.url, e)
            self._notify("LinkedIn Autocomment",
                         f"Could not open a browser. The dashboard is at {self.url}")
            return False

    def open_health(self):
        """Show the selector health page.

        Deliberately opens the dashboard's own health screen rather than
        running a check from the menu. A real check needs a profile and, for
        the posting path, a live LinkedIn session — so a menu item that ran
        one silently would either pick a profile the operator did not choose
        or report on a path it never looked at. That second failure is
        finding G3, and it is not being rebuilt in a new place.
        """
        return self.show_window()

    def quit(self):
        """Stop the server if we own it, then let the app exit.

        Quit means quit: leaving the server running after the app is gone
        recreates the invisible background process this phase exists to
        remove. A server we did not start is left alone.
        """
        if self.supervisor.owns_process():
            self.supervisor.stop()
        return True
