"""进场、聊天、送礼三条实时路径的 UID 精确匹配与身份入库测试。"""

import sqlite3
import sys
import tempfile
import unittest
import unittest.mock  # 先于 web_listener 的 subprocess.Popen 包装加载 asyncio
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import identity_registry as ir
import web_listener


REAL_SEC_UID = 'MS4wLjABAAAA-kekeray'
ANON_WEBCAST_UID = '1000000000000000019'
NUMERIC_UID = 108625123456


def _user(message, *, nickname, display, sec_uid='', webcast_uid='',
          user_id=0, consume_level=0, mystery_man=0, display_id=''):
    message.user.nickname = nickname
    message.user.desensitized_nickname = display
    message.user.sec_uid = sec_uid
    message.user.webcast_uid = webcast_uid
    message.user.id = user_id
    message.user.consume_diamond_level = consume_level
    message.user.mystery_man = mystery_man
    message.user.display_id = display_id
    return message


def gift_payload(**kwargs):
    message = web_listener.Live_pb2.GiftMessage()
    _user(message, **kwargs)
    message.gift.name = '玫瑰'
    message.giftId = '123'
    message.repeatCount = 1
    message.repeatEnd = 1
    message.fanTicketCount = 10
    return message.SerializeToString()


def chat_payload(content='大家好', **kwargs):
    message = web_listener.Live_pb2.ChatMessage()
    _user(message, **kwargs)
    message.content = content
    return message.SerializeToString()


def member_payload(**kwargs):
    message = web_listener.Live_pb2.MemberMessage()
    _user(message, **kwargs)
    return message.SerializeToString()


HIGH_LEVEL_SENDER = {
    'nickname': '示例用户甲',
    'display': '克*Ray',
    'sec_uid': REAL_SEC_UID,
    'webcast_uid': ANON_WEBCAST_UID,
    'user_id': NUMERIC_UID,
    'display_id': 'kekeray',
}

ANONYMOUS_FORM = {
    'nickname': 'dou8328160',
    'display': 'dou8328160',
    'sec_uid': '',
    'webcast_uid': ANON_WEBCAST_UID,
    'user_id': 111111,
    'display_id': '111111',
    'mystery_man': 2,
}


class IdentityMatchingLiveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.db_patch = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        web_listener._init_db()
        ir.invalidate_cache()

        self.catalog_patch = patch.object(
            web_listener.RoomListener, '_get_gift_catalog', return_value={}
        )
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.lookup_patch = patch.object(
            web_listener, 'lookup_user', return_value={}
        )
        self.lookup_user = self.lookup_patch.start()
        self.addCleanup(self.lookup_patch.stop)

        self.listener = web_listener.RoomListener('room-a', 'A厅')

    # ---------- 工具 ----------

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def identity_rows(self):
        return self.query('SELECT real_name, source FROM identity_profiles')

    def drain(self):
        events = []
        while not self.listener.events.empty():
            events.append(self.listener.events.get_nowait())
        return events

    def last_event(self):
        events = self.drain()
        self.assertTrue(events, '事件队列为空')
        return events[-1]['data']

    def send_high_level_gift(self, consume_level=41, listener=None, **overrides):
        listener = listener or self.listener
        fields = dict(HIGH_LEVEL_SENDER, consume_level=consume_level)
        fields.update(overrides)
        listener._handle_gift_payload(gift_payload(**fields), msg_id=1)
        return self.drain()

    # ---------- 建表与迁移 ----------

    def test_init_db_creates_permanent_identity_tables(self):
        names = {
            row[0]
            for row in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertLessEqual(
            {
                'identity_profiles',
                'identity_identifiers',
                'identity_alias_history',
            },
            names,
        )

    def test_init_db_imports_resolved_mystery_records_idempotently(self):
        web_listener._save_mystery_record(
            'room-a', REAL_SEC_UID, '神秘人339175', '示例用户甲',
            {'unique_id': 'kekeray'}, 'gift', timestamp=1786982399,
        )

        web_listener._init_db()
        web_listener._init_db()

        self.assertEqual([('示例用户甲', 'legacy_import')], self.identity_rows())

    # ---------- 送礼路径与 41 级门槛 ----------

    def test_level_40_gift_sender_stays_out_of_the_registry(self):
        self.send_high_level_gift(consume_level=40)

        self.assertEqual([], self.identity_rows())

    def test_level_41_gift_sender_enters_the_registry(self):
        events = self.send_high_level_gift(consume_level=41)

        self.assertEqual([('示例用户甲', 'high_level_gift')], self.identity_rows())
        self.assertEqual(
            '示例用户甲', events[-1]['data']['matched_identity']['real_name']
        )

    def test_gift_registers_every_valid_uid_and_rejects_placeholders(self):
        self.send_high_level_gift(consume_level=41)

        self.assertEqual(
            {
                ('sec_uid', REAL_SEC_UID),
                ('webcast_uid', ANON_WEBCAST_UID),
                ('numeric_uid', str(NUMERIC_UID)),
            },
            {
                tuple(row)
                for row in self.query(
                    'SELECT identifier_type, identifier_value '
                    'FROM identity_identifiers'
                )
            },
        )

    def test_resolved_mystery_gift_enters_without_the_level_threshold(self):
        self.lookup_user.return_value = {
            'nickname': '示例用户甲', 'unique_id': 'kekeray', 'follower_count': 1234,
        }

        self.listener._handle_gift_payload(gift_payload(
            nickname='dou8328160', display='dou8328160', sec_uid=REAL_SEC_UID,
            webcast_uid=ANON_WEBCAST_UID, consume_level=3, mystery_man=2,
        ), msg_id=2)

        self.assertEqual(
            [('示例用户甲', 'resolved_mystery')], self.identity_rows()
        )

    def test_chat_and_enter_register_a_mystery_once_it_is_resolved(self):
        self.lookup_user.return_value = {
            'nickname': '示例用户甲', 'unique_id': 'kekeray', 'follower_count': 1234,
        }
        resolved = {
            'nickname': 'dou8328160', 'display': 'dou8328160',
            'sec_uid': REAL_SEC_UID, 'consume_level': 3, 'mystery_man': 2,
        }

        self.listener._handle_chat_payload(chat_payload(**resolved))
        self.assertEqual([('示例用户甲', 'resolved_mystery')], self.identity_rows())

        web_listener.RoomListener('room-b', 'B厅')._handle_member_payload(
            member_payload(**resolved)
        )
        self.assertEqual([('示例用户甲', 'resolved_mystery')], self.identity_rows())

    def test_chat_and_enter_never_register_a_high_level_ordinary_user(self):
        ordinary = dict(HIGH_LEVEL_SENDER, consume_level=60)

        self.listener._handle_chat_payload(chat_payload(**ordinary))
        self.listener._handle_member_payload(member_payload(**ordinary))

        self.assertEqual([], self.identity_rows())

    # ---------- 四条形态一致匹配 ----------

    def test_anonymous_forms_match_the_same_identity_on_every_live_path(self):
        self.send_high_level_gift(consume_level=41)

        self.listener._handle_member_payload(member_payload(**ANONYMOUS_FORM))
        entered = self.last_event()
        self.listener._handle_chat_payload(chat_payload(**ANONYMOUS_FORM))
        chatted = self.last_event()
        self.listener._handle_gift_payload(
            gift_payload(consume_level=3, **ANONYMOUS_FORM), msg_id=3
        )
        gifted = self.last_event()

        for label, data in (
            ('enter', entered), ('chat', chatted), ('gift', gifted)
        ):
            with self.subTest(path=label):
                self.assertEqual('dou8328160', data['display'])
                self.assertEqual(
                    '示例用户甲', data['matched_identity']['real_name']
                )
                self.assertEqual('kekeray', data['matched_identity']['douyin_id'])

    def test_uid_match_works_from_another_room_and_another_day(self):
        self.send_high_level_gift(consume_level=41)
        other_room = web_listener.RoomListener('room-b', 'B厅')

        other_room._handle_chat_payload(chat_payload(
            nickname='神秘人339175', display='神秘人339175',
            webcast_uid=ANON_WEBCAST_UID, user_id=111111, mystery_man=2,
        ))

        data = other_room.events.get_nowait()['data']
        self.assertEqual('神秘人339175', data['display'])
        self.assertEqual('示例用户甲', data['matched_identity']['real_name'])

    def test_alias_history_keeps_every_day_and_room_separately(self):
        self.send_high_level_gift(consume_level=41)
        self.listener._handle_chat_payload(chat_payload(**ANONYMOUS_FORM))
        self.listener._handle_chat_payload(chat_payload(
            nickname='神秘人二阶', display='神秘人二阶',
            webcast_uid=ANON_WEBCAST_UID, mystery_man=2,
        ))

        self.assertEqual(
            {('dou8328160', 'dou'), ('神秘人二阶', 'mystery_tier')},
            {
                tuple(row)
                for row in self.query(
                    'SELECT alias, alias_type FROM identity_alias_history'
                )
            },
        )

    def test_unresolved_stranger_stays_unmatched_and_uncreated(self):
        self.send_high_level_gift(consume_level=41)

        self.listener._handle_chat_payload(chat_payload(
            nickname='dou9999999', display='dou9999999',
            webcast_uid='1000000000000000021', user_id=999888777,
            consume_level=41, display_id='kekeray', mystery_man=2,
        ))

        data = self.listener.events.get_nowait()['data']
        self.assertEqual('dou9999999', data['display'])
        self.assertIsNone(data['matched_identity'])
        self.assertEqual([('示例用户甲', 'high_level_gift')], self.identity_rows())

    def test_same_nickname_and_douyin_id_never_reuse_another_uid_identity(self):
        events = self.send_high_level_gift(consume_level=41)
        known_id = events[-1]['data']['matched_identity']['identity_id']

        # 昵称、抖音号、消费等级全一样，只有 UID 不同：绝不能认成同一个人。
        self.listener._handle_chat_payload(chat_payload(
            nickname='示例用户甲', display='dou9999999',
            webcast_uid='1000000000000000021', user_id=999888777,
            consume_level=41, display_id='kekeray', mystery_man=2,
        ))

        data = self.listener.events.get_nowait()['data']
        self.assertEqual('dou9999999', data['display'])
        self.assertNotEqual(known_id, data['matched_identity']['identity_id'])
        self.assertEqual(
            {(known_id, 'sec_uid', REAL_SEC_UID),
             (known_id, 'webcast_uid', ANON_WEBCAST_UID),
             (known_id, 'numeric_uid', str(NUMERIC_UID))},
            {
                tuple(row)
                for row in self.query(
                    'SELECT identity_id, identifier_type, identifier_value '
                    'FROM identity_identifiers WHERE identity_id = ?',
                    (known_id,),
                )
            },
        )

    def test_conflicting_uids_never_return_a_guessed_identity(self):
        self.send_high_level_gift(consume_level=41)
        self.listener._handle_gift_payload(gift_payload(
            nickname='demo_001', display='另*人', sec_uid='MS4wLjABAAAA-other',
            user_id=777666555, consume_level=45, display_id='otherid',
        ), msg_id=4)
        self.drain()

        self.listener._handle_chat_payload(chat_payload(
            nickname='dou8328160', display='dou8328160',
            webcast_uid=ANON_WEBCAST_UID, user_id=777666555, mystery_man=2,
        ))

        data = self.listener.events.get_nowait()['data']
        self.assertIsNone(data['matched_identity'])
        self.assertEqual(
            2,
            len(self.query('SELECT id FROM identity_profiles')),
        )

    # ---------- 原始数据不被改写 ----------

    def test_original_interaction_rows_are_never_rewritten_by_matching(self):
        self.send_high_level_gift(consume_level=41)
        before = self.query('SELECT * FROM interaction_log ORDER BY id')
        self.assertEqual(1, len(before))

        # 同一个人以后的匿名形态匹配上了，也不能回头改写已落库的礼物和票数。
        self.listener._handle_gift_payload(
            gift_payload(consume_level=3, **ANONYMOUS_FORM), msg_id=5
        )
        self.listener._handle_chat_payload(chat_payload(**ANONYMOUS_FORM))

        after = self.query('SELECT * FROM interaction_log ORDER BY id')
        self.assertEqual(before, after[:1])
        self.assertEqual(
            ['克*Ray', 'dou8328160', 'dou8328160'],
            [row[3] for row in self.query(
                'SELECT id, room_id, sec_uid, display FROM interaction_log '
                'ORDER BY id'
            )],
        )

    def test_matching_does_not_touch_legacy_mystery_records(self):
        self.lookup_user.return_value = {
            'nickname': '示例用户甲', 'unique_id': 'kekeray',
        }
        self.listener._handle_gift_payload(gift_payload(
            nickname='dou8328160', display='dou8328160', sec_uid=REAL_SEC_UID,
            consume_level=3, mystery_man=2,
        ), msg_id=6)
        before = self.query('SELECT * FROM mystery_records')

        self.listener._handle_chat_payload(chat_payload(**ANONYMOUS_FORM))

        self.assertEqual(before, self.query('SELECT * FROM mystery_records'))

    # ---------- 错误边界 ----------

    def test_identity_failure_never_blocks_the_live_event(self):
        # 先建好身份，确保这条聊天本来是能匹配上的，失败降级才有意义。
        self.send_high_level_gift(consume_level=41)
        self.query('SELECT 1')

        with patch.object(
            web_listener.identity_registry, 'match_user',
            side_effect=RuntimeError('身份库炸了'),
        ):
            self.listener._handle_chat_payload(chat_payload(**ANONYMOUS_FORM))

        data = self.listener.events.get_nowait()['data']
        self.assertEqual('dou8328160', data['display'])
        self.assertIsNone(data['matched_identity'])
        self.assertEqual(
            ('dou8328160', 'chat', '大家好'),
            tuple(self.query(
                "SELECT display, type, content FROM interaction_log "
                "WHERE type = 'chat'"
            )[0]),
        )

    def test_identity_write_failure_never_blocks_the_gift(self):
        with patch.object(
            web_listener.identity_registry, 'register_gift_sender',
            side_effect=RuntimeError('身份库炸了'),
        ):
            self.send_high_level_gift(consume_level=41)

        self.assertEqual(
            1, len(self.query('SELECT id FROM interaction_log'))
        )

    def test_identity_tables_survive_the_seven_day_cleanup(self):
        self.send_high_level_gift(consume_level=41)

        web_listener._cleanup_expired_data(now=2000000000, force=True)

        self.assertEqual([('示例用户甲', 'high_level_gift')], self.identity_rows())
        self.assertEqual(
            3, len(self.query('SELECT id FROM identity_identifiers'))
        )
        self.assertEqual(
            0, len(self.query('SELECT id FROM interaction_log'))
        )

    # ---------- 当前在线名单 ----------

    def online_records(self):
        self.listener.running = True
        self.listener.online_snapshot.update_success([{
            'user_key': f'webcast:{ANON_WEBCAST_UID}',
            'sec_uid': '',
            'webcast_uid': ANON_WEBCAST_UID,
            'user_id': '111111',
            'display_id': '111111',
            'nickname': 'dou8328160',
            'mystery_man': 2,
            'rank': 1,
        }], 1786982399)
        with patch.dict(
            web_listener.listeners, {'room-a': self.listener}, clear=True
        ):
            response = web_listener.app.test_client().get('/api/online/room-a')
        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        return payload['records']

    def test_online_roster_matches_the_same_identity_as_the_live_paths(self):
        self.send_high_level_gift(consume_level=41)

        record = self.online_records()[0]

        self.assertEqual('dou8328160', record['nickname'])
        self.assertEqual('示例用户甲', record['matched_identity']['real_name'])
        self.assertEqual('kekeray', record['matched_identity']['douyin_id'])

    def test_online_roster_without_a_known_uid_stays_unresolved(self):
        record = self.online_records()[0]

        self.assertEqual('dou8328160', record['nickname'])
        self.assertIsNone(record['matched_identity'])
        self.assertTrue(record['is_mystery'])

    def test_online_polling_never_duplicates_the_daily_alias(self):
        self.send_high_level_gift(consume_level=41)

        for _ in range(3):
            self.online_records()

        self.assertEqual(
            [('dou8328160', 'room-a')],
            [
                tuple(row)
                for row in self.query(
                    'SELECT alias, room_id FROM identity_alias_history'
                )
            ],
        )

    # ---------- 公屏历史 ----------

    def test_feed_history_carries_the_matched_identity(self):
        self.send_high_level_gift(consume_level=41)
        self.listener._handle_chat_payload(chat_payload(**ANONYMOUS_FORM))

        events = web_listener._build_feed_events('room-a')

        chat_events = [event for event in events if event['type'] == 'chat']
        self.assertEqual(1, len(chat_events))
        self.assertEqual('dou8328160', chat_events[0]['display'])
        self.assertEqual(
            '示例用户甲', chat_events[0]['matched_identity']['real_name']
        )


if __name__ == '__main__':
    unittest.main()
