"""Unit tests for the connector base (core/connectors/base.py).

Exercised against a fake connector, so no external systems are touched.
"""

from __future__ import annotations

import pytest

from core.connectors import Connector, ConnectorError, TransientConnectorError


class _FakeConnector(Connector):
    """A connector whose backing 'system' is controllable for tests."""

    name = "fake"
    cache_ttl_seconds = 100.0

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.transient_failures = 0  # fail this many times, then succeed
        self.permanent_fail = False

    async def _fetch_fresh(self, key: str):
        self.calls += 1
        if self.permanent_fail:
            raise RuntimeError("the system is down")
        if self.calls <= self.transient_failures:
            raise TransientConnectorError("temporary blip")
        return f"data-for-{key}"


async def test_fresh_fetch_then_cache_hit():
    c = _FakeConnector()
    first = await c.get("alpha")
    assert first.data == "data-for-alpha"
    assert first.stale is False
    assert c.calls == 1

    second = await c.get("alpha")
    assert second.data == "data-for-alpha"
    assert second.stale is False
    assert c.calls == 1  # served from cache, no second fetch


async def test_expired_cache_refetches():
    c = _FakeConnector()
    c.cache_ttl_seconds = 0  # everything is immediately stale
    await c.get("beta")
    await c.get("beta")
    assert c.calls == 2  # ttl expired, fetched again


async def test_transient_failure_is_retried():
    c = _FakeConnector()
    c.transient_failures = 2  # first two attempts fail, third succeeds
    result = await c.get("gamma")
    assert result.data == "data-for-gamma"
    assert c.calls == 3


async def test_permanent_failure_without_cache_raises():
    c = _FakeConnector()
    c.permanent_fail = True
    with pytest.raises(ConnectorError):
        await c.get("delta")


async def test_permanent_failure_serves_stale_cache():
    c = _FakeConnector()
    c.cache_ttl_seconds = 0  # force a refetch on the second call
    await c.get("epsilon")  # caches good data
    c.permanent_fail = True

    result = await c.get("epsilon")
    assert result.data == "data-for-epsilon"  # the old cached value
    assert result.stale is True


async def test_permanent_failure_no_stale_when_disallowed():
    c = _FakeConnector()
    c.cache_ttl_seconds = 0
    await c.get("zeta")
    c.permanent_fail = True
    with pytest.raises(ConnectorError):
        await c.get("zeta", allow_stale=False)


async def test_invalidate_drops_cache():
    c = _FakeConnector()
    await c.get("eta")
    c.invalidate("eta")
    await c.get("eta")
    assert c.calls == 2



async def test_breaker_trips_after_threshold_failures():
    """N consecutive failures trip the breaker."""
    c = _FakeConnector()
    c.breaker_failure_threshold = 3
    c.permanent_fail = True

    for _ in range(3):
        try:
            await c.get("alpha")
        except ConnectorError:
            pass

    assert c._breaker_tripped_at > 0


async def test_breaker_serves_stale_while_open():
    """While the breaker is open, get() returns stale cache without
    calling _fetch_fresh again."""
    c = _FakeConnector()
    c.breaker_failure_threshold = 2
    c.cache_ttl_seconds = 0  # force a refetch on every call
    await c.get("beta")          # caches good data
    c.calls = 0                  # reset counter for clarity
    c.permanent_fail = True

    # Two failures trip the breaker.
    await c.get("beta")
    await c.get("beta")
    assert c._breaker_tripped_at > 0
    calls_before = c.calls

    # Next call: breaker is open, must serve stale without re-calling.
    result = await c.get("beta")
    assert result.stale is True
    assert c.calls == calls_before  # no new fetch attempt


async def test_breaker_resets_on_success():
    """A single success after some failures resets the breaker."""
    c = _FakeConnector()
    c.breaker_failure_threshold = 2
    c.transient_failures = 1  # first attempt fails, second succeeds
    await c.get("gamma")
    # The breaker should be reset by the success.
    assert c._breaker_failures == 0
    assert c._breaker_tripped_at == 0.0


async def test_breaker_open_raises_when_no_stale_cache():
    """If the breaker trips before any cache exists, callers see a
    clean ConnectorError instead of stale data."""
    import pytest

    c = _FakeConnector()
    c.breaker_failure_threshold = 2
    c.permanent_fail = True
    for _ in range(2):
        try:
            await c.get("delta")
        except ConnectorError:
            pass
    # Breaker open, no cache to fall back on -> raises.
    with pytest.raises(ConnectorError):
        await c.get("delta")
