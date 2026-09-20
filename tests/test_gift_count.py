import sys
import unittest
import unittest.mock  # 先于 web_listener 的 subprocess.Popen 包装加载 asyncio
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import web_listener


class GiftCountTests(unittest.TestCase):
    def test_repeat_count_wins_when_combo_count_is_one(self):
        # 抖音真实 payload：字段5 repeatCount=5，字段6 comboCount=1。
        base = web_listener.Live_pb2.GiftMessage()
        base.comboCount = 1
        payload = base.SerializeToString() + b"\x28\x05"

        parsed = web_listener.Live_pb2.GiftMessage()
        parsed.ParseFromString(payload)

        # 缺少新计数器时，用现有线上逻辑作为基线，确保本测试先 RED。
        counter = getattr(
            web_listener,
            "_gift_message_count",
            lambda message: int(message.comboCount or 1),
        )
        self.assertEqual(counter(parsed), 5)


if __name__ == "__main__":
    unittest.main()
