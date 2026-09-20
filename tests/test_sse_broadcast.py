import queue
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import web_listener


class SseBroadcastTests(unittest.TestCase):
    """同一个厅必须支持多人同时看。

    原实现是单队列 + 连接版本号：后连的把先连的踢下线，
    而且事件被 queue.get() 取走即消失，两个订阅者会瓜分消息。
    多人场景下表现为「互相踢下线」和「各自只看到一半公屏」。
    """

    def _listener(self):
        listener = web_listener.RoomListener.__new__(web_listener.RoomListener)
        listener.room_id = 'room-a'
        listener.nickname = 'A厅'
        listener.running = True
        web_listener._init_listener_subscribers(listener)
        return listener

    def test_every_subscriber_receives_the_same_event(self):
        listener = self._listener()
        first = web_listener.subscribe_events(listener)
        second = web_listener.subscribe_events(listener)

        web_listener.publish_event(listener, {'type': 'chat', 'data': {'x': 1}})

        self.assertEqual('chat', first.get(timeout=1)['type'])
        self.assertEqual('chat', second.get(timeout=1)['type'])

    def test_a_new_subscriber_does_not_evict_existing_ones(self):
        listener = self._listener()
        first = web_listener.subscribe_events(listener)

        second = web_listener.subscribe_events(listener)
        web_listener.publish_event(listener, {'type': 'gift', 'data': {}})

        # 老连接仍然活着并能收到消息
        self.assertEqual('gift', first.get(timeout=1)['type'])
        self.assertEqual('gift', second.get(timeout=1)['type'])

    def test_unsubscribing_stops_delivery_without_affecting_others(self):
        listener = self._listener()
        first = web_listener.subscribe_events(listener)
        second = web_listener.subscribe_events(listener)

        web_listener.unsubscribe_events(listener, first)
        web_listener.publish_event(listener, {'type': 'enter', 'data': {}})

        self.assertEqual('enter', second.get(timeout=1)['type'])
        with self.assertRaises(queue.Empty):
            first.get(timeout=0.05)

    def test_a_stalled_subscriber_never_blocks_the_others(self):
        """某个浏览器卡住不取消息时，不能拖垮监听线程或别的订阅者。"""
        listener = self._listener()
        stalled = web_listener.subscribe_events(listener, maxsize=2)
        healthy = web_listener.subscribe_events(listener)

        for index in range(20):
            web_listener.publish_event(listener, {'type': 'chat', 'data': {'i': index}})

        # 健康订阅者仍拿得到最后一条
        received = []
        while True:
            try:
                received.append(healthy.get(timeout=0.05))
            except queue.Empty:
                break
        self.assertEqual(20, len(received))
        self.assertEqual(19, received[-1]['data']['i'])
        # 卡住的那个被限流，但不影响流程
        self.assertLessEqual(stalled.qsize(), 2)

    def test_no_generation_based_eviction_remains(self):
        """守住回归：版本号踢人机制必须彻底移除。"""
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'web_listener.py',
        )
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        self.assertNotIn('_sse_generations', source)


class SqliteWalTests(unittest.TestCase):
    """20 人一直在读、后台监听一直在写，默认回滚日志下读写互锁。"""

    def test_database_runs_in_wal_mode(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as handle:
            path = handle.name
        with patch.object(web_listener, '_DB_PATH', path):
            web_listener.init_db()
            conn = sqlite3.connect(path)
            try:
                mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            finally:
                conn.close()
        self.assertEqual('wal', str(mode).lower())


if __name__ == '__main__':
    unittest.main()
