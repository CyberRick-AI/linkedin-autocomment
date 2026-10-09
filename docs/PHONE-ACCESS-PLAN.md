# Plan: use Guild Autopilot from your Android phone

**Goal:** open the dashboard on your phone, from anywhere, as a home-screen app that is safe to use. The Mac still does all the work. The phone is the remote control.

**Where the work happens:** the `feat/macos-app` branch on your fork (`~/Projects/LinkedIn/fork-mac`). It's tested the same way as everything so far, and nothing goes to Jeff's repo.

**Time:** about 2–3 hours of my work, plus about 15 minutes of yours.

---

## Why it isn't safe to open up today

| Problem | Risk if exposed | Fix in this plan |
|---|---|---|
| Debug mode is on (`dashboard.py:2709`) | Anyone who can reach the page can **run commands on your Mac** | Step 1 |
| No login | Anyone who reaches it can comment, connect and post **as you** on LinkedIn and X | Step 2 |
| Only reachable on the Mac | The phone can't connect | Step 3 (Tailscale) |

Right now it's only reachable from the Mac itself, which is why it's safe. That stays true until Step 3, and Step 3 only happens after Steps 1 and 2 are done and tested.

---

## Step 1: Turn off debug mode (me, ~20 min)
- Start the dashboard with debug off. The Mac app's Start/Stop/Restart already handles the server itself, so it doesn't need the debug restarter.
- Update the Restart button code and tests, which currently expect debug mode's two server processes.
- Add a test that **fails if debug mode is ever turned back on**.

## Step 2: Add a login (me, ~1 hour)
- **A dashboard password, separate from LinkedIn.** You set it once in Settings, and it's stored in the **Mac Keychain** like your other secrets. It's never in a file.
- **Every page and action requires login**, except the login page itself.
- **Stays logged in on your phone for 30 days**, then asks again. A "Sign out everywhere" button in Settings.
- **Protection against outside sites** triggering actions in your logged-in browser (a CSRF check on every action).
- **Slows down guessing:** after 5 wrong passwords it waits before allowing more attempts.
- **On the Mac itself:** you choose whether the built-in app window also asks for the password. Default: no, since it's your own machine.
- Tests for all of it: logged out is refused, a wrong password is refused, the throttle works, and actions without the CSRF check are refused.

## Step 3: Private connection with Tailscale (you ~15 min, me ~20 min)
Tailscale creates a **private network between your own devices**. The dashboard is never on the public internet: nothing is opened on your router, and only devices signed in to your Tailscale account can reach it.

**You:**
1. Install **Tailscale** on the Mac (tailscale.com/download) and sign in. The free plan is enough.
2. Install **Tailscale** on your Android phone (Play Store) and sign in with the **same account**.

**Me:**
3. Run `tailscale serve`, which gives the dashboard a private **https** address like `https://cyberricks-mini.<your-tailnet>.ts.net`. The dashboard itself stays bound to the Mac only, and Tailscale relays to it.
4. Show that address in Settings → "Open on your phone", with a QR code.

## Step 4: Make it feel like a phone app (me, ~30 min)
- **Add to Home screen:** the Guild seal as the app icon, opening full-screen without browser bars.
- **Phone layout fixes:** the redesign already fits phone screens. I'd add a **bottom tab bar** (Home · Calendar · Engage · Create · Settings) so it's easy to use one-handed, and make buttons comfortable to tap.
- **⌘K on the phone:** the search button opens the same command list.

## Step 5: Keep the Mac available (together, ~10 min)
- **Open at login:** add Guild Autopilot to Login Items so it starts with the Mac.
- **Don't sleep:** System Settings → Energy → *Prevent automatic sleeping when the display is off*.
- **What phone actions do:** some (scrape, post comments, log in) open Chrome **on the Mac**. The phone shows progress and results, so you don't need to be at the Mac.

## Step 6: Test together (~15 min)
On the Mac, then on your phone over Wi-Fi, then **on mobile data (Wi-Fi off)** to prove it works away from home:
- [ ] Login works, a wrong password is refused, staying logged in works
- [ ] Home shows the live queue ring and calendar
- [ ] ⌘K / search works
- [ ] Add to Home screen gives the Guild icon
- [ ] A phone with Tailscale **off** can't reach it at all

---

## What you'll need ready
- Your Mac on and awake, and about 15 minutes with your phone nearby
- A dashboard password you haven't used anywhere else
- Willingness to install Tailscale on both devices (free)

## Options for later
- Notifications on your phone when a post goes live or a step fails
- The "ask Grok" box in the command bar, for real this time
- Upgrade to Buffer Essentials (~$10/month for both channels), so LinkedIn sign-up links go in the first comment without the Mac being involved
