"""Dashboard hardening: debug mode, bind address, port config, error pages.

ROADMAP Phase 5, closing AUDIT findings D1 (Flask debug on by default) and G2
(port hardcoded in three places).

Offline: no browser, no network, no socket is bound. ``main`` is exercised
with ``app.run`` replaced, so these assert the configuration the server would
boot with rather than booting one.
"""

import pytest
from werkzeug.debug import DebuggedApplication

from linkedin_automation import dashboard
from linkedin_automation import profile_manager as pm


@pytest.fixture(autouse=True)
def _clear_dashboard_env(monkeypatch):
    """Start every test from an unset environment, whatever the shell has."""
    monkeypatch.delenv("LINKEDIN_DASHBOARD_DEBUG", raising=False)
    monkeypatch.delenv("LINKEDIN_DASHBOARD_PORT", raising=False)
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)


@pytest.fixture
def run_calls(monkeypatch):
    """Capture the kwargs ``main`` would hand to ``app.run``, without serving.

    Also neutralises the two side effects in ``main``: the .env credential
    migration (touches the OS credential store) and the scheduler thread
    (would drive LinkedIn).
    """
    calls = []
    monkeypatch.setattr(dashboard.app, "run", lambda **kw: calls.append(kw))
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(dashboard.scheduler_engine, "start", lambda: None)
    return calls


@pytest.fixture
def error_client():
    """Test client that runs error handlers instead of propagating.

    The shared ``api_client`` fixture sets ``TESTING=True``, which makes Flask
    re-raise out of the test client. That is the right default for endpoint
    tests and the wrong one here: it would bypass the very handlers under test.
    """
    previous = dashboard.app.config.get("TESTING")
    dashboard.app.config.update(TESTING=False)
    yield dashboard.app.test_client()
    dashboard.app.config.update(TESTING=previous)


# ─── D1: debug mode is off, and the debugger is not loaded ────────────────────

def test_debug_off_by_default():
    assert dashboard.get_debug() is False


def test_app_debug_flag_is_false():
    """The gate's literal assertion: ``app.debug is False`` under default config."""
    assert dashboard.app.debug is False


def test_werkzeug_debugger_middleware_absent():
    """Nothing has wrapped the WSGI app in the interactive debugger."""
    assert not isinstance(dashboard.app.wsgi_app, DebuggedApplication)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_debug_opt_in_accepts_truthy_values(monkeypatch, value):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_DEBUG", value)
    assert dashboard.get_debug() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "2"])
def test_debug_stays_off_for_anything_else(monkeypatch, value):
    """Only an explicit opt-in counts. A stray value does not arm the debugger."""
    monkeypatch.setenv("LINKEDIN_DASHBOARD_DEBUG", value)
    assert dashboard.get_debug() is False


def test_main_runs_with_debug_off(run_calls):
    dashboard.main()
    assert run_calls[0]["debug"] is False


def test_main_honours_the_debug_opt_in(monkeypatch, run_calls):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_DEBUG", "1")
    dashboard.main()
    assert run_calls[0]["debug"] is True


def test_debug_banner_only_prints_when_debug_is_on(monkeypatch, run_calls, capsys):
    dashboard.main()
    assert "DEBUG MODE IS ON" not in capsys.readouterr().out

    monkeypatch.setenv("LINKEDIN_DASHBOARD_DEBUG", "1")
    dashboard.main()
    assert "DEBUG MODE IS ON" in capsys.readouterr().out


# ─── D1: the bind address stays on loopback ───────────────────────────────────

def test_bind_address_is_loopback():
    assert dashboard.HOST == "127.0.0.1"


def test_main_binds_loopback(run_calls):
    """Not 0.0.0.0. An unauthenticated LinkedIn-driving API stays off the network."""
    dashboard.main()
    assert run_calls[0]["host"] == "127.0.0.1"


def test_bind_address_ignores_the_environment(monkeypatch, run_calls):
    """The host is a constant on purpose: no env var can widen the bind."""
    monkeypatch.setenv("LINKEDIN_DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("FLASK_RUN_HOST", "0.0.0.0")
    dashboard.main()
    assert run_calls[0]["host"] == "127.0.0.1"


# ─── G2: the port is configuration, with 6500 as the default ──────────────────

def test_port_defaults_to_6500():
    assert dashboard.get_port() == 6500


def test_port_reads_the_environment(monkeypatch):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_PORT", "7777")
    assert dashboard.get_port() == 7777


def test_port_tolerates_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_PORT", "  8080 ")
    assert dashboard.get_port() == 8080


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_port_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_PORT", value)
    assert dashboard.get_port() == 6500


def test_unparseable_port_warns_and_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_PORT", "six thousand")
    with caplog.at_level("WARNING", logger=dashboard.logger.name):
        assert dashboard.get_port() == 6500
    assert "six thousand" in caplog.text


@pytest.mark.parametrize("value", ["0", "-1", "65536", "999999"])
def test_out_of_range_port_warns_and_falls_back(monkeypatch, caplog, value):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_PORT", value)
    with caplog.at_level("WARNING", logger=dashboard.logger.name):
        assert dashboard.get_port() == 6500
    assert value in caplog.text


def test_main_serves_on_the_configured_port(monkeypatch, run_calls, capsys):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_PORT", "7001")
    dashboard.main()
    assert run_calls[0]["port"] == 7001
    # The banner must agree with the bind, or the printed URL sends the user
    # to a port nothing is listening on.
    assert "http://localhost:7001" in capsys.readouterr().out


# ─── The reloader guard: one scheduler, not two ───────────────────────────────

def test_scheduler_starts_once_when_debug_is_off(monkeypatch, run_calls):
    starts = []
    monkeypatch.setattr(dashboard.scheduler_engine, "start", lambda: starts.append(1))
    dashboard.main()
    assert starts == [1]


def test_scheduler_does_not_start_in_the_reloader_parent(monkeypatch, run_calls):
    """Under debug, the parent only forks. Starting there gives two schedulers.

    The previous guard tested ``app.debug``, which is still False at this point
    because ``app.run`` has not been called yet, so it started the scheduler in
    both processes. The guard now reads the computed debug value.
    """
    monkeypatch.setenv("LINKEDIN_DASHBOARD_DEBUG", "1")
    starts = []
    monkeypatch.setattr(dashboard.scheduler_engine, "start", lambda: starts.append(1))
    dashboard.main()
    assert starts == []


def test_scheduler_starts_in_the_reloader_child(monkeypatch, run_calls):
    monkeypatch.setenv("LINKEDIN_DASHBOARD_DEBUG", "1")
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    starts = []
    monkeypatch.setattr(dashboard.scheduler_engine, "start", lambda: starts.append(1))
    dashboard.main()
    assert starts == [1]


# ─── Error pages carry no traceback, no locals, no secrets ────────────────────

def test_unhandled_error_returns_a_generic_500(monkeypatch, error_client):
    def boom():
        raise RuntimeError("profiles backend exploded")

    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "list_profiles", boom)

    resp = error_client.get("/api/profiles")
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Internal Server Error", "status": 500}


def test_error_page_shows_no_traceback(monkeypatch, error_client):
    def boom():
        raise RuntimeError("profiles backend exploded")

    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "list_profiles", boom)

    body = error_client.get("/api/profiles").get_data(as_text=True)
    for marker in ("Traceback", "RuntimeError", "exploded", "dashboard.py", "File \""):
        assert marker not in body


def test_error_page_shows_no_credential_in_scope(monkeypatch, error_client):
    """A failure with a secret in the message must not render it to the client.

    This is the D1 consequence that outlives debug mode: the Werkzeug debugger
    renders local variables, and the login and provider paths hold credentials.
    """
    secret = "hunter2-not-a-real-password"

    def boom():
        raise RuntimeError(f"failed to authenticate with password={secret}")

    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "list_profiles", boom)

    body = error_client.get("/api/profiles").get_data(as_text=True)
    assert secret not in body
    assert "password" not in body.lower()


def test_unhandled_error_is_logged_server_side(monkeypatch, error_client, caplog):
    """Suppressing the response detail must not suppress the diagnosis."""
    def boom():
        raise RuntimeError("profiles backend exploded")

    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "list_profiles", boom)

    with caplog.at_level("ERROR", logger=dashboard.logger.name):
        error_client.get("/api/profiles")
    assert "/api/profiles" in caplog.text
    assert "exploded" in caplog.text


def test_unknown_route_returns_json_not_an_html_page(error_client):
    resp = error_client.get("/api/there-is-no-such-endpoint")
    assert resp.status_code == 404
    assert resp.get_json()["status"] == 404
