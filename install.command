#!/usr/bin/env bash
# Double-clickable macOS installer. Finder runs .command files in Terminal,
# starting in the user's home directory — hence the cd below.
#
# Three steps: dependencies, then the macOS-only extras the app needs, then
# build the app and put it in /Applications.
#
# The app is BUILT here rather than shipped prebuilt, because the bundle
# records where this project folder lives. A .app copied from someone else's
# machine points at a folder you do not have.
cd "$(dirname "$0")"

./setup.sh
status=$?

if [ $status -ne 0 ]; then
    echo
    echo "Setup did not complete (exit $status). The error is above."
    echo
    read -r -p "Press Return to close this window..."
    exit $status
fi

echo
echo "============================================"
echo "  Building the Mac app"
echo "============================================"
echo

# The menu bar item and the window need PyObjC. It is macOS-only, which is why
# it is not in requirements.txt. Without it the app still works, it just opens
# the dashboard in your browser instead of its own window.
if uv pip install -r requirements-macos.txt; then
    echo "macOS app support installed."
else
    echo "Could not install the macOS extras. The app will still run and will"
    echo "open the dashboard in your browser instead of its own window."
fi

echo
if uv run python tools/build_app.py --install; then
    APP_BUILT=1
else
    APP_BUILT=0
    echo "The app could not be built. You can still use ./run.sh."
fi

echo
echo "============================================"
echo "  Next steps"
echo "============================================"
echo
echo "1. Open 'LinkedIn Autocomment' from your Applications folder."
if [ "$APP_BUILT" = "1" ]; then
    echo "   It is unsigned, so the FIRST time you must RIGHT-CLICK it and"
    echo "   choose Open, then confirm. After that a normal double-click works."
fi
echo
echo "2. Edit .env in this folder and set OPENAI_API_KEY. You pay for your"
echo "   own usage."
echo
echo "3. Add a profile and sign in to LinkedIn once, from Terminal in this"
echo "   folder:"
echo "     uv run python -m linkedin_automation.profile_manager add <name>"
echo "     uv run python tools/login_check.py --profile <name>"
echo
echo "See docs/INSTALL-MACOS.md for detail."
echo
read -r -p "Press Return to close this window..."
