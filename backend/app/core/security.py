"""Password hashing, JWT issuing/decoding, and handover-code generation.

The handover code is the mechanism that makes "an LLM cannot mark a donation
delivered" structurally true: only a party holding the plaintext code can
advance a pickup, and the database stores a hash of it.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.settings import get_settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Unambiguous alphabet: no 0/O, no 1/I/L. A volunteer reads this aloud.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def create_access_token(subject: str, role: str, org_id: str | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "org_id": org_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_min)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises ValueError on any invalid token. Callers turn that into a 401."""
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except JWTError as exc:
        raise ValueError(f"invalid token: {exc}") from exc


def generate_handover_code() -> str:
    """Plaintext code, shown once to the donor. Only its hash is stored."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def hash_handover_code(code: str) -> str:
    return _pwd.hash(code.strip().upper())


def verify_handover_code(code: str, hashed: str) -> bool:
    return _pwd.verify(code.strip().upper(), hashed)
