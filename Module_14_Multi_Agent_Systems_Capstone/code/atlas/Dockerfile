# Atlas application image: the web surface and the worker share it.
#
# Built by the `web` and `worker` services in docker-compose.yml (profile
# "app"). Dependencies come from uv.lock, so the image matches a fresh
# `uv sync` on the laptop. Only third-party packages go into the venv; the
# Atlas source stays at /app and is imported from there, because
# core/config.py derives REPO_ROOT (agents/, procedures/, workflows/,
# core/web/static/) from the location of core/config.py itself.
#
# Secrets never enter the image: .env is excluded by .dockerignore and
# reaches the containers at run time through compose `env_file`.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN pip install --no-cache-dir "uv>=0.5,<1"

WORKDIR /app

# Dependency layer first so a code change does not reinstall packages.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agents ./agents
COPY core ./core
COPY procedures ./procedures
COPY scripts ./scripts
COPY workflows ./workflows

# Unprivileged runtime user. The bind-mounted logs/, backups/ and
# procedures/ need to be writable by this uid on a Linux host (VPS); Docker
# Desktop on Windows maps bind mounts without uid checks.
RUN useradd --create-home --uid 10001 atlas \
    && mkdir -p /app/logs /app/backups /home/atlas/.cache \
    && chown -R atlas:atlas /app /home/atlas/.cache
USER atlas

EXPOSE 8000

# The compose services set the real command; this is the web default.
CMD ["python", "-m", "scripts.run_web", "--host", "0.0.0.0", "--port", "8000"]
