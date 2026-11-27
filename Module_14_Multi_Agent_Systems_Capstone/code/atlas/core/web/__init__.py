"""Atlas -- shared web fetching utilities.

A single HTTP fetcher used by any agent that needs to read an arbitrary
URL: News & Discovery for forwarded links, Opportunity Scout for gig
postings, more later. The fetch streams with a byte cap, guards content
type, and strips HTML lightly. It is deliberately not a full readability
extractor; the model handles imperfect text fine, and a heavyweight
parsing library is not worth the dependency.

The URL comes from a model, and a model can be talked into anything by
the page it read last. So fetch_url reads only the public internet: http
and https only, and every host, on the first request and on every
redirect, must resolve to public addresses. Loopback (Atlas's own API,
Ollama, Postgres), private ranges, link-local (cloud metadata at
169.254.169.254), multicast, reserved and unspecified addresses are
refused. Redirects are followed by hand so each hop is checked, and the
connection itself goes through _GuardedBackend, which resolves the host
again and connects to the checked address, so a DNS answer that changes
between the check and the connect (rebinding) cannot reach a local
service either.

Hidden elements (display:none, white text, the hidden attribute) are
dropped before the text reaches the model, the same as job discovery.

fetch_url_tool() is the ToolSpec factory; each agent calls it with a
short description suffix so the model sees the right framing for its
own job.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpcore
import httpx

from core.htmltext import strip_hidden
from core.runtime.agent import ToolSpec

# Cap the page body we accept off the wire (a couple of megabytes is
# plenty for an article) and cap the readable text handed back to the
# model so a long page does not blow the agent's context.
_MAX_BYTES = 2_000_000
_MAX_TEXT_CHARS = 30_000
_FETCH_TIMEOUT_S = 15.0
_MAX_REDIRECTS = 5
_USER_AGENT = "Atlas/1.0 (+single-user research assistant)"

# Cheap-but-good-enough HTML cleanup. The model handles imperfect text;
# a heavyweight readability lib is not worth the dep.
_SCRIPT_STYLE_RE = re.compile(
    r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[\s ]+")
_TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE
)

_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&mdash;": "-",
    "&ndash;": "-",
    "&hellip;": "...",
}


def _decode_entities(text: str) -> str:
    for entity, repl in _ENTITIES.items():
        text = text.replace(entity, repl)
    return text


def html_to_text(html: str) -> str:
    """Strip HTML to readable text. Imperfect but adequate for the model.
    Hidden elements go first, contents and all."""
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    stripped = strip_hidden(stripped)
    stripped = _TAG_RE.sub(" ", stripped)
    stripped = _decode_entities(stripped)
    return _WS_RE.sub(" ", stripped).strip()


def extract_title(html: str) -> str | None:
    """Pull the <title> text from an HTML page, or None if absent."""
    m = _TITLE_RE.search(html)
    if not m:
        return None
    title = _decode_entities(m.group(1))
    return _WS_RE.sub(" ", title).strip() or None


def _looks_like_html_or_text(content_type: str) -> bool:
    ct = content_type.lower()
    return "html" in ct or ct.startswith("text/")


# ---------------------------------------------------------------- the guard


class FetchRefused(Exception):
    """The URL points somewhere fetch_url does not go, or the fetch cannot
    finish. The message is shown to the agent as the error."""


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# NAT64 (RFC 6052): on a network with a NAT64 gateway, 64:ff9b::7f00:1 reaches
# 127.0.0.1. Python counts the well-known prefix as global, so the IPv4 address
# inside it is what gets checked.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _is_blocked(ip: IPAddress) -> bool:
    """True for any address that is not plainly on the public internet."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    elif isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global  # shared address space (100.64/10) and the like
    )


async def _resolve(host: str, port: int) -> list[str]:
    """Every address the host resolves to. Tests replace this with a fake DNS."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _public_addresses(host: str, port: int) -> list[IPAddress]:
    """The host's addresses, or FetchRefused if any of them is not public.
    One private answer refuses the host: a name that resolves to both a
    public and a private address is how rebinding tricks start."""
    host = host.strip().strip("[]")
    if not host:
        raise FetchRefused("url has no host")
    addresses: list[IPAddress] = []
    try:
        addresses.append(ipaddress.ip_address(host))
    except ValueError:
        pass  # a name, not a literal: resolve it
    if not addresses:
        try:
            names = await _resolve(host, port)
        except (OSError, UnicodeError) as exc:
            raise FetchRefused(f"could not resolve {host}: {exc}") from exc
        for name in names:
            try:
                addresses.append(ipaddress.ip_address(name.split("%", 1)[0]))
            except ValueError:
                continue
        if not addresses:
            raise FetchRefused(f"could not resolve {host}")
    for ip in addresses:
        if _is_blocked(ip):
            raise FetchRefused(
                f"refused {host}: it points at a private or local address ({ip}); "
                "only public web pages are fetched"
            )
    return addresses


async def _check_url(url: httpx.URL) -> None:
    """Scheme and host checks for one hop."""
    if url.scheme not in ("http", "https"):
        raise FetchRefused(f"refused {url}: only http and https are fetched")
    host = url.raw_host.decode("ascii", errors="replace")
    port = url.port or (443 if url.scheme == "https" else 80)
    await _public_addresses(host, port)


async def check_public_url(url: str) -> None:
    """Raise FetchRefused unless url is http(s) on a public host. For code
    that hands a URL to another downloader (yt-dlp) which follows its own
    redirects: this checks only the first hop, so pair it with a downloader
    that cannot be sent anywhere (see core.transcription)."""
    try:
        parsed = httpx.URL((url or "").strip())
    except httpx.InvalidURL as exc:
        raise FetchRefused(f"not a valid url: {exc}") from exc
    await _check_url(parsed)


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """Connect only to addresses that pass the guard. The host is resolved
    here, checked, and the socket opened to the checked IP, so the address
    connected to is the address approved. TLS still verifies against the
    hostname: httpcore passes it to start_tls separately."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109  (httpcore's interface)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await _public_addresses(host, port)
        last: Exception | None = None
        for ip in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(ip), port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise httpcore.ConnectError(f"could not connect to {host}: {last}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109  (httpcore's interface)
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise FetchRefused("unix sockets are not fetched")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _transport() -> httpx.AsyncBaseTransport:
    """The real transport, with the guarded backend under its pool.
    trust_env=False: an environment proxy would carry the request past the
    connect-time check. Tests replace this with an httpx.MockTransport."""
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool._network_backend = _GuardedBackend()
    return transport


async def _get(client: httpx.AsyncClient, url: str) -> tuple[str, str, str]:
    """GET the URL, following up to _MAX_REDIRECTS redirects by hand and
    checking every hop. Returns (final url, content type, text)."""
    current = httpx.URL(url)
    for _hop in range(_MAX_REDIRECTS + 1):
        await _check_url(current)
        async with client.stream("GET", current) as response:
            if response.is_redirect:
                current = response.url.join(response.headers["location"])
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not _looks_like_html_or_text(content_type):
                raise FetchRefused(
                    f"unsupported content type {content_type!r}; "
                    "only html and text are read"
                )
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) >= _MAX_BYTES:
                    break
            raw = bytes(buffer[:_MAX_BYTES]).decode(
                response.encoding or "utf-8", errors="replace"
            )
            return str(response.url), content_type, raw
    raise FetchRefused(f"more than {_MAX_REDIRECTS} redirects fetching {url}")


async def fetch_html(url: str) -> tuple[str, str]:
    """(final url, raw html) for a public page, under the same guard as
    fetch_url: public addresses only, every redirect checked, byte cap.
    For code that needs the links and structured data a page carries (a job
    posting's apply link, its JSON-LD), not text for a model. Raises
    FetchRefused or httpx.HTTPError."""
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise FetchRefused("url must start with http:// or https://")
    async with httpx.AsyncClient(
        transport=_transport(), timeout=httpx.Timeout(_FETCH_TIMEOUT_S), follow_redirects=False,
        trust_env=False, headers={"User-Agent": _USER_AGENT},
    ) as client:
        final_url, _content_type, raw = await _get(client, url)
    return final_url, raw


async def fetch_url(url: str) -> dict[str, Any]:
    """Fetch a public URL and return its readable text and title, or an error.

    Caps the response body at a few megabytes and the returned text at
    30 000 chars so a single fetch can never blow the agent's context.
    Refuses anything that is not HTML or text/*, and anything that is not
    on the public internet (see the module docstring).
    """
    url = (url or "").strip()
    if not url:
        return {"error": "url is required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}

    try:
        async with httpx.AsyncClient(
            transport=_transport(),
            timeout=httpx.Timeout(_FETCH_TIMEOUT_S),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            final_url, content_type, raw = await _get(client, url)
    except FetchRefused as exc:
        return {"error": str(exc)}
    except httpx.HTTPStatusError as exc:
        return {"error": f"http {exc.response.status_code} fetching {url}"}
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return {"error": f"fetch failed: {type(exc).__name__}: {exc}"}

    text = (
        html_to_text(raw)
        if "html" in content_type.lower()
        else _WS_RE.sub(" ", raw).strip()
    )
    truncated = len(text) > _MAX_TEXT_CHARS
    if truncated:
        text = text[:_MAX_TEXT_CHARS]
    return {
        "url": final_url,
        "title": extract_title(raw),
        "content": text,
        "truncated": truncated,
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def fetch_url_tool(description_suffix: str = "") -> ToolSpec:
    """Build a fetch_url ToolSpec. `description_suffix` lets each agent
    add a sentence framing what kind of page it fetches (a forwarded
    link, a gig posting, etc.)."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        return await fetch_url(args.get("url") or "")

    base = (
        "Fetch a public web page over HTTP(S) and return its readable text "
        "and title. Private, local and internal addresses are refused. The "
        "response is capped so a long page cannot blow your context. If the "
        "fetch fails, say so plainly and work from whatever was pasted "
        "alongside the URL."
    )
    if description_suffix:
        base = base.rstrip(".") + ". " + description_suffix
    return ToolSpec(
        name="fetch_url",
        description=base,
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (http or https).",
                }
            },
            "required": ["url"],
        },
        handler=handler,
    )


__all__ = [
    "FetchRefused",
    "check_public_url",
    "html_to_text",
    "extract_title",
    "fetch_url",
    "fetch_url_tool",
]
