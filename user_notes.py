"""super_admin 给后台用户加备注。

存本地 SQLite（与监听业务同一个库），不进 Supabase 账号体系：
- 自包含、可直接上线，不用手动跑 Supabase 迁移；
- 与 kekemi_auth 的 Supabase 后端解耦，备注属管理元数据，丢失不影响账号本身。
"""

import os
import sqlite3
import time

from runtime_config import resolve_database_path

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_NOTE_LEN = 500


def _db_path():
    return resolve_database_path(os.environ, _PROJECT_DIR)


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or _db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=8000')
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS user_admin_notes (
            user_id TEXT PRIMARY KEY,
            note TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        '''
    )
    return conn


def set_note(user_id, note, actor_id='', now=None, db_path=None):
    """写/改某用户备注；备注传空串即清空。返回落库后的值。"""
    user_id = str(user_id or '').strip()
    if not user_id:
        raise ValueError('user_id 不能为空')
    note = str(note or '').strip()
    if len(note) > MAX_NOTE_LEN:
        raise ValueError(f'备注最多 {MAX_NOTE_LEN} 字')
    actor_id = str(actor_id or '').strip()
    timestamp = int(now if now is not None else time.time())
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                '''
                INSERT INTO user_admin_notes (user_id, note, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    note = excluded.note,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                ''',
                (user_id, note, actor_id, timestamp),
            )
    finally:
        conn.close()
    return {'user_id': user_id, 'note': note, 'updated_at': timestamp}


def get_notes(user_ids=None, db_path=None):
    """返回 {user_id: note}，只含非空备注。user_ids 为空则返回全部。"""
    conn = _connect(db_path)
    try:
        if user_ids is not None:
            ids = [str(value) for value in user_ids if value]
            if not ids:
                return {}
            placeholders = ','.join('?' * len(ids))
            rows = conn.execute(
                f'SELECT user_id, note FROM user_admin_notes WHERE user_id IN ({placeholders})',
                ids,
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT user_id, note FROM user_admin_notes'
            ).fetchall()
        return {row['user_id']: row['note'] for row in rows if row['note']}
    finally:
        conn.close()
