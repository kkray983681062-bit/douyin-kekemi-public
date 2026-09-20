import unittest
from unittest.mock import patch

import web_listener


LEAKY_MESSAGE = r"could not open D:\douyin\mystery_history.db: table app_sessions locked"


class ApiErrorMaskingTests(unittest.TestCase):
    """接口异常只能写服务器日志，绝不把原文回显给前端。

    Python 的异常消息常带绝对路径、SQL 片段和内部字段名，
    公网部署后攻击者只要故意触发报错就能拿到目录结构和实现细节。
    """

    def _assert_masked(self, payload):
        self.assertFalse(payload['success'])
        message = payload['error']
        self.assertNotIn('douyin', message)
        self.assertNotIn('mystery_history.db', message)
        self.assertNotIn('app_sessions', message)
        self.assertNotIn(r'D:\\', message)
        self.assertTrue(message.strip(), '仍然要给用户一句可读的提示')

    def test_emoji_map_failure_hides_exception_text(self):
        with patch('web_listener.json.load', side_effect=RuntimeError(LEAKY_MESSAGE)):
            payload = web_listener.app.test_client().get('/api/emoji_map').get_json()

        self._assert_masked(payload)

    def test_feed_failure_hides_exception_text(self):
        with patch('web_listener._build_feed_events', side_effect=RuntimeError(LEAKY_MESSAGE)):
            payload = web_listener.app.test_client().get('/api/feed/room-a').get_json()

        self._assert_masked(payload)

    def test_masked_response_keeps_extra_fields(self):
        """带 fallback 标记的响应在脱敏后仍要保留该标记。"""
        with web_listener.app.test_request_context():
            payload = web_listener.api_error(
                RuntimeError(LEAKY_MESSAGE), fallback=True
            ).get_json()

        self._assert_masked(payload)
        self.assertTrue(payload['fallback'])

    def _source(self):
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'web_listener.py')
        with open(path, encoding='utf-8') as handle:
            return handle.read()

    def test_no_response_builds_error_from_raw_exception(self):
        """守住回归：源码里不得再直接把异常对象塞进响应。

        异常必须走两个明确出口之一：
        - api_error()      系统异常，只写日志，前端收通用提示
        - business_error() 业务异常（"价格必须是正整数"、"礼物不存在"），
                           这类本来就该让用户看到，用独立函数把意图写明
        所以源码里不应再出现任何 'error': str(...) 的裸写法。
        """
        source = self._source()
        self.assertNotIn("'error': str(", source)
        self.assertNotIn('"error": str(', source)

    def test_business_error_keeps_the_message_visible(self):
        """业务异常必须保留原文——用户需要知道自己哪里填错了。"""
        with web_listener.app.test_request_context():
            payload = web_listener.business_error(
                ValueError('单价必须是正整数')
            ).get_json()

        self.assertFalse(payload['success'])
        self.assertEqual('单价必须是正整数', payload['error'])


if __name__ == '__main__':
    unittest.main()
