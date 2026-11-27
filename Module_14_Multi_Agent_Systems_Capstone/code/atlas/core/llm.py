"""Atlas -- provider-agnostic LLM wrapper.

The runtime calls Anthropic directly today. This module is the seam:
if Claude pricing doubles or the API changes, the rest of Atlas does
not need to change, only the provider implementation behind this API.

What's here:
  * complete(messages, model_tier, ...) -- one-shot completion that
    returns text. Works for the entity extractor and any other small
    helper that does not need tool use.
  * The Messages API surface (with tools) stays in core/runtime/agent.py
    where the cache-control, tool-use loop, and episode plumbing live.
    Wrapping that is a bigger refactor than this insurance file needs
    to be.

Provider switching is a config flag + a new function. Today there is
one provider: anthropic. Adding OpenAI is a ~50 line addition; the
function signature is named-arg friendly so callers do not need to
change.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

# on_usage(model, token counts, cost in USD): see complete().
UsageHook = Callable[[str, dict[str, int], Decimal], None]


# Default tier resolutions per provider. Anthropic-only today; openai
# stubs are placeholders so the swap is a config-level change later.
_TIER_TO_MODEL = {
    # Anthropic tiers resolve from settings at call time (resolve_model).
    "openai": {
        "ceo": "gpt-5-opus-equivalent",  # placeholder
        "specialist": "gpt-5-equivalent",  # placeholder
        "scan": "gpt-5-haiku-equivalent",  # placeholder
    },
}


def current_provider() -> str:
    """Return the configured provider. Defaults to anthropic."""
    return (getattr(settings, "llm_provider", None) or "anthropic").lower()


def resolve_model(tier: str, *, provider: str | None = None) -> str:
    """Map a tier (ceo/specialist/scan) to a concrete model string."""
    p = (provider or current_provider()).lower()
    if p == "anthropic":
        if tier not in ("ceo", "specialist", "scan"):
            raise ValueError(f"unknown tier {tier!r}; expected ceo, specialist, or scan")
        return settings.model_for_tier(tier)
    table = _TIER_TO_MODEL.get(p)
    if table is None:
        raise ValueError(f"unknown LLM provider {p!r}")
    if tier not in table:
        raise ValueError(
            f"unknown tier {tier!r}; expected ceo, specialist, or scan"
        )
    return table[tier]


async def complete(
    *,
    system: str,
    user: str,
    tier: str = "scan",
    max_tokens: int = 1024,
    temperature: float = 0.0,
    provider: str | None = None,
    purpose: str = "complete",
    cache_system: bool = False,
    schema: dict[str, Any] | None = None,
    effort: str | None = None,
    on_usage: UsageHook | None = None,
    cache_prefix: str | None = None,
    images: list[tuple[str, bytes]] | None = None,
) -> str:
    """One-shot completion. Returns the model's text. Never raises;
    failures return an empty string and log.

    purpose names the caller ("triage", "discovery_vet", ...) and is
    stored with the call's token usage in llm_calls, so one-shot calls
    count against the daily cap next to agent runs. cache_system marks
    the system prompt for prompt caching; use it when the same long
    rubric is sent many times a day (triage). schema is a JSON schema:
    when given, the reply is constrained to valid JSON of that shape
    (structured outputs), so callers parse instead of regex-hunting.
    effort (low to max) sets reasoning depth on the models that take it.
    temperature is honored on Haiku only; the 5.5 models reject it.
    on_usage(model, usage, cost_usd) is called once the model answered
    (refusals included), after the call is booked; no call means no
    answer came back, which is how a caller tells a failure from an
    empty reply. cache_prefix: when the user text starts with it, that
    part goes as its own block marked for prompt caching (5 minutes), so
    calls sharing it pay the read rate after the first. images:
    (media type, bytes) pairs sent ahead of the text, for reading video
    frames (core/capabilities/extract.py)."""
    p = (provider or current_provider()).lower()
    if p == "anthropic":
        return await _anthropic_complete(
            system=system,
            user=user,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            purpose=purpose,
            cache_system=cache_system,
            schema=schema,
            effort=effort,
            on_usage=on_usage,
            cache_prefix=cache_prefix,
            images=images,
        )
    if p == "openai":
        return await _openai_complete_stub(
            system=system,
            user=user,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    log.error("unsupported provider %r", p)
    return ""


def _older_haiku(model: str) -> bool:
    """Haiku 4.5 and earlier: sampling yes, effort no. Haiku 5.5 is the
    other way round, like the Opus and Sonnet 5.5 generation."""
    m = (model or "").lower()
    return "haiku" in m and "haiku-5" not in m


def takes_sampling(model: str) -> bool:
    """Whether the model still accepts temperature (Haiku 4.5 does; the
    5.5 generation, Haiku included, returns 400 on it)."""
    return _older_haiku(model)


def takes_effort(model: str) -> bool:
    """Whether the model accepts output_config.effort (Haiku 4.5 does not)."""
    return not _older_haiku(model)


def request_kwargs(
    *,
    model: str,
    max_tokens: int,
    system: Any,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    effort: str | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The Messages API keyword arguments for one call, shaped for the
    model: sampling only where it is accepted, effort only where it is,
    structured output when a schema is given."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if temperature is not None and takes_sampling(model):
        kwargs["temperature"] = temperature
    output_config: dict[str, Any] = {}
    if effort and takes_effort(model):
        output_config["effort"] = effort
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        kwargs["output_config"] = output_config
    return kwargs


async def _anthropic_complete(
    *,
    system: str,
    user: str,
    tier: str,
    max_tokens: int,
    temperature: float,
    purpose: str,
    cache_system: bool = False,
    schema: dict[str, Any] | None = None,
    effort: str | None = None,
    on_usage: UsageHook | None = None,
    cache_prefix: str | None = None,
    images: list[tuple[str, bytes]] | None = None,
) -> str:
    """Anthropic Messages API call. Reuses the shared client from agent.py."""
    model = resolve_model(tier, provider="anthropic")
    if effort is None and tier == "scan":
        # Triage, vetting and summaries: low effort keeps Haiku's thinking short.
        effort = settings.effort_scan
    system_param: Any = system
    if cache_system:
        system_param = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    content: Any = user
    if cache_prefix and user.startswith(cache_prefix) and len(cache_prefix) < len(user):
        content = [
            {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user[len(cache_prefix):]},
        ]
    if images:
        import base64

        blocks = [{"type": "image", "source": {"type": "base64", "media_type": media,
                                               "data": base64.b64encode(data).decode("ascii")}}
                  for media, data in images]
        content = blocks + (content if isinstance(content, list) else [{"type": "text", "text": user}])
    try:
        from core.runtime.agent import get_client

        client = get_client()
        resp = await client.messages.create(**request_kwargs(
            model=model, max_tokens=max_tokens, system=system_param,
            messages=[{"role": "user", "content": content}],
            temperature=temperature, effort=effort, schema=schema,
        ))
        if getattr(resp, "stop_reason", None) == "refusal":
            log.warning("anthropic complete declined (%s): %s", purpose,
                        getattr(getattr(resp, "stop_details", None), "category", None))
            await _record_usage(purpose, model, tier, getattr(resp, "usage", None), on_usage)
            return ""
        parts = [
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ]
    except Exception:
        log.exception("anthropic complete failed")
        return ""
    await _record_usage(purpose, model, tier, getattr(resp, "usage", None), on_usage)
    return "".join(parts)


async def complete_json(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    tier: str = "scan",
    max_tokens: int = 1024,
    purpose: str = "complete",
    cache_system: bool = False,
    effort: str | None = None,
) -> Any:
    """complete() with structured output, parsed. Returns the decoded
    JSON (a dict or list per the schema) or None when the call failed
    or the reply was not valid JSON. Never raises."""
    text = await complete(
        system=system, user=user, tier=tier, max_tokens=max_tokens, purpose=purpose,
        cache_system=cache_system, schema=schema, effort=effort,
    )
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        log.warning("complete_json(%s): reply was not valid JSON (%d chars)", purpose, len(text))
        return None


def describe_api_error(exc: BaseException, model: str | None = None) -> str:
    """One plain sentence saying why a Claude call failed, safe to show Trilok.

    Maps the SDK's typed errors to the fix; the API's own message is kept
    (trimmed) because it names the cause, e.g. a low credit balance. API
    error bodies never echo the key."""
    import anthropic

    detail = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
    if not detail:
        detail = str(getattr(exc, "message", "") or exc)
    detail = " ".join(detail.split())[:240]
    which = f" {model}" if model else ""
    if isinstance(exc, anthropic.AuthenticationError):
        return ("Anthropic rejected the API key (401). Put a valid ANTHROPIC_API_KEY in .env, "
                "then run scripts\\deploy.bat.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"This API key is not allowed to use{which} (403): {detail}"
    if isinstance(exc, anthropic.NotFoundError):
        return f"Anthropic does not know the model{which} (404): {detail}"
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic is rate limiting this key (429). Wait a minute and try again."
    if isinstance(exc, anthropic.BadRequestError):
        return f"Anthropic rejected the request (400): {detail}"
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return "Could not reach api.anthropic.com. Check the laptop's internet connection and firewall."
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f"Anthropic had a server error ({exc.status_code}). Try again shortly."
        return f"Anthropic returned {exc.status_code}: {detail}"
    return f"{type(exc).__name__}: {detail}"


def is_transient(exc: BaseException) -> bool:
    """True when the same call could succeed a few minutes later: no
    network, a timeout, rate limiting, or an Anthropic server error
    (overloaded included). A bad key, a 400 such as an empty credit
    balance, or Atlas's own daily cap are not: retrying only repeats them."""
    import anthropic
    import httpx

    if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError | anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return isinstance(exc, httpx.TransportError | ConnectionError | TimeoutError)


def usage_dict(usage: Any) -> dict[str, int]:
    """The four token counts from an SDK usage object (or a dict)."""
    out: dict[str, int] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "web_search_requests",
    ):
        val = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if val:
            out[key] = int(val)
    # Server tools report their own counter under usage.server_tool_use.
    stu = usage.get("server_tool_use") if isinstance(usage, dict) else getattr(usage, "server_tool_use", None)
    if stu is not None:
        n = stu.get("web_search_requests") if isinstance(stu, dict) else getattr(stu, "web_search_requests", None)
        if n:
            out["web_search_requests"] = out.get("web_search_requests", 0) + int(n)
    return out


async def _record_usage(
    purpose: str, model: str, tier: str, usage: Any, on_usage: UsageHook | None = None
) -> None:
    """Book the call's tokens against the cap, then tell on_usage. Never raises."""
    counts = usage_dict(usage)
    cost = Decimal("0")
    try:
        from core.costs import cost_of_usage, record_llm_call

        cost = cost_of_usage(model, counts)
        await record_llm_call(purpose, model, counts, tier=tier)
    except Exception:
        log.exception("could not record usage for %s", purpose)
    if on_usage is not None:
        try:
            on_usage(model, counts, cost)
        except Exception:
            log.exception("on_usage hook failed for %s", purpose)


async def _openai_complete_stub(
    *,
    system: str,
    user: str,
    tier: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """OpenAI completion stub. Not implemented; raises on the first call.

    Wire-up plan when ready:
      pip install openai
      import openai
      client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
      resp = await client.chat.completions.create(
          model=resolve_model(tier, provider="openai"),
          max_tokens=max_tokens,
          temperature=temperature,
          messages=[
              {"role": "system", "content": system},
              {"role": "user", "content": user},
          ],
      )
      return resp.choices[0].message.content or ""
    """
    log.error(
        "openai provider selected but not implemented; flip "
        "ATLAS_LLM_PROVIDER back to anthropic"
    )
    return ""


__all__ = ["complete", "complete_json", "current_provider", "resolve_model", "request_kwargs",
           "takes_effort", "takes_sampling", "usage_dict"]
