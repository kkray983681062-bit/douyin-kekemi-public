import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_listener


class TempDbMixin:
    """每个用例一个独立临时库：别写到开发库上，也避免并发跑时互相锁。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.patcher = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.patcher.start()
        web_listener._init_db()
        web_listener._READ_CACHE.clear()
        self.client = web_listener.app.test_client()

    def tearDown(self):
        self.patcher.stop()
        self.tempdir.cleanup()


def _seed(room, n, ts=None):
    import time
    ts = ts or int(time.time())
    for i in range(n):
        web_listener._save_interaction(
            room, 'sec-w%03d' % i, '神秘人%06d' % i, 'gift', content='玫瑰',
            gift_id='g', gift_quantity=1, unit_diamonds=10,
            total_diamonds=10, price_known=True, quantity_verified=True,
            recipient_key='host-%d' % (i % 3), recipient_name='主持%d' % (i % 3),
            event_key='%s-w%d' % (room, i), ticket_count=1000 - i,
            ticket_source='gift_price', timestamp=ts,
        )


class WeeklyRealNameTests(TempDbMixin, unittest.TestCase):
    """周榜也要带真名——之前只给日榜接了，周榜漏了。"""

    def test_weekly_visitors_carry_real_name(self):
        _seed('room-wk1', 3)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO mystery_records (sec_uid, display, real_name) "
            "VALUES ('sec-w000', '神秘人000000', '少年时')"
        )
        conn.commit()
        conn.close()
        web_listener._READ_CACHE.clear()
        data = self.client.get('/api/admin/weekly_rank/room-wk1').get_json()
        by_key = {r['sender_key']: r for r in data['visitors']}
        self.assertEqual('少年时', by_key['sec-w000']['real_name'])

    def test_weekly_hosts_carry_real_name_field(self):
        _seed('room-wk2', 3)
        data = self.client.get('/api/admin/weekly_rank/room-wk2').get_json()
        self.assertIn('real_name', data['hosts'][0])


class WeeklyPagingTests(TempDbMixin, unittest.TestCase):
    """周榜和日榜一样默认只发前几名。"""

    def test_default_returns_only_top_slice(self):
        _seed('room-wk3', 60)
        data = self.client.get('/api/admin/weekly_rank/room-wk3').get_json()
        self.assertEqual(web_listener.RANK_PAGE_SIZE, len(data['visitors']))
        self.assertEqual(60, data['visitor_total'])
        self.assertTrue(data['visitors_truncated'])

    def test_full_can_be_requested(self):
        _seed('room-wk4', 60)
        data = self.client.get(
            '/api/admin/weekly_rank/room-wk4?limit=0').get_json()
        self.assertEqual(60, len(data['visitors']))
        self.assertFalse(data['visitors_truncated'])

    def test_limit_and_week_start_both_in_cache_key(self):
        _seed('room-wk5', 60)
        short = self.client.get('/api/admin/weekly_rank/room-wk5').get_json()
        full = self.client.get(
            '/api/admin/weekly_rank/room-wk5?limit=0').get_json()
        self.assertEqual(web_listener.RANK_PAGE_SIZE, len(short['visitors']))
        self.assertEqual(60, len(full['visitors']), '缓存不能把截断版当全量返回')


if __name__ == '__main__':
    unittest.main()
