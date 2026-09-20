"""抖音直播间在线名单的标准化、排序和线程安全快照。"""

from __future__ import annotations

import copy
import re
import threading
import time

import requests

from builder.header import HeaderBuilder
from builder.params import Params
from static import Live_pb2


_ANONYMOUS_PLACEHOLDER_IDS = {'0', '111111', '?'}
_DOU_ALIAS_RE = re.compile(r'^dou\d{5,}$', re.IGNORECASE)


def _identity_value(value):
    """过滤抖音给真匿名用户填充的占位身份值。"""
    text = str(value or '').strip()
    return '' if text in _ANONYMOUS_PLACEHOLDER_IDS else text


def is_anonymous_mystery_record(record):
    """识别尚未反查出真实资料、但匿名形态已经明确的当前用户。"""
    if not isinstance(record, dict):
        return False
    try:
        mystery_man = int(
            record.get('mystery_man') or record.get('mysteryMan') or 0
        )
    except (TypeError, ValueError):
        mystery_man = 0
    if mystery_man >= 2:
        return True

    nickname = str(
        record.get('nickname') or record.get('name')
        or record.get('desensitized_nickname') or ''
    ).strip()
    has_anonymous_alias = bool(
        _DOU_ALIAS_RE.fullmatch(nickname)
        or (nickname.startswith('神秘人') and len(nickname) > 3)
    )
    if not has_anonymous_alias:
        return False

    # 在线榜会把真匿名用户的 sec_uid/id/display_id 都伪装成 111111；
    # 普通用户即使 mystery_man=1，仍会带真实身份字段，不能误判。
    return not any((
        _identity_value(record.get('sec_uid') or record.get('secUid')),
        _identity_value(
            record.get('id_str') or record.get('id') or record.get('user_id')
        ),
        _identity_value(record.get('display_id') or record.get('displayId')),
    ))


def _positive_int(value, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def audience_user_key(user):
    """为名单用户生成房间内稳定键，优先使用不可变身份字段。"""
    if not isinstance(user, dict):
        return ''
    sec_uid = _identity_value(user.get('sec_uid') or user.get('secUid'))
    if sec_uid:
        return f'sec:{sec_uid}'
    webcast_uid = str(
        user.get('webcast_uid') or user.get('webcastUid') or ''
    ).strip()
    if webcast_uid:
        return f'webcast:{webcast_uid}'
    user_id = _identity_value(
        user.get('id_str') or user.get('id') or user.get('user_id') or ''
    )
    if user_id:
        return f'id:{user_id}'
    display_id = _identity_value(
        user.get('display_id') or user.get('displayId') or ''
    )
    if display_id:
        return f'display:{display_id}'
    nickname = str(user.get('nickname') or user.get('name') or '').strip()
    return f'nickname:{nickname}' if nickname else ''


def _avatar_url(user):
    avatar = user.get('avatar_thumb') or user.get('avatarThumb') or {}
    if not isinstance(avatar, dict):
        return ''
    urls = avatar.get('url_list') or avatar.get('urlList') or []
    if isinstance(urls, list) and urls:
        return str(urls[0] or '')
    return str(avatar.get('url') or '')


def normalize_audience_response(payload):
    """把 `/webcast/ranklist/audience/` 返回值转换成稳定的用户列表。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if not isinstance(data, dict):
        data = payload
    ranks = data.get('ranks') or data.get('rank_list') or []
    if not isinstance(ranks, list):
        return []

    records = []
    seen = set()
    for position, rank_item in enumerate(ranks, start=1):
        if not isinstance(rank_item, dict):
            continue
        user = rank_item.get('user') or rank_item.get('user_info') or {}
        if not isinstance(user, dict):
            continue
        key = audience_user_key(user)
        if not key or key in seen:
            continue
        seen.add(key)
        rank = _positive_int(rank_item.get('rank'), position)
        records.append({
            'user_key': key,
            'user_id': _identity_value(
                user.get('id_str') or user.get('id') or user.get('user_id') or ''
            ),
            'sec_uid': _identity_value(
                user.get('sec_uid') or user.get('secUid')
            ),
            'webcast_uid': str(
                user.get('webcast_uid') or user.get('webcastUid') or ''
            ),
            'nickname': str(user.get('nickname') or user.get('name') or '?'),
            'display_id': _identity_value(
                user.get('display_id') or user.get('displayId') or ''
            ),
            'avatar_url': _avatar_url(user),
            'rank': rank,
            # 匿名厅里抖音会给普通观众和神秘人都下发 1；只保留原值供诊断，
            # 不在这里据此判定神秘身份。
            'mystery_man': int(
                user.get('mystery_man') or user.get('mysteryMan') or 0
            ),
        })
    return records


def _profile_extra(profile):
    extra = profile.get('extra') or {}
    if isinstance(extra, str):
        try:
            import json
            extra = json.loads(extra)
        except (TypeError, ValueError):
            extra = {}
    return extra if isinstance(extra, dict) else {}


def attach_mystery_profiles(records, profiles):
    """以当前在线名单为边界，附加已确认的神秘人永久资料。"""
    records = records if isinstance(records, list) else []
    profiles = profiles if isinstance(profiles, list) else []
    by_sec_uid = {}
    by_unique_id = {}
    by_real_name = {}
    for raw in profiles:
        if not isinstance(raw, dict):
            continue
        profile = dict(raw)
        extra = _profile_extra(profile)
        sec_uid = str(profile.get('sec_uid') or '').strip()
        unique_id = str(extra.get('unique_id') or '').strip()
        real_name = str(profile.get('real_name') or '').strip()
        normalized = {
            'sec_uid': sec_uid,
            'display': str(profile.get('display') or ''),
            'real_name': real_name,
            'unique_id': unique_id,
            'follower_count': int(extra.get('follower_count') or 0),
            'aweme_count': int(extra.get('aweme_count') or 0),
            'signature': str(extra.get('signature') or ''),
        }
        if sec_uid:
            by_sec_uid[sec_uid] = normalized
        if unique_id:
            by_unique_id[unique_id] = normalized
        if real_name:
            by_real_name.setdefault(real_name, []).append(normalized)

    enriched = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = dict(raw)
        sec_uid = str(record.get('sec_uid') or '').strip()
        display_id = str(record.get('display_id') or '').strip()
        nickname = str(record.get('nickname') or '').strip()
        profile = by_sec_uid.get(sec_uid) if sec_uid else None
        if profile is None and display_id:
            profile = by_unique_id.get(display_id)
        if profile is None and nickname:
            same_name = by_real_name.get(nickname) or []
            if len(same_name) == 1:
                profile = same_name[0]
        record['is_mystery'] = (
            profile is not None or is_anonymous_mystery_record(record)
        )
        record['mystery_resolved'] = profile is not None
        record['mystery_profile'] = copy.deepcopy(profile) if profile else None
        enriched.append(record)
    return enriched


def attach_matched_identities(records, matcher):
    """给在线名单附加 UID 精确匹配到的真实身份，既有字段一律不动。

    `matcher(record)` 只允许按 UID 完全一致返回身份；它失败时该条按未匹配处理，
    名单本身绝不因此丢人或报错。
    """
    enriched = []
    failures = 0
    first_error = None
    for raw in records if isinstance(records, list) else []:
        if not isinstance(raw, dict):
            continue
        record = dict(raw)
        try:
            matched = matcher(record)
        except Exception as exc:
            matched = None
            failures += 1
            first_error = first_error or exc
        record['matched_identity'] = matched or None
        enriched.append(record)
    if failures:
        print(
            f'[身份库] 在线名单 {failures} 条身份匹配失败，按身份待解析显示: '
            f'{first_error}',
            flush=True,
        )
    return enriched


def _record_key(record):
    key = str(record.get('user_key') or '').strip()
    return key or audience_user_key(record)


def _spend_for(record, spend_by_user):
    keys = [
        _record_key(record),
        str(record.get('sec_uid') or ''),
        str(record.get('webcast_uid') or ''),
        str(record.get('user_id') or ''),
        str(record.get('display_id') or ''),
        str(record.get('nickname') or ''),
        f"display:{record.get('display_id')}" if record.get('display_id') else '',
        f"display:{record.get('nickname')}" if record.get('nickname') else '',
    ]
    for key in keys:
        if key and key in spend_by_user:
            return max(0, _positive_int(spend_by_user.get(key), 0))
    return 0


def merge_online_snapshot(roster, mic_users=None, spend_by_user=None, fan_tickets=None):
    """主持置顶；其余在线观众按本厅已核算礼物价值排序。"""
    roster = roster if isinstance(roster, list) else []
    mic_users = mic_users if isinstance(mic_users, list) else []
    spend_by_user = spend_by_user if isinstance(spend_by_user, dict) else {}

    merged = {}
    order = []
    for position, raw in enumerate(roster, start=1):
        if not isinstance(raw, dict):
            continue
        record = dict(raw)
        key = _record_key(record)
        if not key or key in merged:
            continue
        record['user_key'] = key
        record['rank'] = _positive_int(record.get('rank'), position)
        record['is_mic'] = False
        record['mic_slot'] = 0
        merged[key] = record
        order.append(key)

    for position, raw in enumerate(mic_users, start=1):
        if not isinstance(raw, dict):
            continue
        mic = dict(raw)
        key = _record_key(mic)
        if not key:
            continue
        if key in merged:
            record = merged[key]
            for field, value in mic.items():
                if value not in (None, ''):
                    record[field] = value
        else:
            record = mic
            record['user_key'] = key
            record['rank'] = len(roster) + position
            merged[key] = record
            order.append(key)
        record['is_mic'] = True
        record['mic_slot'] = _positive_int(mic.get('mic_slot'), position)
        # 麦位票数（fan_ticket，本场收礼值）覆盖：连麦布局消息只给房主，
        # 嘉宾靠 RoomDataSyncMessage 的 fan_ticket 才有值。
        if fan_tickets:
            sec_uid = str(record.get('sec_uid') or '')
            if sec_uid and sec_uid in fan_tickets:
                record['received_tickets'] = max(0, int(fan_tickets[sec_uid] or 0))

    records = []
    for key in order:
        record = merged[key]
        record['known_diamonds'] = _spend_for(record, spend_by_user)
        records.append(record)

    records.sort(key=lambda record: (
        0 if record.get('is_mic') else 1,
        _positive_int(record.get('mic_slot'), 9999) if record.get('is_mic') else 0,
        0 if record.get('is_mic') else -int(record.get('known_diamonds') or 0),
        _positive_int(record.get('rank'), 999999),
        str(record.get('nickname') or ''),
    ))
    return records


class OnlineAudienceSnapshot:
    """失败时保留最后一次成功名单的线程安全内存快照。"""

    def __init__(self, room_id):
        self._lock = threading.RLock()
        self._state = {
            'room_id': str(room_id),
            'records': [],
            'updated_at': 0,
            'checked_at': 0,
            'stale': True,
            'error': '',
        }

    def update_success(self, records, updated_at):
        timestamp = int(updated_at)
        with self._lock:
            self._state.update({
                'records': copy.deepcopy(records if isinstance(records, list) else []),
                'updated_at': timestamp,
                'checked_at': timestamp,
                'stale': False,
                'error': '',
            })

    def update_error(self, error, checked_at):
        with self._lock:
            self._state.update({
                'checked_at': int(checked_at),
                'stale': True,
                'error': str(error or '在线名单刷新失败'),
            })

    def read(self):
        with self._lock:
            return copy.deepcopy(self._state)


def _read_varint(payload, offset):
    value = 0
    shift = 0
    while offset < len(payload) and shift < 70:
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError('invalid protobuf varint')


def _wire_fields(payload):
    fields = []
    offset = 0
    while offset < len(payload):
        key, offset = _read_varint(payload, offset)
        field_number = key >> 3
        wire_type = key & 7
        if field_number < 1:
            raise ValueError('invalid protobuf field')
        if wire_type == 0:
            value, offset = _read_varint(payload, offset)
        elif wire_type == 1:
            if offset + 8 > len(payload):
                raise ValueError('truncated fixed64')
            value = payload[offset:offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(payload, offset)
            if length < 0 or offset + length > len(payload):
                raise ValueError('truncated bytes')
            value = payload[offset:offset + length]
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(payload):
                raise ValueError('truncated fixed32')
            value = payload[offset:offset + 4]
            offset += 4
        else:
            raise ValueError(f'unsupported protobuf wire type: {wire_type}')
        fields.append((field_number, wire_type, value))
    return fields


def _linkmic_user_record(user, position, received_tickets=0):
    sec_uid = str(getattr(user, 'sec_uid', '') or '')
    webcast_uid = str(getattr(user, 'webcast_uid', '') or '')
    user_id = str(getattr(user, 'id', 0) or '')
    display_id = str(getattr(user, 'display_id', '') or '')
    raw = {
        'sec_uid': sec_uid,
        'webcast_uid': webcast_uid,
        'id_str': user_id,
        'display_id': display_id,
        'nickname': str(getattr(user, 'nickname', '') or '?'),
    }
    return {
        'user_key': audience_user_key(raw),
        'user_id': user_id,
        'sec_uid': sec_uid,
        'webcast_uid': webcast_uid,
        'display_id': display_id,
        'nickname': raw['nickname'],
        'mic_slot': position,
        'received_tickets': max(0, int(received_tickets or 0)),
    }


def roster_from_fan_tickets(fan_tickets, names=None):
    """麦位票只有 sec_uid 和票数，没有名字——用它把麦上名单还原出来。

    2026-08-23 线上：四个厅下播重开换 room_id 之后，握手响应里那条带名单的
    LinkmicPlaymodeMessage 一条都不发（老厅是 13694 字节 / 9 人，新厅只剩
    一条 86 字节的空壳），麦上主持、静默扫描、在线名单全线失效。但同一份
    响应里 RoomDataSyncMessage 照常报出 9 个麦位的票数，按 sec_uid 索引。

    抖音给票数的先后本身就是麦位顺序，直接照抄，不要按票数重排。
    名字由调用方补（先查本地历史，查不到再问抖音用户主页）；查不到就留空，
    不要填 '?'——下游要能分辨「没查到名字」和「他就叫问号」。
    """
    names = names or {}
    users = []
    for sec_uid, ticket in (fan_tickets or {}).items():
        sec_uid = str(sec_uid or '')
        if not sec_uid:
            continue
        users.append({
            'user_key': 'sec:%s' % sec_uid,
            'user_id': '',
            'sec_uid': sec_uid,
            'webcast_uid': '',
            'display_id': '',
            'nickname': str(names.get(sec_uid) or ''),
            'mic_slot': len(users) + 1,
            'received_tickets': max(0, int(ticket or 0)),
            # 标明这是反推的，不是抖音直接给的名单。排查「名单对不对」时
            # 必须分得清哪些是一手数据。
            'derived': True,
        })
    return users


def _linkmic_user_sec_uid(blob):
    try:
        user = Live_pb2.User()
        user.ParseFromString(blob)
        sec_uid = str(getattr(user, 'sec_uid', '') or '')
        if sec_uid and getattr(user, 'nickname', '') and getattr(user, 'id', 0):
            return sec_uid
    except Exception:
        pass
    return None


def _audience_fan_ticket(blob):
    """若 blob 是 linkmic_audience_content（app_id=2079 @field3），返回 fan_ticket（field1）。"""
    try:
        fields = _wire_fields(blob)
    except (TypeError, ValueError):
        return None
    if not any(num == 3 and wire == 0 and val == 2079 for num, wire, val in fields):
        return None
    for num, wire, val in fields:
        if num == 1 and wire == 0:
            return int(val)
    return None


def extract_linkmic_fan_tickets(payload):
    """从 WebcastRoomDataSyncMessage 提取每个麦位的 fan_ticket（本场麦位收礼值）。

    抖音网页版登录态下才推送、且才显示的「麦位票数」。结构经抓包逆向确认：
    每个 LinkmicUser 里有 user(User) 与更深层的 linkmic_audience_content
    （app_id=2079 为特征字段，fan_ticket 为其 field 1）。
    这里不依赖固定字段路径，递归定位：进入含 user 的作用域后，
    在其子层遇到 audience_content 时用该 user 的 sec_uid 配对。
    返回 {sec_uid: fan_ticket}。
    """
    payload = bytes(payload or b'')
    result = {}

    def walk(blob, current_sec_uid, depth=0):
        if depth > 16 or not blob:
            return
        try:
            fields = _wire_fields(blob)
        except (TypeError, ValueError):
            return
        for num, wire, value in fields:
            if wire == 2 and isinstance(value, bytes):
                sec_uid = _linkmic_user_sec_uid(value)
                if sec_uid:
                    current_sec_uid = sec_uid
        ticket = _audience_fan_ticket(blob)
        if ticket is not None and current_sec_uid:
            result[current_sec_uid] = ticket
        for num, wire, value in fields:
            if wire == 2 and isinstance(value, bytes):
                walk(value, current_sec_uid, depth + 1)

    walk(payload, None)
    return result


def extract_linkmic_users(payload):
    """从尚未纳入精简 proto 的连麦消息中只读提取用户列表。"""
    payload = bytes(payload or b'')
    users = []
    seen = set()

    def visit(blob, depth=0):
        if depth > 8 or not blob:
            return
        try:
            fields = _wire_fields(blob)
        except (TypeError, ValueError):
            return

        user_blobs = [value for number, wire, value in fields
                      if number == 1 and wire == 2]
        has_linkmic_marker = any(number == 2 and wire == 0 for number, wire, _ in fields)
        ticket_values = [value for number, wire, value in fields
                         if number == 3 and wire == 0]
        received_tickets = int(ticket_values[0]) if ticket_values else 0
        if user_blobs and has_linkmic_marker:
            for user_blob in user_blobs:
                try:
                    user = Live_pb2.User()
                    user.ParseFromString(user_blob)
                except Exception:
                    continue
                nickname = str(getattr(user, 'nickname', '') or '')
                sec_uid = str(getattr(user, 'sec_uid', '') or '')
                webcast_uid = str(getattr(user, 'webcast_uid', '') or '')
                user_id = int(getattr(user, 'id', 0) or 0)
                if not nickname or not (sec_uid or webcast_uid or user_id):
                    continue
                record = _linkmic_user_record(
                    user, len(users) + 1, received_tickets,
                )
                key = record['user_key']
                if key and key not in seen:
                    seen.add(key)
                    users.append(record)

        for _, wire, value in fields:
            if wire == 2:
                visit(value, depth + 1)

    visit(payload)
    return users


def fetch_online_audience(
        auth, room_id, anchor_id, sec_anchor_id, timeout=15):
    """调用抖音当前在线名单接口；调用方负责缓存与失败保留。"""
    params = Params()
    for key, value in (
        ('aid', '6383'),
        ('app_name', 'douyin_web'),
        ('live_id', '1'),
        ('device_platform', 'web'),
        ('language', 'zh-CN'),
        ('enter_from', 'web_live'),
        ('cookie_enabled', 'true'),
        ('screen_width', '2560'),
        ('screen_height', '1600'),
        ('browser_language', 'zh-CN'),
        ('browser_platform', 'Win32'),
        ('browser_name', 'Chrome'),
        ('browser_version', '138.0.0.0'),
        ('webcast_sdk_version', '1.0.15'),
        ('room_id', str(room_id)),
        ('anchor_id', str(anchor_id)),
        ('sec_anchor_id', str(sec_anchor_id)),
        ('ignoreToast', 'true'),
        ('rank_type', '30'),
        ('update_scene', 'rank_message'),
        ('msToken', str(getattr(auth, 'msToken', '') or '')),
    ):
        params.add_param(key, value)
    params.with_a_bogus()
    response = requests.get(
        'https://live.douyin.com/webcast/ranklist/audience/',
        headers={
            'User-Agent': HeaderBuilder.ua,
            'Referer': f'https://live.douyin.com/{room_id}',
        },
        params=params.get(),
        cookies=getattr(auth, 'cookie', {}),
        verify=True,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError('在线名单返回格式无效')
    status_code = int(payload.get('status_code', 0) or 0)
    if status_code != 0:
        raise RuntimeError(f'在线名单接口状态码 {status_code}')
    return payload


class OnlineAudiencePoller:
    """每个房间唯一的后台轮询器；网页只读取它维护的共享快照。"""

    def __init__(self, room_id, snapshot, fetcher, anchor_provider,
                 interval=10, clock=None):
        self.room_id = str(room_id)
        self.snapshot = snapshot
        self.fetcher = fetcher
        self.anchor_provider = anchor_provider
        self.interval = max(0.01, float(interval))
        self.clock = clock or time.time
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def refresh_once(self):
        checked_at = int(self.clock())
        try:
            anchor = self.anchor_provider() or {}
            anchor_id = str(anchor.get('anchor_id') or '')
            sec_anchor_id = str(
                anchor.get('sec_anchor_id') or anchor.get('sec_uid') or ''
            )
            if not anchor_id or not sec_anchor_id:
                raise RuntimeError('等待直播间主播身份')
            payload = self.fetcher(self.room_id, anchor_id, sec_anchor_id)
            records = normalize_audience_response(payload)
            self.snapshot.update_success(records, checked_at)
            return True
        except Exception as exc:
            self.snapshot.update_error(str(exc), checked_at)
            return False

    def _run(self):
        while not self._stop_event.is_set():
            self.refresh_once()
            if self._stop_event.wait(self.interval):
                break

    def start(self):
        if self.is_running:
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)
