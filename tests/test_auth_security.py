import base64
import unittest

from kekemi_auth.config import AuthConfig
from kekemi_auth.security import (
    constant_time_invite_matches,
    hash_ip,
    hash_token,
    new_session_credentials,
    normalize_email,
    normalize_username,
    validate_password,
)


def _decode_urlsafe(value):
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class AuthSecurityTests(unittest.TestCase):
    def test_username_is_nfkc_normalized_and_casefolded(self):
        display, normalized = normalize_username('  ＫｅＫｅＭｉ  ')

        self.assertEqual('KeKeMi', display)
        self.assertEqual('kekemi', normalized)

    def test_username_rejects_invisible_or_out_of_range_values(self):
        for value in ('\u200b\u200b', 'a', 'x' * 25, 'ray\nadmin'):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    normalize_username(value)

    def test_email_is_trimmed_lowercase_and_requires_normal_shape(self):
        self.assertEqual('ray@example.com', normalize_email(' Ray@Example.COM '))

        for value in ('', 'missing-at.example.com', 'ray@', '@example.com'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_email(value)

    def test_password_requires_at_least_eight_characters(self):
        with self.assertRaises(ValueError):
            validate_password('1234567')

        self.assertEqual('12345678', validate_password('12345678'))

    def test_session_uses_independent_256_bit_secrets_and_stores_hashes(self):
        credentials = new_session_credentials()

        self.assertEqual(32, len(_decode_urlsafe(credentials.token)))
        self.assertEqual(32, len(_decode_urlsafe(credentials.csrf_token)))
        self.assertNotEqual(credentials.token, credentials.csrf_token)
        self.assertEqual(hash_token(credentials.token), credentials.token_hash)
        self.assertEqual(hash_token(credentials.csrf_token), credentials.csrf_hash)
        self.assertNotEqual(credentials.token, credentials.token_hash)

    def test_invite_comparison_and_ip_hash_do_not_expose_plain_values(self):
        self.assertTrue(constant_time_invite_matches('kekemi', 'kekemi'))
        self.assertFalse(constant_time_invite_matches('Kekemi', 'kekemi'))
        self.assertFalse(constant_time_invite_matches('', 'kekemi'))

        first = hash_ip('203.0.113.9', 'secret-a')
        self.assertEqual(first, hash_ip('203.0.113.9', 'secret-a'))
        self.assertNotEqual(first, hash_ip('203.0.113.9', 'secret-b'))
        self.assertNotIn('203.0.113.9', first)

    def test_production_rejects_disabled_or_incomplete_auth(self):
        with self.assertRaisesRegex(RuntimeError, 'production'):
            AuthConfig.from_env({'APP_ENV': 'production', 'AUTH_REQUIRED': '0'})

        with self.assertRaisesRegex(RuntimeError, 'SUPABASE_SECRET_KEY'):
            AuthConfig.from_env({
                'APP_ENV': 'production',
                'AUTH_REQUIRED': '1',
                'SUPABASE_URL': 'https://example.supabase.co',
                'REGISTRATION_INVITE_CODE': 'invite',
                'APP_SESSION_SECRET': 'session-secret',
            })

    def test_railway_environment_cannot_fall_back_to_disabled_development_auth(self):
        with self.assertRaisesRegex(RuntimeError, 'production'):
            AuthConfig.from_env({
                'RAILWAY_ENVIRONMENT': 'production',
                'AUTH_REQUIRED': '0',
            })

    def test_new_supabase_secret_key_is_supported_and_preferred(self):
        base = {
            'AUTH_REQUIRED': '1',
            'SUPABASE_URL': 'https://example.supabase.co',
            'SUPABASE_SECRET_KEY': 'sb_secret_new',
            'REGISTRATION_INVITE_CODE': 'invite',
            'APP_SESSION_SECRET': 'session-secret',
        }

        config = AuthConfig.from_env(base)
        preferred = AuthConfig.from_env({
            **base,
            'SUPABASE_SERVICE_ROLE_KEY': 'legacy-service-role',
        })

        self.assertEqual('sb_secret_new', config.service_role_key)
        self.assertEqual('sb_secret_new', preferred.service_role_key)

    def test_legacy_supabase_service_role_key_remains_compatible(self):
        config = AuthConfig.from_env({
            'AUTH_REQUIRED': '1',
            'SUPABASE_URL': 'https://example.supabase.co',
            'SUPABASE_SERVICE_ROLE_KEY': 'legacy-service-role',
            'REGISTRATION_INVITE_CODE': 'invite',
            'APP_SESSION_SECRET': 'session-secret',
        })

        self.assertEqual('legacy-service-role', config.service_role_key)

    def test_development_defaults_to_disabled_and_uses_fixed_session_windows(self):
        config = AuthConfig.from_env({})

        self.assertFalse(config.required)
        self.assertEqual('development', config.app_env)
        self.assertEqual(30 * 86400, config.app_session_seconds)
        self.assertEqual(24 * 3600, config.status_session_seconds)


if __name__ == '__main__':
    unittest.main()
