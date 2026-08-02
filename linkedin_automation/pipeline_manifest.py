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

    # ── The three surfaces added in Phase 12 ──────────────────────────────────
    # AUDIT E2. ROADMAP.md pins four feature surfaces that every phase must
    # leave working, and until Phase 12 this manifest declared only the comment
    # pipeline. The other three were therefore invisible to the drift check
    # below, to the boundary check, and to anything reading this file to learn
    # what the system does. None of them has ever been run, by anybody.

    PipelineStep(
        name="send_connections",
        module="linkedin_automation.auto_connector",
        entry_function="main",
        target="connections/session_*.json",
        critical_columns=["session", "weekly"],
        expected_min=0,
        freshness_minutes=None,
        upstream=[],
        downstream=[],
        data_source="LinkedIn people search via Selenium",
        known_failure_modes=[
            "invite-link selector rot: zero targets on a page that has results, "
            "which is indistinguishable from an empty search without the "
            "no_connect_links failure capture",
            "daily or weekly cap already reached: the run exits having sent "
            "nothing, which is correct and looks identical to a failure",
            "run() catches every exception and returns partial results, so a "
            "total failure is reported as a session that sent zero",
        ],
        requires_linkedin_session=True,
    ),
    PipelineStep(
        name="generate_post",
        module="linkedin_automation.post_generator",
        entry_function="main",
        target="posts/post_queue.json",
        critical_columns=["id", "type", "text", "status"],
        expected_min=0,
        freshness_minutes=None,
        upstream=[],
        downstream=["publish_post", "scheduled_publish"],
        data_source="LLM provider, or a fetched article URL for reaction posts",
        known_failure_modes=[
            "missing or unfunded API key for the configured provider",
            "article fetch blocked or paywalled, so a reaction post has no source",
        ],
        # Generation is provider-side. Publishing is what needs the session.
        requires_linkedin_session=False,
    ),
    PipelineStep(
        name="publish_post",
        module="linkedin_automation.poster",
        entry_function="main",
        target="posts/post_history.json",
        critical_columns=["id", "posted_at"],
        expected_min=0,
        freshness_minutes=None,
        upstream=["generate_post"],
        downstream=[],
        data_source="LinkedIn via Selenium",
        known_failure_modes=[
            "post composer selector rot: the modal never opens, or opens and "
            "the text is typed but never submitted",
            "a published post cannot be unpublished quietly, so a duplicate is "
            "the expensive failure here",
        ],
        requires_linkedin_session=True,
    ),
    PipelineStep(
        name="scheduled_publish",
        module="linkedin_automation.scheduler",
        entry_function="Scheduler",
        target=None,
        expected_min=None,
        freshness_minutes=None,
        upstream=["generate_post"],
        downstream=[],
        data_source="the post queue, on a randomized twice-daily timer",
        known_failure_modes=[
            "runs inside the dashboard process, so closing the dashboard stops "
            "it silently",
            "the browser lock is held by a manual run, so a due slot passes "
            "without firing",
        ],
        # The scheduler itself starts no browser: it submits jobs that do, and
        # those jobs are the steps above. Declaring it False would be defensible
        # and is deliberately not done, because it *causes* browser work and the
        # flag exists to keep automated runners away from anything that reaches
        # LinkedIn.
        requires_linkedin_session=True,
    ),
]


# The feature surfaces ROADMAP.md pins. Every phase must leave all four
# working, and Phase 12 tests them as a set. Kept here rather than in the test
# so the manifest is the single place that says what the system does.
FEATURE_SURFACES: Dict[str, List[str]] = {
    "comment pipeline": ["scrape_feed", "generate_comments", "post_comments"],
    "auto connector": ["send_connections"],
    "post creator": ["generate_post", "publish_post"],
    "scheduler": ["scheduled_publish"],
}


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


# Modules in the package that define ``main`` but are deliberately not pipeline
# steps. Exempting by name, with a reason, so that adding a step and forgetting
# to declare it is caught while a genuine non-step does not need the check
# weakened.
NON_STEP_MAIN_MODULES: Dict[str, str] = {
    "dashboard": "the web UI, an operator surface rather than a pipeline step",
}


def undeclared_step_modules(package_dir: Optional[str] = None) -> List[str]:
    """Return package modules that look like steps but are not declared.

    This is the drift check named in this module's docstring. Until Phase 12
    it did not exist: :func:`validate` only ever compared the manifest against
    itself, so the question "does a step exist in the codebase with no manifest
    entry?" could not be answered here, and three of the four pinned feature
    surfaces were in fact missing.

    A module counts as a step if it defines a module-level ``main``. Matched on
    the source text rather than by importing, because importing every module to
    inspect it would run each one's import-time side effects, and six of them
    call ``load_dotenv()`` at import.
    """
    import os
    import re

    package_dir = package_dir or os.path.dirname(os.path.abspath(__file__))
    declared_modules = {step.module.rsplit(".", 1)[-1] for step in STEPS}
    has_main = re.compile(r"^def main\b", re.MULTILINE)

    undeclared = []
    for filename in sorted(os.listdir(package_dir)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        stem = filename[:-3]
        if stem in declared_modules or stem in NON_STEP_MAIN_MODULES:
            continue
        path = os.path.join(package_dir, filename)
        with open(path, "r", encoding="utf-8") as handle:
            if has_main.search(handle.read()):
                undeclared.append(stem)

    return undeclared


def surface_steps(surface: str) -> List[PipelineStep]:
    """Return the declared steps making up one pinned feature surface."""
    return [get(name) for name in FEATURE_SURFACES.get(surface, []) if get(name)]


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

    # The pinned feature surfaces must name declared steps. A surface pointing
    # at a step that was renamed is the same drift this manifest exists to
    # catch, one level up.
    for surface, step_names in sorted(FEATURE_SURFACES.items()):
        if not step_names:
            problems.append(f"feature surface {surface!r} declares no steps")
        for name in step_names:
            if name not in known:
                problems.append(
                    f"feature surface {surface!r}: unknown step {name!r}"
                )

    return problems
