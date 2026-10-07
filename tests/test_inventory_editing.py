"""Synthetic inventory/YAML tests; no real saves or installed resources."""

from io import BytesIO
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_game_rules import assets, item, open_rules, balance
from test_player_state import character, payload
from test_save_format import container
from inventory_yaml import MAX_YAML, parse_inventory_yaml, read_inventory_yaml, write_inventory_yaml
from player_state import PlayerState
from save_format import SaveDocument, SaveError


class InventoryEditingTests(unittest.TestCase):
    def setUp(self):
        resources = [(0x12345678, item(1234, 50, "Synthetic wood")),
                     (0x12345678, item(5678, 20, "Synthetic stone")),
                     (0x87654321, balance())]
        stateful = bytearray(item(9000, 1, "Synthetic weapon"))
        stateful[1252] = 1
        randomized = bytearray(item(9001, 1, "Synthetic random item"))
        randomized[22] = 1
        resources += [(0x12345678, bytes(stateful)), (0x12345678, bytes(randomized))]
        equipment = bytearray(item(9002, 1, "Synthetic equipment"))
        equipment[204] = 1
        resources.append((0x12345678, bytes(equipment)))
        index, self.data, _ = assets(resources)
        self.rules = open_rules(index=index, resource_data=self.data)
        reader = patch.object(Path, "open", side_effect=lambda *a, **kw: BytesIO(self.data))
        reader.start()
        self.addCleanup(reader.stop)
        staleness = patch.object(self.rules, "assert_unchanged")
        self.staleness = staleness.start()
        self.addCleanup(staleness.stop)

    def document(self, raw=None):
        return SaveDocument(container([(123, b"CHAR", character(raw)),
                                       (456, b"CHAR", character()),
                                       (123, b"COUT", b"opaque appearance")]))

    def stacks(self, doc):
        return [(s.item_id, s.quantity) for s in PlayerState(doc.blob(123, b"CHAR").data).inventory()]

    def test_picker_excludes_persistent_and_randomized_items(self):
        self.assertEqual(set(self.rules.addable_items()), {1234, 5678})
        self.assertFalse(self.rules.items_for({9000})[9000].plain)
        self.assertFalse(self.rules.items_for({9001})[9001].plain)
        self.assertFalse(self.rules.items_for({9002})[9002].plain)
        self.rules._progression_verified = False
        with self.assertRaisesRegex(SaveError, "unverified"):
            self.rules.addable_items()

    def test_empty_slots_and_add_merge_split_preserve_unrelated_bytes(self):
        doc = self.document()
        before = PlayerState(doc.blob(123, b"CHAR").data)
        slots = before.inventory(include_empty=True)
        self.assertEqual([(s.slot, s.item_id, s.accessible) for s in slots],
                         [(0, 1234, True), (1, 0, True)])
        doc.merge_inventory(123, [(1234, 60)], self.rules)
        self.assertEqual(self.stacks(doc), [(1234, 50), (1234, 27)])
        after = PlayerState(doc.blob(123, b"CHAR").data)
        allowed = {i for s in slots for i in range(s.quantity_offset - 4, s.quantity_offset + 4)}
        self.assertTrue({i for i, (a, b) in enumerate(zip(before.payload, after.payload))
                         if a != b} <= allowed)
        self.assertEqual(before.experience(), after.experience())
        self.assertEqual(before.skills(), after.skills())
        output = SaveDocument(doc.serialize())
        self.assertEqual(output.blob(456, b"CHAR").compressed, doc.blob(456, b"CHAR").compressed)
        self.assertEqual(output.blob(123, b"COUT").compressed, doc.blob(123, b"COUT").compressed)

    def test_add_new_item_quantity_then_remove_and_revert(self):
        doc = self.document()
        doc.merge_inventory(123, [(5678, 10)], self.rules)
        self.assertEqual(self.stacks(doc), [(1234, 17), (5678, 10)])
        doc.edit_inventory(123, 100, 1, 19, self.rules)
        self.assertEqual(self.stacks(doc)[1], (5678, 19))
        doc.remove_inventory(123, 100, 1, self.rules)
        self.assertEqual(doc.serialize(), doc.original)
        self.assertFalse(doc.changes)
        doc.remove_inventory(123, 100, 0, self.rules)
        self.assertEqual(self.stacks(doc), [])
        doc.merge_inventory(123, [(1234, 17)], self.rules)
        self.assertEqual(doc.serialize(), doc.original)
        self.assertFalse(doc.changes)

    def test_full_inventory_invalid_id_or_quantity_import_is_atomic(self):
        for entries in ([(1234, 84)], [(5678, 21)], [(5678, 1), (9000, 1)],
                        [(9001, 1)], [(9999, 1)], [(1234, 0)], [(1234, -1)],
                        [(True, 1)], [(1234, True)], [(1234, 2 ** 32)]):
            doc = self.document()
            with self.subTest(entries=entries), self.assertRaises(SaveError):
                doc.merge_inventory(123, entries, self.rules)
            self.assertEqual(doc.serialize(), doc.original)
            self.assertFalse(doc.changes)
        doc = self.document()
        doc.merge_inventory(123, [(5678, 1)], self.rules)
        before, changes = doc.serialize(), dict(doc.changes)
        with self.assertRaises(SaveError):
            doc.merge_inventory(123, [(1234, 1), (5678, 20)], self.rules)
        self.assertEqual(doc.serialize(), before)
        self.assertEqual(doc.changes, changes)

    def test_protected_hidden_and_stateful_slots_not_touched(self):
        for raw in (payload(category=2, available=0), payload(available=1)):
            doc = self.document(raw)
            with self.subTest(raw=raw), self.assertRaises(SaveError):
                doc.merge_inventory(123, [(5678, 1)], self.rules)
            self.assertEqual(doc.serialize(), doc.original)
        doc = self.document(payload(stateful=True))
        original_slot = PlayerState(doc.blob(123, b"CHAR").data).inventory()[0]
        doc.merge_inventory(123, [(5678, 1)], self.rules)
        self.assertEqual(PlayerState(doc.blob(123, b"CHAR").data).inventory()[0], original_slot)
        for raw in (payload(stateful=True), payload(category=2, available=0)):
            doc = self.document(raw)
            with self.assertRaises(SaveError):
                doc.remove_inventory(123, 100, 0, self.rules)
            self.assertEqual(doc.inventory_export(123, self.rules), [])

    def test_export_current_staged_inventory_and_merge_duplicates(self):
        doc = self.document()
        doc.merge_inventory(123, [(1234, 1), (1234, 2)], self.rules)
        self.assertEqual(doc.inventory_export(123, self.rules),
                         [{"item_id": 1234, "name": "Synthetic wood", "quantity": 20}])
        self.staleness.side_effect = SaveError("Rules changed")
        for operation in (lambda: doc.merge_inventory(123, [(5678, 1)], self.rules),
                          lambda: doc.remove_inventory(123, 100, 0, self.rules),
                          lambda: doc.inventory_export(123, self.rules)):
            with self.assertRaisesRegex(SaveError, "Rules changed"):
                operation()

    def test_export_excludes_items_whose_definitions_need_state(self):
        doc = self.document()
        state = PlayerState(doc.blob(123, b"CHAR").data)
        doc.blob(123, b"CHAR").data = state.patch_inventory_slots({(100, 0): (9000, 1)})
        self.assertEqual(doc.inventory_export(123, self.rules), [])


class InventoryYamlTests(unittest.TestCase):
    def valid(self, items="- item_id: 1234\n  quantity: 17\n"):
        return ("format: enshrouded-inventory\nversion: 1\nitems:\n" + items).encode()

    def test_portable_roundtrip_and_exclusive_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.yaml"
            write_inventory_yaml(path, [
                {"item_id": 1234, "name": "Synthetic: material", "quantity": 17}])
            self.assertEqual(read_inventory_yaml(path), [(1234, 17)])
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_inventory_yaml(path, [])
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(parse_inventory_yaml(self.valid("  []\n")), [])

    def test_unsafe_or_malformed_yaml_rejected(self):
        invalid = [
            b"!!python/object/apply:os.system ['echo invalid']",
            self.valid("- item_id: true\n  quantity: 1\n"),
            self.valid("- item_id: 1234\n  quantity: false\n"),
            self.valid("- item_id: 1234\n  quantity: 0\n"),
            self.valid("- item_id: 1234\n  quantity: 1.5\n"),
            self.valid("- item_id: 1234\n  quantity: 1\n  entity: 100\n"),
            self.valid("- item_id: 1234\n  item_id: 5678\n  quantity: 1\n"),
            self.valid("- &a {item_id: 1234, quantity: 1}\n- *a\n"),
            self.valid("- item_id: 1234\n  quantity: 1\n  name: 1\n"),
            self.valid().replace(b"version: 1", b"version: true"),
            self.valid().replace(b"version: 1", b"version: 2"),
            self.valid() + b"version: 1\n",
            self.valid() + b"---\n{}\n",
            self.valid("- " + "[" * 10 + "]" * 10 + "\n"),
            b"\xff",
            b"x" * (MAX_YAML + 1),
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:80]), self.assertRaises(SaveError):
                parse_inventory_yaml(raw)
