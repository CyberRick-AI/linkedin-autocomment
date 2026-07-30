#!/usr/bin/env bash
# Double-clickable macOS installer. Finder runs .command files in Terminal,
# starting in the user's home directory — hence the cd below.
cd "$(dirname "$0")"

./setup.sh
status=$?

echo
if [ $status -eq 0 ]; then
    echo "Setup finished. Next: edit .env with your OpenAI API key,"
    echo "then see docs/INSTALL-MACOS.md for the remaining steps."
else
    echo "Setup did not complete (exit $status). The error is above."
fi

echo
read -r -p "Press Return to close this window..."
