# Shared module for persistent Chrome sessions and multi-profile management
"""Multi-profile credential storage and Chrome-session management.

Stores LinkedIn profiles (username/password/session dir) in
``data/profiles/profiles.json``, builds Selenium Chrome drivers backed by
per-profile persistent ``user-data-dir`` sessions, and exposes the process exit
codes and ``LoginRequiredError`` used by the CLI scripts and dashboard.
"""

import os
import json
import time
import logging
import getpass
import sys
from enum import Enum
from typing import Optional, Dict, Tuple

# Route TLS through the OS trust store so webdriver-manager (which uses requests)
# can fetch/verify chromedriver behind corporate TLS-intercepting proxies that
# otherwise cause "Could not reach host". No-op if truststore isn't installed.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv

# Optional: passwords go to the OS credential store when one is available.
# Guarded so an existing checkout that hasn't reinstalled requirements still
# imports and keeps working with in-file storage.
try:
    import keyring
except Exception:  # pragma: no cover - exercised via the fallback tests
    keyring = None

load_dotenv()

logger = logging.getLogger(__name__)


# ─── Process exit codes ───────────────────────────────────────────────────────
# CLI scripts (post finder, poster) use these so the dashboard can tell a login
# failure apart from any other error and show an actionable message.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOGIN_REQUIRED = 2
# A monitor that only fails on total breakage cannot drive an alert: the
# interesting state is the one where something has started to slip. The selector
# watchdog exits with this on DEGRADED so a scheduled run can act on it, while
# still being distinguishable from BROKEN (EXIT_ERROR) and from a crash.
EXIT_DEGRADED = 3

# Explicit page load ceiling. Selenium defaults to 300s, which turns a slow or
# hanging navigation into a multi-minute stall with no diagnostic.
PAGE_LOAD_TIMEOUT_SECONDS = 45


def wait_for_human(prompt: str) -> bool:
    """Block for a human at a terminal. Return False if there is no terminal.

    The dashboard launches the finder, poster and connector with
    ``sys.executable -m``, so those processes have no stdin. A bare ``input()``
    on such a path does not prompt anybody: it blocks forever, and the job sits
    in the dashboard as "running" with no further output and no error.

    Callers must handle False by stopping with an actionable message rather
    than continuing as if the human had answered.
    """
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        input(prompt)
        return True
    except (EOFError, KeyboardInterrupt):
        return False


class LoginStatus(Enum):
    """Three-state login result.

    A two-state check has to invent an answer when it cannot tell, and the
    invented answer here was "logged out", which is the branch that types the
    user's password into LinkedIn. UNKNOWN exists so that branch is never
    entered on a guess.
    """

    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"


class LoginRequiredError(RuntimeError):
    """Raised when a LinkedIn session could not be established.

    Signals "the user must log in manually" (expired or missing persistent
    session) as opposed to a generic runtime failure.
    """


# ─── Configuration ────────────────────────────────────────────────────────────

# Project root = one level above this package dir. All runtime data and the
# default-config template live at the project root (never inside the package),
# so paths resolve identically no matter which directory the app is launched
# from. This module lives at <root>/linkedin_automation/profile_manager.py.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.join(PROJECT_ROOT, "data")

PROFILES_DIR = os.path.join(DATA_ROOT, "profiles")
PROFILES_FILE = os.path.join(PROFILES_DIR, "profiles.json")
CHROME_SESSIONS_DIR = os.path.join(PROFILES_DIR, "chrome_sessions")

os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(CHROME_SESSIONS_DIR, exist_ok=True)


# ─── Credential Storage ──────────────────────────────────────────────────────
# Passwords belong in the OS credential store (Keychain on macOS, Credential
# Manager on Windows, Secret Service on Linux), not in profiles.json. Each
# profile records where its password lives:
#
#   "password_location": "keyring"  -> read it from the OS store
#   "password_location": "file"     -> read the legacy "password" field
#
# Profiles written before this change have no marker and a populated
# "password" field; they are treated as "file" and can be moved across with
# migrate_passwords_to_keyring(). Machines with no keyring backend (headless
# Linux, minimal containers) keep working on in-file storage.

KEYRING_SERVICE = "linkedin-autocomment"
LOCATION_KEYRING = "keyring"
LOCATION_FILE = "file"


def keyring_available() -> bool:
    """True when a usable OS credential store is present."""
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    # keyring installs a "fail" backend when nothing usable is found; its
    # priority is 0 and any real backend scores higher.
    return getattr(backend, "priority", 0) > 0


def set_profile_password(name: str, password: str) -> str:
    """Store ``password`` for profile ``name`` and return where it landed.

    Returns ``LOCATION_KEYRING`` when the OS store accepted it, otherwise
    ``LOCATION_FILE`` so the caller knows to keep the value in profiles.json.
    """
    if keyring_available():
        try:
            keyring.set_password(KEYRING_SERVICE, name, password)
            return LOCATION_KEYRING
        except Exception as e:
            logger.warning(
                "Could not write to the OS credential store (%s); "
                "falling back to in-file storage for profile '%s'", e, name
            )
    return LOCATION_FILE


def get_profile_password(profile: Dict, name: str = None) -> str:
    """Return a profile's password, wherever it is stored.

    ``name`` is only needed for keyring lookups; it falls back to the profile's
    own recorded name when omitted.
    """
    location = profile.get("password_location")
    if location is None:
        # Legacy profile: no marker, password sits in the file.
        return profile.get("password") or ""

    if location == LOCATION_KEYRING:
        lookup = name or profile.get("name")
        if not lookup:
            logger.error("Cannot read keyring password without a profile name")
            return ""
        try:
            return keyring.get_password(KEYRING_SERVICE, lookup) or ""
        except Exception as e:
            logger.error("Could not read the OS credential store: %s", e)
            return ""

    return profile.get("password") or ""


def delete_profile_password(name: str):
    """Remove a profile's password from the OS store, if it is there."""
    if not keyring_available():
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except Exception:
        # Nothing stored under that name, or the store refused. Not fatal:
        # remove_profile still drops the profile entry itself.
        logger.debug("No keyring entry to delete for '%s'", name, exc_info=True)


# ─── Generation-provider API keys (ROADMAP Phase 8) ───────────────────────────
#
# A separate keyring service from LINKEDIN credentials, deliberately. Both are
# keyed by a short string, and LinkedIn entries are keyed by profile name: a
# profile called "openai" would otherwise collide with the OpenAI API key and
# one would overwrite the other.

API_KEY_SERVICE = "linkedin-autocomment-api-keys"


def set_api_key(provider: str, api_key: str) -> str:
    """Store a provider's API key in the OS credential store.

    Returns ``LOCATION_KEYRING`` on success. Unlike LinkedIn passwords there is
    no in-file fallback: an API key must never land in ``profiles.json``. When
    no credential store is available this raises, and the caller tells the user
    to use the environment variable instead.
    """
    if not api_key or not api_key.strip():
        raise ValueError("API key is empty")

    if not keyring_available():
        raise RuntimeError(
            "No OS credential store is available, so the API key was not "
            "saved. Set the provider's environment variable in .env instead."
        )

    keyring.set_password(API_KEY_SERVICE, provider, api_key.strip())
    return LOCATION_KEYRING


def get_api_key(provider: str) -> str:
    """Return a provider's stored API key, or '' when there is none."""
    if not keyring_available():
        return ""
    try:
        return keyring.get_password(API_KEY_SERVICE, provider) or ""
    except Exception as e:
        logger.error("Could not read the API key for '%s': %s", provider, e)
        return ""


def delete_api_key(provider: str):
    """Remove a provider's API key from the OS credential store."""
    if not keyring_available():
        return
    try:
        keyring.delete_password(API_KEY_SERVICE, provider)
    except Exception:
        logger.debug("No stored API key to delete for '%s'", provider, exc_info=True)


def api_key_status(provider: str) -> Dict:
    """Describe whether a key is set, without revealing it.

    Returns ``{"set", "source", "last4"}``. ``last4`` is the last four
    characters and is the *only* part of the value that ever leaves this
    function, which is what lets the Settings screen confirm which key is
    installed without the key being readable from the UI or a response body.
    """
    from . import providers

    key = get_api_key(provider)
    source = LOCATION_KEYRING

    if not key:
        env_name = providers.API_KEY_ENV.get(provider, "")
        key = os.environ.get(env_name, "").strip() if env_name else ""
        source = "env" if key else None

    if not key:
        return {"set": False, "source": None, "last4": ""}

    return {"set": True, "source": source, "last4": key[-4:]}


def migrate_passwords_to_keyring() -> Tuple[int, int]:
    """Move any in-file passwords into the OS credential store.

    Returns ``(moved, left_in_file)``. Safe to run repeatedly, and safe to
    interrupt: a password is only cleared from profiles.json after the OS store
    has accepted it.
    """
    if not keyring_available():
        logger.warning(
            "No OS credential store available; leaving passwords in profiles.json"
        )
        data = load_profiles()
        return 0, len(data["profiles"])

    data = load_profiles()
    moved = 0
    left = 0

    for name, profile in data["profiles"].items():
        if profile.get("password_location") == LOCATION_KEYRING:
            continue
        password = profile.get("password")
        if not password:
            continue

        if set_profile_password(name, password) == LOCATION_KEYRING:
            profile["password"] = None
            profile["password_location"] = LOCATION_KEYRING
            moved += 1
        else:
            left += 1

    if moved:
        save_profiles(data)
        logger.info("Moved %d password(s) into the OS credential store", moved)

    return moved, left


# ─── Profile Storage ─────────────────────────────────────────────────────────

def load_profiles() -> Dict:
    """Load all profiles from disk, tolerating a missing, empty, or corrupt file.

    Always returns a dict with normalized ``profiles`` (dict) and ``default``
    (a name that exists in ``profiles``, else None) keys. A stale ``default``
    pointing at a removed profile is dropped.
    """
    default = {"profiles": {}, "default": None}

    if not os.path.exists(PROFILES_FILE):
        return default

    try:
        # utf-8-sig, not utf-8: Windows tools (PowerShell Set-Content) write a
        # BOM by default, and a BOM raised UnicodeDecodeError here, which the
        # handler below reported as "unreadable; starting fresh" — silently
        # losing every profile. utf-8-sig reads correctly with or without one.
        with open(PROFILES_FILE, 'r', encoding='utf-8-sig') as f:
            content = f.read().strip()
        if not content:
            return default
        data = json.loads(content)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning(f"profiles.json unreadable ({e}); treating as empty")
        return default

    if not isinstance(data, dict):
        logger.warning("profiles.json is not a JSON object; treating as empty")
        return default

    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}

    default_name = data.get("default")
    if default_name not in profiles:
        default_name = None

    return {"profiles": profiles, "default": default_name}


def save_profiles(data: Dict):
    """Atomically save profiles to disk (write to a temp file, then rename)."""
    os.makedirs(PROFILES_DIR, exist_ok=True)
    tmp_path = PROFILES_FILE + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, PROFILES_FILE)


def get_profile(name: str) -> Optional[Dict]:
    """Get a single profile by name.

    The returned copy carries its own ``name`` so downstream callers (notably
    ``login``) can look the password up in the OS credential store. The stored
    JSON is unchanged — this key exists only on the in-memory copy.
    """
    data = load_profiles()
    profile = data["profiles"].get(name)
    if profile is None:
        return None
    return {**profile, "name": name}


def get_default_profile_name() -> Optional[str]:
    """Get the default profile name."""
    data = load_profiles()
    # If a default is explicitly set, use it
    if data.get("default") and data["default"] in data.get("profiles", {}):
        return data["default"]
    # If only one profile exists, it's the default
    profiles = data.get("profiles", {})
    if len(profiles) == 1:
        return list(profiles.keys())[0]
    return None


def add_profile(name: str, username: str, password: str, set_default: bool = False) -> Dict:
    """Add a new profile.

    The password goes to the OS credential store when one is available; only
    when it isn't does it stay in profiles.json (see the Credential Storage
    section above).
    """
    data = load_profiles()

    # Stored RELATIVE to PROFILES_DIR. An absolute path captured here is only
    # correct until the install is moved or renamed, after which Chrome is
    # handed a dead path, creates a fresh empty profile there, and the user
    # lands on the login wall with no indication why. Resolution happens at
    # use time via resolve_session_dir().
    session_dir = os.path.join(CHROME_SESSIONS_DIR, name)
    os.makedirs(session_dir, exist_ok=True)
    stored_session_dir = os.path.join("chrome_sessions", name)

    location = set_profile_password(name, password)

    data["profiles"][name] = {
        "username": username,
        "password": None if location == LOCATION_KEYRING else password,
        "password_location": location,
        "session_dir": stored_session_dir,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_used": None
    }
    
    # Set as default if requested or if it's the first profile
    if set_default or len(data["profiles"]) == 1:
        data["default"] = name
    
    save_profiles(data)
    logger.info(f"Profile '{name}' added successfully")
    return data["profiles"][name]


def remove_profile(name: str):
    """Remove a profile."""
    data = load_profiles()
    if name in data["profiles"]:
        delete_profile_password(name)
        del data["profiles"][name]
        if data.get("default") == name:
            # Set a new default if available
            remaining = list(data["profiles"].keys())
            data["default"] = remaining[0] if remaining else None
        save_profiles(data)
        logger.info(f"Profile '{name}' removed")
    else:
        logger.warning(f"Profile '{name}' not found")


def set_default_profile(name: str):
    """Set the default profile."""
    data = load_profiles()
    if name in data["profiles"]:
        data["default"] = name
        save_profiles(data)
        logger.info(f"Default profile set to '{name}'")
    else:
        logger.error(f"Profile '{name}' not found")


def list_profiles() -> Dict:
    """List all profiles."""
    data = load_profiles()
    return data


def update_last_used(name: str):
    """Update the last-used timestamp for a profile."""
    data = load_profiles()
    if name in data["profiles"]:
        data["profiles"][name]["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_profiles(data)


# ─── Profile-Specific Data Directories ────────────────────────────────────────

def get_data_dir(profile_name: str = None, subdir: str = None) -> str:
    """
    Get the profile-specific data directory.
    
    Structure:
        data/<profile_name>/
        data/<profile_name>/linkedin_timeline/
        data/<profile_name>/quality_comments/
        data/<profile_name>/quality_comments/debug_screenshots/
    
    Args:
        profile_name: Profile name (uses default if None)
        subdir: Optional subdirectory (e.g. 'linkedin_timeline', 'quality_comments')
    
    Returns:
        Absolute path to the directory (created if needed)
    """
    if not profile_name:
        profile_name = get_default_profile_name()
    if not profile_name:
        profile_name = "default"
    
    base = os.path.join(DATA_ROOT, profile_name)
    
    if subdir:
        path = os.path.join(base, subdir)
    else:
        path = base
    
    os.makedirs(path, exist_ok=True)
    return path


def get_timeline_dir(profile_name: str = None) -> str:
    """Get profile-specific linkedin_timeline directory."""
    return get_data_dir(profile_name, "linkedin_timeline")


def get_comments_dir(profile_name: str = None) -> str:
    """Get profile-specific quality_comments directory."""
    return get_data_dir(profile_name, "quality_comments")


def get_screenshots_dir(profile_name: str = None) -> str:
    """Get profile-specific debug_screenshots directory."""
    return get_data_dir(profile_name, os.path.join("quality_comments", "debug_screenshots"))


def get_progress_file(profile_name: str = None) -> str:
    """Get profile-specific posting_progress.json path."""
    comments_dir = get_comments_dir(profile_name)
    return os.path.join(comments_dir, "posting_progress.json")


# ─── Per-Profile Config ───────────────────────────────────────────────────────

# Default config template, shipped at the project root (not inside the package).
DEFAULT_CONFIG_FILE = os.path.join(PROJECT_ROOT, "default_profile_config.json")


def get_config_path(profile_name: str = None) -> str:
    """Path to a profile's config: data/<profile_name>/profile_config.json."""
    return os.path.join(get_data_dir(profile_name), "profile_config.json")


def load_default_config() -> Dict:
    """Load the default config template; fall back to a minimal dict if missing."""
    try:
        with open(DEFAULT_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"default_profile_config.json unreadable ({e}); using minimal defaults")
        return {
            "display_name": "", "headline": "", "niche": "",
            "post_finder": {
                "keywords_tier1": [], "keywords_tier2": [],
                "min_quality_score": 5, "max_posts_per_scan": 50,
                "post_types_to_engage": ["thought_leadership", "technical", "other"],
            },
            "comment_generator": {
                "persona": "", "tone": "", "voice": "",
                "topics_of_expertise": [], "things_to_avoid": [],
                "comment_length_range": [15, 60], "style_mix": {},
            },
            "connector": {"note_template": "", "max_daily_requests": 25},
        }


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Return base deep-merged with override (override wins; nested dicts merged).

    Used so a user config is filled in with any keys added to the default
    template later (additive) without overwriting the user's own values.
    """
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def save_profile_config(profile_name: str, config: Dict):
    """Atomically write a profile's config (temp file + rename)."""
    path = get_config_path(profile_name)
    tmp_path = path + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, path)


def get_profile_config(profile_name: str = None) -> Dict:
    """Return a profile's config, creating it from the default on first use.

    Existing configs are deep-merged onto the default template so newly-added
    default keys appear without clobbering user values. Never raises: a corrupt
    config falls back to (and is treated as) the default.
    """
    path = get_config_path(profile_name)
    default = load_default_config()

    if not os.path.exists(path):
        save_profile_config(profile_name, default)
        logger.info(f"Created default profile config at {path}")
        return default

    try:
        with open(path, 'r', encoding='utf-8') as f:
            user_config = json.load(f)
        if not isinstance(user_config, dict):
            raise ValueError("config is not a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"profile_config.json unreadable ({e}); using defaults")
        return default

    return _deep_merge(default, user_config)


def reset_profile_config(profile_name: str = None) -> Dict:
    """Overwrite a profile's config with the default template and return it."""
    default = load_default_config()
    save_profile_config(profile_name, default)
    logger.info(f"Reset profile config for '{profile_name or get_default_profile_name()}'")
    return default


# ─── Auto-migrate from .env ──────────────────────────────────────────────────

def auto_migrate_from_env():
    """If no profiles exist but .env has credentials, create a default profile."""
    data = load_profiles()
    if data["profiles"]:
        return  # Already have profiles
    
    username = os.getenv('LINKEDIN_USERNAME') or os.getenv('LINKEDIN_ALT_USERNAME')
    password = os.getenv('LINKEDIN_PASSWORD') or os.getenv('LINKEDIN_ALT_PASSWORD')
    
    if username and password:
        logger.info("Auto-migrating .env credentials to profile 'default'...")
        add_profile("default", username, password, set_default=True)


# ─── Chrome Driver with Persistent Session ────────────────────────────────────

def _looks_absolute(path: str) -> bool:
    """True for an absolute path on *either* platform.

    ``os.path.isabs`` only understands the host's convention, so a Windows
    path like ``C:\\Users\\...`` reads as *relative* on macOS and would be
    joined onto the data root. profiles.json is portable data — a user with
    installs on both machines can copy it — so absoluteness has to be judged
    for both conventions.
    """
    if not path:
        return False
    if os.path.isabs(path):
        return True
    if len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] in "\\/":
        return True          # C:\... or C:/...
    return path.startswith("\\\\")   # UNC \\server\share


def resolve_session_dir(profile: Dict, name: str = None) -> str:
    """Return the absolute Chrome session directory for a profile.

    Handles three cases so an install can be moved without breaking:

    * **Relative** (written by current code): joined to the live ``PROFILES_DIR``.
    * **Absolute and still present** (legacy, install not moved): used as-is.
    * **Absolute and gone** (legacy, install moved): falls back to the canonical
      location under the current data root, with a warning. Previously this was
      the silent failure that produced an empty Chrome profile and a login wall.
    """
    stored = (profile or {}).get("session_dir") or ""
    name = name or (profile or {}).get("name")
    canonical = os.path.join(CHROME_SESSIONS_DIR, name) if name else ""

    if stored and not _looks_absolute(stored):
        return os.path.join(PROFILES_DIR, stored)

    if stored and os.path.isdir(stored):
        return stored

    if canonical:
        if stored:
            logger.warning(
                "Profile %r records a session directory that no longer exists "
                "(%s). The install was probably moved or renamed. Using %s "
                "instead.", name, stored, canonical
            )
        return canonical

    return stored


def session_exists(session_dir: str) -> bool:
    """Best-effort check for whether a profile already has a Chrome session.

    A freshly created profile's session dir is empty; Chrome populates a
    ``Default`` subdirectory (cookies, localStorage, etc.) after the first real
    run. Used only to decide whether to print first-run login instructions.
    """
    if not session_dir or not os.path.isdir(session_dir):
        return False
    default_dir = os.path.join(session_dir, "Default")
    return os.path.isdir(default_dir) and bool(os.listdir(default_dir))


def create_driver(profile_name: str = None, headless: bool = False) -> Tuple[webdriver.Chrome, Dict]:
    """
    Create a Chrome driver with persistent session for the given profile.
    
    Returns (driver, profile_info) tuple.
    """
    # Auto-migrate on first use
    auto_migrate_from_env()
    
    # Resolve profile
    if not profile_name:
        profile_name = get_default_profile_name()
    
    if not profile_name:
        raise ValueError(
            "No profile specified and no default set.\n"
            "Run: python linkedin_profile_manager.py add <name> \n"
            "  or use --profile <name>"
        )
    
    profile = get_profile(profile_name)
    if not profile:
        raise ValueError(
            f"Profile '{profile_name}' not found.\n"
            f"Available profiles: {', '.join(load_profiles()['profiles'].keys()) or 'none'}\n"
            f"Run: python linkedin_profile_manager.py add {profile_name}"
        )
    
    logger.info(f"Using profile: '{profile_name}' ({profile['username']})")

    # If this profile has no persistent Chrome session yet, tell the user how to
    # establish one instead of letting an automated login silently fail later.
    if not session_exists(resolve_session_dir(profile, profile_name)):
        logger.warning(
            "No existing Chrome session for profile '%s'. A browser window will "
            "open: log in to LinkedIn manually, then close it — your session "
            "will persist for future runs. You can also run: "
            "python tools/login_check.py --profile %s",
            profile_name, profile_name,
        )

    # Setup Chrome options with persistent user-data-dir
    options = Options()
    session_dir = resolve_session_dir(profile, profile_name)
    os.makedirs(session_dir, exist_ok=True)
    options.add_argument(f"--user-data-dir={os.path.abspath(session_dir)}")
    options.add_argument("--profile-directory=Default")
    
    # Anti-detection
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    # Suppress noise
    options.add_argument('--log-level=3')
    options.add_argument('--disable-logging')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    
    if headless:
        options.add_argument('--headless=new')
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    # Selenium's default page load timeout is 300s. A hanging navigation then
    # blocks for five minutes and surfaces as an ambiguous login status, which
    # is what previously led to an automated credential login. Fail fast and
    # let the caller report UNKNOWN instead.
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    driver.maximize_window()
    
    # Anti-detection JS
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    
    update_last_used(profile_name)
    
    return driver, profile


# URL fragments that mean LinkedIn bounced a logged-out session to an auth wall.
# Checked FIRST (a login redirect often carries the original path in a
# session_redirect param, so "login" must win over any "/search" in that param).
LOGGED_OUT_URL_MARKERS = ("login", "authwall", "checkpoint", "challenge", "/signup", "/uas/")

# URL fragments for authenticated LinkedIn pages a caller may have loaded. Includes
# people-search (/search/results/) so the connector's search-page callers aren't
# falsely reported as logged out just because the URL isn't /feed/.
LOGGED_IN_URL_MARKERS = ("/feed", "/in/", "mynetwork", "/search/results", "/search/")


def is_logged_in_on_page(driver: webdriver.Chrome) -> bool:
    """Determine login status from the page already loaded — without navigating.

    URL-based (no brittle element wait): a logged-out session is redirected to a
    login/authwall/checkpoint page, so those markers mean logged out; otherwise
    staying on an authenticated URL (the feed, a profile, my-network, or a
    /search/results/ page) means logged in. Works for callers that have already
    navigated to /feed/ (avoiding a degraded re-navigation, see BLOCKED.md) AND
    for the connector/search-health callers sitting on a search results page.

    Because it checks the URL and not the presence of any specific element, an
    empty search (a valid logged-in page with zero result cards) is correctly
    reported as logged in — "no results" is not "not logged in".
    """
    return login_status_on_page(driver) is LoginStatus.LOGGED_IN


def login_status_on_page(driver: webdriver.Chrome) -> "LoginStatus":
    """Return the three-state login status of the page already loaded.

    ``is_logged_in_on_page`` collapses this to a bool for callers that only
    need "may I proceed". Anything that would act on a negative — above all
    ``login``, which types the stored password — must use this instead.

    UNKNOWN is not a failure. It means the URL matched neither the logged-out
    markers nor the logged-in ones: a page that did not finish loading, an
    interstitial, or a LinkedIn URL shape nobody has seen yet. Treating that as
    "logged out" is what previously authorized an automated credential login
    against a session that was in fact perfectly valid.
    """
    try:
        current_url = driver.current_url or ""
    except Exception as e:
        # The window is gone, or the driver is dead. Not evidence of logout.
        logger.warning(
            "Could not read the current URL to determine login status (%s); "
            "reporting UNKNOWN rather than assuming logged out", e
        )
        return LoginStatus.UNKNOWN

    if any(marker in current_url for marker in LOGGED_OUT_URL_MARKERS):
        return LoginStatus.LOGGED_OUT
    if any(marker in current_url for marker in LOGGED_IN_URL_MARKERS):
        return LoginStatus.LOGGED_IN

    logger.warning(
        "Login status is UNKNOWN: URL %r matches neither the logged-in nor the "
        "logged-out markers. Not treating this as logged out.",
        current_url[:120],
    )
    return LoginStatus.UNKNOWN


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """Check login status by navigating to /feed/, then inspecting the URL.

    URL-based (no brittle element wait): a logged-out session is redirected to a
    login/authwall page, so reaching /feed/ (or another authenticated page) means
    the session is valid. The old element wait used stale selectors and required
    the feed to be scrolled (posts lazy-load), producing false "not logged in".
    """
    return login_status(driver) is LoginStatus.LOGGED_IN


def login_status(driver: webdriver.Chrome) -> LoginStatus:
    """Navigate to the feed and return the three-state login status.

    A navigation that times out or raises yields UNKNOWN, never LOGGED_OUT.
    The two are not the same, and only one of them should lead to typing a
    password.
    """
    try:
        driver.get('https://www.linkedin.com/feed/')
    except Exception as e:
        logger.warning(
            "Navigation to the LinkedIn feed failed while checking login "
            "status (%s). Reporting UNKNOWN: a failed navigation is not "
            "evidence that the session expired.", e
        )
        return LoginStatus.UNKNOWN

    time.sleep(4)
    return login_status_on_page(driver)


def login(driver: webdriver.Chrome, profile: Dict) -> bool:
    """
    Login to LinkedIn. Checks if already logged in first (persistent session).
    """
    logger.info("Checking login status...")

    status = login_status(driver)
    if status is LoginStatus.LOGGED_IN:
        logger.info("✓ Already logged in (persistent session)")
        return True

    if status is LoginStatus.UNKNOWN:
        # Do NOT fall through to the credential login. An ambiguous result is
        # usually a page that did not load, not an expired session, and typing
        # the password is the single most flagged action available to us.
        logger.error(
            "Could not determine login status (the page matched neither the "
            "logged-in nor the logged-out markers). Refusing to attempt an "
            "automated credential login on an ambiguous result. Run: "
            "python tools/login_check.py --profile <name>"
        )
        return False

    logger.info("Not logged in, performing fresh login...")

    try:
        driver.get('https://www.linkedin.com/login')
        time.sleep(2)
        
        # Check if we're already redirected to feed (race condition with cookie)
        if "feed" in driver.current_url:
            logger.info("✓ Session restored during navigation")
            return True
        
        username = profile.get('username', '')
        password = get_profile_password(profile)

        if not username or not password:
            logger.error("Missing credentials in profile")
            return False
        
        driver.find_element(By.ID, "username").send_keys(username)
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        
        time.sleep(5)
        
        if "feed" in driver.current_url:
            logger.info("✓ Login successful")
            return True
        
        if "checkpoint" in driver.current_url or "challenge" in driver.current_url:
            logger.warning("⚠ Security checkpoint - please complete manually")
            if not wait_for_human("Press Enter after completing the checkpoint..."):
                logger.error(
                    "A LinkedIn security checkpoint needs a human, but this "
                    "process has no terminal (it was launched by the dashboard "
                    "or a scheduler). Stopping instead of blocking forever. "
                    "Run: python tools/login_check.py --profile <name>"
                )
                return False
            if "feed" in driver.current_url or "/in/" in driver.current_url:
                logger.info("✓ Checkpoint completed, logged in")
                return True
        
        logger.error(f"Login failed - landed on: {driver.current_url}")
        return False
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return False


# ─── Convenience function for scripts ─────────────────────────────────────────

def setup_and_login(profile_name: str = None, headless: bool = False) -> Tuple[webdriver.Chrome, Dict, str]:
    """
    One-call setup: create driver, login, return (driver, profile, profile_name).
    
    Usage in scripts:
        driver, profile, profile_name = setup_and_login(args.profile)
    """
    auto_migrate_from_env()
    
    if not profile_name:
        profile_name = get_default_profile_name()
    
    if not profile_name:
        raise ValueError("No profile available. Run: python linkedin_profile_manager.py add <name>")
    
    driver, profile = create_driver(profile_name, headless=headless)
    
    if not login(driver, profile):
        driver.quit()
        raise RuntimeError(f"Failed to log in with profile '{profile_name}'")
    
    return driver, profile, profile_name


# ─── CLI for Profile Management ──────────────────────────────────────────────

def cli():
    """Command-line interface for managing profiles."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='LinkedIn Profile Manager - Manage persistent sessions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all profiles
  python linkedin_profile_manager.py list
  
  # Add a new profile
  python linkedin_profile_manager.py add work
  
  # Add with credentials inline
  python linkedin_profile_manager.py add personal --username user@email.com --password mypass
  
  # Set default profile
  python linkedin_profile_manager.py default work
  
  # Remove a profile
  python linkedin_profile_manager.py remove old_profile
  
  # Test login for a profile
  python linkedin_profile_manager.py test work
  
  # Auto-migrate from .env
  python linkedin_profile_manager.py migrate
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command')
    
    # List
    subparsers.add_parser('list', help='List all profiles')
    
    # Add
    add_parser = subparsers.add_parser('add', help='Add a new profile')
    add_parser.add_argument('name', help='Profile name (e.g. work, personal)')
    add_parser.add_argument('--username', help='LinkedIn username/email')
    add_parser.add_argument('--password', help='LinkedIn password')
    add_parser.add_argument('--default', action='store_true', help='Set as default')
    
    # Remove
    remove_parser = subparsers.add_parser('remove', help='Remove a profile')
    remove_parser.add_argument('name', help='Profile name to remove')
    
    # Default
    default_parser = subparsers.add_parser('default', help='Set default profile')
    default_parser.add_argument('name', help='Profile name')
    
    # Test
    test_parser = subparsers.add_parser('test', help='Test login for a profile')
    test_parser.add_argument('name', nargs='?', help='Profile name (uses default if omitted)')
    
    # Migrate
    subparsers.add_parser('migrate', help='Migrate credentials from .env to profile')
    subparsers.add_parser(
        'secure-credentials',
        help='Move passwords out of profiles.json into the OS credential store',
    )

    # Config
    config_parser = subparsers.add_parser('config', help='View or edit a profile config')
    config_parser.add_argument('name', nargs='?', help='Profile name (uses default if omitted)')
    config_parser.add_argument('--edit', action='store_true', help='Open the config in your editor')
    config_parser.add_argument('--reset', action='store_true', help='Regenerate config from the default template')

    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == 'list':
        data = list_profiles()
        profiles = data.get("profiles", {})
        default = data.get("default")
        
        if not profiles:
            print("\nNo profiles configured.")
            print("Run: python linkedin_profile_manager.py add <name>")
            print("  or: python linkedin_profile_manager.py migrate  (import from .env)")
            return
        
        print(f"\n{'='*60}")
        print("LINKEDIN PROFILES")
        print(f"{'='*60}")
        
        for name, info in profiles.items():
            is_default = " [DEFAULT]" if name == default else ""
            print(f"\n  📋 {name}{is_default}")
            print(f"     Username:  {info['username']}")
            print(f"     Created:   {info.get('created', 'unknown')}")
            print(f"     Last used: {info.get('last_used', 'never')}")
            print(f"     Session:   {info.get('session_dir', 'N/A')}")
        
        print(f"\n  Total: {len(profiles)} profile(s)")
        print()
    
    elif args.command == 'add':
        name = args.name
        
        # Check if already exists
        if get_profile(name):
            print(f"Profile '{name}' already exists. Remove it first or choose a different name.")
            return
        
        username = args.username
        password = args.password
        
        if not username:
            username = input(f"LinkedIn username/email for '{name}': ").strip()
        if not password:
            # Do NOT strip the password: special chars and intentional leading/
            # trailing spaces must be preserved exactly. getpass avoids the
            # shell/argparse mangling that breaks passwords with +, &, !, etc.
            password = getpass.getpass(f"LinkedIn password for '{name}': ")

        if not username or not password:
            print("Username and password are required.")
            return
        
        add_profile(name, username, password, set_default=args.default)
        print(f"\n✅ Profile '{name}' created successfully!")
        
        if args.default:
            print("   Set as default profile.")
        
        print(f"\nTip: Run 'python linkedin_profile_manager.py test {name}' to verify login")
    
    elif args.command == 'remove':
        profile = get_profile(args.name)
        if not profile:
            print(f"Profile '{args.name}' not found.")
            return
        
        confirm = input(f"Remove profile '{args.name}' ({profile['username']})? [y/N]: ").strip().lower()
        if confirm == 'y':
            remove_profile(args.name)
            print(f"✅ Profile '{args.name}' removed.")
        else:
            print("Cancelled.")
    
    elif args.command == 'default':
        if get_profile(args.name):
            set_default_profile(args.name)
            print(f"✅ Default profile set to '{args.name}'")
        else:
            print(f"Profile '{args.name}' not found.")
    
    elif args.command == 'test':
        profile_name = args.name or get_default_profile_name()
        if not profile_name:
            print("No profile specified and no default set.")
            return
        
        print(f"Testing login for profile '{profile_name}'...")
        try:
            driver, profile, _ = setup_and_login(profile_name)
            print(f"\n✅ Login successful for '{profile_name}'!")
            print(f"   Current URL: {driver.current_url}")
            print("   Session will persist for next use.")
            input("\nPress Enter to close browser...")
            driver.quit()
        except Exception as e:
            print(f"\n❌ Login failed: {e}")
    
    elif args.command == 'secure-credentials':
        if not keyring_available():
            print(
                "❌ No OS credential store is available on this machine.\n"
                "   Passwords will stay in data/profiles/profiles.json.\n"
                "   On Linux, installing a Secret Service provider "
                "(e.g. gnome-keyring) enables this."
            )
            return EXIT_ERROR

        moved, left = migrate_passwords_to_keyring()
        if moved:
            print(f"✅ Moved {moved} password(s) into the OS credential store.")
            print("   profiles.json no longer contains them.")
        else:
            print("Nothing to move — no plain-text passwords found in profiles.json.")
        if left:
            print(f"⚠️  {left} password(s) could not be moved and remain in the file.")

    elif args.command == 'migrate':
        auto_migrate_from_env()
        data = load_profiles()
        if data["profiles"]:
            print("✅ Migrated to profile 'default'")
            default_profile = data["profiles"].get("default", {})
            print(f"   Username: {default_profile.get('username', 'N/A')}")
        else:
            print("No credentials found in .env to migrate.")

    elif args.command == 'config':
        profile_name = args.name or get_default_profile_name()
        if not profile_name:
            print("No profile specified and no default set.")
            return
        config_path = get_config_path(profile_name)

        if args.reset:
            reset_profile_config(profile_name)
            print(f"✅ Reset config for '{profile_name}' -> {config_path}")
        elif args.edit:
            # Ensure the file exists (create default on first use), then open it.
            get_profile_config(profile_name)
            editor = os.environ.get('EDITOR') or ('notepad' if os.name == 'nt' else 'nano')
            print(f"Opening {config_path} in {editor}...")
            try:
                import subprocess
                # Deliberately no timeout: this hands control to an
                # interactive editor and the human decides when they are done.
                # A ceiling here would kill their session mid-edit.
                subprocess.run([editor, config_path])  # noqa: S603
            except Exception as e:
                print(f"Could not open editor ({e}). Edit the file directly:\n  {config_path}")
        else:
            # Print the (effective) config.
            config = get_profile_config(profile_name)
            print(f"\nConfig for '{profile_name}' ({config_path}):\n")
            print(json.dumps(config, indent=2))


if __name__ == "__main__":
    cli()
