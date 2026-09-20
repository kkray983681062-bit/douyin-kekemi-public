"""北京时间日榜、周榜与礼物归属修正的纯数据逻辑。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


_BEIJING_TZ = timezone(timedelta(hours=8))


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def resolve_event_tickets(
        fan_ticket_count, quantity, fallback_total, quantity_verified):
    """解析一条已去重礼物组的票数与来源。

    `fan_ticket_count` 是平台随礼物消息给出的单票值；数据库中的一条
    连击记录代表该组当前最终数量，因此这里乘最终数量。若平台没有给
    单票值，才回退到已经完成单价和数量核验的礼物合计。
    """
    if not quantity_verified:
        return None, 'unknown'
    verified_quantity = _positive_int(quantity)
    if verified_quantity is None:
        return None, 'unknown'
    platform_unit = _positive_int(fan_ticket_count)
    if platform_unit is not None:
        return platform_unit * verified_quantity, 'douyin_fan_ticket'
    fallback = _positive_int(fallback_total)
    if fallback is not None:
        return fallback, 'gift_price'
    return None, 'unknown'


def _table_columns(conn, table_name):
    return {
        row[1] for row in conn.execute(f'PRAGMA table_info({table_name})').fetchall()
    }


def record_room_hall(conn, room_id, hall_name, now):
    """登记 room_id 属于哪个厅；监听启动时调用，是最权威的来源。"""
    room_id = str(room_id or '').strip()
    if not room_id:
        return
    hall_name = str(hall_name or '').strip()
    with conn:
        conn.execute('''
            INSERT INTO room_halls (room_id, hall_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(room_id) DO UPDATE SET
                hall_name = CASE
                    WHEN excluded.hall_name <> '' THEN excluded.hall_name
                    ELSE room_halls.hall_name
                END,
                updated_at = excluded.updated_at
        ''', (room_id, hall_name, int(now)))


def resolve_hall_name(conn, room_id):
    """room_id -> 厅名；查不到或为空返回 ''（调用方据此退回只看本场）。"""
    room_id = str(room_id or '').strip()
    if not room_id:
        return ''
    row = conn.execute(
        'SELECT hall_name FROM room_halls WHERE room_id = ?', (room_id,)
    ).fetchone()
    return str(row['hall_name']).strip() if row and row['hall_name'] else ''


def hall_room_ids(conn, room_id):
    """同一个厅的所有场次 room_id。

    厅名未知或为空时只返回自己 —— 绝不能把一堆未知厅并成一个，
    那会让互不相干的直播间数据混在一张榜上。
    """
    room_id = str(room_id or '').strip()
    hall_name = resolve_hall_name(conn, room_id)
    if not hall_name:
        return [room_id]
    rows = conn.execute(
        'SELECT room_id FROM room_halls WHERE hall_name = ? ORDER BY room_id',
        (hall_name,),
    ).fetchall()
    rooms = [str(row['room_id']) for row in rows]
    return rooms or [room_id]


def backfill_room_halls(conn):
    """给历史 room_id 补厅名：监听启动前的场次没登记过，从既有资料反推。

    mystery_records.nickname 存的就是该用户所在厅的厅名，按 last_room_id 取众数。
    只补 room_halls 里缺的，已登记的不覆盖（登记的更权威）。
    """
    rows = conn.execute('''
        SELECT last_room_id AS room_id, nickname, COUNT(*) AS hits
        FROM mystery_records
        WHERE COALESCE(last_room_id, '') <> ''
          AND COALESCE(nickname, '') <> ''
          AND last_room_id NOT IN (SELECT room_id FROM room_halls)
        GROUP BY last_room_id, nickname
        ORDER BY last_room_id ASC, hits DESC
    ''').fetchall()
    seen = set()
    filled = 0
    with conn:
        for row in rows:
            room_id = str(row['room_id'])
            if room_id in seen:
                continue
            seen.add(room_id)
            conn.execute(
                'INSERT OR IGNORE INTO room_halls (room_id, hall_name, updated_at) '
                'VALUES (?, ?, 0)',
                (room_id, str(row['nickname']).strip()),
            )
            filled += 1
    return filled


def beijing_day_bounds(now):
    current = datetime.fromtimestamp(int(now), tz=_BEIJING_TZ)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def init_leaderboard_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gift_recipient_adjustments (
            interaction_id INTEGER PRIMARY KEY,
            room_id TEXT NOT NULL,
            recipient_key TEXT NOT NULL,
            recipient_name TEXT NOT NULL,
            note TEXT NOT NULL,
            actor TEXT NOT NULL,
            adjusted_at INTEGER NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_gift_adjustment_room_time
        ON gift_recipient_adjustments(room_id, adjusted_at DESC)
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS weekly_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            period_start INTEGER NOT NULL,
            period_end INTEGER NOT NULL,
            locked_at INTEGER NOT NULL,
            UNIQUE(room_id, period_start)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS weekly_rows (
            period_id INTEGER NOT NULL,
            board_type TEXT NOT NULL,
            participant_key TEXT NOT NULL,
            display TEXT NOT NULL,
            tickets INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY (period_id, board_type, participant_key)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS weekly_host_large_details (
            period_id INTEGER NOT NULL,
            host_key TEXT NOT NULL,
            visitor_key TEXT NOT NULL,
            visitor_display TEXT NOT NULL,
            slot_start INTEGER NOT NULL,
            slot_end INTEGER NOT NULL,
            tickets INTEGER NOT NULL,
            PRIMARY KEY (period_id, host_key, visitor_key, slot_start)
        )
    ''')
    # 厅名映射：抖音每开一场直播就换新 room_id（实测每个 room_id 只覆盖一天），
    # 按 room_id 聚合的「周榜」只能捞到当前这一场 = 当天。厅名跨场次稳定，用它做聚合键。
    conn.execute('''
        CREATE TABLE IF NOT EXISTS room_halls (
            room_id TEXT PRIMARY KEY,
            hall_name TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_room_halls_name
        ON room_halls(hall_name)
    ''')
    if 'hall_name' not in _table_columns(conn, 'weekly_periods'):
        conn.execute(
            "ALTER TABLE weekly_periods ADD COLUMN hall_name TEXT NOT NULL DEFAULT ''"
        )
    # 归档按厅一周一行；room_id 列只留代表场次，查询一律走 hall_name。
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_hall_period
        ON weekly_periods(hall_name, period_start)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_weekly_period_room_start
        ON weekly_periods(room_id, period_start DESC)
    ''')
    conn.commit()


# SQLite 默认最多 999 个绑定变量（新版是 32766）。取个保守值分批查，
# 免得键特别多时直接抛 OperationalError 把榜单接口打成 500。
_SQL_VARS_PER_QUERY = 400


def identity_key_map(conn, keys):
    """把一批榜单键映射到规范键（'id:<identity_id>'）。

    只映射能查到关联的；查不到的调用方原样保留。`display:` / `room:`
    前缀是我们自己造的占位键，不进查询。

    返回 (映射, 规范键 -> 真名)。
    """
    wanted = {str(k or '') for k in keys}
    wanted = {k for k in wanted if k and not k.startswith('display:')
              and not k.startswith('room:')}
    if not wanted:
        return {}, {}
    ordered = sorted(wanted)
    identity_of = {}
    ambiguous = set()
    names_by_id = {}
    # UNIQUE 只保证 (type, value) 不重，同一个值挂在两种类型下指向不同身份
    # 是能存进去的。分批查，避免键特别多时撞上 SQL 变量数上限。
    for start in range(0, len(ordered), _SQL_VARS_PER_QUERY):
        chunk = ordered[start:start + _SQL_VARS_PER_QUERY]
        placeholders = ','.join('?' * len(chunk))
        for row in conn.execute(
                f'SELECT ii.identifier_value, ii.identity_id, p.real_name '
                f'FROM identity_identifiers ii '
                f'LEFT JOIN identity_profiles p ON p.id = ii.identity_id '
                f'WHERE ii.identifier_value IN ({placeholders})', chunk):
            value, identity_id = str(row[0]), int(row[1])
            names_by_id[identity_id] = str(row[2] or '')
            if value in identity_of and identity_of[value] != identity_id:
                # 一个标识符指向两个身份：身份库自己遇到这种情况就判冲突、
                # 拒绝识别（identity_registry.lookup_identity）。榜单这边
                # 更不该自作主张挑一个——合错人比拆开严重得多，拆开只是
                # 低估，合错是把两个人的票算到一个人头上。
                ambiguous.add(value)
                continue
            identity_of[value] = identity_id

    canonical = {}
    real_names = {}
    for value, identity_id in identity_of.items():
        if value in ambiguous:
            continue
        key = 'id:%d' % identity_id
        canonical[value] = key
        real_names[key] = names_by_id.get(identity_id, '')
    return canonical, real_names


def merge_rows_by_identity(conn, rows, key_name):
    """把同一个人的多行并成一行。

    同一个人戴马甲和不戴马甲会落到不同的键上：脱马甲时 sec_uid 是真的，
    戴马甲时 sec_uid 为空、代码退回存 webcast_uid（每次进厅都换一个）。
    榜单按原始键聚合就会把一个人拆成好几行、每行都低估——线上实测
    示例用户乙被拆成 #14（3531 票）和 #20（2897 票），拆得最狠的一个身份被
    拆成 17 个键、合计 11 万票。

    identity_identifiers 早就把这些标识符挂在同一个 identity 上了（靠高
    等级礼物揭示时建立），榜单只是没用上——事实上榜单能在马甲名后面的
    括号里写出真名，用的正是这条关联。

    没被关联过的键原样不动：抖音的深度匿名下我们看不到就是看不到，
    绝不能按显示名猜合并。

    在 Python 里做而不是改那 5 处 SQL：逻辑集中一处、好测，而且
    identity_identifiers 只有两千多行、按 IN 列表查很轻。
    """
    items = [dict(row) for row in rows]
    canonical, real_names = identity_key_map(
        conn, (item.get(key_name) for item in items))
    if not canonical:
        # merged_keys 必须恒有，不能只在发生过合并时才出现——否则前端得
        # 分两种情况处理，同一个响应里 visitors 有、hosts 没有。
        for item in items:
            item['merged_keys'] = 1
        return items

    merged = {}
    order = []
    for item in items:
        raw = str(item.get(key_name) or '')
        key = canonical.get(raw, raw)
        item[key_name] = key
        if key not in merged:
            item['merged_keys'] = 1
            merged[key] = item
            order.append(key)
            continue
        merged[key]['merged_keys'] = int(merged[key].get('merged_keys') or 1) + 1
        target = merged[key]
        target['tickets'] = int(target.get('tickets') or 0) + int(item.get('tickets') or 0)
        target['gift_events'] = (int(target.get('gift_events') or 0)
                                 + int(item.get('gift_events') or 0))
        target['last_gift_at'] = max(int(target.get('last_gift_at') or 0),
                                     int(item.get('last_gift_at') or 0))

    # display 保持原样，不用真名顶替：马甲名是拿去跟抖音 App 对照的线索，
    # 抹掉之后界面上也再看不出「这行是合并来的」。真名由既有的括号机制
    # （attach_real_names）带出来，不需要在这里动 display。

    # SQL 里的 ORDER BY 是按合并前的票数排的，合并后要重排
    result = [merged[key] for key in order]
    result.sort(key=lambda r: (-int(r.get('tickets') or 0),
                               -int(r.get('last_gift_at') or 0),
                               str(r.get(key_name) or '')))
    return result


# 「大额时段」的门槛。只在按身份合并之后判，SQL 里不设任何下限——
# 两个马甲各 20000、合起来 40000 的情况必须算数。
LARGE_DETAIL_THRESHOLD = 30000


def _rank_rows(rows, key_name):
    records = []
    for rank, row in enumerate(rows, start=1):
        item = dict(row)
        item['rank'] = rank
        item[key_name] = str(item.get(key_name) or '')
        item['tickets'] = int(item.get('tickets') or 0)
        item['gift_events'] = int(item.get('gift_events') or 0)
        item['last_gift_at'] = int(item.get('last_gift_at') or 0)
        records.append(item)
    return records


def query_daily_boards(conn, room_id, now):
    init_leaderboard_schema(conn)
    day_start, day_end = beijing_day_bounds(now)
    cutoff = min(int(now), day_end - 1)
    visitors = conn.execute('''
        SELECT
            CASE
                WHEN i.sec_uid <> '' THEN i.sec_uid
                ELSE 'display:' || i.display
            END AS sender_key,
            MAX(i.display) AS display,
            SUM(i.ticket_count) AS tickets,
            COUNT(*) AS gift_events,
            MAX(i.timestamp) AS last_gift_at
        FROM interaction_log i
        WHERE i.room_id = ?
          AND i.type = 'gift'
          AND i.timestamp >= ?
          AND i.timestamp <= ?
          AND i.ticket_count IS NOT NULL
          AND i.ticket_count > 0
        GROUP BY sender_key
        ORDER BY tickets DESC, last_gift_at DESC, sender_key ASC
    ''', (str(room_id), day_start, cutoff)).fetchall()
    hosts = conn.execute('''
        SELECT
            COALESCE(
                NULLIF(a.recipient_key, ''),
                NULLIF(i.recipient_key, ''),
                'room:' || i.room_id
            ) AS recipient_key,
            MAX(COALESCE(
                NULLIF(a.recipient_name, ''),
                NULLIF(i.recipient_name, ''),
                i.room_id
            )) AS display,
            SUM(i.ticket_count) AS tickets,
            COUNT(*) AS gift_events,
            MAX(i.timestamp) AS last_gift_at
        FROM interaction_log i
        LEFT JOIN gift_recipient_adjustments a
          ON a.interaction_id = i.id
        WHERE i.room_id = ?
          AND i.type = 'gift'
          AND i.timestamp >= ?
          AND i.timestamp <= ?
          AND i.ticket_count IS NOT NULL
          AND i.ticket_count > 0
        GROUP BY COALESCE(
            NULLIF(a.recipient_key, ''),
            NULLIF(i.recipient_key, ''),
            'room:' || i.room_id
        )
        ORDER BY tickets DESC, last_gift_at DESC, 1 ASC
    ''', (str(room_id), day_start, cutoff)).fetchall()
    return {
        'day_start': day_start,
        'day_end': day_end,
        'visitors': _rank_rows(
            merge_rows_by_identity(conn, visitors, 'sender_key'), 'sender_key'),
        'hosts': _rank_rows(
            merge_rows_by_identity(conn, hosts, 'recipient_key'),
            'recipient_key'),
    }


def set_recipient_adjustment(
        conn, interaction_id, recipient_key, recipient_name,
        note, actor, now):
    recipient_key = str(recipient_key or '').strip()
    recipient_name = str(recipient_name or '').strip()
    note = str(note or '').strip()
    actor = str(actor or '').strip() or 'local_admin'
    if not recipient_key:
        raise ValueError('主持标识不能为空')
    if not recipient_name:
        raise ValueError('主持昵称不能为空')
    if not note:
        raise ValueError('备注不能为空')
    row = conn.execute('''
        SELECT id, room_id, type
        FROM interaction_log WHERE id = ?
    ''', (int(interaction_id),)).fetchone()
    if row is None or row['type'] != 'gift':
        raise LookupError('礼物记录不存在')
    timestamp = int(now)
    with conn:
        conn.execute('''
            INSERT INTO gift_recipient_adjustments (
                interaction_id, room_id, recipient_key, recipient_name,
                note, actor, adjusted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(interaction_id) DO UPDATE SET
                room_id = excluded.room_id,
                recipient_key = excluded.recipient_key,
                recipient_name = excluded.recipient_name,
                note = excluded.note,
                actor = excluded.actor,
                adjusted_at = excluded.adjusted_at
        ''', (
            int(interaction_id), str(row['room_id']), recipient_key,
            recipient_name, note, actor, timestamp,
        ))
    return {
        'interaction_id': int(interaction_id),
        'room_id': str(row['room_id']),
        'recipient_key': recipient_key,
        'recipient_name': recipient_name,
        'note': note,
        'actor': actor,
        'adjusted_at': timestamp,
    }


def list_adjustable_gifts(conn, room_id, now, window_seconds=7 * 86400):
    init_leaderboard_schema(conn)
    rows = conn.execute('''
        SELECT
            i.id, i.room_id, i.sec_uid, i.display, i.content,
            i.gift_id, i.gift_quantity, i.ticket_count, i.ticket_source,
            i.recipient_key AS original_recipient_key,
            i.recipient_name AS original_recipient_name,
            i.timestamp,
            a.recipient_key AS adjusted_recipient_key,
            a.recipient_name AS adjusted_recipient_name,
            a.note, a.actor, a.adjusted_at
        FROM interaction_log i
        LEFT JOIN gift_recipient_adjustments a
          ON a.interaction_id = i.id
        WHERE i.room_id = ?
          AND i.type = 'gift'
          AND i.timestamp >= ?
        ORDER BY i.timestamp DESC, i.id DESC
        LIMIT 500
    ''', (str(room_id), int(now) - int(window_seconds))).fetchall()
    return [dict(row) for row in rows]


def beijing_week_bounds(now):
    current = datetime.fromtimestamp(int(now), tz=_BEIJING_TZ)
    days_since_saturday = (current.weekday() - 5) % 7
    start = (current - timedelta(days=days_since_saturday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(start.timestamp()), int((start + timedelta(days=7)).timestamp())


def _query_period_boards(conn, room_ids, period_start, cutoff):
    """按一批 room_id（同一个厅的所有场次）聚合周榜。"""
    if isinstance(room_ids, (str, bytes)):
        room_ids = [room_ids]
    room_ids = [str(r) for r in room_ids if str(r or '').strip()]
    if not room_ids:
        return {'visitors': [], 'hosts': []}
    placeholders = ','.join('?' * len(room_ids))
    visitors = conn.execute('''
        SELECT
            CASE
                WHEN i.sec_uid <> '' THEN i.sec_uid
                ELSE 'display:' || i.display
            END AS sender_key,
            MAX(i.display) AS display,
            SUM(i.ticket_count) AS tickets,
            COUNT(*) AS gift_events,
            MAX(i.timestamp) AS last_gift_at
        FROM interaction_log i
        WHERE i.room_id IN (__ROOMS__)
          AND i.type = 'gift'
          AND i.timestamp >= ?
          AND i.timestamp <= ?
          AND i.ticket_count IS NOT NULL
          AND i.ticket_count > 0
        GROUP BY sender_key
        ORDER BY tickets DESC, last_gift_at DESC, sender_key ASC
    '''.replace('__ROOMS__', placeholders), (*room_ids, int(period_start), int(cutoff))).fetchall()
    hosts = conn.execute('''
        SELECT
            COALESCE(
                NULLIF(a.recipient_key, ''),
                NULLIF(i.recipient_key, ''),
                'room:' || i.room_id
            ) AS recipient_key,
            MAX(COALESCE(
                NULLIF(a.recipient_name, ''),
                NULLIF(i.recipient_name, ''),
                i.room_id
            )) AS display,
            SUM(i.ticket_count) AS tickets,
            COUNT(*) AS gift_events,
            MAX(i.timestamp) AS last_gift_at
        FROM interaction_log i
        LEFT JOIN gift_recipient_adjustments a
          ON a.interaction_id = i.id
        WHERE i.room_id IN (__ROOMS__)
          AND i.type = 'gift'
          AND i.timestamp >= ?
          AND i.timestamp <= ?
          AND i.ticket_count IS NOT NULL
          AND i.ticket_count > 0
        GROUP BY COALESCE(
            NULLIF(a.recipient_key, ''),
            NULLIF(i.recipient_key, ''),
            'room:' || i.room_id
        )
        ORDER BY tickets DESC, last_gift_at DESC, 1 ASC
    '''.replace('__ROOMS__', placeholders), (*room_ids, int(period_start), int(cutoff))).fetchall()
    detail_rows = conn.execute('''
        WITH gift_events AS (
            SELECT
                COALESCE(
                    NULLIF(a.recipient_key, ''),
                    NULLIF(i.recipient_key, ''),
                    'room:' || i.room_id
                ) AS host_key,
                CASE
                    WHEN i.sec_uid <> '' THEN i.sec_uid
                    ELSE 'display:' || i.display
                END AS visitor_key,
                i.display AS visitor_display,
                (CAST((i.timestamp + 28800) / 7200 AS INTEGER) * 7200 - 28800)
                    AS slot_start,
                i.ticket_count AS ticket_count
            FROM interaction_log i
            LEFT JOIN gift_recipient_adjustments a
              ON a.interaction_id = i.id
            WHERE i.room_id IN (__ROOMS__)
              AND i.type = 'gift'
              AND i.timestamp >= ?
              AND i.timestamp <= ?
              AND i.ticket_count IS NOT NULL
              AND i.ticket_count > 0
        )
        SELECT
            host_key,
            visitor_key,
            MAX(visitor_display) AS visitor_display,
            slot_start,
            SUM(ticket_count) AS tickets
        FROM gift_events
        GROUP BY host_key, visitor_key, slot_start
        -- 这里不设任何门槛：还没按身份合并，任何下限都会把某些成分提前
        -- 丢掉。设过十分之一的粗筛，结果是 40000+2500 的组合明细只写
        -- 40000（静默少算），或者 11 个各 2999 的组合明细整条消失——
        -- 当时的理由「凑够十个也到不了门槛」把成分数当成 ≤10 了，而线上
        -- 实测有拆成 17 个键的身份。
        -- 量过：全部 5 个厅一周的 (主持,访客,时段) 组合一共 5495 个，
        -- 单厅一千出头，全量拉回来再筛毫无压力。
        ORDER BY slot_start ASC, tickets DESC, visitor_key ASC
    '''.replace('__ROOMS__', placeholders),
        (*room_ids, int(period_start), int(cutoff))).fetchall()
    # 明细的两个键都要按身份规范化：主持键不规范化，主持行一合并就挂空；
    # 访客键不规范化，同一个人的两个马甲会在明细里并排出现，跟上面那行
    # 合并过的数字自相矛盾。
    detail_keys = set()
    for raw in detail_rows:
        detail_keys.add(str(raw['host_key']))
        detail_keys.add(str(raw['visitor_key']))
    canonical, real_names = identity_key_map(conn, detail_keys)

    # 规范化之后按 (主持, 访客, 时段) 重新聚合。不能只是把几个列表拼起来：
    # 两个规范化到同一身份的原始主持键各带一条相同 (访客, 时段) 的明细时，
    # weekly_host_large_details 的主键会撞，lock_completed_weeks 抛
    # IntegrityError、整个归档回滚、所有厅的周榜 500，而且那一周永远归不
    # 了档（period_start 只看前一周，过了周六就再也不看它）。
    grouped = {}
    for raw in detail_rows:
        host_key = canonical.get(str(raw['host_key']), str(raw['host_key']))
        visitor_key = canonical.get(str(raw['visitor_key']), str(raw['visitor_key']))
        slot_start = int(raw['slot_start'])
        bucket = grouped.get((host_key, visitor_key, slot_start))
        if bucket is None:
            grouped[(host_key, visitor_key, slot_start)] = {
                'host_key': host_key,
                'visitor_key': visitor_key,
                # 保留马甲名，跟主行口径一致。真名交给 attach_real_names
                # 那套括号机制——而且明细会被归档进
                # weekly_host_large_details，顶替成真名的话那一周就永久
                # 定型了，以后改展示层也救不回来。
                'visitor_display': str(raw['visitor_display'] or ''),
                'slot_start': slot_start,
                'slot_end': slot_start + 2 * 3600,
                'tickets': int(raw['tickets'] or 0),
            }
        else:
            bucket['tickets'] += int(raw['tickets'] or 0)

    details_by_host = {}
    for detail in grouped.values():
        # 精确阈值在这里判，合并之后
        if detail['tickets'] < LARGE_DETAIL_THRESHOLD:
            continue
        details_by_host.setdefault(detail['host_key'], []).append(detail)
    for details in details_by_host.values():
        # 排序口径要跟归档后的读取路径一致（slot ASC, tickets DESC,
        # visitor_key ASC），否则同分的明细在「实时」和「已归档」两个视图里
        # 顺序会不一样。
        details.sort(key=lambda d: (d['slot_start'], -d['tickets'],
                                    d['visitor_key']))
    host_records = _rank_rows(
        merge_rows_by_identity(conn, hosts, 'recipient_key'), 'recipient_key')
    for host in host_records:
        host['large_details'] = details_by_host.get(host['recipient_key'], [])
    return {
        'visitors': _rank_rows(
            merge_rows_by_identity(conn, visitors, 'sender_key'), 'sender_key'),
        'hosts': host_records,
    }


def query_live_weekly_boards(conn, room_id, now):
    init_leaderboard_schema(conn)
    period_start, period_end = beijing_week_bounds(now)
    cutoff = min(int(now), period_end - 1)
    # 按厅聚合：room_id 每场直播都变，只查当前 room_id 等于只查这一场。
    boards = _query_period_boards(
        conn, hall_room_ids(conn, room_id), period_start, cutoff
    )
    return {
        'room_id': str(room_id),
        'hall_name': resolve_hall_name(conn, room_id),
        'period_start': period_start,
        'period_end': period_end,
        'locked': False,
        'locked_at': None,
        'visitors': boards['visitors'],
        'hosts': boards['hosts'],
    }


def lock_completed_weeks(conn, now):
    init_leaderboard_schema(conn)
    current_start, _ = beijing_week_bounds(now)
    period_start = current_start - 7 * 86400
    period_end = current_start
    backfill_room_halls(conn)
    room_rows = conn.execute('''
        SELECT DISTINCT room_id
        FROM interaction_log
        WHERE type = 'gift'
          AND timestamp >= ?
          AND timestamp < ?
    ''', (period_start, period_end)).fetchall()
    # 按厅归组：同一个厅一周内的多场直播归档成一行，否则归档下来还是「每场一份」。
    halls = {}
    for room_row in room_rows:
        rid = str(room_row['room_id'])
        key = resolve_hall_name(conn, rid) or rid
        halls.setdefault(key, []).append(rid)
    locked = []
    for hall_name, hall_rooms in halls.items():
        try:
            room_id = sorted(hall_rooms)[0]
            rooms = hall_room_ids(conn, room_id)
            existing = conn.execute('''
                SELECT id FROM weekly_periods
                WHERE hall_name = ? AND period_start = ?
            ''', (hall_name, period_start)).fetchone()
            if existing is not None:
                continue
            boards = _query_period_boards(
                conn, rooms, period_start, period_end - 1
            )
            with conn:
                cursor = conn.execute('''
                    INSERT INTO weekly_periods (
                        room_id, hall_name, period_start, period_end, locked_at
                    ) VALUES (?, ?, ?, ?, ?)
                ''', (room_id, hall_name, period_start, period_end, int(now)))
                period_id = int(cursor.lastrowid)
                for board_type, records, key_name in (
                    ('visitor', boards['visitors'], 'sender_key'),
                    ('host', boards['hosts'], 'recipient_key'),
                ):
                    for record in records:
                        conn.execute('''
                            INSERT INTO weekly_rows (
                                period_id, board_type, participant_key,
                                display, tickets, rank
                            ) VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            period_id, board_type, record[key_name],
                            record['display'], int(record['tickets']),
                            int(record['rank']),
                        ))
                for host in boards['hosts']:
                    for detail in host.get('large_details', []):
                        conn.execute('''
                            INSERT INTO weekly_host_large_details (
                                period_id, host_key, visitor_key,
                                visitor_display, slot_start, slot_end, tickets
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            period_id, host['recipient_key'], detail['visitor_key'],
                            detail['visitor_display'], detail['slot_start'],
                            detail['slot_end'], detail['tickets'],
                        ))
            locked.append({
                'room_id': room_id,
                'period_start': period_start,
                'period_end': period_end,
                'locked_at': int(now),
            })
        except Exception as exc:
            # 一个厅归档失败不能拖垮其它厅。原来没有这层守卫：某个厅抛
            # 异常，整趟归档回滚、所有厅的周榜一起 500，而 period_start
            # 只看紧邻的上一周——过了周六那一周就永远归不了档，
            # _query_locked_week 永久 404。触发器可以修，放大器也得收。
            print(f'[周榜] 归档 {hall_name} 失败，跳过该厅: {exc}', flush=True)
            continue
    return locked


def _query_locked_week(conn, room_id, period_start):
    # 归档按厅存；本周的新场次 room_id 在上周并不存在，只能用厅名去找。
    hall_name = resolve_hall_name(conn, room_id) or str(room_id)
    period = conn.execute('''
        SELECT * FROM weekly_periods
        WHERE hall_name = ? AND period_start = ?
    ''', (hall_name, int(period_start))).fetchone()
    if period is None:
        raise LookupError('周榜周期不存在')
    rows = conn.execute('''
        SELECT board_type, participant_key, display, tickets, rank
        FROM weekly_rows
        WHERE period_id = ?
        ORDER BY board_type ASC, rank ASC, participant_key ASC
    ''', (int(period['id']),)).fetchall()
    details = conn.execute('''
        SELECT host_key, visitor_key, visitor_display,
               slot_start, slot_end, tickets
        FROM weekly_host_large_details
        WHERE period_id = ?
        ORDER BY slot_start ASC, tickets DESC, visitor_key ASC
    ''', (int(period['id']),)).fetchall()
    detail_map = {}
    for raw in details:
        item = dict(raw)
        item['tickets'] = int(item['tickets'])
        detail_map.setdefault(str(item['host_key']), []).append(item)
    visitors = []
    hosts = []
    for raw in rows:
        item = dict(raw)
        item['tickets'] = int(item['tickets'])
        item['rank'] = int(item['rank'])
        if item.pop('board_type') == 'visitor':
            item['sender_key'] = item.pop('participant_key')
            visitors.append(item)
        else:
            item['recipient_key'] = item.pop('participant_key')
            item['large_details'] = detail_map.get(item['recipient_key'], [])
            hosts.append(item)
    return {
        'room_id': str(room_id),
        'period_start': int(period['period_start']),
        'period_end': int(period['period_end']),
        'locked': True,
        'locked_at': int(period['locked_at']),
        'visitors': visitors,
        'hosts': hosts,
    }


def query_weekly_board(conn, room_id, now, week_start=None):
    init_leaderboard_schema(conn)
    lock_completed_weeks(conn, now)
    if week_start is None:
        return query_live_weekly_boards(conn, room_id, now)
    return _query_locked_week(conn, room_id, int(week_start))


def list_week_periods(conn, room_id):
    init_leaderboard_schema(conn)
    hall_name = resolve_hall_name(conn, room_id) or str(room_id)
    rows = conn.execute('''
        SELECT room_id, hall_name, period_start, period_end, locked_at
        FROM weekly_periods
        WHERE hall_name = ?
        ORDER BY period_start DESC
    ''', (hall_name,)).fetchall()
    return [dict(row) for row in rows]
