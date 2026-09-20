import argparse
import getpass
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from kekemi_auth.config import AuthConfig
from kekemi_auth.security import normalize_email, normalize_username, validate_password
from kekemi_auth.supabase_backend import SupabaseAuthBackend
from kekemi_auth.types import Profile


def _parser():
    parser = argparse.ArgumentParser(
        description='Create or explicitly promote the first Kekemi super-admin.',
        allow_abbrev=False,
    )
    parser.add_argument('--email', required=True)
    parser.add_argument('--username')
    parser.add_argument(
        '--password-env',
        help='Read a new account password from this environment variable.',
    )
    parser.add_argument('--promote-existing', action='store_true')
    parser.add_argument('--reason', default='')
    return parser


def _print_profile(profile, stream):
    print(f'id={profile.id}', file=stream)
    print(f'username={profile.username}', file=stream)
    print(f'role={profile.role}', file=stream)
    print(f'status={profile.status}', file=stream)


def main(
    argv=None,
    *,
    environ=None,
    backend=None,
    stdout=None,
    stderr=None,
    getpass_fn=None,
    clock=None,
):
    args = _parser().parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    getpass_fn = getpass_fn or getpass.getpass
    clock = clock or (lambda: datetime.now(timezone.utc))

    if environ is None:
        load_dotenv()
        environ = os.environ

    try:
        config = AuthConfig.from_env(environ)
    except (RuntimeError, ValueError):
        print('认证服务配置不完整，请检查必需环境变量。', file=stderr)
        return 2
    if not config.supabase_url or not config.service_role_key:
        print(
            '缺少 SUPABASE_URL 或 SUPABASE_SECRET_KEY'
            '（也兼容旧 SUPABASE_SERVICE_ROLE_KEY）。',
            file=stderr,
        )
        return 2

    try:
        email = normalize_email(args.email)
    except ValueError as exc:
        print(str(exc), file=stderr)
        return 2

    auth_backend = backend or SupabaseAuthBackend(config)
    existing = auth_backend.get_profile_by_email(email)
    if existing is not None:
        if existing.role == 'super_admin' and existing.status == 'active':
            _print_profile(existing, stdout)
            return 0
        if not args.promote_existing:
            print(
                '该邮箱已有普通账号；如确认提权，请显式使用 --promote-existing。',
                file=stderr,
            )
            return 2
        reason = str(args.reason or '').strip()
        if not reason:
            print('--promote-existing 必须同时提供 --reason。', file=stderr)
            return 2
        try:
            promoted = auth_backend.bootstrap_super_admin(
                existing.id, reason, clock(),
            )
        except Exception:
            print('超级管理员提权失败；未输出服务端敏感信息。', file=stderr)
            return 1
        _print_profile(promoted, stdout)
        return 0

    try:
        username, username_normalized = normalize_username(args.username)
    except ValueError as exc:
        print(str(exc), file=stderr)
        return 2
    if auth_backend.get_profile_by_username(username_normalized) is not None:
        print('用户名已被使用。', file=stderr)
        return 2

    if args.password_env:
        password = str(environ.get(args.password_env, ''))
        if not password:
            print('指定的密码环境变量为空或不存在。', file=stderr)
            return 2
    else:
        password = getpass_fn('新超级管理员密码: ')
    try:
        password = validate_password(password)
    except ValueError as exc:
        print(str(exc), file=stderr)
        return 2

    now = clock()
    reason = str(args.reason or '').strip() or 'initial super-admin bootstrap'
    user_id = None
    try:
        user_id = auth_backend.create_auth_user(email, password)
        auth_backend.create_profile(Profile(
            id=user_id,
            username=username,
            username_normalized=username_normalized,
            email_normalized=email,
            role='member',
            status='pending',
        ))
        profile = auth_backend.bootstrap_super_admin(user_id, reason, now)
    except Exception:
        if user_id:
            try:
                auth_backend.delete_auth_user(user_id)
            except Exception:
                print(
                    f'初始化失败且自动清理失败，请人工核对用户 ID {user_id}。',
                    file=stderr,
                )
                return 1
        print('超级管理员初始化失败；已撤销新建认证账号。', file=stderr)
        return 1

    _print_profile(profile, stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
