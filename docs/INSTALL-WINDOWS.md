# Windows installation: from a clean machine to a running dashboard

A step-by-step walkthrough from a clean Windows machine to a running dashboard, written for people who have never used a terminal. If you are comfortable with Python tooling, the [README quick start](../README.md) is faster — this guide exists for everyone else.

**Time required:** 25–30 minutes for one-time setup. Every launch after that takes about 10 seconds.

> [!WARNING]
> This tool automates a real LinkedIn account, which violates LinkedIn's User Agreement and can result in account restriction or a permanent ban. You accept that risk. Start small — three comments a day — and review every draft before it posts.

---

## Contents

- [What you need first](#what-you-need-first)
- [How the whole thing fits together](#how-the-whole-thing-fits-together)
- [Phase 1 — Install the prerequisites](#phase-1--install-the-prerequisites)
- [Phase 2 — Run setup](#phase-2--run-setup)
- [Phase 3 — Add your OpenAI API key](#phase-3--add-your-openai-api-key)
- [Phase 4 — Create your LinkedIn profile](#phase-4--create-your-linkedin-profile)
- [Phase 5 — Establish the login session](#phase-5--establish-the-login-session)
- [Phase 6 — Launch the dashboard](#phase-6--launch-the-dashboard)
- [Phase 7 — Your first run](#phase-7--your-first-run)
- [Troubleshooting](#troubleshooting)
- [Command reference](#command-reference)

---

## What you need first

| Requirement | Why it is needed | Cost |
|---|---|---|
| Windows 10 or 11 | The setup scripts are Windows batch files | — |
| Google Chrome | The tool drives a real Chrome window to act on LinkedIn | Free |
| `uv` package manager | Installs Python and all dependencies; you do **not** install Python separately | Free |
| OpenAI API key | Writes the comments and posts (model: `gpt-4o-mini`) | ~$5 credit |
| A LinkedIn account | The account the tool will operate | — |

Everything runs on your own computer. Your LinkedIn credentials are stored encrypted locally and are never uploaded. The only outbound service call is to OpenAI, for text generation.

> [!TIP]
> Keep the project folder **out of OneDrive, Desktop, or any synced directory**. The tool stores a live Chrome session inside its own folder, and file-sync software will corrupt it. A plain path like `C:\Projects\` is ideal.

---

## How the whole thing fits together

| Phase | What happens | One-time? |
|---|---|---|
| 1 — Prerequisites | Install Chrome and uv, download the project | Yes |
| 2 — Setup | Run `setup.bat`, which installs all dependencies | Yes |
| 3 — API key | Paste your OpenAI key into the `.env` file | Yes |
| 4 — Profile | Store your LinkedIn credentials encrypted | Yes |
| 5 — Login | Log in once in Chrome; the session is saved | Yes |
| 6 — Launch | Start the dashboard at `localhost:6500` | Each session |
| 7 — Operate | Scrape, review, generate, review, post | Daily |

After setup, day-to-day operation is three moves: double-click `run.bat`, open `http://localhost:6500`, work the pipeline.

---

## Phase 1 — Install the prerequisites

### 1.1 Install Google Chrome

Install Chrome from [google.com/chrome](https://www.google.com/chrome/) if it is not already present. If it is, open **Settings → About Chrome** and let it finish any pending update. The tool controls Chrome directly, and an outdated browser is the most common cause of a failed first run.

You do **not** need to download ChromeDriver. The matching driver is fetched automatically on first run.

### 1.2 Install uv

`uv` builds the project's environment. It also downloads and manages Python for you, so there is no separate Python installation step.

Press the Windows key, type `powershell`, open **Windows PowerShell**, and run:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> [!IMPORTANT]
> After uv finishes installing, **close PowerShell and every open Command Prompt window**. New windows must be opened for Windows to pick up uv on the `PATH`. Skipping this produces `ERROR: uv not found` in Phase 2, and it is the single most common setup failure.

Open a **new** terminal window and verify:

```bat
uv --version
```

You should see a version number. If you see `'uv' is not recognized`, restart the computer and try again.

### 1.3 Download the project

On the [repository page](https://github.com/drjeff-ai/linkedin-autocomment), click the green **Code** button, then **Download ZIP**. Right-click the downloaded file and choose **Extract All**. Extract to a simple location such as:

```
C:\Projects\linkedin-autocomment
```

Open the extracted folder. If there is a single folder inside with the same name, go into it — you want the level that directly contains `setup.bat`, `run.bat`, and `requirements.txt`. That is the **project folder** referenced throughout this guide.

If you have Git installed you can instead clone it, which makes future updates a single `git pull`:

```bat
git clone https://github.com/drjeff-ai/linkedin-autocomment.git
```

---

## Phase 2 — Run setup

### 2.1 Open a Command Prompt in the project folder

You will use this constantly, so learn it once. Open the project folder in File Explorer, click into the **address bar** at the top, type `cmd`, and press Enter.

A Command Prompt opens already pointed at the project folder. Every command in this guide is run from a window opened this way — it eliminates an entire category of "file not found" errors caused by running from the wrong directory.

### 2.2 Run the setup script

```bat
setup.bat
```

You can also double-click `setup.bat` in File Explorer, but running it from a Command Prompt is better: if it fails, the window stays open so you can read the error.

The script creates a virtual environment, installs Flask, Selenium, the OpenAI library and the rest of the dependencies, copies `.env.example` to `.env`, and creates the `data/` folder.

### 2.3 Confirm success

You will see `Setup complete!` followed by a numbered **Next steps** list and `Press any key to continue`. That screen is the correct outcome — it is an instruction list, not an error. Phases 3 through 6 below are those next steps, expanded.

> [!WARNING]
> If you see `ERROR: uv not found`, you skipped closing and reopening the terminal after installing uv. Close every Command Prompt window, open a fresh one in the project folder, and run `setup.bat` again.

---

## Phase 3 — Add your OpenAI API key

### 3.1 Create the key

Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys), sign in, and click **Create new secret key**. Copy it immediately — it begins with `sk-` and OpenAI will never show it to you again.

### 3.2 Add billing credit

A new OpenAI account has no credit, and a key with no credit returns an error on every request. At [platform.openai.com/settings/organization/billing](https://platform.openai.com/settings/organization/billing), add a payment method and purchase $5 in credit.

That $5 goes a long way. The tool uses `gpt-4o-mini`, a fraction of a cent per comment, so typical monthly use lands well under a dollar.

### 3.3 Put the key in `.env`

From your Command Prompt in the project folder:

```bat
notepad .env
```

Find the `OPENAI_API_KEY` line near the top and replace the placeholder. Keep it on one line, with no quotation marks and no spaces around the equals sign:

```diff
- OPENAI_API_KEY=sk-your-key-here
+ OPENAI_API_KEY=sk-proj-AbCdEf123456...
```

Save with <kbd>Ctrl</kbd>+<kbd>S</kbd> and close Notepad.

> [!NOTE]
> Leave the `LINKEDIN_USERNAME` and `LINKEDIN_PASSWORD` lines commented out. Your LinkedIn credentials go in the next phase instead, where they are stored encrypted rather than sitting in a plain text file.

---

## Phase 4 — Create your LinkedIn profile

A "profile" here means one LinkedIn account plus its saved browser session. Pick a short lowercase nickname with no spaces — this guide uses `rick`; substitute your own throughout.

```bat
uv run python -m linkedin_automation.profile_manager add rick
```

You will be prompted for your LinkedIn email and password. Special characters are fine. Credentials are encrypted and written to `data\profiles\profiles.json` on your machine only.

Verify:

```bat
uv run python -m linkedin_automation.profile_manager list
```

You can add more profiles later if you manage multiple accounts; each keeps a completely separate browser session.

---

## Phase 5 — Establish the login session

Read all four steps before running the command.

The tool never types your password into LinkedIn automatically. It opens a real Chrome window and you log in by hand, one time. That session is then saved and reused, so LinkedIn sees a normal returning browser rather than a fresh automated login on every run.

### 5.1 Run the login check

```bat
uv run python tools/login_check.py --profile rick
```

### 5.2 Log in inside the Chrome window that opens

A Chrome window opens on its own and navigates to the LinkedIn feed. The Command Prompt reports either a green check (already logged in) or a red X (login required).

If login is required, log in inside that Chrome window exactly as you normally would, completing any two-factor code or "Is this you?" verification.

### 5.3 Return to the Command Prompt and press Enter

Once your feed has fully loaded, **do not close Chrome yourself**. Switch back to the Command Prompt and press <kbd>Enter</kbd>. The script re-checks your status and closes Chrome for you.

> [!IMPORTANT]
> Closing the Chrome window manually instead of pressing Enter is the number one reason this step appears to fail. Leave Chrome open, press Enter in the terminal, and let the script shut the browser down.

### 5.4 Confirm the green check

```
✅ Profile 'rick' is logged in to LinkedIn.
   Scrape / post / connect runs will reuse this session.
```

The session is stored in `data\profiles\chrome_sessions\rick\` and persists across restarts. You will not need to log in again until LinkedIn expires it, typically after several weeks. If a later run reports that login is required, repeat this phase.

---

## Phase 6 — Launch the dashboard

Double-click `run.bat`, or from a Command Prompt in the project folder:

```bat
uv run python -m linkedin_automation.dashboard
```

> [!IMPORTANT]
> Leave that window open the entire time you use the tool. It is the server itself — closing it shuts down the dashboard and stops the scheduler.

Then open **`http://localhost:6500`** in your browser. Use `http`, not `https`. If the page does not load, confirm the server window is still open and shows no error.

---

## Phase 7 — Your first run

The comment pipeline is deliberately built so nothing reaches LinkedIn without you reading it first. Work the steps in order.

| Step | Button | What it does |
|---|---|---|
| 1 | Scrape | Scans your LinkedIn feed and collects candidate posts |
| 2 | Review Posts | You delete the junk before spending any API credit |
| 3 | Generate | Drafts comments with GPT for the posts you kept |
| 4 | Review Comments | You read, edit, or delete every draft |
| 5 | Post | Publishes approved comments with human-like typing delays |

**Set the Generate limit to 3 for your first several runs.** A small batch lets you evaluate whether the tone sounds like you before any volume is at stake. Expect to delete a lot of drafts early on — that is the tuning signal, not a malfunction.

### Tuning the voice

If drafts do not sound like you, adjust the persona, tone, and voice settings:

```bat
uv run python -m linkedin_automation.profile_manager config rick --edit
```

The same file controls the post finder's keywords and human-behavior timing. Use `--reset` in place of `--edit` to restore defaults.

### Post Creator and the scheduler

The **Post Creator** tab drafts original content in eight style options, or a reaction post from an article URL. Drafts land in a queue for manual publishing or scheduler pickup.

The **scheduler** publishes on a randomized twice-daily rhythm — by default 8–11am and 2–5pm, four to eight items per run, with a 10% chance of skipping any run.

> [!WARNING]
> Leave the scheduler off until you have manually reviewed at least a week of output and trust the voice.

---

## Troubleshooting

Check the basics first, in this order: is the server window still open, is the session still logged in, and does the OpenAI account still have credit? Those three account for most of what looks like a broken install.

| Symptom | Cause | Fix |
|---|---|---|
| `uv not found` | Terminal opened before uv was installed | Close all terminals, open a new one; reboot if needed |
| `setup.bat` flashes and closes | It hit an error and exited | Run `setup.bat` from a Command Prompt to read the message |
| OpenAI 401 / invalid key | Key mistyped, or no billing credit | Re-check `.env`; confirm credit at platform.openai.com |
| "Login required" mid-run | LinkedIn session expired | Re-run `tools/login_check.py --profile rick` |
| Chrome opens then closes | Chrome outdated or missing | Update via **Settings → About Chrome** |
| Dashboard will not load | Server window closed, or `https` used | Reopen `run.bat`; use `http://localhost:6500` |
| Scrape finds nothing | LinkedIn changed its page structure | Run the selector health check below |
| Large `no_url` pile in Trash | URL extraction is missing posts | Run the selector health check below |

### Selector health check

LinkedIn changes its page structure every few months, breaking the scraper's CSS selectors.

```bat
uv run python -m linkedin_automation.selector_health --profile rick
```

It reports `HEALTHY`, `DEGRADED`, or `BROKEN` and dumps page HTML for diagnosis. A `BROKEN` result means the project code needs updating — check the repository for a newer version before debugging it yourself.

### Login check exit codes

| Code | Meaning | What to do |
|---|---|---|
| `0` | Logged in successfully | Continue to Phase 6 |
| `2` | Login required | Re-run and log in manually |
| `1` | Error — bad profile or browser failure | Check the profile name; update Chrome |

---

## Command reference

All commands run from a Command Prompt opened in the project folder. Replace `rick` with your own profile name.

| Task | Command |
|---|---|
| Start the dashboard | `run.bat` |
| Check / restore login | `uv run python tools/login_check.py --profile rick` |
| Add another profile | `uv run python -m linkedin_automation.profile_manager add NAME` |
| List profiles | `uv run python -m linkedin_automation.profile_manager list` |
| Set default profile | `uv run python -m linkedin_automation.profile_manager default rick` |
| Edit voice / settings | `uv run python -m linkedin_automation.profile_manager config rick --edit` |
| Reset settings | `uv run python -m linkedin_automation.profile_manager config rick --reset` |
| Selector health check | `uv run python -m linkedin_automation.selector_health --profile rick` |
| Edit API key | `notepad .env` |

---

## Setup checklist

- [ ] Google Chrome installed and fully updated
- [ ] uv installed; `uv --version` returns a version number
- [ ] Project downloaded and extracted outside OneDrive
- [ ] `setup.bat` completed with the "Setup complete!" screen
- [ ] OpenAI API key created and billing credit added
- [ ] Key pasted into `.env` with no quotes or spaces
- [ ] LinkedIn profile added and confirmed with `list`
- [ ] Login check returns the green check mark
- [ ] Dashboard loads at `http://localhost:6500`
- [ ] Generate limit set to 3 for the first run
- [ ] Scheduler left off until the voice is trusted
