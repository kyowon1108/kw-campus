"""Small crypto helpers: device signatures, result decryption, access tokens, PKCE."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
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


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return b64u(os.urandom(nbytes))


# --- device signatures (WebCrypto ECDSA P-256 / SHA-256, raw r||s) ---------------

def public_key_from_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("expected an EC P-256 public key")
    x = int.from_bytes(b64u_decode(jwk["x"]), "big")
    y = int.from_bytes(b64u_decode(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def verify_device_signature(jwk: dict, message: str, signature_b64u: str) -> bool:
    raw = b64u_decode(signature_b64u)
    if len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        public_key_from_jwk(jwk).verify(der, message.encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


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
    expected = b64u(hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
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
