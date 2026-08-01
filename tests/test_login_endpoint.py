"""Phase 5c — signing in is a button, not a terminal command.

Logging in was the only step in the pipeline with no UI, which is why it was
the only step that still sent the operator to a shell. Everything else — scrape,
generate, post, connect, health check, settings — has lived in the dashboard
for months.
"""

from pathlib import Path

import pytest

from linkedin_automation import dashboard as dash
from linkedin_automation import profile_manager as pm


TEMPLATE = (Path(__file__).parent.parent / "linkedin_automation"
            / "templates" / "dashboard.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


@pytest.fixture
def started_jobs(monkeypatch):
    calls = []
    monkeypatch.setattr(dash, "can_start_browser_task", lambda name: True)
    monkeypatch.setattr(dash, "run_job",
                        lambda job_id, fn, *a, **kw: calls.append((job_id, fn, a, kw)))
    return calls


@pytest.fixture
def profile(api_client):
    api_client.post("/api/profiles", json={
        "name": "rick", "username": "a@b.com", "password": "x"})
    return "rick"


# ─── The endpoint ─────────────────────────────────────────────────────────────

def test_login_starts_a_browser_job(api_client, profile, started_jobs):
    resp = api_client.post("/api/profiles/rick/login", json={})
    assert resp.status_code == 200
    assert resp.get_json()["job_id"].startswith("login_rick_")

    _jid, _fn, _args, kwargs = started_jobs[0]
    assert kwargs["task_type"] == "browser"
    assert kwargs["category"] == "login"


def test_an_unknown_profile_is_a_404(api_client, started_jobs):
    resp = api_client.post("/api/profiles/nobody/login", json={})
    assert resp.status_code == 404
    assert started_jobs == []


def test_a_busy_profile_is_refused(api_client, profile, monkeypatch):
    monkeypatch.setattr(dash, "can_start_browser_task", lambda name: False)
    assert api_client.post("/api/profiles/rick/login", json={}).status_code == 409


@pytest.mark.parametrize("body", [["a"], {"wait": "yes"}, {"wait": 1}])
def test_a_malformed_body_is_a_400_not_a_500(api_client, profile, started_jobs, body):
    resp = api_client.post("/api/profiles/rick/login", json=body)
    assert resp.status_code == 400
    assert started_jobs == []


def test_it_runs_the_login_check_script_with_the_profile(api_client, profile,
                                                         started_jobs, monkeypatch):
    api_client.post("/api/profiles/rick/login", json={"wait": True})
    captured = {}

    def fake_subprocess(job_id, cmd):
        captured["cmd"] = cmd
        return pm.EXIT_OK, ""

    monkeypatch.setattr(dash, "run_subprocess", fake_subprocess)
    _jid, fn, args, _kw = started_jobs[0]
    result = fn("job-1", *args)

    assert captured["cmd"][1].endswith("login_check.py")
    assert captured["cmd"][2:] == ["--profile", "rick"]
    assert "--no-wait" not in captured["cmd"]
    assert result["logged_in"] is True
    assert result["status"] == "LOGGED_IN"


def test_wait_false_reports_without_waiting(api_client, profile, started_jobs, monkeypatch):
    api_client.post("/api/profiles/rick/login", json={"wait": False})
    captured = {}
    monkeypatch.setattr(dash, "run_subprocess",
                        lambda jid, cmd: (captured.setdefault("cmd", cmd), (pm.EXIT_OK, ""))[1])
    _jid, fn, args, _kw = started_jobs[0]
    fn("job-1", *args)
    assert "--no-wait" in captured["cmd"]


def test_the_three_exit_codes_are_distinguished(api_client, profile,
                                                started_jobs, monkeypatch):
    """login_check returns 0 signed in, 2 not signed in, 1 broken. Collapsing
    those would tell the operator to retry when the tool is actually failing."""
    expected = {
        pm.EXIT_OK: ("LOGGED_IN", True),
        pm.EXIT_LOGIN_REQUIRED: ("NOT_LOGGED_IN", False),
        pm.EXIT_ERROR: ("ERROR", False),
    }
    for code, (status, logged_in) in expected.items():
        api_client.post("/api/profiles/rick/login", json={})
        monkeypatch.setattr(dash, "run_subprocess", lambda jid, cmd, c=code: (c, ""))
        _jid, fn, args, _kw = started_jobs[-1]
        result = fn("job-1", *args)
        assert result["status"] == status
        assert result["logged_in"] is logged_in


def test_the_script_path_actually_exists():
    """A path built from __file__ is exactly the kind of thing that breaks when
    a module moves, and it would break at click time, not import time."""
    assert Path(dash.LOGIN_CHECK_SCRIPT).is_file()


# ─── The app stops sending people to a terminal ───────────────────────────────

def test_login_required_messages_name_the_button(api_client, profile):
    """Four messages told the operator to run a shell command for something the
    app can now do. An app that refers you to a terminal has a gap, not a UI."""
    source = (Path(__file__).parent.parent / "linkedin_automation"
              / "dashboard.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    messages = [i for i, line in enumerate(lines) if "Login required" in line]
    assert messages, "the login-required messages have moved or been renamed"
    for i in messages:
        window = "\n".join(lines[i:i + 3])
        assert "Log in" in window and "dashboard header" in window, \
            f"dashboard.py:{i + 1} offers only the terminal command"


def test_the_button_is_in_the_header():
    assert 'id="loginBtn"' in TEMPLATE
    assert "startLogin()" in TEMPLATE
    header = TEMPLATE.split("<header>")[1].split("</header>")[0]
    assert 'id="loginBtn"' in header, "the login button is not in the header"


def test_the_button_confirms_before_opening_a_browser():
    body = TEMPLATE.split("async function startLogin()")[1].split("\nfunction ")[0]
    assert "await askConfirm(" in body
    assert "if (!ok) return;" in body


def test_the_confirmation_says_where_the_password_goes():
    """The operator should not have to wonder whether this app is about to type
    their LinkedIn password for them. It never does."""
    body = TEMPLATE.split("async function startLogin()")[1].split("\nfunction ")[0]
    assert "never by this app" in body or "never through this app" in body
