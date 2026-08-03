"""macapp_ui.py — the Cocoa shell: a status item and a window (Phase 14b).

**This module holds no decisions.** Every one lives in
:class:`~app_controller.AppController`, which is plain Python and covered by
tests. Cocoa cannot be driven in a headless suite, so anything with logic in
it here would be untestable by construction, and this project has already been
bitten twice by code that was correct and unreachable.

What is here: build an ``NSStatusItem``, render the controller's menu into an
``NSMenu``, put a ``WKWebView`` in an ``NSWindow``, and forward clicks.

Importing this module requires PyObjC, which is macOS-only and installed from
``requirements-macos.txt``. Nothing else in the package imports it, so the
dashboard, the CLI tools and the test suite all run without it.
"""

import logging

import AppKit
import objc
import WebKit
from Foundation import NSObject, NSURL, NSURLRequest, NSMakeRect

from . import app_controller as ctrl

logger = logging.getLogger(__name__)

# SF Symbols, which is what other menu bar apps use. The first version set a
# text glyph instead, and it worked in the sense that the status item existed
# and reported visible: it was simply too small and too anonymous to find in a
# crowded menu bar. A symbol is recognisably an app icon.
#
# Filled means running, outlined means stopped, so the state reads at a glance
# without a colour that would fail in dark mode or for a colourblind operator.
SYMBOL_RUNNING = "person.2.circle.fill"
SYMBOL_STOPPED = "person.2.circle"
SYMBOL_BUSY = "ellipsis.circle"

# Fallback if the symbol is unavailable. Never leave the button blank: a
# status item with no content is a control the operator cannot find at all.
GLYPH_RUNNING = "◉"
GLYPH_STOPPED = "○"
GLYPH_BUSY = "◌"

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860


class DashboardWindow:
    """An ``NSWindow`` holding a ``WKWebView``, with the interface the
    controller expects: ``show`` / ``hide`` / ``is_visible`` / ``load``.

    Closing it hides rather than terminates. That is the whole reason the app
    has a menu bar presence: the dashboard is a background service, and
    closing a window must not stop the scheduler.
    """

    def __init__(self, title="LinkedIn Autocomment"):
        rect = NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        style = (AppKit.NSWindowStyleMaskTitled
                 | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable
                 | AppKit.NSWindowStyleMaskResizable)

        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False)
        self.window.setTitle_(title)
        self.window.setReleasedWhenClosed_(False)   # hide, do not deallocate
        self.window.center()

        config = WebKit.WKWebViewConfiguration.alloc().init()
        self.webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            rect, config)
        self.webview.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self.window.setContentView_(self.webview)

        self._loaded = None

    def load(self, url):
        # Reloading on every show would throw away whatever the operator had
        # open — a half-written note, a review list scrolled into place.
        if self._loaded == url:
            return
        request = NSURLRequest.requestWithURL_(NSURL.URLWithString_(url))
        self.webview.loadRequest_(request)
        self._loaded = url

    def reload(self):
        self.webview.reload()

    def show(self):
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def hide(self):
        self.window.orderOut_(None)

    def is_visible(self):
        return bool(self.window.isVisible())


class AppDelegate(NSObject):
    """Owns the status item and forwards clicks to the controller."""

    def initWithController_(self, controller):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.controller = controller
        self._actions = {}       # NSMenuItem tag -> action id
        return self

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def applicationDidFinishLaunching_(self, notification):
        self.statusItem = (AppKit.NSStatusBar.systemStatusBar()
                           .statusItemWithLength_(AppKit.NSVariableStatusItemLength))
        # Retained explicitly. NSApplication holds its delegate weakly, and a
        # status item whose owner is collected disappears from the menu bar
        # with no error anywhere.
        self.retain()
        self.menu = AppKit.NSMenu.alloc().init()
        # Rebuild on every open so enabled/disabled always reflects reality
        # rather than whatever was true when the app launched.
        self.menu.setDelegate_(self)
        self.statusItem.setMenu_(self.menu)
        self._refresh_glyph()

        # Now that the run loop is up, a window can actually be displayed.
        if getattr(self, "startOnLaunch", False):
            try:
                self.controller.start()
            except Exception:
                logger.exception("Starting the dashboard on launch failed")
            self._refresh_glyph()

    def applicationShouldTerminate_(self, sender):
        # Quit stops the server if this app started it. Doing it here rather
        # than only in the menu handler covers Cmd-Q and a logout.
        try:
            self.controller.quit()
        except Exception:
            logger.exception("Cleanup on quit failed")
        return AppKit.NSTerminateNow

    # ── menu ──────────────────────────────────────────────────────────────────

    def menuNeedsUpdate_(self, menu):
        menu.removeAllItems()
        self._actions.clear()

        for index, spec in enumerate(self.controller.menu()):
            if spec.action == ctrl.SEPARATOR:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue

            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                spec.label, None, "")
            if spec.enabled and spec.action is not None:
                item.setAction_(objc.selector(self.menuClicked_, signature=b"v@:@"))
                item.setTarget_(self)
                item.setTag_(index)
                self._actions[index] = spec.action
            else:
                item.setEnabled_(False)
            menu.addItem_(item)

        self._refresh_glyph()

    def menuClicked_(self, sender):
        action = self._actions.get(sender.tag())
        if action is None:
            return
        try:
            self.controller.handle(action)
        except Exception:
            # A raising menu handler takes the whole app down, and the one
            # thing worse than a control that does nothing is one that quits.
            logger.exception("Menu action %r failed", action)
        if action == ctrl.QUIT:
            AppKit.NSApp.terminate_(self)
        self._refresh_glyph()

    def _refresh_glyph(self):
        state = self.controller.state()
        symbol = {ctrl.RUNNING: SYMBOL_RUNNING,
                  ctrl.BUSY: SYMBOL_BUSY}.get(state, SYMBOL_STOPPED)
        glyph = {ctrl.RUNNING: GLYPH_RUNNING,
                 ctrl.BUSY: GLYPH_BUSY}.get(state, GLYPH_STOPPED)

        button = self.statusItem.button()
        if button is None:
            return

        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol, "LinkedIn Autocomment")
        if image is not None:
            # Template so it follows the menu bar's own colour in light and
            # dark mode rather than being drawn as a fixed-colour bitmap.
            image.setTemplate_(True)
            button.setImage_(image)
            button.setTitle_("")
        else:
            button.setImage_(None)
            button.setTitle_(glyph)
        button.setToolTip_(self.controller.status_title())


def run(supervisor=None):
    """Build the app and enter the Cocoa run loop. Does not return."""
    app = AppKit.NSApplication.sharedApplication()
    # Accessory: no Dock tile, no app menu. Info.plist sets LSUIElement too;
    # this covers running the module directly, outside the bundle.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    window = DashboardWindow()

    def notify(title, message):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.runModal()

    controller = ctrl.AppController(
        supervisor=supervisor, window=window, notify=notify)

    delegate = AppDelegate.alloc().initWithController_(controller)
    app.setDelegate_(delegate)

    # Deliberately NOT started here. start() shows the window, and ordering a
    # window front before the run loop exists does nothing at all: the app
    # launched, the server came up, and neither the window nor any error
    # appeared. Observed 2026-08-02. The delegate does it after launch instead.
    delegate.startOnLaunch = True

    app.run()
