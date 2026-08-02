# Operating the dashboard: start, restart, stop

Short runbook for the day-to-day. The one thing worth reading before anything
else is [Restarting after an update](#restarting-after-an-update), because the
obvious way to do it does not work.

Paths below are relative to the project folder (the one holding `run.sh`).

---

## The rule that catches everyone

**A running dashboard does not pick up code changes.**

Flask's auto-reloader only runs in debug mode, and debug mode is off by
default and deliberately so: it ships the Werkzeug debugger, which executes
Python typed into the browser. So after `git pull`, or after any edit, the
server keeps running the code it was started with, for as long as it stays up.

There is no symptom. The dashboard looks fine and behaves exactly as it did
before the update, which reads as "the fix did not work" rather than "the fix
is not loaded".

**Relaunching the app does not restart the server either.** The app checks
whether port 6500 is already answering and, if it is, simply opens the
dashboard rather than starting a second one. That is correct — two servers on
one port means the second fails to bind, and if it somehow succeeded there
would be two schedulers publishing from the same queue — but it does mean
double-clicking the app after an update changes nothing.

---

## Restarting after an update

Two steps, in this order.

**1. Stop what is running.**

```bash
pkill -f "linkedin_automation.dashboard"
```

**2. Start it again.** Either the app:

```bash
open "dist/LinkedIn Autocomment.app"
```

or a terminal:

```bash
./run.sh
```

Prefer the app. `run.sh` ties the server's life to that terminal window:
closing the window stops the dashboard, and it takes the scheduler with it.

**3. Confirm you are on the new code.**

```bash
ps -eo etime,command | grep "[l]inkedin_automation.dashboard"
```

The elapsed time should be seconds, not hours. If it still shows a long
uptime, step 1 did not take effect and you are still on the old build.

---

## Starting fresh

```bash
open "dist/LinkedIn Autocomment.app"
```

Or double-click `run.command` in Finder. Or `./run.sh` from a terminal.

The dashboard binds `127.0.0.1:6500` only. It is not reachable from another
machine, and it has no password, which is why the loopback bind matters.

To build the app bundle, or rebuild it after moving the project folder:

```bash
python tools/build_app.py
```

The bundle records the project's location at build time, so **move the folder
and the app stops working until you rebuild it.**

The app is unsigned. The first open is blocked by Gatekeeper: right-click it
and choose **Open**, then confirm. Once only.

---

## Stopping

```bash
pkill -f "linkedin_automation.dashboard"
```

If it was started from a terminal, Ctrl+C in that window does the same.

### Check nothing was left behind

A browser job that was interrupted can leave Chrome running, holding the
profile. It is invisible: no window, no dashboard entry.

```bash
ps -eo pid,command | grep "[c]hrome_sessions"
```

Expect no output. If something is listed, note its PID and:

```bash
kill <pid>
```

An orphan like this **locks the profile**, and every later run and every login
attempt fails while it holds the lock. The only symptom is "The login check
could not run", which names nothing useful.

The scripts now install a signal handler so a stop closes the browser
properly, so this should not recur. The check is here because the failure is
silent and the fix is ten seconds.

---

## When something looks wrong

| Symptom | First thing to check |
|---|---|
| A fix you just pulled has no effect | Uptime. The server is almost certainly still the old process. |
| "The login check could not run" | A stray Chrome holding the profile. See above. |
| The dashboard will not start | Something else on port 6500: `lsof -nP -iTCP:6500 -sTCP:LISTEN` |
| A button does nothing | The job log on that panel, then the server's own output. |
| A run stopped early | Whether you pressed Stop. A deliberate stop is reported as one. |

---

## Things that are not restarts

- **Refreshing the browser** reloads the page from the running server. It
  changes nothing about which code that server is executing.
- **Relaunching the app** opens the dashboard. See above.
- **Closing the dashboard tab** does nothing at all; the server keeps running.
