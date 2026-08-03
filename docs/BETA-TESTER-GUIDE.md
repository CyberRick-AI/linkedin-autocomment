# LinkedIn Autocomment — setup guide for beta testers

Everything you need, in order. Set aside about twenty minutes for the first
run, most of which is waiting for downloads.

**Mac only.** It drives a real Chrome window, so it needs a desktop machine.

---

## What this thing actually does

It finds posts on your LinkedIn feed, writes draft comments with an AI model,
and shows them to you. **You approve every comment before it is posted.** It
also sends connection requests, writes posts, and can schedule publishing.

Two things worth knowing before you start:

- **You pay for the AI.** You bring your own API key. Typical cost is a
  fraction of a cent per comment — a full run of 7 comments cost $0.0062 in
  testing — but it is your account and your bill.
- **It automates your real LinkedIn account.** LinkedIn does not love
  automation. It has rate limits and can show verification challenges. Start
  small. The daily caps in the app exist for a reason; do not raise them on
  day one.

---

## Step 1 — Install `uv`

`uv` manages the Python side. Open **Terminal** (Cmd+Space, type "Terminal")
and paste this:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Then close Terminal and open a new one.** The installer changes your PATH
and the old window will not see it.

Check it worked:

```bash
uv --version
```

A version number means you are fine. "command not found" means the new-window
step was skipped.

---

## Step 2 — Get the code

```bash
mkdir -p ~/Projects && cd ~/Projects
git clone --branch beta https://github.com/CyberRick-AI/linkedin-autocomment.git
```

> **`--branch beta` matters. Do not leave it out.** This is a fork of someone
> else's project, and the fork's default branch is still the original author's
> code — none of what you are testing is on it. Copy the command exactly.
>
> `beta` is the branch you want and it will stay that way. It moves forward as
> fixes land, so `git pull` in that folder gets you the latest.

If `git` is missing, macOS will offer to install the developer tools. Accept,
wait, then run the command again.

---

## Step 3 — Run the installer

In Finder, open `~/Projects/linkedin-autocomment` and **double-click
`install.command`**.

A Terminal window opens and runs for a few minutes. It installs everything,
builds the Mac app, and puts it in your Applications folder.

> If macOS refuses to run it, right-click `install.command`, choose **Open**,
> and confirm. Same reason as Step 4.

When it finishes it prints the next steps. Leave the window open for
reference; you can close it when you are done.

---

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
at the top of the screen, but if your menu bar is crowded macOS hides it — use
the Dock.

---

## Step 5 — Create a profile

A window opens showing the dashboard.

Press the **+** button in the top right. Give it any name you like — your own
first name is fine. This is just a label for a set of settings and a browser
session.

---

## Step 6 — Choose an AI provider and paste a key

Open the **Settings** tab, choose a provider from the dropdown, and paste your
key. The app stores it in your Mac's Keychain, not in a file.

You only need **one**. Here is the honest comparison.

### The easy choices

| Provider | Get a key at | Notes |
|---|---|---|
| **OpenAI** | platform.openai.com → API keys | The default. Model `gpt-4o-mini` is cheap and good enough for comments. |
| **Anthropic** | console.anthropic.com | Claude. Default is `claude-haiku-4-5`, the fast cheap one. |
| **xAI** | console.x.ai | Grok. Default `grok-4`. This is what the tool was tested with most. |

All three want a credit card and a few dollars of prepaid credit. You will not
get through it quickly.

### The cheaper or faster ones

| Provider | Get a key at | Notes |
|---|---|---|
| **DeepSeek** | platform.deepseek.com | Cheap. Ships a default model (`deepseek-chat`), so nothing to look up. |
| **Groq** | console.groq.com | Very fast, generous free tier. |
| **Together** | api.together.xyz | Many open models behind one key. |
| **Mistral** | console.mistral.ai | European, solid. |
| **Fireworks** | fireworks.ai | Fast open-model hosting. |
| **OpenRouter** | openrouter.ai | One key, hundreds of models from every vendor. Good if you want to experiment. |

> **Groq, Together, Mistral, Fireworks and OpenRouter ship no default model**,
> so you have to type the exact model name into the Model box yourself. That is
> deliberate: these providers rename and retire models often, and a guessed
> default that quietly stops existing is worse than an empty box. Copy the
> name from the provider's own model list. The Settings screen tells you when
> a provider needs this.
>
> OpenAI, Anthropic, xAI and DeepSeek fill the model in for you.

### Free, if you do not want to pay anyone

| Provider | Notes |
|---|---|
| **Ollama** | Runs a model on your own Mac. No key, no cost, no data leaving your machine. Install from ollama.com, run `ollama pull llama3.1`, then choose Ollama and type `llama3.1`. Slower, and quality depends on your hardware. Needs a reasonably recent Mac with plenty of memory. |

### If your provider is not in the list

Choose **Custom (OpenAI-compatible)**. Paste the provider's base URL and the
model name, and it will work with no code change. Most providers these days
speak the same API format, so this covers almost anything — including a model
running on your own server.

If you are not sure what to pick: **xAI**, because it is the combination that
has had the most real use here.

### Then press Test Connection

It sends one tiny prompt and tells you what actually worked. **This is the
only thing in the app that spends money without you starting a run**, and it
costs a fraction of a cent. Do it — it catches a wrong key or a wrong model
name immediately, instead of halfway through your first real run.

---

## Step 7 — Log in to LinkedIn

Press **Log in** in the dashboard header.

A Chrome window opens on LinkedIn's own sign-in page. **Type your password
there.** It goes to LinkedIn and never passes through this app — the app has
no way to see it.

Sign in, complete any verification LinkedIn asks for, and leave the window
alone. The app notices when you are in and closes it itself.

Your session is saved, so you only do this every few weeks.

---

## Step 8 — Your first run, small

Go to the **Comment Pipeline** tab.

1. **Scrape** — set *Max posts to scan* to **10**, not the default. Press
   Start Scraping. Chrome opens and scrolls your feed. Do not touch it.
2. **Review Posts** — look at what it found. Bin anything you would not
   comment on.
3. **Generate** — writes draft comments. This is the step that costs money.
4. **Review Comments** — **read every one.** Edit anything that does not sound
   like you. This gate is the product; do not skip it.
5. **Post** — puts them on LinkedIn under your name.

Start with two or three comments. Judge whether they sound like you before
doing more.

---

## What to watch out for

**The two review steps are the whole point.** The tool drafts; you decide.
Anything that gets posted is yours, in public, under your name.

**Start slow on connections.** The Auto-Connector defaults to 25 requests a
day. LinkedIn watches this closely. If it starts asking "Do you know this
person?" repeatedly, the app stops itself after three in a row — that is
deliberate, and pushing past it is how accounts get restricted.

**Notes on connection requests are limited.** LinkedIn caps how many
invitations can carry a personal note on a free account. When you run out, the
app stops sending rather than sending blank invitations you did not write.

**It breaks every few months, predictably.** LinkedIn redesigns its pages and
the tool stops finding things. That is expected, not a defect. The **Check
Selectors** button tells you whether that has happened.

---

## Getting fixes

This is under active development and you will be sent fixes. To pick them up:

```bash
cd ~/Projects/linkedin-autocomment
git pull
```

Then **quit the app and open it again** — or use **Restart** in its menu.

> A running app does not pick up new code. Nothing warns you about this: it
> keeps working exactly as it did before, which looks like the fix did not
> work. If you pull and nothing changes, you have not restarted.

If the update touched dependencies, run `./setup.sh` again. It is safe to
re-run at any time.

---

## When something goes wrong

| What you see | What it usually means |
|---|---|
| A fix was applied but nothing changed | The app needs restarting. Menu → **Restart**. |
| "The login check could not run" | A leftover Chrome is holding your profile. Quit the app fully and reopen it. |
| A button does nothing | Look at the log panel underneath it. |
| Nothing at all happens on first open | You double-clicked instead of right-click → Open. See Step 4. |
| Chrome opens and immediately closes | Your saved session expired. Press **Log in** again. |

`docs/OPERATING.md` covers start, stop and restart in more detail.

---

## What would help most as a tester

This has been used properly by exactly one person, so anything you hit is
probably new. Most useful to report:

1. **Anything that fails silently** — a button that does nothing, a run that
   finishes having done less than you expected with no explanation. That class
   of bug has been the most common one here by a wide margin.
2. **Where the instructions were wrong or unclear**, including this document.
3. **Anything that surprised you**, especially if the tool did something on
   LinkedIn you did not expect. That matters more than a crash, because a
   crash is visible and this is not.
4. **What it cost you.** The Settings screen tracks spending.

Screenshots of the log panel are worth more than a description. Copy the red
text if there is any.
