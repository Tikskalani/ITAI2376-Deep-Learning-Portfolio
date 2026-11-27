#!/usr/bin/env bash
# Atlas cloud session start.
#
# Runs from the SessionStart hook in .claude/settings.json on every cloud
# session (new or resumed). Brings up what must be *running* for the test
# suite: Postgres with pgvector, Ollama with nomic-embed-text, the schema.
# Exits immediately on a laptop so it never touches a local environment.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$(git rev-parse --show-toplevel)"
echo "[session_start] atlas cloud session starting in $(pwd)"

# Test-only configuration. No real secrets live in the cloud. Settings
# requires these keys to be present; the agent tests mock the API client.
if [ ! -f .env ]; then
  TEST_POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > .env <<ENV
ANTHROPIC_API_KEY=sk-ant-cloud-test-placeholder
ATLAS_MODEL_CEO=claude-opus-5-5
ATLAS_MODEL_SPECIALIST=claude-sonnet-5-5
ATLAS_MODEL_SCAN=claude-haiku-5-5
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5434
POSTGRES_DB=atlas
POSTGRES_USER=atlas
POSTGRES_PASSWORD=$TEST_POSTGRES_PASSWORD
OLLAMA_URL=http://127.0.0.1:11435
OLLAMA_PORT=11435
OLLAMA_EMBED_MODEL=nomic-embed-text
OLLAMA_EMBED_DIM=768
ATLAS_ENV=local
ATLAS_LOG_LEVEL=INFO
ATLAS_TIMEZONE=America/Chicago
ATLAS_DAILY_USD_CAP=0
ATLAS_HEARTBEAT_URL=
ENV
  echo "[session_start] wrote test-only .env"
fi

# Dependencies, in case the environment cache predates a pyproject change.
pip install --quiet -e ".[dev]"

# Infrastructure.
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
echo "[session_start] waiting for postgres and ollama health"
for _ in $(seq 1 60); do
  pg_ok=$(docker inspect -f '{{.State.Health.Status}}' atlas-postgres 2>/dev/null || echo starting)
  ol_ok=$(docker inspect -f '{{.State.Health.Status}}' atlas-ollama 2>/dev/null || echo starting)
  if [ "$pg_ok" = "healthy" ] && [ "$ol_ok" = "healthy" ]; then
    break
  fi
  sleep 2
done
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps

# Embedding model (a no-op when the volume already has it).
docker exec atlas-ollama ollama pull nomic-embed-text

# Schema, in lexical order, same as scripts/migrate.bat.
for f in db/migrations/*.sql; do
  echo "[session_start] applying $(basename "$f")"
  docker exec -i atlas-postgres psql -U atlas -d atlas -v ON_ERROR_STOP=1 -q < "$f"
done

echo "[session_start] ready: run 'python -m pytest -q'"
