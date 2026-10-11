import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import Settings

_hasher = PasswordHasher()
_PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token(prefix: str = "") -> str:
    raw = secrets.token_urlsafe(32)
    return f"{prefix}{raw}" if prefix else raw


def pairing_code() -> str:
    raw = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def public_worker_code() -> str:
    raw = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(6))
    return f"SC-{raw}"


def encode_access_token(settings: Settings, user_id: str, role: str) -> str:
    exp = datetime.now(UTC) + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode(
        {"sub": user_id, "role": role, "typ": "access", "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(settings: Settings, token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def sign_subject(settings: Settings, purpose: str, subject: str, ttl_seconds: int | None = None) -> str:
    exp = int(datetime.now(UTC).timestamp()) + (ttl_seconds or settings.media_url_ttl_seconds)
    msg = f"{purpose}.{subject}.{exp}"
    sig = hmac.new(settings.jwt_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_subject(settings: Settings, purpose: str, subject: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(datetime.now(UTC).timestamp()):
        return False
    msg = f"{purpose}.{subject}.{exp}"
    expected = hmac.new(settings.jwt_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
