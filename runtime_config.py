"""Environment-derived runtime settings for local and Railway execution."""

import os
from typing import Mapping


def is_production(environ: Mapping[str, str]) -> bool:
    app_env = str(environ.get('APP_ENV', '') or '').strip().lower()
    railway_environment = str(
        environ.get('RAILWAY_ENVIRONMENT', '') or ''
    ).strip()
    return app_env == 'production' or bool(railway_environment)


def _port(environ: Mapping[str, str]) -> int:
    try:
        value = int(environ.get('PORT', 5000))
    except (TypeError, ValueError):
        return 5000
    return value if 1 <= value <= 65535 else 5000


def resolve_server_options(environ: Mapping[str, str]) -> dict:
    production = is_production(environ)
    return {
        'host': '0.0.0.0' if production else '127.0.0.1',
        'port': _port(environ),
        'use_reloader': not production,
    }


def resolve_database_path(
    environ: Mapping[str, str],
    project_dir: str,
) -> str:
    configured = str(environ.get('SQLITE_DB_PATH', '') or '').strip()
    if configured:
        if not os.path.isabs(configured):
            configured = os.path.join(project_dir, configured)
        return os.path.abspath(configured)

    mount_path = str(
        environ.get('RAILWAY_VOLUME_MOUNT_PATH', '') or ''
    ).strip()
    base_dir = mount_path or project_dir
    return os.path.abspath(os.path.join(base_dir, 'mystery_history.db'))


def ensure_database_parent(database_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(database_path))
    os.makedirs(parent, exist_ok=True)

# 在线名单同步间隔（秒）：决定多久去抖音抓一次票数与在线名单。
# 直接换算成抖音请求频率——5 个厅 × 每 3 秒一次 ≈ 每分钟 100 次。
# 所有人共用同一份 Cookie，调太低触发风控的代价是整个抖音账号，
# 因此下限锁 1 秒，非法值一律回落默认而不是默默接受。
ONLINE_SYNC_INTERVAL_DEFAULT = 3


# 麦位实时票数刷新间隔（秒）。抖音只在握手响应（get_webcast_detail）里下发麦位票，
# 实时消息流一条都不推（2026-08-19 实测 150s 窗口 LIVE=0 / INIT=3），所以只能定时重拉。
# 每刷新一次 = 每个厅一次抖音请求：5 个厅 × 每 3 秒 ≈ 100 次/分钟，与在线名单同步同量级。
# 直接换算成风控成本（所有厅共用一份 Cookie），因此可配置，调大即可降频。
MIC_TICKET_REFRESH_DEFAULT = 3


def resolve_mic_ticket_interval(environ: Mapping[str, str]) -> int:
    raw = str(environ.get('MIC_TICKET_REFRESH_INTERVAL', '') or '').strip()
    if not raw:
        return MIC_TICKET_REFRESH_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return MIC_TICKET_REFRESH_DEFAULT
    if value < 1:
        return MIC_TICKET_REFRESH_DEFAULT
    return value


# 只读接口共享缓存的存活时间（秒）。同一个厅同一秒内只计算一次、所有查看者共用，
# CPU 开销因此与在线人数无关。设为 0 关闭缓存（排障用）。
RESPONSE_CACHE_TTL_DEFAULT = 1.0


def resolve_response_cache_ttl(environ: Mapping[str, str]) -> float:
    raw = str(environ.get('RESPONSE_CACHE_TTL', '') or '').strip()
    if not raw:
        return RESPONSE_CACHE_TTL_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return RESPONSE_CACHE_TTL_DEFAULT
    if value < 0:
        return RESPONSE_CACHE_TTL_DEFAULT
    return value


def resolve_online_sync_interval(environ: Mapping[str, str]) -> int:
    raw = str(environ.get('ONLINE_SYNC_INTERVAL', '') or '').strip()
    if not raw:
        return ONLINE_SYNC_INTERVAL_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return ONLINE_SYNC_INTERVAL_DEFAULT
    if value < 1:
        return ONLINE_SYNC_INTERVAL_DEFAULT
    return value


# 麦上主持静默多久算异常（秒）。默认 15 分钟。
SILENCE_THRESHOLD_DEFAULT = 900


def resolve_silence_threshold(environ: Mapping[str, str]) -> int:
    raw = str(environ.get('SILENCE_THRESHOLD_SECONDS', '') or '').strip()
    if not raw:
        return SILENCE_THRESHOLD_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return SILENCE_THRESHOLD_DEFAULT
    return value if value > 0 else SILENCE_THRESHOLD_DEFAULT


# 静默检查器多久扫一次（秒）。15 分钟的规则，误差 1 分钟无所谓。
SILENCE_SCAN_INTERVAL_DEFAULT = 60


def resolve_silence_scan_interval(environ: Mapping[str, str]) -> int:
    raw = str(environ.get('SILENCE_SCAN_INTERVAL', '') or '').strip()
    if not raw:
        return SILENCE_SCAN_INTERVAL_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return SILENCE_SCAN_INTERVAL_DEFAULT
    return value if value > 0 else SILENCE_SCAN_INTERVAL_DEFAULT
