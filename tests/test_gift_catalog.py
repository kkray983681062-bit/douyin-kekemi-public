import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


from gift_catalog import (
    init_gift_catalog,
    list_gifts,
    list_name_prices,
    list_price_history,
    observe_gift,
    resolve_gift_price,
    resolve_name_price,
    set_manual_price,
    set_name_price,
    sync_douyin_prices,
)
import web_listener


NOW = 1_800_000_000


def make_connection():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE interaction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            sec_uid TEXT NOT NULL DEFAULT '',
            display TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL,
            content TEXT DEFAULT '',
            gift_id TEXT DEFAULT '',
            gift_quantity INTEGER DEFAULT 1,
            unit_diamonds INTEGER,
            total_diamonds INTEGER,
            price_known INTEGER DEFAULT 0,
            quantity_verified INTEGER DEFAULT 0,
            price_source TEXT DEFAULT '',
            timestamp INTEGER NOT NULL
        )
    ''')
    init_gift_catalog(conn)
    return conn


class GiftCatalogTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()

    def tearDown(self):
        self.conn.close()

    def test_unknown_gift_is_one_pending_library_row_until_price_is_saved(self):
        observe_gift(self.conn, 'gift-9', '雷霆一击', None, NOW - 10)
        observe_gift(self.conn, 'gift-9', '雷霆一击', None, NOW)

        gifts = list_gifts(self.conn)
        self.assertEqual(1, len(gifts))
        self.assertEqual('gift-9', gifts[0]['gift_id'])
        self.assertEqual(2, gifts[0]['occurrence_count'])
        self.assertTrue(gifts[0]['price_pending'])

        result = set_manual_price(
            self.conn,
            gift_id='gift-9',
            gift_name='雷霆一击',
            unit_diamonds=520,
            note='直播间确认',
            reason='补充未知礼物价格',
            actor='admin-a',
            now=NOW + 1,
        )
        observe_gift(self.conn, 'gift-9', '雷霆一击', None, NOW + 2)

        gift = list_gifts(self.conn)[0]
        self.assertFalse(gift['price_pending'])
        self.assertEqual(520, gift['unit_diamonds'])
        self.assertEqual('manual', gift['price_source'])
        self.assertEqual(0, result['recalculated_rows'])

    def test_manual_price_has_priority_and_every_edit_is_audited(self):
        observe_gift(self.conn, 'gift-1', '星光真爱', 99, NOW - 2)
        live_resolution = resolve_gift_price(self.conn, 'gift-1', 99)
        self.assertEqual(99, live_resolution.unit_diamonds)
        self.assertEqual('douyin', live_resolution.source)

        set_manual_price(
            self.conn, 'gift-1', '星光真爱', 688,
            '第一次确认', '修正平台价格', 'admin-a', NOW - 1,
        )
        set_manual_price(
            self.conn, 'gift-1', '星光真爱', 520,
            '第二次确认', '现场复核修正', 'admin-b', NOW,
        )

        resolution = resolve_gift_price(self.conn, 'gift-1', 99)
        history = list_price_history(self.conn, 'gift-1')
        self.assertEqual(520, resolution.unit_diamonds)
        self.assertEqual('manual', resolution.source)
        self.assertEqual(2, len(history))
        self.assertEqual(520, history[0]['new_unit_diamonds'])
        self.assertEqual(688, history[0]['old_unit_diamonds'])
        self.assertEqual('admin-b', history[0]['actor'])
        self.assertEqual(688, history[1]['new_unit_diamonds'])
        self.assertIsNone(history[1]['old_unit_diamonds'])

    def test_saving_price_recalculates_only_recent_quantity_verified_gifts(self):
        rows = [
            ('recent', NOW - 2 * 86400, 3, 1),
            ('old', NOW - 8 * 86400, 4, 1),
            ('unverified', NOW - 86400, 5, 0),
        ]
        for display, timestamp, quantity, verified in rows:
            self.conn.execute('''
                INSERT INTO interaction_log (
                    room_id, display, type, content, gift_id, gift_quantity,
                    unit_diamonds, total_diamonds, price_known,
                    quantity_verified, price_source, timestamp
                ) VALUES ('room-a', ?, 'gift', '一束花开', 'gift-2', ?,
                          NULL, NULL, 0, ?, '', ?)
            ''', (display, quantity, verified, timestamp))
        self.conn.commit()
        observe_gift(self.conn, 'gift-2', '一束花开', None, NOW)

        result = set_manual_price(
            self.conn, 'gift-2', '一束花开', 99,
            '实测单价', '补录价格', 'admin-a', NOW,
        )

        data = {
            row['display']: dict(row)
            for row in self.conn.execute('''
                SELECT display, unit_diamonds, total_diamonds, price_known,
                       quantity_verified, price_source
                FROM interaction_log ORDER BY id
            ''').fetchall()
        }
        self.assertEqual(1, result['recalculated_rows'])
        self.assertEqual(99, data['recent']['unit_diamonds'])
        self.assertEqual(297, data['recent']['total_diamonds'])
        self.assertEqual(1, data['recent']['price_known'])
        self.assertEqual('manual', data['recent']['price_source'])
        self.assertIsNone(data['old']['total_diamonds'])
        self.assertIsNone(data['unverified']['total_diamonds'])

    def test_douyin_catalog_sync_reprices_recent_rows_but_never_manual_overrides(self):
        for gift_id, display, source in (
            ('gift-live', '平台价格', ''),
            ('gift-manual', '管理员价格', 'manual'),
        ):
            self.conn.execute('''
                INSERT INTO interaction_log (
                    room_id, display, type, content, gift_id, gift_quantity,
                    unit_diamonds, total_diamonds, price_known,
                    quantity_verified, price_source, timestamp
                ) VALUES ('room-a', ?, 'gift', ?, ?, 3,
                          1, 3, 1, 1, ?, ?)
            ''', (display, display, gift_id, source, NOW - 10))
        observe_gift(self.conn, 'gift-live', '平台价格', 1, NOW - 10)
        observe_gift(self.conn, 'gift-manual', '管理员价格', 1, NOW - 10)
        set_manual_price(
            self.conn, 'gift-manual', '管理员价格', 520,
            '管理员确认', '保持人工价格', 'admin', NOW - 5,
        )

        updated = sync_douyin_prices(
            self.conn,
            {'gift-live': 99, 'gift-manual': 88},
            now=NOW,
        )

        rows = {
            row['display']: dict(row)
            for row in self.conn.execute('''
                SELECT display, unit_diamonds, total_diamonds, price_source
                FROM interaction_log ORDER BY id
            ''')
        }
        self.assertEqual(1, updated)
        self.assertEqual(297, rows['平台价格']['total_diamonds'])
        self.assertEqual('douyin', rows['平台价格']['price_source'])
        self.assertEqual(1560, rows['管理员价格']['total_diamonds'])
        self.assertEqual('manual', rows['管理员价格']['price_source'])

    def test_manual_price_requires_positive_integer_and_reason(self):
        observe_gift(self.conn, 'gift-3', '测试礼物', None, NOW)
        with self.assertRaises(ValueError):
            set_manual_price(
                self.conn, 'gift-3', '测试礼物', 0,
                '', '价格不能为零', 'admin', NOW,
            )
        with self.assertRaises(ValueError):
            set_manual_price(
                self.conn, 'gift-3', '测试礼物', 10,
                '', '   ', 'admin', NOW,
            )


class GiftNamePriceTests(unittest.TestCase):
    """按礼物名兜底定价：优先级最高、支持未出现礼物、抗 gift_id 名字漂移。"""

    def setUp(self):
        self.conn = make_connection()

    def tearDown(self):
        self.conn.close()

    def test_name_price_wins_over_gift_id_manual_and_live(self):
        observe_gift(self.conn, '3780', '钻石兔兔', 299, NOW - 5)
        set_manual_price(
            self.conn, '3780', '钻石兔兔', 299,
            '旧价', '按id录入', 'admin', NOW - 4,
        )
        # 没名字价时用 gift_id 人工价
        before = resolve_gift_price(self.conn, '3780', None, '钻石兔兔')
        self.assertEqual(299, before.unit_diamonds)

        set_name_price(
            self.conn, '钻石兔兔', 360,
            '现场确认', '按名字校准', 'ray', NOW,
        )
        after = resolve_gift_price(self.conn, '3780', 999, '钻石兔兔')
        self.assertEqual(360, after.unit_diamonds)
        self.assertEqual('manual', after.source)

    def test_name_price_applies_to_never_seen_gift(self):
        set_name_price(
            self.conn, '钻石飞艇', 23333,
            '', '预置未出现礼物', 'ray', NOW,
        )
        resolution = resolve_gift_price(self.conn, 'name:钻石飞艇', None, '钻石飞艇')
        self.assertEqual(23333, resolution.unit_diamonds)
        self.assertEqual('manual', resolution.source)

    def test_setting_name_price_reprices_recent_by_name_and_audits(self):
        for display, ts, qty, verified in (
            ('recent', NOW - 2 * 86400, 2, 1),
            ('old', NOW - 8 * 86400, 2, 1),
            ('unverified', NOW - 86400, 2, 0),
        ):
            self.conn.execute('''
                INSERT INTO interaction_log (
                    room_id, display, type, content, gift_id, gift_quantity,
                    unit_diamonds, total_diamonds, price_known,
                    quantity_verified, price_source, timestamp
                ) VALUES ('room-a', ?, 'gift', '钻石游轮', '6024', ?,
                          NULL, NULL, 0, ?, '', ?)
            ''', (display, qty, verified, ts))
        self.conn.commit()

        result = set_name_price(
            self.conn, '钻石游轮', 7200,
            '现场确认', '按名字校准', 'ray', NOW,
        )
        rows = {
            row['display']: dict(row)
            for row in self.conn.execute(
                'SELECT display, unit_diamonds, total_diamonds, price_source '
                'FROM interaction_log ORDER BY id'
            )
        }
        self.assertEqual(1, result['recalculated_rows'])
        self.assertEqual(7200, rows['recent']['unit_diamonds'])
        self.assertEqual(14400, rows['recent']['total_diamonds'])
        self.assertEqual('manual', rows['recent']['price_source'])
        self.assertIsNone(rows['old']['total_diamonds'])
        self.assertIsNone(rows['unverified']['total_diamonds'])
        history = list_price_history(self.conn, 'name:钻石游轮')
        self.assertEqual(1, len(history))
        self.assertEqual(7200, history[0]['new_unit_diamonds'])
        self.assertEqual('钻石游轮', list_name_prices(self.conn)[0]['gift_name'])

    def test_name_price_ignored_for_blank_or_placeholder_or_unknown(self):
        self.assertIsNone(resolve_name_price(self.conn, ''))
        self.assertIsNone(resolve_name_price(self.conn, '?'))
        self.assertIsNone(resolve_name_price(self.conn, '从没录过的礼物'))

    def test_name_price_requires_positive_integer_and_reason_and_name(self):
        with self.assertRaises(ValueError):
            set_name_price(self.conn, '钻石秘境', 0, '', '价必须正', 'ray', NOW)
        with self.assertRaises(ValueError):
            set_name_price(self.conn, '钻石秘境', 16000, '', '   ', 'ray', NOW)
        with self.assertRaises(ValueError):
            set_name_price(self.conn, '', 16000, '', '名字不能空', 'ray', NOW)


class GiftSkinPricingTests(unittest.TestCase):
    """多皮肤各价不同的礼物：未单独定价的皮肤 → 待补，绝不借用基础价；普通礼物照旧。"""

    def setUp(self):
        self.conn = make_connection()

    def tearDown(self):
        self.conn.close()

    def _seen(self, gift_id, content):
        self.conn.execute(
            "INSERT INTO interaction_log (room_id, type, content, gift_id, "
            "gift_quantity, quantity_verified, timestamp) "
            "VALUES ('r', 'gift', ?, ?, 1, 1, ?)",
            (content, gift_id, NOW),
        )
        self.conn.commit()

    def test_unpriced_skin_on_skinned_gift_is_pending_not_borrowed(self):
        self._seen('3780', '钻石兔兔')
        set_name_price(self.conn, '钻石兔兔', 360, '', '校准', 'ray', NOW)
        # 已定价皮肤 → 名字价
        self.assertEqual(360, resolve_gift_price(self.conn, '3780', 299, '钻石兔兔').unit_diamonds)
        # 同 gift_id 未定价的「比心兔兔」→ 待补，不借用抖音基础价 299
        pending = resolve_gift_price(self.conn, '3780', 299, '比心兔兔')
        self.assertIsNone(pending.unit_diamonds)
        self.assertEqual('pending', pending.source)
        # 若把基础皮肤也单独定价，就恢复正常取值
        self._seen('3780', '比心兔兔')
        set_name_price(self.conn, '比心兔兔', 299, '', '基础价', 'ray', NOW)
        self.assertEqual(299, resolve_gift_price(self.conn, '3780', 999, '比心兔兔').unit_diamonds)

    def test_normal_single_name_gift_price_unaffected(self):
        observe_gift(self.conn, 'g-rose', '玫瑰', 1, NOW)
        # 不是多皮肤礼物 → 照旧走抖音实时价
        rose = resolve_gift_price(self.conn, 'g-rose', 5, '玫瑰')
        self.assertEqual(5, rose.unit_diamonds)
        self.assertEqual('douyin', rose.source)

    def test_observe_marks_skinned_for_priced_name_seen_later(self):
        # 给一个尚未出现过的名字预置价（此时映射不到 gift_id）
        set_name_price(self.conn, '钻石热气球', 620, '', '预置', 'ray', NOW)
        # 它第一次出现在某 gift_id 上（抖音发的是基础价）→ observe 应把该 gift_id 记为多皮肤
        observe_gift(self.conn, '3773', '钻石热气球', 520, NOW)
        self.assertEqual(620, resolve_gift_price(self.conn, '3773', 520, '钻石热气球').unit_diamonds)
        base = resolve_gift_price(self.conn, '3773', 520, '热气球')
        self.assertIsNone(base.unit_diamonds)
        self.assertEqual('pending', base.source)


class GiftCatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.db_patch = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.db_patch.start()
        web_listener._init_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            observe_gift(conn, 'gift-api', '未知测试礼物', None, int(time.time()))
        finally:
            conn.close()
        self.client = web_listener.app.test_client()

    def tearDown(self):
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_admin_can_list_and_save_one_global_gift_price(self):
        before = self.client.get('/api/admin/gifts')
        saved = self.client.put('/api/admin/gifts/gift-api', json={
            'gift_name': '未知测试礼物',
            'unit_diamonds': 88,
            'note': '现场确认',
            'reason': '补录平台未返回的价格',
            'actor': '本地管理员',
        })
        after = self.client.get('/api/admin/gifts')
        history = self.client.get('/api/admin/gifts/gift-api/history')

        self.assertEqual(200, before.status_code)
        self.assertTrue(before.get_json()['records'][0]['price_pending'])
        self.assertEqual(200, saved.status_code)
        self.assertEqual(88, saved.get_json()['gift']['unit_diamonds'])
        self.assertEqual('manual', saved.get_json()['gift']['price_source'])
        self.assertFalse(after.get_json()['records'][0]['price_pending'])
        self.assertEqual(1, len(history.get_json()['records']))

    def test_admin_price_route_rejects_zero_and_blank_reason(self):
        response = self.client.put('/api/admin/gifts/gift-api', json={
            'gift_name': '未知测试礼物',
            'unit_diamonds': 0,
            'reason': '   ',
        })

        self.assertEqual(400, response.status_code)
        self.assertFalse(response.get_json()['success'])


if __name__ == '__main__':
    unittest.main()


class PendingNameTests(unittest.TestCase):
    """礼物库要能看见「没定价的名字」，否则它们永远补不上。

    2026-08-22 线上实况暴露的问题：一个 gift_id 底下挂着多个皮肤名，
    礼物库按 gift_id 列、一个 id 一行一个数字。id=3729 底下有 6 个名字
    3 种价（钻石飞机 3600 / 私人飞机 3000 / 碧空飞机 没定价），那一行
    显示的 3000 谁都不代表，而「碧空飞机」在界面上根本不存在——
    于是它一直算不进账，你想给它定价都找不到入口。

    按名字定价（set_name_price）是整条链路里优先级最高的一层，
    但在这之前它没有任何 HTTP 入口，从来没被调用过。
    """

    def setUp(self):
        self.conn = make_connection()

    def tearDown(self):
        self.conn.close()

    def _gift(self, gift_id, name, ts=NOW):
        observe_gift(self.conn, gift_id, name, None, ts)
        self.conn.execute(
            "INSERT INTO interaction_log (room_id, type, content, gift_id, "
            "gift_quantity, timestamp) VALUES ('r1', 'gift', ?, ?, 1, ?)",
            (name, gift_id, ts))
        self.conn.commit()

    def test_lists_names_that_cannot_be_priced(self):
        from gift_catalog import list_pending_names
        self._gift('3729', '私人飞机', NOW - 300)
        self._gift('3729', '钻石飞机', NOW - 200)
        self._gift('3729', '碧空飞机', NOW - 100)
        # 给其中一个名字定价 -> 这个 id 变成多皮肤，其余未定价的名字待补
        set_name_price(self.conn, '钻石飞机', 3600, '', '现场确认', 'ray', NOW)

        pending = list_pending_names(self.conn)
        names = [p['gift_name'] for p in pending]
        self.assertIn('碧空飞机', names)
        self.assertIn('私人飞机', names)
        self.assertNotIn('钻石飞机', names, '已定价的不该出现在待补里')

    def test_pending_entry_carries_what_you_need_to_decide_the_price(self):
        from gift_catalog import list_pending_names
        self._gift('3729', '钻石飞机', NOW - 200)
        self._gift('3729', '碧空飞机', NOW - 100)
        self._gift('3729', '碧空飞机', NOW - 50)
        set_name_price(self.conn, '钻石飞机', 3600, '', '现场确认', 'ray', NOW)

        item = next(p for p in list_pending_names(self.conn)
                    if p['gift_name'] == '碧空飞机')
        self.assertEqual('3729', item['gift_id'])
        self.assertEqual(2, item['occurrence_count'])
        self.assertEqual(NOW - 50, item['last_seen'])
        # 同 id 其他名字各是什么价——定价时的参照，不用去别处翻
        self.assertEqual([{'gift_name': '钻石飞机', 'unit_diamonds': 3600}],
                         item['siblings'])

    def test_priced_name_disappears_from_pending(self):
        from gift_catalog import list_pending_names
        self._gift('3780', '比心兔兔', NOW - 200)
        self._gift('3780', '甄爱比心兔兔', NOW - 100)
        set_name_price(self.conn, '比心兔兔', 299, '', '确认', 'ray', NOW)
        self.assertIn('甄爱比心兔兔',
                      [p['gift_name'] for p in list_pending_names(self.conn)])

        set_name_price(self.conn, '甄爱比心兔兔', 299, '', '确认', 'ray', NOW)
        self.assertEqual([], list_pending_names(self.conn))

    def test_single_skin_gift_is_not_pending(self):
        """普通单名礼物有抖音价就够了，不该被拉进待补列表刷屏。"""
        from gift_catalog import list_pending_names
        observe_gift(self.conn, '9001', '小心心', 1, NOW)
        self.conn.execute(
            "INSERT INTO interaction_log (room_id, type, content, gift_id, "
            "gift_quantity, timestamp) VALUES ('r1','gift','小心心','9001',1,?)",
            (NOW,))
        self.conn.commit()
        self.assertEqual([], list_pending_names(self.conn))

    def test_library_price_matches_what_accounting_actually_uses(self):
        """礼物库显示的价必须就是结算用的价，不能各算各的。

        改之前 list_gifts 只看 gift_catalog 的 id 层，不看名字价也不看
        多皮肤规则；线上 233 个礼物里有 3 个显示价 != 实际生效价
        （钻石飞机 显示3000/实际3600、嘉年华 显示33000/实际30000、
        抖音1号 显示10001/实际待补）。看账的人会被这个数字带偏。
        """
        self._gift('3729', '钻石飞机', NOW)
        set_manual_price(self.conn, '3729', '钻石飞机', 3000, '', 'id 层旧价', 'ray', NOW)
        set_name_price(self.conn, '钻石飞机', 3600, '', '现场确认', 'ray', NOW)

        row = next(g for g in list_gifts(self.conn) if g['gift_id'] == '3729')
        effective = resolve_gift_price(self.conn, '3729', None, '钻石飞机')
        self.assertEqual(effective.unit_diamonds, row['unit_diamonds'],
                         '显示价必须等于生效价')
        self.assertEqual(3600, row['unit_diamonds'])
