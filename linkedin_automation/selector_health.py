"""linkedin_automation/selector_health.py — detect broken LinkedIn selectors and generate
diagnostics for a human + Claude Code to fix them.

Opens the profile's Chrome session, navigates to the feed, and checks every
selector the scraper relies on (pulled from ``linkedin_ai_post_finder`` so the
registry stays in sync). Reports HEALTHY / DEGRADED / BROKEN, and when something
breaks it dumps the current DOM, suggests replacement selectors, and (if a
critical selector failed) writes ``.dev/SELECTOR_FIX_NEEDED.md`` with a
ready-to-paste fix prompt. It NEVER auto-edits selectors.

Usage: uv run python -m linkedin_automation.selector_health --profile jeff
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from . import profile_manager as pm
from .post_finder import LinkedInScraper
from .auto_connector import LinkedInAutoConnector
from .comment_poster import LinkedInCommentPoster
from .failure_capture import capture_failure

logger = logging.getLogger(__name__)

DEBUG_DUMP_FILE = "selector_debug_dump.html"
FIX_NEEDED_FILE = os.path.join(".dev", "SELECTOR_FIX_NEEDED.md")
HEALTH_RESULT_FILE = "selector_health.json"  # written under the profile data dir

# Registry of every selector the scraper depends on. Selectors are pulled from
# LinkedInScraper so this stays in sync with what the scraper actually uses.
# Each registry key maps to the finder symbol a fixer should edit.
SELECTOR_REGISTRY: Dict[str, Dict] = {
    "feed_container": {
        "selectors": list(LinkedInScraper.POST_SELECTORS),
        "min_expected": 3,
        "critical": True,
        "fix_symbol": "POST_SELECTORS",
    },
    "post_text": {
        "selectors": list(LinkedInScraper.TEXT_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "fix_symbol": "TEXT_SELECTORS",
    },
    "author": {
        "selectors": list(LinkedInScraper.AUTHOR_SELECTORS),
        "min_expected": 1,
        "critical": True,
        "fix_symbol": "AUTHOR_SELECTORS",
    },
    "overflow_menu": {
        "selectors": [LinkedInScraper.CONTROL_MENU_SELECTOR],
        "min_expected": 1,
        "critical": True,
        "fix_symbol": "CONTROL_MENU_SELECTOR",
    },
    "copy_link_item": {
        "selectors": [LinkedInScraper.MENU_ITEM_SELECTORS],
        "min_expected": 1,
        "critical": False,
        "requires_menu_open": True,
        "fix_symbol": "MENU_ITEM_SELECTORS / COPY_LINK_TEXT",
        "note": (f"text-matched '{LinkedInScraper.COPY_LINK_TEXT}'; only present "
                 "after the overflow menu is opened"),
    },
    "scroll_container": {
        "selectors": list(LinkedInScraper.SCROLL_CONTAINER_SELECTORS),
        "min_expected": 1,
        "critical": False,
        "fix_symbol": "SCROLL_CONTAINER_SELECTORS",
    },
    # Connector (auto-connect) selectors live on the people-SEARCH page, not the
    # feed, so the feed health run does not test them (page="search"); they are
    # registered here, in sync with LinkedInAutoConnector's constants, so the
    # watchdog tracks them and a future search-page check can use them.
    # The connector now iterates Connect LINKS directly (link-first), so the
    # Connect link is the one CRITICAL signal that it can work. The card/name
    # checks below are FALLBACK-only (LinkedIn removed data-view-name), so they
    # are non-critical — a 0 there is expected on the current DOM and must not
    # read as BROKEN.
    "connector_connect_link": {
        "selectors": list(LinkedInAutoConnector.CONNECT_LINK_SELECTORS)
        + [LinkedInAutoConnector.CONNECT_ACTION_SELECTOR],
        "min_expected": 1, "critical": True, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.CONNECT_LINK_SELECTORS",
        "note": "link-first PRIMARY: the 'Invite <Name> to connect' links",
    },
    "connector_search_result": {
        "selectors": [LinkedInAutoConnector.SEARCH_RESULT_SELECTOR]
        + list(LinkedInAutoConnector.RESULT_CARD_FALLBACK_SELECTORS),
        "min_expected": 1, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.SEARCH_RESULT_SELECTOR",
        "note": "fallback-only card wrapper (data-view-name removed); link-first is primary",
    },
    "connector_result_name": {
        "selectors": [LinkedInAutoConnector.RESULT_NAME_SELECTOR],
        "min_expected": 1, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.RESULT_NAME_SELECTOR",
        "note": "fallback-only name selector; link-first parses the name from the invite aria-label",
    },
    "connector_pagination": {
        "selectors": list(LinkedInAutoConnector.PAGINATION_NEXT_SELECTORS)
        + [LinkedInAutoConnector.PAGE_INDICATOR_SELECTOR],
        "min_expected": 1, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.PAGINATION_NEXT_SELECTORS",
        "note": "page-2+ Next button / numbered page indicators",
    },
    "connector_send_button": {
        "selectors": list(LinkedInAutoConnector.SEND_BUTTON_SELECTORS)
        + [LinkedInAutoConnector.SEND_BUTTON_LEGACY_SELECTOR],
        "min_expected": 1, "critical": True, "page": "search", "modal_only": True,
        "fix_symbol": "LinkedInAutoConnector.SEND_BUTTON_SELECTORS / SEND_BUTTON_LEGACY_SELECTOR",
        "note": ("modal-only: the Send button appears AFTER clicking Connect, so "
                 "it is NOT on the search results page and is skipped by the "
                 "non-clicking search health check"),
    },
    "connector_interop_outlet": {
        "selectors": [LinkedInAutoConnector.INTEROP_OUTLET_SELECTOR],
        "min_expected": 0, "critical": False, "page": "search",
        "fix_symbol": "LinkedInAutoConnector.INTEROP_OUTLET_SELECTOR",
        "note": "shadow-DOM host for the connect dialog; only present mid-connect",
    },

    # ─── Posting path (ROADMAP Phase 11, closes AUDIT G3) ─────────────────────
    #
    # Observed 2026-07-31: a posting run placed zero of three comments, and this
    # health check reported HEALTHY minutes afterwards. Its registry held only
    # feed-scraping selectors, so it gave a green light on the path least likely
    # to be the problem while ignoring the one that had just failed. Posting is
    # the highest-consequence action in the project and it had no coverage.
    #
    # These live on a post PERMALINK page (page="post"), not the feed. Two of
    # them are interaction-gated and cannot be counted by a passive page load;
    # they are marked so the report says "not checked" rather than implying a
    # clean result. Claiming a green on something never tested is the exact
    # failure this section exists to fix.
    "post_detail": {
        "selectors": list(LinkedInCommentPoster.POST_DETAIL_SELECTORS),
        "min_expected": 1, "critical": True, "page": "post",
        "fix_symbol": "LinkedInCommentPoster.POST_DETAIL_SELECTORS",
        "note": ("proves the permalink page rendered; the 2026-07-31 break was "
                 "here, when LinkedIn moved to data-testid and the legacy "
                 "selectors stopped matching"),
    },
    "post_like_button": {
        "selectors": list(LinkedInCommentPoster.LIKE_BUTTON_SELECTORS),
        "min_expected": 1, "critical": False, "page": "post",
        "fix_symbol": "LinkedInCommentPoster.LIKE_BUTTON_SELECTORS",
        "note": ("absent when the post is already liked, which is why this is "
                 "non-critical: a zero here is ambiguous, not broken"),
    },
    "post_comment_button": {
        "selectors": list(LinkedInCommentPoster.COMMENT_BUTTON_LABEL_SELECTORS)
        + [LinkedInCommentPoster.COMMENT_BUTTON_TEXT_SELECTOR],
        "min_expected": 1, "critical": True, "page": "post",
        "fix_symbol": ("LinkedInCommentPoster.COMMENT_BUTTON_LABEL_SELECTORS / "
                       "COMMENT_BUTTON_TEXT_SELECTOR"),
        "note": "the action-bar button that opens the comment box",
    },
    "post_comment_input": {
        "selectors": list(LinkedInCommentPoster.COMMENT_INPUT_SELECTORS),
        "min_expected": 1, "critical": True, "page": "post",
        "requires_interaction": True,
        "fix_symbol": "LinkedInCommentPoster.COMMENT_INPUT_SELECTORS",
        "note": ("the editor itself; only exists after the comment button is "
                 "clicked, so a passive page check cannot count it"),
    },
    "post_submit_button": {
        "selectors": [LinkedInCommentPoster.SUBMIT_BUTTON_XPATH],
        "min_expected": 1, "critical": True, "page": "post",
        "requires_interaction": True, "xpath": True,
        "fix_symbol": "LinkedInCommentPoster.SUBMIT_BUTTON_XPATH",
        "note": ("matched by visible text 'Comment', excluding aria-label="
                 "'Comment' which is the button that OPENS the box. That "
                 "collision is what broke posting on 2026-07-31: a guard "
                 "skipped any button labelled 'Comment' and so skipped the "
                 "submit button itself"),
    },
}


# ─── Pure logic (unit-tested without a browser) ───────────────────────────────

def check_registry(count_fn: Callable[[str], int], registry: Dict = None) -> Dict:
    """Evaluate every registry entry using ``count_fn(selector) -> int``.

    Returns ``{key: {ok, count, matched_selector, counts, critical, min_expected,
    note}}``. A key is ``ok`` when its best-matching selector reaches
    ``min_expected`` (or when ``min_expected`` is 0).
    """
    registry = registry if registry is not None else SELECTOR_REGISTRY
    result = {}
    for key, spec in registry.items():
        counts = {}
        for sel in spec["selectors"]:
            try:
                counts[sel] = int(count_fn(sel))
            except Exception:
                counts[sel] = 0
        best_sel, best_count = None, 0
        for sel in spec["selectors"]:
            if counts[sel] > best_count:
                best_count, best_sel = counts[sel], sel
        min_exp = spec.get("min_expected", 0)
        ok = (best_count >= min_exp) if min_exp > 0 else True
        result[key] = {
            "ok": ok,
            "count": best_count,
            "matched_selector": best_sel if best_count > 0 else None,
            "counts": counts,
            "critical": spec.get("critical", False),
            "min_expected": min_exp,
            "note": spec.get("note", ""),
        }
    return result


def overall_status(check: Dict) -> str:
    """HEALTHY (all ok), DEGRADED (a non-critical fail), or BROKEN (a critical fail)."""
    if any((not c["ok"]) and c["critical"] for c in check.values()):
        return "BROKEN"
    if any(not c["ok"] for c in check.values()):
        return "DEGRADED"
    return "HEALTHY"


def classify_search_page(logged_in: bool, card_count: int) -> str:
    """Distinguish 'not logged in' from 'logged in but no results' for a search page.

    Login is decided by the URL/authwall (``logged_in``), NEVER by the presence of
    result cards — an empty search is a valid logged-in page. Returns:
      - ``not_logged_in`` — the URL bounced to login/authwall,
      - ``no_results`` — logged in, but zero cards (an empty search OR a stale card
        selector; the two can't be told apart from card count alone),
      - ``ok`` — logged in with result cards present.
    """
    if not logged_in:
        return "not_logged_in"
    if card_count <= 0:
        return "no_results"
    return "ok"


def suggest_selectors(diag: Dict) -> List[str]:
    """Suggest candidate selectors from stable hooks found in the live DOM."""
    out = []
    for v in sorted(set(diag.get("data_view_names", []))):
        out.append(f"[data-view-name='{v}']")
    for t in sorted(set(diag.get("data_testids", []))):
        out.append(f"[data-testid='{t}']")
    return out


def build_fix_markdown(profile_name: str, status: str, check: Dict,
                       dump_path: str, suggestions: List[str]) -> str:
    """Build the .dev/SELECTOR_FIX_NEEDED.md content (failed selectors + fix prompt)."""
    failed = {k: c for k, c in check.items() if not c["ok"]}
    lines = [
        "# SELECTOR FIX NEEDED",
        "",
        f"- **Status:** {status}",
        f"- **Profile:** {profile_name}",
        f"- **Detected:** {datetime.now().isoformat()}",
        f"- **DOM dump:** `{dump_path}`",
        "",
        "## Failed selectors",
        "",
    ]
    for key, c in failed.items():
        spec = SELECTOR_REGISTRY.get(key, {})
        crit = "CRITICAL" if c["critical"] else "non-critical"
        lines.append(f"### `{key}` ({crit}) — edit `LinkedInScraper.{spec.get('fix_symbol', '?')}`")
        lines.append(f"- expected >= {c['min_expected']} matches, got {c['count']}")
        for sel, n in c.get("counts", {}).items():
            lines.append(f"  - `{sel}` -> {n}")
        if c.get("note"):
            lines.append(f"- note: {c['note']}")
        lines.append("")

    lines.append("## Suggested hooks from the current DOM")
    lines.append("")
    if suggestions:
        for s in suggestions[:40]:
            lines.append(f"- `{s}`")
    else:
        lines.append("- (none collected)")
    lines.append("")

    failed_keys = ", ".join(failed.keys())
    lines += [
        "## Ready-to-paste prompt for Claude Code",
        "",
        "```",
        f"The LinkedIn scraper's selectors broke ({status}). Failed: {failed_keys}.",
        f"Read `{dump_path}` (the current feed DOM for the first few posts) and update",
        "the matching selector lists in linkedin_automation/post_finder.py "
        "(POST_SELECTORS / TEXT_SELECTORS / AUTHOR_SELECTORS / CONTROL_MENU_SELECTOR /",
        "MENU_ITEM_SELECTORS / SCROLL_CONTAINER_SELECTORS) so they match the current DOM.",
        "Add the new working selectors FIRST, keep the old ones as fallbacks. Then run",
        f"`uv run python -m linkedin_automation.selector_health --profile {profile_name}` and confirm HEALTHY.",
        "```",
        "",
    ]
    return "\n".join(lines)


# ─── Browser-driven checks ────────────────────────────────────────────────────

_WALK_JS = """
const root = arguments[0], maxDepth = arguments[1];
const out = [];
function attrs(el){const o={}; for (const a of el.attributes) o[a.name]=a.value; return o;}
(function rec(el, d){
    out.push({tag: el.tagName.toLowerCase(), attrs: attrs(el)});
    if (d < maxDepth) for (const c of el.children) rec(c, d+1);
})(root, 0);
return out;
"""


def _check_copy_link(driver) -> Dict:
    """Open the first post's overflow menu and look for the 'Copy link' item."""
    spec = SELECTOR_REGISTRY["copy_link_item"]
    base = {
        "critical": spec["critical"], "min_expected": spec["min_expected"],
        "note": spec.get("note", ""), "counts": {}, "matched_selector": None,
    }
    try:
        btn = driver.find_element(By.CSS_SELECTOR, LinkedInScraper.CONTROL_MENU_SELECTOR)
    except Exception:
        return {**base, "ok": False, "count": 0, "error": "no overflow menu to open"}
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.4)
        btn.click()
        time.sleep(1.2)
        items = driver.find_elements(By.CSS_SELECTOR, LinkedInScraper.MENU_ITEM_SELECTORS)
        n = sum(1 for it in items
                if LinkedInScraper.COPY_LINK_TEXT in (it.text or "").strip().lower())
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            logger.debug("ESC dismiss failed", exc_info=True)
        return {
            **base, "ok": n >= spec["min_expected"], "count": n,
            "matched_selector": LinkedInScraper.MENU_ITEM_SELECTORS if n else None,
            "counts": {LinkedInScraper.MENU_ITEM_SELECTORS: n},
        }
    except Exception as e:
        return {**base, "ok": False, "count": 0, "error": str(e)}


def collect_diagnostics(driver, max_posts: int = 3) -> Dict:
    """Dump the first ``max_posts`` post containers and summarize their attributes."""
    posts = []
    for sel in LinkedInScraper.POST_SELECTORS:
        posts = driver.find_elements(By.CSS_SELECTOR, sel)
        if posts:
            break
    posts = posts[:max_posts]

    htmls, view_names, testids, classes, arias = [], [], [], [], []
    for p in posts:
        try:
            htmls.append(p.get_attribute("outerHTML") or "")
        except Exception:
            logger.debug("outerHTML read failed", exc_info=True)
        try:
            for node in driver.execute_script(_WALK_JS, p, 6):
                a = node.get("attrs", {})
                if a.get("data-view-name"):
                    view_names.append(a["data-view-name"])
                if a.get("data-testid"):
                    testids.append(a["data-testid"])
                if a.get("class"):
                    classes.extend(a["class"].split())
                if a.get("aria-label") and node.get("tag") == "button":
                    arias.append(a["aria-label"])
        except Exception:
            logger.debug("attribute walk failed", exc_info=True)

    try:
        with open(DEBUG_DUMP_FILE, "w", encoding="utf-8") as f:
            f.write(f"<!-- selector_debug_dump: first {len(posts)} feed posts, "
                    f"{datetime.now().isoformat()} -->\n")
            for h in htmls:
                f.write(h + "\n\n<hr/>\n\n")
        logger.info(f"Dumped {len(posts)} post containers to {DEBUG_DUMP_FILE}")
    except Exception:
        logger.debug("Could not write debug dump", exc_info=True)

    return {
        "posts_dumped": len(posts),
        "data_view_names": sorted(set(view_names)),
        "data_testids": sorted(set(testids)),
        "class_tokens": sorted(set(classes))[:60],
        "button_aria_labels": sorted(set(arias)),
    }


def run_health_check(profile_name: str = None, scrolls: int = 3) -> Dict:
    """Open Chrome, scan the feed, and return the structured health result.

    Also writes selector_health.json under the profile data dir, a DOM dump when
    anything failed, and .dev/SELECTOR_FIX_NEEDED.md when a critical selector failed.
    """
    driver, _profile = pm.create_driver(profile_name)
    try:
        logger.info("Loading LinkedIn feed...")
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(5)

        if not pm.is_logged_in_on_page(driver):
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile '{profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile {profile_name or 'default'}"
            )

        for _ in range(scrolls):
            driver.execute_script("window.scrollBy(0, 1200);")
            time.sleep(2)

        def count_fn(sel):
            return len(driver.find_elements(By.CSS_SELECTOR, sel))

        # Check feed-page selectors here. Skip the menu-dependent copy_link_item
        # (checked separately below) and any page="search" entries (connector
        # selectors live on the search page, not the feed, so they can't be
        # tested by this feed run).
        page_registry = {
            k: v for k, v in SELECTOR_REGISTRY.items()
            if not v.get("requires_menu_open") and v.get("page", "feed") == "feed"
        }
        check = check_registry(count_fn, page_registry)
        check["copy_link_item"] = _check_copy_link(driver)
        logger.info("(connector search-page selectors are registered but not tested on the feed)")

        status = overall_status(check)
        failed = [k for k, c in check.items() if not c["ok"]]
        for key, c in check.items():
            mark = "PASS" if c["ok"] else "FAIL"
            crit = "critical" if c["critical"] else "optional"
            logger.info(f"  [{mark}] {key:16s} ({crit}) count={c['count']} "
                        f"min={c['min_expected']} via={c['matched_selector']}")

        result = {
            "profile": profile_name,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "failed": failed,
            "checks": check,
        }

        if failed:
            diag = collect_diagnostics(driver)
            result["debug_dump"] = DEBUG_DUMP_FILE
            result["diagnostics"] = diag
            result["suggested_selectors"] = suggest_selectors(diag)
            logger.warning(f"Failed selectors: {failed}")
            logger.info(f"Suggested hooks: {result['suggested_selectors']}")

        if status == "BROKEN":
            md = build_fix_markdown(
                profile_name or "default", status, check,
                DEBUG_DUMP_FILE, result.get("suggested_selectors", []),
            )
            os.makedirs(os.path.dirname(FIX_NEEDED_FILE), exist_ok=True)
            with open(FIX_NEEDED_FILE, "w", encoding="utf-8") as f:
                f.write(md)
            result["fix_file"] = FIX_NEEDED_FILE
            logger.warning(f"BROKEN — wrote fix instructions to {FIX_NEEDED_FILE}")
            # A critical selector broke — capture a screenshot so the BROKEN report
            # has a VISUAL of the feed alongside the HTML DOM dump.
            shot = capture_failure(driver, "feed_selectors_broken", profile_name)
            if shot:
                result["failure_screenshot"] = shot

        # Persist the result for the dashboard endpoint to read.
        try:
            out_path = os.path.join(pm.get_data_dir(profile_name), HEALTH_RESULT_FILE)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["result_file"] = out_path
        except Exception:
            logger.debug("Could not write selector_health.json", exc_info=True)

        return result
    finally:
        driver.quit()


def run_search_health_check(profile_name: str, search_url: str, scrolls: int = 3) -> Dict:
    """Check the connector's people-SEARCH-page selectors against a live search URL.

    Navigates to ``search_url`` (the same one passed to the connector), scrolls to
    trigger lazy-loading, and runs check_registry over the page="search" entries —
    WITHOUT clicking Connect (so no invitations are sent). Modal-only entries (the
    Send button, which appears only after a Connect click) are skipped. Writes
    selector_health_search.json under the profile data dir.
    """
    driver, _profile = pm.create_driver(profile_name)
    try:
        logger.info(f"Loading people-search results: {search_url}")
        driver.get(search_url)
        time.sleep(6)

        if not pm.is_logged_in_on_page(driver):
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile '{profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile {profile_name or 'default'}"
            )

        for _ in range(scrolls):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        def count_fn(sel):
            return len(driver.find_elements(By.CSS_SELECTOR, sel))

        # Only the connector's search-page selectors, minus modal-only ones (the
        # Send button isn't present until Connect is clicked, which we never do).
        search_registry = {
            k: v for k, v in SELECTOR_REGISTRY.items()
            if v.get("page") == "search" and not v.get("modal_only")
        }
        check = check_registry(count_fn, search_registry)

        status = overall_status(check)
        failed = [k for k, c in check.items() if not c["ok"]]
        for key, c in check.items():
            mark = "PASS" if c["ok"] else "FAIL"
            crit = "critical" if c["critical"] else "optional"
            logger.info(f"  [{mark}] {key:24s} ({crit}) count={c['count']} "
                        f"min={c['min_expected']} via={c['matched_selector']}")

        # We only reach here past the URL/authwall login gate, so login is
        # confirmed regardless of how many results rendered. Base "has results" on
        # the link-first Connect-link count (cards are a dead fallback now); this
        # distinguishes an empty search from stale selectors without conflating
        # either with "not logged in".
        results_count = max(
            check.get("connector_connect_link", {}).get("count", 0),
            check.get("connector_search_result", {}).get("count", 0),
        )
        search_state = classify_search_page(True, results_count)
        if search_state == "no_results":
            logger.info(
                "Logged in (URL confirms), but 0 result cards found — the search "
                "may simply have no people results, or the card selector may be "
                "stale. Try a broader search URL to disambiguate."
            )

        result = {
            "profile": profile_name,
            "page": "search",
            "search_url": search_url,
            "status": status,
            "logged_in": True,
            "search_state": search_state,
            "timestamp": datetime.now().isoformat(),
            "failed": failed,
            "checks": check,
        }

        # A critical search-page selector broke — capture a visual alongside the
        # JSON report (the search page, not the feed).
        if status == "BROKEN":
            shot = capture_failure(driver, "search_selectors_broken", profile_name)
            if shot:
                result["failure_screenshot"] = shot

        try:
            out_path = os.path.join(pm.get_data_dir(profile_name), "selector_health_search.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["result_file"] = out_path
        except Exception:
            logger.debug("Could not write selector_health_search.json", exc_info=True)

        return result
    finally:
        driver.quit()


def _print_summary(result: Dict):
    print("\n" + "=" * 60)
    print(f"  SELECTOR HEALTH: {result['status']}")
    print("=" * 60)
    for key, c in result["checks"].items():
        mark = "✓" if c["ok"] else "✗"
        print(f"  {mark} {key:16s} count={c['count']:>3} (min {c['min_expected']}) "
              f"{'[critical]' if c['critical'] else ''}")
    if result.get("debug_dump"):
        print(f"\n  DOM dump: {result['debug_dump']}")
    if result.get("suggested_selectors"):
        print("  Suggested hooks:")
        for s in result["suggested_selectors"][:20]:
            print(f"    {s}")
    if result.get("fix_file"):
        print(f"\n  ⚠ BROKEN — fix instructions written to {result['fix_file']}")
    print()


def main(argv=None) -> int:
    """CLI entry point. Exit 0 = HEALTHY/DEGRADED, 1 = BROKEN/error, 2 = login required."""
    # Make emoji output safe on the Windows cp1252 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Check LinkedIn scraper selector health")
    parser.add_argument("--profile", default=None, help="Profile name (default profile if omitted)")
    parser.add_argument("--scrolls", type=int, default=3, help="Feed scrolls before checking")
    parser.add_argument("--search-url", default=None,
                        help="Check the connector's people-SEARCH-page selectors against this "
                             "URL instead of the feed (does NOT click Connect / send invites)")
    parser.add_argument("--json", action="store_true", help="Print the JSON result to stdout")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    try:
        pm.auto_migrate_from_env()
        if args.search_url:
            result = run_search_health_check(args.profile, args.search_url, scrolls=args.scrolls)
        else:
            result = run_health_check(args.profile, scrolls=args.scrolls)
    except pm.LoginRequiredError as e:
        print(f"\n❌ {e}")
        return pm.EXIT_LOGIN_REQUIRED
    except Exception as e:
        print(f"\n❌ Health check failed: {e}")
        return pm.EXIT_ERROR

    if args.json:
        print(json.dumps(result))
    else:
        _print_summary(result)

    return pm.EXIT_ERROR if result["status"] == "BROKEN" else pm.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
