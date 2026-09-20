"""运行中代码的版本指纹：让「线上到底跑的哪一版」可被机器判定。

背景：2026-08-18 生产被本地 `railway up` 直推覆盖了 4 次，每次都把 main 的部署
顶掉，线上悄悄退回旧代码（麦位实时票数、礼物皮肤定价、礼物库搜索全部消失），
而 `/healthz` 照样 200，花了一个多小时才发现。指纹让这种事 5 秒暴露：
比对 `/version` 的 code_hash 与 `git show origin/main:<file>` 算出的指纹。
"""

import hashlib
import os

# 决定线上行为的核心文件。改了它们就必须重新部署，因此纳入指纹。
# 两端（服务端读磁盘、校验脚本读 git）必须用同一份清单且顺序一致。
FINGERPRINT_FILES = (
    'web_listener.py',
    'online_audience.py',
    'gift_catalog.py',
    'identity_registry.py',
    'user_notes.py',
    'static/app.js',
)


def fingerprint(read_file):
    """按固定顺序哈希清单里的文件内容；read_file(rel) 返回 bytes 或 None。"""
    digest = hashlib.sha256()
    for relative_path in FINGERPRINT_FILES:
        digest.update(relative_path.encode('utf-8'))
        digest.update(b'\0')
        digest.update(read_file(relative_path) or b'<missing>')
        digest.update(b'\0')
    return digest.hexdigest()[:12]


def local_fingerprint(root):
    """从磁盘上的工作目录算指纹（服务端用）。"""
    def read(relative_path):
        try:
            with open(os.path.join(root, relative_path), 'rb') as handle:
                return handle.read()
        except OSError:
            return None
    return fingerprint(read)


def version_payload(root, environ, started_at):
    """给 /version 用的响应体；只含版本信息，不含任何密钥。"""
    commit = str(environ.get('RAILWAY_GIT_COMMIT_SHA') or '')
    return {
        'commit': commit[:12],
        'branch': str(environ.get('RAILWAY_GIT_BRANCH') or ''),
        # 没有 git 元数据 = 不是从 GitHub 部署的（例如 railway up 直推本地工作树），
        # 这本身就是要抓的异常信号。
        'source': 'github' if commit else 'cli-or-local',
        'code_hash': local_fingerprint(root),
        'deployment_id': str(environ.get('RAILWAY_DEPLOYMENT_ID') or '')[:8],
        'started_at': int(started_at),
    }
