"""Atlas runtime configuration.

Single source of truth for env-derived settings. Every module imports `settings`
from here rather than reading os.environ directly.
"""

from __future__ import annotations

from datetime import time as dtime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- Anthropic ------------------------------------------------------------
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    model_ceo: str = Field("claude-opus-5-5", alias="ATLAS_MODEL_CEO")
    model_specialist: str = Field("claude-sonnet-5-5", alias="ATLAS_MODEL_SPECIALIST")
    model_scan: str = Field("claude-haiku-5-5", alias="ATLAS_MODEL_SCAN")
    # Reasoning depth per tier (low, medium, high, xhigh, max). Opus 5.5
    # defaults to medium on the API, so the CEO's is set explicitly.
    effort_ceo: str = Field("high", alias="ATLAS_EFFORT_CEO")
    effort_specialist: str = Field("medium", alias="ATLAS_EFFORT_SPECIALIST")
    # Live chat with the CEO. Opus 5.5 at medium answers everyday questions
    # as well as high for about half the thinking tokens; scheduled briefs
    # and dispatched work keep ATLAS_EFFORT_CEO.
    effort_chat: str = Field("medium", alias="ATLAS_EFFORT_CHAT")
    # Scan-tier one-shot calls (triage, vetting, summaries) on Haiku 5.5.
    effort_scan: str = Field("low", alias="ATLAS_EFFORT_SCAN")
    # Server-side refusal fallbacks: a policy decline is re-run on
    # Anthropic's recommended substitute inside the same call.
    refusal_fallbacks: bool = Field(True, alias="ATLAS_REFUSAL_FALLBACKS")
    # Anthropic-hosted web search and web fetch for the agents that opt in
    # (news_discovery, opportunity_scout, the CEO). $10 per 1,000 searches.
    web_tools: bool = Field(True, alias="ATLAS_WEB_TOOLS")
    web_search_max_uses: int = Field(5, alias="ATLAS_WEB_SEARCH_MAX_USES")
    # Remote MCP servers Claude may call directly, as a JSON list:
    # [{"name": "notion", "url": "https://...", "token": "...",
    #   "agents": ["ceo"], "allowed_tools": ["search"]}]
    # These run on Anthropic's side, so the approval policy cannot gate
    # them: list read-only tools only.
    mcp_servers_json: str = Field("", alias="ATLAS_MCP_SERVERS")

    # -- Discovery / transcription -------------------------------------------
    whisper_model: str = Field("base", alias="ATLAS_WHISPER_MODEL")
    ytdlp_cookies_from_browser: str | None = Field(
        None, alias="ATLAS_YTDLP_COOKIES_FROM_BROWSER"
    )
    # Runs a trial skill must be used in before the Sunday review judges it.
    skill_trial_runs: int = Field(10, alias="ATLAS_SKILL_TRIAL_RUNS")

    # -- Postgres -------------------------------------------------------------
    # 127.0.0.1, not localhost: on this laptop "localhost" can resolve to ::1,
    # where a WSL relay also listens on 5434 and resets every connection
    # (2026-10-22: AtlasWeb could not start until the host was IPv4).
    pg_host: str = Field("127.0.0.1", alias="POSTGRES_HOST")
    pg_port: int = Field(5434, alias="POSTGRES_PORT")
    pg_db: str = Field("atlas", alias="POSTGRES_DB")
    pg_user: str = Field("atlas", alias="POSTGRES_USER")
    pg_password: str = Field(..., alias="POSTGRES_PASSWORD")

    @property
    def pg_dsn(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    # -- Ollama ---------------------------------------------------------------
    # 127.0.0.1 for the same reason as pg_host: a WSL relay answers ::1:11435.
    ollama_url: str = Field("http://127.0.0.1:11435", alias="OLLAMA_URL")
    ollama_embed_model: str = Field("nomic-embed-text", alias="OLLAMA_EMBED_MODEL")
    ollama_embed_dim: int = Field(768, alias="OLLAMA_EMBED_DIM")

    # -- Notifications (R1) ---------------------------------------------------
    # Web push to the installed PWA. Generate a pair with
    # `python -m scripts.vapid_keys` and paste both; the public key is what
    # the browser receives as applicationServerKey. Blank disables push.
    vapid_public_key: str = Field("", alias="ATLAS_VAPID_PUBLIC_KEY")
    vapid_private_key: str = Field("", alias="ATLAS_VAPID_PRIVATE_KEY")
    vapid_subject: str = Field("", alias="ATLAS_VAPID_SUBJECT")  # mailto:you@example.com

    # Email fallback: used when no push lands, for high-priority items, and
    # for anything still unread an hour later. Blank host disables it.
    smtp_host: str = Field("", alias="ATLAS_SMTP_HOST")
    smtp_port: int = Field(587, alias="ATLAS_SMTP_PORT")
    smtp_user: str = Field("", alias="ATLAS_SMTP_USER")
    smtp_password: str = Field("", alias="ATLAS_SMTP_PASSWORD")
    smtp_from: str = Field("", alias="ATLAS_SMTP_FROM")
    smtp_starttls: bool = Field(True, alias="ATLAS_SMTP_STARTTLS")
    notify_email: str = Field("", alias="ATLAS_NOTIFY_EMAIL")

    # -- Runtime --------------------------------------------------------------
    env: Literal["local", "vps"] = Field("local", alias="ATLAS_ENV")
    log_level: str = Field("INFO", alias="ATLAS_LOG_LEVEL")
    timezone: str = Field("America/Chicago", alias="ATLAS_TIMEZONE")

    loop_interval_min: int = Field(30, alias="ATLAS_LOOP_INTERVAL_MIN")
    # Each half of a loop tick (due triggers, senses) is cut off after this,
    # so one hung call cannot stall every later tick.
    loop_stage_timeout_min: float = Field(10.0, alias="ATLAS_LOOP_STAGE_TIMEOUT_MIN")
    # The web process watches the worker's heartbeat and pages on a stall.
    web_watchdog: bool = Field(True, alias="ATLAS_WEB_WATCHDOG")
    # The worker watches the web app's /api/health and restarts the AtlasWeb
    # service after three misses in a row (core/loop/webwatch.py).
    worker_watches_web: bool = Field(True, alias="ATLAS_WORKER_WATCHES_WEB")
    web_url: str = Field("http://127.0.0.1:8000", alias="ATLAS_WEB_URL")
    # Writes under /api/ need the dashboard's X-Atlas-Client header and a
    # same-host Origin (core/web/guard.py). Off only as an escape hatch.
    write_guard: bool = Field(True, alias="ATLAS_WRITE_GUARD")
    # Extra hosts allowed as an Origin for writes, comma separated, when the
    # dashboard is served under a name the request's Host does not carry.
    allowed_origins: str = Field("", alias="ATLAS_ALLOWED_ORIGINS")
    brief_morning: dtime = Field(dtime(8, 0), alias="ATLAS_BRIEF_MORNING")
    brief_evening: dtime = Field(dtime(20, 0), alias="ATLAS_BRIEF_EVENING")
    # Send a short templated brief, with no model call, when nothing changed
    # since the last one (core/loop/briefs.py). On since 2026-10-22 (Trilok read
    # the template); scripts/quiet_brief_audit.py checks a week of them.
    brief_skip_unchanged: bool = Field(True, alias="ATLAS_BRIEF_SKIP_UNCHANGED")

    approval_ttl_hours: int = Field(24, alias="ATLAS_APPROVAL_TTL_HOURS")
    # How long a running agent blocks on an approval card before carrying
    # on with "pending". The card stays open until the TTL.
    approval_wait_minutes: int = Field(10, alias="ATLAS_APPROVAL_WAIT_MIN")
    daily_usd_cap: float = Field(8.0, alias="ATLAS_DAILY_USD_CAP")
    # The most one claude.ai export import may spend on the scan model
    # before it pauses for him to raise the cap (core/kb.py).
    kb_usd_cap: float = Field(10.0, alias="ATLAS_KB_USD_CAP")
    # The resume engine (core/resume_engine/bridge.py): the most one run may
    # spend, and the most all its runs may spend in a UTC day. Both sit inside
    # ATLAS_DAILY_USD_CAP. Every paid call reserves its worst case first.
    resume_run_usd_cap: float = Field(0.40, alias="ATLAS_RESUME_RUN_USD_CAP")
    resume_daily_usd_cap: float = Field(1.00, alias="ATLAS_RESUME_DAILY_USD_CAP")
    # Safe-start for mail, calendar and GitHub (core/privacy.py). While false, no
    # model reads their content: signals keep sender and subject only and are
    # scored by rules. Model reads need this AND mail_model_reads: true in
    # data/model_exclude.yaml. Canvas is not affected.
    mail_model_reads: bool = Field(False, alias="ATLAS_MAIL_MODEL_READS")

    # Capabilities (K2C, core/capabilities/): a shared video, article or page
    # becomes a source-checked, versioned procedure. Off hides the feature; it
    # never brings back prompt injection of internet-derived skills (that
    # retirement, migration 037, does not read this flag).
    capabilities: bool = Field(True, alias="ATLAS_CAPABILITIES")
    # The most one source may spend across its stages (a pilot target, not a
    # proven cost), inside ATLAS_DAILY_USD_CAP. A stage that would not fit is
    # held (held_budget), never skipped.
    cap_source_usd: float = Field(0.50, alias="ATLAS_CAP_SOURCE_USD")
    cap_tier: str = Field("specialist", alias="ATLAS_CAP_TIER")  # Sonnet 5.5; Haiku is evaluated later
    cap_max_frames: int = Field(8, alias="ATLAS_CAP_MAX_FRAMES")
    capabilities_dir: Path = Field(REPO_ROOT / "data" / "capabilities", alias="ATLAS_CAPABILITIES_DIR")
    # Where "Install in Claude Code" writes an approved procedure as a skill.
    claude_skills_dir: Path = Field(Path.home() / ".claude" / "skills", alias="ATLAS_CLAUDE_SKILLS_DIR")

    # Optional healthchecks.io ping URL. The executive loop GETs it on every
    # successful tick so an external service can page the owner when Atlas
    # goes quiet. Blank disables the ping.
    heartbeat_url: str = Field("", alias="ATLAS_HEARTBEAT_URL")

    procedures_dir: Path = Field(REPO_ROOT / "procedures", alias="ATLAS_PROCEDURES_DIR")
    # Procedures Atlas writes itself (adopted learnings). Gitignored, so the
    # tracked procedures_dir only holds hand-curated playbooks.
    runtime_procedures_dir: Path = Field(
        REPO_ROOT / "data" / "procedures", alias="ATLAS_RUNTIME_PROCEDURES_DIR"
    )
    log_dir: Path = Field(REPO_ROOT / "logs", alias="ATLAS_LOG_DIR")
    backup_dir: Path = Field(REPO_ROOT / "backups", alias="ATLAS_BACKUP_DIR")
    backup_retention_days: int = Field(14, alias="ATLAS_BACKUP_RETENTION_DAYS")
    backup_hour: int = Field(3, alias="ATLAS_BACKUP_HOUR")

    # Optional Google Calendar private ICS URL. When set, the COO's
    # upcoming_events tool reads the calendar through core.connectors.
    # The URL is secret and lives only in .env, never in source.
    calendar_ics_url: str | None = Field(None, alias="ATLAS_CALENDAR_ICS_URL")

    # -- Offsite backup -------------------------------------------------------
    # Default is "none": Atlas writes the daily pg_dump to backup_dir and
    # nothing else. Set to "rclone" once rclone is installed and configured
    # with `rclone config` so backups also leave this machine each night.
    offsite_backend: Literal["none", "rclone"] = Field(
        "none", alias="ATLAS_OFFSITE_BACKEND"
    )
    offsite_remote: str = Field("", alias="ATLAS_OFFSITE_REMOTE")
    offsite_retention_days: int = Field(
        30, alias="ATLAS_OFFSITE_RETENTION_DAYS"
    )
    offsite_rclone_path: str = Field(
        "rclone", alias="ATLAS_OFFSITE_RCLONE_PATH"
    )

    # Bearer token the iOS Shortcuts send to POST /api/ingest. A long random
    # string; blank disables the route. Cloudflare Access covers the rest.
    ingest_token: str = Field("", alias="ATLAS_INGEST_TOKEN")
    # Bearer token for the MCP server at /mcp (Claude sessions as a remote
    # connector). Blank disables it.
    mcp_token: str = Field("", alias="ATLAS_MCP_TOKEN")

    # -- Senses (R2) ----------------------------------------------------------
    # GitHub: a fine-grained token with read access to contents, issues and
    # pull requests on the client repos. Repos come from ATLAS_GITHUB_REPOS
    # (comma-separated owner/name) plus every active project's repos column.
    # Blank token disables the connector.
    github_token: str = Field("", alias="ATLAS_GITHUB_TOKEN")
    github_repos: str = Field("", alias="ATLAS_GITHUB_REPOS")

    # Canvas (HCC Eagle Online): base URL and a personal access token from
    # Account > Settings > New Access Token. Blank token disables it.
    canvas_url: str = Field("", alias="ATLAS_CANVAS_URL")
    canvas_token: str = Field("", alias="ATLAS_CANVAS_TOKEN")

    # Google (Gmail and Calendar connectors): an OAuth "Desktop app" client
    # and the refresh token printed by `python -m scripts.google_auth`.
    # Blank refresh token disables both connectors.
    google_client_id: str = Field("", alias="ATLAS_GOOGLE_CLIENT_ID")
    google_client_secret: str = Field("", alias="ATLAS_GOOGLE_CLIENT_SECRET")
    google_refresh_token: str = Field("", alias="ATLAS_GOOGLE_REFRESH_TOKEN")
    google_calendar_id: str = Field("primary", alias="ATLAS_GOOGLE_CALENDAR_ID")

    # How many urgency-3 interrupts the loop may push per day outside the
    # briefs. Silence is the default; this is the ceiling.
    interrupt_budget: int = Field(2, alias="ATLAS_INTERRUPT_BUDGET")

    # The night shift (core/night.py): propose in the evening, run late,
    # report in the morning, all local time; the cap is per night and sits
    # under the daily cap (the vision's bar is "under $2 a night").
    night_propose_hour: int = Field(22, alias="ATLAS_NIGHT_PROPOSE_HOUR")
    night_propose_minute: int = Field(30, alias="ATLAS_NIGHT_PROPOSE_MINUTE")
    night_run_hour: int = Field(23, alias="ATLAS_NIGHT_RUN_HOUR")
    night_run_minute: int = Field(30, alias="ATLAS_NIGHT_RUN_MINUTE")
    night_report_hour: int = Field(7, alias="ATLAS_NIGHT_REPORT_HOUR")
    night_report_minute: int = Field(30, alias="ATLAS_NIGHT_REPORT_MINUTE")
    night_usd_cap: float = Field(2.0, alias="ATLAS_NIGHT_USD_CAP")
    # Signals per Haiku triage call.
    triage_batch: int = Field(20, alias="ATLAS_TRIAGE_BATCH")

    def model_for_tier(self, tier: str) -> str:
        """The Claude model for a tier (ceo, specialist, scan), read now:
        every caller resolves through here instead of a copy taken at
        import, so one setting change (or a test pin) reaches all of them."""
        tiers = {"ceo": self.model_ceo, "specialist": self.model_specialist, "scan": self.model_scan}
        if tier not in tiers:
            raise ValueError(f"unknown model tier {tier!r}; expected one of {sorted(tiers)}")
        return tiers[tier]

    @property
    def github_repo_list(self) -> list[str]:
        return [r.strip().lower() for r in self.github_repos.split(",") if r.strip()]

    # -- LLM provider ---------------------------------------------------------
    # "anthropic" is the only fully-wired provider today. "openai" is a
    # stub kept for future insurance; flipping to it without wiring the
    # real call will produce empty completions and a log error.
    llm_provider: str = Field("anthropic", alias="ATLAS_LLM_PROVIDER")

    @field_validator("brief_morning", "brief_evening", mode="before")
    @classmethod
    def _parse_hhmm(cls, v):
        if isinstance(v, str) and ":" in v:
            hh, mm = v.split(":", 1)
            return dtime(int(hh), int(mm))
        return v

    @field_validator(
        "procedures_dir", "runtime_procedures_dir", "log_dir", "backup_dir", "capabilities_dir", mode="before"
    )
    @classmethod
    def _resolve_path(cls, v):
        p = Path(v)
        if not p.is_absolute():
            p = REPO_ROOT / p
        return p


settings = Settings()  # type: ignore[call-arg]


def reload(path: Path | None = None) -> None:
    """Re-read .env into the live singleton.

    The Connect tab writes tokens into .env from the dashboard; the web
    process calls this right after, and the worker calls it when the file's
    mtime changes (core/loop/envwatch.py), so a connection made in the
    browser is in force everywhere without a restart. Every module holds
    the same `settings` object, so the fields are replaced in place."""
    fresh = Settings(_env_file=path or REPO_ROOT / ".env")  # type: ignore[call-arg]
    for name in Settings.model_fields:
        object.__setattr__(settings, name, getattr(fresh, name))
