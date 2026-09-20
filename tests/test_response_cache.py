import threading
import unittest

from response_cache import ResponseCache


class FakeClock:
    """手控时钟：TTL 行为必须可确定地测，不能靠 sleep。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ResponseCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = ResponseCache(ttl=1.0, clock=self.clock)
        self.calls = 0

    def compute(self, value='v'):
        def run():
            self.calls += 1
            return value
        return run

    def test_same_key_within_ttl_computes_once(self):
        self.assertEqual('v', self.cache.get_or_compute('k', self.compute()))
        self.assertEqual('v', self.cache.get_or_compute('k', self.compute()))
        self.assertEqual(1, self.calls, '同一秒内必须只算一次')

    def test_recomputes_after_ttl_expires(self):
        self.cache.get_or_compute('k', self.compute())
        self.clock.advance(1.5)
        self.cache.get_or_compute('k', self.compute())
        self.assertEqual(2, self.calls)

    def test_different_keys_are_isolated(self):
        self.cache.get_or_compute('a', self.compute('A'))
        self.assertEqual('B', self.cache.get_or_compute('b', self.compute('B')))
        self.assertEqual(2, self.calls)

    def test_exception_is_not_cached_and_propagates(self):
        def boom():
            self.calls += 1
            raise RuntimeError('炸了')
        with self.assertRaises(RuntimeError):
            self.cache.get_or_compute('k', boom)
        # 失败结果不能被缓存，下一次必须重新计算
        self.assertEqual('v', self.cache.get_or_compute('k', self.compute()))
        self.assertEqual(2, self.calls)

    def test_ttl_zero_disables_caching(self):
        cache = ResponseCache(ttl=0, clock=self.clock)
        cache.get_or_compute('k', self.compute())
        cache.get_or_compute('k', self.compute())
        self.assertEqual(2, self.calls, 'TTL=0 时每次都要重新计算')

    def test_clear_empties_cache(self):
        self.cache.get_or_compute('k', self.compute())
        self.cache.clear()
        self.cache.get_or_compute('k', self.compute())
        self.assertEqual(2, self.calls)
        self.assertEqual(0, self.cache.stats()['entries'] - 1)

    def test_entries_are_capped_so_memory_cannot_grow_unbounded(self):
        # room_id 每场直播都变，键会一直涨，必须有上限
        cache = ResponseCache(ttl=1.0, max_entries=8, clock=self.clock)
        for index in range(50):
            cache.get_or_compute(('room', index), lambda: index)
            self.clock.advance(2)
        self.assertLessEqual(cache.stats()['entries'], 8)


class SingleFlightTests(unittest.TestCase):
    """TTL 到期瞬间的并发是这个缓存的关键场景。

    没有单飞的话，100 个请求会同时发现缓存过期、同时去算，
    瞬间打出比不缓存还高的负载 —— 这个测试就是守住它。
    """

    def test_concurrent_callers_trigger_only_one_computation(self):
        cache = ResponseCache(ttl=60)
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_compute():
            calls.append(1)
            started.set()
            release.wait(2)
            return 'value'

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    cache.get_or_compute('k', slow_compute)
                )
            )
            for _ in range(12)
        ]
        for thread in threads:
            thread.start()
        started.wait(2)
        release.set()
        for thread in threads:
            thread.join(5)

        self.assertEqual(1, len(calls), '12 个并发只能触发一次计算')
        self.assertEqual(['value'] * 12, results)


class ResponseCacheConfigTests(unittest.TestCase):
    def test_default_when_unset(self):
        import runtime_config
        self.assertEqual(
            runtime_config.RESPONSE_CACHE_TTL_DEFAULT,
            runtime_config.resolve_response_cache_ttl({}),
        )

    def test_env_value_is_used(self):
        import runtime_config
        self.assertEqual(
            2.5, runtime_config.resolve_response_cache_ttl(
                {'RESPONSE_CACHE_TTL': '2.5'}))

    def test_zero_disables_cache_and_is_kept(self):
        import runtime_config
        self.assertEqual(
            0.0, runtime_config.resolve_response_cache_ttl(
                {'RESPONSE_CACHE_TTL': '0'}))

    def test_illegal_or_negative_falls_back_to_default(self):
        import runtime_config
        for raw in ('', 'abc', '-1'):
            self.assertEqual(
                runtime_config.RESPONSE_CACHE_TTL_DEFAULT,
                runtime_config.resolve_response_cache_ttl(
                    {'RESPONSE_CACHE_TTL': raw}),
                raw,
            )


class EndpointCacheIntegrationTests(unittest.TestCase):
    """接口级：同一个厅连续请求只应触发一次计算。"""

    def test_cached_json_reuses_payload_within_ttl(self):
        import web_listener
        calls = []

        def build():
            calls.append(1)
            return {'success': True, 'n': len(calls)}, 200

        with web_listener.app.test_request_context('/'):
            first = web_listener._cached_json(('t', 'room-a'), build)
            second = web_listener._cached_json(('t', 'room-a'), build)
        self.assertEqual(1, len(calls))
        self.assertEqual(200, first[1])
        self.assertEqual(200, second[1])

    def test_cached_json_keeps_status_code_for_error_payload(self):
        import web_listener

        def build():
            return {'success': False, 'error': '未找到该直播间监听器'}, 404

        with web_listener.app.test_request_context('/'):
            response = web_listener._cached_json(('t', 'room-missing'), build)
        self.assertEqual(404, response[1])

    def test_different_rooms_do_not_share_cache(self):
        import web_listener
        calls = []

        def build():
            calls.append(1)
            return {'success': True}, 200

        with web_listener.app.test_request_context('/'):
            web_listener._cached_json(('t', 'room-a'), build)
            web_listener._cached_json(('t', 'room-b'), build)
        self.assertEqual(2, len(calls))

    def test_weekly_key_includes_week_start_so_periods_do_not_mix(self):
        """带查询参数的接口，参数必须进键——漏了就会把上周数据当本周返回。"""
        import web_listener
        seen = []

        def build_for(tag):
            def build():
                seen.append(tag)
                return {'success': True, 'tag': tag}, 200
            return build

        with web_listener.app.test_request_context('/'):
            a = web_listener._cached_json(('weekly', 'r', ''), build_for('本周'))
            b = web_listener._cached_json(('weekly', 'r', '123'), build_for('上周'))
        self.assertEqual(['本周', '上周'], seen)
        self.assertNotEqual(a[0].get_json()['tag'], b[0].get_json()['tag'])


if __name__ == '__main__':
    unittest.main()
