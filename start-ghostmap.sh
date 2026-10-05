#!/usr/bin/env bash
# Ghost Map launcher (runs from source) for macOS / Linux.
# First run creates .venv and installs dependencies (needs internet once).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Ghost Map, this takes a minute..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e . || { rm -rf .venv; echo "Setup failed"; exit 1; }
fi
exec .venv/bin/python -m ghostmap web --open "$@"
