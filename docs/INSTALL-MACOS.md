# macOS installation: from a clean Mac to a running dashboard

A step-by-step walkthrough for macOS, written for people who have never used a terminal. If you are comfortable with Python tooling, the [README quick start](../README.md) is faster — this guide exists for everyone else. The Windows equivalent is [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md).

Tested on Apple Silicon. Intel Macs use the same steps; `webdriver-manager` resolves the right ChromeDriver either way.

**Time required:** 20–25 minutes for one-time setup. Every launch after that is a double-click.

> [!WARNING]
> This tool automates a real LinkedIn account, which violates LinkedIn's User Agreement and can result in account restriction or a permanent ban. You accept that risk. Start small — three comments a day — and review every draft before it posts.

---

## What you need first

| Requirement | Why it is needed | Cost |
|---|---|---|
| macOS (Apple Silicon or Intel) | — | — |
| Google Chrome | The tool drives a real Chrome window to act on LinkedIn | Free |
| `uv` package manager | Installs Python and all dependencies; you do **not** install Python separately | Free |
| OpenAI API key | Writes the comments and posts (model: `gpt-4o-mini`) | ~$5 credit |
| A LinkedIn account | The account the tool will operate | — |

Everything runs on your own Mac, and your LinkedIn credentials never leave it. The only outbound service call is to OpenAI, for text generation. See [Phase 4](#phase-4--create-your-linkedin-profile) for how those credentials are stored on disk.

> [!TIP]
> Keep the project folder **out of iCloud Drive**, and out of Desktop or Documents if you have "Desktop & Documents Folders" sync turned on. The tool stores a live Chrome session inside its own folder, and file-sync will corrupt it. A plain path like `~/Projects/` is ideal.

---

## Phase 1 — Install the prerequisites

### 1.1 Install Google Chrome

Install Chrome from [google.com/chrome](https://www.google.com/chrome/) if it is not already present. If it is, open **Chrome → Settings → About Chrome** and let it finish any pending update. The tool controls Chrome directly, and an outdated browser is a common cause of a failed first run.

You do **not** need to download ChromeDriver — the matching build is fetched automatically on first run.

### 1.2 Install uv

`uv` builds the project's environment and manages Python for you, so there is no separate Python install.

Open **Terminal** (press <kbd>⌘</kbd>+<kbd>Space</kbd>, type `Terminal`, press Return) and run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> [!IMPORTANT]
> When it finishes, **quit Terminal completely** (<kbd>⌘</kbd>+<kbd>Q</kbd>) and open it again. The installer adds `uv` to your `PATH`, and an already-open shell will not see it. Skipping this produces `uv not found` in the next phase.

Verify in the new window:

```bash
uv --version
```

### 1.3 Download the project

On the [repository page](https://github.com/drjeff-ai/linkedin-autocomment), click the green **Code** button, then **Download ZIP**. macOS expands it to your Downloads folder on double-click. Move the resulting folder somewhere stable, for example `~/Projects/linkedin-autocomment`.

Or clone it, which makes future updates a single `git pull`:

```bash
git clone https://github.com/drjeff-ai/linkedin-autocomment.git
```

The folder you want is the one that directly contains `install.command`, `setup.sh`, and `requirements.txt`. That is the **project folder** referenced throughout this guide.

---

## Phase 2 — Run setup

### Option A — Double-click (easiest)

Open the project folder in Finder and double-click **`install.command`**. Terminal opens, installs everything, and reports `Setup complete!`.

> [!NOTE]
> **If macOS refuses to open it** — "cannot be opened because it is from an unidentified developer", or nothing happens — that is Gatekeeper quarantining files downloaded from the internet. **Right-click `install.command` → Open → Open** to run it once; macOS remembers the choice. Cloning with `git` instead of downloading the ZIP avoids the quarantine flag entirely.

### Option B — Terminal

```bash
cd ~/Projects/linkedin-autocomment
./setup.sh
```

Either way, the script creates the virtual environment, installs Flask, Selenium, the OpenAI library and the rest, copies `.env.example` to `.env`, and creates `data/`. Re-running it is safe — it reuses an existing `.venv` rather than rebuilding.

---

## Phase 3 — Add your OpenAI API key

### 3.1 Create the key

Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys), sign in, and click **Create new secret key**. Copy it immediately — it begins with `sk-` and OpenAI will never show it again.

### 3.2 Add billing credit

A new OpenAI account has no credit, and a key with no credit errors on every request. At [platform.openai.com/settings/organization/billing](https://platform.openai.com/settings/organization/billing), add a payment method and purchase $5 in credit. The tool uses `gpt-4o-mini`, a fraction of a cent per comment, so typical monthly use lands well under a dollar.

### 3.3 Put the key in `.env`

From the project folder:

```bash
open -e .env
```

That opens the file in TextEdit. Find the `OPENAI_API_KEY` line and replace the placeholder. Keep it on one line, with no quotation marks and no spaces around the equals sign:

```diff
- OPENAI_API_KEY=sk-your-key-here
+ OPENAI_API_KEY=sk-proj-AbCdEf123456...
```

Save with <kbd>⌘</kbd>+<kbd>S</kbd> and close TextEdit.

> [!NOTE]
> Leave the `LINKEDIN_USERNAME` and `LINKEDIN_PASSWORD` lines commented out. Your LinkedIn credentials go in the next phase instead, which keeps them out of this file and out of your shell environment.

---

## Phase 4 — Create your LinkedIn profile

A "profile" is one LinkedIn account plus its saved browser session. Pick a short lowercase nickname — this guide uses `rick`.

```bash
uv run python -m linkedin_automation.profile_manager add rick
```

You will be prompted for your LinkedIn email and password. Special characters are fine. Credentials are written to `data/profiles/profiles.json` on your Mac and are never uploaded anywhere.

> [!WARNING]
> That file stores your password as **plain text**, not encrypted. It is covered by `.gitignore`, so it will not end up in a repository — but any program or person with access to the Mac can read it. Keep FileVault on (**System Settings → Privacy & Security → FileVault**), keep the folder out of shared or synced directories, and consider a LinkedIn password you do not reuse elsewhere.

Verify:

```bash
uv run python -m linkedin_automation.profile_manager list
```

---

## Phase 5 — Establish the login session

Read all four steps before running the command.

The tool never types your password into LinkedIn automatically. It opens a real Chrome window and you log in by hand, one time. That session is saved and reused, so LinkedIn sees a normal returning browser.

### 5.1 Run the login check

```bash
uv run python tools/login_check.py --profile rick
```

The first run downloads a ChromeDriver matching your Chrome version — a few seconds, once.

### 5.2 Log in inside the Chrome window that opens

A Chrome window opens on its own and navigates to the LinkedIn feed. Log in there exactly as you normally would, completing any two-factor code or "Is this you?" verification.

### 5.3 Return to Terminal and press Return

Once your feed has loaded, **do not close Chrome yourself**. Switch back to Terminal and press <kbd>Return</kbd>. The script re-checks your status and closes Chrome for you.

> [!IMPORTANT]
> Closing the Chrome window manually instead of pressing Return is the most common reason this step appears to fail. Leave Chrome open, press Return in Terminal, and let the script shut the browser down.

### 5.4 Confirm the green check

```
✅ Profile 'rick' is logged in to LinkedIn.
   Scrape / post / connect runs will reuse this session.
```

The session lives in `data/profiles/chrome_sessions/rick/` and persists across restarts, typically for several weeks. If a later run reports that login is required, repeat this phase.

---

## Phase 6 — Launch the dashboard

Double-click **`run.command`** in Finder. Terminal opens, the server starts, and your browser opens `http://localhost:6500` automatically after a few seconds.

Or from Terminal:

```bash
./run.sh
```

> [!IMPORTANT]
> Leave that Terminal window open the entire time you use the tool. It is the server — closing it shuts down the dashboard and stops the scheduler. Press <kbd>Control</kbd>+<kbd>C</kbd> to stop it deliberately.

---

## Phase 7 — Your first run

Identical to Windows. The comment pipeline runs in five steps with human review gates:

| Step | Button | What it does |
|---|---|---|
| 1 | Scrape | Scans your LinkedIn feed and collects candidate posts |
| 2 | Review Posts | You delete the junk before spending any API credit |
| 3 | Generate | Drafts comments with GPT for the posts you kept |
| 4 | Review Comments | You read, edit, or delete every draft |
| 5 | Post | Publishes approved comments with human-like typing delays |

**Set the Generate limit to 3 for your first several runs.** Expect to delete a lot of drafts early on — that is the tuning signal, not a malfunction.

Tune the persona, tone and voice with:

```bash
uv run python -m linkedin_automation.profile_manager config rick --edit
```

On macOS this opens in `nano` unless you have `$EDITOR` set. In nano, save with <kbd>Control</kbd>+<kbd>O</kbd> then <kbd>Return</kbd>, and exit with <kbd>Control</kbd>+<kbd>X</kbd>.

See the [Windows guide's Phase 7](INSTALL-WINDOWS.md#phase-7--your-first-run) for the Post Creator and scheduler details — the dashboard is identical on both platforms.

---

## Troubleshooting

Check the basics first: is the Terminal window still open, is the session still logged in, and does the OpenAI account still have credit?

| Symptom | Cause | Fix |
|---|---|---|
| `uv not found` | Terminal was open before uv was installed | Quit Terminal (<kbd>⌘</kbd>+<kbd>Q</kbd>) and reopen |
| "unidentified developer" on `.command` | Gatekeeper quarantine on downloaded files | Right-click the file → **Open** → **Open** |
| `.command` file does nothing | Executable bit lost in transit | `chmod +x install.command run.command` |
| `permission denied: ./setup.sh` | Same cause | `chmod +x setup.sh run.sh` |
| OpenAI 401 / invalid key | Key mistyped, or no billing credit | Re-check `.env`; confirm credit at platform.openai.com |
| "Login required" mid-run | LinkedIn session expired | Re-run `tools/login_check.py --profile rick` |
| Chrome opens then closes | Chrome outdated | **Chrome → Settings → About Chrome** |
| Dashboard will not load | Server stopped, or `https` used | Restart `run.command`; use `http://localhost:6500` |
| Scrape finds nothing | LinkedIn changed its page structure | Run the selector health check below |

### Selector health check

```bash
uv run python -m linkedin_automation.selector_health --profile rick
```

Reports `HEALTHY`, `DEGRADED`, or `BROKEN` and dumps page HTML for diagnosis.

### Port 6500 already in use

```bash
lsof -ti :6500 | xargs kill
```

---

## macOS-specific notes

- **Comment submission uses <kbd>⌘</kbd>+<kbd>Return</kbd>** rather than Ctrl+Return. Chrome on macOS does not route Ctrl+Return to the page's submit handler. The tool selects the right modifier automatically (`linkedin_automation/platform_compat.py`), and falls back to clicking the submit button if the shortcut does not take.
- **Clipboard fallback uses `pbpaste`.** Post URLs are normally captured by an in-page JavaScript intercept that never touches your clipboard; `pbpaste` is only the fallback.
- **The scheduler needs the dashboard running.** It is a background thread inside the server, so it only fires while `run.command` is open. There is no launchd job — if you want unattended scheduling, that is a separate setup.

---

## Command reference

Run from the project folder. Replace `rick` with your profile name.

| Task | Command |
|---|---|
| Start the dashboard | `./run.sh` (or double-click `run.command`) |
| Re-run setup | `./setup.sh` (or double-click `install.command`) |
| Check / restore login | `uv run python tools/login_check.py --profile rick` |
| Add another profile | `uv run python -m linkedin_automation.profile_manager add NAME` |
| List profiles | `uv run python -m linkedin_automation.profile_manager list` |
| Edit voice / settings | `uv run python -m linkedin_automation.profile_manager config rick --edit` |
| Selector health check | `uv run python -m linkedin_automation.selector_health --profile rick` |
| Edit API key | `open -e .env` |
| Run the tests | `uv run pytest tests/ -v` |
