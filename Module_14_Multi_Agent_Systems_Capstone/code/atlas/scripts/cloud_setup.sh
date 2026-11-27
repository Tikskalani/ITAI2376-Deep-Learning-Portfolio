#!/usr/bin/env bash
# Atlas cloud environment setup script.
#
# Paste the body of this file into the "Setup script" field of the Atlas
# cloud environment at claude.ai/code. It runs as root on a fresh Ubuntu
# 24.04 VM before Claude Code launches, and its results are cached as a
# filesystem snapshot, so it should only do things that persist on disk:
# pull Docker images, install packages. Nothing that must be *running*
# belongs here; scripts/cloud_session_start.sh handles that per session.
#
# Keep total runtime under about five minutes or the snapshot is not cached.
set -euo pipefail

# Locate the repository checkout. The VM clones it before this runs; the
# path varies, so find the compose file rather than assuming a directory.
REPO="$(dirname "$(find / -maxdepth 4 -name docker-compose.yml -path '*atlas*' 2>/dev/null | head -n 1)")"
if [ -z "$REPO" ] || [ ! -f "$REPO/pyproject.toml" ]; then
  echo "[cloud_setup] atlas checkout not found; skipping repo-specific steps"
  exit 0
fi
cd "$REPO"
echo "[cloud_setup] repo at $REPO"

# Postgres client for the migration runner, in parallel with the image pulls.
(apt-get update -qq && apt-get install -y -qq postgresql-client >/dev/null) &

# Pull the two images so each session starts with them on disk.
(docker compose pull --quiet) &

# Python dependencies, including dev tools, into the system interpreter.
(pip install --quiet -e ".[dev]") &

wait

# Pull the embedding model into the Ollama volume so the cached snapshot
# carries it and no session has to download it. Requires registry.ollama.ai
# and ollama.com on the environment's allowed domains (Custom network access
# with the default package-manager list also enabled).
export POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up -d ollama
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' atlas-ollama 2>/dev/null)" = "healthy" ] && break
  sleep 2
done
docker exec atlas-ollama ollama pull nomic-embed-text || echo "[cloud_setup] model pull failed; the session hook retries"
docker compose stop

echo "[cloud_setup] done"
