from pathlib import Path
import os
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from save_format import (Bdb, KNOW, Knowledge, MAX_BLOB, RING_NAMES, SaveDocument,
                         SaveError, assert_game_closed, crc64, fnv1a, resolve_index)


def bdb(name: str = "Hero", level: int = 25) -> bytes:
    output = bytearray(116)
    output[:4] = b"BDB1"

    def section(field: int, data: bytes, count: int) -> None:
        start = len(output)
        struct.pack_into("<II", output, field, start - field, count)
        output.extend(data)

    encoded = name.encode("utf-8")
    pool = (struct.pack("<H", 4) + b"name"
            + struct.pack("<H", len(encoded)) + encoded
            + struct.pack("<H", 5) + b"level")
    section(4, bytes([20, 14, 8]), 3)
    section(12, struct.pack("<III", 1, 7, level), 3)
    section(60, pool, len(pool))
    section(68, struct.pack("<IIII", 1, 0, 9 + len(encoded), 0), 2)
    section(100, struct.pack("<IIII", fnv1a("level"), 1, fnv1a("name"), 1), 2)
    section(108, struct.pack("<II", 3, 2), 2)
    return bytes(output)


def know() -> bytes:
    return struct.pack("<IIIIIII", 2, 1, 2, 500, 7, 3, 1)


def container(records: list[tuple[int, bytes, bytes]]) -> bytes:
    compressor = zstd.ZstdCompressor()
    table, data = [], []
    for owner, tag, payload in records:
        compressed = compressor.compress(payload)
        table.append(struct.pack("<I4sI", owner, tag, len(compressed)))
        data.append(compressed)
    table_bytes, data_bytes = b"".join(table), b"".join(data)
    return (struct.pack("<4sIQQ", b"KSC1", len(records),
                        crc64(table_bytes), crc64(data_bytes))
            + table_bytes + data_bytes)


def sample() -> bytes:
    return container([(123, b"CHAR", bdb()), (123, KNOW, know()),
                      (456, b"CHAR", bdb("Mage")), (456, b"FOWR", b"opaque-map"),
                      (0, b"KNPL", b"opaque-player-list")])


class ParserTests(unittest.TestCase):
    def test_crc_known_vector(self):
        self.assertEqual(crc64(b"123456789"), 0x995DC9BBDF1939FA)
        self.assertEqual(crc64(b""), 0)

    def test_noop_is_exact(self):
        raw = sample()
        doc = SaveDocument(raw)
        self.assertEqual(doc.owners, [123, 456])
        self.assertEqual(doc.serialize(), raw)

    def test_targeted_edits_preserve_all_other_data(self):
        doc = SaveDocument(sample())
        doc.edit_character(123, "Abcd", 25)
        doc.edit_knowledge(123, 7, 8)
        output = SaveDocument(doc.serialize())
        self.assertEqual(Bdb(output.blob(123, b"CHAR").data).character(), ("Abcd", 25))
        self.assertEqual(Knowledge(output.blob(123, KNOW).data).entries[7][0], 8)
        for original, edited in zip(doc.blobs, output.blobs):
            self.assertEqual(original.data, edited.data)
            if original.owner != 123:
                self.assertEqual(original.compressed, edited.compressed)
        before = doc.blob(123, b"CHAR").original
        after = output.blob(123, b"CHAR").data
        self.assertEqual(len(before), len(after))
        db = Bdb(before)
        _, start, length = db.string(db.field("name"))
        allowed = set(range(start, start + length))
        allowed.update(range(db.values + 4 * db.field("level"),
                             db.values + 4 * db.field("level") + 4))
        self.assertTrue(all(i in allowed for i, (a, b) in enumerate(zip(before, after))
                            if a != b))
        self.assertEqual(len(doc.changes), 2)

    def test_reverting_edits_restores_exact_container(self):
        doc = SaveDocument(sample())
        doc.edit_character(123, "Abcd", 25)
        doc.edit_knowledge(123, 7, 8)
        doc.edit_character(123, "Hero", 25)
        doc.edit_knowledge(123, 7, 1)
        self.assertEqual(doc.changes, {})
        self.assertEqual(doc.serialize(), doc.original)

    def test_name_unicode_lengths(self):
        original = Bdb(bdb("Héro"))
        self.assertEqual(Bdb(original.patch_character("Émil", 25)).character(), ("Émil", 25))
        for name in ("Hero", "", "A\nBCD", "longer"):
            with self.subTest(name=name), self.assertRaises(SaveError):
                original.patch_character(name, 25)

    def test_invalid_levels_and_values(self):
        doc = SaveDocument(sample())
        for value in (0, -1, 2**32, True, 1.5, 24, 26, 30):
            with self.subTest(value=value), self.assertRaises(SaveError):
                doc.edit_character(123, "Hero", value)
        for value in (-1, 2**32, True):
            with self.subTest(value=value), self.assertRaises(SaveError):
                doc.edit_knowledge(123, 7, value)
        with self.assertRaises(SaveError):
            doc.edit_knowledge(123, 999, 0)
        self.assertFalse(doc.changes)
        self.assertEqual(doc.serialize(), doc.original)

    def test_knowledge_is_not_assumed_sorted(self):
        table = Knowledge(know())
        self.assertEqual(list(table.entries), [500, 7])
        self.assertEqual(Knowledge(table.patch(500, 0)).entries[500][0], 0)
        self.assertEqual(Knowledge(table.patch(7, 2**32 - 1)).entries[7][0], 2**32 - 1)

    def test_malformed_knowledge(self):
        for raw in (b"", know()[:-1], struct.pack("<IIIIIII", 2, 1, 2, 7, 7, 1, 2),
                    struct.pack("<III", 3, 0, 0)):
            with self.subTest(raw=raw), self.assertRaises(SaveError):
                Knowledge(raw)

    def test_corrupt_containers(self):
        raw = sample()
        for corrupted in (raw[:20], raw[:-1], b"XXXX" + raw[4:],
                          raw[:80] + bytes([raw[80] ^ 1]) + raw[81:]):
            with self.subTest(size=len(corrupted)), self.assertRaises(SaveError):
                SaveDocument(corrupted)
        with self.assertRaises(SaveError):
            SaveDocument(container([(1, b"CHAR", bdb()), (1, b"CHAR", bdb())]))

    def test_trailing_data_and_invalid_zstd_with_valid_crc(self):
        frame = zstd.ZstdCompressor().compress(b"payload")
        for payload in (frame + b"extra", b"not-zstd"):
            table = struct.pack("<I4sI", 1, b"CHAR", len(payload))
            raw = struct.pack("<4sIQQ", b"KSC1", 1, crc64(table), crc64(payload))
            with self.assertRaises(SaveError):
                SaveDocument(raw + table + payload)

    def test_decompression_limit(self):
        raw = container([(1, b"CHAR", b"x" * (MAX_BLOB + 1))])
        with self.assertRaisesRegex(SaveError, "safety limit"):
            SaveDocument(raw)

    def test_invalid_bdb(self):
        original = bdb()
        cases = [b"BAD!" + original[4:], original[:100]]
        data = bytearray(original)
        struct.pack_into("<I", data, 4, 0xFFFFFFFF)
        cases.append(bytes(data))
        data = bytearray(original)
        db = Bdb(original)
        struct.pack_into("<I", data, db.links, db.count + 1)
        cases.append(bytes(data))
        for raw in cases:
            with self.subTest(raw=raw[:12]), self.assertRaises(SaveError):
                Bdb(raw)
        data = bytearray(original)
        data[db.types + db.field("level")] = 21
        with self.assertRaises(SaveError):
            Bdb(bytes(data)).character()

    def test_field_description(self):
        self.assertEqual(Bdb(bdb()).describe_fields(),
                         [("node 1/level", "uint32 25"), ("node 1/name", "'Hero'")])

    def test_shared_name_storage_blocks_rename_but_allows_level(self):
        data = bytearray(bdb("name"))
        db = Bdb(bytes(data))
        struct.pack_into("<I", data, db.values + 4 * db.field("name"), 1)
        shared = Bdb(bytes(data))
        with self.assertRaisesRegex(SaveError, "shares storage"):
            shared.patch_character("abcd", 25)
        self.assertEqual(Bdb(shared.patch_character("name", 25)).character(), ("name", 25))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saves = self.root / "remote"
        self.saves.mkdir()
        for name in RING_NAMES[:-1]:
            (self.saves / name).write_bytes(sample())
        self.index = self.saves / "characters-index"
        self.index.write_text('{"latest":2,"time":123,"deleted":false}', encoding="utf-8")
        self.doc = SaveDocument.open(self.index)
        self.doc.edit_character(123, "Abcd", 25)

    def test_active_index_and_snapshot(self):
        self.assertEqual(self.doc.source, self.saves / "characters-2")
        snapshot = SaveDocument.open(self.saves / "characters")
        self.assertIsNone(snapshot.index_path)
        self.index.write_text('{"latest":0}', encoding="utf-8")
        self.assertEqual(resolve_index(self.index)[0], self.saves / "characters")

    def test_invalid_index(self):
        for raw in ('invalid', '[]', '{"latest":true}', '{"latest":10}',
                    '{"latest":-1}', '{"latest":2,"deleted":true}',
                    '{"latest":"2"}', '{"latest":2,"deleted":null}'):
            self.index.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(SaveError):
                SaveDocument.open(self.index)

    def test_export_and_backup(self):
        destination = self.root / "edited.ksc"
        backup = self.doc.export(destination)
        self.assertEqual(backup.read_bytes(), self.doc.original)
        self.assertEqual(Bdb(SaveDocument.open(destination).blob(123, b"CHAR").data
                             ).character(), ("Abcd", 25))
        self.doc.assert_unchanged()

    def test_export_collisions_and_live_folder(self):
        destination = self.root / "edited.ksc"
        self.doc.export(destination)
        with self.assertRaises(SaveError):
            self.doc.export(destination)
        with self.assertRaises(SaveError):
            self.doc.export(self.saves / "edited.ksc")
        with self.assertRaises(SaveError):
            self.doc.export(self.root / "characters-1")
        self.doc.assert_unchanged()

    def test_changed_source_or_index(self):
        (self.saves / "characters-2").write_bytes(b"modified")
        with self.assertRaises(SaveError):
            self.doc.export(self.root / "edited.ksc")
        (self.saves / "characters-2").write_bytes(self.doc.original)
        self.index.write_text('{"latest":1}', encoding="utf-8")
        with self.assertRaises(SaveError):
            self.doc.export(self.root / "edited.ksc")

    @patch("save_format.assert_game_closed")
    def test_apply_active_full_backup(self, check):
        before = {name: (self.saves / name).read_bytes() for name in RING_NAMES}
        backup = self.doc.apply_active(self.root / "backup", cloud_disabled=True)
        for name, data in before.items():
            self.assertEqual((backup / name).read_bytes(), data)
            if name != "characters-2":
                self.assertEqual((self.saves / name).read_bytes(), data)
        updated = SaveDocument.open(self.index)
        self.assertEqual(Bdb(updated.blob(123, b"CHAR").data).character(), ("Abcd", 25))
        self.assertEqual(check.call_count, 2)

    @patch("save_format.assert_game_closed")
    def test_apply_requires_active_cloud_and_new_external_backup(self, _check):
        with self.assertRaises(SaveError):
            self.doc.apply_active(self.root / "backup", cloud_disabled=False)
        with self.assertRaises(SaveError):
            self.doc.apply_active(self.saves / "backup", cloud_disabled=True)
        with self.assertRaises(SaveError):
            self.doc.apply_active(self.root, cloud_disabled=True)
        snapshot = SaveDocument.open(self.saves / "characters")
        snapshot.edit_character(123, "Abcd", 25)
        with self.assertRaises(SaveError):
            snapshot.apply_active(self.root / "backup", cloud_disabled=True)

    @patch("save_format.assert_game_closed")
    def test_cloud_off_apply_blocks_steam_remote_profile(self, check):
        remote = self.root / "userdata" / "123" / "1203620" / "remote"
        remote.mkdir(parents=True)
        (remote / "characters-2").write_bytes(sample())
        index = remote / "characters-index"
        index.write_bytes(self.index.read_bytes())
        doc = SaveDocument.open(index)
        doc.edit_character(123, "Abcd", 25)
        with self.assertRaisesRegex(SaveError, "separate profile"):
            doc.apply_active(self.root / "backup", cloud_disabled=True)
        check.assert_not_called()
        self.assertFalse((self.root / "backup").exists())
        doc.assert_unchanged()
        doc.export(self.root / "cloud-export.ksc")

    @patch("save_format.assert_game_closed", side_effect=SaveError("Close Steam"))
    def test_running_process_aborts_before_backup(self, _check):
        with self.assertRaises(SaveError):
            self.doc.apply_active(self.root / "backup", cloud_disabled=True)
        self.assertFalse((self.root / "backup").exists())
        self.doc.assert_unchanged()

    def test_change_during_backup_aborts(self):
        calls = 0
        def race():
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.saves / "characters-1").write_bytes(b"new-generation")
        with patch("save_format.assert_game_closed", side_effect=race):
            with self.assertRaises(SaveError):
                self.doc.apply_active(self.root / "backup", cloud_disabled=True)
        self.doc.assert_unchanged()
        self.assertFalse(list(self.saves.glob(".character-editor-*")))

    @patch("save_format.assert_game_closed")
    def test_failed_replace_keeps_original_and_backup(self, _check):
        with patch("save_format.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.doc.apply_active(self.root / "backup", cloud_disabled=True)
        self.doc.assert_unchanged()
        self.assertEqual((self.root / "backup" / "characters-2").read_bytes(), self.doc.original)
        self.assertFalse(list(self.saves.glob(".character-editor-*")))

    @patch("save_format.subprocess.run")
    def test_process_check_blocks_game_and_steam(self, run):
        run.return_value.stdout = "explorer\nsteam\n"
        with patch("save_format.os.name", "nt"), self.assertRaises(SaveError):
            assert_game_closed()
        run.return_value.stdout = "explorer\nenshrouded\n"
        with patch("save_format.os.name", "nt"), self.assertRaises(SaveError):
            assert_game_closed()


@unittest.skipUnless(os.environ.get("ENSHROUDED_TEST_SAVE_DIR"),
                     "Set ENSHROUDED_TEST_SAVE_DIR for opt-in read-only real-save checks.")
class RealSaveTests(unittest.TestCase):
    def test_all_snapshots_and_temporary_edit_roundtrip(self):
        folder = Path(os.environ["ENSHROUDED_TEST_SAVE_DIR"])
        for name in RING_NAMES[:-1]:
            with self.subTest(snapshot=name):
                path = folder / name
                raw = path.read_bytes()
                doc = SaveDocument.open(path)
                self.assertEqual(doc.serialize(), raw)
                for blob in doc.blobs:
                    if blob.tag == b"CHAR":
                        b = Bdb(blob.data)
                        original_name, original_level = b.character()
                        b.describe_fields()
                        first = original_name[0]
                        base = {1: 0x41, 2: 0xA1, 3: 0x4E00, 4: 0x10000}[
                            len(first.encode("utf-8"))]
                        replacement = chr(base + (ord(first) == base))
                        edited_name = replacement + original_name[1:]
                        doc.edit_character(blob.owner, edited_name, original_level)
                        break
                with tempfile.TemporaryDirectory() as directory:
                    destination = Path(directory) / "edited.ksc"
                    backup = doc.export(destination)
                    self.assertEqual(backup.read_bytes(), raw)
                    updated = SaveDocument.open(destination)
                    self.assertEqual(Bdb(updated.blob(blob.owner, b"CHAR").data).character(),
                                     (edited_name, original_level))
                    for old, new in zip(doc.blobs, updated.blobs):
                        self.assertEqual(old.data, new.data)
                        if old.data == old.original:
                            self.assertEqual(old.compressed, new.compressed)
                self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
