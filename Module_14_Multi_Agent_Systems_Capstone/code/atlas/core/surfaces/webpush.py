"""Atlas -- Web Push, implemented on `cryptography` alone.

Why not pywebpush: its dependency http-ece ships only an sdist that fails
to build on the cloud VM and is a toolchain hazard on Windows. The two
standards involved are short, and owning them means one less thing that
can break the only push path to Trilok's phone.

  RFC 8291  Message Encryption for Web Push (aes128gcm content coding)
  RFC 8292  VAPID: the server signs a short ES256 JWT so the push service
            knows who is sending

Key material lives in .env: ATLAS_VAPID_PRIVATE_KEY (base64url, the raw
32-byte scalar), ATLAS_VAPID_PUBLIC_KEY (base64url, the 65-byte
uncompressed point the browser passes as applicationServerKey), and
ATLAS_VAPID_SUBJECT (a mailto: or https: contact). generate_vapid_keys()
makes a pair; scripts/vapid_keys.py prints one for pasting.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_CURVE = ec.SECP256R1()
_RECORD_SIZE = 4096
_PUSH_TIMEOUT = httpx.Timeout(15.0)


class WebPushError(RuntimeError):
    """The push service refused the message."""


class SubscriptionGone(WebPushError):
    """404 or 410: the subscription is dead and must be deleted."""


# ---------------------------------------------------------------------------
# base64url helpers (no padding, as the Push API uses)
# ---------------------------------------------------------------------------


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# VAPID keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VapidKeys:
    private_key: str  # base64url raw scalar
    public_key: str  # base64url uncompressed point


def generate_vapid_keys() -> VapidKeys:
    priv = ec.generate_private_key(_CURVE)
    return VapidKeys(
        private_key=b64url_encode(priv.private_numbers().private_value.to_bytes(32, "big")),
        public_key=b64url_encode(_public_bytes(priv.public_key())),
    )


def _public_bytes(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _load_private(private_key_b64: str) -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(b64url_decode(private_key_b64), "big")
    return ec.derive_private_key(scalar, _CURVE)


def vapid_authorization(
    endpoint: str,
    *,
    subject: str,
    private_key: str,
    public_key: str,
    ttl_seconds: int = 12 * 3600,
) -> str:
    """The `Authorization: vapid t=<jwt>, k=<key>` header value (RFC 8292)."""
    parsed = urlparse(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"
    header = b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64url_encode(
        json.dumps(
            {"aud": audience, "exp": int(time.time()) + ttl_seconds, "sub": subject},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    der = _load_private(private_key).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = f"{header}.{claims}.{b64url_encode(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={public_key}"


# ---------------------------------------------------------------------------
# RFC 8291 encryption
# ---------------------------------------------------------------------------


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, *, p256dh: str, auth: str) -> bytes:
    """Encrypt `payload` for a subscription (its p256dh and auth keys).

    Returns the aes128gcm body: salt | rs | idlen | server public key |
    ciphertext. Single record; payloads are small (push services cap at
    about 4 KB).
    """
    ua_public_raw = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, ua_public_raw)

    server_private = ec.generate_private_key(_CURVE)
    server_public_raw = _public_bytes(server_private.public_key())
    shared = server_private.exchange(ec.ECDH(), ua_public)

    key_info = b"WebPush: info\x00" + ua_public_raw + server_public_raw
    ikm = _hkdf(auth_secret, shared, key_info, 32)

    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    if len(payload) + 1 + 16 > _RECORD_SIZE:
        raise WebPushError(f"payload too large for one record ({len(payload)} bytes)")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!I", _RECORD_SIZE) + bytes([len(server_public_raw)]) + server_public_raw
    return header + ciphertext


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


async def send_push(
    subscription: dict[str, Any],
    payload: dict[str, Any],
    *,
    subject: str,
    private_key: str,
    public_key: str,
    ttl: int = 86400,
    urgency: str = "normal",
    client: httpx.AsyncClient | None = None,
) -> int:
    """POST one encrypted message to a subscription's push endpoint.

    subscription has endpoint, p256dh, auth (the PushSubscription JSON the
    browser hands the PWA, flattened). Returns the HTTP status (201 or
    200 on success). Raises SubscriptionGone on 404/410 so the caller can
    delete the row, WebPushError on any other failure.
    """
    endpoint = subscription["endpoint"]
    body = encrypt(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        p256dh=subscription["p256dh"],
        auth=subscription["auth"],
    )
    headers = {
        "Authorization": vapid_authorization(
            endpoint, subject=subject, private_key=private_key, public_key=public_key
        ),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency if urgency in ("very-low", "low", "normal", "high") else "normal",
    }
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=_PUSH_TIMEOUT)
    try:
        try:
            resp = await client.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise WebPushError(f"push transport error: {exc}") from exc
    finally:
        if own_client:
            await client.aclose()
    if resp.status_code in (404, 410):
        raise SubscriptionGone(f"subscription gone ({resp.status_code})")
    if resp.status_code >= 400:
        raise WebPushError(f"push service returned {resp.status_code}: {resp.text[:200]}")
    return resp.status_code


__all__ = [
    "VapidKeys",
    "WebPushError",
    "SubscriptionGone",
    "b64url_encode",
    "b64url_decode",
    "generate_vapid_keys",
    "vapid_authorization",
    "encrypt",
    "send_push",
]
