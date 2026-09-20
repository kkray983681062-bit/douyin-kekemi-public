import os
import tempfile
import unittest

from runtime_config import (
    ensure_database_parent,
    is_production,
    resolve_database_path,
    resolve_online_sync_interval,
    resolve_server_options,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_production_uses_railway_host_port_and_no_reloader(self):
        options = resolve_server_options({
            'APP_ENV': 'production',
            'PORT': '8123',
        })

        self.assertEqual({
            'host': '0.0.0.0',
            'port': 8123,
            'use_reloader': False,
        }, options)

    def test_railway_environment_is_production_without_app_env(self):
        self.assertTrue(is_production({'RAILWAY_ENVIRONMENT': 'production'}))
        self.assertEqual('0.0.0.0', resolve_server_options({
            'RAILWAY_ENVIRONMENT': 'production',
            'PORT': '9000',
        })['host'])

    def test_local_defaults_keep_loopback_and_reloader(self):
        self.assertEqual({
            'host': '127.0.0.1',
            'port': 5000,
            'use_reloader': True,
        }, resolve_server_options({}))

    def test_invalid_port_falls_back_to_5000(self):
        self.assertEqual(5000, resolve_server_options({
            'APP_ENV': 'production',
            'PORT': 'not-a-number',
        })['port'])

    def test_explicit_sqlite_path_wins_over_volume_mount(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = os.path.join(temp_dir, 'custom.db')

            actual = resolve_database_path({
                'SQLITE_DB_PATH': expected,
                'RAILWAY_VOLUME_MOUNT_PATH': '/app/data',
            }, 'C:/repo')

        self.assertEqual(os.path.abspath(expected), actual)

    def test_relative_sqlite_path_is_resolved_from_project_directory(self):
        project_dir = os.path.abspath('project-root')

        actual = resolve_database_path({
            'SQLITE_DB_PATH': 'data/custom.db',
        }, project_dir)

        self.assertEqual(
            os.path.abspath(os.path.join(project_dir, 'data', 'custom.db')),
            actual,
        )

    def test_volume_mount_supplies_default_database_path(self):
        expected = os.path.abspath(os.path.join(
            '/app/data', 'mystery_history.db'
        ))

        actual = resolve_database_path({
            'RAILWAY_VOLUME_MOUNT_PATH': '/app/data',
        }, 'C:/repo')

        self.assertEqual(expected, actual)

    def test_database_parent_is_created_before_sqlite_connects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, 'nested', 'app.db')

            ensure_database_parent(database_path)

            self.assertTrue(os.path.isdir(os.path.dirname(database_path)))


if __name__ == '__main__':
    unittest.main()


class OnlineSyncIntervalTests(unittest.TestCase):
    """在线名单同步间隔要可配置。

    这个值决定「多久去抖音抓一次票数」，直接换算成抖音请求频率：
    5 个厅 × 每 3 秒一次 ≈ 每分钟 100 次。调太低会触发风控，
    而所有人共用同一份 Cookie，代价是整个抖音账号。
    因此必须能不改代码就调整。
    """

    def test_defaults_to_three_seconds(self):
        self.assertEqual(3, resolve_online_sync_interval({}))

    def test_environment_overrides_the_default(self):
        self.assertEqual(8, resolve_online_sync_interval({'ONLINE_SYNC_INTERVAL': '8'}))

    def test_rejects_values_below_one_second(self):
        """低于 1 秒必然打爆抖音接口，直接拒绝而不是默默接受。"""
        for bad in ('0', '0.2', '-5'):
            self.assertEqual(3, resolve_online_sync_interval({'ONLINE_SYNC_INTERVAL': bad}))

    def test_ignores_garbage_instead_of_crashing(self):
        self.assertEqual(3, resolve_online_sync_interval({'ONLINE_SYNC_INTERVAL': 'soon'}))
