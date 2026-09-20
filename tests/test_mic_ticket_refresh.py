import unittest
from unittest.mock import patch

import runtime_config
import web_listener
from static import Live_pb2


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


def _linkmic_payload(seats):
    """构造一条 RoomDataSyncMessage：LinkmicUser(user + audience_content)。"""
    blobs = b''
    for sec_uid, ticket in seats:
        user = Live_pb2.User()
        user.id = 1
        user.nickname = 'x'
        user.sec_uid = sec_uid
        audience = _varint_field(1, ticket) + _varint_field(3, 2079)
        blobs += _len_field(11, _len_field(1, user.SerializeToString())
                            + _len_field(3, audience))
    return _len_field(5, _len_field(1, blobs))


def _roster_payload(hosts):
    """构造一条 WebcastLinkmicPlaymodeMessage：麦上名单。

    hosts 为空时构造一条「不带名单」的同类消息——真实握手响应里就有这种，
    2026-08-21 实测一次响应含 2 条 LinkmicPlaymode，一条解出 0 人、一条 9 人。
    """
    entries = b''
    for nickname, sec_uid in hosts:
        user = Live_pb2.User(nickname=nickname, sec_uid=sec_uid, id=1000)
        entries += _len_field(2, _len_field(1, user.SerializeToString())
                              + _varint_field(2, 6666))
    return _len_field(3, _len_field(1, entries))


def _detail_frame(seats, hosts=None, roster_first=False):
    """模拟 get_webcast_detail 返回的 LiveResponse。

    真实响应同时含 RoomDataSyncMessage（票）和 LinkmicPlaymodeMessage（名单）。
    """
    response = Live_pb2.LiveResponse()

    def add_roster(hs):
        message = response.messagesList.add()
        message.method = 'WebcastLinkmicPlaymodeMessage'
        message.payload = _roster_payload(hs)

    if hosts is not None and roster_first:
        for group in hosts:
            add_roster(group)
    message = response.messagesList.add()
    message.method = 'WebcastRoomDataSyncMessage'
    message.payload = _linkmic_payload(seats)
    if hosts is not None and not roster_first:
        for group in hosts:
            add_roster(group)
    return response.SerializeToString()


class MicTicketIntervalConfigTests(unittest.TestCase):
    """刷新频率直接换算成抖音请求量，必须可配置且非法值有兜底。"""

    def test_default_when_unset(self):
        self.assertEqual(
            runtime_config.MIC_TICKET_REFRESH_DEFAULT,
            runtime_config.resolve_mic_ticket_interval({}),
        )

    def test_env_value_is_used(self):
        self.assertEqual(
            30, runtime_config.resolve_mic_ticket_interval(
                {'MIC_TICKET_REFRESH_INTERVAL': '30'}))

    def test_illegal_or_too_small_falls_back_to_default(self):
        for raw in ('', 'abc', '0', '-5'):
            self.assertEqual(
                runtime_config.MIC_TICKET_REFRESH_DEFAULT,
                runtime_config.resolve_mic_ticket_interval(
                    {'MIC_TICKET_REFRESH_INTERVAL': raw}),
                raw,
            )


class MicTicketRefreshTests(unittest.TestCase):
    """麦位票只在握手响应里下发、实时流不推，所以必须定时重拉。

    实测（2026-08-19，150s 窗口）：LIVE 推送 RoomDataSync = 0 条，
    握手响应 = 3 条；重拉后票值随礼物钻数精确增长（+1 / +599 / +599）。
    """

    def _listener(self):
        listener = web_listener.RoomListener.__new__(web_listener.RoomListener)
        listener.room_id = 'room-a'
        listener.mic_fan_tickets = {}
        listener.mic_users = []
        listener.nickname = ''
        listener.anchor_id = ''
        listener.sec_anchor_id = ''
        listener.running = True
        return listener

    def test_refresh_pulls_tickets_from_detail_response(self):
        listener = self._listener()
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=_detail_frame([('uidA', 722), ('uidB', 103)])):
            listener._refresh_mic_state()
        self.assertEqual({'uidA': 722, 'uidB': 103}, listener.mic_fan_tickets)

    def test_repeated_refresh_updates_changed_seat_and_keeps_others(self):
        listener = self._listener()
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=_detail_frame([('uidA', 722), ('uidB', 103)])):
            listener._refresh_mic_state()
        # 之后 uidB 收了 599 钻的礼物
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=_detail_frame([('uidB', 702)])):
            listener._refresh_mic_state()
        self.assertEqual({'uidA': 722, 'uidB': 702}, listener.mic_fan_tickets)

    def test_request_failure_keeps_last_known_tickets(self):
        listener = self._listener()
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=_detail_frame([('uidA', 722)])):
            listener._refresh_mic_state()
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          side_effect=RuntimeError('网络抖动')):
            listener._refresh_mic_state()  # 不能抛出
        self.assertEqual({'uidA': 722}, listener.mic_fan_tickets)

    def test_unparseable_response_does_not_wipe_tickets(self):
        listener = self._listener()
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=_detail_frame([('uidA', 722)])):
            listener._refresh_mic_state()
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=b'\xff\xff not-protobuf'):
            listener._refresh_mic_state()
        self.assertEqual({'uidA': 722}, listener.mic_fan_tickets)


class MicRosterRefreshTests(unittest.TestCase):
    """麦上名单和麦位票一样，只在握手响应里下发，实时流一条都不推。

    实测（2026-08-21，121s 窗口，RUYUE·心愿厅）：5 条 Linkmic 类消息全部
    到达于第 1.31~1.32 秒（连接瞬间的补发），之后 119 秒真正的实时推送里
    LinkmicPlaymode = 0 条。与 2026-08-19 对 RoomDataSync 的实测结论一致。

    当时给票加了定时重拉，没发现名单是同一个毛病：名单从连上那一刻起就
    再不更新，下麦的人一直挂着、新上麦的人永远进不来。线上实际后果是
    静默扫描对着空麦位数了几个小时（汀汀/米菲/凭 各 4 次告警，人早走了），
    而真正在麦上的鱼糕/kill/周游 完全没被监控。
    """

    def _listener(self):
        listener = web_listener.RoomListener.__new__(web_listener.RoomListener)
        listener.room_id = 'room-a'
        listener.mic_fan_tickets = {}
        listener.mic_users = []
        listener.nickname = ''
        listener.anchor_id = ''
        listener.sec_anchor_id = ''
        listener.running = True
        return listener

    def _refresh(self, listener, frame):
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=frame):
            listener._refresh_mic_state()

    def test_refresh_updates_roster_not_only_tickets(self):
        listener = self._listener()
        self._refresh(listener, _detail_frame(
            [('uidA', 722)], hosts=[[('主持甲', 'uidA'), ('主持乙', 'uidB')]]))
        self.assertEqual(['主持甲', '主持乙'],
                         [u['nickname'] for u in listener.mic_users])
        self.assertEqual({'uidA': 722}, listener.mic_fan_tickets)

    def test_departed_host_is_dropped_and_new_one_picked_up(self):
        """线上那个症状：下麦的不走、上麦的不来。"""
        listener = self._listener()
        self._refresh(listener, _detail_frame(
            [('uidA', 722)], hosts=[[('主持甲', 'uidA'), ('主持乙', 'uidB')]]))
        # 甲下麦、丙上麦
        self._refresh(listener, _detail_frame(
            [('uidB', 103)], hosts=[[('主持乙', 'uidB'), ('主持丙', 'uidC')]]))
        self.assertEqual(['主持乙', '主持丙'],
                         [u['nickname'] for u in listener.mic_users])

    def test_roster_free_message_does_not_wipe_known_roster(self):
        """握手响应里混着一条不带名单的同类消息，两种顺序都不能被它清空。"""
        for roster_first in (False, True):
            listener = self._listener()
            self._refresh(listener, _detail_frame(
                [('uidA', 722)], hosts=[[('主持甲', 'uidA')]]))
            self._refresh(listener, _detail_frame(
                [('uidA', 722)], hosts=[[], [('主持甲', 'uidA')]],
                roster_first=roster_first))
            self.assertEqual(['主持甲'],
                             [u['nickname'] for u in listener.mic_users],
                             'roster_first=%s' % roster_first)

    def test_request_failure_keeps_last_known_roster(self):
        listener = self._listener()
        self._refresh(listener, _detail_frame(
            [('uidA', 722)], hosts=[[('主持甲', 'uidA')]]))
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          side_effect=RuntimeError('网络抖动')):
            listener._refresh_mic_state()
        self.assertEqual(['主持甲'], [u['nickname'] for u in listener.mic_users])


if __name__ == '__main__':
    unittest.main()


def _detail_frame_no_roster(seats):
    """新厅的样子：只有票数，没有那条带名单的大消息。

    2026-08-23 实测，老厅 LinkmicPlaymode ×2（86字节→0人 + 13694字节→9人），
    新厅只有 ×1（86字节→0人），而 RoomDataSync 照常报出 9 个麦位票。
    """
    response = Live_pb2.LiveResponse()
    empty = response.messagesList.add()
    empty.method = 'WebcastLinkmicPlaymodeMessage'
    empty.payload = b''
    message = response.messagesList.add()
    message.method = 'WebcastRoomDataSyncMessage'
    message.payload = _linkmic_payload(seats)
    return response.SerializeToString()


class RosterFallbackTests(unittest.TestCase):
    """名单消息不下发时，用本轮麦位票反推名单。

    线上四个厅重开之后名单全空，麦上主持、静默扫描、在线名单一起失效。
    """

    def _listener(self):
        listener = web_listener.RoomListener.__new__(web_listener.RoomListener)
        listener.room_id = 'room-a'
        listener.mic_fan_tickets = {}
        listener.mic_users = []
        listener.nickname = ''
        listener.anchor_id = ''
        listener.sec_anchor_id = ''
        listener.running = True
        listener._mic_nickname_lookup = lambda secs: {}
        return listener

    def _refresh(self, listener, frame):
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=frame):
            listener._refresh_mic_state()

    def test_roster_is_derived_when_the_roster_message_is_missing(self):
        listener = self._listener()
        self._refresh(listener, _detail_frame_no_roster(
            [('uidA', 1889), ('uidB', 2499)]))
        self.assertEqual(['uidA', 'uidB'],
                         [u['sec_uid'] for u in listener.mic_users])
        self.assertTrue(all(u['derived'] for u in listener.mic_users))

    def test_real_roster_wins_over_the_derived_one(self):
        """有真名单就别用反推的——反推的没有名字。"""
        listener = self._listener()
        self._refresh(listener, _detail_frame(
            [('uidA', 1889)], hosts=[[('主持甲', 'uidA')]]))
        self.assertEqual(['主持甲'],
                         [u['nickname'] for u in listener.mic_users])
        self.assertFalse(listener.mic_users[0].get('derived'))

    def test_departed_hosts_do_not_come_back_from_the_ticket_cache(self):
        """mic_fan_tickets 是累积的，下麦的人留在里面。

        直接拿它反推会让下麦的人全部复活——必须只用本轮响应里的麦位。
        """
        listener = self._listener()
        self._refresh(listener, _detail_frame_no_roster(
            [('uidA', 100), ('uidB', 200)]))
        self.assertEqual({'uidA', 'uidB'},
                         {u['sec_uid'] for u in listener.mic_users})
        # uidA 下麦了：本轮响应里只剩 uidB
        self._refresh(listener, _detail_frame_no_roster([('uidB', 250)]))
        self.assertEqual(['uidB'], [u['sec_uid'] for u in listener.mic_users])
        # 累积的票数照旧留着（渲染时按 mic_users 过滤，无害）
        self.assertIn('uidA', listener.mic_fan_tickets)

    def test_names_are_filled_from_the_lookup(self):
        listener = self._listener()
        listener._mic_nickname_lookup = lambda secs: {'uidA': 'Ry.霜'}
        self._refresh(listener, _detail_frame_no_roster(
            [('uidA', 100), ('uidB', 200)]))
        self.assertEqual(['Ry.霜', ''],
                         [u['nickname'] for u in listener.mic_users])

    def test_no_seats_and_no_roster_keeps_the_last_known_roster(self):
        """两样都没有时不要清空——空结果多半是消息没带，不是麦上没人。"""
        listener = self._listener()
        self._refresh(listener, _detail_frame(
            [('uidA', 100)], hosts=[[('主持甲', 'uidA')]]))
        with patch.object(web_listener.DouyinAPI, 'get_webcast_detail',
                          return_value=Live_pb2.LiveResponse().SerializeToString()):
            listener._refresh_mic_state()
        self.assertEqual(['主持甲'], [u['nickname'] for u in listener.mic_users])
