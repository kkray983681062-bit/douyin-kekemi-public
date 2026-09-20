import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import web_listener


def member_payload(nickname, sec_uid='', webcast_uid='', mystery_man=0):
    message = web_listener.Live_pb2.MemberMessage()
    message.user.nickname = nickname
    message.user.sec_uid = sec_uid
    message.user.webcast_uid = webcast_uid
    message.user.mystery_man = mystery_man
    return message.SerializeToString()


class MemberEntryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.db_patch = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.db_patch.start()
        web_listener._init_db()
        self.listener = web_listener.RoomListener('room-a', 'A厅')

    def tearDown(self):
        self.db_patch.stop()
        self.tempdir.cleanup()

    def db_counts(self):
        conn = sqlite3.connect(self.db_path)
        try:
            profile_count = conn.execute(
                'SELECT COUNT(*) FROM mystery_records'
            ).fetchone()[0]
            interaction_count = conn.execute(
                'SELECT COUNT(*) FROM interaction_log'
            ).fetchone()[0]
            enter_total = conn.execute(
                'SELECT COALESCE(SUM(enter_count), 0) FROM mystery_records'
            ).fetchone()[0]
        finally:
            conn.close()
        return profile_count, interaction_count, enter_total

    def profile(self, sec_uid):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM mystery_records WHERE sec_uid=?', (sec_uid,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def test_regular_entry_is_emitted_without_database_write(self):
        """开关打开时的原行为：只下发、不写库。

        _PUSH_REGULAR_ENTER 默认是 False（进场事件量最大又没信息量，
        前端还因为 mystery_enter 不在 FEED_TYPE_MAP 里而全部丢弃，
        推了等于白推）。这条测试显式把开关打开，钉住「一旦打开，
        行为仍是只下发不写库」——回滚时靠它保证退得回去。
        """
        before = self.db_counts()

        with patch.object(
            web_listener,
            'is_real_mystery_user',
            return_value=(False, '普通游客', '普通游客', 0),
        ), patch.object(web_listener, '_PUSH_REGULAR_ENTER', True):
            self.listener._handle_member_payload(member_payload(
                nickname='普通游客', sec_uid='sec-regular', mystery_man=0,
            ))

        event = self.listener.events.get_nowait()
        self.assertEqual('mystery_enter', event['type'])
        self.assertTrue(event['data']['is_regular'])
        self.assertEqual('room-a', event['data']['room_id'])
        self.assertEqual(before, self.db_counts())

    def test_regular_entry_is_not_pushed_when_switch_is_off(self):
        """默认行为：普通观众进场既不下发也不写库。

        这一步是纯省流量、零数据损失——进场本来就不入库，前端也全丢。
        """
        before = self.db_counts()
        with patch.object(
            web_listener,
            'is_real_mystery_user',
            return_value=(False, '普通游客', '普通游客', 0),
        ), patch.object(web_listener, '_PUSH_REGULAR_ENTER', False):
            self.listener._handle_member_payload(member_payload(
                nickname='普通游客', sec_uid='sec-regular', mystery_man=0,
            ))

        self.assertTrue(self.listener.events.empty(), '开关关掉就不该下发')
        self.assertEqual(before, self.db_counts(), '本来就不写库，仍然不写')

    def test_identified_mystery_entry_updates_profile_without_counting_entry(self):
        with (
            patch.object(
                web_listener,
                'is_real_mystery_user',
                return_value=(True, 'dou7101055', 'dou7101055', 2),
            ),
            patch.object(
                web_listener,
                'lookup_user',
                return_value={
                    'sec_uid': 'sec-mystery',
                    'nickname': '示例用户甲',
                    'unique_id': '7101055',
                },
            ),
        ):
            self.listener._handle_member_payload(member_payload(
                nickname='dou7101055', sec_uid='sec-mystery', mystery_man=2,
            ))

        event = self.listener.events.get_nowait()
        self.assertEqual('mystery_enter', event['type'])
        self.assertFalse(event['data']['is_regular'])
        self.assertEqual('示例用户甲', event['data']['real_name'])
        row = self.profile('sec-mystery')
        self.assertIsNotNone(row)
        self.assertEqual(0, row['enter_count'])
        self.assertEqual((1, 0, 0), self.db_counts())

    def test_unidentified_mystery_entry_is_emitted_without_profile_row(self):
        with (
            patch.dict(web_listener._private_name_cache, {}, clear=True),
            patch.object(
                web_listener,
                'is_real_mystery_user',
                return_value=(True, 'dou7101055', 'dou7101055', 2),
            ),
            patch.object(web_listener, 'lookup_user', return_value=None),
        ):
            self.listener._handle_member_payload(member_payload(
                nickname='dou7101055', webcast_uid='webcast-anon', mystery_man=2,
            ))

        event = self.listener.events.get_nowait()
        self.assertEqual('mystery_enter', event['type'])
        self.assertEqual('dou7101055', event['data']['display'])
        self.assertEqual('webcast-anon', event['data']['webcast_uid'])
        self.assertEqual((0, 0, 0), self.db_counts())

    def test_database_profile_is_not_replayed_as_an_entry_event(self):
        web_listener._save_mystery_record(
            'room-a', 'sec-old', '神秘人123', 'demo_004', {}, 'enter',
            timestamp=1_700_000_000,
        )

        events = web_listener._build_feed_events('room-a')

        self.assertEqual([], [event for event in events if event['type'] == 'enter'])

    def test_start_loads_profile_card_without_replaying_an_entry(self):
        web_listener._save_mystery_record(
            'room-a', 'sec-old', '神秘人123', 'demo_004', {}, 'chat',
            timestamp=1_700_000_000,
        )

        with (
            patch.dict(web_listener.listeners, {}, clear=True),
            patch.object(web_listener.RoomListener, 'start', autospec=True),
        ):
            response = web_listener.app.test_client().post(
                '/api/start', json={'room_id': 'room-a', 'nickname': 'A厅'}
            )
            listener = web_listener.listeners['room-a']

            self.assertTrue(response.get_json()['success'])
            self.assertEqual(1, len(listener.recent_mysteries))
            self.assertEqual([], list(listener.recent_entries))
            self.assertTrue(listener.events.empty())


if __name__ == '__main__':
    unittest.main()
