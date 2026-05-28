#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  ComponentDB — Starting up"
echo "========================================"

# Create venv if needed
if [ ! -d "venv" ]; then
  echo "→ Creating Python virtual environment…"
  python3 -m venv venv
fi

# Activate
source venv/bin/activate

# Install deps
echo "→ Installing dependencies…"
pip install -q -r requirements.txt

# Launch
echo "→ Starting server…"
python app.py