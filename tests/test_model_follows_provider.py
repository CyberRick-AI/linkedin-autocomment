"""Phase 8c — one place chooses the provider, and the panels report it.

Phase 8 shipped the Settings screen but left three older model dropdowns in
place, each listing OpenAI models only. They won: the dashboard always sent a
model, and the generator treats an explicit ``--model`` as an override of the
profile config. So a profile configured for xAI called xAI's endpoint asking
for ``gpt-4o-mini``, and failed with a vendor error that read as "xAI is
broken".

Phase 8's own criterion said switching provider takes effect on the next
generation run. The provider did. The model did not.
"""

import re
from pathlib import Path

import pytest

from linkedin_automation import dashboard as dash
from linkedin_automation import profile_manager as pm


REPO = Path(__file__).parent.parent
TEMPLATE = REPO / "linkedin_automation" / "templates" / "dashboard.html"
DASHBOARD_PY = REPO / "linkedin_automation" / "dashboard.py"


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


# ─── No panel offers a competing model list ───────────────────────────────────

def test_no_generation_panel_has_its_own_model_dropdown():
    """The regression guard. Re-adding one of these silently overrides Settings
    for every run, and the symptom appears at the vendor, not here."""
    html = TEMPLATE.read_text(encoding="utf-8")
    stale = [sid for sid in ("commentModel", "posterModel", "articleModel")
             if re.search(rf'<select[^>]*id="{sid}"', html)]
    assert stale == [], f"model dropdowns competing with Settings: {stale}"


def test_the_panels_report_the_configured_provider_instead():
    """Removing the dropdowns must not leave the operator guessing what will run."""
    html = TEMPLATE.read_text(encoding="utf-8")
    for element in ("commentModelInfo", "posterModelInfo", "articleModelInfo"):
        assert f'id="{element}"' in html, element
    assert "function renderActiveModel(" in html
    assert "refreshActiveModels()" in html


def test_no_panel_sends_a_model_with_its_generate_request():
    """Sending any model at all is the bug; the value does not matter."""
    html = TEMPLATE.read_text(encoding="utf-8")
    for sid in ("commentModel", "posterModel", "articleModel"):
        assert f"getElementById('{sid}')" not in html, sid


# ─── The endpoints stop defaulting to an OpenAI model ─────────────────────────

def test_no_endpoint_defaults_to_an_openai_model():
    source = DASHBOARD_PY.read_text(encoding="utf-8")
    offenders = re.findall(r"body\.get\(['\"]model['\"],\s*['\"][^'\"]+['\"]\)", source)
    assert offenders == [], f"endpoints still defaulting a model: {offenders}"


def test_generate_omits_the_model_flag_when_none_is_given(api_client, monkeypatch, tmp_path):
    """`--model None` would crash the subprocess build; a default would override
    Settings. The flag has to be absent."""
    from linkedin_automation import post_store

    calls = []
    monkeypatch.setattr(dash, "can_start_browser_task", lambda name: True)
    monkeypatch.setattr(dash, "run_job",
                        lambda job_id, fn, *a, **kw: calls.append((job_id, fn, a, kw)))
    monkeypatch.setattr(dash, "_write_lifecycle_input",
                        lambda pname, posts: (str(tmp_path / "in.json"), 1))
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda pname: type("S", (), {
                            "get_posts_by_status": lambda self, s: [{}],
                            "counts": lambda self: {}})())

    api_client.post("/api/profiles", json={"name": "rick", "username": "a@b.com",
                                           "password": "x"})
    resp = api_client.post("/api/comments/rick/generate", json={})
    assert resp.status_code == 200

    captured = {}
    monkeypatch.setattr(dash, "run_subprocess",
                        lambda jid, cmd: (captured.setdefault("cmd", cmd), (0, ""))[1])
    monkeypatch.setattr(dash, "log_job", lambda *a, **k: None)
    _jid, fn, args, _kw = calls[0]
    try:
        fn("job-1", *args)
    except Exception:
        pass  # the job does more than run the subprocess; the cmd is what matters

    assert "--model" not in captured["cmd"]
    assert "--profile" in captured["cmd"]


def test_an_explicit_model_override_still_works(api_client, monkeypatch, tmp_path):
    """The override is a real capability, just no longer the default."""
    from linkedin_automation import post_store

    calls = []
    monkeypatch.setattr(dash, "can_start_browser_task", lambda name: True)
    monkeypatch.setattr(dash, "run_job",
                        lambda job_id, fn, *a, **kw: calls.append((job_id, fn, a, kw)))
    monkeypatch.setattr(dash, "_write_lifecycle_input",
                        lambda pname, posts: (str(tmp_path / "in.json"), 1))
    monkeypatch.setattr(post_store, "load_synced_store",
                        lambda pname: type("S", (), {
                            "get_posts_by_status": lambda self, s: [{}],
                            "counts": lambda self: {}})())

    api_client.post("/api/profiles", json={"name": "rick", "username": "a@b.com",
                                           "password": "x"})
    api_client.post("/api/comments/rick/generate", json={"model": "grok-4-fast"})

    captured = {}
    monkeypatch.setattr(dash, "run_subprocess",
                        lambda jid, cmd: (captured.setdefault("cmd", cmd), (0, ""))[1])
    monkeypatch.setattr(dash, "log_job", lambda *a, **k: None)
    _jid, fn, args, _kw = calls[0]
    try:
        fn("job-1", *args)
    except Exception:
        pass

    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "grok-4-fast"


# ─── The provider endpoint carries what the panels need ───────────────────────

def test_the_provider_endpoint_reports_label_and_locality(api_client):
    api_client.post("/api/profiles", json={"name": "rick", "username": "a@b.com",
                                           "password": "x"})
    data = api_client.get("/api/profiles/rick/provider").get_json()
    assert data["label"], "the panels render the label, not the raw name"
    assert data["local"] is False
    assert "key" in data


def test_a_local_provider_is_not_warned_about_a_missing_key(api_client):
    """Warning that Ollama has no key would be wrong, not merely noisy."""
    api_client.post("/api/profiles", json={"name": "rick", "username": "a@b.com",
                                           "password": "x"})
    api_client.post("/api/profiles/rick/provider",
                    json={"provider": "ollama", "model": "llama3"})
    data = api_client.get("/api/profiles/rick/provider").get_json()
    assert data["local"] is True


# ─── The key list follows the selection ───────────────────────────────────────

def test_the_key_list_is_rendered_from_its_own_function():
    """It renders at load AND on every provider change. At load the dropdown
    still holds the first option rather than the profile's provider, so a list
    keyed on the selection showed the wrong row — the same ordering trap Phase 8
    hit with the model hint."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "function renderKeyList()" in html
    assert html.count("renderKeyList()") >= 3   # definition, load, provider change

    picked = html.split("function onProviderPicked()")[1].split("\nasync function")[0]
    assert "renderKeyList()" in picked, "the key list does not follow the dropdown"


def test_the_key_list_collapses_the_unconfigured_providers():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "<details" in html.split("function renderKeyList()")[1][:2000]
    assert "with no key" in html
