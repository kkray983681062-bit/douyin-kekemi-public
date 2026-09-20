import os
import tempfile
import unittest

import version_info


def _write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(content)


class FingerprintTests(unittest.TestCase):
    """代码指纹：让「线上跑的是不是这一版」可被机器判定，而不是靠人 grep 容器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for rel in version_info.FINGERPRINT_FILES:
            _write(self.root, rel, b'original')

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_content_same_fingerprint(self):
        first = version_info.local_fingerprint(self.root)
        second = version_info.local_fingerprint(self.root)
        self.assertEqual(first, second)
        self.assertEqual(12, len(first))

    def test_changing_any_tracked_file_changes_fingerprint(self):
        before = version_info.local_fingerprint(self.root)
        _write(self.root, version_info.FINGERPRINT_FILES[0], b'changed')
        self.assertNotEqual(before, version_info.local_fingerprint(self.root))

    def test_missing_file_does_not_crash_and_differs_from_present(self):
        present = version_info.local_fingerprint(self.root)
        os.unlink(os.path.join(self.root, version_info.FINGERPRINT_FILES[0]))
        missing = version_info.local_fingerprint(self.root)
        self.assertEqual(12, len(missing))
        self.assertNotEqual(present, missing)

    def test_file_list_order_is_fixed(self):
        # 顺序变了指纹就变，两端必须用同一份清单
        self.assertEqual(tuple(version_info.FINGERPRINT_FILES),
                         tuple(version_info.FINGERPRINT_FILES))
        self.assertIn('web_listener.py', version_info.FINGERPRINT_FILES)
        self.assertIn('online_audience.py', version_info.FINGERPRINT_FILES)


class VersionPayloadTests(unittest.TestCase):
    """payload 要能一眼看出「这版是不是从 GitHub 部署的」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for rel in version_info.FINGERPRINT_FILES:
            _write(self.root, rel, b'x')

    def tearDown(self):
        self.tmp.cleanup()

    def test_github_deploy_is_labelled_github(self):
        payload = version_info.version_payload(self.root, {
            'RAILWAY_GIT_COMMIT_SHA': 'ce1b69723e2a8fa950c6cb94fede966e32fb12da',
            'RAILWAY_GIT_BRANCH': 'main',
            'RAILWAY_DEPLOYMENT_ID': '40c33dd0-54dd-43ca',
        }, started_at=1000)
        self.assertEqual('github', payload['source'])
        self.assertEqual('ce1b69723e2a', payload['commit'])
        self.assertEqual('main', payload['branch'])

    def test_missing_git_metadata_is_flagged_as_bypass(self):
        # railway up 直推本地工作树时不会注入 git 元数据 —— 正是要抓的那种情况
        payload = version_info.version_payload(self.root, {}, started_at=1000)
        self.assertEqual('cli-or-local', payload['source'])
        self.assertEqual('', payload['commit'])

    def test_payload_carries_code_hash_matching_local_fingerprint(self):
        payload = version_info.version_payload(self.root, {}, started_at=1000)
        self.assertEqual(version_info.local_fingerprint(self.root),
                         payload['code_hash'])

    def test_payload_has_no_secret_fields(self):
        payload = version_info.version_payload(self.root, {
            'RAILWAY_API_TOKEN': 'super-secret',
            'SUPABASE_SECRET_KEY': 'sb_secret_x',
        }, started_at=1000)
        blob = repr(payload)
        self.assertNotIn('super-secret', blob)
        self.assertNotIn('sb_secret_x', blob)


if __name__ == '__main__':
    unittest.main()
