#!/usr/bin/env bash
# LinkedIn Automation — setup for macOS and Linux (mirrors setup.bat).
# Creates the venv, installs dependencies, seeds .env, and makes data/.
set -euo pipefail

cd "$(dirname "$0")"

echo "============================================"
echo "  LinkedIn Automation - Setup (uv)"
echo "============================================"
echo

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found. Install it:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo
    echo "Then close this terminal, open a new one, and run setup again."
    exit 1
fi

echo "Creating virtual environment and installing dependencies..."
# Re-running setup must be safe: reuse an existing venv rather than failing on
# it (uv errors out if .venv is already there). Delete .venv to start clean.
if [ -d ".venv" ]; then
    echo "Reusing existing virtual environment (.venv)"
else
    uv venv
fi
uv pip install -r requirements.txt
# Dev/test tooling (pytest, ruff) so 'uv run pytest' works after setup
uv pip install -r requirements-dev.txt

if [ ! -f ".env" ]; then
    echo
    echo "Creating .env from template..."
    cp .env.example .env
    echo
    echo "*** IMPORTANT: Edit .env and add your OPENAI_API_KEY ***"
    echo
fi

mkdir -p data

echo
echo "============================================"
echo "  Setup complete!"
echo
echo "  Next steps — all of them in the dashboard, no terminal needed:"
echo
echo "  1. Start it:  ./run.sh      (then open http://localhost:6500)"
echo "  2. Press + in the header to create a profile."
echo "  3. Open Settings, choose a provider, paste your API key."
echo "  4. Press 'Log in' and sign in to LinkedIn in the window that opens."
echo
echo "  On a Mac, install.command builds an app so you can skip step 1."
echo "============================================"
