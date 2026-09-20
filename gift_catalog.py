"""全局礼物价格库、管理员覆盖、审计和最近记录重算。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GiftPriceResolution:
    unit_diamonds: int | None
    source: str

    @property
    def known(self):
        return self.unit_diamonds is not None


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _table_columns(conn, table_name):
    return {
        row[1] for row in conn.execute(f'PRAGMA table_info({table_name})').fetchall()
    }


def init_gift_catalog(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gift_catalog (
            gift_id TEXT PRIMARY KEY,
            gift_name TEXT NOT NULL DEFAULT '',
            observed_unit_diamonds INTEGER,
            manual_unit_diamonds INTEGER,
            note TEXT NOT NULL DEFAULT '',
            price_pending INTEGER NOT NULL DEFAULT 1,
            occurrence_count INTEGER NOT NULL DEFAULT 0,
            first_seen INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gift_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gift_id TEXT NOT NULL,
            gift_name TEXT NOT NULL DEFAULT '',
            old_unit_diamonds INTEGER,
            new_unit_diamonds INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            changed_at INTEGER NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_gift_price_history_gift_time
        ON gift_price_history(gift_id, changed_at DESC, id DESC)
    ''')
    # 按礼物「名字」兜底定价：抖音同一 gift_id 名字会漂移，且没见过的礼物拿不到
    # gift_id，无法预先按 id 录价。这张表按人类可读的礼物名定价，优先级高于按
    # gift_id 的价，从而抗 id 名字漂移、并支持给「尚未出现过」的礼物预置价格。
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gift_name_prices (
            gift_name TEXT PRIMARY KEY,
            unit_diamonds INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        )
    ''')
    # 「多皮肤各价不同」的礼物：一个 gift_id 只要有某个皮肤被单独按名字定过价，
    # 就登记在这里。此后该 gift_id 再出现没定过价的名字，一律视为新皮肤 → 待补价格，
    # 绝不借用基础价/别的皮肤价（普通单名礼物不入此表，走原有抖音价逻辑）。
    conn.execute('''
        CREATE TABLE IF NOT EXISTS skinned_gift_ids (
            gift_id TEXT PRIMARY KEY
        )
    ''')
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if 'interaction_log' in tables:
        columns = _table_columns(conn, 'interaction_log')
        if 'price_source' not in columns:
            conn.execute(
                "ALTER TABLE interaction_log ADD COLUMN price_source TEXT DEFAULT ''"
            )
        columns = _table_columns(conn, 'interaction_log')
        for name, definition in (
            ('ticket_count', 'INTEGER'),
            ('ticket_source', "TEXT DEFAULT 'unknown'"),
            ('room_ticket_total', 'INTEGER'),
        ):
            if name not in columns:
                conn.execute(
                    f'ALTER TABLE interaction_log ADD COLUMN {name} {definition}'
                )
    conn.commit()


def observe_gift(conn, gift_id, gift_name, live_unit_diamonds, now):
    gift_id = str(gift_id or '').strip()
    if not gift_id:
        raise ValueError('gift_id 不能为空')
    gift_name = str(gift_name or '').strip() or gift_id
    live_price = _positive_int(live_unit_diamonds)
    timestamp = int(now)
    with conn:
        conn.execute('''
            INSERT INTO gift_catalog (
                gift_id, gift_name, observed_unit_diamonds,
                price_pending, occurrence_count, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(gift_id) DO UPDATE SET
                gift_name = CASE
                    WHEN excluded.gift_name <> '' THEN excluded.gift_name
                    ELSE gift_catalog.gift_name
                END,
                observed_unit_diamonds = CASE
                    WHEN excluded.observed_unit_diamonds IS NOT NULL
                        THEN excluded.observed_unit_diamonds
                    ELSE gift_catalog.observed_unit_diamonds
                END,
                price_pending = CASE
                    WHEN gift_catalog.manual_unit_diamonds IS NOT NULL THEN 0
                    WHEN excluded.observed_unit_diamonds IS NOT NULL THEN 0
                    ELSE gift_catalog.price_pending
                END,
                occurrence_count = gift_catalog.occurrence_count + 1,
                last_seen = MAX(gift_catalog.last_seen, excluded.last_seen)
        ''', (
            gift_id,
            gift_name,
            live_price,
            0 if live_price is not None else 1,
            timestamp,
            timestamp,
        ))
        # 这个名字本身已被单独定名字价 → 登记其 gift_id 为「多皮肤」，
        # 之后该 gift_id 的其它未定价名字走待补。
        if conn.execute(
            'SELECT 1 FROM gift_name_prices WHERE gift_name = ? LIMIT 1',
            (gift_name,),
        ).fetchone():
            conn.execute(
                'INSERT OR IGNORE INTO skinned_gift_ids (gift_id) VALUES (?)',
                (gift_id,),
            )


def _is_skinned_gift(conn, gift_id):
    """该 gift_id 是否已有某个皮肤被单独定价（即「多皮肤各价不同」的礼物）。"""
    gift_id = str(gift_id or '').strip()
    if not gift_id:
        return False
    return conn.execute(
        'SELECT 1 FROM skinned_gift_ids WHERE gift_id = ? LIMIT 1', (gift_id,)
    ).fetchone() is not None


def _mark_skinned_gift_ids_for_name(conn, gift_name):
    """把某个已定名字价的名字，映射到它出现过的所有 gift_id 并登记为「多皮肤」。"""
    name = str(gift_name or '').strip()
    if not name:
        return
    rows = conn.execute(
        "SELECT DISTINCT gift_id FROM interaction_log "
        "WHERE type = 'gift' AND content = ? AND COALESCE(gift_id, '') <> ''",
        (name,),
    ).fetchall()
    for row in rows:
        conn.execute(
            'INSERT OR IGNORE INTO skinned_gift_ids (gift_id) VALUES (?)',
            (str(row[0]),),
        )


def resolve_name_price(conn, gift_name):
    """按礼物名查兜底价；查不到返回 None。"""
    name = str(gift_name or '').strip()
    if not name or name == '?':
        return None
    row = conn.execute(
        'SELECT unit_diamonds FROM gift_name_prices WHERE gift_name = ?',
        (name,),
    ).fetchone()
    if row is None:
        return None
    return _positive_int(row['unit_diamonds'])


def resolve_gift_price(conn, gift_id, live_unit_diamonds=None, gift_name=None):
    gift_id = str(gift_id or '').strip()
    # 名字价优先级最高：人工按名字校准的价格是权威口径，且抗 gift_id 名字漂移。
    name_price = resolve_name_price(conn, gift_name)
    if name_price is not None:
        return GiftPriceResolution(name_price, 'manual')
    # 皮肤规则：这个 gift_id 已有别的皮肤被单独定价（多皮肤各价不同），而当前名字
    # 没定过价 → 视为新皮肤，待补价格，绝不借用基础价/别的皮肤价。普通单名礼物
    # 不在 skinned_gift_ids 里，跳过此分支、走下面原有的抖音价逻辑。
    name = str(gift_name or '').strip()
    if name and name != '?' and _is_skinned_gift(conn, gift_id):
        return GiftPriceResolution(None, 'pending')
    row = conn.execute('''
        SELECT observed_unit_diamonds, manual_unit_diamonds
        FROM gift_catalog WHERE gift_id = ?
    ''', (gift_id,)).fetchone()
    if row is not None:
        manual = _positive_int(row['manual_unit_diamonds'])
        if manual is not None:
            return GiftPriceResolution(manual, 'manual')
    live = _positive_int(live_unit_diamonds)
    if live is not None:
        return GiftPriceResolution(live, 'douyin')
    if row is not None:
        observed = _positive_int(row['observed_unit_diamonds'])
        if observed is not None:
            return GiftPriceResolution(observed, 'douyin')
    return GiftPriceResolution(None, 'unknown')


def reprice_recent_interactions(
        conn, gift_id, unit_diamonds, now, window_seconds=7 * 86400):
    unit_diamonds = _positive_int(unit_diamonds)
    if unit_diamonds is None:
        raise ValueError('礼物单价必须是正整数')
    columns = _table_columns(conn, 'interaction_log')
    if 'price_source' not in columns:
        conn.execute(
            "ALTER TABLE interaction_log ADD COLUMN price_source TEXT DEFAULT ''"
        )
    cursor = conn.execute('''
        UPDATE interaction_log
        SET unit_diamonds = ?,
            total_diamonds = ? * gift_quantity,
            price_known = 1,
            price_source = 'manual',
            ticket_count = ? * gift_quantity,
            ticket_source = 'gift_price'
        WHERE type = 'gift'
          AND gift_id = ?
          AND timestamp >= ?
          AND quantity_verified = 1
          AND gift_quantity > 0
          AND COALESCE(ticket_source, 'unknown') <> 'douyin_fan_ticket'
    ''', (
        unit_diamonds,
        unit_diamonds,
        unit_diamonds,
        str(gift_id),
        int(now) - int(window_seconds),
    ))
    return max(0, int(cursor.rowcount or 0))


def sync_douyin_prices(
        conn, price_by_gift_id, now, window_seconds=7 * 86400):
    """用抖音实时价回算近期记录，但永远不覆盖管理员价格。"""
    if not isinstance(price_by_gift_id, dict):
        return 0
    cutoff = int(now) - int(window_seconds)
    manual_ids = {
        str(row[0])
        for row in conn.execute('''
            SELECT gift_id FROM gift_catalog
            WHERE manual_unit_diamonds IS NOT NULL
              AND manual_unit_diamonds > 0
        ''').fetchall()
    }
    updated = 0
    with conn:
        for raw_id, raw_price in price_by_gift_id.items():
            gift_id = str(raw_id or '').strip()
            price = _positive_int(
                getattr(raw_price, 'unit_diamonds', raw_price)
            )
            if not gift_id or price is None or gift_id in manual_ids:
                continue
            conn.execute('''
                UPDATE gift_catalog
                SET observed_unit_diamonds = ?, price_pending = 0
                WHERE gift_id = ? AND manual_unit_diamonds IS NULL
            ''', (price, gift_id))
            cursor = conn.execute('''
                UPDATE interaction_log
                SET unit_diamonds = ?,
                    total_diamonds = ? * gift_quantity,
                    price_known = 1,
                    price_source = 'douyin',
                    ticket_count = ? * gift_quantity,
                    ticket_source = 'gift_price'
                WHERE type = 'gift'
                  AND gift_id = ?
                  AND timestamp >= ?
                  AND quantity_verified = 1
                  AND gift_quantity > 0
                  AND COALESCE(price_source, '') <> 'manual'
                  AND COALESCE(ticket_source, 'unknown') <> 'douyin_fan_ticket'
                  AND (
                    unit_diamonds IS NULL OR unit_diamonds <> ? OR
                    total_diamonds IS NULL OR
                    total_diamonds <> ? * gift_quantity OR
                    price_known <> 1 OR COALESCE(price_source, '') <> 'douyin'
                  )
            ''', (price, price, price, gift_id, cutoff, price, price))
            updated += max(0, int(cursor.rowcount or 0))
    return updated


def set_manual_price(
        conn, gift_id, gift_name, unit_diamonds, note, reason, actor, now):
    gift_id = str(gift_id or '').strip()
    if not gift_id:
        raise ValueError('gift_id 不能为空')
    price = _positive_int(unit_diamonds)
    if price is None:
        raise ValueError('礼物单价必须是正整数')
    reason = str(reason or '').strip()
    if not reason:
        raise ValueError('修改原因不能为空')
    gift_name = str(gift_name or '').strip() or gift_id
    note = str(note or '').strip()
    actor = str(actor or '').strip() or 'local_admin'
    timestamp = int(now)

    with conn:
        existing = conn.execute('''
            SELECT gift_name, manual_unit_diamonds
            FROM gift_catalog WHERE gift_id = ?
        ''', (gift_id,)).fetchone()
        if existing is None:
            conn.execute('''
                INSERT INTO gift_catalog (
                    gift_id, gift_name, manual_unit_diamonds, note,
                    price_pending, first_seen, last_seen, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
            ''', (
                gift_id, gift_name, price, note,
                timestamp, timestamp, actor, timestamp,
            ))
            old_price = None
        else:
            old_price = _positive_int(existing['manual_unit_diamonds'])
            if not gift_name:
                gift_name = str(existing['gift_name'] or gift_id)
            conn.execute('''
                UPDATE gift_catalog
                SET gift_name = ?, manual_unit_diamonds = ?, note = ?,
                    price_pending = 0, updated_by = ?, updated_at = ?
                WHERE gift_id = ?
            ''', (gift_name, price, note, actor, timestamp, gift_id))

        conn.execute('''
            INSERT INTO gift_price_history (
                gift_id, gift_name, old_unit_diamonds, new_unit_diamonds,
                note, reason, actor, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            gift_id, gift_name, old_price, price,
            note, reason, actor, timestamp,
        ))
        recalculated = reprice_recent_interactions(
            conn, gift_id, price, timestamp
        )
    return {
        'gift_id': gift_id,
        'unit_diamonds': price,
        'price_source': 'manual',
        'recalculated_rows': recalculated,
    }


def reprice_recent_interactions_by_name(
        conn, gift_name, unit_diamonds, now, window_seconds=7 * 86400):
    """按礼物名重算近期记录，逻辑与按 gift_id 版一致，只是匹配 content=名字。"""
    unit_diamonds = _positive_int(unit_diamonds)
    if unit_diamonds is None:
        raise ValueError('礼物单价必须是正整数')
    gift_name = str(gift_name or '').strip()
    if not gift_name:
        raise ValueError('礼物名不能为空')
    columns = _table_columns(conn, 'interaction_log')
    if 'price_source' not in columns:
        conn.execute(
            "ALTER TABLE interaction_log ADD COLUMN price_source TEXT DEFAULT ''"
        )
    cursor = conn.execute('''
        UPDATE interaction_log
        SET unit_diamonds = ?,
            total_diamonds = ? * gift_quantity,
            price_known = 1,
            price_source = 'manual',
            ticket_count = ? * gift_quantity,
            ticket_source = 'gift_price'
        WHERE type = 'gift'
          AND content = ?
          AND timestamp >= ?
          AND quantity_verified = 1
          AND gift_quantity > 0
          AND COALESCE(ticket_source, 'unknown') <> 'douyin_fan_ticket'
    ''', (
        unit_diamonds,
        unit_diamonds,
        unit_diamonds,
        gift_name,
        int(now) - int(window_seconds),
    ))
    return max(0, int(cursor.rowcount or 0))


def set_name_price(conn, gift_name, unit_diamonds, note, reason, actor, now):
    """按礼物名设权威价：写 gift_name_prices、审计到 gift_price_history、重算近期记录。"""
    gift_name = str(gift_name or '').strip()
    if not gift_name:
        raise ValueError('礼物名不能为空')
    price = _positive_int(unit_diamonds)
    if price is None:
        raise ValueError('礼物单价必须是正整数')
    reason = str(reason or '').strip()
    if not reason:
        raise ValueError('修改原因不能为空')
    note = str(note or '').strip()
    actor = str(actor or '').strip() or 'local_admin'
    timestamp = int(now)

    with conn:
        existing = conn.execute(
            'SELECT unit_diamonds FROM gift_name_prices WHERE gift_name = ?',
            (gift_name,),
        ).fetchone()
        old_price = _positive_int(existing['unit_diamonds']) if existing else None
        conn.execute('''
            INSERT INTO gift_name_prices (
                gift_name, unit_diamonds, note, updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(gift_name) DO UPDATE SET
                unit_diamonds = excluded.unit_diamonds,
                note = excluded.note,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
        ''', (gift_name, price, note, actor, timestamp))
        conn.execute('''
            INSERT INTO gift_price_history (
                gift_id, gift_name, old_unit_diamonds, new_unit_diamonds,
                note, reason, actor, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            f'name:{gift_name}', gift_name, old_price, price,
            note, reason, actor, timestamp,
        ))
        recalculated = reprice_recent_interactions_by_name(
            conn, gift_name, price, timestamp
        )
        # 该名字对应的 gift_id 登记为「多皮肤」，此后其未定价的皮肤走待补。
        _mark_skinned_gift_ids_for_name(conn, gift_name)
    return {
        'gift_name': gift_name,
        'unit_diamonds': price,
        'price_source': 'manual',
        'recalculated_rows': recalculated,
    }


def list_name_prices(conn):
    rows = conn.execute('''
        SELECT gift_name, unit_diamonds, note, updated_by, updated_at
        FROM gift_name_prices ORDER BY gift_name
    ''').fetchall()
    return [dict(row) for row in rows]


def list_pending_names(conn):
    """出现过、但按当前规则拿不到价的礼物「名字」。

    礼物库是按 gift_id 列的，一个 id 一行一个数字。但一个 gift_id 底下
    可能挂着多个皮肤名、各价不同——2026-08-22 线上实况：id=3729 底下有
    6 个名字 3 种价（钻石飞机 3600 / 私人飞机·金色飞机·七夕飞机 3000 /
    碧空飞机 没定价）。那一行显示的数字谁都不代表，而「碧空飞机」在按 id
    的列表里根本不出现——于是它永远补不上价，对应的礼物一直不计入账目。

    这个函数把这些名字捞出来，让它们在界面上可见、可定价。
    """
    rows = conn.execute("""
        SELECT gift_id,
               content AS gift_name,
               COUNT(*) AS occurrence_count,
               MIN(timestamp) AS first_seen,
               MAX(timestamp) AS last_seen
        FROM interaction_log
        WHERE type = 'gift'
          AND COALESCE(gift_id, '') <> ''
          AND COALESCE(content, '') <> ''
          AND content <> '?'
        GROUP BY gift_id, content
    """).fetchall()

    pending = []
    for row in rows:
        gift_id = str(row['gift_id'])
        gift_name = str(row['gift_name'])
        if resolve_gift_price(
                conn, gift_id, None, gift_name).unit_diamonds is not None:
            continue
        # 同 id 其他名字各是什么价：定价时的参照，省得再去别处翻。
        siblings = []
        others = conn.execute("""
            SELECT DISTINCT content FROM interaction_log
            WHERE type = 'gift' AND gift_id = ?
              AND COALESCE(content, '') <> '' AND content <> ?
            ORDER BY content
        """, (gift_id, gift_name)).fetchall()
        for other in others:
            other_name = str(other['content'])
            price = resolve_gift_price(
                conn, gift_id, None, other_name).unit_diamonds
            if price is not None:
                siblings.append(
                    {'gift_name': other_name, 'unit_diamonds': price})
        pending.append({
            'gift_id': gift_id,
            'gift_name': gift_name,
            'occurrence_count': int(row['occurrence_count'] or 0),
            'first_seen': int(row['first_seen'] or 0),
            'last_seen': int(row['last_seen'] or 0),
            'siblings': siblings,
        })
    # 最近出现的排最前：刚冒出来的新皮肤最该马上补价。
    pending.sort(key=lambda item: -item['last_seen'])
    return pending


def list_gifts(conn):
    rows = conn.execute('''
        SELECT * FROM gift_catalog
        ORDER BY price_pending DESC, last_seen DESC, gift_id ASC
    ''').fetchall()
    records = []
    for row in rows:
        item = dict(row)
        # 显示价必须就是结算用的价。以前这里自己算一套（只看 id 层的
        # manual/observed，不看名字价、不看多皮肤规则），线上 233 个礼物里
        # 有 3 个显示价 != 实际生效价——看账的人会被界面上的数字带偏。
        # 现在直接问 resolve_gift_price，一个函数一个答案。
        resolution = resolve_gift_price(
            conn, item.get('gift_id'), None, item.get('gift_name'))
        item['unit_diamonds'] = resolution.unit_diamonds
        item['price_source'] = resolution.source
        item['price_pending'] = (
            bool(item.get('price_pending')) or resolution.source == 'pending'
        )
        records.append(item)
    return records


def list_price_history(conn, gift_id=None):
    if gift_id is None:
        rows = conn.execute('''
            SELECT * FROM gift_price_history
            ORDER BY changed_at DESC, id DESC
        ''').fetchall()
    else:
        rows = conn.execute('''
            SELECT * FROM gift_price_history
            WHERE gift_id = ?
            ORDER BY changed_at DESC, id DESC
        ''', (str(gift_id),)).fetchall()
    return [dict(row) for row in rows]
