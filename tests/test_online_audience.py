import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


from online_audience import (
    OnlineAudiencePoller,
    OnlineAudienceSnapshot,
    attach_matched_identities,
    attach_mystery_profiles,
    extract_linkmic_users,
    fetch_online_audience,
    merge_online_snapshot,
    normalize_audience_response,
)
from static import Live_pb2
import web_listener
from runtime_config import resolve_online_sync_interval


def _varint(value):
    encoded = bytearray()
    value = int(value)
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def _bytes_field(number, value):
    value = bytes(value)
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _int_field(number, value):
    return _varint(number << 3) + _varint(value)


class OnlineAudienceDomainTests(unittest.TestCase):
    def test_current_mystery_is_an_identity_intersection_not_mystery_man_one(self):
        roster = [
            {
                'user_key': 'sec:ordinary', 'sec_uid': 'ordinary',
                'nickname': '普通观众', 'display_id': 'ordinary-id',
                'mystery_man': 1,
            },
            {
                'user_key': 'sec:mystery-a', 'sec_uid': 'mystery-a',
                'nickname': 'demo_004', 'display_id': 'real-id',
                'mystery_man': 1,
            },
        ]
        profiles = [{
            'sec_uid': 'mystery-a', 'display': '神秘人123456',
            'real_name': 'demo_004',
            'extra': {
                'unique_id': 'real-id', 'follower_count': 321,
                'aweme_count': 12,
            },
        }]

        records = attach_mystery_profiles(roster, profiles)

        self.assertFalse(records[0]['is_mystery'])
        self.assertTrue(records[1]['is_mystery'])
        self.assertEqual('神秘人123456', records[1]['mystery_profile']['display'])
        self.assertEqual(321, records[1]['mystery_profile']['follower_count'])

    def test_anonymous_aliases_are_current_mysteries_without_resolved_profiles(self):
        roster = [
            {
                'user_key': 'sec:ordinary', 'sec_uid': 'ordinary',
                'nickname': '普通观众', 'display_id': 'ordinary-id',
                'mystery_man': 1,
            },
            {
                'user_key': 'webcast:dou-user', 'sec_uid': '',
                'webcast_uid': 'dou-user', 'nickname': 'dou7101055',
                'display_id': '', 'mystery_man': 1,
            },
            {
                'user_key': 'webcast:masked-user', 'sec_uid': '',
                'webcast_uid': 'masked-user', 'nickname': '神秘人X823',
                'display_id': '', 'mystery_man': 1,
            },
            {
                'user_key': 'webcast:tier-user', 'sec_uid': '',
                'webcast_uid': 'tier-user', 'nickname': '神秘人二阶',
                'display_id': '', 'mystery_man': 1,
            },
            {
                'user_key': 'webcast:deep-user', 'sec_uid': '',
                'webcast_uid': 'deep-user', 'nickname': '匿名访客',
                'display_id': '', 'mystery_man': 2,
            },
        ]

        records = attach_mystery_profiles(roster, [])

        self.assertFalse(records[0]['is_mystery'])
        self.assertEqual(
            [True, True, True, True],
            [record['is_mystery'] for record in records[1:]],
        )
        self.assertTrue(all(record['mystery_profile'] is None
                            for record in records[1:]))

    def test_matched_identity_is_attached_without_touching_existing_fields(self):
        roster = [{
            'user_key': 'webcast:anon', 'sec_uid': '', 'webcast_uid': 'anon',
            'nickname': 'dou8328160', 'display_id': '', 'mystery_man': 2,
            'rank': 3, 'known_diamonds': 520, 'is_mystery': True,
            'mystery_resolved': False, 'mystery_profile': None,
        }]
        identity = {'identity_id': 7, 'real_name': '示例用户甲'}

        records = attach_matched_identities(roster, lambda record: identity)

        self.assertEqual(identity, records[0]['matched_identity'])
        self.assertEqual('dou8328160', records[0]['nickname'])
        self.assertEqual(3, records[0]['rank'])
        self.assertEqual(520, records[0]['known_diamonds'])
        self.assertIsNone(records[0]['mystery_profile'])
        self.assertNotIn('matched_identity', roster[0])

    def test_unmatched_roster_entries_stay_unresolved(self):
        roster = [{'user_key': 'webcast:anon', 'nickname': '神秘人339175'}]

        records = attach_matched_identities(roster, lambda record: None)

        self.assertIsNone(records[0]['matched_identity'])

    def test_matcher_failure_never_drops_the_roster(self):
        roster = [
            {'user_key': 'webcast:a', 'nickname': 'dou8328160'},
            {'user_key': 'webcast:b', 'nickname': '神秘人二阶'},
        ]

        def boom(record):
            raise RuntimeError('身份库炸了')

        records = attach_matched_identities(roster, boom)

        self.assertEqual(2, len(records))
        self.assertEqual(
            [None, None], [record['matched_identity'] for record in records]
        )

    def test_placeholder_identity_does_not_merge_distinct_anonymous_users(self):
        payload = {
            'status_code': 0,
            'data': {
                'ranks': [
                    {
                        'rank': 1,
                        'user': {
                            'id_str': '111111', 'sec_uid': '111111',
                            'webcast_uid': 'anonymous-a',
                            'nickname': 'dou7101055', 'display_id': '111111',
                            'mystery_man': 1,
                        },
                    },
                    {
                        'rank': 2,
                        'user': {
                            'id_str': '111111', 'sec_uid': '111111',
                            'webcast_uid': 'anonymous-b',
                            'nickname': '神秘人一阶', 'display_id': '111111',
                            'mystery_man': 1,
                        },
                    },
                ],
            },
        }

        records = normalize_audience_response(payload)

        self.assertEqual(2, len(records))
        self.assertEqual(
            ['webcast:anonymous-a', 'webcast:anonymous-b'],
            [record['user_key'] for record in records],
        )
        self.assertEqual(['', ''], [record['sec_uid'] for record in records])
        self.assertEqual(['', ''], [record['display_id'] for record in records])

    def test_normalizes_real_rank_shape_and_deduplicates_user(self):
        payload = {
            'status_code': 0,
            'data': {
                'ranks': [
                    {
                        'rank': 1,
                        'score': 1,
                        'user': {
                            'id_str': '1001',
                            'sec_uid': 'sec-a',
                            'nickname': 'demo_005',
                            'display_id': 'alpha',
                            'avatar_thumb': {'url_list': ['https://img/a.jpg']},
                        },
                    },
                    {
                        'rank': 2,
                        'score': 1,
                        'user': {
                            'id_str': '1002',
                            'sec_uid': 'sec-b',
                            'nickname': 'demo_006',
                            'display_id': 'beta',
                        },
                    },
                    {
                        'rank': 3,
                        'score': 999999,
                        'user': {
                            'id_str': '1001',
                            'sec_uid': 'sec-a',
                            'nickname': 'demo_007',
                            'display_id': 'alpha-duplicate',
                        },
                    },
                ],
            },
        }

        records = normalize_audience_response(payload)

        self.assertEqual(2, len(records))
        self.assertEqual('sec:sec-a', records[0]['user_key'])
        self.assertEqual('demo_005', records[0]['nickname'])
        self.assertEqual('alpha', records[0]['display_id'])
        self.assertEqual('https://img/a.jpg', records[0]['avatar_url'])
        self.assertEqual(1, records[0]['rank'])
        self.assertEqual('sec:sec-b', records[1]['user_key'])

    def test_mic_users_are_pinned_then_viewers_sort_by_local_room_spend(self):
        roster = [
            {'user_key': 'sec:viewer-a', 'sec_uid': 'viewer-a', 'nickname': '游客甲', 'rank': 1},
            {'user_key': 'sec:host-b', 'sec_uid': 'host-b', 'nickname': '主持乙', 'rank': 2},
            {'user_key': 'sec:viewer-c', 'sec_uid': 'viewer-c', 'nickname': '游客丙', 'rank': 3},
        ]
        mic_users = [
            {'user_key': 'sec:host-a', 'sec_uid': 'host-a', 'nickname': '主持甲', 'mic_slot': 1},
            {'user_key': 'sec:host-b', 'sec_uid': 'host-b', 'nickname': '主持乙', 'mic_slot': 2},
        ]
        spend = {
            'sec:viewer-a': 50,
            'sec:viewer-c': 900,
            'sec:host-b': 200,
        }

        records = merge_online_snapshot(roster, mic_users, spend)

        self.assertEqual(
            ['主持甲', '主持乙', '游客丙', '游客甲'],
            [record['nickname'] for record in records],
        )
        self.assertEqual([True, True, False, False], [record['is_mic'] for record in records])
        self.assertEqual(900, records[2]['known_diamonds'])
        self.assertEqual(50, records[3]['known_diamonds'])

    def test_refresh_error_keeps_last_successful_roster_and_marks_stale(self):
        snapshot = OnlineAudienceSnapshot('room-a')
        snapshot.update_success(
            [{'user_key': 'sec:a', 'sec_uid': 'a', 'nickname': '仍在厅里', 'rank': 1}],
            updated_at=1_800_000_000,
        )

        snapshot.update_error('temporary timeout', checked_at=1_800_000_010)
        state = snapshot.read()

        self.assertEqual('room-a', state['room_id'])
        self.assertEqual(['仍在厅里'], [row['nickname'] for row in state['records']])
        self.assertEqual(1_800_000_000, state['updated_at'])
        self.assertEqual(1_800_000_010, state['checked_at'])
        self.assertTrue(state['stale'])
        self.assertEqual('temporary timeout', state['error'])

    def test_linkmic_payload_extracts_users_in_mic_order(self):
        first = Live_pb2.User(nickname='主持甲', sec_uid='host-a', id=1001)
        second = Live_pb2.User(nickname='主持乙', sec_uid='host-b', id=1002)
        entry_a = (
            _bytes_field(1, first.SerializeToString())
            + _int_field(2, 6666)
            + _int_field(3, 16401)
        )
        entry_b = (
            _bytes_field(1, second.SerializeToString())
            + _int_field(2, 6666)
            + _int_field(3, 9676)
        )
        payload = _bytes_field(
            3,
            _bytes_field(1, _bytes_field(2, entry_a) + _bytes_field(2, entry_b)),
        )

        users = extract_linkmic_users(payload)

        self.assertEqual(['主持甲', '主持乙'], [user['nickname'] for user in users])
        self.assertEqual(['sec:host-a', 'sec:host-b'], [user['user_key'] for user in users])
        self.assertEqual([1, 2], [user['mic_slot'] for user in users])
        self.assertEqual([16401, 9676], [user['received_tickets'] for user in users])

    def test_rank_request_uses_room_anchor_and_authenticated_web_params(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'status_code': 0, 'data': {'ranks': []}}
        auth = SimpleNamespace(cookie={'ttwid': 'cookie'}, msToken='token-1')

        with patch('online_audience.requests.get', return_value=response) as get:
            payload = fetch_online_audience(
                auth, 'room-a', 'anchor-a', 'sec-anchor-a', timeout=7,
            )

        self.assertEqual(0, payload['status_code'])
        _, kwargs = get.call_args
        self.assertEqual('room-a', kwargs['params']['room_id'])
        self.assertEqual('anchor-a', kwargs['params']['anchor_id'])
        self.assertEqual('sec-anchor-a', kwargs['params']['sec_anchor_id'])
        self.assertEqual('30', kwargs['params']['rank_type'])
        self.assertEqual('token-1', kwargs['params']['msToken'])
        self.assertEqual(7, kwargs['timeout'])

    def test_poller_shares_snapshot_and_stop_ends_background_loop(self):
        first_refresh = threading.Event()
        calls = []

        def fetcher(room_id, anchor_id, sec_anchor_id):
            calls.append((room_id, anchor_id, sec_anchor_id))
            first_refresh.set()
            return {'data': {'ranks': [{
                'rank': 1,
                'user': {'sec_uid': 'sec-a', 'nickname': 'demo_008'},
            }]}}

        snapshot = OnlineAudienceSnapshot('room-a')
        poller = OnlineAudiencePoller(
            room_id='room-a',
            snapshot=snapshot,
            fetcher=fetcher,
            anchor_provider=lambda: {
                'anchor_id': 'anchor-a',
                'sec_anchor_id': 'sec-anchor-a',
            },
            interval=0.01,
        )
        poller.start()
        self.assertTrue(first_refresh.wait(1))
        poller.stop()
        poller.join(1)
        calls_after_stop = len(calls)
        time.sleep(0.03)

        self.assertFalse(poller.is_running)
        self.assertEqual(calls_after_stop, len(calls))
        self.assertEqual('demo_008', snapshot.read()['records'][0]['nickname'])


class OnlineAudienceRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.db_patch = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.db_patch.start()
        web_listener._init_db()
        web_listener.listeners.clear()

    def tearDown(self):
        web_listener.listeners.clear()
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_online_route_is_room_specific_and_pins_mic_host(self):
        listener = web_listener.RoomListener('room-a', 'A厅')
        listener.running = True
        listener.online_snapshot.update_success([
            {'user_key': 'sec:viewer-a', 'sec_uid': 'viewer-a',
             'nickname': '游客甲', 'rank': 1},
        ], updated_at=1_800_000_000)
        listener.mic_users = [
            {'user_key': 'sec:host-a', 'sec_uid': 'host-a',
             'nickname': '主持甲', 'mic_slot': 1,
             'received_tickets': 16401},
        ]
        web_listener.listeners['room-a'] = listener
        web_listener._save_interaction(
            'room-a', 'viewer-a', '游客甲', 'gift',
            content='测试礼物', gift_id='gift-1', gift_quantity=2,
            unit_diamonds=50, total_diamonds=100,
            price_known=True, quantity_verified=True,
            event_key='gift-msg:online-test', timestamp=int(time.time()),
        )

        client = web_listener.app.test_client()
        response = client.get('/api/online/room-a')
        missing = client.get('/api/online/room-b')

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertEqual('room-a', data['room_id'])
        # 跟随配置而非写死数字，调整同步间隔时不必再改这里
        self.assertEqual(resolve_online_sync_interval({}), data['poll_interval'])
        self.assertEqual(['主持甲', '游客甲'], [r['nickname'] for r in data['records']])
        self.assertTrue(data['records'][0]['is_mic'])
        self.assertEqual(16401, data['records'][0]['received_tickets'])
        self.assertEqual(16401, data['records'][0]['known_tickets'])
        self.assertEqual(100, data['records'][1]['known_diamonds'])
        self.assertEqual(100, data['records'][1]['known_tickets'])
        self.assertEqual(404, missing.status_code)

    def test_online_route_marks_only_online_profiles_as_current_mystery(self):
        listener = web_listener.RoomListener('room-a', 'A厅')
        listener.running = True
        listener.online_snapshot.update_success([
            {'user_key': 'sec:current-mystery', 'sec_uid': 'current-mystery',
             'nickname': 'demo_009', 'display_id': 'real-a', 'rank': 1,
             'mystery_man': 1},
            {'user_key': 'sec:ordinary', 'sec_uid': 'ordinary',
             'nickname': '普通乙', 'display_id': 'ordinary-b', 'rank': 2,
             'mystery_man': 1},
        ], updated_at=1_800_000_000)
        web_listener.listeners['room-a'] = listener
        web_listener._save_mystery_record(
            'room-a', 'current-mystery', '神秘人123456', 'demo_009',
            {'unique_id': 'real-a', 'follower_count': 10, 'aweme_count': 2},
            'chat', is_regular=0,
        )
        web_listener._save_mystery_record(
            'room-a', 'not-online', '神秘人654321', '已离厅',
            {'unique_id': 'left-room'}, 'chat', is_regular=0,
        )

        data = web_listener.app.test_client().get('/api/online/room-a').get_json()
        current = [row for row in data['records'] if row['is_mystery']]

        self.assertEqual(['demo_009'], [row['nickname'] for row in current])
        self.assertNotIn('已离厅', [row['nickname'] for row in data['records']])
        self.assertFalse(next(row for row in data['records']
                              if row['nickname'] == '普通乙')['is_mystery'])


if __name__ == '__main__':
    unittest.main()


class RosterFromFanTicketsTests(unittest.TestCase):
    """名单消息不下发时，用麦位票把麦上名单还原出来。

    2026-08-23 线上事故：四个厅下播重开换了 room_id 之后，握手响应里那条
    带名单的 LinkmicPlaymodeMessage（老厅 13694 字节 / 9 人）**一条都不发**，
    只剩一条 86 字节的空壳。结果麦上主持、静默扫描、在线名单全线失效。

    但同一份响应里 RoomDataSyncMessage 仍然报出 9 个麦位的票数，
    按 sec_uid 索引——服务端知道谁在麦上，只是没给名字。
    """

    def test_builds_roster_records_in_seat_order(self):
        from online_audience import roster_from_fan_tickets
        users = roster_from_fan_tickets({'sec-a': 1889, 'sec-b': 2499,
                                         'sec-c': 0})
        self.assertEqual(['sec-a', 'sec-b', 'sec-c'],
                         [u['sec_uid'] for u in users])
        # 按票数排会打乱麦位顺序；抖音给的先后本身就是麦位顺序，照抄。
        self.assertEqual([1, 2, 3], [u['mic_slot'] for u in users])
        self.assertEqual([1889, 2499, 0],
                         [u['received_tickets'] for u in users])

    def test_record_shape_matches_the_real_roster(self):
        """反推的记录要能无缝顶替真名单，字段少一个下游就炸。"""
        from online_audience import roster_from_fan_tickets
        user = roster_from_fan_tickets({'sec-a': 10})[0]
        for key in ('user_key', 'user_id', 'sec_uid', 'webcast_uid',
                    'display_id', 'nickname', 'mic_slot', 'received_tickets'):
            self.assertIn(key, user, key)
        self.assertEqual('sec:sec-a', user['user_key'])

    def test_names_are_filled_in_when_known(self):
        from online_audience import roster_from_fan_tickets
        users = roster_from_fan_tickets(
            {'sec-a': 1, 'sec-b': 2}, names={'sec-a': 'Ry.霜'})
        self.assertEqual('Ry.霜', users[0]['nickname'])
        # 查不到名字时留空，不要填 '?'：下游要能分辨「没查到」和「他就叫?」
        self.assertEqual('', users[1]['nickname'])

    def test_marks_records_as_derived(self):
        """标出来是必须的：这是反推的，不是抖音直接给的。

        没有这个标记，以后排查「名单对不对」时分不清哪些是一手数据。
        """
        from online_audience import roster_from_fan_tickets
        self.assertTrue(roster_from_fan_tickets({'sec-a': 1})[0]['derived'])

    def test_empty_tickets_give_empty_roster(self):
        from online_audience import roster_from_fan_tickets
        self.assertEqual([], roster_from_fan_tickets({}))
        self.assertEqual([], roster_from_fan_tickets(None))

    def test_blank_sec_uid_is_skipped(self):
        from online_audience import roster_from_fan_tickets
        self.assertEqual([], roster_from_fan_tickets({'': 100}))
