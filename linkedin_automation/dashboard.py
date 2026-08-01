# Flask backend for the LinkedIn Automation Dashboard
"""Flask backend for the LinkedIn Automation Dashboard.

Serves the single-page UI and the JSON API that drives the scrape → generate →
review → post pipeline. Long-running browser/AI tasks run as background jobs
(see ``run_job`` / ``run_subprocess``); profile management is delegated to
``linkedin_profile_manager``. Binds loopback only, on port 6500 by default
(see ``LINKEDIN_DASHBOARD_PORT``).
"""

import os
import sys
import json
import time
import threading
import subprocess
import glob
import logging
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv

from .comment_fields import normalize_comment_fields, comments_to_txt
# Profile manager calls load_dotenv() on import; importing it here (before our
# own load_dotenv) is intentional and order-independent.
from . import profile_manager as pm
from . import post_store
from . import atomic_io
from . import providers
from . import scheduler as scheduler_mod
from . import selector_health as shc

load_dotenv()

# Directory holding this package's bundled files (the dashboard HTML template).
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

logger = logging.getLogger(__name__)

# ─── Server Configuration ─────────────────────────────────────────────────────

DEFAULT_PORT = 6500

# The bind address is deliberately a constant, not an environment variable.
# Every endpoint here is unauthenticated, and several of them drive a
# logged-in LinkedIn session. Loopback is the only thing standing between that
# API and the rest of the network, so exposing it has to be a code change that
# someone reviews, not a variable someone exports. Dashboard authentication is
# the prerequisite for ever binding wider, and it does not exist yet.
HOST = '127.0.0.1'

_TRUTHY = {'1', 'true', 'yes', 'on'}


def get_port():
    """Return the port to serve on: ``LINKEDIN_DASHBOARD_PORT``, else 6500.

    A value that is not a number, or is outside 1-65535, falls back to the
    default with a warning instead of raising. A dashboard on the wrong port
    is a recoverable annoyance; a launcher that dies on a typo is not.
    """
    raw = os.environ.get('LINKEDIN_DASHBOARD_PORT', '').strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning(
            "LINKEDIN_DASHBOARD_PORT=%r is not a number; falling back to %d",
            raw, DEFAULT_PORT)
        return DEFAULT_PORT
    if not 1 <= port <= 65535:
        logger.warning(
            "LINKEDIN_DASHBOARD_PORT=%r is outside 1-65535; falling back to %d",
            raw, DEFAULT_PORT)
        return DEFAULT_PORT
    return port


def get_debug():
    """Return whether Flask debug mode is on. Off unless explicitly opted in.

    Debug mode ships the Werkzeug interactive debugger, which runs arbitrary
    Python typed into the browser. That is by design and it is not a bug in
    Werkzeug, but it means debug mode is a remote code execution surface the
    moment the bind address is anything but loopback. Opt in per run with
    ``LINKEDIN_DASHBOARD_DEBUG=1``; never leave it set.
    """
    return os.environ.get('LINKEDIN_DASHBOARD_DEBUG', '').strip().lower() in _TRUTHY


# ─── Error Handling ───────────────────────────────────────────────────────────

@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Render 4xx/5xx raised as HTTP exceptions as JSON, matching the API."""
    return jsonify({'error': e.name, 'status': e.code}), e.code


@app.errorhandler(Exception)
def handle_unexpected_exception(e):
    """Turn any unhandled exception into a generic 500 carrying no detail.

    The traceback goes to the server log, where the operator can read it. The
    response body gets the status and nothing else: no traceback, no source
    excerpt, no local variables. Locals are the point. A failure inside the
    login or provider paths has credentials in scope, and an error page that
    renders them has handed them to whoever triggered the error.
    """
    logger.exception("Unhandled error serving %s %s", request.method, request.path)
    return jsonify({'error': 'Internal Server Error', 'status': 500}), 500


# ─── Subprocess Environment (fix Windows cp1252 encoding) ─────────────────────

_subprocess_env = os.environ.copy()
_subprocess_env['PYTHONIOENCODING'] = 'utf-8'
_subprocess_env['PYTHONUNBUFFERED'] = '1'

# ─── Job Tracking ─────────────────────────────────────────────────────────────

jobs = {}  # job_id -> {status, progress, result, error, log, profile, task_type}

# Task types:
#   "browser" = needs a Chrome instance (connect, post_comments, publish)
#   "api"     = just API calls, no browser (generate_comments, generate_posts, article_gen)
# Rule: Only ONE browser task per profile at a time. API tasks are unlimited.

def get_active_jobs(profile=None, task_type=None):
    """Get currently running jobs, optionally filtered."""
    active = {jid: j for jid, j in jobs.items() if j["status"] == "running"}
    if profile:
        active = {jid: j for jid, j in active.items() if j.get("profile") == profile}
    if task_type:
        active = {jid: j for jid, j in active.items() if j.get("task_type") == task_type}
    return active


def can_start_browser_task(profile):
    """Check if a browser task can start for this profile."""
    return len(get_active_jobs(profile=profile, task_type="browser")) == 0


def run_job(job_id, func, *args, profile=None, task_type="api", category="", **kwargs):
    """Run a function in a background thread and track its progress."""
    jobs[job_id] = {
        "status": "running",
        "progress": "",
        "log": [],
        "result": None,
        "error": None,
        "started": datetime.now().isoformat(),
        "profile": profile,
        "task_type": task_type,
        "category": category,
    }
    
    def wrapper():
        """Run the job function in the thread, recording result/error + status."""
        # Record result/error and logs BEFORE flipping status, so a poller that
        # observes a terminal status always sees the accompanying data.
        try:
            result = func(job_id, *args, **kwargs)
            jobs[job_id]["result"] = result
            jobs[job_id]["status"] = "completed"
        except pm.LoginRequiredError as e:
            # Actionable, traceback-free: the user just needs to log in.
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["login_required"] = True
            jobs[job_id]["log"].append(str(e))
            jobs[job_id]["status"] = "failed"
        except FileNotFoundError as e:
            msg = f"File not found: {e.filename or e}. The expected input/output file is missing."
            jobs[job_id]["error"] = msg
            jobs[job_id]["log"].append(msg)
            jobs[job_id]["status"] = "failed"
        except PermissionError as e:
            msg = f"Permission denied: {e.filename or e}. Check the file is not open elsewhere and is writable."
            jobs[job_id]["error"] = msg
            jobs[job_id]["log"].append(msg)
            jobs[job_id]["status"] = "failed"
        except Exception as e:
            jobs[job_id]["error"] = str(e)
            import traceback
            jobs[job_id]["log"].append(f"ERROR: {traceback.format_exc()}")
            jobs[job_id]["status"] = "failed"
    
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return job_id


def log_job(job_id, message):
    """Append a log message to a job."""
    if job_id in jobs:
        jobs[job_id]["log"].append(message)
        jobs[job_id]["progress"] = message


def run_subprocess(job_id, cmd, on_start=None):
    """Run ``cmd``, streaming stdout to the job log while capturing stderr
    separately. On a non-zero exit, the captured stderr is logged so failures
    are diagnosable instead of silently merged into stdout.

    ``on_start(process)``, if given, is called right after the process starts
    (e.g. to register the handle so the job can be killed).

    Returns ``(returncode, stderr_text)``.
    """
    log_job(job_id, f"Running: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
        env=_subprocess_env,
    )

    if on_start is not None:
        on_start(process)

    # Drain stderr on a separate thread so a large stderr cannot deadlock the
    # process while we stream stdout.
    stderr_chunks = []

    def _drain_stderr():
        for err_line in process.stderr:
            stderr_chunks.append(err_line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    for line in process.stdout:
        line = line.strip()
        if line:
            log_job(job_id, line)

    process.wait()
    stderr_thread.join(timeout=5)

    stderr_text = "".join(stderr_chunks).strip()
    if process.returncode != 0 and stderr_text:
        log_job(job_id, f"stderr: {stderr_text}")

    return process.returncode, stderr_text


# ─── Reusable browser-task job bodies ─────────────────────────────────────────
# Extracted from the scrape/post endpoints so the scheduler can run the exact
# same jobs (via run_job / the browser lock) that a manual click would.

def _scrape_job(job_id, profile_name, max_posts, min_quality):
    """Run the post finder subprocess and return the newest output file."""
    log_job(job_id, "Starting post finder...")
    cmd = [
        sys.executable, "-m", "linkedin_automation.post_finder",
        "--max-posts", str(max_posts),
        "--min-quality", str(min_quality),
        "--profile", profile_name,
    ]
    returncode, _ = run_subprocess(job_id, cmd)

    if returncode == pm.EXIT_LOGIN_REQUIRED:
        raise pm.LoginRequiredError(
            f"Login required. Press \"Log in\" in the dashboard header, or run: "
            f"python tools/login_check.py --profile {profile_name}"
        )
    if returncode != 0:
        raise RuntimeError(f"Post finder exited with code {returncode}")

    timeline_dir = pm.get_timeline_dir(profile_name)
    json_files = sorted(
        glob.glob(os.path.join(timeline_dir, "ai_posts_*.json")),
        key=os.path.getmtime, reverse=True,
    )
    if json_files:
        log_job(job_id, f"Posts saved to: {json_files[0]}")
        return {"file": json_files[0]}
    raise RuntimeError("No output file found")


def _post_comments_job(job_id, profile_name, comments_file, count):
    """Run the comment poster subprocess for ``count`` comments from a TXT file."""
    log_job(job_id, f"Posting {count} comment(s) from: {comments_file}")
    cmd = [
        sys.executable, "-m", "linkedin_automation.comment_poster",
        comments_file,
        "--count", str(count),
        "--profile", profile_name,
    ]
    returncode, _ = run_subprocess(job_id, cmd)

    if returncode == pm.EXIT_LOGIN_REQUIRED:
        raise pm.LoginRequiredError(
            f"Login required. Press \"Log in\" in the dashboard header, or run: "
            f"python tools/login_check.py --profile {profile_name}"
        )
    if returncode != 0:
        raise RuntimeError(f"Comment poster exited with code {returncode}")

    log_job(job_id, "Finished posting")
    return {"posted": count}


# ─── Scrape / comment file merging ─────────────────────────────────────────────
# The review lists were each loading only the single newest JSON file, orphaning
# posts/comments from earlier scrapes that hadn't been acted on yet. These
# helpers merge across recent files, dedupe, and drop anything already handled.

MERGE_WINDOW_DAYS = 7    # only merge files touched in the last week
MAX_MERGED_POSTS = 100   # cap the review list so it stays manageable


def _files_within_days(dirpath, pattern, days, now=None):
    """Return JSON files in ``dirpath`` matching ``pattern`` whose mtime is within
    ``days``, newest first. Non-recursive (so a ``archived/`` subdir is skipped)."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    dated = []
    for fp in glob.glob(os.path.join(dirpath, pattern)):
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        if mtime >= cutoff:
            dated.append((fp, mtime))
    dated.sort(key=lambda t: t[1], reverse=True)
    return [fp for fp, _ in dated]


def _load_json(path):
    """Read a JSON file, returning None (and logging) on any error."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        logger.debug("Could not read JSON file %s", path, exc_info=True)
        return None


def _dedupe_key(item):
    """Identity for a post or comment: its URL, else a hash of author + first 100
    chars of text. Delegates to ``post_store.post_key`` so the file-merge dedup
    and the lifecycle store share one identity rule and can never diverge."""
    return post_store.post_key(item)


def _post_score(post):
    """Best-available relevance score for sorting (0 when absent). Bools excluded."""
    for key in ("relevance_score", "quality_score", "score"):
        value = post.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return 0


def merge_posts(datas, max_posts=MAX_MERGED_POSTS):
    """Merge ``quality_posts`` across loaded scrape files (``datas`` newest-first):
    dedupe by URL/hash keeping the higher-scored copy, sort by score descending,
    and cap at ``max_posts``."""
    best = {}
    for data in datas:
        if not isinstance(data, dict):
            continue
        for post in data.get("quality_posts", []) or []:
            key = _dedupe_key(post)
            if key not in best or _post_score(post) > _post_score(best[key]):
                best[key] = post
    merged = sorted(best.values(), key=_post_score, reverse=True)
    return merged[:max_posts]


def merge_comments(datas):
    """Merge comment lists across active comment files (``datas`` newest-first):
    dedupe by URL/hash, keeping the first (newest) occurrence and preserving order."""
    seen = set()
    out = []
    for data in datas:
        if isinstance(data, list):
            comments = data
        elif isinstance(data, dict):
            comments = data.get("comments", []) or []
        else:
            continue
        for comment in comments:
            key = _dedupe_key(comment)
            if key in seen:
                continue
            seen.add(key)
            out.append(comment)
    return out


def _posted_urls(profile_name):
    """URLs already posted/commented (from posting_progress.json)."""
    progress_file = pm.get_progress_file(profile_name)
    if not os.path.exists(progress_file):
        return set()
    data = _load_json(progress_file) or {}
    return set(data.get("posted_comments", []) or [])


def _pipeline_comment_urls(profile_name):
    """URLs that already have comments in the pipeline — generated (comments_*.json)
    or curated (ready_*.json). Archived files (in archived/) are excluded since
    glob is non-recursive."""
    comments_dir = pm.get_comments_dir(profile_name)
    urls = set()
    if not os.path.isdir(comments_dir):
        return urls
    for pattern in ("comments_*.json", "ready_*.json"):
        for cf in glob.glob(os.path.join(comments_dir, pattern)):
            data = _load_json(cf)
            if data is None:
                continue
            comments = data if isinstance(data, list) else data.get("comments", []) or []
            for comment in comments:
                url = normalize_comment_fields(comment)["url"]
                if url:
                    urls.add(url)
    return urls


# ─── API: Profiles ───────────────────────────────────────────────────────────

@app.route('/api/profiles', methods=['GET'])
def get_profiles():
    """GET /api/profiles — list all profiles (auto-migrating from .env first)."""
    pm.auto_migrate_from_env()
    data = pm.list_profiles()
    return jsonify(data)


@app.route('/api/profiles', methods=['POST'])
def create_profile():
    """POST /api/profiles — create a new profile from name/username/password."""
    body = request.json
    name = body.get('name', '').strip()
    username = body.get('username', '').strip()
    password = body.get('password', '').strip()
    is_default = body.get('set_default', False)
    
    if not name or not username or not password:
        return jsonify({"error": "name, username, and password are required"}), 400
    
    if pm.get_profile(name):
        return jsonify({"error": f"Profile '{name}' already exists"}), 409
    
    pm.add_profile(name, username, password, set_default=is_default)
    return jsonify({"ok": True, "message": f"Profile '{name}' created"})


@app.route('/api/profiles/<name>/default', methods=['POST'])
def set_default_route(name):
    """POST /api/profiles/<name>/default — set the default profile."""
    if not pm.get_profile(name):
        return jsonify({"error": f"Profile '{name}' not found"}), 404
    pm.set_default_profile(name)
    return jsonify({"ok": True})


@app.route('/api/profiles/<name>', methods=['DELETE'])
def delete_profile(name):
    """DELETE /api/profiles/<name> — remove a profile."""
    if not pm.get_profile(name):
        return jsonify({"error": f"Profile '{name}' not found"}), 404
    pm.remove_profile(name)
    return jsonify({"ok": True})


@app.route('/api/profiles/<name>/config', methods=['GET'])
def get_profile_config_route(name):
    """GET /api/profiles/<name>/config — return the profile's config (creates default on first use)."""
    return jsonify(pm.get_profile_config(name))


@app.route('/api/profiles/<name>/config', methods=['POST'])
def update_profile_config_route(name):
    """POST /api/profiles/<name>/config — update the profile's config (merged onto defaults)."""
    body = request.json
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    pm.save_profile_config(name, body)
    # Return the effective (default-merged) config so the client sees the result.
    return jsonify({"ok": True, "config": pm.get_profile_config(name)})


@app.route('/api/profiles/<name>/config/reset', methods=['POST'])
def reset_profile_config_route(name):
    """POST /api/profiles/<name>/config/reset — regenerate the config from defaults."""
    return jsonify({"ok": True, "config": pm.reset_profile_config(name)})


# ─── API: Settings (generation provider) ──────────────────────────────────────
#
# The API key is write-only across this whole section. A GET never returns it,
# and the only part of a stored key that ever leaves the process is its last
# four characters, which is enough to confirm *which* key is installed and not
# enough to use it. See ROADMAP Phase 8.

@app.route('/api/settings/providers', methods=['GET'])
def get_providers_route():
    """GET /api/settings/providers — the provider catalogue plus key status.

    Drives the Settings screen's dropdown. ``key`` per provider reports whether
    a key is set, where it came from, and its last four characters only.
    """
    return jsonify({
        "providers": [
            {
                "name": spec.name,
                "label": spec.label,
                "default_model": spec.default_model,
                "default_model_verified": spec.default_model_verified,
                "base_url": spec.base_url or "",
                "requires_base_url": spec.requires_base_url,
                "local": spec.local,
                "env_var": spec.key_env,
                "key": {"set": True, "source": "local", "last4": ""} if spec.local
                       else pm.api_key_status(spec.name),
            }
            for spec in providers.SPECS.values()
        ],
        "default_provider": providers.DEFAULT_PROVIDER,
        "credential_store_available": pm.keyring_available(),
    })


@app.route('/api/profiles/<name>/provider', methods=['GET'])
def get_profile_provider_route(name):
    """GET /api/profiles/<name>/provider — this profile's provider and model."""
    try:
        provider, model, base_url = providers.resolve_provider_config(
            pm.get_profile_config(name))
    except providers.ProviderError as e:
        # A config saved with a bad provider name, a custom provider with no
        # base URL, or a provider with no model. Report it rather than papering
        # over it with a default, so the user can see what to fix.
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "provider": provider,
        "model": model,
        "base_url": base_url or "",
        "key": pm.api_key_status(provider),
    })


@app.route('/api/profiles/<name>/provider', methods=['POST'])
def set_profile_provider_route(name):
    """POST /api/profiles/<name>/provider — set this profile's provider/model.

    Body: ``{"provider": "openai"|"anthropic"|"xai", "model": "<optional>"}``.

    Writes only the ``provider`` block, so persona, tone, and voice are left
    untouched — changing where the text is generated must not change the voice
    it is generated in.
    """
    body = request.json if request.is_json else None
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    provider = (body.get("provider") or "").strip().lower()
    try:
        providers.validate_provider(provider)
    except providers.ProviderError as e:
        return jsonify({"error": str(e)}), 400

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return jsonify({"error": "model must be a string"}), 400
    spec = providers.get_spec(provider)
    model = (model or "").strip() or spec.default_model

    base_url = body.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        return jsonify({"error": "base_url must be a string"}), 400
    base_url = (base_url or "").strip()

    # Validate the combination before writing it, so a config that cannot
    # resolve is never persisted. This is what makes the Custom option safe:
    # a base URL is demanded up front rather than at the first generation run.
    candidate = {"name": provider, "model": model}
    if base_url:
        candidate["base_url"] = base_url
    try:
        providers.resolve_provider_config({"provider": candidate})
    except providers.ProviderError as e:
        return jsonify({"error": str(e)}), 400

    config = pm.get_profile_config(name)
    config["provider"] = candidate
    pm.save_profile_config(name, config)

    return jsonify({
        "ok": True,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "key": pm.api_key_status(provider),
    })


@app.route('/api/settings/api-key', methods=['POST'])
def set_api_key_route():
    """POST /api/settings/api-key — store a provider's API key in the OS store.

    Body: ``{"provider": "...", "api_key": "..."}``.

    The key goes to the credential store, never to ``profiles.json`` and never
    to ``.env``. The response echoes only the last four characters.
    """
    body = request.json if request.is_json else None
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    provider = (body.get("provider") or "").strip().lower()
    try:
        providers.validate_provider(provider)
    except providers.ProviderError as e:
        return jsonify({"error": str(e)}), 400

    api_key = body.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return jsonify({"error": "api_key is required and must be a non-empty string"}), 400

    try:
        pm.set_api_key(provider, api_key)
    except RuntimeError as e:
        # No OS credential store. Actionable, and deliberately not a 500.
        return jsonify({"error": str(e)}), 409

    return jsonify({"ok": True, "provider": provider, "key": pm.api_key_status(provider)})


@app.route('/api/settings/test-connection', methods=['POST'])
def test_connection_route():
    """POST /api/settings/test-connection — probe a provider with one small call.

    Body: ``{"provider": "...", "model": "...", "base_url": "..."}``.

    **This is the one endpoint in the project that deliberately spends money**,
    and it spends it only because a human pressed the button. Two calls maximum,
    matching the retry cap in PROJECT.md, logged to ``api_usage.jsonl`` before
    each call.

    It exists because provider quirks cannot be enumerated in advance. Finding
    out that a model rejects temperature or leaks its reasoning is far cheaper
    here than during a real generation run.
    """
    body = request.json if request.is_json else None
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    provider = (body.get("provider") or "").strip().lower()
    try:
        providers.validate_provider(provider)
    except providers.ProviderError as e:
        return jsonify({"error": str(e)}), 400

    for field_name in ("model", "base_url"):
        value = body.get(field_name)
        if value is not None and not isinstance(value, str):
            return jsonify({"error": f"{field_name} must be a string"}), 400

    report = providers.probe(
        provider,
        model=(body.get("model") or "").strip() or None,
        base_url=(body.get("base_url") or "").strip() or None,
    )
    # A failed probe is a 200 with ok=False, not an HTTP error: the report is
    # the result, and the UI needs to render why it failed.
    return jsonify(report)


@app.route('/api/settings/api-key/<provider>', methods=['DELETE'])
def delete_api_key_route(provider):
    """DELETE /api/settings/api-key/<provider> — forget a stored API key."""
    provider = (provider or "").strip().lower()
    try:
        providers.validate_provider(provider)
    except providers.ProviderError as e:
        return jsonify({"error": str(e)}), 400

    pm.delete_api_key(provider)
    return jsonify({"ok": True, "provider": provider, "key": pm.api_key_status(provider)})


# ─── API: Posts ───────────────────────────────────────────────────────────────

@app.route('/api/posts/<profile_name>', methods=['GET'])
def get_posts(profile_name):
    """Review Posts list = the lifecycle store's NEW bin (the source of truth).

    Reads the reconciled store, so ``len(posts)`` here always equals the NEW
    lifecycle bin. (These used to diverge: this endpoint read only the recent
    ``ai_posts_*.json`` files, while NEW posts live across *every* scrape and in
    the store — so the store could show 48 NEW while this showed 37.) Recent
    scrape files are still read, but only to *enrich* each NEW post with
    display-only metadata (likes/comments/reposts/quality) that the trimmed store
    record doesn't keep.
    """
    store = post_store.load_synced_store(profile_name)
    new_recs = store.by_status(post_store.NEW)

    # Enrichment map: post key → scrape post dict (display metadata only).
    timeline_dir = pm.get_timeline_dir(profile_name)
    files = _files_within_days(timeline_dir, "ai_posts_*.json", MERGE_WINDOW_DAYS)
    enrich = {}
    for data in (d for d in (_load_json(fp) for fp in files) if d is not None):
        for p in data.get("quality_posts", []) or []:
            enrich[post_store.post_key(p)] = p

    posts = []
    for r in new_recs:
        meta = enrich.get(r["key"], {})
        posts.append({
            # Display-only extras from the scrape file (absent for store-only posts).
            "likes": meta.get("likes"),
            "comments": meta.get("comments"),
            "reposts": meta.get("reposts"),
            "quality": meta.get("quality"),
            # The store is the source of truth for identity / content / status.
            "key": r["key"],
            "url": r.get("url", ""),
            "author_name": r.get("author", ""),
            "author": r.get("author", ""),
            "text": r.get("text", ""),
            "post_type": r.get("category", "") or meta.get("post_type", ""),
            "relevance_score": r.get("relevance_score", 0),
            "status": post_store.NEW,
        })

    counts = store.counts()
    logger.info("Review Posts for %s: %d NEW from store (bin=%d), enriched from %d file(s).",
                profile_name, len(posts), counts["NEW"], len(files))
    return jsonify({
        "posts": posts,
        "counts": counts,
        "source": "lifecycle_store",
        "file": files[0] if files else None,
        "files": files,
    })


@app.route('/api/posts/<profile_name>/scrape', methods=['POST'])
def scrape_posts(profile_name):
    """Start a post scraping job."""
    body = request.json or {}
    max_posts = body.get('max_posts', 50)
    min_quality = body.get('min_quality', 10)
    
    job_id = f"scrape_{profile_name}_{int(time.time())}"

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, _scrape_job, profile_name, max_posts, min_quality,
            profile=profile_name, task_type="browser", category="scrape")
    return jsonify({"job_id": job_id})


@app.route('/api/posts/<profile_name>/save', methods=['POST'])
def save_posts(profile_name):
    """Save curated posts (after user deletes some)."""
    body = request.json
    posts = body.get('posts', [])
    source_file = body.get('source_file', '')
    
    # Load the original file to preserve metadata
    if source_file and os.path.exists(source_file):
        with open(source_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {}
    
    # Update with curated posts
    data['quality_posts'] = posts
    data['quality_found'] = len(posts)
    data['curated_at'] = datetime.now().isoformat()
    
    # Save as a new curated file
    timeline_dir = pm.get_timeline_dir(profile_name)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    curated_file = os.path.join(timeline_dir, f'ai_posts_curated_{timestamp}.json')
    
    atomic_io.write_json_atomic(curated_file, data)
    
    return jsonify({"ok": True, "file": curated_file, "count": len(posts)})


# ─── API: Post lifecycle (NEW / GENERATED / COMMENTED / TRASH) ────────────────

def _lifecycle_record_view(rec):
    """Trim a store record to the fields the dashboard UI needs."""
    return {
        "key": rec.get("key"),
        "url": rec.get("url", ""),
        "author": rec.get("author", ""),
        "text": rec.get("text", ""),
        "category": rec.get("category", ""),
        "relevance_score": rec.get("relevance_score", 0),
        "status": rec.get("status"),
        "trash_reason": rec.get("trash_reason"),
        "comment": rec.get("comment"),
        "scraped_at": rec.get("scraped_at"),
        "generated_at": rec.get("generated_at"),
        "commented_at": rec.get("commented_at"),
    }


@app.route('/api/posts/<profile_name>/lifecycle', methods=['GET'])
def posts_lifecycle(profile_name):
    """Lifecycle counts + posts for a profile.

    Migrates the store from legacy files on first use and reconciles COMMENTED
    from posting_progress.json, so the four bins are always consistent with the
    authoritative posted ledger. Optional ``?status=NEW|GENERATED|COMMENTED|TRASH``
    returns just that bin; otherwise all posts are returned grouped under
    ``posts`` keyed by status.
    """
    store = post_store.load_synced_store(profile_name)
    counts = store.counts()

    status = (request.args.get("status") or "").upper()
    if status in post_store.STATUSES:
        posts = [_lifecycle_record_view(r) for r in store.by_status(status)]
        return jsonify({"counts": counts, "status": status, "posts": posts})

    grouped = {
        s: [_lifecycle_record_view(r) for r in store.by_status(s)]
        for s in post_store.STATUSES
    }
    return jsonify({"counts": counts, "posts": grouped})


@app.route('/api/posts/<profile_name>/reject', methods=['POST'])
def reject_post(profile_name):
    """Move a post → TRASH (manual). Body: ``{"key"|"url": ...}``."""
    body = request.json or {}
    ident = body.get("key") or body.get("url")
    if not ident:
        return jsonify({"error": "key or url is required"}), 400
    store = post_store.PostStore(profile_name)
    if not store.reject(ident, save=True):
        return jsonify({"error": "Post not found in store"}), 404
    return jsonify({"ok": True, "counts": store.counts()})


@app.route('/api/posts/<profile_name>/restore', methods=['POST'])
def restore_post(profile_name):
    """Restore a trashed post → NEW (or GENERATED if it has a draft).

    Body: ``{"key"|"url": ...}``.
    """
    body = request.json or {}
    ident = body.get("key") or body.get("url")
    if not ident:
        return jsonify({"error": "key or url is required"}), 400
    store = post_store.PostStore(profile_name)
    if not store.restore(ident, save=True):
        return jsonify({"error": "Post not found in trash"}), 404
    return jsonify({"ok": True, "counts": store.counts()})


# ─── API: Comments ────────────────────────────────────────────────────────────

@app.route('/api/comments/<profile_name>', methods=['GET'])
def get_comments(profile_name):
    """Merge active (non-archived) generated comments, excluding already-posted ones.

    Like get_posts, this merges across recent comments_*.json files so
    generated-but-not-posted comments aren't orphaned when a newer file appears.
    Only active comment files are merged — save_comments archives originals into
    archived/ (excluded here) and writes curated ready_*.json (not part of the
    review queue). Comments already posted (posting_progress.json) are filtered.
    """
    comments_dir = pm.get_comments_dir(profile_name)
    files = _files_within_days(comments_dir, "comments_*.json", MERGE_WINDOW_DAYS)

    if not files:
        return jsonify({"comments": [], "file": None, "files": [], "filtered_count": 0})

    datas = [d for d in (_load_json(fp) for fp in files) if d is not None]
    merged = merge_comments(datas)

    posted_urls = _posted_urls(profile_name)

    # Merge normalized fields into each comment so the frontend gets BOTH naming
    # conventions (url AND post_url, author AND post_author). Generator output
    # only has post_* keys, which made the "open post" link never render.
    remaining = []
    for c in merged:
        norm = normalize_comment_fields(c)
        if norm["url"] and norm["url"] in posted_urls:
            continue
        remaining.append({**c, **norm})

    filtered_k = len(merged) - len(remaining)
    logger.info(
        f"Merged {len(merged)} comments from {len(files)} files, "
        f"filtered {filtered_k} already-posted, showing {len(remaining)}."
    )

    return jsonify({
        "file": files[0],
        "files": files,
        "total": len(remaining),
        "comments": remaining,
        "merged_count": len(merged),
        "filtered_count": filtered_k,
    })


def _write_lifecycle_input(profile_name, records):
    """Serialize the store's NEW records to a generator-shaped input JSON file.

    The generator reads ``{"quality_posts": [...]}`` with per-post ``url`` /
    ``text`` / ``author_name`` keys, then marks each commented post GENERATED in
    the same store (by URL). Only URL-bearing records are written — a post with
    no URL can't be commented on or posted, so it stays NEW.
    """
    quality_posts = []
    for r in records:
        if not r.get("url"):
            continue
        quality_posts.append({
            "url": r["url"],
            "text": r.get("text", ""),
            "author_name": r.get("author", ""),
            "relevance_score": r.get("relevance_score", 0),
            "post_type": r.get("category", ""),
            "should_engage": True,
        })
    timeline_dir = pm.get_timeline_dir(profile_name)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(timeline_dir, f'lifecycle_new_{ts}.json')
    atomic_io.write_json_atomic(path, {
        "source": "lifecycle_store",
        "generated_at": datetime.now().isoformat(),
        "quality_posts": quality_posts,
    })
    return path, len(quality_posts)


@app.route('/api/comments/<profile_name>/generate', methods=['POST'])
def generate_comments(profile_name):
    """Start a comment generation job driven by the lifecycle store.

    The lifecycle store (posts_db.json) is the source of truth for which posts
    are actionable. We reconcile it, take every NEW post (across ALL scrapes, not
    just the latest file), write those to a generator-shaped input file, and run
    the generator on it. An explicit ``input_file`` in the body still overrides
    (legacy/advanced use).
    """
    body = request.json or {}
    input_file = body.get('input_file', '')
    model = body.get('model', 'gpt-4o-mini')
    # Optional cap. None/blank/<=0 means "generate for all engaging posts" — there
    # is no rate-limit reason to cap (it's OpenAI-only). A positive int is a ceiling.
    limit = body.get('limit')
    if limit in (None, "", 0):
        limit = None
    else:
        try:
            limit = int(limit)
            if limit <= 0:
                limit = None
        except (TypeError, ValueError):
            limit = None

    if not input_file:
        # Source of truth: the reconciled lifecycle store, NOT the latest scrape
        # file. This is the fix — NEW posts live across many scrape files, and the
        # latest file's posts may all already be handled.
        store = post_store.load_synced_store(profile_name)
        new_posts = store.get_posts_by_status(post_store.NEW)
        input_file, actionable = _write_lifecycle_input(profile_name, new_posts)
        if actionable == 0:
            counts = store.counts()
            return jsonify({
                "error": "No NEW posts to generate for. Scrape fresh posts, or "
                         "the queue may already be generated/commented.",
                "counts": counts,
            }), 400

    job_id = f"generate_{profile_name}_{int(time.time())}"
    
    def do_generate(jid, pname, infile, mdl, lmt):
        """Background job: run the comment generator subprocess."""
        log_job(jid, f"Generating comments from: {infile}")
        log_job(jid, f"Comment cap: {lmt if lmt is not None else 'none (all engaging posts)'}")

        cmd = [
            sys.executable, "-m", "linkedin_automation.comment_generator",
            infile,
            "--model", mdl,
            "--profile", pname,
        ]
        # Only pass --limit when the user set a cap; omitting it means no limit.
        if lmt is not None:
            cmd.extend(["--limit", str(lmt)])

        returncode, _ = run_subprocess(jid, cmd)

        if returncode != 0:
            raise RuntimeError(f"Comment generator exited with code {returncode}")
        
        # Find the output file
        comments_dir = pm.get_comments_dir(pname)
        json_files = sorted(
            glob.glob(os.path.join(comments_dir, "comments_*.json")),
            key=os.path.getmtime, reverse=True
        )
        
        if json_files:
            log_job(jid, f"Comments saved to: {json_files[0]}")
            return {"file": json_files[0]}
        
        raise RuntimeError("No comments file found")
    
    run_job(job_id, do_generate, profile_name, input_file, model, limit,
            profile=profile_name, task_type="api", category="generate_comments")
    return jsonify({"job_id": job_id})


@app.route('/api/comments/<profile_name>/save', methods=['POST'])
def save_comments(profile_name):
    """Save edited comments and archive originals so they leave the review step."""
    body = request.json
    comments = body.get('comments', [])
    
    comments_dir = pm.get_comments_dir(profile_name)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Save JSON for records (use 'ready_' prefix so it doesn't match review query)
    json_file = os.path.join(comments_dir, f'ready_{timestamp}.json')
    json_data = {
        "generated_at": datetime.now().isoformat(),
        "total": len(comments),
        "curated": True,
        "comments": comments
    }
    atomic_io.write_json_atomic(json_file, json_data)
    
    # Save TXT for the poster script (canonical format owned by comment_fields)
    txt_file = os.path.join(comments_dir, f'daily_comments_curated_{timestamp}.txt')
    txt_content = comments_to_txt(comments, datetime.now().strftime('%Y-%m-%d %H:%M'))
    with open(txt_file, 'w', encoding='utf-8') as f:
        f.write(txt_content)
    
    # Archive original comment files so they don't reload into review
    archive_dir = os.path.join(comments_dir, "archived")
    os.makedirs(archive_dir, exist_ok=True)
    for f_path in glob.glob(os.path.join(comments_dir, "comments_*.json")):
        try:
            dest = os.path.join(archive_dir, os.path.basename(f_path))
            os.rename(f_path, dest)
        except Exception:
            logger.warning("Could not archive original comments file %s", f_path, exc_info=True)

    return jsonify({"ok": True, "json_file": json_file, "txt_file": txt_file, "count": len(comments)})


# ─── API: Posting ─────────────────────────────────────────────────────────────

@app.route('/api/post/<profile_name>', methods=['POST'])
def post_comments(profile_name):
    """Start posting comments."""
    body = request.json or {}
    comments_file = body.get('comments_file', '')
    count = body.get('count', 1)
    
    if not comments_file:
        # Find latest curated or daily comments txt
        comments_dir = pm.get_comments_dir(profile_name)
        txt_files = sorted(
            glob.glob(os.path.join(comments_dir, "daily_comments_*.txt")),
            key=os.path.getmtime, reverse=True
        )
        if not txt_files:
            return jsonify({"error": "No comments file found. Generate comments first."}), 400
        comments_file = txt_files[0]
    
    job_id = f"post_{profile_name}_{int(time.time())}"

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, _post_comments_job, profile_name, comments_file, count,
            profile=profile_name, task_type="browser", category="post_comments")
    return jsonify({"job_id": job_id})


# ─── API: Selector Health ─────────────────────────────────────────────────────

@app.route('/api/health/<profile_name>/selectors', methods=['POST'])
def selector_health(profile_name):
    """POST /api/health/<name>/selectors — run one page's selector check as a job.

    Body (all optional): ``{"page": "feed"|"search"|"post", "url": "..."}``.
    ``feed`` is the default and needs no URL; ``search`` and ``post`` each need
    the URL of the page to check, because neither can be reached from the feed.

    The job result is the structured report (HEALTHY/DEGRADED/BROKEN plus
    per-selector counts) that the selector-health module writes; poll
    /api/jobs/<job_id> for it. Every one of these needs a live LinkedIn session,
    so they are Rick's to run.
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    page = body.get("page", "feed")
    if page not in shc.PAGES:
        return jsonify({"error": f"Unknown page '{page}'. Expected one of "
                                 f"{list(shc.PAGES)}"}), 400

    url = body.get("url")
    if page in ("search", "post"):
        if not isinstance(url, str) or not url.strip():
            return jsonify({"error": f"The {page} check needs a 'url': the "
                                     f"{page} page cannot be reached from the feed"}), 400
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": "'url' must be an http(s) URL"}), 400

    job_id = f"health_{page}_{profile_name}_{int(time.time())}"
    flag = {"search": "--search-url", "post": "--post-url"}.get(page)

    def do_health(jid, pname):
        cmd = [sys.executable, "-m", "linkedin_automation.selector_health",
               "--profile", pname]
        if flag:
            cmd += [flag, url]
        returncode, _ = run_subprocess(jid, cmd)

        if returncode == pm.EXIT_LOGIN_REQUIRED:
            raise pm.LoginRequiredError(
                f"Login required. Press \"Log in\" in the dashboard header, or run: "
                f"python tools/login_check.py --profile {pname}"
            )

        # The script writes the structured result even when the status is BROKEN
        # (exit 1) or DEGRADED (exit 3), so read it rather than treating non-zero
        # as failure. A degraded run is the one you most want to see.
        result_path = os.path.join(pm.get_data_dir(pname),
                                   shc.RESULT_FILE_BY_PAGE[page])
        if os.path.exists(result_path):
            with open(result_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"status": "UNKNOWN", "page": page, "returncode": returncode}

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, do_health, profile_name,
            profile=profile_name, task_type="browser", category="selector_health")
    return jsonify({"job_id": job_id})


# tools/ is a sibling of the package, not a module inside it.
LOGIN_CHECK_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "login_check.py")


@app.route('/api/profiles/<name>/login', methods=['POST'])
def profile_login(name):
    """POST /api/profiles/<name>/login — open Chrome and wait for a manual sign-in.

    Body (optional): ``{"wait": false}`` to report the current status and exit
    instead of waiting.

    This is the one step that had no button, which is why it was the one step
    that still needed a terminal. It runs ``tools/login_check.py``, which since
    Phase 5b polls the browser rather than prompting on stdin — a subprocess
    launched from here has no stdin at all, so the old bare ``input()`` would
    have hung here exactly as it did everywhere else (finding B7).

    **Rick types his password into LinkedIn's own page**, in the Chrome window
    this opens. Nothing in this project ever types it for him on the way in.
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400
    wait = body.get("wait", True)
    if not isinstance(wait, bool):
        return jsonify({"error": "'wait' must be true or false"}), 400

    if name not in pm.load_profiles().get("profiles", {}):
        return jsonify({"error": f"No profile named '{name}'"}), 404

    job_id = f"login_{name}_{int(time.time())}"

    def do_login(jid, pname):
        cmd = [sys.executable, LOGIN_CHECK_SCRIPT, "--profile", pname]
        if not wait:
            cmd.append("--no-wait")
        returncode, _ = run_subprocess(jid, cmd)

        # login_check: 0 logged in, 1 error, 2 not logged in.
        if returncode == pm.EXIT_OK:
            return {"logged_in": True, "status": "LOGGED_IN",
                    "message": f"Profile '{pname}' is signed in to LinkedIn. "
                               f"Scrape, post and connect runs will reuse this session."}
        if returncode == pm.EXIT_LOGIN_REQUIRED:
            return {"logged_in": False, "status": "NOT_LOGGED_IN",
                    "message": "Not signed in. The window timed out, or it was "
                               "closed before the sign-in completed. Press Log in "
                               "again — the session persists once it takes."}
        return {"logged_in": False, "status": "ERROR",
                "message": "The login check could not run. See the job log.",
                "returncode": returncode}

    if not can_start_browser_task(name):
        return jsonify({"error": f"A browser task is already running for {name}. Wait for it to finish."}), 409
    run_job(job_id, do_login, name, profile=name, task_type="browser", category="login")
    return jsonify({"job_id": job_id})


@app.route('/api/profiles/<name>/credential-check', methods=['GET'])
def credential_check(name):
    """GET /api/profiles/<name>/credential-check — is the stored password sound?

    Answers three things with **zero LinkedIn contact**: where the password is
    stored, whether it can actually be read back, and which username it belongs
    to. Never returns the password, and never returns any part of it.

    It deliberately does NOT answer whether the credential is correct on
    LinkedIn, and says so. Only an authentication attempt can answer that, and
    that is the automated-login path the audit calls the highest-risk code in
    the project: LinkedIn flags automated logins hardest, which is the whole
    reason the persistent-session design exists. A check that quietly implied it
    had verified the credential would be the same lie as a selector report that
    passes a path it never looked at.
    """
    profiles = pm.load_profiles().get("profiles", {})
    profile = profiles.get(name)
    if profile is None:
        return jsonify({"error": f"No profile named '{name}'"}), 404

    stored_in = profile.get("password_location") or "file"
    roundtrip_ok, detail = False, ""
    try:
        secret = pm.get_profile_password(profile, name)
        roundtrip_ok = bool(secret)
        if not roundtrip_ok:
            detail = ("Nothing came back. The entry is missing or empty, so an "
                      "automatic re-login would have no password to use.")
    except Exception as e:
        detail = f"Could not read it back ({type(e).__name__})."

    return jsonify({
        "profile": name,
        "username": profile.get("username"),
        "stored_in": stored_in,
        "in_keychain": stored_in == pm.LOCATION_KEYRING,
        "plaintext_in_file": profile.get("password") is not None,
        "roundtrip_ok": roundtrip_ok,
        "detail": detail,
        "checked": [
            "the username recorded for this profile",
            "where the password is stored",
            "that the password can be read back",
        ],
        "not_checked": [
            "whether these credentials are correct on LinkedIn. Only signing in "
            "can tell you that, and this tool will not sign in to test a "
            "password. You find out when you log in yourself.",
        ],
    })


@app.route('/api/health/<profile_name>/report', methods=['GET'])
def selector_health_report(profile_name):
    """GET /api/health/<name>/report — the whole registry, both paths, at a glance.

    Merges whatever per-page runs have been saved into one report in which
    EVERY selector appears, with the ones that were not tested marked as such
    and told why. Runs nothing and needs no session: it reads saved results.

    This endpoint exists because of what happened on 2026-07-31. A feed-only run
    reported HEALTHY minutes after a posting run placed zero of three comments,
    and it was not wrong about the feed. The fix is a report that cannot present
    partial coverage as a clean bill of health.
    """
    report = shc.build_health_report(shc.load_saved_reports(profile_name))
    return jsonify(report)


# ─── API: Auto-Connector ─────────────────────────────────────────────────────

@app.route('/api/connector/<profile_name>/stats', methods=['GET'])
def connector_stats(profile_name):
    """Get connection stats for a profile."""
    from .auto_connector import ConnectionTracker
    tracker = ConnectionTracker(profile_name)
    stats = tracker.get_stats()
    return jsonify(stats)


@app.route('/api/connector/<profile_name>/start', methods=['POST'])
def start_connector(profile_name):
    """Start the auto-connector."""
    body = request.json or {}
    search_url = body.get('search_url', '')
    max_requests = body.get('max_requests', 25)
    max_pages = body.get('max_pages', 10)
    note = body.get('note', '')

    if not search_url:
        return jsonify({"error": "search_url is required"}), 400

    job_id = f"connect_{profile_name}_{int(time.time())}"

    def do_connect(jid, pname, url, mx, pg, nt):
        """Background job: run the auto-connector subprocess."""
        log_job(jid, f"Starting auto-connector for {pname}...")

        cmd = [
            sys.executable, "-m", "linkedin_automation.auto_connector",
            url,
            "--max", str(mx),
            "--pages", str(pg),
            "--profile", pname
        ]

        if nt:
            cmd.extend(["--note", nt])

        def _register(proc):
            # Store process ref so the stop-connector endpoint can kill it.
            jobs[jid]["process"] = proc
            jobs[jid]["profile"] = pname

        returncode, _ = run_subprocess(jid, cmd, on_start=_register)

        if returncode == pm.EXIT_LOGIN_REQUIRED:
            raise pm.LoginRequiredError(
                f"Login required. Press \"Log in\" in the dashboard header, or run: "
                f"python tools/login_check.py --profile {pname}"
            )
        if returncode != 0:
            raise RuntimeError(f"Auto-connector exited with code {returncode}")

        log_job(jid, "Connector finished")
        return {"completed": True}

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, do_connect, profile_name, search_url, max_requests, max_pages, note,
            profile=profile_name, task_type="browser", category="connector")
    return jsonify({"job_id": job_id})


@app.route('/api/connector/<profile_name>/stop', methods=['POST'])
def stop_connector(profile_name):
    """Stop a running auto-connector."""
    # Method 1: Create a stop file that the connector checks for
    stop_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f".stop_connector_{profile_name}")
    try:
        with open(stop_file, 'w') as f:
            f.write("stop")
    except Exception as e:
        return jsonify({"error": f"Failed to create stop file: {e}"}), 500

    # Method 2: Also try to terminate the subprocess directly
    for jid, job in jobs.items():
        if (job.get("status") == "running" and
            job.get("profile") == profile_name and
            "process" in job):
            try:
                proc = job["process"]
                if proc.poll() is None:  # Still running
                    proc.terminate()
                    log_job(jid, "⛔ Stop requested — terminating process...")
                    job["status"] = "stopped"
            except Exception as e:
                log_job(jid, f"Error terminating: {e}")

    return jsonify({"stopped": True, "message": f"Stop signal sent for {profile_name}"})


@app.route('/api/connector/<profile_name>/history', methods=['GET'])
def connector_history(profile_name):
    """Get recent connection request history."""
    from .auto_connector import ConnectionTracker
    tracker = ConnectionTracker(profile_name)
    recent = tracker.data.get("sent_requests", [])[-50:]  # Last 50
    recent.reverse()
    return jsonify({"requests": recent})


# ─── API: Post Generator & Queue ─────────────────────────────────────────────

@app.route('/api/poster/<profile_name>/queue', methods=['GET'])
def poster_queue(profile_name):
    """Get the post queue."""
    from .post_generator import PostQueue
    queue = PostQueue(profile_name)
    posts = queue.list_queued()
    next_post = queue.get_next()
    next_id = next_post["id"] if next_post else None
    history = queue.history[-20:]
    history.reverse()
    return jsonify({"posts": posts, "next_id": next_id, "history": history})


@app.route('/api/poster/<profile_name>/generate', methods=['POST'])
def poster_generate(profile_name):
    """Generate thought leadership post(s)."""
    body = request.json or {}
    count = body.get('count', 1)
    style = body.get('style', None)
    topic = body.get('topic', None)
    model = body.get('model', 'gpt-4o-mini')

    job_id = f"postgen_{profile_name}_{int(time.time())}"

    def do_generate(jid, pname, cnt, sty, top, mdl):
        """Background job: run the LinkedIn post generator subprocess."""
        from .post_generator import PostGenerator
        gen = PostGenerator(profile_name=pname, model=mdl)
        results = []
        for i in range(cnt):
            log_job(jid, f"Generating post {i+1}/{cnt}...")
            post = gen.generate_thought_leadership(style=sty, topic=top)
            log_job(jid, f"✓ Post #{post.get('id', '?')} created [{post.get('style', '')}]")
            log_job(jid, f"  {post['text'][:120]}...")
            results.append(post)
            if i < cnt - 1:
                time.sleep(1)
        log_job(jid, f"Done — {len(results)} post(s) generated")
        return {"posts": results}

    run_job(job_id, do_generate, profile_name, count, style, topic, model,
            profile=profile_name, task_type="api", category="generate_posts")
    return jsonify({"job_id": job_id})


@app.route('/api/poster/<profile_name>/article', methods=['POST'])
def poster_article(profile_name):
    """Generate a post from an article URL."""
    body = request.json or {}
    url = body.get('url', '')
    model = body.get('model', 'gpt-4o-mini')

    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = f"article_{profile_name}_{int(time.time())}"

    def do_article(jid, pname, article_url, mdl):
        """Background job: generate a post from an article URL."""
        from .post_generator import PostGenerator
        gen = PostGenerator(profile_name=pname, model=mdl)
        log_job(jid, f"Fetching article: {article_url}")
        post = gen.generate_from_article(article_url)
        log_job(jid, f"✓ Article post #{post.get('id', '?')} created")
        log_job(jid, f"  {post['text'][:120]}...")
        return {"post": post}

    run_job(job_id, do_article, profile_name, url, model,
            profile=profile_name, task_type="api", category="generate_article")
    return jsonify({"job_id": job_id})


@app.route('/api/poster/<profile_name>/post', methods=['POST'])
def poster_publish(profile_name):
    """Publish a post to LinkedIn."""
    body = request.json or {}
    post_id = body.get('id', None)

    job_id = f"publish_{profile_name}_{int(time.time())}"

    def do_publish(jid, pname, pid):
        """Background job: publish a queued post to the feed."""
        from .post_generator import PostGenerator
        gen = PostGenerator(profile_name=pname)

        if pid:
            log_job(jid, f"Publishing post #{pid}...")
            success = gen.post_by_id(pid, profile_name=pname)
        else:
            log_job(jid, "Publishing next post in queue...")
            success = gen.post_next(profile_name=pname)

        if success:
            log_job(jid, "✓ Post published to LinkedIn!")
        else:
            raise RuntimeError("Failed to publish post")
        return {"published": success}

    if not can_start_browser_task(profile_name):
        return jsonify({"error": f"A browser task is already running for {profile_name}. Wait for it to finish."}), 409
    run_job(job_id, do_publish, profile_name, post_id,
            profile=profile_name, task_type="browser", category="publish")
    return jsonify({"job_id": job_id})


@app.route('/api/poster/<profile_name>/queue/<int:post_id>', methods=['DELETE'])
def poster_remove(profile_name, post_id):
    """Remove a post from the queue."""
    from .post_generator import PostQueue
    queue = PostQueue(profile_name)
    if queue.remove(post_id):
        return jsonify({"removed": True})
    return jsonify({"error": "Post not found"}), 404


# ─── API: Jobs ────────────────────────────────────────────────────────────────

@app.route('/api/jobs/active', methods=['GET'])
def active_jobs_list():
    """List all active/running jobs."""
    active = get_active_jobs()
    result = []
    for jid, j in active.items():
        result.append({
            "job_id": jid,
            "profile": j.get("profile", ""),
            "task_type": j.get("task_type", ""),
            "category": j.get("category", ""),
            "started": j.get("started", ""),
            "last_log": j["log"][-1] if j["log"] else "",
        })
    return jsonify({"jobs": result})


@app.route('/api/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    """GET /api/jobs/<job_id> — return a serializable snapshot of a job's state."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    # Filter out non-serializable keys (like subprocess.Popen objects)
    safe_keys = {"status", "progress", "log", "result", "error", "started", "login_required"}
    safe_job = {k: v for k, v in jobs[job_id].items() if k in safe_keys}
    return jsonify(safe_job)


# ─── Scheduler ────────────────────────────────────────────────────────────────
# The scheduler runs its own actions through run_job / the browser lock, exactly
# like a manual click. These executor callbacks are the only coupling; the engine
# itself (scheduler.py) has no Flask imports.

def _sched_submit_post_job(profile_name, comments_file, count):
    """Start a scheduled posting job unless the browser lock is held."""
    if not can_start_browser_task(profile_name):
        logger.info("Scheduler: browser busy for %s, post job not submitted", profile_name)
        return None
    job_id = f"sched_post_{profile_name}_{int(time.time())}"
    run_job(job_id, _post_comments_job, profile_name, comments_file, count,
            profile=profile_name, task_type="browser", category="scheduled_post")
    return job_id


def _sched_submit_scrape_job(profile_name, max_posts, min_quality):
    """Start a scheduled scrape job unless the browser lock is held."""
    if not can_start_browser_task(profile_name):
        logger.info("Scheduler: browser busy for %s, scrape job not submitted", profile_name)
        return None
    job_id = f"sched_scrape_{profile_name}_{int(time.time())}"
    run_job(job_id, _scrape_job, profile_name, max_posts, min_quality,
            profile=profile_name, task_type="browser", category="scheduled_scrape")
    return job_id


scheduler_engine = scheduler_mod.Scheduler(
    submit_post_job=_sched_submit_post_job,
    submit_scrape_job=_sched_submit_scrape_job,
    browser_available=can_start_browser_task,
    list_profiles=lambda: list(pm.list_profiles().get("profiles", {}).keys()),
)


@app.route('/api/scheduler/<profile_name>/status', methods=['GET'])
def scheduler_status(profile_name):
    """GET — scheduler state: master flag, per-job windows with today's rolled
    fire times + skip flags, count ranges, and recent scheduled runs."""
    return jsonify(scheduler_engine.status(profile_name))


@app.route('/api/scheduler/<profile_name>/toggle', methods=['POST'])
def scheduler_toggle(profile_name):
    """POST — enable/disable the master switch or a specific job.

    Body: ``{"job": "master"|"post_comments"|"scrape", "enabled": bool}``.
    """
    body = request.json or {}
    target = body.get("job", "master")
    enabled = body.get("enabled")
    if enabled is None:
        return jsonify({"error": "enabled (bool) is required"}), 400
    if target not in ("master",) + scheduler_mod.JOB_TYPES:
        return jsonify({"error": f"unknown job target '{target}'"}), 400
    sched = scheduler_engine.toggle(profile_name, target, bool(enabled))
    return jsonify({"ok": True, "scheduler": sched})


@app.route('/api/scheduler/<profile_name>/config', methods=['POST'])
def scheduler_config(profile_name):
    """POST — deep-merge a partial scheduler config (windows/counts/skip_chance)."""
    body = request.json
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    sched = scheduler_engine.update_config(profile_name, body)
    return jsonify({"ok": True, "scheduler": sched})


@app.route('/api/scheduler/<profile_name>/run-now', methods=['POST'])
def scheduler_run_now(profile_name):
    """POST — fire a job immediately for testing, ignoring window/skip/min-gap.

    Body: ``{"job": "post_comments"|"scrape"}``.
    """
    body = request.json or {}
    job = body.get("job")
    if job not in scheduler_mod.JOB_TYPES:
        return jsonify({"error": "job must be post_comments or scrape"}), 400
    result = scheduler_engine.run_now(profile_name, job)
    return jsonify({"ok": True, "result": result})


# ─── Serve Frontend ──────────────────────────────────────────────────────────

@app.route('/')
def index():
    """GET / — serve the single-page dashboard UI."""
    return send_file(os.path.join(_PACKAGE_DIR, 'templates', 'dashboard.html'))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    """Boot the dashboard: loopback bind, debugger off unless opted in."""
    pm.auto_migrate_from_env()
    port = get_port()
    debug = get_debug()
    print("\n" + "=" * 50)
    print("  LinkedIn Automation Dashboard")
    print(f"  http://localhost:{port}")
    if debug:
        print("")
        print("  DEBUG MODE IS ON. The Werkzeug debugger is live, and it")
        print("  executes Python typed into the browser. Local use only.")
    print("=" * 50 + "\n")
    # Debug mode runs the reloader, which forks a child that re-imports this
    # module. Start the scheduler only in the process that will actually serve,
    # or two scheduler threads race over the same jobs. The previous guard read
    # app.debug, which is False until app.run() sets it, so it was true in both
    # processes and started the scheduler twice. Read the computed value.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        scheduler_engine.start()
    app.run(host=HOST, port=port, debug=debug)


if __name__ == '__main__':
    main()