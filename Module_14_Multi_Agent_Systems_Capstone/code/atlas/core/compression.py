"""Atlas -- context compression for cheap prompt-cache reads.

Long memory_context blocks (week summaries, big fact dumps, brief
chains) push every agent run into a higher token bucket. This module
compresses anything above a budget into a Haiku-written abstract that
keeps the key facts and drops the filler.

The compression is one-way and lossy by design. Anything called via
this module should be material the agent only needs the gist of, not
material that needs exact wording.

Disabled by default (max_chars passes through). Callers opt in with
compress_for_context(text, max_chars=N).
"""

from __future__ import annotations

import logging

from core.llm import complete

log = logging.getLogger(__name__)


_SUMMARIZE_PROMPT = """\
Compress the following text to no more than {target} characters. Keep
every named entity, every date, every dollar amount, every commitment,
and every decision. Drop ceremony, transitional phrases, and anything
the reader could reconstruct from context. Return only the compressed
text, no preamble.

Text:
{body}
"""


async def compress_for_context(
    text: str,
    *,
    max_chars: int = 4000,
    target_chars: int | None = None,
) -> str:
    """Return text compressed to roughly target_chars (default half of max).

    No-op if text is already short. On any failure returns the original
    truncated to max_chars so callers can rely on a hard upper bound.
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text

    target = target_chars or max(800, max_chars // 2)

    try:
        compressed = await complete(
            system=(
                "You are a precise text compressor. You preserve facts "
                "and drop filler."
            ),
            user=_SUMMARIZE_PROMPT.format(target=target, body=text),
            tier="scan",
            max_tokens=1200,
            temperature=0.0,
        )
    except Exception:
        log.exception("compression call failed; truncating instead")
        compressed = ""

    compressed = (compressed or "").strip()
    if not compressed or len(compressed) > max_chars:
        # Fall back to hard truncation if Haiku ignored the cap.
        return text[: max_chars - 30] + "\n[...truncated by Atlas]"
    return compressed


__all__ = ["compress_for_context"]
