"""Termination unwinds the stack, so the browser gets closed (Phase 15).

Found 2026-08-02, and it is the reason Rick could not log in.

He pressed Stop during a connector run. ``stop_connector`` calls
``proc.terminate()``, which is SIGTERM, and **Python does not run ``finally``
blocks when a signal kills the process**. So ``driver.quit()`` never ran and
Chrome was orphaned, still holding ``chrome_sessions/Rick``. Twenty-three
hours later that process was still alive and still owned the profile lock, so
every attempt to open that profile failed. The only symptom on screen was
"The login check could not run".

The orphan is invisible: no window, no dashboard entry, nothing but a stale
`SingletonLock` symlink pointing at a live pid.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from linkedin_automation import platform_compat as pc


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_handler_installs_for_the_signals_a_stop_actually_sends():
    installed = pc.exit_cleanly_on_termination()

    assert "SIGTERM" in installed, "a dashboard Stop sends SIGTERM"
    if hasattr(signal, "SIGHUP"):
        assert "SIGHUP" in installed, "closing run.command's window sends SIGHUP"


def test_an_unknown_signal_name_is_skipped_rather_than_raising():
    """Platform differences must not take down the script at startup."""
    assert pc.exit_cleanly_on_termination(["SIGNOPE"]) == []


def test_the_terminated_exit_code_is_distinguishable():
    """128+15. A stop the operator asked for must not look like a crash."""
    assert pc.EXIT_TERMINATED == 143


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="needs SIGTERM")
def test_sigterm_runs_finally_blocks_in_a_real_subprocess(tmp_path):
    """The actual property, proven end to end rather than by reading the docs.

    A child installs the handler, enters a try/finally, writes a marker from
    the finally, and is then killed with SIGTERM from outside. If the marker
    exists, cleanup ran; if not, the process died mid-stack exactly as the
    connector did and Chrome would have been orphaned.
    """
    marker = tmp_path / "cleanup_ran.txt"
    ready = tmp_path / "ready.txt"

    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {REPO!r})
        from linkedin_automation import platform_compat as pc

        pc.exit_cleanly_on_termination()
        try:
            open({str(ready)!r}, "w").write("up")
            time.sleep(30)
        finally:
            open({str(marker)!r}, "w").write("closed the browser")
    """), encoding="utf-8")

    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the child never started"

        proc.terminate()
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert marker.exists(), (
        "SIGTERM killed the process without running its finally block, which "
        "is how Chrome gets orphaned holding the profile lock"
    )
    assert proc.returncode == pc.EXIT_TERMINATED


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="needs SIGTERM")
def test_without_the_handler_the_finally_is_skipped(tmp_path):
    """Proven to fail: the same child, minus the handler, loses its cleanup.

    Without this, the test above passes for reasons nobody has checked.
    """
    marker = tmp_path / "cleanup_ran.txt"
    ready = tmp_path / "ready.txt"

    script = tmp_path / "child_nohandler.py"
    script.write_text(textwrap.dedent(f"""
        import time
        try:
            open({str(ready)!r}, "w").write("up")
            time.sleep(30)
        finally:
            open({str(marker)!r}, "w").write("closed the browser")
    """), encoding="utf-8")

    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        proc.terminate()
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert not marker.exists(), (
        "the default SIGTERM disposition ran a finally block, which would mean "
        "the handler this phase adds is unnecessary"
    )


def test_every_browser_driving_entry_point_installs_the_handler():
    """All four, because any one of them can orphan a Chrome on the profile.

    The connector is where it was observed. The scraper, the comment poster
    and the post publisher all open the same profile in the same way and are
    all stoppable from the dashboard.
    """
    import inspect

    from linkedin_automation import auto_connector, comment_poster, post_finder, poster

    for module in (auto_connector, comment_poster, post_finder, poster):
        source = inspect.getsource(module.main)
        assert "exit_cleanly_on_termination" in source, (
            f"{module.__name__}.main does not install the termination handler, "
            f"so a Stop there orphans Chrome and locks the profile"
        )
