"""
Unit tests — Security Utilities layer.

Tests cover all public functions in ``apps.api.core.security``.
No database, no network calls (HIBP is patched), no FastAPI.

All tests are synchronous except the HIBP check, which is async.

Engineering Spec references:
  Part 2, Section 8.2, Decision 1  — JWT tokens
  Part 2, Section 8.2, Decision 2  — bcrypt + HIBP
  Part 2, Section 8.2, Decision 4  — TOTP AES encryption
  Part 2, Section 8.3              — JWT payload spec, password policy

Milestone: M1-Step17
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps.api.core.exceptions import UnauthorizedError
from apps.api.core.security import (
    PasswordPolicyError,
    TokenPayload,
    check_hibp_password,
    create_access_token,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_password_reset_token,
    generate_raw_refresh_token,
    generate_refresh_token_expiry,
    generate_totp_secret,
    get_totp_provisioning_uri,
    hash_password,
    hash_password_reset_token,
    hash_refresh_token,
    validate_password_complexity,
    verify_access_token,
    verify_password,
    verify_totp_code,
)

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_settings() -> MagicMock:
    """
    Lightweight Settings mock — avoids reading .env during unit tests.
    Uses values representative of the real config schema.
    """
    s = MagicMock()
    s.jwt_secret = "test-jwt-secret-must-be-long-enough-for-hs256"
    s.jwt_algorithm = "HS256"
    s.jwt_access_token_expire_minutes = 15
    s.jwt_refresh_token_expire_days = 30
    s.secret_key = "test-application-secret-key-32bytes!!"
    return s


@pytest.fixture()
def sample_user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def sample_tenant_id() -> uuid.UUID:
    return uuid.uuid4()


# ─── Password hashing ────────────────────────────────────────────────────────


class TestPasswordHashing:
    """Spec Part 2, Section 8.2, Decision 2 — bcrypt cost factor 12."""

    def test_hash_returns_bcrypt_format(self) -> None:
        """Hashed value must start with $2b$12$ (bcrypt, rounds=12)."""
        h = hash_password("SecurePass1!")
        assert h.startswith("$2b$12$"), f"Unexpected format: {h[:10]}"

    def test_hash_is_non_deterministic(self) -> None:
        """Two hashes of the same password must differ (unique salt per call)."""
        h1 = hash_password("SecurePass1!")
        h2 = hash_password("SecurePass1!")
        assert h1 != h2

    def test_verify_correct_password(self) -> None:
        h = hash_password("CorrectHorse1!")
        assert verify_password("CorrectHorse1!", h) is True

    def test_verify_wrong_password(self) -> None:
        h = hash_password("CorrectHorse1!")
        assert verify_password("WrongPassword9@", h) is False

    def test_verify_empty_password_against_valid_hash(self) -> None:
        h = hash_password("NotEmpty1!")
        assert verify_password("", h) is False

    def test_verify_returns_false_on_malformed_hash(self) -> None:
        """Should not raise — return False for any invalid hash format."""
        assert verify_password("anypassword", "not-a-hash") is False

    def test_verify_constant_time_no_exception(self) -> None:
        """verify_password must never raise regardless of inputs."""
        for bad_hash in ["", "x", "$2b$", "0" * 100, "💥"]:
            result = verify_password("password", bad_hash)
            assert isinstance(result, bool)


# ─── Long-password regression tests ──────────────────────────────────────────


class TestPasswordHashingLongPasswords:
    """
    Regression tests for the bcrypt 72-byte limit (P0 audit finding).

    Root cause:
        bcrypt 5.x hard-rejects passwords whose UTF-8 encoding exceeds 72
        bytes, raising ValueError. The spec requires no maximum password
        length.

    Fix:
        ``_prepare_password_bytes`` pre-hashes the password with SHA-256
        to produce a fixed 32-byte digest before bcrypt processing. Two
        passwords differing at any position produce different digests —
        there is no truncation at any boundary.
    """

    def test_hash_password_73_ascii_chars(self) -> None:
        """73 ASCII characters = 73 bytes — must not raise after the fix."""
        long_password = "A" * 73 + "a1!"
        result = hash_password(long_password)
        assert result.startswith("$2b$12$")

    def test_hash_password_200_chars(self) -> None:
        """A 200-character passphrase must hash without error."""
        passphrase = "correct horse battery staple " * 7  # 203 chars
        result = hash_password(passphrase)
        assert result.startswith("$2b$12$")

    def test_verify_password_over_72_bytes(self) -> None:
        """Round-trip verify must work correctly for passwords over 72 bytes."""
        long_password = "B" * 80 + "b1!"
        h = hash_password(long_password)
        assert verify_password(long_password, h) is True
        assert verify_password("B" * 80 + "b1?", h) is False  # one char different

    def test_no_truncation_beyond_72_bytes(self) -> None:
        """
        Two passwords differing only at byte 73 must NOT match each other's hash.

        Before the fix, bcrypt would silently truncate both passwords to 72
        bytes so ``"A" * 73`` and ``"A" * 72 + "B"`` produced the *same* hash.
        With SHA-256 pre-hashing, they produce different digests and therefore
        different hashes.
        """
        pw_a = "A" * 73
        pw_b = "A" * 72 + "B"  # differs only at position 73
        h_a = hash_password(pw_a)
        # pw_b must NOT verify against pw_a's hash
        assert verify_password(pw_a, h_a) is True
        assert verify_password(pw_b, h_a) is False

    def test_hash_multibyte_unicode(self) -> None:
        """
        Multibyte Unicode password must hash and verify correctly.

        'é' encodes to 2 bytes in UTF-8, so 40 × 'é' = 80 UTF-8 bytes.
        Adding 'A1!' makes 83 bytes — well over bcrypt's 72-byte limit
        without the prehash fix.
        """
        unicode_password = "é" * 40 + "A1!"  # 83 UTF-8 bytes
        result = hash_password(unicode_password)
        assert result.startswith("$2b$12$")
        assert verify_password(unicode_password, result) is True

    def test_hash_emoji_password(self) -> None:
        """
        Emoji characters (4 bytes each in UTF-8) must hash without error.

        20 × '🔐' = 80 UTF-8 bytes; adding 'Aa1!' makes 84 bytes.
        """
        emoji_password = "🔐" * 20 + "Aa1!"  # 84 UTF-8 bytes
        result = hash_password(emoji_password)
        assert result.startswith("$2b$12$")
        assert verify_password(emoji_password, result) is True


# ─── Password policy ─────────────────────────────────────────────────────────


class TestPasswordComplexity:
    """Spec Part 2, Section 8.3 — Password Policy."""

    def test_valid_password_passes(self) -> None:
        validate_password_complexity("SecurePass1!")  # should not raise

    def test_too_short_raises(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_complexity("Short1!")
        assert any("12 characters" in v for v in exc_info.value.violations)

    def test_missing_uppercase_raises(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_complexity("alllowercase1!")
        assert any("uppercase" in v.lower() for v in exc_info.value.violations)

    def test_missing_lowercase_raises(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_complexity("ALLUPPERCASE1!")
        assert any("lowercase" in v.lower() for v in exc_info.value.violations)

    def test_missing_digit_raises(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_complexity("NoDigitsHere!!")
        assert any("digit" in v.lower() for v in exc_info.value.violations)

    def test_missing_special_char_raises(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_complexity("NoSpecialChar1")
        assert any("special" in v.lower() for v in exc_info.value.violations)

    def test_multiple_violations_reported_together(self) -> None:
        """All violations returned at once, not just the first one."""
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_complexity("short")
        assert len(exc_info.value.violations) >= 3

    def test_no_maximum_length(self) -> None:
        """Spec explicitly states no maximum password length."""
        long_password = "A" * 500 + "a1!"
        validate_password_complexity(long_password)  # should not raise

    def test_exactly_12_chars_passes(self) -> None:
        validate_password_complexity("Abcdefgh1!xy")  # exactly 12


# ─── HIBP check ───────────────────────────────────────────────────────────────


class TestHIBPCheck:
    """
    Spec Part 2, Section 8.2, Decision 2 — k-anonymity HIBP check.

    Network is always mocked — these are unit tests.
    """

    @pytest.mark.anyio
    async def test_pwned_password_returns_true(self) -> None:
        """If HIBP returns a matching suffix, must return True."""
        import hashlib

        password = "password123"
        sha1 = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
        suffix = sha1[5:]

        mock_response = MagicMock()
        mock_response.text = f"{suffix}:12345\nOTHERSUFFIX:1\n"
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=mock_response))
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await check_hibp_password(password)

        assert result is True

    @pytest.mark.anyio
    async def test_clean_password_returns_false(self) -> None:
        """If suffix is not in HIBP response, must return False."""
        mock_response = MagicMock()
        mock_response.text = "AAAAABBBBBCCCCC:1\nDDDDDEEEEEFFFFF:1\n"
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=mock_response))
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await check_hibp_password("UniquePassword999!")

        assert result is False

    @pytest.mark.anyio
    async def test_network_error_returns_false(self) -> None:
        """Network failure must return False (fail open), not raise."""
        import httpx as _httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                side_effect=_httpx.ConnectError("connection refused")
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await check_hibp_password("AnyPassword1!")

        assert result is False


# ─── JWT access token ─────────────────────────────────────────────────────────


class TestJWTAccessToken:
    """Spec Part 2, Section 8.3 — JWT payload and token lifecycle."""

    def test_create_returns_token_and_jti(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        token, jti = create_access_token(
            sample_user_id, sample_tenant_id, "analyst", settings=mock_settings
        )
        assert isinstance(token, str)
        assert len(token) > 0
        assert isinstance(jti, str)
        assert len(jti) == 36  # UUID4 string length

    def test_token_contains_expected_claims(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        from jose import jwt as jose_jwt

        token, jti = create_access_token(
            sample_user_id, sample_tenant_id, "owner", settings=mock_settings
        )
        decoded = jose_jwt.decode(
            token, mock_settings.jwt_secret, algorithms=[mock_settings.jwt_algorithm]
        )
        assert decoded["sub"] == str(sample_user_id)
        assert decoded["tid"] == str(sample_tenant_id)
        assert decoded["role"] == "owner"
        assert decoded["jti"] == jti
        assert decoded["type"] == "access"
        assert "exp" in decoded
        assert "iat" in decoded

    def test_expiry_is_15_minutes(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        from jose import jwt as jose_jwt

        token, _ = create_access_token(
            sample_user_id, sample_tenant_id, "analyst", settings=mock_settings
        )
        decoded = jose_jwt.decode(
            token, mock_settings.jwt_secret, algorithms=[mock_settings.jwt_algorithm]
        )
        issued = datetime.fromtimestamp(decoded["iat"], tz=UTC)
        expiry = datetime.fromtimestamp(decoded["exp"], tz=UTC)
        delta = expiry - issued
        # Allow 2-second tolerance for execution time
        assert timedelta(minutes=14, seconds=58) <= delta <= timedelta(minutes=15, seconds=2)

    def test_verify_valid_token_returns_payload(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        token, jti = create_access_token(
            sample_user_id, sample_tenant_id, "admin", settings=mock_settings
        )
        payload = verify_access_token(token, settings=mock_settings)
        assert isinstance(payload, TokenPayload)
        assert payload.sub == sample_user_id
        assert payload.tid == sample_tenant_id
        assert payload.role == "admin"
        assert payload.jti == jti
        assert payload.type == "access"

    def test_verify_expired_token_raises(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        from jose import jwt as jose_jwt

        payload = {
            "sub": str(sample_user_id),
            "tid": str(sample_tenant_id),
            "role": "analyst",
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
            "iat": datetime.now(UTC) - timedelta(minutes=16),
            "type": "access",
        }
        expired_token = jose_jwt.encode(
            payload, mock_settings.jwt_secret, algorithm=mock_settings.jwt_algorithm
        )
        with pytest.raises(UnauthorizedError) as exc_info:
            verify_access_token(expired_token, settings=mock_settings)
        assert "expired" in exc_info.value.message.lower()

    def test_verify_wrong_secret_raises(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        token, _ = create_access_token(
            sample_user_id, sample_tenant_id, "viewer", settings=mock_settings
        )
        wrong_settings = MagicMock()
        wrong_settings.jwt_secret = "completely-different-secret-for-test"
        wrong_settings.jwt_algorithm = "HS256"
        with pytest.raises(UnauthorizedError):
            verify_access_token(token, settings=wrong_settings)

    def test_verify_malformed_token_raises(self, mock_settings: MagicMock) -> None:
        with pytest.raises(UnauthorizedError):
            verify_access_token("not.a.jwt", settings=mock_settings)

    def test_verify_rejects_wrong_type(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        """A token with type != 'access' must be rejected."""
        from jose import jwt as jose_jwt

        payload = {
            "sub": str(sample_user_id),
            "tid": str(sample_tenant_id),
            "role": "analyst",
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(UTC) + timedelta(days=30),
            "iat": datetime.now(UTC),
            "type": "refresh",  # <-- wrong type
        }
        wrong_type_token = jose_jwt.encode(
            payload, mock_settings.jwt_secret, algorithm=mock_settings.jwt_algorithm
        )
        with pytest.raises(UnauthorizedError):
            verify_access_token(wrong_type_token, settings=mock_settings)

    def test_each_token_has_unique_jti(
        self, mock_settings: MagicMock, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        """Every token must have a globally unique jti."""
        tokens = [
            create_access_token(sample_user_id, sample_tenant_id, "analyst", settings=mock_settings)
            for _ in range(10)
        ]
        jtis = [jti for _, jti in tokens]
        assert len(set(jtis)) == 10, "Duplicate jti values detected"


# ─── Refresh token ───────────────────────────────────────────────────────────


class TestRefreshToken:
    """Spec Part 2, Section 8.2, Decision 1 — opaque 256-bit refresh token."""

    def test_raw_token_is_64_hex_chars(self) -> None:
        token = generate_raw_refresh_token()
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_raw_tokens_are_unique(self) -> None:
        tokens = [generate_raw_refresh_token() for _ in range(20)]
        assert len(set(tokens)) == 20

    def test_hash_is_64_hex_chars(self) -> None:
        raw = generate_raw_refresh_token()
        h = hash_refresh_token(raw)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_raw_produces_same_hash(self) -> None:
        raw = generate_raw_refresh_token()
        assert hash_refresh_token(raw) == hash_refresh_token(raw)

    def test_different_raws_produce_different_hashes(self) -> None:
        r1 = generate_raw_refresh_token()
        r2 = generate_raw_refresh_token()
        assert hash_refresh_token(r1) != hash_refresh_token(r2)

    def test_expiry_is_30_days(self, mock_settings: MagicMock) -> None:
        before = datetime.now(UTC)
        expiry = generate_refresh_token_expiry(settings=mock_settings)
        after = datetime.now(UTC)
        lower_bound = before + timedelta(days=29, hours=23)
        upper_bound = after + timedelta(days=30, seconds=5)
        assert lower_bound < expiry < upper_bound


# ─── Password reset token ─────────────────────────────────────────────────────


class TestPasswordResetToken:
    def test_token_is_url_safe_base64(self) -> None:
        token = generate_password_reset_token()
        import base64 as _b64

        # Should not raise
        decoded = _b64.urlsafe_b64decode(token + "==")
        assert len(decoded) == 36  # 288 bits / 8

    def test_tokens_are_unique(self) -> None:
        tokens = [generate_password_reset_token() for _ in range(20)]
        assert len(set(tokens)) == 20

    def test_hash_is_deterministic(self) -> None:
        token = generate_password_reset_token()
        assert hash_password_reset_token(token) == hash_password_reset_token(token)

    def test_hash_differs_between_tokens(self) -> None:
        t1 = generate_password_reset_token()
        t2 = generate_password_reset_token()
        assert hash_password_reset_token(t1) != hash_password_reset_token(t2)


# ─── TOTP ─────────────────────────────────────────────────────────────────────


class TestTOTP:
    """Spec Part 2, Section 8.2, Decision 4 — TOTP AES-256-GCM encryption."""

    def test_generate_secret_is_valid_base32(self) -> None:
        secret = generate_totp_secret()
        assert isinstance(secret, str)
        assert len(secret) == 32
        # base32 alphabet only
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret)

    def test_secrets_are_unique(self) -> None:
        secrets_ = [generate_totp_secret() for _ in range(20)]
        assert len(set(secrets_)) == 20

    def test_provisioning_uri_format(self) -> None:
        secret = generate_totp_secret()
        uri = get_totp_provisioning_uri(secret, "user@example.com")
        assert uri.startswith("otpauth://totp/")
        assert "user%40example.com" in uri or "user@example.com" in uri
        assert "Financial%20Data%20Hub" in uri or "Financial Data Hub" in uri

    def test_encrypt_decrypt_roundtrip(self, mock_settings: MagicMock) -> None:
        secret = generate_totp_secret()
        encrypted = encrypt_totp_secret(secret, settings=mock_settings)
        decrypted = decrypt_totp_secret(encrypted, settings=mock_settings)
        assert decrypted == secret

    def test_encrypt_produces_unique_ciphertext(self, mock_settings: MagicMock) -> None:
        """Each encryption of the same secret must produce different ciphertext (fresh nonce)."""
        secret = generate_totp_secret()
        c1 = encrypt_totp_secret(secret, settings=mock_settings)
        c2 = encrypt_totp_secret(secret, settings=mock_settings)
        assert c1 != c2

    def test_decrypt_wrong_key_raises(self, mock_settings: MagicMock) -> None:
        secret = generate_totp_secret()
        encrypted = encrypt_totp_secret(secret, settings=mock_settings)

        wrong_settings = MagicMock()
        wrong_settings.secret_key = "totally-different-key-00000000000"
        with pytest.raises(ValueError):
            decrypt_totp_secret(encrypted, settings=wrong_settings)

    def test_decrypt_truncated_input_raises(self, mock_settings: MagicMock) -> None:
        with pytest.raises(ValueError):
            decrypt_totp_secret("dG9vc2hvcnQ=", settings=mock_settings)

    def test_decrypt_garbage_input_raises(self, mock_settings: MagicMock) -> None:
        with pytest.raises(ValueError):
            decrypt_totp_secret("!!NOT-BASE64!!", settings=mock_settings)

    def test_verify_totp_correct_code(self, mock_settings: MagicMock) -> None:
        import pyotp as _pyotp

        secret = generate_totp_secret()
        encrypted = encrypt_totp_secret(secret, settings=mock_settings)
        code = _pyotp.TOTP(secret).now()
        assert verify_totp_code(encrypted, code, settings=mock_settings) is True

    def test_verify_totp_wrong_code(self, mock_settings: MagicMock) -> None:
        secret = generate_totp_secret()
        encrypted = encrypt_totp_secret(secret, settings=mock_settings)
        assert verify_totp_code(encrypted, "000000", settings=mock_settings) is False

    def test_verify_totp_bad_encrypted_secret(self, mock_settings: MagicMock) -> None:
        """Bad encrypted secret must return False, not raise."""
        assert verify_totp_code("!!garbage!!", "123456", settings=mock_settings) is False


# ─── TokenPayload model ───────────────────────────────────────────────────────


class TestTokenPayload:
    """Pydantic model validation for JWT payload parsing."""

    def _valid_raw(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            "sub": str(user_id),
            "tid": str(tenant_id),
            "role": "analyst",
            "jti": str(uuid.uuid4()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "iat": int(now.timestamp()),
            "type": "access",
        }

    def test_valid_payload_parses(
        self, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        payload = TokenPayload.model_validate(self._valid_raw(sample_user_id, sample_tenant_id))
        assert payload.sub == sample_user_id
        assert payload.tid == sample_tenant_id
        assert payload.type == "access"

    def test_exp_coerced_to_datetime(
        self, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        payload = TokenPayload.model_validate(self._valid_raw(sample_user_id, sample_tenant_id))
        assert isinstance(payload.exp, datetime)
        assert payload.exp.tzinfo is not None

    def test_wrong_type_rejected(
        self, sample_user_id: uuid.UUID, sample_tenant_id: uuid.UUID
    ) -> None:
        from pydantic import ValidationError

        raw = self._valid_raw(sample_user_id, sample_tenant_id)
        raw["type"] = "refresh"
        with pytest.raises(ValidationError):
            TokenPayload.model_validate(raw)
