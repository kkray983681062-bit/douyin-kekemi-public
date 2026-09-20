import json
import unittest

import web_listener


class ETagTests(unittest.TestCase):
    """榜单没变化时返回 304 空响应，别把同一份 35 KB 每 2 秒重发一遍。

    直播间不是每 2 秒都有人送礼，大部分轮询请求的数据其实一模一样。
    """

    def setUp(self):
        web_listener._READ_CACHE.clear()

    def test_response_carries_etag_and_revalidate_header(self):
        with web_listener.app.test_request_context('/'):
            response, status = web_listener._cached_json(
                ('t', 'r1'), lambda: ({'success': True, 'rows': [1, 2, 3]}, 200)
            )
        self.assertEqual(200, status)
        self.assertTrue(response.headers.get('ETag'), '必须带 ETag')
        # no-cache 的含义是「可以缓存但每次都要回来问」，正是我们要的
        self.assertIn('no-cache', response.headers.get('Cache-Control', ''))

    def test_same_content_returns_304_with_empty_body(self):
        payload = ({'success': True, 'rows': [1, 2, 3]}, 200)
        with web_listener.app.test_request_context('/'):
            first, _ = web_listener._cached_json(('t', 'r2'), lambda: payload)
            etag = first.headers['ETag']
        web_listener._READ_CACHE.clear()
        with web_listener.app.test_request_context(
                '/', headers={'If-None-Match': etag}):
            second, status = web_listener._cached_json(('t', 'r2'), lambda: payload)
        self.assertEqual(304, status)
        self.assertEqual(b'', second.get_data(), '304 不能带 body')
        self.assertEqual(etag, second.headers.get('ETag'), '304 也要带 ETag')

    def test_changed_content_returns_200_with_new_etag(self):
        with web_listener.app.test_request_context('/'):
            first, _ = web_listener._cached_json(
                ('t', 'r3'), lambda: ({'rows': [1]}, 200))
            old_etag = first.headers['ETag']
        web_listener._READ_CACHE.clear()
        with web_listener.app.test_request_context(
                '/', headers={'If-None-Match': old_etag}):
            second, status = web_listener._cached_json(
                ('t', 'r3'), lambda: ({'rows': [1, 2]}, 200))
        self.assertEqual(200, status)
        self.assertNotEqual(old_etag, second.headers['ETag'])
        self.assertEqual({'rows': [1, 2]}, json.loads(second.get_data()))

    def test_error_payload_is_not_etagged(self):
        # 404 之类不参与条件请求，避免把错误状态缓存住
        with web_listener.app.test_request_context('/'):
            response, status = web_listener._cached_json(
                ('t', 'r4'), lambda: ({'success': False}, 404))
        self.assertEqual(404, status)
        self.assertIsNone(response.headers.get('ETag'))

    def test_etag_differs_between_rooms(self):
        with web_listener.app.test_request_context('/'):
            a, _ = web_listener._cached_json(('t', 'ra'), lambda: ({'v': 'a'}, 200))
            b, _ = web_listener._cached_json(('t', 'rb'), lambda: ({'v': 'b'}, 200))
        self.assertNotEqual(a.headers['ETag'], b.headers['ETag'])


class DailyRankConditionalTests(unittest.TestCase):
    """接口层：第二次带 If-None-Match 请求同一个日榜应拿到 304。"""

    def setUp(self):
        web_listener._READ_CACHE.clear()
        self.client = web_listener.app.test_client()

    def test_daily_rank_second_request_is_304(self):
        first = self.client.get('/api/daily_rank/room-etag')
        self.assertEqual(200, first.status_code)
        etag = first.headers.get('ETag')
        self.assertTrue(etag)
        second = self.client.get(
            '/api/daily_rank/room-etag', headers={'If-None-Match': etag})
        self.assertEqual(304, second.status_code)
        self.assertEqual(b'', second.get_data())


if __name__ == '__main__':
    unittest.main()
