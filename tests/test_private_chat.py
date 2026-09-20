import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import web_listener


class PrivateChatTests(unittest.TestCase):
    def test_private_chat_without_sec_uid_is_emitted_and_uses_webcast_uid(self):
        listener = web_listener.RoomListener('room-a', 'A')
        listener.is_private = True

        msg = web_listener.Live_pb2.ChatMessage()
        msg.user.nickname = 'dou7101055'
        msg.user.mystery_man = 2
        msg.user.webcast_uid = 'stable-webcast-token'
        msg.content = '测试'

        handler = getattr(listener, '_handle_chat_payload', None)
        self.assertIsNotNone(handler, 'private chat must have a processable handler')

        with (
            patch.dict(web_listener._private_name_cache, {}, clear=True),
            patch.object(
                web_listener,
                'is_real_mystery_user',
                return_value=(True, 'dou7101055', 'dou7101055', 2),
            ),
            patch.object(web_listener, 'lookup_user', return_value=None),
            patch.object(web_listener, '_save_mystery_record') as save_identity,
            patch.object(web_listener, '_save_interaction') as save_interaction,
        ):
            handler(msg.SerializeToString())

        event = listener.events.get_nowait()
        self.assertEqual(event['type'], 'mystery_chat')
        self.assertEqual(event['data']['content'], '测试')
        self.assertEqual(event['data']['room_id'], 'room-a')
        self.assertFalse(event['data']['is_regular'])
        save_identity.assert_not_called()
        self.assertEqual(save_interaction.call_args.args[1], 'stable-webcast-token')


if __name__ == '__main__':
    unittest.main()
