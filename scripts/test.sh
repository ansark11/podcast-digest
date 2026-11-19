#!/usr/bin/env bash
# Runs the unit tests; exits non-zero if any fail. This is <TEST_COMMAND>.
set -euo pipefail
cd "$(dirname "$0")/.."
py=python3
[ -x .venv/bin/python ] && py=.venv/bin/python
"$py" -m unittest discover -s tests -q
