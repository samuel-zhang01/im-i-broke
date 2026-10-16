# Backend: FastAPI + parsers/analytics. Also used by the explicit
# `import` profile to populate a genuinely new source-only installation.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==26.2.1 && \
    pip install --no-cache-dir -r requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends age && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY scripts ./scripts
# Host checkout permissions must not make root-owned source unreadable to ledger.
# Restrict normalization to application code; household directories stay private.
RUN chmod -R a=rX /app/app /app/scripts

# /app/data holds the SQLite DB; /app/uploads holds user-added statement PDFs.
# Both are mounted as named volumes so they persist and are shared with the
# source import service.
RUN groupadd --gid 10001 ledger && useradd --uid 10001 --gid ledger --no-create-home ledger && \
    mkdir -p /app/data /app/uploads /app/keys /app/backups && chown ledger:ledger /app/data /app/uploads /app/keys /app/backups && \
    chmod 700 /app/data /app/uploads /app/keys /app/backups
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
USER 10001:10001
ENTRYPOINT ["/entrypoint.sh"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-server-header", "--no-access-log", "--limit-concurrency", "32", "--timeout-keep-alive", "5"]
