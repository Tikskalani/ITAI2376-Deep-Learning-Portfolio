# Tool policy and skill provenance (K2C Stage 1)

This covers what Stage 1 of the capabilities work (K2C, docs and decisions of
2026-10-20) changes, and what it does not. It is the security groundwork that
comes before any new ingestion or retrieval path. It has not been merged or
deployed. Merging and deploying wait for Trilok's approval.

## What changes

### Tools without a policy are refused (`core/runtime/policy.py`)

| Situation | Before | Now |
|---|---|---|
| No `approval_policy` row matches a tool | ran as if `auto` | refused: `unmatched`, audited, `policy.refused` event naming the fix |
| The policy table cannot be read | every tool ran | refused: `unavailable`, with the reason |
| A late approval runs | ran | re-checks the policy first |

"Allowed on purpose" and "nobody classified it" are now different states.

### Every shipped tool has an explicit row (migration 037)

Each gate was chosen from what the handler does, not from the tool's name.

**Counting basis** (`tests/test_tool_policy_coverage.py` pins these):
- 54 unique local tool names, or 56 with the two Anthropic server tools.
- 104 agent-tool registrations: a tool several agents carry counts once per agent.
- The Oct 20 report said 52 and 54. The two since then are `optimize_resume` and `revise_with_answers` from the resume engine (#76). Both stay `auto`, flagged for review: they replace a draft, ask questions in Jobs and make paid calls capped per run and per day.

**Now ask first (`approve`):**
- **Delegation:** `dispatch_agent`, `run_workflow` and `queue_for_tonight`. Delegated work does not yet inherit the caller's project scope, and has no bound on depth, fan-out, retries or spend. When it does, these can move back.
- **`draft_email`:** creates an object in Gmail. Writing draft text inside Atlas needs no tool.
- **`import_statement`:** reads a local file at any path the model names.
- **`calendar_create_event`:** unchanged; it already asked first.

**Flagged for review but kept `auto`, as established behavior:**
- Bulk or overwriting writes: `categorize`, `update_recurring_bill`, `update_task_template`, `save_materials`, `add_task` (rules).
- Writes recorded in your name: `log_decision`, `rate_run`.
- Memory written from untrusted input: `remember_fact`, `record_lesson`.

**Unchanged by design:**
- The hard-rule fences from migration 011 still win where they match; `deny` beats an exact `auto`.
- Every row is inserted `on conflict do nothing`, so a gate changed by hand stays changed (`*submit*` stays "on its own").

### Validated against real use

All 22 tool names called in the last 60 days were matched against these rows with `policy.match_gate`, reading the live database only. None would be refused. The one visible change is `dispatch_agent` (12 calls in 60 days): it now asks first.

### Skills carry provenance and only your own reach a prompt (`core/skills.py`)

`skills.provenance` is one of `user_authored`, `external_internet`, `agent_reflection` or `unknown`:
- It is backfilled from the learning candidate each skill came from. A missing source stays `unknown`; nothing is invented.
- A trigger fixes it once set. Nothing becomes `user_authored` after the fact.

What reaches a system prompt:
- `user_authored` skills.
- `agent_reflection` skills you approved. Approval is a separate `skill_reviews` row and never changes provenance.
- Internet-derived and unknown skills are reference material only, approved or not.

The reflection skills stop being injected until you approve each one. There were seven on Oct 20; on 2026-10-23 all 13 active skills are agent reflections (checked against the live data, read-only). Nothing is deleted.

Procedure files get the same labels (`procedure_provenance`). `load_procedure` returns the label with the text, as reference material.

### Rollback

None of this sits behind a feature flag. Turning the future capabilities feature off cannot bring back unmatched-tool execution or injection of internet-derived skills. Reverting means reverting this change.

## Fetching

- **`fetch_url`** (#75, plus Stage 1):
  - http(s) only, every hop checked, redirects followed by hand.
  - Connects to the address it checked: `_GuardedBackend` resolves, checks, then opens the socket to that IP. TLS still verifies the hostname.
  - Stage 1 adds the NAT64 prefix (`64:ff9b::/96`), checked as the IPv4 address it wraps.
- **`fetch_transcript` and audio transcription:** URLs must be http(s) on a public host (`check_public_url`), and yt-dlp runs with site extractors only (`-generic` off).

## Not covered: open risks

1. **yt-dlp check-to-connect gap.** The URL is checked, then yt-dlp resolves DNS and follows redirects itself. A host that answers the check with a public address and the connect with a private one could still reach a local service. Site-only extractors narrow this; they do not close it. Closing it needs a network boundary yt-dlp cannot route around, for example a proxy that refuses private destinations, or running the download in a container with no route to the host.
2. **Remote MCP tools (Zapier).** Anthropic calls them directly during a run that attaches the server (`mcp_servers` in the request). Atlas's policy never sees those calls. Only the actions enabled at zapier.com/mcp limit them. A run that attaches Zapier can do whatever those actions allow, without an Atlas approval card.
3. **Anthropic server tools** (`web_search`, `web_fetch`) are gated only when attached (`server:<name>` rows), not call by call.
4. **The job submitter** runs from the scheduler, not as an agent tool. Its authorization is the Apply tap in Career plus the pre-submit check, not the tool policy.
5. **The symlink-escape test** for procedures is skipped on the laptop (Windows will not let this account create symlinks). CI on Linux runs it.

Atlas is therefore not globally fail-closed. Agent tools are. The paths above are not, and the capabilities module must not rely on them for any check it needs.

## Compatibility

- **The CEO's delegation now raises an approval card.** This covers "Dispatch CFO on…" from chat, workflows and queueing night tasks. Night tasks you approved in the night shift run as before; queuing new ones through the CEO asks.
- **Gmail drafts ask first.** The `*draft_email*` fence already did this; the exact row makes it explicit.
- **Prompts no longer carry the 13 active skills.** All 13 are agent reflections. Each returns to prompts when you approve it (Playbook, Approve); its provenance stays agent_reflection.
- **A new tool cannot ship without a row.** CI fails until someone picks its gate.
