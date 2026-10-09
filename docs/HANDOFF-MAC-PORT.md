# Handoff: the Mac port and Rick's additions

**For:** Jeff
**From:** Rick (CyberRick-AI), prepared with Claude
**Where:** fork `CyberRick-AI/linkedin-autocomment`, branch `feat/macos-app` (also its `main`), 11 commits on top of your `main` at `0395c04`
**Status:** nothing has been pushed to `drjeff-ai/linkedin-autocomment`. Take all of this, some of it, or none.

---

## In one paragraph

Your current `main` runs on Windows only. This branch makes it run on macOS, adds a menu bar app, and brings forward four features from Rick's earlier Mac branch that your Sept 15 restart didn't include: passwords in the OS keychain, a choice of AI provider with a Settings tab, a Log in button, and a Restart button. It also adds a visual redesign of the dashboard. The full suite is **2134 passed, 1 skipped, 2 xfailed** on macOS and ruff is clean. **It has not been run on Windows**: please run the suite there before merging anything.

---

## Your files that this branch changes

Twenty-four of your existing files are modified. The ones that change behaviour, and how:

| File | Size of change | What changed |
|---|---|---|
| `comment_poster.py` | +7 / −4 | Keyboard submit uses Cmd+Enter on macOS (Ctrl+Enter elsewhere, unchanged). SIGTERM handler in `main()`. |
| `post_finder.py` | +8 / −7 | The clipboard **fallback** reads via `platform_compat` (PowerShell on Windows, same as before; pbpaste on macOS). Unused `subprocess` import removed. SIGTERM handler. |
| `auto_connector.py`, `poster.py` | +2 each | SIGTERM handler in `main()` so a stopped run unwinds and closes Chrome. |
| `profile_manager.py` | +262 / −9 | (1) chromedriver path goes through `ensure_driver_runnable` before Selenium (macOS quarantine fix; no-op elsewhere). (2) Passwords go to the OS keychain via `keyring`, with in-file fallback and a `secure-credentials` migration command. Existing profiles keep working unchanged. (3) API-key helpers for the provider layer. |
| `comment_generator.py` | +98 / −64 | Uses `providers.py` instead of `OpenAI(api_key=os.getenv('OPENAI_API_KEY'))`. Default provider is still OpenAI. |
| `post_generator.py` | +45 / −33 | Same provider switch; the article fetcher's chromedriver gets the same macOS fix. |
| `dashboard.py` | +506 / −17 | New routes: provider settings and keys, login, stored-credential check, restart, `/api/scheduled/<p>/slots`. `DASHBOARD_PORT` constant. Queue state help is now per platform (see "Bugs fixed in your code"). **`app.run(debug=True)` is unchanged.** |
| `templates/dashboard.html` | +2024 / −84 | The redesign (new top bar, Home, Calendar, ⌘K command bar) plus the Settings tab. **Every existing element id, JS function and endpoint call is kept**; the old six tabs are now grouped under Engage / Create / Settings. |
| `tools/login_check.py` | +79 / −6 | Can be driven from the dashboard: polls the browser instead of `input()`, `--timeout`, closed window = not signed in. |
| `tools/run_linkedin_workflow.py` | +11 / −5 | Stops requiring `OPENAI_API_KEY` when another provider is configured. |
| `requirements.txt` | +2 | `keyring==25.6.0`, `anthropic==0.120.2`. |
| `.env.example`, `README.md`, `default_profile_config.json`, `.gitignore` | small | Provider fields, keychain note, a one-line Mac pointer, `dist/` ignored. |

**Tests of yours that were adapted**, intent unchanged: `test_comment_relevance`, `test_post_generator_config` and three in `test_platform_policy` now mock the provider instead of the OpenAI client. `test_profile_manager` and `test_login_flow` read passwords through `get_profile_password`. `test_declared_dependencies` now also reads `requirements-macos.txt` and asserts PyObjC stays out of `requirements.txt`. No test was deleted or skipped.

---

## Behaviour changes you should know about

1. **`api_usage.jsonl`** records the provider actually used, not always `openai`. Same record shape.
2. **Relevance model** is the provider's cheap default, not a hard-coded `gpt-4o-mini`. With OpenAI that is still `gpt-4o-mini`.
3. **`--model` CLI flags** no longer default to `gpt-4o-mini`; empty means "the profile's provider default".
4. **Generator exit code** is 1 when the provider can't be set up (it was 0).
5. **Restart button:** after a restart the server runs detached and logs to `logs/dashboard_restart.log`, so closing the original console no longer stops it.
6. **Passwords:** new profiles store the LinkedIn password in Windows Credential Manager / macOS Keychain. Old profiles are untouched until someone runs `profile_manager secure-credentials`.

---

## Bugs fixed in your code along the way

- **X queue label.** A published X row said "live on LinkedIn, first comment still owed - the comment sweep will pick it up". X has no comment pass, so that told the operator to wait for a step that never runs. State help is now looked up per platform. `tests/test_reconcile_published.py` has two new tests; your LinkedIn assertions are unchanged.
- **Mac window dialogs.** Your dashboard has 44 `alert()` / `confirm()` / `prompt()` calls. A WKWebView without a UI delegate drops them and answers every `confirm()` with false, so every "Delete this?" button did nothing in the Mac app. Fixed in `macapp_ui.py` only; your HTML is untouched for this.

---

## New files (all additive)

- **Mac:** `platform_compat.py`, `server_supervisor.py`, `app_controller.py`, `macapp.py`, `macapp_ui.py`, `tools/build_app.py`, `setup.sh`, `run.sh`, `run.command`, `install.command`, `requirements-macos.txt` (PyObjC, macOS only), `docs/INSTALL-MACOS.md`.
- **Features:** `providers.py` (11 providers: OpenAI, Anthropic, xAI, DeepSeek, Groq, Together, OpenRouter, Mistral, Fireworks, Ollama, custom OpenAI-compatible), `restart_helper.py`.
- **Assets and docs:** `static/` (Guild seal logo and favicon), `docs/PHONE-ACCESS-PLAN.md` (a plan only, not built).
- 17 new test files.

---

## Windows: please check these

None of this has run on Windows. The places most likely to differ:

1. **`restart_helper.process_alive`** has a Windows branch (OpenProcess + GetExitCodeProcess) because `os.kill(pid, 0)` terminates the process on Windows. It's unit-tested with a fake kernel32 but has never run on real Windows. This is what failed on Windows CI in Rick's earlier branch (`test_process_alive_reports_a_dead_pid`).
2. **`platform_compat.exit_cleanly_on_termination`** installs SIGTERM/SIGHUP handlers; SIGHUP doesn't exist on Windows and is skipped.
3. **`keyring`** uses Windows Credential Manager. Existing profiles are unaffected until migrated.
4. **The redesign** relies on `backdrop-filter` and Google Fonts with system fallbacks. Worth a look in Edge and Chrome.

To test:
```
uv pip install -r requirements.txt -r requirements-dev.txt
uv run pytest -q
uv run python -m linkedin_automation.dashboard
```
Then click through Home, Calendar, Engage (all three tabs), Create (Post creator, Scheduled posting with the LinkedIn/X switch), Settings, and ⌘K / Ctrl+K.

---

## Decisions where we went a different way, and why

- **LinkedIn first comment on Mac.** Your first-comment pass runs from Windows Task Scheduler (`setup_task_scheduler.ps1`), and there's no macOS equivalent yet. This weekend's LinkedIn posts carry the link **in the body** (empty `first_comment_link`), which your `comment_pass` already treats as done. A launchd job, or Buffer Essentials' `firstComment`, would be the proper fix.
- **Buffer's slot limit is organisation-wide.** Confirmed live: `used: 6, used_this_channel: 3` across LinkedIn + X on one free account, matching your note in SCHEDULED_POSTING.md.
- **Debug mode left on.** `app.run(debug=True)` is unchanged so your reloader-based restart flow and tests still apply. The phone plan turns it off before anything is reachable off the machine.

---

## Suggested way to take it

The commits are layered, so you can stop at any point:

1. `6f5e750` + `5e5a9aa`: Mac support and install guide. Smallest touch on your files.
2. `8146d4f`: keychain passwords.
3. `f151e17`: provider choice and Settings tab. The biggest change to your generator code.
4. `940aa42`, `9c205bf`: Log in and Restart buttons.
5. `ac4c0cc`, `5f41e45`: the two bug fixes above. Worth taking even if nothing else is.
6. `1a5346d`, `02abe15`: the redesign and live queue ring.
7. `597c5d1`: phone-access plan (docs only).

Questions go to Rick.
