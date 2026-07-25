FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=10000
ENV SUMMON_AFRICA_MIN_RECORDS=500


EXPOSE 10000
CMD ["sh", "-c", "python serve_field.py --host 0.0.0.0 --port ${PORT:-10000}"]
