import math
import threading
import time
from collections import defaultdict, deque


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after):
        super().__init__('rate limit exceeded')
        self.retry_after = max(1, int(math.ceil(retry_after)))


class FixedWindowRateLimiter:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._attempts = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key, limit, window_seconds):
        now = self.clock()
        window_start = now - window_seconds
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= window_start:
                attempts.popleft()
            if len(attempts) >= limit:
                raise RateLimitExceeded(attempts[0] + window_seconds - now)
            attempts.append(now)
