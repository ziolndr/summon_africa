#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q -r requirements.txt

MIN_RECORDS="${SUMMON_AFRICA_MIN_RECORDS:-150}"
PER_CHANNEL="${SUMMON_AFRICA_PER_CHANNEL:-45}"
MAX_RECORDS="${SUMMON_AFRICA_MAX_RECORDS:-1800}"
CURRENT="$(python - <<'PY'
import json
from pathlib import Path
p=Path('SUMMON_field_manifest.json')
try: print(int(json.loads(p.read_text()).get('count') or 0))
except Exception: print(0)
PY
)"

if [[ "${SUMMON_AFRICA_SKIP_SYNC:-0}" != "1" && "$CURRENT" -lt "$MIN_RECORDS" ]]; then
  echo "SUMMON AFRICA — EXPANDING PLAYABLE FIELD"
  echo "────────────────────────────────────────────────────────"
  set +e
  python ingest_youtube_playable.py \
    --per-channel "$PER_CHANNEL" \
    --max-records "$MAX_RECORDS"
  INGEST_STATUS=$?
  set -e
  if [[ $INGEST_STATUS -ne 0 ]]; then
    echo "Playable sync did not complete; retaining the current catalog."
  fi
fi

NEEDS_BUILD=0
[[ ! -f SUMMON_field_manifest.json ]] && NEEDS_BUILD=1
[[ -f data/imported.jsonl && data/imported.jsonl -nt SUMMON_field_manifest.json ]] && NEEDS_BUILD=1
[[ -f data/manual.jsonl && data/manual.jsonl -nt SUMMON_field_manifest.json ]] && NEEDS_BUILD=1
[[ data/seed.json -nt SUMMON_field_manifest.json ]] && NEEDS_BUILD=1

if [[ $NEEDS_BUILD -eq 1 || "$CURRENT" -lt "$MIN_RECORDS" ]]; then
  echo ""
  echo "SUMMON AFRICA — BUILDING 72D PLAYABLE FIELD"
  echo "────────────────────────────────────────────────────────"
  python build_field.py
fi

COUNT="$(python - <<'PY'
import json
from pathlib import Path
p=Path('SUMMON_field_manifest.json')
try: print(int(json.loads(p.read_text()).get('count') or 0))
except Exception: print(0)
PY
)"

echo ""
echo "SUMMON AFRICA READY · ${COUNT} PLAYABLE RECORDS"
echo "http://127.0.0.1:8798"
(sleep 1.2; open "http://127.0.0.1:8798") &
exec python serve_field.py --host 0.0.0.0 --port 8798
