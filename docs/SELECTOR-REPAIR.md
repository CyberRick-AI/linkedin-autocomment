# Repairing a broken selector

LinkedIn changes its DOM every few months. When it does, this tool does not
crash: it finds nothing and reports success at finding nothing. That silence is
the whole problem, and this document is the loop that ends it.

Read [MAINTENANCE.md](MAINTENANCE.md) for the wider picture. This file is the
narrow one: a selector stopped matching, and you have to find out which and
replace it.

---

## Who does which step

Some steps need a logged-in LinkedIn session. Those cannot be automated and
cannot be delegated to Claude Code, which never authenticates to LinkedIn,
never scrapes, and never posts. Every step is marked.

| | Step | Needs a live session? |
|---|---|---|
| 1 | Recognise the symptom | no |
| 2 | Run the offline check first | no |
| 3 | Capture the page | **yes — Rick** |
| 4 | Probe the capture | no |
| 5 | Choose the replacement | no |
| 6 | Edit the constant | no |
| 7 | Prove the fix on the capture | no |
| 8 | Confirm on the live site | **yes — Rick** |
| 9 | Commit, and add a fixture | no |

Steps 3 and 8 are the only two that need an account. Everything between them is
ordinary offline work on a saved file, which is the point of capturing one.

---

## 1. Recognise the symptom

Selector breakage does not look like an error. It looks like nothing happening.

| Symptom | Likely path |
|---|---|
| Scrape returns 0 posts, or a large `TRASH(no_url)` pile | feed |
| `Post content not found on page`, and each post takes ~80 seconds to fail | post |
| Comments are typed and then `Posted 0, skipped 3` | post, submit button |
| Connector finds no one on a search that clearly has results | search |
| Any of the above, while `selector_health` reports `HEALTHY` | see below |

That last row is the one to distrust most. Until Phase 11 the registry only
covered feed scraping, so a posting failure produced a green light. It now
covers all three paths, and a report that only checked one of them reads
`INCOMPLETE` rather than `HEALTHY`. **If the top-line status is `INCOMPLETE`,
the thing you care about may simply not have been looked at.**

---

## 2. Run the offline check first

Costs nothing and needs no session. Confirms the tooling itself is sound before
you go looking at LinkedIn:

```bash
python -m linkedin_automation.selector_health --fixture tests/fixtures/feed_healthy.html
python -m linkedin_automation.selector_health --fixture tests/fixtures/post_healthy.html --page post
```

Both must print `HEALTHY` and exit 0. If either does not, the break is in this
repository, not on LinkedIn, and the rest of this document does not apply.

Then look at what the dashboard already knows:

```
Scrape panel -> Full Report
```

or

```bash
curl -s localhost:6500/api/health/<profile>/report | python -m json.tool
```

This runs nothing. It merges the saved per-page results and lists every
selector in the registry, marking the ones that were never checked and saying
why. Read it before capturing anything: the answer may already be there.

---

## 3. Capture the page — **Rick**

You need the DOM as LinkedIn is serving it *to your account*, which means a real
session. Two ways, and the first is better because it is one command:

```bash
# Feed
python -m linkedin_automation.selector_health --profile rick

# One post permalink. Read-only: loads the page and counts. It does not open
# the comment box and cannot post.
python -m linkedin_automation.selector_health --profile rick \
    --post-url "https://www.linkedin.com/feed/update/urn:li:activity:.../"
```

When anything fails, the feed run writes `selector_debug_dump.html` (the first
few post containers) and, on `BROKEN`, `.dev/SELECTOR_FIX_NEEDED.md` plus a
screenshot.

For the posting path, and especially for the **submit** button, use the probe
with `--open-box`. That clicks the action-bar Comment button so the editor and
submit button exist at all. It types nothing and submits nothing:

```bash
python tools/selector_probe.py --profile rick --open-box \
    --url "https://www.linkedin.com/feed/update/urn:li:activity:.../"
```

To hand the page to someone else, or to keep it, save it:

```bash
python tools/selector_probe.py --profile rick --open-box --url "<post url>" --json > probe.json
```

and in the browser that opened, **Save Page As -> Webpage, Complete** into a
scratch directory. A saved copy turns every later step into offline work.

> Anything saved from a live feed contains real names, real post text, and real
> profile URLs. Keep it out of the repository. Only hand-authored synthetic
> fixtures belong in `tests/fixtures/`.

---

## 4. Probe the capture

No session needed from here on.

```bash
python tools/selector_probe.py --fixture /path/to/saved.html --page post
```

The probe prints one row per registry entry and one line per selector inside
it, with the match count. This is the step that turns hours into minutes: you
stop guessing which of forty selectors went stale and read it.

```
[FAIL] post_detail  (critical)  best=0 min=1
         0   span[data-testid='expandable-text-box']
         0   div[role='listitem']
         0   div.occludable-update
         ...
         -> edit LinkedInCommentPoster.POST_DETAIL_SELECTORS
```

Then read the bottom of the output. It lists the `data-testid`,
`data-view-name` and button `aria-label` values the page actually carries.
Those are your candidates, taken from the page in front of you rather than from
memory.

A row can also read `? <selector> <- UNSUPPORTED`. That means the offline
engine could not parse the selector, **not** that the page lacks it. Check that
one live, or extend `linkedin_automation/dom_probe.py`. It is deliberately loud:
a probe that answered "0 matches" for a selector it did not understand would be
lying in the direction that costs the most.

---

## 5. Choose the replacement

In order of how well they have survived:

1. `data-testid` — has outlived every break so far
2. `data-view-name` — durable, though LinkedIn has removed individual ones
3. `role` and `aria-label` — stable, because accessibility tooling depends on them
4. visible text via XPath — for buttons, when nothing else distinguishes them
5. class names — **last resort.** LinkedIn ships hashed classes that change
   between deploys. A class-based selector is a fix with an expiry date.

Two rules learned the hard way:

**Do not pick a selector that matches on pages you do not want.** `main` matches
a post permalink, and it also matches LinkedIn's error page, so it would report
that a post had loaded when nothing had. Prefer a selector that can only be true
when the thing you need is really there.

**Add the new selector first and keep the old ones as fallbacks.** Once a
working selector matches, the rest cost nothing, and they keep the tool working
for anyone on an older DOM.

---

## 6. Edit the constant

Selectors live in class constants, never inline in a method body. That is not
style: the health registry is built from those constants so it cannot drift from
what the code actually uses, and a literal buried in a method is invisible to
it. Inlining one silently removes it from monitoring, so a test asserts the
registry lists *are* the constants and fails if you do.

| Path | Class | Constants |
|---|---|---|
| feed | `LinkedInScraper` (`post_finder.py`) | `POST_SELECTORS`, `TEXT_SELECTORS`, `AUTHOR_SELECTORS`, `CONTROL_MENU_SELECTOR`, `MENU_ITEM_SELECTORS`, `SCROLL_CONTAINER_SELECTORS` |
| post | `LinkedInCommentPoster` (`comment_poster.py`) | `POST_DETAIL_SELECTORS`, `LIKE_BUTTON_SELECTORS`, `COMMENT_BUTTON_LABEL_SELECTORS`, `COMMENT_INPUT_SELECTORS`, `SUBMIT_BUTTON_XPATH` |
| search | `LinkedInAutoConnector` (`auto_connector.py`) | `CONNECT_LINK_SELECTORS`, `SEARCH_RESULT_SELECTOR`, `RESULT_NAME_SELECTOR`, `PAGINATION_NEXT_SELECTORS`, `SEND_BUTTON_SELECTORS` |

The probe names the constant to edit in its `-> edit ...` line, so you should
not have to grep for it.

---

## 7. Prove the fix on the capture

```bash
python tools/selector_probe.py --fixture /path/to/saved.html --page post
python -m pytest tests/ -q && python -m ruff check .
```

The failing rows should now read `OK`. Still no session required.

---

## 8. Confirm on the live site — **Rick**

An offline pass proves the selector matches the page you saved. It does not
prove the element is the one the code should click.

```bash
python -m linkedin_automation.selector_health --profile rick
python -m linkedin_automation.selector_health --profile rick --post-url "<post url>"
```

Then run the real thing once, on a single item, and look at the result on
LinkedIn. On the posting path especially: matching the right button and clicking
the right button are different claims, and only the second one posts a comment.

Exit codes, so this can drive a scheduled check:

| Code | Meaning |
|---|---|
| 0 | `HEALTHY` |
| 1 | `BROKEN`, or the run failed |
| 2 | login required |
| 3 | `DEGRADED` |

---

## 9. Commit, and add a fixture

Record in the commit message what you observed, not just what you changed. The
counts you saw are the evidence, and they are what the next person needs:

```
0  div.occludable-update
0  div.feed-shared-update-v2
1  span[data-testid='expandable-text-box']
```

Then ask whether the break could have been caught offline. If a small
hand-authored fixture in `tests/fixtures/` would have failed before the fix,
add one. It costs a few lines and converts a class of silent breakage into a
red test on every push.

Do not commit anything captured from the live feed.

---

## Worked example: the two fixes of 2026-07-31

One evening, two breaks, same root cause: LinkedIn had relabelled the post
permalink page. It took about five hours, almost all of it spent on step 4
before there was a tool for step 4. This is what happened, and what the loop
above does with it now.

### Symptom

`Posted 0, skipped 3`, on every run. Each post took roughly 80 seconds to fail
and logged `Post content not found on page`. Scraping was fine. Then
`selector_health` was run and reported `HEALTHY`.

That combination is the whole story: the check covered the feed, the feed was
genuinely fine, and the failure was somewhere the check had never looked.

### Break 1 — the permalink page

`navigate_to_post` waited for one of four selectors to prove the page had
rendered. All four were legacy class names LinkedIn no longer emits, so all four
`WebDriverWait` calls ran to their 20-second timeout. Four times twenty seconds
is the 80 seconds per post.

Counting candidates against a live permalink gave this immediately:

```
0  div.occludable-update
0  div.feed-shared-update-v2
0  article.feed-shared-article
0  div[data-urn*='activity']
1  span[data-testid='expandable-text-box']
1  div[role='listitem']
```

`post_finder` had already been migrated to `data-testid` selectors, which is
exactly why scraping worked and posting did not. The two verified selectors went
to the front of the list and the four legacy ones stayed as fallbacks. `main`
was rejected despite matching, because it also matches LinkedIn's error page.

**Today:** `python tools/selector_probe.py --fixture saved.html --page post`
prints that table, and `post_detail` is a registered critical entry, so the same
break now reports `BROKEN` and exits 1.

### Break 2 — the submit button

With break 1 fixed, posting reached the comment box, typed the comment, and then
did nothing. Two causes, both from the same relabelling:

**Every submit selector was a class name** —
`comments-comment-box__submit-button--cr`, `artdeco-button--primary`. LinkedIn
now ships hashed class names that change between deploys, so no class-based
selector could survive. The fix locates the button by visible text: exactly one
button on a post page reads `Comment`.

**And the guard excluded the button it needed.** The code skipped any button
whose text was `Comment`, commented `Skip comment opener`. That was correct when
the submit button read `Post`. After the relabel, the opener and the submitter
both involved the word Comment, and the guard was now skipping the submit
button itself.

The two are told apart by *where* the word sits:

| Button | `aria-label` | Visible text | Does |
|---|---|---|---|
| action bar | `Comment` | the comment count, e.g. `39` | opens the box |
| submit | none | `Comment` | posts the comment |

So `SUBMIT_BUTTON_XPATH` is `//button[normalize-space(.)='Comment']`, matched on
visible text, and the opener is keyed on `aria-label`. Verified end to end
afterwards: `Found submit button: 'Comment'`, then `Posted 1, skipped 0, of 3
parsed`.

**Today:** both buttons are registered entries. `post_submit_button` carries
`xpath: True`, because counting it with a CSS engine returns zero with total
confidence, and it carries `requires_interaction: True`, because it does not
exist until the box is opened, so a passive check reports it as *not checked*
rather than as fine. `tests/fixtures/post_box_open.html` contains both buttons
and a test asserts the XPath finds one and not the other.

### What the episode actually taught

The five hours did not go on the fix. Each fix was one line. They went on not
knowing which selector had gone stale, and the throwaway script that finally
answered it in minutes is now `tools/selector_probe.py`, kept precisely so that
this is never re-derived.

The second lesson is sharper, and it is why `INCOMPLETE` exists: a monitor that
does not cover the risky path is worse than no monitor. `selector_health` said
`HEALTHY` while three comments failed to post, and it was telling the truth
about the only thing it had been asked. A green light on a question nobody asked
reads exactly like a green light on the question they did.
