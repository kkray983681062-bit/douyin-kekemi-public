import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import web_listener


class UserLevelTestBase(unittest.TestCase):
    """每个用例独立临时库；进程内去重缓存也要清，否则用例互相污染。"""

    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as handle:
            self.db_path = handle.name
        self.db_patch = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.db_patch.start()
        web_listener.init_db()
        web_listener._user_level_written.clear()

    def tearDown(self):
        self.db_patch.stop()

    def levels(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return dict(conn.execute(
                'SELECT user_key, consume_level FROM user_levels'
            ).fetchall())
        finally:
            conn.close()


class RecordUserLevelTests(UserLevelTestBase):
    def test_stores_latest_observed_level(self):
        web_listener.record_user_level('uid-a', 12)
        self.assertEqual({'uid-a': 12}, self.levels())

    def test_updates_when_level_changes(self):
        web_listener.record_user_level('uid-a', 12)
        web_listener.record_user_level('uid-a', 13)
        self.assertEqual({'uid-a': 13}, self.levels())

    def test_ignores_zero_and_missing_uid(self):
        web_listener.record_user_level('uid-a', 0)
        web_listener.record_user_level('', 30)
        web_listener.record_user_level(None, 30)
        self.assertEqual({}, self.levels())

    def test_same_level_writes_only_once(self):
        web_listener.record_user_level('uid-a', 12)
        conn = sqlite3.connect(self.db_path)
        conn.execute('UPDATE user_levels SET consume_level = 99')
        conn.commit()
        conn.close()
        # 同值命中进程内缓存，不再写库——99 不会被覆盖回 12
        web_listener.record_user_level('uid-a', 12)
        self.assertEqual({'uid-a': 99}, self.levels())


class FeedLevelStampTests(UserLevelTestBase):
    def test_feed_events_carry_latest_observed_level(self):
        web_listener._save_interaction('room-a', 'uid-a', '壕客', 'chat', content='你好')
        web_listener._save_interaction('room-a', '', '匿名者', 'chat', content='匿名发言')
        web_listener.record_user_level('uid-a', 41)

        events = web_listener._build_feed_events('room-a')

        by_display = {ev['display']: ev for ev in events}
        self.assertEqual(41, by_display['壕客']['consume_level'])
        self.assertEqual(0, by_display['匿名者']['consume_level'])


class AttachConsumeLevelsTests(UserLevelTestBase):
    def test_stamps_roster_records_by_sec_uid(self):
        web_listener.record_user_level('uid-a', 27)
        records = [
            {'sec_uid': 'uid-a', 'nickname': '甲'},
            {'sec_uid': 'uid-b', 'nickname': '乙'},
            {'sec_uid': '', 'nickname': '匿名'},
        ]
        web_listener.attach_consume_levels(records, 'sec_uid')
        self.assertEqual(27, records[0]['consume_level'])
        self.assertEqual(0, records[1]['consume_level'])
        self.assertEqual(0, records[2]['consume_level'])

    def test_stamps_weekly_visitor_rows_and_skips_display_keys(self):
        web_listener.record_user_level('uid-a', 55)
        rows = [
            {'sender_key': 'uid-a', 'display': '壕客', 'tickets': 100},
            {'sender_key': 'display:马甲人', 'display': '马甲人', 'tickets': 50},
        ]
        web_listener.attach_consume_levels(rows, 'sender_key')
        self.assertEqual(55, rows[0]['consume_level'])
        self.assertEqual(0, rows[1]['consume_level'])


if __name__ == '__main__':
    unittest.main()


class _Content:
    def __init__(self, level=0, alt='', name=''):
        self.level = level
        self.alternative_text = alt
        self.name = name


class _Badge:
    def __init__(self, content):
        self.content = content


class _User:
    def __init__(self, badges):
        self.badge_image_list = badges


class GetConsumeLevelTests(unittest.TestCase):
    """抖音已弃用 user.consume_diamond_level（实测 31/31 全为 0），
    荣誉等级搬进了 badge_image_list：荣誉徽章 alternative_text 含「荣誉等级」
    且无 name；粉丝团徽章带团名 name。顺序不保证，不能取第一个了事。"""

    def test_picks_honor_badge_even_when_fanclub_comes_first(self):
        user = _User([
            _Badge(_Content(level=17, alt='ni小汁粉丝团等级17级勋章', name='ni小汁')),
            _Badge(_Content(level=49, alt='荣誉等级49级勋章')),
        ])
        self.assertEqual(49, web_listener.get_consume_level(user))

    def test_returns_zero_without_badges(self):
        self.assertEqual(0, web_listener.get_consume_level(_User([])))
        self.assertEqual(0, web_listener.get_consume_level(object()))

    def test_fanclub_only_user_has_no_consume_level(self):
        user = _User([
            _Badge(_Content(level=17, alt='某某粉丝团等级17级勋章', name='某某')),
        ])
        self.assertEqual(0, web_listener.get_consume_level(user))

    def test_nameless_level_badge_is_accepted_as_fallback(self):
        """alternative_text 缺失时退而求其次：带等级且无团名的徽章。"""
        user = _User([_Badge(_Content(level=33))])
        self.assertEqual(33, web_listener.get_consume_level(user))
