"""The dashboard's selector health endpoints (Phase 11b).

No Selenium and no browser: the job runner is stubbed, so what is asserted is
the request contract and the report the UI renders, not a live check.
"""

import json
import os

import pytest

from linkedin_automation import dashboard as dash
from linkedin_automation import profile_manager as pm
from linkedin_automation import selector_health as shc


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


@pytest.fixture
def started_jobs(monkeypatch):
    """Capture what would have been run instead of launching Chrome."""
    calls = []
    monkeypatch.setattr(dash, "can_start_browser_task", lambda name: True)
    monkeypatch.setattr(dash, "run_job",
                        lambda job_id, fn, *a, **kw: calls.append((job_id, fn, a, kw)))
    return calls


# ─── POST /api/health/<profile>/selectors ─────────────────────────────────────

def test_the_default_check_is_the_feed_and_needs_no_url(api_client, started_jobs):
    resp = api_client.post("/api/health/rick/selectors", json={})
    assert resp.status_code == 200
    assert resp.get_json()["job_id"].startswith("health_feed_rick_")


def test_a_body_free_post_still_works(api_client, started_jobs):
    """The button shipped before Phase 11b sent no body. It must keep working."""
    resp = api_client.post("/api/health/rick/selectors")
    assert resp.status_code == 200
    assert len(started_jobs) == 1


@pytest.mark.parametrize("page", ["post", "search"])
def test_the_other_paths_are_refused_without_a_url(api_client, started_jobs, page):
    """Neither page is reachable from the feed, so running without a URL could
    only ever check the wrong page and report a confident result about it."""
    resp = api_client.post("/api/health/rick/selectors", json={"page": page})
    assert resp.status_code == 400
    assert "url" in resp.get_json()["error"]
    assert started_jobs == []


@pytest.mark.parametrize("bad", ["", "   ", "linkedin.com/feed", 12, None])
def test_a_url_that_is_not_an_http_url_is_refused(api_client, started_jobs, bad):
    resp = api_client.post("/api/health/rick/selectors",
                           json={"page": "post", "url": bad})
    assert resp.status_code == 400
    assert started_jobs == []


def test_an_unknown_page_is_refused_and_names_the_valid_ones(api_client, started_jobs):
    resp = api_client.post("/api/health/rick/selectors", json={"page": "profile"})
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "profile" in error and "feed" in error and "post" in error
    assert started_jobs == []


def test_a_non_object_body_is_refused(api_client, started_jobs):
    resp = api_client.post("/api/health/rick/selectors",
                           json=["page", "post"])
    assert resp.status_code == 400
    assert started_jobs == []


def test_the_post_check_passes_the_url_through_to_the_read_only_flag(
        api_client, started_jobs, monkeypatch, tmp_path):
    url = "https://www.linkedin.com/feed/update/urn:li:activity:0/"
    resp = api_client.post("/api/health/rick/selectors",
                           json={"page": "post", "url": url})
    assert resp.status_code == 200

    captured = {}

    def fake_subprocess(job_id, cmd):
        captured["cmd"] = cmd
        return pm.EXIT_OK, ""

    monkeypatch.setattr(dash, "run_subprocess", fake_subprocess)
    _job_id, fn, args, _kw = started_jobs[0]
    fn("job-1", *args)

    assert "--post-url" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--post-url") + 1] == url
    # It runs the module's read-only post check. Nothing here can open the
    # comment box, and nothing here can post.
    assert "linkedin_automation.selector_health" in captured["cmd"]


def test_a_degraded_run_returns_the_report_rather_than_failing(
        api_client, started_jobs, monkeypatch, tmp_path):
    """DEGRADED exits non-zero on purpose. The endpoint must still surface the
    report: a degraded result is the one most worth reading."""
    report = shc.run_fixture_health_check(
        os.path.join(os.path.dirname(__file__), "fixtures", "feed_degraded.html"))
    with open(tmp_path / shc.HEALTH_RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f)

    api_client.post("/api/health/rick/selectors", json={"page": "feed"})
    monkeypatch.setattr(dash, "run_subprocess", lambda jid, cmd: (pm.EXIT_DEGRADED, ""))
    _job_id, fn, args, _kw = started_jobs[0]

    result = fn("job-1", "rick")
    assert result["status"] == "DEGRADED"
    assert result["failed"] == ["scroll_container"]


def test_a_login_required_exit_is_raised_as_such(api_client, started_jobs, monkeypatch):
    api_client.post("/api/health/rick/selectors", json={"page": "feed"})
    monkeypatch.setattr(dash, "run_subprocess",
                        lambda jid, cmd: (pm.EXIT_LOGIN_REQUIRED, ""))
    _job_id, fn, args, _kw = started_jobs[0]
    with pytest.raises(pm.LoginRequiredError):
        fn("job-1", "rick")


def test_a_busy_profile_is_refused_with_409(api_client, monkeypatch):
    monkeypatch.setattr(dash, "can_start_browser_task", lambda name: False)
    resp = api_client.post("/api/health/rick/selectors", json={"page": "feed"})
    assert resp.status_code == 409


# ─── GET /api/health/<profile>/report ─────────────────────────────────────────

def test_the_report_covers_the_whole_registry_with_nothing_run(api_client):
    report = api_client.get("/api/health/rick/report").get_json()
    keys = [e["key"] for page in report["pages"].values() for e in page["entries"]]
    assert sorted(keys) == sorted(shc.SELECTOR_REGISTRY)
    assert report["overall"] == "INCOMPLETE"
    assert report["complete"] is False


def test_a_saved_feed_run_shows_up_and_still_reads_incomplete(api_client, tmp_path):
    """The assertion this whole phase is for. A green feed run is a green feed
    run, not a green tool."""
    saved = shc.run_fixture_health_check(
        os.path.join(os.path.dirname(__file__), "fixtures", "feed_healthy.html"))
    with open(tmp_path / shc.HEALTH_RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    report = api_client.get("/api/health/rick/report").get_json()
    assert report["pages"]["feed"]["ran"] is True
    assert report["pages"]["feed"]["status"] == "HEALTHY"
    assert report["pages"]["post"]["ran"] is False
    assert report["overall"] == "INCOMPLETE"

    post_entries = {e["key"]: e for e in report["pages"]["post"]["entries"]}
    assert post_entries["post_detail"]["level"] == "amber"
    assert "live LinkedIn session" in post_entries["post_detail"]["reason"]


def test_a_saved_broken_post_run_turns_the_overall_red(api_client, tmp_path):
    saved = shc.run_fixture_health_check(
        os.path.join(os.path.dirname(__file__), "fixtures", "feed_broken.html"))
    with open(tmp_path / shc.HEALTH_RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f)

    report = api_client.get("/api/health/rick/report").get_json()
    assert report["overall"] == "BROKEN"
    entries = {e["key"]: e for e in report["pages"]["feed"]["entries"]}
    assert entries["feed_container"]["level"] == "red"
    assert entries["feed_container"]["fix_symbol"] == "POST_SELECTORS"


def test_an_unreadable_saved_report_does_not_take_the_endpoint_down(api_client, tmp_path):
    (tmp_path / shc.HEALTH_RESULT_FILE).write_text("{ not json", encoding="utf-8")
    report = api_client.get("/api/health/rick/report").get_json()
    assert report["pages"]["feed"]["ran"] is False
    assert report["overall"] == "INCOMPLETE"
