import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


from gift_analytics import (
    GiftComboTracker,
    beijing_day_start_timestamp,
    gift_value,
    parse_gift_catalog,
    query_host_gifts,
    query_spender_rank,
    query_spender_rank_since,
)
from static import Live_pb2
import web_listener


class GiftDomainTests(unittest.TestCase):
    def test_gift_proto_exposes_ticket_and_embedded_price_fields(self):
        message = Live_pb2.GiftMessage()
        message.fanTicketCount = 99
        message.roomFanTicketCount = 12345
        message.groupCount = 5
        message.gift.diamondCount = 99
        message.gift.combo = True

        restored = Live_pb2.GiftMessage()
        restored.ParseFromString(message.SerializeToString())
        self.assertEqual(99, restored.fanTicketCount)
        self.assertEqual(12345, restored.roomFanTicketCount)
        self.assertEqual(5, restored.groupCount)
        self.assertEqual(99, restored.gift.diamondCount)
        self.assertTrue(restored.gift.combo)

    def test_gift_proto_exposes_combo_group_and_end_fields(self):
        message = Live_pb2.GiftMessage()
        message.groupId = 8001
        message.repeatEnd = 1

        restored = Live_pb2.GiftMessage()
        restored.ParseFromString(message.SerializeToString())
        self.assertEqual(8001, restored.groupId)
        self.assertEqual(1, restored.repeatEnd)

    def test_catalog_and_value_use_verified_diamond_count(self):
        catalog = parse_gift_catalog({
            'data': {
                'gifts': [
                    {
                        'id': 123,
                        'name': '一束花开',
                        'diamond_count': 99,
                        'combo': True,
                    },
                    {
                        'id': 8888,
                        'name': '红包',
                        'diamond_count': 0,
                        'combo': False,
                    },
                ],
            },
        })

        self.assertEqual(99, catalog['123'].unit_diamonds)
        self.assertTrue(catalog['123'].combo)
        self.assertIsNone(catalog['8888'].unit_diamonds)
        self.assertEqual((True, 495), gift_value(99, 5, True))

    def test_combo_updates_contribute_only_incremental_quantity(self):
        tracker = GiftComboTracker()
        deltas = []

        for count in (1, 2, 3, 4, 5):
            result = tracker.observe(
                'room-1', 'sender-1', '123', count, 8001,
                count, count, count == 5, True,
            )
            deltas.append(result.quantity_delta)

        self.assertEqual([1, 1, 1, 1, 1], deltas)
        self.assertEqual(5, result.display_count)
        self.assertTrue(result.verified)
        self.assertEqual('gift:sender-1:123:group:8001', result.event_key)

    def test_duplicate_message_id_has_zero_delta(self):
        tracker = GiftComboTracker()
        first = tracker.observe(
            'room-1', 'sender-1', '123', 91, 8001,
            1, 1, False, True,
        )
        duplicate = tracker.observe(
            'room-1', 'sender-1', '123', 91, 8001,
            1, 1, False, True,
        )

        self.assertEqual(1, first.quantity_delta)
        self.assertEqual(0, duplicate.quantity_delta)
        self.assertTrue(duplicate.duplicate)

    def test_combo_without_group_is_visible_but_not_value_verified(self):
        tracker = GiftComboTracker()
        quantity = tracker.observe(
            'room-1', 'sender-1', '123', 92, 0,
            5, 5, True, True,
        )

        self.assertEqual(5, quantity.display_count)
        self.assertFalse(quantity.verified)
        self.assertEqual((False, None), gift_value(99, 5, quantity.verified))

    def test_unknown_price_never_contributes_to_total(self):
        self.assertEqual((False, None), gift_value(None, 5, True))


class GiftStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.db_patch = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.db_patch.start()
        web_listener._init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tempdir.cleanup()

    def rows(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

    def test_same_combo_event_updates_one_final_gift_row(self):
        for quantity in (1, 2, 3, 4, 5):
            web_listener._save_interaction(
                'room-a', 'sender-a', '游客A', 'gift',
                content='一束花开', gift_count=quantity,
                gift_id='123', gift_quantity=quantity,
                unit_diamonds=99, total_diamonds=99 * quantity,
                price_known=True, quantity_verified=True,
                recipient_key='room:room-a', recipient_name='A厅',
                event_key='gift:sender-a:123:group:8001',
                timestamp=1_800_000_000 + quantity,
            )

        rows = self.rows('SELECT * FROM interaction_log')
        self.assertEqual(1, len(rows))
        self.assertEqual(5, rows[0]['gift_quantity'])
        self.assertEqual(5, rows[0]['gift_count'])
        self.assertEqual(99, rows[0]['unit_diamonds'])
        self.assertEqual(495, rows[0]['total_diamonds'])
        self.assertEqual(1, rows[0]['price_known'])
        self.assertEqual(1, rows[0]['quantity_verified'])

    def test_unknown_price_is_stored_without_diamond_total(self):
        web_listener._save_interaction(
            'room-a', 'sender-a', '游客A', 'gift',
            content='未知礼物', gift_count=2,
            gift_id='999', gift_quantity=2,
            unit_diamonds=None, total_diamonds=None,
            price_known=False, quantity_verified=True,
            event_key='gift-msg:99', timestamp=1_800_000_100,
        )

        row = self.rows('SELECT * FROM interaction_log')[0]
        self.assertEqual(0, row['price_known'])
        self.assertIsNone(row['unit_diamonds'])
        self.assertIsNone(row['total_diamonds'])

    def test_cleanup_removes_old_interactions_and_regular_profiles_only(self):
        now = 1_800_000_000
        old = now - 8 * 86400
        recent = now - 2 * 86400
        web_listener._save_mystery_record(
            'room-a', 'regular-old', '普通旧用户', '普通旧用户', {}, 'chat',
            timestamp=old, is_regular=1,
        )
        web_listener._save_mystery_record(
            'room-a', 'mystery-old', 'dou7101055', '示例用户甲', {}, 'chat',
            timestamp=old, is_regular=0,
        )
        web_listener._save_interaction(
            'room-a', 'regular-old', '普通旧用户', 'chat',
            content='旧消息', timestamp=old,
        )
        web_listener._save_interaction(
            'room-a', 'recent-user', '最近用户', 'chat',
            content='最近消息', timestamp=recent,
        )

        deleted = web_listener._cleanup_expired_data(now=now, force=True)

        profiles = self.rows(
            'SELECT sec_uid, is_regular FROM mystery_records ORDER BY sec_uid'
        )
        interactions = self.rows(
            'SELECT content FROM interaction_log ORDER BY timestamp'
        )
        self.assertEqual([{'sec_uid': 'mystery-old', 'is_regular': 0}], profiles)
        self.assertEqual([{'content': '最近消息'}], interactions)
        self.assertEqual(1, deleted['interactions'])
        self.assertEqual(1, deleted['regular_profiles'])

    def test_live_gift_handler_emits_and_persists_verified_diamond_value(self):
        listener = web_listener.RoomListener('room-a', 'A厅')
        catalog = parse_gift_catalog({'data': {'gifts': [{
            'id': 123,
            'name': '一束花开',
            'diamond_count': 99,
            'combo': True,
        }]}})
        message = Live_pb2.GiftMessage()
        message.giftId = '123'
        message.gift.id = 123
        message.gift.name = '一束花开'
        message.repeatCount = 5
        message.comboCount = 5
        message.groupId = 8001
        message.repeatEnd = 1
        message.fanTicketCount = 99
        message.roomFanTicketCount = 12345
        message.user.nickname = 'dou7101055'
        message.user.sec_uid = 'sender-a'
        message.user.mystery_man = 2

        handler = getattr(listener, '_handle_gift_payload', None)
        self.assertIsNotNone(handler, 'gift messages need a testable handler')
        with (
            patch.object(listener, '_get_gift_catalog', return_value=catalog),
            patch.object(
                web_listener,
                'is_real_mystery_user',
                return_value=(True, 'dou7101055', 'dou7101055', 2),
            ),
            patch.object(
                web_listener,
                'lookup_user',
                return_value={
                    'sec_uid': 'sender-a',
                    'nickname': '示例用户甲',
                    'unique_id': '7101055',
                },
            ),
        ):
            handler(message.SerializeToString(), msg_id=91)

        event = listener.events.get_nowait()
        self.assertEqual('mystery_gift', event['type'])
        self.assertEqual(5, event['data']['count'])
        self.assertEqual(99, event['data']['unit_diamonds'])
        self.assertEqual(495, event['data']['total_diamonds'])
        self.assertEqual(495, event['data'].get('ticket_count'))
        self.assertEqual('douyin_fan_ticket', event['data'].get('ticket_source'))
        self.assertTrue(event['data']['price_known'])
        rows = self.rows('SELECT * FROM interaction_log')
        self.assertEqual(1, len(rows))
        self.assertEqual(495, rows[0]['total_diamonds'])
        self.assertEqual(495, rows[0].get('ticket_count'))
        self.assertEqual('douyin_fan_ticket', rows[0].get('ticket_source'))
        self.assertEqual(12345, rows[0].get('room_ticket_total'))
        self.assertEqual('A厅', rows[0]['recipient_name'])

    def test_live_gift_uses_fan_ticket_unit_times_final_combo_quantity(self):
        listener = web_listener.RoomListener('room-a', 'A厅')
        catalog = parse_gift_catalog({'data': {'gifts': [{
            'id': 456,
            'name': '万象烟花',
            'diamond_count': 520,
            'combo': True,
        }]}})
        message = Live_pb2.GiftMessage()
        message.giftId = '456'
        message.gift.id = 456
        message.gift.name = '万象烟花'
        message.gift.diamondCount = 520
        message.fanTicketCount = 688
        message.roomFanTicketCount = 90000
        message.repeatCount = 4
        message.comboCount = 4
        message.groupId = 9001
        message.repeatEnd = 1
        message.user.nickname = '游客甲'
        message.user.sec_uid = 'sender-a'

        with (
            patch.object(listener, '_get_gift_catalog', return_value=catalog),
            patch.object(
                web_listener, 'is_real_mystery_user',
                return_value=(False, '游客甲', '游客甲', 0),
            ),
            patch.object(web_listener, 'lookup_user', return_value=None),
        ):
            listener._handle_gift_payload(message.SerializeToString(), msg_id=94)

        event = listener.events.get_nowait()['data']
        row = self.rows('SELECT * FROM interaction_log')[0]
        self.assertEqual(2752, event.get('ticket_count'))
        self.assertEqual('douyin_fan_ticket', event.get('ticket_source'))
        self.assertEqual(2752, row.get('ticket_count'))
        self.assertEqual(90000, row.get('room_ticket_total'))

    def test_live_gift_without_fan_ticket_falls_back_to_verified_price_total(self):
        listener = web_listener.RoomListener('room-a', 'A厅')
        catalog = parse_gift_catalog({'data': {'gifts': [{
            'id': 789,
            'name': '点亮粉丝团',
            'diamond_count': 99,
            'combo': False,
        }]}})
        message = Live_pb2.GiftMessage()
        message.giftId = '789'
        message.gift.id = 789
        message.gift.name = '点亮粉丝团'
        message.repeatCount = 2
        message.groupCount = 2
        message.groupId = 9002
        message.repeatEnd = 1
        message.user.nickname = '游客乙'
        message.user.sec_uid = 'sender-b'

        with (
            patch.object(listener, '_get_gift_catalog', return_value=catalog),
            patch.object(
                web_listener, 'is_real_mystery_user',
                return_value=(False, '游客乙', '游客乙', 0),
            ),
            patch.object(web_listener, 'lookup_user', return_value=None),
        ):
            listener._handle_gift_payload(message.SerializeToString(), msg_id=95)

        event = listener.events.get_nowait()['data']
        row = self.rows('SELECT * FROM interaction_log')[0]
        self.assertEqual(198, event.get('ticket_count'))
        self.assertEqual('gift_price', event.get('ticket_source'))
        self.assertEqual(198, row.get('ticket_count'))
        self.assertEqual('gift_price', row.get('ticket_source'))

    def test_duplicate_live_gift_message_is_ignored(self):
        listener = web_listener.RoomListener('room-a', 'A厅')
        catalog = parse_gift_catalog({'data': {'gifts': [{
            'id': 123,
            'name': '一束花开',
            'diamond_count': 99,
            'combo': False,
        }]}})
        message = Live_pb2.GiftMessage()
        message.giftId = '123'
        message.gift.name = '一束花开'
        message.repeatCount = 1
        message.user.nickname = '普通游客'
        message.user.sec_uid = 'sender-a'

        handler = getattr(listener, '_handle_gift_payload', None)
        self.assertIsNotNone(handler, 'gift messages need a testable handler')
        with (
            patch.object(listener, '_get_gift_catalog', return_value=catalog),
            patch.object(
                web_listener,
                'is_real_mystery_user',
                return_value=(False, '普通游客', '普通游客', 0),
            ),
            patch.object(web_listener, 'lookup_user', return_value=None),
        ):
            handler(message.SerializeToString(), msg_id=92)
            handler(message.SerializeToString(), msg_id=92)

        self.assertEqual(1, listener.events.qsize())
        self.assertEqual(1, len(self.rows('SELECT * FROM interaction_log')))

    def test_live_gift_uses_embedded_price_before_stale_catalog_price(self):
        listener = web_listener.RoomListener('room-a', 'A厅')
        stale_catalog = parse_gift_catalog({'data': {'gifts': [{
            'id': 123,
            'name': '动态礼物',
            'diamond_count': 366,
            'combo': False,
        }]}})
        message = Live_pb2.GiftMessage()
        message.giftId = '123'
        message.gift.id = 123
        message.gift.name = '动态礼物'
        message.gift.diamondCount = 99
        message.repeatCount = 2
        message.groupCount = 2
        message.groupId = 8002
        message.repeatEnd = 1
        message.user.nickname = '普通游客'
        message.user.sec_uid = 'sender-a'

        with (
            patch.object(listener, '_get_gift_catalog', return_value=stale_catalog),
            patch.object(
                web_listener, 'is_real_mystery_user',
                return_value=(False, '普通游客', '普通游客', 0),
            ),
            patch.object(web_listener, 'lookup_user', return_value=None),
        ):
            listener._handle_gift_payload(message.SerializeToString(), msg_id=93)

        event = listener.events.get_nowait()['data']
        row = self.rows('SELECT * FROM interaction_log')[0]
        self.assertEqual(99, event['unit_diamonds'])
        self.assertEqual(198, event['total_diamonds'])
        self.assertEqual('douyin', event['price_source'])
        self.assertEqual(198, row['total_diamonds'])

    def test_spender_rank_uses_three_days_and_host_summary_uses_seven_days(self):
        now = 1_800_000_000

        def save(room_id, sender, display, age_days, quantity, unit, event_key,
                 price_known=True, quantity_verified=True):
            total = unit * quantity if price_known and quantity_verified else None
            web_listener._save_interaction(
                room_id, sender, display, 'gift', content='测试礼物',
                gift_count=quantity, gift_id='123', gift_quantity=quantity,
                unit_diamonds=unit if price_known else None,
                total_diamonds=total,
                price_known=price_known,
                quantity_verified=quantity_verified,
                recipient_key=f'room:{room_id}', recipient_name=f'{room_id}主播',
                event_key=event_key,
                timestamp=now - age_days * 86400,
            )

        save('room-a', 'sender-a', '游客A', 2, 5, 99, 'a-2d')
        save('room-a', 'sender-a', '游客A', 4, 2, 10, 'a-4d')
        save('room-a', 'sender-b', '游客B', 6, 1, 1, 'b-6d')
        save('room-a', 'sender-a', '游客A', 2, 3, 0, 'a-unknown', price_known=False)
        save('room-b', 'sender-z', '游客Z', 2, 9, 999, 'other-room')

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rank = query_spender_rank(conn, 'room-a', now)
            host = query_host_gifts(conn, 'room-a', now)
        finally:
            conn.close()

        self.assertEqual(['sender-a'], [row['sender_key'] for row in rank])
        self.assertEqual(495, rank[0]['known_diamonds'])
        self.assertEqual(5, rank[0]['known_gift_quantity'])
        self.assertEqual(1, rank[0]['unknown_gift_events'])
        self.assertEqual(516, host['summary']['known_diamonds'])
        self.assertEqual(8, host['summary']['known_gift_quantity'])
        self.assertEqual(1, host['summary']['unknown_gift_events'])
        self.assertEqual(4, host['summary']['gift_events'])
        self.assertEqual(4, len(host['gifts']))
        self.assertNotIn('游客Z', {gift['display'] for gift in host['gifts']})

    def test_online_audience_rank_uses_beijing_today_not_rolling_three_days(self):
        now = 1_798_822_800  # 2027-01-02 01:00:00 +08:00
        self.assertEqual(1_798_819_200, beijing_day_start_timestamp(now))

        def save(sender, display, timestamp, total, event_key):
            web_listener._save_interaction(
                'room-a', sender, display, 'gift', content='测试礼物',
                gift_count=1, gift_id='today-rank', gift_quantity=1,
                unit_diamonds=total, total_diamonds=total,
                price_known=True, quantity_verified=True,
                event_key=event_key, timestamp=timestamp,
            )

        save('viewer-a', '游客甲', 1_798_819_140, 1000, 'gift-msg:yesterday-a')
        save('viewer-a', '游客甲', 1_798_819_260, 50, 'gift-msg:today-a')
        save('viewer-b', '游客乙', 1_798_819_300, 100, 'gift-msg:today-b')

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            records = query_spender_rank_since(
                conn, 'room-a', beijing_day_start_timestamp(now)
            )
        finally:
            conn.close()

        self.assertEqual(['游客乙', '游客甲'], [row['display'] for row in records])
        self.assertEqual([100, 50], [row['known_diamonds'] for row in records])

    def test_analytics_routes_report_room_scoped_windows(self):
        now = int(time.time())
        web_listener._save_interaction(
            'room-a', 'sender-a', '游客A', 'gift', content='一束花开',
            gift_count=5, gift_id='123', gift_quantity=5,
            unit_diamonds=99, total_diamonds=495,
            price_known=True, quantity_verified=True,
            recipient_key='room:room-a', recipient_name='A厅',
            event_key='route-gift', timestamp=now,
        )
        client = web_listener.app.test_client()

        rank_response = client.get('/api/spender_rank/room-a')
        host_response = client.get('/api/host_gifts/room-a')
        other_response = client.get('/api/spender_rank/room-b')

        self.assertEqual(200, rank_response.status_code)
        self.assertEqual(200, host_response.status_code)
        rank = rank_response.get_json()
        host = host_response.get_json()
        other = other_response.get_json()
        self.assertTrue(rank['success'])
        self.assertEqual('room-a', rank['room_id'])
        self.assertEqual(3, rank['window_days'])
        self.assertEqual('monitoring_period', rank['scope'])
        self.assertEqual(495, rank['records'][0]['known_diamonds'])
        self.assertEqual(7, host['window_days'])
        self.assertEqual(495, host['summary']['known_diamonds'])
        self.assertEqual([], other['records'])

    def test_feed_and_interaction_history_include_gift_value_but_only_seven_days(self):
        now = int(time.time())
        web_listener._save_interaction(
            'room-a', 'sender-a', '游客A', 'chat', content='八天前',
            timestamp=now - 8 * 86400,
        )
        web_listener._save_interaction(
            'room-a', 'sender-a', '游客A', 'gift', content='一束花开',
            gift_count=5, gift_id='123', gift_quantity=5,
            unit_diamonds=99, total_diamonds=495,
            price_known=True, quantity_verified=True,
            event_key='feed-gift', timestamp=now,
        )

        feed = web_listener._build_feed_events('room-a')
        response = web_listener.app.test_client().get(
            '/api/interactions/room-a/sender-a'
        ).get_json()

        self.assertEqual(1, len(feed))
        self.assertEqual('gift', feed[0]['type'])
        self.assertEqual(5, feed[0]['gift_quantity'])
        self.assertEqual(99, feed[0]['unit_diamonds'])
        self.assertEqual(495, feed[0]['total_diamonds'])
        self.assertTrue(feed[0]['price_known'])
        self.assertEqual(1, len(response['interactions']))
        self.assertEqual(495, response['interactions'][0]['total_diamonds'])

    def test_all_records_separates_gift_events_from_total_quantity(self):
        now = int(time.time())
        web_listener._save_mystery_record(
            'room-a', 'sender-a', '游客A', '游客A', {}, 'gift',
            timestamp=now, is_regular=1, room_nickname='A厅',
        )
        web_listener._save_interaction(
            'room-a', 'sender-a', '游客A', 'gift', content='一束花开',
            gift_count=5, gift_id='123', gift_quantity=5,
            unit_diamonds=99, total_diamonds=495,
            price_known=True, quantity_verified=True,
            event_key='all-records-gift', timestamp=now,
        )

        payload = web_listener.app.test_client().get(
            '/api/all_records/room-a'
        ).get_json()

        self.assertTrue(payload['success'])
        self.assertEqual(1, len(payload['records']))
        record = payload['records'][0]
        self.assertEqual(1, record['gift_event_count'])
        self.assertEqual(5, record['gift_quantity_total'])
        self.assertEqual(5, record['gift_count'])

    def test_all_records_uses_requested_room_not_users_last_room(self):
        now = int(time.time())
        web_listener._save_mystery_record(
            'room-a', 'sender-a', '游客A', '游客A', {}, 'chat',
            timestamp=now - 10, is_regular=1, room_nickname='A厅',
        )
        web_listener._save_mystery_record(
            'room-b', 'sender-a', '游客A', '游客A', {}, 'chat',
            timestamp=now, is_regular=1, room_nickname='B厅',
        )

        payload = web_listener.app.test_client().get(
            '/api/all_records/room-a'
        ).get_json()

        self.assertEqual(1, len(payload['records']))
        self.assertEqual('room-b', payload['records'][0]['last_room_id'])
        self.assertEqual('room-a', payload['records'][0]['room_id'])


if __name__ == '__main__':
    unittest.main()
