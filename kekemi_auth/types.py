from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional


class AuthBackendError(RuntimeError):
    """A non-retryable server-side authentication backend error."""


class AuthBackendUnavailable(AuthBackendError):
    """The authentication backend is temporarily unavailable."""


class DuplicateIdentity(AuthBackendError):
    """A normalized username or email already exists."""


class StateConflict(AuthBackendError):
    """The target changed after the administrator loaded it."""


class AuthPermissionDenied(AuthBackendError):
    """The backend rejected an administrative authorization boundary."""


@dataclass(frozen=True)
class Profile:
    id: str
    username: str
    username_normalized: str
    email_normalized: str
    role: str = 'member'
    status: str = 'pending'
    status_reason: str = ''
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None
    suspended_at: Optional[str] = None
    suspended_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_record(self):
        return asdict(self)

    def public_dict(self, include_email=False):
        data = {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'status': self.status,
            'status_reason': self.status_reason,
            'approved_at': self.approved_at,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
        if include_email:
            data['email'] = self.email_normalized
        return data


@dataclass(frozen=True)
class StoredSession:
    id: str
    token_hash: str
    user_id: str
    access_level: str
    csrf_hash: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None
    created_ip_hash: str = ''
    user_agent: str = ''


@dataclass(frozen=True)
class SessionContext:
    session: StoredSession
    profile: Profile
