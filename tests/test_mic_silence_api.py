import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mic_silence
import web_listener


class FakeSnapshot:
    def read(self):
        return {'records': []}


class FakeListener:
    """够 /api/status 和静默扫描两边用的最小桩。"""

    def __init__(self, room_id, douyin_id, hall, anchor, mic):
        self.room_id = room_id
        self.douyin_id = douyin_id
        self.nickname = hall
        self.sec_anchor_id = anchor
        self.mic_users = mic
        self.running = True
        # 以下三个只有 /api/status 会读
        self.mystery_count = 0
        self.recent_mysteries = []
        self.online_snapshot = FakeSnapshot()


class BrokenListener:
    """读麦位就抛异常的桩：验证单个厅出错不会掀翻整轮扫描。

    必须把 property 定义在类上——挂到实例属性上不会触发描述符协议，
    取到的只是 property 对象本身，抛出来的异常就不是我们想测的那个了。
    """

    douyin_id = 'demo_hall_a'
    nickname = 'demo_003'
    sec_anchor_id = 'anchor-1'
    running = True

    @property
    def mic_users(self):
        raise RuntimeError('模拟麦位读取炸了')


class SilenceApiTests(unittest.TestCase):
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

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_init_db_creates_silence_tables(self):
        conn = self._conn()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn('mic_silence_state', names)
        self.assertIn('mic_silence_watches', names)

    def test_scan_once_advances_state_and_fires(self):
        listener = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        with patch.dict(web_listener.listeners, {'room-1': listener}, clear=True):
            self.assertEqual(0, web_listener.silence_scan_once(now=1_800_000_000))
            fired = web_listener.silence_scan_once(now=1_800_000_000 + 16 * 60)
        self.assertEqual(1, fired)

    def test_scan_once_skips_room_without_douyin_id(self):
        """临时厅没有抖音号，不支持订阅，直接跳过。"""
        listener = FakeListener(
            'room-t', '', '临时厅', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        with patch.dict(web_listener.listeners, {'room-t': listener}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000)
        conn = self._conn()
        count = conn.execute('SELECT COUNT(*) FROM mic_silence_state').fetchone()[0]
        conn.close()
        self.assertEqual(0, count)

    def test_scan_once_never_raises(self):
        """检查器挂掉不能影响监听：异常只写日志，且不连累同轮其他厅。"""
        healthy = FakeListener(
            'room-1', 'OtherId8888', '正常厅', 'anchor-2',
            [{'sec_uid': 'host-b', 'nickname': '主持乙'}])
        with patch.dict(
                web_listener.listeners,
                {'room-x': BrokenListener(), 'room-1': healthy},
                clear=True):
            self.assertEqual(
                0, web_listener.silence_scan_once(now=1_800_000_000))
        conn = self._conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM mic_silence_state WHERE room_id = 'room-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(1, count, '一个厅炸了不能连累同轮其他厅')

    def test_scan_once_closes_room_when_listener_stops_running(self):
        """厅还在 listeners 里，但 running 已经是 False（下播那一刻）。"""
        listener = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        with patch.dict(web_listener.listeners, {'room-1': listener}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000)
            web_listener.silence_scan_once(now=1_800_000_000 + 16 * 60)  # 一条未恢复告警
            listener.running = False
            web_listener.silence_scan_once(now=1_800_000_000 + 17 * 60)
        conn = self._conn()
        state_count = conn.execute(
            'SELECT COUNT(*) FROM mic_silence_state').fetchone()[0]
        resolved = conn.execute(
            'SELECT resolved_at FROM mic_silence_alerts').fetchone()[0]
        conn.close()
        self.assertEqual(0, state_count, '下播后状态行要清掉')
        self.assertGreater(resolved, 0, '下播后未恢复的告警要标记 resolved')

    def test_scan_once_closes_room_removed_from_listeners(self):
        """厅被 /api/stop 整个移出 listeners，per-listener 循环根本看不到它，
        必须靠按 mic_silence_state 里剩的 room_id 扫一遍才能发现。"""
        listener = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        with patch.dict(web_listener.listeners, {'room-1': listener}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000)
            web_listener.silence_scan_once(now=1_800_000_000 + 16 * 60)
        # 模拟 /api/stop：整个从 listeners 里删掉，不只是 running=False
        with patch.dict(web_listener.listeners, {}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000 + 17 * 60)
        conn = self._conn()
        state_count = conn.execute(
            'SELECT COUNT(*) FROM mic_silence_state').fetchone()[0]
        resolved = conn.execute(
            'SELECT resolved_at FROM mic_silence_alerts').fetchone()[0]
        conn.close()
        self.assertEqual(0, state_count, '孤儿状态行要被扫尾清掉')
        self.assertGreater(resolved, 0, '孤儿告警也要标记 resolved')

    def test_scan_once_does_not_close_running_room_with_empty_mic_this_tick(self):
        """麦位这一刻恰好没人不等于下播——WS 抖动会短暂清空麦位列表，
        这属于 missing_ticks 该管的事，不是 close_room 该管的事。"""
        listener = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        with patch.dict(web_listener.listeners, {'room-1': listener}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000)
            listener.mic_users = []       # 仍在运行，只是这一刻麦位空了
            web_listener.silence_scan_once(now=1_800_000_000 + 60)
        conn = self._conn()
        row = conn.execute('SELECT missing_ticks FROM mic_silence_state').fetchone()
        conn.close()
        self.assertIsNotNone(row, '还在运行的厅，状态行不能被 close_room 提前清掉')
        self.assertEqual(1, row['missing_ticks'], '应该走 missing_ticks 累加，不是直接清掉')

    def test_cleanup_deletes_old_silence_alerts_and_dismissals_but_keeps_records(self):
        """告警/叉掉记录该跟 interaction_log 一样按 7 天清；证据表任何时候
        都不能被这个清理碰到——那个 exemption 是故意的，是这张表存在的意义。"""
        conn = self._conn()
        now = 1_800_000_000
        old = now - 8 * 86400     # 超过 7 天 cutoff
        recent = now - 3600
        conn.execute(
            "INSERT INTO mic_silence_alerts (id, room_id, douyin_id, hall_name, "
            "sec_uid, nickname, alert_index, created_at, resolved_at) VALUES "
            "(1,'room-1','demo_hall_a','H','host-a','主持甲',1,?,0)", (old,))
        conn.execute(
            "INSERT INTO mic_silence_alerts (id, room_id, douyin_id, hall_name, "
            "sec_uid, nickname, alert_index, created_at, resolved_at) VALUES "
            "(2,'room-1','demo_hall_a','H','host-a','主持甲',2,?,0)", (recent,))
        conn.execute(
            "INSERT INTO mic_silence_dismissals (alert_id, user_id, dismissed_at) "
            "VALUES (1, 'admin-1', ?)", (old,))
        conn.execute(
            "INSERT INTO mic_silence_dismissals (alert_id, user_id, dismissed_at) "
            "VALUES (2, 'admin-1', ?)", (recent,))
        conn.execute(
            "INSERT INTO mic_silence_records (room_id, douyin_id, hall_name, "
            "sec_uid, nickname, beijing_date, mic_since, created_at, "
            "alert_count, chat_snapshot) VALUES "
            "('room-1','demo_hall_a','H','host-a','主持甲','2027-01-02',?,?,3,'{}')",
            (old, old))
        conn.commit()
        conn.close()

        web_listener._cleanup_expired_data(now=now, force=True)

        conn = self._conn()
        alert_ids = {row[0] for row in
                     conn.execute('SELECT id FROM mic_silence_alerts')}
        dismissal_alert_ids = {row[0] for row in
                                conn.execute('SELECT alert_id FROM mic_silence_dismissals')}
        records_count = conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0]
        conn.close()

        self.assertEqual({2}, alert_ids, '超过 7 天的告警要被清掉，没超的留着')
        self.assertEqual({2}, dismissal_alert_ids,
                          '孤儿叉掉记录（对应告警已被清掉）要跟着清掉')
        self.assertEqual(1, records_count, '证据表任何时候都不清')

    def test_room_with_existing_state_and_open_alert_survives_a_mid_tick_exception(self):
        """running_room_ids 必须在调用 scan_room 之前就记下这个 room_id，
        顺序一旦被"整理"反了，一个厅这一轮扫描炸了就会被孤儿扫尾误判成
        「不在跑」，把还活着的状态行删掉、未恢复的告警标记 resolved——
        跟真实下播完全不是一回事。test_scan_once_never_raises 测不出这个：
        它的 BrokenListener 从来没有配套的状态行/告警。"""
        healthy = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        with patch.dict(web_listener.listeners, {'room-1': healthy}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000)
            web_listener.silence_scan_once(now=1_800_000_000 + 16 * 60)  # 一条未恢复告警

        # 同一个 room_id，换成读 mic_users 就炸的桩（douyin_id 跟上面一致）。
        with patch.dict(web_listener.listeners, {'room-1': BrokenListener()}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000 + 17 * 60)

        conn = self._conn()
        state_count = conn.execute(
            "SELECT COUNT(*) FROM mic_silence_state WHERE room_id = 'room-1'"
        ).fetchone()[0]
        resolved = conn.execute(
            'SELECT resolved_at FROM mic_silence_alerts').fetchone()[0]
        conn.close()
        self.assertEqual(1, state_count,
                          '这一轮扫描炸了，但厅还在跑，状态行不能被孤儿扫尾清掉')
        self.assertEqual(0, resolved,
                          '同理，未恢复的告警不能被误标 resolved')

    def test_watch_routes_roundtrip(self):
        saved = self.client.put('/api/admin/silence/watches',
                                json={'douyin_ids': ['demo_hall_a']})
        listed = self.client.get('/api/admin/silence/watches')
        self.assertEqual(200, saved.status_code)
        self.assertEqual(['demo_hall_a'], listed.get_json()['douyin_ids'])

    def test_alerts_route_filters_by_subscription(self):
        listener = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        with patch.dict(web_listener.listeners, {'room-1': listener}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000)
            web_listener.silence_scan_once(now=1_800_000_000 + 16 * 60)
        before = self.client.get('/api/admin/silence/alerts').get_json()
        self.assertEqual([], before['alerts'], '没订阅就看不到')
        self.client.put('/api/admin/silence/watches',
                        json={'douyin_ids': ['demo_hall_a']})
        after = self.client.get('/api/admin/silence/alerts').get_json()
        self.assertEqual(1, len(after['alerts']))

    def test_status_exposes_douyin_id(self):
        """前端铃铛按抖音号匹配订阅，/api/status 必须把它带出来。"""
        listener = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1', [])
        with patch.dict(web_listener.listeners, {'room-1': listener}, clear=True):
            payload = self.client.get('/api/status').get_json()
        self.assertEqual('demo_hall_a', payload['active'][0]['douyin_id'])

    def test_records_route_returns_room_records(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO mic_silence_records (room_id, douyin_id, hall_name, "
            "sec_uid, nickname, beijing_date, mic_since, created_at, "
            "alert_count, chat_snapshot) VALUES "
            "('room-1','demo_hall_a','demo_003','host-a','主持甲','2027-01-02',"
            "1,2,3,'{\"buckets\":[]}')")
        conn.commit()
        conn.close()
        payload = self.client.get(
            '/api/admin/silence/records/room-1').get_json()
        self.assertEqual(1, len(payload['records']))
        self.assertEqual('主持甲', payload['records'][0]['nickname'])
        self.assertIn('buckets', payload['records'][0]['chat_snapshot'])



class UnreadApiTests(unittest.TestCase):
    """告警条从主页拿掉后新增的两条口径：按厅给未读数、打开面板即已读。"""

    setUp = SilenceApiTests.setUp
    tearDown = SilenceApiTests.tearDown

    def _fire(self):
        listener = FakeListener(
            'room-1', 'demo_hall_a', 'demo_003', 'anchor-1',
            [{'sec_uid': 'host-a', 'nickname': '主持甲'}])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO interaction_log (room_id, sec_uid, display, type, "
            "content, timestamp) VALUES ('room-1','bystander','x','chat','在的',?)",
            (1_800_000_000 + 5,))
        conn.commit(); conn.close()
        with patch.dict(web_listener.listeners, {'room-1': listener}, clear=True):
            web_listener.silence_scan_once(now=1_800_000_000)
            web_listener.silence_scan_once(now=1_800_000_000 + 16 * 60)
        self.client.put('/api/admin/silence/watches',
                        json={'douyin_ids': ['demo_hall_a']})

    def test_alerts_route_reports_unread_by_hall(self):
        self._fire()
        payload = self.client.get('/api/admin/silence/alerts').get_json()
        self.assertEqual({'demo_hall_a': 1}, payload['unread_by_hall'])
        self.assertEqual(1, payload['count'])

    def test_mark_read_route_zeroes_the_count(self):
        self._fire()
        ids = [a['id'] for a in
               self.client.get('/api/admin/silence/alerts').get_json()['alerts']]
        marked = self.client.post('/api/admin/silence/alerts/read',
                                  json={'alert_ids': ids})
        after = self.client.get('/api/admin/silence/alerts').get_json()
        self.assertEqual(200, marked.status_code)
        self.assertEqual({}, after['unread_by_hall'], '角标归 0')
        self.assertEqual(1, len(after['alerts']),
                         '读过不等于不再静默，人还在麦上就还要显示')
        self.assertTrue(after['alerts'][0]['read'])

    def test_mark_read_tolerates_an_empty_list(self):
        self._fire()
        r = self.client.post('/api/admin/silence/alerts/read', json={})
        self.assertEqual(200, r.status_code)


if __name__ == '__main__':
    unittest.main()
