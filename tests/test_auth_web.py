import unittest
from dataclasses import replace
from datetime import datetime, timezone

from flask import Flask, jsonify
from jinja2 import DictLoader

from fake_auth_backend import FakeAuthBackend
from kekemi_auth.config import AuthConfig
from kekemi_auth.security import hash_token
from kekemi_auth.service import AuthService
from kekemi_auth.web import init_auth


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
CONFIG = AuthConfig.from_env({
    'AUTH_REQUIRED': '1',
    'SUPABASE_URL': 'https://example.supabase.co',
    'SUPABASE_SERVICE_ROLE_KEY': 'service-role',
    'REGISTRATION_INVITE_CODE': 'kekemi',
    'APP_SESSION_SECRET': 'session-secret',
    'SUPABASE_RESET_REDIRECT_URL': 'https://app.example/reset-password',
})


def create_test_app(config=CONFIG):
    backend = FakeAuthBackend()
    service = AuthService(config, backend, clock=lambda: NOW)
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.jinja_loader = DictLoader({
        'login.html': 'login',
        'register.html': 'register',
        'account_status.html': 'status {{ current_user.status }}',
        'reset_password.html': 'reset',
        'admin.html': 'admin {{ current_user.role }}',
    })

    @app.get('/')
    def index():
        return 'index'

    @app.get('/api/status')
    def status():
        return jsonify(success=True)

    @app.post('/api/start')
    def start():
        return jsonify(success=True)

    @app.post('/api/stop')
    def stop():
        return jsonify(success=True)

    @app.get('/api/admin/gifts')
    def admin_gifts():
        return jsonify(success=True)

    @app.get('/stream/<room_id>')
    def stream(room_id):
        return f'stream {room_id}'

    init_auth(app, service=service, config=config)
    return app, backend, service


def login(client, identifier='Ray', password='password8'):
    return client.post('/api/auth/login', json={
        'identifier': identifier,
        'password': password,
    })


def csrf_value(client):
    cookie = client.get_cookie('kekemi_csrf')
    return cookie.value if cookie else ''


class AuthWebTests(unittest.TestCase):
    def test_healthz_stays_public_when_authentication_is_required(self):
        app, _, _ = create_test_app()
        response = app.test_client().get('/healthz')

        self.assertEqual(200, response.status_code)
        self.assertEqual({'status': 'ok'}, response.get_json())

    def test_unauthenticated_pages_redirect_but_api_and_stream_return_json_401(self):
        app, _, _ = create_test_app()
        client = app.test_client()

        page = client.get('/')
        api = client.get('/api/status')
        stream = client.get('/stream/room-a')

        self.assertEqual(302, page.status_code)
        self.assertIn('/login?next=/', page.headers['Location'])
        self.assertEqual(401, api.status_code)
        self.assertEqual('需要登录', api.get_json()['error'])
        self.assertEqual(401, stream.status_code)
        self.assertEqual('需要登录', stream.get_json()['error'])

    def test_pending_user_can_use_status_routes_but_not_live_data(self):
        app, backend, _ = create_test_app()
        backend.add_user(status='pending')
        client = app.test_client()
        self.assertEqual(200, login(client).status_code)

        status_page = client.get('/account-status')
        me = client.get('/api/auth/me')
        live_data = client.get('/api/status')

        self.assertEqual(200, status_page.status_code)
        self.assertIn(b'status pending', status_page.data)
        self.assertEqual('pending', me.get_json()['user']['status'])
        self.assertEqual(403, live_data.status_code)
        self.assertEqual('账号尚未获准访问', live_data.get_json()['error'])

    def test_active_member_reads_data_but_cannot_mutate_listener_or_use_admin(self):
        app, backend, _ = create_test_app()
        backend.add_user(status='active', role='member')
        client = app.test_client()
        self.assertEqual(200, login(client).status_code)

        self.assertEqual(200, client.get('/api/status').status_code)
        self.assertEqual(403, client.post('/api/start', json={}).status_code)
        self.assertEqual(403, client.get('/api/admin/gifts').status_code)
        self.assertEqual(403, client.get('/admin').status_code)

    def test_privileged_mutation_requires_matching_session_bound_csrf(self):
        # /api/start 已归 super_admin 独有，这里验的是「变更操作必须过 CSRF」
        app, backend, _ = create_test_app()
        backend.add_user(status='active', role='super_admin')
        client = app.test_client()
        self.assertEqual(200, login(client).status_code)

        missing = client.post('/api/start', json={})
        wrong = client.post(
            '/api/start', json={}, headers={'X-CSRF-Token': 'wrong'}
        )
        allowed = client.post(
            '/api/start',
            json={},
            headers={'X-CSRF-Token': csrf_value(client)},
        )

        self.assertEqual(403, missing.status_code)
        self.assertEqual('CSRF 校验失败', missing.get_json()['error'])
        self.assertEqual(403, wrong.status_code)
        self.assertEqual(200, allowed.status_code)
        self.assertEqual(200, client.get('/api/admin/gifts').status_code)
        self.assertEqual(200, client.get('/admin').status_code)

    def test_login_cookie_is_http_only_lax_thirty_days_and_secure_in_production(self):
        production = replace(CONFIG, app_env='production')
        app, backend, _ = create_test_app(production)
        backend.add_user(status='active')
        client = app.test_client()

        response = login(client)

        cookies = response.headers.getlist('Set-Cookie')
        session_cookie = next(
            value for value in cookies if value.startswith('kekemi_session=')
        )
        csrf_cookie = next(
            value for value in cookies if value.startswith('kekemi_csrf=')
        )
        self.assertIn('HttpOnly', session_cookie)
        self.assertIn('SameSite=Lax', session_cookie)
        self.assertIn('Secure', session_cookie)
        self.assertIn('Max-Age=2592000', session_cookie)
        self.assertNotIn('HttpOnly', csrf_cookie)
        self.assertIn('Secure', csrf_cookie)

    def test_logout_revokes_session_and_clears_both_cookies(self):
        app, backend, _ = create_test_app()
        backend.add_user(status='active')
        client = app.test_client()
        login(client)
        raw_token = client.get_cookie('kekemi_session').value

        response = client.post(
            '/api/auth/logout',
            headers={'X-CSRF-Token': csrf_value(client)},
        )

        self.assertEqual(200, response.status_code)
        self.assertIsNotNone(backend.sessions[hash_token(raw_token)].revoked_at)
        self.assertIsNone(client.get_cookie('kekemi_session'))
        self.assertIsNone(client.get_cookie('kekemi_csrf'))

    def test_login_rate_limit_returns_retry_after_without_account_disclosure(self):
        limited = replace(CONFIG, login_attempts=2, login_window_seconds=60)
        app, _, _ = create_test_app(limited)
        client = app.test_client()

        first = login(client, 'missing', 'wrongpass')
        second = login(client, 'missing', 'wrongpass')
        third = login(client, 'missing', 'wrongpass')

        self.assertEqual(401, first.status_code)
        self.assertEqual(401, second.status_code)
        self.assertEqual(429, third.status_code)
        self.assertGreaterEqual(int(third.headers['Retry-After']), 1)
        self.assertEqual('请求过于频繁，请稍后重试', third.get_json()['error'])

    def test_development_disabled_auth_preserves_local_super_admin_access(self):
        config = AuthConfig.from_env({})
        app = Flask(__name__)
        app.config.update(TESTING=True)

        @app.get('/api/status')
        def status():
            from flask import g
            return jsonify(role=g.current_user.role)

        @app.post('/api/start')
        def start():
            return jsonify(success=True)

        init_auth(app, config=config)
        client = app.test_client()

        self.assertEqual('super_admin', client.get('/api/status').get_json()['role'])
        self.assertEqual(200, client.post('/api/start', json={}).status_code)


if __name__ == '__main__':
    unittest.main()


class ListenerControlPermissionTests(unittest.TestCase):
    """监听的启停只归 super_admin。

    默认那几个厅是所有人共用的后台监听，一旦被停，所有人当场断流，
    且停掉期间的互动记录永久缺失、事后补不回来。因此 admin 也只能看，
    不能动监听；只有 super_admin 可以临时加别的厅。
    """

    def _client_as(self, role):
        app, backend, _ = create_test_app()
        backend.add_user(status='active', role=role)
        client = app.test_client()
        self.assertEqual(200, login(client).status_code)
        return client

    def _post(self, client, path):
        return client.post(
            path, json={}, headers={'X-CSRF-Token': csrf_value(client)}
        )

    def test_admin_cannot_control_listeners(self):
        client = self._client_as('admin')
        for path in ('/api/start', '/api/stop'):
            self.assertEqual(403, self._post(client, path).status_code, path)

    def test_super_admin_can_control_listeners(self):
        client = self._client_as('super_admin')
        for path in ('/api/start', '/api/stop'):
            self.assertEqual(200, self._post(client, path).status_code, path)

    def test_member_still_cannot_control_listeners(self):
        client = self._client_as('member')
        for path in ('/api/start', '/api/stop'):
            self.assertEqual(403, self._post(client, path).status_code, path)

    def test_admin_keeps_admin_only_pages(self):
        """admin 失去监听控制权，但礼物库这类管理页仍然要保留。"""
        client = self._client_as('admin')
        self.assertEqual(200, client.get('/api/admin/gifts').status_code)
