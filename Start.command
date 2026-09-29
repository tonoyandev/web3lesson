#!/bin/bash
# Double-click to open anti-persona on macOS.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is needed. Get it from https://www.python.org/downloads/"
  read -r -p "Press Enter to close."
  exit 1
fi
python3 app.py
