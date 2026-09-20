from dataclasses import asdict
import re
from datetime import datetime, timezone

import requests

from .config import AuthConfig
from .types import (
    AuthBackendError,
    AuthBackendUnavailable,
    AuthPermissionDenied,
    DuplicateIdentity,
    Profile,
    SessionContext,
    StateConflict,
    StoredSession,
)


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


# Postgres 返回微秒时会去掉末尾的 0：.725240 变成 .72524。
# Python 3.10 的 fromisoformat 只认 3 位或 6 位小数秒，5 位直接抛
# ValueError；3.11 起才放宽成任意位数。**生产是 3.10、本机是 3.11**，
# 所以这个坑在本地永远复现不了，只在线上随机发作（约每 10 个时间戳
# 撞上 1 个）。2026-08-21 事故：一个会话时间戳 .72524，整站 500。
_ISO_FRACTION_RE = re.compile(r'\.(\d+)')


def _normalize_iso_fraction(text):
    """把 ISO 时间串的小数秒补齐/截断到 6 位。

    只处理第一个小数点（ISO 时间串里只有小数秒会出现小数点，日期里的
    连字符不受影响）。超过 6 位（纳秒）截断而不是报错——宁可丢一点
    精度，也不能因为解不了时间戳把整站打成 500。
    """
    return _ISO_FRACTION_RE.sub(
        lambda m: '.' + m.group(1)[:6].ljust(6, '0'), text, count=1)


def _datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(
        _normalize_iso_fraction(str(value).replace('Z', '+00:00')))


def _profile(row):
    return Profile(
        id=str(row['id']),
        username=row.get('username', ''),
        username_normalized=row.get('username_normalized', ''),
        email_normalized=row.get('email_normalized', ''),
        role=row.get('role', 'member'),
        status=row.get('status', 'pending'),
        status_reason=row.get('status_reason') or '',
        approved_at=row.get('approved_at'),
        approved_by=row.get('approved_by'),
        suspended_at=row.get('suspended_at'),
        suspended_by=row.get('suspended_by'),
        created_at=row.get('created_at'),
        updated_at=row.get('updated_at'),
    )


def _stored_session(row):
    return StoredSession(
        id=str(row['id']),
        token_hash=row['token_hash'],
        user_id=str(row['user_id']),
        access_level=row['access_level'],
        csrf_hash=row['csrf_hash'],
        created_at=_datetime(row['created_at']),
        last_seen_at=_datetime(row['last_seen_at']),
        expires_at=_datetime(row['expires_at']),
        revoked_at=_datetime(row.get('revoked_at')),
        created_ip_hash=row.get('created_ip_hash') or '',
        user_agent=row.get('user_agent') or '',
    )


class SupabaseAuthBackend:
    def __init__(self, config: AuthConfig, http=None):
        self.config = config
        self.http = http or requests.Session()

    def _headers(self, bearer=None, prefer=None):
        headers = {
            'apikey': self.config.service_role_key,
            'Content-Type': 'application/json',
        }
        if bearer:
            headers['Authorization'] = f'Bearer {bearer}'
        elif not self.config.service_role_key.startswith('sb_secret_'):
            # Legacy service-role keys are JWTs. New sb_secret_* values are
            # opaque API keys and must never be presented as bearer JWTs.
            headers['Authorization'] = (
                f'Bearer {self.config.service_role_key}'
            )
        if prefer:
            headers['Prefer'] = prefer
        return headers

    def _perform(self, method, path, *, bearer=None, prefer=None, **kwargs):
        try:
            response = self.http.request(
                method,
                f'{self.config.supabase_url}{path}',
                headers=self._headers(bearer=bearer, prefer=prefer),
                timeout=10,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise AuthBackendUnavailable(
                '认证服务暂时不可用，请稍后重试'
            ) from exc
        if response.status_code >= 500:
            raise AuthBackendUnavailable('认证服务暂时不可用，请稍后重试')
        return response

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _expect_success(response, *, duplicate=False, conflict=False):
        if 200 <= response.status_code < 300:
            return
        if duplicate and response.status_code in {409, 422}:
            raise DuplicateIdentity('用户名或邮箱已被使用')
        if conflict and response.status_code in {400, 409}:
            payload = SupabaseAuthBackend._json(response) or {}
            message = str(payload.get('message') or payload.get('error') or '')
            if 'permission' in message.lower() or 'forbidden' in message.lower():
                raise AuthPermissionDenied('无权执行该管理员操作')
            raise StateConflict('账号状态已变化，请刷新后重试')
        if response.status_code == 403:
            raise AuthPermissionDenied('无权执行该管理员操作')
        raise AuthBackendError('认证服务请求失败')

    def create_auth_user(self, email, password):
        response = self._perform(
            'POST',
            '/auth/v1/admin/users',
            json={'email': email, 'password': password, 'email_confirm': True},
        )
        self._expect_success(response, duplicate=True)
        payload = self._json(response) or {}
        user_id = payload.get('id') or (payload.get('user') or {}).get('id')
        if not user_id:
            raise AuthBackendError('认证服务返回了无效用户')
        return str(user_id)

    def delete_auth_user(self, user_id):
        response = self._perform('DELETE', f'/auth/v1/admin/users/{user_id}')
        self._expect_success(response)

    def verify_password(self, email, password):
        response = self._perform(
            'POST',
            '/auth/v1/token?grant_type=password',
            json={'email': email, 'password': password},
        )
        if response.status_code in {400, 401}:
            return None
        self._expect_success(response)
        payload = self._json(response) or {}
        user_id = (payload.get('user') or {}).get('id')
        return str(user_id) if user_id else None

    def send_password_reset(self, email, redirect_url):
        payload = {'email': email}
        if redirect_url:
            payload['redirect_to'] = redirect_url
        response = self._perform('POST', '/auth/v1/recover', json=payload)
        if response.status_code not in {400, 404}:
            self._expect_success(response)

    def update_password(self, recovery_access_token, password):
        response = self._perform(
            'PUT',
            '/auth/v1/user',
            bearer=recovery_access_token,
            json={'password': password},
        )
        self._expect_success(response)
        payload = self._json(response) or {}
        user_id = payload.get('id') or (payload.get('user') or {}).get('id')
        if not user_id:
            raise AuthBackendError('密码重置凭证无效或已过期')
        return str(user_id)

    def create_profile(self, profile):
        profile_record = {
            key: value
            for key, value in profile.to_record().items()
            if value is not None
        }
        response = self._perform(
            'POST',
            '/rest/v1/user_profiles',
            prefer='return=representation',
            json=profile_record,
        )
        self._expect_success(response, duplicate=True)
        rows = self._json(response) or []
        if not rows:
            raise AuthBackendError('用户资料创建失败')
        return _profile(rows[0])

    def _get_profile(self, field, value):
        response = self._perform(
            'GET',
            '/rest/v1/user_profiles',
            params={
                'select': '*',
                field: f'eq.{value}',
                'limit': '1',
            },
        )
        self._expect_success(response)
        rows = self._json(response) or []
        return _profile(rows[0]) if rows else None

    def get_profile_by_id(self, user_id):
        return self._get_profile('id', user_id)

    def get_profile_by_username(self, normalized):
        return self._get_profile('username_normalized', normalized)

    def get_profile_by_email(self, normalized):
        return self._get_profile('email_normalized', normalized)

    def create_session(self, session):
        payload = asdict(session)
        for key in ('created_at', 'last_seen_at', 'expires_at', 'revoked_at'):
            payload[key] = _iso(payload[key])
        response = self._perform(
            'POST',
            '/rest/v1/app_sessions',
            prefer='return=minimal',
            json=payload,
        )
        self._expect_success(response, duplicate=True)

    def get_session(self, token_hash, now):
        response = self._perform(
            'GET',
            '/rest/v1/app_sessions',
            params={
                'select': '*',
                'token_hash': f'eq.{token_hash}',
                'revoked_at': 'is.null',
                'expires_at': f'gt.{_iso(now)}',
                'limit': '1',
            },
        )
        self._expect_success(response)
        rows = self._json(response) or []
        if not rows:
            return None
        session = _stored_session(rows[0])
        profile = self.get_profile_by_id(session.user_id)
        return SessionContext(session=session, profile=profile) if profile else None

    def revoke_session(self, token_hash, now):
        response = self._perform(
            'PATCH',
            '/rest/v1/app_sessions',
            params={'token_hash': f'eq.{token_hash}', 'revoked_at': 'is.null'},
            prefer='return=minimal',
            json={'revoked_at': _iso(now)},
        )
        self._expect_success(response)

    def revoke_user_sessions(self, user_id, now):
        response = self._perform(
            'PATCH',
            '/rest/v1/app_sessions',
            params={'user_id': f'eq.{user_id}', 'revoked_at': 'is.null'},
            prefer='return=representation',
            json={'revoked_at': _iso(now)},
        )
        self._expect_success(response)
        return len(self._json(response) or [])

    def resubmit_profile(self, user_id, now):
        response = self._perform(
            'PATCH',
            '/rest/v1/user_profiles',
            params={'id': f'eq.{user_id}', 'status': 'eq.rejected'},
            prefer='return=representation',
            json={
                'status': 'pending',
                'status_reason': '',
                'updated_at': _iso(now),
            },
        )
        self._expect_success(response)
        rows = self._json(response) or []
        if not rows:
            raise StateConflict('账号状态已变化，请刷新后重试')
        return _profile(rows[0])

    def list_users(self, status=None):
        params = {'select': '*', 'order': 'created_at.desc'}
        if status:
            params['status'] = f'eq.{status}'
        response = self._perform(
            'GET', '/rest/v1/user_profiles', params=params
        )
        self._expect_success(response)
        return [_profile(row) for row in (self._json(response) or [])]

    def _rpc_profile(self, name, payload):
        response = self._perform(
            'POST', f'/rest/v1/rpc/{name}', json=payload
        )
        self._expect_success(response, conflict=True)
        data = self._json(response)
        if isinstance(data, list):
            data = data[0] if data else None
        if not data:
            raise AuthBackendError('管理员操作没有返回用户资料')
        return _profile(data)

    def transition_user(
        self, actor_id, target_id, action, expected_status,
        reason, ip_hash, now,
    ):
        return self._rpc_profile('admin_transition_user', {
            'p_actor_user_id': actor_id,
            'p_target_user_id': target_id,
            'p_action': action,
            'p_expected_status': expected_status,
            'p_reason': reason,
            'p_ip_hash': ip_hash,
            'p_now': _iso(now),
        })

    def change_role(
        self, actor_id, target_id, role, reason, ip_hash, now,
    ):
        return self._rpc_profile('admin_change_user_role', {
            'p_actor_user_id': actor_id,
            'p_target_user_id': target_id,
            'p_role': role,
            'p_reason': reason,
            'p_ip_hash': ip_hash,
            'p_now': _iso(now),
        })

    def bootstrap_super_admin(self, target_id, reason, now):
        return self._rpc_profile('bootstrap_promote_super_admin', {
            'p_target_user_id': target_id,
            'p_reason': reason,
            'p_now': _iso(now),
        })

    def force_revoke_sessions(
        self, actor_id, target_id, reason, ip_hash, now,
    ):
        response = self._perform(
            'POST',
            '/rest/v1/rpc/admin_force_revoke_sessions',
            json={
                'p_actor_user_id': actor_id,
                'p_target_user_id': target_id,
                'p_reason': reason,
                'p_ip_hash': ip_hash,
                'p_now': _iso(now),
            },
        )
        self._expect_success(response, conflict=True)
        data = self._json(response)
        return int(data or 0)

    def delete_user(self, actor_id, target_id, reason, ip_hash, now):
        """硬删除账号。

        权限校验、审计写入、删除都在 public.admin_delete_user 这个 SQL
        函数里一次做完——审计必须在删除之前落库，因为 admin_audit_logs
        的外键是 on delete set null，删完之后那条日志就不知道是关于谁的了。

        返回被删账号的身份快照（用户名/邮箱/角色/状态），供调用方回显。
        """
        response = self._perform(
            'POST',
            '/rest/v1/rpc/admin_delete_user',
            json={
                'p_actor_user_id': actor_id,
                'p_target_user_id': target_id,
                'p_reason': reason,
                'p_ip_hash': ip_hash,
                'p_now': _iso(now),
            },
        )
        self._expect_success(response, conflict=True)
        return self._json(response) or {}

    def list_audit_logs(self, limit):
        response = self._perform(
            'GET',
            '/rest/v1/admin_audit_logs',
            params={
                'select': '*',
                'order': 'created_at.desc',
                'limit': str(min(max(int(limit), 1), 200)),
            },
        )
        self._expect_success(response)
        return list(self._json(response) or [])
