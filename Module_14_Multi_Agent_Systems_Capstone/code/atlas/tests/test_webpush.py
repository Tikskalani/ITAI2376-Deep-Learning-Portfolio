"""Tests for core/surfaces/webpush.py: the RFC 8291 encryption round-trips
against a decryptor written here from the spec, the VAPID JWT verifies
with the public key, and send_push maps push-service responses. No
network: httpx goes through a MockTransport."""

from __future__ import annotations

import json
import struct
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.surfaces import webpush


def _ua_keys() -> tuple[ec.EllipticCurvePrivateKey, str, str]:
    """A fake browser subscription: private key, p256dh, auth."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return priv, webpush.b64url_encode(pub), webpush.b64url_encode(b"\x01" * 16)


def _decrypt(body: bytes, ua_priv: ec.EllipticCurvePrivateKey, auth_b64: str) -> bytes:
    """RFC 8291 section 3 from the receiver's side."""
    salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    assert rs == 4096
    server_pub_raw = body[21 : 21 + idlen]
    ciphertext = body[21 + idlen :]
    server_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_pub_raw)
    shared = ua_priv.exchange(ec.ECDH(), server_pub)
    ua_pub_raw = ua_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    ikm = HKDF(hashes.SHA256(), 32, webpush.b64url_decode(auth_b64),
               b"WebPush: info\x00" + ua_pub_raw + server_pub_raw).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_b64url_round_trip_without_padding():
    raw = bytes(range(0, 37))
    enc = webpush.b64url_encode(raw)
    assert "=" not in enc and "+" not in enc and "/" not in enc
    assert webpush.b64url_decode(enc) == raw
    assert webpush.b64url_decode(enc + "==") == raw  # tolerant of padding


def test_generate_keys_shapes():
    keys = webpush.generate_vapid_keys()
    assert len(webpush.b64url_decode(keys.private_key)) == 32
    pub = webpush.b64url_decode(keys.public_key)
    assert len(pub) == 65 and pub[0] == 0x04


def test_encrypt_round_trip_and_fresh_salt_each_time():
    ua_priv, p256dh, auth = _ua_keys()
    payload = json.dumps({"title": "Brief", "body": "x" * 500}).encode()
    one = webpush.encrypt(payload, p256dh=p256dh, auth=auth)
    two = webpush.encrypt(payload, p256dh=p256dh, auth=auth)
    assert one != two  # random salt and ephemeral key
    assert _decrypt(one, ua_priv, auth) == payload
    assert _decrypt(two, ua_priv, auth) == payload


def test_encrypt_rejects_oversize_payload():
    _, p256dh, auth = _ua_keys()
    with pytest.raises(webpush.WebPushError):
        webpush.encrypt(b"x" * 4100, p256dh=p256dh, auth=auth)


def test_vapid_authorization_verifies_with_public_key():
    keys = webpush.generate_vapid_keys()
    header = webpush.vapid_authorization(
        "https://web.push.apple.com/QABC123", subject="mailto:t@example.com",
        private_key=keys.private_key, public_key=keys.public_key,
    )
    assert header.startswith("vapid t=") and f", k={keys.public_key}" in header
    jwt = header[len("vapid t="):].split(",")[0]
    h, c, s = jwt.split(".")
    assert json.loads(webpush.b64url_decode(h)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(webpush.b64url_decode(c))
    assert claims["aud"] == "https://web.push.apple.com"
    assert claims["sub"] == "mailto:t@example.com"
    assert time.time() < claims["exp"] <= time.time() + 12 * 3600 + 5
    sig = webpush.b64url_decode(s)
    assert len(sig) == 64
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), webpush.b64url_decode(keys.public_key)
    )
    pub.verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if wrong


async def _send(status: int, calls: list[httpx.Request]) -> int:
    ua_priv, p256dh, auth = _ua_keys()
    keys = webpush.generate_vapid_keys()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text="nope" if status >= 400 else "")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await webpush.send_push(
            {"endpoint": "https://push.example/sub/1", "p256dh": p256dh, "auth": auth},
            {"title": "hi"},
            subject="mailto:t@example.com", private_key=keys.private_key,
            public_key=keys.public_key, urgency="high", client=client,
        )


async def test_send_push_headers_and_status_mapping():
    calls: list[httpx.Request] = []
    assert await _send(201, calls) == 201
    req = calls[0]
    assert req.headers["Content-Encoding"] == "aes128gcm"
    assert req.headers["Urgency"] == "high"
    assert req.headers["TTL"] == "86400"
    assert req.headers["Authorization"].startswith("vapid t=")
    assert len(req.content) > 21 + 65

    with pytest.raises(webpush.SubscriptionGone):
        await _send(410, [])
    with pytest.raises(webpush.SubscriptionGone):
        await _send(404, [])
    with pytest.raises(webpush.WebPushError):
        await _send(500, [])
