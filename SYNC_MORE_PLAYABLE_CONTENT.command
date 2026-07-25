#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
cd "$ROOT"
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -q -r requirements.txt

PER_CHANNEL="${SUMMON_AFRICA_PER_CHANNEL:-75}"
MAX_RECORDS="${SUMMON_AFRICA_MAX_RECORDS:-3000}"

python ingest_youtube_playable.py \
  --per-channel "$PER_CHANNEL" \
  --max-records "$MAX_RECORDS"
python build_field.py

echo ""
echo "Playable catalog synchronized and rebuilt."
echo "Restart START_SUMMON_AFRICA.command if the server was already running."
