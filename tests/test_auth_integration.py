import unittest
import unittest.mock  # load asyncio before dy_util wraps subprocess.Popen
from datetime import datetime, timezone

from fake_auth_backend import FakeAuthBackend
from kekemi_auth.config import AuthConfig
from kekemi_auth.rate_limit import FixedWindowRateLimiter
from kekemi_auth.service import AuthService
from kekemi_auth.types import AuthBackendUnavailable
from kekemi_auth.web import classify_access

import web_listener


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
CONFIG = AuthConfig.from_env({
    'AUTH_REQUIRED': '1',
    'SUPABASE_URL': 'https://example.supabase.co',
    'SUPABASE_SERVICE_ROLE_KEY': 'service-role',
    'REGISTRATION_INVITE_CODE': 'kekemi',
    'APP_SESSION_SECRET': 'session-secret',
})


class UnavailableService:
    def authenticate_session(self, raw_token):
        raise AuthBackendUnavailable('认证服务暂时不可用，请稍后重试')


class AuthIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = web_listener.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.extension = self.app.extensions['kekemi_auth']
        self.original = dict(self.extension)
        self.backend = FakeAuthBackend()
        self.service = AuthService(CONFIG, self.backend, clock=lambda: NOW)
        self.extension.update({
            'config': CONFIG,
            'service': self.service,
            'limiter': FixedWindowRateLimiter(),
        })

    def tearDown(self):
        self.extension.clear()
        self.extension.update(self.original)

    def test_real_route_map_has_no_unclassified_business_route(self):
        route_policies = {}
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == 'static':
                continue
            for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
                path = str(rule)
                policy = classify_access(method, path)
                route_policies[(method, path)] = policy
                self.assertIn(policy, {'public', 'status', 'app', 'admin', 'super_admin'})

        for (method, path), policy in route_policies.items():
            if path.startswith('/api/admin/') or path == '/admin':
                self.assertEqual('admin', policy, (method, path))
            elif path.startswith('/api/auth/'):
                expected = 'status' if path in {
                    '/api/auth/me', '/api/auth/logout', '/api/auth/resubmit'
                } else 'public'
                self.assertEqual(expected, policy, (method, path))
            elif path.startswith('/api/'):
                if (method, path) in {
                    ('POST', '/api/resolve'),
                    ('POST', '/api/start'),
                    ('POST', '/api/stop'),
                    ('POST', '/api/stop_all'),
                }:
                    expected = 'super_admin'
                elif (method, path) == ('POST', '/api/toggle_record_all'):
                    expected = 'admin'
                else:
                    expected = 'app'
                self.assertEqual(expected, policy, (method, path))
            elif path.startswith('/stream/'):
                self.assertEqual('app', policy, (method, path))

    def test_real_app_requires_login_and_member_cannot_start_listener(self):
        self.backend.add_user(status='active', role='member')
        client = self.app.test_client()

        self.assertEqual(401, client.get('/api/status').status_code)
        login = client.post('/api/auth/login', json={
            'identifier': 'Ray', 'password': 'password8'
        })
        self.assertEqual(200, login.status_code)
        self.assertEqual(200, client.get('/api/status').status_code)
        self.assertEqual(
            403,
            client.post('/api/start', json={'room_id': 'room-a'}).status_code,
        )

    def test_auth_backend_outage_never_stops_existing_listeners(self):
        class SentinelListener:
            running = True

        original_listeners = web_listener.listeners
        sentinel = SentinelListener()
        web_listener.listeners = {'room-a': sentinel}
        self.extension['service'] = UnavailableService()
        client = self.app.test_client()
        client.set_cookie('kekemi_session', 'any-token')
        try:
            response = client.get('/api/status')

            self.assertEqual(503, response.status_code)
            self.assertTrue(sentinel.running)
            self.assertIs(web_listener.listeners['room-a'], sentinel)
        finally:
            web_listener.listeners = original_listeners

    def test_member_index_hides_listener_and_admin_controls_server_side(self):
        self.backend.add_user(
            user_id='member-1', username='Member',
            email='member@example.com', role='member', status='active',
        )
        self.backend.add_user(
            user_id='admin-1', username='Admin',
            email='admin@example.com', role='admin', status='active',
        )
        member_client = self.app.test_client()
        member_client.post('/api/auth/login', json={
            'identifier': 'Member', 'password': 'password8'
        })
        admin_client = self.app.test_client()
        admin_client.post('/api/auth/login', json={
            'identifier': 'Admin', 'password': 'password8'
        })

        member_html = member_client.get('/').get_data(as_text=True)
        admin_html = admin_client.get('/').get_data(as_text=True)

        self.assertNotIn('id="input"', member_html)
        self.assertNotIn('id="modeGiftLibrary"', member_html)
        self.assertNotIn('id="modeHostWeekly"', member_html)
        self.assertIn('data-current-role="member"', member_html)
        self.assertIn('id="accountLogout"', member_html)
        self.assertIn('id="input"', admin_html)
        self.assertIn('id="modeGiftLibrary"', admin_html)
        self.assertIn('data-current-role="admin"', admin_html)

    def test_disabled_auth_mode_keeps_existing_local_status_route(self):
        self.extension['config'] = AuthConfig.from_env({})
        self.extension['service'] = None
        client = self.app.test_client()

        response = client.get('/api/status')

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.is_json)


if __name__ == '__main__':
    unittest.main()
