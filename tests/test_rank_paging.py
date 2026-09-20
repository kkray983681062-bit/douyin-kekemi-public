import unittest

import web_listener


def _seed(room, n):
    for i in range(n):
        web_listener._save_interaction(
            room, 'sec-%03d' % i, '游客%03d' % i, 'gift', content='玫瑰',
            gift_id='g', gift_quantity=1, unit_diamonds=10,
            total_diamonds=10, price_known=True, quantity_verified=True,
            recipient_key='host-%d' % (i % 3), recipient_name='主持%d' % (i % 3),
            event_key='%s-e%d' % (room, i), ticket_count=1000 - i,
            ticket_source='gift_price',
        )


class DailyRankPagingTests(unittest.TestCase):
    """日榜默认只发前若干名。

    实测：前 20 名占全厅 74~99.9% 的票，后面一百多号人多是 1、2 票，
    而整份榜单每 2 秒重发一次 —— 大厅 41KB 里有 82% 是没人看的尾巴。
    """

    def setUp(self):
        web_listener._READ_CACHE.clear()
        self.client = web_listener.app.test_client()

    def test_default_returns_only_top_slice(self):
        _seed('room-p1', 60)
        data = self.client.get('/api/daily_rank/room-p1').get_json()
        self.assertEqual(web_listener.RANK_PAGE_SIZE, len(data['visitors']))
        self.assertEqual(60, data['visitor_total'], '要告诉前端一共多少人')
        self.assertTrue(data['visitors_truncated'])

    def test_top_slice_keeps_the_highest_ranked(self):
        _seed('room-p2', 40)
        data = self.client.get('/api/daily_rank/room-p2').get_json()
        ranks = [row['rank'] for row in data['visitors']]
        self.assertEqual(list(range(1, web_listener.RANK_PAGE_SIZE + 1)), ranks,
                         '截的必须是前几名，不能是随便几行')

    def test_full_can_be_requested_explicitly(self):
        _seed('room-p3', 60)
        data = self.client.get('/api/daily_rank/room-p3?limit=0').get_json()
        self.assertEqual(60, len(data['visitors']))
        self.assertFalse(data['visitors_truncated'])

    def test_short_board_is_not_marked_truncated(self):
        _seed('room-p4', 5)
        data = self.client.get('/api/daily_rank/room-p4').get_json()
        self.assertEqual(5, len(data['visitors']))
        self.assertFalse(data['visitors_truncated'])

    def test_limit_is_part_of_cache_key_so_slices_do_not_mix(self):
        _seed('room-p5', 60)
        short = self.client.get('/api/daily_rank/room-p5').get_json()
        full = self.client.get('/api/daily_rank/room-p5?limit=0').get_json()
        self.assertEqual(web_listener.RANK_PAGE_SIZE, len(short['visitors']))
        self.assertEqual(60, len(full['visitors']), '缓存不能把截断版当全量返回')


if __name__ == '__main__':
    unittest.main()
