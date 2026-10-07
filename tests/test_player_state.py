import struct
import unittest
from unittest.mock import Mock

from test_save_format import Bdb, SaveDocument, SaveError, container, fnv1a
from player_state import PlayerState, Schema
from game_rules import GameRules, ItemRule


def schema(name, types, enums=()):
    output = bytearray(40)
    output[:4] = b"CTCB"
    struct.pack_into("<I", output, 12, 2)
    pool = bytearray()
    references = {}

    def string(text):
        if text not in references:
            encoded = text.encode()
            references[text] = len(pool) + 1
            pool.extend(bytes([len(encoded) - 1]) + encoded)
        return references[text]

    type_data, fields = bytearray(), bytearray()
    for type_name, inner, size, count, kind, members, enum_start in types:
        start = len(fields) // 8 + 1 if kind == 18 else enum_start << 16
        type_data.extend(struct.pack("<HHHHIIIIII", string(type_name),
                                     string(type_name), 1, inner, size, count,
                                     kind, 0, 0, start))
        for field, child, offset in members:
            fields.extend(struct.pack("<HHI", string(field), child, offset))
    enum_data = b"".join(struct.pack("<QQ", string(label), value)
                         for label, value in enums)

    def section(at, data, count):
        struct.pack_into("<II", output, at, len(output) - at, count)
        output.extend(data)

    section(16, type_data, len(types))
    section(24, pool, len(pool))
    section(32, fields, len(fields) // 8)
    if enum_data:
        output.extend(bytes((-len(output)) % 8))
    output.extend(enum_data)
    struct.pack_into("<I", output, 4, len(output))
    return bytes(output)


def inventory_schema():
    return schema("Inventory", [
        ("Inventory", 0, 24, 1, 18, [("slots", 2, 0)], 0),
        ("Stacks", 3, 24, 2, 19, [], 0),
        ("ItemStack", 0, 12, 2, 18, [("id", 4, 0), ("data", 7, 4)], 0),
        ("ItemId", 5, 4, 0, 17, [], 0),
        ("HashKey32", 0, 4, 1, 18, [("value", 6, 0)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
        ("ItemData", 0, 8, 2, 18, [("count", 6, 0), ("pide", 8, 4)], 0),
        ("EntityId", 0, 4, 1, 18, [("id", 6, 0)], 0),
    ])


def setup_schema():
    return schema("InventorySetup", [
        ("InventorySetup", 0, 13, 5, 18, [
            ("linksEntities", 2, 0), ("linksCategories", 5, 8),
            ("genericSlotCount", 7, 10), ("availableSlotCount", 7, 11),
            ("isInitialized", 8, 12)], 0),
        ("EntityArray", 3, 8, 2, 19, [], 0),
        ("EntityId", 0, 4, 1, 18, [("id", 4, 0)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
        ("Categories", 6, 2, 2, 19, [], 0),
        ("InventoryCategory", 7, 1, 6, 12, [], 1),
        ("uint8", 0, 1, 0, 2, [], 0),
        ("bool", 0, 1, 0, 1, [], 0),
    ], tuple(zip(("Invalid", "Customization", "Equipment", "Currency",
                  "Generic", "Virtual"), range(6))))


def attribute_schema(name, count):
    return schema(name, [
        (name, 2, 36 + 4 * count, 1, 18, [("dataStorage", 4, 36)], 0),
        ("Attribute", 0, 20, 2, 18, [
            ("storageOffset", 3, 8), ("storageSize", 3, 12)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
        ("Values", 3, 4 * count, count, 19, [], 0),
    ])


def skills_schema():
    return schema("UnlockedSkillNodes", [
        ("UnlockedSkillNodes", 0, 22, 3, 18, [
            ("knownSkillNodes", 2, 4), ("activeSkillImpacts", 5, 12),
            ("unlockLevel", 7, 20)], 0),
        ("Nodes", 3, 8, 2, 19, [], 0),
        ("SkillNodeId", 0, 4, 1, 18, [("value", 4, 0)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
        ("Impacts", 6, 8, 2, 19, [], 0),
        ("EntityId", 0, 4, 1, 18, [("id", 4, 0)], 0),
        ("Levels", 8, 2, 2, 19, [], 0),
        ("SkillUnlockLevel", 0, 1, 1, 18, [("value", 9, 0)], 0),
        ("uint8", 0, 1, 0, 2, [], 0),
    ])


def attribute(values):
    raw = bytearray(36)
    struct.pack_into("<II", raw, 8, 36, len(values))
    return bytes(raw) + struct.pack(f"<{len(values)}I", *values)


def payload(*, virtual=False, stateful=False, category=4, available=2,
            skill_nodes=((345, 0, 25),), xp=(1548, 0, 4252)):
    definitions = [
        ("Experience", 48, attribute_schema("Experience", 3)),
        ("Level", 44, attribute_schema("Level", 2)),
        ("InventorySetup", 13, setup_schema()),
        ("Inventory", 24, inventory_schema()),
        ("UnlockedSkillNodes", 22, skills_schema()),
    ]
    setup = struct.pack("<IIBBBBB", 100, 200 if virtual else 0,
                        category, 5 if virtual else 0, 2, available, 1)
    skills = bytearray(22)
    for i, (node, impact, rank) in enumerate(skill_nodes):
        struct.pack_into("<I", skills, 4 + 4 * i, node)
        struct.pack_into("<I", skills, 12 + 4 * i, impact)
        skills[20 + i] = rank
    groups = [
        (1, [(1, attribute(xp)), (2, attribute((25, 100))),
             (3, setup), (5, bytes(skills))]),
        (100, [(4, struct.pack("<IIIIII", 1234, 17, 300 if stateful else 0,
                              0, 0, 0))]),
    ]
    if virtual:
        groups.extend([
            (200, [(3, struct.pack("<IIBBBBB", 201, 0, 4, 0, 2, 2, 1))]),
            (201, [(4, struct.pack("<IIIIII", 5678, 5, 0, 0, 0, 0))]),
        ])
    if stateful:
        groups.append((300, []))
    raw = bytearray(41)
    raw[8:12] = b"EHD0"
    struct.pack_into("<I", raw, 12, 1)
    raw.extend(struct.pack("<H", len(groups)))
    for entity, _ in groups:
        raw.extend(struct.pack("<QH", entity, 0))
    for _, components in groups:
        raw.extend(struct.pack("<HH", len(components), 0))
        for type_id, data in components:
            raw.extend(struct.pack("<HH", type_id, len(data)) + data)
    struct.pack_into("<II", raw, 0, len(raw), 0)
    struct.pack_into("<I", raw, 37, len(raw) - 41)
    raw.extend(b"ESC2" + struct.pack("<HHIII", len(definitions), 1, 0, 0, 0))
    for i, (name, size, metadata) in enumerate(definitions, 1):
        raw.extend(struct.pack("<I", len(name)) + name.encode()
                   + struct.pack("<IIHHH", 0, 0, i, size, len(metadata)) + metadata)
    raw.extend(bytes(16) + struct.pack("<I", 9) + b"Synthetic" + bytes(80))
    return bytes(raw)


def character(raw=None):
    if raw is None:
        raw = payload()
    output = bytearray(116)
    output[:4] = b"BDB1"

    def section(at, data, count):
        struct.pack_into("<II", output, at, len(output) - at, count)
        output.extend(data)

    section(4, bytes([20, 14, 8, 21]) + bytes([6]) * len(raw), len(raw) + 4)
    section(12, struct.pack("<IIII", 1, 1, 25, 1)
            + b"".join(struct.pack("<I", byte) for byte in raw), len(raw) + 4)
    section(28, struct.pack("<II", len(raw), 1), 1)
    section(60, b"\x04\x00Hero", 6)
    section(76, b"".join(struct.pack("<I", i + 5) for i in range(len(raw))), len(raw))
    section(100, b"".join(struct.pack("<II", fnv1a(name), 1)
                          for name in ("name", "level", "data")), 3)
    section(108, struct.pack("<III", 2, 3, 4), 3)
    return bytes(output)


def knowledge():
    entries = [(1000 + i, 1) for i in range(19)] + [(0xD267AE0F, 1)]
    return (struct.pack("<III", 2, 0, len(entries))
            + b"".join(struct.pack("<I", key) for key, _ in entries)
            + b"".join(struct.pack("<I", value) for _, value in entries))


class PlayerStateTests(unittest.TestCase):
    def test_schema_named_paths_enums_and_inheritance(self):
        s = Schema(inventory_schema())
        self.assertEqual(s.path(("data", "pide", "id"), 3)[0], 8)
        s = Schema(setup_schema())
        self.assertEqual(s.enum(s.array("linksCategories")[1])[4], "Generic")
        self.assertEqual(Schema(attribute_schema("Level", 2)).field("storageOffset")[0], 8)

    def test_baseline_and_skill_records(self):
        state = PlayerState(character())
        xp = state.experience()
        self.assertEqual((xp.level, xp.current, xp.gain, xp.required), (25, 1548, 0, 4252))
        self.assertEqual([(s.node_id, s.unlock_level) for s in state.skills()], [(345, 25)])
        stack = state.inventory()[0]
        self.assertEqual((stack.entity, stack.slot, stack.item_id, stack.quantity),
                         (100, 0, 1234, 17))
        self.assertTrue(stack.accessible)

    def test_quantity_patch_exact_byte_preservation_and_roundtrip(self):
        original = character()
        state = PlayerState(original)
        edited = state.patch_quantity(100, 0, 499, item_id=1234, max_stack=500)
        result = PlayerState(edited)
        self.assertEqual(result.inventory()[0].quantity, 499)
        self.assertEqual(result.experience(), state.experience())
        self.assertEqual(result.skills(), state.skills())
        at = state.inventory()[0].quantity_offset
        allowed = {byte for i in range(at, at + 4)
                   for byte in range(state.positions[i], state.positions[i] + 4)}
        self.assertTrue(all(i in allowed for i, pair in enumerate(zip(original, edited))
                            if pair[0] != pair[1]))
        self.assertEqual(result.patch_quantity(100, 0, 17, item_id=1234, max_stack=500),
                         original)
        doc = SaveDocument(container([(123, b"CHAR", edited), (456, b"CHAR", character())]))
        self.assertEqual(doc.serialize(), doc.original)

    def test_virtual_inventory_and_unavailable_slots(self):
        self.assertEqual(len(PlayerState(character(payload(virtual=True))).inventory()), 2)
        state = PlayerState(character(payload(available=0)))
        self.assertFalse(state.inventory()[0].accessible)
        with self.assertRaisesRegex(SaveError, "accessible"):
            state.patch_quantity(100, 0, 1, item_id=1234, max_stack=500)

    def test_boundary_values_and_stateful_or_other_category(self):
        state = PlayerState(character())
        for quantity in (0, -1, 501, 2**32, True, 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(SaveError):
                state.patch_quantity(100, 0, quantity, item_id=1234, max_stack=500)
        for raw in (payload(stateful=True), payload(category=2, available=0)):
            with self.subTest(), self.assertRaisesRegex(SaveError, "plain generic"):
                PlayerState(character(raw)).patch_quantity(
                    100, 0, 1, item_id=1234, max_stack=500)
        with self.assertRaisesRegex(SaveError, "match"):
            state.patch_quantity(100, 0, 1, item_id=999, max_stack=500)

    def test_unknown_version_and_bad_framing(self):
        original = payload()
        cases = [original[:30], original[:-1]]
        raw = bytearray(original)
        raw[u32_at(raw, 0):u32_at(raw, 0) + 4] = b"ESC0"
        cases.append(bytes(raw))
        raw = bytearray(original)
        struct.pack_into("<I", raw, 37, 0)
        cases.append(bytes(raw))
        for raw in cases:
            with self.subTest(length=len(raw)), self.assertRaises(SaveError):
                PlayerState(character(raw))

    def test_shared_bdb_nodes_and_corrupt_schema_rejected(self):
        raw = bytearray(character())
        db = Bdb(raw)
        at, _ = db.section(76, 4)
        struct.pack_into("<I", raw, at + 4, u32_at(raw, at))
        with self.assertRaisesRegex(SaveError, "share"):
            PlayerState(bytes(raw))
        raw = bytearray(inventory_schema())
        struct.pack_into("<I", raw, 20, 999999)
        with self.assertRaises(SaveError):
            Schema(bytes(raw))

    def test_document_inventory_tracking_reversion_and_invalid_level_noop(self):
        doc = SaveDocument(container([(123, b"CHAR", character()),
                                      (456, b"CHAR", character())]))
        rules = Mock(spec=GameRules)
        rules.items_for.return_value = {1234: ItemRule(1234, 500, "Synthetic wood")}
        doc.edit_inventory(123, 100, 0, 500, rules)
        self.assertEqual(len(doc.changes), 1)
        self.assertIn("17 -> 500", next(iter(doc.changes.values())))
        self.assertEqual(doc.blob(456, b"CHAR").data, doc.blob(456, b"CHAR").original)
        before = doc.serialize()
        with self.assertRaisesRegex(SaveError, "Load installed"):
            doc.edit_character(123, "Abcd", 30)
        self.assertEqual(doc.serialize(), before)
        doc.edit_inventory(123, 100, 0, 17, rules)
        self.assertFalse(doc.changes)
        self.assertEqual(doc.serialize(), doc.original)


def u32_at(raw, at):
    return struct.unpack_from("<I", raw, at)[0]


if __name__ == "__main__":
    unittest.main()
