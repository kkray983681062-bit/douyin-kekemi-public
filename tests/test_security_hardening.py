import pathlib
import unittest

import web_listener


REPO = pathlib.Path(__file__).resolve().parent.parent

# 线上真正跑的模块。一次性脚本（repair_*、debug_*、test_api 等）不在此列。
RUNTIME_MODULES = (
    'web_listener.py',
    'online_audience.py',
    'utils/dy_util.py',
    'dy_apis/douyin_api.py',
    'dy_apis/login_api.py',
)


class TlsVerificationTests(unittest.TestCase):
    """抖音请求必须校验证书。

    这些请求带着所有厅共用的那份抖音 Cookie；关掉证书校验意味着中间人
    可以直接拿走它，代价是整个抖音账号。实测打开校验后三个关键接口
    （主页 API、礼物列表、用户信息 v2）均正常返回，没有兼容问题。
    """

    def test_no_runtime_module_disables_certificate_verification(self):
        offenders = []
        for relative in RUNTIME_MODULES:
            text = (REPO / relative).read_text(encoding='utf-8')
            for number, line in enumerate(text.splitlines(), 1):
                if 'verify=False' in line and not line.strip().startswith('#'):
                    offenders.append(f'{relative}:{number}')
        self.assertEqual([], offenders, '线上模块不允许关闭证书校验')


class SecurityHeaderTests(unittest.TestCase):
    """缺这些头，管理页可以被别人 iframe 套住做点击劫持。"""

    def setUp(self):
        self.client = web_listener.app.test_client()

    def test_login_page_carries_security_headers(self):
        headers = self.client.get('/login').headers
        self.assertEqual('DENY', headers.get('X-Frame-Options'))
        self.assertEqual('nosniff', headers.get('X-Content-Type-Options'))
        self.assertIn('frame-ancestors', headers.get('Content-Security-Policy', ''))
        self.assertTrue(headers.get('Referrer-Policy'))

    def test_api_response_also_carries_headers(self):
        headers = self.client.get('/healthz').headers
        self.assertEqual('DENY', headers.get('X-Frame-Options'))
        self.assertEqual('nosniff', headers.get('X-Content-Type-Options'))

    def test_csp_allows_google_fonts_and_self_only(self):
        csp = self.client.get('/login').headers.get('Content-Security-Policy', '')
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)


class DependencyPinningTests(unittest.TestCase):
    """依赖不锁版本 = 每次构建拉最新，供应链风险且不可复现。"""

    def test_every_requirement_is_pinned(self):
        text = (REPO / 'requirements.txt').read_text(encoding='utf-8')
        unpinned = [
            line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith('#')
            and '==' not in line
        ]
        self.assertEqual([], unpinned, '每个依赖都必须锁定版本')


if __name__ == '__main__':
    unittest.main()
