import os
import unittest
from unittest.mock import patch

import web_listener


class RoomLimitTests(unittest.TestCase):
    def setUp(self):
        self.original_listeners = web_listener.listeners
        web_listener.listeners = {}

    def tearDown(self):
        web_listener.listeners = self.original_listeners

    def test_status_uses_environment_room_limit(self):
        with patch.dict(os.environ, {'MAX_ROOMS': '8'}):
            payload = web_listener.app.test_client().get('/api/status').get_json()

        self.assertEqual(8, payload['max'])

    def test_invalid_room_limit_falls_back_to_ten(self):
        with patch.dict(os.environ, {'MAX_ROOMS': 'not-a-number'}):
            payload = web_listener.app.test_client().get('/api/status').get_json()

        self.assertEqual(10, payload['max'])


if __name__ == '__main__':
    unittest.main()
