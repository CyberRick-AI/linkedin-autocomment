"""The Settings surfaces must not fail silently.

Ported from the beta branch's Phase 5b suite, keeping the parts that apply to
what was ported here (the Settings tab). They share one shape: a control
declines to act and says nothing, so the operator cannot tell a refusal from a
hang from a success.

**Scope, deliberately.** Beta converted every ``alert()`` / ``confirm()`` in the
dashboard. This codebase's other tabs still use the native dialogs, and
converting them is a separate change, so the "no native dialogs anywhere" gate
is scoped here to the Settings section of the script — the part that has a
control which spends money (Test Connection) and one that destroys a stored key
(Forget). An embedded browser — the macOS app's WKWebView included — suppresses
``confirm()`` and hands the page ``false``, which would turn both into dead
buttons.
"""

import re
from pathlib import Path

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import providers


TEMPLATE = (Path(__file__).parent.parent / "linkedin_automation"
            / "templates" / "dashboard.html").read_text(encoding="utf-8")

# Real calls always carry an argument. The prose in the comments writes bare
# `alert()` / `confirm()` when naming what was removed, and that is not a call.
NATIVE_DIALOG_CALL = re.compile(r"(?<![.\w])(alert|confirm)\(\s*[^)\s]")


def _settings_script():
    """The Settings tab's JavaScript, from its banner to the next section."""
    start = TEMPLATE.index("// ─── Settings: provider + API keys")
    return TEMPLATE[start:TEMPLATE.index("// ─── Mode Switching", start)]


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


# ─── No native dialogs in the Settings tab ────────────────────────────────────

def test_the_settings_tab_calls_no_native_dialogs():
    offenders = [m.group(0) for m in NATIVE_DIALOG_CALL.finditer(_settings_script())]
    assert offenders == []


def test_the_replacements_are_actually_present():
    """Zero native calls is also what deleting every message would achieve."""
    assert "function showToast(" in TEMPLATE
    assert "function askConfirm(" in TEMPLATE
    assert 'id="toastWrap"' in TEMPLATE
    assert 'id="confirmModal"' in TEMPLATE


def test_every_guard_awaits_its_confirmation():
    """askConfirm returns a Promise. Used without await it is always truthy,
    which would turn each guard into an unconditional proceed — worse than the
    silent refusal it replaced, because one of these spends money."""
    for match in re.finditer(r".*askConfirm\(.*", TEMPLATE):
        line = match.group(0)
        if "function askConfirm" in line:
            continue
        assert "await askConfirm(" in line, line.strip()


def test_the_guarded_settings_actions_are_still_guarded():
    guarded = re.findall(r"await askConfirm\(\s*\n?\s*`?([^`\n]{0,60})", TEMPLATE)
    joined = " ".join(guarded).lower()
    assert "send one small test prompt" in joined  # spends money
    assert "forget the stored" in joined           # destroys a stored key


def test_a_dismissed_confirmation_resolves_false():
    """Escape, Cancel and a backdrop click must all mean no. A confirmation
    that defaults to yes when unanswered is not a guard."""
    body = TEMPLATE.split("function askConfirm(")[1].split("\n// ")[0]
    assert "settle(false)" in body
    assert "e.key === 'Escape'" in body
    assert "cancelBtn.onclick = () => settle(false)" in body
    # And exactly one path says yes.
    assert body.count("settle(true)") == 2  # the OK button and Enter


def test_settings_failures_are_styled_as_failures():
    """A toast that looks like every other toast is a message nobody reads."""
    assert "showToast('Paste a key first', 'error')" in TEMPLATE
    assert ".toast.error" in TEMPLATE


def test_vendor_text_is_escaped_before_it_reaches_the_page():
    """Test Connection renders the vendor's reply and error text. Both come
    from a third party, so they go through esc() rather than into innerHTML raw."""
    script = _settings_script()
    assert "${esc(r.error)}" in script
    assert "${esc(r.text)}" in script
    assert "${r.error}" not in script
    assert "${r.text}" not in script


# ─── The key field names the provider it will save to ─────────────────────────

def test_the_key_field_label_is_driven_by_the_dropdown():
    assert 'id="setApiKeyLabel"' in TEMPLATE
    assert "Paste a key for ${(p && p.label) || name}" in TEMPLATE


def test_the_key_list_says_it_is_status_only():
    """It reads as a chooser and is not. A key pasted while the dropdown still
    said OpenAI is stored as the OpenAI key."""
    assert "status only" in TEMPLATE


def test_every_provider_has_a_label_to_show(api_client):
    """The label falls back to the raw name, but a blank one would render
    'Paste a key for '. Assert every shipped provider actually has one."""
    rows = api_client.get("/api/settings/providers").get_json()
    rows = rows["providers"] if isinstance(rows, dict) else rows
    assert len(rows) == len(providers.SPECS)
    for row in rows:
        assert row.get("label"), row.get("name")


# ─── A placeholder key must not read as configured ────────────────────────────

def test_the_env_template_ships_no_placeholder_key():
    """A copied .env reported OpenAI as `set` on first boot, which suppresses
    the actionable missing-key error and turns it into a 401 mid-generation.

    Scoped to the generation-provider keys (the ones the Settings tab reports
    on). BUFFER_API_KEY is also blank in the template, but it is not this
    test's concern."""
    template = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")
    provider_vars = {env for env in providers.API_KEY_ENV.values() if env}
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() in provider_vars:
            assert value.strip() == "", line


def test_the_xai_default_model_is_marked_verified():
    """Confirmed live 2026-08-01: grok-4 answered, temperature accepted."""
    assert providers.SPECS["xai"].default_model == "grok-4"
    assert providers.SPECS["xai"].default_model_verified is True


def test_an_unverified_default_still_says_so():
    """The flag must stay meaningful. If everything were marked verified the
    UI's warning would be decoration."""
    unverified = [s.name for s in providers.SPECS.values()
                  if s.default_model and not s.default_model_verified]
    assert unverified, "no spec is unverified any more; the warning is now dead UI"
