"""Phase 8d — the generator must not demand OpenAI's key for a non-OpenAI provider.

Found on Rick's first Generate press. The generator carried a hardcoded
``OPENAI_API_KEY`` check that predated the provider layer, and it was invisible
because ``.env`` shipped a placeholder that satisfied ``os.getenv``. Phase 5b
cleared that placeholder — correctly, it made a missing key look configured —
and in doing so exposed a gate that refused to run a profile configured for xAI
while naming the wrong vendor's variable.

Then it printed the error and returned 0, so the dashboard's return-code check
passed and the operator was shown "No comments file found": a downstream
symptom in place of the cause.
"""

import re
from pathlib import Path

import pytest

from linkedin_automation import comment_generator as cg
from linkedin_automation import providers


REPO = Path(__file__).parent.parent


# ─── No module demands a specific vendor's key ────────────────────────────────

def test_no_module_hardcodes_an_openai_key_check():
    """The provider is per-profile configuration. Demanding OpenAI's variable
    is wrong for every profile that is not using OpenAI."""
    offenders = []
    for path in sorted(REPO.glob("linkedin_automation/*.py")) + sorted(REPO.glob("tools/*.py")):
        if path.name == "providers.py":
            continue          # the one place that legitimately names key variables
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"getenv\(['\"]OPENAI_API_KEY['\"]\)", source):
            offenders.append(f"{path.relative_to(REPO)}:{source[:match.start()].count(chr(10)) + 1}")
    assert offenders == [], f"hardcoded OpenAI key checks: {offenders}"


def test_the_provider_layer_names_the_right_variable():
    """What replaces the removed gate. The error has to name the provider that
    is actually selected, not the one that used to be hardcoded."""
    import os

    saved = {k: os.environ.pop(k, None) for k in ("XAI_API_KEY", "OPENAI_API_KEY")}
    try:
        with pytest.raises(providers.ProviderError) as excinfo:
            providers.get_provider("xai")
        message = str(excinfo.value)
        assert "XAI_API_KEY" in message
        assert "OPENAI_API_KEY" not in message
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


# ─── A failure exits non-zero ─────────────────────────────────────────────────

def test_the_generator_exits_non_zero_on_a_provider_failure(comments_dir, monkeypatch):
    """Returning 0 after printing an error told the dashboard the run had
    succeeded, which is why the real cause never reached the screen.

    In-process rather than a subprocess (as the beta version was): a real
    ``python -m`` child would load the developer's ``.env`` and, resolving a
    key, read the developer's real OS credential store — exactly what the
    autouse fake keyring exists to prevent. ``main`` returns the exit code and
    ``__main__`` hands it to ``sys.exit``, so asserting the return is the same
    contract (and ``test_main_returns_an_error_code_rather_than_none`` pins the
    ``sys.exit(main())`` wiring).
    """
    from linkedin_automation import profile_manager as pm

    monkeypatch.setattr(pm, "get_profile_config",
                        lambda profile_name=None: {"provider": {"name": "xai"}})
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    code = cg.main([str(comments_dir / "missing.json"), "--platform", "linkedin",
                    "--profile", "someone"])
    assert code == cg.EXIT_ERROR


def test_an_empty_run_is_still_a_success(comments_dir, monkeypatch):
    """Adapted for this codebase: every post being turned down is a normal
    outcome here (generate_all_comments logs and records it), so it keeps
    exiting 0 as it always has. Only a failure exits non-zero."""
    monkeypatch.setattr(cg.AuthenticCommentGenerator, "generate_all_comments",
                        lambda self: [])
    code = cg.main([str(comments_dir / "in.json"), "--platform", "linkedin",
                    "--profile", "someone"])
    assert code == cg.EXIT_OK


def test_main_returns_an_error_code_rather_than_none():
    """`return` with no value is 0 to sys.exit, which is how this hid."""
    source = (REPO / "linkedin_automation" / "comment_generator.py").read_text(encoding="utf-8")
    main_body = source.split("def main(")[1]
    assert "sys.exit(main())" in source, "main's return value must reach the exit code"
    assert "return EXIT_ERROR" in main_body
    assert "return EXIT_OK" in main_body


def test_the_exit_codes_survive_a_missing_profile_manager():
    """pm is an optional import in this module, so reading pm.EXIT_* directly
    on the error path would raise NameError in the degraded mode."""
    assert cg.EXIT_OK == 0
    assert cg.EXIT_ERROR == 1


# ─── The dashboard stops replacing the cause with a symptom ───────────────────

def test_the_no_file_error_points_at_the_log():
    source = (REPO / "linkedin_automation" / "dashboard.py").read_text(encoding="utf-8")
    # Scoped to the generate job. The same words appear in the *post* endpoint,
    # where "no comments file to post from" really is the cause.
    generate_job = source.split("def do_generate(")[1].split("\n    run_job(")[0]
    assert 'RuntimeError("No comments file found")' not in generate_job, (
        "the bare message is back; there it reads as the cause and is only ever "
        "a symptom of whatever the generator actually hit")
    assert "job log" in generate_job
