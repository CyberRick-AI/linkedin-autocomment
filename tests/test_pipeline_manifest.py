"""Tests for the pipeline manifest (phase-1).

Offline: the manifest is declarative data, so nothing here imports a driver,
touches the network, or runs a step."""

import importlib

import pytest

from linkedin_automation import pipeline_manifest as pm


def test_manifest_is_structurally_sound():
    """Unique names, dependency references resolve, bounds are not inverted."""
    assert pm.validate() == []


def test_the_four_existing_steps_are_declared():
    assert set(pm.by_name()) == {
        "scrape_feed",
        "generate_comments",
        "post_comments",
        "selector_health",
    }


@pytest.mark.parametrize("step", pm.STEPS, ids=lambda s: s.name)
def test_every_declared_module_actually_exists(step):
    """A manifest entry pointing at a module that is not there is drift."""
    importlib.import_module(step.module)


@pytest.mark.parametrize("step", pm.STEPS, ids=lambda s: s.name)
def test_every_step_documents_how_it_fails(step):
    """A step with no known failure modes has not been thought about."""
    assert step.known_failure_modes, f"{step.name} declares no failure modes"


# ─── the boundary, made machine checkable ────────────────────────────────────

def test_linkedin_driving_steps_are_flagged():
    """The three steps that touch a live session must be marked.

    This is the operating boundary in executable form: work that needs a real
    LinkedIn account cannot be verified by an automated runner, so it belongs
    to the human.
    """
    flagged = {s.name for s in pm.session_required_steps()}
    assert flagged == {"scrape_feed", "post_comments", "selector_health"}


def test_generation_is_the_only_offline_step():
    assert [s.name for s in pm.offline_steps()] == ["generate_comments"]


def test_session_and_offline_partition_the_manifest():
    assert len(pm.session_required_steps()) + len(pm.offline_steps()) == len(pm.STEPS)


# ─── dependency graph ────────────────────────────────────────────────────────

def test_pipeline_order_is_declared_both_ways():
    scrape = pm.get("scrape_feed")
    generate = pm.get("generate_comments")
    post = pm.get("post_comments")

    assert generate.name in scrape.downstream
    assert scrape.name in generate.upstream
    assert post.name in generate.downstream
    assert generate.name in post.upstream


def test_selector_health_is_independent():
    """It detects other steps' failures; nothing feeds it and it feeds nothing."""
    health = pm.get("selector_health")
    assert health.upstream == []
    assert health.downstream == []


def test_get_returns_none_for_an_undeclared_step():
    assert pm.get("does_not_exist") is None


# ─── validate() actually catches things ──────────────────────────────────────

def test_validate_catches_a_dangling_upstream(monkeypatch):
    broken = pm.PipelineStep(
        name="orphan",
        module="linkedin_automation.post_store",
        entry_function="main",
        target=None,
        data_source="test",
        requires_linkedin_session=False,
        upstream=["no_such_step"],
    )
    monkeypatch.setattr(pm, "STEPS", pm.STEPS + [broken])
    problems = pm.validate()
    assert any("unknown upstream" in p for p in problems)


def test_validate_catches_a_one_sided_dependency(monkeypatch):
    """Declaring an upstream without the matching downstream is a real bug."""
    lopsided = pm.PipelineStep(
        name="lopsided",
        module="linkedin_automation.post_store",
        entry_function="main",
        target=None,
        data_source="test",
        requires_linkedin_session=False,
        upstream=["scrape_feed"],  # scrape_feed does not list it downstream
    )
    monkeypatch.setattr(pm, "STEPS", pm.STEPS + [lopsided])
    problems = pm.validate()
    assert any("does not list it downstream" in p for p in problems)


def test_validate_catches_duplicate_names(monkeypatch):
    dupe = pm.get("scrape_feed")
    monkeypatch.setattr(pm, "STEPS", pm.STEPS + [dupe])
    assert any("duplicate step name" in p for p in pm.validate())


def test_validate_catches_inverted_bounds(monkeypatch):
    inverted = pm.PipelineStep(
        name="inverted",
        module="linkedin_automation.post_store",
        entry_function="main",
        target=None,
        data_source="test",
        requires_linkedin_session=False,
        expected_min=50,
        expected_max=5,
    )
    monkeypatch.setattr(pm, "STEPS", pm.STEPS + [inverted])
    assert any("exceeds" in p for p in pm.validate())
