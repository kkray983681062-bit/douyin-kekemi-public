"""克克咪 1.0 - 抖音直播间实时监听与数据面板。

Flask Web 服务，供本机浏览器查看在线观众、匿名用户和互动数据。
"""
import sys, os, time, gzip, zlib, json, threading, queue, re, requests, urllib3, hashlib
from collections import deque
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import utils.common_util as cu
cu.load_env()

from builder.params import Params
from builder.header import HeaderBuilder
from utils.dy_util import generate_signature
from dy_apis.douyin_api import DouyinAPI
from urllib.parse import urlencode
from websocket import WebSocketApp
import static.Live_pb2 as Live_pb2
from gift_analytics import (
    GiftComboTracker,
    beijing_day_start_timestamp,
    gift_value,
    parse_gift_catalog,
    query_host_gifts,
    query_spender_rank,
    query_spender_rank_since,
)
from gift_catalog import (
    init_gift_catalog,
    list_pending_names,
    set_name_price,
    list_gifts,
    list_price_history,
    observe_gift,
    resolve_gift_price,
    set_manual_price,
    sync_douyin_prices,
)
from online_audience import (
    OnlineAudiencePoller,
    OnlineAudienceSnapshot,
    attach_matched_identities,
    attach_mystery_profiles,
    extract_linkmic_users,
    extract_linkmic_fan_tickets,
    roster_from_fan_tickets,
    fetch_online_audience,
    merge_online_snapshot,
)
import identity_registry
import mic_silence
from leaderboards import (
    lock_completed_weeks,
    record_room_hall,
    init_leaderboard_schema,
    list_adjustable_gifts,
    list_week_periods,
    query_daily_boards,
    query_weekly_board,
    resolve_event_tickets,
    set_recipient_adjustment,
)
import response_cache
import response_compress
import security_headers
from runtime_config import (
    ensure_database_parent,
    is_production,
    resolve_database_path,
    resolve_server_options,
    resolve_mic_ticket_interval,
    resolve_online_sync_interval,
    resolve_response_cache_ttl,
    resolve_silence_scan_interval,
    resolve_silence_threshold,
)

from flask import Flask, Response, request, jsonify, render_template, g
from kekemi_auth import init_auth

app = Flask(__name__)
# 响应 gzip：日榜实测 35.2 KB、每 2 秒刷一次，压缩后约六分之一，
# 直接决定出网流量成本。SSE 流式响应在 response_compress 里被显式跳过。
response_compress.install(app)
# 安全响应头：原本一个都没有，管理页可被 iframe 套住做点击劫持
# （诱导已登录的 super_admin 点「停止监听」「改角色」）。
security_headers.install(app)


def api_error(exc, message='服务器处理失败，请稍后重试', **extra):
    """接口异常只写服务器日志，不把原文回显给前端。

    Python 的异常消息常带绝对路径、SQL 片段和内部字段名，
    公网部署后攻击者只要故意触发报错就能拿到目录结构和实现细节。
    """
    app.logger.exception('API 处理失败: %s', exc)
    payload = {'success': False, 'error': message}
    payload.update(extra)
    return jsonify(payload)


def business_error(exc, status=None):
    """业务异常的明确出口：原文就是给用户看的提示，照常回显。

    与 api_error 的区别在于意图——这里捕获的是 ValueError / LookupError
    这类"你填错了""东西不存在"，用户需要看到具体哪里不对；
    系统异常（数据库、网络、未预期崩溃）一律走 api_error。
    """
    message = str(exc)
    payload = jsonify({'success': False, 'error': message})
    return (payload, status) if status else payload
init_auth(app)

# ========== 工具函数 ==========
GENDER_MAP = {0: '未设置', 1: '男', 2: '女'}
_TTWID = cu.dy_live_auth.cookie.get('ttwid', '')
_user_info_cache = {}
_level_cache = {}
_last_api_call = 0  # 限流时间戳
_record_all_enabled = True  # 全局录制开关：True=记录所有用户，False=仅记录神秘人（默认开启，监听即记录）

# 性能开关：普通观众（is_regular=True）的进场/弹幕是否实时推给前端。
# 关掉后 Egress 大幅下降，数据完整性不受影响：
#   进场 —— 本来就不写库，只推送，前端还因不在 FEED_TYPE_MAP 里而全部丢弃，关掉零损失；
#   弹幕 —— 仍走 _write_all_user / _save_interaction 入库，公屏改由 /api/feed 定时拉取。
# 神秘人的事件不受这两个开关影响，始终实时推送。
_PUSH_REGULAR_ENTER = False
_PUSH_REGULAR_CHAT = False
_private_name_cache = {}  # (room_id:display) -> real_nickname, 私密直播间送礼拿到真实名后缓存


def _max_rooms():
    """本地默认 10 个；Railway 可通过 MAX_ROOMS 调整。"""
    try:
        return max(1, int(os.getenv('MAX_ROOMS', '10')))
    except (TypeError, ValueError):
        return 10


def _gift_message_count(message):
    """抖音连送数量主要在 repeatCount，旧礼物则可能只填 comboCount。"""
    repeat_count = int(getattr(message, 'repeatCount', 0) or 0)
    combo_count = int(getattr(message, 'comboCount', 0) or 0)
    return max(repeat_count, combo_count, 1)

# ========== SQLite 持久化 ==========
import sqlite3
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = resolve_database_path(os.environ, _PROJECT_DIR)

# 只读接口共享缓存：同一个厅同一秒内只算一次，所有查看者共用，
# CPU 开销因此与在线人数无关。只放与「谁在看」无关的数据。
_READ_CACHE = response_cache.ResponseCache(
    ttl=resolve_response_cache_ttl(os.environ)
)


def _cached_json(key, build):
    """缓存 (payload, status)、算 ETag、内容没变就回 304。

    缓存的是数据而不是 Response 对象——Response 会被 after_request 逐请求改写
    （比如写 Set-Cookie），跨请求复用同一个实例会串数据。

    条件请求：直播间不是每 2 秒都有人送礼，大部分轮询拿到的数据一模一样，
    却照样把整份榜单重发一遍（日榜实测 35.2 KB）。带上 ETag 后浏览器会自动
    捎上 If-None-Match，没变就回 304 空响应，前端无需任何改动。
    """
    payload, status = _READ_CACHE.get_or_compute(key, build)
    if status != 200:
        # 错误响应不参与条件请求，免得把错误状态缓存住
        return jsonify(payload), status
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    etag = '"%s"' % hashlib.md5(body.encode('utf-8')).hexdigest()[:20]
    if request.headers.get('If-None-Match') == etag:
        response = app.response_class(status=304)
    else:
        response = app.response_class(body, mimetype='application/json')
    response.headers['ETag'] = etag
    # no-cache 不是「别缓存」，而是「可以存但每次都回来问」——正是我们要的
    response.headers['Cache-Control'] = 'no-cache'
    return response, response.status_code
ensure_database_parent(_DB_PATH)
_last_cleanup_time = 0


def _ensure_column(conn, table_name, column_name, definition):
    columns = {
        row[1] for row in conn.execute(f'PRAGMA table_info({table_name})').fetchall()
    }
    if column_name not in columns:
        conn.execute(
            f'ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}'
        )

def _init_db():
    conn = sqlite3.connect(_DB_PATH)
    # WAL 模式：多人同时读 + 后台监听持续写，默认回滚日志会读写互锁。
    # WAL 让读写并行，是持久设置，写进数据库文件本身。
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mystery_records (
            sec_uid TEXT NOT NULL,
            display TEXT,
            real_name TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            extra TEXT DEFAULT '{}',
            last_room_id TEXT DEFAULT '',
            seen_room_ids TEXT DEFAULT '',
            first_seen INTEGER DEFAULT 0,
            last_seen INTEGER DEFAULT 0,
            enter_count INTEGER DEFAULT 0,
            gift_count INTEGER DEFAULT 0,
            chat_count INTEGER DEFAULT 0,
            is_regular INTEGER DEFAULT 0,
            PRIMARY KEY (sec_uid, display)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_mr_last_room ON mystery_records(last_room_id)
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS display_names (
            sec_uid TEXT NOT NULL,
            display TEXT NOT NULL,
            seen_count INTEGER DEFAULT 1,
            first_seen INTEGER DEFAULT 0,
            last_seen INTEGER DEFAULT 0,
            PRIMARY KEY (sec_uid, display)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS interaction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            sec_uid TEXT NOT NULL,
            display TEXT NOT NULL,
            type TEXT NOT NULL,
            content TEXT DEFAULT '',
            gift_count INTEGER DEFAULT 1,
            timestamp INTEGER NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_interaction_log
        ON interaction_log(sec_uid, timestamp)
    ''')
    gift_columns = {
        'gift_id': "TEXT DEFAULT ''",
        'gift_quantity': 'INTEGER DEFAULT 1',
        'unit_diamonds': 'INTEGER',
        'total_diamonds': 'INTEGER',
        'price_known': 'INTEGER DEFAULT 0',
        'quantity_verified': 'INTEGER DEFAULT 0',
        'recipient_key': "TEXT DEFAULT ''",
        'recipient_name': "TEXT DEFAULT ''",
        'event_key': "TEXT DEFAULT ''",
        'price_source': "TEXT DEFAULT ''",
        'ticket_count': 'INTEGER',
        'ticket_source': "TEXT DEFAULT 'unknown'",
        'room_ticket_total': 'INTEGER',
    }
    for column_name, definition in gift_columns.items():
        _ensure_column(conn, 'interaction_log', column_name, definition)
    conn.execute('''
        UPDATE interaction_log
        SET gift_quantity = gift_count
        WHERE type = 'gift'
    ''')
    conn.execute('''
        UPDATE interaction_log
        SET ticket_count = total_diamonds,
            ticket_source = 'gift_price'
        WHERE type = 'gift'
          AND ticket_count IS NULL
          AND total_diamonds IS NOT NULL
          AND total_diamonds > 0
          AND quantity_verified = 1
    ''')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_event_key
        ON interaction_log(room_id, event_key)
        WHERE event_key <> ''
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_interaction_room_time
        ON interaction_log(room_id, timestamp)
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_levels (
            user_key TEXT PRIMARY KEY,
            consume_level INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS room_search_history (
            input_text TEXT PRIMARY KEY,
            nickname TEXT NOT NULL DEFAULT '',
            room_id TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0
        )
    ''')
    init_gift_catalog(conn)
    init_leaderboard_schema(conn)
    identity_registry.init_identity_schema(conn)
    try:
        mic_silence.init_silence_schema(conn)
    except Exception as exc:
        # _init_db 是模块级执行，这里抛出去 = gunicorn 加载不了 app = 整个
        # 服务起不来。静默提醒挂掉绝不能挡住直播监听，跟旁边那行
        # import_legacy_mystery_records 一个道理。
        print(f'[静默] 建表失败，本功能停用但服务照常: {exc}', flush=True)
    conn.commit()
    try:
        # 旧神秘人迁移必须幂等，且失败不能挡住服务启动。
        identity_registry.import_legacy_mystery_records(conn)
    except Exception as exc:
        print(f'[身份库] 旧神秘人导入失败，不影响启动: {exc}', flush=True)
    conn.close()


def _cleanup_expired_data(now=None, force=False):
    """清理 7 天前的互动和普通资料；永久神秘人资料不动。"""
    global _last_cleanup_time
    now = int(time.time()) if now is None else int(now)
    if not force and now - _last_cleanup_time < 3600:
        return {'interactions': 0, 'regular_profiles': 0, 'skipped': True}
    cutoff = now - 7 * 86400
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        lock_completed_weeks(conn, now)
        interaction_cursor = conn.execute(
            'DELETE FROM interaction_log WHERE timestamp < ?', (cutoff,)
        )
        regular_cursor = conn.execute('''
            DELETE FROM mystery_records
            WHERE is_regular = 1 AND last_seen < ?
        ''', (cutoff,))
        # 静默告警和它的叉掉记录跟这里其它表一样按 7 天清；mic_silence_records
        # 证据表故意不在这里出现——那张表存在的全部意义就是在原始数据和告警
        # 都清完之后还留着证据，这个豁免是设计决定，不是漏清。
        alerts_cursor = conn.execute(
            'DELETE FROM mic_silence_alerts WHERE created_at < ?', (cutoff,))
        dismissals_cursor = conn.execute(
            'DELETE FROM mic_silence_dismissals WHERE alert_id NOT IN '
            '(SELECT id FROM mic_silence_alerts)')
        conn.commit()
        deleted = {
            'interactions': max(0, interaction_cursor.rowcount),
            'regular_profiles': max(0, regular_cursor.rowcount),
            'silence_alerts': max(0, alerts_cursor.rowcount),
            'silence_dismissals': max(0, dismissals_cursor.rowcount),
            'skipped': False,
        }
    finally:
        conn.close()
    _last_cleanup_time = now
    return deleted


# ===== 默认厅自动监听 =====
# 通过 DEFAULT_WATCH_IDS 配置抖音号（逗号分隔）。room_id 每场直播都变，
# 所以名单只能记抖音号，启动时现解析成当天的 room_id。
# 策略：已在监听的厅零请求；掉线的进等待队列，每轮只查一个号（错峰，
# 避免并发请求尖峰）；主播没开播就等下一轮；下播由 room_offline 停掉
# 监听，下一轮自动回到等待队列——服务在线期间这些厅「尽可能在线」。
AUTO_WATCH_INTERVAL_SECONDS = 60



def default_watch_ids():
    raw = os.environ.get('DEFAULT_WATCH_IDS', '')
    return [part.strip() for part in raw.split(',') if part.strip()]


def _watched_douyin_ids():
    return {
        getattr(listener, 'douyin_id', '')
        for listener in listeners.values()
        if listener.running
    }


def _auto_watch_one(douyin_id):
    """接一个默认厅：开播就监听。顺序调用，天然错峰，不并发。"""
    try:
        info = get_room_id_by_douyin_id(douyin_id)
    except Exception as exc:
        print(f'[自动监听] 解析 {douyin_id} 失败: {exc}', flush=True)
        return
    if not info or not info.get('success'):
        return
    if not int(info.get('live_status') or 0):
        return  # 没开播，等下一轮
    room_id = str(info.get('room_id') or '')
    if not room_id or room_id == '0':
        return
    result = start_room_listener(
        room_id=room_id,
        nickname=info.get('nickname', ''),
        sec_uid=info.get('sec_uid', ''),
        douyin_id=douyin_id,
        anchor_id=str(info.get('anchor_id') or ''),
    )
    if result and result.get('success'):
        print(f'[自动监听] {douyin_id} 已开播，监听 {room_id}', flush=True)


def _is_temporary_room(room_id):
    """临时厅 = 正在监听、但不是由默认名单里的抖音号启动的厅。

    手输的号如果正好在名单里，会被认作默认厅——否则会出现「停了它 60 秒后
    又被 auto_watch_tick 拉回来」的怪现象。
    已停止的厅返回 False：那批数据已并入公共历史，谁都能查。
    """
    listener = listeners.get(str(room_id))
    if listener is None:
        return False
    return getattr(listener, 'douyin_id', '') not in default_watch_ids()


def _current_role():
    return str(getattr(getattr(g, 'current_user', None), 'role', '') or '')


def _deny_temporary_room(room_id):
    """非 super_admin 不得取临时厅的数据；拒绝返回 (响应, 403)，放行返回 None。

    必须在 _cached_json 之前调用：先鉴权再缓存，缓存内容才与「谁在看」无关。
    顺序写反会让 super_admin 算出的临时厅数据被普通用户命中——那是数据泄露。
    """
    if not _is_temporary_room(room_id):
        return None
    if _current_role() == 'super_admin':
        return None
    return jsonify({'success': False, 'error': '无权限'}), 403


def auto_watch_tick():
    """一轮把所有还没在监听的默认厅都补一遍，重启后秒级就位。

    已在监听的厅零请求；待接的顺序解析（非并发），5 个号就是 5 次普通
    请求，量很小，换来上线即尽快补齐而不是每 60 秒只接一个。
    """
    configured = default_watch_ids()
    if not configured:
        return
    for douyin_id in configured:
        if douyin_id in _watched_douyin_ids():
            continue
        _auto_watch_one(douyin_id)


def _auto_watch_loop():
    while True:
        time.sleep(AUTO_WATCH_INTERVAL_SECONDS)
        try:
            auto_watch_tick()
        except Exception as exc:
            print(f'[自动监听] tick 异常: {exc}', flush=True)


def silence_scan_once(now=None):
    """扫一轮所有在监听的厅，返回本轮新产生的告警数。

    整轮吞异常：这个功能挂掉绝不能影响直播监听。
    """
    now = int(time.time()) if now is None else int(now)
    threshold = resolve_silence_threshold(os.environ)
    fired = 0
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        print(f'[静默] 打不开数据库: {exc}', flush=True)
        return 0
    try:
        running_room_ids = set()
        for room_id, listener in list(listeners.items()):
            try:
                douyin_id = str(getattr(listener, 'douyin_id', '') or '')
                if not douyin_id:
                    continue          # 临时厅不支持订阅，跳过
                if not getattr(listener, 'running', False):
                    # 下播那一刻：还开着的告警不收尾就会永远停在 resolved_at=0，
                    # 前端会一直把它当「正在静默」显示，其实人早下麦了。
                    mic_silence.close_room(conn, room_id, now)
                    continue
                running_room_ids.add(str(room_id))
                fired += len(mic_silence.scan_room(
                    conn,
                    room_id,
                    list(getattr(listener, 'mic_users', []) or []),
                    str(getattr(listener, 'sec_anchor_id', '') or ''),
                    douyin_id,
                    str(getattr(listener, 'nickname', '') or ''),
                    now,
                    threshold,
                ))
            except Exception as exc:
                print(f'[静默] 扫描 {room_id} 失败: {exc}', flush=True)

        # 上面这个循环只看得到还在 listeners 里的厅。/api/stop、stop_all
        # 会把厅整个从 listeners 删掉，per-listener 的收尾钩子根本轮不到它，
        # 所以另按状态表里实际剩下的 room_id 扫一遍：谁不在这一轮的运行厅
        # 集合里，谁就该收尾。顺带也清掉重新部署留下的孤儿行，不用再单独
        # 写一个启动时的扫尾。
        try:
            orphan_room_ids = {
                str(row[0]) for row in conn.execute(
                    'SELECT DISTINCT room_id FROM mic_silence_state')
            } - running_room_ids
            for room_id in orphan_room_ids:
                mic_silence.close_room(conn, room_id, now)
        except Exception as exc:
            print(f'[静默] 收尾孤儿房间失败: {exc}', flush=True)
    finally:
        conn.close()
    return fired


def start_silence_thread():
    interval = resolve_silence_scan_interval(os.environ)

    def loop():
        while True:
            time.sleep(interval)
            try:
                silence_scan_once()
            except Exception as exc:
                print(f'[静默] 本轮异常: {exc}', flush=True)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def start_auto_watch_thread():
    if not default_watch_ids():
        return None
    print(f'[自动监听] 已启动，名单: {default_watch_ids()}', flush=True)
    # 启动即补齐一次，不必等后台线程首个 60 秒 sleep 才接第一个厅。
    try:
        auto_watch_tick()
    except Exception as exc:
        print(f'[自动监听] 启动首轮异常: {exc}', flush=True)
    thread = threading.Thread(target=_auto_watch_loop, daemon=True)
    thread.start()
    return thread


# 测试与外部脚本使用的公开名。
init_db = _init_db

_init_db()
_cleanup_expired_data(force=True)
# 注意：start_auto_watch_thread() 会立即 tick 一次，用到 listeners 等
# 后面才定义的全局，因此启动调用放在模块末尾，不能在这里调。

def _save_mystery_record(room_id, sec_uid, display, real_name, extra, event_type,
                         timestamp=None, is_regular=0, room_nickname=None,
                         is_private=False, count_event=True):
    """保存或更新记录。无 sec_uid 的匿名用户不存储"""
    if not sec_uid:
        return
    # 2026-08 实测：匿名房同样能拿到真实身份，不再强制降级为普通用户，
    # 否则神秘人页和历史页永远是空的。（原逻辑：if is_private: is_regular = 1）
    if timestamp is None:
        timestamp = int(time.time())
    if extra and isinstance(extra, dict) and extra.get('nickname'):
        if not real_name or real_name == display:
            real_name = extra['nickname']
    try:
        extra = dict(extra or {})
        if room_nickname:
            extra['room_nickname'] = room_nickname
        conn = sqlite3.connect(_DB_PATH)

        if sec_uid:
            # 收集同一 sec_uid 所有旧记录的累积数据
            old_rows = conn.execute(
                'SELECT real_name, extra, seen_room_ids, enter_count, gift_count, chat_count FROM mystery_records WHERE sec_uid=?',
                (sec_uid,)
            ).fetchall()

            merged_seen = set()
            merged_enter = 0
            merged_gift = 0
            merged_chat = 0
            best_real_name = real_name
            merged_extra = dict(extra)
            for old in old_rows:
                if old[2]:
                    for rid in old[2].split(','):
                        if rid:
                            merged_seen.add(rid)
                merged_enter += old[3] or 0
                merged_gift += old[4] or 0
                merged_chat += old[5] or 0
                old_rn = (old[0] or '').strip()
                if old_rn and not old_rn.startswith('dou') and not old_rn.startswith('神秘人'):
                    if (not best_real_name) or best_real_name.startswith('dou') or best_real_name.startswith('神秘人'):
                        best_real_name = real_name = old_rn
                if old[1]:
                    try:
                        old_extra = json.loads(old[1]) if isinstance(old[1], str) else old[1]
                        for k, v in old_extra.items():
                            if not merged_extra.get(k):
                                merged_extra[k] = v
                    except:
                        pass

            # 删掉所有旧记录
            conn.execute('DELETE FROM mystery_records WHERE sec_uid=?', (sec_uid,))

            real_name = best_real_name or real_name
            extra = merged_extra
            enter_count = merged_enter
            gift_count = merged_gift
            chat_count = merged_chat
            seen_room_ids = merged_seen
        else:
            # 匿名用户：按 display 处理，不跨 display 合并
            enter_count = 0
            gift_count = 0
            chat_count = 0
            seen_room_ids = set()

        if room_id:
            seen_room_ids.add(str(room_id))
        seen_room_ids_str = ','.join(sorted(seen_room_ids))
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else '{}'

        # 按事件类型累加本次计数
        add_enter = 1 if count_event and event_type == 'enter' else 0
        add_gift = 1 if count_event and event_type == 'gift' else 0
        add_chat = 1 if count_event and event_type == 'chat' else 0

        conn.execute('''
            INSERT INTO mystery_records (sec_uid, display, real_name, extra, last_room_id, seen_room_ids,
                first_seen, last_seen, is_regular, enter_count, gift_count, chat_count, nickname)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (sec_uid or '', display or '', real_name, extra_json,
              str(room_id) if room_id else '', seen_room_ids_str,
              timestamp, timestamp, is_regular,
              enter_count + add_enter, gift_count + add_gift, chat_count + add_chat,
              room_nickname or ''))

        # display_name 记录：私密房跳过（马甲无编号无意义）
        if not is_private:
            conn.execute('''
                INSERT INTO display_names (sec_uid, display, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sec_uid, display) DO UPDATE SET
                    seen_count = seen_count + 1,
                    last_seen = MAX(last_seen, ?)
            ''', (sec_uid or '', display or '', timestamp, timestamp, timestamp))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ERROR] _save_mystery_record 失败: {e}", flush=True)

# ===== 消费等级（最近观察值）=====
# 等级只在直播事件里出现一瞬，不落库则历史公屏、周榜、在线名单都补不上。
# 口径：显示的是「最近一次见到的等级」，不是事件发生当时的等级。
# sec_uid -> 昵称。名单消息不下发时靠它给反推出来的麦位补名字；
# 这个映射不会变，查到就一直留着，避免反复问抖音。
_MIC_NICKNAME_CACHE = {}

_user_level_written = {}  # sec_uid -> 上次写入的等级；进程内去重，进场事件高频，不能每条都写库


def record_user_level(sec_uid, consume_level):
    try:
        level = int(consume_level or 0)
    except (TypeError, ValueError):
        return
    if not sec_uid or level <= 0:
        return
    if _user_level_written.get(sec_uid) == level:
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            'INSERT INTO user_levels (user_key, consume_level, updated_at) '
            'VALUES (?, ?, ?) '
            'ON CONFLICT(user_key) DO UPDATE SET '
            'consume_level = excluded.consume_level, updated_at = excluded.updated_at',
            (str(sec_uid), level, int(time.time())),
        )
        conn.commit()
        conn.close()
        _user_level_written[str(sec_uid)] = level
    except Exception as e:
        print(f'[LEVEL] 写入失败: {e}', flush=True)


def attach_consume_levels(rows, key_field):
    """给行列表就地盖上 consume_level（查不到一律 0，前端按 0 隐藏）。

    key 兼容两种格式：裸 sec_uid（在线名单、interaction_log），以及周榜的
    sender_key——它在无 sec_uid 时是 'display:昵称'，那类查不到属预期。
    """
    keys = {str(row.get(key_field) or '') for row in rows}
    merged_ids = {}
    for key in keys:
        if _MERGED_KEY_RE.fullmatch(key):
            merged_ids[int(key[3:])] = key
    keys = {k for k in keys if k and not k.startswith('display:')
            and not k.startswith('id:')}
    levels = {}
    if merged_ids:
        conn = None
        try:
            conn = sqlite3.connect(_DB_PATH)
            members = _merged_identity_members(conn, merged_ids)
            values = sorted({v for vs in members.values() for v in vs})
            by_value = {}
            for start in range(0, len(values), _SQL_VARS_PER_QUERY):
                chunk = values[start:start + _SQL_VARS_PER_QUERY]
                placeholders = ','.join('?' * len(chunk))
                by_value.update({
                    str(row[0]): int(row[1] or 0)
                    for row in conn.execute(
                        f'SELECT user_key, consume_level FROM user_levels '
                        f'WHERE user_key IN ({placeholders})', chunk)
                })
            for identity_id, key in merged_ids.items():
                found = [by_value.get(v, 0) for v in members.get(identity_id, [])]
                # 取这个身份下所有马甲里最高的等级：等级是账号属性，
                # 哪个马甲上记到过就是多少。
                if found:
                    levels[key] = max(found)
        except Exception as exc:
            app.logger.warning('合并键等级查询失败: %s', exc)
        finally:
            if conn is not None:
                conn.close()

    if keys:
        conn = None
        try:
            conn = sqlite3.connect(_DB_PATH)
            ordered = sorted(keys)
            for start in range(0, len(ordered), _SQL_VARS_PER_QUERY):
                chunk = ordered[start:start + _SQL_VARS_PER_QUERY]
                placeholders = ','.join('?' * len(chunk))
                # 必须 update：这里原来是 levels = {...} 重新赋值，把上面
                # 查好的合并键等级整个抹掉。周榜前 20 行必然混着合并键和
                # 裸 sec_uid，所以生产上必现，而单行的测试走不到这个分支。
                levels.update({
                    str(row[0]): int(row[1] or 0)
                    for row in conn.execute(
                        f'SELECT user_key, consume_level FROM user_levels '
                        f'WHERE user_key IN ({placeholders})', chunk)
                })
        except Exception as exc:
            app.logger.warning('等级查询失败: %s', exc)
        finally:
            # 原本这段没有 finally，连接一直漏着——测试里表现为临时库删不掉。
            if conn is not None:
                conn.close()
    for row in rows:
        row['consume_level'] = levels.get(str(row.get(key_field) or ''), 0)
    return rows


# 日榜默认只发前 N 名：实测前 20 名占全厅 74~99.9% 的票，后面一百多号人
# 多是 1、2 票，而整份榜单每 2 秒重发一次 —— 大厅 41KB 里 82% 是没人看的尾巴。
RANK_PAGE_SIZE = 20


# 主页反查失败时会把真名填成这些占位值，绝不能当成真名显示出去。
_PLACEHOLDER_REAL_NAMES = ('', '?', '-', '未知')


# 榜单按身份合并后的规范键。严格 ASCII 十进制、不带前导零：
# 'id:0706' 和 'id:706' 必须是两个不同的东西，不能撞到一起。
_MERGED_KEY_RE = re.compile(r'id:(?:0|[1-9][0-9]*)$')
# SQLite 老版本最多 999 个绑定变量。跟 leaderboards 那边取同一个保守值。
_SQL_VARS_PER_QUERY = 400


def _merged_identity_members(conn, merged_ids):
    """把 'id:N' 展开成这个身份下的所有原始标识符。

    user_levels、mystery_records 这些表都是按原始 sec_uid 存的，规范键
    在里面查不到。不展开的话，合并过的行等级会变成 0、徽章消失——而
    消失的恰恰是这次改动刚提到榜首的那批大额用户。
    """
    if not merged_ids:
        return {}
    ordered = sorted(merged_ids)
    members = {}
    for start in range(0, len(ordered), _SQL_VARS_PER_QUERY):
        chunk = ordered[start:start + _SQL_VARS_PER_QUERY]
        placeholders = ','.join('?' * len(chunk))
        for row in conn.execute(
            f'SELECT identity_id, identifier_value FROM identity_identifiers '
            f'WHERE identity_id IN ({placeholders})', chunk,
        ):
            members.setdefault(int(row[0]), []).append(str(row[1]))
    return members


def attach_real_names(rows, key_field):
    """给榜单行就地盖上 real_name：神秘人已解析出真名时带出来，否则留空串。

    只有「确实解析出来、且和马甲显示名不同」才算真名——普通用户的真名就是
    显示名，没必要在括号里重复一遍；解析失败的占位值一律当没有。

    key 兼容两种格式：裸 sec_uid，以及榜单的 sender_key/recipient_key——
    深度匿名没有 sec_uid 时它是 'display:昵称'，那类查不到属预期。
    """
    keys = {str(row.get(key_field) or '') for row in rows}
    # 'id:<identity_id>' 是榜单按身份合并后的规范键，它不在
    # identity_identifiers.identifier_value 里，得直接按 id 查档案；
    # 不单独处理的话，合并过的行反而查不出真名。
    merged_ids = {}
    for key in keys:
        # 只认严格的 ASCII 十进制、无前导零。str.isdigit() 连上标数字都认，
        # 'id:²' 能过守卫但 int() 会抛，而 int() 在 try 外面——直接 500。
        if _MERGED_KEY_RE.fullmatch(key):
            merged_ids[int(key[3:])] = key
    keys = {k for k in keys if k and not k.startswith('display:')
            and not k.startswith('id:')}
    names = {}
    if merged_ids:
        conn = None
        try:
            conn = sqlite3.connect(_DB_PATH)
            placeholders = ','.join('?' * len(merged_ids))
            ordered = sorted(merged_ids)
            for row in conn.execute(
                f'SELECT id, real_name FROM identity_profiles '
                f'WHERE id IN ({placeholders})', ordered,
            ):
                names[merged_ids[int(row[0])]] = str(row[1] or '')
        except Exception as exc:
            app.logger.warning('合并键真名查询失败: %s', exc)
        finally:
            if conn is not None:
                conn.close()
    if keys:
        conn = None
        try:
            conn = sqlite3.connect(_DB_PATH)
            placeholders = ','.join('?' * len(keys))
            ordered = sorted(keys)
            # 永久身份库优先级更低：先铺它，再让神秘人档案覆盖（后者更贴近本次马甲）。
            for row in conn.execute(
                f'SELECT i.identifier_value, p.real_name '
                f'FROM identity_identifiers i '
                f'JOIN identity_profiles p ON p.id = i.identity_id '
                f'WHERE i.identifier_value IN ({placeholders})',
                ordered,
            ):
                names[str(row[0])] = str(row[1] or '')
            for row in conn.execute(
                f'SELECT sec_uid, real_name FROM mystery_records '
                f'WHERE sec_uid IN ({placeholders})',
                ordered,
            ):
                names[str(row[0])] = str(row[1] or '')
        except Exception as exc:
            print(f'[真名] 查询失败: {exc}', flush=True)
        finally:
            # 异常路径也必须关连接，否则连接泄漏（Windows 上直接表现为文件占用）
            if conn is not None:
                conn.close()
    for row in rows:
        real = str(names.get(str(row.get(key_field) or '')) or '').strip()
        display = str(row.get('display') or '').strip()
        if real in _PLACEHOLDER_REAL_NAMES or real == display:
            real = ''
        row['real_name'] = real
    return rows


def chat_event_key(display, timestamp, content):
    """聊天的去重键。

    匿名房 sec_uid 每次进出都变，所以只能用「显示名 + 秒级时间 + 内容」，
    掺 sec_uid 会让匿名重复漏判。

    提到模块级是因为它不只给入库用：公屏的重复其实出在下发这一层——
    库层靠 (room_id, event_key) 唯一索引早就干净了，但 send_event 是无条件
    发的，管不到 SSE；而聊天事件不带 timestamp，前端只能拿 Date.now() 顶上，
    跟 /api/feed 返回的服务器接收秒对不上，同一条消息就被算成两条。
    处理器拿到这个键塞进事件里，SSE 和 /api/feed 才能用同一个身份。
    """
    # timestamp 为 None 时补当前秒：_save_interaction 一直有这个便利，
    # 这个函数既然公开给别人复用，就别在这儿变成一个会抛 TypeError 的坑。
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    digest = hashlib.md5(
        ('%s|%s|%s' % (display, timestamp, content)).encode('utf-8')
    ).hexdigest()[:16]
    return 'chat-%s' % digest


def _save_interaction(room_id, sec_uid, display, i_type, content='', gift_count=1,
                      timestamp=None, gift_id='', gift_quantity=None,
                      unit_diamonds=None, total_diamonds=None,
                      price_known=False, quantity_verified=False,
                      recipient_key='', recipient_name='', event_key='',
                      price_source='', ticket_count=None,
                      ticket_source='unknown', room_ticket_total=None):
    """保存单条互动记录（聊天/送礼）

    2026-08 起真匿名用户（mystery_man=2）不再下发 sec_uid，原先在此直接 return，
    导致他们的弹幕/礼物完全进不了公屏。改为只要有 room_id 和显示名就记录，
    sec_uid 允许为空（调用方会尽量传 webcast_uid 兜底）。身份表 mystery_records
    仍然要求真 sec_uid，不受此处影响。
    """
    if not room_id or not (sec_uid or display):
        return
    if timestamp is None:
        timestamp = int(time.time())
    gift_quantity = int(gift_count or 1) if gift_quantity is None else int(gift_quantity or 1)
    gift_count = gift_quantity if i_type == 'gift' else int(gift_count or 1)
    # 抖音弹幕协议会把同一条聊天推送两次；聊天此前不带 event_key，
    # (room_id, event_key) 唯一索引对空值不生效，于是重复入库、公屏出现两遍。
    # 调用方没给键就在这里补，键的算法见 chat_event_key。
    if i_type == 'chat' and not event_key:
        event_key = chat_event_key(display, timestamp, content)
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute('''
            INSERT INTO interaction_log (
                room_id, sec_uid, display, type, content, gift_count, timestamp,
                gift_id, gift_quantity, unit_diamonds, total_diamonds,
                price_known, quantity_verified, recipient_key, recipient_name,
                event_key, price_source, ticket_count, ticket_source,
                room_ticket_total
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(room_id, event_key) WHERE event_key <> '' DO UPDATE SET
                sec_uid = excluded.sec_uid,
                display = excluded.display,
                content = excluded.content,
                gift_count = excluded.gift_count,
                timestamp = MAX(interaction_log.timestamp, excluded.timestamp),
                gift_id = excluded.gift_id,
                gift_quantity = excluded.gift_quantity,
                unit_diamonds = excluded.unit_diamonds,
                total_diamonds = excluded.total_diamonds,
                price_known = excluded.price_known,
                quantity_verified = excluded.quantity_verified,
                recipient_key = excluded.recipient_key,
                recipient_name = excluded.recipient_name,
                price_source = excluded.price_source,
                ticket_count = excluded.ticket_count,
                ticket_source = excluded.ticket_source,
                room_ticket_total = excluded.room_ticket_total
        ''', (
            room_id, sec_uid or '', display, i_type, content, gift_count, timestamp,
            str(gift_id or ''), gift_quantity, unit_diamonds, total_diamonds,
            int(bool(price_known)), int(bool(quantity_verified)),
            recipient_key or '', recipient_name or '', event_key or '',
            price_source or '', ticket_count, ticket_source or 'unknown',
            room_ticket_total,
        ))
        conn.commit()
        conn.close()
        _cleanup_expired_data()
    except Exception as e:
        print(f"[DB] interaction save error: {e}", flush=True)

# ========== 全局身份库接入 ==========
# 身份库只提供附加信息：任何失败都降级成「身份待解析」，绝不阻断公屏、礼物、
# 票数和榜单，也绝不改写原始匿名编号、聊天内容和礼物记录。


def _identity_source(user, extra=None):
    """收集本次消息里全部可能的 UID；匿名房的 sec_uid 只存在于主页缓存里。"""
    source = {
        'sec_uid': getattr(user, 'sec_uid', '') or '',
        'webcast_uid': getattr(user, 'webcast_uid', '') or '',
        'user_id': getattr(user, 'id', 0) or 0,
    }
    if not source['sec_uid'] and isinstance(extra, dict):
        source['sec_uid'] = extra.get('sec_uid') or ''
    return source


def _identity_match(user, extra=None, display='', room_id=''):
    """按 UID 精确匹配身份，命中时顺带记录当天的匿名别名。"""
    try:
        source = _identity_source(user, extra)
        if not identity_registry.extract_identifiers(source):
            # 全是占位 UID 的真匿名用户，连数据库都不用开。
            return None
        conn = sqlite3.connect(_DB_PATH)
        try:
            matched = identity_registry.match_user(conn, source)
            if matched:
                identity_registry.record_alias(
                    conn, matched['identity_id'], display,
                    room_id=room_id, source='live_match',
                )
            return matched
        finally:
            conn.close()
    except Exception as exc:
        print(f'[身份库] 匹配失败，本次按身份待解析处理: {exc}', flush=True)
        return None


def _identity_write(register, user, extra, display, real_name, douyin_id,
                    consume_level, room_id):
    """身份写入的统一外壳：整理资料、捕获全部异常，失败只降级不抛。"""
    profile = dict(extra or {})
    source = _identity_source(user, extra)
    if source['sec_uid']:
        profile.setdefault('sec_uid', source['sec_uid'])
    douyin_id = str(douyin_id or '').strip()
    if douyin_id in ('?', '0', '111111'):
        douyin_id = ''
    try:
        conn = sqlite3.connect(_DB_PATH)
        try:
            return register(
                conn, source, room_id=room_id, display=display,
                real_name=real_name, douyin_id=douyin_id, profile=profile,
                consume_level=consume_level,
            )
        finally:
            conn.close()
    except Exception as exc:
        print(f'[身份库] 身份写入失败，本次不入库: {exc}', flush=True)
        return None


def _identity_register_gift(user, extra, display, real_name, douyin_id,
                            consume_level, is_mystery, room_id):
    """送礼路径：已解析神秘人无门槛入库，普通送礼人需 >= 41 级。"""
    register = (
        identity_registry.register_resolved_mystery if is_mystery
        else identity_registry.register_gift_sender
    )
    return _identity_write(register, user, extra, display, real_name,
                           douyin_id, consume_level, room_id)


def _identity_register_mystery(user, extra, display, real_name, douyin_id,
                               consume_level, room_id):
    """进场/聊天路径解析出真实资料的神秘人；普通用户绝不从这里入库。"""
    if not identity_registry.is_resolved_real_name(real_name, display):
        # 还没解析出真名的匿名用户占绝大多数，先挡掉，别白开数据库连接。
        return None
    return _identity_write(
        identity_registry.register_resolved_mystery, user, extra, display,
        real_name, douyin_id, consume_level, room_id,
    )


def _feed_matched_identities(sec_uids):
    """公屏历史按已落库的 UID 精确补身份；失败时整段降级为未匹配。"""
    matches = {}
    values = {str(value) for value in sec_uids if value}
    if not values:
        return matches
    try:
        conn = sqlite3.connect(_DB_PATH)
        try:
            for value in values:
                # interaction_log 的 sec_uid 列可能存 sec_uid，也可能存 webcast_uid。
                matched = identity_registry.match_user(
                    conn, {'sec_uid': value, 'webcast_uid': value}
                )
                if matched:
                    matches[value] = matched
        finally:
            conn.close()
    except Exception as exc:
        print(f'[身份库] 公屏历史身份补全失败: {exc}', flush=True)
    return matches


def _identity_match_roster(records, room_id):
    """当前在线名单的批量精确匹配；同一别名当天同厅只维护一行别名历史。"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        try:
            def matcher(record):
                matched = identity_registry.match_user(conn, record)
                if matched:
                    identity_registry.record_alias(
                        conn, matched['identity_id'],
                        record.get('nickname') or '',
                        room_id=room_id, source='online_roster',
                    )
                return matched

            return attach_matched_identities(records, matcher)
        finally:
            conn.close()
    except Exception as exc:
        print(f'[身份库] 在线名单身份补全失败: {exc}', flush=True)
        return attach_matched_identities(records, lambda record: None)


def _load_room_history(room_id):
    """加载某个直播间所有历史记录（仅神秘人）"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row

        # 查询该房间出现过的所有神秘人（seen_room_ids 包含 room_id）
        cur = conn.execute('''
            SELECT * FROM mystery_records
            WHERE is_regular = 0
            ORDER BY last_seen DESC
        ''')
        all_rows = [dict(r) for r in cur.fetchall()]

        # 过滤：seen_room_ids 包含该 room_id
        rows = []
        for r in all_rows:
            seen = (r.get('seen_room_ids') or '').split(',')
            if str(room_id) in seen:
                rows.append(r)

        # 拉取所有相关的 display_names
        if rows:
            all_sec_uids = list(set(r['sec_uid'] for r in rows if r.get('sec_uid')))
            placeholders = ','.join('?' * len(all_sec_uids)) if all_sec_uids else "'none'"
            cur2 = conn.execute(f'''
                SELECT sec_uid, display, last_seen, seen_count
                FROM display_names
                WHERE sec_uid IN ({placeholders})
            ''', all_sec_uids if all_sec_uids else [])
            dn_rows = cur2.fetchall()
        else:
            dn_rows = []

        conn.close()

        # 按 sec_uid 分组，合并 display_names
        display_map = {}
        for d in dn_rows:
            key = d['sec_uid']
            if key not in display_map:
                display_map[key] = []
            display_map[key].append({'display': d['display'], 'last_seen': d['last_seen'], 'seen_count': d['seen_count']})

        merged = {}
        for row in rows:
            key = row['sec_uid']
            if key not in merged:
                row['displays'] = sorted(display_map.get(key, []), key=lambda x: x['last_seen'], reverse=True)
                if row.get('extra'):
                    try:
                        row['extra'] = json.loads(row['extra'])
                    except:
                        row['extra'] = {}
                row['is_current'] = False
                merged[key] = row
            else:
                existing = merged[key]
                if (row.get('last_seen') or 0) > (existing.get('last_seen') or 0):
                    existing['last_seen'] = row['last_seen']

        result = list(merged.values())
        for item in result:
            ex = item.get('extra')
            if isinstance(ex, dict) and ex.get('room_nickname'):
                item['room_nickname'] = ex['room_nickname']
        result.sort(key=lambda x: x.get('last_seen', 0) or 0, reverse=True)
        return result
    except Exception as e:
        print(f"[DB] load error: {e}", flush=True)
        return []

def _load_history_by_nickname(nickname):
    """按直播间昵称查跨房间历史记录"""
    try:
        clean_name = nickname.replace(' ', '').lower()
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.execute('SELECT * FROM mystery_records WHERE is_regular = 0')
        all_rows = [dict(r) for r in cur.fetchall()]

        rows = []
        for r in all_rows:
            # 匹配 nickname 字段（line 184 存的 room_nickname）
            db_nick = (r.get('nickname') or '').replace(' ', '').lower()
            match1 = (clean_name in db_nick or db_nick in clean_name)
            # 匹配 extra JSON 中的 room_nickname（line 113 存）
            extra_str = r.get('extra') or '{}'
            match2 = False
            try:
                extra = json.loads(extra_str) if isinstance(extra_str, str) else extra_str
                extra_nick = (extra.get('room_nickname') or '').replace(' ', '').lower()
                match2 = (clean_name in extra_nick or extra_nick in clean_name)
            except:
                pass
            if match1 or match2:
                rows.append(r)

        print(f"[DEBUG] _load_history_by_nickname: name={nickname!r} clean={clean_name!r} total={len(all_rows)} matched={len(rows)}", flush=True)

        if not rows:
            conn.close()
            return []

        # 拉取 display_names（复用 _load_room_history 逻辑）
        all_sec_uids = list(set(r['sec_uid'] for r in rows if r.get('sec_uid')))
        placeholders = ','.join('?' * len(all_sec_uids)) if all_sec_uids else "'none'"
        cur2 = conn.execute(f'''
            SELECT sec_uid, display, last_seen, seen_count
            FROM display_names
            WHERE sec_uid IN ({placeholders})
        ''', all_sec_uids if all_sec_uids else [])
        dn_rows = cur2.fetchall()
        conn.close()

        # 按 sec_uid 分组，合并 display_names
        display_map = {}
        for d in dn_rows:
            key = d['sec_uid']
            if key not in display_map:
                display_map[key] = []
            display_map[key].append({'display': d['display'], 'last_seen': d['last_seen'], 'seen_count': d['seen_count']})

        merged = {}
        for row in rows:
            key = row['sec_uid']
            if key not in merged:
                row['displays'] = sorted(display_map.get(key, []), key=lambda x: x['last_seen'], reverse=True)
                if row.get('extra'):
                    try:
                        row['extra'] = json.loads(row['extra'])
                    except:
                        row['extra'] = {}
                row['is_current'] = False
                merged[key] = row
            else:
                existing = merged[key]
                if (row.get('last_seen') or 0) > (existing.get('last_seen') or 0):
                    existing['last_seen'] = row['last_seen']

        result = list(merged.values())
        for item in result:
            ex = item.get('extra')
            if isinstance(ex, dict) and ex.get('room_nickname'):
                item['room_nickname'] = ex['room_nickname']
        result.sort(key=lambda x: x.get('last_seen', 0) or 0, reverse=True)
        return result
    except Exception as e:
        print(f"[DB] load_by_nickname error: {e}", flush=True)
        return []

def gender_str(g):
    return GENDER_MAP.get(g, '未知')

def user_id_str(user):
    return (getattr(user, 'unique_id', '') or
            getattr(user, 'display_id', '') or
            str(user.short_id or '?'))

def get_consume_level(user):
    """从徽章列表提取荣誉等级（即消费等级）。

    抖音已弃用 user.consume_diamond_level（2026-08 实测 31/31 全为 0），
    等级挪进了 badge_image_list：荣誉徽章 alternative_text 含「荣誉等级」
    且无 name；粉丝团徽章带团名 name。徽章顺序不保证，不能取第一个了事。
    """
    fallback = 0
    try:
        for badge in user.badge_image_list:
            content = getattr(badge, 'content', None)
            if content is None:
                continue
            level = int(getattr(content, 'level', 0) or 0)
            if level <= 0:
                continue
            alt = getattr(content, 'alternative_text', '') or ''
            if '荣誉等级' in alt:
                return level
            # alternative_text 缺失时的兜底：带等级且无团名（粉丝团必有 name）
            if not alt and not (getattr(content, 'name', '') or ''):
                fallback = fallback or level
    except Exception:
        pass
    if fallback:
        return fallback
    # 旧字段兜底：抖音若恢复下发、或旧对象仍带值时照常生效
    try:
        return int(getattr(user, 'consume_diamond_level', 0) or 0)
    except (TypeError, ValueError):
        return 0


def get_badge_level(user):
    try:
        for badge in user.badge_image_list:
            c = badge.content if hasattr(badge, 'content') else None
            if c and hasattr(c, 'level') and c.level:
                sec_uid = getattr(user, 'sec_uid', None)
                if sec_uid: _level_cache[sec_uid] = c.level
                return c.level
    except: pass
    sec_uid = getattr(user, 'sec_uid', None)
    if sec_uid and sec_uid in _level_cache:
        return _level_cache[sec_uid]
    return 0

def lookup_user(sec_uid):
    global _last_api_call
    if sec_uid in _user_info_cache: return _user_info_cache[sec_uid]
    if not sec_uid or len(sec_uid) < 10: return {}
    for attempt in range(3):
        try:
            # 限流：两次API调用至少间隔0.3秒
            elapsed = time.time() - _last_api_call
            if elapsed < 0.3:
                time.sleep(0.3 - elapsed)
            _last_api_call = time.time()
            params = {'device_platform': 'webapp', 'aid': '6383',
                      'sec_user_id': sec_uid, 'version_code': '170400', 'msToken': ''}
            headers = {'User-Agent': 'Mozilla/5.0 ... Chrome/116.0.0.0',
                       'Referer': f'https://www.douyin.com/user/{sec_uid}'}
            resp = requests.get('https://www.douyin.com/aweme/v1/web/user/profile/other/',
                                params=params, headers=headers,
                                cookies={'ttwid': _TTWID}, verify=True, timeout=8)
            j = resp.json()
            if j.get('status_code') == 0 and 'user' in j:
                u = j['user']
                info = {'nickname': u.get('nickname','?'),
                        'unique_id': u.get('unique_id') or u.get('short_id','?'),
                        'ip_location': u.get('ip_location',''),
                        'follower_count': u.get('follower_count',0),
                        'following_count': u.get('following_count',0),
                        'total_favorited': u.get('total_favorited',0),
                        'aweme_count': u.get('aweme_count',0),
                        'signature': (u.get('signature') or '')[:100]}
                _user_info_cache[sec_uid] = info
                return info
            else:
                raise Exception(f"status_code={j.get('status_code')}")
        except Exception as e:
            print(f"[WARN] lookup_user 第{attempt+1}次失败({sec_uid}): {e}", flush=True)
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
    # 查询失败不缓存空结果，下次重试
    return {}

def is_real_mystery_user(user):
    display = (user.desensitized_nickname or '').strip()
    real_name = (user.nickname or '').strip()
    mystery_man = getattr(user, 'mystery_man', 0)
    is_masked = display.startswith('神秘人') and len(display) > 3
    is_dou_mystery = ((display.startswith('dou') and len(display) > 5) or
                      (real_name.startswith('dou') and len(real_name) > 5))
    # 2026-08 实测确认：mystery_man=1 是普通用户身上的其它标记（仍带 sec_uid/display_id/真名），
    # 只有 =2 才是真匿名（nickname 变 douXXXX、id 变 111111、sec_uid 字段直接不下发）。
    # 阈值必须是 >=2，改成 >=1 会把整屏普通观众全部误判为神秘人。
    is_deep = mystery_man >= 2
    return is_masked or is_dou_mystery or is_deep, display, real_name, mystery_man

# 抖音号允许字母、数字、下划线，也允许点和连字符。
# 原来写的是 ^[a-zA-Z0-9_]+$，不认点——demo.1 这种号直接掉进后面的链接
# 解析分支，全都不匹配，最后返回「无法解析链接或抖音号」。已配置的几个厅
# （demo_hall_a / demo_hall_b / demo_hall_c）都没有点，所以一直没暴露。
_DOUYIN_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def _looks_like_douyin_id(text):
    """判断这串输入是不是抖音号（而不是链接）。

    放开点之后必须挡住裸域名：'live.douyin.com' 也满足字符集，
    当成抖音号丢给用户信息接口只会白跑一趟。
    """
    text = str(text or '')
    if not text or text.startswith('http'):
        return False
    if '/' in text or 'douyin.com' in text.lower():
        return False
    return bool(_DOUYIN_ID_RE.match(text))


def get_room_id_by_douyin_id(douyin_id):
    """通过抖音号/链接获取 room_id"""
    # 抖音号 → 用旧版v2 API查
    if _looks_like_douyin_id(douyin_id):
        try:
            _auth_cookies = dict(cu.dy_live_auth.cookie)
            resp = requests.get('https://www.douyin.com/web/api/v2/user/info/',
                params={'unique_id': douyin_id},
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36',
                         'Referer': 'https://www.douyin.com/'},
                cookies=_auth_cookies, verify=True, timeout=10)
            j = resp.json()
            if j.get('status_code') == 0 and 'user_info' in j:
                sec_uid = j['user_info']['sec_uid']
                nickname = j['user_info']['nickname']
                # 查直播状态
                resp2 = requests.get('https://www.douyin.com/aweme/v1/web/user/profile/other/',
                    params={'device_platform': 'webapp', 'aid': '6383',
                            'sec_user_id': sec_uid, 'version_code': '170400', 'msToken': ''},
                    headers={'User-Agent': 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36',
                             'Referer': f'https://www.douyin.com/user/{sec_uid}'},
                    cookies={'ttwid': _TTWID}, verify=True, timeout=10)
                j2 = resp2.json()
                if j2.get('status_code') == 0 and 'user' in j2:
                    u = j2['user']
                    return {'success': True, 'nickname': nickname,
                            'room_id': str(u.get('room_id', 0)),
                            'live_status': u.get('live_status', 0),
                            'sec_uid': sec_uid,
                            # 主播的数字 uid。在线名单接口要它，而这里本来
                            # 就拿到了，以前被丢掉。
                            'anchor_id': str(u.get('uid') or '')}
        except: pass
        return {'success': False, 'error': '查询失败，请检查抖音号是否存在'}
    # v.douyin.com 短链接
    match = re.search(r'v\.douyin\.com/(\w+)', douyin_id)
    if match:
        try:
            resp = requests.head(f'https://{match.group(0)}', allow_redirects=True,
                headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            url = resp.url
            rid = re.search(r'/(\d+)\??', url)
            if rid: return {'success': True, 'room_id': rid.group(1), 'type': 'live', 'live_status': 1}
            # 可能是用户主页 → 提取 sec_uid 查直播状态
            sec = re.search(r'sec_uid=([^&]+)', url)
            if sec:
                sec_uid = sec.group(1)
                try:
                    _auth_cookies = dict(cu.dy_live_auth.cookie)
                    resp2 = requests.get(
                        'https://www.douyin.com/aweme/v1/web/user/profile/other/',
                        params={'device_platform': 'webapp', 'aid': '6383',
                                'sec_user_id': sec_uid, 'version_code': '170400', 'msToken': ''},
                        headers={'User-Agent': 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36',
                                 'Referer': f'https://www.douyin.com/user/{sec_uid}'},
                        cookies={'ttwid': _TTWID}, verify=True, timeout=10)
                    j2 = resp2.json()
                    if j2.get('status_code') == 0 and 'user' in j2:
                        u = j2['user']
                        return {'success': True, 'nickname': u.get('nickname', ''),
                                'room_id': str(u.get('room_id', 0)),
                                'live_status': u.get('live_status', 0),
                                'sec_uid': sec_uid}
                except: pass
                return {'success': True, 'sec_uid': sec_uid, 'live_status': 0,
                        'room_id': '0', 'nickname': ''}
        except: pass
    # 直接是数字→当做room_id
    if douyin_id.isdigit():
        return {'success': True, 'room_id': douyin_id, 'type': 'room'}
    return {'success': False, 'error': '无法解析链接或抖音号'}

# ========== 房间监听器 ==========
listeners = {}
# 每个房间的 SSE 连接版本号，新连接 +1，旧连接检测到版本落后自动退出
# ===== SSE 事件广播 =====
# 每个浏览器连接一条独立队列，事件复制分发给全部订阅者。
# 原来是所有连接共用一条 queue.Queue：取走即消失，两个人看同一个厅
# 会瓜分消息；再配上「新连接把旧连接踢下线」的版本号机制，多人根本没法用。
SSE_QUEUE_MAXSIZE = 500


def _init_listener_subscribers(listener):
    listener._subscribers = []
    listener._subscriber_lock = threading.Lock()
    # listener.events 本身也是一个订阅者：既有测试与调试代码可以继续
    # 从它读事件；设上限防止没人消费时无限膨胀。
    listener.events = subscribe_events(listener)


def subscribe_events(listener, maxsize=SSE_QUEUE_MAXSIZE):
    channel = queue.Queue(maxsize=maxsize)
    with listener._subscriber_lock:
        listener._subscribers.append(channel)
    return channel


def unsubscribe_events(listener, channel):
    with listener._subscriber_lock:
        if channel in listener._subscribers:
            listener._subscribers.remove(channel)


def publish_event(listener, event):
    """分发给所有订阅者。

    某个浏览器卡住不取消息时，它的队列会满——此时丢弃该连接的这条事件，
    绝不阻塞监听线程，也不影响其他人。宁可让卡住的那个漏消息，
    也不能让一个坏连接拖垮整个厅。
    """
    with listener._subscriber_lock:
        channels = list(listener._subscribers)
    for channel in channels:
        try:
            channel.put_nowait(event)
        except queue.Full:
            pass

class RoomListener:
    def __init__(self, room_id, nickname='', sec_uid='', anchor_id=''):
        self.room_id = room_id
        self.nickname = nickname
        self.sec_uid = sec_uid
        _init_listener_subscribers(self)
        self.douyin_id = ''  # 启动它的抖音号；自动监听靠它识别厅
        self.running = False
        self.thread = None
        self.ws = None
        self.mystery_count = 0
        self.recent_mysteries = []
        self.recent_entries = deque(maxlen=500)
        self._gift_combo_tracker = GiftComboTracker()
        self._gift_catalog = {}
        self._gift_catalog_loaded_at = 0
        self._mystery_seq = 0
        self.online_snapshot = OnlineAudienceSnapshot(room_id)
        self.mic_users = []
        self.mic_fan_tickets = {}  # sec_uid -> 本场麦位收礼值(fan_ticket)
        self._mic_ticket_thread = None
        # 发现厅时抖音就给了主播的数字 uid，一并收下。少了它，
        # fetch_online_audience 凑不齐参数，在线名单只能干等，而另外两条
        # 补救路径都不可靠：麦上名单那条消息时有时无，抓直播间网页的正则
        # 被抖音改版打挂了。主播是固定的，从发现那一步带过来最省事。
        self.anchor_id = anchor_id or ''
        self.sec_anchor_id = sec_uid or ''
        self._online_poller = None
        self._last_anchor_lookup = 0
        self.is_private = False  # 是否为隐私直播间（sec_uid 为空即匿名模式）
        self.last_msg_time = time.time()  # 最后收到消息的时间，用于下播检测

    def start(self):
        self.running = True
        # 登记本场 room_id 属于哪个厅：room_id 每场直播都变，周榜要靠厅名把
        # 一周内的多场串起来，否则「周榜」只覆盖当前这一场（= 当天）。
        try:
            conn = sqlite3.connect(_DB_PATH)
            try:
                init_leaderboard_schema(conn)
                record_room_hall(conn, self.room_id, self.nickname, int(time.time()))
            finally:
                conn.close()
        except Exception as exc:
            print(f'[周榜] 厅名登记失败 {self.room_id}: {exc}', flush=True)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.running = False
        if self._online_poller:
            self._online_poller.stop()
        if self.ws:
            try: self.ws.close()
            except: pass
        if self._online_poller:
            self._online_poller.join(0.5)

    def _update_room_data_sync(self, payload):
        # 麦位实时票数（fan_ticket）只在登录态推送的 RoomDataSyncMessage 里，
        # 连麦布局消息只带房主。空结果不覆盖，避免偶发消息清掉已知票数。
        try:
            tickets = extract_linkmic_fan_tickets(payload)
        except Exception as exc:
            print(f'[LINKMIC] fan_ticket 解析失败: {exc}', flush=True)
            return
        if tickets:
            # 累积而非替换：RoomDataSyncMessage 可能是增量（每条只含部分麦位），
            # 直接替换会让增量消息抹掉其他麦位已知的票数。已下麦的 sec_uid
            # 留在 dict 里无害——渲染时按当前 mic_users 过滤。
            self.mic_fan_tickets.update(tickets)

    def _refresh_mic_state(self):
        """重拉握手响应，刷新麦上名单和麦位票。

        抖音把这两样都只放在 get_webcast_detail 的响应里，实时消息流一条都不推：

        - RoomDataSyncMessage（票）：2026-08-19 实测 150 秒窗口 LIVE=0、INIT=3
        - LinkmicPlaymodeMessage（名单）：2026-08-21 实测 121 秒窗口，5 条 Linkmic
          类消息全部到达于第 1.31~1.32 秒（连接瞬间的补发），之后 119 秒 LIVE=0

        所以不定时重拉，两样都会停在连接那一刻不动。票的重拉 2026-08-19 就加了，
        当时只按票命名、只取了票那一支，名单在这儿被原样丢掉，从此再不更新——
        下麦的人一直挂着，新上麦的人永远进不来。2026-08-21 线上实测：RUYUE·心愿厅
        名单卡了 146 分钟，8 个人 missing_ticks 全是 0，静默扫描对着 3 个空麦位
        各数出 4 次告警，而真正在麦上的 3 个人完全没被监控。

        这两样必须一起刷——它们本来就来自同一份响应，分开就会再错一次。
        """
        try:
            user_unique_id = cu.dy_live_auth.cookie.get(
                'uid', '1000000000000000023'
            )
            raw = DouyinAPI.get_webcast_detail(
                cu.dy_live_auth,
                str(user_unique_id),
                self.room_id,
                f'https://live.douyin.com/{self.room_id}',
            )
            frame = Live_pb2.LiveResponse()
            frame.ParseFromString(raw)
        except Exception as exc:
            # 拉取或解析失败保留上一次已知票数，不清空。
            print(f'[LINKMIC] 麦位票刷新失败 {self.room_id}: {exc}', flush=True)
            return
        got_roster = False
        seats = {}
        for item in frame.messagesList:
            # 与连接时那段（_run 里的 INIT 分支）保持一致：两种都要。
            # 一份响应里可能有多条 LinkmicPlaymode，其中带名单的只有一条，
            # 其余解出 0 人；_update_linkmic_users 对空结果直接返回，
            # 所以先后顺序无所谓。
            if item.method == 'WebcastLinkmicPlaymodeMessage':
                if self._update_linkmic_users(item.payload):
                    got_roster = True
            elif item.method == 'WebcastRoomDataSyncMessage':
                try:
                    seats.update(extract_linkmic_fan_tickets(item.payload))
                except Exception:
                    pass
                self._update_room_data_sync(item.payload)

        # 名单消息没下发时用本轮麦位反推。2026-08-23 线上：四个厅下播重开
        # 换 room_id 之后，带名单的那条 LinkmicPlaymode 一条都不发（老厅
        # 13694 字节 / 9 人，新厅只剩一条 86 字节的空壳），而 RoomDataSync
        # 照常报出 9 个麦位——服务端知道谁在麦上，只是没给名字。
        #
        # 只能用「本轮」的 seats，不能用 self.mic_fan_tickets：后者是累积的，
        # 下麦的人留在里面（渲染时按 mic_users 过滤所以无害），拿它反推会让
        # 下麦的人全部复活。
        if not got_roster and seats:
            names = {}
            try:
                names = self._mic_nickname_lookup(list(seats)) or {}
            except Exception as exc:
                print(f'[麦上] 反查昵称失败 {self.room_id}: {exc}', flush=True)
            self.mic_users = roster_from_fan_tickets(seats, names)

    def _start_mic_state_refresh(self):
        if self._mic_ticket_thread and self._mic_ticket_thread.is_alive():
            return
        interval = resolve_mic_ticket_interval(os.environ)

        def loop():
            while self.running:
                time.sleep(interval)
                if not self.running:
                    break
                self._refresh_mic_state()

        self._mic_ticket_thread = threading.Thread(target=loop, daemon=True)
        self._mic_ticket_thread.start()

    def _update_linkmic_users(self, payload):
        """返回是否真的拿到了名单——调用方要据此决定用不用反推兜底。"""
        users = extract_linkmic_users(payload)
        if not users:
            return False
        self.mic_users = users
        room_name = str(self.nickname or '').strip()
        owner = next(
            (user for user in users
             if room_name and str(user.get('nickname') or '').strip() == room_name),
            None,
        )
        if owner is None and self.sec_anchor_id:
            owner = next(
                (user for user in users
                 if user.get('sec_uid') == self.sec_anchor_id),
                None,
            )
        if owner:
            self.anchor_id = str(owner.get('user_id') or self.anchor_id or '')
            self.sec_anchor_id = str(
                owner.get('sec_uid') or self.sec_anchor_id or ''
            )
        return True

    def _mic_nickname_lookup(self, sec_uids):
        """麦位 sec_uid -> 昵称。

        名单消息不下发时只能拿到 sec_uid，名字分两层补：

        1. 本地历史（display_names / interaction_log）——免费，主持基本都是
           老面孔，2026-08-23 实测示例 9 个麦位 9 个全中
        2. 抖音用户主页接口——只给本地查不到的新人用。sec_uid 到昵称是固定
           映射，查到就永久缓存，不会反复请求

        第 2 层每轮最多问 2 个：所有厅共用一份 Cookie，被限流会连累正常监听
        （2026-08-23 排查时就把连接打掉过一次）。查不完的下一轮接着查。
        """
        wanted = [str(s or '') for s in (sec_uids or []) if str(s or '')]
        if not wanted:
            return {}
        names = {}
        pending = []
        for sec in wanted:
            cached = _MIC_NICKNAME_CACHE.get(sec)
            if cached:
                names[sec] = cached
            else:
                pending.append(sec)
        if pending:
            conn = sqlite3.connect(_DB_PATH)
            try:
                marks = ','.join('?' * len(pending))
                rows = conn.execute(
                    'SELECT sec_uid, display FROM display_names '
                    'WHERE sec_uid IN (%s) ORDER BY last_seen' % marks,
                    pending).fetchall()
                for sec, display in rows:
                    if display:
                        names[str(sec)] = str(display)
                still = [s for s in pending if s not in names]
                if still:
                    marks = ','.join('?' * len(still))
                    rows = conn.execute(
                        "SELECT sec_uid, display FROM interaction_log "
                        "WHERE sec_uid IN (%s) AND COALESCE(display,'') <> '' "
                        "ORDER BY timestamp" % marks, still).fetchall()
                    for sec, display in rows:
                        names[str(sec)] = str(display)
            except Exception as exc:
                print(f'[麦上] 本地反查昵称失败 {self.room_id}: {exc}', flush=True)
            finally:
                conn.close()
        unknown = [s for s in wanted if s not in names][:2]
        for sec in unknown:
            try:
                info = DouyinAPI.get_user_info(
                    cu.dy_live_auth, 'https://www.douyin.com/user/' + sec)
                user = (info.get('user') if isinstance(info, dict) else None) or {}
                nickname = str(user.get('nickname') or '')
            except Exception as exc:
                print(f'[麦上] 问抖音要昵称失败 {sec[:16]}: {exc}', flush=True)
                continue
            if nickname:
                names[sec] = nickname
        for sec, nickname in names.items():
            if nickname:
                _MIC_NICKNAME_CACHE[sec] = nickname
        return names

    def _anchor_info(self):
        if self.anchor_id and self.sec_anchor_id:
            return {
                'anchor_id': self.anchor_id,
                'sec_anchor_id': self.sec_anchor_id,
            }
        now = time.time()
        if now - self._last_anchor_lookup >= 30:
            self._last_anchor_lookup = now
            try:
                info = DouyinAPI.get_live_info(
                    cu.dy_live_auth, str(self.room_id)
                )
                if isinstance(info, dict):
                    self.anchor_id = str(info.get('anchor_id') or self.anchor_id or '')
                    self.sec_anchor_id = str(
                        info.get('sec_uid') or self.sec_anchor_id or ''
                    )
                else:
                    # get_live_info 抓直播间网页解主播信息，抖音 2026-08 改版后
                    # 五条正则挂了四条（roomId 变成数字、status_str 整个消失、
                    # anchor / sec_uid 的键名对不上），失败时返回 (None,None,None)。
                    # 以前这里静默略过，排查时以为「兜底跑过了只是没结果」。
                    # 主路径已改成从发现厅时直接带 anchor_id，这条只是安全网，
                    # 但它坏了必须能看见。
                    print(f'[在线名单] 房间 {self.room_id} 网页兜底解析失败'
                          f'（get_live_info 返回 {type(info).__name__}），'
                          f'主播身份只能靠麦上名单', flush=True)
            except Exception as exc:
                print(f"[在线名单] 房间 {self.room_id} 主播信息失败: {exc}", flush=True)
        return {
            'anchor_id': self.anchor_id,
            'sec_anchor_id': self.sec_anchor_id,
        }

    def _start_online_polling(self):
        if self._online_poller and self._online_poller.is_running:
            return
        self._online_poller = OnlineAudiencePoller(
            room_id=self.room_id,
            snapshot=self.online_snapshot,
            fetcher=lambda room_id, anchor_id, sec_anchor_id: fetch_online_audience(
                cu.dy_live_auth,
                room_id,
                anchor_id,
                sec_anchor_id,
                timeout=15,
            ),
            anchor_provider=self._anchor_info,
            interval=resolve_online_sync_interval(os.environ),
        )
        self._online_poller.start()

    def send_event(self, event_type, data):
        if event_type == 'mystery_enter':
            self.recent_entries.append({
                'type': 'enter',
                'room_id': self.room_id,
                'sec_uid': data.get('sec_uid', '') or data.get('webcast_uid', ''),
                'display': data.get('display', '') or '?',
                'real_name': data.get('real_name', '') or data.get('display', '') or '?',
                'matched_identity': data.get('matched_identity'),
                'content': '',
                'count': 1,
                'timestamp': int(data.get('timestamp') or time.time()),
            })
        record_user_level(data.get('sec_uid'), data.get('consume_level'))
        publish_event(self, {'type': event_type, 'data': data, 'time': time.time()})

    def _write_all_user(self, info):
        try:
            # 写入 SQLite
            extra = info.get('extra') or {}
            if info.get('unique_id') and not extra.get('unique_id'):
                extra['unique_id'] = info['unique_id']
            _save_mystery_record(
                info.get('room_id', self.room_id),
                info.get('sec_uid', '') or '',
                info.get('display', '') or '',
                info.get('real_name', '') or extra.get('nickname', '') or '',
                extra,
                info.get('event_type', 'enter'),
                is_regular=1,
                room_nickname=self.nickname
            )
        except:
            pass

    def _handle_chat_payload(self, payload):
        """解析并保存一条聊天；匿名房也必须走到这里。"""
        msg = Live_pb2.ChatMessage()
        msg.ParseFromString(payload)
        user = msg.user
        is_mystery, display, real_name, mm = is_real_mystery_user(user)

        # 时间戳和去重键在这里算一次，入库和 SSE 共用同一份。
        # 各算各的会让两边对不上——公屏重复正是这么来的。
        chat_ts = int(time.time())
        chat_key = chat_event_key(display, chat_ts, msg.content)

        # 私密直播间：先查送礼阶段积累的实名缓存。
        extra = None
        if not user.sec_uid:
            self.is_private = True
            cached = _private_name_cache.get(f"{self.room_id}:{display}")
            if cached:
                print(f"[CACHE] 缓存命中(chat): {display} -> {cached.get('nickname','?')}", flush=True)
                extra = cached
            else:
                print(f"[CACHE] 缓存未命中(chat): {self.room_id}:{display}", flush=True)

        if is_mystery:
            if not extra:
                extra = lookup_user(user.sec_uid)
            if extra and extra.get('nickname') and extra['nickname'] == display:
                is_mystery = False

        if is_mystery:
            badge_lv = get_badge_level(user)
            uid = user_id_str(user) or (extra.get('unique_id', '') if extra else '')
            chat_info = {
                'display': display, 'real_name': real_name,
                'content': msg.content, 'sec_uid': user.sec_uid,
                'badge_level': badge_lv,
                'consume_level': get_consume_level(user),
                'unique_id': uid,
                'mystery_man': mm,
                'is_regular': False,
            }
            if extra:
                if extra.get('nickname') and extra['nickname'] != real_name:
                    chat_info['real_name'] = extra['nickname']
                chat_info['extra'] = extra

            # 有真实身份才写身份表；没身份的（真匿名）仍然写互动并进公屏。
            has_id = bool(user.sec_uid) or (extra and extra.get('sec_uid'))
            chat_info['room_id'] = self.room_id
            chat_info['room_nickname'] = self.nickname
            chat_info['timestamp'] = chat_ts
            chat_info['event_key'] = chat_key
            if has_id:
                _save_mystery_record(
                    self.room_id, user.sec_uid or '', display, real_name, extra,
                    'chat', room_nickname=self.nickname, is_private=self.is_private,
                )
            _save_interaction(
                self.room_id,
                user.sec_uid or getattr(user, 'webcast_uid', '') or '',
                display, 'chat', content=msg.content,
                timestamp=chat_ts, event_key=chat_key,
            )
            chat_info['matched_identity'] = _identity_register_mystery(
                user, extra, display, chat_info['real_name'],
                chat_info['unique_id'], get_consume_level(user),
                self.room_id,
            ) or _identity_match(
                user, extra, display=display, room_id=self.room_id
            )
            self.send_event('mystery_chat', chat_info)
        elif _record_all_enabled:
            chat_real_name = extra['nickname'] if extra and extra.get('nickname') else real_name
            chat_info = {
                'display': display, 'real_name': chat_real_name,
                'content': msg.content, 'sec_uid': user.sec_uid,
                'badge_level': get_badge_level(user),
                'consume_level': get_consume_level(user),
                'unique_id': user_id_str(user),
                'is_regular': True, 'event_type': 'chat',
                'room_id': self.room_id,
                'room_nickname': self.nickname,
                'timestamp': chat_ts,
                'event_key': chat_key,
            }
            if extra:
                chat_info['extra'] = extra
            self._write_all_user(chat_info)
            _save_interaction(
                self.room_id,
                user.sec_uid or getattr(user, 'webcast_uid', '') or '',
                display, 'chat', content=msg.content,
                timestamp=chat_ts, event_key=chat_key,
            )
            chat_info['matched_identity'] = _identity_match(
                user, extra, display=display, room_id=self.room_id
            )
            if _PUSH_REGULAR_CHAT:
                self.send_event('mystery_chat', chat_info)

    def _handle_member_payload(self, payload):
        """进场只走实时事件；有可靠身份的真神秘人仅更新永久资料。"""
        msg = Live_pb2.MemberMessage()
        msg.ParseFromString(payload)
        user = msg.user
        is_mystery, display, real_name, mm = is_real_mystery_user(user)

        extra = None
        if not user.sec_uid:
            self.is_private = True
            cached = _private_name_cache.get(f"{self.room_id}:{display}")
            if cached:
                print(
                    f"[CACHE] 缓存命中(enter): {display} -> {cached.get('nickname', '?')}",
                    flush=True,
                )
                extra = cached
            else:
                print(f"[CACHE] 缓存未命中(enter): {self.room_id}:{display}", flush=True)

        if is_mystery:
            if not extra:
                extra = lookup_user(user.sec_uid)
            if extra and extra.get('nickname') and extra['nickname'] == display:
                is_mystery = False

        timestamp = int(time.time())
        room_fields = {
            'room_id': self.room_id,
            'room_nickname': self.nickname,
            'timestamp': timestamp,
        }

        if is_mystery:
            self._mystery_seq += 1
            resolved_name = real_name
            if extra and extra.get('nickname') and extra['nickname'] != resolved_name:
                resolved_name = extra['nickname']
            uid = user_id_str(user) or (extra.get('unique_id', '') if extra else '')
            info = {
                'display': display,
                'real_name': resolved_name,
                'unique_id': uid,
                'sec_uid': user.sec_uid,
                'webcast_uid': getattr(user, 'webcast_uid', '') or '',
                'gender': gender_str(user.gender),
                'consume_level': get_consume_level(user),
                'badge_level': get_badge_level(user),
                'mystery_man': mm,
                'mystery_seq': self._mystery_seq,
                'is_regular': False,
                **room_fields,
            }
            if extra:
                info['extra'] = extra

            self.mystery_count += 1
            self.recent_mysteries.append(info)
            if len(self.recent_mysteries) > 500:
                self.recent_mysteries = self.recent_mysteries[-500:]

            identity_sec_uid = user.sec_uid or (extra.get('sec_uid', '') if extra else '')
            if identity_sec_uid:
                _save_mystery_record(
                    self.room_id,
                    identity_sec_uid,
                    display,
                    resolved_name,
                    extra,
                    'enter',
                    timestamp=timestamp,
                    room_nickname=self.nickname,
                    is_private=self.is_private,
                    count_event=False,
                )
            info['matched_identity'] = _identity_register_mystery(
                user, extra, display, resolved_name, uid,
                get_consume_level(user), self.room_id,
            ) or _identity_match(
                user, extra, display=display, room_id=self.room_id
            )
            self.send_event('mystery_enter', info)
            return

        resolved_name = extra.get('nickname') if extra and extra.get('nickname') else real_name
        info = {
            'display': display,
            'real_name': resolved_name,
            'unique_id': user_id_str(user),
            'sec_uid': user.sec_uid,
            'webcast_uid': getattr(user, 'webcast_uid', '') or '',
            'gender': gender_str(user.gender),
            'consume_level': get_consume_level(user),
            'badge_level': get_badge_level(user),
            'mystery_man': mm,
            'is_regular': True,
            'event_type': 'enter',
            **room_fields,
        }
        if extra:
            info['extra'] = extra
        info['matched_identity'] = _identity_match(
            user, extra, display=display, room_id=self.room_id
        )
        if _PUSH_REGULAR_ENTER:
            self.send_event('mystery_enter', info)

    def _get_gift_catalog(self, force=False):
        """读取当前直播间礼物目录；失败时保留旧缓存并让价格显示为未知。"""
        now = time.time()
        if self._gift_catalog and not force and now - self._gift_catalog_loaded_at < 1800:
            return self._gift_catalog
        try:
            response = requests.get(
                'https://live.douyin.com/webcast/gift/list/',
                params={
                    'device_platform': 'webapp',
                    'aid': '6383',
                    'room_id': self.room_id,
                    'web_rid': self.room_id,
                },
                headers={
                    'User-Agent': HeaderBuilder.ua,
                    'Referer': f'https://live.douyin.com/{self.room_id}',
                },
                cookies=cu.dy_live_auth.cookie,
                verify=True,
                timeout=20,
            )
            response.raise_for_status()
            catalog = parse_gift_catalog(response.json())
            if catalog:
                self._gift_catalog = catalog
                self._gift_catalog_loaded_at = now
                try:
                    conn = sqlite3.connect(_DB_PATH)
                    conn.row_factory = sqlite3.Row
                    try:
                        recalculated = sync_douyin_prices(
                            conn, catalog, int(now)
                        )
                    finally:
                        conn.close()
                    if recalculated:
                        print(
                            f"[礼物目录] 已按抖音实时价回算 {recalculated} 条近期记录",
                            flush=True,
                        )
                except Exception as exc:
                    print(f"[礼物目录] 近期价格回算失败: {exc}", flush=True)
        except Exception as exc:
            print(f"[礼物目录] 房间 {self.room_id} 加载失败: {exc}", flush=True)
        return self._gift_catalog

    def _handle_gift_payload(self, payload, msg_id=0):
        """解析礼物、计算可验证钻石价值、保存并推送。"""
        msg = Live_pb2.GiftMessage()
        msg.ParseFromString(payload)
        user = msg.user
        is_mystery, display, real_name, mm = is_real_mystery_user(user)

        extra = lookup_user(user.sec_uid) if user.sec_uid else None
        if extra and extra.get('nickname'):
            if self.is_private:
                cached = dict(extra)
                cached['sec_uid'] = user.sec_uid
                _private_name_cache[f"{self.room_id}:{display}"] = cached
            if extra['nickname'] != real_name:
                real_name = extra['nickname']
        if is_mystery and extra and extra.get('nickname') == display:
            is_mystery = False

        gift_name = msg.gift.name if msg.gift and msg.gift.name else '?'
        gift_id = str(
            msg.giftId or (msg.gift.id if msg.gift else '') or f'name:{gift_name}'
        )
        catalog = self._get_gift_catalog()
        price = catalog.get(gift_id)
        combo_enabled = bool(
            (price and price.combo)
            or bool(getattr(msg.gift, 'combo', False))
            or getattr(msg, 'groupId', 0)
            or int(getattr(msg, 'comboCount', 0) or 0) > 1
            or int(getattr(msg, 'repeatCount', 0) or 0) > 1
        )
        sender_key = (
            user.sec_uid
            or getattr(user, 'webcast_uid', '')
            or user_id_str(user)
            or display
        )
        quantity = self._gift_combo_tracker.observe(
            self.room_id,
            sender_key,
            gift_id,
            msg_id,
            getattr(msg, 'groupId', 0),
            getattr(msg, 'repeatCount', 0),
            getattr(msg, 'comboCount', 0),
            bool(getattr(msg, 'repeatEnd', 0)),
            combo_enabled,
        )
        if quantity.duplicate:
            return

        embedded_unit_diamonds = int(
            getattr(msg.gift, 'diamondCount', 0) or 0
        ) or None
        live_unit_diamonds = (
            embedded_unit_diamonds
            or (price.unit_diamonds if price else None)
        )
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                observe_gift(
                    conn,
                    gift_id,
                    gift_name,
                    live_unit_diamonds,
                    int(time.time()),
                )
                price_resolution = resolve_gift_price(
                    conn, gift_id, live_unit_diamonds, gift_name
                )
            finally:
                conn.close()
            unit_diamonds = price_resolution.unit_diamonds
            price_source = price_resolution.source
        except Exception as exc:
            print(f"[礼物库] {gift_id} 记录失败: {exc}", flush=True)
            unit_diamonds = live_unit_diamonds
            price_source = 'douyin' if live_unit_diamonds else 'unknown'
        price_known, total_diamonds = gift_value(
            unit_diamonds,
            quantity.display_count,
            quantity.verified,
        )
        ticket_count, ticket_source = resolve_event_tickets(
            int(getattr(msg, 'fanTicketCount', 0) or 0),
            quantity.display_count,
            total_diamonds,
            quantity.verified,
        )
        room_ticket_total = int(
            getattr(msg, 'roomFanTicketCount', 0) or 0
        ) or None

        to_user = msg.toUser
        to_user_id = user_id_str(to_user)
        if to_user_id == '?':
            to_user_id = ''
        recipient_key = (
            to_user.sec_uid
            or getattr(to_user, 'webcast_uid', '')
            or to_user_id
        )
        recipient_name = to_user.nickname or ''
        if not recipient_key:
            recipient_key = f'room:{self.room_id}'
            recipient_name = self.nickname or self.room_id

        identity_key = user.sec_uid or getattr(user, 'webcast_uid', '') or ''
        gift_info = {
            'display': display,
            'real_name': real_name,
            'sec_uid': user.sec_uid,
            'webcast_uid': getattr(user, 'webcast_uid', '') or '',
            'gift_id': gift_id,
            'gift_name': gift_name,
            'count': quantity.display_count,
            'gift_quantity': quantity.display_count,
            'quantity_verified': quantity.verified,
            'unit_diamonds': unit_diamonds,
            'total_diamonds': total_diamonds,
            'price_known': price_known,
            'price_source': price_source,
            'ticket_count': ticket_count,
            'ticket_source': ticket_source,
            'room_ticket_total': room_ticket_total,
            'combo_key': quantity.event_key,
            'recipient_key': recipient_key,
            'recipient_name': recipient_name,
            'badge_level': get_badge_level(user),
            'consume_level': get_consume_level(user),
            'unique_id': user_id_str(user) or (extra.get('unique_id', '') if extra else ''),
            'is_regular': not is_mystery,
            'event_type': 'gift',
            'room_id': self.room_id,
            'room_nickname': self.nickname,
            'timestamp': int(time.time()),
        }
        if extra:
            gift_info['extra'] = extra

        if is_mystery:
            identity_sec_uid = user.sec_uid or (extra.get('sec_uid', '') if extra else '')
            if identity_sec_uid:
                _save_mystery_record(
                    self.room_id,
                    identity_sec_uid,
                    display,
                    real_name,
                    extra,
                    'gift',
                    room_nickname=self.nickname,
                    is_private=self.is_private,
                )
        elif user.sec_uid or (extra and extra.get('sec_uid')):
            self._write_all_user(gift_info)

        _save_interaction(
            self.room_id,
            identity_key,
            display,
            'gift',
            content=gift_name,
            gift_count=quantity.display_count,
            gift_id=gift_id,
            gift_quantity=quantity.display_count,
            unit_diamonds=unit_diamonds,
            total_diamonds=total_diamonds,
            price_known=price_known,
            quantity_verified=quantity.verified,
            recipient_key=recipient_key,
            recipient_name=recipient_name,
            event_key=quantity.event_key,
            price_source=price_source,
            ticket_count=ticket_count,
            ticket_source=ticket_source,
            room_ticket_total=room_ticket_total,
        )
        # 原始礼物和票数已经落库，身份库只在其后附加信息。
        gift_info['matched_identity'] = _identity_register_gift(
            user, extra, display, real_name, gift_info['unique_id'],
            get_consume_level(user), is_mystery, self.room_id,
        ) or _identity_match(
            user, extra, display=display, room_id=self.room_id
        )
        self.send_event('mystery_gift', gift_info)

    def _run(self):
        # ====== 匿名模式检测（在 WS 连接之前，只检测一次） ======
        if not self.is_private:
            try:
                _resp = requests.get(f'https://live.douyin.com/{self.room_id}',
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                    cookies=cu.dy_live_auth.cookie, verify=True, timeout=10)
                if 'live_room_mode' in _resp.text and ':1' in _resp.text.split('live_room_mode')[1][:20]:
                    self.is_private = True
                    self.send_event('room_anonymous', {
                        'message': '当前直播间为匿名模式。系统会继续同步在线名单并记录公屏互动；获取到可用身份线索时会尝试匹配，无法确认的用户将保持匿名。'
                    })
                    print(f"[匿名] 房间 {self.room_id} 已启用匿名模式监听（在线名单、聊天、礼物）", flush=True)
            except Exception as e:
                print(f"[匿名检测] {self.room_id} 失败: {e}", flush=True)

        reconnect_attempts = 0
        max_reconnects = 5
        while self.running and reconnect_attempts < max_reconnects:
            try:
                user_unique_id = cu.dy_live_auth.cookie.get('uid', '1000000000000000023')
                auth = cu.dy_live_auth

                # ====== 先调 get_webcast_detail 拿真实 cursor + internalExt ======
                try:
                    _ws_init = DouyinAPI.get_webcast_detail(
                        auth, str(user_unique_id), self.room_id,
                        f"https://live.douyin.com/{self.room_id}"
                    )
                    _init_frame = Live_pb2.LiveResponse()
                    _init_frame.ParseFromString(_ws_init)
                    _cursor = str(_init_frame.cursor)
                    _internal_ext = _init_frame.internalExt
                    for _item in _init_frame.messagesList:
                        if _item.method == 'WebcastLinkmicPlaymodeMessage':
                            self._update_linkmic_users(_item.payload)
                        elif _item.method == 'WebcastRoomDataSyncMessage':
                            self._update_room_data_sync(_item.payload)
                except Exception as e:
                    print(f"[WARN] get_webcast_detail failed: {e}, using defaults")
                    _cursor = '-1'
                    _internal_ext = ''

                self._start_online_polling()
                self._start_mic_state_refresh()

                sig = generate_signature(self.room_id, user_unique_id)

                params = Params()
                (params.add_param('app_name','douyin_web').add_param('version_code','180800')
                 .add_param('webcast_sdk_version','1.0.15').add_param('update_version_code','1.0.15')
                 .add_param('compress','gzip').add_param('device_platform','web')
                 .add_param('cookie_enabled','true').add_param('screen_width','1707')
                 .add_param('screen_height','960').add_param('browser_language','zh-CN')
                 .add_param('browser_platform','Win32').add_param('browser_name','Mozilla')
                 .add_param('browser_version',HeaderBuilder.ua.split('Mozilla/')[-1])
                 .add_param('browser_online','true').add_param('tz_name','Etc/GMT-8')
                 .add_param('cursor', _cursor).add_param('host','https://live.douyin.com')
                 .add_param('aid','6383').add_param('live_id','1').add_param('did_rule','3')
                 .add_param('endpoint','live_pc').add_param('support_wrds','1')
                 .add_param('user_unique_id',user_unique_id).add_param('im_path','/webcast/im/fetch/')
                 .add_param('identity','audience').add_param('need_persist_msg_count','15')
                 .add_param('insert_task_id','').add_param('live_reason','')
                 .add_param('room_id',self.room_id).add_param('heartbeatDuration','0')
                 .add_param('signature',sig))
                if _internal_ext:
                    params.add_param('internal_ext', _internal_ext)

                wss_url = f"wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/?{urlencode(params.get())}"

                def on_message(ws, message):
                    try:
                        frame = Live_pb2.PushFrame()
                        frame.ParseFromString(message)
                        origin_bytes = gzip.decompress(frame.payload)
                        response = Live_pb2.LiveResponse()
                        response.ParseFromString(origin_bytes)

                        if response.needAck:
                            s = Live_pb2.PushFrame()
                            s.payloadType = "ack"
                            s.payload = response.internalExt.encode('utf-8')
                            s.logId = frame.logId
                            ws.send(s.SerializeToString(), opcode=0x02)

                        for item in response.messagesList:
                            self.last_msg_time = time.time()
                            if item.method == 'WebcastMemberMessage':
                                self._handle_member_payload(item.payload)

                            elif item.method == 'WebcastChatMessage':
                                self._handle_chat_payload(item.payload)

                            elif item.method == 'WebcastGiftMessage':
                                try:
                                    self._handle_gift_payload(item.payload, msg_id=item.msgId)
                                except Exception as exc:
                                    print(f"[礼物] 处理失败: {exc}", flush=True)

                            elif item.method == 'WebcastRoomStatsMessage':
                                pass  # 忽略在线/累计，不刷屏

                            elif item.method == 'WebcastRoomRankMessage':
                                pass  # 忽略排行榜，不刷屏

                            elif item.method == 'WebcastRoomUserSeqMessage':
                                pass  # 忽略榜一二三，不刷屏

                            elif item.method == 'WebcastLinkmicPlaymodeMessage':
                                self._update_linkmic_users(item.payload)
                            elif item.method == 'WebcastRoomDataSyncMessage':
                                self._update_room_data_sync(item.payload)

                    except: pass

                def on_open(ws):
                    reconnect_attempts = 0  # 连接成功，重置重试计数
                    self.send_event('connected', {'room_id': self.room_id, 'nickname': self.nickname})
                    def ping():
                        while self.running:
                            try:
                                # 每30秒检查一次下播（如果超过3分钟没收到消息）
                                if time.time() - self.last_msg_time > 180:
                                    try:
                                        info = DouyinAPI.get_live_info(cu.dy_live_auth, str(self.room_id))
                                        if not info or not isinstance(info, dict) or info.get('room_status') != '2':
                                            self.send_event('room_offline', {'room_id': self.room_id, 'nickname': self.nickname, 'mystery_count': self.mystery_count})
                                            self.running = False
                                            break
                                    except:
                                        pass
                                f = Live_pb2.PushFrame()
                                f.payloadType = "hb"
                                ws.send(f.SerializeToString(), opcode=0x02)
                                time.sleep(10)
                            except: break
                    threading.Thread(target=ping, daemon=True).start()

                def on_close(ws, code, msg):
                    if self.running:
                        self.send_event('disconnected', {'reconnecting': True, 'code': code})
                    else:
                        self.send_event('disconnected', {'code': code, 'mystery_count': self.mystery_count})

                def on_error(ws, error):
                    # 原文可能带 wss URL 与签名参数，只写服务端日志；
                    # 前端拿到通用提示即可（handleEvent 里本就不使用它）。
                    print(
                        f"[监听] 房间 {self.room_id} WebSocket 错误: {error}",
                        flush=True,
                    )
                    self.send_event('error', {'error': '直播间连接异常'})

                self.ws = WebSocketApp(url=wss_url,
                    header={'Pragma':'no-cache','Accept-Language':'zh-CN,zh;q=0.9',
                            'User-Agent':HeaderBuilder.ua,
                            'Upgrade':'websocket','Cache-Control':'no-cache','Connection':'Upgrade'},
                    cookie=auth.cookie_str,
                    on_message=on_message, on_open=on_open,
                    on_error=on_error, on_close=on_close)
                self.ws.run_forever(origin='https://live.douyin.com')

                if self.running:
                    # 检查直播间是否还在播，防止下播后无限重连
                    try:
                        info = DouyinAPI.get_live_info(cu.dy_live_auth, str(self.room_id))
                        if not info or not isinstance(info, dict) or info.get('room_status') != '2':
                            self.send_event('room_offline', {'room_id': self.room_id, 'nickname': self.nickname, 'mystery_count': self.mystery_count})
                            self.running = False
                            break
                    except Exception:
                        pass

                    reconnect_attempts += 1
                    time.sleep(5)
            except Exception as e:
                if self.running:
                    self.send_event('error', {'error': f'连接异常: {str(e)}'})
                    time.sleep(5)

# ========== Flask 路由 ==========

@app.route('/')
def index():
    auth_extension = app.extensions['kekemi_auth']
    return render_template(
        'index.html',
        current_user=g.current_user,
        auth_enabled=auth_extension['config'].required,
    )

@app.route('/api/resolve', methods=['POST'])
def resolve():
    """解析抖音号/链接，返回直播间信息"""
    data = request.get_json()
    if not data or 'input' not in data:
        return jsonify({'success': False, 'error': '请输入抖音号或链接'})
    result = get_room_id_by_douyin_id(data['input'].strip())
    return jsonify(result)

@app.route('/api/start', methods=['POST'])
def start_listen():
    """开始监听直播间（上限由 MAX_ROOMS 配置）"""
    data = request.get_json()
    room_id = data.get('room_id', '')
    nickname = data.get('nickname', '')
    if not room_id:
        return jsonify({'success': False, 'error': '缺少room_id'})
    result = start_room_listener(
        room_id=room_id, nickname=nickname,
        sec_uid=data.get('sec_uid', '') or '',
        douyin_id=data.get('douyin_id', '') or '',
        # 解析那步就拿到了主播的数字 uid，一并收下。缺了它
        # _anchor_info 凑不齐参数，在线名单会一直「等待首次同步」。
        anchor_id=str(data.get('anchor_id', '') or ''),
    )
    return jsonify(result)


def start_room_listener(room_id, nickname='', sec_uid='', douyin_id='',
                        anchor_id=''):
    """启动一个房间监听。/api/start 与默认厅自动监听共用。

    性能说明：
      - 每个直播间 = 一个 Python 线程 + 一个 WebSocket 连接
      - 内存约 20-30MB / 房间；CPU 空闲时几乎为零
      - 主要瓶颈在抖音 API 限流，与房间数量无关
    """
    room_id = str(room_id)
    # 已在监听
    if room_id in listeners and listeners[room_id].running:
        return {'success': True, 'room_id': room_id, 'already': True,
                'is_temporary': _is_temporary_room(room_id),
                'douyin_id': str(getattr(listeners[room_id], 'douyin_id', '') or '')}

    max_rooms = _max_rooms()
    running_count = sum(1 for l in listeners.values() if l.running)
    if running_count >= max_rooms:
        return {'success': False, 'error': f'最多同时监听{max_rooms}个直播间，请先停止一个再试'}

    listener = RoomListener(room_id, nickname, sec_uid, anchor_id=anchor_id)
    # 记住抖音号：room_id 每场直播都变，自动监听靠它判断这个厅是否在线。
    listener.douyin_id = douyin_id
    listeners[room_id] = listener

    # 从数据库加载该房间之前抓到的神秘人资料卡；不伪造实时进场事件。
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.execute('''
            SELECT * FROM mystery_records
            WHERE is_regular = 0
            ORDER BY first_seen ASC
            LIMIT 100
        ''')
        all_db = [dict(r) for r in cur.fetchall()]
        # 过滤：seen_room_ids 包含当前 room_id
        db_records = []
        for r in all_db:
            seen = (r.get('seen_room_ids') or '').split(',')
            if str(room_id) in seen:
                db_records.append(r)
        conn.close()

        seq = 0
        for r in db_records:
            seq += 1
            extra = {}
            if r.get('extra'):
                try:
                    extra = json.loads(r['extra']) if isinstance(r['extra'], str) else r['extra']
                except:
                    extra = {}
            # 不再需要跨房间合并——DB 中已自然合并
            replay_info = {
                'display': r['display'],
                'real_name': r['real_name'] or r['display'],
                'unique_id': extra.get('unique_id', ''),
                'sec_uid': r['sec_uid'],
                'gender': '未知',
                'consume_level': 0,
                'badge_level': 0,
                'mystery_man': False,
                'mystery_seq': seq,
                'is_regular': False,
                'extra': extra,
                'room_id': room_id,
                'room_nickname': nickname,
            }
            listener.recent_mysteries.append(replay_info)
        listener.mystery_count = len(db_records)
        if seq > 0:
            print(f"[资料] 房间 {room_id} 已加载 {seq} 条历史神秘人记录", flush=True)
    except Exception as e:
        print(f"[资料] 加载失败: {e}", flush=True)

    listener.start()
    # 带上 is_temporary：前端靠它决定这个厅的叉号是「停止监听」还是「从页面收起」。
    # 不给的话，刚开的临时厅长得跟常驻厅一样，点叉只是藏起来，监听照跑。
    # douyin_id 同理：静默铃铛靠它匹配订阅，不给就得等 refreshRooms 那 5 秒才出现。
    return {'success': True, 'room_id': room_id, 'active': running_count + 1,
            'max': max_rooms, 'is_temporary': _is_temporary_room(room_id),
            'douyin_id': douyin_id}

@app.route('/api/stop', methods=['POST'])
def stop_listen():
    """停止指定监听"""
    data = request.get_json()
    room_id = data.get('room_id', '')
    ua = request.headers.get('User-Agent', 'unknown')
    print(f"[操作] 停止监听 room={room_id} | UA: {ua[:120]}", flush=True)
    if room_id in listeners:
        listeners[room_id].stop()
        del listeners[room_id]
        return jsonify({'success': True, 'room_id': room_id})
    return jsonify({'success': False, 'error': '未找到该监听'})

@app.route('/api/stop_all', methods=['POST'])
def stop_all():
    """停止所有监听"""
    ua = request.headers.get('User-Agent', 'unknown')
    print(f"[操作] 停止全部监听 | UA: {ua[:120]}", flush=True)
    for rid, listener in list(listeners.items()):
        listener.stop()
    listeners.clear()
    return jsonify({'success': True})

@app.route('/api/status')
def status():
    """查看所有监听器状态"""
    result = {'active': [], 'count': 0, 'max': _max_rooms()}
    is_super = _current_role() == 'super_admin'
    for rid, listener in listeners.items():
        if listener.running:
            temporary = _is_temporary_room(rid)
            # 临时厅只属于 super_admin：别人的列表里根本不出现。
            if temporary and not is_super:
                continue
            result['active'].append({
                'room_id': rid,
                'is_temporary': temporary,
                'nickname': listener.nickname,
                'douyin_id': str(getattr(listener, 'douyin_id', '') or ''),
                'mystery_count': listener.mystery_count,
                'unique_count': len(set(m.get('sec_uid') for m in listener.recent_mysteries if m.get('sec_uid'))),
                'online_count': len(listener.online_snapshot.read().get('records', [])),
            })
    result['count'] = len(result['active'])
    return jsonify(result)

@app.route('/api/toggle_record_all', methods=['POST'])
def toggle_record_all():
    """开关：记录全部用户（仅后端记录，前端仍只显示神秘人）"""
    global _record_all_enabled
    data = request.get_json()
    _record_all_enabled = data.get('enabled', False)
    return jsonify({'success': True, 'record_all': _record_all_enabled})

@app.route('/api/record_all_status')
def record_all_status():
    """获取当前录制状态"""
    return jsonify({'record_all': _record_all_enabled})


@app.route('/api/online/<room_id>')
def online_audience(room_id):
    """返回单个正在监听房间的共享在线快照，不触发浏览器级抓取。"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    try:
        return _cached_json(
            ('online', str(room_id)),
            lambda: _online_audience_payload(room_id),
        )
    except Exception as exc:
        return api_error(exc), 500


def _online_audience_payload(room_id):
    listener = listeners.get(str(room_id))
    if not listener or not listener.running:
        return {'success': False, 'error': '未找到该直播间监听器'}, 404
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        now = int(time.time())
        day_start = beijing_day_start_timestamp(now)
        ranks = query_spender_rank_since(conn, room_id, day_start)
        mystery_rows = [dict(row) for row in conn.execute('''
            SELECT sec_uid, display, real_name, extra
            FROM mystery_records
            WHERE is_regular = 0 AND seen_room_ids LIKE ?
        ''', (f'%{room_id}%',)).fetchall()]
    finally:
        conn.close()
    spend_by_user = {
        str(row.get('sender_key') or ''): int(row.get('known_diamonds') or 0)
        for row in ranks
    }
    state = listener.online_snapshot.read()
    records = merge_online_snapshot(
        state.get('records', []),
        listener.mic_users,
        spend_by_user,
        listener.mic_fan_tickets,
    )
    records = attach_mystery_profiles(records, mystery_rows)
    records = _identity_match_roster(records, str(room_id))
    attach_consume_levels(records, 'sec_uid')
    for record in records:
        # 麦上主持使用连麦快照里抖音展示的实时收票；普通观众仍按
        # 本厅北京时间当天实际送礼价值排行。
        if record.get('is_mic'):
            record['known_tickets'] = int(
                record.get('received_tickets') or 0
            )
        else:
            record['known_tickets'] = int(
                record.get('known_diamonds') or 0
            )
    return {
        'success': True,
        'room_id': str(room_id),
        'room_nickname': listener.nickname,
        'records': records,
        'count': len(records),
        'updated_at': int(state.get('updated_at') or 0),
        'checked_at': int(state.get('checked_at') or 0),
        'stale': bool(state.get('stale')),
        'error': state.get('error') or '',
        'poll_interval': resolve_online_sync_interval(os.environ),
        'ranking_scope': 'beijing_today',
        'day_start': day_start,
    }, 200

@app.route('/api/real_names')
def real_names():
    """返回所有已缓存的demo_004映射（sec_uid → 信息）"""
    return jsonify({
        'success': True,
        'users': _user_info_cache,
        'private_names': {k: v for k, v in _private_name_cache.items()},
        'count': len(_user_info_cache)
    })

@app.route('/api/history_rooms')
def history_rooms():
    """返回有历史记录的直播间列表"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur = conn.execute('''
            SELECT last_room_id, seen_room_ids, last_seen, extra
            FROM mystery_records WHERE is_regular = 0
            ORDER BY last_seen DESC
        ''')
        # 从 seen_room_ids + last_room_id 重建房间列表
        room_map = {}  # room_id -> {last_seen, count}
        for r in cur.fetchall():
            last_room = r[0]
            seen = (r[1] or '').split(',')
            last_seen = r[2]
            # 所有出现过的房间
            all_rooms = set(seen)
            if last_room:
                all_rooms.add(last_room)
            for rid in all_rooms:
                if not rid:
                    continue
                if rid not in room_map:
                    room_map[rid] = {'room_id': rid, 'last_seen': last_seen, 'mystery_count': 0}
                if (last_seen or 0) > (room_map[rid]['last_seen'] or 0):
                    room_map[rid]['last_seen'] = last_seen
                room_map[rid]['mystery_count'] += 1
        conn.close()
        rooms = sorted(room_map.values(), key=lambda x: x['last_seen'] or 0, reverse=True)
        # 补上房间昵称
        for r in rooms:
            listener = listeners.get(r['room_id'])
            if listener and listener.nickname:
                r['nickname'] = listener.nickname
            else:
                r['short_id'] = r['room_id'][:8]
        # 从数据库恢复房间昵称
        conn2 = sqlite3.connect(_DB_PATH)
        for r in rooms:
            cur2 = conn2.execute(
                "SELECT extra FROM mystery_records WHERE seen_room_ids LIKE ? AND extra LIKE '%room_nickname%' LIMIT 1",
                ('%' + r['room_id'] + '%',))
            row2 = cur2.fetchone()
            if row2:
                try:
                    ex = json.loads(row2[0])
                    if ex.get('room_nickname'):
                        r['nickname'] = ex['room_nickname']
                except:
                    pass
        conn2.close()
        return jsonify({'success': True, 'rooms': rooms})
    except Exception as e:
        return api_error(e)

# ========== 搜索历史 API ==========

@app.route('/api/search_history/save', methods=['POST'])
def search_history_save():
    """保存搜索历史（按 input_text 去重）"""
    try:
        data = request.get_json(force=True)
        input_text = (data.get('input') or '').strip()
        nickname = (data.get('nickname') or '').strip()
        room_id = (data.get('room_id') or '').strip()
        if not input_text:
            return jsonify({'success': False, 'error': 'input is required'})
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            'INSERT OR REPLACE INTO room_search_history (input_text, nickname, room_id, created_at) VALUES (?, ?, ?, ?)',
            (input_text, nickname, room_id, int(time.time()))
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return api_error(e)

@app.route('/api/search_history/list')
def search_history_list():
    """返回最近 20 条搜索历史"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur = conn.execute(
            'SELECT input_text, nickname, room_id, created_at FROM room_search_history ORDER BY created_at DESC LIMIT 20'
        )
        rows = cur.fetchall()
        conn.close()
        data = [{'input_text': r[0], 'nickname': r[1], 'room_id': r[2], 'created_at': r[3]} for r in rows]
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return api_error(e)

@app.route('/api/search_history/delete', methods=['POST'])
def search_history_delete():
    """删除一条搜索历史"""
    try:
        data = request.get_json(force=True)
        input_text = (data.get('input') or '').strip()
        if not input_text:
            return jsonify({'success': False, 'error': 'input is required'})
        conn = sqlite3.connect(_DB_PATH)
        conn.execute('DELETE FROM room_search_history WHERE input_text = ?', (input_text,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return api_error(e)

@app.route('/api/history_all')
def history_all():
    """返回指定直播间的历史神秘人记录（跨会话持久化）"""
    room_id = request.args.get('room_id', '')
    if not room_id:
        return jsonify({'success': False, 'error': '缺少room_id'})
    from datetime import datetime, timezone, timedelta
    # 北京时间
    beijing_tz = timezone(timedelta(hours=8))
    beijing_now = datetime.now(beijing_tz)
    # 计算凌晨3点截止线：如果现在>=今天3点，用今天3点；否则用昨天3点
    today_3am = beijing_now.replace(hour=3, minute=0, second=0, microsecond=0)
    if beijing_now >= today_3am:
        cutoff = today_3am
    else:
        cutoff = today_3am - timedelta(days=1)
    cutoff_ts = int(cutoff.timestamp())
    # dou 马甲按当天午夜判断
    today_midnight = beijing_now.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_ts = int(today_midnight.timestamp())

    history = _load_room_history(room_id)
    # 标记马甲状态：同类替换（dou只留最新dou，神秘人只留最新神秘人），不同类型都保留
    for item in history:
        displays = item.get('displays', []) or []
        if not displays:
            continue

        # 按类型分组
        mystery = [d for d in displays if d.get('display','').startswith('神秘人')]
        dou = [d for d in displays if d.get('display','').startswith('dou')]
        other = [d for d in displays if not d.get('display','').startswith('神秘人') and not d.get('display','').startswith('dou')]

        new_displays = []

        # 神秘人：今天3点后出现过 → 有效✅，否则已失效
        if mystery:
            mystery.sort(key=lambda x: x.get('last_seen', 0))
            latest_m = mystery[-1]
            today_m = [d for d in mystery if d.get('last_seen', 0) >= cutoff_ts]
            if today_m:
                latest_m['is_current'] = True
            else:
                latest_m['is_current'] = False
            new_displays.append(latest_m)

        # dou：当天午夜后有2+不同 → 最新稳定✅，否则仅供参考
        if dou:
            dou.sort(key=lambda x: x.get('last_seen', 0))
            today_d = [d for d in dou if d.get('last_seen', 0) >= midnight_ts]
            latest_d = dou[-1]
            if len(today_d) >= 2:
                latest_d['is_current'] = True
            else:
                latest_d['is_current'] = False
            new_displays.append(latest_d)

        # 其他：不显示（用户只要神秘人和dou两种）

        if new_displays:
            new_displays.sort(key=lambda x: x.get('last_seen', 0))
            item['displays'] = new_displays
            item['is_current'] = any(d.get('is_current') for d in new_displays)
            # 如果 extra 里有demo_004，用它覆盖
            extra_nickname = (item.get('extra') or {}).get('nickname')
            if extra_nickname and extra_nickname != item.get('real_name'):
                item['real_name'] = extra_nickname
            # 从 extra 提取房间昵称
            extra_rn = (item.get('extra') or {}).get('room_nickname')
            if extra_rn:
                item['room_nickname'] = extra_rn
                item['nickname'] = extra_rn
        else:
            item['displays'] = []
            item['is_current'] = False
    return jsonify({'success': True, 'records': history, 'count': len(history)})


@app.route('/api/history_by_nickname')
def history_by_nickname():
    """按直播间昵称查跨房间历史记录"""
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': '缺少name参数'})

    from datetime import datetime, timezone, timedelta
    beijing_tz = timezone(timedelta(hours=8))
    beijing_now = datetime.now(beijing_tz)
    today_3am = beijing_now.replace(hour=3, minute=0, second=0, microsecond=0)
    if beijing_now >= today_3am:
        cutoff = today_3am
    else:
        cutoff = today_3am - timedelta(days=1)
    cutoff_ts = int(cutoff.timestamp())
    today_midnight = beijing_now.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_ts = int(today_midnight.timestamp())

    history = _load_history_by_nickname(name)

    # 标记马甲状态（复用 history_all 的逻辑）
    for item in history:
        displays = item.get('displays', []) or []
        if not displays:
            continue
        mystery = [d for d in displays if d.get('display','').startswith('神秘人')]
        dou = [d for d in displays if d.get('display','').startswith('dou')]
        other = [d for d in displays if not d.get('display','').startswith('神秘人') and not d.get('display','').startswith('dou')]
        new_displays = []
        if mystery:
            mystery.sort(key=lambda x: x.get('last_seen', 0))
            latest_m = mystery[-1]
            today_m = [d for d in mystery if d.get('last_seen', 0) >= cutoff_ts]
            if today_m:
                latest_m['is_current'] = True
            else:
                latest_m['is_current'] = False
            new_displays.append(latest_m)
        if dou:
            dou.sort(key=lambda x: x.get('last_seen', 0))
            today_d = [d for d in dou if d.get('last_seen', 0) >= midnight_ts]
            latest_d = dou[-1]
            if len(today_d) >= 2:
                latest_d['is_current'] = True
            else:
                latest_d['is_current'] = False
            new_displays.append(latest_d)
        if new_displays:
            new_displays.sort(key=lambda x: x.get('last_seen', 0))
            item['displays'] = new_displays
            item['is_current'] = any(d.get('is_current') for d in new_displays)
            extra_nickname = (item.get('extra') or {}).get('nickname')
            if extra_nickname and extra_nickname != item.get('real_name'):
                item['real_name'] = extra_nickname
            extra_rn = (item.get('extra') or {}).get('room_nickname')
            if extra_rn:
                item['room_nickname'] = extra_rn
                item['nickname'] = extra_rn
        else:
            item['displays'] = []
            item['is_current'] = False
    return jsonify({'success': True, 'count': len(history), 'records': history})


@app.route('/api/history_all_all')
def history_all_all():
    """返回所有直播间的历史神秘人记录，不分房间"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row

        cur = conn.execute('''
            SELECT r.*
            FROM mystery_records r
            WHERE r.is_regular = 0
            ORDER BY r.last_seen DESC
            LIMIT 500
        ''')
        all_records = [dict(r) for r in cur.fetchall()]

        # 拉取 display_names
        cur2 = conn.execute('''
            SELECT sec_uid, display, last_seen, seen_count
            FROM display_names
            ORDER BY last_seen DESC
        ''')
        dn_rows = [dict(r) for r in cur2.fetchall()]
        conn.close()

        display_map = {}
        for d in dn_rows:
            su = d['sec_uid']
            if su not in display_map:
                display_map[su] = []
            display_map[su].append({
                'display': d['display'],
                'last_seen': d['last_seen'],
                'seen_count': d['seen_count']
            })

        for item in all_records:
            su = item.get('sec_uid', '') or ''
            if item.get('extra'):
                try:
                    item['extra'] = json.loads(item['extra'])
                except:
                    item['extra'] = {}
            item['displays'] = sorted(display_map.get(su, []), key=lambda x: x['last_seen'], reverse=True)

        # 标记马甲状态
        from datetime import datetime, timezone, timedelta
        beijing_tz = timezone(timedelta(hours=8))
        beijing_now = datetime.now(beijing_tz)
        today_3am = beijing_now.replace(hour=3, minute=0, second=0, microsecond=0)
        cutoff_ts = int(today_3am.timestamp()) if beijing_now >= today_3am else int((today_3am - timedelta(days=1)).timestamp())
        today_midnight = beijing_now.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ts = int(today_midnight.timestamp())

        for item in all_records:
            displays = item.get('displays', []) or []
            if not displays:
                item_display = item.get('display', '') or ''
                if item_display:
                    displays = [{'display': item_display, 'last_seen': item.get('last_seen', 0)}]
                    item['displays'] = displays
                else:
                    continue
            mystery = [d for d in displays if d.get('display','').startswith('神秘人')]
            dou = [d for d in displays if d.get('display','').startswith('dou')]
            new_displays = []
            if mystery:
                mystery.sort(key=lambda x: x.get('last_seen', 0))
                latest_m = mystery[-1]
                latest_m['is_current'] = (any(d.get('last_seen', 0) >= cutoff_ts for d in mystery) or
                                            (item.get('last_seen', 0) or 0) >= cutoff_ts)
                new_displays.append(latest_m)
            if dou:
                dou.sort(key=lambda x: x.get('last_seen', 0))
                latest_d = dou[-1]
                today_d = [d for d in dou if d.get('last_seen', 0) >= midnight_ts]
                latest_d['is_current'] = len(today_d) >= 2
                new_displays.append(latest_d)
            if new_displays:
                item['displays'] = new_displays
                item['is_current'] = any(d.get('is_current') for d in new_displays)
                extra_nickname = (item.get('extra') or {}).get('nickname')
                if extra_nickname and extra_nickname != item.get('real_name'):
                    item['real_name'] = extra_nickname
            else:
                item['displays'] = []

        return jsonify({'success': True, 'records': all_records, 'count': len(all_records)})
    except Exception as e:
        return api_error(e)

@app.route('/api/history/<room_id>')
def history(room_id):
    """获取当前监听房间的神秘人历史"""
    listener = listeners.get(room_id)
    if not listener:
        return jsonify({'success': False, 'error': '未找到监听器'})
    return jsonify({'success': True, 'mystery_count': listener.mystery_count,
                    'history': listener.recent_mysteries[-50:]})

@app.route('/api/all_records/<room_id>')
def all_records(room_id):
    """获取全部用户记录（从 SQLite 读取，已聚合）"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    hours = request.args.get('hours', default=0, type=int)
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row

        # 查全表，Python 侧按 seen_room_ids 过滤 + 时间过滤
        cur = conn.execute('''
            SELECT r.*,
                   GROUP_CONCAT(d.display || ':' || d.last_seen, '|') as all_displays
            FROM mystery_records r
            LEFT JOIN display_names d ON d.sec_uid = r.sec_uid
            GROUP BY r.sec_uid, r.display
            ORDER BY r.last_seen DESC
            LIMIT 500
        ''')

        now = int(time.time())
        rows = []
        cutoff = now - hours * 3600 if hours > 0 else 0
        for r in cur.fetchall():
            row = dict(r)
            # 过滤：seen_room_ids 包含 room_id
            seen = (row.get('seen_room_ids') or '').split(',')
            if str(room_id) not in seen:
                continue
            # 时间过滤
            if cutoff and (row.get('last_seen') or 0) < cutoff:
                continue
            if row.get('all_displays'):
                names = []
                for entry in row['all_displays'].split('|'):
                    parts = entry.rsplit(':', 1)
                    if len(parts) == 2:
                        names.append({'display': parts[0], 'last_seen': int(parts[1])})
                row['displays'] = sorted(names, key=lambda x: x['last_seen'], reverse=True)
            else:
                row['displays'] = []
            if row.get('extra'):
                try:
                    row['extra'] = json.loads(row['extra'])
                except:
                    row['extra'] = {}
            # 当前接口已经按 room_id 过滤；卡片和互动展开必须继续使用
            # “正在查看的房间”，不能跳到此用户最后出现的另一个房间。
            requested_room_id = str(room_id)
            row['room_id'] = requested_room_id
            current_listener = listeners.get(requested_room_id)
            if current_listener and current_listener.nickname:
                row['room_nickname'] = current_listener.nickname
            elif str(row.get('last_room_id') or '') == requested_room_id:
                row['room_nickname'] = (row.get('extra') or {}).get('room_nickname', '')
            else:
                row['room_nickname'] = requested_room_id
            rows.append(row)

        # “全部”页中的互动数量只按当前直播间的持久化明细计算。
        # 聊天/礼物最多保存 7 天；hours=2 时再收窄到最近 2 小时。
        activity_cutoff = now - 7 * 86400
        if cutoff:
            activity_cutoff = max(activity_cutoff, cutoff)
        activity_rows = conn.execute('''
            SELECT sec_uid,
                   SUM(CASE WHEN type = 'chat' THEN 1 ELSE 0 END) AS chat_count,
                   SUM(CASE WHEN type = 'gift' THEN 1 ELSE 0 END) AS gift_event_count,
                   SUM(CASE WHEN type = 'gift' THEN COALESCE(gift_quantity, 0) ELSE 0 END)
                       AS gift_quantity_total
            FROM interaction_log
            WHERE room_id = ? AND timestamp >= ?
            GROUP BY sec_uid
        ''', (str(room_id), activity_cutoff)).fetchall()
        activity_by_user = {row['sec_uid']: dict(row) for row in activity_rows}

        for row in rows:
            stats = activity_by_user.get(row.get('sec_uid'), {})
            row['enter_count'] = 0
            row['chat_count'] = int(stats.get('chat_count') or 0)
            row['gift_event_count'] = int(stats.get('gift_event_count') or 0)
            row['gift_quantity_total'] = int(stats.get('gift_quantity_total') or 0)
            # 保留旧字段给尚未更新的前端，但含义统一为礼物总数量。
            row['gift_count'] = row['gift_quantity_total']
        conn.close()

        # 按 sec_uid 去重（DB 已自然合并，这里只是兜底）
        seen_keys = {}
        for row in rows:
            key = row.get('sec_uid', '') or row.get('display', '')
            if key not in seen_keys:
                seen_keys[key] = row
            elif (row.get('last_seen') or 0) > (seen_keys[key].get('last_seen') or 0):
                seen_keys[key] = row

        # 每种 display 类型只保留最新的，不同类型都保留
        deduped = []
        for row in seen_keys.values():
            names = row.get('displays', [])
            if names:
                mystery = [n for n in names if n.get('display','').startswith('神秘人')]
                dou = [n for n in names if n.get('display','').startswith('dou')]
                other = [n for n in names if not n.get('display','').startswith('神秘人') and not n.get('display','').startswith('dou')]
                filtered = []
                for group in [mystery, dou, other]:
                    if group:
                        group.sort(key=lambda x: x.get('last_seen', 0))
                        filtered.append(group[-1])
                filtered.sort(key=lambda x: x.get('last_seen', 0))
                row['display'] = filtered[-1]['display']
                row['displays'] = filtered
            deduped.append(row)
        deduped.sort(key=lambda x: x.get('last_seen', 0) or 0, reverse=True)
        return jsonify({'success': True, 'records': deduped})
    except Exception as e:
        return api_error(e, fallback=True)

@app.route('/api/interactions/<room_id>/<sec_uid>')
def get_interactions(room_id, sec_uid):
    """获取某个用户在某直播间的互动记录"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.execute('''
            SELECT type, content, gift_count, timestamp,
                   gift_id, gift_quantity, unit_diamonds, total_diamonds,
                   price_known, quantity_verified, recipient_name, event_key
            FROM interaction_log
            WHERE room_id = ? AND sec_uid = ? AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 100
        ''', (room_id, sec_uid, int(time.time()) - 7 * 86400))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({'success': True, 'interactions': rows})
    except Exception as e:
        return api_error(e)


@app.route('/api/spender_rank/<room_id>')
def spender_rank(room_id):
    """当前直播间最近 3 天的监听期游客钻石榜。"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    def build():
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            records = query_spender_rank(conn, room_id, int(time.time()))
        finally:
            conn.close()
        return {
            'success': True,
            'room_id': room_id,
            'window_days': 3,
            'scope': 'monitoring_period',
            'note': '仅统计本工具监听期间成功定价且数量可验证的礼物；价格未知礼物不计入钻石合计。',
            'records': records,
            'count': len(records),
        }, 200
    try:
        return _cached_json(('spender', str(room_id)), build)
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/daily_rank/<room_id>')
def daily_rank(room_id):
    """公开的北京时间当日游客榜与主持榜。"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    # limit=0 表示要全量（前端「显示全部」用）；不传就按默认只发前几名。
    raw_limit = request.args.get('limit')
    try:
        limit = RANK_PAGE_SIZE if raw_limit in (None, '') else max(0, int(raw_limit))
    except (TypeError, ValueError):
        limit = RANK_PAGE_SIZE

    def build():
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            result = query_daily_boards(conn, room_id, int(time.time()))
        finally:
            conn.close()
        visitors = result['visitors']
        hosts = result['hosts']
        visitor_total, host_total = len(visitors), len(hosts)
        if limit:
            visitors = visitors[:limit]
            hosts = hosts[:limit]
        # 只给真正要发出去的行补真名，省掉尾巴那一百多号人的查询
        attach_real_names(visitors, 'sender_key')
        attach_real_names(hosts, 'recipient_key')
        return {
            'success': True,
            'room_id': str(room_id),
            'scope': 'beijing_today',
            'day_start': result['day_start'],
            'day_end': result['day_end'],
            'note': '仅统计本工具今天监听期间捕获且票数可核算的礼物。',
            'visitors': visitors,
            'hosts': hosts,
            'visitor_total': visitor_total,
            'host_total': host_total,
            'visitors_truncated': bool(limit) and visitor_total > limit,
            'hosts_truncated': bool(limit) and host_total > limit,
        }, 200
    try:
        # limit 决定发多少行，必须进缓存键，否则截断版会被当全量返回
        return _cached_json(('daily', str(room_id), limit), build)
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/gift_assignments/<room_id>')
def admin_gift_assignments(room_id):
    """本地管理员查看单厅最近七天可修正的礼物。"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            records = list_adjustable_gifts(
                conn, room_id, int(time.time())
            )
        finally:
            conn.close()
        return jsonify({
            'success': True,
            'room_id': str(room_id),
            'records': records,
            'count': len(records),
        })
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/gift_assignments/<int:interaction_id>', methods=['PUT'])
def admin_update_gift_assignment(interaction_id):
    """保存主持归属修正；不覆盖原始礼物接收人。"""
    data = request.get_json(silent=True) or {}
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            adjustment = set_recipient_adjustment(
                conn,
                interaction_id,
                data.get('recipient_key'),
                data.get('recipient_name'),
                data.get('note'),
                data.get('actor') or 'local_admin',
                int(time.time()),
            )
        finally:
            conn.close()
        return jsonify({'success': True, 'adjustment': adjustment})
    except ValueError as exc:
        return business_error(exc, 400)
    except LookupError as exc:
        return business_error(exc, 404)
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/weekly_rank/<room_id>/periods')
def admin_weekly_rank_periods(room_id):
    """返回单厅已锁定的历史周榜周期。"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            lock_completed_weeks(conn, int(time.time()))
            periods = list_week_periods(conn, room_id)
        finally:
            conn.close()
        return jsonify({
            'success': True,
            'room_id': str(room_id),
            'periods': periods,
        })
    except Exception as exc:
        return api_error(exc), 500


def _silence_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _current_user_id():
    return str(getattr(getattr(g, 'current_user', None), 'id', '') or '')


@app.route('/api/admin/silence/alerts')
def silence_alerts():
    """不走 _READ_CACHE：那个缓存的前提是「结果与谁在看无关」
    （见 response_cache.py 开头），而订阅和叉掉都是 per-admin 的。
    为它开一个「带 user_id 就算例外」的口子，将来必然有人照着写一个
    忘了加键的路由。这个接口本来就是几个 admin 每 5 秒查一次的小查询。
    """
    try:
        now = int(time.time())
        user_id = _current_user_id()
        conn = _silence_conn()
        try:
            alerts = mic_silence.list_open_alerts(conn, user_id, now)
            unread = mic_silence.unread_counts_by_hall(conn, user_id, now)
        finally:
            conn.close()
        # unread_by_hall 给房间栏的铃铛用：铃铛挂在厅上，所以按厅给数。
        return jsonify({'success': True, 'alerts': alerts,
                        'count': len(alerts), 'unread_by_hall': unread,
                        # 阈值可配，前端别写死
                        'threshold': resolve_silence_threshold(os.environ)})
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/silence/alerts/read', methods=['POST'])
def silence_mark_read():
    """打开铃铛面板即视为读完，未读数归 0。

    复用 per-admin 的叉掉表，不另建已读表——两者语义本来就一样。
    """
    data = request.get_json(silent=True) or {}
    try:
        conn = _silence_conn()
        try:
            mic_silence.mark_alerts_read(
                conn, _current_user_id(),
                data.get('alert_ids') or [], int(time.time()))
        finally:
            conn.close()
        return jsonify({'success': True})
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/silence/watches')
def silence_watches():
    try:
        conn = _silence_conn()
        try:
            douyin_ids = mic_silence.list_watches(conn, _current_user_id())
        finally:
            conn.close()
        # 跟 /api/status 同一条规则：临时厅只属于 super_admin，这里没有
        # 单独把关，其他角色一样能在订阅列表页看到临时厅的厅名。
        is_super = _current_role() == 'super_admin'
        halls = {}
        for rid, listener in list(listeners.items()):
            if not getattr(listener, 'running', False):
                continue
            if _is_temporary_room(rid) and not is_super:
                continue
            douyin_id = str(getattr(listener, 'douyin_id', '') or '')
            if not douyin_id:
                continue
            halls[douyin_id] = str(getattr(listener, 'nickname', '') or '')
        return jsonify({
            'success': True,
            'douyin_ids': douyin_ids,
            'halls': halls,
        })
    except Exception as exc:
        return api_error(exc), 500


def _temporary_douyin_ids():
    """当前在跑、且判定为临时厅的监听器对应的抖音号集合。

    给 silence_set_watches 过滤用：非 super_admin 不能靠直接提交
    douyin_id 订阅到一个临时厅——那是 Fix F 刚补的那道墙隔壁的另一扇门，
    UI 摸不到，但 PUT 请求本身不知道调用者是从哪里拼出这个号的。
    """
    return {
        str(getattr(listener, 'douyin_id', '') or '')
        for rid, listener in list(listeners.items())
        if getattr(listener, 'running', False) and _is_temporary_room(rid)
    } - {''}


@app.route('/api/admin/silence/watches', methods=['PUT'])
def silence_set_watches():
    data = request.get_json(silent=True) or {}
    try:
        wanted = data.get('douyin_ids') or []
        if _current_role() != 'super_admin':
            blocked = _temporary_douyin_ids()
            wanted = [d for d in wanted if str(d) not in blocked]
        conn = _silence_conn()
        try:
            saved = mic_silence.set_watches(
                conn, _current_user_id(), wanted, int(time.time()))
        finally:
            conn.close()
        return jsonify({'success': True, 'douyin_ids': saved})
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/silence/records/<room_id>')
def silence_records(room_id):
    # 跟其它 9 个 per-room admin 路由一样，鉴权要在任何 DB 工作之前——
    # 顺序写反会让 super_admin 才能看的临时厅证据被普通用户翻到。
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    try:
        conn = _silence_conn()
        try:
            rows = conn.execute(
                'SELECT * FROM mic_silence_records WHERE room_id = ? '
                'ORDER BY created_at DESC LIMIT 100', (str(room_id),)
            ).fetchall()
        finally:
            conn.close()
        records = []
        for row in rows:
            item = dict(row)
            try:
                item['chat_snapshot'] = json.loads(item['chat_snapshot'] or '{}')
            except (TypeError, ValueError):
                item['chat_snapshot'] = {'buckets': []}
            records.append(item)
        return jsonify({'success': True, 'records': records,
                        'count': len(records)})
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/weekly_rank/<room_id>')
def admin_weekly_rank(room_id):
    """返回当前实时周榜或指定的永久锁定周榜。"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    raw_week_start = request.args.get('week_start')
    raw_limit = request.args.get('limit')
    try:
        limit = RANK_PAGE_SIZE if raw_limit in (None, '') else max(0, int(raw_limit))
    except (TypeError, ValueError):
        limit = RANK_PAGE_SIZE

    def build():
        week_start = None
        if raw_week_start not in (None, ''):
            week_start = int(raw_week_start)
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            result = query_weekly_board(
                conn, room_id, int(time.time()), week_start=week_start
            )
        finally:
            conn.close()
        visitors = result.get('visitors') or []
        hosts = result.get('hosts') or []
        visitor_total, host_total = len(visitors), len(hosts)
        if limit:
            visitors = visitors[:limit]
            hosts = hosts[:limit]
        attach_consume_levels(visitors, 'sender_key')
        # 神秘人已解析出真名的，在榜上用括号带出来（和日榜一致）
        attach_real_names(visitors, 'sender_key')
        attach_real_names(hosts, 'recipient_key')
        result['visitors'] = visitors
        result['hosts'] = hosts
        return {
            'success': True,
            **result,
            'visitor_total': visitor_total,
            'host_total': host_total,
            'visitors_truncated': bool(limit) and visitor_total > limit,
            'hosts_truncated': bool(limit) and host_total > limit,
        }, 200

    try:
        # week_start 决定查哪一周、limit 决定发多少行，两个都必须进缓存键，
        # 否则会串数据或把截断版当全量返回。
        return _cached_json(
            ('weekly', str(room_id), str(raw_week_start or ''), limit), build
        )
    except ValueError:
        return jsonify({'success': False, 'error': 'week_start 必须是时间戳'}), 400
    except LookupError as exc:
        return business_error(exc, 404)
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/host_gifts/<room_id>')
def host_gifts(room_id):
    """当前直播间最近 7 天的主播收礼明细和钻石总值。"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            result = query_host_gifts(conn, room_id, int(time.time()))
        finally:
            conn.close()
        return jsonify({
            'success': True,
            'room_id': room_id,
            'window_days': 7,
            'scope': 'monitoring_period',
            'note': '仅统计本工具监听期间成功定价且数量可验证的礼物；钻石总值不是平台结算收入。',
            'summary': result['summary'],
            'gifts': result['gifts'],
        })
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/gifts')
def admin_gifts():
    """本地管理员查看全局礼物库；后续接账号权限时复用同一接口。"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            records = list_gifts(conn)
            pending_names = list_pending_names(conn)
        finally:
            conn.close()
        return jsonify({
            'success': True,
            'records': records,
            'count': len(records),
            'pending_count': sum(1 for row in records if row.get('price_pending')),
            # 按 gift_id 列的那批看不见「同一个 id 下没定价的皮肤名」，
            # 它们不计入账目却在界面上不存在，只能单独带出来。
            'pending_names': pending_names,
            'pending_name_count': len(pending_names),
        })
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/gifts/<gift_id>', methods=['PUT'])
def admin_update_gift(gift_id):
    """保存一份全局管理员价格，并重算最近七天可信数量记录。"""
    data = request.get_json(silent=True) or {}
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                'SELECT gift_name FROM gift_catalog WHERE gift_id = ?',
                (str(gift_id),),
            ).fetchone()
            if existing is None:
                return jsonify({'success': False, 'error': '礼物库中不存在该礼物'}), 404
            result = set_manual_price(
                conn,
                gift_id=str(gift_id),
                gift_name=data.get('gift_name') or existing['gift_name'],
                unit_diamonds=data.get('unit_diamonds'),
                note=data.get('note', ''),
                reason=data.get('reason', ''),
                actor=data.get('actor', '') or 'local_admin',
                now=int(time.time()),
            )
            gift = next(
                row for row in list_gifts(conn)
                if row.get('gift_id') == str(gift_id)
            )
        finally:
            conn.close()
        return jsonify({
            'success': True,
            'gift': gift,
            'recalculated_rows': result['recalculated_rows'],
        })
    except ValueError as exc:
        return business_error(exc, 400)
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/gift-names', methods=['PUT'])
def admin_update_gift_name_price():
    """按礼物名设权威价。

    名字价在 resolve_gift_price 里优先级最高（抗 gift_id 名字漂移、
    多皮肤各价不同时唯一能表达的层），但在这之前它只有函数没有 HTTP
    入口，从来没被调用过——于是没定价的皮肤名永远补不上。
    """
    data = request.get_json(silent=True) or {}
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            result = set_name_price(
                conn,
                gift_name=data.get('gift_name'),
                unit_diamonds=data.get('unit_diamonds'),
                note=data.get('note', ''),
                reason=data.get('reason', ''),
                actor=data.get('actor', '') or 'local_admin',
                now=int(time.time()),
            )
        finally:
            conn.close()
        return jsonify({'success': True, 'result': result})
    except ValueError as exc:
        return business_error(exc, 400)
    except Exception as exc:
        return api_error(exc), 500


@app.route('/api/admin/gifts/<gift_id>/history')
def admin_gift_history(gift_id):
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            records = list_price_history(conn, str(gift_id))
        finally:
            conn.close()
        return jsonify({
            'success': True,
            'gift_id': str(gift_id),
            'records': records,
            'count': len(records),
        })
    except Exception as exc:
        return api_error(exc), 500

def _build_feed_events(room_id, limit=200, since=0):
    """从数据库构建公屏历史事件列表（与 /api/feed/<room_id> 同逻辑）。

    since>0 时只返回该时间点之后的事件。公屏改成定时轮询之后，每次都拉
    全量 200 条等于把省下来的推送流量成倍还回去（实测单次 15.7 KB，
    每 3 秒一次就是 452 MB/天/人）。

    边界用 >= 而不是 >：同一秒可能有多条，用 > 会漏掉边界上的。
    重叠的那几条前端按 feedEventKey 去重，多传无害，漏传是真丢数据。
    """
    events = []
    since = max(0, int(since or 0))
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row

        # 1. 从 interaction_log 取 chat + gift 事件
        cur = conn.execute('''
            SELECT sec_uid, display, type, content, gift_count, timestamp,
                   gift_id, gift_quantity, unit_diamonds, total_diamonds,
                   price_known, quantity_verified, recipient_name, event_key
            FROM interaction_log
            WHERE room_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC
        ''', (room_id, max(since, int(time.time()) - 7 * 86400)))

        for r in cur.fetchall():
            events.append({
                'type': r['type'],
                'display': r['display'] or '',
                'real_name': r['display'] or '',
                'content': r['content'] or '',
                'count': r['gift_count'] or 1,
                'gift_id': r['gift_id'] or '',
                'gift_quantity': r['gift_quantity'] or r['gift_count'] or 1,
                'unit_diamonds': r['unit_diamonds'],
                'total_diamonds': r['total_diamonds'],
                'price_known': bool(r['price_known']),
                'quantity_verified': bool(r['quantity_verified']),
                'recipient_name': r['recipient_name'] or '',
                'combo_key': r['event_key'] or '',
                'timestamp': r['timestamp'],
                'sec_uid': r['sec_uid'] or '',
            })

        conn.close()

        # 进场只保留在当前监听进程内存中，绝不从数据库合成历史。
        listener = listeners.get(room_id)
        if listener:
            events.extend(
                entry for entry in list(listener.recent_entries)
                if int(entry.get('timestamp') or 0) >= since
            )

        # 合并、按 timestamp 排序（最新的在前）、取 limit 条、再反转回时间正序
        events.sort(key=lambda x: x['timestamp'], reverse=True)
        events = events[:limit]
        events.reverse()

        # 按已落库的 UID 精确补真实身份；原始 display 和内容一律不改。
        identities = _feed_matched_identities(
            event.get('sec_uid') for event in events
        )
        for event in events:
            if not event.get('matched_identity'):
                event['matched_identity'] = identities.get(
                    str(event.get('sec_uid') or '')
                )

        # 盖上最近观察到的消费等级（0 = 未知，前端隐藏）
        attach_consume_levels(events, 'sec_uid')

    except Exception as e:
        print(f"[FEED] _build_feed_events 失败: {e}", flush=True)

    return events


def _gzip_sse(generator):
    """流式 gzip 压缩 SSE 输出。

    关键：每块之后必须 Z_SYNC_FLUSH，否则数据被压缩器缓冲，
    事件不会实时到达浏览器，SSE 就失去意义了。
    """
    compressor = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    for chunk in generator:
        buf = compressor.compress(chunk.encode('utf-8'))
        buf += compressor.flush(zlib.Z_SYNC_FLUSH)
        if buf:
            yield buf
    # 收尾块只在正常跑完时补。绝不能放进 finally：客户端断开时 Python 会向
    # 生成器抛 GeneratorExit，在 finally 里再 yield 会变成
    # 「RuntimeError: generator ignored GeneratorExit」——每断一个 SSE 连接
    # 就抛一次。而 SSE 本来就是长连接、正常结束方式就是客户端断开。
    tail = compressor.flush(zlib.Z_FINISH)
    if tail:
        yield tail


# 单次响应小于这个字节数就不压：压缩本身要 CPU，小响应压完省不了几个字节。
_GZIP_MIN_BYTES = 800


@app.after_request
def _gzip_json_response(response):
    """按 Accept-Encoding 压缩 JSON/文本响应。

    实测 /api/feed 单次 15.7 KB 的公屏 JSON，压完只剩 2~3 KB。
    Egress 占账单 29%，这是最省事的一刀。

    三条红线：
    - 流式响应（SSE）绝对不碰。它已经在 _gzip_sse 里逐块压过，再走这里会被
      收集成整块，实时性直接没了——SSE 就失去意义。
    - 已经带 Content-Encoding 的不重复压。
    - Vary 不管压没压都要加，否则中间缓存会把压过的响应喂给不支持 gzip 的客户端。
    """
    try:
        vary = response.headers.get('Vary', '')
        if 'accept-encoding' not in vary.lower():
            response.headers['Vary'] = (
                (vary + ', ') if vary else '') + 'Accept-Encoding'

        if response.direct_passthrough or response.is_streamed:
            return response
        if response.headers.get('Content-Encoding'):
            return response
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return response
        ctype = (response.content_type or '').lower()
        if not ('json' in ctype or 'javascript' in ctype or ctype.startswith('text/')):
            return response
        data = response.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return response
        packed = gzip.compress(data, 6)
        if len(packed) >= len(data):
            return response          # 压完更大就别压了
        response.set_data(packed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(packed))
    except Exception as exc:
        # 压缩失败绝不能影响正常返回。
        print(f'[GZIP] 响应压缩失败: {exc}', flush=True)
    return response


@app.route('/stream/<room_id>')
def stream(room_id):
    """SSE 实时事件流。

    每个浏览器连接拿一条独立队列，互不影响：同一个厅可以多人同时看，
    谁都不会被后来者踢下线，也不会瓜分彼此的消息。
    """
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    def generate():
        listener = listeners.get(room_id)
        if not listener:
            yield f"data: {json.dumps({'type': 'error', 'data': {'error': '未找到监听器'}})}\n\n"
            return
        # 先订阅再取历史，这中间产生的实时事件会进队列，不会漏。
        channel = subscribe_events(listener)
        try:
            init_data = {'room_id': room_id, 'nickname': listener.nickname,
                         'mystery_count': listener.mystery_count,
                         'is_anonymous': listener.is_private,
                         'history': listener.recent_mysteries[-50:],
                         'feed': _build_feed_events(room_id)}
            yield f"data: {json.dumps({'type': 'init', 'data': init_data})}\n\n"
            while listener.running or not channel.empty():
                try:
                    event = channel.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            # 浏览器关掉页面时释放队列，否则订阅者会越积越多。
            unsubscribe_events(listener, channel)

    headers = {'Cache-Control': 'no-cache', 'Connection': 'keep-alive',
               'Access-Control-Allow-Origin': '*', 'Vary': 'Accept-Encoding'}
    if 'gzip' in (request.headers.get('Accept-Encoding') or '').lower():
        headers['Content-Encoding'] = 'gzip'
        return Response(_gzip_sse(generate()), mimetype='text/event-stream',
                        headers=headers)
    return Response(generate(), mimetype='text/event-stream', headers=headers)




@app.route('/api/feed/<room_id>')
def feed_events(room_id):
    """返回某个直播间的互动时间线（公屏用）"""
    denied = _deny_temporary_room(room_id)
    if denied:
        return denied
    limit = request.args.get('limit', default=200, type=int)
    # since 非法时回落成 0（拉全量），不报错——前端拿不到数据比慢一点更糟。
    since = request.args.get('since', default=0, type=int) or 0

    def build():
        events = _build_feed_events(room_id, limit, since=since)
        return {'success': True, 'events': events, 'count': len(events)}, 200

    try:
        # limit 和 since 都决定返回什么，必须一起进缓存键，
        # 否则第二个人会拿到别人的增量结果。
        return _cached_json(
            ('feed', str(room_id), int(limit or 0), int(since)), build)
    except Exception as e:
        return api_error(e)


@app.route('/api/emoji_map')
def emoji_map():
    """返回抖音表情映射（名字 -> 本地图片路径）"""
    import os
    map_path = os.path.join(os.path.dirname(__file__), 'static', 'emoji_map.json')
    try:
        with open(map_path, 'r', encoding='utf-8') as f:
            return jsonify({'success': True, 'map': json.load(f)})
    except Exception as e:
        return api_error(e)


# 模块级启动，放在所有全局（listeners 等）定义之后，gunicorn 加载
# web_listener:app 时即执行；启动会立即 tick 一次补齐默认厅。
start_auto_watch_thread()
start_silence_thread()


if __name__ == '__main__':
    server_options = resolve_server_options(os.environ)
    app.config['TEMPLATES_AUTO_RELOAD'] = not is_production(os.environ)
    app.run(threaded=True, debug=False, **server_options)
