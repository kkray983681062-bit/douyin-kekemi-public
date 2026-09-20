"""礼物价格、连击数量和钻石统计的纯逻辑。"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class GiftPrice:
    gift_id: str
    name: str
    unit_diamonds: int | None
    combo: bool


@dataclass(frozen=True)
class GiftQuantity:
    display_count: int
    quantity_delta: int
    verified: bool
    event_key: str
    duplicate: bool = False


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_gift_catalog(payload):
    """把直播间礼物目录转换为 gift_id -> GiftPrice。"""
    root = payload.get('data', payload) if isinstance(payload, dict) else {}
    gifts = root.get('gifts', []) if isinstance(root, dict) else []
    catalog = {}
    for gift in gifts:
        if not isinstance(gift, dict):
            continue
        raw_id = gift.get('id')
        if raw_id in (None, ''):
            continue
        gift_id = str(raw_id)
        catalog[gift_id] = GiftPrice(
            gift_id=gift_id,
            name=str(gift.get('name') or ''),
            unit_diamonds=_positive_int(gift.get('diamond_count')),
            combo=bool(gift.get('combo')),
        )
    return catalog


def gift_value(unit_diamonds, quantity, quantity_verified):
    """只对价格和数量都可验证的礼物计算钻石合计。"""
    price = _positive_int(unit_diamonds)
    count = _positive_int(quantity)
    if not quantity_verified or price is None or count is None:
        return False, None
    return True, price * count


class GiftComboTracker:
    """将抖音连击累计值转换成本次增量，并对消息 ID 去重。"""

    def __init__(self, max_seen_messages=5000):
        self._combo_totals = {}
        self._seen_messages = OrderedDict()
        self._max_seen_messages = max(100, int(max_seen_messages))

    @staticmethod
    def _display_count(repeat_count, combo_count):
        values = []
        for value in (repeat_count, combo_count, 1):
            try:
                values.append(int(value or 0))
            except (TypeError, ValueError):
                values.append(0)
        return max(values)

    @staticmethod
    def _event_key(sender_key, gift_id, msg_id, group_id):
        if group_id:
            return f'gift:{sender_key}:{gift_id}:group:{int(group_id)}'
        if msg_id:
            return f'gift-msg:{int(msg_id)}'
        return ''

    def _remember_message(self, room_id, msg_id):
        if not msg_id:
            return False
        key = (str(room_id), int(msg_id))
        if key in self._seen_messages:
            self._seen_messages.move_to_end(key)
            return True
        self._seen_messages[key] = None
        while len(self._seen_messages) > self._max_seen_messages:
            self._seen_messages.popitem(last=False)
        return False

    def observe(self, room_id, sender_key, gift_id, msg_id, group_id,
                repeat_count, combo_count, repeat_end, combo_enabled):
        room_id = str(room_id or '')
        sender_key = str(sender_key or 'anonymous')
        gift_id = str(gift_id or '')
        group_id = int(group_id or 0)
        display_count = self._display_count(repeat_count, combo_count)
        event_key = self._event_key(sender_key, gift_id, msg_id, group_id)
        verified = bool(group_id) if combo_enabled else True

        if self._remember_message(room_id, msg_id):
            return GiftQuantity(
                display_count=display_count,
                quantity_delta=0,
                verified=verified,
                event_key=event_key,
                duplicate=True,
            )

        if combo_enabled:
            if not group_id:
                return GiftQuantity(
                    display_count=display_count,
                    quantity_delta=display_count,
                    verified=False,
                    event_key=event_key,
                )
            combo_key = (room_id, sender_key, gift_id, group_id)
            previous = int(self._combo_totals.get(combo_key, 0) or 0)
            quantity_delta = display_count - previous if display_count >= previous else display_count
            self._combo_totals[combo_key] = display_count
            if repeat_end:
                self._combo_totals.pop(combo_key, None)
            return GiftQuantity(
                display_count=display_count,
                quantity_delta=max(0, quantity_delta),
                verified=True,
                event_key=event_key,
            )

        return GiftQuantity(
            display_count=display_count,
            quantity_delta=display_count,
            verified=True,
            event_key=event_key,
        )


_KNOWN_VALUE_SQL = (
    'price_known = 1 AND quantity_verified = 1 '
    'AND total_diamonds IS NOT NULL'
)

_BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_day_start_timestamp(now):
    """给定 Unix 时间，返回其所在北京时间自然日的 00:00。"""
    current = datetime.fromtimestamp(int(now), tz=_BEIJING_TZ)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def query_spender_rank_since(conn, room_id, cutoff):
    """查询某厅从指定时点起的已核算送礼排名。"""
    rows = conn.execute(f'''
        SELECT
            CASE
                WHEN sec_uid <> '' THEN sec_uid
                ELSE 'display:' || display
            END AS sender_key,
            MAX(display) AS display,
            COUNT(*) AS gift_events,
            COALESCE(SUM(gift_quantity), 0) AS gift_quantity,
            COALESCE(SUM(CASE WHEN {_KNOWN_VALUE_SQL}
                THEN gift_quantity ELSE 0 END), 0) AS known_gift_quantity,
            COALESCE(SUM(CASE WHEN {_KNOWN_VALUE_SQL}
                THEN total_diamonds ELSE 0 END), 0) AS known_diamonds,
            COALESCE(SUM(CASE WHEN NOT ({_KNOWN_VALUE_SQL})
                THEN 1 ELSE 0 END), 0) AS unknown_gift_events,
            MAX(timestamp) AS last_gift_at
        FROM interaction_log
        WHERE room_id = ? AND type = 'gift' AND timestamp >= ?
        GROUP BY sender_key
        HAVING known_diamonds > 0
        ORDER BY known_diamonds DESC, last_gift_at DESC
        LIMIT 100
    ''', (str(room_id), int(cutoff))).fetchall()
    return [dict(row) for row in rows]


def query_spender_rank(conn, room_id, now, window_seconds=3 * 86400):
    """查询单个直播间最近 3 天、监听期间已验证的游客钻石榜。"""
    cutoff = int(now) - int(window_seconds)
    return query_spender_rank_since(conn, room_id, cutoff)


def query_host_gifts(conn, room_id, now, window_seconds=7 * 86400):
    """查询单个直播间最近 7 天的主播收礼明细与已验证钻石总值。"""
    cutoff = int(now) - int(window_seconds)
    summary_row = conn.execute(f'''
        SELECT
            COUNT(*) AS gift_events,
            COALESCE(SUM(gift_quantity), 0) AS gift_quantity,
            COALESCE(SUM(CASE WHEN {_KNOWN_VALUE_SQL}
                THEN gift_quantity ELSE 0 END), 0) AS known_gift_quantity,
            COALESCE(SUM(CASE WHEN {_KNOWN_VALUE_SQL}
                THEN total_diamonds ELSE 0 END), 0) AS known_diamonds,
            COALESCE(SUM(CASE WHEN NOT ({_KNOWN_VALUE_SQL})
                THEN 1 ELSE 0 END), 0) AS unknown_gift_events
        FROM interaction_log
        WHERE room_id = ? AND type = 'gift' AND timestamp >= ?
    ''', (str(room_id), cutoff)).fetchone()
    gift_rows = conn.execute('''
        SELECT
            id, room_id, sec_uid, display, content, gift_count, timestamp,
            gift_id, gift_quantity, unit_diamonds, total_diamonds,
            price_known, quantity_verified, recipient_key, recipient_name,
            event_key
        FROM interaction_log
        WHERE room_id = ? AND type = 'gift' AND timestamp >= ?
        ORDER BY timestamp DESC, id DESC
        LIMIT 500
    ''', (str(room_id), cutoff)).fetchall()
    return {
        'summary': dict(summary_row),
        'gifts': [dict(row) for row in gift_rows],
    }
