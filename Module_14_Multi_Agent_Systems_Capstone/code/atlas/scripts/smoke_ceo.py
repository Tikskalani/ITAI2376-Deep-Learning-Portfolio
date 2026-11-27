"""Atlas -- CEO smoke test.

Runs one real CEO turn end to end, without Telegram. Use it to verify the
dispatch -> agent runtime -> Claude API -> memory path against a live
stack before trusting the Telegram bot.

This spends real Claude API tokens (Opus) on the ANTHROPIC_API_KEY
account. One short turn is cheap, but it is not free.

Requires, live:
  * Postgres with migrations applied
  * Ollama serving nomic-embed-text
  * ANTHROPIC_API_KEY set in .env

Run from repo root:

    python -m scripts.smoke_ceo
    python -m scripts.smoke_ceo "what is my Side Shop MOQ target?"

To prove memory persists across conversations, run it once with the
default message (the CEO stores a fact), then run it again with a recall
question and check the CEO answers from memory.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from agents.ceo import register_ceo
from core import db
from core.runtime.dispatcher import dispatch

_DEFAULT_MESSAGE = (
    "This is a smoke test. Remember that my Side Shop minimum order "
    "quantity target is 50 units per supplier, then confirm in one sentence."
)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _main() -> None:
    _setup_logging()
    log = logging.getLogger("atlas.smoke_ceo")

    message = " ".join(sys.argv[1:]).strip() or _DEFAULT_MESSAGE
    log.info("smoke message: %s", message)
    log.info("this spends real Claude API tokens; one short turn is cheap")

    await db.get_pool()
    log.info("postgres pool ready")
    register_ceo()
    log.info("CEO registered with the dispatcher")

    try:
        result = await dispatch(
            "ceo",
            message,
            dispatched_by="scripts/smoke_ceo",
            participants=["owner", "ceo"],
        )
    finally:
        await db.close_pool()

    bar = "=" * 64
    print("\n" + bar)
    print("CEO reply:")
    print(result.text or "(no text returned)")
    print(bar)
    print(f"episode_id  : {result.episode_id}")
    print(f"turns       : {result.turns}")
    print(f"stop_reason : {result.stop_reason}")
    if result.tool_calls:
        print("tool calls  :")
        for call in result.tool_calls:
            flag = " [error]" if call["is_error"] else ""
            print(f"  - {call['name']}{flag}  input={call['input']}")
    else:
        print("tool calls  : none")
    usage = result.usage
    print(
        "tokens      : "
        f"in={usage.get('input_tokens', 0)} "
        f"out={usage.get('output_tokens', 0)} "
        f"cache_read={usage.get('cache_read_input_tokens', 0)} "
        f"cache_write={usage.get('cache_creation_input_tokens', 0)}"
    )
    print(bar)


if __name__ == "__main__":
    asyncio.run(_main())
