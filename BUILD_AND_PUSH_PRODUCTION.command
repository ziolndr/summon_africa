#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
cd "$ROOT"

REMOTE_EXPECTED="https://github.com/ziolndr/summon_africa.git"
MIN_RECORDS="${SUMMON_AFRICA_MIN_RECORDS:-500}"
PER_CHANNEL="${SUMMON_AFRICA_PER_CHANNEL:-100}"
MAX_RECORDS="${SUMMON_AFRICA_MAX_RECORDS:-3000}"

if [[ ! -d .git ]]; then
  echo "ERROR: run this command from the cloned summon_africa Git repository."
  exit 1
fi

git remote set-url origin "$REMOTE_EXPECTED"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

ARGS=(
  --per-channel "$PER_CHANNEL"
  --max-records "$MAX_RECORDS"
  --min-records "$MIN_RECORDS"
  --workers 5
)

if [[ -n "${YOUTUBE_API_KEY:-}" ]]; then
  ARGS+=(--api-key "$YOUTUBE_API_KEY")
fi

python ingest_youtube_playable.py "${ARGS[@]}"
python build_field.py --min-records "$MIN_RECORDS"

COUNT="$(python - <<'PY'
import json
print(int(json.load(open('SUMMON_field_manifest.json'))['count']))
PY
)"

if (( COUNT < MIN_RECORDS )); then
  echo "ERROR: field contains only $COUNT records; refusing to push."
  exit 1
fi

echo "PRODUCTION FIELD READY · $COUNT playable records"

git add -A
git commit -m "Build SUMMON Africa playable field with ${COUNT} titles" || true
git pull --rebase origin main
git push origin main

echo "PUSHED · $(git rev-parse --short HEAD) · $COUNT playable records"
