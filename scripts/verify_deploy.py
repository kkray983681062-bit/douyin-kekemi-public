"""对账：线上跑的代码是不是指定的 git 版本。

用法（在仓库根目录跑）：
    python scripts/verify_deploy.py                 # 默认对 origin/main
    python scripts/verify_deploy.py origin/main https://your-app.example

退出码 0 = 一致；非 0 = 线上不是这一版（曾经发生过：本地 `railway up` 直推把
main 的部署整个覆盖掉，healthz 照样 200，但代码悄悄退回了旧版）。
"""

import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import version_info  # noqa: E402

# Windows 控制台默认不是 UTF-8，中文结论会变乱码，读不出 PASS/FAIL。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DEFAULT_REF = 'origin/main'
DEFAULT_BASE = os.environ.get('DEPLOY_BASE_URL', '')


def read_from_git(ref):
    def read(relative_path):
        try:
            return subprocess.check_output(
                ['git', 'show', f'{ref}:{relative_path}'],
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            return None
    return read


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REF
    base = (sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BASE).rstrip('/')

    if not base:
        print('Provide a base URL as the second argument or DEPLOY_BASE_URL.')
        return 2

    expected = version_info.fingerprint(read_from_git(ref))
    with urllib.request.urlopen(f'{base}/version', timeout=20) as response:
        live = json.load(response)

    same = live.get('code_hash') == expected
    print(f'{ref:<14} 指纹 = {expected}')
    print(f'{"线上":<14} 指纹 = {live.get("code_hash")}')
    print(f'  线上来源 = {live.get("source")}  commit = {live.get("commit") or "(无)"}'
          f'  部署 = {live.get("deployment_id")}')
    if live.get('source') != 'github':
        print('  ⚠ 线上不是从 GitHub 部署的（疑似 railway up 直推本地工作树）')
    print('PASS 线上就是这一版' if same else 'FAIL 线上跑的不是这一版，需要从 main 重新部署')
    return 0 if same else 1


if __name__ == '__main__':
    sys.exit(main())
