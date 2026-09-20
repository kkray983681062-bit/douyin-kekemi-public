"""全局身份库：跨直播间按 UID 精确匹配高等级用户与已解析神秘人。

只接受 `sec_uid`、`webcast_uid` 和数字 UID 完全一致的自动匹配；昵称、匿名编号、
消费等级、头像和抖音号（`display_id`/`unique_id`）一律只用于展示，绝不参与认人。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

# 普通用户实际送礼时进入身份库的消费等级门槛，含 41 级本身。
HIGH_LEVEL_THRESHOLD = 41

_BEIJING_TZ = timezone(timedelta(hours=8))

IDENTIFIER_TYPES = ('sec_uid', 'webcast_uid', 'numeric_uid')

# 抖音给真匿名用户填充的占位身份值：sec_uid/id/display_id 都可能被替换成这些值，
# 一旦写进身份库就会把互不相干的匿名用户合并成同一个人。
_ANONYMOUS_PLACEHOLDER_IDS = {'0', '111111', '?'}

_DOU_ALIAS_RE = re.compile(r'^dou\d{5,}$', re.IGNORECASE)
_MYSTERY_NUMBER_RE = re.compile(r'^神秘人\d+$')

# 主页反查失败时会把昵称填成这些占位值，不能当作已解析的真实身份。
_PLACEHOLDER_REAL_NAMES = {'?', '-', '未知'}

# 各标识类型在用户对象里的候选字段，按优先级排列。
_IDENTIFIER_FIELDS = {
    'sec_uid': ('sec_uid', 'secUid'),
    'webcast_uid': ('webcast_uid', 'webcastUid'),
    'numeric_uid': ('id_str', 'user_id', 'id', 'uid'),
}


def init_identity_schema(conn):
    """幂等建立永久身份三表；这些表不参与任何过期清理。"""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS identity_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_sec_uid TEXT DEFAULT '',
            real_name TEXT DEFAULT '',
            douyin_id TEXT DEFAULT '',
            profile_json TEXT DEFAULT '{}',
            max_consume_level INTEGER DEFAULT 0,
            source TEXT DEFAULT '',
            first_seen INTEGER DEFAULT 0,
            last_seen INTEGER DEFAULT 0,
            last_room_id TEXT DEFAULT '',
            created_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS identity_identifiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_id INTEGER NOT NULL,
            identifier_type TEXT NOT NULL,
            identifier_value TEXT NOT NULL,
            source TEXT DEFAULT '',
            first_seen INTEGER DEFAULT 0,
            last_seen INTEGER DEFAULT 0,
            UNIQUE(identifier_type, identifier_value)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_identity_identifiers_identity
        ON identity_identifiers(identity_id)
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS identity_alias_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            alias_type TEXT DEFAULT 'other_anonymous',
            beijing_date TEXT NOT NULL,
            room_id TEXT DEFAULT '',
            source TEXT DEFAULT '',
            first_seen INTEGER DEFAULT 0,
            last_seen INTEGER DEFAULT 0,
            UNIQUE(identity_id, alias, beijing_date, room_id)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_identity_alias_identity_date
        ON identity_alias_history(identity_id, beijing_date)
    ''')


def normalize_identifier(identifier_type, value):
    """返回可入库的标识值；类型不认识或值是占位符时返回空串。"""
    if identifier_type not in IDENTIFIER_TYPES:
        return ''
    text = '' if value is None else str(value).strip()
    if not text or text in _ANONYMOUS_PLACEHOLDER_IDS:
        return ''
    if identifier_type == 'numeric_uid' and not text.isdigit():
        return ''
    return text


def _read_field(source, *names):
    for name in names:
        if isinstance(source, dict):
            if name not in source:
                continue
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ''


def extract_identifiers(source):
    """从 protobuf User 或名单 dict 中提取全部有效标识，按固定类型顺序去重。"""
    if source is None:
        return ()
    identifiers = []
    for identifier_type in IDENTIFIER_TYPES:
        raw = _read_field(source, *_IDENTIFIER_FIELDS[identifier_type])
        value = normalize_identifier(identifier_type, raw)
        if value and (identifier_type, value) not in identifiers:
            identifiers.append((identifier_type, value))
    return tuple(identifiers)


def beijing_date(timestamp):
    """给定 Unix 时间，返回其所属北京时间自然日的 `YYYY-MM-DD`。"""
    moment = datetime.fromtimestamp(int(timestamp), tz=_BEIJING_TZ)
    return moment.strftime('%Y-%m-%d')


def classify_alias(alias):
    """区分 dou 编号、神秘人数字编号、神秘人几阶和其它匿名别名。"""
    text = str(alias or '').strip()
    if _DOU_ALIAS_RE.fullmatch(text):
        return 'dou'
    if _MYSTERY_NUMBER_RE.fullmatch(text):
        return 'mystery_number'
    if text.startswith('神秘人') and text.endswith('阶') and len(text) > 4:
        return 'mystery_tier'
    return 'other_anonymous'


def is_anonymous_alias(text):
    """判断一个名字是不是抖音当天下发的匿名编号，而不是demo_004。"""
    return classify_alias(text) != 'other_anonymous'


def is_resolved_real_name(real_name, display=''):
    """demo_004必须非空、不是占位资料、不等于当天匿名编号，且本身不是匿名编号。"""
    text = str(real_name or '').strip()
    if not text or text in _PLACEHOLDER_REAL_NAMES:
        return False
    if text == str(display or '').strip():
        return False
    return not is_anonymous_alias(text)


# ========== 精确匹配 ==========

# 进程内「标识 → 身份 id」缓存。只缓存命中的绑定关系，不缓存任何资料字段，
# 资料每次都回数据库读；未命中一律重查，因此新入库的人不会被旧缓存挡住。
# 任何身份写入后整体失效。
_MATCH_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX_ENTRIES = 50000


def invalidate_cache():
    with _CACHE_LOCK:
        _MATCH_CACHE.clear()


@dataclass(frozen=True)
class IdentityLookup:
    """一次 UID 精确查询的结果。冲突时不返回任何身份。"""

    identity_id: int | None = None
    matched_by: tuple = ()
    candidate_ids: tuple = ()
    conflict: bool = False


def _identity_id_for(conn, identifier_type, value):
    key = (identifier_type, value)
    with _CACHE_LOCK:
        if key in _MATCH_CACHE:
            return _MATCH_CACHE[key]
    row = conn.execute('''
        SELECT identity_id FROM identity_identifiers
        WHERE identifier_type = ? AND identifier_value = ?
    ''', (identifier_type, value)).fetchone()
    if row is None:
        return None
    identity_id = int(row[0])
    with _CACHE_LOCK:
        if len(_MATCH_CACHE) >= _CACHE_MAX_ENTRIES:
            _MATCH_CACHE.clear()
        _MATCH_CACHE[key] = identity_id
    return identity_id


def lookup_identity(conn, identifiers):
    """只按 UID 完全一致查身份；多个 UID 指向不同身份时返回冲突且不认人。"""
    hits = {}
    for identifier_type, raw in identifiers or ():
        value = normalize_identifier(identifier_type, raw)
        if not value:
            continue
        identity_id = _identity_id_for(conn, identifier_type, value)
        if identity_id is None:
            continue
        hits.setdefault(identity_id, []).append(identifier_type)
    if not hits:
        return IdentityLookup()
    if len(hits) > 1:
        return IdentityLookup(
            candidate_ids=tuple(sorted(hits)),
            conflict=True,
        )
    identity_id, matched_by = next(iter(hits.items()))
    return IdentityLookup(
        identity_id=identity_id,
        matched_by=tuple(matched_by),
        candidate_ids=(identity_id,),
    )


def _load_profile_json(raw):
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def identity_payload(conn, identity_id, matched_by=()):
    """把身份行整理成可直接放进 SSE / JSON 的 `matched_identity`。"""
    row = conn.execute('''
        SELECT canonical_sec_uid, real_name, douyin_id, profile_json,
               max_consume_level, last_room_id, first_seen, last_seen
        FROM identity_profiles WHERE id = ?
    ''', (identity_id,)).fetchone()
    if row is None:
        return None
    profile = _load_profile_json(row[3])
    sec_uid = str(row[0] or '') or str(profile.get('sec_uid') or '')
    return {
        'identity_id': int(identity_id),
        'real_name': str(row[1] or ''),
        'douyin_id': str(row[2] or ''),
        'sec_uid': sec_uid,
        'follower_count': int(profile.get('follower_count') or 0),
        'aweme_count': int(profile.get('aweme_count') or 0),
        'signature': str(profile.get('signature') or ''),
        'max_consume_level': int(row[4] or 0),
        'profile_url': (
            f'https://www.douyin.com/user/{quote(sec_uid)}' if sec_uid else ''
        ),
        'matched_by': list(matched_by),
    }


def match_user(conn, source):
    """对外的匹配入口：命中返回 `matched_identity`，未命中或冲突返回 None。"""
    identifiers = extract_identifiers(source)
    if not identifiers:
        return None
    result = lookup_identity(conn, identifiers)
    if result.identity_id is None:
        return None
    return identity_payload(conn, result.identity_id, result.matched_by)


# ========== 身份建立与更新 ==========


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _merge_profile(existing, incoming):
    """保守合并主页资料：只有非空新值才覆盖，已知资料不会被空值抹掉。"""
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if not value:
            continue
        merged[key] = value
    return merged


def record_alias(conn, identity_id, alias, room_id='', timestamp=None,
                 source='', commit=True):
    """按北京自然日记录一个匿名别名；demo_004不写别名历史，返回是否写入。

    一行记录的核心事实是「某身份在某个北京自然日用过某个别名」，`room_id` 是
    尽可能补上的观察位置。旧数据迁移证明不了房间，只能写空值，因此同一别名同一天
    的「房间未知」行与实际观察到的行必须收敛成一行，否则会并排出现看起来重复的记录：

    - 带真实房间写入时，当天同别名的「房间未知」行被这条实际观察取代：它的首次
      出现时间并进保留行后删除。这样既能升级只有空行的情况，也能收敛上线初期
      已经并排落库的空行 + 实际房间行。
    - 房间未知的写入（例如重复跑旧数据导入）遇到当天同别名已有记录时直接跳过，
      绝不把信息更少的弱记录再加回来。

    两条规则都不会把两个**已知**房间合并，跨北京日期也互不影响。
    """
    text = str(alias or '').strip()
    if not identity_id or not is_anonymous_alias(text):
        return False
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    identity_id = int(identity_id)
    room_id = str(room_id or '')
    day = beijing_date(timestamp)
    first_seen = timestamp

    if room_id:
        weaker = conn.execute('''
            SELECT first_seen FROM identity_alias_history
            WHERE identity_id = ? AND alias = ? AND beijing_date = ?
              AND room_id = ''
        ''', (identity_id, text, day)).fetchone()
        if weaker is not None:
            first_seen = min(first_seen, _safe_int(weaker[0]) or first_seen)
            conn.execute('''
                DELETE FROM identity_alias_history
                WHERE identity_id = ? AND alias = ? AND beijing_date = ?
                  AND room_id = ''
            ''', (identity_id, text, day))
    elif conn.execute('''
        SELECT 1 FROM identity_alias_history
        WHERE identity_id = ? AND alias = ? AND beijing_date = ?
    ''', (identity_id, text, day)).fetchone() is not None:
        return True

    conn.execute('''
        INSERT INTO identity_alias_history
            (identity_id, alias, alias_type, beijing_date, room_id, source,
             first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identity_id, alias, beijing_date, room_id) DO UPDATE SET
            first_seen = MIN(identity_alias_history.first_seen,
                             excluded.first_seen),
            last_seen = MAX(identity_alias_history.last_seen, excluded.last_seen)
    ''', (
        identity_id, text, classify_alias(text), day,
        room_id, source or '', first_seen, timestamp,
    ))
    if commit:
        conn.commit()
    return True


def _attach_identifiers(conn, identity_id, identifiers, source, timestamp):
    """绑定本次全部有效 UID；已属于别人的标识不会被抢走。"""
    for identifier_type, value in identifiers:
        conn.execute('''
            INSERT INTO identity_identifiers
                (identity_id, identifier_type, identifier_value, source,
                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                last_seen = MAX(identity_identifiers.last_seen, excluded.last_seen)
            WHERE identity_identifiers.identity_id = excluded.identity_id
        ''', (
            int(identity_id), identifier_type, value, source or '',
            timestamp, timestamp,
        ))


def _create_identity(conn, *, sec_uid, real_name, douyin_id, profile,
                     consume_level, room_id, timestamp, first_seen, source):
    cursor = conn.execute('''
        INSERT INTO identity_profiles
            (canonical_sec_uid, real_name, douyin_id, profile_json,
             max_consume_level, source, first_seen, last_seen, last_room_id,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        sec_uid, real_name, douyin_id,
        json.dumps(profile, ensure_ascii=False), consume_level, source,
        first_seen, timestamp, room_id, timestamp, timestamp,
    ))
    return int(cursor.lastrowid)


def _update_identity(conn, identity_id, *, sec_uid, real_name, douyin_id,
                     profile, consume_level, room_id, timestamp, first_seen):
    row = conn.execute('''
        SELECT canonical_sec_uid, real_name, douyin_id, profile_json,
               max_consume_level, first_seen, last_seen, last_room_id
        FROM identity_profiles WHERE id = ?
    ''', (identity_id,)).fetchone()
    if row is None:
        return
    existing_first = _safe_int(row[5])
    earliest = min(existing_first, first_seen) if existing_first else first_seen
    conn.execute('''
        UPDATE identity_profiles
        SET canonical_sec_uid = ?, real_name = ?, douyin_id = ?,
            profile_json = ?, max_consume_level = ?, first_seen = ?,
            last_seen = ?, last_room_id = ?, updated_at = ?
        WHERE id = ?
    ''', (
        sec_uid or str(row[0] or ''),
        real_name or str(row[1] or ''),
        douyin_id or str(row[2] or ''),
        json.dumps(
            _merge_profile(_load_profile_json(row[3]), profile),
            ensure_ascii=False,
        ),
        max(consume_level, _safe_int(row[4])),
        earliest,
        max(timestamp, _safe_int(row[6])),
        room_id or str(row[7] or ''),
        timestamp,
        identity_id,
    ))


def _register(conn, user, *, source, room_id='', display='', real_name='',
              douyin_id='', profile=None, consume_level=0, timestamp=None,
              first_seen=None):
    """在一个事务内建立或更新身份、绑定 UID、记录当天匿名别名。"""
    identifiers = extract_identifiers(user)
    if not identifiers:
        return None
    lookup = lookup_identity(conn, identifiers)
    if lookup.conflict:
        # 冲突时既不认人也不改任何旧绑定，留给后续管理员功能处理。
        print(
            f'[身份库] UID 冲突，本次不匹配也不改绑定: '
            f'{identifiers} -> identity {lookup.candidate_ids}',
            flush=True,
        )
        return None

    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    resolved_name = str(real_name or '').strip()
    if is_anonymous_alias(resolved_name):
        resolved_name = ''
    fields = {
        'sec_uid': next((v for t, v in identifiers if t == 'sec_uid'), ''),
        'real_name': resolved_name,
        'douyin_id': str(douyin_id or '').strip(),
        'profile': dict(profile or {}),
        'consume_level': max(0, _safe_int(consume_level)),
        'room_id': str(room_id or ''),
        'timestamp': timestamp,
        'first_seen': _safe_int(first_seen) or timestamp,
    }
    try:
        identity_id = lookup.identity_id
        if identity_id is None:
            identity_id = _create_identity(conn, source=source, **fields)
        else:
            _update_identity(conn, identity_id, **fields)
        _attach_identifiers(conn, identity_id, identifiers, source, timestamp)
        record_alias(
            conn, identity_id, display, room_id=fields['room_id'],
            timestamp=timestamp, source=source, commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        invalidate_cache()
        raise
    invalidate_cache()
    matched_by = lookup.matched_by or tuple(t for t, _ in identifiers)
    return identity_payload(conn, identity_id, matched_by)


def register_gift_sender(conn, user, *, room_id='', display='', real_name='',
                         douyin_id='', profile=None, consume_level=0,
                         is_mystery=False, timestamp=None):
    """普通用户实际送礼且消费等级 >= 41 时入库；神秘人和 40 级及以下不写。"""
    if is_mystery or _safe_int(consume_level) < HIGH_LEVEL_THRESHOLD:
        return None
    return _register(
        conn, user, source='high_level_gift', room_id=room_id, display=display,
        real_name=real_name, douyin_id=douyin_id, profile=profile,
        consume_level=consume_level, timestamp=timestamp,
    )


def register_resolved_mystery(conn, user, *, room_id='', display='',
                              real_name='', douyin_id='', profile=None,
                              consume_level=0, timestamp=None,
                              source='resolved_mystery'):
    """已解析出真实资料的神秘人入库，不受 41 级门槛限制。"""
    if not is_resolved_real_name(real_name, display):
        return None
    return _register(
        conn, user, source=source, room_id=room_id, display=display,
        real_name=real_name, douyin_id=douyin_id, profile=profile,
        consume_level=consume_level, timestamp=timestamp,
    )


# ========== 旧神秘人迁移 ==========


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


def _legacy_alias_pairs(conn, sec_uid, own_display, own_last_seen):
    """该身份可证实的历史别名：旧记录本身 + `display_names` 各行的最后日期。"""
    pairs = [(own_display, own_last_seen)]
    if _table_exists(conn, 'display_names'):
        pairs.extend(
            (str(row[0] or ''), _safe_int(row[1]))
            for row in conn.execute(
                'SELECT display, last_seen FROM display_names WHERE sec_uid = ?',
                (sec_uid,),
            ).fetchall()
        )
    return pairs


def import_legacy_mystery_records(conn):
    """把已解析真实身份的旧神秘人幂等导入身份库；旧表只读，不补造历史日期。

    汇总里 `aliases` 是本次覆盖的「别名+北京日期」行数，不是写入尝试次数：
    旧记录自身的 display 常和 `display_names` 里同名同日的行落到同一行别名，
    按尝试次数报数会虚高，和表里实际行数对不上。
    """
    summary = {'identities': 0, 'aliases': 0, 'skipped': 0}
    if not _table_exists(conn, 'mystery_records'):
        return summary
    rows = conn.execute('''
        SELECT sec_uid, display, real_name, extra, last_room_id,
               first_seen, last_seen
        FROM mystery_records
        WHERE is_regular = 0
    ''').fetchall()
    for row in rows:
        raw_sec_uid = str(row[0] or '')
        sec_uid = normalize_identifier('sec_uid', raw_sec_uid)
        display = str(row[1] or '')
        real_name = str(row[2] or '')
        if not sec_uid or not is_resolved_real_name(real_name, display):
            summary['skipped'] += 1
            continue
        extra = _load_profile_json(row[3])
        last_seen = _safe_int(row[6]) or _safe_int(row[5])
        matched = _register(
            conn,
            {'sec_uid': sec_uid},
            source='legacy_import',
            room_id=str(row[4] or ''),
            # 别名单独按各自可证实的最后日期导入，这里不当成"今天"的别名。
            display='',
            real_name=real_name,
            douyin_id=str(extra.get('unique_id') or ''),
            profile=extra,
            consume_level=0,
            timestamp=last_seen or int(time.time()),
            first_seen=_safe_int(row[5]) or last_seen,
        )
        if matched is None:
            summary['skipped'] += 1
            continue
        summary['identities'] += 1
        written = set()
        for alias, alias_last_seen in _legacy_alias_pairs(
            conn, raw_sec_uid, display, last_seen
        ):
            # 没有可证实日期的别名一律丢弃，绝不补造。
            if not alias_last_seen:
                continue
            key = (str(alias or '').strip(), beijing_date(alias_last_seen))
            if key in written:
                continue
            if record_alias(
                conn, matched['identity_id'], alias, room_id='',
                timestamp=alias_last_seen, source='legacy_import', commit=False,
            ):
                written.add(key)
        summary['aliases'] += len(written)
        conn.commit()
    invalidate_cache()
    return summary
