import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


import leaderboards
import web_listener


NOW = 1_798_822_800  # 2027-01-02 01:00:00 +08:00
DAY_START = 1_798_819_200  # 2027-01-02 00:00:00 +08:00
DAY_END = 1_798_905_600  # 2027-01-03 00:00:00 +08:00
WEEK_END = 1_799_424_000  # 2027-01-09 00:00:00 +08:00


class LeaderboardDatabaseMixin:
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / 'history.db')
        self.db_patch = patch.object(web_listener, '_DB_PATH', self.db_path)
        self.db_patch.start()
        web_listener._init_db()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        if hasattr(leaderboards, 'init_leaderboard_schema'):
            leaderboards.init_leaderboard_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.db_patch.stop()
        self.tempdir.cleanup()

    def save_gift(
            self, room_id, sender_key, sender_name, recipient_key,
            recipient_name, tickets, timestamp, event_key):
        web_listener._save_interaction(
            room_id, sender_key, sender_name, 'gift',
            content='测试礼物', gift_count=1, gift_id='gift-rank',
            gift_quantity=1, unit_diamonds=tickets,
            total_diamonds=tickets, price_known=tickets is not None,
            quantity_verified=True, recipient_key=recipient_key,
            recipient_name=recipient_name, event_key=event_key,
            ticket_count=tickets,
            ticket_source='gift_price' if tickets is not None else 'unknown',
            timestamp=timestamp,
        )
        return int(self.conn.execute(
            'SELECT id FROM interaction_log WHERE room_id = ? AND event_key = ?',
            (room_id, event_key),
        ).fetchone()['id'])


class DailyBoardTests(LeaderboardDatabaseMixin, unittest.TestCase):
    def require(self, name):
        self.assertTrue(
            hasattr(leaderboards, name),
            f'leaderboards.{name} must implement the daily-board contract',
        )
        return getattr(leaderboards, name)

    def test_beijing_day_bounds_are_natural_midnight(self):
        bounds = self.require('beijing_day_bounds')
        self.assertEqual((DAY_START, DAY_END), bounds(NOW))

    def test_daily_boards_use_today_and_exact_room_only(self):
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            1000, DAY_START - 60, 'yesterday-a',
        )
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            50, DAY_START + 60, 'today-a',
        )
        self.save_gift(
            'room-a', 'viewer-b', '游客乙', 'host-b', '主持乙',
            100, DAY_START + 120, 'today-b',
        )
        self.save_gift(
            'room-b', 'viewer-z', '游客错厅', 'host-z', '主持错厅',
            9999, DAY_START + 180, 'other-room',
        )

        query = self.require('query_daily_boards')
        result = query(self.conn, 'room-a', NOW)

        self.assertEqual(DAY_START, result['day_start'])
        self.assertEqual(DAY_END, result['day_end'])
        self.assertEqual(
            [('游客乙', 100), ('游客甲', 50)],
            [(row['display'], row['tickets']) for row in result['visitors']],
        )
        self.assertEqual(
            [('主持乙', 100), ('主持甲', 50)],
            [(row['display'], row['tickets']) for row in result['hosts']],
        )

    def test_recipient_adjustment_changes_host_board_not_visitor_board(self):
        interaction_id = self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'room:room-a', '大头',
            1000, DAY_START + 60, 'wrong-host',
        )
        query = self.require('query_daily_boards')
        adjust = self.require('set_recipient_adjustment')

        before = query(self.conn, 'room-a', NOW)
        adjust(
            self.conn, interaction_id, 'host-real', '主持真名',
            '礼物刷给大头，改给主持真名', '管理员甲', NOW,
        )
        after = query(self.conn, 'room-a', NOW)

        self.assertEqual(before['visitors'], after['visitors'])
        self.assertEqual('大头', before['hosts'][0]['display'])
        self.assertEqual('主持真名', after['hosts'][0]['display'])
        self.assertEqual(1000, after['hosts'][0]['tickets'])

    def test_unknown_ticket_gifts_do_not_enter_either_board(self):
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            None, DAY_START + 60, 'unknown-ticket',
        )
        self.save_gift(
            'room-a', 'viewer-b', '游客乙', 'host-b', '主持乙',
            1, DAY_START + 120, 'known-ticket',
        )

        result = self.require('query_daily_boards')(self.conn, 'room-a', NOW)

        self.assertEqual(['游客乙'], [row['display'] for row in result['visitors']])
        self.assertEqual(['主持乙'], [row['display'] for row in result['hosts']])


class LeaderboardRouteTests(LeaderboardDatabaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.interaction_id = self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'room:room-a', '大头',
            688, int(time.time()), 'route-gift',
        )
        self.client = web_listener.app.test_client()

    def test_daily_rank_route_returns_public_room_scoped_contract(self):
        response = self.client.get('/api/daily_rank/room-a')

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual('room-a', payload['room_id'])
        self.assertEqual('beijing_today', payload['scope'])
        self.assertEqual('游客甲', payload['visitors'][0]['display'])
        self.assertEqual('大头', payload['hosts'][0]['display'])

    def test_assignment_route_requires_note_and_updates_effective_host(self):
        invalid = self.client.put(
            f'/api/admin/gift_assignments/{self.interaction_id}',
            json={
                'recipient_key': 'host-real',
                'recipient_name': '主持真名',
                'note': '   ',
            },
        )
        saved = self.client.put(
            f'/api/admin/gift_assignments/{self.interaction_id}',
            json={
                'recipient_key': 'host-real',
                'recipient_name': '主持真名',
                'note': '刷错给大头',
                'actor': '管理员甲',
            },
        )

        self.assertEqual(400, invalid.status_code)
        self.assertEqual(200, saved.status_code)
        self.assertEqual('主持真名', saved.get_json()['adjustment']['recipient_name'])


class WeeklyBoardTests(LeaderboardDatabaseMixin, unittest.TestCase):
    def require(self, name):
        self.assertTrue(
            hasattr(leaderboards, name),
            f'leaderboards.{name} must implement the weekly-board contract',
        )
        return getattr(leaderboards, name)

    def test_beijing_week_runs_saturday_to_saturday(self):
        bounds = self.require('beijing_week_bounds')
        self.assertEqual((DAY_START, WEEK_END), bounds(DAY_START + 3600))
        self.assertEqual(
            (DAY_START - 7 * 86400, DAY_START),
            bounds(DAY_START - 1),
        )

    def test_current_week_adjustment_changes_hosts_not_visitors(self):
        wrong_id = self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'room:room-a', '大头',
            1000, DAY_START + 3600, 'weekly-wrong',
        )
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-b', '主持乙',
            500, DAY_START + 3700, 'weekly-right',
        )
        query = self.require('query_live_weekly_boards')
        before = query(self.conn, 'room-a', DAY_START + 7200)

        self.require('set_recipient_adjustment')(
            self.conn, wrong_id, 'host-a', '主持甲',
            '刷给大头，归主持甲', '管理员甲', DAY_START + 7100,
        )
        after = query(self.conn, 'room-a', DAY_START + 7200)

        self.assertEqual(
            [('游客甲', 1500)],
            [(row['display'], row['tickets']) for row in before['visitors']],
        )
        self.assertEqual(before['visitors'], after['visitors'])
        self.assertEqual(
            [('主持甲', 1000), ('主持乙', 500)],
            [(row['display'], row['tickets']) for row in after['hosts']],
        )

    def test_host_large_detail_uses_fixed_two_hour_slot_and_keeps_all_tickets(self):
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            12000, DAY_START + 10 * 60, 'large-a-1',
        )
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            18000, DAY_START + 90 * 60, 'large-a-2',
        )
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            29999, DAY_START + 3 * 3600, 'below-next-slot',
        )
        self.save_gift(
            'room-a', 'viewer-b', '游客乙', 'host-a', '主持甲',
            20000, DAY_START + 60 * 60, 'other-viewer',
        )

        result = self.require('query_live_weekly_boards')(
            self.conn, 'room-a', DAY_START + 4 * 3600,
        )

        host = result['hosts'][0]
        self.assertEqual(79999, host['tickets'])
        self.assertEqual(1, len(host['large_details']))
        detail = host['large_details'][0]
        self.assertEqual('游客甲', detail['visitor_display'])
        self.assertEqual(DAY_START, detail['slot_start'])
        self.assertEqual(DAY_START + 2 * 3600, detail['slot_end'])
        self.assertEqual(30000, detail['tickets'])

    def test_locked_week_is_permanent_after_source_adjustment_and_deletion(self):
        interaction_id = self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            5000, DAY_START + 3600, 'locked-source',
        )
        lock = self.require('lock_completed_weeks')
        query = self.require('query_weekly_board')

        locked_periods = lock(self.conn, WEEK_END + 3600)
        before = query(
            self.conn, 'room-a', WEEK_END + 3600,
            week_start=DAY_START,
        )
        self.require('set_recipient_adjustment')(
            self.conn, interaction_id, 'host-b', '主持乙',
            '锁定后普通修正', '管理员甲', WEEK_END + 3700,
        )
        self.conn.execute('DELETE FROM interaction_log WHERE id = ?', (interaction_id,))
        self.conn.commit()
        after = query(
            self.conn, 'room-a', WEEK_END + 7200,
            week_start=DAY_START,
        )

        self.assertEqual(1, len(locked_periods))
        self.assertTrue(before['locked'])
        self.assertEqual('主持甲', before['hosts'][0]['display'])
        self.assertEqual(before, after)


class WeeklyRouteTests(LeaderboardDatabaseMixin, unittest.TestCase):
    def test_weekly_routes_return_current_and_locked_periods(self):
        self.save_gift(
            'room-a', 'viewer-a', '游客甲', 'host-a', '主持甲',
            100, int(time.time()), 'weekly-route-current',
        )
        client = web_listener.app.test_client()
        current = client.get('/api/admin/weekly_rank/room-a')
        periods = client.get('/api/admin/weekly_rank/room-a/periods')

        self.assertEqual(200, current.status_code)
        self.assertFalse(current.get_json()['locked'])
        self.assertEqual('主持甲', current.get_json()['hosts'][0]['display'])
        self.assertEqual(200, periods.status_code)
        self.assertTrue(periods.get_json()['success'])


WEEK_START = 1_798_819_200  # 2027-01-02 00:00:00 +08:00（该周周六 = 本周起点）


class WeeklyByHallTests(LeaderboardDatabaseMixin, unittest.TestCase):
    """周榜必须按「厅」聚合，而不是按 room_id。

    抖音每开一场直播就换一个新 room_id（实测：每个 room_id 的数据只覆盖一天），
    所以按 room_id 过滤的「周榜」只可能捞到当前这一场 = 当天，
    表现为周榜和日榜数字完全一样。厅名（如 demo_003）跨场次稳定，用它做聚合键。
    """

    def require(self, name):
        self.assertTrue(hasattr(leaderboards, name), f'leaderboards.{name} 未实现')
        return getattr(leaderboards, name)

    def test_hall_rooms_group_sessions_of_same_hall(self):
        self.require('record_room_hall')(self.conn, 'room-mon', 'demo_003', NOW)
        self.require('record_room_hall')(self.conn, 'room-tue', 'demo_003', NOW)
        self.require('record_room_hall')(self.conn, 'room-other', 'RUYUE·契约', NOW)

        rooms = self.require('hall_room_ids')(self.conn, 'room-mon')

        self.assertEqual({'room-mon', 'room-tue'}, set(rooms))

    def test_unknown_or_blank_hall_never_merges_rooms(self):
        # 解析不出厅名时必须退回「只看自己」，绝不能把所有未知厅并成一个
        self.require('record_room_hall')(self.conn, 'room-blank', '', NOW)
        self.assertEqual(['room-x'], self.require('hall_room_ids')(self.conn, 'room-x'))
        self.assertEqual(['room-blank'],
                         self.require('hall_room_ids')(self.conn, 'room-blank'))

    def test_live_weekly_sums_across_sessions_while_daily_stays_one_session(self):
        self.require('record_room_hall')(self.conn, 'room-sat', 'demo_003', NOW)
        self.require('record_room_hall')(self.conn, 'room-now', 'demo_003', NOW)
        # 本周内的上一场（周六），和今天这一场
        self.save_gift('room-sat', 'viewer-a', '游客甲', 'host-1', '主持甲',
                       500, WEEK_START + 3600, 'w-old')
        self.save_gift('room-now', 'viewer-a', '游客甲', 'host-1', '主持甲',
                       200, NOW - 600, 'w-new')

        weekly = self.require('query_live_weekly_boards')(self.conn, 'room-now', NOW)
        daily = self.require('query_daily_boards')(self.conn, 'room-now', NOW)

        self.assertEqual(700, weekly['visitors'][0]['tickets'], '周榜要跨场次累计')
        self.assertEqual(700, weekly['hosts'][0]['tickets'])
        self.assertEqual(200, daily['visitors'][0]['tickets'], '日榜仍只算当前这一场')

    def test_weekly_excludes_other_hall(self):
        self.require('record_room_hall')(self.conn, 'room-now', 'demo_003', NOW)
        self.require('record_room_hall')(self.conn, 'room-alien', 'RUYUE·契约', NOW)
        self.save_gift('room-now', 'viewer-a', '游客甲', 'host-1', '主持甲',
                       200, NOW - 600, 'w-mine')
        self.save_gift('room-alien', 'viewer-b', '游客乙', 'host-2', '主持乙',
                       999, NOW - 600, 'w-alien')

        weekly = self.require('query_live_weekly_boards')(self.conn, 'room-now', NOW)

        self.assertEqual(['游客甲'], [r['display'] for r in weekly['visitors']])

    def test_backfill_maps_history_rooms_from_mystery_records(self):
        # 历史 room_id 在监听里没登记过，只能从既有资料反推厅名
        self.conn.execute(
            "INSERT INTO mystery_records (sec_uid, display, nickname, last_room_id) "
            "VALUES ('sec-1', '甲', 'RUYUE·暧昧', 'room-history')"
        )
        self.conn.commit()

        self.require('backfill_room_halls')(self.conn)
        self.require('record_room_hall')(self.conn, 'room-today', 'RUYUE·暧昧', NOW)

        self.assertEqual({'room-history', 'room-today'},
                         set(self.require('hall_room_ids')(self.conn, 'room-today')))

    def test_locked_week_is_archived_per_hall_and_readable_by_any_session(self):
        self.require('record_room_hall')(self.conn, 'room-lastweek', 'RUYUE·心愿', NOW)
        self.require('record_room_hall')(self.conn, 'room-thisweek', 'RUYUE·心愿', NOW)
        prev_start = WEEK_START - 7 * 86400
        self.save_gift('room-lastweek', 'viewer-a', '游客甲', 'host-1', '主持甲',
                       300, prev_start + 3600, 'lw-1')

        self.require('lock_completed_weeks')(self.conn, NOW)
        # 用「本周的新场次」也要能读到上周归档（room_id 已经变了）
        locked = self.require('query_weekly_board')(
            self.conn, 'room-thisweek', NOW, week_start=prev_start)

        self.assertTrue(locked['locked'])
        self.assertEqual(300, locked['visitors'][0]['tickets'])
        periods = self.require('list_week_periods')(self.conn, 'room-thisweek')
        self.assertEqual([prev_start], [p['period_start'] for p in periods])



class IdentityMergeTests(LeaderboardDatabaseMixin, unittest.TestCase):
    """同一个人戴马甲和不戴马甲，榜单上必须并成一行。

    线上实测：示例用户乙（抖音号 demo_user_a，身份 706）戴马甲时显示 dou2494080、
    脱了马甲显示本名，两次送礼分别记在 webcast_uid 和 sec_uid 上，榜单按
    原始键聚合就拆成了 #14（3531 票）和 #20（2897 票）两行。而系统本来
    就知道他俩是一个人——#14 括号里能写出「示例用户乙」正是因为这条关联。

    拆得最狠的一个身份被拆成 17 个键、合计 11 万票，榜单头部整个是错的。

    关联本身是可信的：查过拆得最狠的三个身份，标识符全是 id
    （sec_uid ×1 + webcast_uid ×N + numeric_uid ×1），没有一个是按显示名
    关联的——正是「同一个人每次进厅换一个会话令牌」的形态。
    """

    SEC_UID = 'MS4wLjABAAAA_SYNTHETIC_0011_TEST_ONLY'
    WEBCAST = 'MS4wLjPaRgy5-20MUOUtkwtnK-i0lfZF7-le_9'

    def link_identity(self, identity_id, real_name, pairs):
        """把若干标识符挂到同一个身份上，模拟高等级礼物揭示后的关联。"""
        self.conn.execute(
            'INSERT OR REPLACE INTO identity_profiles '
            '(id, canonical_sec_uid, real_name, douyin_id, source) '
            'VALUES (?, ?, ?, ?, ?)',
            (identity_id, pairs[0][1], real_name, 'demo_user_a', 'high_level_gift'))
        for kind, value in pairs:
            self.conn.execute(
                'INSERT OR REPLACE INTO identity_identifiers '
                '(identity_id, identifier_type, identifier_value, source, '
                ' first_seen, last_seen) VALUES (?, ?, ?, ?, 0, 0)',
                (identity_id, kind, value, 'high_level_gift'))
        self.conn.commit()

    def _split_gifts(self):
        # 戴马甲那一轮：sec_uid 为空，代码退回存 webcast_uid
        self.save_gift('room-a', self.WEBCAST, 'dou2494080',
                       'host-a', '主持甲', 3531, NOW, 'g-masked')
        # 脱了马甲那一轮
        self.save_gift('room-a', self.SEC_UID, '示例用户乙',
                       'host-a', '主持甲', 2897, NOW + 60, 'g-plain')

    def test_daily_board_splits_without_the_link(self):
        """前置条件：没有关联时确实是两行——否则下面那条测的就不是合并。"""
        self._split_gifts()
        boards = leaderboards.query_daily_boards(self.conn, 'room-a', NOW + 120)
        self.assertEqual(2, len(boards['visitors']))

    def test_daily_board_merges_the_two_disguises(self):
        self._split_gifts()
        self.link_identity(706, '示例用户乙',
                           [('sec_uid', self.SEC_UID),
                            ('webcast_uid', self.WEBCAST)])
        rows = leaderboards.query_daily_boards(
            self.conn, 'room-a', NOW + 120)['visitors']
        self.assertEqual(1, len(rows), '同一个人只该有一行')
        self.assertEqual(3531 + 2897, rows[0]['tickets'], '票数要相加')
        self.assertEqual(2, rows[0]['gift_events'], '次数也要相加')
        self.assertEqual(2, rows[0]['merged_keys'], '要标出这行是两个键并来的')
        # display 保留原样、不用真名顶替：马甲名是拿去跟抖音 App 对照的
        # 线索，抹掉之后界面上也再看不出这行是合并来的。真名由既有的括号
        # 机制（attach_real_names）带出来。
        self.assertIn(rows[0]['display'], ('示例用户乙', 'dou2494080'))

    def test_weekly_board_merges_too(self):
        self._split_gifts()
        self.link_identity(706, '示例用户乙',
                           [('sec_uid', self.SEC_UID),
                            ('webcast_uid', self.WEBCAST)])
        rows = leaderboards.query_live_weekly_boards(
            self.conn, 'room-a', NOW + 120)['visitors']
        self.assertEqual(1, len(rows))
        self.assertEqual(3531 + 2897, rows[0]['tickets'])

    def test_merged_row_keeps_the_latest_gift_time(self):
        self._split_gifts()
        self.link_identity(706, '示例用户乙',
                           [('sec_uid', self.SEC_UID),
                            ('webcast_uid', self.WEBCAST)])
        rows = leaderboards.query_daily_boards(
            self.conn, 'room-a', NOW + 120)['visitors']
        self.assertEqual(NOW + 60, rows[0]['last_gift_at'])

    def test_merging_reorders_the_board(self):
        """合并之后票数变大，名次要跟着往上走——不能还挂在原来的位置。"""
        self.save_gift('room-a', 'big-spender', '大哥', 'host-a', '主持甲',
                       5000, NOW, 'g-big')
        self._split_gifts()
        self.link_identity(706, '示例用户乙',
                           [('sec_uid', self.SEC_UID),
                            ('webcast_uid', self.WEBCAST)])
        rows = leaderboards.query_daily_boards(
            self.conn, 'room-a', NOW + 120)['visitors']
        self.assertEqual([6428, 5000], [r['tickets'] for r in rows],
                         '6428 > 5000，合并后要排到第一')
        self.assertEqual([1, 2], [r['rank'] for r in rows], '名次要重排')
        self.assertEqual('大哥', rows[1]['display'], '没合并的那行不受影响')

    def test_unlinked_people_are_left_alone(self):
        """没被关联过的照旧——抖音匿名机制下我们看不到就是看不到。"""
        self.save_gift('room-a', '', '神秘人123', 'host-a', '主持甲',
                       100, NOW, 'g-anon1')
        self.save_gift('room-a', '', '神秘人456', 'host-a', '主持甲',
                       200, NOW + 10, 'g-anon2')
        rows = leaderboards.query_daily_boards(
            self.conn, 'room-a', NOW + 120)['visitors']
        self.assertEqual(2, len(rows), '两个不同的匿名者不能被并到一起')

    def test_hosts_are_merged_by_identity_as_well(self):
        """收礼主持同理——Ray 要求全部榜单都改。"""
        self.save_gift('room-a', 'v1', '游客甲', self.WEBCAST, '主持马甲',
                       300, NOW, 'h-1')
        self.save_gift('room-a', 'v2', '游客乙', self.SEC_UID, '主持本名',
                       400, NOW + 10, 'h-2')
        self.link_identity(706, '示例用户乙',
                           [('sec_uid', self.SEC_UID),
                            ('webcast_uid', self.WEBCAST)])
        rows = leaderboards.query_daily_boards(
            self.conn, 'room-a', NOW + 120)['hosts']
        self.assertEqual(1, len(rows), '同一个主持只该有一行')
        self.assertEqual(700, rows[0]['tickets'])



class LargeDetailIdentityTests(LeaderboardDatabaseMixin, unittest.TestCase):
    """周榜主持的「大额时段」明细也要按身份合并。

    这里踩过一个会炸生产的坑：明细按原始 host_key 分组，主持行合并后
    键变成 'id:N'，我第一版只是把几个列表 extend 到一起——两个规范化到
    同一身份的原始键各带一条相同 (visitor_key, slot_start) 的明细时，
    weekly_host_large_details 的主键 (period_id, host_key, visitor_key,
    slot_start) 会撞，lock_completed_weeks 抛 IntegrityError，整个归档
    回滚、所有厅的周榜 500，而且那一周永远归不了档（period_start 只看
    前一周，过了周六就再也不看它）。
    """

    HOST_SEC = 'MS4wHOSTsec'
    HOST_MASK = 'MS4wHOSTmask'
    V_SEC = 'MS4wVsec'
    V_MASK = 'MS4wVmask'

    def link(self, identity_id, real_name, values):
        self.conn.execute(
            'INSERT OR REPLACE INTO identity_profiles '
            '(id, canonical_sec_uid, real_name, source) VALUES (?, ?, ?, ?)',
            (identity_id, values[0], real_name, 'high_level_gift'))
        for n, value in enumerate(values):
            self.conn.execute(
                'INSERT OR REPLACE INTO identity_identifiers '
                '(identity_id, identifier_type, identifier_value, source, '
                ' first_seen, last_seen) VALUES (?, ?, ?, ?, 0, 0)',
                (identity_id, 'sec_uid' if n == 0 else 'webcast_uid',
                 value, 'high_level_gift'))
        self.conn.commit()

    def test_same_slot_details_are_summed_not_concatenated(self):
        """同一个 (访客, 时段) 在主持的两个马甲下各有一条——必须合成一条。

        不合的话 lock_completed_weeks 插入时会撞主键。
        """
        self.save_gift('room-a', self.V_SEC, '大哥', self.HOST_SEC, '主持本名',
                       40000, NOW, 'd-1')
        self.save_gift('room-a', self.V_SEC, '大哥', self.HOST_MASK, '主持马甲',
                       35000, NOW + 60, 'd-2')
        self.link(706, '主持真名', [self.HOST_SEC, self.HOST_MASK])
        hosts = leaderboards.query_live_weekly_boards(
            self.conn, 'room-a', NOW + 120)['hosts']
        details = hosts[0]['large_details']
        self.assertEqual(1, len(details), '同一个 (访客, 时段) 只该有一条明细')
        self.assertEqual(75000, details[0]['tickets'], '票数要相加')

    def test_locking_the_week_does_not_blow_up(self):
        """端到端：归档不能抛 IntegrityError。"""
        self.save_gift('room-a', self.V_SEC, '大哥', self.HOST_SEC, '主持本名',
                       40000, NOW, 'd-1')
        self.save_gift('room-a', self.V_SEC, '大哥', self.HOST_MASK, '主持马甲',
                       35000, NOW + 60, 'd-2')
        self.link(706, '主持真名', [self.HOST_SEC, self.HOST_MASK])
        # 归档跑的是「上一周」，把时间推到下周
        leaderboards.lock_completed_weeks(self.conn, NOW + 8 * 86400)
        rows = self.conn.execute(
            'SELECT COUNT(*) FROM weekly_host_large_details').fetchone()[0]
        self.assertGreaterEqual(rows, 1, '归档要成功落库')

    def test_detail_visitor_keys_are_merged_too(self):
        """访客在明细里也会戴两个马甲——不合并的话，明细跟上面那行自相矛盾。"""
        self.save_gift('room-a', self.V_SEC, '大哥本名', self.HOST_SEC, '主持',
                       32000, NOW, 'v-1')
        self.save_gift('room-a', self.V_MASK, '大哥马甲', self.HOST_SEC, '主持',
                       31000, NOW + 60, 'v-2')
        self.link(900, '大哥真名', [self.V_SEC, self.V_MASK])
        details = leaderboards.query_live_weekly_boards(
            self.conn, 'room-a', NOW + 120)['hosts'][0]['large_details']
        self.assertEqual(1, len(details), '同一个访客只该有一条')
        self.assertEqual(63000, details[0]['tickets'])

    def test_threshold_applies_after_merging(self):
        """两个马甲各 20000、合起来 40000：合并后过阈值，就该出现在明细里。

        阈值原来在 SQL 的 HAVING 里、合并之前就筛掉了，于是主持行显示
        40000 票、明细却是空的——正是这次改动要消灭的「拆开算」。
        """
        self.save_gift('room-a', self.V_SEC, '大哥本名', self.HOST_SEC, '主持',
                       20000, NOW, 't-1')
        self.save_gift('room-a', self.V_MASK, '大哥马甲', self.HOST_SEC, '主持',
                       20000, NOW + 60, 't-2')
        self.link(900, '大哥真名', [self.V_SEC, self.V_MASK])
        details = leaderboards.query_live_weekly_boards(
            self.conn, 'room-a', NOW + 120)['hosts'][0]['large_details']
        self.assertEqual(1, len(details))
        self.assertEqual(40000, details[0]['tickets'])

    def test_below_threshold_after_merging_still_excluded(self):
        self.save_gift('room-a', self.V_SEC, '甲', self.HOST_SEC, '主持',
                       10000, NOW, 's-1')
        self.save_gift('room-a', self.V_MASK, '乙', self.HOST_SEC, '主持',
                       10000, NOW + 60, 's-2')
        self.link(900, '大哥真名', [self.V_SEC, self.V_MASK])
        details = leaderboards.query_live_weekly_boards(
            self.conn, 'room-a', NOW + 120)['hosts'][0]['large_details']
        self.assertEqual([], details, '合并后 20000 仍不到 30000，不该出现')



class MergeSafetyTests(LeaderboardDatabaseMixin, unittest.TestCase):
    """合并的几条保守边界。合错人比拆开更糟——拆开只是低估，合错是冤枉人。"""

    def test_ambiguous_identifier_is_left_unmerged(self):
        """一个标识符挂到两个身份上时，跟身份库自己的规矩一致：不认人。

        identity_registry.lookup_identity 遇到这种情况直接判冲突、拒绝识别。
        榜单这边要是自作主张挑一个，一条坏关联就能把两个人的票并成一行。
        """
        for identity_id in (1, 2):
            self.conn.execute(
                'INSERT OR REPLACE INTO identity_profiles '
                '(id, canonical_sec_uid, real_name, source) VALUES (?,?,?,?)',
                (identity_id, 'c%d' % identity_id, '身份%d' % identity_id, 'x'))
        # 同一个值挂在两个身份下（类型不同，所以 UNIQUE 拦不住）
        self.conn.execute(
            'INSERT INTO identity_identifiers (identity_id, identifier_type, '
            'identifier_value, source, first_seen, last_seen) '
            "VALUES (1,'sec_uid','AMBIG','x',0,0)")
        self.conn.execute(
            'INSERT INTO identity_identifiers (identity_id, identifier_type, '
            'identifier_value, source, first_seen, last_seen) '
            "VALUES (2,'webcast_uid','AMBIG','x',0,0)")
        self.conn.commit()
        canonical, _ = leaderboards.identity_key_map(self.conn, ['AMBIG'])
        self.assertEqual({}, canonical, '有歧义就别合，交给人去修身份库')

    def test_display_keeps_the_mask_name(self):
        """合并后不能把马甲名抹掉——那是 Ray 拿去跟抖音 App 对照的线索，
        而且抹掉之后界面上再也看不出「这行是合并来的」。真名由既有的
        括号机制带出来，不需要顶替 display。"""
        self.conn.execute(
            'INSERT OR REPLACE INTO identity_profiles '
            '(id, canonical_sec_uid, real_name, source) VALUES (706,?,?,?)',
            ('SEC', '示例用户乙', 'high_level_gift'))
        self.conn.execute(
            'INSERT INTO identity_identifiers (identity_id, identifier_type, '
            'identifier_value, source, first_seen, last_seen) '
            "VALUES (706,'sec_uid','SEC','x',0,0)")
        self.conn.commit()
        self.save_gift('room-a', 'SEC', 'dou2494080', 'host-a', '主持甲',
                       100, NOW, 'm-1')
        rows = leaderboards.query_daily_boards(
            self.conn, 'room-a', NOW + 60)['visitors']
        self.assertEqual('dou2494080', rows[0]['display'],
                         '单独一行、没合并，display 更不该被换掉')

    def test_merged_row_reports_how_many_keys_went_in(self):
        """界面上要看得出这行是合并来的，否则坏关联无从发现。"""
        self.conn.execute(
            'INSERT OR REPLACE INTO identity_profiles '
            '(id, canonical_sec_uid, real_name, source) VALUES (706,?,?,?)',
            ('SEC', '示例用户乙', 'high_level_gift'))
        for kind, value in (('sec_uid', 'SEC'), ('webcast_uid', 'MASK')):
            self.conn.execute(
                'INSERT INTO identity_identifiers (identity_id, '
                'identifier_type, identifier_value, source, first_seen, '
                "last_seen) VALUES (706,?,?,'x',0,0)", (kind, value))
        self.conn.commit()
        self.save_gift('room-a', 'SEC', '示例用户乙', 'host-a', '主持甲',
                       100, NOW, 'n-1')
        self.save_gift('room-a', 'MASK', 'dou2494080', 'host-a', '主持甲',
                       200, NOW + 10, 'n-2')
        rows = leaderboards.query_daily_boards(
            self.conn, 'room-a', NOW + 60)['visitors']
        self.assertEqual(300, rows[0]['tickets'])
        self.assertEqual(2, rows[0]['merged_keys'], '合了几个键要说出来')
        self.assertEqual(1, rows[1]['merged_keys'] if len(rows) > 1 else 1)



class DetailPrecisionTests(LeaderboardDatabaseMixin, unittest.TestCase):
    """明细的门槛必须在合并之后精确判，不能先在 SQL 里粗筛。

    我上一版在 SQL 里按门槛的十分之一粗筛，注释里写「就算凑够十个也到
    不了门槛」——那是把成分数当成 ≤10 了，而线上实测有拆成 17 个键的
    身份。后果有两种，都是静默的：低于粗筛线的成分被无声丢掉（主持行
    42500、明细却写 40000），或者成分够多时整条明细消失。

    线上量过：全部 5 个厅一周的 (主持,访客,时段) 组合一共 5495 个，
    单厅一千出头，不设下限也完全拉得动。
    """

    HOST = 'MS4wHOST'
    V_SEC = 'MS4wVsec'
    V_MASK = 'MS4wVmask'

    def link(self, identity_id, real_name, values):
        self.conn.execute(
            'INSERT OR REPLACE INTO identity_profiles '
            '(id, canonical_sec_uid, real_name, source) VALUES (?,?,?,?)',
            (identity_id, values[0], real_name, 'high_level_gift'))
        for n, value in enumerate(values):
            self.conn.execute(
                'INSERT OR REPLACE INTO identity_identifiers '
                '(identity_id, identifier_type, identifier_value, source, '
                ' first_seen, last_seen) VALUES (?,?,?,?,0,0)',
                (identity_id, 'sec_uid' if n == 0 else 'webcast_uid',
                 value, 'high_level_gift'))
        self.conn.commit()

    def _details(self):
        return leaderboards.query_live_weekly_boards(
            self.conn, 'room-a', NOW + 300)['hosts'][0]['large_details']

    def test_small_component_is_not_dropped(self):
        """40000 + 2500：不能只算 40000。"""
        self.save_gift('room-a', self.V_SEC, '本名', self.HOST, '主持',
                       40000, NOW, 'p-1')
        self.save_gift('room-a', self.V_MASK, '马甲', self.HOST, '主持',
                       2500, NOW + 60, 'p-2')
        self.link(900, '大哥', [self.V_SEC, self.V_MASK])
        details = self._details()
        self.assertEqual(1, len(details))
        self.assertEqual(42500, details[0]['tickets'],
                         '低于粗筛线的成分也要算进去')

    def test_many_small_components_still_reach_the_threshold(self):
        """11 个各 2999：合起来 32989 过门槛，明细不能是空的。"""
        values = ['MS4wV%02d' % n for n in range(11)]
        for n, value in enumerate(values):
            self.save_gift('room-a', value, '马甲%d' % n, self.HOST, '主持',
                           2999, NOW + n, 'q-%d' % n)
        self.link(901, '大哥', values)
        details = self._details()
        self.assertEqual(1, len(details))
        self.assertEqual(11 * 2999, details[0]['tickets'])

    def test_detail_keeps_the_mask_name_not_the_real_name(self):
        """明细的显示名也要保留马甲名。

        主行留马甲名、点开的明细却是真名，同一屏自相矛盾；而且明细会被
        归档进 weekly_host_large_details，那一周就永久定型了，改展示层
        也救不回来。
        """
        self.save_gift('room-a', self.V_SEC, '马甲名字', self.HOST, '主持',
                       40000, NOW, 'r-1')
        self.link(902, '真名张三', [self.V_SEC])
        self.assertEqual('马甲名字', self._details()[0]['visitor_display'])


class ArchiveIsolationTests(LeaderboardDatabaseMixin, unittest.TestCase):
    """一个厅归档失败不能打挂所有厅。

    上一轮那条 Critical 的后果之所以那么大，就是因为 per-hall 循环没有
    守卫：一个厅抛异常，整趟归档回滚、所有厅的周榜一起 500，而且
    period_start 只看紧邻的上一周，过了周六那一周就永远归不了档。
    触发器修掉了，这个放大器也得收。
    """

    def test_one_bad_hall_does_not_abort_the_others(self):
        self.save_gift('room-a', 'v1', '甲', 'h1', '主持一',
                       100, NOW, 'a-1')
        self.save_gift('room-b', 'v2', '乙', 'h2', '主持二',
                       200, NOW, 'b-1')
        leaderboards.record_room_hall(self.conn, 'room-a', '厅甲', NOW)
        leaderboards.record_room_hall(self.conn, 'room-b', '厅乙', NOW)
        real = leaderboards._query_period_boards
        calls = []

        def flaky(conn, room_ids, period_start, cutoff):
            calls.append(tuple(room_ids))
            if 'room-a' in room_ids:
                raise sqlite3.IntegrityError('模拟某个厅炸了')
            return real(conn, room_ids, period_start, cutoff)

        with patch.object(leaderboards, '_query_period_boards', flaky):
            leaderboards.lock_completed_weeks(self.conn, NOW + 8 * 86400)
        self.assertEqual(2, len(calls), '炸掉的厅不能让后面的厅不跑')
        halls = [r[0] for r in self.conn.execute(
            'SELECT hall_name FROM weekly_periods')]
        self.assertIn('厅乙', halls, '好的厅要照常归档')


if __name__ == '__main__':
    unittest.main()
