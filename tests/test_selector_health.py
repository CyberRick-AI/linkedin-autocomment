"""Tests for selector_health_check pure logic (selector-watchdog).

No browser: check_registry takes a count function, so we feed it fakes. Also
verifies the registry stays in sync with the scraper's selector constants."""

from linkedin_automation import selector_health as shc
from linkedin_automation.post_finder import LinkedInScraper


# ─── Registry stays in sync with the scraper ──────────────────────────────────

def test_registry_pulls_selectors_from_finder():
    reg = shc.SELECTOR_REGISTRY
    assert reg["feed_container"]["selectors"] == list(LinkedInScraper.POST_SELECTORS)
    assert reg["post_text"]["selectors"] == list(LinkedInScraper.TEXT_SELECTORS)
    assert reg["author"]["selectors"] == list(LinkedInScraper.AUTHOR_SELECTORS)
    assert reg["overflow_menu"]["selectors"] == [LinkedInScraper.CONTROL_MENU_SELECTOR]
    assert reg["scroll_container"]["selectors"] == list(LinkedInScraper.SCROLL_CONTAINER_SELECTORS)


def test_registry_criticality_and_keys():
    reg = shc.SELECTOR_REGISTRY
    feed_keys = {"feed_container", "post_text", "author",
                 "overflow_menu", "copy_link_item", "scroll_container"}
    assert feed_keys.issubset(set(reg))
    assert reg["feed_container"]["critical"] is True
    assert reg["overflow_menu"]["critical"] is True
    assert reg["copy_link_item"]["critical"] is False
    assert reg["copy_link_item"].get("requires_menu_open") is True


def test_registry_includes_connector_selectors_in_sync():
    from linkedin_automation.auto_connector import LinkedInAutoConnector as C
    reg = shc.SELECTOR_REGISTRY
    # Link-first: the Connect link is the CRITICAL primary signal.
    connect_sels = reg["connector_connect_link"]["selectors"]
    assert connect_sels[:len(C.CONNECT_LINK_SELECTORS)] == list(C.CONNECT_LINK_SELECTORS)
    assert reg["connector_connect_link"]["critical"] is True
    # The card + name checks are now FALLBACK-only (non-critical): LinkedIn removed
    # data-view-name, so a 0 there must not read as BROKEN.
    assert C.SEARCH_RESULT_SELECTOR in reg["connector_search_result"]["selectors"]
    assert reg["connector_search_result"]["critical"] is False
    assert reg["connector_result_name"]["selectors"] == [C.RESULT_NAME_SELECTOR]
    assert reg["connector_result_name"]["critical"] is False
    # Pagination is registered (non-critical) and pulls the connector's constants.
    assert reg["connector_pagination"]["selectors"][0] == C.PAGINATION_NEXT_SELECTORS[0]
    assert reg["connector_pagination"]["critical"] is False
    # The Send button is modal-only (appears after clicking Connect), so it's
    # skipped by the non-clicking search health check.
    assert reg["connector_send_button"].get("modal_only") is True
    # They are marked page="search" so the feed health run skips them.
    for key in ("connector_connect_link", "connector_search_result",
                "connector_result_name", "connector_pagination",
                "connector_send_button", "connector_interop_outlet"):
        assert reg[key]["page"] == "search"


# ─── check_registry ───────────────────────────────────────────────────────────

def test_all_healthy():
    chk = shc.check_registry(lambda sel: 5)
    assert shc.overall_status(chk) == "HEALTHY"
    assert all(c["ok"] for c in chk.values())


def test_min_expected_enforced_for_feed():
    # feed_container needs >= 3 matches.
    chk2 = shc.check_registry(lambda sel: 2, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk2["feed_container"]["ok"] is False
    chk3 = shc.check_registry(lambda sel: 3, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk3["feed_container"]["ok"] is True


def test_critical_failure_is_broken():
    # Only the primary feed selector breaks; fallbacks also 0 -> BROKEN.
    chk = shc.check_registry(lambda sel: 0, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk["feed_container"]["ok"] is False
    assert shc.overall_status(chk) == "BROKEN"


def test_noncritical_failure_is_degraded():
    sub = {"feed_container": shc.SELECTOR_REGISTRY["feed_container"],
           "scroll_container": shc.SELECTOR_REGISTRY["scroll_container"]}
    counts = {"mainFeed": 0, "scaffold-finite-scroll": 0}

    def fake(sel):
        return 0 if any(s in sel for s in counts) else 5

    chk = shc.check_registry(fake, sub)
    assert chk["feed_container"]["ok"] is True
    assert chk["scroll_container"]["ok"] is False
    assert shc.overall_status(chk) == "DEGRADED"


def test_matched_selector_is_first_working_fallback():
    # Primary feed selector fails, a fallback works -> ok via fallback.
    def fake(sel):
        return 0 if "feed-full-update" in sel else 4

    chk = shc.check_registry(fake, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    assert chk["feed_container"]["ok"] is True
    assert chk["feed_container"]["matched_selector"] != "div[data-view-name='feed-full-update']"


def test_per_selector_counts_recorded():
    chk = shc.check_registry(lambda sel: 7, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    counts = chk["feed_container"]["counts"]
    assert all(v == 7 for v in counts.values())
    assert "div[data-view-name='feed-full-update']" in counts


# ─── suggestions + fix markdown ───────────────────────────────────────────────

def test_suggest_selectors():
    out = shc.suggest_selectors({"data_view_names": ["feed-actor", "feed-full-update"],
                                 "data_testids": ["mainFeed"]})
    assert "[data-view-name='feed-actor']" in out
    assert "[data-testid='mainFeed']" in out


def test_build_fix_markdown_contains_essentials():
    chk = shc.check_registry(lambda sel: 0, {"feed_container": shc.SELECTOR_REGISTRY["feed_container"]})
    md = shc.build_fix_markdown("jeff", "BROKEN", chk, "selector_debug_dump.html",
                                ["[data-view-name='feed-full-update']"])
    assert "feed_container" in md
    assert "POST_SELECTORS" in md            # the symbol a fixer edits
    assert "selector_debug_dump.html" in md  # dump location
    assert "Ready-to-paste prompt" in md
    assert "jeff" in md
