import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_listener


class AttachRealNamesTests(unittest.TestCase):
    """日榜里的神秘人，如果已经解析出真名，要能带出来。

    真名只在「确实解析出来且和马甲名不同」时才给；解析失败的占位值
    （?、-、未知）绝不能当成真名显示出去。
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.patcher = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.patcher.start()
        web_listener._init_db()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.patcher.stop()
        self.tempdir.cleanup()

    def _record(self, sec_uid, display, real_name):
        self.conn.execute(
            'INSERT INTO mystery_records (sec_uid, display, real_name) VALUES (?, ?, ?)',
            (sec_uid, display, real_name),
        )
        self.conn.commit()

    def test_resolved_mystery_gets_real_name(self):
        self._record('sec-1', '神秘人602191', 'kiyo')
        rows = [{'sender_key': 'sec-1', 'display': '神秘人602191'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('kiyo', rows[0]['real_name'])

    def test_unresolved_leaves_real_name_empty(self):
        rows = [{'sender_key': 'sec-none', 'display': '神秘人111697'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('', rows[0]['real_name'])

    def test_real_name_equal_to_display_is_not_reported(self):
        # 普通用户：真名就是显示名，不该在括号里重复一遍
        self._record('sec-2', '示例用户甲', '示例用户甲')
        rows = [{'sender_key': 'sec-2', 'display': '示例用户甲'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('', rows[0]['real_name'])

    def test_placeholder_names_are_never_reported_as_real(self):
        for placeholder in ('?', '-', '未知', ''):
            self.conn.execute('DELETE FROM mystery_records')
            self._record('sec-3', '神秘人999', placeholder)
            rows = [{'sender_key': 'sec-3', 'display': '神秘人999'}]
            web_listener.attach_real_names(rows, 'sender_key')
            self.assertEqual('', rows[0]['real_name'], placeholder)

    def test_display_prefixed_key_has_no_sec_uid_so_stays_empty(self):
        # 深度匿名没有 sec_uid，sender_key 是 'display:昵称'，查不到属预期
        rows = [{'sender_key': 'display:dou7101055', 'display': 'dou7101055'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('', rows[0]['real_name'])

    def test_identity_profile_is_used_when_mystery_record_missing(self):
        self.conn.execute(
            "INSERT INTO identity_profiles (id, canonical_sec_uid, real_name) "
            "VALUES (7, 'sec-9', '追光者')"
        )
        self.conn.execute(
            "INSERT INTO identity_identifiers "
            "(identity_id, identifier_type, identifier_value) "
            "VALUES (7, 'sec_uid', 'sec-9')"
        )
        self.conn.commit()
        rows = [{'sender_key': 'sec-9', 'display': '神秘人631309'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('追光者', rows[0]['real_name'])


class DailyRankRouteRealNameTests(unittest.TestCase):
    """接口层：日榜返回的每一行都要带 real_name 字段（没有就是空串）。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.patcher = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.patcher.start()
        web_listener._init_db()
        self.client = web_listener.app.test_client()

    def tearDown(self):
        self.patcher.stop()
        self.tempdir.cleanup()

    def test_daily_rank_rows_carry_real_name_field(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO mystery_records (sec_uid, display, real_name) '
            "VALUES ('sec-x', '神秘人602191', 'kiyo')"
        )
        conn.commit()
        conn.close()
        web_listener._save_interaction(
            'room-r', 'sec-x', '神秘人602191', 'gift', content='玫瑰',
            gift_id='g1', gift_quantity=1, unit_diamonds=100,
            total_diamonds=100, price_known=True, quantity_verified=True,
            recipient_key='host-1', recipient_name='主持甲',
            event_key='e1', ticket_count=100, ticket_source='gift_price',
        )
        payload = self.client.get('/api/daily_rank/room-r').get_json()
        self.assertTrue(payload['success'])
        self.assertEqual('kiyo', payload['visitors'][0]['real_name'])



class MergedKeyRealNameTests(unittest.TestCase):
    """榜单按身份合并之后，键变成了 'id:<identity_id>'。

    attach_real_names 原来是拿键去 identity_identifiers.identifier_value 里
    查——规范键根本不在那张表里，不处理的话合并过的行反而查不出真名了。
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.patcher = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.patcher.start()
        web_listener._init_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO identity_profiles (id, canonical_sec_uid, real_name, '
            'douyin_id, source) VALUES (706, ?, ?, ?, ?)',
            ('MS4wSEC', '示例用户乙', 'demo_user_a', 'high_level_gift'))
        conn.commit(); conn.close()

    def tearDown(self):
        self.patcher.stop()
        self.tempdir.cleanup()

    def test_real_name_resolves_for_a_merged_key(self):
        rows = [{'sender_key': 'id:706', 'display': 'dou2494080'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('示例用户乙', rows[0]['real_name'])

    def test_unknown_merged_key_stays_empty(self):
        rows = [{'sender_key': 'id:99999', 'display': 'x'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('', rows[0]['real_name'])

    def test_plain_keys_still_work(self):
        rows = [{'sender_key': 'display:神秘人123', 'display': '神秘人123'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('', rows[0]['real_name'])



class MergedKeyRobustnessTests(unittest.TestCase):
    """'id:N' 这个键格式来自榜单合并，attach_real_names 得扛得住畸形输入。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.patcher = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.patcher.start()
        web_listener._init_db()

    def tearDown(self):
        self.patcher.stop()
        self.tempdir.cleanup()

    def test_unicode_digit_key_does_not_crash(self):
        """str.isdigit() 认上标数字，'id:²' 能过守卫但 int() 会炸——
        而那个 int() 在 try 外面，直接把接口打成 500。"""
        rows = [{'sender_key': 'id:²', 'display': 'x'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('', rows[0]['real_name'])

    def test_leading_zero_key_is_not_treated_as_a_number(self):
        rows = [{'sender_key': 'id:0706', 'display': 'x'}]
        web_listener.attach_real_names(rows, 'sender_key')
        self.assertEqual('', rows[0]['real_name'])

    def test_consume_level_survives_a_mixed_row_list(self):
        """合并键和普通 sec_uid 混在同一批行里时，两边的等级都要在。

        踩过的坑：普通键那段是 `levels = {...}` 重新赋值、不是 update，
        把前面查好的合并键等级整个抹掉。而周榜前 20 行必然是混着的，
        所以生产上必现——徽章消失的恰恰是这次改动刚提到榜首的那批人。
        单行的测试走不到这个分支（普通键集合为空、根本不进那段）。
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO identity_profiles (id, canonical_sec_uid, real_name, '
            "source) VALUES (706,'SEC','示例用户乙','x')")
        conn.execute(
            'INSERT INTO identity_identifiers (identity_id, identifier_type, '
            "identifier_value, source, first_seen, last_seen) "
            "VALUES (706,'sec_uid','SEC','x',0,0)")
        conn.execute("INSERT INTO user_levels (user_key, consume_level, "
                     "updated_at) VALUES ('SEC', 46, 0)")
        conn.execute("INSERT INTO user_levels (user_key, consume_level, "
                     "updated_at) VALUES ('PLAIN', 12, 0)")
        conn.commit(); conn.close()
        rows = [{'sender_key': 'id:706', 'display': 'dou2494080'},
                {'sender_key': 'PLAIN', 'display': '普通人'}]
        web_listener.attach_consume_levels(rows, 'sender_key')
        self.assertEqual(46, rows[0]['consume_level'], '合并键的等级不能被抹掉')
        self.assertEqual(12, rows[1]['consume_level'], '普通键照常')

    def test_consume_level_resolves_for_a_merged_key(self):
        """合并后的键在 user_levels 里查不到，等级徽章会消失——而消失的
        恰恰是这次改动刚提到榜首的那批大额用户。要按身份下面的成员键去查。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO identity_profiles (id, canonical_sec_uid, real_name, '
            "source) VALUES (706,'SEC','示例用户乙','x')")
        conn.execute(
            'INSERT INTO identity_identifiers (identity_id, identifier_type, '
            "identifier_value, source, first_seen, last_seen) "
            "VALUES (706,'sec_uid','SEC','x',0,0)")
        conn.execute(
            'INSERT INTO user_levels (user_key, consume_level, updated_at) '
            "VALUES ('SEC', 46, 0)")
        conn.commit(); conn.close()
        rows = [{'sender_key': 'id:706', 'display': 'dou2494080'}]
        web_listener.attach_consume_levels(rows, 'sender_key')
        self.assertEqual(46, rows[0]['consume_level'])


if __name__ == '__main__':
    unittest.main()
