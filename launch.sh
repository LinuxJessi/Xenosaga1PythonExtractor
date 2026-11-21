#!/usr/bin/env bash
# Xenosaga I Extractor — Linux launcher.
# Run: ./launch.sh   (or double-click where your file manager allows it)
# Installs the kit's packages (requirements.txt: numpy, fonttools, uharfbuzz)
# for this user the first time they are missing (or into a kit-local .venv
# when that Python refuses user installs, PEP 668).
set -e
cd "$(dirname "$0")"
PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
fi
if [ -z "$PY" ]; then
    echo "Python 3 is not installed. Install it with your package manager"
    echo "(e.g. sudo apt install python3) and run this launcher again."
    exit 1
fi
CHECK="import numpy, fontTools, uharfbuzz"
# a kit-local virtual environment made by an earlier run (see below) wins
if [ -x .venv/bin/python ] && .venv/bin/python -c "$CHECK" >/dev/null 2>&1; then
    PY=.venv/bin/python
elif ! "$PY" -c "$CHECK" >/dev/null 2>&1; then
    echo "Installing the kit's Python packages (numpy, fonttools, uharfbuzz) for this user..."
    if "$PY" -m pip install --user -r requirements.txt; then
        :
    else
        # Homebrew's Python and Debian/Ubuntu's system Python refuse
        # "pip install --user" (PEP 668, "externally-managed-environment");
        # a virtual environment inside the kit folder always works
        echo "pip could not install for this user (on Homebrew and Debian/Ubuntu Pythons this is"
        echo "PEP 668, \"externally-managed-environment\"). Installing into a private environment"
        echo "in this folder instead (.venv)..."
        if "$PY" -m venv .venv && .venv/bin/python -m pip install -r requirements.txt \
                && .venv/bin/python -c "$CHECK" >/dev/null 2>&1; then
            PY=.venv/bin/python
        else
            echo "Could not install the packages in requirements.txt."
            echo "(On Debian/Ubuntu, venv needs: sudo apt install python3-venv; pip needs internet access.)"
            echo "To install by hand into the kit folder:"
            echo "  $PY -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
            echo "then run this launcher again."
            exit 1
        fi
    fi
fi
exec "$PY" gui.py "$@"
