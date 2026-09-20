import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@dataclass(frozen=True)
class SessionCredentials:
    token: str
    token_hash: str
    csrf_token: str
    csrf_hash: str


def normalize_username(value):
    display = unicodedata.normalize('NFKC', str(value or '')).strip()
    if not 2 <= len(display) <= 24:
        raise ValueError('用户名长度必须为 2–24 个字符')
    if any(unicodedata.category(char).startswith('C') for char in display):
        raise ValueError('用户名不能包含不可见或控制字符')
    return display, display.casefold()


def normalize_email(value):
    normalized = unicodedata.normalize('NFKC', str(value or '')).strip().lower()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ValueError('请输入有效邮箱')
    return normalized


def validate_password(value):
    password = str(value or '')
    if len(password) < 8:
        raise ValueError('密码至少需要 8 位')
    return password


def hash_token(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def hash_ip(value, secret):
    return hmac.new(
        str(secret).encode('utf-8'),
        str(value or '').encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def constant_time_invite_matches(supplied, expected):
    supplied_bytes = str(supplied or '').encode('utf-8')
    expected_bytes = str(expected or '').encode('utf-8')
    return bool(expected_bytes) and hmac.compare_digest(supplied_bytes, expected_bytes)


def new_session_credentials():
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    return SessionCredentials(
        token=token,
        token_hash=hash_token(token),
        csrf_token=csrf_token,
        csrf_hash=hash_token(csrf_token),
    )
