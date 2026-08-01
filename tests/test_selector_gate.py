"""The offline selector gate, the report schema, and the probe (Phase 11b).

Everything here runs with no browser, no network, and no LinkedIn session. That
is the point. Before this, the only way to learn whether a selector still
matched was to run the tool against the live site, which meant only Rick could
ever find out, and only after a run had already failed.
"""

import json
from pathlib import Path

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import selector_health as shc
from tools import selector_probe


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return str(FIXTURES / name)


# ─── The gate: a saved page decides the exit code ─────────────────────────────

@pytest.mark.parametrize("name,page,status,code", [
    ("feed_healthy.html", "feed", "HEALTHY", pm.EXIT_OK),
    ("feed_degraded.html", "feed", "DEGRADED", pm.EXIT_DEGRADED),
    ("feed_broken.html", "feed", "BROKEN", pm.EXIT_ERROR),
    ("post_healthy.html", "post", "HEALTHY", pm.EXIT_OK),
])
def test_fixture_run_reports_status_and_exit_code(name, page, status, code, capsys):
    result = shc.run_fixture_health_check(fixture(name), page=page)
    assert result["status"] == status
    assert result["source"] == "fixture"
    assert result["page"] == page

    assert shc.main(["--fixture", fixture(name), "--page", page]) == code
    assert status in capsys.readouterr().out


def test_the_broken_fixture_names_the_one_selector_that_broke():
    """A status alone is not actionable. The report has to say which hook went."""
    result = shc.run_fixture_health_check(fixture("feed_broken.html"), page="feed")
    assert result["failed"] == ["feed_container"]
    assert result["checks"]["feed_container"]["count"] == 0
    # Everything else still passes: one renamed hook is enough to break a run.
    assert result["checks"]["post_text"]["ok"] is True
    assert result["checks"]["author"]["ok"] is True


def test_degraded_is_non_zero_but_distinguishable_from_broken():
    """A watchdog that only trips on total breakage cannot drive a scheduled
    check, and one that returns the same code for both cannot be triaged."""
    assert pm.EXIT_DEGRADED != pm.EXIT_OK
    assert pm.EXIT_DEGRADED != pm.EXIT_ERROR
    assert pm.EXIT_DEGRADED != pm.EXIT_LOGIN_REQUIRED
    assert shc.exit_code_for("HEALTHY") == pm.EXIT_OK
    assert shc.exit_code_for("DEGRADED") == pm.EXIT_DEGRADED
    assert shc.exit_code_for("BROKEN") == pm.EXIT_ERROR


def test_a_fixture_run_never_reaches_a_profile_or_a_browser(monkeypatch):
    """The gate has to work on a machine that has never had a LinkedIn session,
    or it cannot run in CI, which is the only place it runs unattended."""
    def explode(*a, **k):  # pragma: no cover - the assertion is that it is never hit
        raise AssertionError("a fixture run touched the browser/profile layer")

    monkeypatch.setattr(pm, "create_driver", explode)
    monkeypatch.setattr(pm, "auto_migrate_from_env", explode)
    assert shc.main(["--fixture", fixture("feed_healthy.html")]) == pm.EXIT_OK


def test_gated_entries_are_reported_not_checked_never_counted_as_zero():
    """The post page loads with the comment box closed, so the editor and the
    submit button do not exist. Counting a zero there and calling it a failure
    trains the reader to ignore reds; calling it a pass is the AUDIT G3 lie."""
    result = shc.run_fixture_health_check(fixture("post_healthy.html"), page="post")
    assert "post_comment_input" not in result["checks"]
    assert "post_submit_button" not in result["checks"]
    assert result["status"] == "HEALTHY"

    full = shc.registry_for_page("post", include_gated=True)
    assert set(full) - set(result["checks"]) == {"post_comment_input", "post_submit_button"}


def test_a_capture_with_the_box_open_can_gate_on_the_gated_selectors():
    """--include-gated is how the posting path gets a real gate.

    Everything up to the comment button can be checked on a page load. The
    editor and the submit button cannot, and those two are the ones that decide
    whether a comment is ever posted. A capture taken with the box open is the
    only artifact that can cover them offline, so it gets its own gate.
    """
    result = shc.run_fixture_health_check(fixture("post_box_open.html"),
                                          page="post", include_gated=True)
    assert result["status"] == "HEALTHY"
    assert set(result["checks"]) == set(shc.registry_for_page("post", include_gated=True))
    assert shc.validate_report(result) == []

    assert shc.main(["--fixture", fixture("post_box_open.html"),
                     "--page", "post", "--include-gated"]) == pm.EXIT_OK


def test_the_gated_run_counts_the_submit_button_as_xpath_not_as_css():
    """The one selector in the registry a CSS engine cannot answer.

    Counting //button[normalize-space(.)='Comment'] with a CSS counter does not
    error, it just finds nothing, and the check would then report zero for the
    button that decides whether a comment is posted at all.
    """
    result = shc.run_fixture_health_check(fixture("post_box_open.html"),
                                          page="post", include_gated=True)
    submit = result["checks"]["post_submit_button"]
    assert submit["ok"] is True
    assert submit["count"] == 1

    # And the closed-box capture is the honest zero: the button really is absent
    # there, which is why the ungated run reports it as not checked instead.
    closed = shc.run_fixture_health_check(fixture("post_healthy.html"),
                                          page="post", include_gated=True)
    assert closed["checks"]["post_submit_button"]["count"] == 0
    assert closed["status"] == "BROKEN"


def test_a_selector_the_engine_cannot_parse_fails_the_run_rather_than_scoring_zero(monkeypatch):
    """check_registry turns most counting errors into 0, which is right for a
    live driver rejecting a selector. An unparseable selector is different: a 0
    there is a measurement nobody took."""
    from linkedin_automation import dom_probe

    registry = {"made_up": {"selectors": ["div:nth-child(2)"], "min_expected": 1,
                            "critical": True, "page": "feed", "fix_symbol": "nowhere"}}
    monkeypatch.setattr(shc, "SELECTOR_REGISTRY", registry)
    with pytest.raises(dom_probe.UnsupportedSelector):
        shc.run_fixture_health_check(fixture("feed_healthy.html"), page="feed")


def test_an_unknown_page_is_rejected_rather_than_silently_checking_the_feed():
    with pytest.raises(ValueError) as excinfo:
        shc.run_fixture_health_check(fixture("feed_healthy.html"), page="nonsense")
    assert "nonsense" in str(excinfo.value)


# ─── The JSON report schema ───────────────────────────────────────────────────

def test_a_real_report_validates(capsys):
    for name, page in [("feed_healthy.html", "feed"), ("feed_broken.html", "feed"),
                       ("post_healthy.html", "post")]:
        result = shc.run_fixture_health_check(fixture(name), page=page)
        assert shc.validate_report(result) == [], name

    # And the same object survives the JSON round trip the dashboard reads it
    # through, which is where a non-serialisable value would surface.
    shc.main(["--fixture", fixture("feed_healthy.html"), "--json"])
    parsed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert shc.validate_report(parsed) == []


def test_the_schema_catches_a_missing_field():
    result = shc.run_fixture_health_check(fixture("feed_healthy.html"))
    del result["timestamp"]
    assert any("timestamp" in e for e in shc.validate_report(result))


def test_the_schema_catches_a_status_that_is_not_one_of_the_three():
    result = shc.run_fixture_health_check(fixture("feed_healthy.html"))
    result["status"] = "PROBABLY FINE"
    assert any("status" in e for e in shc.validate_report(result))


def test_the_schema_catches_a_failed_list_that_disagrees_with_the_checks():
    """The field most likely to drift, because it is derived. A report claiming
    HEALTHY while a check is not ok is worse than no report."""
    result = shc.run_fixture_health_check(fixture("feed_broken.html"))
    result["failed"] = []
    errors = shc.validate_report(result)
    assert any("failed" in e and "feed_container" in e for e in errors)


def test_the_schema_rejects_a_non_object():
    assert shc.validate_report(["not", "a", "report"]) == ["report is not an object"]


# ─── The whole-registry report the dashboard renders ──────────────────────────

def test_a_feed_only_run_never_presents_itself_as_a_clean_bill_of_health():
    """The single most important assertion in this phase.

    On 2026-07-31 a run placed zero of three comments and the health check said
    HEALTHY minutes later, because it had only ever looked at the feed. A report
    covering part of the registry must say so in its top-line status.
    """
    feed = shc.run_fixture_health_check(fixture("feed_healthy.html"), page="feed")
    report = shc.build_health_report({"feed": feed, "search": None, "post": None})

    assert report["overall"] == "INCOMPLETE"
    assert report["complete"] is False
    assert report["pages"]["feed"]["ran"] is True
    assert report["pages"]["post"]["ran"] is False


def test_every_registry_entry_appears_exactly_once_across_the_pages():
    report = shc.build_health_report({"feed": None, "search": None, "post": None})
    keys = [entry["key"] for page in report["pages"].values() for entry in page["entries"]]
    assert sorted(keys) == sorted(shc.SELECTOR_REGISTRY)
    assert len(keys) == len(set(keys))


def test_an_unrun_page_says_it_needs_a_live_session():
    report = shc.build_health_report({"feed": None, "search": None, "post": None})
    post_entries = {e["key"]: e for e in report["pages"]["post"]["entries"]}
    assert post_entries["post_detail"]["status"] == "not_checked"
    assert "live LinkedIn session" in post_entries["post_detail"]["reason"]
    # A gated entry states its own, more specific reason instead.
    assert "comment box is opened" in post_entries["post_comment_input"]["reason"]


def test_levels_map_onto_green_amber_red():
    broken = shc.run_fixture_health_check(fixture("feed_broken.html"), page="feed")
    degraded = shc.run_fixture_health_check(fixture("feed_degraded.html"), page="feed")

    red = {e["key"]: e for e in
           shc.build_health_report({"feed": broken})["pages"]["feed"]["entries"]}
    assert red["feed_container"]["level"] == "red"
    assert red["post_text"]["level"] == "green"
    assert red["copy_link_item"]["level"] == "amber"      # not checked

    amber = {e["key"]: e for e in
             shc.build_health_report({"feed": degraded})["pages"]["feed"]["entries"]}
    assert amber["scroll_container"]["level"] == "amber"  # failed, non-critical


def test_overall_prefers_broken_over_degraded_over_incomplete():
    broken = shc.run_fixture_health_check(fixture("feed_broken.html"), page="feed")
    degraded = shc.run_fixture_health_check(fixture("feed_degraded.html"), page="feed")
    post = shc.run_fixture_health_check(fixture("post_healthy.html"), page="post")

    assert shc.build_health_report({"feed": broken})["overall"] == "BROKEN"
    assert shc.build_health_report({"feed": degraded})["overall"] == "DEGRADED"
    assert shc.build_health_report({"feed": broken, "post": degraded})["overall"] == "BROKEN"
    assert shc.build_health_report({"post": post})["overall"] == "INCOMPLETE"


def test_a_report_carries_the_fix_symbol_for_every_entry():
    """The report is read by whoever has to repair it, so it names the constant
    to edit rather than leaving them to grep for the selector."""
    report = shc.build_health_report({})
    for page in report["pages"].values():
        for entry in page["entries"]:
            assert entry["fix_symbol"], entry["key"]


# ─── The probe ────────────────────────────────────────────────────────────────

def test_the_probe_counts_every_selector_including_the_gated_ones():
    probe = selector_probe.probe_html(
        Path(fixture("post_box_open.html")).read_text(encoding="utf-8"), page="post")
    rows = {row["key"]: row for row in probe["rows"]}
    assert set(rows) == set(shc.registry_for_page("post", include_gated=True))
    # Every selector gets its own count, not just the winning one: that is what
    # turns "the posting path is broken" into "these two of six stopped matching".
    assert set(rows["post_detail"]["counts"]) == set(
        shc.SELECTOR_REGISTRY["post_detail"]["selectors"])


def test_the_probe_separates_the_submit_button_from_the_button_that_opens_the_box():
    """The 2026-07-31 fix, asserted end to end.

    Both buttons involve the word Comment. The submit selector is XPath on
    visible text precisely so these do not collide, and the probe has to
    evaluate it as XPath or it returns a confident zero for the one selector
    that decides whether a comment is ever posted.
    """
    probe = selector_probe.probe_html(
        Path(fixture("post_box_open.html")).read_text(encoding="utf-8"), page="post")
    rows = {row["key"]: row for row in probe["rows"]}

    submit = rows["post_submit_button"]
    assert submit["xpath"] is True
    assert submit["count"] == 1
    assert rows["post_comment_button"]["counts"]["button[aria-label*='Comment']"] == 1


def test_the_probe_skips_rather_than_fails_a_gated_selector_on_a_closed_page():
    probe = selector_probe.probe_html(
        Path(fixture("post_healthy.html")).read_text(encoding="utf-8"), page="post")
    rows = {row["key"]: row for row in probe["rows"]}
    assert rows["post_submit_button"]["count"] == 0
    assert rows["post_submit_button"]["gated"] is True
    assert selector_probe.status_of(probe) == "HEALTHY"


def test_the_probe_reports_the_status_the_gate_would():
    for name, page, status in [("feed_healthy.html", "feed", "HEALTHY"),
                               ("feed_degraded.html", "feed", "DEGRADED"),
                               ("feed_broken.html", "feed", "BROKEN")]:
        probe = selector_probe.probe_html(
            Path(fixture(name)).read_text(encoding="utf-8"), page=page)
        assert selector_probe.status_of(probe) == status, name


def test_the_probe_lists_candidate_hooks_from_the_page_it_just_read():
    """The other half of the repair loop: not only what stopped matching, but
    what this page actually carries to match on instead."""
    probe = selector_probe.probe_html(
        Path(fixture("feed_healthy.html")).read_text(encoding="utf-8"), page="feed")
    assert "[data-testid='mainFeed']" in probe["candidates"]
    assert "feed-actor-image" in probe["hooks"]["data_view_names"]


def test_an_unsupported_selector_is_shown_as_unknown_not_as_zero(monkeypatch):
    """If the engine cannot parse a selector the probe must say so. A row
    reading 0 is a claim that the page does not have it; that claim has to be
    earned."""
    registry = {"made_up": {"selectors": ["div:nth-child(2)"], "min_expected": 1,
                            "critical": True, "page": "feed",
                            "fix_symbol": "nowhere"}}
    monkeypatch.setattr(shc, "SELECTOR_REGISTRY", registry)
    probe = selector_probe.probe_html("<div></div><div></div>", page="feed")
    row = probe["rows"][0]
    assert row["counts"]["div:nth-child(2)"] is None
    assert "div:nth-child(2)" in row["unsupported"]
    assert "UNSUPPORTED" in selector_probe.format_table(probe, "BROKEN")


def test_the_table_renders_and_names_the_symbol_to_edit():
    probe = selector_probe.probe_html(
        Path(fixture("feed_broken.html")).read_text(encoding="utf-8"), page="feed")
    table = selector_probe.format_table(probe, selector_probe.status_of(probe))
    assert "[FAIL] feed_container" in table
    assert "edit POST_SELECTORS" in table
    assert "Hooks present on this page" in table


def test_the_probe_cli_exits_with_the_status(capsys):
    assert selector_probe.main(["--fixture", fixture("feed_healthy.html"),
                                "--page", "feed"]) == pm.EXIT_OK
    assert selector_probe.main(["--fixture", fixture("feed_broken.html"),
                                "--page", "feed"]) == pm.EXIT_ERROR
    assert selector_probe.main(["--fixture", fixture("feed_degraded.html"),
                                "--page", "feed"]) == pm.EXIT_DEGRADED
    assert "SELECTOR PROBE" in capsys.readouterr().out


def test_the_probe_cli_emits_valid_json(capsys):
    selector_probe.main(["--fixture", fixture("post_box_open.html"),
                         "--page", "post", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "HEALTHY"
    assert payload["source"] == "fixture"
    assert payload["schema_version"] == shc.SCHEMA_VERSION


@pytest.mark.parametrize("url,page", [
    ("https://www.linkedin.com/feed/", "feed"),
    ("https://www.linkedin.com/feed/update/urn:li:activity:0/", "post"),
    ("https://www.linkedin.com/posts/example-activity-0", "post"),
    ("https://www.linkedin.com/search/results/people/?keywords=x", "search"),
])
def test_page_inference_from_a_url(url, page):
    assert selector_probe.infer_page(url) == page


def test_a_missing_fixture_fails_loudly_with_a_non_zero_exit(capsys):
    assert selector_probe.main(["--fixture", str(FIXTURES / "nope.html")]) == pm.EXIT_ERROR
    assert "Probe failed" in capsys.readouterr().out
