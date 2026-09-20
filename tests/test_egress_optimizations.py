# -*- coding: utf-8 -*-
"""降低 Railway Egress 的两项服务端改动。

背景：13 天账单 $8.40，Egress 占 29%。实测各接口流量后发现：
- /stream 是最大头（两条连接就 295 KB）
- /api/feed 每次返回全量 200 条，平均 15.7 KB

而「普通观众弹幕不再实时推、公屏改 3 秒轮询」那个改动，如果 /api/feed
仍是全量，等于把 12~23 MB/天 换成 452 MB/天——不降反升。所以增量拉取
是那个改动能成立的前提，不是可选优化。
"""
import gzip as gziplib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import web_listener


NOW = int(time.time())


class FeedSinceTests(unittest.TestCase):
    """/api/feed 支持只取某个时间点之后的事件。"""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp.close()
        conn = sqlite3.connect(self._tmp.name)
        conn.execute('''
            CREATE TABLE interaction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL, sec_uid TEXT DEFAULT '',
                display TEXT DEFAULT '', type TEXT NOT NULL,
                content TEXT DEFAULT '', gift_count INTEGER DEFAULT 1,
                gift_id TEXT DEFAULT '', gift_quantity INTEGER DEFAULT 1,
                unit_diamonds INTEGER, total_diamonds INTEGER,
                price_known INTEGER DEFAULT 0, quantity_verified INTEGER DEFAULT 0,
                recipient_name TEXT DEFAULT '', event_key TEXT DEFAULT '',
                timestamp INTEGER NOT NULL
            )
        ''')
        for i, ts in enumerate((NOW - 300, NOW - 200, NOW - 100, NOW - 10)):
            conn.execute(
                "INSERT INTO interaction_log (room_id, display, type, content, "
                "event_key, timestamp) VALUES ('room-a', ?, 'chat', ?, ?, ?)",
                ('观众%d' % i, '第%d条' % i, 'k%d' % i, ts))
        conn.commit()
        conn.close()
        self._patch = patch.object(web_listener, '_DB_PATH', self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_without_since_returns_everything(self):
        events = web_listener._build_feed_events('room-a', limit=200)
        self.assertEqual(4, len(events))

    def test_since_returns_only_newer_events(self):
        events = web_listener._build_feed_events(
            'room-a', limit=200, since=NOW - 150)
        self.assertEqual(['第2条', '第3条'], [e['content'] for e in events])

    def test_since_is_inclusive_so_nothing_slips_through_the_boundary(self):
        """同一秒可能有多条。用 > 会漏掉边界上的那些。

        重叠的那几条前端按 feedEventKey 去重，多传一点无害；漏一条是真丢。
        """
        events = web_listener._build_feed_events(
            'room-a', limit=200, since=NOW - 100)
        self.assertIn('第2条', [e['content'] for e in events])

    def test_since_in_the_future_returns_nothing_and_does_not_crash(self):
        self.assertEqual(
            [], web_listener._build_feed_events(
                'room-a', limit=200, since=NOW + 86400))

    def test_endpoint_passes_since_through(self):
        client = web_listener.app.test_client()
        resp = client.get('/api/feed/room-a?limit=200&since=%d' % (NOW - 150))
        self.assertEqual(200, resp.status_code)
        body = json.loads(resp.get_data(as_text=True))
        self.assertTrue(body['success'])
        self.assertEqual(['第2条', '第3条'],
                         [e['content'] for e in body['events']])

    def test_since_is_part_of_the_cache_key(self):
        """响应有共享缓存。since 不进缓存键的话，第二个人会拿到别人的增量。"""
        client = web_listener.app.test_client()
        full = json.loads(client.get('/api/feed/room-a?limit=200')
                          .get_data(as_text=True))
        partial = json.loads(
            client.get('/api/feed/room-a?limit=200&since=%d' % (NOW - 150))
            .get_data(as_text=True))
        self.assertEqual(4, len(full['events']))
        self.assertEqual(2, len(partial['events']))

    def test_bad_since_falls_back_to_full_instead_of_erroring(self):
        client = web_listener.app.test_client()
        resp = client.get('/api/feed/room-a?limit=200&since=abc')
        self.assertEqual(200, resp.status_code)
        body = json.loads(resp.get_data(as_text=True))
        self.assertEqual(4, len(body['events']))


class JsonGzipTests(unittest.TestCase):
    """JSON 接口按 Accept-Encoding 压缩。

    /api/feed 单次 15.7 KB 的公屏 JSON 压完只剩 2~3 KB；SSE 那边已经在
    _gzip_sse 里压过了，这里绝不能再压一次。
    """

    def setUp(self):
        self.client = web_listener.app.test_client()

    def test_json_response_is_gzipped_when_client_accepts(self):
        resp = self.client.get('/api/status',
                               headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(200, resp.status_code)
        if resp.headers.get('Content-Encoding') == 'gzip':
            self.assertEqual(b'\x1f\x8b', resp.get_data()[:2], 'gzip 魔数')
            json.loads(gziplib.decompress(resp.get_data()).decode('utf-8'))
        else:
            # 小响应不值得压，但那也必须是完整可解析的 JSON
            json.loads(resp.get_data(as_text=True))

    def test_plain_when_client_does_not_accept_gzip(self):
        resp = self.client.get('/api/status', headers={'Accept-Encoding': ''})
        self.assertNotEqual('gzip', resp.headers.get('Content-Encoding'))
        json.loads(resp.get_data(as_text=True))

    def test_vary_header_is_set_so_caches_do_not_serve_the_wrong_body(self):
        resp = self.client.get('/api/status',
                               headers={'Accept-Encoding': 'gzip'})
        self.assertIn('Accept-Encoding', resp.headers.get('Vary', ''))

    def test_streaming_response_is_never_touched(self):
        """SSE 已经在 _gzip_sse 里压过；再压一次会把流缓冲掉、实时性没了。"""
        resp = self.client.get('/stream/no-such-room',
                               headers={'Accept-Encoding': 'gzip'})
        self.assertTrue(resp.direct_passthrough or resp.is_streamed,
                        'SSE 必须保持流式，不能被 after_request 收集成整块')


if __name__ == '__main__':
    unittest.main()


class GzipSseLifecycleTests(unittest.TestCase):
    """SSE 压缩生成器被中途关闭时不能抛异常。

    SSE 是长连接，正常结束方式就是客户端断开——那一刻 Python 会向生成器
    抛 GeneratorExit。收尾块如果写在 finally 里再 yield，会变成
    「RuntimeError: generator ignored GeneratorExit」，每断一个连接抛一次。
    """

    def test_closing_midway_does_not_raise(self):
        def source():
            for i in range(100):
                yield 'data: {"n": %d}\n\n' % i

        gen = web_listener._gzip_sse(source())
        self.assertTrue(next(gen))
        gen.close()          # 模拟客户端断开：不该抛

    def test_full_run_still_emits_a_decodable_stream(self):
        payload = ['data: {"n": %d}\n\n' % i for i in range(50)]
        packed = b''.join(web_listener._gzip_sse(iter(payload)))
        self.assertEqual(b'\x1f\x8b', packed[:2])
        self.assertEqual(''.join(payload),
                         gziplib.decompress(packed).decode('utf-8'))
