"""Atlas -- the connector base.

A connector is Atlas's uniform way to reach into an external system: an
Alibaba listing, a property portal, a bank export. Concrete connectors
land from Phase 2 on; this module is the shared base they build on, so
twelve integrations are not twelve fragile one-off scripts.

Every connector gets, for free:
  * an in-memory TTL cache, so repeat reads in a short window are cheap
  * a staleness signal (ConnectorResult.stale), so a caller knows when it
    is looking at cached-but-old data
  * retry on transient failures
  * a clean failure mode (ConnectorError) instead of leaking arbitrary
    exceptions to callers

Subclasses set `name` and `cache_ttl_seconds` and implement one method,
`_fetch_fresh`. Everything else lives here.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    """A connector could not return data, after retries and fallbacks."""


class TransientConnectorError(ConnectorError):
    """A failure worth retrying: a timeout, a 5xx, a rate limit."""


@dataclass
class ConnectorResult:
    """One connector read.

    data       -- whatever the connector returned
    source     -- the connector's name
    fetched_at -- unix time the data was actually fetched from the source
    stale      -- True if data came from cache past its TTL
    """

    data: Any
    source: str
    fetched_at: float
    stale: bool


class Connector(ABC):
    """Base class for every external-system connector.

    Subclasses set `name` and `cache_ttl_seconds` and implement
    `_fetch_fresh`. Callers use `get`.
    """

    name: str = "connector"
    cache_ttl_seconds: float = 300.0
    max_attempts: int = 3

    # Circuit breaker: after `breaker_failure_threshold` consecutive
    # failed fetches, the breaker trips for `breaker_cooldown_seconds`.
    # While tripped, get() skips the live call and serves stale cache
    # immediately (or raises if no cache). A single success resets it.
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 300.0

    def __init__(self) -> None:
        # key -> (fetched_at, data)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._breaker_failures: int = 0
        self._breaker_tripped_at: float = 0.0

    def _breaker_is_open(self, now: float) -> bool:
        if self._breaker_tripped_at == 0.0:
            return False
        return (now - self._breaker_tripped_at) < self.breaker_cooldown_seconds

    def _record_breaker_failure(self) -> None:
        self._breaker_failures += 1
        if self._breaker_failures >= self.breaker_failure_threshold:
            self._breaker_tripped_at = time.time()
            log.warning(
                "%s: circuit breaker tripped after %d consecutive failures",
                self.name,
                self._breaker_failures,
            )

    def _record_breaker_success(self) -> None:
        if self._breaker_failures > 0 or self._breaker_tripped_at > 0:
            log.info("%s: circuit breaker reset on success", self.name)
        self._breaker_failures = 0
        self._breaker_tripped_at = 0.0

    @abstractmethod
    async def _fetch_fresh(self, key: str) -> Any:
        """Fetch fresh data for `key` from the external system.

        Raise TransientConnectorError for failures worth retrying; raise
        any other exception for permanent ones.
        """
        raise NotImplementedError

    async def get(
        self, key: str, *, allow_stale: bool = True
    ) -> ConnectorResult:
        """Return data for `key`, fresh if possible.

        A fresh cache hit is returned immediately. Otherwise a fresh fetch
        is attempted with retry. If that fails, a stale cache entry (if one
        exists and allow_stale is True) is returned with stale=True. If
        there is nothing to fall back on, ConnectorError is raised.
        """
        now = time.time()
        cached = self._cache.get(key)

        # Strict comparison: a TTL of 0 means never serve from cache, even
        # when two calls land in the same clock tick (Windows resolves
        # time.time() to ~15 ms).
        if cached is not None and now - cached[0] < self.cache_ttl_seconds:
            return ConnectorResult(cached[1], self.name, cached[0], stale=False)

        # If the breaker is open, do not attempt the live call. Serve
        # whatever stale data we have, or raise.
        if self._breaker_is_open(now):
            if cached is not None and allow_stale:
                log.warning(
                    "%s: breaker open, serving stale for %r",
                    self.name,
                    key,
                )
                return ConnectorResult(
                    cached[1], self.name, cached[0], stale=True
                )
            raise ConnectorError(
                f"{self.name}: circuit breaker open and no stale cache for "
                f"{key!r}"
            )

        try:
            data = await self._fetch_with_retry(key)
        except Exception as exc:
            self._record_breaker_failure()
            if cached is not None and allow_stale:
                log.warning(
                    "%s: fresh fetch for %r failed (%s); serving stale data",
                    self.name,
                    key,
                    exc,
                )
                return ConnectorResult(
                    cached[1], self.name, cached[0], stale=True
                )
            raise ConnectorError(
                f"{self.name}: could not fetch {key!r}: {exc}"
            ) from exc

        self._record_breaker_success()
        fetched_at = time.time()
        self._cache[key] = (fetched_at, data)
        return ConnectorResult(data, self.name, fetched_at, stale=False)

    async def _fetch_with_retry(self, key: str) -> Any:
        """Call _fetch_fresh, retrying transient failures."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(TransientConnectorError),
            reraise=True,
        ):
            with attempt:
                return await self._fetch_fresh(key)
        raise ConnectorError(  # pragma: no cover
            f"{self.name}: retry loop exited without a result"
        )

    def invalidate(self, key: str | None = None) -> None:
        """Drop cached data: one key, or all of it when key is None."""
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)


__all__ = [
    "Connector",
    "ConnectorResult",
    "ConnectorError",
    "TransientConnectorError",
]
