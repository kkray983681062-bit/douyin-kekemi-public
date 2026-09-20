import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import web_listener


class ChatDedupTests(unittest.TestCase):
    """抖音弹幕协议会把同一条聊天推送两次；聊天此前不带 event_key，
    数据库的 (room_id, event_key) 唯一索引对空值不生效，于是重复入库，
    公屏上同一句话出现两遍。聊天必须像礼物一样带去重键。

    匿名房 sec_uid 每次进出都变，所以去重键不能依赖 sec_uid，
    只能用「房间 + 显示名 + 秒级时间 + 内容」。
    """

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp.close()
        conn = sqlite3.connect(self._tmp.name)
        conn.executescript('''
            CREATE TABLE interaction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT, sec_uid TEXT, display TEXT, type TEXT,
                content TEXT, gift_count INTEGER, timestamp INTEGER,
                gift_id TEXT, gift_quantity INTEGER, unit_diamonds INTEGER,
                total_diamonds INTEGER, price_known INTEGER, quantity_verified INTEGER,
                recipient_key TEXT, recipient_name TEXT, event_key TEXT DEFAULT '',
                price_source TEXT, ticket_count INTEGER, ticket_source TEXT,
                room_ticket_total INTEGER
            );
            CREATE UNIQUE INDEX idx_interaction_event_key
                ON interaction_log(room_id, event_key) WHERE event_key <> '';
        ''')
        conn.commit()
        conn.close()

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def _count_chat(self):
        conn = sqlite3.connect(self._tmp.name)
        n = conn.execute(
            "SELECT COUNT(*) FROM interaction_log WHERE type='chat'"
        ).fetchone()[0]
        conn.close()
        return n

    def test_same_chat_delivered_twice_is_stored_once(self):
        with patch.object(web_listener, '_DB_PATH', self._tmp.name):
            for _ in range(2):
                web_listener._save_interaction(
                    'room-a', '', 'dou7101055', 'chat',
                    content='test', timestamp=1786979456,
                )
        self.assertEqual(1, self._count_chat(), '重复弹幕只应入库一次')

    def test_anonymous_changing_sec_uid_still_dedups(self):
        """匿名房同一条弹幕两次投递带不同 sec_uid，仍要判为同一条。"""
        with patch.object(web_listener, '_DB_PATH', self._tmp.name):
            web_listener._save_interaction(
                'room-a', 'MS4wLjAAsecuidone', '眠鱼怪怪', 'chat',
                content='哎呀我去', timestamp=1786971955,
            )
            web_listener._save_interaction(
                'room-a', 'MS4wLjBBsecuidtwo', '眠鱼怪怪', 'chat',
                content='哎呀我去', timestamp=1786971955,
            )
        self.assertEqual(1, self._count_chat())

    def test_different_chats_are_kept(self):
        """内容不同、或时间不同，是两条真实弹幕，都要留。"""
        with patch.object(web_listener, '_DB_PATH', self._tmp.name):
            web_listener._save_interaction(
                'room-a', '', 'kk大王', 'chat',
                content='[打call]', timestamp=1786971900,
            )
            web_listener._save_interaction(
                'room-a', '', 'kk大王', 'chat',
                content='[打call]', timestamp=1786971955,
            )
            web_listener._save_interaction(
                'room-a', '', 'kk大王', 'chat',
                content='你好', timestamp=1786971955,
            )
        self.assertEqual(3, self._count_chat(), '不同弹幕不能被误删')

    def test_explicit_event_key_is_respected(self):
        """礼物已自带 event_key 的路径不受影响。"""
        with patch.object(web_listener, '_DB_PATH', self._tmp.name):
            for _ in range(2):
                web_listener._save_interaction(
                    'room-a', 'sec', 'sender', 'gift',
                    content='玫瑰', event_key='combo-xyz', timestamp=1786971955,
                )
            conn = sqlite3.connect(self._tmp.name)
            n = conn.execute(
                "SELECT COUNT(*) FROM interaction_log WHERE type='gift'"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(1, n)



class ChatEventKeyExposureTests(unittest.TestCase):
    """公屏重复的真正来源不在入库，而在下发。

    _save_interaction 靠 (room_id, event_key) 唯一索引在库层去重，但
    send_event 是无条件发的，库层去重管不到 SSE。更要命的是聊天事件不带
    timestamp，前端只能拿 Date.now() 顶上（static/app.js），而 /api/feed
    返回的是服务器接收秒——同一条消息两边算出的去重键必然不同，于是
    公屏渲染两遍。这跟抖音重不重推无关，刷新和实时消息一撞就会发生。

    所以聊天必须像礼物一样，把 event_key 一路带到前端。
    """

    def test_chat_event_key_is_stable_for_the_same_message(self):
        key = web_listener.chat_event_key('Ry.灰', 1786979456, '喵喵2')
        again = web_listener.chat_event_key('Ry.灰', 1786979456, '喵喵2')
        self.assertEqual(key, again)
        self.assertTrue(key.startswith('chat-'), '沿用既有前缀，别改库里已有的键')

    def test_chat_event_key_separates_different_messages(self):
        base = web_listener.chat_event_key('Ry.灰', 1786979456, '喵喵2')
        self.assertNotEqual(base, web_listener.chat_event_key('Ry.灰', 1786979456, '别的话'))
        self.assertNotEqual(base, web_listener.chat_event_key('别人', 1786979456, '喵喵2'))
        self.assertNotEqual(base, web_listener.chat_event_key('Ry.灰', 1786979457, '喵喵2'))

    def test_key_matches_what_save_interaction_writes(self):
        """前端拿到的键必须跟库里那条一模一样，否则 SSE 和 /api/feed 还是对不上。"""
        with patch.object(web_listener, '_DB_PATH', self._tmp.name):
            web_listener._save_interaction(
                'room-a', '', 'Ry.灰', 'chat',
                content='喵喵2', timestamp=1786979456,
            )
        conn = sqlite3.connect(self._tmp.name)
        stored = conn.execute(
            "SELECT event_key FROM interaction_log WHERE type='chat'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(
            web_listener.chat_event_key('Ry.灰', 1786979456, '喵喵2'), stored)

    setUp = ChatDedupTests.setUp
    tearDown = ChatDedupTests.tearDown



class ChatHandlerKeyTests(unittest.TestCase):
    """钉住真正要紧的那个不变量：处理器算一次键，入库和 SSE 用的必须是同一个。

    只测 chat_event_key 本身是不够的——公屏重复的根源正是「两边各算各的」。
    如果哪天有人把键改成在两个分支里分别重算，单元测试照样全绿，
    重复却会原样回来。所以这里直接调真实的 _handle_chat_payload，
    同时截住入库和下发两条路，比对它们拿到的键。

    处理器只用到 self 的 5 个属性，用桩对象即可，不必真起监听线程。
    """

    def _run_handler(self, display='Ry.灰', content='喵喵2', mystery=False,
                     push_regular=True):
        import static.Live_pb2 as Live_pb2
        msg = Live_pb2.ChatMessage()
        msg.content = content
        msg.user.nickname = display
        msg.user.sec_uid = 'MS4wLjABAAAAstub'

        published = []
        saved = []

        class Stub:
            room_id = 'room-a'
            nickname = '测试厅'
            is_private = False
            def send_event(self, event_type, data):
                published.append(data)
            def _write_all_user(self, info):
                pass

        handler = web_listener.RoomListener.__dict__['_handle_chat_payload']
        with patch.object(web_listener, '_DB_PATH', self._tmp.name),                 patch.object(web_listener, '_save_interaction',
                             side_effect=lambda *a, **k: saved.append(k)),                 patch.object(web_listener, '_record_all_enabled', True),                 patch.object(web_listener, 'is_real_mystery_user',
                             return_value=(mystery, display, display, 2 if mystery else 0)),                 patch.object(web_listener, 'lookup_user', return_value=None),                 patch.object(web_listener, '_save_mystery_record'),                 patch.object(web_listener, '_identity_register_mystery',
                             return_value=None),                 patch.object(web_listener, '_identity_match', return_value=None),                 patch.object(web_listener, '_PUSH_REGULAR_CHAT', push_regular):
            handler(Stub(), msg.SerializeToString())
        return saved, published

    def test_saved_and_published_share_one_key(self):
        saved, published = self._run_handler()
        self.assertEqual(1, len(saved), '应当入库一次')
        self.assertEqual(1, len(published), '应当下发一次')
        self.assertTrue(saved[0].get('event_key'), '入库必须显式带键，不能靠兜底')
        self.assertEqual(saved[0]['event_key'], published[0].get('event_key'),
                         'SSE 与入库的去重键必须是同一个')

    def test_published_event_carries_timestamp(self):
        """前端拿不到 timestamp 就会用 Date.now() 顶上，那正是重复的来源。"""
        saved, published = self._run_handler()
        self.assertEqual(saved[0].get('timestamp'), published[0].get('timestamp'))
        self.assertIsInstance(published[0].get('timestamp'), int)

    def test_mystery_branch_shares_one_key_too(self):
        """神秘人走的是另一条分支，也必须用同一个键。

        少了这条，把神秘人分支的键去掉测试照样全绿——实测过。
        """
        saved, published = self._run_handler(mystery=True)
        self.assertEqual(1, len(saved))
        self.assertEqual(1, len(published))
        self.assertTrue(saved[0].get('event_key'))
        self.assertEqual(saved[0]['event_key'], published[0].get('event_key'))
        self.assertEqual(saved[0].get('timestamp'), published[0].get('timestamp'))

    def test_key_is_the_documented_algorithm(self):
        saved, published = self._run_handler(display='天晴了', content='下午档')
        self.assertEqual(
            web_listener.chat_event_key('天晴了', saved[0]['timestamp'], '下午档'),
            published[0]['event_key'])

    def test_regular_chat_is_saved_but_not_pushed_when_switch_is_off(self):
        """_PUSH_REGULAR_CHAT=False 时：照常入库，不再实时下发。

        Egress 优化关掉的只是推送。公屏改由 /api/feed 从 SQLite 重建，
        所以入库这一步一步都不能少——少了就是真丢数据，不是省流量。
        """
        saved, published = self._run_handler(push_regular=False)
        self.assertEqual(1, len(saved), '开关关掉也必须照常入库')
        self.assertTrue(saved[0].get('event_key'), '入库仍要带键')
        self.assertEqual([], published, '开关关掉就不该再下发')

    def test_mystery_chat_is_pushed_even_when_the_regular_switch_is_off(self):
        """开关只管普通观众。神秘人是这个工具的核心，永远实时。"""
        saved, published = self._run_handler(mystery=True, push_regular=False)
        self.assertEqual(1, len(saved))
        self.assertEqual(1, len(published), '神秘人不受开关影响')
        self.assertEqual(saved[0]['event_key'], published[0].get('event_key'))

    setUp = ChatDedupTests.setUp
    tearDown = ChatDedupTests.tearDown


if __name__ == '__main__':
    unittest.main()
