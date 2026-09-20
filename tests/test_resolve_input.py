import unittest
from unittest.mock import patch

import web_listener


class DouyinIdShapeTests(unittest.TestCase):
    """抖音号允许点和连字符，不是只有字母数字下划线。

    2026-08-23：Ray 反馈 demo.1 一直卡在「解析中」。原来判断「这是不是
    抖音号」的正则是 ^[a-zA-Z0-9_]+$，不认点——demo.1 直接掉进后面的
    链接解析分支，全都不匹配，最后返回「无法解析链接或抖音号」。
    实测这个号在抖音那边完全正常（room_id 1000000000000000020，
    直播中，在线名单能拉到 15 人）。

    已配置的几个厅（demo_hall_a / demo_hall_b / demo_hall_c）都没有点，
    所以这个坑一直没暴露。
    """

    def test_dotted_id_is_recognised(self):
        self.assertTrue(web_listener._looks_like_douyin_id('demo.1'))

    def test_existing_ids_still_recognised(self):
        for value in ('demo_hall_a', 'demo_hall_b', 'demo_hall_c', 'ruyue.888',
                      'a-b_c.1', '123456'):
            self.assertTrue(web_listener._looks_like_douyin_id(value), value)

    def test_links_are_not_treated_as_ids(self):
        """带点了就更要挡住链接，否则会被当成抖音号丢给用户信息接口。"""
        for value in ('https://live.douyin.com/123', 'http://x.com',
                      'v.douyin.com/abc', 'www.douyin.com/user/xx',
                      'live.douyin.com', ''):
            self.assertFalse(web_listener._looks_like_douyin_id(value), value)

    def test_dotted_id_takes_the_douyin_id_branch(self):
        """端到端：带点的号要走用户信息接口，不能掉进链接分支。"""
        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        def fake_get(url, **kwargs):
            calls.append(url)
            if 'v2/user/info' in url:
                return FakeResponse({'status_code': 0, 'user_info': {
                    'sec_uid': 'sec-x', 'nickname': 'demo_010'}})
            return FakeResponse({'status_code': 0, 'user': {
                'room_id': 1000000000000000020, 'live_status': 1,
                'uid': '2769016234444315'}})

        with patch.object(web_listener.requests, 'get', side_effect=fake_get):
            result = web_listener.get_room_id_by_douyin_id('demo.1')

        self.assertTrue(result['success'], result)
        self.assertEqual('1000000000000000020', result['room_id'])
        self.assertEqual('2769016234444315', result['anchor_id'])
        self.assertEqual('demo_010', result['nickname'])
        self.assertTrue(any('v2/user/info' in u for u in calls),
                        '必须走抖音号那条路')


if __name__ == '__main__':
    unittest.main()


class ManualStartCarriesIdentityTests(unittest.TestCase):
    """手动加的厅也要带上主播身份，否则在线名单永远同步不了。

    2026-08-23：Ray 手动加了 demo.1（示例点唱厅），麦上主持 8 个都正常，
    但在线名单一直「等待首次同步」。原因是解析那步明明拿到了 sec_uid 和
    主播 uid，startListening 只发了 room_id + nickname，转手全扔了；
    /api/start 又只读 sec_uid / douyin_id（前端从没发过）、根本不读
    anchor_id。于是 _anchor_info 两样都凑不齐，在线名单干等。

    douyin_id 一并带上：它是「这个厅下播重开换了房间号还能跟上」的依据，
    手动加的厅原本是空的，跟丢了就再也回不来。
    """

    def test_start_passes_anchor_id_and_identity(self):
        captured = {}

        def fake_start(**kwargs):
            captured.update(kwargs)
            return {'success': True, 'room_id': kwargs['room_id']}

        client = web_listener.app.test_client()
        with patch.object(web_listener, 'start_room_listener',
                          side_effect=fake_start):
            resp = client.post('/api/start', json={
                'room_id': '1000000000000000020',
                'nickname': '示例点唱厅',
                'sec_uid': 'sec-x',
                'anchor_id': '2769016234444315',
                'douyin_id': 'demo.1',
            })

        self.assertEqual(200, resp.status_code)
        self.assertEqual('2769016234444315', captured.get('anchor_id'))
        self.assertEqual('sec-x', captured.get('sec_uid'))
        self.assertEqual('demo.1', captured.get('douyin_id'))

    def test_start_without_identity_still_works(self):
        """老前端不发这些字段时不能起不来。"""
        captured = {}

        def fake_start(**kwargs):
            captured.update(kwargs)
            return {'success': True, 'room_id': kwargs['room_id']}

        client = web_listener.app.test_client()
        with patch.object(web_listener, 'start_room_listener',
                          side_effect=fake_start):
            resp = client.post('/api/start', json={'room_id': '123'})

        self.assertEqual(200, resp.status_code)
        self.assertEqual('', captured.get('anchor_id'))
