# Production Dockerfile for Danecast Trades
# Builds frontend and backend together for Cloud Run deployment

# Stage 1: Build frontend
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci --production=false

COPY frontend/ ./
RUN npm run build


# Stage 2: Python backend with frontend static files
FROM python:3.12-slim

WORKDIR /app

# pg_dump is needed for the nightly backup job. Pin it to v17 to match the
# Neon production server (PG 17.10) — pg_dump refuses to dump from a newer
# server, and Debian bookworm's default postgresql-client is only v15.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the hash-pinned lockfile, so the image is
# reproducible and a tampered or swapped package fails the build rather than
# shipping. requirements.txt stays the source of truth for direct deps; see
# the regeneration command in its header.
COPY backend/requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy backend code
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini .

# Copy frontend build from stage 1
COPY --from=frontend-builder /frontend/dist ./static

# Cloud Run sets PORT environment variable (default 8080)
ENV PORT=8080

EXPOSE 8080

# Run as an unprivileged user so an RCE in the app or a dependency can't
# rewrite the image's code or packages. The code stays root-owned (read-only
# to the app); the only runtime writes are the FMP/scanner cache, created
# relative to the CWD as ./scanner_cache, and pg_dump's temp file for the
# nightly backup, which goes to /tmp (world-writable already).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app \
    && install -d -o app -g app /app/scanner_cache
USER app

# Run with production settings
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
