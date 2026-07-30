#!/usr/bin/env bash
# Double-clickable macOS launcher: starts the dashboard and opens the browser.
# Closing this Terminal window stops the server (and the scheduler with it).
cd "$(dirname "$0")"

# Open the dashboard once the server has had a moment to bind the port. Runs in
# the background so it does not block the server itself.
( sleep 4; open "http://localhost:6500" >/dev/null 2>&1 ) &

./run.sh
status=$?

echo
echo "Dashboard stopped (exit $status)."
read -r -p "Press Return to close this window..."
