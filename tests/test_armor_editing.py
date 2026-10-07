from dataclasses import replace
import struct
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from armor_editing import COMPONENT_HASHES, add_armor
from entity_format import EntityArchive
from game_rules import ArmorRule, GameRules, ItemRule
from player_state import PlayerState, Schema
from save_format import Bdb, SaveDocument, SaveError
from test_entity_format import fixture
from test_player_state import (
    attribute, attribute_schema, character, inventory_schema, schema, setup_schema, skills_schema,
)
from test_save_format import container


RARITIES = tuple(enumerate(("Common", "Uncommon", "Rare", "Epic", "Legendary")))
FLAGS = tuple(enumerate(("Broken", "DurabilityInitialized", "ContinuousDurabilityLoss",
                        "IsEquipped", "HasLevel", "HasDamage", "IsNew",
                        "HasContinuousDurability", "HasWeaponGemSlot", "IsTinted")))


def armor_rule():
    return ArmorRule(
        item_id=7000, label="Synthetic mage legs", template_guid=b"\x11" * 16,
        min_level=30, max_level=50, rarities=RARITIES, default_rarity=2,
        level_definition=b"\x22" * 16, level_root_id=0x724BF568,
        level_signature=0x57B5E058, perk_ids=(111, 222, 333, 111, 555))


def level_schema():
    return schema("Level", [
        ("Level", 2, 44, 2, 18, [("definition", 8, 20), ("dataStorage", 10, 36)], 0),
        ("Attribute", 0, 20, 5, 18, [
            ("rootId", 3, 0), ("signature", 3, 4), ("storageOffset", 4, 8),
            ("storageSize", 4, 12), ("flags", 5, 16)], 0),
        ("HashKey32", 0, 4, 1, 18, [("value", 4, 0)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
        ("Bitmask16<keen::ecs::AttributeFlags>", 6, 2, 0, 14, [], 0),
        ("AttributeFlags", 7, 1, 2, 12, [], 1),
        ("uint8", 0, 1, 0, 2, [], 0),
        ("AttributeRootReference", 9, 16, 0, 17, [], 0),
        ("ObjectReference", 0, 16, 0, 28, [], 0),
        ("Values", 4, 8, 2, 19, [], 0),
    ], (("Initialized", 0), ("Break_OnWrite", 1)))


def item_state_schema():
    return schema("ItemState", [
        ("ItemState", 0, 20, 6, 18, [
            ("containedInSlotId", 2, 0), ("itemId", 6, 8), ("baseDamageUi", 8, 12),
            ("itemState", 9, 14), ("itemRarityUi", 12, 16), ("itemLevelUi", 5, 17)], 0),
        ("InventorySlotId", 0, 8, 2, 18, [("entityId", 3, 0), ("slotIndex", 5, 4)], 0),
        ("EntityId", 0, 4, 1, 18, [("id", 4, 0)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
        ("uint8", 0, 1, 0, 2, [], 0),
        ("ItemId", 7, 4, 0, 17, [], 0),
        ("HashKey32", 0, 4, 1, 18, [("value", 4, 0)], 0),
        ("uint16", 0, 2, 0, 4, [], 0),
        ("ItemStateMask", 10, 2, 0, 17, [], 0),
        ("Bitmask16<keen::ItemStateFlag>", 11, 2, 0, 14, [], 0),
        ("ItemStateFlag", 5, 1, 10, 12, [], 1),
        ("ItemRarity", 5, 1, 5, 12, [], 11),
    ], tuple((label, value) for value, label in (*FLAGS, *RARITIES)))


def owner_schema():
    return schema("OwnerRelationship", [
        ("OwnerRelationship", 0, 4, 1, 18, [("ownerEntityId", 2, 0)], 0),
        ("EntityId", 0, 4, 1, 18, [("id", 3, 0)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
    ])


def used_schema():
    return schema("UsedItem", [
        ("UsedItem", 0, 4, 1, 18, [("itemId", 2, 0)], 0),
        ("ItemId", 3, 4, 0, 17, [], 0),
        ("HashKey32", 0, 4, 1, 18, [("value", 4, 0)], 0),
        ("uint32", 0, 4, 0, 6, [], 0),
    ])


def perk_schema():
    return schema("PerkContainerNew", [
        ("PerkContainerNew", 0, 2, 2, 18, [
            ("availablePerks", 2, 0), ("unlockedPerks", 2, 1)], 0),
        ("uint8", 0, 1, 0, 2, [], 0),
    ])


def armor_character(*, full=False, available=2, missing_perks=False, container_entity=100):
    rule = armor_rule()
    definitions = [
        (1, "Experience", 48, attribute_schema("Experience", 3)),
        (2, "Level", 44, level_schema()),
        (3, "InventorySetup", 13, setup_schema()),
        (4, "Inventory", 24, inventory_schema()),
        (5, "UnlockedSkillNodes", 22, skills_schema()),
        (6, "ItemState", 20, item_state_schema()),
        (7, "OwnerRelationship", 4, owner_schema()),
        (8, "PerkContainerNew", 2, perk_schema()),
        (9, "UsedItem", 4, used_schema()),
        (10, "StaticTransform", 1,
         schema("StaticTransform", [("StaticTransform", 0, 1, 0, 18, [], 0)])),
    ]
    if missing_perks:
        definitions = [entry for entry in definitions if entry[1] != "PerkContainerNew"]
    native_level = (struct.pack("<IIIIH2x", rule.level_root_id, rule.level_signature, 36, 2, 1)
                    + rule.level_definition + struct.pack("<II", 25, 100))
    entities = [
        (1, 1, ((1, attribute((1548, 0, 4252))), (2, native_level),
                (3, struct.pack("<IIBBBBB", 100, 0, 4, 0, 2, available, 1)),
                (5, bytes(22))), ()),
        (container_entity, 1, ((4, struct.pack("<IIIIII", 1234 if full else 0, 17 if full else 0,
                                 0, 5678 if full else 0, 20 if full else 0, 0)),), ()),
    ]
    mask = bytearray(80)
    mask[10 // 8] |= 1 << (10 % 8)
    raw = bytearray(fixture(
        entities, definitions, [(rule.template_guid, "Base_Pide", bytes(mask)),
                                (b"\x33" * 16, "SyntheticPlayer", bytes(80))]))
    archive = EntityArchive(bytes(raw))
    struct.pack_into("<I", raw, archive.schema_at + 8,
                     sum(len(metadata) for _, _, _, metadata in definitions))
    cursor = archive.schema_at + 20
    for _, name, _, metadata in definitions:
        header = cursor + 4 + len(name)
        value = COMPONENT_HASHES.get(name, 0)
        struct.pack_into("<II", raw, header, value, value)
        cursor = header + 14 + len(metadata)
    return character(bytes(raw))


def mocked_rules():
    rules = Mock(spec=GameRules)
    rule = armor_rule()

    def armor_for(item_id):
        if item_id != rule.item_id:
            raise SaveError("Unsupported armor")
        return rule

    rules.armor_for.side_effect = armor_for
    rules.armor_catalogue.return_value = {rule.item_id: rule}
    rules.items_for.side_effect = lambda ids: {
        key: ItemRule(key, 50, "Synthetic item", key != rule.item_id) for key in ids}
    rules.addable_items.return_value = {1234: ItemRule(1234, 50, "Synthetic material", True)}
    return rules


class ArmorEditingTests(unittest.TestCase):
    def test_full_container_ids_use_unique_low32_saved_backlinks(self):
        from test_component_schema import generated_definition
        container = 0x1234567800000064
        before = PlayerState(armor_character(missing_perks=True, container_entity=container))
        after = PlayerState(add_armor(before, armor_rule(), 30, 2,
                                      perk_definition=generated_definition()))
        stack = after.inventory()[0]
        self.assertEqual(stack.entity, container)
        new = after.archive.entities[-1]
        components = {after.archive.definitions[c.component_id].name: c.data for c in new.server}
        self.assertEqual(struct.unpack_from("<IB", components["ItemState"]), (100, 0))
        self.assertEqual(components["OwnerRelationship"], struct.pack("<I", 100))
        self.assertEqual(after.references[100], container)
        self.assertEqual(after.experience(), before.experience())

    def test_missing_perk_schema_is_generated_atomically_and_reused(self):
        from test_component_schema import generated_definition
        original = armor_character(missing_perks=True)
        doc = SaveDocument(container([(123, b"CHAR", original),
                                      (456, b"CHAR", armor_character())]))
        rules = mocked_rules()
        definition = generated_definition()
        rules.perk_component_definition.return_value = definition
        before = PlayerState(original)
        doc.add_armor(123, 7000, 30, 0, rules)
        after = PlayerState(doc.blob(123, b"CHAR").data)
        self.assertEqual(len(after.archive.definitions), len(before.archive.definitions) + 1)
        generated_id = next(key for key, value in after.archive.definitions.items()
                            if value.name == "PerkContainerNew")
        self.assertEqual(after.archive.definitions[generated_id], definition)
        self.assertEqual(after.experience(), before.experience())
        self.assertEqual(after.skills(), before.skills())
        for key, value in before.archive.definitions.items():
            self.assertEqual(after.archive.definitions[key], value)
        self.assertTrue(any("generated missing PerkContainerNew" in value
                            for value in doc.changes.values()))
        doc.edit_character(123, "Abcd", 25)
        doc.add_armor(123, 7000, 50, 4, rules)
        rules.perk_component_definition.assert_called_once()
        after = PlayerState(doc.blob(123, b"CHAR").data)
        self.assertEqual(after.archive.definitions[generated_id], definition)
        self.assertEqual(len(after.archive.definitions), len(before.archive.definitions) + 1)
        for entity, rarity in zip(after.archive.entities[-2:], (0, 4)):
            perks = next(c for c in entity.server if c.component_id == generated_id)
            self.assertEqual(perks.data, bytes((rarity + 1, 0)))
        roundtrip = SaveDocument(doc.serialize())
        self.assertEqual(PlayerState(roundtrip.blob(123, b"CHAR").data).archive.definitions,
                         after.archive.definitions)
        self.assertEqual(roundtrip.blob(456, b"CHAR").compressed,
                         doc.blob(456, b"CHAR").compressed)

    def test_missing_schema_failures_do_not_stage_schema_only_changes(self):
        from test_component_schema import generated_definition
        for full, level, rarity, quantity in (
                (True, 30, 2, 1), (False, 29, 2, 1), (False, 30, 5, 1),
                (False, 30, 2, 3), (False, 30, 2, True)):
            doc = SaveDocument(container([(123, b"CHAR",
                                           armor_character(full=full, missing_perks=True))]))
            original = doc.serialize()
            rules = mocked_rules()
            rules.perk_component_definition.return_value = generated_definition()
            with self.subTest(full=full, level=level, rarity=rarity, quantity=quantity):
                with self.assertRaises(SaveError):
                    doc.add_armor(123, 7000, level, rarity, rules, quantity)
                self.assertEqual(doc.serialize(), original)
                self.assertFalse(doc.changes)
                self.assertFalse(doc.rule_sources)
        doc = SaveDocument(container([(123, b"CHAR", armor_character(missing_perks=True))]))
        original = doc.serialize()
        rules = mocked_rules()
        rules.perk_component_definition.side_effect = SaveError("Unsupported installed metadata")
        with self.assertRaisesRegex(SaveError, "metadata"):
            doc.add_armor(123, 7000, 30, 2, rules)
        self.assertEqual(doc.serialize(), original)
        self.assertFalse(doc.changes)
        rules.perk_component_definition.side_effect = None
        rules.perk_component_definition.return_value = replace(
            generated_definition(), schema=b"invalid CTCB")
        with self.assertRaises(SaveError):
            doc.add_armor(123, 7000, 30, 2, rules)
        self.assertEqual(doc.serialize(), original)

    def test_existing_schemas_are_not_generated_or_silently_repaired(self):
        from test_component_schema import generated_definition
        rules = mocked_rules()
        rules.perk_component_definition.side_effect = SaveError("Must not generate")
        doc = SaveDocument(container([(123, b"CHAR", armor_character())]))
        before = PlayerState(doc.blob(123, b"CHAR").data)
        doc.add_armor(123, 7000, 30, 2, rules)
        rules.perk_component_definition.assert_not_called()
        after = PlayerState(doc.blob(123, b"CHAR").data)
        self.assertEqual(before.archive.definitions, after.archive.definitions)
        self.assertFalse(any("generated missing" in v for v in doc.changes.values()))
        raw = before.payload.replace(b"unlockedPerks", b"unexpectedKey")
        doc = SaveDocument(container([(123, b"CHAR", character(raw))]))
        original = doc.serialize()
        with self.assertRaisesRegex(SaveError, "fields"):
            doc.add_armor(123, 7000, 30, 2, rules)
        rules.perk_component_definition.assert_not_called()
        self.assertEqual(doc.serialize(), original)
        raw = before.payload.replace(b"OwnerRelationship", b"X" * len("OwnerRelationship"))
        with self.assertRaisesRegex(SaveError, "OwnerRelationship"):
            add_armor(PlayerState(character(raw)), armor_rule(), 30, 2,
                      perk_definition=generated_definition())

    def test_all_rarities_and_level_boundaries_initialize_exact_values(self):
        original = armor_character()
        state = PlayerState(original)
        for level in (30, 50):
            for rarity in range(5):
                with self.subTest(level=level, rarity=rarity):
                    result = PlayerState(add_armor(state, armor_rule(), level, rarity))
                    stack = result.inventory()[0]
                    self.assertEqual((stack.entity, stack.slot, stack.item_id, stack.quantity),
                                     (100, 0, 7000, 1))
                    self.assertGreater(stack.item_entity, 100)
                    entity = result.archive.entities[-1]
                    self.assertEqual(stack.item_entity, entity.entity_id)
                    components = {result.archive.definitions[c.component_id].name: c.data
                                  for c in (*entity.server, *entity.client)}
                    self.assertEqual(set(components), set(COMPONENT_HASHES))
                    self.assertEqual(components["ItemState"],
                                     struct.pack("<IB3xIHHBB2x", 100, 0, 7000, 0, 16, rarity, level))
                    self.assertEqual(components["OwnerRelationship"], struct.pack("<I", 100))
                    self.assertEqual(components["UsedItem"], struct.pack("<I", 7000))
                    self.assertEqual(components["PerkContainerNew"], bytes((rarity + 1, 0)))
                    self.assertEqual(components["StaticTransform"], b"")
                    self.assertEqual(struct.unpack_from("<II", components["Level"], 36),
                                     (level, 50))
                    self.assertEqual(components["Level"][:36],
                                     struct.pack("<IIIIH2x", 0x724BF568, 0x57B5E058, 36, 2, 1)
                                     + b"\x22" * 16)
                    self.assertEqual(result.experience(), state.experience())
                    self.assertEqual(result.skills(), state.skills())
                    self.assertEqual(Bdb(result.character).character(), Bdb(original).character())
                    self.assertEqual(result.payload[result.archive.schema_at:],
                                     state.payload[state.archive.schema_at:])
                    self.assertNotIn("PideRebalanced", components)

    def test_two_items_have_distinct_references_and_slot_backlinks(self):
        state = PlayerState(armor_character())
        result = PlayerState(add_armor(state, armor_rule(), 32, 4, 2))
        self.assertEqual(len(result.entities), len(state.entities) + 2)
        self.assertEqual(len({s.item_entity for s in result.inventory()}), 2)
        for stack, entity in zip(result.inventory(), result.archive.entities[-2:]):
            item = next(c for c in entity.server
                        if result.archive.definitions[c.component_id].name == "ItemState")
            self.assertEqual(struct.unpack_from("<IB", item.data), (stack.entity, stack.slot))
        descriptors_before = Bdb(state.character).section(28, 8)[1]
        self.assertEqual(Bdb(result.character).section(28, 8)[1], descriptors_before + 1)

    def test_invalid_inputs_and_capacity_are_atomic(self):
        state = PlayerState(armor_character())
        for level, rarity, quantity in (
            (29, 2, 1), (51, 2, 1), (True, 2, 1), (30.0, 2, 1),
            (30, 5, 1), (30, True, 1), (30, 2, 0), (30, 2, 3), (30, 2, True)):
            with self.subTest(level=level, rarity=rarity, quantity=quantity):
                with self.assertRaises(SaveError):
                    add_armor(state, armor_rule(), level, rarity, quantity)
                self.assertEqual(state.character, armor_character())
        for raw in (armor_character(full=True), armor_character(available=0)):
            with self.assertRaisesRegex(SaveError, "empty inventory slots"):
                add_armor(PlayerState(raw), armor_rule(), 30, 2)
        with self.assertRaisesRegex(SaveError, "rarity"):
            add_armor(state, replace(armor_rule(), rarities=((2, "Rare"),)), 30, 4)

    def test_missing_template_schema_and_changed_flags_are_blocked(self):
        state = PlayerState(armor_character())
        with self.assertRaisesRegex(SaveError, "Base_Pide"):
            add_armor(state, replace(armor_rule(), template_guid=b"\xff" * 16), 30, 2)
        for label in (b"Initialized", b"HasLevel", b"OwnerRelationship", b"Base_Pide"):
            raw = state.payload.replace(label, b"X" * len(label))
            with self.subTest(label=label), self.assertRaises(SaveError):
                add_armor(PlayerState(character(raw)), armor_rule(), 30, 2)

    def test_wrong_template_mask_and_component_hash_are_rejected(self):
        state = PlayerState(armor_character())
        template = state.archive.templates[0]
        raw = bytearray(state.payload)
        mask_at = template.offset + 16 + 4 + len(template.name)
        raw[mask_at] |= 1
        with self.assertRaisesRegex(SaveError, "base components"):
            add_armor(PlayerState(character(bytes(raw))), armor_rule(), 30, 2)
        raw = bytearray(state.payload)
        at = raw.index(b"OwnerRelationship", state.archive.schema_at) + len("OwnerRelationship")
        struct.pack_into("<I", raw, at, 0)
        with self.assertRaisesRegex(SaveError, "OwnerRelationship definition"):
            add_armor(PlayerState(character(bytes(raw))), armor_rule(), 30, 2)

    def test_repeated_staging_and_rename_preserve_first_armor(self):
        doc = SaveDocument(container([(123, b"CHAR", armor_character())]))
        rules = mocked_rules()
        doc.add_armor(123, 7000, 30, 0, rules)
        first = PlayerState(doc.blob(123, b"CHAR").data).archive.entities[-1]
        doc.edit_character(123, "Abcd", 25)
        doc.add_armor(123, 7000, 50, 4, rules)
        state = PlayerState(doc.blob(123, b"CHAR").data)
        self.assertEqual(state.archive.entities[-2].entity_id, first.entity_id)
        self.assertEqual(tuple((c.component_id, c.data) for c in state.archive.entities[-2].server),
                         tuple((c.component_id, c.data) for c in first.server))
        self.assertEqual(Bdb(state.character).character(), ("Abcd", 25))
        self.assertTrue(any("level 30, Common" in text for text in doc.changes.values()))
        self.assertTrue(any("level 50, Legendary" in text for text in doc.changes.values()))
        before = doc.serialize()
        with self.assertRaisesRegex(SaveError, "empty inventory slots"):
            doc.add_armor(123, 7000, 35, 2, rules)
        self.assertEqual(doc.serialize(), before)

    def test_document_staging_preview_plain_edit_and_preservation(self):
        doc = SaveDocument(container([(123, b"CHAR", armor_character()),
                                      (456, b"CHAR", armor_character())]))
        rules = mocked_rules()
        doc.add_armor(123, 7000, 30, 2, rules)
        preview = next(iter(doc.changes.values()))
        self.assertIn("Synthetic mage legs, level 30, Rare", preview)
        self.assertIn("0 unlocked upgrades", preview)
        doc.merge_inventory(123, [(1234, 10)], rules)
        self.assertTrue(any("level 30, Rare" in value for value in doc.changes.values()))
        encoded = doc.serialize()
        roundtrip = SaveDocument(encoded)
        self.assertEqual(roundtrip.blob(456, b"CHAR").compressed, doc.blob(456, b"CHAR").compressed)
        self.assertEqual(len(PlayerState(roundtrip.blob(123, b"CHAR").data).inventory()), 2)
        self.assertEqual(len(doc.inventory_export(123, rules)), 1)
        stack = PlayerState(doc.blob(123, b"CHAR").data).inventory()[0]
        with self.assertRaisesRegex(SaveError, "plain Generic"):
            doc.remove_inventory(123, stack.entity, stack.slot, rules)

    def test_document_source_failure_does_not_mutate(self):
        doc = SaveDocument(container([(123, b"CHAR", armor_character())]))
        original = doc.serialize()
        rules = mocked_rules()
        rules.assert_unchanged.side_effect = SaveError("Game rule source changed")
        with self.assertRaisesRegex(SaveError, "source changed"):
            doc.add_armor(123, 7000, 30, 2, rules)
        self.assertEqual(doc.serialize(), original)
        self.assertFalse(doc.changes)

    def test_ctcb_enum_alignment_in_item_and_attribute_schemas(self):
        s = Schema(item_state_schema())
        self.assertEqual(s.enum(s.field("itemRarityUi")[1]), dict(RARITIES))
        s = Schema(level_schema())
        mask = s.type(s.field("flags")[1])
        self.assertEqual(s.enum(mask.inner)[0], "Initialized")


if __name__ == "__main__":
    unittest.main()
