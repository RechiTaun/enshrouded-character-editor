from dataclasses import replace
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from entity_format import ComponentDefinition, EntityArchive
from save_format import SaveError
from test_component_schema import generated_definition
from test_entity_format import fixture


def archive_fixture(mask=None, ids=None):
    definitions = [(key, f"Opaque{key}", 1, b"opaque") for key in (ids or (1, 2, 3))]
    raw = bytearray(fixture(
        entities=[(1, 0, ((1, b"\xff"),), ())], definitions=definitions,
        templates=[(b"\x11" * 16, "Base", bytes(80) if mask is None else mask)]))
    at, = struct.unpack_from("<I", raw)
    struct.pack_into("<I", raw, at + 8, sum(len(d[3]) for d in definitions))
    return bytes(raw)


class SchemaGrowthTests(unittest.TestCase):
    def test_append_preserves_entities_definitions_templates_and_registry(self):
        raw = archive_fixture()
        old = EntityArchive(raw)
        definition = generated_definition()
        new = EntityArchive(old.append_definition(old.next_component_id(), definition))
        self.assertEqual(new.definitions[4], definition)
        self.assertEqual(new.schema_at, old.schema_at)
        self.assertEqual(new.entities, old.entities)
        self.assertEqual(new.payload[:old.schema_at], raw[:old.schema_at])
        self.assertEqual(new.payload[old.schema_at + 12:old.schema_at + 20],
                         raw[old.schema_at + 12:old.schema_at + 20])
        self.assertEqual(new.payload[old.schema_at + 20:old._definitions_end],
                         raw[old.schema_at + 20:old._definitions_end])
        self.assertEqual(new.payload[new._definitions_end:], raw[old._definitions_end:])
        self.assertEqual(struct.unpack_from("<H", new.payload, new.schema_at + 4)[0], 4)
        self.assertEqual(struct.unpack_from("<I", new.payload, new.schema_at + 8)[0], 18 + 280)
        for key, value in old.definitions.items():
            self.assertEqual(new.definitions[key], value)
        self.assertEqual([(t.guid, t.name, t.component_mask) for t in new.templates],
                         [(t.guid, t.name, t.component_mask) for t in old.templates])

    def test_template_only_ids_are_reserved_and_exhaustion_is_visible(self):
        mask = bytearray(80)
        mask[0] |= 1 << 4
        archive = EntityArchive(archive_fixture(bytes(mask)))
        self.assertEqual(archive.next_component_id(), 5)
        with self.assertRaisesRegex(SaveError, "template bit"):
            archive.append_definition(4, generated_definition())
        with self.assertRaisesRegex(SaveError, "No free"):
            EntityArchive(archive_fixture(b"\xff" * 80)).next_component_id()

    def test_conflicts_types_and_limits_are_rejected(self):
        archive = EntityArchive(archive_fixture())
        definition = generated_definition()
        for key, value in (
                (1, definition), (640, definition), (True, definition),
                (4, None), (4, replace(definition, name="Opaque1")),
                (4, replace(definition, name="")), (4, replace(definition, name="bad\0name")),
                (4, replace(definition, name="é")), (4, replace(definition, name="x" * 256)),
                (4, replace(definition, declared_size=True)),
                (4, replace(definition, declared_size=65536)),
                (4, replace(definition, hash_a=0)), (4, replace(definition, hash_b=-1)),
                (4, replace(definition, hash_a=0xFEDCBA98)),
                (4, replace(definition, hash_b=0x76543210)),
                (4, replace(definition, schema=bytearray(b"x"))),
                (4, replace(definition, schema=b"")),
                (4, replace(definition, schema=b"x" * 65536))):
            with self.subTest(key=key, value=value), self.assertRaises(SaveError):
                archive.append_definition(key, value)
        raw = bytearray(archive.payload)
        struct.pack_into("<I", raw, archive.schema_at + 8, 0)
        with self.assertRaisesRegex(SaveError, "allocation"):
            EntityArchive(bytes(raw)).append_definition(4, definition)
        with self.assertRaisesRegex(SaveError, "namespace"):
            EntityArchive(archive_fixture(ids=(1, 640))).append_definition(4, definition)
        with self.assertRaisesRegex(SaveError, "safety limit"), \
                patch("entity_format.MAX_PLAYER_BYTES", len(archive.payload) + 1):
            archive.append_definition(4, definition)

    def test_append_then_entity_and_second_definition_remain_consistent(self):
        archive = EntityArchive(archive_fixture())
        definition = generated_definition()
        archive = EntityArchive(archive.append_definition(4, definition))
        archive = EntityArchive(archive.append_entity(2, 0, ((4, b"\x03\x00"),)))
        other = ComponentDefinition("AnotherSynthetic", 1, b"opaque", 0x1111, 0x2222)
        archive = EntityArchive(archive.append_definition(5, other))
        self.assertEqual(archive.entities[-1].server[0].data, b"\x03\x00")
        self.assertEqual(struct.unpack_from("<I", archive.payload, archive.schema_at + 8)[0],
                         sum(len(d.schema) for d in archive.definitions.values()))


if __name__ == "__main__":
    unittest.main()
