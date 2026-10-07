from hashlib import sha256
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from test_player_state import character, knowledge
from test_save_format import container
from character_transfer import (EXTENSION, HEADER, MAGIC, character_identity,
                                export_character, parse_character, read_character,
                                write_character)
from game_rules import GameRules
from player_state import PlayerState
from save_format import Bdb, KNOW, SaveDocument, SaveError, fnv1a


def transfer_rules():
    rules = Mock(spec=GameRules)
    rules.character_transfer_build.return_value = b"\xee" * 32
    return rules


def identified_character(owner=123, *, raw=None, cloud_id=0):
    original = character(raw)
    db = Bdb(original)
    output = bytearray(original)

    def section(field, data, count):
        struct.pack_into("<II", output, field, len(output) - field, count)
        output.extend(data)

    section(4, original[db.types:db.types + db.count] + bytes((15, 15, 8)), db.count + 3)
    section(12, original[db.values:db.values + db.count * 4]
            + struct.pack("<III", 1, cloud_id, 8), db.count + 3)
    section(36, struct.pack("<I", owner) + b"\x22" * 12, 1)
    keys, count = db.section(100, 8)
    section(100, original[keys:keys + count * 8]
            + b"".join(struct.pack("<II", fnv1a(label), 1)
                       for label in ("id", "cloudId", "version")), count + 3)
    links, count = db.section(108, 4)
    section(108, original[links:links + count * 4]
            + struct.pack("<III", db.count + 1, db.count + 2, db.count + 3), count + 3)
    return bytes(output)


def appearance():
    output = bytearray(116)
    output[:4] = b"BDB1"

    def section(field, data, count):
        struct.pack_into("<II", output, field, len(output) - field, count)
        output.extend(data)

    section(4, bytes((20, 20, 20, 8)), 4)
    section(12, struct.pack("<IIII", 1, 2, 3, 2), 4)
    section(100, b"".join(struct.pack("<II", fnv1a(label), 1)
                          for label in ("items", "setup", "version")), 3)
    section(108, struct.pack("<III", 2, 3, 4), 3)
    return bytes(output)


def extra_field(raw, label, kind=None, value=0, *, node=None):
    db = Bdb(raw)
    output = bytearray(raw)

    def section(field, data, count):
        struct.pack_into("<II", output, field, len(output) - field, count)
        output.extend(data)

    if node is None:
        node = db.count
        section(4, raw[db.types:db.types + db.count] + bytes([kind]), db.count + 1)
        section(12, raw[db.values:db.values + db.count * 4] + struct.pack("<I", value),
                db.count + 1)
    keys, count = db.section(100, 8)
    section(100, raw[keys:keys + count * 8] + struct.pack("<II", fnv1a(label), 1), count + 1)
    links, count = db.section(108, 4)
    section(108, raw[links:links + count * 4] + struct.pack("<I", node + 1), count + 1)
    return bytes(output)


def array_alias(raw, label, node):
    db = Bdb(raw)
    descriptors, descriptor_count = db.section(28, 8)
    children, child_count = db.section(76, 4)
    output = bytearray(extra_field(raw, label, 21, descriptor_count + 1))
    for field, data, count in (
            (28, raw[descriptors:descriptors + descriptor_count * 8]
             + struct.pack("<II", 1, child_count + 1), descriptor_count + 1),
            (76, raw[children:children + child_count * 4]
             + struct.pack("<I", node + 1), child_count + 1)):
        struct.pack_into("<II", output, field, len(output) - field, count)
        output.extend(data)
    return bytes(output)


def records(owner=123, **kwargs):
    return [(owner, b"CHAR", identified_character(owner, **kwargs)),
            (owner, b"COUT", appearance()), (owner, KNOW, knowledge()),
            (owner, b"FOWR", b"FGOW\x00opaque-character-map")]


def envelope(archive, *, build=b"\xee" * 32):
    return HEADER.pack(MAGIC, len(archive), build, sha256(archive).digest()) + archive


class CharacterTransferTests(unittest.TestCase):
    def setUp(self):
        self.rules = transfer_rules()
        self.doc = SaveDocument(container(records() + records(456)
                                          + [(0, b"KNPL", b"global-knowledge")]))

    def share(self, owner=123):
        return export_character(self.doc, owner, self.rules)

    def destination(self):
        values = [(owner, tag,
                   Bdb(data).patch_character("Home", 25) if tag == b"CHAR"
                   else b"FGOW\x00recipient-map" if tag == b"FOWR" else data)
                  for owner, tag, data in records(789)]
        return SaveDocument(container(values + [(0, b"KNPL", b"recipient-global")]))

    def test_export_without_edits_contains_only_selected_complete_character(self):
        original = self.doc.serialize()
        raw = self.share()
        shared = parse_character(raw, self.rules)
        self.assertEqual((shared.owner, shared.name, shared.level), (123, "Hero", 25))
        exported = SaveDocument(shared.archive)
        self.assertEqual({b.owner for b in exported.blobs}, {123})
        self.assertEqual(len(exported.blobs), 4)
        for b in exported.blobs:
            self.assertEqual(b.compressed, self.doc.blob(123, b.tag).compressed)
        self.assertEqual(self.doc.serialize(), original)
        self.assertFalse(self.doc.changes)

    def test_import_adds_records_and_serializes_unchanged_appended_blobs(self):
        dest = self.destination()
        old = list(dest.blobs)
        shared = parse_character(self.share(), self.rules)
        plan = dest.prepare_character_import(shared, self.rules)
        self.assertFalse(dest.changes)
        self.assertEqual(dest.owners, [789])
        owner = dest.stage_character_import(plan)
        self.assertNotIn(owner, (0, 123, 789))
        self.assertEqual(set(dest.owners), {owner, 789})
        result = SaveDocument(dest.serialize())
        for b in old:
            self.assertEqual(result.blob(b.owner, b.tag).compressed, b.compressed)
        for b in SaveDocument(shared.archive).blobs:
            imported = result.blob(owner, b.tag)
            if b.tag == b"CHAR":
                restored = bytearray(imported.data)
                db = Bdb(imported.data)
                at = db.section(36, 16)[0] + 16 * (db.node(db.field("id"))[1] - 1)
                restored[at:at + 16] = shared.identity
                self.assertEqual(bytes(restored), b.data)
                self.assertNotEqual(imported.compressed, b.compressed)
            else:
                self.assertEqual(imported.data, b.data)
                self.assertEqual(imported.compressed, b.compressed)
        self.assertEqual(result.blob(0, b"KNPL").data, b"recipient-global")
        self.assertEqual(PlayerState(result.blob(owner, b"CHAR").data).experience(),
                         PlayerState(self.doc.blob(123, b"CHAR").data).experience())
        self.assertEqual(PlayerState(result.blob(owner, b"CHAR").data).skills(),
                         PlayerState(self.doc.blob(123, b"CHAR").data).skills())
        self.assertEqual(PlayerState(result.blob(owner, b"CHAR").data).inventory(),
                         PlayerState(self.doc.blob(123, b"CHAR").data).inventory())

    def test_export_and_import_keep_pending_edits_and_other_owner_changes(self):
        self.doc.edit_character(123, "Mage", 25)
        self.doc.edit_character(456, "Abcd", 25)
        shared = parse_character(self.share(), self.rules)
        self.assertEqual(shared.name, "Mage")
        dest = self.destination()
        dest.edit_character(789, "Abcd", 25)
        old_changes = dict(dest.changes)
        owner = dest.stage_character_import(dest.prepare_character_import(shared, self.rules))
        result = SaveDocument(dest.serialize())
        self.assertEqual(Bdb(result.blob(789, b"CHAR").data).character()[0], "Abcd")
        self.assertEqual(Bdb(result.blob(owner, b"CHAR").data).character()[0], "Mage")
        self.assertTrue(old_changes.items() <= dest.changes.items())
        self.assertIn("Import character", dest.changes[(owner, "import")])

    def test_equipment_payload_and_all_opaque_records_remain_exact(self):
        from armor_editing import add_armor
        from test_armor_editing import armor_character, armor_rule
        equipped = add_armor(PlayerState(armor_character()), armor_rule(), 30, 2)
        raw = PlayerState(equipped).payload
        source = SaveDocument(container(records(raw=raw)))
        shared = parse_character(export_character(source, 123, self.rules), self.rules)
        dest = self.destination()
        owner = dest.stage_character_import(dest.prepare_character_import(shared, self.rules))
        result = SaveDocument(dest.serialize())
        self.assertNotEqual(character_identity(result.blob(owner, b"CHAR").data), shared.identity)
        self.assertEqual(PlayerState(result.blob(owner, b"CHAR").data).payload, raw)
        for b in source.blobs:
            if b.tag != b"CHAR":
                self.assertEqual(result.blob(owner, b.tag).data, b.data)

    def test_existing_owner_and_appearance_only_collision_require_renamed_fresh_copy(self):
        shared = parse_character(self.share(), self.rules)
        for dest in (self.doc, SaveDocument(container([(123, b"COUT", appearance())]))):
            before = dest.serialize()
            plan = dest.prepare_character_import(shared, self.rules)
            self.assertTrue(plan.rename_required)
            with self.assertRaisesRegex(SaveError, "Choose a new name"):
                dest.stage_character_import(plan)
            self.assertEqual(dest.serialize(), before)
            self.assertFalse(dest.changes)
            renamed = dest.prepare_character_import(shared, self.rules, name="A separate copy")
            self.assertFalse(renamed.rename_required)
            owner = dest.stage_character_import(renamed)
            self.assertNotIn(owner, (0, 123, 456))
            self.assertEqual(Bdb(dest.blob(owner, b"CHAR").data).character()[0], "A separate copy")
            for old in SaveDocument(before).blobs:
                self.assertEqual(dest.blob(old.owner, old.tag).data, old.data)
                self.assertEqual(dest.blob(old.owner, old.tag).compressed, old.compressed)

    def test_repeat_import_creates_another_renamed_copy_and_stale_preparation_is_rejected(self):
        dest = self.destination()
        shared = parse_character(self.share(), self.rules)
        plan = dest.prepare_character_import(shared, self.rules)
        dest.edit_character(789, "Abcd", 25)
        with self.assertRaisesRegex(SaveError, "Staged save changed"):
            dest.stage_character_import(plan)
        first = dest.stage_character_import(dest.prepare_character_import(shared, self.rules))
        before = dest.serialize()
        repeated = dest.prepare_character_import(shared, self.rules)
        self.assertTrue(repeated.rename_required)
        with self.assertRaisesRegex(SaveError, "Choose a new name"):
            dest.stage_character_import(repeated)
        self.assertEqual(dest.serialize(), before)
        second = dest.stage_character_import(
            dest.prepare_character_import(shared, self.rules, name="Second copy"))
        self.assertNotIn(second, (0, 123, 789, first))
        self.assertEqual(len(dest.owners), 3)

    def test_discard_removes_imports_and_restores_all_original_data(self):
        dest = self.destination()
        original = dest.serialize()
        dest.edit_character(789, "Abcd", 25)
        shared = parse_character(self.share(), self.rules)
        owner = dest.stage_character_import(dest.prepare_character_import(shared, self.rules))
        dest.edit_character(owner, "Mage", 25)
        dest.discard_changes()
        self.assertEqual(dest.serialize(), original)
        self.assertEqual(dest.owners, [789])
        self.assertFalse(dest.changes)
        self.assertFalse(dest.rule_sources)

    def test_repeat_file_after_renaming_matches_gameplay_without_original_identity_or_name(self):
        shared = parse_character(self.share(), self.rules)
        dest = self.destination()
        first = dest.stage_character_import(
            dest.prepare_character_import(shared, self.rules, name="A longer first copy"))
        self.assertNotEqual(character_identity(dest.blob(first, b"CHAR").data), shared.identity)
        self.assertNotEqual(Bdb(dest.blob(first, b"CHAR").data).character()[0], shared.name)
        repeat = dest.prepare_character_import(shared, self.rules)
        self.assertTrue(repeat.rename_required)
        second = dest.stage_character_import(
            dest.prepare_character_import(shared, self.rules, name="Another independent copy"))
        self.assertNotEqual(second, first)
        self.assertEqual(PlayerState(dest.blob(first, b"CHAR").data).payload,
                         PlayerState(dest.blob(second, b"CHAR").data).payload)

    def test_name_unicode_growth_preserves_pools_guid_and_all_other_records(self):
        shared = parse_character(self.share(), self.rules)
        original = SaveDocument(shared.archive).blob(123, b"CHAR").data
        old = Bdb(original)
        name = "A longer copy \u00e9 \U0001f9d9"
        plan = self.doc.prepare_character_import(shared, self.rules, name=name)
        copied = SaveDocument(plan.character.archive)
        raw = copied.blob(plan.character.owner, b"CHAR").data
        new = Bdb(raw)
        self.assertEqual(new.character(), (name, 25))
        self.assertEqual(raw[new.pool:new.pool + old.pool_size],
                         original[old.pool:old.pool + old.pool_size])
        self.assertEqual(PlayerState(raw).payload, PlayerState(original).payload)
        self.assertEqual(character_identity(raw), plan.character.identity)
        self.assertEqual(struct.unpack_from("<I", plan.character.identity)[0], plan.character.owner)
        for source in SaveDocument(shared.archive).blobs:
            if source.tag != b"CHAR":
                self.assertEqual(copied.blob(plan.character.owner, source.tag).data, source.data)
                self.assertEqual(copied.blob(plan.character.owner, source.tag).compressed,
                                 source.compressed)
        with self.assertRaisesRegex(SaveError, "exactly"):
            new.patch_character("Short", 25)

    def test_invalid_or_reused_names_are_atomic(self):
        shared = parse_character(self.share(), self.rules)
        before = self.doc.serialize()
        for name in ("", " ", "Hero", "hErO", " Hero ", "bad\nname", "\ud800",
                     "x" * 65536, 123):
            with self.subTest(name=str(name)[:20]), self.assertRaises(SaveError):
                self.doc.prepare_character_import(shared, self.rules, name=name)
            self.assertEqual(self.doc.serialize(), before)
            self.assertFalse(self.doc.changes)

    def test_bounded_identity_allocation_retries_zero_source_and_saved_owners(self):
        shared = parse_character(self.share(), self.rules)
        dest = self.destination()
        values = [UUID(bytes_le=struct.pack("<I", owner) + b"\x77" * 12)
                  for owner in (0, 123, 789, 900)]
        with patch("character_transfer.uuid4", side_effect=values) as generate:
            plan = dest.prepare_character_import(shared, self.rules)
        self.assertEqual(generate.call_count, 4)
        self.assertEqual(plan.character.owner, 900)
        with patch("character_transfer.uuid4", return_value=values[2]) as generate, \
                self.assertRaisesRegex(SaveError, "allocate a unique"):
            dest.prepare_character_import(shared, self.rules)
        self.assertEqual(generate.call_count, 128)
        self.assertFalse(dest.changes)

    def test_allocation_reserves_prior_staged_copies_even_with_different_full_guid(self):
        shared = parse_character(self.share(), self.rules)
        dest = self.destination()
        first_guid = UUID(bytes_le=struct.pack("<I", 900) + b"\x66" * 12)
        with patch("character_transfer.uuid4", return_value=first_guid):
            first = dest.prepare_character_import(shared, self.rules)
        dest.stage_character_import(first)
        values = [UUID(bytes_le=struct.pack("<I", owner) + b"\x77" * 12)
                  for owner in (900, 901)]
        with patch("character_transfer.uuid4", side_effect=values) as generate:
            second = dest.prepare_character_import(shared, self.rules, name="Second staged copy")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(second.character.owner, 901)

    def test_import_name_limit_is_utf8_bytes_and_accepts_exact_uint16_boundary(self):
        shared = parse_character(self.share(), self.rules)
        name = "\u00e9" * 32767 + "x"
        self.assertEqual(len(name.encode("utf-8")), 65535)
        plan = self.doc.prepare_character_import(shared, self.rules, name=name)
        self.assertEqual(plan.character.name, name)
        with self.assertRaisesRegex(SaveError, "UTF-8 string limit"):
            self.doc.prepare_character_import(shared, self.rules, name="\u00e9" * 32768)

    def test_identity_and_name_alias_nodes_and_guid_storage_are_blocked(self):
        shared = parse_character(self.share(), self.rules)
        source = SaveDocument(shared.archive)
        original = source.blob(123, b"CHAR").data
        db = Bdb(original)
        variants = [extra_field(original, "aliasId", node=db.field("id")),
                    extra_field(original, "aliasGuid", 15, db.node(db.field("id"))[1]),
                    extra_field(original, "aliasName", node=db.field("name")),
                    array_alias(original, "aliasIdArray", db.field("id")),
                    array_alias(original, "aliasNameArray", db.field("name"))]
        for raw in variants:
            values = [(b.owner, b.tag, raw if b.tag == b"CHAR" else b.data) for b in source.blobs]
            incoming = parse_character(envelope(container(values)), self.rules)
            with self.assertRaisesRegex(SaveError, "shared|shares"):
                self.doc.prepare_character_import(incoming, self.rules, name="A new copy")

    def test_import_rename_does_not_change_other_string_users(self):
        original = identified_character()
        db = Bdb(original)
        raw = extra_field(original, "otherLabel", 14, db.node(db.field("name"))[1])
        values = [(o, tag, raw if tag == b"CHAR" else data) for o, tag, data in records()]
        incoming = parse_character(envelope(container(values)), self.rules)
        plan = self.doc.prepare_character_import(incoming, self.rules, name="Long fresh copy")
        result = Bdb(SaveDocument(plan.character.archive).blob(plan.character.owner, b"CHAR").data)
        self.assertEqual(result.character()[0], "Long fresh copy")
        self.assertEqual(result.string(result.field("otherLabel"))[0], "Hero")

    def test_extra_guid_reference_in_opaque_record_is_blocked_without_rebinding(self):
        shared = parse_character(self.share(), self.rules)
        values = [(o, tag, data + shared.identity if tag == b"FOWR" else data)
                  for o, tag, data in records()]
        incoming = parse_character(envelope(container(values)), self.rules)
        with self.assertRaisesRegex(SaveError, "Additional character identity"):
            self.doc.prepare_character_import(incoming, self.rules, name="Another copy")

    def test_import_name_growth_limit_is_atomic(self):
        shared = parse_character(self.share(), self.rules)
        before = self.doc.serialize()
        with patch("character_transfer.MAX_BLOB", 1), self.assertRaisesRegex(SaveError, "safety limit"):
            self.doc.prepare_character_import(shared, self.rules, name="A new longer copy")
        self.assertEqual(self.doc.serialize(), before)

    def test_bad_envelopes_are_rejected(self):
        valid = self.share()
        variants = [b"", valid[:HEADER.size - 1], valid[:-1], valid + b"x",
                    b"INVALID!" + valid[8:], valid[:44] + b"\0" * 32 + valid[76:],
                    envelope(valid[HEADER.size:], build=b"\x33" * 32)]
        for raw in variants:
            with self.subTest(size=len(raw)), self.assertRaises(SaveError):
                parse_character(raw, self.rules)
        with patch("character_transfer.MAX_SHARE", len(valid) - 1), self.assertRaises(SaveError):
            parse_character(valid, self.rules)

    def test_missing_extra_global_and_multiple_owner_records_rejected(self):
        variants = [records()[:-1], records() + [(123, b"OTHER"[:4], b"opaque")],
                    records() + [(0, b"KNPL", b"opaque")], records() + records(456)]
        for values in variants:
            with self.subTest(tags=[tag for _, tag, _ in values]), self.assertRaises(SaveError):
                parse_character(envelope(container(values)), self.rules)

    def test_identity_storage_null_cloud_binding_and_versions_validated(self):
        valid = identified_character()
        db = Bdb(valid)
        changes = [(db.values + db.field("id") * 4, 0),
                   (db.values + db.field("id") * 4, 2),
                   (db.values + db.field("cloudId") * 4, 1),
                   (db.values + db.field("version") * 4, 9),
                   (db.section(36, 16)[0], 456),
                   (36, db.types - 36)]
        for at, value in changes:
            raw = bytearray(valid)
            struct.pack_into("<I", raw, at, value)
            with self.subTest(at=at, value=value), self.assertRaises(SaveError):
                parse_character(envelope(container(
                    [(123, b"CHAR", bytes(raw)), *records()[1:]])), self.rules)
        self.assertEqual(character_identity(valid)[:4], struct.pack("<I", 123))

    def test_inconsistent_destination_identity_rejected(self):
        shared = parse_character(self.share(), self.rules)
        dest = SaveDocument(container([(789, tag, data) for _, tag, data in records(456)]))
        with self.assertRaisesRegex(SaveError, "Existing character GUID"):
            dest.prepare_character_import(shared, self.rules)

    def test_uninitialized_destination_character_is_preserved_not_interpreted(self):
        shared = parse_character(self.share(), self.rules)
        dest = self.destination()
        raw = bytearray(dest.blob(789, b"CHAR").data)
        db = Bdb(raw)
        struct.pack_into("<I", raw, db.values + 4 * db.field("data"), 0)
        values = [(b.owner, b.tag, bytes(raw) if b.tag == b"CHAR" else b.data)
                  for b in dest.blobs]
        dest = SaveDocument(container(values))
        with self.assertRaises(SaveError):
            PlayerState(bytes(raw))
        owner = dest.stage_character_import(dest.prepare_character_import(shared, self.rules))
        result = SaveDocument(dest.serialize())
        self.assertEqual(result.blob(789, b"CHAR").data, bytes(raw))
        self.assertNotIn(owner, (0, 123, 789))

    def test_invalid_appearance_exploration_and_uninitialized_character(self):
        for tag, data in ((b"COUT", b"corrupt"), (b"FOWR", b"wrong"),
                          (b"CHAR", identified_character(raw=b"uninitialized"))):
            values = [(o, t, data if t == tag else d) for o, t, d in records()]
            with self.subTest(tag=tag), self.assertRaises(SaveError):
                parse_character(envelope(container(values)), self.rules)

    def test_import_size_validation_failure_leaves_document_unchanged(self):
        dest = self.destination()
        shared = parse_character(self.share(), self.rules)
        before = dest.serialize()
        with patch("save_format.MAX_TOTAL", 1), self.assertRaises(SaveError):
            dest.prepare_character_import(shared, self.rules)
        self.assertEqual(dest.serialize(), before)
        self.assertFalse(dest.changes)

    def test_sources_rechecked_before_staging_and_build_gate(self):
        dest = self.destination()
        shared = parse_character(self.share(), self.rules)
        plan = dest.prepare_character_import(shared, self.rules)
        before = dest.serialize()
        self.rules.assert_unchanged.side_effect = SaveError("game updated")
        with self.assertRaisesRegex(SaveError, "game updated"):
            dest.stage_character_import(plan)
        self.assertEqual(dest.serialize(), before)
        rules = GameRules.__new__(GameRules)
        rules._progression_verified = False
        with self.assertRaisesRegex(SaveError, "verified game executable"):
            rules.character_transfer_build()
        rules._progression_verified = True
        rules._source_hashes = (b"\xee" * 32, b"\xff" * 32)
        with patch.object(rules, "assert_unchanged") as check:
            self.assertEqual(rules.character_transfer_build(), b"\xee" * 32)
            check.assert_called_once()

    def test_exclusive_file_export_stale_guards_and_no_live_save_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            save_folder = directory / "save"
            save_folder.mkdir()
            source = save_folder / "characters-2"
            source.write_bytes(self.doc.original)
            self.doc.source = source
            raw = self.share()
            output = directory / ("hero" + EXTENSION)
            write_character(output, raw, self.doc, self.rules)
            self.assertEqual(read_character(output, self.rules).owner, 123)
            with self.assertRaises(FileExistsError):
                write_character(output, raw, self.doc, self.rules)
            for path in (save_folder / ("hero" + EXTENSION), directory / "characters",
                         save_folder / "nested" / ("hero" + EXTENSION),
                         directory / "remotecache.vdf", directory / "hero.yaml"):
                with self.subTest(path=path.name), self.assertRaises(SaveError):
                    write_character(path, raw, self.doc, self.rules)
                self.assertFalse(path.exists())
            self.assertEqual(source.read_bytes(), self.doc.original)
            source.write_bytes(b"stale")
            with self.assertRaisesRegex(SaveError, "Source save changed"):
                write_character(directory / ("other" + EXTENSION), raw, self.doc, self.rules)
            self.assertFalse((directory / ("other" + EXTENSION)).exists())

    def test_read_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ("big" + EXTENSION)
            path.write_bytes(b"x" * 20)
            with patch("character_transfer.MAX_SHARE", 10), self.assertRaisesRegex(SaveError, "limit"):
                read_character(path, self.rules)

    def test_imported_character_uses_existing_apply_backups_and_provider_guards(self):
        shared = parse_character(self.share(), self.rules)
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            save_folder = directory / "save"
            save_folder.mkdir()
            original = self.destination().original
            source = save_folder / "characters-2"
            source.write_bytes(original)
            index = save_folder / "characters-index"
            index.write_text('{"latest":2,"deleted":false}', encoding="utf-8")
            dest = SaveDocument.open(index)
            owner = dest.stage_character_import(dest.prepare_character_import(shared, self.rules))
            with self.assertRaisesRegex(SaveError, "registered Steam active profile"):
                dest.apply_active(directory / "blocked-backup", cloud_enabled=True)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((directory / "blocked-backup").exists())
            with patch("save_format.assert_game_closed"):
                backup = dest.apply_active(directory / "backup", cloud_disabled=True)
            self.assertEqual((backup / source.name).read_bytes(), original)
            self.assertEqual(set(SaveDocument.open(index).owners), {owner, 789})
            self.assertEqual(SaveDocument.open(index).blob(0, b"KNPL").data, b"recipient-global")
