#!/usr/bin/env bash
# One-time setup (needs internet). After this the app runs fully offline.
set -e
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
if [ "$PYTHON" = "python3" ] && command -v python3.11 >/dev/null 2>&1; then
  PYTHON=python3.11
fi
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
(cd frontend && npm install && npm run build)
echo
echo "Core install done. Now finish the CONFIGURE steps in README.md (models, voices, fonts, ffmpeg)."
