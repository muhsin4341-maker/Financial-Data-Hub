"""
Security Utilities — Foundation Layer.

Engineering Specification references:
  Part 2, Section 8.2, Decision 1  — JWT access tokens (15-min) + refresh tokens (30-day)
  Part 2, Section 8.2, Decision 2  — bcrypt cost factor 12; HIBP k-anonymity check
  Part 2, Section 8.2, Decision 4  — TOTP secret AES-256-GCM encrypted in users table
  Part 2, Section 8.3              — JWT payload: { sub, tid, role, exp, jti }
  Part 2, Section 8.3              — Password policy: 12 chars, complexity, HIBP, lockout
  Part 3, Section 10.4             — Secrets from AWS Secrets Manager; never in code

Responsibilities of this module (pure utilities — NO database, NO FastAPI):
  - Password hashing and verification (bcrypt)
  - Password policy validation (complexity + HIBP k-anonymity)
  - JWT access token creation and verification
  - Opaque refresh token generation and hashing
  - Password reset token generation and hashing
  - TOTP secret generation, AES-256-GCM encryption/decryption, and verification

Implementation note — passlib compatibility:
  passlib 1.7.4 has a known incompatibility with bcrypt >= 4.0 (the auto-detection
  code reads `bcrypt.__about__.__version__` which no longer exists in bcrypt 5.x).
  The installed stack has bcrypt 5.0.0. This module therefore wraps the `bcrypt`
  package directly, providing an identical public API to what passlib would expose.
  If passlib is updated to fix this issue, replace the internals with a CryptContext
  call — the public signatures of `hash_password()` and `verify_password()` are
  unchanged.

Implementation note — bcrypt 72-byte limit:
  bcrypt 5.x hard-rejects passwords whose UTF-8 encoding exceeds 72 bytes
  (raising ValueError). Prior versions silently truncated, which was a different
  problem. The spec mandates no maximum password length. The fix is SHA-256
  pre-hashing: the password is first reduced to a fixed 32-byte digest before
  bcrypt processing. This is the canonical solution used by Django's bcrypt
  hasher. Full details in ``_prepare_password_bytes``.

Milestone: M1-Step17 — Security utilities
Status: COMPLETE
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import bcrypt as _bcrypt
import httpx
import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError
from pydantic import BaseModel, field_validator

from apps.api.core.config import Settings, get_settings
from apps.api.core.exceptions import UnauthorizedError

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

#: Token type identifiers embedded in the JWT payload.
_ACCESS_TOKEN_TYPE = "access"

#: HIBP k-anonymity API — accepts the first 5 hex chars of a SHA-1 hash.
_HIBP_API_URL = "https://api.pwnedpasswords.com/range/{prefix}"

#: Number of bcrypt rounds mandated by Spec Part 2, Section 8.2, Decision 2.
_BCRYPT_ROUNDS: int = 12

#: HKDF-like derivation info label for TOTP encryption key.
_TOTP_KEY_INFO = b"financial-data-hub:totp-encryption-key:v1"


# ---------------------------------------------------------------------------
# TokenPayload — typed result of JWT verification
# ---------------------------------------------------------------------------


class TokenPayload(BaseModel):
    """
    Decoded and validated JWT access token payload.

    Engineering Spec Part 2, Section 8.3:
      { "sub": "user_id", "tid": "tenant_id", "role": "analyst",
        "exp": timestamp, "jti": "token_id" }

    Additional fields `iat` and `type` are added by this implementation for
    defence-in-depth (type confusion attacks).
    """

    sub: uuid.UUID
    """user_id — maps to users.id"""

    tid: uuid.UUID
    """tenant_id — maps to tenants.id; determines multi-tenant context"""

    role: str
    """RBAC role string: owner | admin | analyst | viewer"""

    jti: str
    """JWT ID — unique per token; used as Redis blocklist key on revocation"""

    exp: datetime
    """Absolute expiry timestamp (UTC). jose validates this automatically."""

    iat: datetime
    """Issued-at timestamp (UTC)."""

    type: Literal["access"]
    """Token type guard — rejects refresh tokens presented as access tokens."""

    @field_validator("sub", "tid", mode="before")
    @classmethod
    def _coerce_uuid(cls, v: object) -> uuid.UUID:
        """Accept both str and UUID; jose returns str from JSON."""
        if isinstance(v, uuid.UUID):
            return v
        return uuid.UUID(str(v))

    @field_validator("exp", "iat", mode="before")
    @classmethod
    def _coerce_datetime(cls, v: object) -> datetime:
        """jose decodes exp/iat as int (Unix timestamp). Normalise to datetime."""
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=UTC)
        if not isinstance(v, (int, float)):
            raise ValueError(f"Expected numeric timestamp, got {type(v).__name__!r}")
        return datetime.fromtimestamp(float(v), tz=UTC)


# ---------------------------------------------------------------------------
# Password hashing  (Spec Part 2, Section 8.2, Decision 2)
# ---------------------------------------------------------------------------


def _prepare_password_bytes(plain_password: str) -> bytes:
    """
    Pre-hash a plaintext password with SHA-256 before bcrypt processing.

    **Why this exists:**
    bcrypt 5.x hard-rejects passwords whose UTF-8 encoding exceeds 72 bytes,
    raising ``ValueError``. Prior bcrypt versions silently truncated, meaning
    ``"A" * 73`` and ``"A" * 72 + "B"`` produced the *same* hash — a security
    flaw. bcrypt 5.x fixed the truncation by rejecting long inputs entirely,
    but that breaks the spec requirement of no maximum password length.

    **The fix — SHA-256 prehash:**
    Pre-hashing with SHA-256 produces a fixed 32-byte digest regardless of
    the input length or Unicode content. 32 bytes is well within bcrypt's
    72-byte limit. Critically, any two passwords that differ *anywhere*
    produce different SHA-256 digests, so there is no truncation: two
    passwords differing only at byte 73 will hash differently.

    This is the canonical solution recommended by bcrypt's documentation and
    used by Django's bcrypt hasher.

    Args:
        plain_password: The user-supplied plaintext password (any length).

    Returns:
        A 32-byte SHA-256 digest of the UTF-8-encoded password.
    """
    return hashlib.sha256(plain_password.encode("utf-8")).digest()


def hash_password(plain_password: str) -> str:
    """
    Hash a plaintext password using bcrypt at cost factor 12.

    Engineering Spec Part 2, Section 8.2, Decision 2:
      "bcrypt with cost factor 12 via passlib[bcrypt].
       Never store plaintext or reversibly-encrypted passwords."

    The password is first reduced to a 32-byte SHA-256 digest by
    ``_prepare_password_bytes`` before bcrypt processing. This satisfies
    the spec's no-maximum-length requirement for passwords of any length
    and any Unicode content.

    Args:
        plain_password: The user-supplied plaintext password (any length).

    Returns:
        A bcrypt hash string (60 characters, ``$2b$12$…``) safe to store
        in the ``users.password_hash`` column.
    """
    salt = _bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return _bcrypt.hashpw(_prepare_password_bytes(plain_password), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plaintext password against a stored bcrypt hash.

    The password is pre-hashed with SHA-256 (matching ``hash_password``)
    before the bcrypt comparison. ``bcrypt.checkpw`` uses constant-time
    comparison internally to prevent timing attacks.

    Args:
        plain_password:   The user-supplied plaintext password (any length).
        hashed_password:  The value stored in ``users.password_hash``.

    Returns:
        True if the password matches the hash, False otherwise.
        Never raises on invalid hash format — returns False instead.
    """
    try:
        return _bcrypt.checkpw(
            _prepare_password_bytes(plain_password),
            hashed_password.encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 — invalid hash format, malformed input
        return False


# ---------------------------------------------------------------------------
# Password policy validation  (Spec Part 2, Section 8.3)
# ---------------------------------------------------------------------------


class PasswordPolicyError(ValueError):
    """
    Raised when a candidate password violates the complexity policy.

    Engineering Spec Part 2, Section 8.3 — Password Policy:
      - Minimum 12 characters
      - Must contain: uppercase letter, lowercase letter, digit, special character
      - No maximum length
      - Account lockout after 10 failed attempts; 30-minute unlock or admin reset
        (lockout is enforced by the auth router / repository, not here)
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


def validate_password_complexity(password: str) -> None:
    """
    Enforce the password complexity policy synchronously.

    Raises ``PasswordPolicyError`` with a list of all violations if the
    password does not meet requirements. Does NOT check HIBP — call
    ``check_hibp_password()`` separately for the breach check.

    Args:
        password: Candidate plaintext password.

    Raises:
        PasswordPolicyError: If one or more complexity rules are violated.
    """
    violations: list[str] = []

    if len(password) < 12:
        violations.append("Password must be at least 12 characters long")
    if not re.search(r"[A-Z]", password):
        violations.append("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", password):
        violations.append("Password must contain at least one lowercase letter")
    if not re.search(r"\d", password):
        violations.append("Password must contain at least one digit")
    if not re.search(r"[^A-Za-z0-9]", password):
        violations.append("Password must contain at least one special character")

    if violations:
        raise PasswordPolicyError(violations)


async def check_hibp_password(password: str) -> bool:
    """
    Check whether a password has appeared in a known data breach using the
    HaveIBeenPwned API with k-anonymity (Spec Part 2, Section 8.2, Decision 2).

    The raw password is NEVER sent to the API. Only the first 5 hex characters
    of its SHA-1 hash are transmitted. The server returns all hashes sharing
    that prefix; the suffix match is performed locally.

    Args:
        password: Candidate plaintext password.

    Returns:
        True  — password appears in a known breach (caller should reject it).
        False — password not found in HIBP (no guarantee it is safe, just not listed).

    Note:
        Network errors are caught and return False to avoid blocking registration
        when HIBP is unreachable. Log the exception at WARNING level in the caller.
    """
    sha1_hex = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = sha1_hex[:5], sha1_hex[5:]

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                _HIBP_API_URL.format(prefix=prefix),
                headers={"Add-Padding": "true"},  # Prevents traffic analysis
            )
            response.raise_for_status()
    except httpx.HTTPError:
        # Network or HTTP error — fail open rather than blocking the user.
        # The auth router SHOULD log this at WARNING level.
        return False

    # Each line is "<SUFFIX>:<count>" — check if our suffix appears.
    for line in response.text.splitlines():
        parts = line.split(":")
        if len(parts) == 2 and parts[0].upper() == suffix:
            return True
    return False


# ---------------------------------------------------------------------------
# JWT access token  (Spec Part 2, Section 8.3)
# ---------------------------------------------------------------------------


def create_access_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    *,
    settings: Settings | None = None,
) -> tuple[str, str]:
    """
    Encode a signed JWT access token.

    Engineering Spec Part 2, Section 8.3 — JWT Access Token Payload:
      { "sub": user_id, "tid": tenant_id, "role": role,
        "exp": timestamp, "jti": token_id }

    Additional fields `iat` and `type` are included for defence-in-depth.

    Args:
        user_id:   Primary key of the authenticated user.
        tenant_id: Primary key of the active tenant context.
        role:      RBAC role string for the user within this tenant.
        settings:  Optional settings override (useful in tests). Defaults to
                   ``get_settings()``.

    Returns:
        A ``(token, jti)`` tuple where:
          - ``token`` is the signed JWT string to include in the
            ``Authorization: Bearer <token>`` header.
          - ``jti`` is the unique JWT ID — store it alongside the
            refresh token record to enable full session revocation.
    """
    cfg = settings or get_settings()
    now = datetime.now(UTC)
    jti = str(uuid.uuid4())

    payload: dict[str, object] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "jti": jti,
        "exp": now + timedelta(minutes=cfg.jwt_access_token_expire_minutes),
        "iat": now,
        "type": _ACCESS_TOKEN_TYPE,
    }

    token = jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)
    return token, jti


def verify_access_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> TokenPayload:
    """
    Decode and validate a JWT access token.

    Validates signature, expiry, and token type. Returns a typed
    ``TokenPayload`` on success.

    Args:
        token:    The raw JWT string from the ``Authorization: Bearer`` header.
        settings: Optional settings override (useful in tests).

    Returns:
        ``TokenPayload`` with all claims fully validated and typed.

    Raises:
        UnauthorizedError: For any token problem — expired, bad signature,
            malformed, missing claims, or wrong token type. The error message
            is intentionally generic to avoid leaking cryptographic detail
            to callers. The specific cause is embedded in the exception
            ``details`` dict (internal use; must not reach API responses).
    """
    cfg = settings or get_settings()

    try:
        raw = jwt.decode(
            token,
            cfg.jwt_secret,
            algorithms=[cfg.jwt_algorithm],
            options={"require": ["sub", "tid", "role", "jti", "exp", "iat", "type"]},
        )
    except ExpiredSignatureError:
        raise UnauthorizedError("Access token has expired") from None
    except (JWTClaimsError, JWTError) as exc:
        raise UnauthorizedError("Invalid access token") from exc

    # Reject refresh tokens presented as access tokens (type confusion guard).
    if raw.get("type") != _ACCESS_TOKEN_TYPE:
        raise UnauthorizedError("Invalid token type")

    try:
        return TokenPayload.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — pydantic validation error
        raise UnauthorizedError("Malformed token payload") from exc


# ---------------------------------------------------------------------------
# Opaque refresh token  (Spec Part 2, Section 8.2, Decision 1)
# ---------------------------------------------------------------------------


def generate_raw_refresh_token() -> str:
    """
    Generate a cryptographically secure opaque refresh token.

    This is the value stored in the httpOnly cookie. It is a 256-bit
    URL-safe hex string; knowledge of it is sufficient to use the token.

    Returns:
        A 64-character lowercase hex string (256 bits of entropy).
    """
    return secrets.token_hex(32)


def hash_refresh_token(raw_token: str) -> str:
    """
    Compute the SHA-256 hex digest of a raw refresh token.

    The database column ``refresh_tokens.token_hash`` stores this digest —
    never the raw token. Token lookup during refresh is performed by hashing
    the cookie value and querying by hash.

    Args:
        raw_token: The raw 256-bit hex token from the httpOnly cookie.

    Returns:
        A 64-character lowercase SHA-256 hex digest.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_refresh_token_expiry(*, settings: Settings | None = None) -> datetime:
    """
    Compute the expiry datetime for a new refresh token.

    Engineering Spec Part 2, Section 8.2, Decision 1:
      Refresh tokens expire after 30 days.

    Args:
        settings: Optional settings override.

    Returns:
        UTC datetime 30 days from now.
    """
    cfg = settings or get_settings()
    return datetime.now(UTC) + timedelta(days=cfg.jwt_refresh_token_expire_days)


# ---------------------------------------------------------------------------
# Password reset token
# ---------------------------------------------------------------------------


def generate_password_reset_token() -> str:
    """
    Generate a cryptographically secure password reset token.

    This is the raw value placed in the reset link email. The database
    stores a SHA-256 hash of this value (``users.password_reset_token``).

    Engineering Spec Phase 1 Implementation Guide, Step 21:
      POST /auth/forgot-password — generate reset token, queue email Celery task.

    Returns:
        A 48-character URL-safe base64 string (288 bits of entropy).
        URL-safe encoding avoids percent-encoding issues in email links.
    """
    raw = secrets.token_bytes(36)  # 288 bits
    return base64.urlsafe_b64encode(raw).decode("ascii")


def hash_password_reset_token(raw_token: str) -> str:
    """
    Compute the SHA-256 hex digest of a raw password reset token.

    The ``users.password_reset_token`` column stores this hash. On receipt
    of a reset request, hash the URL token and compare against the column.

    Args:
        raw_token: The raw URL-safe base64 token from the reset link.

    Returns:
        A 64-character lowercase SHA-256 hex digest.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# TOTP / MFA utilities  (Spec Part 2, Section 8.2, Decision 4)
# ---------------------------------------------------------------------------


def generate_totp_secret() -> str:
    """
    Generate a new TOTP base32 secret.

    Engineering Spec Part 2, Section 8.2, Decision 4:
      "TOTP via authenticator app using pyotp library. Store TOTP secret
       AES-encrypted in users table. Never store TOTP secret in plaintext."

    Returns:
        A 32-character base32 string suitable for pyotp. This is the plaintext
        secret — it MUST be passed to ``encrypt_totp_secret()`` before storage.
    """
    return pyotp.random_base32()


def get_totp_provisioning_uri(
    secret: str,
    email: str,
    issuer: str = "Financial Data Hub",
) -> str:
    """
    Build the ``otpauth://`` URI for QR code presentation.

    Args:
        secret: Plaintext base32 TOTP secret (before encryption).
        email:  User's email address (account identifier in authenticator app).
        issuer: Displayed issuer name in the authenticator app.

    Returns:
        A ``otpauth://totp/...`` URI to encode as a QR code.
    """
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp_code(encrypted_secret: str, code: str, *, settings: Settings | None = None) -> bool:
    """
    Verify a TOTP code against an AES-encrypted secret stored in the database.

    Decrypts the secret in-memory, verifies the code with a ±1 window
    (allows for clock drift up to 30 seconds), and discards the plaintext.

    Args:
        encrypted_secret: The value from ``users.totp_secret`` (AES-256-GCM
                          encrypted + base64-encoded).
        code:             The 6-digit code from the authenticator app.
        settings:         Optional settings override.

    Returns:
        True if the code is valid within the current ±1 time window.
        False if the code is wrong, the secret is malformed, or decryption fails.
    """
    try:
        plain_secret = decrypt_totp_secret(encrypted_secret, settings=settings)
        return pyotp.TOTP(plain_secret).verify(code, valid_window=1)
    except Exception:  # noqa: BLE001 — decryption failure, malformed base32, etc.
        return False


def _derive_totp_key(settings: Settings) -> bytes:
    """
    Derive a 32-byte AES key from the application ``secret_key`` setting.

    Uses SHA-256 keyed with a domain-specific label to produce a dedicated
    key for TOTP encryption, separate from the JWT signing secret.

    This is a lightweight KDF (key-derivation function). For stricter security
    a proper HKDF could be used; SHA-256 is sufficient here because the input
    key material (``secret_key``) is a high-entropy randomly generated value.

    Args:
        settings: Application settings containing ``secret_key``.

    Returns:
        A 32-byte key for use with AES-256-GCM.
    """
    return hashlib.sha256(_TOTP_KEY_INFO + settings.secret_key.encode("utf-8")).digest()


def encrypt_totp_secret(plain_secret: str, *, settings: Settings | None = None) -> str:
    """
    Encrypt a plaintext TOTP base32 secret using AES-256-GCM.

    Engineering Spec Part 2, Section 8.2, Decision 4:
      "Store TOTP secret AES-encrypted in users table.
       Never store TOTP secret in plaintext."

    The output format is:
      base64url( nonce[12 bytes] || ciphertext_with_tag )

    A fresh 96-bit nonce is generated for every call (IV reuse would be
    catastrophic for GCM — this design guarantees uniqueness).

    Args:
        plain_secret: Plaintext base32 TOTP secret from ``generate_totp_secret()``.
        settings:     Optional settings override.

    Returns:
        A base64url-encoded string (nonce + ciphertext + auth tag) safe to
        store in ``users.totp_secret``.
    """
    cfg = settings or get_settings()
    key = _derive_totp_key(cfg)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce, unique per encryption
    ciphertext = aesgcm.encrypt(nonce, plain_secret.encode("utf-8"), b"")
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_totp_secret(encrypted_secret: str, *, settings: Settings | None = None) -> str:
    """
    Decrypt an AES-256-GCM-encrypted TOTP secret.

    Args:
        encrypted_secret: The value from ``users.totp_secret`` as produced
                          by ``encrypt_totp_secret()``.
        settings:         Optional settings override.

    Returns:
        The plaintext base32 TOTP secret.

    Raises:
        ValueError: If decryption fails (authentication tag mismatch, truncated
                    input, or wrong key). Callers should treat this as an
                    internal error and never surface the reason to the user.
    """
    cfg = settings or get_settings()
    key = _derive_totp_key(cfg)
    aesgcm = AESGCM(key)

    try:
        raw = base64.urlsafe_b64decode(encrypted_secret.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 — base64 decode errors are not typed
        raise ValueError("TOTP secret is not valid base64url") from exc

    if len(raw) < 13:  # minimum: 12-byte nonce + 1 byte ciphertext
        raise ValueError("TOTP secret payload is too short")

    nonce, ciphertext = raw[:12], raw[12:]
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, b"")
    except Exception as exc:  # noqa: BLE001 — cryptography raises generic Exception on tag mismatch
        raise ValueError("TOTP secret decryption failed (authentication tag mismatch)") from exc

    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    # Types
    "TokenPayload",
    "PasswordPolicyError",
    # Password
    "hash_password",
    "verify_password",
    "validate_password_complexity",
    "check_hibp_password",
    # Access token
    "create_access_token",
    "verify_access_token",
    # Refresh token
    "generate_raw_refresh_token",
    "hash_refresh_token",
    "generate_refresh_token_expiry",
    # Password reset token
    "generate_password_reset_token",
    "hash_password_reset_token",
    # TOTP
    "generate_totp_secret",
    "get_totp_provisioning_uri",
    "verify_totp_code",
    "encrypt_totp_secret",
    "decrypt_totp_secret",
]
