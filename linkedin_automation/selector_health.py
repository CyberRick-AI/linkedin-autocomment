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

from . import dom_probe
from . import profile_manager as pm
from .post_finder import LinkedInScraper
from .auto_connector import LinkedInAutoConnector
from .comment_poster import LinkedInCommentPoster
from .failure_capture import capture_failure

logger = logging.getLogger(__name__)

DEBUG_DUMP_FILE = "selector_debug_dump.html"
FIX_NEEDED_FILE = os.path.join(".dev", "SELECTOR_FIX_NEEDED.md")
HEALTH_RESULT_FILE = "selector_health.json"  # written under the profile data dir
SEARCH_RESULT_FILE = "selector_health_search.json"
POST_RESULT_FILE = "selector_health_post.json"

# Bump when a field is removed or its meaning changes. Adding a field does not
# require a bump; readers must ignore what they do not recognise.
# The field-by-field contract lives in docs/SELECTOR-HEALTH-SCHEMA.md.
SCHEMA_VERSION = 1

PAGES = ("feed", "search", "post")

RESULT_FILE_BY_PAGE = {
    "feed": HEALTH_RESULT_FILE,
    "search": SEARCH_RESULT_FILE,
    "post": POST_RESULT_FILE,
}

# Why an entry could not be counted by a passive page load. Every one of these
# is a reason to say "not checked", never a reason to record a zero. Recording a
# zero for something that was never tested is how a health check ends up
# reporting HEALTHY minutes after a run that posted nothing (AUDIT G3).
GATE_FLAGS = ("requires_interaction", "requires_menu_open", "modal_only")

GATE_REASONS = {
    "requires_interaction": ("not checked: this element does not exist until the "
                             "comment box is opened, so a page load cannot count it"),
    "requires_menu_open": ("not checked: only present after the post's overflow "
                           "menu is opened"),
    "modal_only": ("not checked: only present after Connect is clicked, which the "
                   "health check never does"),
}

PAGE_NOT_RUN_REASON = {
    "feed": "not checked: no feed run recorded. Needs a live LinkedIn session.",
    "search": ("not checked: no people-search run recorded. Needs a live LinkedIn "
               "session and a search URL."),
    "post": ("not checked: no post-page run recorded. Needs a live LinkedIn session "
             "and a post URL."),
}

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

def check_registry(count_fn: Callable[[str], int], registry: Dict = None,
                   xpath_count_fn: Callable[[str], int] = None) -> Dict:
    """Evaluate every registry entry using ``count_fn(selector) -> int``.

    ``xpath_count_fn`` counts entries flagged ``xpath: True``, which cannot be
    counted by a CSS engine. It defaults to ``count_fn`` because a synthetic
    counter in a test answers both the same way; every real caller (Selenium,
    ``dom_probe``) passes a genuine XPath counter, because for the submit button
    a CSS-only count would return a confident zero.

    Returns ``{key: {ok, count, matched_selector, counts, critical, min_expected,
    note}}``. A key is ``ok`` when its best-matching selector reaches
    ``min_expected`` (or when ``min_expected`` is 0).
    """
    registry = registry if registry is not None else SELECTOR_REGISTRY
    xpath_count_fn = xpath_count_fn or count_fn
    result = {}
    for key, spec in registry.items():
        counter = xpath_count_fn if spec.get("xpath") else count_fn
        counts = {}
        for sel in spec["selectors"]:
            try:
                counts[sel] = int(counter(sel))
            except dom_probe.UnsupportedSelector:
                # Deliberately NOT folded into the zero below. A selector the
                # offline engine cannot parse is an unknown, and an unknown
                # recorded as "0 matches" is a claim the page lacks something
                # nobody actually looked for. Fail the run instead.
                raise
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


def gate_reason(spec: Dict) -> str:
    """Return why a passive page load cannot count this entry, or "" if it can."""
    for flag in GATE_FLAGS:
        if spec.get(flag):
            return GATE_REASONS[flag]
    return ""


def registry_for_page(page: str, include_gated: bool = False,
                      registry: Dict = None) -> Dict:
    """Return the registry entries that live on ``page``.

    Interaction-gated entries are excluded unless ``include_gated``, because a
    page load cannot count them. The probe passes ``include_gated=True`` when it
    is run against a capture taken with the comment box already open; the health
    gate never does, so it can never pass on the strength of an untested entry.
    """
    registry = registry if registry is not None else SELECTOR_REGISTRY
    return {
        key: spec for key, spec in registry.items()
        if spec.get("page", "feed") == page and (include_gated or not gate_reason(spec))
    }


def result_level(entry_status: str, critical: bool) -> str:
    """Map an entry status onto the dashboard's green / amber / red."""
    if entry_status == "pass":
        return "green"
    if entry_status == "not_checked":
        return "amber"
    return "red" if critical else "amber"


def build_health_report(results_by_page: Dict, registry: Dict = None) -> Dict:
    """Merge per-page run results into one report covering the WHOLE registry.

    ``results_by_page`` maps a page name to that page's run result (the dict
    ``run_health_check`` and friends return), or to ``None`` when that page was
    never run.

    Every registry entry appears in the output exactly once, and an entry that
    was not tested is reported as ``not_checked`` with the reason. That is the
    difference between this report and a bare status string: a run that only
    covered the feed cannot present itself as a clean bill of health for
    posting, which is precisely what happened on 2026-07-31.
    """
    registry = registry if registry is not None else SELECTOR_REGISTRY
    pages, any_fail_critical, any_fail_other, any_unchecked = {}, False, False, False

    for page in PAGES:
        result = results_by_page.get(page) or None
        checks = (result or {}).get("checks", {})
        entries = []
        for key, spec in registry.items():
            if spec.get("page", "feed") != page:
                continue
            check = checks.get(key)
            reason = ""
            if check is None:
                status = "not_checked"
                reason = gate_reason(spec) or PAGE_NOT_RUN_REASON[page]
            else:
                status = "pass" if check.get("ok") else "fail"
            critical = bool(spec.get("critical"))
            if status == "fail":
                if critical:
                    any_fail_critical = True
                else:
                    any_fail_other = True
            elif status == "not_checked":
                any_unchecked = True
            entries.append({
                "key": key,
                "page": page,
                "critical": critical,
                "status": status,
                "level": result_level(status, critical),
                "reason": reason,
                "count": (check or {}).get("count", 0),
                "min_expected": spec.get("min_expected", 0),
                "matched_selector": (check or {}).get("matched_selector"),
                "selectors": list(spec["selectors"]),
                "counts": (check or {}).get("counts", {}),
                "fix_symbol": spec.get("fix_symbol", ""),
                "note": spec.get("note", ""),
            })
        pages[page] = {
            "ran": result is not None,
            "status": (result or {}).get("status", "NOT_RUN"),
            "source": (result or {}).get("source", "none"),
            "timestamp": (result or {}).get("timestamp"),
            "entries": entries,
        }

    if any_fail_critical:
        overall = "BROKEN"
    elif any_fail_other:
        overall = "DEGRADED"
    elif any_unchecked:
        # Deliberately NOT "HEALTHY". Reporting a clean bill for a registry that
        # was only partly tested is the exact failure this project already had.
        overall = "INCOMPLETE"
    else:
        overall = "HEALTHY"

    return {
        "schema_version": SCHEMA_VERSION,
        "overall": overall,
        "complete": not any_unchecked,
        "pages": pages,
    }


# ─── Report schema ────────────────────────────────────────────────────────────
#
# Documented field by field in docs/SELECTOR-HEALTH-SCHEMA.md. Kept as data
# rather than prose so a test can assert a real report satisfies it; a schema
# only living in a markdown file drifts the first time someone adds a key.

REPORT_SCHEMA: Dict[str, Dict] = {
    "profile": {"types": (str, type(None)), "required": True},
    "page": {"types": (str,), "required": True, "choices": PAGES},
    "source": {"types": (str,), "required": True, "choices": ("live", "fixture")},
    "status": {"types": (str,), "required": True,
               "choices": ("HEALTHY", "DEGRADED", "BROKEN")},
    "schema_version": {"types": (int,), "required": True},
    "timestamp": {"types": (str,), "required": True},
    "failed": {"types": (list,), "required": True},
    "checks": {"types": (dict,), "required": True},
}

CHECK_SCHEMA: Dict[str, Dict] = {
    "ok": {"types": (bool,), "required": True},
    "count": {"types": (int,), "required": True},
    "matched_selector": {"types": (str, type(None)), "required": True},
    "counts": {"types": (dict,), "required": True},
    "critical": {"types": (bool,), "required": True},
    "min_expected": {"types": (int,), "required": True},
    "note": {"types": (str,), "required": True},
}


def _validate_fields(obj: Dict, schema: Dict, where: str) -> List[str]:
    errors = []
    for field, rule in schema.items():
        if field not in obj:
            if rule.get("required"):
                errors.append(f"{where}: missing required field '{field}'")
            continue
        value = obj[field]
        if not isinstance(value, rule["types"]):
            names = "/".join(t.__name__ for t in rule["types"])
            errors.append(f"{where}: '{field}' should be {names}, got "
                          f"{type(value).__name__}")
            continue
        if "choices" in rule and value not in rule["choices"]:
            errors.append(f"{where}: '{field}' is {value!r}, expected one of "
                          f"{list(rule['choices'])}")
    return errors


def validate_report(report: Dict) -> List[str]:
    """Return a list of schema violations. Empty list means the report is valid."""
    if not isinstance(report, dict):
        return ["report is not an object"]
    errors = _validate_fields(report, REPORT_SCHEMA, "report")
    for key, check in (report.get("checks") or {}).items():
        if not isinstance(check, dict):
            errors.append(f"checks['{key}'] is not an object")
            continue
        errors.extend(_validate_fields(check, CHECK_SCHEMA, f"checks['{key}']"))
    failed = report.get("failed")
    checks = report.get("checks")
    if isinstance(failed, list) and isinstance(checks, dict):
        for key in failed:
            if key not in checks:
                errors.append(f"report: 'failed' names '{key}', which is not in 'checks'")
        should_fail = {k for k, c in checks.items()
                       if isinstance(c, dict) and not c.get("ok")}
        if should_fail != set(failed):
            errors.append(f"report: 'failed' is {sorted(failed)} but the failing "
                          f"checks are {sorted(should_fail)}")
    return errors


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


def _driver_counters(driver):
    """Return ``(css_count, xpath_count)`` bound to a live driver.

    Two functions, not one, because ``SUBMIT_BUTTON_XPATH`` is matched by
    visible text. Passing it to ``find_elements(By.CSS_SELECTOR, ...)`` does not
    raise, it returns nothing, and the check would then report a confident zero
    for the single highest-consequence selector in the project.
    """
    def count_css(sel):
        return len(driver.find_elements(By.CSS_SELECTOR, sel))

    def count_xpath(sel):
        return len(driver.find_elements(By.XPATH, sel))

    return count_css, count_xpath


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

        count_fn, xpath_count_fn = _driver_counters(driver)

        # Check feed-page selectors here. Skip the menu-dependent copy_link_item
        # (checked separately below) and any page="search" entries (connector
        # selectors live on the search page, not the feed, so they can't be
        # tested by this feed run).
        page_registry = registry_for_page("feed")
        check = check_registry(count_fn, page_registry, xpath_count_fn)
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
            "page": "feed",
            "source": "live",
            "schema_version": SCHEMA_VERSION,
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

        count_fn, xpath_count_fn = _driver_counters(driver)

        # Only the connector's search-page selectors, minus modal-only ones (the
        # Send button isn't present until Connect is clicked, which we never do).
        search_registry = registry_for_page("search")
        check = check_registry(count_fn, search_registry, xpath_count_fn)

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
            "source": "live",
            "schema_version": SCHEMA_VERSION,
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
            out_path = os.path.join(pm.get_data_dir(profile_name), SEARCH_RESULT_FILE)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["result_file"] = out_path
        except Exception:
            logger.debug(f"Could not write {SEARCH_RESULT_FILE}", exc_info=True)

        return result
    finally:
        driver.quit()


def run_post_health_check(profile_name: str, post_url: str) -> Dict:
    """Check the POSTING path's selectors against a live post permalink page.

    Read-only. It loads the permalink and counts; it never opens the comment box
    and never types or submits anything, so it cannot post. The two
    interaction-gated entries (the editor and the submit button) are therefore
    reported as not checked rather than counted, because they do not exist until
    the box is opened.

    **This needs a live LinkedIn session and so is Rick's to run**, per the
    project boundary. Claude Code writes and unit-tests it and never executes it.
    """
    driver, _profile = pm.create_driver(profile_name)
    try:
        logger.info(f"Loading post permalink: {post_url}")
        driver.get(post_url)
        time.sleep(5)

        if not pm.is_logged_in_on_page(driver):
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile '{profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile {profile_name or 'default'}"
            )

        count_fn, xpath_count_fn = _driver_counters(driver)
        check = check_registry(count_fn, registry_for_page("post"), xpath_count_fn)

        status = overall_status(check)
        failed = [k for k, c in check.items() if not c["ok"]]
        for key, c in check.items():
            mark = "PASS" if c["ok"] else "FAIL"
            crit = "critical" if c["critical"] else "optional"
            logger.info(f"  [{mark}] {key:24s} ({crit}) count={c['count']} "
                        f"min={c['min_expected']} via={c['matched_selector']}")
        for key in sorted(set(registry_for_page("post", include_gated=True))
                          - set(check)):
            logger.info(f"  [SKIP] {key:24s} {gate_reason(SELECTOR_REGISTRY[key])}")

        result = {
            "profile": profile_name,
            "page": "post",
            "source": "live",
            "schema_version": SCHEMA_VERSION,
            "post_url": post_url,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "failed": failed,
            "checks": check,
        }

        if status == "BROKEN":
            shot = capture_failure(driver, "post_selectors_broken", profile_name)
            if shot:
                result["failure_screenshot"] = shot

        try:
            out_path = os.path.join(pm.get_data_dir(profile_name), POST_RESULT_FILE)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            result["result_file"] = out_path
        except Exception:
            logger.debug(f"Could not write {POST_RESULT_FILE}", exc_info=True)

        return result
    finally:
        driver.quit()


def run_fixture_health_check(fixture_path: str, page: str = "feed",
                             include_gated: bool = False) -> Dict:
    """Run the registry for ``page`` against a SAVED HTML file. No browser.

    This is what makes the selector gate testable: a fixture with a deliberately
    renamed class must report BROKEN, and the unmodified fixture must report
    HEALTHY. Both are verifiable in CI on a machine with no LinkedIn session and
    no Chrome, which is the only way a gate on this path can run at all.
    """
    if page not in PAGES:
        raise ValueError(f"unknown page '{page}'; expected one of {list(PAGES)}")

    with open(fixture_path, "r", encoding="utf-8") as f:
        html = f.read()

    counter = dom_probe.make_counter(html)
    check = check_registry(
        lambda sel: counter(sel),
        registry_for_page(page, include_gated=include_gated),
        lambda sel: counter(sel, xpath=True),
    )

    status = overall_status(check)
    failed = [k for k, c in check.items() if not c["ok"]]
    return {
        "profile": None,
        "page": page,
        "source": "fixture",
        "schema_version": SCHEMA_VERSION,
        "fixture": fixture_path,
        "status": status,
        "timestamp": datetime.now().isoformat(),
        "failed": failed,
        "checks": check,
    }


def load_saved_reports(profile_name: str = None) -> Dict:
    """Read whatever per-page reports have been written for this profile.

    Missing pages come back as ``None``, which ``build_health_report`` turns
    into "not checked" rather than passing over in silence.
    """
    reports = {}
    for page, filename in RESULT_FILE_BY_PAGE.items():
        reports[page] = None
        try:
            path = os.path.join(pm.get_data_dir(profile_name), filename)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    reports[page] = json.load(f)
        except Exception:
            logger.warning(f"Could not read {filename} for profile "
                           f"{profile_name or 'default'}", exc_info=True)
    return reports


def exit_code_for(status: str) -> int:
    """Map a health status onto a process exit code.

    DEGRADED is non-zero on purpose. A watchdog that only trips on total
    breakage cannot drive a scheduled check, because the state worth acting on
    is the one where something has just started to slip.
    """
    if status == "BROKEN":
        return pm.EXIT_ERROR
    if status == "DEGRADED":
        return pm.EXIT_DEGRADED
    return pm.EXIT_OK


def _print_summary(result: Dict):
    print("\n" + "=" * 60)
    print(f"  SELECTOR HEALTH: {result['status']}  "
          f"[page={result.get('page', 'feed')} source={result.get('source', 'live')}]")
    print("=" * 60)
    for key, c in result["checks"].items():
        mark = "✓" if c["ok"] else "✗"
        print(f"  {mark} {key:24s} count={c['count']:>3} (min {c['min_expected']}) "
              f"{'[critical]' if c['critical'] else ''}")
    skipped = sorted(
        set(registry_for_page(result.get("page", "feed"), include_gated=True))
        - set(result["checks"])
    )
    for key in skipped:
        print(f"  – {key:24s} {gate_reason(SELECTOR_REGISTRY[key])}")
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
    """CLI entry point.

    Exit codes: 0 HEALTHY, 1 BROKEN or error, 2 login required, 3 DEGRADED.
    """
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
    parser.add_argument("--post-url", default=None,
                        help="Check the POSTING path's selectors against this post permalink. "
                             "Read-only: it never opens the comment box and never posts")
    parser.add_argument("--fixture", default=None,
                        help="Check a SAVED HTML file instead of a live page. No browser and no "
                             "LinkedIn session; this is how the gate runs in CI")
    parser.add_argument("--page", default=None, choices=list(PAGES),
                        help="Which registry page a --fixture run should check (default: feed)")
    parser.add_argument("--include-gated", action="store_true",
                        help="Also check the interaction-gated selectors (the comment editor, "
                             "the submit button, the Copy-link item). Only meaningful against a "
                             "--fixture captured with that state already open")
    parser.add_argument("--json", action="store_true", help="Print the JSON result to stdout")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    try:
        if args.fixture:
            # Deliberately before auto_migrate_from_env and before any profile
            # lookup: a fixture run must work on a machine that has never had a
            # LinkedIn session, which is the whole point of it.
            result = run_fixture_health_check(args.fixture, page=args.page or "feed",
                                              include_gated=args.include_gated)
        else:
            pm.auto_migrate_from_env()
            if args.search_url:
                result = run_search_health_check(args.profile, args.search_url,
                                                 scrolls=args.scrolls)
            elif args.post_url:
                result = run_post_health_check(args.profile, args.post_url)
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

    return exit_code_for(result["status"])


if __name__ == "__main__":
    sys.exit(main())
