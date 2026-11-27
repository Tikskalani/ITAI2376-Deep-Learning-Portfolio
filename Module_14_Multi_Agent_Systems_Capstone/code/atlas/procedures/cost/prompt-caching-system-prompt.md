---
name: prompt-caching-system-prompt
domain: cost
version: 1
description: Prompt Caching: Cache Stable System Prompt Prefixes
---

# Prompt Caching: Cache Stable System Prompt Prefixes

## Purpose

Prevent Atlas from re-billing the full system prompt token count on every API call by marking the stable prefix of each agent's system prompt with Anthropic's `cache_control` breakpoint. Cache reads cost 10% of base input token price; uncached reads cost 100%. On any agent that fires more than a handful of times per day, the savings compound quickly.

## Inputs

- The system prompt text for the agent being updated
- Knowledge of which portions of that prompt are stable across calls vs. which vary per-request (e.g., injected context, timestamps, dynamic tool lists)
- The model the agent uses (minimum cacheable length differs by model; see Constraints below)

## Steps

### 1. Identify the stable prefix

Read the agent's full system prompt. Split it mentally into two regions:

- **Stable prefix** — everything that is identical on every single call: role definition, standing instructions, procedure references, behavioral rules, static tool descriptions.
- **Variable suffix** — anything that changes per call: injected memory snippets, current date/time strings, per-task context, dynamic tool lists.

The cache breakpoint must sit at the boundary between these two regions, on the **last block that is identical across all calls**. Placing it on a block that changes even slightly every call means the prefix hash never matches and you write a new cache entry on every request without ever getting a read.

### 2. Restructure the system prompt into content blocks

Anthropics's caching API requires the system field to be an array of content blocks rather than a plain string. Convert the system prompt from a single string to an array. Example shape:

```
system: [
 {
 "type": "text",
 "text": "<everything in the stable prefix>",
 "cache_control": {"type": "ephemeral"}
 },
 {
 "type": "text",
 "text": "<variable suffix, if any, with no cache_control>"
 }
]
```

If the entire system prompt is stable (no per-call variation), a single block with `cache_control` is sufficient.

### 3. Verify minimum token threshold

Check the token count of the stable prefix block against the model's minimum cacheable length:

- Claude Sonnet 4.5 / 4.6, Opus 4.8, Opus 4.1, Opus 4 (deprecated), Sonnet 4 (deprecated): **1,024 tokens minimum**
- Claude Opus 4.5 / 4.6 / 4.7, Haiku 3.5: **2,048–4,096 tokens minimum** (see Anthropic docs for exact per-model value)
- Claude Haiku 4.5: **4,096 tokens minimum**

If the stable prefix falls short, consider expanding it: add more standing instructions, embed a relevant procedure excerpt, or include static few-shot examples until the threshold is met. Cache reads at 10% cost make reaching the minimum worthwhile. If the prefix genuinely cannot reach the threshold, skip caching for that agent; no error is returned but no savings accrue either.

### 4. Do not use automatic top-level caching for prompts with a variable suffix

Anthropic's top-level `cache_control` (automatic caching) places the breakpoint on the **last cacheable block**, which for a prompt with a variable suffix is the block that changes on every call. This produces cache writes with no reads — the same failure mode as putting `cache_control` on the variable block explicitly. Use explicit block-level breakpoints (Step 2) whenever the system prompt has any per-call variation.

Automatic caching is appropriate only for pure multi-turn conversations where the growing message history is the only thing that changes and the system prompt is a single static block.

### 5. Keep tool definitions stable or break them out separately

Tool definitions are part of the cache prefix hierarchy and sit **before** the system prompt. If tool definitions change between calls, the system prompt cache is also invalidated. Options:

- Lock the tool list to a stable set per agent and never mutate it mid-session.
- If some tools are dynamic, separate them: put static tools first (with their own `cache_control` breakpoint) and dynamic tools last (no breakpoint).

Up to 4 explicit cache breakpoints are allowed per request. Use them to cache tools + system at different frequencies if needed.

### 6. Log and monitor cache performance

After deploying, inspect the `usage` fields in every API response:

- `cache_creation_input_tokens` — tokens written to cache this call (new write, costs 125% of base)
- `cache_read_input_tokens` — tokens read from cache (costs 10% of base)
- `input_tokens` — uncached tokens after the last breakpoint

On the first call after a cold start, expect a cache write. On subsequent calls within 5 minutes, expect a cache read with `cache_read_input_tokens` equal to the stable prefix size. If `cache_read_input_tokens` is consistently 0 and `cache_creation_input_tokens` is consistently non-zero, the breakpoint is on a block that is changing; revisit Step 1.

Store the per-call token breakdown in Atlas cost tracking so the savings are visible over time.

### 7. Consider 1-hour TTL for agents with low call frequency

The default cache TTL is 5 minutes, refreshed for free on each hit. If an agent fires less than once every 5 minutes (e.g., a batch job, a nightly summarizer), the cache will expire between calls and every call becomes a write. In this case, opt into the 1-hour TTL by changing the cache_control value:

```
"cache_control": {"type": "ephemeral", "ttl": "1h"}
```

The 1-hour TTL costs 2x base input token price per write (vs. 1.25x for 5-minute TTL) but is cheaper than paying full price on every call if the agent fires even twice per hour.

## Outputs

- Updated agent system prompt serialized as a content-block array with `cache_control` on the stable prefix block
- Confirmation via response usage fields that cache reads are occurring
- Optional: a note in Atlas cost tracking recording the per-agent token savings baseline

## Constraints and edge cases

- Cache entries are workspace-scoped (as of 2026-02-05). If Atlas uses multiple Anthropic workspaces, prompts do not share a cache across them.
- Thinking blocks (extended thinking / Claude's internal reasoning) cannot be directly marked with `cache_control` but are cached implicitly alongside other content in multi-turn tool-use flows. No action needed for thinking blocks specifically.
- Concurrent parallel requests: the cache entry is not available until the first response begins. If Atlas fires parallel calls to the same agent, only the second and later calls (after the first response starts) will hit the cache.
- Do not embed the current timestamp, a per-task UUID, or any other per-call value inside the stable prefix block. Even one character difference resets the prefix hash.
- JSON key ordering: if the codebase serializes tool definitions through a language that randomizes map key order (Go, Swift), pin key ordering explicitly before comparing against cache. Random ordering breaks the prefix hash match.

(Adapted from a learning Atlas ingested: https://youtu.be/demo1)
