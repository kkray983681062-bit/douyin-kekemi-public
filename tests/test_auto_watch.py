import unittest
from unittest.mock import patch

import web_listener


class AutoWatchTests(unittest.TestCase):
    """默认厅自动监听：服务在线期间让配置的厅「尽可能在线」。

    - 名单用抖音号配置（room_id 每场直播都会变，不能写死）
    - 已在监听的厅零请求；只轮询掉线的
    - 一轮补齐所有待接的厅（顺序解析、非并发），启动即秒级就位
    - 主播没开播就等下一轮；下播后由 room_offline 停掉监听，回等待队列
    """

    def setUp(self):
        self.original = dict(web_listener.listeners)
        web_listener.listeners.clear()

    def tearDown(self):
        web_listener.listeners.clear()
        web_listener.listeners.update(self.original)

    def test_default_rooms_come_from_environment(self):
        with patch.dict('os.environ', {'DEFAULT_WATCH_IDS': 'a1, b2 ,c3'}):
            self.assertEqual(['a1', 'b2', 'c3'], web_listener.default_watch_ids())

    def test_no_environment_means_no_auto_watch(self):
        with patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('DEFAULT_WATCH_IDS', None)
            self.assertEqual([], web_listener.default_watch_ids())

    def test_tick_starts_a_live_room(self):
        calls = []
        with patch.object(web_listener, 'default_watch_ids', return_value=['ruyue1']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          return_value={'success': True, 'room_id': '999',
                                        'live_status': 1, 'nickname': 'A厅',
                                        'sec_uid': 'sec-a'}) as resolve, \
             patch.object(web_listener, 'start_room_listener',
                          side_effect=lambda **kw: calls.append(kw)) as start:
            web_listener.auto_watch_tick()

        resolve.assert_called_once_with('ruyue1')
        self.assertEqual(1, len(calls))
        self.assertEqual('999', calls[0]['room_id'])

    def test_tick_skips_an_offline_streamer(self):
        calls = []
        with patch.object(web_listener, 'default_watch_ids', return_value=['ruyue1']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          return_value={'success': True, 'room_id': '0',
                                        'live_status': 0, 'nickname': 'A厅',
                                        'sec_uid': 'sec-a'}), \
             patch.object(web_listener, 'start_room_listener',
                          side_effect=lambda **kw: calls.append(kw)):
            web_listener.auto_watch_tick()

        self.assertEqual([], calls)

    def test_tick_makes_zero_requests_when_everything_is_watched(self):
        listener = web_listener.RoomListener.__new__(web_listener.RoomListener)
        listener.running = True
        listener.sec_uid = 'sec-a'
        listener.douyin_id = 'ruyue1'
        web_listener.listeners['999'] = listener

        with patch.object(web_listener, 'default_watch_ids', return_value=['ruyue1']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id') as resolve:
            web_listener.auto_watch_tick()

        resolve.assert_not_called()

    def test_tick_checks_every_pending_id_in_one_round(self):
        """一轮把所有待接的厅都查一遍，重启后秒级就位而不是等好几分钟。

        仍顺序解析（非并发），5 个号就是 5 次普通请求，量很小；
        换来的是服务上线即尽快把默认厅补齐，不再每 60 秒只接一个。
        """
        with patch.object(web_listener, 'default_watch_ids',
                          return_value=['id1', 'id2', 'id3']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          return_value={'success': True, 'room_id': '0',
                                        'live_status': 0}) as resolve:
            web_listener.auto_watch_tick()

        self.assertEqual(3, resolve.call_count)
        checked = [c.args[0] for c in resolve.call_args_list]
        self.assertEqual(['id1', 'id2', 'id3'], checked)

    def test_tick_only_queries_unwatched_rooms(self):
        """已在监听的厅零请求，一轮只补没在监听的那些。"""
        listener = web_listener.RoomListener.__new__(web_listener.RoomListener)
        listener.running = True
        listener.sec_uid = 'sec-2'
        listener.douyin_id = 'id2'
        web_listener.listeners['999'] = listener

        with patch.object(web_listener, 'default_watch_ids',
                          return_value=['id1', 'id2', 'id3']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          return_value={'success': True, 'room_id': '0',
                                        'live_status': 0}) as resolve:
            web_listener.auto_watch_tick()

        checked = [c.args[0] for c in resolve.call_args_list]
        self.assertEqual(['id1', 'id3'], checked)

    def test_startup_watches_immediately_not_after_first_sleep(self):
        """启动即补齐默认厅，不能先睡 60 秒才接第一个。"""
        started = {}
        with patch.object(web_listener, 'default_watch_ids',
                          return_value=['id1']), \
             patch.object(web_listener, 'auto_watch_tick',
                          side_effect=lambda: started.setdefault('ticked', True)), \
             patch.object(web_listener.threading, 'Thread'):
            web_listener.start_auto_watch_thread()

        self.assertTrue(started.get('ticked'),
                        '启动时应立即 tick 一次，而不是等后台线程首个 sleep 之后')

    def test_startup_tick_runs_real_tick_without_crashing(self):
        """启动首轮跑的是真 tick（不 mock），必须能访问 listeners 等全局，
        不能因模块加载顺序抛 NameError —— 这正是把 tick 提前后踩到的坑。"""
        with patch.object(web_listener, 'default_watch_ids',
                          return_value=['id1']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          return_value={'success': True, 'room_id': '0',
                                        'live_status': 0}), \
             patch.object(web_listener.threading, 'Thread'):
            # 不 patch auto_watch_tick：让启动真的调用它一次
            web_listener.start_auto_watch_thread()  # 不抛异常即通过

    def test_resolve_failure_never_crashes_the_loop(self):
        with patch.object(web_listener, 'default_watch_ids', return_value=['bad']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          side_effect=RuntimeError('network down')):
            web_listener.auto_watch_tick()  # 不抛异常即通过


if __name__ == '__main__':
    unittest.main()


class AnchorIdFromDiscoveryTests(unittest.TestCase):
    """主播的数字 uid 在发现厅时就拿到了，别再丢掉。

    2026-08-23 线上事故：四个厅重开换 room_id 之后，在线名单一直卡在
    「等待直播间主播身份」。fetch_online_audience 要 anchor_id + sec_anchor_id
    两样，而 sec_anchor_id 在建监听器时就有（发现厅时带的），anchor_id 只能
    靠麦上名单里认出主播、或抓直播间网页兜底——名单那条消息新厅不发，
    网页抓取的五条正则又被抖音改版打挂了四条（失败还被 except: pass 吞掉）。

    其实发现厅调的那个用户资料接口返回里就带着 uid，代码只取了
    room_id / live_status / sec_uid，把它扔了。捡回来即可，
    而且主播是固定的，一劳永逸。
    """

    def test_tick_passes_anchor_id_to_the_listener(self):
        calls = []
        with patch.object(web_listener, 'default_watch_ids',
                          return_value=['ruyue1']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          return_value={'success': True, 'room_id': '999',
                                        'live_status': 1, 'nickname': 'A厅',
                                        'sec_uid': 'sec-a',
                                        'anchor_id': '3393530667741963'}), \
             patch.object(web_listener, 'start_room_listener',
                          side_effect=lambda **kw: calls.append(kw)):
            web_listener.auto_watch_tick()

        self.assertEqual('3393530667741963', calls[0]['anchor_id'])

    def test_missing_anchor_id_does_not_break_the_call(self):
        """老的返回值没有这个字段，不能因此起不了监听。"""
        calls = []
        with patch.object(web_listener, 'default_watch_ids',
                          return_value=['ruyue1']), \
             patch.object(web_listener, 'get_room_id_by_douyin_id',
                          return_value={'success': True, 'room_id': '999',
                                        'live_status': 1, 'nickname': 'A厅',
                                        'sec_uid': 'sec-a'}), \
             patch.object(web_listener, 'start_room_listener',
                          side_effect=lambda **kw: calls.append(kw)):
            web_listener.auto_watch_tick()

        self.assertEqual(1, len(calls))
        self.assertEqual('', calls[0]['anchor_id'])

    def test_listener_keeps_the_anchor_id_it_was_built_with(self):
        listener = web_listener.RoomListener(
            'room-1', 'A厅', 'sec-a', anchor_id='3393530667741963')
        self.assertEqual('3393530667741963', listener.anchor_id)
        self.assertEqual('sec-a', listener.sec_anchor_id)
        # 两样都齐 -> 不必再去抓网页兜底
        info = listener._anchor_info()
        self.assertEqual('3393530667741963', info['anchor_id'])
        self.assertEqual('sec-a', info['sec_anchor_id'])
