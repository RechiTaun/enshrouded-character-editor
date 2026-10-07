import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_save_format import sample
from save_format import SaveDocument, SaveError
from steam_storage import parse_catalogue, validate_registration


def catalogue(files):
    entries = []
    for name, contents in files.items():
        sha = hashlib.sha1(contents, usedforsecurity=False).hexdigest()
        entries.append(f'"{name}" {{ "size" "{len(contents)}" "sha" "{sha}" }}')
    return ('"1203620" {\n' + "\n".join(entries) + "\n}").encode()


class SteamStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.remote = self.root / "userdata" / "123" / "1203620" / "remote"
        self.remote.mkdir(parents=True)
        self.source = self.remote / "characters-2"
        self.source.write_bytes(sample())
        self.index = self.remote / "characters-index"
        self.index.write_bytes(b'{"latest":2,"deleted":false}')
        self.cache = self.remote.parent / "remotecache.vdf"
        self.metadata = catalogue({self.source.name: sample(),
                                   self.index.name: self.index.read_bytes()})
        self.cache.write_bytes(self.metadata)
        self.doc = SaveDocument.open(self.index)
        self.doc.edit_character(123, "Abcd", 25)

    def test_registered_provider_saves_only_active_file_and_backs_up_catalogue(self):
        with patch("save_format.assert_game_closed") as check:
            backup = self.doc.apply_active(self.root / "backup", cloud_enabled=True)
        self.assertEqual(check.call_count, 2)
        self.assertEqual((backup / self.source.name).read_bytes(), sample())
        self.assertEqual((backup / "remotecache.vdf").read_bytes(), self.metadata)
        self.assertEqual(self.cache.read_bytes(), self.metadata)
        self.assertEqual((backup / self.index.name).read_bytes(), self.index.read_bytes())
        updated = SaveDocument.open(self.index)
        self.assertEqual(updated.blob(123, b"CHAR").data, self.doc.blob(123, b"CHAR").data)
        self.assertEqual(updated.blob(456, b"CHAR").compressed,
                         self.doc.blob(456, b"CHAR").compressed)
        with self.assertRaisesRegex(SaveError, "does not match"):
            updated.assert_save_provider(cloud_disabled=False, cloud_enabled=True)

    def test_cloud_disabled_or_ambiguous_provider_cannot_write_remote(self):
        for kwargs in ({"cloud_disabled": True}, {},
                       {"cloud_disabled": True, "cloud_enabled": True},
                       {"cloud_enabled": 1}, {"cloud_disabled": "yes"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SaveError):
                self.doc.apply_active(self.root / "backup", **kwargs)
        self.doc.assert_unchanged()

    def test_missing_unregistered_and_stale_metadata_abort(self):
        cases = (None, catalogue({}), catalogue({self.source.name: b"stale",
                                               self.index.name: self.index.read_bytes()}))
        for raw in cases:
            if raw is None:
                self.cache.unlink()
            else:
                self.cache.write_bytes(raw)
            doc = SaveDocument.open(self.index)
            doc.edit_character(123, "Abcd", 25)
            with self.subTest(), self.assertRaises(SaveError):
                doc.apply_active(self.root / "backup", cloud_enabled=True)
            self.assertEqual(self.source.read_bytes(), sample())
            self.assertFalse((self.root / "backup").exists())

    def test_catalogue_change_at_load_or_during_backup_aborts(self):
        self.cache.write_bytes(self.metadata + b"\n")
        with self.assertRaisesRegex(SaveError, "catalogue changed"):
            self.doc.apply_active(self.root / "backup", cloud_enabled=True)
        self.cache.write_bytes(self.metadata)
        calls = 0

        def race():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.cache.write_bytes(self.metadata + b"\n")

        with patch("save_format.assert_game_closed", side_effect=race):
            with self.assertRaisesRegex(SaveError, "catalogue changed"):
                self.doc.apply_active(self.root / "backup", cloud_enabled=True)
        self.assertEqual(self.source.read_bytes(), sample())
        self.assertFalse(list(self.remote.glob(".character-editor-*")))

    def test_running_steam_aborts_before_backup(self):
        with patch("save_format.assert_game_closed", side_effect=SaveError("Close Steam")):
            with self.assertRaisesRegex(SaveError, "Close Steam"):
                self.doc.apply_active(self.root / "backup", cloud_enabled=True)
        self.assertFalse((self.root / "backup").exists())
        self.doc.assert_unchanged()

    def test_parser_bounds_ambiguity_and_hash_checks(self):
        for raw in (b'"a" {', b'"a" "b" }', b'"a" "b" "A" "c"',
                    b'"a" "\\n"', b'"a" {' * 10 + b"}" * 10, b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(SaveError):
                parse_catalogue(raw)
        validate_registration(self.metadata, self.source.name, sample())
        with self.assertRaisesRegex(SaveError, "does not match"):
            validate_registration(self.metadata, self.source.name, b"changed")
        self.assertEqual(parse_catalogue(b'// comment\n"a" { "b" "c" }'),
                         {"a": {"b": "c"}})


if __name__ == "__main__":
    unittest.main()
