import unittest
from datetime import datetime, timedelta, timezone

import requests

from kekemi_auth import supabase_backend
from kekemi_auth.config import AuthConfig
from kekemi_auth.supabase_backend import SupabaseAuthBackend
from kekemi_auth.types import (
    AuthBackendError,
    AuthBackendUnavailable,
    DuplicateIdentity,
    Profile,
    StateConflict,
    StoredSession,
)


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
CONFIG = AuthConfig.from_env({
    'AUTH_REQUIRED': '1',
    'SUPABASE_URL': 'https://example.supabase.co',
    'SUPABASE_SERVICE_ROLE_KEY': 'service-role',
    'REGISTRATION_INVITE_CODE': 'invite',
    'APP_SESSION_SECRET': 'session-secret',
})


def config_with_new_secret():
    return AuthConfig.from_env({
        'AUTH_REQUIRED': '1',
        'SUPABASE_URL': 'https://example.supabase.co',
        'SUPABASE_SECRET_KEY': 'sb_secret_new',
        'REGISTRATION_INVITE_CODE': 'invite',
        'APP_SESSION_SECRET': 'session-secret',
    })


class FakeResponse:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class RecordingHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError('unexpected HTTP call')
        return self.responses.pop(0)


class TimeoutHttp:
    def request(self, method, url, **kwargs):
        raise requests.Timeout('network timeout containing no credentials')


def profile_payload(**changes):
    payload = {
        'id': 'user-1',
        'username': 'Ray',
        'username_normalized': 'ray',
        'email_normalized': 'ray@example.com',
        'role': 'member',
        'status': 'active',
        'status_reason': '',
        'approved_at': None,
        'approved_by': None,
        'suspended_at': None,
        'suspended_by': None,
        'created_at': '2026-08-17T07:00:00+00:00',
        'updated_at': '2026-08-17T07:00:00+00:00',
    }
    payload.update(changes)
    return payload


class SupabaseAuthBackendTests(unittest.TestCase):
    def test_new_secret_key_uses_apikey_without_fake_bearer_jwt(self):
        http = RecordingHttp([FakeResponse(200, {'id': 'user-1'})])
        backend = SupabaseAuthBackend(config_with_new_secret(), http=http)

        backend.create_auth_user('ray@example.com', 'password8')

        headers = http.calls[0][2]['headers']
        self.assertEqual('sb_secret_new', headers['apikey'])
        self.assertNotIn('Authorization', headers)

    def test_user_access_token_is_the_only_bearer_with_new_secret_key(self):
        http = RecordingHttp([FakeResponse(200, {'id': 'user-1'})])
        backend = SupabaseAuthBackend(config_with_new_secret(), http=http)

        backend.update_password('user.jwt.token', 'new-password8')

        headers = http.calls[0][2]['headers']
        self.assertEqual('sb_secret_new', headers['apikey'])
        self.assertEqual('Bearer user.jwt.token', headers['Authorization'])

    def test_create_auth_user_uses_server_admin_endpoint(self):
        http = RecordingHttp([FakeResponse(200, {'id': 'user-1'})])
        backend = SupabaseAuthBackend(CONFIG, http=http)

        user_id = backend.create_auth_user('ray@example.com', 'password8')

        self.assertEqual('user-1', user_id)
        method, url, kwargs = http.calls[0]
        self.assertEqual('POST', method)
        self.assertEqual(
            'https://example.supabase.co/auth/v1/admin/users', url
        )
        self.assertEqual('Bearer service-role', kwargs['headers']['Authorization'])
        self.assertEqual('service-role', kwargs['headers']['apikey'])
        self.assertEqual({
            'email': 'ray@example.com',
            'password': 'password8',
            'email_confirm': True,
        }, kwargs['json'])
        self.assertEqual(10, kwargs['timeout'])

    def test_duplicate_auth_user_maps_to_duplicate_identity(self):
        http = RecordingHttp([
            FakeResponse(422, {'code': 'email_exists', 'msg': 'already registered'})
        ])
        backend = SupabaseAuthBackend(CONFIG, http=http)

        with self.assertRaises(DuplicateIdentity):
            backend.create_auth_user('ray@example.com', 'password8')

    def test_password_failure_is_not_exposed_as_backend_error(self):
        http = RecordingHttp([
            FakeResponse(400, {'error': 'invalid_grant', 'error_description': 'bad'})
        ])
        backend = SupabaseAuthBackend(CONFIG, http=http)

        self.assertIsNone(backend.verify_password('ray@example.com', 'wrongpass'))

    def test_create_profile_uses_postgrest_and_returns_domain_profile(self):
        http = RecordingHttp([FakeResponse(201, [profile_payload(status='pending')])])
        backend = SupabaseAuthBackend(CONFIG, http=http)
        profile = Profile(
            id='user-1',
            username='Ray',
            username_normalized='ray',
            email_normalized='ray@example.com',
            role='member',
            status='pending',
        )

        created = backend.create_profile(profile)

        self.assertEqual('pending', created.status)
        method, url, kwargs = http.calls[0]
        self.assertEqual('POST', method)
        self.assertEqual(
            'https://example.supabase.co/rest/v1/user_profiles', url
        )
        self.assertEqual('return=representation', kwargs['headers']['Prefer'])
        self.assertEqual('ray', kwargs['json']['username_normalized'])
        self.assertNotIn('password', kwargs['json'])
        self.assertNotIn('created_at', kwargs['json'])
        self.assertNotIn('updated_at', kwargs['json'])

    def test_session_lookup_returns_latest_profile_not_stored_role(self):
        session_row = {
            'id': 'session-1',
            'token_hash': 'hash-1',
            'user_id': 'user-1',
            'access_level': 'app',
            'csrf_hash': 'csrf-hash',
            'created_at': '2026-08-17T07:00:00+00:00',
            'last_seen_at': '2026-08-17T07:00:00+00:00',
            'expires_at': '2026-09-16T07:00:00+00:00',
            'revoked_at': None,
            'created_ip_hash': 'ip-hash',
            'user_agent': 'browser',
        }
        http = RecordingHttp([
            FakeResponse(200, [session_row]),
            FakeResponse(200, [profile_payload(role='admin')]),
        ])
        backend = SupabaseAuthBackend(CONFIG, http=http)

        context = backend.get_session('hash-1', NOW)

        self.assertEqual('session-1', context.session.id)
        self.assertEqual('admin', context.profile.role)
        self.assertEqual('eq.hash-1', http.calls[0][2]['params']['token_hash'])
        self.assertEqual('is.null', http.calls[0][2]['params']['revoked_at'])

    def test_admin_transition_uses_transactional_rpc_and_server_actor(self):
        http = RecordingHttp([FakeResponse(200, profile_payload(status='active'))])
        backend = SupabaseAuthBackend(CONFIG, http=http)

        result = backend.transition_user(
            actor_id='admin-1',
            target_id='user-1',
            action='approve',
            expected_status='pending',
            reason='审核通过',
            ip_hash='ip-hash',
            now=NOW,
        )

        self.assertEqual('active', result.status)
        method, url, kwargs = http.calls[0]
        self.assertEqual('POST', method)
        self.assertEqual(
            'https://example.supabase.co/rest/v1/rpc/admin_transition_user', url
        )
        self.assertEqual('admin-1', kwargs['json']['p_actor_user_id'])
        self.assertEqual('user-1', kwargs['json']['p_target_user_id'])
        self.assertEqual('pending', kwargs['json']['p_expected_status'])

    def test_rpc_conflict_maps_to_state_conflict(self):
        http = RecordingHttp([
            FakeResponse(409, {'code': 'P0001', 'message': 'state_conflict'})
        ])
        backend = SupabaseAuthBackend(CONFIG, http=http)

        with self.assertRaises(StateConflict):
            backend.transition_user(
                'admin-1', 'user-1', 'approve', 'pending', '', 'ip-hash', NOW
            )

    def test_bootstrap_promotion_uses_service_role_rpc_and_reason(self):
        http = RecordingHttp([FakeResponse(200, profile_payload(
            role='super_admin', status='active',
        ))])
        backend = SupabaseAuthBackend(CONFIG, http=http)

        result = backend.bootstrap_super_admin(
            'user-1', '所有者明确批准', NOW,
        )

        self.assertEqual(('super_admin', 'active'), (result.role, result.status))
        method, url, kwargs = http.calls[0]
        self.assertEqual('POST', method)
        self.assertEqual(
            'https://example.supabase.co/rest/v1/rpc/bootstrap_promote_super_admin',
            url,
        )
        self.assertEqual({
            'p_target_user_id': 'user-1',
            'p_reason': '所有者明确批准',
            'p_now': '2026-08-17T08:00:00+00:00',
        }, kwargs['json'])

    def test_network_and_server_errors_are_retryable_without_secret_leak(self):
        with self.assertRaises(AuthBackendUnavailable) as timeout_error:
            SupabaseAuthBackend(CONFIG, http=TimeoutHttp()).get_profile_by_id('u')
        self.assertNotIn('service-role', str(timeout_error.exception))

        backend = SupabaseAuthBackend(
            CONFIG,
            http=RecordingHttp([FakeResponse(500, text='service-role internal')]),
        )
        with self.assertRaises(AuthBackendUnavailable) as server_error:
            backend.get_profile_by_id('u')
        self.assertNotIn('service-role', str(server_error.exception))

    def test_create_session_serializes_utc_timestamps(self):
        http = RecordingHttp([FakeResponse(201, [])])
        backend = SupabaseAuthBackend(CONFIG, http=http)
        session = StoredSession(
            id='session-1',
            token_hash='token-hash',
            user_id='user-1',
            access_level='app',
            csrf_hash='csrf-hash',
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(days=30),
            created_ip_hash='ip-hash',
            user_agent='browser',
        )

        backend.create_session(session)

        payload = http.calls[0][2]['json']
        self.assertEqual('2026-08-17T08:00:00+00:00', payload['created_at'])
        self.assertEqual('2026-09-16T08:00:00+00:00', payload['expires_at'])

    def test_unexpected_client_error_uses_safe_backend_exception(self):
        backend = SupabaseAuthBackend(
            CONFIG,
            http=RecordingHttp([
                FakeResponse(400, {'message': 'bad request service-role'})
            ]),
        )

        with self.assertRaises(AuthBackendError) as error:
            backend.get_profile_by_id('u')
        self.assertNotIn('service-role', str(error.exception))


if __name__ == '__main__':
    unittest.main()


class IsoFractionNormalisationTests(unittest.TestCase):
    """小数秒必须补齐到 6 位，否则生产上解不了。

    Postgres 返回微秒时会去掉末尾的 0：.725240 变成 .72524。
    Python 3.10 的 fromisoformat 只认 3 位或 6 位，5 位直接抛 ValueError。
    生产跑 3.10、本机跑 3.11，而 3.11 起 fromisoformat 放宽成任意位数——
    **所以直接断言 _datetime() 能解，在本机是必然通过的，测了等于没测。**
    这里改成直接断言补齐函数的输出字符串，与 Python 版本无关。

    2026-08-21 线上事故：kekeray 重新注册后登录，会话时间戳恰好是
    '2026-08-21T18:46:29.72524+00:00'，整站 500，任何页面都打不开。
    """

    def test_five_digit_fraction_is_padded_to_six(self):
        self.assertEqual(
            '2026-08-21T18:46:29.725240+00:00',
            supabase_backend._normalize_iso_fraction(
                '2026-08-21T18:46:29.72524+00:00'))

    def test_every_shorter_length_is_padded(self):
        for digits, expected in [
            ('7', '700000'), ('72', '720000'), ('725', '725000'),
            ('7252', '725200'), ('72524', '725240'), ('725240', '725240'),
        ]:
            self.assertEqual(
                '2026-08-21T18:46:29.%s+00:00' % expected,
                supabase_backend._normalize_iso_fraction(
                    '2026-08-21T18:46:29.%s+00:00' % digits),
                digits)

    def test_over_six_digits_is_truncated_not_rejected(self):
        """纳秒精度：截断到微秒，别抛错——宁可少一点精度也不能整站 500。"""
        self.assertEqual(
            '2026-08-21T18:46:29.725240+00:00',
            supabase_backend._normalize_iso_fraction(
                '2026-08-21T18:46:29.725240123+00:00'))

    def test_timestamp_without_fraction_is_untouched(self):
        for text in ('2026-08-21T18:46:29+00:00', '2026-08-21T18:46:29Z',
                     '2026-08-21'):
            self.assertEqual(
                text, supabase_backend._normalize_iso_fraction(text))

    def test_date_hyphens_are_not_mistaken_for_the_fraction(self):
        self.assertEqual(
            '2026-08-21T18:46:29.725240+00:00',
            supabase_backend._normalize_iso_fraction(
                '2026-08-21T18:46:29.72524+00:00'))

    def test_datetime_parses_the_timestamp_that_took_the_site_down(self):
        parsed = supabase_backend._datetime('2026-08-21T18:46:29.72524+00:00')
        self.assertEqual(2026, parsed.year)
        self.assertEqual(725240, parsed.microsecond)

    def test_datetime_still_handles_none_and_datetime_inputs(self):
        self.assertIsNone(supabase_backend._datetime(None))
        self.assertIsNone(supabase_backend._datetime(''))
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        self.assertIs(now, supabase_backend._datetime(now))
