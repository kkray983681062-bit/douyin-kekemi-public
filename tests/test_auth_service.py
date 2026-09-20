import unittest
from datetime import datetime, timezone

from fake_auth_backend import FakeAuthBackend
from kekemi_auth.config import AuthConfig
from kekemi_auth.security import hash_token
from kekemi_auth.service import (
    AuthService,
    ProfileCreationFailed,
    PublicAuthError,
    RequestMetadata,
)
from kekemi_auth.types import AuthBackendError, DuplicateIdentity


NOW = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
CONFIG = AuthConfig.from_env({
    'AUTH_REQUIRED': '1',
    'SUPABASE_URL': 'https://example.supabase.co',
    'SUPABASE_SERVICE_ROLE_KEY': 'service-role',
    'REGISTRATION_INVITE_CODE': 'kekemi',
    'APP_SESSION_SECRET': 'session-secret',
    'SUPABASE_RESET_REDIRECT_URL': 'https://app.example/reset-password',
})
REQUEST = RequestMetadata(ip_hash='ip-hash', user_agent='test browser')


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeAuthBackend()
        self.service = AuthService(CONFIG, self.backend, clock=lambda: NOW)

    def test_registration_rejects_wrong_invite_before_creating_auth_user(self):
        with self.assertRaisesRegex(PublicAuthError, '邀请码无效'):
            self.service.register(
                'Ray', 'ray@example.com', 'password8', 'wrong', REQUEST
            )

        self.assertEqual({}, self.backend.credentials)
        self.assertEqual({}, self.backend.profiles)

    def test_registration_creates_pending_member_and_status_session(self):
        result = self.service.register(
            'Ｒａｙ', 'RAY@EXAMPLE.COM', 'password8', 'kekemi', REQUEST
        )

        self.assertEqual('Ray', result.profile.username)
        self.assertEqual('ray', result.profile.username_normalized)
        self.assertEqual('ray@example.com', result.profile.email_normalized)
        self.assertEqual('member', result.profile.role)
        self.assertEqual('pending', result.profile.status)
        self.assertEqual('status', result.session.access_level)
        self.assertEqual(24 * 3600, int(
            (result.session.expires_at - result.session.created_at).total_seconds()
        ))
        self.assertEqual(
            result.session, self.backend.sessions[result.credentials.token_hash]
        )

    def test_registration_compensates_when_profile_creation_fails(self):
        self.backend.fail_create_profile = True

        with self.assertRaises(ProfileCreationFailed):
            self.service.register(
                'Ray', 'ray@example.com', 'password8', 'kekemi', REQUEST
            )

        self.assertEqual(['generated-user-1'], self.backend.deleted_auth_users)
        self.assertEqual({}, self.backend.sessions)

    def test_registration_reports_cleanup_failure_without_hiding_original_stage(self):
        self.backend.fail_create_profile = True
        self.backend.fail_delete_auth_user = True

        with self.assertRaisesRegex(ProfileCreationFailed, '补偿删除也失败'):
            self.service.register(
                'Ray', 'ray@example.com', 'password8', 'kekemi', REQUEST
            )

    def test_duplicate_normalized_identity_is_public_and_creates_nothing(self):
        self.backend.add_user(username='Ray', email='first@example.com')

        with self.assertRaisesRegex(PublicAuthError, '用户名或邮箱已被使用'):
            self.service.register(
                'ＲＡＹ', 'other@example.com', 'password8', 'kekemi', REQUEST
            )

        self.assertNotIn('other@example.com', self.backend.credentials)

    def test_pending_login_creates_only_twenty_four_hour_status_session(self):
        self.backend.add_user(status='pending', password='password8')

        result = self.service.login('Ray', 'password8', REQUEST)

        self.assertEqual('status', result.session.access_level)
        self.assertEqual(24 * 3600, int(
            (result.session.expires_at - result.session.created_at).total_seconds()
        ))

    def test_active_login_by_username_or_email_creates_thirty_day_app_session(self):
        profile = self.backend.add_user(status='active', password='password8')

        for identifier in ('Ray', 'RAY@EXAMPLE.COM'):
            with self.subTest(identifier=identifier):
                result = self.service.login(identifier, 'password8', REQUEST)
                self.assertEqual(profile.id, result.profile.id)
                self.assertEqual('app', result.session.access_level)
                self.assertEqual(30 * 86400, int(
                    (result.session.expires_at - result.session.created_at)
                    .total_seconds()
                ))

    def test_unknown_account_and_wrong_password_share_one_public_error(self):
        self.backend.add_user(status='active', password='password8')

        for identifier, password in (
            ('missing', 'password8'),
            ('Ray', 'wrongpass'),
        ):
            with self.subTest(identifier=identifier):
                with self.assertRaisesRegex(
                    PublicAuthError, '^账号或密码错误$'
                ):
                    self.service.login(identifier, password, REQUEST)

    def test_authentication_uses_latest_status_and_revokes_invalid_app_session(self):
        profile = self.backend.add_user(status='active')
        login = self.service.login('Ray', 'password8', REQUEST)
        self.backend.profiles[profile.id] = type(profile)(
            **{**profile.__dict__, 'status': 'suspended'}
        )

        context = self.service.authenticate_session(login.credentials.token)

        self.assertIsNone(context)
        self.assertIsNotNone(
            self.backend.sessions[hash_token(login.credentials.token)].revoked_at
        )

    def test_rejected_user_can_resubmit_and_other_states_cannot(self):
        profile = self.backend.add_user(status='rejected', reason='资料不符')
        login = self.service.login('Ray', 'password8', REQUEST)

        updated = self.service.resubmit(login.credentials.token)

        self.assertEqual(profile.id, updated.id)
        self.assertEqual('pending', updated.status)
        self.assertEqual('', updated.status_reason)
        with self.assertRaisesRegex(PublicAuthError, '当前状态不能重新提交'):
            self.service.resubmit(login.credentials.token)

    def test_password_reset_revokes_every_existing_session(self):
        profile = self.backend.add_user(status='active')
        first = self.service.login('Ray', 'password8', REQUEST)
        second = self.service.login('Ray', 'password8', REQUEST)
        self.backend.recovery_tokens['recovery-token'] = profile.id

        user_id = self.service.reset_password(
            'recovery-token', 'newpassword8'
        )

        self.assertEqual(profile.id, user_id)
        self.assertIsNone(
            self.service.authenticate_session(first.credentials.token)
        )
        self.assertIsNone(
            self.service.authenticate_session(second.credentials.token)
        )

    def test_forgot_password_has_same_result_for_known_and_unknown_identifier(self):
        self.backend.add_user(status='active')

        known = self.service.forgot_password('Ray')
        unknown = self.service.forgot_password('missing')

        self.assertEqual(known, unknown)
        self.assertEqual([
            ('ray@example.com', 'https://app.example/reset-password')
        ], self.backend.reset_requests)


if __name__ == '__main__':
    unittest.main()


class AdminDeleteUserTests(unittest.TestCase):
    """硬删除账号：不可恢复，所以每一道闸都要有测试压着。

    这里测的是 service 层，它的作用是给出中文报错、挡住明显非法的调用。
    **权威判定在 SQL 函数里**（public.admin_delete_user）——service 层被
    绕过（比如以后有人加了别的调用入口）时，数据库那层仍然会拒绝。
    所以这些测试证明的是"友好且不浪费一次往返"，不是"安全边界只有这一层"。
    """

    def setUp(self):
        self.backend = FakeAuthBackend()
        self.service = AuthService(CONFIG, self.backend, clock=lambda: NOW)
        self.superadmin = self.backend.add_user(
            username='kekeray', email='ray@example.com',
            role='super_admin', status='active')
        self.admin = self.backend.add_user(
            username='小啾', email='jiu@example.com',
            role='admin', status='active')
        self.target = self.backend.add_user(
            username='小淘气', email='taoqi@example.com',
            role='member', status='active')

    def test_plain_admin_cannot_delete(self):
        """普通 admin 不行——与「强制下线」同级，只有超管能用。"""
        with self.assertRaisesRegex(PublicAuthError, '无权限'):
            self.service.admin_delete_user(
                self.admin, self.target.id, '离职', 'ip-hash')
        self.assertEqual([], self.backend.admin_calls)
        self.assertIn(self.target.id, self.backend.profiles)

    def test_suspended_super_admin_cannot_delete(self):
        frozen = self.backend.add_user(
            username='冻结的超管', email='frozen@example.com',
            role='super_admin', status='suspended')
        with self.assertRaisesRegex(PublicAuthError, '无权限'):
            self.service.admin_delete_user(
                frozen, self.target.id, '离职', 'ip-hash')
        self.assertEqual([], self.backend.admin_calls)

    def test_empty_reason_rejected_before_touching_backend(self):
        """原因是审计里唯一说明「为什么删」的字段，不能省。"""
        for reason in ('', '   ', None):
            with self.assertRaisesRegex(PublicAuthError, '原因'):
                self.service.admin_delete_user(
                    self.superadmin, self.target.id, reason, 'ip-hash')
        self.assertEqual([], self.backend.admin_calls)
        self.assertIn(self.target.id, self.backend.profiles)

    def test_cannot_delete_self(self):
        """删掉自己＝把自己锁在门外，而且没人能撤销。"""
        with self.assertRaisesRegex(PublicAuthError, '自己'):
            self.service.admin_delete_user(
                self.superadmin, self.superadmin.id, '手滑', 'ip-hash')
        self.assertEqual([], self.backend.admin_calls)
        self.assertIn(self.superadmin.id, self.backend.profiles)

    def test_unknown_target_reports_404_without_calling_backend(self):
        with self.assertRaises(PublicAuthError) as ctx:
            self.service.admin_delete_user(
                self.superadmin, 'no-such-user', '离职', 'ip-hash')
        self.assertEqual(404, ctx.exception.status_code)
        self.assertEqual([], self.backend.admin_calls)

    def test_cannot_delete_the_last_super_admin(self):
        """删除不能成为「不许降级最后一个超管」那条保护的后门。"""
        second = self.backend.add_user(
            username='二号超管', email='two@example.com',
            role='super_admin', status='active')
        # 此刻两个 active 超管，删掉一个是允许的
        self.service.admin_delete_user(
            self.superadmin, second.id, '交接完成', 'ip-hash')
        self.assertNotIn(second.id, self.backend.profiles)
        # 只剩自己一个超管了，而删自己本来就不允许——这里换个角度：
        # 再造一个 suspended 超管，它不算 active，仍然只剩一个可用超管
        lonely = self.backend.add_user(
            username='停用超管', email='sleep@example.com',
            role='super_admin', status='suspended')
        with self.assertRaisesRegex(PublicAuthError, '超级管理员'):
            self.service.admin_delete_user(
                self.superadmin, lonely.id, '清理', 'ip-hash')
        self.assertIn(lonely.id, self.backend.profiles)

    def test_successful_delete_removes_profile_sessions_and_credentials(self):
        result = self.service.admin_delete_user(
            self.superadmin, self.target.id, '离职交接完毕', 'ip-hash')
        self.assertEqual(
            [('delete_user', self.superadmin.id, self.target.id,
              '离职交接完毕', 'ip-hash')],
            self.backend.admin_calls)
        self.assertNotIn(self.target.id, self.backend.profiles)
        self.assertNotIn('taoqi@example.com', self.backend.credentials)
        # 返回被删账号的身份，供调用方回显与记录
        self.assertEqual('小淘气', result['username'])
        self.assertEqual('taoqi@example.com', result['email_normalized'])

    def test_reason_is_trimmed_before_reaching_backend(self):
        self.service.admin_delete_user(
            self.superadmin, self.target.id, '  离职  ', 'ip-hash')
        self.assertEqual('离职', self.backend.admin_calls[0][3])
