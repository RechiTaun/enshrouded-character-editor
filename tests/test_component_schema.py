from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from component_schema import generate_perk_schema
from game_rules import _Pe
from player_state import Schema
from save_format import SaveError
from test_game_rules import executable, open_rules


def perk_executable():
    raw, records = executable()
    data = bytearray(raw)

    def allocate(value):
        at = len(data)
        data.extend(value)
        return at

    def address(at):
        return 0x180003000 + at - 512

    def text(value):
        return address(allocate(value.encode("ascii") + b"\0")), len(value)

    keen, ecs = allocate(bytes(24)), allocate(bytes(24))
    struct.pack_into("<QQQ", data, keen, *text("keen"), 0)
    struct.pack_into("<QQQ", data, ecs, *text("ecs"), address(keen))
    types = (
        ("keen::ecs::PerkContainerNew", "PerkContainerNew", "ecs.PerkContainerNew",
         2, 18, 0xE288D50C, 0x12345678),
        ("keen::ecs::Component", "Component", "ecs.Component", 1, 18,
         0xB4B5CB62, 0xABCDEF01),
        ("keen::uint8", "uint8", "uint8", 1, 2, 0xD297A9C6, 0x12345679),
    )
    for name, _, _, _, _, _, _ in types:
        if name not in records:
            records[name] = allocate(bytes(104))
    for name, short, serialized, size, kind, primary, secondary in types:
        at = records[name]
        struct.pack_into("<QQ", data, at, *text(short))
        struct.pack_into("<QQ", data, at + 16, *text(serialized))
        struct.pack_into("<QQ", data, at + 32, *text(name))
        struct.pack_into("<Q", data, at + 48, address(keen if name == "keen::uint8" else ecs))
        struct.pack_into("<IHHII", data, at + 64, size, 1, 1, 0, kind)
        struct.pack_into("<II", data, at + 80, primary, secondary)
    root = records["keen::ecs::PerkContainerNew"]
    struct.pack_into("<Q", data, root + 56, address(records["keen::ecs::Component"]))
    fields = allocate(bytes(96))
    struct.pack_into("<I", data, root + 72, 2)
    struct.pack_into("<Q", data, root + 88, address(fields))
    for i, name in enumerate(("availablePerks", "unlockedPerks")):
        at = fields + 48 * i
        struct.pack_into("<QQ", data, at, *text(name))
        struct.pack_into("<QQ", data, at + 16, address(records["keen::uint8"]), i)
    struct.pack_into("<IIII", data, 272, len(data) - 512, 0x3000, len(data) - 512, 512)
    return bytes(data), records, fields, (keen, ecs)


def generated_definition():
    return generate_perk_schema(_Pe(perk_executable()[0]))


class ComponentSchemaTests(unittest.TestCase):
    def test_complete_compact_schema_not_only_decoded_fields(self):
        definition = generated_definition()
        raw = definition.schema
        schema = Schema(raw)
        self.assertEqual((definition.name, definition.declared_size,
                          definition.hash_a, definition.hash_b),
                         ("PerkContainerNew", 2, 0xE288D50C, 0xE288D50C))
        self.assertEqual(len(raw), 280)
        self.assertEqual(struct.unpack_from("<IIII", raw), (0x42435443, 280, 40, 2))
        self.assertEqual(struct.unpack_from("<HHHH", raw, 48), (1, 0, 6, 1))
        self.assertEqual(schema.section(16, 32), (56, 3))
        self.assertEqual((schema.pool, schema.pool_size, schema.fields, schema.field_count),
                         (152, 106, 260, 2))
        self.assertEqual(struct.unpack_from("<HHHHIIIIII", raw, 56),
                         (10, 27, 2, 2, 2, 2, 18, 0xE288D50C, 0x12345678, 1))
        self.assertEqual(struct.unpack_from("<HHHHIIIIII", raw, 88),
                         (77, 87, 2, 0, 1, 0, 18, 0xB4B5CB62, 0xABCDEF01, 3))
        self.assertEqual(struct.unpack_from("<HHHHIIIIII", raw, 120),
                         (101, 101, 1, 0, 1, 0, 2, 0xD297A9C6, 0x12345679, 0))
        self.assertEqual(struct.unpack_from("<HHIHHI", raw, 260), (48, 3, 0, 63, 3, 1))
        self.assertEqual(raw[258:260], bytes(2))
        self.assertEqual(raw[276:], bytes(4))
        self.assertEqual(schema.members(1),
                         [("availablePerks", 3, 0), ("unlockedPerks", 3, 1)])
        self.assertEqual(schema.type(1).inner, 2)
        self.assertEqual(schema.members(2), [])

    def test_invalid_layout_hashes_alignment_inheritance_and_fields(self):
        original, types, fields, namespaces = perk_executable()
        root, base, scalar = (types[name] for name in
                              ("keen::ecs::PerkContainerNew", "keen::ecs::Component", "keen::uint8"))
        for at, fmt, value in (
                (root + 64, "<I", 3), (root + 68, "<H", 2),
                (root + 72, "<I", 1), (root + 76, "<I", 17),
                (root + 80, "<I", 123), (root + 84, "<I", 0),
                (root + 56, "<Q", 0), (base + 56, "<Q", _Pe(original).address(root)),
                (base + 64, "<I", 2), (scalar + 76, "<I", 1),
                (fields + 16, "<Q", _Pe(original).address(base)),
                (fields + 48 + 24, "<Q", 0),
                (namespaces[1] + 16, "<Q", _Pe(original).address(namespaces[1]))):
            raw = bytearray(original)
            struct.pack_into(fmt, raw, at, value)
            with self.subTest(offset=at, value=value), self.assertRaises(SaveError):
                generate_perk_schema(_Pe(bytes(raw)))
        raw = original.replace(b"ecs.PerkContainerNew\0", b"bad.PerkContainerNew\0")
        with self.assertRaisesRegex(SaveError, "type"):
            generate_perk_schema(_Pe(raw))
        raw = original.replace(b"unlockedPerks\0", b"unexpectedKey\0")
        with self.assertRaisesRegex(SaveError, "fields"):
            generate_perk_schema(_Pe(raw))

    def test_api_is_lazy_cached_and_revalidates_sources(self):
        rules = open_rules(exe=perk_executable()[0])
        with patch.object(rules, "assert_unchanged") as check, \
                patch("component_schema.generate_perk_schema",
                      wraps=generate_perk_schema) as generate:
            first = rules.perk_component_definition()
            self.assertIs(rules.perk_component_definition(), first)
            generate.assert_called_once()
            self.assertEqual(check.call_count, 3)
            check.side_effect = SaveError("Sources changed")
            with self.assertRaisesRegex(SaveError, "Sources changed"):
                rules.perk_component_definition()

    def test_unverified_missing_and_changed_metadata_fail_explicitly(self):
        rules = open_rules(exe=perk_executable()[0], verified=False)
        with self.assertRaisesRegex(SaveError, "verified"):
            rules.perk_component_definition()
        rules = open_rules()
        with patch.object(rules, "assert_unchanged"), self.assertRaisesRegex(SaveError, "metadata"):
            rules.perk_component_definition()
        rules = open_rules(exe=perk_executable()[0])
        with patch.object(rules, "assert_unchanged",
                          side_effect=[None, SaveError("Sources changed")]):
            with self.assertRaisesRegex(SaveError, "Sources changed"):
                rules.perk_component_definition()
        self.assertFalse(hasattr(rules, "_generated_perk_schema"))


if __name__ == "__main__":
    unittest.main()
