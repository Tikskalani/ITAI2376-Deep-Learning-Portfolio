# Capabilities (K2C): tutorials become checked, versioned procedures

Trilok's specification of 2026-10-20, built as one narrow end-to-end workflow inside Atlas.

The workflow: share a tutorial (a YouTube video, a reel, an article) and Atlas does the following:
- reads what it says and what it shows;
- follows the resource it points to;
- extracts the actionable claims and checks one or more against a primary source;
- writes a procedure with its evidence.

After you approve the procedure:
- the CTO can retrieve it as reference while working on your projects;
- outcomes are recorded against the exact version used;
- you can install it in Claude Code as a skill.

This proves one workflow. It does not validate the whole product.

## Where it lives

| Part | File |
|---|---|
| Schema | `db/migrations/038_capabilities.sql` |
| Capture (dedupe, the guarded download, pages) | `core/capabilities/capture.py` |
| Timed transcript, frame selection, reading frames | `core/capabilities/extract.py` |
| Resources, claims, checks, the procedure | `core/capabilities/build.py` |
| Stages, states, cache policy | `core/capabilities/pipeline.py` |
| The per-source budget | `core/capabilities/budget.py` |
| The CTO's tools | `core/capabilities/retrieval.py` |
| Install in Claude Code | `core/capabilities/claude_skill.py` |
| Dashboard routes | `core/web/capabilities.py`; the screen is Inbox, Skills to review |
| Phone share sheet | `POST /api/ingest` with `kind: learn` uses this when the feature is on |
| Test project | `k2c-test` in `scripts/seed_projects.py` |

## What leaves the machine

The original video is processed locally:
- yt-dlp downloads it, with site extractors only, from public hosts only, at most 720p and 45 minutes;
- for YouTube, yt-dlp must solve the player's JavaScript challenge or YouTube answers 403 (seen 2026-10-24). Atlas names Deno as the only runtime (`core/transcription.py` `ytdlp_js_opts`), because yt-dlp runs Deno with no file, network or environment permissions. Node would run YouTube's script with full access, so Atlas never enables it. Install Deno once: `winget install DenoLand.Deno`. Without it, the failure says so;
- faster-whisper transcribes the audio;
- PyAV cuts the frames.

Selected frames and transcript text are sent to the configured model provider (Anthropic, Sonnet 5.5 by default, `ATLAS_CAP_TIER`):
- at most `ATLAS_CAP_MAX_FRAMES` frames (8), each JPEG at most 1280 px wide;
- the transcript, description and resolved resource text, to extract claims, check one and write the procedure.

Resource pages and primary sources are fetched through Atlas's guarded fetcher (`core/web`).

Kept on this laptop:
- the source records and evidence text, in Postgres;
- the frames, in `data/capabilities/<source>/`.

The video file is deleted once its frames are cut; its frames and transcript are the evidence.

## Storage and backup

- **Database:** the records are covered by the nightly `pg_dump`.
- **Frames:** these are files. `data/` is gitignored, and gitignored is not backed up. So the nightly backup copies `data/capabilities/` into `backups/capabilities/` (`core/backup.py`, `mirror_capabilities`): new and changed files only, never deleting from the backup.
- **Recovery:**
  - A database restored from a dump finds its frames in `backups/capabilities/` (copy them back to `data/capabilities/`).
  - The review screen says when a frame file is missing, rather than showing a broken image.
- **Same disk:** the backup folder is on the same laptop.
- **Offsite:** when offsite backup is configured (rclone), it carries `backups/capabilities/` too. Its retention prune deletes only database dumps (`atlas-*.dump`), never frames.

## The four dimensions, never one flag

| Dimension | Stored in | Values |
|---|---|---|
| Extraction fidelity: did the source say or show it? | `cap_claims.fidelity`, computed from the stored evidence, never taken from the model | quoted, paraphrased, from_visual, from_resource, not_located |
| External support: does a primary source support it? | `cap_checks` (verdict, URL, verbatim excerpt, date) | supported, contradicted, inconclusive, not_checked |
| Practical test | `cap_versions.test_status` | not_practically_tested (nothing executes downloaded content) |
| Your approval | `cap_reviews`, separate from the content | approved, withdrawn |

**How the claim fields are read:**
- A quote found in the transcript proves only that the claim was extracted.
- A quote that cannot be found does not make a claim false. It may be on screen, in the resource, a paraphrase or an extraction error, and the fidelity label says which.

**How a check is made:**
- At least one consequential claim is checked: against the resolved resource when the claim cites it, otherwise against the official documentation URL the model names. That page is fetched through the guard, and the model never reads the open web on its own.
- A verdict whose excerpt does not appear verbatim in what was read becomes inconclusive.
- Every other claim is marked not_checked.
- A package with any check is labelled **partially checked**. None is labelled verified.

## Versions, approval, withdrawal

- **Immutable versions:** a version's content (`cap_versions.body`) cannot change. A trigger refuses updates. A new reading is a new version.
- **Approval is a record:** approval and withdrawal are rows in `cap_reviews`. The latest decides.
- **Withdrawal:**
  - the agents stop seeing the version;
  - any Claude Code skill installed from it is removed;
  - nothing is deleted.
- **Every use is recorded:** each retrieval writes `cap_uses` (exact version, run, agent).

## The CTO's tools, and what they cannot do

| Tool | Kind | Gate (migration 038) | Rule |
|---|---|---|---|
| `search_capabilities` | read | auto | Approved, current, in scope. Returns "no suitable result" when none fits |
| `get_capability` | read | auto | The same checks as search, so knowing a version ID opens nothing. Records the use |
| `report_capability_outcome` | write | auto | Refused unless this same run retrieved that version with `get_capability` |

**The run's identity:**
- It comes from the runtime (`core/runtime/run_context.py`, set around each tool call and reset after), never from the model's input.
- Concurrent runs each see their own identity.

**What a procedure is to an agent:**
- It is returned as a tool result inside an envelope that says it is reference data from an external tutorial.
- Its text is cleaned: invisible characters and lines aimed at AI are removed (`core/jobs/untrusted.py`).
- It is never placed in a system prompt.

The envelope and the cleanup are boundaries, not a guarantee. Policy (`core/runtime/policy.py`) decides what any tool may do, whatever a package says, and approving a package grants no tool or permission.

## Scope and privacy

- **Public material only:** procedures are built only from the public source that was shared, never from your tasks or projects, so approved packages carry no private detail and are readable in any run.
- **Project packages:** a package can carry a `project_slug`, and is then visible only in that project's runs. The project comes from the run, not the model.
- **Outcome notes:** a run's outcome note, which could mention private work, is stored against that run's use and is never returned by search or get.
- **Known limit:** runs do not yet carry a project, so outcomes are not yet grouped by project. The slice uses `k2c-test` and public tutorials.

## Cache, duplicates, failures

| Situation | What happens | Charged? |
|---|---|---|
| The same link (any URL form of the same video) while done or processing | Reused | No |
| Failed, or held for budget | Stays as it is until you tap **Try again** (a retry attempt) | Yes, only then |
| Done, and you ask to read it again | A refresh attempt (`refresh: true`, or Try again on a done source) | Yes, only then |
| Changed source | A changed video is the same link, so refresh it explicitly | Yes, only then |

**Visible states:** every attempt is its own `cap_attempts` row with its reason, status, stage, error and cost. The source shows queued, processing (with its stage), done, failed or held_budget.

## Budget

- **Caps:** `ATLAS_CAP_SOURCE_USD` per source ($0.50, a pilot target, not a proven cost), inside `ATLAS_DAILY_USD_CAP`.
- **Reserve before every call:** each model call reserves its worst case first: input at the input rate, with images at (width x height) / 750 tokens, plus `max_tokens` at the output rate.
- **A stage that will not fit:** if it does not fit what the source or the day has left, the source is **held_budget**, with the stage that was held. Atlas never raises a cap or skips a stage and calls the source done.
- **Limits of the guarantee:**
  - The provider bills after a call completes, so the ceiling holds for each call's worst case as priced in `core/costs.py`.
  - A price change at the provider is outside it until that table is updated.
- **Estimated cost of one 10-minute tutorial with Sonnet 5.5** ($2 / $10 per million tokens):

  | Stage | Estimate |
  |---|---|
  | 8 frames (about 10,000 image tokens plus text) | about $0.03 to $0.06 |
  | Claims (about 8,000 tokens in, 2,000 out) | about $0.04 |
  | One or two checks | about $0.02 to $0.05 |
  | Procedure (about 10,000 in, 3,000 out) | about $0.05 |
  | **Total** | **about $0.15 to $0.25** |

  This is an estimate, not a measurement. The first live run records the real figure per stage (`cap_attempts.cost_usd`, `llm_calls`).

## Feature flag and rollback

- **`ATLAS_CAPABILITIES=false`:**
  - every capabilities route answers 404;
  - the CTO loses the three tools;
  - the phone share sheet goes back to the old one-sentence learning path.
- **What the flag does not touch:** the retirement of internet-derived skill injection (migration 037, `core/skills.py`) does not read this flag, so turning capabilities off cannot bring that injection back.

## Tests and what they do not prove

**What the tests establish:** `tests/test_capabilities.py` uses fixtures (a fake model, no network, no download) to establish orchestration, validation, storage, permissions, scope, the cache policy, budget holds, failures, the flag, the Claude Code install and removal, and the backup mirror.

**What they do not prove:**
- real extraction accuracy;
- real model resistance to malicious content;
- that a step citing an evidence ID is actually supported by that evidence.

**What proves those:** the first live run on a tutorial you choose, plus your review of its evidence.

**What is not covered:**
- **yt-dlp check-to-connect gap:** the download URL is checked, then yt-dlp does its own DNS lookups and redirects (open since S1, `docs/tool-policy.md`).
- **The other open paths:** Zapier and other remote MCP tools, Anthropic server tools and the job submitter are outside tool-policy enforcement. None of them is used by this module, and none can loosen it.
