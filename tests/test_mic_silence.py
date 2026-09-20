import json
import sqlite3
import unittest

import mic_silence
import runtime_config


def make_conn():
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
            timestamp INTEGER NOT NULL
        )
    ''')
    mic_silence.init_silence_schema(conn)
    return conn


class SchemaTests(unittest.TestCase):
    def test_all_five_tables_exist(self):
        conn = make_conn()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('mic_silence_state', 'mic_silence_alerts',
                      'mic_silence_records', 'mic_silence_watches',
                      'mic_silence_dismissals'):
            self.assertIn(table, names)
        conn.close()

    def test_schema_is_idempotent(self):
        conn = make_conn()
        mic_silence.init_silence_schema(conn)   # 再来一次不能炸
        conn.close()

    def test_alerts_created_at_index_exists(self):
        """list_open_alerts 按 created_at 做下限过滤 + 排序，没有这个索引
        就会退化成整表扫描 + 临时 B-tree 排序（5 秒轮询一次，代价不小）。"""
        conn = make_conn()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        self.assertIn('idx_silence_alerts_created', names)


class ConfigTests(unittest.TestCase):
    def test_threshold_default_is_15_minutes(self):
        self.assertEqual(900, runtime_config.SILENCE_THRESHOLD_DEFAULT)
        self.assertEqual(900, runtime_config.resolve_silence_threshold({}))

    def test_threshold_reads_env(self):
        self.assertEqual(60, runtime_config.resolve_silence_threshold(
            {'SILENCE_THRESHOLD_SECONDS': '60'}))

    def test_threshold_rejects_illegal(self):
        for raw in ('', 'abc', '0', '-5'):
            self.assertEqual(
                runtime_config.SILENCE_THRESHOLD_DEFAULT,
                runtime_config.resolve_silence_threshold(
                    {'SILENCE_THRESHOLD_SECONDS': raw}), raw)

    def test_scan_interval_default_is_60(self):
        self.assertEqual(60, runtime_config.SILENCE_SCAN_INTERVAL_DEFAULT)
        self.assertEqual(60, runtime_config.resolve_silence_scan_interval({}))

    def test_scan_interval_reads_env_and_rejects_illegal(self):
        self.assertEqual(5, runtime_config.resolve_silence_scan_interval(
            {'SILENCE_SCAN_INTERVAL': '5'}))
        self.assertEqual(
            runtime_config.SILENCE_SCAN_INTERVAL_DEFAULT,
            runtime_config.resolve_silence_scan_interval(
                {'SILENCE_SCAN_INTERVAL': 'x'}))


MIN = 60


def chat(conn, room_id, sec_uid, at, text='hi'):
    conn.execute(
        "INSERT INTO interaction_log (room_id, sec_uid, display, type, content, timestamp) "
        "VALUES (?, ?, 'x', 'chat', ?, ?)", (room_id, sec_uid, text, at))
    conn.commit()


def keep_alive(conn, room_id, start, end, step=10 * 60):
    """让这个厅的入库管道在这段时间里一直有动静。

    生成证据前会检查「最后一条记录距现在多久」（_pipeline_alive）——正常
    运转的厅每分钟都有聊天/礼物/进场落库，长时间一条没有说明写入侧出了
    问题。测试里不垫的话，扫到第 3 次时管道看起来已经死了，证据不会生成。
    用的是别人（keepalive）的记录，不是被测主持，不进他的快照也不重置
    他的计时。
    """
    for ts in range(int(start), int(end) + 1, int(step)):
        conn.execute(
            "INSERT INTO interaction_log (room_id, sec_uid, display, type, "
            "content, timestamp) VALUES (?, 'keepalive', 'x', 'chat', '.', ?)",
            (room_id, ts))
    conn.commit()


def scan(conn, now, mic=(('host-a', '主持甲'),), anchor='anchor-1'):
    users = [{'sec_uid': s, 'nickname': n} for s, n in mic]
    return mic_silence.scan_room(
        conn, 'room-1', users, anchor, 'demo_hall_a', 'demo_003',
        now, threshold=15 * MIN)


class ScanTests(unittest.TestCase):
    """静默判定的状态推进。"""

    def setUp(self):
        self.conn = make_conn()
        self.t0 = 1_800_000_000

    def tearDown(self):
        self.conn.close()

    def test_first_scan_records_mic_since_and_does_not_alert(self):
        self.assertEqual([], scan(self.conn, self.t0))
        row = self.conn.execute('SELECT * FROM mic_silence_state').fetchone()
        self.assertEqual(self.t0, row['mic_since'])
        self.assertEqual(self.t0, row['last_spoke_at'])
        self.assertEqual(0, row['alert_count'])

    def test_alerts_only_after_threshold(self):
        scan(self.conn, self.t0)
        self.assertEqual([], scan(self.conn, self.t0 + 15 * MIN - 1),
                         '14分59秒不能报')
        fired = scan(self.conn, self.t0 + 15 * MIN + 1)
        self.assertEqual(1, len(fired))
        self.assertEqual(1, fired[0]['alert_index'])

    def test_does_not_repeat_every_scan_after_threshold(self):
        """报一次之后要重新等满 15 分钟，不能每分钟报一次。"""
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 15 * MIN + 1)
        self.assertEqual([], scan(self.conn, self.t0 + 16 * MIN),
                         '刚报过一分钟不能又报')
        self.assertEqual(1, len(scan(self.conn, self.t0 + 30 * MIN + 2)))

    def test_speaking_resets_timer_but_keeps_count(self):
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 15 * MIN + 1)          # 第 1 次
        chat(self.conn, 'room-1', 'host-a', self.t0 + 16 * MIN)
        self.assertEqual([], scan(self.conn, self.t0 + 20 * MIN),
                         '说过话之后计时要重置')
        row = self.conn.execute('SELECT * FROM mic_silence_state').fetchone()
        self.assertEqual(1, row['alert_count'], '发言不清零计数')
        fired = scan(self.conn, self.t0 + 31 * MIN + 2)
        self.assertEqual(2, fired[0]['alert_index'], '计数接着往上加')

    def test_anchor_is_never_alerted(self):
        anchor_only = (('anchor-1', 'demo_003'),)
        scan(self.conn, self.t0, mic=anchor_only)
        self.assertEqual([], scan(self.conn, self.t0 + 60 * MIN, mic=anchor_only))
        self.assertEqual(0, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_state').fetchone()[0],
            '大头连状态行都不该建')

    def test_leaving_needs_three_missing_ticks(self):
        scan(self.conn, self.t0)
        for tick in range(1, 3):
            scan(self.conn, self.t0 + tick * MIN, mic=())
            self.assertEqual(1, self.conn.execute(
                'SELECT COUNT(*) FROM mic_silence_state').fetchone()[0],
                f'第 {tick} 次没看到还不能算下麦')
        scan(self.conn, self.t0 + 3 * MIN, mic=())
        self.assertEqual(0, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_state').fetchone()[0])

    def test_reappearing_clears_missing_ticks(self):
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + MIN, mic=())
        scan(self.conn, self.t0 + 2 * MIN)              # 又出现了
        row = self.conn.execute('SELECT * FROM mic_silence_state').fetchone()
        self.assertEqual(0, row['missing_ticks'])

    def test_alert_row_carries_douyin_id_and_hall_name(self):
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 15 * MIN + 1)
        row = self.conn.execute('SELECT * FROM mic_silence_alerts').fetchone()
        self.assertEqual('demo_hall_a', row['douyin_id'])
        self.assertEqual('demo_003', row['hall_name'])
        self.assertEqual('主持甲', row['nickname'])

    def test_resolved_when_host_speaks_again(self):
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 15 * MIN + 1)
        chat(self.conn, 'room-1', 'host-a', self.t0 + 16 * MIN)
        scan(self.conn, self.t0 + 17 * MIN)
        row = self.conn.execute('SELECT * FROM mic_silence_alerts').fetchone()
        self.assertGreater(row['resolved_at'], 0, '恢复发言要标记 resolved')

    def test_resolved_when_host_leaves(self):
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 15 * MIN + 1)
        for tick in range(1, 4):
            scan(self.conn, self.t0 + (15 + tick) * MIN, mic=())
        row = self.conn.execute('SELECT * FROM mic_silence_alerts').fetchone()
        self.assertGreater(row['resolved_at'], 0, '下麦也要标记 resolved')


class EvidenceTests(unittest.TestCase):
    """满 3 次生成证据；聊天内容当场快照，不能只存引用。"""

    def setUp(self):
        self.conn = make_conn()
        # 2027-01-02 12:00:00 +08:00
        self.t0 = 1_798_862_400

    def tearDown(self):
        self.conn.close()

    def _fire_three(self):
        keep_alive(self.conn, 'room-1', self.t0 - 3600, self.t0 + 6 * 3600)
        # 用 20 分钟一档而不是 16：有测试会在 t0+60 插一条聊天，
        # 16 分钟档下 baseline 差正好等于阈值 900 秒，卡在边界上。
        scan(self.conn, self.t0)
        for n in range(1, 4):
            scan(self.conn, self.t0 + n * 20 * MIN)
        return self.conn.execute(
            'SELECT * FROM mic_silence_records').fetchall()

    def test_record_created_on_third_alert(self):
        rows = self._fire_three()
        self.assertEqual(1, len(rows))
        self.assertEqual(3, rows[0]['alert_count'])
        self.assertEqual('demo_hall_a', rows[0]['douyin_id'])
        self.assertEqual('主持甲', rows[0]['nickname'])

    def test_only_one_record_per_mic_session(self):
        self._fire_three()
        for n in range(4, 7):
            scan(self.conn, self.t0 + n * 20 * MIN)
        self.assertEqual(1, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0],
            '同一轮上麦只生成一条')

    def test_only_bands_this_session_crossed_are_listed(self):
        """12:00 上麦、13:00 出证据，只跨了 12:00-14:00 一档。

        原来这里断言的是「整天 12 档、空档也列」。整天口径已被线上数据
        证伪（会把别轮上麦说的话混进来），现在只列这轮跨到的档；
        「跨到但一句没说的档要列出来」这层意思由下面那条断言守住。
        """
        rows = self._fire_three()
        snapshot = json.loads(rows[0]['chat_snapshot'])
        self.assertEqual([('2027-01-02', '12:00', '14:00')],
                         [(b['date'], b['start'], b['end'])
                          for b in snapshot['buckets']])

    def test_a_crossed_band_with_no_speech_is_still_listed(self):
        """跨到了但一个字没说的档必须列出来——那才是证据。"""
        rows = self._fire_three()
        snapshot = json.loads(rows[0]['chat_snapshot'])
        self.assertTrue(snapshot['buckets'], '至少要有一档')
        self.assertEqual(0, snapshot['buckets'][0]['count'])
        self.assertEqual([], snapshot['buckets'][0]['messages'])

    def test_snapshot_copies_message_text_and_time(self):
        chat(self.conn, 'room-1', 'host-a', self.t0 + 60, '来了')
        rows = self._fire_three()
        snapshot = json.loads(rows[0]['chat_snapshot'])
        found = [m for b in snapshot['buckets'] for m in b['messages']]
        self.assertEqual(1, len(found))
        self.assertEqual('来了', found[0]['text'])
        self.assertRegex(found[0]['at'], r'^\d{2}:\d{2}:\d{2}$')

    def test_snapshot_survives_interaction_log_cleanup(self):
        chat(self.conn, 'room-1', 'host-a', self.t0 + 60, '来了')
        self._fire_three()
        self.conn.execute('DELETE FROM interaction_log')   # 模拟 7 天清理
        self.conn.commit()
        # 从 DB 重新读取，确认快照数据确实被复制了而非引用
        row = self.conn.execute(
            'SELECT * FROM mic_silence_records').fetchone()
        snapshot = json.loads(row['chat_snapshot'])
        found = [m for b in snapshot['buckets'] for m in b['messages']]
        self.assertEqual('来了', found[0]['text'], '证据不能跟着原始数据消失')


class CorroborationTests(unittest.TestCase):
    """空的 interaction_log 不能当成「这个人没说话」的证据——record_all
    被关、WS 断线重连、写入失败，都会让日志出现空档，空档说的是管道的事，
    不是这个人的事。生成证据前先确认这个厅的入库管道还活着。

    判定口径换过两次，见 _pipeline_alive 的说明：现在看的是「最后一条
    记录距现在多久」，不限 type，跟厅里聊不聊天无关。"""

    def setUp(self):
        self.conn = make_conn()
        # 2027-01-02 12:00:00 +08:00，跟 EvidenceTests 对齐
        self.t0 = 1_798_862_400

    def tearDown(self):
        self.conn.close()

    def _fire_three(self):
        scan(self.conn, self.t0)
        for n in range(1, 4):
            scan(self.conn, self.t0 + n * 20 * MIN)

    def test_completely_empty_log_does_not_produce_a_record(self):
        """全厅（不分是谁）一条聊天都没有：这段时间的日志本身就是空的，
        不能拿它去指认某个具体的人「没说话」。"""
        self._fire_three()
        self.assertEqual(0, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0],
            '全厅一条聊天记录都没有，不该生成证据')

    def test_state_survives_a_dead_pipeline(self):
        """corroboration 没过不能把这一轮的状态弄丢——计数要留着，
        日志一恢复就能接着补证据（见下面那条）。"""
        self._fire_three()
        row = self.conn.execute(
            "SELECT alert_count FROM mic_silence_state "
            "WHERE room_id = 'room-1' AND sec_uid = 'host-a'").fetchone()
        self.assertIsNotNone(row, '状态行要还在')
        self.assertEqual(3, row['alert_count'], '次数照常累计')

    def test_record_is_produced_when_someone_else_was_active(self):
        """管道活着就够了——动的是谁不重要，不必是这个主持本人。"""
        keep_alive(self.conn, 'room-1', self.t0, self.t0 + 90 * MIN)
        self._fire_three()
        self.assertEqual(1, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0],
            '全厅日志活着，证据应该照常生成')

    def test_later_alert_retries_once_logging_resumes(self):
        """日志恢复之后欠的证据要能补上，不能因为第 3 次撞上了日志空档
        就永远错过。每一次达到阈值的告警都会重新走一遍 corroboration。"""
        self._fire_three()
        self.assertEqual(0, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0])

        chat(self.conn, 'room-1', 'viewer-1', self.t0 + 61 * MIN, '在的')
        scan(self.conn, self.t0 + 80 * MIN)   # 第 4 次告警

        self.assertEqual(1, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0],
            '日志恢复之后，之前欠的证据要能补上')


class MidnightEvidenceTests(unittest.TestCase):
    """厅是 24 小时开的，上麦跨零点是常态。

    原来这个类断言「一条记录覆盖两天各 12 档」。2 小时档成为记账单位之后
    口径变了：跨零点就是 22:00-24:00 和次日 00:00-02:00 各自一条记录，
    互不相干。要守的东西没变——零点前那半段不能丢，而且日期要跟对。
    """

    def setUp(self):
        self.conn = make_conn()
        # 2027-01-02 23:00:00 +08:00
        self.t0 = 1_798_902_000
        keep_alive(self.conn, 'room-1', self.t0 - 3600, self.t0 + 6 * 3600)

    def tearDown(self):
        self.conn.close()

    def _records(self):
        # 16 分钟一档而不是 22：23:00 上麦，22:00-24:00 这一档只剩 60 分钟，
        # 22 分钟的节奏在跨零点前只攒得到 2 次，凑不满 3 次就出不了记录，
        # 那就测不到「零点两边各一条」这件事。
        scan(self.conn, self.t0)
        for n in range(1, 7):        # 23:16 23:32 23:48 | 00:04 00:20 00:36
            scan(self.conn, self.t0 + n * 16 * MIN)
        return self.conn.execute(
            'SELECT * FROM mic_silence_records ORDER BY band_start').fetchall()

    def test_each_side_of_midnight_gets_its_own_record(self):
        rows = self._records()
        bands = [json.loads(r['chat_snapshot'])['buckets'][0] for r in rows]
        self.assertEqual(
            [('2027-01-02', '22:00', '24:00'), ('2027-01-03', '00:00', '02:00')],
            [(b['date'], b['start'], b['end']) for b in bands],
            '零点两边各一条，日期各自跟对')

    def test_pre_midnight_chat_stays_in_the_pre_midnight_record(self):
        chat(self.conn, 'room-1', 'host-a', self.t0 + 60, '零点前说的')
        rows = self._records()
        first = json.loads(rows[0]['chat_snapshot'])
        found = [m for b in first['buckets'] for m in b['messages']]
        self.assertEqual(['零点前说的'], [m['text'] for m in found],
                         '零点前说的话归零点前那条，不能丢也不能挪到次日')
        second = json.loads(rows[1]['chat_snapshot'])
        self.assertEqual(0, sum(b['count'] for b in second['buckets']))

    def test_last_band_of_a_day_ends_at_24(self):
        bands = [json.loads(r['chat_snapshot'])['buckets'][0]
                 for r in self._records()]
        self.assertEqual('24:00', bands[0]['end'],
                         '一天里最后那档写 24:00，不是 00:00——'
                         '「22:00–00:00」读起来像跨了天')


class CloseRoomTests(unittest.TestCase):
    """下播/停止监听收尾：清状态行、结掉未恢复的告警，证据表不能被碰。"""

    def setUp(self):
        self.conn = make_conn()
        self.t0 = 1_800_000_000

    def tearDown(self):
        self.conn.close()

    def test_close_room_deletes_state_and_resolves_open_alerts(self):
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 15 * MIN + 1)   # 产生一条未恢复的告警
        self.assertEqual(1, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_state').fetchone()[0])

        mic_silence.close_room(self.conn, 'room-1', self.t0 + 20 * MIN)

        self.assertEqual(0, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_state').fetchone()[0],
            '下播后状态行要清掉')
        alert = self.conn.execute(
            'SELECT resolved_at FROM mic_silence_alerts').fetchone()
        self.assertGreater(alert['resolved_at'], 0,
                            '下播那一刻还开着的告警要标记 resolved')

    def test_close_room_does_not_touch_evidence_records(self):
        """证据是这张表存在的全部意义：close_room 绝不能碰它。"""
        # 垫上 keepalive 让入库管道看起来是活着的（这个测试测的是
        # close_room 不碰证据表，不是管道死活判定本身）。
        keep_alive(self.conn, 'room-1', self.t0, self.t0 + 90 * MIN)
        scan(self.conn, self.t0)
        for n in range(1, 4):
            scan(self.conn, self.t0 + n * 20 * MIN)
        before = self.conn.execute(
            'SELECT * FROM mic_silence_records').fetchall()
        self.assertEqual(1, len(before), '前置条件：应该已经生成一条证据')

        mic_silence.close_room(self.conn, 'room-1', self.t0 + 90 * MIN)

        after = self.conn.execute(
            'SELECT * FROM mic_silence_records').fetchall()
        self.assertEqual(1, len(after), '证据记录不能被下播收尾删掉')
        self.assertEqual(dict(before[0]), dict(after[0]),
                          '证据记录的内容也不能被改动')

    def test_close_room_only_affects_the_given_room(self):
        scan(self.conn, self.t0, mic=(('host-a', '主持甲'),))
        mic_silence.scan_room(
            self.conn, 'room-2', [{'sec_uid': 'host-b', 'nickname': '主持乙'}],
            'anchor-2', 'OtherId', '别的厅', self.t0, threshold=15 * MIN)

        mic_silence.close_room(self.conn, 'room-1', self.t0 + MIN)

        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM mic_silence_state WHERE room_id = 'room-1'"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM mic_silence_state WHERE room_id = 'room-2'"
        ).fetchone()[0], '不能连累别的厅')


class WatchAndDismissTests(unittest.TestCase):
    """订阅按抖音号存；叉掉是 per-admin 的，A 叉掉不能影响 B。"""

    def setUp(self):
        self.conn = make_conn()
        self.t0 = 1_800_000_000
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 16 * MIN)          # 产生一条告警
        self.alert_id = self.conn.execute(
            'SELECT id FROM mic_silence_alerts').fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_set_and_list_watches(self):
        mic_silence.set_watches(self.conn, 'admin-1',
                                ['demo_hall_a', 'demo_hall_c'], self.t0)
        self.assertEqual(['demo_hall_a', 'demo_hall_c'],
                         sorted(mic_silence.list_watches(self.conn, 'admin-1')))

    def test_set_watches_replaces_whole_set(self):
        mic_silence.set_watches(self.conn, 'admin-1', ['demo_hall_a'], self.t0)
        mic_silence.set_watches(self.conn, 'admin-1', ['demo_hall_c'], self.t0)
        self.assertEqual(['demo_hall_c'],
                         mic_silence.list_watches(self.conn, 'admin-1'))

    def test_watches_are_per_admin(self):
        mic_silence.set_watches(self.conn, 'admin-1', ['demo_hall_a'], self.t0)
        self.assertEqual([], mic_silence.list_watches(self.conn, 'admin-2'))

    def test_alerts_filtered_by_subscription(self):
        self.assertEqual([], mic_silence.list_open_alerts(
            self.conn, 'admin-1', now=self.t0 + 16 * MIN),
                         '没订阅就不该看到告警')
        mic_silence.set_watches(self.conn, 'admin-1', ['demo_hall_a'], self.t0)
        alerts = mic_silence.list_open_alerts(
            self.conn, 'admin-1', now=self.t0 + 16 * MIN)
        self.assertEqual(1, len(alerts))
        self.assertEqual('主持甲', alerts[0]['nickname'])

    def test_resolved_alert_drops_out_without_being_dismissed(self):
        """恢复发言的告警自己消失，不用手动叉。

        这里原来断言的是反过来的：「灰色留着直到叉掉」。实跑一晚被否了
        ——一个厅一晚攒 30 多条灰的，把真正要看的 2 条红的埋了。留在这个
        类里是给照着旧口径找过来的人一个明确的落点。
        """
        mic_silence.set_watches(self.conn, 'admin-1', ['demo_hall_a'], self.t0)
        self.assertEqual(1, len(mic_silence.list_open_alerts(
            self.conn, 'admin-1', now=self.t0 + 17 * MIN)), '还在静默时要有')
        self.conn.execute('UPDATE mic_silence_alerts SET resolved_at = ?',
                          (self.t0 + 17 * MIN,))
        self.conn.commit()
        self.assertEqual([], mic_silence.list_open_alerts(
            self.conn, 'admin-1', now=self.t0 + 17 * MIN))




class AlertLookbackTests(unittest.TestCase):
    """告警条只关心「现在需要谁去处理」：超过一天的不再是待办，
    历史证据由 mic_silence_records 存着，这里没必要也不该无限往回翻。
    """

    def setUp(self):
        self.conn = make_conn()
        self.t0 = 1_800_000_000
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 15 * MIN + 1)
        self.created_at = self.t0 + 15 * MIN + 1
        mic_silence.set_watches(self.conn, 'admin-1', ['demo_hall_a'], self.t0)

    def tearDown(self):
        self.conn.close()

    def test_alert_within_24h_is_returned(self):
        alerts = mic_silence.list_open_alerts(
            self.conn, 'admin-1', now=self.created_at + 3600)
        self.assertEqual(1, len(alerts))

    def test_alert_older_than_24h_is_not_returned(self):
        now = self.created_at + mic_silence.ALERT_LOOKBACK_SECONDS + 1
        alerts = mic_silence.list_open_alerts(self.conn, 'admin-1', now=now)
        self.assertEqual([], alerts, '超过 24 小时的告警不再是待办')

    def test_alert_exactly_at_boundary_is_still_returned(self):
        now = self.created_at + mic_silence.ALERT_LOOKBACK_SECONDS
        alerts = mic_silence.list_open_alerts(self.conn, 'admin-1', now=now)
        self.assertEqual(1, len(alerts), '正好 24 小时整还算在窗口内')



class BandSnapshotTests(unittest.TestCase):
    """快照只覆盖 now 所在的那个 2 小时档，且不早于上麦时刻。

    这个类原来叫 SessionScopedSnapshotTests，断言的是「覆盖整轮上麦」。
    那一版是为了修「快照带进了别轮上麦说的话」（线上 Ry.鱼小小 22:40 上麦
    却带着她当天 12:00-14:00 的 13 条）。后来口径又收紧了一次：2 小时档
    才是记账单位，档内满 3 次就挂一条记录、整点一过重新开始，所以证据也
    只该覆盖那一档——档一说的话不能算进档二的账上。

    原来那几条关切一条没丢，只是窗口更窄了：别轮的不进、上麦之前的不进、
    首尾和中间的空白都要标。
    """

    NOON = 1_798_862_400                        # 2027-01-02 12:00:00 +08:00
    MIC_SINCE = NOON + 40 * MIN                 # 12:40 上麦（12:00-14:00 档中途）
    UNTIL = NOON + 100 * MIN                    # 13:40 出证据，同一档内

    def setUp(self):
        self.conn = make_conn()

    def tearDown(self):
        self.conn.close()

    def _snap(self, threshold=15 * MIN):
        return mic_silence.build_chat_snapshot(
            self.conn, 'room-1', 'host-a', self.MIC_SINCE, self.UNTIL, threshold)

    def test_only_the_band_now_falls_in(self):
        snap = self._snap()
        self.assertEqual([('2027-01-02', '12:00', '14:00')],
                         [(b['date'], b['start'], b['end']) for b in snap['buckets']],
                         '只有 now 所在的那一档')

    def test_chat_from_another_band_is_excluded(self):
        """别的档说的话不能算进这一档——原来是「别轮上麦」，现在收紧到「别档」。"""
        chat(self.conn, 'room-1', 'host-a', self.NOON - 30 * MIN, '上一档说的')
        chat(self.conn, 'room-1', 'host-a', self.NOON + 130 * MIN, '下一档说的')
        found = [m for b in self._snap()['buckets'] for m in b['messages']]
        self.assertEqual([], found)

    def test_chat_before_going_on_mic_is_excluded(self):
        """12:10 说的话在 12:00-14:00 这档里，但那时她还没上麦。"""
        chat(self.conn, 'room-1', 'host-a', self.NOON + 10 * MIN, '还没上麦')
        chat(self.conn, 'room-1', 'host-a', self.MIC_SINCE + 10 * MIN, '上麦后说的')
        found = [m for b in self._snap()['buckets'] for m in b['messages']]
        self.assertEqual(['上麦后说的'], [m['text'] for m in found])

    def test_first_message_carries_the_gap_since_the_window_started(self):
        chat(self.conn, 'room-1', 'host-a', self.MIC_SINCE + 18 * MIN, '来了')
        found = [m for b in self._snap()['buckets'] for m in b['messages']]
        self.assertEqual(18 * MIN, found[0]['gap'], '第一条标「窗口起点到第一句」')

    def test_middle_gaps_are_measured_between_messages(self):
        for offset, text in ((10, '一'), (22, '二'), (52, '三')):
            chat(self.conn, 'room-1', 'host-a', self.MIC_SINCE + offset * MIN, text)
        found = [m for b in self._snap()['buckets'] for m in b['messages']]
        self.assertEqual([10 * MIN, 12 * MIN, 30 * MIN], [m['gap'] for m in found])

    def test_tail_gap_and_labels_are_reported(self):
        chat(self.conn, 'room-1', 'host-a', self.MIC_SINCE + 30 * MIN, '最后一句')
        snap = self._snap()
        self.assertEqual(30 * MIN, snap['tail_gap'], '最后一条到出证据隔了多久')
        self.assertEqual('12:40:00', snap['mic_since'], '窗口起点 = 上麦时刻')
        self.assertEqual('13:40:00', snap['until'])

    def test_window_starts_at_the_band_when_the_host_was_already_on_mic(self):
        """上麦早于本档起点时，窗口从档起点算，不回溯到上麦那一刻。"""
        snap = mic_silence.build_chat_snapshot(
            self.conn, 'room-1', 'host-a',
            self.NOON - 3 * 3600, self.UNTIL, 15 * MIN)
        self.assertEqual('12:00:00', snap['mic_since'])

    def test_never_spoke_at_all(self):
        snap = self._snap()
        self.assertEqual(60 * MIN, snap['tail_gap'], '一句没说，整段就是一个空白')
        self.assertEqual(0, sum(b['count'] for b in snap['buckets']))

    def test_threshold_is_recorded_so_old_records_render_right(self):
        """阈值可配置；记下当时用的值，日后改了阈值也不会把老记录标错。"""
        self.assertEqual(15 * MIN, self._snap()['gap_threshold'])
        self.assertEqual(20 * MIN, self._snap(threshold=20 * MIN)['gap_threshold'])


class UnreadAlertTests(unittest.TestCase):
    """告警条从主页拿掉，改成铃铛上挂未读数，所以口径变了：

    - 已恢复发言的**不再返回**（实跑一晚，30 多条灰色的把 2 条红色的埋了）
    - 「打开面板 = 读完」复用既有的 per-admin 叉掉表，不另建表
    """

    def setUp(self):
        self.conn = make_conn()
        self.t0 = 1_800_000_000
        chat(self.conn, 'room-1', 'bystander', self.t0 + 5, '在的')
        scan(self.conn, self.t0)
        scan(self.conn, self.t0 + 16 * MIN)
        mic_silence.set_watches(self.conn, 'admin-1', ['demo_hall_a'], self.t0)

    def tearDown(self):
        self.conn.close()

    def _open(self):
        return mic_silence.list_open_alerts(self.conn, 'admin-1', self.t0 + 20 * MIN)

    def test_resolved_alerts_are_no_longer_returned(self):
        self.assertEqual(1, len(self._open()), '还在静默的要返回')
        self.conn.execute('UPDATE mic_silence_alerts SET resolved_at = ?',
                          (self.t0 + 17 * MIN,))
        self.conn.commit()
        self.assertEqual([], self._open(), '恢复发言的自己消失，不该再占未读')

    def test_mark_read_zeroes_the_count_but_keeps_the_alert(self):
        """「已读」只该影响角标数字，不能把还在静默的人从列表里删掉。

        踩过的坑：已读一度直接复用叉掉表的过滤，结果一打开面板、
        5 秒后轮询回来，面板就写「此刻没有人在静默」——而那几个人
        还在麦上、还在静默。看一眼就把要看的东西看没了。
        """
        alerts = self._open()
        mic_silence.mark_alerts_read(
            self.conn, 'admin-1', [a['id'] for a in alerts], self.t0 + 20 * MIN)
        after = self._open()
        self.assertEqual(1, len(after), '读过不等于不再静默')
        self.assertTrue(after[0]['read'], '但要标出来已经读过了')
        self.assertEqual({}, mic_silence.unread_counts_by_hall(
            self.conn, 'admin-1', self.t0 + 20 * MIN), '角标归 0')

    def test_mark_read_is_per_admin(self):
        mic_silence.set_watches(self.conn, 'admin-2', ['demo_hall_a'], self.t0)
        alerts = self._open()
        mic_silence.mark_alerts_read(
            self.conn, 'admin-1', [a['id'] for a in alerts], self.t0 + 20 * MIN)
        self.assertEqual({'demo_hall_a': 1}, mic_silence.unread_counts_by_hall(
            self.conn, 'admin-2', self.t0 + 20 * MIN), 'A 读完不影响 B 的角标')

    def test_mark_read_is_idempotent_and_tolerates_junk_ids(self):
        alerts = self._open()
        ids = [a['id'] for a in alerts]
        for _ in range(2):
            mic_silence.mark_alerts_read(
                self.conn, 'admin-1', ids + [999999], self.t0 + 20 * MIN)
        self.assertEqual({}, mic_silence.unread_counts_by_hall(
            self.conn, 'admin-1', self.t0 + 20 * MIN))

    def test_unread_counts_are_grouped_by_hall(self):
        """铃铛挂在厅上，所以要按厅给数。"""
        counts = mic_silence.unread_counts_by_hall(
            self.conn, 'admin-1', self.t0 + 20 * MIN)
        self.assertEqual({'demo_hall_a': 1}, counts)



class BandScopedCountTests(unittest.TestCase):
    """静默次数按自然 2 小时档独立计，整点一过归零重来。

    原来是整轮上麦一路累加、下麦才清零，于是在麦越久数字越大——线上
    实测 Ry.眠鱼怪 在麦 4 小时报到「第 14 次」（14:00-16:00 七次 +
    16:00-18:00 七次），Ry.AS1 在麦 6.9 小时报到 17 次。这个数字既不
    反映「他现在有多离谱」，也没法跟别人比。

    改成：6-8 档满 3 次可以继续涨到 7 次，8-10 档从 0 重新开始。
    """

    NOON = 1_798_862_400            # 2027-01-02 12:00:00 +08:00

    def setUp(self):
        self.conn = make_conn()

    def tearDown(self):
        self.conn.close()

    def _fire_at(self, offsets, mic_at=0):
        """在 NOON+mic_at 上麦，然后在给定的各个时刻扫描。

        每个档都垫一条旁观者聊天：corroboration 现在也按档走（每档各自
        验证「这段时间日志是不是断供了」），只在第一档垫的话，第二档会
        因为全厅零聊天而被判成日志断供、不出证据。旁观者不是 host-a，
        不进他的快照也不重置他的计时。
        """
        keep_alive(self.conn, 'room-1', self.NOON - 3600, self.NOON + 6 * 3600)
        scan(self.conn, self.NOON + mic_at)
        for off in offsets:
            scan(self.conn, self.NOON + mic_at + off)

    def _indexes(self):
        return [r['alert_index'] for r in self.conn.execute(
            'SELECT alert_index FROM mic_silence_alerts ORDER BY id')]

    def test_count_restarts_in_the_next_band(self):
        # 12:00 上麦。12:00-14:00 档里报三次，进 14:00-16:00 档再报两次。
        self._fire_at([20 * MIN, 40 * MIN, 60 * MIN,        # 12:20 12:40 13:00
                       125 * MIN, 145 * MIN])                # 14:05 14:25
        self.assertEqual([1, 2, 3, 1, 2], self._indexes(),
                         '跨到下一个 2 小时档，次数从 1 重新开始')

    def test_count_keeps_rising_inside_one_band(self):
        """同一个档里满 3 次之后可以继续涨。"""
        self._fire_at([20 * MIN, 40 * MIN, 60 * MIN, 80 * MIN, 100 * MIN])
        self.assertEqual([1, 2, 3, 4, 5], self._indexes())

    def test_state_records_which_band_the_count_belongs_to(self):
        self._fire_at([20 * MIN, 125 * MIN])
        row = self.conn.execute('SELECT * FROM mic_silence_state').fetchone()
        self.assertEqual(self.NOON + 2 * 3600, row['band_start'],
                         '计数归属的档要记下来，否则没法判断该不该归零')
        self.assertEqual(1, row['alert_count'])

    def test_one_record_per_band_not_per_session(self):
        """6-8 满 3 次出一条；8-10 又满 3 次是另一条，互不相干。"""
        self._fire_at([20 * MIN, 40 * MIN, 60 * MIN,          # 档一满 3 次
                       125 * MIN, 145 * MIN, 165 * MIN])      # 档二满 3 次
        rows = self.conn.execute(
            'SELECT band_start, alert_count FROM mic_silence_records '
            'ORDER BY band_start').fetchall()
        self.assertEqual(2, len(rows), '两个档各一条')
        self.assertEqual([self.NOON, self.NOON + 2 * 3600],
                         [r['band_start'] for r in rows])
        self.assertEqual([3, 3], [r['alert_count'] for r in rows])

    def test_record_count_follows_further_silences_in_the_same_band(self):
        """满 3 次先建记录，同档再静默就把这条更新到最新，不新建。"""
        self._fire_at([20 * MIN, 40 * MIN, 60 * MIN, 80 * MIN, 100 * MIN])
        rows = self.conn.execute(
            'SELECT alert_count FROM mic_silence_records').fetchall()
        self.assertEqual(1, len(rows), '一个档最多一条记录')
        self.assertEqual(5, rows[0]['alert_count'], '次数要跟着涨到 5')

    def test_snapshot_covers_only_this_band(self):
        """快照只含这一档，不是整轮上麦。"""
        # 放在刚上麦那一分钟：再晚会把 last_spoke 推后，第一次告警凑不满
        # 阈值，整个节奏就错开了，测不到本来要测的东西。
        chat(self.conn, 'room-1', 'host-a', self.NOON + 60, '档一说的')
        self._fire_at([20 * MIN, 40 * MIN, 60 * MIN,
                       125 * MIN, 145 * MIN, 165 * MIN])
        rows = self.conn.execute(
            'SELECT band_start, chat_snapshot FROM mic_silence_records '
            'ORDER BY band_start').fetchall()
        band2 = json.loads(rows[1]['chat_snapshot'])
        self.assertEqual([('2027-01-02', '14:00', '16:00')],
                         [(b['date'], b['start'], b['end']) for b in band2['buckets']],
                         '第二条只含 14:00-16:00 这一档')
        self.assertEqual(0, sum(b['count'] for b in band2['buckets']),
                         '档一说的话不能算进档二')

    def test_snapshot_starts_at_mic_on_when_the_host_joined_mid_band(self):
        """13:00 上麦，这一档的快照从 13:00 算起，不是 12:00。"""
        # 13:20 / 13:40 / 13:55，三次都在 12:00-14:00 这一档内。
        # 用 60 分钟的话第三次正好落在 14:00——那是下一档的起点，计数会归零。
        self._fire_at([20 * MIN, 40 * MIN, 55 * MIN], mic_at=60 * MIN)
        snap = json.loads(self.conn.execute(
            'SELECT chat_snapshot FROM mic_silence_records').fetchone()[0])
        self.assertEqual('13:00:00', snap['mic_since'],
                         '他 12:00 根本不在麦上，别把那一小时算成他的静默')



class SameBandRejoinTests(unittest.TestCase):
    """同一档内掉麦重进，不能把已经存在的永久证据降级覆盖。

    upsert 的冲突键是 (厅, 主持, 档)，不含 mic_since。掉一次麦状态行就被
    删掉重建、mic_since 变新，同档再攒满 3 次会打到同一行上：次数从 4 掉回
    3、早段快照被换成只含后半段的新快照。而 mic_silence_records 是唯一
    豁免 7 天清理的表，interaction_log 过 7 天就没了——覆盖掉的内容
    永久不可恢复。

    掉麦不是罕见事件：进程重启后的孤儿清扫会删状态行（每次部署都走一遍），
    WS 重连导致麦位连续 3 次为空也会。

    时序要算准：16 分钟一档，首轮 +16/+32/+48/+64 攒到 4 次，+65..67 掉麦，
    +68 重进，再 +84/+100/+116 攒 3 次——全部落在 12:00-14:00 这一档内。
    用 20 分钟节奏的话第二轮会跨到下一档，压根打不到同一行上。
    """

    NOON = 1_798_862_400            # 2027-01-02 12:00:00 +08:00

    def setUp(self):
        self.conn = make_conn()
        keep_alive(self.conn, 'room-1', self.NOON - 3600, self.NOON + 6 * 3600)
        chat(self.conn, 'room-1', 'host-a', self.NOON + 30, '前半段说的')

    def tearDown(self):
        self.conn.close()

    def _run(self):
        scan(self.conn, self.NOON)
        for off in (16, 32, 48, 64):
            scan(self.conn, self.NOON + off * MIN)
        before = self.conn.execute(
            'SELECT * FROM mic_silence_records').fetchone()
        for tick in (65, 66, 67):
            scan(self.conn, self.NOON + tick * MIN, mic=())
        scan(self.conn, self.NOON + 68 * MIN)
        for off in (84, 100, 116):
            scan(self.conn, self.NOON + off * MIN)
        after = self.conn.execute(
            'SELECT * FROM mic_silence_records').fetchone()
        return before, after

    def test_rejoining_does_not_lower_the_count(self):
        before, after = self._run()
        self.assertEqual(4, before['alert_count'], '前置条件：首轮攒到 4 次')
        self.assertGreaterEqual(after['alert_count'], 4,
                                '次数只能往上走，不能因为掉麦重进而倒退')

    def test_rejoining_does_not_destroy_the_earlier_snapshot(self):
        _, after = self._run()
        texts = [m['text'] for b in json.loads(after['chat_snapshot'])['buckets']
                 for m in b['messages']]
        self.assertIn('前半段说的', texts,
                      '早段的发言是证据，不能被只含后半段的新快照顶掉')

    def test_record_stays_a_single_row_for_the_band(self):
        self._run()
        self.assertEqual(1, self.conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0],
            '一个档仍然只有一条')



class PipelineLivenessTests(unittest.TestCase):
    """corroboration 问的是「这条管道现在还活着吗」，不是「他说话了吗」。

    早先是「窗口内全厅有没有聊天」。窗口跟着快照走时太窄，改成固定回看
    2 小时又太宽——日志在回看窗口前半段还活着、后半段死掉，检查照样通过，
    于是在一段已经没有数据的时间上写出「他 45 分钟一句没说」的永久证据。
    而安静厅里长时间在麦，2 小时内真的没人说话，又会把该出的证据卡掉。

    改成看「这个厅最后一条记录距现在多久」，且不限 type——礼物、进场都
    算管道活着。这个判断跟厅里聊不聊天无关，两个方向的误判一起收敛。
    """

    NOON = 1_798_862_400

    def setUp(self):
        self.conn = make_conn()

    def tearDown(self):
        self.conn.close()

    def _alive(self, now):
        return mic_silence._pipeline_alive(self.conn, 'room-1', now)

    def test_recent_activity_means_alive(self):
        chat(self.conn, 'room-1', 'anyone', self.NOON, 'x')
        self.assertTrue(self._alive(self.NOON + 3 * MIN))

    def test_stale_activity_means_the_pipeline_died(self):
        """日志前半段还活着、后半段死掉——固定 2 小时回看会误判成活着。"""
        chat(self.conn, 'room-1', 'anyone', self.NOON, 'x')
        self.assertFalse(self._alive(self.NOON + 90 * MIN),
                         '90 分钟没有任何一条记录，管道已经死了')

    def test_non_chat_rows_also_count_as_alive(self):
        """礼物、进场都说明管道在工作，别只认聊天——安静厅里没人聊天
        是常态，拿它判断管道死活会把该出的证据全卡掉。"""
        self.conn.execute(
            "INSERT INTO interaction_log (room_id, sec_uid, display, type, "
            "content, timestamp) VALUES ('room-1','u','x','gift','玫瑰',?)",
            (self.NOON,))
        self.conn.commit()
        self.assertTrue(self._alive(self.NOON + 3 * MIN))

    def test_empty_log_means_dead(self):
        self.assertFalse(self._alive(self.NOON))

    def test_other_rooms_do_not_count(self):
        chat(self.conn, 'room-2', 'anyone', self.NOON, 'x')
        self.assertFalse(self._alive(self.NOON + 3 * MIN),
                         '别的厅活着不代表这个厅的管道活着')



class LegacySchemaMigrationTests(unittest.TestCase):
    """线上那张表已经有数据了，补列和建索引必须在真实形态上验过。

    make_conn 建的是全新库，CREATE TABLE 里本来就有 band_start，
    _ensure_column 必然空转——也就是说最危险的那条路径（有数据的旧表
    补列 + 在有数据的表上建部分唯一索引）此前一次都没跑过。
    """

    def _legacy_conn(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        # band_start 之前的表结构，原样照抄
        conn.executescript("""
            CREATE TABLE mic_silence_state (
                room_id TEXT NOT NULL, sec_uid TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '', douyin_id TEXT NOT NULL DEFAULT '',
                hall_name TEXT NOT NULL DEFAULT '', mic_since INTEGER NOT NULL,
                last_spoke_at INTEGER NOT NULL, alert_count INTEGER NOT NULL DEFAULT 0,
                last_alert_at INTEGER NOT NULL DEFAULT 0,
                missing_ticks INTEGER NOT NULL DEFAULT 0,
                evidence_done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, sec_uid));
            CREATE TABLE mic_silence_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL, douyin_id TEXT NOT NULL DEFAULT '',
                hall_name TEXT NOT NULL DEFAULT '', sec_uid TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '', beijing_date TEXT NOT NULL,
                mic_since INTEGER NOT NULL, created_at INTEGER NOT NULL,
                alert_count INTEGER NOT NULL,
                chat_snapshot TEXT NOT NULL DEFAULT '{}');
        """)
        # 同一个 (厅, 主持) 的多条老记录——正是部分唯一索引要放过的形态
        for n in range(3):
            conn.execute(
                "INSERT INTO mic_silence_records (room_id, sec_uid, nickname, "
                "beijing_date, mic_since, created_at, alert_count) "
                "VALUES ('room-1','host-a','甲','2026-08-20',?,?,3)",
                (1000 + n * 100, 1100 + n * 100))
        conn.execute(
            "INSERT INTO mic_silence_state (room_id, sec_uid, mic_since, "
            "last_spoke_at, alert_count) VALUES ('room-1','host-a',1,1,14)")
        conn.commit()
        return conn

    def test_columns_are_added_to_populated_tables(self):
        conn = self._legacy_conn()
        mic_silence.init_silence_schema(conn)
        for table in ('mic_silence_state', 'mic_silence_records'):
            cols = {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}
            self.assertIn('band_start', cols, table)
        conn.close()

    def test_old_rows_get_band_start_zero_and_are_left_alone(self):
        conn = self._legacy_conn()
        mic_silence.init_silence_schema(conn)
        rows = conn.execute(
            'SELECT id, band_start FROM mic_silence_records ORDER BY id').fetchall()
        self.assertEqual([(1, 0), (2, 0), (3, 0)],
                         [(r['id'], r['band_start']) for r in rows],
                         '老记录原样留着，band_start 默认 0')
        conn.close()

    def test_partial_index_tolerates_duplicate_legacy_rows(self):
        """老记录里同一个 (厅, 主持) 有好几条——唯一索引带 WHERE 才建得起来。"""
        conn = self._legacy_conn()
        mic_silence.init_silence_schema(conn)
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='mic_silence_records'")}
        self.assertIn('idx_silence_records_band', idx)
        conn.close()

    def test_migration_is_idempotent(self):
        conn = self._legacy_conn()
        for _ in range(3):
            mic_silence.init_silence_schema(conn)
        self.assertEqual(3, conn.execute(
            'SELECT COUNT(*) FROM mic_silence_records').fetchone()[0])
        conn.close()

    def test_legacy_state_row_does_not_carry_its_count_into_the_new_world(self):
        """老状态行 alert_count=14、band_start=0：新代码下第一次告警必须是第 1 次。"""
        conn = self._legacy_conn()
        conn.execute("""
            CREATE TABLE interaction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
                sec_uid TEXT NOT NULL DEFAULT '', display TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL, content TEXT DEFAULT '', timestamp INTEGER NOT NULL)
        """)
        mic_silence.init_silence_schema(conn)
        now = 1_798_862_400
        conn.execute(
            'UPDATE mic_silence_state SET mic_since = ?, last_spoke_at = ?',
            (now - 3600, now - 3600))
        conn.commit()
        fired = mic_silence.scan_room(
            conn, 'room-1', [{'sec_uid': 'host-a', 'nickname': '甲'}],
            'anchor-1', 'R', 'H', now, threshold=15 * MIN)
        self.assertEqual([1], [f['alert_index'] for f in fired],
                         '不能把老的 14 接着往上数')
        conn.close()


if __name__ == '__main__':
    unittest.main()
