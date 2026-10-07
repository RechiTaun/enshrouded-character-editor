import struct
import unittest
from dataclasses import replace
from unittest.mock import patch

from player_state import PlayerState
from save_format import Bdb, SaveDocument, SaveError, fnv1a, u32
from test_player_state import attribute, character, payload
from test_save_format import container


def character_with_siblings():
    original = character()
    db = Bdb(original)
    children, count = db.section(76, 4)
    types = original[db.types:db.types + db.count] + bytes([6]) * 4 + bytes([21]) * 2
    values = (original[db.values:db.values + db.count * 4] +
              struct.pack("<6I", 11, 22, 33, 44, 2, 3))
    arrays = (struct.pack("<II", count, 3) +
              struct.pack("<IIII", 2, 1, 2, count + 3))
    links = (struct.pack("<II", db.count + 1, db.count + 2) +
             original[children:children + count * 4] +
             struct.pack("<II", db.count + 3, db.count + 4))
    keys, key_count = db.section(100, 8)
    maps, _ = db.section(108, 4)
    output = bytearray(original)

    def append(field, data, items):
        output.extend(bytes((-len(output)) % 4))
        struct.pack_into("<II", output, field, len(output) - field, items)
        output.extend(data)

    append(4, types, db.count + 6)
    append(12, values, db.count + 6)
    append(28, arrays, 3)
    append(76, links, count + 4)
    append(100, original[keys:keys + key_count * 8] +
           struct.pack("<IIII", fnv1a("before"), 1, fnv1a("after"), 1), key_count + 2)
    append(108, original[maps:maps + key_count * 4] +
           struct.pack("<II", db.count + 5, db.count + 6), key_count + 2)
    append(20, b"opaque-vendor-section", 1)
    output.extend(b"opaque-footer")
    return bytes(output)


def array_values(character_data, name):
    db = Bdb(character_data)
    kind, reference = db.node(db.field(name))
    if kind != 21:
        raise AssertionError("Expected array")
    descriptors, _ = db.section(28, 8)
    count, start = struct.unpack_from("<II", character_data, descriptors + (reference - 1) * 8)
    children, _ = db.section(76, 4)
    return tuple(db.node(u32(character_data, children + (start - 1 + i) * 4) - 1)
                 for i in range(count))


def appended_payload(state):
    return state.archive.append_entity(
        state.archive.next_entity_reference(), 0, ((2, attribute((25, 100))),))


class PlayerStateGrowthTests(unittest.TestCase):
    def test_same_width_keeps_existing_exact_patch(self):
        original = character()
        state = PlayerState(original)
        raw = bytearray(state.payload)
        struct.pack_into("<I", raw, state.inventory()[0].quantity_offset, 19)
        self.assertEqual(state.replace_payload(bytes(raw)),
                         state.patch_quantity(100, 0, 19, item_id=1234, max_stack=500))
        self.assertEqual(state.replace_payload(state.payload), original)

    def test_growth_preserves_nodes_siblings_and_original_storage(self):
        original = character_with_siblings()
        state = PlayerState(original)
        raw = appended_payload(state)
        edited = state.replace_payload(raw)
        result = PlayerState(edited)
        self.assertEqual(result.payload, raw)
        self.assertEqual(result.experience(), state.experience())
        self.assertEqual(result.skills(), state.skills())
        self.assertEqual(result.inventory(include_empty=True),
                         [replace(stack, quantity_offset=stack.quantity_offset + 10)
                          for stack in state.inventory(include_empty=True)])
        self.assertEqual(Bdb(edited).character(), Bdb(original).character())
        for name in ("before", "after"):
            self.assertEqual(array_values(edited, name), array_values(original, name))
        header_fields = set(range(4, 20)) | set(range(28, 36)) | set(range(76, 84))
        self.assertTrue(all(index in header_fields or a == b
                            for index, (a, b) in enumerate(zip(original, edited))))
        before_db, after_db = Bdb(original), Bdb(edited)
        changed_nodes = {((position - before_db.values) // 4) for position in state.positions}
        for index in range(before_db.count):
            if index not in changed_nodes and before_db.node(index)[0] != 21:
                self.assertEqual(before_db.node(index), after_db.node(index))
        self.assertEqual(state.payload[state.archive.schema_at:],
                         result.payload[result.archive.schema_at:])
        old_descriptors, old_count = before_db.section(28, 8)
        new_descriptors, _ = after_db.section(28, 8)
        self.assertEqual(original[old_descriptors:old_descriptors + old_count * 8],
                         edited[new_descriptors:new_descriptors + old_count * 8])

    def test_non_array_storage_is_opaque_and_preserved(self):
        original = character_with_siblings()
        db = Bdb(original)
        at, count = db.section(28, 8)
        generic_storage = original[at:at + count * 8] + struct.pack("<II", 0xFEDCBA98, 1)
        raw = bytearray(original)
        struct.pack_into("<II", raw, 28, len(raw) - 28, count + 1)
        raw.extend(generic_storage)
        state = PlayerState(bytes(raw))
        edited = state.replace_payload(appended_payload(state))
        new_at, _ = Bdb(edited).section(28, 8)
        self.assertEqual(edited[new_at:new_at + len(generic_storage)], generic_storage)

    def test_repeated_growth_then_same_width_edit(self):
        original = character_with_siblings()
        state = PlayerState(original)
        first = PlayerState(state.replace_payload(appended_payload(state)))
        second = PlayerState(first.replace_payload(appended_payload(first)))
        self.assertEqual(len(second.entities), len(state.entities) + 2)
        delta = second.archive.schema_at - first.archive.schema_at
        self.assertEqual(second.archive.templates,
                         tuple(replace(template, offset=template.offset + delta)
                               for template in first.archive.templates))
        edited = second.patch_quantity(100, 0, 20, item_id=1234, max_stack=500)
        self.assertEqual(PlayerState(edited).inventory()[0].quantity, 20)
        for name in ("before", "after"):
            self.assertEqual(array_values(edited, name), array_values(original, name))

    def test_shared_descriptor_is_rejected_without_mutation(self):
        raw = bytearray(character_with_siblings())
        db = Bdb(raw)
        struct.pack_into("<I", raw, db.values + db.field("after") * 4,
                         db.node(db.field("data"))[1])
        original = bytes(raw)
        state = PlayerState(original)
        with self.assertRaisesRegex(SaveError, "shares its array descriptor"):
            state.replace_payload(appended_payload(state))
        self.assertEqual(state.character, original)

    def test_shared_or_invalid_sibling_children_are_rejected(self):
        original = character_with_siblings()
        db = Bdb(original)
        descriptors, _ = db.section(28, 8)
        for start, reason in ((4, "shares array children"), (0, "Invalid sibling"),
                              (0xFFFFFFFF, "Invalid sibling")):
            with self.subTest(start=start):
                raw = bytearray(original)
                struct.pack_into("<I", raw, descriptors + 12, start)
                state = PlayerState(bytes(raw))
                with self.assertRaisesRegex(SaveError, reason):
                    state.replace_payload(appended_payload(state))
                self.assertEqual(state.character, bytes(raw))

    def test_child_and_blob_limits_check_exact_result(self):
        state = PlayerState(character_with_siblings())
        raw = appended_payload(state)
        with patch("player_state.MAX_PLAYER_BYTES", len(raw) + 3):
            with self.assertRaisesRegex(SaveError, "child table"):
                state.replace_payload(raw)
        with patch("player_state.MAX_PLAYER_BYTES", len(raw) + 4):
            expected = state.replace_payload(raw)
        with patch("player_state.MAX_BLOB", len(expected) - 1):
            with self.assertRaisesRegex(SaveError, "blob safety limit"):
                state.replace_payload(raw)
        with patch("player_state.MAX_BLOB", len(expected)):
            self.assertEqual(state.replace_payload(raw), expected)

    def test_invalid_or_smaller_payload_is_rejected(self):
        state = PlayerState(character(payload(virtual=True)))
        for raw, reason in ((payload(), "Shrinking"), (b"invalid", "range")):
            with self.subTest(reason=reason), self.assertRaisesRegex(SaveError, reason):
                state.replace_payload(raw)

    def test_container_roundtrip_preserves_other_frames(self):
        original = character_with_siblings()
        doc = SaveDocument(container([(123, b"CHAR", original),
                                      (456, b"CHAR", character()),
                                      (123, b"TEST", b"opaque bytes")]))
        state = PlayerState(original)
        edited = state.replace_payload(appended_payload(state))
        doc.blob(123, b"CHAR").data = edited
        result = SaveDocument(doc.serialize())
        self.assertEqual(result.blob(123, b"CHAR").data, edited)
        for owner, tag in ((456, b"CHAR"), (123, b"TEST")):
            self.assertEqual(result.blob(owner, tag).compressed, doc.blob(owner, tag).compressed)
        self.assertEqual(PlayerState(result.blob(123, b"CHAR").data).payload,
                         PlayerState(edited).payload)


if __name__ == "__main__":
    unittest.main()
