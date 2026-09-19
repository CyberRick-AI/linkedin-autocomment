"""Run the X finder against a live X timeline — the go/no-go runner.

The offline suite proves the parser handles saved DOM. This proves the saved DOM
still resembles what X serves today, which no fixture can. Two jobs, in order:

1. **Selector drift check.** Count every ``page="x_timeline"`` registry entry
   against the LIVE page and report any that no longer match. The fixtures were
   shaped from captures taken 2026-08-23..28; if X has rotated its DOM since,
   this is where it shows, and a miss here is a re-harvest signal rather than a
   finder bug.
2. **The scrape itself.** Parse, analyse, and (only with ``--write``) store.

DRY RUN IS THE DEFAULT. A first live run should look before it writes, so
nothing touches the store until you pass ``--write``.

ONLY THE CONFIRMED TIMELINE PATH RUNS HERE. Search mode is not wired up: the
search dump was blocked by the scrubber at capture time and never saved, so
``x_finder.parse_search_results`` has no real DOM behind it. The follow button
and the profile page are likewise unexercised — none of them is on the read path.

The account guard is x_dump's, unchanged: this refuses to drive anything not
explicitly marked ``"platform": "x"``, because a profile that merely exists is a
LinkedIn profile for a real person.

    # check the session first (touches nothing)
    .venv\\Scripts\\python.exe tools\\x_dump.py --preflight
    .venv\\Scripts\\python.exe tools\\x_dump.py --profile xtest --login-only

    # look, don't touch
    .venv\\Scripts\\python.exe tools\\run_x_finder.py --profile xtest

    # commit the harvest to the X store
    .venv\\Scripts\\python.exe tools\\run_x_finder.py --profile xtest --write
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import selector_health as shc          # noqa: E402
from linkedin_automation import x_finder as xf                  # noqa: E402

import importlib.util as _ilu                                   # noqa: E402

# x_dump lives in tools/ and is not a package module, so it is loaded by path
# rather than imported. Reusing it is the point: the account guard, the session
# directory layout and the driver flags must be identical to the harvest's, or
# this would be a second, subtly different way to open the same account.
_spec = _ilu.spec_from_file_location(
    "x_dump", os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_dump.py"))
x_dump = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(x_dump)


TIMELINE_PAGE = "x_timeline"


def live_counter(driver):
    """``count(selector) -> int`` against the live DOM.

    Same one-argument shape as :func:`dom_probe.make_counter`, so
    :func:`selector_health.check_registry` runs unchanged against a browser or a
    fixture without knowing which it has.
    """
    def count(selector: str) -> int:
        try:
            return len(driver.find_elements(shc.selector_by(selector), selector))
        except Exception:
            return 0
    return count


def check_live_selectors(driver) -> dict:
    """Run the x_timeline registry against the live page."""
    registry = {k: v for k, v in shc.SELECTOR_REGISTRY.items()
                if v.get("page") == TIMELINE_PAGE}
    return shc.check_registry(live_counter(driver), registry)


def report_selectors(check: dict) -> int:
    """Print the drift report. Returns the number of entries that missed."""
    status = shc.overall_status(check)
    print("\n─── live selector check (x_timeline) ─────────────────────────")
    print("  status: %s" % status)
    missed = 0
    for key in sorted(check):
        c = check[key]
        mark = "ok " if c["ok"] else "MISS"
        if not c["ok"]:
            missed += 1
        crit = "critical" if c["critical"] else "        "
        print("  [%s] %-26s %-8s count=%-4d min=%d"
              % (mark, key, crit, c["count"], c["min_expected"]))
    if missed:
        print("\n  %d entry/entries no longer match the live DOM." % missed)
        print("  That is a RE-HARVEST signal, not a finder bug: the selectors")
        print("  were confirmed against captures from 2026-08-23..28. Re-run")
        print("  tools/x_dump.py --profile <p> --pages timeline_foryou and")
        print("  compare the testids before changing any parser code.")
    return missed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the X finder against a live timeline (go/no-go)")
    parser.add_argument("--profile", default=None,
                        help="X TEST profile name (must be marked platform=x)")
    parser.add_argument("--scrolls", type=int, default=3,
                        help="scrolls; X virtualizes, so it reads after each one")
    parser.add_argument("--pause", type=float, default=2.0,
                        help="seconds to wait after each scroll for X to render")
    parser.add_argument("--write", action="store_true",
                        help="write to the X store (default: dry run)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--limit", type=int, default=12,
                        help="how many parsed posts to print")
    args = parser.parse_args(argv)

    # Same guard as every x_dump live mode.
    name, info = x_dump.resolve_x_test_profile(args.profile)
    if name is None:
        print("refusing to run: %s" % info)
        return 2

    print("X finder live run — profile %r%s"
          % (name, "" if args.write else "  (DRY RUN, nothing is stored)"))

    driver = x_dump.create_x_driver(name, headless=args.headless)
    try:
        driver.get(xf.XTimelineFinder.TIMELINE_URL)
        x_dump.wait_for_app(driver)

        state, evidence = x_dump.login_state(driver)
        print("  login state: %s" % state)
        if state != "logged_in":
            print("  evidence: %s" % evidence)
            print("\n  NOT LOGGED IN. Re-establish the session first:")
            print("    .venv\\Scripts\\python.exe tools\\x_dump.py "
                  "--profile %s --manual-login" % name)
            return 3

        missed = report_selectors(check_live_selectors(driver))

        finder = xf.XTimelineFinder(driver, analyzer=None)
        posts = finder.collect(scrolls=args.scrolls,
                               pause=lambda: time.sleep(args.pause))

        print("\n─── parsed %d posts ──────────────────────────────────────" % len(posts))
        for post in posts[:args.limit]:
            print("  %s" % post.url)
            print("      @%-16s engage=%-5s score=%-3d  r/rt/l=%d/%d/%d"
                  % (post.author_handle or "(x-route)", post.should_engage,
                     post.relevance_score, post.replies, post.reposts, post.likes))
        if len(posts) > args.limit:
            print("  ... %d more" % (len(posts) - args.limit))

        # Permalinks are the whole read path. Say plainly whether they came out.
        addressable = [p for p in posts if p.url.startswith("https://x.com/")]
        print("\n  addressable permalinks: %d/%d" % (len(addressable), len(posts)))
        relevant = [p for p in posts if p.should_engage]
        print("  relevant (would land NEW): %d" % len(relevant))

        if not args.write:
            print("\n  DRY RUN — nothing written. Re-run with --write to store.")
            return 0 if (posts and not missed) else 1

        store = xf.open_store(name)
        before = len(store.posts)
        summary = xf.harvest_to_store(posts, store)
        print("\n─── store ────────────────────────────────────────────────")
        print("  %s" % store.path)
        print("  records: %d -> %d   summary=%s" % (before, len(store.posts), summary))
        return 0 if (posts and not missed) else 1
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
