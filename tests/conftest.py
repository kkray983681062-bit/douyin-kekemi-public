"""测试级夹具。

只读接口的共享缓存是进程级的：多个用例常用同一个 room_id、在同一秒内连续跑，
后面的用例会吃到前面的缓存结果。生产上「同一厅 1 秒内不重算」正是想要的行为，
测试里则必须隔离，所以每个用例前清一次。
"""

import pytest

import web_listener


@pytest.fixture(autouse=True)
def _clear_read_cache():
    web_listener._READ_CACHE.clear()
    yield
