# RouteShield backend - FastAPI + SQLite (PostgreSQL-compatible data layer)
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (kept minimal)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data/campaigns ./data/campaigns

# Persistent data directory (SQLite DB + cached report PDFs) - mount a volume
# at /data in production so database state survives container rebuilds.
RUN mkdir -p /data/reports
ENV DATABASE_URL=sqlite:////data/routeshield.db \
    REPORT_CACHE_DIR=/data/reports

# Non-root user
RUN useradd -m -u 10001 routeshield && chown -R routeshield:routeshield /app /data
USER routeshield

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
