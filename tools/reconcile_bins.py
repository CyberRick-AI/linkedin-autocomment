"""One-off diagnostic: reconcile a profile's lifecycle store and print bin counts.

The dashboard's lifecycle bins (NEW / GENERATED / COMMENTED / TRASH) can drift —
a post stays NEW even though it was already commented on (URL in
posting_progress.json) or already had a draft generated (present in a
comments_*.json / ready_*.json file). This loads the store, reconciles it
against those authoritative files, and prints the before/after counts so you can
confirm the true state.

Usage:
    uv run python tools/reconcile_bins.py --profile jeff
    uv run python tools/reconcile_bins.py --profile jeff --dry-run   # don't save
"""

import argparse

# Make `import linkedin_automation` resolve when run as `python tools/reconcile_bins.py`.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from linkedin_automation import post_store


def main():
    """Parse args, reconcile the profile's store, and print before/after bins."""
    parser = argparse.ArgumentParser(description="Reconcile + print lifecycle bin counts")
    parser.add_argument("--profile", default=None, help="Profile name (default profile if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what reconciliation would do without saving")
    args = parser.parse_args()

    store = post_store.PostStore(args.profile)
    before = dict(store.counts())

    if args.dry_run:
        # Reconcile a throwaway copy so nothing is written to disk.
        import copy
        result = post_store.PostStore(args.profile, path=store.path + ".dryrun")
        result.data = copy.deepcopy(store.data)
        result.save = lambda: None  # type: ignore[assignment]
    else:
        result = store
    stats = {}
    post_store.reconcile(args.profile, store=result, stats=stats)

    after = dict(result.counts())
    after_reasons = result.trash_reason_counts()
    new_recs = result.get_posts_by_status(post_store.NEW)
    actionable_new = sum(1 for r in new_recs if r.get("url"))

    print("\nLifecycle bins for profile:", args.profile or "(default)")
    print("  store file:", store.path)
    print("  mode:", "DRY RUN (not saved)" if args.dry_run else "reconciled + saved")
    print("\n  {:<12} {:>8} {:>8} {:>8}".format("bin", "before", "after", "delta"))
    print("  " + "-" * 40)
    for status in post_store.STATUSES:
        b, a = before.get(status, 0), after.get(status, 0)
        print("  {:<12} {:>8} {:>8} {:>+8}".format(status, b, a, a - b))
    print("  " + "-" * 40)
    print("  {:<12} {:>8} {:>8}".format("TOTAL", sum(before.values()), sum(after.values())))

    print("\n  Reconciliation actions:")
    print(f"    NEW -> COMMENTED (already posted):     {stats.get('commented', 0)}")
    print(f"    NEW -> GENERATED (draft on disk):      {stats.get('generated_from_files', 0)}")
    print(f"    GENERATED draft recovered from file:   {stats.get('recovered_drafts', 0)}")
    print(f"    GENERATED -> NEW (draft unrecoverable): {stats.get('demoted_generated', 0)}")
    print(f"    NEW -> TRASH (no_url):                 {stats.get('trashed_no_url', 0)}")

    if after_reasons:
        print("\n  TRASH by reason:")
        for reason, n in sorted(after_reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {reason:<14} {n:>6}")

    print(f"\n  NEW posts, all with a URL and actionable by 'Generate': {actionable_new}")
    if actionable_new != after.get(post_store.NEW, 0):
        # Should not happen after reconcile (url-less NEW are trashed); flag if so.
        print(f"  (WARNING: {after.get(post_store.NEW, 0) - actionable_new} NEW post(s) "
              f"still have no URL — reconcile may not have run.)")
    print()


if __name__ == "__main__":
    main()
