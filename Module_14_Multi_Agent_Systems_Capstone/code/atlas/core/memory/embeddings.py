"""Atlas — embeddings via local Ollama.

Calls nomic-embed-text running in the local Ollama container. Returns a
list[float] of length OLLAMA_EMBED_DIM (768 by default). Raises
EmbeddingError on connection or shape failures so callers can decide
whether to fall back to plain-text search or skip the write.
"""

from __future__ import annotations

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings


class EmbeddingError(RuntimeError):
    """Raised when Ollama is unreachable or returns an unexpected response."""


_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


async def embed(text: str) -> list[float]:
    """Return a 768-dim embedding for `text`.

    Retries transient connection failures up to 3 times with exponential
    backoff. Any other failure raises EmbeddingError with a useful message.
    """
    if not text or not text.strip():
        raise EmbeddingError("Cannot embed empty text")

    url = f"{settings.ollama_url.rstrip('/')}/api/embeddings"
    payload = {"model": settings.ollama_embed_model, "prompt": text}

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"Ollama request failed: {exc}") from exc

    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise EmbeddingError(f"Ollama returned no embedding (keys={list(data.keys())})")
    if len(vec) != settings.ollama_embed_dim:
        raise EmbeddingError(
            f"Ollama returned {len(vec)}-dim vector, expected {settings.ollama_embed_dim}"
        )
    return [float(x) for x in vec]
