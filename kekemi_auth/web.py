import functools
import hmac
import os

import time

import user_notes
import version_info

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
)

from .config import AuthConfig
from .rate_limit import FixedWindowRateLimiter, RateLimitExceeded
from .security import hash_ip, hash_token
from .service import (
    AuthService,
    ProfileCreationFailed,
    PublicAuthError,
    RequestMetadata,
)
from .supabase_backend import SupabaseAuthBackend
from .types import (
    AuthBackendError,
    AuthBackendUnavailable,
    AuthPermissionDenied,
    Profile,
    StateConflict,
)


SESSION_COOKIE = 'kekemi_session'
CSRF_COOKIE = 'kekemi_csrf'
# 仓库根目录（kekemi_auth 的上一层），/version 据此读源码算指纹。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER_STARTED_AT = time.time()
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
PUBLIC_PAGES = {'/login', '/register', '/reset-password', '/healthz', '/version'}
PUBLIC_AUTH_APIS = {
    '/api/auth/register',
    '/api/auth/login',
    '/api/auth/forgot-password',
    '/api/auth/reset-password',
}
STATUS_PATHS = {
    '/account-status',
    '/api/auth/me',
    '/api/auth/resubmit',
    '/api/auth/logout',
}
# 监听的启停归 super_admin 独有：默认那几个厅是所有人共用的后台监听，
# 一旦被停，所有人当场断流，且停掉期间的互动记录永久缺失、事后补不回来。
SUPER_ADMIN_MUTATIONS = {
    ('POST', '/api/resolve'),
    ('POST', '/api/start'),
    ('POST', '/api/stop'),
    ('POST', '/api/stop_all'),
}
ADMIN_MUTATIONS = {
    ('POST', '/api/toggle_record_all'),
}


def classify_access(method, path):
    method = str(method or 'GET').upper()
    path = str(path or '/')
    if path.startswith('/static/') or path in PUBLIC_PAGES:
        return 'public'
    if path in PUBLIC_AUTH_APIS:
        return 'public'
    if path in STATUS_PATHS:
        return 'status'
    if (method, path) in SUPER_ADMIN_MUTATIONS:
        return 'super_admin'
    if (
        path == '/admin'
        or path.startswith('/api/admin/')
        or (method, path) in ADMIN_MUTATIONS
    ):
        return 'admin'
    return 'app'


def _extension():
    return current_app.extensions['kekemi_auth']


def _service():
    return _extension()['service']


def _config():
    return _extension()['config']


def _is_api_request():
    return request.path.startswith('/api/') or request.path.startswith('/stream/')


def _deny(message, status):
    if _is_api_request():
        return jsonify({'success': False, 'error': message}), status
    if status == 401:
        next_path = request.full_path.rstrip('?')
        return redirect(f'/login?next={next_path}')
    return message, status


def _raw_client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',', 1)[0].strip()
    return request.remote_addr or ''


def _request_metadata():
    config = _config()
    return RequestMetadata(
        ip_hash=hash_ip(_raw_client_ip(), config.session_secret),
        user_agent=request.headers.get('User-Agent', '')[:500],
    )


def _set_session_cookies(response, result):
    config = _config()
    secure = config.app_env == 'production'
    max_age = max(0, int(
        (result.session.expires_at - result.session.created_at).total_seconds()
    ))
    response.set_cookie(
        SESSION_COOKIE,
        result.credentials.token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite='Lax',
        path='/',
    )
    response.set_cookie(
        CSRF_COOKIE,
        result.credentials.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite='Lax',
        path='/',
    )
    return response


def _clear_session_cookies(response):
    secure = _config().app_env == 'production'
    response.delete_cookie(
        SESSION_COOKIE, path='/', httponly=True, secure=secure, samesite='Lax'
    )
    response.delete_cookie(
        CSRF_COOKIE, path='/', httponly=False, secure=secure, samesite='Lax'
    )
    return response


def _csrf_valid(context):
    header = request.headers.get('X-CSRF-Token', '')
    cookie = request.cookies.get(CSRF_COOKIE, '')
    if not header or not cookie or not hmac.compare_digest(header, cookie):
        return False
    return hmac.compare_digest(
        hash_token(header), context.session.csrf_hash
    )


def _auth_error_response(exc):
    if isinstance(exc, PublicAuthError):
        return jsonify({'success': False, 'error': str(exc)}), exc.status_code
    if isinstance(exc, ValueError):
        return jsonify({'success': False, 'error': str(exc)}), 400
    if isinstance(exc, AuthBackendUnavailable):
        return jsonify({'success': False, 'error': str(exc)}), 503
    if isinstance(exc, ProfileCreationFailed):
        current_app.logger.error('Registration profile compensation failed: %s', exc)
        return jsonify({
            'success': False,
            'error': '注册暂时未完成，请稍后重试或联系管理员',
        }), 503
    if isinstance(exc, StateConflict):
        return jsonify({
            'success': False,
            'error': '账号状态已变化，请刷新后重试',
        }), 409
    if isinstance(exc, AuthPermissionDenied):
        return jsonify({'success': False, 'error': '无权限'}), 403
    if isinstance(exc, AuthBackendError):
        current_app.logger.warning('Authentication backend rejected request: %s', exc)
        return jsonify({'success': False, 'error': str(exc)}), 400
    raise exc


def require_role(*roles):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            profile = getattr(g, 'current_user', None)
            if profile is None:
                return _deny('需要登录', 401)
            if profile.role not in roles:
                return _deny('无权限', 403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _build_blueprint():
    blueprint = Blueprint('kekemi_auth', __name__)

    @blueprint.get('/login')
    def login_page():
        return render_template('login.html')

    @blueprint.get('/register')
    def register_page():
        return render_template('register.html')

    @blueprint.get('/account-status')
    def account_status_page():
        return render_template('account_status.html', current_user=g.current_user)

    @blueprint.get('/reset-password')
    def reset_password_page():
        return render_template('reset_password.html')

    @blueprint.get('/admin')
    def admin_page():
        return render_template('admin.html', current_user=g.current_user)

    @blueprint.get('/healthz')
    def healthz():
        return jsonify({'status': 'ok'})

    # 版本指纹：healthz 200 只证明「活着」，不证明「跑的是哪一版」。
    # 线上被本地直推覆盖过，靠这个端点对账 —— 见 scripts/verify_deploy.py。
    @blueprint.get('/version')
    def version_api():
        return jsonify(version_info.version_payload(
            _PROJECT_ROOT, os.environ, _SERVER_STARTED_AT,
        ))

    @blueprint.post('/api/auth/register')
    def register_api():
        data = request.get_json(silent=True) or {}
        key = (
            'register',
            _request_metadata().ip_hash,
            str(data.get('email') or '').strip().casefold(),
        )
        limiter = _extension()['limiter']
        config = _config()
        try:
            limiter.check(
                key, config.register_attempts, config.register_window_seconds
            )
            result = _service().register(
                data.get('username'),
                data.get('email'),
                data.get('password'),
                data.get('invite_code'),
                _request_metadata(),
            )
            response = jsonify({
                'success': True,
                'user': result.profile.public_dict(include_email=True),
                'access_level': result.session.access_level,
                'next_url': '/account-status',
            })
            return _set_session_cookies(response, result)
        except RateLimitExceeded as exc:
            response = jsonify({
                'success': False,
                'error': '请求过于频繁，请稍后重试',
            })
            response.status_code = 429
            response.headers['Retry-After'] = str(exc.retry_after)
            return response
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.post('/api/auth/login')
    def login_api():
        data = request.get_json(silent=True) or {}
        identifier = str(data.get('identifier') or '').strip()
        metadata = _request_metadata()
        limiter = _extension()['limiter']
        config = _config()
        try:
            limiter.check(
                ('login', metadata.ip_hash, identifier.casefold()),
                config.login_attempts,
                config.login_window_seconds,
            )
            result = _service().login(
                identifier, data.get('password'), metadata
            )
            next_url = (
                '/' if result.session.access_level == 'app'
                else '/account-status'
            )
            response = jsonify({
                'success': True,
                'user': result.profile.public_dict(include_email=True),
                'access_level': result.session.access_level,
                'next_url': next_url,
            })
            return _set_session_cookies(response, result)
        except RateLimitExceeded as exc:
            response = jsonify({
                'success': False,
                'error': '请求过于频繁，请稍后重试',
            })
            response.status_code = 429
            response.headers['Retry-After'] = str(exc.retry_after)
            return response
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.post('/api/auth/logout')
    def logout_api():
        try:
            _service().logout(request.cookies.get(SESSION_COOKIE, ''))
            return _clear_session_cookies(jsonify({'success': True}))
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.get('/api/auth/me')
    def me_api():
        return jsonify({
            'success': True,
            'user': g.current_user.public_dict(include_email=True),
            'access_level': g.auth_session.access_level,
        })

    @blueprint.post('/api/auth/resubmit')
    def resubmit_api():
        try:
            profile = _service().resubmit(
                request.cookies.get(SESSION_COOKIE, '')
            )
            return jsonify({
                'success': True,
                'user': profile.public_dict(include_email=True),
            })
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.post('/api/auth/forgot-password')
    def forgot_password_api():
        data = request.get_json(silent=True) or {}
        identifier = str(data.get('identifier') or '').strip()
        metadata = _request_metadata()
        limiter = _extension()['limiter']
        config = _config()
        try:
            limiter.check(
                ('recovery', metadata.ip_hash, identifier.casefold()),
                config.recovery_attempts,
                config.recovery_window_seconds,
            )
            message = _service().forgot_password(identifier)
            return jsonify({'success': True, 'message': message})
        except RateLimitExceeded as exc:
            response = jsonify({
                'success': False,
                'error': '请求过于频繁，请稍后重试',
            })
            response.status_code = 429
            response.headers['Retry-After'] = str(exc.retry_after)
            return response
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.post('/api/auth/reset-password')
    def reset_password_api():
        data = request.get_json(silent=True) or {}
        try:
            _service().reset_password(
                data.get('recovery_access_token'), data.get('password')
            )
            return _clear_session_cookies(jsonify({
                'success': True,
                'message': '密码已更新，请重新登录。',
                'next_url': '/login',
            }))
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.get('/api/admin/users')
    def admin_users_api():
        try:
            users = _service().list_users(
                g.current_user, request.args.get('status') or None
            )
            return jsonify({
                'success': True,
                'users': [
                    profile.public_dict(include_email=True)
                    for profile in users
                ],
                'count': len(users),
            })
        except Exception as exc:
            return _auth_error_response(exc)

    def _transition_response(user_id, action):
        data = request.get_json(silent=True) or {}
        try:
            profile = _service().admin_transition(
                g.current_user,
                user_id,
                action,
                data.get('expected_status'),
                data.get('reason'),
                _request_metadata().ip_hash,
            )
            return jsonify({
                'success': True,
                'user': profile.public_dict(include_email=True),
            })
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.post('/api/admin/users/<user_id>/approve')
    def admin_approve_user_api(user_id):
        return _transition_response(user_id, 'approve')

    @blueprint.post('/api/admin/users/<user_id>/reject')
    def admin_reject_user_api(user_id):
        return _transition_response(user_id, 'reject')

    @blueprint.post('/api/admin/users/<user_id>/suspend')
    def admin_suspend_user_api(user_id):
        return _transition_response(user_id, 'suspend')

    @blueprint.post('/api/admin/users/<user_id>/restore')
    def admin_restore_user_api(user_id):
        return _transition_response(user_id, 'restore')

    @blueprint.post('/api/admin/users/<user_id>/revoke-sessions')
    def admin_revoke_sessions_api(user_id):
        data = request.get_json(silent=True) or {}
        try:
            count = _service().admin_force_logout(
                g.current_user,
                user_id,
                data.get('reason'),
                _request_metadata().ip_hash,
            )
            return jsonify({
                'success': True,
                'revoked_sessions': count,
            })
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.post('/api/admin/users/<user_id>/delete')
    def admin_delete_user_api(user_id):
        data = request.get_json(silent=True) or {}
        try:
            deleted = _service().admin_delete_user(
                g.current_user,
                user_id,
                data.get('reason'),
                _request_metadata().ip_hash,
            )
            # 只回显用户名，让前端能提示「已删除 XXX」。不回邮箱等其余字段：
            # 账号已经没了，多回一份身份信息没有用途，只多一个泄露面。
            return jsonify({
                'success': True,
                'deleted_username': str((deleted or {}).get('username') or ''),
            })
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.post('/api/admin/users/<user_id>/role')
    def admin_change_role_api(user_id):
        data = request.get_json(silent=True) or {}
        try:
            profile = _service().admin_change_role(
                g.current_user,
                user_id,
                data.get('role'),
                data.get('reason'),
                _request_metadata().ip_hash,
            )
            return jsonify({
                'success': True,
                'user': profile.public_dict(include_email=True),
            })
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.get('/api/admin/audit-logs')
    def admin_audit_logs_api():
        try:
            limit = request.args.get('limit', default=100, type=int)
            logs = _service().list_audit_logs(g.current_user, limit)
            return jsonify({
                'success': True,
                'logs': logs,
                'count': len(logs),
            })
        except Exception as exc:
            return _auth_error_response(exc)

    # 用户备注：只有 super_admin 能写/看。备注存本地库（user_notes），
    # 与 Supabase 账号体系解耦；classify_access 已把 /api/admin/* 归为 admin，
    # 这里的 require_role 再收紧到 super_admin。
    @blueprint.post('/api/admin/users/<user_id>/note')
    @require_role('super_admin')
    def admin_set_user_note_api(user_id):
        data = request.get_json(silent=True) or {}
        try:
            result = user_notes.set_note(
                user_id,
                data.get('note', ''),
                actor_id=getattr(g.current_user, 'id', ''),
            )
            return jsonify({'success': True, 'note': result['note']})
        except ValueError as exc:
            return _deny(str(exc), 400)
        except Exception as exc:
            return _auth_error_response(exc)

    @blueprint.get('/api/admin/user-notes')
    @require_role('super_admin')
    def admin_user_notes_api():
        try:
            return jsonify({'success': True, 'notes': user_notes.get_notes()})
        except Exception as exc:
            return _auth_error_response(exc)

    return blueprint


def init_auth(app, service=None, config=None):
    if 'kekemi_auth' in app.extensions:
        return app.extensions['kekemi_auth']
    config = config or AuthConfig.from_env(os.environ)
    if service is None and config.required:
        service = AuthService(config, SupabaseAuthBackend(config))
    extension = {
        'config': config,
        'service': service,
        'limiter': FixedWindowRateLimiter(),
    }
    app.extensions['kekemi_auth'] = extension

    @app.before_request
    def enforce_kekemi_auth():
        extension = app.extensions['kekemi_auth']
        active_config = extension['config']
        active_service = extension['service']
        if not active_config.required:
            g.current_user = Profile(
                id='local-development',
                username='本地管理员',
                username_normalized='local-development',
                email_normalized='local@localhost.invalid',
                role='super_admin',
                status='active',
            )
            g.auth_session = None
            return None

        policy = classify_access(request.method, request.path)
        if policy == 'public':
            return None

        raw_token = request.cookies.get(SESSION_COOKIE, '')
        try:
            context = active_service.authenticate_session(raw_token)
        except AuthBackendUnavailable as exc:
            return _deny(str(exc), 503)
        if context is None:
            return _deny('需要登录', 401)

        g.current_user = context.profile
        g.auth_session = context.session

        if policy != 'status' and (
            context.session.access_level != 'app'
            or context.profile.status != 'active'
        ):
            return _deny('账号尚未获准访问', 403)

        if policy == 'super_admin' and context.profile.role != 'super_admin':
            return _deny('无权限', 403)

        if policy == 'admin' and context.profile.role not in {'admin', 'super_admin'}:
            return _deny('无权限', 403)

        if request.method not in SAFE_METHODS and not _csrf_valid(context):
            return _deny('CSRF 校验失败', 403)
        return None

    app.register_blueprint(_build_blueprint())
    return extension
