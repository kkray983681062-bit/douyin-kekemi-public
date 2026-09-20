"""麦上主持静默提醒：判定、告警、证据快照的纯逻辑。

麦上主持（大头除外）连续 15 分钟没在公屏打字就提醒 admin；累计满 3 次
留下一条带聊天证据的永久记录。

不依赖 Flask，可独立测试。设计见
docs/superpowers/specs/2026-08-20-silence-alert-design.md
"""

import json
from datetime import datetime, timedelta, timezone

_BEIJING_TZ = timezone(timedelta(hours=8))

# 证据按北京时间自然 2 小时分档：00:00-02:00 … 22:00-24:00
BUCKET_HOURS = 2

# 满几次生成永久证据记录
EVIDENCE_AT_COUNT = 3

# 管道多久没动静就算死了。扫描每 60 秒一轮，正常运转的厅每分钟都有
# 聊天/礼物/进场落库；20 分钟一条都没有，说明写入侧出了问题
# （record_all 被关、WS 断线重连、_save_interaction 静默失败）。
PIPELINE_STALE_AFTER = 20 * 60

# 连续几次快照看不到某人才算下麦。WS 断线重连时麦位列表会短暂为空，
# 直接判定下麦会误清计数。
MISSING_TICKS_TO_LEAVE = 3

# 告警条只回答「现在需要谁去处理」：超过一天没处理的不再是待办，
# 历史证据由 mic_silence_records 存着，没必要也不该在这张表上无限往回翻。
# 这个下限同时是让 created_at 索引能被用上的前提——见 list_open_alerts。
ALERT_LOOKBACK_SECONDS = 24 * 3600


def _ensure_column(conn, table, column, definition):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)}
    if column not in columns:
        conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, column, definition))


def init_silence_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mic_silence_state (
            room_id TEXT NOT NULL,
            sec_uid TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            douyin_id TEXT NOT NULL DEFAULT '',
            hall_name TEXT NOT NULL DEFAULT '',
            mic_since INTEGER NOT NULL,
            last_spoke_at INTEGER NOT NULL,
            alert_count INTEGER NOT NULL DEFAULT 0,
            last_alert_at INTEGER NOT NULL DEFAULT 0,
            missing_ticks INTEGER NOT NULL DEFAULT 0,
            -- 已废弃：早先用它保证「一轮上麦只出一条证据」。改成按 2 小时
            -- 档 upsert 之后（同档更新、换档新建），不再需要这个标志。
            -- 保留列是为了免一次迁移；代码里已经没有任何地方读写它。
            evidence_done INTEGER NOT NULL DEFAULT 0,
            -- 计数归属的 2 小时档（该档 00 分的 epoch）。整点一过就归零重来：
            -- 早先是整轮上麦一路累加，在麦 4 小时就报到「第 14 次」，这个
            -- 数字既不反映他现在有多离谱，也没法跟别人比。
            band_start INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (room_id, sec_uid)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mic_silence_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            douyin_id TEXT NOT NULL DEFAULT '',
            hall_name TEXT NOT NULL DEFAULT '',
            sec_uid TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            alert_index INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            resolved_at INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_silence_alerts_open
        ON mic_silence_alerts(resolved_at, created_at DESC)
    ''')
    # 这条曾经是主力：当时 list_open_alerts 只筛 created_at，上面那个复合
    # 索引用不上。现在查询同时筛 resolved_at = 0 和 created_at 下限，
    # 复合索引两头都吃得上、连排序都省了，实测计划就是
    # `SEARCH a USING INDEX idx_silence_alerts_open (resolved_at=? AND created_at>?)`。
    # 保留它是因为删索引要迁移，而它对写入的开销可以忽略。
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_silence_alerts_created
        ON mic_silence_alerts(created_at DESC)
    ''')
    # 证据表不参与 7 天清理：interaction_log 会被清掉，所以这里存内容快照。
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mic_silence_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            douyin_id TEXT NOT NULL DEFAULT '',
            hall_name TEXT NOT NULL DEFAULT '',
            sec_uid TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            beijing_date TEXT NOT NULL,
            mic_since INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            alert_count INTEGER NOT NULL,
            chat_snapshot TEXT NOT NULL DEFAULT '{}',
            band_start INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_silence_records_room
        ON mic_silence_records(room_id, created_at DESC)
    ''')
    # 订阅键是抖音号：room_id 每场直播都变，厅名主播随时能改。
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mic_silence_watches (
            user_id TEXT NOT NULL,
            douyin_id TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, douyin_id)
        )
    ''')
    # 已读是 per-admin 的：A 读过不能影响 B 的角标，所以不能是告警表上的
    # 一列。建表时它是「手动叉掉」用的；手动叉掉随主页告警条一起废除后，
    # 这张表复用为已读标记，没做重命名迁移（见 mark_alerts_read）。
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mic_silence_dismissals (
            alert_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            dismissed_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (alert_id, user_id)
        )
    ''')
    # 线上已有表，加列要走 ALTER（CREATE TABLE IF NOT EXISTS 对已存在的表
    # 不生效）。默认 0 表示「老数据，没有档归属」。
    _ensure_column(conn, 'mic_silence_state', 'band_start', 'INTEGER NOT NULL DEFAULT 0')
    _ensure_column(conn, 'mic_silence_records', 'band_start', 'INTEGER NOT NULL DEFAULT 0')
    # 这个索引必须建在补列之后：老库里 mic_silence_records 已经存在，
    # CREATE TABLE IF NOT EXISTS 不会给它加列，索引引用 band_start 会直接炸。
    # 一个「厅 × 主持 × 2 小时档」最多一条记录：档内满 3 次先建，之后同档
    # 再静默就把这条更新到最新次数和最新快照，不新建。
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_silence_records_band '
        'ON mic_silence_records(room_id, sec_uid, band_start) '
        'WHERE band_start <> 0')
    conn.commit()


def _latest_chat_at(conn, room_id, sec_uid, since):
    row = conn.execute(
        "SELECT MAX(timestamp) FROM interaction_log "
        "WHERE room_id = ? AND sec_uid = ? AND type = 'chat' AND timestamp > ?",
        (str(room_id), str(sec_uid), int(since)),
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


def _resolve_open_alerts(conn, room_id, sec_uid, now):
    conn.execute(
        'UPDATE mic_silence_alerts SET resolved_at = ? '
        'WHERE room_id = ? AND sec_uid = ? AND resolved_at = 0',
        (int(now), str(room_id), str(sec_uid)),
    )


def close_room(conn, room_id, now):
    """一个厅下播或停止监听时收尾：清状态行、把未结的告警标记为已结束。

    不做这一步的话，下播那一刻还开着的告警会永远停在 resolved_at = 0，
    前端把它渲染成红色的「正在静默」，说的却是几小时前就下麦的人。
    每天每个厅都会攒一批，几天就堆满整个告警条。
    """
    room_id = str(room_id)
    now = int(now)
    with conn:
        conn.execute(
            'UPDATE mic_silence_alerts SET resolved_at = ? '
            'WHERE room_id = ? AND resolved_at = 0',
            (now, room_id))
        conn.execute(
            'DELETE FROM mic_silence_state WHERE room_id = ?',
            (room_id,))


def scan_room(conn, room_id, mic_users, anchor_sec_uid, douyin_id,
              hall_name, now, threshold):
    """推进一个厅的静默状态，返回本轮新产生的告警。

    大头（主播本人）直接跳过——数据实测他们从不在公屏打字，
    不排除就是天天误报。
    """
    room_id = str(room_id)
    now = int(now)
    anchor = str(anchor_sec_uid or '')
    present = {}
    for user in mic_users or []:
        sec_uid = str((user or {}).get('sec_uid') or '')
        if not sec_uid or sec_uid == anchor:
            continue
        present[sec_uid] = str((user or {}).get('nickname') or '')

    existing = {
        str(row['sec_uid']): row
        for row in conn.execute(
            'SELECT * FROM mic_silence_state WHERE room_id = ?', (room_id,))
    }

    fired = []
    with conn:
        # 新上麦
        for sec_uid, nickname in present.items():
            if sec_uid in existing:
                continue
            conn.execute(
                'INSERT INTO mic_silence_state (room_id, sec_uid, nickname, '
                'douyin_id, hall_name, mic_since, last_spoke_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (room_id, sec_uid, nickname, str(douyin_id or ''),
                 str(hall_name or ''), now, now),
            )

        # 不在麦上了
        for sec_uid, row in existing.items():
            if sec_uid in present:
                continue
            ticks = int(row['missing_ticks']) + 1
            if ticks >= MISSING_TICKS_TO_LEAVE:
                conn.execute(
                    'DELETE FROM mic_silence_state WHERE room_id = ? AND sec_uid = ?',
                    (room_id, sec_uid))
                _resolve_open_alerts(conn, room_id, sec_uid, now)
            else:
                conn.execute(
                    'UPDATE mic_silence_state SET missing_ticks = ? '
                    'WHERE room_id = ? AND sec_uid = ?',
                    (ticks, room_id, sec_uid))

        # 在麦上的：更新发言、判定超时
        for sec_uid, nickname in present.items():
            row = existing.get(sec_uid)
            if row is None:
                continue                      # 本轮刚建，下一轮再判
            last_spoke = int(row['last_spoke_at'])
            spoke_at = _latest_chat_at(conn, room_id, sec_uid, last_spoke)
            if spoke_at:
                last_spoke = spoke_at
                _resolve_open_alerts(conn, room_id, sec_uid, now)
            conn.execute(
                'UPDATE mic_silence_state SET last_spoke_at = ?, '
                'missing_ticks = 0, nickname = ? '
                'WHERE room_id = ? AND sec_uid = ?',
                (last_spoke, nickname or row['nickname'], room_id, sec_uid))

            # 取「最后发言」和「上次提醒」的较晚者：只看 last_spoke_at 的话，
            # 一旦超时就会每分钟报一次（扫描本身是每分钟跑的）。
            baseline = max(last_spoke, int(row['last_alert_at']))
            if now - baseline < int(threshold):
                continue
            # 计数按自然 2 小时档走：整点一过归零重来。原来整轮上麦一路
            # 累加，在麦 4 小时就报到「第 14 次」，那个数字既不反映他现在
            # 有多离谱，也没法跟别人比。last_alert_at 不跟着重置——它管的是
            # 「报过一次要重新等满一个阈值」，跟计数是两回事。
            band = _band_bounds(now)[0]
            carried = int(row['alert_count']) if int(row['band_start']) == band else 0
            index = carried + 1
            cursor = conn.execute(
                'INSERT INTO mic_silence_alerts (room_id, douyin_id, hall_name, '
                'sec_uid, nickname, alert_index, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (room_id, str(douyin_id or ''), str(hall_name or ''), sec_uid,
                 nickname or row['nickname'], index, now),
            )
            conn.execute(
                'UPDATE mic_silence_state SET alert_count = ?, last_alert_at = ?, '
                'band_start = ? WHERE room_id = ? AND sec_uid = ?',
                (index, now, band, room_id, sec_uid))
            fired.append({
                'alert_id': int(cursor.lastrowid),
                'sec_uid': sec_uid,
                'nickname': nickname or row['nickname'],
                'alert_index': index,
            })
            if index >= EVIDENCE_AT_COUNT:
                if _pipeline_alive(conn, room_id, now):
                    # 同档已有记录时，快照要从「本档最早那次上麦」算起。
                    # 掉麦重进会让状态行的 mic_since 变新，只按新的算的话，
                    # 早段的发言就被后半段的快照顶掉了——而这张表豁免 7 天
                    # 清理、interaction_log 过期即删，顶掉的内容不可恢复。
                    prior = conn.execute(
                        'SELECT alert_count, mic_since FROM mic_silence_records '
                        'WHERE room_id = ? AND sec_uid = ? AND band_start = ?',
                        (room_id, sec_uid, band)).fetchone()
                    since = int(row['mic_since'])
                    if prior:
                        since = min(since, int(prior['mic_since']))
                    snapshot = build_chat_snapshot(
                        conn, room_id, sec_uid, since, now, threshold)
                    # 次数取较大者：掉麦重进后计数从 0 重来，直接写进去会把
                    # 已经记下的 4 次改成 3 次，永久证据不能这么往下掉。
                    kept = max(index, int(prior['alert_count'])) if prior else index
                    # 一个「厅 × 主持 × 2 小时档」最多一条：档内满 3 次先建，
                    # 之后同档再静默就把这条更新到最新次数和最新快照。
                    # 6-8 档可以涨到 7 次，8-10 档是另一条、从 0 重新开始。
                    conn.execute(
                        'INSERT INTO mic_silence_records (room_id, douyin_id, '
                        'hall_name, sec_uid, nickname, beijing_date, mic_since, '
                        'created_at, alert_count, chat_snapshot, band_start) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                        'ON CONFLICT(room_id, sec_uid, band_start) '
                        'WHERE band_start <> 0 DO UPDATE SET '
                        '  alert_count = excluded.alert_count, '
                        '  created_at = excluded.created_at, '
                        '  nickname = excluded.nickname, '
                        '  douyin_id = excluded.douyin_id, '
                        '  hall_name = excluded.hall_name, '
                        '  mic_since = excluded.mic_since, '
                        '  chat_snapshot = excluded.chat_snapshot',
                        (room_id, str(douyin_id or ''), str(hall_name or ''),
                         sec_uid, nickname or row['nickname'],
                         _beijing(now).strftime('%Y-%m-%d'),
                         since, now, kept,
                         json.dumps(snapshot, ensure_ascii=False), band),
                    )
                else:
                    # 全厅这段窗口一条聊天都没有：日志本身断供了（record_all
                    # 被关/WS 断线重连/写入失败），不是这个人真没说话。不写
                    # 证据——留着 alert_count，下一次满足条件的告警会重新走到
                    # 这里，日志一旦恢复就能补上。
                    print(
                        f'[静默] {room_id} 入库管道已停 '
                        f'{PIPELINE_STALE_AFTER // 60} 分钟以上，'
                        f'不给 {nickname or sec_uid} 生成证据（空白说的是管道的事）',
                        flush=True)
    return fired


def _beijing(ts):
    return datetime.fromtimestamp(int(ts), tz=_BEIJING_TZ)


def _day_start_ts(ts):
    """某个时刻所在北京日的 00:00 对应的 epoch 秒。"""
    moment = _beijing(ts)
    return int(moment.replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())


def _snapshot_window(mic_since, now):
    """证据窗口 = now 所在的那个 2 小时档，且不早于上麦时刻。

    2 小时档是记账单位：档内满 3 次就挂一条记录，整点一过重新开始。所以
    证据也只该覆盖那一档——档一说的话不能算进档二的账上。

    取 max(档起点, mic_since)：他可能是档中途才上麦，那之前的时间他根本
    不在麦上，不能算成他的静默。

    这个窗口只给快照用，**不给 corroboration 用**（见 CORROBORATION_LOOKBACK）。
    两者早先是同一个窗口，那时快照覆盖整轮上麦、够宽。现在快照被切到档内、
    还要从上麦时刻算起，档中途上麦的人窗口可能只有几分钟——而「几分钟没人
    说话」在安静时段完全正常，拿它去判断「日志是不是断供了」只会误判。
    """
    band_start = _band_bounds(now)[0]
    start_ts = max(band_start, int(mic_since))
    end_ts = int(now)
    if end_ts <= start_ts:      # 刚上麦就出证据；不该发生，兜底成一分钟
        end_ts = start_ts + 60
    return start_ts, end_ts


def _band_bounds(ts):
    """某个时刻所在的 2 小时档的起止 epoch 秒（按北京时间自然切分）。"""
    day = _day_start_ts(ts)
    index = (int(ts) - day) // (BUCKET_HOURS * 3600)
    lower = day + index * BUCKET_HOURS * 3600
    return lower, lower + BUCKET_HOURS * 3600


def _pipeline_alive(conn, room_id, now):
    """这个厅的入库管道此刻还活着吗。

    生成证据前拿这个把关：interaction_log 可能因为 record_all 被关、
    WS 断线重连、或写入失败（_save_interaction 失败只写日志、不往上抛）
    而出现空档。空档说的是管道的事，不是这个主持的事，拿它去指认某个
    具体的人「没说话」就是编数据填证据的空。

    看的是「最后一条记录距现在多久」，而且**不限 type**——礼物、进场都
    说明管道在工作。早先两版都错在问「窗口内有没有人聊天」：跟着快照走
    时窗口太窄；改成固定回看 2 小时又太宽，日志在窗口前半段还活着、后半段
    死掉照样能过检，于是在一段已经没有数据的时间上写出永久证据。而且安静
    厅里两小时真的没人聊天是常态，那样又会把该出的证据全卡掉。问「管道
    最后一次动是什么时候」跟厅里聊不聊天无关，两个方向的误判一起收敛。
    """
    row = conn.execute(
        'SELECT MAX(timestamp) FROM interaction_log WHERE room_id = ?',
        (str(room_id),),
    ).fetchone()
    if not row or row[0] is None:
        return False
    return int(now) - int(row[0]) <= PIPELINE_STALE_AFTER


def build_chat_snapshot(conn, room_id, sec_uid, mic_since, now, threshold):
    """把这一轮上麦按北京时间 2 小时档快照出来。

    必须复制内容而不是存引用：interaction_log 是 7 天自动清理的，
    只存引用的话证据过几天就变空了。

    只列这轮上麦真正跨到的那几档，档内也只算上麦之后的发言（见
    _snapshot_window）。每条带 gap = 距上一条隔了多久，第一条的 gap 是
    「上麦到第一句」；tail_gap 是「最后一句到出证据」。真正构成证据的
    正是这些空白，光有发言时间还得让人自己减。
    """
    start_ts, end_ts = _snapshot_window(mic_since, now)

    rows = conn.execute(
        "SELECT timestamp, content FROM interaction_log "
        "WHERE room_id = ? AND sec_uid = ? AND type = 'chat' "
        "AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (str(room_id), str(sec_uid), start_ts, end_ts),
    ).fetchall()

    # gap 要在整段上算，不能按档算——跨档的两条之间同样是一段空白。
    spoken = []
    previous = start_ts
    for row in rows:
        ts = int(row['timestamp'])
        spoken.append({
            'ts': ts,
            'at': _beijing(ts).strftime('%H:%M:%S'),
            'text': str(row['content'] or ''),
            'gap': ts - previous,
        })
        previous = ts

    buckets = []
    band_start = _band_bounds(start_ts)[0]
    while band_start < end_ts:
        band_end = band_start + BUCKET_HOURS * 3600
        messages = [
            {k: v for k, v in m.items() if k != 'ts'}
            for m in spoken if band_start <= m['ts'] < band_end
        ]
        index = (band_start - _day_start_ts(band_start)) // (BUCKET_HOURS * 3600)
        buckets.append({
            'date': _beijing(band_start).strftime('%Y-%m-%d'),
            'start': '%02d:00' % (index * BUCKET_HOURS),
            # 末档写 24:00 而不是 00:00：「22:00–00:00」读起来像跨了天
            'end': '%02d:00' % ((index + 1) * BUCKET_HOURS),
            'count': len(messages),
            'messages': messages,
        })
        band_start = band_end

    return {
        'buckets': buckets,
        'mic_since': _beijing(start_ts).strftime('%H:%M:%S'),
        'until': _beijing(end_ts).strftime('%H:%M:%S'),
        'tail_gap': end_ts - (spoken[-1]['ts'] if spoken else start_ts),
        # 记下当时的阈值：日后改了配置，老记录仍按当时的口径渲染
        'gap_threshold': int(threshold),
    }


def set_watches(conn, user_id, douyin_ids, now):
    """整份替换某个 admin 的订阅，返回落库后的抖音号列表。"""
    user_id = str(user_id or '')
    if not user_id:
        return []
    wanted = sorted({
        str(item).strip() for item in (douyin_ids or []) if str(item).strip()
    })
    with conn:
        conn.execute('DELETE FROM mic_silence_watches WHERE user_id = ?',
                     (user_id,))
        for douyin_id in wanted:
            conn.execute(
                'INSERT OR IGNORE INTO mic_silence_watches '
                '(user_id, douyin_id, created_at) VALUES (?, ?, ?)',
                (user_id, douyin_id, int(now)))
    return wanted


def list_watches(conn, user_id):
    return [
        str(row[0]) for row in conn.execute(
            'SELECT douyin_id FROM mic_silence_watches '
            'WHERE user_id = ? ORDER BY douyin_id', (str(user_id or ''),))
    ]


def list_open_alerts(conn, user_id, now, limit=50):
    """这个 admin 订阅的厅里、此刻仍在静默的告警，带 read 标记。

    read 只影响铃铛角标，**不影响是否返回**。曾经把「已读」直接做成
    叉掉式的过滤，结果一打开面板、5 秒后轮询回来，面板就写「此刻没有
    人在静默」——而那几个人还在麦上、还在静默，看一眼就把要看的东西
    看没了。要消失的只有「已恢复发言」，那是状态变了；读过只是我看过了。

    早先已恢复发言的也返回、由前端灰色显示、只有手动叉掉才消失。实跑
    一晚被否了：一个厅一晚攒 30 多条灰的，把真正要看的 2 条红的埋了。
    现在恢复发言即从列表消失（resolved_at 非 0 就不返回），告警条也从
    主页拿掉、改成铃铛上挂未读数。

    只看最近 ALERT_LOOKBACK_SECONDS：这个接口挂在 5 秒轮询上，不给时间
    下限的话 SQLite 只能整表扫描再拿临时 B-tree 排序——单 worker 进程里
    这是从监听和 SSE 那边偷走的 GIL 时间。现在筛 resolved_at 又筛
    created_at，(resolved_at, created_at) 那个复合索引两头都吃得上，
    连排序都省了。`now` 由调用方传入，跟本文件其它函数一致：这里不读
    墙钟，纯函数，测试才能摆脱真实时间。
    """
    user_id = str(user_id or '')
    cutoff = int(now) - ALERT_LOOKBACK_SECONDS
    rows = conn.execute(
        'SELECT a.*, d.dismissed_at AS read_at FROM mic_silence_alerts a '
        'JOIN mic_silence_watches w '
        '  ON w.douyin_id = a.douyin_id AND w.user_id = ? '
        'LEFT JOIN mic_silence_dismissals d '
        '  ON d.alert_id = a.id AND d.user_id = ? '
        'WHERE a.resolved_at = 0 AND a.created_at >= ? '
        'ORDER BY a.created_at DESC LIMIT ?',
        (user_id, user_id, cutoff, int(limit)),
    ).fetchall()
    return [
        {
            'id': int(row['id']),
            'room_id': str(row['room_id']),
            'douyin_id': str(row['douyin_id']),
            'hall_name': str(row['hall_name']),
            'sec_uid': str(row['sec_uid']),
            'nickname': str(row['nickname']),
            'alert_index': int(row['alert_index']),
            'created_at': int(row['created_at']),
            'read': row['read_at'] is not None,
        }
        for row in rows
    ]


def mark_alerts_read(conn, user_id, alert_ids, now):
    """把这些告警对这个 admin 标为已读——「打开铃铛面板 = 读完」。

    沿用 mic_silence_dismissals 这张表（建表时它是「手动叉掉」用的，
    手动叉掉随着告警条一起废了，表复用为已读标记，没做重命名迁移）。
    已读只影响铃铛角标，不影响 list_open_alerts 返不返回——那条区别
    是踩过坑才分清的，见 list_open_alerts 的说明。

    重复调用靠 (alert_id, user_id) 主键去重，不会多出行；传进来不存在
    的 id 会留下孤儿行，由 _cleanup_expired_data 里那条
    `alert_id NOT IN (SELECT id FROM mic_silence_alerts)` 定期扫掉。
    """
    user_id = str(user_id or '')
    if not user_id or not alert_ids:
        return 0
    with conn:
        conn.executemany(
            'INSERT OR IGNORE INTO mic_silence_dismissals '
            '(alert_id, user_id, dismissed_at) VALUES (?, ?, ?)',
            [(int(a), user_id, int(now)) for a in alert_ids],
        )
    return len(alert_ids)


def unread_counts_by_hall(conn, user_id, now):
    """每个厅有几条未读——铃铛挂在厅按钮上，所以按厅给数。"""
    counts = {}
    # limit 跟 list_open_alerts 的默认值对齐：两边不一致的话，未读超过
    # 上限时角标和面板会各说各话——角标显示有、点开却是空的。
    for alert in list_open_alerts(conn, user_id, now):
        if alert['read']:
            continue
        key = alert['douyin_id']
        counts[key] = counts.get(key, 0) + 1
    return counts
