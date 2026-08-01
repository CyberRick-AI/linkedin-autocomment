"""pipeline_manifest.py — the declared inventory of automated pipeline steps.

Every automated step declares itself here **before** its collector or compute
function is written. The manifest is what lets a structural check answer two
questions that are otherwise invisible until something breaks in production:

* Does a step exist in the codebase with no manifest entry (drift)?
* Does an automated run touch a step that requires a live LinkedIn session?

That second question is the machine-checkable form of the project's operating
boundary: work that needs a real LinkedIn account belongs to a human, because
nobody else can verify its result.

The manifest describes intent, not behavior. It does not execute anything.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PipelineStep:
    """One automated step in the pipeline.

    Args:
        name: Unique identifier, e.g. ``scrape_feed``.
        module: Import path of the module that implements the step.
        entry_function: Callable invoked to run it.
        target: File or store written, relative to the profile's data dir.
        critical_columns: Keys that must be present on every written record.
        expected_min: Fewest records a healthy run produces, or None if unbounded.
        expected_max: Most records a healthy run produces, or None if unbounded.
        freshness_minutes: How recently the target must have been written for the
            step to count as having run, or None if freshness is meaningless.
        upstream: Steps that must run before this one.
        downstream: Steps that consume this one's output.
        data_source: Where the data comes from.
        known_failure_modes: What breaks, and how it presents.
        requires_linkedin_session: True when the step drives a logged-in LinkedIn
            session. These belong to Rick, never to Claude Code.
    """

    name: str
    module: str
    entry_function: str
    target: Optional[str]
    data_source: str
    requires_linkedin_session: bool
    critical_columns: List[str] = field(default_factory=list)
    expected_min: Optional[int] = None
    expected_max: Optional[int] = None
    freshness_minutes: Optional[int] = None
    upstream: List[str] = field(default_factory=list)
    downstream: List[str] = field(default_factory=list)
    known_failure_modes: List[str] = field(default_factory=list)


# ─── The declared steps ───────────────────────────────────────────────────────
# Seeded from the four steps that existed when the manifest was introduced.
# Add an entry here BEFORE writing a new collector or compute function.

STEPS: List[PipelineStep] = [
    PipelineStep(
        name="scrape_feed",
        module="linkedin_automation.post_finder",
        entry_function="main",
        target="posts_db.json",
        critical_columns=["url", "status", "extracted_at"],
        expected_min=1,
        expected_max=100,
        freshness_minutes=10,
        upstream=[],
        downstream=["generate_comments"],
        data_source="LinkedIn feed via Selenium",
        known_failure_modes=[
            "selector rot: zero records written on an apparently successful run",
            "clipboard intercept miss: large TRASH(no_url) pile, a partial "
            "failure that looks like success",
        ],
        requires_linkedin_session=True,
    ),
    PipelineStep(
        name="generate_comments",
        module="linkedin_automation.comment_generator",
        entry_function="main",
        target="quality_comments/comments_*.json",
        critical_columns=["post_url", "comment"],
        expected_min=0,
        freshness_minutes=None,
        upstream=["scrape_feed"],
        downstream=["post_comments"],
        data_source="LLM provider (OpenAI today; Anthropic and xAI planned)",
        known_failure_modes=[
            "missing or unfunded API key",
            "provider rate limit or outage",
        ],
        requires_linkedin_session=False,
    ),
    PipelineStep(
        name="post_comments",
        module="linkedin_automation.comment_poster",
        entry_function="main",
        target="quality_comments/posting_progress.json",
        critical_columns=["posted_comments"],
        expected_min=0,
        freshness_minutes=None,
        # Human review sits between generate and post. It is a gate, not an
        # automated step, so it is deliberately absent from this manifest.
        upstream=["generate_comments"],
        downstream=[],
        data_source="LinkedIn via Selenium",
        known_failure_modes=[
            "permalink page selector rot: 'Post content not found on page'",
            "submit button relabelled: types the comment, never submits it",
        ],
        requires_linkedin_session=True,
    ),
    PipelineStep(
        name="selector_health",
        module="linkedin_automation.selector_health",
        entry_function="main",
        target=None,
        data_source="LinkedIn DOM live, or a saved HTML fixture in test mode",
        known_failure_modes=[
            "reports HEALTHY while an unregistered path is broken; the registry "
            "must cover posting as well as scraping",
        ],
        # False against fixtures, True against the live feed. Declared True so
        # the stricter rule applies by default.
        requires_linkedin_session=True,
    ),
]


# ─── Lookups and checks ───────────────────────────────────────────────────────

def by_name() -> Dict[str, PipelineStep]:
    """Return the steps keyed by name."""
    return {step.name: step for step in STEPS}


def get(name: str) -> Optional[PipelineStep]:
    """Return one step by name, or None if it is not declared."""
    return by_name().get(name)


def session_required_steps() -> List[PipelineStep]:
    """Return the steps that drive a logged-in LinkedIn session.

    These are the human's to run. An automated runner should refuse them
    rather than discovering the constraint at the browser.
    """
    return [step for step in STEPS if step.requires_linkedin_session]


def offline_steps() -> List[PipelineStep]:
    """Return the steps that need no LinkedIn session."""
    return [step for step in STEPS if not step.requires_linkedin_session]


def validate() -> List[str]:
    """Return a list of structural problems, empty when the manifest is sound.

    Checks that names are unique, that every upstream and downstream reference
    points at a declared step, that the dependency graph is consistent in both
    directions, and that record-count bounds are not inverted.
    """
    problems: List[str] = []
    names = [step.name for step in STEPS]

    duplicates = {n for n in names if names.count(n) > 1}
    for name in sorted(duplicates):
        problems.append(f"duplicate step name: {name}")

    known = set(names)
    for step in STEPS:
        for dep in step.upstream:
            if dep not in known:
                problems.append(f"{step.name}: unknown upstream step {dep!r}")
            elif step.name not in get(dep).downstream:
                problems.append(
                    f"{step.name} lists {dep!r} upstream, but {dep!r} does not "
                    f"list it downstream"
                )
        for dep in step.downstream:
            if dep not in known:
                problems.append(f"{step.name}: unknown downstream step {dep!r}")
            elif step.name not in get(dep).upstream:
                problems.append(
                    f"{step.name} lists {dep!r} downstream, but {dep!r} does not "
                    f"list it upstream"
                )

        if (
            step.expected_min is not None
            and step.expected_max is not None
            and step.expected_min > step.expected_max
        ):
            problems.append(
                f"{step.name}: expected_min {step.expected_min} exceeds "
                f"expected_max {step.expected_max}"
            )

    return problems
