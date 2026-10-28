# syntax=docker/dockerfile:1
# =============================================================================
# Daylio API Server — multi-stage Docker image (Task 0.6, WORKPLAN §5.1/§8.3)
# -----------------------------------------------------------------------------
# Stage 1 "build" : python:3.12-slim, installs pip-tools and the locked
#                   dependencies (requirements.lock) into /opt/venv.
# Stage 2 "runtime": python:3.12-slim, copies the venv + the package sources,
#                   runs the WSGI app with Waitress (no gunicorn, R10).
# =============================================================================

FROM python:3.14-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=100

WORKDIR /build

# Install the LOCKED dependencies into a real venv (R10: lean image, no
# gunicorn). pip-compile --check consistency is verified by the CI lint job
# (Phase 7).
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock

# -----------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Application configuration: if not provided, the resolver falls back to
# config/config.example.yml baked into the image. Production deployments set
# CONFIG_FILE=/app/config/config.yml (mount the dir or write the file) and
# provide secrets through environment variables (never baked in, R7).
# ENV CONFIG_FILE=/app/config/config.yml

COPY --from=build /opt/venv /opt/venv
COPY src/ /app/src/
COPY version.ini /app/version.ini
COPY config/config.example.yml /app/config/config.example.yml

# Runtime data directory (SQLite source of truth; mount ./data here).
RUN mkdir -p /app/data

EXPOSE 5000

# WSGI entry point exposed by src/daylio_api/app.py (the `app` variable).
CMD ["waitress-serve", "--port=5000", "daylio_api.app:app"]