from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


PBKDF2_ITERATIONS = 310_000


def hash_password(
    password: str,
    *,
    iterations: int = PBKDF2_ITERATIONS,
    min_length: int = 12,
) -> str:
    if len(password) < min_length:
        raise ValueError(f"Password must contain at least {min_length} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(salt), int(iterations)
        )
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


def hash_api_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_session_token(
    payload: dict[str, Any], secret_key: str, expires_in_seconds: int
) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + expires_in_seconds
    body["nonce"] = secrets.token_urlsafe(8)
    encoded = _b64(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64(
        hmac.new(secret_key.encode(), encoded.encode(), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}"


def verify_session_token(token: str, secret_key: str) -> dict[str, Any] | None:
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64(
            hmac.new(secret_key.encode(), encoded.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(encoded))
        # ``exp`` is the first second in which a session is no longer valid.
        # Accepting equality grants every token an unintended extra second and
        # makes boundary behaviour depend on request scheduling.
        if int(payload["exp"]) <= int(time.time()):
            return None
        return payload
    except (ValueError, KeyError, TypeError):
        return None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
