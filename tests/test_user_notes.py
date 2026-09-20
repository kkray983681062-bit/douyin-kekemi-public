import os
import tempfile
import unittest

import user_notes


class UserNotesTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        handle.close()
        self.db = handle.name

    def tearDown(self):
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_set_and_get_roundtrip_trims(self):
        user_notes.set_note('u1', '  VIP 客户  ', actor_id='super1', now=1000, db_path=self.db)
        self.assertEqual({'u1': 'VIP 客户'}, user_notes.get_notes(db_path=self.db))

    def test_empty_note_clears_from_listing(self):
        user_notes.set_note('u1', '备注', db_path=self.db)
        user_notes.set_note('u1', '', db_path=self.db)
        self.assertEqual({}, user_notes.get_notes(db_path=self.db))

    def test_upsert_overwrites(self):
        user_notes.set_note('u1', '旧备注', now=1, db_path=self.db)
        user_notes.set_note('u1', '新备注', now=2, db_path=self.db)
        self.assertEqual({'u1': '新备注'}, user_notes.get_notes(db_path=self.db))

    def test_get_notes_filters_by_ids(self):
        user_notes.set_note('u1', 'a', db_path=self.db)
        user_notes.set_note('u2', 'b', db_path=self.db)
        self.assertEqual({'u1': 'a'}, user_notes.get_notes(['u1'], db_path=self.db))
        self.assertEqual({}, user_notes.get_notes([], db_path=self.db))

    def test_validation_rejects_blank_id_and_overlong_note(self):
        with self.assertRaises(ValueError):
            user_notes.set_note('', 'x', db_path=self.db)
        with self.assertRaises(ValueError):
            user_notes.set_note('u1', 'x' * (user_notes.MAX_NOTE_LEN + 1), db_path=self.db)

    def test_get_notes_on_fresh_db_is_empty(self):
        self.assertEqual({}, user_notes.get_notes(db_path=self.db))


if __name__ == '__main__':
    unittest.main()
