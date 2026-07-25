FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=10000
ENV SUMMON_AFRICA_MIN_RECORDS=500

RUN python - <<'PY'
import json
from pathlib import Path
p = Path('SUMMON_field_manifest.json')
if not p.exists():
    raise SystemExit('Missing SUMMON_field_manifest.json')
count = int(json.loads(p.read_text())['count'])
if count < 500:
    raise SystemExit(f'Refusing Docker build: catalog has only {count} records; minimum is 500')
print(f'Docker catalog validation passed: {count:,} records')
PY

EXPOSE 10000
CMD ["sh", "-c", "python serve_field.py --host 0.0.0.0 --port ${PORT:-10000}"]
