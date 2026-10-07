"""Independent synthetic fixtures for lossless EHD0/ESC2 framing."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from entity_format import EntityArchive, MAX_PLAYER_BYTES
from save_format import SaveError


def fixture(entities=None, definitions=None, templates=None):
    if entities is None:
        entities = [(0x1234567800000007, 1, ((2, b"abc"), (1, b"")), ((3, b"\xff"),)),
                    (11, 0, (), ((1, b""),))]
    if definitions is None:
        definitions = [(1, "Opaque", 4, b"CTCB\xff\x00unknown"),
                       (2, "Extra", 1, b"opaque-schema"),
                       (3, "Client", 0, b"")]
    if templates is None:
        templates = [(bytes(range(16)), "Base", bytes(80)),
                     (b"\xff" * 16, "Équipement", b"\xff" * 80)]
    header = bytearray(range(43))
    header[8:12] = b"EHD0"
    struct.pack_into("<I", header, 12, 1)
    struct.pack_into("<H", header, 41, len(entities))
    table, groups = bytearray(), bytearray()
    for entity_id, template_index, server, client in entities:
        table.extend(struct.pack("<QH", entity_id, template_index))
        groups.extend(struct.pack("<HH", len(server), len(client)))
        for component_id, data in (*server, *client):
            groups.extend(struct.pack("<HH", component_id, len(data)) + data)
    suffix = bytearray(b"ESC2" + struct.pack("<HH", len(definitions), len(templates))
                       + bytes(range(12)))
    for component_id, name, size, schema in definitions:
        encoded = name.encode("ascii")
        suffix.extend(struct.pack("<I", len(encoded)) + encoded
                      + struct.pack("<IIHHH", 0xFEDCBA98, 0x76543210,
                                    component_id, size, len(schema)) + schema)
    for guid, name, mask in templates:
        encoded = name.encode("utf-8")
        suffix.extend(guid + struct.pack("<I", len(encoded)) + encoded + mask)
    schema_at = len(header) + len(table) + len(groups)
    struct.pack_into("<I", header, 0, schema_at)
    struct.pack_into("<I", header, 37, schema_at - 41)
    return bytes(header + table + groups + suffix)


def patch(data, offset, fmt, value):
    result = bytearray(data)
    struct.pack_into(fmt, result, offset, value)
    return bytes(result)


class EntityArchiveTests(unittest.TestCase):
    def test_lossless_opaque_records_and_partitions(self):
        raw = fixture()
        archive = EntityArchive(raw)
        self.assertIs(archive.payload, raw)
        self.assertEqual(archive.payload, raw)
        self.assertEqual(archive.references[7], 0x1234567800000007)
        self.assertEqual(archive.next_entity_reference(), 12)
        self.assertEqual(archive.definitions[1].schema, b"CTCB\xff\x00unknown")
        self.assertEqual(archive.definitions[1].declared_size, 4)
        self.assertEqual(archive.definitions[1].hash_a, 0xFEDCBA98)
        self.assertEqual(archive.definitions[1].hash_b, 0x76543210)
        self.assertEqual([c.component_id for c in archive.entities[0].server], [2, 1])
        self.assertEqual([c.component_id for c in archive.entities[0].client], [3])
        self.assertEqual(archive.entities[0].server[1].data, b"")
        self.assertEqual(archive.templates[1].name, "Équipement")
        self.assertEqual(archive.templates[1].component_mask, b"\xff" * 80)
        for entity in archive.entities:
            for component in (*entity.server, *entity.client):
                self.assertEqual(raw[component.offset:component.offset + len(component.data)],
                                 component.data)
        with self.assertRaises(TypeError):
            archive.definitions[5] = archive.definitions[1]
        with self.assertRaises(FrozenInstanceError):
            archive.templates[0].name = "changed"

    def test_append_preserves_schema_groups_and_unknown_header(self):
        archive = EntityArchive(fixture())
        result = archive.append_entity(12, 0, ((3, b"added"),), ((2, b""), (1, b"x")))
        appended = EntityArchive(result)
        self.assertEqual(appended.payload[appended.schema_at:],
                         archive.payload[archive.schema_at:])
        old_groups_at = 43 + 10 * len(archive.entities)
        new_groups_at = old_groups_at + 10
        old_groups = archive.payload[old_groups_at:archive.schema_at]
        self.assertEqual(result[new_groups_at:new_groups_at + len(old_groups)], old_groups)
        self.assertEqual(result[43:old_groups_at], archive.payload[43:old_groups_at])
        self.assertEqual(result[4:37], archive.payload[4:37])
        self.assertEqual(appended.references[12], 12)
        self.assertEqual(appended.next_entity_reference(), 13)
        self.assertEqual(appended.entities[-1].template_index, 0)
        self.assertEqual(appended.entities[-1].server[0].data, b"added")
        for old, new in zip(archive.entities, appended.entities):
            self.assertEqual(old.entity_id, new.entity_id)
            for partition in ("server", "client"):
                self.assertEqual([(c.component_id, c.data) for c in getattr(old, partition)],
                                 [(c.component_id, c.data) for c in getattr(new, partition)])
                for before, after in zip(getattr(old, partition), getattr(new, partition)):
                    self.assertEqual(after.offset, before.offset + 10)

    def test_empty_archive_and_uint32_boundaries(self):
        empty = EntityArchive(fixture(entities=[]))
        self.assertEqual(empty.next_entity_reference(), 1)
        maximum = EntityArchive(empty.append_entity(0xFFFFFFFF, 1, ()))
        self.assertEqual(maximum.entities[0].entity_id, 0xFFFFFFFF)
        with self.assertRaises(SaveError):
            maximum.next_entity_reference()
        existing = EntityArchive(fixture(entities=[(0xFFFFFFFFFFFFFFFF, 0, (), ())]))
        self.assertEqual(existing.entities[0].entity_id, 0xFFFFFFFFFFFFFFFF)
        with self.assertRaises(SaveError):
            existing.next_entity_reference()

    def test_invalid_append_is_atomic(self):
        archive = EntityArchive(fixture())
        raw = archive.payload
        cases = [
            (7, 0, (), ()), (11, 0, (), ()), (0, 0, (), ()),
            (True, 0, (), ()), (-1, 0, (), ()), (2**32, 0, (), ()),
            (12.0, 0, (), ()), ("12", 0, (), ()),
            (12, True, (), ()), (12, -1, (), ()), (12, 2, (), ()),
            (12, 65536, (), ()), (12, 0.0, (), ()),
            (12, 0, ((99, b""),), ()), (12, 0, ((True, b""),), ()),
            (12, 0, ((-1, b""),), ()), (12, 0, ((65536, b""),), ()),
            (12, 0, ((1.0, b""),), ()), (12, 0, ((1, b""), (1, b"")), ()),
            (12, 0, ((1, b""),), ((1, b""),)),
            (12, 0, ((1, b"x" * 65536),), ()),
            (12, 0, ((1, bytearray()),), ()), (12, 0, ((1, ""),), ()),
            (12, 0, [], ()), (12, 0, (1,), ()),
            (12, 0, ((1,),), ()), (12, 0, (), None),
            (12, 0, ((1, b""),) * 65536, ()),
        ]
        for args in cases:
            with self.subTest(args=str(args)[:100]), self.assertRaises(SaveError):
                archive.append_entity(*args)
            self.assertIs(archive.payload, raw)
            self.assertEqual(len(archive.entities), 2)

    def test_duplicate_full_and_low32_references(self):
        for entity_id in (7, 0x100000007):
            with self.subTest(entity_id=entity_id), self.assertRaises(SaveError):
                EntityArchive(fixture(entities=[(7, 0, (), ()), (entity_id, 1, (), ())]))

    def test_corrupt_definition_records(self):
        raw = fixture()
        archive = EntityArchive(raw)
        definition = archive.schema_at + 20
        name_at = definition + 4
        metadata_at = name_at + len("Opaque")
        corruptions = [
            patch(raw, definition, "<I", 0), patch(raw, definition, "<I", 256),
            raw[:name_at] + b"\xff" + raw[name_at + 1:],
            raw[:name_at] + b"\0" + raw[name_at + 1:],
            patch(raw, metadata_at + 12, "<H", 65535),
            fixture(definitions=[(1, "One", 0, b""), (1, "Two", 0, b"")]),
            fixture(definitions=[(1, "One", 0, b""), (2, "One", 0, b"")]),
        ]
        for damaged in corruptions:
            with self.subTest(size=len(damaged)), self.assertRaises(SaveError):
                EntityArchive(damaged)

    def test_corrupt_templates_and_boundaries(self):
        raw = fixture()
        archive = EntityArchive(raw)
        name_length_at = archive.templates[0].offset + 16
        name_at = name_length_at + 4
        groups_at = 43 + 10 * len(archive.entities)
        corruptions = [
            raw[:42], raw[:-1], raw + b"extra",
            patch(raw, 0, "<I", archive.schema_at - 1),
            patch(raw, 37, "<I", archive.schema_at - 40),
            patch(raw, 0, "<I", 0), patch(raw, 0, "<I", 0xFFFFFFFF),
            patch(raw, 12, "<I", 2), raw[:8] + b"NOPE" + raw[12:],
            raw[:archive.schema_at] + b"NOPE" + raw[archive.schema_at + 4:],
            patch(raw, 41, "<H", 65535),
            patch(raw, 51, "<H", 2),
            patch(raw, archive.schema_at + 6, "<H", 0),
            patch(raw, archive.schema_at + 6, "<H", 3),
            patch(raw, name_length_at, "<I", 0),
            patch(raw, name_length_at, "<I", MAX_PLAYER_BYTES + 1),
            raw[:name_at] + b"\xff" + raw[name_at + 1:],
            raw[:name_at] + b"\0" + raw[name_at + 1:],
            patch(raw, groups_at, "<H", 65535),
            patch(raw, groups_at + 4, "<H", 99),
            patch(raw, groups_at + 6, "<H", 65535),
            fixture(entities=[(1, 0, ((1, b""),), ((1, b""),))]),
            fixture(entities=[(1, 0, ((99, b""),), ())]),
        ]
        for damaged in corruptions:
            with self.subTest(size=len(damaged)), self.assertRaises(SaveError):
                EntityArchive(damaged)

    def test_every_truncation_rejected(self):
        raw = fixture()
        for length in range(len(raw)):
            with self.subTest(length=length), self.assertRaises(SaveError):
                EntityArchive(raw[:length])

    def test_serialized_size_and_archive_limits(self):
        archive = EntityArchive(fixture(entities=[]))
        result = archive.append_entity(1, 0, ((1, b"x" * 65535),))
        self.assertEqual(len(EntityArchive(result).entities[0].server[0].data), 65535)
        large = fixture(entities=[(i, 0, ((1, b"x" * 65000),), ()) for i in range(1, 17)])
        archive = EntityArchive(large)
        with self.assertRaises(SaveError):
            archive.append_entity(17, 0, ((1, b"x" * 65000),))
        self.assertEqual(archive.payload, large)
        with self.assertRaises(SaveError):
            EntityArchive(b"x" * (MAX_PLAYER_BYTES + 1))
        with self.assertRaises(SaveError):
            EntityArchive(bytearray(fixture()))

    def test_exact_archive_size_limit(self):
        template = (bytes(16), "X", bytes(80))
        base = fixture(entities=[], templates=[template])
        name = "X" * (MAX_PLAYER_BYTES - 14 - len(base) + 1)
        archive = EntityArchive(fixture(entities=[], templates=[(bytes(16), name, bytes(80))]))
        result = archive.append_entity(1, 0, ())
        self.assertEqual(len(result), MAX_PLAYER_BYTES)
        full = EntityArchive(result)
        with self.assertRaises(SaveError):
            full.append_entity(2, 0, ())
        self.assertEqual(full.payload, result)

    def test_groups_must_consume_exact_section(self):
        raw = fixture()
        archive = EntityArchive(raw)
        extra = raw[:archive.schema_at] + b"\x00" + raw[archive.schema_at:]
        extra = patch(extra, 0, "<I", archive.schema_at + 1)
        extra = patch(extra, 37, "<I", archive.schema_at - 40)
        with self.assertRaises(SaveError):
            EntityArchive(extra)

    def test_component_id_boundaries_and_zero_existing_reference(self):
        archive = EntityArchive(fixture(
            entities=[(0x100000000, 0, (), ())],
            definitions=[(0, "Zero", 4, b""), (65535, "Last", 1, b"")]))
        self.assertEqual(archive.references[0], 0x100000000)
        self.assertEqual(archive.next_entity_reference(), 1)
        result = archive.append_entity(1, 0, ((0, b""),), ((65535, b""),))
        appended = EntityArchive(result)
        self.assertEqual(appended.entities[-1].server[0].component_id, 0)
        self.assertEqual(appended.entities[-1].client[0].component_id, 65535)

    def test_entity_count_limit(self):
        raw = fixture(entities=[(i, 0, (), ()) for i in range(1, 65536)])
        archive = EntityArchive(raw)
        with self.assertRaises(SaveError):
            archive.append_entity(65536, 0, ())


if __name__ == "__main__":
    unittest.main()
