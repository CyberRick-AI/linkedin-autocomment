"""selector_probe.py — count what every selector the codebase relies on actually matches.

The repair tool. When LinkedIn changes its DOM the symptom is silence: no error,
no exception, just zero posts scraped or zero comments placed. Guessing which of
forty-odd selectors went stale is what turned 2026-07-31 into a five-hour
debugging session. A throwaway script that navigated to one post and printed
match counts located the fix in minutes. This is that script, kept.

It answers one question per row: *this selector, on this page, matches N
elements.* Then it lists the stable-looking hooks the page does carry, so a
replacement is chosen from what is there rather than from memory.

It never types, never submits, and never sends anything. With ``--open-box`` it
clicks the action-bar Comment button so the two interaction-gated selectors can
be counted at all; that is the only click it ever makes.

Usage:
    # Offline. No browser, no LinkedIn session. Works anywhere.
    python tools/selector_probe.py --fixture tests/fixtures/post_healthy.html --page post

    # Live. Needs a logged-in session, so this one is Rick's to run.
    python tools/selector_probe.py --url https://www.linkedin.com/feed/update/urn:li:activity:123/
    python tools/selector_probe.py --url <post url> --open-box   # also counts the editor + submit button
    python tools/selector_probe.py --url <people search url> --page search

Exit codes:
    0  HEALTHY
    1  BROKEN (a critical selector matched nothing) or an error
    2  login required
    3  DEGRADED (a non-critical selector matched nothing)
"""

import argparse
import json
import logging
import sys
import time

# Make `import linkedin_automation` resolve when run as `python tools/selector_probe.py`
# (project root is this file's grandparent directory).
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from linkedin_automation import dom_probe
from linkedin_automation import profile_manager as pm
from linkedin_automation import selector_health as shc

logger = logging.getLogger(__name__)

# Hooks worth trying first when a selector goes stale. Ordered by how well they
# have survived: LinkedIn ships hashed class names that change between deploys,
# while data-testid and data-view-name have outlived every break so far.
CANDIDATE_KINDS = ("data_testids", "data_view_names", "button_aria_labels")


def probe_html(html: str, page: str = "feed") -> dict:
    """Count every registry selector for ``page`` against a saved document.

    Interaction-gated entries are INCLUDED here, unlike in the health gate. The
    probe is a diagnostic, not a pass/fail check: when you are staring at a
    capture taken with the comment box open, the editor and submit button are
    exactly the rows you need.
    """
    counter = dom_probe.make_counter(html)
    registry = shc.registry_for_page(page, include_gated=True)

    rows = []
    for key, spec in registry.items():
        is_xpath = bool(spec.get("xpath"))
        counts, unsupported = {}, {}
        for selector in spec["selectors"]:
            try:
                counts[selector] = counter(selector, xpath=is_xpath)
            except dom_probe.UnsupportedSelector as e:
                # Loudly, never as a zero. A selector this engine cannot parse
                # is an unknown, and an unknown reported as "0 matches" is how a
                # probe starts lying about the thing it exists to check.
                counts[selector] = None
                unsupported[selector] = str(e)
        best = max((n for n in counts.values() if n is not None), default=0)
        rows.append({
            "key": key,
            "page": page,
            "critical": bool(spec.get("critical")),
            "min_expected": spec.get("min_expected", 0),
            "xpath": is_xpath,
            "gated": bool(shc.gate_reason(spec)),
            "gate_reason": shc.gate_reason(spec),
            "count": best,
            "ok": best >= spec.get("min_expected", 0),
            "counts": counts,
            "unsupported": unsupported,
            "fix_symbol": spec.get("fix_symbol", ""),
            "note": spec.get("note", ""),
        })

    hooks = dom_probe.harvest_hooks(html)
    return {
        "page": page,
        "schema_version": shc.SCHEMA_VERSION,
        "rows": rows,
        "hooks": hooks,
        "candidates": dom_probe.candidate_selectors(hooks),
    }


def status_of(probe: dict) -> str:
    """HEALTHY / DEGRADED / BROKEN over the rows the page could actually answer.

    Gated rows that matched nothing are excluded: on a page where the comment
    box was never opened, a zero for the editor means "not opened", not
    "broken". Counting it as a failure would train the reader to ignore reds.
    """
    judged = [r for r in probe["rows"] if not (r["gated"] and r["count"] == 0)]
    if any(not r["ok"] and r["critical"] for r in judged):
        return "BROKEN"
    if any(not r["ok"] for r in judged):
        return "DEGRADED"
    return "HEALTHY"


def format_table(probe: dict, status: str) -> str:
    """Render the probe as a plain-text table."""
    lines = [
        "",
        "=" * 78,
        f"  SELECTOR PROBE — page={probe['page']}  status={status}",
        "=" * 78,
    ]
    for row in probe["rows"]:
        if row["gated"] and row["count"] == 0:
            verdict = "SKIP"
        elif row["ok"]:
            verdict = "OK"
        else:
            verdict = "FAIL" if row["critical"] else "warn"
        flags = []
        if row["critical"]:
            flags.append("critical")
        if row["xpath"]:
            flags.append("xpath")
        if row["gated"]:
            flags.append("gated")
        lines.append("")
        lines.append(f"[{verdict:4s}] {row['key']}  "
                     f"({', '.join(flags) or 'optional'})  "
                     f"best={row['count']} min={row['min_expected']}")
        for selector, n in row["counts"].items():
            if n is None:
                lines.append(f"         ?   {selector}   "
                             f"<- UNSUPPORTED: {row['unsupported'][selector]}")
            else:
                lines.append(f"       {n:>3}   {selector}")
        if verdict == "SKIP":
            lines.append(f"         -> {row['gate_reason']}")
        elif verdict != "OK":
            lines.append(f"         -> edit {row['fix_symbol']}")

    lines += ["", "-" * 78, "  Hooks present on this page (candidate replacements)", "-" * 78]
    for kind in CANDIDATE_KINDS:
        values = probe["hooks"].get(kind, [])
        lines.append(f"  {kind} ({len(values)}):")
        for value in values[:40]:
            lines.append(f"      {value}")
        if len(values) > 40:
            lines.append(f"      ... and {len(values) - 40} more")
    lines.append("")
    return "\n".join(lines)


def probe_live(url: str, page: str, profile_name: str = None,
               open_box: bool = False, scrolls: int = 0) -> dict:
    """Load ``url`` in the profile's Chrome session and probe the rendered DOM.

    **Needs a live LinkedIn session, so this is Rick's to run.** Claude Code
    writes and unit-tests this function and never executes it. See PROJECT.md
    section 4.
    """
    driver, _profile = pm.create_driver(profile_name)
    try:
        logger.info(f"Loading {url}")
        driver.get(url)
        time.sleep(5)

        if not pm.is_logged_in_on_page(driver):
            raise pm.LoginRequiredError(
                f"LinkedIn login failed for profile '{profile_name or 'default'}'. "
                f"Run: python tools/login_check.py --profile {profile_name or 'default'}"
            )

        for _ in range(scrolls):
            driver.execute_script("window.scrollBy(0, 1000);")
            time.sleep(2)

        if open_box:
            _open_comment_box(driver)

        probe = probe_html(driver.page_source, page=page)
        probe["url"] = url
        probe["profile"] = profile_name
        probe["source"] = "live"
        probe["box_opened"] = bool(open_box)
        return probe
    finally:
        driver.quit()


def _open_comment_box(driver):
    """Click the action-bar Comment button so the gated selectors exist.

    The single click this tool ever makes. It opens the editor; it does not type
    and does not submit. Without it the two selectors most likely to be the
    cause of a posting failure cannot be counted at all, which is the position
    the 2026-07-31 debugging session started from.
    """
    from selenium.webdriver.common.by import By
    from linkedin_automation.comment_poster import LinkedInCommentPoster as P

    for selector in P.COMMENT_BUTTON_LABEL_SELECTORS:
        for button in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
                time.sleep(0.3)
                button.click()
                time.sleep(2)
                logger.info(f"Opened the comment box via {selector}")
                return True
            except Exception:
                logger.debug(f"Could not click {selector}", exc_info=True)
    logger.warning("Could not open the comment box; gated selectors stay uncounted")
    return False


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Count what every LinkedIn selector the codebase relies on actually matches")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Live page to probe (needs a logged-in session)")
    source.add_argument("--fixture", help="Saved HTML file to probe (no browser, no session)")
    parser.add_argument("--page", default=None, choices=list(shc.PAGES),
                        help="Which registry to check (default: inferred from the URL, else feed)")
    parser.add_argument("--profile", default=None, help="Profile name for the live run")
    parser.add_argument("--scrolls", type=int, default=0, help="Scrolls before probing a live page")
    parser.add_argument("--open-box", action="store_true",
                        help="Live only: click the Comment button so the editor and submit "
                             "button can be counted. Types nothing, submits nothing")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the table")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    page = args.page or (infer_page(args.url) if args.url else "feed")

    try:
        if args.fixture:
            with open(args.fixture, "r", encoding="utf-8") as f:
                probe = probe_html(f.read(), page=page)
            probe["fixture"] = args.fixture
            probe["source"] = "fixture"
        else:
            probe = probe_live(args.url, page, profile_name=args.profile,
                               open_box=args.open_box, scrolls=args.scrolls)
    except pm.LoginRequiredError as e:
        print(f"\n{e}")
        return pm.EXIT_LOGIN_REQUIRED
    except Exception as e:
        print(f"\nProbe failed: {e}")
        return pm.EXIT_ERROR

    status = status_of(probe)
    probe["status"] = status

    if args.json:
        print(json.dumps(probe, indent=2))
    else:
        print(format_table(probe, status))

    return shc.exit_code_for(status)


def infer_page(url: str) -> str:
    """Guess which registry a URL belongs to. Explicit --page always wins."""
    lowered = (url or "").lower()
    if "/search/results/people" in lowered:
        return "search"
    if "/feed/update/" in lowered or "/posts/" in lowered or "urn:li:activity" in lowered:
        return "post"
    return "feed"


if __name__ == "__main__":
    sys.exit(main())
