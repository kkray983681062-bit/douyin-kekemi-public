import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .backend import AuthBackend
from .config import AuthConfig
from .security import (
    SessionCredentials,
    constant_time_invite_matches,
    hash_token,
    new_session_credentials,
    normalize_email,
    normalize_username,
    validate_password,
)
from .types import (
    AuthBackendError,
    AuthPermissionDenied,
    DuplicateIdentity,
    Profile,
    SessionContext,
    StateConflict,
    StoredSession,
)


PASSWORD_RESET_MESSAGE = '如果账号存在，重置邮件已发送，请检查邮箱。'


class PublicAuthError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class ProfileCreationFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class RequestMetadata:
    ip_hash: str
    user_agent: str


@dataclass(frozen=True)
class AuthResult:
    profile: Profile
    session: StoredSession
    credentials: SessionCredentials


class AuthService:
    def __init__(self, config: AuthConfig, backend: AuthBackend, clock=None):
        self.config = config
        self.backend = backend
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _issue_session(self, profile, access_level, metadata):
        now = self.clock()
        lifetime = (
            self.config.app_session_seconds
            if access_level == 'app'
            else self.config.status_session_seconds
        )
        credentials = new_session_credentials()
        session = StoredSession(
            id=str(uuid.uuid4()),
            token_hash=credentials.token_hash,
            user_id=profile.id,
            access_level=access_level,
            csrf_hash=credentials.csrf_hash,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=lifetime),
            created_ip_hash=metadata.ip_hash,
            user_agent=str(metadata.user_agent or '')[:500],
        )
        self.backend.create_session(session)
        return AuthResult(profile, session, credentials)

    def register(self, username, email, password, invite_code, metadata):
        if not constant_time_invite_matches(invite_code, self.config.invite_code):
            raise PublicAuthError('邀请码无效')
        display, username_normalized = normalize_username(username)
        email_normalized = normalize_email(email)
        validated_password = validate_password(password)

        if (
            self.backend.get_profile_by_username(username_normalized)
            or self.backend.get_profile_by_email(email_normalized)
        ):
            raise PublicAuthError('用户名或邮箱已被使用')

        try:
            user_id = self.backend.create_auth_user(
                email_normalized, validated_password
            )
        except DuplicateIdentity as exc:
            raise PublicAuthError('用户名或邮箱已被使用') from exc

        profile = Profile(
            id=user_id,
            username=display,
            username_normalized=username_normalized,
            email_normalized=email_normalized,
            role='member',
            status='pending',
        )
        try:
            created = self.backend.create_profile(profile)
        except Exception as profile_error:
            try:
                self.backend.delete_auth_user(user_id)
            except Exception as cleanup_error:
                raise ProfileCreationFailed(
                    '用户资料创建失败，补偿删除也失败；请按用户 ID 清理'
                ) from cleanup_error
            if isinstance(profile_error, DuplicateIdentity):
                raise PublicAuthError('用户名或邮箱已被使用') from profile_error
            raise ProfileCreationFailed(
                '用户资料创建失败，已撤销认证账号'
            ) from profile_error

        return self._issue_session(created, 'status', metadata)

    def _find_login_profile(self, identifier):
        raw = str(identifier or '').strip()
        try:
            if '@' in raw:
                return self.backend.get_profile_by_email(normalize_email(raw))
            _, normalized = normalize_username(raw)
            return self.backend.get_profile_by_username(normalized)
        except ValueError:
            return None

    def login(self, identifier, password, metadata):
        profile = self._find_login_profile(identifier)
        if profile is None:
            raise PublicAuthError('账号或密码错误', 401)
        user_id = self.backend.verify_password(
            profile.email_normalized, str(password or '')
        )
        if not user_id or str(user_id) != profile.id:
            raise PublicAuthError('账号或密码错误', 401)

        latest = self.backend.get_profile_by_id(profile.id)
        if latest is None:
            raise PublicAuthError('账号或密码错误', 401)
        access_level = 'app' if latest.status == 'active' else 'status'
        return self._issue_session(latest, access_level, metadata)

    def authenticate_session(self, raw_token):
        if not raw_token:
            return None
        token_hash = hash_token(raw_token)
        now = self.clock()
        context = self.backend.get_session(token_hash, now)
        if context is None:
            return None
        if context.session.access_level == 'app' and context.profile.status != 'active':
            self.backend.revoke_session(token_hash, now)
            return None
        if context.session.access_level not in {'app', 'status'}:
            self.backend.revoke_session(token_hash, now)
            return None
        if context.profile.status not in {
            'pending', 'active', 'rejected', 'suspended'
        }:
            self.backend.revoke_session(token_hash, now)
            return None
        return context

    def logout(self, raw_token):
        if raw_token:
            self.backend.revoke_session(hash_token(raw_token), self.clock())

    def resubmit(self, raw_token):
        context = self.authenticate_session(raw_token)
        if (
            context is None
            or context.session.access_level != 'status'
            or context.profile.status != 'rejected'
        ):
            raise PublicAuthError('当前状态不能重新提交', 409)
        try:
            return self.backend.resubmit_profile(
                context.profile.id, self.clock()
            )
        except StateConflict as exc:
            raise PublicAuthError('当前状态不能重新提交', 409) from exc

    def forgot_password(self, identifier):
        profile = self._find_login_profile(identifier)
        if profile is not None:
            self.backend.send_password_reset(
                profile.email_normalized,
                self.config.reset_redirect_url,
            )
        return PASSWORD_RESET_MESSAGE

    def reset_password(self, recovery_access_token, password):
        if not recovery_access_token:
            raise PublicAuthError('重置链接无效或已过期', 400)
        validated_password = validate_password(password)
        user_id = self.backend.update_password(
            recovery_access_token, validated_password
        )
        self.backend.revoke_user_sessions(user_id, self.clock())
        return user_id

    @staticmethod
    def _require_admin(actor, super_only=False):
        allowed = {'super_admin'} if super_only else {'admin', 'super_admin'}
        if actor is None or actor.status != 'active' or actor.role not in allowed:
            raise PublicAuthError('无权限', 403)

    def list_users(self, actor, status=None):
        self._require_admin(actor)
        if status and status not in {'pending', 'active', 'rejected', 'suspended'}:
            raise PublicAuthError('无效账号状态')
        return self.backend.list_users(status)

    def admin_transition(
        self, actor, target_id, action, expected_status, reason, ip_hash,
    ):
        self._require_admin(actor)
        expected_by_action = {
            'approve': 'pending',
            'reject': 'pending',
            'suspend': 'active',
            'restore': 'suspended',
        }
        if action not in expected_by_action:
            raise PublicAuthError('无效管理员操作')
        if expected_status != expected_by_action[action]:
            raise PublicAuthError('账号状态已变化，请刷新后重试', 409)
        clean_reason = str(reason or '').strip()
        if action in {'reject', 'suspend', 'restore'} and not clean_reason:
            raise PublicAuthError('请填写操作原因')

        target = self.backend.get_profile_by_id(target_id)
        if target is None:
            raise PublicAuthError('用户不存在', 404)
        if target.id == actor.id:
            raise PublicAuthError('不能管理自己的账号状态', 403)
        if actor.role == 'admin' and target.role != 'member':
            raise PublicAuthError('无权管理管理员账号', 403)
        if target.status != expected_status:
            raise PublicAuthError('账号状态已变化，请刷新后重试', 409)
        if action == 'suspend' and target.role == 'super_admin':
            active_supers = [
                item for item in self.backend.list_users('active')
                if item.role == 'super_admin'
            ]
            if len(active_supers) <= 1:
                raise PublicAuthError('不能暂停最后一个超级管理员', 403)

        try:
            return self.backend.transition_user(
                actor.id,
                target.id,
                action,
                expected_status,
                clean_reason,
                ip_hash,
                self.clock(),
            )
        except StateConflict as exc:
            raise PublicAuthError('账号状态已变化，请刷新后重试', 409) from exc
        except AuthPermissionDenied as exc:
            raise PublicAuthError('无权限', 403) from exc

    def admin_change_role(self, actor, target_id, role, reason, ip_hash):
        self._require_admin(actor, super_only=True)
        if role not in {'member', 'admin'}:
            raise PublicAuthError('角色只能是 member 或 admin')
        clean_reason = str(reason or '').strip()
        if not clean_reason:
            raise PublicAuthError('请填写角色修改原因')
        target = self.backend.get_profile_by_id(target_id)
        if target is None:
            raise PublicAuthError('用户不存在', 404)
        if target.id == actor.id:
            raise PublicAuthError('不能修改自己的角色', 403)
        if target.role == 'super_admin':
            active_supers = [
                item for item in self.backend.list_users('active')
                if item.role == 'super_admin'
            ]
            if len(active_supers) <= 1:
                raise PublicAuthError('不能降级最后一个超级管理员', 403)
        try:
            return self.backend.change_role(
                actor.id,
                target.id,
                role,
                clean_reason,
                ip_hash,
                self.clock(),
            )
        except AuthPermissionDenied as exc:
            raise PublicAuthError('无权限', 403) from exc

    def admin_force_logout(self, actor, target_id, reason, ip_hash):
        self._require_admin(actor, super_only=True)
        clean_reason = str(reason or '').strip()
        if not clean_reason:
            raise PublicAuthError('请填写强制下线原因')
        target = self.backend.get_profile_by_id(target_id)
        if target is None:
            raise PublicAuthError('用户不存在', 404)
        if target.id == actor.id:
            raise PublicAuthError('不能强制下线自己', 403)
        try:
            return self.backend.force_revoke_sessions(
                actor.id,
                target.id,
                clean_reason,
                ip_hash,
                self.clock(),
            )
        except AuthPermissionDenied as exc:
            raise PublicAuthError('无权限', 403) from exc

    def admin_delete_user(self, actor, target_id, reason, ip_hash):
        """硬删除账号，不可恢复。

        这一层的校验是为了给中文报错、并且在明显非法时不白跑一趟网络。
        **权威判定在 SQL 函数里**：即使有人从别的入口绕过这里，
        public.admin_delete_user 仍会自己校验一遍。
        """
        self._require_admin(actor, super_only=True)
        clean_reason = str(reason or '').strip()
        if not clean_reason:
            raise PublicAuthError('请填写删除账号原因')
        target = self.backend.get_profile_by_id(target_id)
        if target is None:
            raise PublicAuthError('用户不存在', 404)
        if target.id == actor.id:
            raise PublicAuthError('不能删除自己', 403)
        if target.role == 'super_admin':
            # 与 admin_change_role 里「不许降级最后一个超管」同一条保护：
            # 少了这条，删除就成了绕过它的后门。
            active_supers = [
                item for item in self.backend.list_users('active')
                if item.role == 'super_admin'
            ]
            if len(active_supers) <= 1:
                raise PublicAuthError('不能删除最后一个超级管理员', 403)
        try:
            return self.backend.delete_user(
                actor.id,
                target.id,
                clean_reason,
                ip_hash,
                self.clock(),
            )
        except AuthPermissionDenied as exc:
            raise PublicAuthError('无权限', 403) from exc

    def list_audit_logs(self, actor, limit=100):
        self._require_admin(actor)
        return self.backend.list_audit_logs(min(max(int(limit), 1), 200))
