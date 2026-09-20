"""全局身份库的建表、UID 标准化、别名分类与北京日期边界测试。"""

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import identity_registry as ir
from static import Live_pb2


def _memory_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ir.init_identity_schema(conn)
    return conn


class IdentitySchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        self.addCleanup(self.conn.close)

    def table_names(self):
        return {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    def test_creates_three_permanent_identity_tables(self):
        self.assertLessEqual(
            {
                'identity_profiles',
                'identity_identifiers',
                'identity_alias_history',
            },
            self.table_names(),
        )

    def test_init_is_idempotent(self):
        ir.init_identity_schema(self.conn)
        ir.init_identity_schema(self.conn)

        self.assertLessEqual(
            {
                'identity_profiles',
                'identity_identifiers',
                'identity_alias_history',
            },
            self.table_names(),
        )

    def test_identifier_type_and_value_pair_is_unique(self):
        self.conn.execute(
            'INSERT INTO identity_profiles (real_name) VALUES (?)', ('示例用户甲',)
        )
        self.conn.execute(
            'INSERT INTO identity_profiles (real_name) VALUES (?)', ('demo_001',)
        )
        self.conn.execute('''
            INSERT INTO identity_identifiers
                (identity_id, identifier_type, identifier_value)
            VALUES (1, 'sec_uid', 'MS4wLjAB-real')
        ''')

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('''
                INSERT INTO identity_identifiers
                    (identity_id, identifier_type, identifier_value)
                VALUES (2, 'sec_uid', 'MS4wLjAB-real')
            ''')

    def test_same_identifier_value_allowed_across_identifier_types(self):
        self.conn.execute(
            'INSERT INTO identity_profiles (real_name) VALUES (?)', ('示例用户甲',)
        )
        self.conn.execute('''
            INSERT INTO identity_identifiers
                (identity_id, identifier_type, identifier_value)
            VALUES (1, 'webcast_uid', '1000000000000000019')
        ''')
        self.conn.execute('''
            INSERT INTO identity_identifiers
                (identity_id, identifier_type, identifier_value)
            VALUES (1, 'numeric_uid', '1000000000000000019')
        ''')

        self.assertEqual(
            2,
            self.conn.execute(
                'SELECT COUNT(*) FROM identity_identifiers'
            ).fetchone()[0],
        )

    def test_alias_history_is_unique_per_identity_alias_date_and_room(self):
        self.conn.execute(
            'INSERT INTO identity_profiles (real_name) VALUES (?)', ('示例用户甲',)
        )
        self.conn.execute('''
            INSERT INTO identity_alias_history
                (identity_id, alias, alias_type, beijing_date, room_id)
            VALUES (1, 'dou8328160', 'dou', '2026-08-17', 'room-a')
        ''')

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('''
                INSERT INTO identity_alias_history
                    (identity_id, alias, alias_type, beijing_date, room_id)
                VALUES (1, 'dou8328160', 'dou', '2026-08-17', 'room-a')
            ''')


class NormalizeIdentifierTests(unittest.TestCase):
    def test_keeps_real_sec_uid_webcast_uid_and_numeric_uid(self):
        self.assertEqual(
            'MS4wLjABAAAA-real',
            ir.normalize_identifier('sec_uid', '  MS4wLjABAAAA-real  '),
        )
        self.assertEqual(
            '1000000000000000019',
            ir.normalize_identifier('webcast_uid', '1000000000000000019'),
        )
        self.assertEqual(
            '108625123456',
            ir.normalize_identifier('numeric_uid', '108625123456'),
        )

    def test_rejects_douyin_anonymous_placeholders(self):
        for identifier_type in ('sec_uid', 'webcast_uid', 'numeric_uid'):
            for placeholder in ('', '   ', '0', '?', '111111', None):
                with self.subTest(type=identifier_type, value=placeholder):
                    self.assertEqual(
                        '', ir.normalize_identifier(identifier_type, placeholder)
                    )

    def test_numeric_uid_must_be_all_digits(self):
        self.assertEqual('', ir.normalize_identifier('numeric_uid', 'abc123'))
        self.assertEqual('', ir.normalize_identifier('numeric_uid', '123abc'))
        self.assertEqual('', ir.normalize_identifier('numeric_uid', '12 34'))

    def test_unknown_identifier_type_is_rejected(self):
        self.assertEqual('', ir.normalize_identifier('display_id', 'kekeray'))
        self.assertEqual('', ir.normalize_identifier('nickname', '示例用户甲'))


class ExtractIdentifiersTests(unittest.TestCase):
    def test_extracts_all_three_types_from_protobuf_user(self):
        user = Live_pb2.User()
        user.sec_uid = 'MS4wLjABAAAA-real'
        user.webcast_uid = '1000000000000000019'
        user.id = 108625123456
        user.display_id = 'kekeray'
        user.nickname = '示例用户甲'

        self.assertEqual(
            (
                ('sec_uid', 'MS4wLjABAAAA-real'),
                ('webcast_uid', '1000000000000000019'),
                ('numeric_uid', '108625123456'),
            ),
            ir.extract_identifiers(user),
        )

    def test_never_treats_douyin_id_or_nickname_as_identifier(self):
        user = Live_pb2.User()
        user.display_id = 'kekeray'
        user.nickname = '示例用户甲'
        user.short_id = 998877

        self.assertEqual((), ir.extract_identifiers(user))

    def test_drops_anonymous_placeholder_fields(self):
        user = Live_pb2.User()
        user.webcast_uid = '1000000000000000019'
        user.id = 111111
        user.display_id = '111111'

        self.assertEqual(
            (('webcast_uid', '1000000000000000019'),),
            ir.extract_identifiers(user),
        )

    def test_extracts_from_online_roster_dict(self):
        record = {
            'sec_uid': 'MS4wLjABAAAA-real',
            'webcast_uid': '1000000000000000019',
            'user_id': '108625123456',
            'display_id': 'kekeray',
            'nickname': 'dou8328160',
        }

        self.assertEqual(
            (
                ('sec_uid', 'MS4wLjABAAAA-real'),
                ('webcast_uid', '1000000000000000019'),
                ('numeric_uid', '108625123456'),
            ),
            ir.extract_identifiers(record),
        )

    def test_deduplicates_repeated_values_of_same_type(self):
        record = {'sec_uid': 'MS4wLjABAAAA-real', 'secUid': 'MS4wLjABAAAA-real'}

        self.assertEqual(
            (('sec_uid', 'MS4wLjABAAAA-real'),),
            ir.extract_identifiers(record),
        )

    def test_returns_empty_for_missing_or_bad_input(self):
        self.assertEqual((), ir.extract_identifiers(None))
        self.assertEqual((), ir.extract_identifiers({}))
        self.assertEqual((), ir.extract_identifiers('示例用户甲'))


class BeijingDateTests(unittest.TestCase):
    def test_utc_1559_is_still_the_same_beijing_day(self):
        # 2026-08-17T15:59:59Z = 2026-08-17 23:59:59 北京时间
        self.assertEqual('2026-08-17', ir.beijing_date(1786982399))

    def test_utc_1600_rolls_into_the_next_beijing_day(self):
        # 2026-08-17T16:00:00Z = 2026-08-18 00:00:00 北京时间
        self.assertEqual('2026-08-18', ir.beijing_date(1786982400))


class ClassifyAliasTests(unittest.TestCase):
    def test_dou_alias(self):
        self.assertEqual('dou', ir.classify_alias('dou8328160'))
        self.assertEqual('dou', ir.classify_alias('DOU8328160'))

    def test_mystery_number_alias(self):
        self.assertEqual('mystery_number', ir.classify_alias('神秘人339175'))

    def test_mystery_tier_alias(self):
        self.assertEqual('mystery_tier', ir.classify_alias('神秘人二阶'))
        self.assertEqual('mystery_tier', ir.classify_alias('神秘人三阶'))
        self.assertEqual('mystery_tier', ir.classify_alias('神秘人六阶'))
        self.assertEqual('mystery_tier', ir.classify_alias('神秘人十二阶'))

    def test_anything_else_is_other_anonymous(self):
        self.assertEqual('other_anonymous', ir.classify_alias('示例用户甲'))
        self.assertEqual('other_anonymous', ir.classify_alias(''))


def _seed_identity(conn, real_name, identifiers, douyin_id='', profile=None):
    """直接写入身份行，让匹配测试不依赖写入路径的实现。"""
    import json

    cursor = conn.execute('''
        INSERT INTO identity_profiles
            (canonical_sec_uid, real_name, douyin_id, profile_json)
        VALUES (?, ?, ?, ?)
    ''', (
        next((v for t, v in identifiers if t == 'sec_uid'), ''),
        real_name,
        douyin_id,
        json.dumps(profile or {}, ensure_ascii=False),
    ))
    identity_id = cursor.lastrowid
    for identifier_type, value in identifiers:
        conn.execute('''
            INSERT INTO identity_identifiers
                (identity_id, identifier_type, identifier_value)
            VALUES (?, ?, ?)
        ''', (identity_id, identifier_type, value))
    conn.commit()
    return identity_id


class LookupIdentityTests(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        self.addCleanup(self.conn.close)
        ir.invalidate_cache()

    def test_single_matching_uid_resolves_the_identity(self):
        identity_id = _seed_identity(
            self.conn, '示例用户甲', [('sec_uid', 'MS4wLjABAAAA-real')]
        )

        result = ir.lookup_identity(self.conn, (('sec_uid', 'MS4wLjABAAAA-real'),))

        self.assertEqual(identity_id, result.identity_id)
        self.assertFalse(result.conflict)
        self.assertEqual(('sec_uid',), result.matched_by)

    def test_webcast_uid_alone_resolves_the_identity(self):
        identity_id = _seed_identity(
            self.conn, '示例用户甲', [('webcast_uid', '1000000000000000019')]
        )

        result = ir.lookup_identity(
            self.conn, (('webcast_uid', '1000000000000000019'),)
        )

        self.assertEqual(identity_id, result.identity_id)

    def test_numeric_uid_alone_resolves_the_identity(self):
        identity_id = _seed_identity(
            self.conn, '示例用户甲', [('numeric_uid', '108625123456')]
        )

        result = ir.lookup_identity(self.conn, (('numeric_uid', '108625123456'),))

        self.assertEqual(identity_id, result.identity_id)

    def test_several_uids_pointing_at_one_identity_is_not_a_conflict(self):
        identity_id = _seed_identity(self.conn, '示例用户甲', [
            ('sec_uid', 'MS4wLjABAAAA-real'),
            ('webcast_uid', '1000000000000000019'),
            ('numeric_uid', '108625123456'),
        ])

        result = ir.lookup_identity(self.conn, (
            ('sec_uid', 'MS4wLjABAAAA-real'),
            ('webcast_uid', '1000000000000000019'),
        ))

        self.assertEqual(identity_id, result.identity_id)
        self.assertFalse(result.conflict)
        self.assertEqual(('sec_uid', 'webcast_uid'), result.matched_by)

    def test_unknown_uid_stays_unresolved(self):
        _seed_identity(self.conn, '示例用户甲', [('sec_uid', 'MS4wLjABAAAA-real')])

        result = ir.lookup_identity(self.conn, (('sec_uid', 'MS4wLjABAAAA-other'),))

        self.assertIsNone(result.identity_id)
        self.assertFalse(result.conflict)

    def test_uids_pointing_at_different_identities_are_a_conflict(self):
        first = _seed_identity(
            self.conn, '示例用户甲', [('sec_uid', 'MS4wLjABAAAA-real')]
        )
        second = _seed_identity(
            self.conn, 'demo_001', [('webcast_uid', '1000000000000000019')]
        )

        result = ir.lookup_identity(self.conn, (
            ('sec_uid', 'MS4wLjABAAAA-real'),
            ('webcast_uid', '1000000000000000019'),
        ))

        self.assertTrue(result.conflict)
        self.assertIsNone(result.identity_id)
        self.assertEqual({first, second}, set(result.candidate_ids))


class MatchUserTests(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        self.addCleanup(self.conn.close)
        ir.invalidate_cache()

    def test_anonymous_alias_gets_the_real_identity_by_uid(self):
        _seed_identity(
            self.conn,
            '示例用户甲',
            [('webcast_uid', '1000000000000000019')],
            douyin_id='kekeray',
            profile={
                'sec_uid': 'MS4wLjABAAAA-real',
                'follower_count': 1234,
                'aweme_count': 56,
                'signature': '打游戏的',
            },
        )

        matched = ir.match_user(self.conn, {
            'nickname': 'dou8328160',
            'webcast_uid': '1000000000000000019',
            'sec_uid': '111111',
            'user_id': '111111',
        })

        self.assertEqual('示例用户甲', matched['real_name'])
        self.assertEqual('kekeray', matched['douyin_id'])
        self.assertEqual(1234, matched['follower_count'])
        self.assertEqual(56, matched['aweme_count'])
        self.assertEqual(['webcast_uid'], matched['matched_by'])
        self.assertEqual(
            'https://www.douyin.com/user/MS4wLjABAAAA-real', matched['profile_url']
        )

    def test_same_nickname_and_douyin_id_never_match_without_uid(self):
        _seed_identity(
            self.conn,
            '示例用户甲',
            [('sec_uid', 'MS4wLjABAAAA-real')],
            douyin_id='kekeray',
        )

        matched = ir.match_user(self.conn, {
            'nickname': '示例用户甲',
            'display_id': 'kekeray',
            'consume_level': 45,
            'sec_uid': 'MS4wLjABAAAA-impostor',
        })

        self.assertIsNone(matched)

    def test_placeholder_only_user_never_matches(self):
        _seed_identity(self.conn, '示例用户甲', [('sec_uid', 'MS4wLjABAAAA-real')])
        # 即使占位值曾被错误写进标识表，也不能凭它认人。
        self.conn.execute('''
            INSERT INTO identity_identifiers
                (identity_id, identifier_type, identifier_value)
            VALUES (1, 'numeric_uid', '111111')
        ''')
        self.conn.commit()
        ir.invalidate_cache()

        matched = ir.match_user(self.conn, {
            'nickname': 'dou8328160',
            'sec_uid': '111111',
            'user_id': '111111',
            'display_id': '111111',
        })

        self.assertIsNone(matched)

    def test_conflicting_uids_return_no_identity(self):
        _seed_identity(self.conn, '示例用户甲', [('sec_uid', 'MS4wLjABAAAA-real')])
        _seed_identity(self.conn, 'demo_001', [('numeric_uid', '108625123456')])

        matched = ir.match_user(self.conn, {
            'sec_uid': 'MS4wLjABAAAA-real',
            'user_id': '108625123456',
        })

        self.assertIsNone(matched)

    def test_cache_is_refreshed_after_invalidation(self):
        _seed_identity(self.conn, '示例用户甲', [('sec_uid', 'MS4wLjABAAAA-real')])
        self.assertEqual(
            '示例用户甲',
            ir.match_user(self.conn, {'sec_uid': 'MS4wLjABAAAA-real'})['real_name'],
        )

        self.conn.execute(
            "UPDATE identity_profiles SET real_name = 'demo_002'"
        )
        self.conn.commit()
        ir.invalidate_cache()

        self.assertEqual(
            'demo_002',
            ir.match_user(self.conn, {'sec_uid': 'MS4wLjABAAAA-real'})['real_name'],
        )


class HighLevelGiftRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        self.addCleanup(self.conn.close)
        ir.invalidate_cache()

    def register(self, consume_level, **overrides):
        payload = {
            'user': {'sec_uid': 'MS4wLjABAAAA-real', 'user_id': '108625123456'},
            'room_id': 'room-a',
            'display': '示例用户甲',
            'real_name': '示例用户甲',
            'douyin_id': 'kekeray',
            'profile': {'follower_count': 1234, 'aweme_count': 56},
            'consume_level': consume_level,
            'timestamp': 1786982399,
        }
        payload.update(overrides)
        user = payload.pop('user')
        return ir.register_gift_sender(self.conn, user, **payload)

    def identity_count(self):
        return self.conn.execute(
            'SELECT COUNT(*) FROM identity_profiles'
        ).fetchone()[0]

    def test_level_40_gift_sender_stays_out_of_the_registry(self):
        self.assertIsNone(self.register(40))
        self.assertEqual(0, self.identity_count())

    def test_level_41_gift_sender_enters_the_registry(self):
        matched = self.register(41)

        self.assertEqual('示例用户甲', matched['real_name'])
        self.assertEqual(1, self.identity_count())

    def test_level_42_gift_sender_enters_the_registry(self):
        self.assertIsNotNone(self.register(42))
        self.assertEqual(1, self.identity_count())

    def test_mystery_sender_never_uses_the_high_level_path(self):
        self.assertIsNone(self.register(45, is_mystery=True))
        self.assertEqual(0, self.identity_count())

    def test_missing_uid_is_never_registered(self):
        self.assertIsNone(
            self.register(45, user={'sec_uid': '111111', 'user_id': '111111'})
        )
        self.assertEqual(0, self.identity_count())

    def test_repeat_gift_updates_instead_of_creating_a_second_identity(self):
        self.register(41)
        self.register(45, room_id='room-b', timestamp=1786982400)

        self.assertEqual(1, self.identity_count())
        self.assertEqual(
            2,
            self.conn.execute(
                'SELECT COUNT(*) FROM identity_identifiers'
            ).fetchone()[0],
        )
        row = self.conn.execute('''
            SELECT max_consume_level, last_room_id, last_seen
            FROM identity_profiles
        ''').fetchone()
        self.assertEqual(45, row[0])
        self.assertEqual('room-b', row[1])
        self.assertEqual(1786982400, row[2])

    def test_max_consume_level_never_goes_down(self):
        self.register(60)
        self.register(41)

        self.assertEqual(
            60,
            self.conn.execute(
                'SELECT max_consume_level FROM identity_profiles'
            ).fetchone()[0],
        )

    def test_blank_profile_never_wipes_a_known_identity(self):
        self.register(41)
        self.register(41, real_name='', douyin_id='', profile={})

        row = self.conn.execute('''
            SELECT real_name, douyin_id, profile_json FROM identity_profiles
        ''').fetchone()
        self.assertEqual('示例用户甲', row[0])
        self.assertEqual('kekeray', row[1])
        self.assertEqual(1234, ir._load_profile_json(row[2])['follower_count'])

    def test_anonymous_alias_never_overwrites_a_real_name(self):
        self.register(41)
        self.register(41, real_name='dou8328160', display='dou8328160')

        self.assertEqual(
            '示例用户甲',
            self.conn.execute(
                'SELECT real_name FROM identity_profiles'
            ).fetchone()[0],
        )

    def test_same_uid_matches_from_another_room(self):
        self.register(41)

        matched = ir.match_user(
            self.conn, {'sec_uid': 'MS4wLjABAAAA-real', 'room_id': 'room-z'}
        )

        self.assertEqual('示例用户甲', matched['real_name'])

    def test_conflicting_uids_leave_existing_bindings_untouched(self):
        _seed_identity(self.conn, '甲', [('sec_uid', 'MS4wLjABAAAA-real')])
        _seed_identity(self.conn, '乙', [('numeric_uid', '108625123456')])
        ir.invalidate_cache()

        self.assertIsNone(self.register(45))

        self.assertEqual(2, self.identity_count())
        rows = dict(self.conn.execute('''
            SELECT identifier_value, identity_id FROM identity_identifiers
        ''').fetchall())
        self.assertEqual({'MS4wLjABAAAA-real': 1, '108625123456': 2}, rows)
        self.assertEqual(
            ['甲', '乙'],
            [
                row[0]
                for row in self.conn.execute(
                    'SELECT real_name FROM identity_profiles ORDER BY id'
                ).fetchall()
            ],
        )

    def test_identity_write_is_atomic(self):
        self.conn.execute('DROP TABLE identity_alias_history')
        self.conn.commit()

        with self.assertRaises(Exception):
            self.register(41, display='dou8328160')

        self.assertEqual(0, self.identity_count())
        self.assertEqual(
            0,
            self.conn.execute(
                'SELECT COUNT(*) FROM identity_identifiers'
            ).fetchone()[0],
        )


class AliasHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        self.addCleanup(self.conn.close)
        ir.invalidate_cache()
        self.identity_id = _seed_identity(
            self.conn, '示例用户甲', [('sec_uid', 'MS4wLjABAAAA-real')]
        )

    def aliases(self):
        return [
            tuple(row)
            for row in self.conn.execute('''
                SELECT alias, alias_type, beijing_date, room_id
                FROM identity_alias_history
                ORDER BY beijing_date, alias, room_id
            ''').fetchall()
        ]

    def test_repeated_polling_on_one_day_keeps_a_single_row(self):
        for offset in range(5):
            ir.record_alias(
                self.conn, self.identity_id, 'dou8328160',
                room_id='room-a', timestamp=1786900000 + offset * 10,
            )

        self.assertEqual(
            [('dou8328160', 'dou', '2026-08-17', 'room-a')], self.aliases()
        )
        row = self.conn.execute('''
            SELECT first_seen, last_seen FROM identity_alias_history
        ''').fetchone()
        self.assertEqual(1786900000, row[0])
        self.assertEqual(1786900040, row[1])

    def test_new_beijing_day_adds_a_row_without_touching_the_old_one(self):
        ir.record_alias(
            self.conn, self.identity_id, 'dou8328160',
            room_id='room-a', timestamp=1786982399,
        )
        ir.record_alias(
            self.conn, self.identity_id, '神秘人339175',
            room_id='room-a', timestamp=1786982400,
        )

        self.assertEqual([
            ('dou8328160', 'dou', '2026-08-17', 'room-a'),
            ('神秘人339175', 'mystery_number', '2026-08-18', 'room-a'),
        ], self.aliases())

    def test_observed_room_upgrades_a_room_unknown_row_in_place(self):
        # 旧数据迁移只能写「房间未知」；同一天实时看到同一个别名时应就地补上房间，
        # 而不是并排再插一行看起来重复的记录。
        ir.record_alias(
            self.conn, self.identity_id, '神秘人586668',
            room_id='', timestamp=1786900000, source='legacy_import',
        )
        ir.record_alias(
            self.conn, self.identity_id, '神秘人586668',
            room_id='room-a', timestamp=1786982399, source='live_match',
        )

        self.assertEqual(
            [('神秘人586668', 'mystery_number', '2026-08-17', 'room-a')],
            self.aliases(),
        )
        row = self.conn.execute('''
            SELECT first_seen, last_seen FROM identity_alias_history
        ''').fetchone()
        self.assertEqual(1786900000, row[0])
        self.assertEqual(1786982399, row[1])

    def test_existing_duplicate_pair_collapses_on_the_next_sighting(self):
        # 复刻上线初期已经落库的两行：同日同别名，一行房间未知、一行真实房间。
        for room, source in (('', 'legacy_import'), ('room-a', 'resolved_mystery')):
            self.conn.execute('''
                INSERT INTO identity_alias_history
                    (identity_id, alias, alias_type, beijing_date, room_id,
                     source, first_seen, last_seen)
                VALUES (?, '神秘人586668', 'mystery_number', '2026-08-17',
                        ?, ?, 1786900000, 1786900000)
            ''', (self.identity_id, room, source))
        self.conn.commit()

        ir.record_alias(
            self.conn, self.identity_id, '神秘人586668',
            room_id='room-a', timestamp=1786982399, source='live_match',
        )

        self.assertEqual(
            [('神秘人586668', 'mystery_number', '2026-08-17', 'room-a')],
            self.aliases(),
        )
        row = self.conn.execute('''
            SELECT first_seen, last_seen FROM identity_alias_history
        ''').fetchone()
        self.assertEqual(1786900000, row[0])
        self.assertEqual(1786982399, row[1])

    def test_room_unknown_write_never_duplicates_a_known_room_row(self):
        ir.record_alias(
            self.conn, self.identity_id, '神秘人586668',
            room_id='room-a', timestamp=1786900000, source='live_match',
        )
        # 重复跑旧数据导入不能把「房间未知」的弱记录再加回来。
        ir.record_alias(
            self.conn, self.identity_id, '神秘人586668',
            room_id='', timestamp=1786982399, source='legacy_import',
        )

        self.assertEqual(
            [('神秘人586668', 'mystery_number', '2026-08-17', 'room-a')],
            self.aliases(),
        )

    def test_upgrade_only_applies_within_the_same_alias_and_day(self):
        ir.record_alias(
            self.conn, self.identity_id, '神秘人586668',
            room_id='', timestamp=1786982399, source='legacy_import',
        )
        # 换了北京日期，不能去改前一天那行的房间。
        ir.record_alias(
            self.conn, self.identity_id, '神秘人586668',
            room_id='room-a', timestamp=1786982400, source='live_match',
        )

        self.assertEqual([
            ('神秘人586668', 'mystery_number', '2026-08-17', ''),
            ('神秘人586668', 'mystery_number', '2026-08-18', 'room-a'),
        ], self.aliases())

    def test_same_alias_in_another_room_is_its_own_row(self):
        ir.record_alias(
            self.conn, self.identity_id, '神秘人二阶',
            room_id='room-a', timestamp=1786982399,
        )
        ir.record_alias(
            self.conn, self.identity_id, '神秘人二阶',
            room_id='room-b', timestamp=1786982399,
        )

        self.assertEqual([
            ('神秘人二阶', 'mystery_tier', '2026-08-17', 'room-a'),
            ('神秘人二阶', 'mystery_tier', '2026-08-17', 'room-b'),
        ], self.aliases())

    def test_real_nickname_is_not_an_anonymous_alias(self):
        self.assertFalse(ir.record_alias(
            self.conn, self.identity_id, '示例用户甲',
            room_id='room-a', timestamp=1786982399,
        ))

        self.assertEqual([], self.aliases())

    def test_high_level_gift_under_an_anonymous_alias_records_it(self):
        ir.register_gift_sender(
            self.conn,
            {'sec_uid': 'MS4wLjABAAAA-real'},
            room_id='room-a',
            display='dou8328160',
            real_name='示例用户甲',
            consume_level=41,
            timestamp=1786982399,
        )

        self.assertEqual(
            [('dou8328160', 'dou', '2026-08-17', 'room-a')], self.aliases()
        )


class ResolvedMysteryRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        self.addCleanup(self.conn.close)
        ir.invalidate_cache()

    def register(self, **overrides):
        payload = {
            'room_id': 'room-a',
            'display': '神秘人339175',
            'real_name': '示例用户甲',
            'douyin_id': 'kekeray',
            'profile': {'follower_count': 1234},
            'consume_level': 3,
            'timestamp': 1786982399,
        }
        payload.update(overrides)
        user = payload.pop('user', {'sec_uid': 'MS4wLjABAAAA-real'})
        return ir.register_resolved_mystery(self.conn, user, **payload)

    def test_resolved_mystery_ignores_the_level_threshold(self):
        matched = self.register()

        self.assertEqual('示例用户甲', matched['real_name'])
        self.assertEqual(
            'resolved_mystery',
            self.conn.execute(
                'SELECT source FROM identity_profiles'
            ).fetchone()[0],
        )

    def test_unresolved_mystery_is_never_promoted(self):
        for real_name in ('', '神秘人339175', 'dou8328160', '神秘人二阶'):
            with self.subTest(real_name=real_name):
                self.assertIsNone(self.register(real_name=real_name))

        self.assertEqual(
            0,
            self.conn.execute(
                'SELECT COUNT(*) FROM identity_profiles'
            ).fetchone()[0],
        )

    def test_resolved_mystery_records_its_anonymous_alias_for_the_day(self):
        self.register()

        self.assertEqual(
            [('神秘人339175', 'mystery_number', '2026-08-17')],
            [
                tuple(row)
                for row in self.conn.execute('''
                    SELECT alias, alias_type, beijing_date
                    FROM identity_alias_history
                ''').fetchall()
            ],
        )


class AnonymousAliasPredicateTests(unittest.TestCase):
    def test_anonymous_forms(self):
        for alias in ('dou8328160', '神秘人339175', '神秘人二阶', '神秘人十二阶'):
            with self.subTest(alias=alias):
                self.assertTrue(ir.is_anonymous_alias(alias))

    def test_real_names_are_not_anonymous(self):
        for alias in ('示例用户甲', '', None, '阶梯'):
            with self.subTest(alias=alias):
                self.assertFalse(ir.is_anonymous_alias(alias))


_LEGACY_SCHEMA = '''
    CREATE TABLE mystery_records (
        sec_uid TEXT NOT NULL,
        display TEXT,
        real_name TEXT DEFAULT '',
        nickname TEXT DEFAULT '',
        extra TEXT DEFAULT '{}',
        last_room_id TEXT DEFAULT '',
        seen_room_ids TEXT DEFAULT '',
        first_seen INTEGER DEFAULT 0,
        last_seen INTEGER DEFAULT 0,
        enter_count INTEGER DEFAULT 0,
        gift_count INTEGER DEFAULT 0,
        chat_count INTEGER DEFAULT 0,
        is_regular INTEGER DEFAULT 0,
        PRIMARY KEY (sec_uid, display)
    );
    CREATE TABLE display_names (
        sec_uid TEXT NOT NULL,
        display TEXT NOT NULL,
        seen_count INTEGER DEFAULT 1,
        first_seen INTEGER DEFAULT 0,
        last_seen INTEGER DEFAULT 0,
        PRIMARY KEY (sec_uid, display)
    );
'''


class LegacyMysteryImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = _memory_db()
        self.addCleanup(self.conn.close)
        self.conn.executescript(_LEGACY_SCHEMA)
        ir.invalidate_cache()

    def add_record(self, sec_uid, display, real_name, extra='{}',
                   last_seen=1786982399, first_seen=1786800000, is_regular=0,
                   last_room_id='room-a'):
        self.conn.execute('''
            INSERT INTO mystery_records
                (sec_uid, display, real_name, extra, last_room_id,
                 first_seen, last_seen, is_regular)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (sec_uid, display, real_name, extra, last_room_id,
              first_seen, last_seen, is_regular))
        self.conn.commit()

    def add_alias(self, sec_uid, display, last_seen):
        self.conn.execute('''
            INSERT INTO display_names (sec_uid, display, last_seen)
            VALUES (?, ?, ?)
        ''', (sec_uid, display, last_seen))
        self.conn.commit()

    def identities(self):
        return [
            tuple(row)
            for row in self.conn.execute('''
                SELECT real_name, douyin_id, canonical_sec_uid, source
                FROM identity_profiles ORDER BY id
            ''').fetchall()
        ]

    def test_resolved_mystery_record_becomes_an_identity(self):
        self.add_record(
            'MS4wLjABAAAA-real', '神秘人339175', '示例用户甲',
            extra='{"unique_id": "kekeray", "follower_count": 1234}',
        )

        summary = ir.import_legacy_mystery_records(self.conn)

        self.assertEqual(1, summary['identities'])
        self.assertEqual(
            [('示例用户甲', 'kekeray', 'MS4wLjABAAAA-real', 'legacy_import')],
            self.identities(),
        )
        matched = ir.match_user(self.conn, {'sec_uid': 'MS4wLjABAAAA-real'})
        self.assertEqual('示例用户甲', matched['real_name'])
        self.assertEqual(1234, matched['follower_count'])

    def test_unresolved_records_are_never_promoted(self):
        self.add_record('MS4wLjABAAAA-a', '神秘人1', '')
        self.add_record('MS4wLjABAAAA-b', '神秘人2', '神秘人2')
        self.add_record('MS4wLjABAAAA-c', 'dou8328160', 'dou8328160')
        self.add_record('MS4wLjABAAAA-d', '神秘人二阶', '神秘人二阶')
        self.add_record('MS4wLjABAAAA-e', '神秘人3', '?')

        summary = ir.import_legacy_mystery_records(self.conn)

        self.assertEqual(0, summary['identities'])
        self.assertEqual([], self.identities())

    def test_records_without_a_usable_sec_uid_are_skipped(self):
        self.add_record('', '神秘人1', '示例用户甲')
        self.add_record('111111', '神秘人2', '示例用户甲')

        summary = ir.import_legacy_mystery_records(self.conn)

        self.assertEqual(0, summary['identities'])
        self.assertEqual([], self.identities())

    def test_ordinary_user_history_is_never_backfilled(self):
        self.add_record(
            'MS4wLjABAAAA-plain', '普通观众', '普通demo_005', is_regular=1
        )

        summary = ir.import_legacy_mystery_records(self.conn)

        self.assertEqual(0, summary['identities'])
        self.assertEqual([], self.identities())

    def test_import_is_idempotent(self):
        self.add_record(
            'MS4wLjABAAAA-real', '神秘人339175', '示例用户甲',
            extra='{"unique_id": "kekeray"}',
        )
        self.add_alias('MS4wLjABAAAA-real', '神秘人339175', 1786982399)

        first = ir.import_legacy_mystery_records(self.conn)
        second = ir.import_legacy_mystery_records(self.conn)

        self.assertEqual(first['identities'], second['identities'])
        for table in (
            'identity_profiles', 'identity_identifiers', 'identity_alias_history'
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    1,
                    self.conn.execute(
                        f'SELECT COUNT(*) FROM {table}'
                    ).fetchone()[0],
                )

    def test_legacy_aliases_use_only_their_own_provable_last_date(self):
        self.add_record(
            'MS4wLjABAAAA-real', '神秘人339175', '示例用户甲',
            extra='{"unique_id": "kekeray"}',
        )
        self.add_alias('MS4wLjABAAAA-real', '神秘人339175', 1786982399)
        self.add_alias('MS4wLjABAAAA-real', 'dou8328160', 1786800000)
        self.add_alias('MS4wLjABAAAA-real', '示例用户甲', 1786982399)
        self.add_alias('MS4wLjABAAAA-other', '神秘人999', 1786982399)

        ir.import_legacy_mystery_records(self.conn)

        self.assertEqual([
            ('dou8328160', 'dou', '2026-08-15', 'legacy_import'),
            ('神秘人339175', 'mystery_number', '2026-08-17', 'legacy_import'),
        ], [
            tuple(row)
            for row in self.conn.execute('''
                SELECT alias, alias_type, beijing_date, source
                FROM identity_alias_history ORDER BY beijing_date, alias
            ''').fetchall()
        ])

    def test_alias_summary_reports_rows_not_write_attempts(self):
        self.add_record(
            'MS4wLjABAAAA-real', '神秘人339175', '示例用户甲',
            extra='{"unique_id": "kekeray"}',
        )
        # 旧记录自身的 display 和 display_names 里同名同日的行会落到同一行别名，
        # 汇总数字必须报实际行数，不能把重复写入尝试也算进去。
        self.add_alias('MS4wLjABAAAA-real', '神秘人339175', 1786982399)
        self.add_alias('MS4wLjABAAAA-real', 'dou8328160', 1786800000)

        summary = ir.import_legacy_mystery_records(self.conn)

        rows = self.conn.execute(
            'SELECT COUNT(*) FROM identity_alias_history'
        ).fetchone()[0]
        self.assertEqual(2, rows)
        self.assertEqual(rows, summary['aliases'])

    def test_legacy_rows_are_left_untouched(self):
        self.add_record(
            'MS4wLjABAAAA-real', '神秘人339175', '示例用户甲',
            extra='{"unique_id": "kekeray"}',
        )
        before = [
            tuple(row)
            for row in self.conn.execute('SELECT * FROM mystery_records')
        ]

        ir.import_legacy_mystery_records(self.conn)

        self.assertEqual(before, [
            tuple(row)
            for row in self.conn.execute('SELECT * FROM mystery_records')
        ])

    def test_missing_legacy_tables_do_not_raise(self):
        self.conn.executescript(
            'DROP TABLE mystery_records; DROP TABLE display_names;'
        )

        self.assertEqual(
            {'identities': 0, 'aliases': 0, 'skipped': 0},
            ir.import_legacy_mystery_records(self.conn),
        )


if __name__ == '__main__':
    unittest.main()
