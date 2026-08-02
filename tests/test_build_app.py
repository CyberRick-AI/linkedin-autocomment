"""The .app bundle's shape (Phase 14a).

A macOS bundle is a directory with a required structure. Getting it wrong
produces one of macOS's least helpful failures: double-clicking does nothing
at all, with no dialog, no log line and nothing in Console. So the structure
is asserted rather than eyeballed.

Offline and side-effect free: every test builds into ``tmp_path``.
"""

import os
import plistlib
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from tools import build_app  # noqa: E402


@pytest.fixture
def bundle(tmp_path):
    """Build a real bundle into tmp, pointing at a fake repo root."""
    return build_app.build_bundle(str(tmp_path), "/fake/repo")


# ─── Structure ────────────────────────────────────────────────────────────────

def test_the_bundle_has_the_directory_layout_macos_requires(bundle):
    """The four paths Launch Services looks for.

    Any one of them missing makes the app silently refuse to open.
    """
    assert bundle.endswith("LinkedIn Autocomment.app")
    assert os.path.isdir(os.path.join(bundle, "Contents", "MacOS"))
    assert os.path.isdir(os.path.join(bundle, "Contents", "Resources"))
    assert os.path.isfile(os.path.join(bundle, "Contents", "Info.plist"))
    assert os.path.isfile(os.path.join(bundle, "Contents", "MacOS", "launcher"))


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows has no executable bit; the bundle is macOS-only")
def test_the_launcher_is_executable(bundle):
    """Without the executable bit the app fails to open and says nothing.

    ``open`` reports no error to the user and writes nothing to stdout, so this
    presents as a double-click that does nothing at all.
    """
    launcher = os.path.join(bundle, "Contents", "MacOS", "launcher")
    mode = os.stat(launcher).st_mode

    assert mode & stat.S_IXUSR, "the launcher is not executable by its owner"


def test_the_executable_named_in_the_plist_is_the_file_that_exists(bundle):
    """A mismatch here is the other silent-refusal case.

    ``CFBundleExecutable`` naming a file that is not in ``MacOS/`` fails
    exactly like a missing executable bit: nothing happens, nothing is logged.
    """
    with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
        plist = plistlib.load(handle)

    named = plist["CFBundleExecutable"]
    assert os.path.isfile(os.path.join(bundle, "Contents", "MacOS", named))


# ─── Info.plist ───────────────────────────────────────────────────────────────

def test_it_is_an_accessory_app_with_no_dock_tile():
    """``LSUIElement`` is what makes this a background service rather than an app.

    Without it the Dock shows a tile and Cmd-Tab lists it, and quitting from
    the Dock would stop the server behind the operator's back. Phase 14's whole
    premise is that this is a background service with a menu bar presence.
    """
    plist = build_app.build_info_plist()

    assert plist["LSUIElement"] is True


def test_a_webview_is_allowed_to_load_the_local_dashboard():
    """App Transport Security blocks plain http:// by default.

    The dashboard is loopback-only and will never be https, so without this the
    Phase 14b window would load a blank page with the reason only visible in a
    WebKit console nobody opens.
    """
    plist = build_app.build_info_plist()

    assert plist["NSAppTransportSecurity"]["NSAllowsLocalNetworking"] is True


def test_the_plist_round_trips_through_the_real_parser(bundle):
    """It has to satisfy macOS's parser, not just Python's dict literal."""
    with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
        plist = plistlib.load(handle)

    for key in ("CFBundleName", "CFBundleIdentifier", "CFBundleExecutable",
                "CFBundleVersion", "CFBundlePackageType"):
        assert plist.get(key), f"{key} is missing or empty"
    assert plist["CFBundlePackageType"] == "APPL"


# ─── The launcher script ──────────────────────────────────────────────────────

def test_the_launcher_uses_the_projects_own_python():
    """Phase 14's criterion: it must not assume a system Python.

    The venv sits beside the code, so the bundle locates it rather than
    freezing a second copy of every dependency into the app.
    """
    repo = "/Users/someone/Projects/LinkedIn/repo"
    script = build_app.build_launcher(repo)

    # Asserted as two parts, because the script composes the path from shell
    # variables and never contains the joined string literally.
    assert f'REPO="{repo}"' in script
    assert '"$REPO/.venv/bin/python"' in script

    # And it must not fall back to whatever python is on PATH, which is the
    # criterion: a system Python has none of this project's dependencies.
    assert "/usr/bin/python" not in script
    assert "exec python" not in script


def test_the_launcher_explains_itself_when_the_environment_is_missing():
    """A GUI app has no stdout, so a missing venv must raise a dialog.

    Otherwise the app opens, exits instantly, and the operator sees a bounce
    with no explanation. That is the silent-surface failure this project keeps
    finding, and a launcher is the easiest place to reintroduce it.
    """
    script = build_app.build_launcher("/fake/repo")

    assert "osascript" in script, "there is no way to tell the user anything"
    assert "setup.sh" in script, "the dialog does not say how to fix it"


def test_the_launcher_execs_rather_than_forking():
    """``exec`` is why quitting leaves nothing behind.

    Without it the shell stays alive as the app's parent, so macOS keeps
    reporting the app as running after it has quit.
    """
    script = build_app.build_launcher("/fake/repo")

    assert "exec " in script


def test_the_launcher_runs_the_app_module_not_the_dashboard():
    """The app owns the server's lifecycle; it is not the server.

    Launching the dashboard directly would give back the process the operator
    cannot stop from the menu, which is what the phase is removing.
    """
    script = build_app.build_launcher("/fake/repo")

    assert "linkedin_automation.macapp" in script
    assert "-m linkedin_automation.dashboard" not in script


def test_the_launcher_is_strict_about_failure():
    """``set -euo pipefail``, so a broken step stops rather than continuing."""
    script = build_app.build_launcher("/fake/repo")

    assert "set -euo pipefail" in script


# ─── Rebuilding ───────────────────────────────────────────────────────────────

def test_building_twice_over_an_existing_bundle_succeeds(tmp_path):
    """Rebuilds are the normal case and must not need a manual delete.

    Deliberately overwrites in place rather than removing the tree first: a
    recursive delete of a path built from a command-line argument is precisely
    the operation this project's filesystem rules single out.
    """
    first = build_app.build_bundle(str(tmp_path), "/repo/one")
    second = build_app.build_bundle(str(tmp_path), "/repo/two")

    assert first == second
    launcher = os.path.join(second, "Contents", "MacOS", "launcher")
    with open(launcher, encoding="utf-8") as handle:
        assert "/repo/two" in handle.read(), "the rebuild left the old path"


def test_the_bundle_name_is_configurable_and_consistent(tmp_path):
    """The .app name and the plist name must agree, or Finder shows two names."""
    bundle = build_app.build_bundle(str(tmp_path), "/fake/repo", app_name="Other Name")

    assert bundle.endswith("Other Name.app")
    with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
        assert plistlib.load(handle)["CFBundleName"] == "Other Name"


# ─── The app entry point ──────────────────────────────────────────────────────

from linkedin_automation import macapp  # noqa: E402
from linkedin_automation import server_supervisor as ss  # noqa: E402


class FakeSupervisor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.starts = 0

    def start(self, timeout=None):
        self.starts += 1
        return self.outcome


def test_the_app_starts_the_server_and_opens_it():
    opened = []
    sup = FakeSupervisor(ss.STARTED)

    code = macapp.main(argv=[], ui=None, supervisor=sup,
                       opener=lambda url: opened.append(url) or True,
                       alerter=lambda t, m: None)

    assert code == macapp.EXIT_OK
    assert sup.starts == 1
    assert opened == ["http://localhost:6500"]


def test_opening_when_the_server_is_already_up_still_shows_the_dashboard():
    """Launching twice must surface the running server, not error at the user.

    Phase 14's criterion. The supervisor refuses the double-bind; this asserts
    the app treats that refusal as success rather than as a failure to report.
    """
    opened = []
    alerts = []

    code = macapp.main(argv=[], ui=None, supervisor=FakeSupervisor(ss.ALREADY_RUNNING),
                       opener=lambda url: opened.append(url) or True,
                       alerter=lambda t, m: alerts.append(m))

    assert code == macapp.EXIT_OK
    assert opened == ["http://localhost:6500"]
    assert alerts == [], "a normal second launch showed the user an error"


def test_a_failed_start_tells_the_user_why_and_does_not_open_a_browser():
    """A GUI app's only voice is a dialog. Silence here is a Dock bounce."""
    opened = []
    alerts = []

    code = macapp.main(argv=[], ui=None, supervisor=FakeSupervisor(ss.FAILED),
                       opener=lambda url: opened.append(url) or True,
                       alerter=lambda t, m: alerts.append(m))

    assert code == macapp.EXIT_ERROR
    assert opened == [], "it opened a browser onto a server that did not start"
    assert len(alerts) == 1
    assert "port 6500" in alerts[0], "the dialog does not name the likely cause"
    assert "run.sh" in alerts[0], "the dialog does not say how to see the error"


def test_a_browser_that_will_not_open_still_tells_the_user_the_address():
    """Degrade to something actionable rather than to nothing."""
    alerts = []

    code = macapp.main(argv=[], ui=None, supervisor=FakeSupervisor(ss.STARTED),
                       opener=lambda url: False,
                       alerter=lambda t, m: alerts.append(m))

    assert code == macapp.EXIT_ERROR
    assert "http://localhost:6500" in alerts[0]


def test_the_port_can_be_overridden_on_the_command_line():
    """So a bounded health check never collides with the operator's server."""
    opened = []

    macapp.main(argv=["6501"], ui=None, supervisor=FakeSupervisor(ss.STARTED),
                opener=lambda url: opened.append(url) or True,
                alerter=lambda t, m: None)

    assert opened == ["http://localhost:6501"]


def test_applescript_quoting_survives_a_message_with_quotes():
    """An unescaped quote turns the dialog into a syntax error and shows nothing."""
    quoted = macapp._as_applescript('He said "no" \\ then left')

    assert quoted.startswith('"') and quoted.endswith('"')
    assert '\\"no\\"' in quoted
    assert "\\\\" in quoted


def test_the_repo_root_is_the_package_parent():
    """The launcher cds here, so a wrong answer breaks every relative path."""
    root = macapp.repo_root()

    assert os.path.isdir(os.path.join(root, "linkedin_automation"))
    assert os.path.isfile(os.path.join(root, "requirements.txt"))


# ─── The Cocoa layer holds no decisions (Phase 14b) ───────────────────────────

def test_the_ui_module_is_not_imported_by_anything_else():
    """PyObjC is macOS-only and optional. Nothing else may depend on it.

    The dashboard, the CLI tools and this suite all have to run on a machine
    that never installed requirements-macos.txt, and on Windows in CI.
    """
    import pathlib

    package = pathlib.Path(build_app.__file__).parent.parent / "linkedin_automation"
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name in ("macapp_ui.py", "macapp.py"):
            continue
        # Real import statements only. A docstring naming the module is not
        # a dependency, and the first version of this test failed on one.
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "macapp_ui" in stripped:
                offenders.append(f"{path.name}: {stripped}")

    assert offenders == [], f"these import the Cocoa UI: {offenders}"


def test_macapp_imports_the_ui_lazily():
    """A top-level import would break the package wherever PyObjC is absent."""
    import pathlib

    source = pathlib.Path(macapp.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and "macapp_ui" in stripped:
            assert line.startswith("        "), (
                f"macapp_ui is imported at module scope: {stripped!r}"
            )


def test_the_cocoa_layer_delegates_every_action_to_the_controller():
    """The split that keeps the app testable.

    Cocoa cannot be exercised headlessly, so any decision made inside the UI
    module is a decision no test covers. This asserts the UI calls into the
    controller rather than reimplementing it.
    """
    import pathlib

    ui = (pathlib.Path(build_app.__file__).parent.parent
          / "linkedin_automation" / "macapp_ui.py").read_text(encoding="utf-8")

    assert "self.controller.handle(action)" in ui
    assert "self.controller.menu()" in ui
    # The UI must not talk to the supervisor directly; that is the controller's
    # job, and going around it is how the two drift apart.
    assert "supervisor.start()" not in ui
    assert "supervisor.stop()" not in ui


def test_the_macos_requirements_are_a_separate_file():
    """Nobody on Linux or Windows, and not the upstream maintainer, installs
    a Cocoa binding to run the dashboard."""
    import pathlib

    root = pathlib.Path(build_app.__file__).parent.parent
    base = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    mac = (root / "requirements-macos.txt").read_text(encoding="utf-8").lower()

    assert "pyobjc" not in base, "PyObjC leaked into the cross-platform requirements"
    assert "pyobjc-framework-cocoa" in mac
    assert "pyobjc-framework-webkit" in mac


def test_the_fallback_runs_when_the_ui_is_unavailable():
    """A missing optional dependency degrades; it does not fail.

    Without PyObjC the app still starts the dashboard and opens a browser,
    which is the Phase 14a behaviour and still better than a terminal window
    you must not close.
    """
    opened = []

    code = macapp.main(argv=["--no-ui"], supervisor=FakeSupervisor(ss.STARTED),
                       opener=lambda url: opened.append(url) or True,
                       alerter=lambda t, m: None, ui=None)

    assert code == macapp.EXIT_OK
    assert opened == ["http://localhost:6500"]


def test_the_ui_is_handed_the_supervisor_when_it_is_available():
    """One supervisor, so the menu acts on the server the app started."""
    handed = {}

    class FakeUI:
        @staticmethod
        def run(supervisor=None):
            handed["supervisor"] = supervisor

    supervisor = FakeSupervisor(ss.STARTED)
    code = macapp.main(argv=[], supervisor=supervisor, ui=FakeUI,
                       opener=lambda url: True, alerter=lambda t, m: None)

    assert code == macapp.EXIT_OK
    assert handed["supervisor"] is supervisor
