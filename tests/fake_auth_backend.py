from dataclasses import replace
from datetime import datetime

from kekemi_auth.types import (
    AuthBackendError,
    DuplicateIdentity,
    Profile,
    SessionContext,
    StateConflict,
)


class FakeAuthBackend:
    def __init__(self):
        self.profiles = {}
        self.credentials = {}
        self.sessions = {}
        self.deleted_auth_users = []
        self.reset_requests = []
        self.recovery_tokens = {}
        self.fail_create_profile = False
        self.fail_delete_auth_user = False
        self.admin_calls = []
        self.audit_logs = []
        self._next_user = 1

    def add_user(
        self,
        *,
        user_id=None,
        username='Ray',
        email='ray@example.com',
        password='password8',
        role='member',
        status='pending',
        reason='',
    ):
        user_id = user_id or f'user-{self._next_user}'
        self._next_user += 1
        profile = Profile(
            id=user_id,
            username=username,
            username_normalized=username.casefold(),
            email_normalized=email.lower(),
            role=role,
            status=status,
            status_reason=reason,
        )
        self.profiles[user_id] = profile
        self.credentials[email.lower()] = (user_id, password)
        return profile

    def create_auth_user(self, email, password):
        if email in self.credentials:
            raise DuplicateIdentity('duplicate')
        user_id = f'generated-user-{self._next_user}'
        self._next_user += 1
        self.credentials[email] = (user_id, password)
        return user_id

    def delete_auth_user(self, user_id):
        if self.fail_delete_auth_user:
            raise AuthBackendError('delete failed')
        self.deleted_auth_users.append(user_id)
        self.credentials = {
            email: value
            for email, value in self.credentials.items()
            if value[0] != user_id
        }

    def verify_password(self, email, password):
        value = self.credentials.get(email)
        return value[0] if value and value[1] == password else None

    def send_password_reset(self, email, redirect_url):
        self.reset_requests.append((email, redirect_url))

    def update_password(self, recovery_access_token, password):
        user_id = self.recovery_tokens.get(recovery_access_token)
        if not user_id:
            raise AuthBackendError('invalid recovery token')
        for email, value in list(self.credentials.items()):
            if value[0] == user_id:
                self.credentials[email] = (user_id, password)
        return user_id

    def create_profile(self, profile):
        if self.fail_create_profile:
            raise AuthBackendError('profile insert failed')
        if any(
            item.username_normalized == profile.username_normalized
            or item.email_normalized == profile.email_normalized
            for item in self.profiles.values()
        ):
            raise DuplicateIdentity('duplicate')
        self.profiles[profile.id] = profile
        return profile

    def get_profile_by_id(self, user_id):
        return self.profiles.get(user_id)

    def get_profile_by_username(self, normalized):
        return next((
            profile for profile in self.profiles.values()
            if profile.username_normalized == normalized
        ), None)

    def get_profile_by_email(self, normalized):
        return next((
            profile for profile in self.profiles.values()
            if profile.email_normalized == normalized
        ), None)

    def create_session(self, session):
        self.sessions[session.token_hash] = session

    def get_session(self, token_hash, now):
        session = self.sessions.get(token_hash)
        if (
            session is None
            or session.revoked_at is not None
            or session.expires_at <= now
        ):
            return None
        profile = self.profiles.get(session.user_id)
        return SessionContext(session, profile) if profile else None

    def revoke_session(self, token_hash, now):
        session = self.sessions.get(token_hash)
        if session and session.revoked_at is None:
            self.sessions[token_hash] = replace(session, revoked_at=now)

    def revoke_user_sessions(self, user_id, now):
        count = 0
        for token_hash, session in list(self.sessions.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self.sessions[token_hash] = replace(session, revoked_at=now)
                count += 1
        return count

    def resubmit_profile(self, user_id, now):
        profile = self.profiles.get(user_id)
        if profile is None or profile.status != 'rejected':
            raise StateConflict('state conflict')
        profile = replace(
            profile,
            status='pending',
            status_reason='',
            updated_at=now.isoformat(),
        )
        self.profiles[user_id] = profile
        return profile

    def list_users(self, status=None):
        rows = list(self.profiles.values())
        return [row for row in rows if not status or row.status == status]

    def transition_user(
        self, actor_id, target_id, action, expected_status,
        reason, ip_hash, now,
    ):
        profile = self.profiles[target_id]
        if profile.status != expected_status:
            raise StateConflict('state conflict')
        statuses = {
            'approve': 'active',
            'reject': 'rejected',
            'suspend': 'suspended',
            'restore': 'active',
        }
        updated = replace(
            profile,
            status=statuses[action],
            status_reason=reason if action in {'reject', 'suspend'} else '',
            updated_at=now.isoformat(),
        )
        self.profiles[target_id] = updated
        if action == 'suspend':
            self.revoke_user_sessions(target_id, now)
        self.admin_calls.append((action, actor_id, target_id, reason, ip_hash))
        return updated

    def change_role(self, actor_id, target_id, role, reason, ip_hash, now):
        profile = replace(
            self.profiles[target_id], role=role, updated_at=now.isoformat()
        )
        self.profiles[target_id] = profile
        self.revoke_user_sessions(target_id, now)
        self.admin_calls.append(
            ('role_change', actor_id, target_id, reason, ip_hash)
        )
        return profile

    def bootstrap_super_admin(self, target_id, reason, now):
        profile = replace(
            self.profiles[target_id],
            role='super_admin',
            status='active',
            status_reason='',
            approved_at=now.isoformat(),
            suspended_at=None,
            suspended_by=None,
            updated_at=now.isoformat(),
        )
        self.profiles[target_id] = profile
        self.revoke_user_sessions(target_id, now)
        self.admin_calls.append(
            ('bootstrap_super_admin', target_id, reason)
        )
        return profile

    def delete_user(self, actor_id, target_id, reason, ip_hash, now):
        profile = self.profiles.pop(target_id)
        self.sessions = {
            token: item for token, item in self.sessions.items()
            if item.get('user_id') != target_id
        }
        self.credentials = {
            email: value for email, value in self.credentials.items()
            if value[0] != target_id
        }
        self.deleted_auth_users.append(target_id)
        self.admin_calls.append(
            ('delete_user', actor_id, target_id, reason, ip_hash)
        )
        return {
            'id': profile.id,
            'username': profile.username,
            'email_normalized': profile.email_normalized,
            'role': profile.role,
            'status': profile.status,
        }

    def force_revoke_sessions(
        self, actor_id, target_id, reason, ip_hash, now,
    ):
        count = self.revoke_user_sessions(target_id, now)
        self.admin_calls.append(
            ('force_revoke_sessions', actor_id, target_id, reason, ip_hash)
        )
        return count

    def list_audit_logs(self, limit):
        return self.audit_logs[:limit]
