from dataclasses import dataclass
from typing import Mapping

from runtime_config import is_production


def _is_enabled(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class AuthConfig:
    required: bool
    app_env: str
    supabase_url: str
    service_role_key: str
    invite_code: str
    session_secret: str
    reset_redirect_url: str
    app_session_seconds: int = 30 * 86400
    status_session_seconds: int = 24 * 3600
    login_attempts: int = 10
    login_window_seconds: int = 15 * 60
    register_attempts: int = 5
    register_window_seconds: int = 60 * 60
    recovery_attempts: int = 5
    recovery_window_seconds: int = 60 * 60

    @classmethod
    def from_env(cls, environ: Mapping[str, str]):
        configured_app_env = str(
            environ.get('APP_ENV', 'development')
        ).strip().lower()
        app_env = 'production' if is_production(environ) else configured_app_env
        required = _is_enabled(environ.get('AUTH_REQUIRED', '0'))
        supabase_secret_key = str(
            environ.get('SUPABASE_SECRET_KEY', '')
        ).strip() or str(
            environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
        ).strip()
        values = {
            'SUPABASE_URL': str(environ.get('SUPABASE_URL', '')).strip(),
            'SUPABASE_SECRET_KEY': supabase_secret_key,
            'REGISTRATION_INVITE_CODE': str(
                environ.get('REGISTRATION_INVITE_CODE', '')
            ).strip(),
            'APP_SESSION_SECRET': str(
                environ.get('APP_SESSION_SECRET', '')
            ).strip(),
        }
        if app_env == 'production' and not required:
            raise RuntimeError(
                'production requires AUTH_REQUIRED=1; authentication cannot be disabled'
            )
        missing = [name for name, value in values.items() if not value]
        if required and missing:
            scope = 'production ' if app_env == 'production' else ''
            raise RuntimeError(
                f"{scope}authentication configuration is incomplete: {', '.join(missing)}"
            )

        return cls(
            required=required,
            app_env=app_env,
            supabase_url=values['SUPABASE_URL'].rstrip('/'),
            service_role_key=values['SUPABASE_SECRET_KEY'],
            invite_code=values['REGISTRATION_INVITE_CODE'],
            session_secret=values['APP_SESSION_SECRET'],
            reset_redirect_url=str(
                environ.get('SUPABASE_RESET_REDIRECT_URL', '')
            ).strip(),
            login_attempts=int(environ.get('AUTH_LOGIN_ATTEMPTS', 10)),
            login_window_seconds=int(
                environ.get('AUTH_LOGIN_WINDOW_SECONDS', 15 * 60)
            ),
            register_attempts=int(environ.get('AUTH_REGISTER_ATTEMPTS', 5)),
            register_window_seconds=int(
                environ.get('AUTH_REGISTER_WINDOW_SECONDS', 60 * 60)
            ),
            recovery_attempts=int(environ.get('AUTH_RECOVERY_ATTEMPTS', 5)),
            recovery_window_seconds=int(
                environ.get('AUTH_RECOVERY_WINDOW_SECONDS', 60 * 60)
            ),
        )
