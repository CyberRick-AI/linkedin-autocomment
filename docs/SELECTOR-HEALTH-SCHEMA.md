# Selector health JSON schema

`linkedin_automation/selector_health.py` writes machine-readable reports so a
scheduled check, the dashboard, or a future alert can act on them without
parsing console output. This file is the contract.

The schema is also **data in the code**: `selector_health.REPORT_SCHEMA` and
`CHECK_SCHEMA`, with `validate_report(report) -> [errors]`. A test validates
real reports against it, so this document cannot quietly drift from what the
tool writes.

- **Current version:** `schema_version: 1`
- **Compatibility:** adding a field does not bump the version. Readers must
  ignore keys they do not recognise. Removing a field or changing what one
  means does bump it.

---

## Two shapes, not one

| Shape | Written by | Covers |
|---|---|---|
| **Page report** | one run against one page | the entries the registry has for that page |
| **Combined report** | `build_health_report()` / `GET /api/health/<profile>/report` | the whole registry, across every page |

They answer different questions, and conflating them is the bug this phase
exists to fix. A page report says "the feed is fine". A combined report says
"the feed is fine and the posting path was never checked", which is the honest
answer when only the feed has run.

---

## Page report

Written to the profile's data directory, one file per page:

| Page | File |
|---|---|
| feed | `selector_health.json` |
| search | `selector_health_search.json` |
| post | `selector_health_post.json` |

```json
{
  "profile": "rick",
  "page": "feed",
  "source": "live",
  "schema_version": 1,
  "status": "DEGRADED",
  "timestamp": "2026-08-01T11:04:22.518233",
  "failed": ["scroll_container"],
  "checks": {
    "feed_container": {
      "ok": true,
      "count": 4,
      "matched_selector": "div[role='listitem']",
      "counts": {
        "div[data-testid='mainFeed'] div[role='listitem']": 0,
        "div[role='listitem']": 4
      },
      "critical": true,
      "min_expected": 3,
      "note": ""
    }
  }
}
```

### Top level

| Field | Type | Meaning |
|---|---|---|
| `profile` | string or null | Profile the run used. `null` for a fixture run. |
| `page` | `"feed"` \| `"search"` \| `"post"` | Which registry page was checked. |
| `source` | `"live"` \| `"fixture"` | Whether a browser or a saved HTML file produced it. |
| `schema_version` | int | See above. |
| `status` | `"HEALTHY"` \| `"DEGRADED"` \| `"BROKEN"` | Derived from `checks`, never set by hand. |
| `timestamp` | string | Local ISO-8601 at the time of the run. |
| `failed` | list of string | Keys in `checks` whose `ok` is false. Must agree with `checks`; `validate_report` enforces that, because a `failed` list that has drifted from its own data is worse than none. |
| `checks` | object | Key to check object, below. **Only entries the page could actually answer appear here.** |

### Check object

| Field | Type | Meaning |
|---|---|---|
| `ok` | bool | Best count reached `min_expected`. Always true when `min_expected` is 0. |
| `count` | int | Best count across the entry's selectors. |
| `matched_selector` | string or null | Which selector produced `count`. `null` when nothing matched. |
| `counts` | object | Every selector to its own count. This is the diagnostic field: it turns "the feed broke" into "two of eight fallbacks still match". |
| `critical` | bool | A failing critical entry means the path cannot work. |
| `min_expected` | int | Threshold for `ok`. |
| `note` | string | Free text from the registry, usually why an entry is unusual. |

### Optional top-level fields

Present only when relevant, and safe to ignore:

`fixture`, `search_url`, `post_url`, `logged_in`, `search_state`, `debug_dump`,
`diagnostics`, `suggested_selectors`, `fix_file`, `failure_screenshot`,
`result_file`.

### What is deliberately absent

An entry that could not be counted **does not appear in `checks` at all**. It is
not recorded as zero.

That is the rule the whole design turns on. A zero is a measurement: this page
does not have this element. An entry that was never evaluated is not a
measurement, and writing it as zero would either raise a false alarm or, worse,
let a check that ran on a fraction of the registry present itself as complete.
The combined report is where those entries surface, with their reasons.

---

## Combined report

Returned by `GET /api/health/<profile>/report`. Reads saved page reports; runs
nothing and needs no session.

```json
{
  "schema_version": 1,
  "overall": "INCOMPLETE",
  "complete": false,
  "pages": {
    "feed": {
      "ran": true,
      "status": "HEALTHY",
      "source": "live",
      "timestamp": "2026-08-01T11:04:22.518233",
      "entries": [
        {
          "key": "feed_container",
          "page": "feed",
          "critical": true,
          "status": "pass",
          "level": "green",
          "reason": "",
          "count": 4,
          "min_expected": 3,
          "matched_selector": "div[role='listitem']",
          "selectors": ["div[data-testid='mainFeed'] div[role='listitem']", "..."],
          "counts": {"div[role='listitem']": 4},
          "fix_symbol": "POST_SELECTORS",
          "note": ""
        }
      ]
    },
    "post": {"ran": false, "status": "NOT_RUN", "source": "none",
             "timestamp": null, "entries": ["..."]},
    "search": {"...": "..."}
  }
}
```

### `overall`

| Value | When |
|---|---|
| `BROKEN` | any critical entry failed |
| `DEGRADED` | a non-critical entry failed, none critical |
| `INCOMPLETE` | nothing failed, but some entry was never checked |
| `HEALTHY` | every entry in the registry was checked and passed |

`INCOMPLETE` is the value that matters. On 2026-07-31 a posting run placed zero
of three comments and the health check reported `HEALTHY` minutes later, because
its registry only covered the feed. It was not lying about the feed. It was
answering a narrower question than the one being asked. `INCOMPLETE` is that
distinction made visible, and `complete` is the same fact as a boolean.

### Entry

Every registry key appears exactly once across the three pages, checked or not.

| Field | Type | Meaning |
|---|---|---|
| `key` | string | Registry key. |
| `page` | string | Which page it lives on. |
| `critical` | bool | Path cannot work without it. |
| `status` | `"pass"` \| `"fail"` \| `"not_checked"` | |
| `level` | `"green"` \| `"amber"` \| `"red"` | What the dashboard renders. `pass` is green; a failing critical entry is red; a failing non-critical entry and anything `not_checked` are amber. |
| `reason` | string | Empty unless `not_checked`, then why. |
| `count`, `min_expected`, `matched_selector`, `counts` | | As in the page report. Zeroed when `not_checked`; read `status` first. |
| `selectors` | list of string | Every selector the entry tries, in order. |
| `fix_symbol` | string | The constant to edit. Every entry has one. |
| `note` | string | Registry note. |

### Reasons an entry is `not_checked`

| Reason | Cause |
|---|---|
| the comment box is not open | `requires_interaction`: the editor and submit button do not exist until the action-bar Comment button is clicked |
| the overflow menu is not open | `requires_menu_open`: the Copy-link item |
| Connect has not been clicked | `modal_only`: the connector's Send button, and the check never clicks Connect |
| that page has no saved run | the page was never checked for this profile. Needs a live LinkedIn session, and so is Rick's to run. |

---

## Exit codes

The CLI's exit code is part of the contract too, so a scheduled run can act
without parsing anything:

| Code | Meaning |
|---|---|
| 0 | `HEALTHY` |
| 1 | `BROKEN`, or the run itself failed |
| 2 | login required |
| 3 | `DEGRADED` |

`DEGRADED` is non-zero on purpose. A watchdog that only trips on total breakage
tells you after the run that mattered has already failed.

---

## Reading a report without a session

```bash
python -m linkedin_automation.selector_health --fixture tests/fixtures/feed_healthy.html --json
python tools/selector_probe.py --fixture tests/fixtures/post_box_open.html --page post --json
```

Both work with no browser, no network, and no LinkedIn account. See
[SELECTOR-REPAIR.md](SELECTOR-REPAIR.md) for the repair loop these feed.
