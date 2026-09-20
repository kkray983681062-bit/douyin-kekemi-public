import unittest

from static import Live_pb2
from online_audience import extract_linkmic_fan_tickets


def _uvarint(n):
    out = b''
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            break
    return out


def _tag(field, wire):
    return _uvarint((field << 3) | wire)


def _varint_field(field, value):
    return _tag(field, 0) + _uvarint(value)


def _len_field(field, data):
    return _tag(field, 2) + _uvarint(len(data)) + data


def _user_bytes(sec_uid, nickname, uid):
    u = Live_pb2.User()
    u.id = uid
    u.nickname = nickname
    u.sec_uid = sec_uid
    return u.SerializeToString()


def _audience_content(fan_ticket):
    # linkmic_audience_content: field1 = fan_ticket, field3 = app_id(2079 特征)
    return _varint_field(1, fan_ticket) + _varint_field(3, 2079)


def _linkmic_user(sec_uid, nickname, uid, fan_ticket):
    # LinkmicUser: field1 = user, field3 = linkmic_audience_content
    return (
        _len_field(1, _user_bytes(sec_uid, nickname, uid))
        + _len_field(3, _audience_content(fan_ticket))
    )


def _room_data_sync(seats):
    # 模拟 WebcastRoomDataSyncMessage 的嵌套：外层容器随意，解析靠递归定位
    inner = b''.join(
        _len_field(11, _linkmic_user(*seat)) for seat in seats
    )
    return _len_field(5, _len_field(1, inner))


class ExtractLinkmicFanTicketTests(unittest.TestCase):
    """从 WebcastRoomDataSyncMessage 提取每个麦位的 fan_ticket（本场麦位收礼值）。

    真实结构由登录态抓包逆向确认：LinkmicUser 里 user(User) + 更深层的
    linkmic_audience_content（app_id=2079 为特征，fan_ticket 为其 field 1）。
    """

    def test_extracts_sec_uid_to_fan_ticket(self):
        payload = _room_data_sync([
            ('MS4wLjA_uidA', '甲', 111, 4076),
            ('MS4wLjA_uidB', '乙', 222, 600),
        ])
        self.assertEqual(
            {'MS4wLjA_uidA': 4076, 'MS4wLjA_uidB': 600},
            extract_linkmic_fan_tickets(payload),
        )

    def test_keeps_zero_ticket_seats(self):
        # 上麦但 0 票的嘉宾也要在映射里（前端据此显示 0，而非漏掉）
        payload = _room_data_sync([('MS4wLjA_uidC', '丙', 333, 0)])
        self.assertEqual({'MS4wLjA_uidC': 0}, extract_linkmic_fan_tickets(payload))

    def test_ignores_users_without_audience_content(self):
        # 只有 user、没有 audience_content 的结构不算麦位（如聊天里的用户）
        payload = _len_field(9, _len_field(1, _user_bytes('MS4wLjA_x', '路人', 999)))
        self.assertEqual({}, extract_linkmic_fan_tickets(payload))

    def test_empty_payload(self):
        self.assertEqual({}, extract_linkmic_fan_tickets(b''))
        self.assertEqual({}, extract_linkmic_fan_tickets(None))



class MergeSnapshotFanTicketTests(unittest.TestCase):
    """麦上主持的收票改用 fan_ticket（本场麦位收礼值）覆盖。

    原来 received_tickets 来自连麦布局消息，只有房主带值、嘉宾恒为 0。
    """

    def test_fan_tickets_override_mic_received_tickets(self):
        from online_audience import merge_online_snapshot
        mic = [
            {'sec_uid': 'uidA', 'user_key': 'sec:uidA', 'nickname': '甲',
             'received_tickets': 0, 'mic_slot': 1, 'is_mic': True},
            {'sec_uid': 'uidB', 'user_key': 'sec:uidB', 'nickname': '乙',
             'received_tickets': 0, 'mic_slot': 2, 'is_mic': True},
        ]
        out = merge_online_snapshot([], mic, {}, {'uidA': 4076, 'uidB': 600})
        by = {r['sec_uid']: r['received_tickets'] for r in out}
        self.assertEqual(4076, by['uidA'])
        self.assertEqual(600, by['uidB'])

    def test_missing_fan_ticket_leaves_existing_value(self):
        from online_audience import merge_online_snapshot
        mic = [{'sec_uid': 'uidA', 'user_key': 'sec:uidA', 'nickname': '甲',
                'received_tickets': 235, 'mic_slot': 1, 'is_mic': True}]
        out = merge_online_snapshot([], mic, {}, {})
        self.assertEqual(235, out[0]['received_tickets'])

if __name__ == '__main__':
    unittest.main()


class RoomDataSyncAccumulateTests(unittest.TestCase):
    """RoomDataSyncMessage 可能增量推送，票数需累积而非替换。"""

    def _listener(self):
        import web_listener
        lis = web_listener.RoomListener.__new__(web_listener.RoomListener)
        lis.mic_fan_tickets = {}
        return lis

    def test_partial_updates_accumulate(self):
        lis = self._listener()
        lis._update_room_data_sync(_room_data_sync([('uidA', '甲', 1, 100)]))
        lis._update_room_data_sync(_room_data_sync([('uidB', '乙', 2, 200)]))
        self.assertEqual({'uidA': 100, 'uidB': 200}, lis.mic_fan_tickets)

    def test_later_value_wins_for_same_seat(self):
        lis = self._listener()
        lis._update_room_data_sync(_room_data_sync([('uidA', '甲', 1, 100)]))
        lis._update_room_data_sync(_room_data_sync([('uidA', '甲', 1, 350)]))
        self.assertEqual({'uidA': 350}, lis.mic_fan_tickets)

    def test_empty_message_does_not_wipe(self):
        lis = self._listener()
        lis._update_room_data_sync(_room_data_sync([('uidA', '甲', 1, 100)]))
        lis._update_room_data_sync(b'')
        self.assertEqual({'uidA': 100}, lis.mic_fan_tickets)
