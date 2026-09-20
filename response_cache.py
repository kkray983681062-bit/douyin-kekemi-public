"""只读接口的秒级共享缓存：同一个厅同一秒内只算一次，所有查看者共用。

背景：原本每个查看者各算一遍 —— 100 个人看同一个厅，服务端就把同一份在线名单
和榜单算 100 遍，结果完全一样。缓存后 CPU 从 O(人数 × 厅数) 变成 O(厅数)。

当前是单进程多线程（gunicorn 1 worker），内存天然共享，所以一个字典就够，
不需要 Redis。将来若拆成多进程，最坏情况是每个进程各算一次，仍然安全。

只放与「谁在看」无关的只读结果；带用户身份的数据一律不进这里。
"""

import threading
import time


class ResponseCache:
    def __init__(self, ttl=1.0, max_entries=512, clock=time.monotonic):
        self._ttl = max(0.0, float(ttl))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries = {}          # key -> (expires_at, value)
        self._locks = {}            # key -> Lock（单飞用）
        self._guard = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _fresh(self, key, now):
        """返回 (命中, 值)；未命中时值为 None。"""
        entry = self._entries.get(key)
        if entry is not None and entry[0] > now:
            return True, entry[1]
        return False, None

    def _lock_for(self, key):
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _purge(self, now):
        """清掉过期项；仍超上限就按到期时间丢最老的。

        键的数量随房间数增长（room_id 每场直播都变），不设上限会一直涨。
        """
        with self._guard:
            for key in [k for k, v in self._entries.items() if v[0] <= now]:
                self._entries.pop(key, None)
                self._locks.pop(key, None)
            overflow = len(self._entries) - self._max_entries
            if overflow > 0:
                ordered = sorted(self._entries.items(), key=lambda kv: kv[1][0])
                for key, _ in ordered[:overflow]:
                    self._entries.pop(key, None)
                    self._locks.pop(key, None)

    def get_or_compute(self, key, compute):
        if self._ttl <= 0:
            return compute()

        now = self._clock()
        hit, value = self._fresh(key, now)
        if hit:
            self._hits += 1
            return value

        # 单飞：同一个键同时只允许一个线程去算，其余在这里排队。
        lock = self._lock_for(key)
        with lock:
            # 拿到锁后必须重查 —— 排队期间前一个线程很可能已经算好了。
            now = self._clock()
            hit, value = self._fresh(key, now)
            if hit:
                self._hits += 1
                return value
            self._misses += 1
            value = compute()      # 抛异常就让它抛出去，绝不缓存失败结果
            self._entries[key] = (self._clock() + self._ttl, value)

        if len(self._entries) > self._max_entries:
            self._purge(self._clock())
        return value

    def clear(self):
        """清空缓存。生产不用；测试需要它来隔离用例——缓存是进程级的，
        多个用例用同一个 room_id 在同一秒内跑，后面的会吃到前面的结果。"""
        with self._guard:
            self._entries.clear()
            self._locks.clear()

    def stats(self):
        return {
            'entries': len(self._entries),
            'hits': self._hits,
            'misses': self._misses,
            'ttl': self._ttl,
        }
