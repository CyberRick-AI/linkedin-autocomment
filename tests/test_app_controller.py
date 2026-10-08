"""The menu bar app's behaviour, with no Cocoa involved (Phase 14b).

Cocoa cannot be driven in a headless suite, so every decision the app makes
lives in :mod:`app_controller` and is tested here. The Cocoa layer binds
widgets to these methods and holds no logic of its own; a separate test
asserts that.

Offline: the supervisor, window, notifier and browser opener are all injected.
"""


from linkedin_automation import app_controller as ac
from linkedin_automation import server_supervisor as ss


class FakeSupervisor:
    def __init__(self, serving=False, owns=False, start_result=ss.STARTED,
                 stop_result=True, port=6500):
        self.serving = serving
        self.owns = owns
        self.start_result = start_result
        self.stop_result = stop_result
        self.port = port
        self.starts = 0
        self.stops = 0

    def is_serving(self):
        return self.serving

    def owns_process(self):
        return self.owns

    def start(self, timeout=None):
        self.starts += 1
        if self.start_result != ss.FAILED:
            self.serving = True
            self.owns = True
        return self.start_result

    def stop(self, timeout=None):
        self.stops += 1
        if self.stop_result:
            self.serving = False
            self.owns = False
        return self.stop_result


class FakeWindow:
    def __init__(self):
        self.visible = False
        self.loaded = []

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def is_visible(self):
        return self.visible

    def load(self, url):
        self.loaded.append(url)


def build(**kwargs):
    supervisor = FakeSupervisor(**kwargs)
    window = FakeWindow()
    notices = []
    opened = []
    controller = ac.AppController(
        supervisor=supervisor, window=window,
        notify=lambda t, m: notices.append(m),
        open_url=lambda url: opened.append(url) or True,
    )
    return controller, supervisor, window, notices, opened


def labels(menu):
    return [item.label for item in menu if item.action != ac.SEPARATOR]


def item(menu, action):
    return next(i for i in menu if i.action == action)


# ─── The menu, as the operator sees it ────────────────────────────────────────

def test_the_menu_offers_every_action_the_roadmap_names():
    controller, *_ = build(serving=True, owns=True)

    actions = {i.action for i in controller.menu()}

    for required in (ac.START, ac.STOP, ac.RESTART, ac.SHOW_WINDOW, ac.QUIT):
        assert required in actions, f"the menu has no {required}"


def test_restart_is_in_the_menu():
    """The gap the runbook exists to paper over.

    A running dashboard does not pick up code changes, Flask's reloader is off
    because it ships the Werkzeug debugger, and relaunching the app only opens
    the window because the port already answers. Without this item, updating
    means finding a terminal.
    """
    controller, *_ = build(serving=True, owns=True)

    assert "Restart" in labels(controller.menu())


def test_there_is_no_log_item():
    """Rick's explicit choice: no log menu until Phase 10 ships real logging.

    An item pointing at today's unstructured logs would look like
    observability without being it.
    """
    menu_labels = " ".join(labels(build(serving=True, owns=True)[0].menu())).lower()

    assert "log" not in menu_labels


def test_stopped_offers_start_and_nothing_that_needs_a_server():
    controller, *_ = build(serving=False)
    menu = controller.menu()

    assert item(menu, ac.START).enabled
    assert not item(menu, ac.STOP).enabled
    assert not item(menu, ac.RESTART).enabled
    assert not item(menu, ac.SHOW_WINDOW).enabled


def test_running_and_ours_enables_stop_and_restart():
    controller, *_ = build(serving=True, owns=True)
    menu = controller.menu()

    assert not item(menu, ac.START).enabled, "Start on a running server would double-bind"
    assert item(menu, ac.STOP).enabled
    assert item(menu, ac.RESTART).enabled
    assert item(menu, ac.SHOW_WINDOW).enabled


def test_a_server_someone_else_started_can_be_opened_but_not_stopped():
    """Honesty about ownership, in the menu itself.

    If ./run.sh started it, this app can show it but must not offer to kill
    it. A greyed-out Stop with a status line explaining why beats a Stop that
    silently does nothing.
    """
    controller, *_ = build(serving=True, owns=False)
    menu = controller.menu()

    assert item(menu, ac.SHOW_WINDOW).enabled
    assert not item(menu, ac.STOP).enabled
    assert not item(menu, ac.RESTART).enabled
    assert "started elsewhere" in controller.status_title()


def test_quit_is_always_available():
    """An app you cannot quit from its own menu is the failure mode inverted."""
    for kwargs in ({"serving": False}, {"serving": True, "owns": True},
                   {"serving": True, "owns": False}):
        controller, *_ = build(**kwargs)
        assert item(controller.menu(), ac.QUIT).enabled


def test_the_status_line_names_the_port_not_just_a_colour():
    controller, *_ = build(serving=True, owns=True, port=6501)

    assert "6501" in controller.status_title()


# ─── Actions ──────────────────────────────────────────────────────────────────

def test_start_launches_the_server_and_shows_the_window():
    controller, supervisor, window, notices, _ = build(serving=False)

    assert controller.start() is True
    assert supervisor.starts == 1
    assert window.visible
    assert window.loaded == ["http://localhost:6500"]
    assert notices == []


def test_a_failed_start_tells_the_user_and_shows_nothing():
    controller, _, window, notices, _ = build(serving=False, start_result=ss.FAILED)

    assert controller.start() is False
    assert not window.visible
    assert "port 6500" in notices[0]


def test_stop_closes_the_window_too():
    """Leaving a window onto a dead server is a page that will not load."""
    controller, supervisor, window, _, _ = build(serving=True, owns=True)
    controller.show_window()

    assert controller.stop() is True
    assert supervisor.stops == 1
    assert not window.visible


def test_stop_on_a_server_we_do_not_own_refuses_and_explains():
    controller, supervisor, _, notices, _ = build(serving=True, owns=False)

    assert controller.stop() is False
    assert supervisor.stops == 0, "it tried to kill a process it does not own"
    assert "did not start" in notices[0]


def test_a_stop_that_fails_is_reported():
    controller, _, _, notices, _ = build(serving=True, owns=True, stop_result=False)

    assert controller.stop() is False
    assert "chromedriver" in notices[0], "it does not say what to look for"


def test_restart_stops_then_starts():
    controller, supervisor, _, _, _ = build(serving=True, owns=True)

    assert controller.restart() is True
    assert supervisor.stops == 1
    assert supervisor.starts == 1


def test_restart_on_a_server_we_do_not_own_refuses():
    controller, supervisor, _, notices, _ = build(serving=True, owns=False)

    assert controller.restart() is False
    assert supervisor.stops == 0 and supervisor.starts == 0
    assert "cannot restart" in notices[0]


def test_a_restart_whose_stop_fails_does_not_go_on_to_start():
    """Starting on top of a server that would not die is the double-bind."""
    controller, supervisor, _, _, _ = build(serving=True, owns=True, stop_result=False)

    assert controller.restart() is False
    assert supervisor.starts == 0


def test_quit_stops_a_server_this_app_started():
    """Quit means quit. Leaving it running rebuilds the invisible background
    process the whole phase exists to remove."""
    controller, supervisor, _, _, _ = build(serving=True, owns=True)

    assert controller.quit() is True
    assert supervisor.stops == 1


def test_quit_leaves_someone_elses_server_alone():
    controller, supervisor, _, _, _ = build(serving=True, owns=False)

    assert controller.quit() is True
    assert supervisor.stops == 0


def test_open_in_browser_uses_the_dashboard_url():
    controller, _, _, _, opened = build(serving=True, owns=True, port=6502)

    assert controller.open_in_browser() is True
    assert opened == ["http://localhost:6502"]


def test_show_window_falls_back_to_the_browser_with_no_window():
    """The controller is usable before the Cocoa layer hands it a window."""
    supervisor = FakeSupervisor(serving=True, owns=True)
    opened = []
    controller = ac.AppController(supervisor=supervisor, window=None,
                                  open_url=lambda url: opened.append(url) or True)

    assert controller.show_window() is True
    assert opened == ["http://localhost:6500"]


def test_an_unknown_action_is_refused_rather_than_raising():
    """A menu wired to a typo must not take the app down."""
    controller, *_ = build(serving=True, owns=True)

    assert controller.handle("nonsense") is False


def test_every_menu_action_has_a_handler():
    """The guard on the wiring.

    A menu item whose identifier no handler answers is a control that does
    nothing and says nothing, which is exactly what Phase 5b removed from the
    dashboard.
    """
    controller, *_ = build(serving=True, owns=True)

    for menu_item in controller.menu():
        if menu_item.action in (None, ac.SEPARATOR):
            continue
        assert menu_item.action in {
            ac.START, ac.STOP, ac.RESTART, ac.SHOW_WINDOW,
            ac.OPEN_IN_BROWSER, ac.HEALTH, ac.QUIT,
        }, f"{menu_item.action} is rendered but unhandled"


def test_busy_disables_everything_that_could_race():
    """Two starts at once is the double-bind the port check exists to prevent."""
    controller, *_ = build(serving=False)
    controller.busy = True

    menu = controller.menu()

    assert controller.state() == ac.BUSY
    assert not item(menu, ac.START).enabled
    assert not item(menu, ac.STOP).enabled
    assert controller.status_title() == "Working…"
