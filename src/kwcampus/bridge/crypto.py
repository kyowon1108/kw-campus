"""Small crypto helpers: device signatures, result decryption, access tokens, PKCE."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64u_decode_strict(text: object) -> bytes | None:
    """Only the one spelling b64u() writes: no '=' padding, no '+' or '/', no stray low bits in
    the last character. Anything else is None, so one value can't arrive in several forms."""
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        return None
    raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    return raw if b64u(raw) == text else None


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return b64u(os.urandom(nbytes))


# --- device signatures (WebCrypto ECDSA P-256 / SHA-256, raw r||s) ---------------

def public_key_from_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("expected an EC P-256 public key")
    x, y = b64u_decode_strict(jwk.get("x")), b64u_decode_strict(jwk.get("y"))
    if x is None or y is None or len(x) != 32 or len(y) != 32:
        raise ValueError("x and y must be 32-byte base64url values")
    return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                         ec.SECP256R1()).public_key()


def _signature_rs(signature_b64u: object) -> tuple[int, int] | None:
    """(r, s) of a raw r||s signature: exactly 86 base64url characters, canonical spelling."""
    if not isinstance(signature_b64u, str) or len(signature_b64u) != 86:
        return None
    raw = b64u_decode_strict(signature_b64u)
    if raw is None or len(raw) != 64:
        return None
    return int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")


def verify_device_signature(jwk: dict, message: str, signature_b64u: object) -> bool:
    rs = _signature_rs(signature_b64u)
    if rs is None:
        return False
    try:
        public_key_from_jwk(jwk).verify(encode_dss_signature(*rs), message.encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def signature_replay_key(signature_b64u: str) -> str:
    """Replay-guard key of a verified signature: a hash of its r value. r is the same for every
    spelling of the signature and for its (r, n - s) twin, which also verifies (WebCrypto signs
    high-s too, so s can't simply be required low)."""
    rs = _signature_rs(signature_b64u)
    if rs is None:
        raise ValueError("not a device signature")
    return hashlib.sha256(b"kw-sig-r:" + rs[0].to_bytes(32, "big")).hexdigest()


# --- per-request result encryption (AES-256-GCM) ----------------------------------

def new_result_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def decrypt_result(key: bytes, iv_b64u: str, ciphertext_b64u: str, request_id: str) -> dict:
    """The request id is the associated data, so a ciphertext can't be replayed into another request."""
    try:
        plain = AESGCM(key).decrypt(b64u_decode(iv_b64u), b64u_decode(ciphertext_b64u), request_id.encode())
    except InvalidTag as e:
        raise ValueError("result failed authentication") from e
    return json.loads(plain)


# --- access tokens (JWT HS256) ----------------------------------------------------

def sign_jwt(secret: bytes, claims: dict) -> str:
    header = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signature = b64u(hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def verify_jwt(secret: bytes, token: str, audience: str, now: float | None = None) -> dict | None:
    try:
        header, payload, signature = token.split(".")
    except ValueError:
        return None
    # compare_digest raises TypeError on a str with non-ASCII characters (a forged bearer token
    # would be a 500 on every route); a real signature is base64url, so anything else is refused.
    if not (header + payload + signature).isascii():
        return None
    expected = b64u(hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return None
    try:
        claims = json.loads(b64u_decode(payload))
        if json.loads(b64u_decode(header)).get("alg") != "HS256":
            return None
    except ValueError:
        return None
    if claims.get("aud") != audience or float(claims.get("exp", 0)) < (now or time.time()):
        return None
    return claims


# --- PKCE ---------------------------------------------------------------------------

def pkce_s256(verifier: str) -> str:
    return b64u(hashlib.sha256(verifier.encode()).digest())
