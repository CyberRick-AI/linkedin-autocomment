#!/usr/bin/env bash
# LinkedIn Automation — start the dashboard on macOS and Linux (mirrors run.bat).
set -euo pipefail

cd "$(dirname "$0")"

echo "Starting LinkedIn Dashboard on http://localhost:6500"
uv run python -m linkedin_automation.dashboard
