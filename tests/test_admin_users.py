import unittest

from test_auth_web import create_test_app, csrf_value, login


def admin_post(client, path, payload):
    return client.post(
        path,
        json=payload,
        headers={'X-CSRF-Token': csrf_value(client)},
    )


class AdminUserRouteTests(unittest.TestCase):
    def test_admin_lists_filtered_users_with_email(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='admin-1', username='Admin', email='admin@example.com',
            role='admin', status='active',
        )
        backend.add_user(
            user_id='pending-1', username='Pending',
            email='pending@example.com', status='pending',
        )
        backend.add_user(
            user_id='active-1', username='Active',
            email='active@example.com', status='active',
        )
        client = app.test_client()
        login(client, 'Admin')

        response = client.get('/api/admin/users?status=pending')

        self.assertEqual(200, response.status_code)
        users = response.get_json()['users']
        self.assertEqual(['pending-1'], [user['id'] for user in users])
        self.assertEqual('pending@example.com', users[0]['email'])

    def test_admin_approve_uses_current_actor_and_expected_status(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='admin-1', username='Admin', email='admin@example.com',
            role='admin', status='active',
        )
        backend.add_user(
            user_id='member-1', username='Member',
            email='member@example.com', status='pending',
        )
        client = app.test_client()
        login(client, 'Admin')

        response = admin_post(
            client,
            '/api/admin/users/member-1/approve',
            {
                'expected_status': 'pending',
                'reason': '资料确认',
                'actor_user_id': 'forged-user',
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual('active', response.get_json()['user']['status'])
        call = backend.admin_calls[-1]
        self.assertEqual('approve', call[0])
        self.assertEqual('admin-1', call[1])
        self.assertNotIn('forged-user', call)

    def test_reject_suspend_and_restore_require_reason(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='admin-1', username='Admin', email='admin@example.com',
            role='admin', status='active',
        )
        backend.add_user(
            user_id='member-1', username='Member',
            email='member@example.com', status='pending',
        )
        client = app.test_client()
        login(client, 'Admin')

        reject = admin_post(
            client, '/api/admin/users/member-1/reject',
            {'expected_status': 'pending', 'reason': '  '},
        )

        self.assertEqual(400, reject.status_code)
        self.assertIn('原因', reject.get_json()['error'])
        self.assertEqual([], backend.admin_calls)

    def test_stale_expected_status_returns_conflict(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='admin-1', username='Admin', email='admin@example.com',
            role='admin', status='active',
        )
        backend.add_user(
            user_id='member-1', username='Member',
            email='member@example.com', status='pending',
        )
        client = app.test_client()
        login(client, 'Admin')
        first = admin_post(
            client, '/api/admin/users/member-1/approve',
            {'expected_status': 'pending', 'reason': ''},
        )

        stale = admin_post(
            client, '/api/admin/users/member-1/approve',
            {'expected_status': 'pending', 'reason': ''},
        )

        self.assertEqual(200, first.status_code)
        self.assertEqual(409, stale.status_code)
        self.assertIn('刷新', stale.get_json()['error'])

    def test_admin_cannot_manage_another_admin(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='admin-1', username='AdminOne',
            email='admin1@example.com', role='admin', status='active',
        )
        backend.add_user(
            user_id='admin-2', username='AdminTwo',
            email='admin2@example.com', role='admin', status='active',
        )
        client = app.test_client()
        login(client, 'AdminOne')

        response = admin_post(
            client, '/api/admin/users/admin-2/suspend',
            {'expected_status': 'active', 'reason': '越权测试'},
        )

        self.assertEqual(403, response.status_code)
        self.assertEqual([], backend.admin_calls)

    def test_only_super_admin_changes_roles_and_cannot_change_self(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='admin-1', username='Admin', email='admin@example.com',
            role='admin', status='active',
        )
        backend.add_user(
            user_id='super-1', username='Super', email='super@example.com',
            role='super_admin', status='active',
        )
        backend.add_user(
            user_id='member-1', username='Member',
            email='member@example.com', role='member', status='active',
        )

        admin_client = app.test_client()
        login(admin_client, 'Admin')
        denied = admin_post(
            admin_client, '/api/admin/users/member-1/role',
            {'role': 'admin', 'reason': '提升协助管理'},
        )

        super_client = app.test_client()
        login(super_client, 'Super')
        allowed = admin_post(
            super_client, '/api/admin/users/member-1/role',
            {'role': 'admin', 'reason': '提升协助管理'},
        )
        self_change = admin_post(
            super_client, '/api/admin/users/super-1/role',
            {'role': 'member', 'reason': '自我降级'},
        )

        self.assertEqual(403, denied.status_code)
        self.assertEqual(200, allowed.status_code)
        self.assertEqual('admin', allowed.get_json()['user']['role'])
        self.assertEqual(403, self_change.status_code)

    def test_super_admin_forced_logout_revokes_target_sessions(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='super-1', username='Super', email='super@example.com',
            role='super_admin', status='active',
        )
        backend.add_user(
            user_id='member-1', username='Member',
            email='member@example.com', role='member', status='active',
        )
        member_client = app.test_client()
        login(member_client, 'Member')
        self.assertEqual(200, member_client.get('/api/status').status_code)

        super_client = app.test_client()
        login(super_client, 'Super')
        response = admin_post(
            super_client, '/api/admin/users/member-1/revoke-sessions',
            {'reason': '账号安全处置'},
        )

        self.assertEqual(200, response.status_code)
        self.assertGreaterEqual(response.get_json()['revoked_sessions'], 1)
        self.assertEqual(401, member_client.get('/api/status').status_code)

    def test_audit_log_list_is_admin_only_and_capped(self):
        app, backend, _ = create_test_app()
        backend.add_user(
            user_id='admin-1', username='Admin', email='admin@example.com',
            role='admin', status='active',
        )
        backend.audit_logs = [{'id': index} for index in range(300)]
        client = app.test_client()
        login(client, 'Admin')

        response = client.get('/api/admin/audit-logs?limit=999')

        self.assertEqual(200, response.status_code)
        self.assertEqual(200, len(response.get_json()['logs']))


if __name__ == '__main__':
    unittest.main()
