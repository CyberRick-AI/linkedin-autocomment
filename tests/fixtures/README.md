# tests/fixtures — saved DOM shapes for the offline selector gate

Every file here is **hand-authored and synthetic**. No real name, profile URL, or
post text from a live feed appears in any of them, per `PROJECT.md` section 8.
They imitate the *shape* of LinkedIn's DOM, which is the only part the selectors
care about.

They exist so the selector watchdog has a gate that runs with no browser, no
network, and no LinkedIn session. Before Phase 11b the only way to find out
whether a selector still matched was to run the tool against the live site,
which meant the check could only ever be run by Rick, after the breakage.

| File | Page | Expected result |
|---|---|---|
| `feed_healthy.html` | feed | `HEALTHY`, exit 0 |
| `feed_degraded.html` | feed | `DEGRADED`, exit 3 — a non-critical hook renamed |
| `feed_broken.html` | feed | `BROKEN`, exit 1 — the critical post container renamed |
| `post_healthy.html` | post | `HEALTHY`, exit 0 — permalink as loaded, comment box closed |
| `post_box_open.html` | post | the same page with the comment box open, so the two interaction-gated selectors can be counted |

Each variant states, in an HTML comment at the top, exactly which hook was
renamed relative to `feed_healthy.html`. Keep that comment accurate: it is the
only thing that explains why a fixture is expected to fail.

`post_box_open.html` carries the trap that broke posting on 2026-07-31. It has
**two** buttons involving the word Comment: the action-bar button with
`aria-label="Comment"` whose visible text is a count, and the submit button with
no aria-label whose visible text is `Comment`. A check that cannot tell them
apart reports success on the wrong one.
