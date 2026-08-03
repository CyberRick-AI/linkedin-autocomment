# LinkedIn Autocomment — the complete guide

Install, everything the app does, how to run it day to day, and the things
worth knowing before you learn them the hard way.

**Mac only.** It drives a real Chrome window, so it needs a desktop machine.

Set aside about twenty minutes for setup, most of which is waiting for
downloads.

---

## Contents

- **Part 1: Install** (steps 1 to 4)
- **Part 2: First-time setup** (steps 5 to 7)
- **Part 3: The five tabs, and what each one does**
- **Part 4: Making it sound like you**
- **Part 5: Running it day to day, and restarting**
- **Part 6: Tips and tricks**
- **Part 7: When something goes wrong**

---

## What this thing actually does

It finds posts on your LinkedIn feed, writes draft comments with an AI model,
and shows them to you. **You approve every comment before it is posted.** It
also sends connection requests, writes your own posts, and can publish them on
a schedule.

Two things to know before you start:

- **You pay for the AI.** You bring your own API key. Typical cost is a
  fraction of a cent per comment. A full run of seven comments cost $0.0062 in
  testing. It is your account and your bill.
- **It automates your real LinkedIn account.** LinkedIn does not love
  automation. It rate limits, and it can show verification challenges. Start
  small. The daily caps exist for a reason, so do not raise them on day one.

---

# Part 1: Install

## Step 1 — Install `uv`

`uv` manages the Python side. Open **Terminal** (Cmd+Space, type "Terminal")
and paste this:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Then close Terminal and open a new one.** The installer changes your PATH and
the old window will not see it.

Check it worked:

```bash
uv --version
```

A version number means you are fine. "command not found" means the
new-window step was skipped.

## Step 2 — Get the code

```bash
mkdir -p ~/Projects && cd ~/Projects
git clone --branch beta https://github.com/CyberRick-AI/linkedin-autocomment.git
```

> **`--branch beta` matters. Do not leave it out.** This is a fork of someone
> else's project, and the fork's default branch is still the original author's
> code. None of what you are testing is on it. Copy the command exactly.
>
> `beta` is the branch you want and it will stay that way. It moves forward as
> fixes land, so `git pull` in that folder gets you the latest.

If `git` is missing, macOS will offer to install the developer tools. Accept,
wait, then run the command again.

## Step 3 — Run the installer

In Finder, open `~/Projects/linkedin-autocomment` and **double-click
`install.command`**.

A Terminal window opens and runs for a few minutes. It installs everything,
builds the Mac app, and puts it in your Applications folder.

> If macOS refuses to run it, right-click `install.command`, choose **Open**,
> and confirm. Same reason as Step 4.

## Step 4 — Open the app the first time (the important bit)

The app is not signed by Apple, because signing costs a yearly developer fee
and this is a beta. macOS therefore blocks it on first launch.

**Do not double-click it the first time.** You will get a message saying it
cannot be opened, with no useful option.

Instead:

1. Open your **Applications** folder
2. **Right-click** (or Control-click) **LinkedIn Autocomment**
3. Choose **Open**
4. A warning appears. Click **Open** again.

**Once only.** From then on a normal double-click works.

This is macOS being cautious about software it has not seen before, not a sign
anything is wrong. You can read every line of what runs: it is a short shell
script inside the app.

The app appears in your **Dock**. It may also add a small icon to the menu bar
at the top of the screen, but if your menu bar is crowded macOS hides it, so
use the Dock.

---

# Part 2: First-time setup

## Step 5 — Create a profile

A window opens showing the dashboard. Press the **+** button in the top right.

Give it any name you like. Your own first name is fine. A profile is just a
label for one set of settings plus one saved browser session, so you only need
more than one if you manage several LinkedIn accounts.

## Step 6 — Choose an AI provider and paste a key

Open the **Settings** tab, choose a provider, and paste your key. The app
stores it in your Mac's Keychain, not in a file.

You only need **one**.

### The easy choices

| Provider | Get a key at | Notes |
|---|---|---|
| **OpenAI** | platform.openai.com | The default. `gpt-4o-mini` is cheap and good enough for comments. |
| **Anthropic** | console.anthropic.com | Claude. Default `claude-haiku-4-5`, the fast cheap one. |
| **xAI** | console.x.ai | Grok. Default `grok-4`. What this tool has had the most real use with. |

All three want a card and a few dollars of prepaid credit.

### Cheaper or faster

| Provider | Get a key at | Notes |
|---|---|---|
| **DeepSeek** | platform.deepseek.com | Cheap, and ships a default model so there is nothing to look up. |
| **Groq** | console.groq.com | Very fast, generous free tier. |
| **Together** | api.together.xyz | Many open models behind one key. |
| **Mistral** | console.mistral.ai | European, solid. |
| **Fireworks** | fireworks.ai | Fast open-model hosting. |
| **OpenRouter** | openrouter.ai | One key, hundreds of models. Good for experimenting. |

> **Groq, Together, Mistral, Fireworks and OpenRouter ship no default model**,
> so you type the exact model name into the Model box yourself. That is
> deliberate: these providers rename and retire models often, and a guessed
> default that quietly stops existing is worse than an empty box. Copy the name
> from the provider's own model list.
>
> OpenAI, Anthropic, xAI and DeepSeek fill it in for you.

### Free, if you would rather not pay anyone

**Ollama** runs a model on your own Mac. No key, no cost, nothing leaving your
machine. Install from ollama.com, run `ollama pull llama3.1`, then choose
Ollama and type `llama3.1`. Slower, and quality depends on your hardware.

### If your provider is not listed

Choose **Custom (OpenAI-compatible)**, paste the base URL and model name. Most
providers speak the same API format, so this covers almost anything, including
a model on your own server.

Not sure? Pick **xAI**.

### Then press Test Connection

It sends one tiny prompt and reports what actually worked: whether it answered,
whether temperature survived, whether the model leaked its reasoning.

**This is the only thing in the app that spends money without you starting a
run**, and it costs a fraction of a cent. Press it. It catches a wrong key or a
wrong model name immediately instead of halfway through your first real run.

**Check Stored Credential** is next to it. It confirms your key round-trips out
of the Keychain. It cannot confirm the key is *valid*, and says so.

## Step 7 — Log in to LinkedIn

Press **Log in** in the header.

Chrome opens on LinkedIn's own sign-in page. **Type your password there.** It
goes to LinkedIn and never passes through this app, which has no way to see it.

Sign in, clear any verification LinkedIn asks for, and leave the window alone.
The app notices when you are in and closes it.

Your session is saved, so this is a once-every-few-weeks job.

---

# Part 3: The five tabs

## Comment Pipeline

Five numbered steps across the top. You move left to right, and **two of them
are review gates that exist so a machine never posts unread text under your
name.**

### 01 Scrape

| Setting | What it does |
|---|---|
| **Max posts to scan** | How many feed posts to look at. Default 50. **Use 10 for your first run.** |
| **Min quality posts** | How many good posts to find before it stops scrolling. **This is a target count, not a score threshold.** See the tip in Part 6, because the name misleads everybody. |

Chrome opens and scrolls your feed. **Do not touch that window.** Posts are
sorted into four bins shown as coloured chips: **New**, **Generated**,
**Commented**, **Trash**.

Two other buttons live here:

- **Check Selectors** tests whether the app can still find things on LinkedIn's
  current page layout. Run it if a scrape suddenly returns nothing.
- **Full Report** shows every selector with green, amber or red, and says
  plainly which ones it could not check and why.

### 02 Review Posts

Everything it kept. Untick anything you would not comment on, then
**Remove Unchecked**. Nothing is generated for a post you remove, so this is
also where you save money.

### 03 Generate

Writes a draft comment for each kept post. **This is the step that costs.** The
panel shows which provider and model will run, read from Settings, and warns in
red if that provider has no key.

### 04 Review Comments

**The most important screen in the app.** Every draft, editable.

Read each one. Edit anything that does not sound like you. **Reject** anything
you do not want. Then **Save & Continue**.

### 05 Post

Publishes the approved comments to LinkedIn under your name. Chrome opens
again. Leave it alone.

Comments already posted are recorded so they can never be posted twice, even if
something crashes mid-run.

---

## Auto-Connector

Sends connection requests from a LinkedIn people-search URL.

| Setting | What it does |
|---|---|
| **LinkedIn search URL** | Run a People search on LinkedIn, then copy the whole address bar in. |
| **Max requests** | How many to send this run. Default 25. |
| **Max pages** | How many result pages to walk. Default 10. |
| **Connection note** | Optional. If filled in, it clicks "Add a note" and types this on every request. |

Three counters across the top show **This week**, **Remaining** and **All time**.
Those caps are per profile and they persist, so restarting the app does not
reset them.

**Stop** halts a run cleanly. Requests already sent stay recorded.

Two behaviours worth understanding:

- **If a note is set but LinkedIn will not let it attach, the app sends
  nothing** rather than sending a blank invitation you did not write. An invite
  cannot be recalled and your weekly allowance is finite.
- **LinkedIn sometimes asks "Do you know this person?" and wants their email.**
  The app never supplies one, skips that person, and moves on. If it happens
  three times in a row it stops the whole run, because that is LinkedIn
  signalling it does not like the pattern.

---

## Post Creator

Writes your own posts. Two ways in.

### Thought Leadership

| Setting | What it does |
|---|---|
| **Style** | One of eight, listed below. |
| **Custom topic** | Optional. Leave blank and it picks from your configured topics. |
| **Count** | How many drafts to generate at once. |

The eight styles:

| Style | What it writes |
|---|---|
| **Hot take** | A bold, confident opinion. No hedging. |
| **Question** | A thought-provoking question that invites discussion. |
| **Observation** | A pattern you have noticed in the industry. |
| **Prediction** | Where things are heading, with timeframes. |
| **Story** | A short first-person anecdote. |
| **Tip** | Something practical people can use. |
| **Contrarian** | Respectfully challenges a common assumption. |
| **Celebration** | Genuine enthusiasm about something in the space. |

### Article Reaction

Paste any article URL and press **Generate from Article**. It fetches the
article, writes a reaction post, and includes the link for reference.

Some sites block automated fetching or hide their text behind a paywall. If
that happens it tells you rather than inventing a reaction.

### The queue

Generated posts land in the **Post Queue**. Read them, then **Publish** a
specific one or **Publish Next**. Published posts move to **Published History**.

---

## Scheduler

Publishes queued comments, and optionally scrapes, on a randomised twice-daily
timer so it does not look like a machine.

| Setting | Default |
|---|---|
| **Master switch** | Off |
| **Post comments** | On, when the master switch is on |
| **Windows** | 08:00 to 11:00, and 14:00 to 17:00 |
| **Count per run** | 4 to 8 |
| **Skip chance** | 10%, so it sometimes just does not run |
| **Scrape** | Off. Window 09:00 to 12:00 when enabled |

**Run Now (test)** fires a job immediately, ignoring windows, so you do not
have to wait until tomorrow to see whether it works.

> **The scheduler lives inside the app.** Quit the app and it stops. It is not
> a background service that survives on its own.

> **Nobody has run the scheduler yet.** Not the author, not anybody. If you
> turn it on you are the first, and that is worth knowing before you let it
> publish unattended.

---

## Settings

Provider, model, API key, and the **Configure** modal (the gear icon) where the
per-profile personality lives. See Part 4.

The key list shows only providers you have configured plus the one selected.
The rest collapse behind a summary line, because a list of ten "not set" rows
answers a question nobody asked.

---

# Part 4: Making it sound like you

This is what separates it from an obvious bot. Open the **gear icon** in the
header.

| Setting | What it controls |
|---|---|
| **Persona** | One line describing who is commenting. |
| **Tone** | Default is warm, clever, light wit, never snarky or condescending. |
| **Voice** | Default forces I and my, never we and our, so it reads as a person. |
| **Topics of expertise** | What you can credibly speak to. |
| **Things to avoid** | Default bans salesy language, generic praise, and em dashes. |
| **Comment length** | Default 15 to 60 words. Short comments read as human. |
| **Style mix** | How often it adds insight, shares experience, asks a question, pushes back, or just agrees. |
| **Keywords** | Tier 1 and tier 2 terms that decide which posts are relevant. |

**Change the keywords first.** They ship tuned for AI and machine learning. If
your feed is about something else, the scraper will bin nearly everything until
you fix this.

**Change the persona and topics second.** Everything else is fine on defaults.

**Reset to defaults** is there if you go too far.

---

# Part 5: Running it day to day

## Starting

Open the app from your Dock or Applications folder. It starts the dashboard
itself and opens the window.

**Closing the window does not stop it.** Click the Dock icon to bring it back.
**Quit** stops the server properly.

## Restarting, and why you will need to

**A running app does not pick up new code.** Nothing warns you about this. It
keeps working exactly as before, which reads as "the fix did not work".

So after any update:

- **From the app:** menu bar icon, then **Restart**. If you cannot see the icon,
  Quit from the Dock and open it again.
- **From Terminal:**

```bash
pkill -f "linkedin_automation.dashboard"
```

then open the app again.

**Check you are actually on the new build:**

```bash
ps -eo etime,command | grep "[l]inkedin_automation.dashboard"
```

The elapsed time should be seconds, not hours.

## Getting fixes

```bash
cd ~/Projects/linkedin-autocomment
git pull
```

Then restart, as above. If the update touched dependencies, run `./setup.sh`
again. It is always safe to re-run.

## Checking nothing was left behind

An interrupted browser job can leave a Chrome running that holds your profile.
It is invisible: no window, no entry in the dashboard.

```bash
ps -eo pid,command | grep "[c]hrome_sessions"
```

Expect nothing. If something is listed, `kill <pid>`. While it is alive, every
run and every login attempt fails with an unhelpful message.

---

# Part 6: Tips and tricks

**"Min Quality Posts" is not a quality threshold.** It is how many good posts to
find before the scraper stops scrolling. The actual relevance bar is fixed
internally and is not adjustable from the screen. Setting this to 5 expecting a
low bar does not do that. If you are getting too few posts, raise **Max posts
to scan** instead, which is the knob that genuinely widens the net. In testing,
scanning more posts took the keep rate from 5% to 22%.

**Do the review gates properly, especially the first week.** The drafts are
good, not perfect. The ones that read as generic are the ones that make you
look automated. Rejecting half is normal early on and it teaches you what to
change in the persona settings.

**Fix your keywords before your first real scrape.** They ship tuned for AI
topics. Everything else gets binned until you change them.

**Start every new capability at 2 or 3, not the default.** Ten posts scanned,
two comments, two connection requests. You learn the same amount and risk
almost nothing.

**One browser job at a time, per profile.** Scrape, Post and Connect all drive
Chrome, and the app refuses to run two at once. If a button says a browser task
is already running, that is why.

**Leave the Chrome window alone while it works.** Clicking in it, scrolling it,
or closing it mid-run will break that run.

**Notes on connection requests are limited on free LinkedIn accounts.** When
your allowance runs out, "Add a note" stops appearing and the app stops sending
rather than sending blanks. That is not a bug.

**Expect it to break every few months.** LinkedIn redesigns its pages, the app
stops finding things, and scrapes return nothing. **Check Selectors** tells you
whether that has happened. It is the single most useful button when something
stops working.

**Watch your spend in Settings.** It is small, but it is real, and it is your
card.

**Comments are recorded the moment they are posted.** If the app crashes
mid-run, restarting it will not re-post anything. You can restart safely.

**Your session lasts weeks, not days.** If you are asked to log in daily,
something is wrong. Check for a stray Chrome as described in Part 5.

**Quality beats volume, and LinkedIn agrees.** Five thoughtful comments a day
will do more for you than fifty generic ones, and it is also the profile that
does not get flagged.

---

# Part 7: When something goes wrong

| What you see | What it usually means |
|---|---|
| A fix was applied but nothing changed | You did not restart. See Part 5. |
| Nothing at all happens on first open | You double-clicked instead of right-click then Open. See Step 4. |
| "The login check could not run" | A leftover Chrome is holding your profile. See Part 5. |
| Chrome opens then closes immediately | Session expired. Press **Log in** again. |
| A scrape returns nothing | Press **Check Selectors**. If it is red, LinkedIn changed its layout. |
| Everything gets binned as low quality | Your keywords do not match your feed. See Part 4. |
| A button does nothing | Read the log panel underneath it. |
| A run stopped early | Check whether you pressed Stop, or whether it stopped itself after three email-verification prompts. |
| "A browser task is already running" | Another Chrome job is going. Wait for it. |
| Generation fails with a provider error | Settings, then **Test Connection**. Usually a wrong model name. |

---

## What would help most as a tester

This has been used properly by exactly one person, so anything you hit is
probably new. Most useful to report, in order:

1. **Anything that fails silently.** A button that does nothing, a run that
   finishes having done less than you expected with no explanation. That class
   of bug has been the most common one in this project by a wide margin, and it
   is the hardest to find from the inside.
2. **Where these instructions were wrong or unclear**, including this document.
3. **Anything that surprised you**, especially if the tool did something on
   LinkedIn you did not expect. That matters more than a crash, because a crash
   is visible and this is not.
4. **What it cost you.**

Screenshots of the log panel are worth more than a description. Copy the red
text if there is any.
