"""실제 Argon2·JWT 검증. 네트워크·DB 없이 실행한다."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import jwt
from pydantic import SecretStr

from app.auth.config import AuthConfigurationError, auth_settings
from app.auth.security import PASSWORD_HASHER, create_access_token, decode_access_token, verify_password

TEST_KEY = "test-only-key-" + "a" * 48


class AuthSecurityTests(TestCase):
    def setUp(self):
        patcher = patch.object(auth_settings, "auth_jwt_secret", SecretStr(TEST_KEY))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.user, self.sid = uuid4(), uuid4()
        self.claims = dict(sub=str(self.user), sid=str(self.sid), iat=int(self.now.timestamp()),
                           exp=int(self.now.timestamp()) + 900, iss=auth_settings.auth_jwt_issuer,
                           aud=auth_settings.auth_jwt_audience)

    def test_password_hash_salts_and_exact_password(self):
        password = "  Test password 2026!  "
        first, second = (PASSWORD_HASHER.hash(password) for _ in range(2))
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("$argon2id$"))
        self.assertTrue(verify_password(password, first))
        for candidate in (password.strip(), "wrong", "\ud800"):
            self.assertFalse(verify_password(candidate, first))
        self.assertFalse(verify_password(password, None))
        self.assertFalse(verify_password(password, "not-a-hash"))

    def test_round_trip(self):
        token = create_access_token(self.user, self.sid, issued_at=self.now,
                                    expires_at=self.now + timedelta(minutes=15))
        decoded = decode_access_token(token)
        self.assertEqual((decoded.user_id, decoded.session_id), (self.user, self.sid))

    def test_expiry_boundary(self):
        token = jwt.encode(self.claims, TEST_KEY, algorithm="HS256")
        for offset, valid in ((899, True), (900, False), (901, False)):
            with self.subTest(offset=offset), patch("jwt.api_jwt.datetime") as clock:
                clock.now.return_value = self.now + timedelta(seconds=offset)
                if valid:
                    self.assertEqual(decode_access_token(token).user_id, self.user)
                else:
                    with self.assertRaises(jwt.ExpiredSignatureError):
                        decode_access_token(token)

    def test_rejects_bad_signature_algorithm_and_malformed_token(self):
        tokens = ["malformed", "x" * 4097,
                  jwt.encode(self.claims, "other-test-key" * 8, algorithm="HS256"),
                  jwt.encode(self.claims, TEST_KEY, algorithm="HS384"),
                  jwt.encode(self.claims, "", algorithm="none")]
        for token in tokens:
            with self.subTest(prefix=token[:8]), self.assertRaises(jwt.InvalidTokenError):
                decode_access_token(token)

    def test_rejects_missing_and_invalid_claims(self):
        variants = [{k: v for k, v in self.claims.items() if k != missing} for missing in self.claims]
        variants += [{**self.claims, key: value} for key, value in (
            ("iss", "other"), ("aud", "other"), ("aud", [auth_settings.auth_jwt_audience]),
            ("sub", "not-uuid"), ("sid", None), ("sid", 123),
            ("iat", int(self.now.timestamp()) + 300), ("iat", True),
            ("exp", str(self.claims["exp"])),
        )]
        for claims in variants:
            with self.subTest(claims=claims), self.assertRaises(jwt.InvalidTokenError):
                decode_access_token(jwt.encode(claims, TEST_KEY, algorithm="HS256"))

    def test_missing_secret_fails_closed(self):
        for key in ("", "short", " " * 64):
            with self.subTest(length=len(key)), patch.object(auth_settings, "auth_jwt_secret", SecretStr(key)):
                with self.assertRaises(AuthConfigurationError):
                    create_access_token(self.user, self.sid, issued_at=self.now,
                                        expires_at=self.now + timedelta(minutes=15))
