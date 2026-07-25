#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
cd "$ROOT"
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -q -r requirements.txt

python ingest_youtube_playable.py \
  --per-channel "${SUMMON_AFRICA_PER_CHANNEL:-75}" \
  --max-records "${SUMMON_AFRICA_MAX_RECORDS:-3000}"
python build_field.py

(sleep 1.2; open "http://127.0.0.1:8798") &
exec python serve_field.py --host 0.0.0.0 --port 8798
