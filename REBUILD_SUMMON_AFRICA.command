#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
cd "$ROOT"
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -q -r requirements.txt
python build_field.py
