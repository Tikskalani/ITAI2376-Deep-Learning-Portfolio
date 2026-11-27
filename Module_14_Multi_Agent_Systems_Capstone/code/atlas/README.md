# Atlas: A Multi-Agent Collaborative System

**ITAI 2376, AI Agent Creation capstone (Option 3: Multi-Agent Collaborative System)**
**Group name:** Atlas
**Group members:** Trilok Kalani (sole member)

Atlas is a personal multi-agent system: a CEO agent leads eleven specialist agents (COO, CFO, CTO, academic, career, client delivery, opportunity scout, news and discovery, build, personal ops, property ops). They share one long-term memory in Postgres with pgvector, run through one gated ReAct tool-use loop, ask for approval before any outward action, and learn from feedback (ratings, Accept/Rework on night work, job outcomes).

This package is the de-personalized source. Names of clients, family and employers were replaced with placeholders, real email addresses with `person@example.com`, and private config (`config/context.yaml`, the profile in `config/jobs.yaml`) with example values. No `.env`, data, logs or backups are included.

## Where things are

| Path | What it is |
|---|---|
| `agents/<name>/system.md`, `tools.py` | Each agent's role prompt and tools. The CEO (`agents/ceo`) is the leader and the only agent with `dispatch_agent`. |
| `core/runtime/agent.py` | The ReAct loop on the Anthropic Messages API: model call, tool gates, tool results, up to 12 turns. |
| `core/runtime/dispatcher.py` | Builds and runs an agent for a task; audit row, events, one retry with a nudge. |
| `core/runtime/policy.py`, `approval.py`, `ledger.py` | Fail-closed tool policy, approval cards, and the idempotent tool ledger. |
| `core/memory/` | Semantic, episodic, procedural and prospective memory; hybrid retrieval (vector + keyword + recency). |
| `core/outcomes.py`, `core/skills.py`, `core/lessons.py`, `core/reflection.py`, `core/trust.py` | Feedback, rewards and policy improvement (the reinforcement learning elements). |
| `core/night.py` | Planning then execution: proposals, approved overnight run, morning report with Accept/Rework. |
| `core/privacy.py`, `core/jobs/untrusted.py`, `core/logredact.py` | Privacy gate, prompt-injection stripping, secret redaction. |
| `core/web/` | FastAPI app and the phone PWA (static, no build step). |
| `db/migrations/` | 38 SQL migrations, applied in order. |
| `evals/`, `scripts/eval_report.py` | Offline eval suites that gate CI; live agent evals. |
| `tests/` | About 1,600 pytest tests against real Postgres and Ollama. |
| `capstone/` | Scripts used for this submission: demo seed data, PDF build, video build, the sanitizer. |

## Setup

Requirements: Python 3.12, Docker, an Anthropic API key.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt        # or: pip install -e ".[dev]"
cp .env.example .env                   # put your ANTHROPIC_API_KEY and a POSTGRES_PASSWORD in it
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
docker exec atlas-ollama ollama pull nomic-embed-text
# apply migrations (Linux/macOS/cloud):
bash scripts/cloud_session_start.sh    # or on Windows: scripts\migrate.bat
```

Run it:

```bash
python -m capstone.seed_demo           # optional: made-up demo projects (needs POSTGRES_DB=atlas_capstone)
python -m scripts.run_web --port 8000  # dashboard at http://127.0.0.1:8000
python -m scripts.run_worker           # scheduler: executive loop, briefs, night shift
```

Open the dashboard, go to **Chat** and ask the CEO something like "My capstone is due Dec 4 at 11:59pm. Put it on my plan, then have the academic agent draft a checklist." Delegation raises an approval card on **Today**; hold **Authorize** to let the specialist run.

## Tests and evals

```bash
python -m pytest -q
python -m scripts.eval_report          # offline evals, no API calls
python -m mypy core agents scripts --ignore-missing-imports
```

The test suite mocks the Anthropic client, so it needs no real key. Live agent evals (`python -m scripts.run_evals <suite>`) do spend tokens.

## Safety defaults

- Tools with no policy row are refused. Payments, transfers, publishing and password changes are denied; messages, calendar events and delegation ask first.
- Mail, calendar and GitHub content never reaches a model until `ATLAS_MAIL_MODEL_READS=true` and the privacy rules file both allow it.
- Spending is capped per agent, per day (`ATLAS_DAILY_USD_CAP`, default $8) and per night shift (`ATLAS_NIGHT_USD_CAP`, default $2).

## Models

Configured in `.env`: the CEO uses the Opus tier, specialists the Sonnet tier, and triage, summaries and reflection the Haiku tier. Embeddings run locally through Ollama (`nomic-embed-text`, 768 dimensions).
