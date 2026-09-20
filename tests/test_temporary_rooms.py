import unittest
from unittest.mock import patch

import web_listener


class FakeProfile:
    def __init__(self, role):
        self.role = role
        self.id = 'u-' + role
        self.username = role
        self.status = 'active'


class FakeListener:
    """只提供判定临时厅所需的字段。"""

    def __init__(self, douyin_id='', nickname='厅'):
        self.douyin_id = douyin_id
        self.nickname = nickname
        self.running = True
        self.mystery_count = 0
        self.recent_mysteries = []

        class _Snap:
            @staticmethod
            def read():
                return {'records': []}
        self.online_snapshot = _Snap()


class TemporaryRoomDetectionTests(unittest.TestCase):
    """默认厅 = douyin_id 在 DEFAULT_WATCH_IDS 名单里；其余都是临时厅。

    这样手输的号如果正好在名单里，会被认作默认厅——否则会出现
    「停了它 60 秒后又被自动监听拉回来」的怪现象。
    """

    def setUp(self):
        self.listeners = {}
        self.patcher = patch.dict(web_listener.listeners, self.listeners, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _watch(self, ids):
        return patch.object(web_listener, 'default_watch_ids', lambda: ids)

    def test_room_started_by_configured_id_is_not_temporary(self):
        web_listener.listeners['r1'] = FakeListener(douyin_id='demo_hall_a')
        with self._watch(['demo_hall_a', 'demo_hall_b']):
            self.assertFalse(web_listener._is_temporary_room('r1'))

    def test_room_started_by_other_id_is_temporary(self):
        web_listener.listeners['r2'] = FakeListener(douyin_id='SomeoneElse')
        with self._watch(['demo_hall_a']):
            self.assertTrue(web_listener._is_temporary_room('r2'))

    def test_listener_without_douyin_id_is_temporary(self):
        web_listener.listeners['r3'] = FakeListener(douyin_id='')
        with self._watch(['demo_hall_a']):
            self.assertTrue(web_listener._is_temporary_room('r3'))

    def test_room_not_being_listened_is_not_temporary(self):
        # 已停止的厅 = 普通历史数据，谁都能查（规格：停掉后并入公共历史）
        with self._watch(['demo_hall_a']):
            self.assertFalse(web_listener._is_temporary_room('gone'))


class StatusFilteringTests(unittest.TestCase):
    """临时厅不能出现在非 super_admin 的房间列表里——不是前端藏，是后端不给。"""

    def setUp(self):
        self.patcher = patch.dict(web_listener.listeners, {
            'default-room': FakeListener('demo_hall_a', '默认厅'),
            'temp-room': FakeListener('Stranger', '临时厅'),
        }, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.watch = patch.object(
            web_listener, 'default_watch_ids', lambda: ['demo_hall_a'])
        self.watch.start()
        self.addCleanup(self.watch.stop)

    def _status_as(self, role):
        with web_listener.app.test_request_context('/api/status'):
            from flask import g
            g.current_user = FakeProfile(role)
            payload = web_listener.status()
        return payload.get_json() if hasattr(payload, 'get_json') else payload

    def test_super_admin_sees_both_and_temporary_is_flagged(self):
        data = self._status_as('super_admin')
        rooms = {item['room_id']: item for item in data['active']}
        self.assertEqual({'default-room', 'temp-room'}, set(rooms))
        self.assertTrue(rooms['temp-room']['is_temporary'])
        self.assertFalse(rooms['default-room']['is_temporary'])

    def test_member_does_not_see_temporary_room_at_all(self):
        data = self._status_as('member')
        self.assertEqual(['default-room'],
                         [item['room_id'] for item in data['active']])
        self.assertEqual(1, data['count'], 'count 也要跟着过滤后的数量')

    def test_admin_does_not_see_temporary_room_either(self):
        data = self._status_as('admin')
        self.assertEqual(['default-room'],
                         [item['room_id'] for item in data['active']])


class TemporaryRoomAccessGateTests(unittest.TestCase):
    """知道 room_id 也不行：非 super_admin 取临时厅的数据一律 403。"""

    def setUp(self):
        self.patcher = patch.dict(web_listener.listeners, {
            'default-room': FakeListener('demo_hall_a', '默认厅'),
            'temp-room': FakeListener('Stranger', '临时厅'),
        }, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.watch = patch.object(
            web_listener, 'default_watch_ids', lambda: ['demo_hall_a'])
        self.watch.start()
        self.addCleanup(self.watch.stop)

    def _deny_as(self, role, room_id):
        with web_listener.app.test_request_context('/'):
            from flask import g
            g.current_user = FakeProfile(role)
            return web_listener._deny_temporary_room(room_id)

    def test_member_is_denied_on_temporary_room(self):
        denied = self._deny_as('member', 'temp-room')
        self.assertIsNotNone(denied)
        self.assertEqual(403, denied[1])

    def test_admin_is_denied_on_temporary_room(self):
        self.assertIsNotNone(self._deny_as('admin', 'temp-room'))

    def test_super_admin_is_allowed_on_temporary_room(self):
        self.assertIsNone(self._deny_as('super_admin', 'temp-room'))

    def test_everyone_is_allowed_on_default_room(self):
        for role in ('member', 'admin', 'super_admin'):
            self.assertIsNone(self._deny_as(role, 'default-room'), role)

    def test_denied_request_leaves_nothing_in_the_shared_cache(self):
        """权限门必须在缓存之前。

        顺序写反（先算后鉴权）会让 super_admin 算出的临时厅数据被缓存，
        普通用户下一次请求就直接命中——那是数据泄露，不只是 bug。
        """
        web_listener._READ_CACHE.clear()
        before = web_listener._READ_CACHE.stats()['entries']
        with web_listener.app.test_request_context('/api/online/temp-room'):
            from flask import g
            g.current_user = FakeProfile('member')
            response = web_listener.online_audience('temp-room')
        status = response[1] if isinstance(response, tuple) else response.status_code
        self.assertEqual(403, status)
        self.assertEqual(
            before, web_listener._READ_CACHE.stats()['entries'],
            '被拒的请求不得在缓存里留下任何条目')


class SilenceRecordsRouteGateTests(unittest.TestCase):
    """/api/admin/silence/records/<room_id> 是唯一没接 _deny_temporary_room
    的 per-room admin 路由——其它 9 个都接了。今天的 UI 摸不到临时厅的
    铃铛，但 POST /api/start 带 douyin_id 或改 DEFAULT_WATCH_IDS 都能让
    一个厅变成临时厅，这条路由不能是漏网之鱼。"""

    def setUp(self):
        self.patcher = patch.dict(web_listener.listeners, {
            'default-room': FakeListener('demo_hall_a', '默认厅'),
            'temp-room': FakeListener('Stranger', '临时厅'),
        }, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.watch = patch.object(
            web_listener, 'default_watch_ids', lambda: ['demo_hall_a'])
        self.watch.start()
        self.addCleanup(self.watch.stop)

    def _records_as(self, role, room_id):
        with web_listener.app.test_request_context(
                f'/api/admin/silence/records/{room_id}'):
            from flask import g
            g.current_user = FakeProfile(role)
            return web_listener.silence_records(room_id)

    def test_admin_is_denied_on_temporary_room(self):
        response = self._records_as('admin', 'temp-room')
        status = response[1] if isinstance(response, tuple) else response.status_code
        self.assertEqual(403, status)

    def test_member_is_denied_on_temporary_room(self):
        response = self._records_as('member', 'temp-room')
        status = response[1] if isinstance(response, tuple) else response.status_code
        self.assertEqual(403, status)

    def test_super_admin_is_allowed_on_temporary_room(self):
        response = self._records_as('super_admin', 'temp-room')
        status = response[1] if isinstance(response, tuple) else response.status_code
        self.assertEqual(200, status)

    def test_everyone_is_allowed_on_default_room(self):
        for role in ('member', 'admin', 'super_admin'):
            response = self._records_as(role, 'default-room')
            status = response[1] if isinstance(response, tuple) else response.status_code
            self.assertEqual(200, status, role)


class SilenceWatchesHallFilteringTests(unittest.TestCase):
    """halls 是订阅列表页用来显示厅名的映射；/api/status 对非 super_admin
    藏临时厅，这里的 halls 是同一条规则该覆盖到的另一个入口。"""

    def setUp(self):
        self.patcher = patch.dict(web_listener.listeners, {
            'default-room': FakeListener('demo_hall_a', '默认厅'),
            'temp-room': FakeListener('Stranger', '临时厅'),
        }, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.watch = patch.object(
            web_listener, 'default_watch_ids', lambda: ['demo_hall_a'])
        self.watch.start()
        self.addCleanup(self.watch.stop)

    def _watches_as(self, role):
        with web_listener.app.test_request_context('/api/admin/silence/watches'):
            from flask import g
            g.current_user = FakeProfile(role)
            payload = web_listener.silence_watches()
        return payload.get_json() if hasattr(payload, 'get_json') else payload

    def test_super_admin_sees_temporary_hall(self):
        data = self._watches_as('super_admin')
        self.assertIn('Stranger', data['halls'])
        self.assertIn('demo_hall_a', data['halls'])

    def test_admin_does_not_see_temporary_hall(self):
        data = self._watches_as('admin')
        self.assertNotIn('Stranger', data['halls'], '非 super_admin 不该看到临时厅')
        self.assertIn('demo_hall_a', data['halls'], '常驻厅照常给')

    def test_member_does_not_see_temporary_hall(self):
        data = self._watches_as('member')
        self.assertNotIn('Stranger', data['halls'])


class SilenceSetWatchesGateTests(unittest.TestCase):
    """PUT /api/admin/silence/watches 不能让非 super_admin 靠自己拼
    douyin_id 订阅到临时厅——那是绕开 Fix F 那道墙的另一扇门：订阅上了，
    list_open_alerts 单靠 douyin_id 关联，会把临时厅的厅名、主持昵称、
    room_id 每 5 秒推给一个本来看不到这个厅的人。"""

    def setUp(self):
        self.patcher = patch.dict(web_listener.listeners, {
            'default-room': FakeListener('demo_hall_a', '默认厅'),
            'temp-room': FakeListener('Stranger', '临时厅'),
        }, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.watch = patch.object(
            web_listener, 'default_watch_ids', lambda: ['demo_hall_a'])
        self.watch.start()
        self.addCleanup(self.watch.stop)

    def _set_watches_as(self, role, douyin_ids):
        with web_listener.app.test_request_context(
                '/api/admin/silence/watches', method='PUT',
                json={'douyin_ids': douyin_ids}):
            from flask import g
            g.current_user = FakeProfile(role)
            payload = web_listener.silence_set_watches()
        return payload.get_json() if hasattr(payload, 'get_json') else payload

    def test_admin_cannot_subscribe_to_a_temporary_halls_douyin_id(self):
        data = self._set_watches_as('admin', ['Stranger'])
        self.assertNotIn('Stranger', data['douyin_ids'],
                          '非 super_admin 不能靠直接调接口订阅临时厅')

    def test_member_cannot_subscribe_to_a_temporary_halls_douyin_id(self):
        data = self._set_watches_as('member', ['Stranger'])
        self.assertNotIn('Stranger', data['douyin_ids'])

    def test_admin_can_still_subscribe_to_a_default_halls_douyin_id(self):
        data = self._set_watches_as('admin', ['demo_hall_a'])
        self.assertIn('demo_hall_a', data['douyin_ids'])

    def test_super_admin_can_subscribe_to_a_temporary_halls_douyin_id(self):
        data = self._set_watches_as('super_admin', ['Stranger'])
        self.assertIn('Stranger', data['douyin_ids'])

    def test_mixed_list_keeps_default_and_drops_temporary_for_non_super_admin(self):
        data = self._set_watches_as('admin', ['demo_hall_a', 'Stranger'])
        self.assertEqual(['demo_hall_a'], data['douyin_ids'])

    def test_unlisted_douyin_id_with_no_running_listener_is_unaffected(self):
        """这条规则只挡「当前正在跑的临时厅」，不能顺手连从未见过的号也拦掉——
        那是另一回事（那种号本来就订阅不到任何真实数据）。"""
        data = self._set_watches_as('admin', ['SomeoneNeverSeen'])
        self.assertIn('SomeoneNeverSeen', data['douyin_ids'])


class FakeRoomListener:
    """替掉真的 RoomListener：不开线程、不连抖音，只记住被怎么创建的。"""

    def __init__(self, room_id, nickname='', sec_uid='', anchor_id=''):
        self.room_id = room_id
        self.nickname = nickname
        self.sec_uid = sec_uid
        self.anchor_id = anchor_id
        self.douyin_id = ''
        self.running = False
        self.mystery_count = 0
        self.recent_mysteries = []

        class _Snap:
            @staticmethod
            def read():
                return {'records': []}
        self.online_snapshot = _Snap()

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class StartResponseFlagTests(unittest.TestCase):
    """开厅的响应必须带 is_temporary。

    前端只有拿到它才知道这个厅的叉号是「停止监听」还是「从我的页面收起」。
    缺了它，刚手动开的临时厅长得跟常驻厅一模一样，点叉只是把它从页面藏起来，
    后台监听照跑——人以为停了，其实没停。
    """

    def setUp(self):
        self.patcher = patch.dict(web_listener.listeners, {}, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.listener_patch = patch.object(
            web_listener, 'RoomListener', FakeRoomListener)
        self.listener_patch.start()
        self.addCleanup(self.listener_patch.stop)
        self.watch = patch.object(
            web_listener, 'default_watch_ids', lambda: ['demo_hall_a'])
        self.watch.start()
        self.addCleanup(self.watch.stop)

    def test_manually_started_room_is_reported_as_temporary(self):
        # 前端手输房间号时不带 douyin_id，正是这条路径
        result = web_listener.start_room_listener('r-manual', nickname='临时观察厅')
        self.assertTrue(result['success'])
        self.assertTrue(result['is_temporary'])

    def test_room_started_by_configured_id_is_reported_as_not_temporary(self):
        result = web_listener.start_room_listener(
            'r-default', nickname='默认厅', douyin_id='demo_hall_a')
        self.assertFalse(result['is_temporary'])

    def test_already_listening_response_also_carries_the_flag(self):
        """重复开同一个厅走的是「已在监听」这条早退分支，标记同样不能少。"""
        existing = FakeRoomListener('r-manual', '临时观察厅')
        existing.douyin_id = 'Stranger'
        existing.running = True
        web_listener.listeners['r-manual'] = existing

        result = web_listener.start_room_listener('r-manual', nickname='临时观察厅')
        self.assertTrue(result.get('already'))
        self.assertTrue(result['is_temporary'])

    def test_room_that_hit_the_limit_is_not_flagged_as_a_started_room(self):
        """开厅失败时没有厅可言，不能顺手报一个 is_temporary 让前端误当成开成功。"""
        with patch.object(web_listener, '_max_rooms', lambda: 0):
            result = web_listener.start_room_listener('r-manual')
        self.assertFalse(result['success'])
        self.assertNotIn('is_temporary', result)

    def test_newly_started_room_returns_douyin_id(self):
        """新启动厅时，响应必须带上传入的抖音号。

        静默铃铛靠它匹配订阅。没有的话，刚开的厅得等 refreshRooms 那 5 秒
        才能拿到 douyin_id，铃铛就不能立刻判断需不需要响。
        """
        result = web_listener.start_room_listener(
            'r-new', nickname='新厅', douyin_id='TestUser123')
        self.assertTrue(result['success'])
        self.assertEqual('TestUser123', result['douyin_id'])

    def test_already_listening_returns_stored_douyin_id(self):
        """重复开同一个厅时，响应必须带既有监听器的抖音号。

        这是「已在监听」的早退分支。如果不返回已存 douyin_id，
        前端就没法让静默铃铛生效（得等 5 秒 poll 才行）。
        """
        existing = FakeRoomListener('r-existing', '已开的厅')
        existing.douyin_id = 'ExistingUser456'
        existing.running = True
        web_listener.listeners['r-existing'] = existing

        result = web_listener.start_room_listener(
            'r-existing', nickname='已开的厅')
        self.assertTrue(result['success'])
        self.assertTrue(result.get('already'))
        self.assertEqual('ExistingUser456', result['douyin_id'])

    def test_already_listening_with_empty_douyin_id(self):
        """监听器的 douyin_id 为空字符串时，也要正常返回空值。

        防止把空值当作「未设置」来跳过响应字段——前端需要完整对象结构。
        """
        existing = FakeRoomListener('r-empty-id', '无号厅')
        existing.douyin_id = ''
        existing.running = True
        web_listener.listeners['r-empty-id'] = existing

        result = web_listener.start_room_listener('r-empty-id')
        self.assertTrue(result['success'])
        self.assertIn('douyin_id', result)
        self.assertEqual('', result['douyin_id'])


if __name__ == '__main__':
    unittest.main()
