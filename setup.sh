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
echo "  Next steps:"
echo "  1. Edit .env with your OpenAI API key"
echo "  2. Add a profile: uv run python -m linkedin_automation.profile_manager add <name>"
echo "  3. Log in once:   uv run python tools/login_check.py --profile <name>"
echo "  4. Run: uv run python -m linkedin_automation.dashboard  (or ./run.sh)"
echo "  5. Open: http://localhost:6500"
echo "============================================"
