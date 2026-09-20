import io
import unittest
from datetime import datetime, timezone

from fake_auth_backend import FakeAuthBackend
from scripts.bootstrap_super_admin import main


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)


def server_env(**extra):
    values = {
        'APP_ENV': 'production',
        'AUTH_REQUIRED': '1',
        'SUPABASE_URL': 'https://project.example.test',
        'SUPABASE_SERVICE_ROLE_KEY': 'test-service-role',
        'REGISTRATION_INVITE_CODE': 'test-invite',
        'APP_SESSION_SECRET': 'test-session-secret',
        'BOOTSTRAP_PASSWORD': 'password8',
    }
    values.update(extra)
    return values


class BootstrapSuperAdminTests(unittest.TestCase):
    def run_command(self, backend, *args, environ=None, getpass_fn=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            list(args),
            environ=environ or server_env(),
            backend=backend,
            stdout=stdout,
            stderr=stderr,
            getpass_fn=getpass_fn,
            clock=lambda: NOW,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_new_email_creates_active_super_admin_without_browser_session(self):
        backend = FakeAuthBackend()
        code, output, error = self.run_command(
            backend,
            '--email', 'Boss@Example.com',
            '--username', 'Boss',
            '--password-env', 'BOOTSTRAP_PASSWORD',
            '--reason', '首次建立超级管理员',
        )

        self.assertEqual(0, code, error)
        profile = backend.get_profile_by_email('boss@example.com')
        self.assertEqual(('super_admin', 'active'), (profile.role, profile.status))
        self.assertEqual({}, backend.sessions)
        self.assertIn(profile.id, output)
        self.assertNotIn('password8', output + error)

    def test_new_supabase_secret_key_can_bootstrap_without_legacy_key(self):
        backend = FakeAuthBackend()
        env = server_env(
            SUPABASE_SECRET_KEY='sb_secret_test',
            SUPABASE_SERVICE_ROLE_KEY='',
        )

        code, output, error = self.run_command(
            backend,
            '--email', 'boss@example.com',
            '--username', 'Boss',
            '--password-env', 'BOOTSTRAP_PASSWORD',
            '--reason', '首次建立超级管理员',
            environ=env,
        )

        self.assertEqual(0, code, error)
        profile = backend.get_profile_by_email('boss@example.com')
        self.assertIsNotNone(profile)
        self.assertEqual('super_admin', profile.role)
        self.assertIn(profile.id, output)

    def test_existing_active_super_admin_is_idempotent(self):
        backend = FakeAuthBackend()
        existing = backend.add_user(
            username='Boss', email='boss@example.com',
            role='super_admin', status='active',
        )

        code, output, error = self.run_command(
            backend, '--email', 'boss@example.com',
        )

        self.assertEqual(0, code, error)
        self.assertIn(existing.id, output)
        self.assertEqual([], backend.admin_calls)
        self.assertEqual(1, len(backend.profiles))

    def test_existing_member_is_refused_without_explicit_promotion(self):
        backend = FakeAuthBackend()
        member = backend.add_user(
            username='Member', email='member@example.com',
            role='member', status='active',
        )

        code, output, error = self.run_command(
            backend, '--email', 'member@example.com',
        )

        self.assertNotEqual(0, code)
        self.assertEqual('member', backend.profiles[member.id].role)
        self.assertIn('promote-existing', error)
        self.assertEqual('', output)

    def test_explicit_existing_promotion_requires_reason_and_is_audited(self):
        backend = FakeAuthBackend()
        member = backend.add_user(
            username='Member', email='member@example.com',
            role='member', status='active',
        )

        code, _, error = self.run_command(
            backend,
            '--email', 'member@example.com',
            '--promote-existing',
        )
        self.assertNotEqual(0, code)
        self.assertIn('reason', error.lower())

        code, output, error = self.run_command(
            backend,
            '--email', 'member@example.com',
            '--promote-existing',
            '--reason', '所有者明确批准',
        )
        self.assertEqual(0, code, error)
        self.assertIn(member.id, output)
        self.assertEqual('super_admin', backend.profiles[member.id].role)
        self.assertEqual('active', backend.profiles[member.id].status)
        self.assertIn(
            ('bootstrap_super_admin', member.id, '所有者明确批准'),
            backend.admin_calls,
        )

    def test_missing_server_configuration_exits_without_printing_secrets(self):
        backend = FakeAuthBackend()
        env = server_env(
            SUPABASE_URL='',
            SUPABASE_SERVICE_ROLE_KEY='do-not-print-this',
        )
        code, output, error = self.run_command(
            backend,
            '--email', 'boss@example.com',
            '--username', 'Boss',
            '--password-env', 'BOOTSTRAP_PASSWORD',
            environ=env,
        )

        self.assertNotEqual(0, code)
        self.assertNotIn('do-not-print-this', output + error)
        self.assertNotIn('password8', output + error)

    def test_password_can_come_from_getpass_but_not_plain_argument(self):
        backend = FakeAuthBackend()
        code, output, error = self.run_command(
            backend,
            '--email', 'boss@example.com',
            '--username', 'Boss',
            '--reason', '首次建立超级管理员',
            getpass_fn=lambda prompt: 'password8',
        )
        self.assertEqual(0, code, error)
        self.assertNotIn('password8', output + error)

        with self.assertRaises(SystemExit):
            main([
                '--email', 'next@example.com', '--username', 'Next',
                '--password', 'never-on-command-line',
            ], backend=backend, environ=server_env())


if __name__ == '__main__':
    unittest.main()
