"""Armor extraction uses generated PE/KFC fixtures, never copied game bytes."""

from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

import zstandard as zstd

from test_game_rules import assets, executable, item, open_rules, source_stat
from game_rules import ArmorRule, GameRules, _Kfc, _LEVEL_DEFINITION, _Resource
from save_format import SaveError


TEMPLATE_GUID = bytes(range(1, 17))
TEMPLATE_HASH = 0x10203040
ROOT_HASH = 0x20304050


def armor_executable():
    original, records = executable()
    data = bytearray(original)

    def allocate(value):
        at = len(data)
        data.extend(value)
        return at

    def address(at):
        return 0x180003000 + at - 512

    def text(value):
        return address(allocate(value.encode("ascii") + b"\0")), len(value)

    definitions = [
        ("keen::ItemRarity", 1, 12, "keen::uint8", 0),
        ("keen::EquipmentSlotType", 1, 12, "keen::uint8", 0),
        ("keen::ItemRarityMask", 1, 17, "keen::Bitmask8<keen::ItemRarity>", 0),
        ("keen::Bitmask8<keen::ItemRarity>", 1, 13, "keen::ItemRarity", 0),
        ("keen::ObjectReference<keen::ecs::Template>", 16, 28, "keen::ecs::Template", 0),
        ("keen::ecs::Template", 6, 18, None, 0),
        ("keen::PerkReference", 16, 17, "keen::ObjectReference<keen::Perk>", 0),
        ("keen::ObjectReference<keen::Perk>", 16, 28, "keen::Perk", 0),
        ("keen::Perk", 204, 18, None, 0),
        ("keen::PerkId", 4, 17, "keen::HashKey32", 0),
        ("keen::StaticArray<keen::PerkReference,5>", 80, 19, "keen::PerkReference", 0),
        ("keen::StaticArray<keen::PerkId,5>", 20, 19, "keen::PerkId", 0),
        ("keen::EquipmentSetup", 984, 18, None, 0),
        ("keen::ItemLevelRange", 8, 18, None, 0),
        ("keen::ItemDamageSetup", 64, 18, None, 0),
        ("keen::ItemArmorSetup", 40, 18, None, 0),
        ("keen::impact::ArmorDistribution", 36, 18, None, 0),
        ("keen::float32", 4, 10, None, 0),
        ("keen::string", 8, 17, "keen::BlobString", 0),
        ("keen::ecs::TemplateFamily", 1, 12, "keen::uint8", 0),
        ("keen::ecs::TemplateResource", 20, 18, None, TEMPLATE_HASH),
        ("keen::BaseAttributeResource", 28, 18, None, ROOT_HASH),
        ("keen::AttributeStructure", 16, 18, None, 0),
        ("keen::AttributeCommand", 4, 17, "keen::uint32", 0),
        ("keen::impact::AttributeIndex", 2, 17, "keen::uint16", 0),
        ("keen::ecs::Component", 1, 18, None, 0),
        ("keen::ecs::StaticTransform", 1, 18, "keen::ecs::Component", 0x5458E4A3),
        ("keen::BlobVariant<keen::ecs::Component>", 12, 27, "keen::ecs::Component", 0),
    ]
    for child in ("keen::BlobVariant<keen::ecs::Component>", "keen::HashKey32",
                  "keen::AttributeStructure", "keen::string", "keen::AttributeCommand"):
        definitions.append((f"keen::BlobArray<{child}>", 8, 24, child, 0))
    for name, size, kind, _, type_hash in definitions:
        at = allocate(bytes(104))
        records[name] = at
        struct.pack_into("<QQ", data, at, *text(name.split("::")[-1]))
        struct.pack_into("<QQ", data, at + 32, *text(name))
        struct.pack_into("<I", data, at + 64, size)
        data[at + 76] = kind
        struct.pack_into("<I", data, at + 80, type_hash)
        if kind == 19:
            struct.pack_into("<I", data, at + 72, 5)
    for name, _, _, inner, _ in definitions:
        if inner:
            struct.pack_into("<Q", data, records[name] + 56, address(records[inner]))
    struct.pack_into("<Q", data, records["keen::ecs::TemplateReference"] + 56,
                     address(records["keen::ObjectReference<keen::ecs::Template>"]))

    def fields(owner, entries, append=False):
        old = b""
        if append:
            at = records[owner]
            count = struct.unpack_from("<I", data, at + 72)[0]
            pointer = struct.unpack_from("<Q", data, at + 88)[0]
            start = pointer - 0x180003000 + 512
            old = bytes(data[start:start + count * 48])
        start = allocate(old + bytes(48 * len(entries)))
        struct.pack_into("<I", data, records[owner] + 72, len(old) // 48 + len(entries))
        struct.pack_into("<Q", data, records[owner] + 88, address(start))
        for i, (name, child, offset) in enumerate(entries):
            at = start + len(old) + 48 * i
            struct.pack_into("<QQ", data, at, *text(name))
            struct.pack_into("<QQ", data, at + 16, address(records[child]), offset)

    fields("keen::ItemInfo", [
        ("rarity", "keen::ItemRarity", 23),
        ("disableRarityGeneration", "keen::ItemRarityMask", 24),
        ("equipment", "keen::EquipmentSetup", 260),
        ("perkReferences", "keen::StaticArray<keen::PerkReference,5>", 1436),
        ("perkIds", "keen::StaticArray<keen::PerkId,5>", 1516),
        ("itemLevelRange", "keen::ItemLevelRange", 1536),
        ("damageSetup", "keen::ItemDamageSetup", 1544),
        ("armorSetup", "keen::ItemArmorSetup", 1608),
    ], append=True)
    fields("keen::EquipmentSetup", [("slot", "keen::EquipmentSlotType", 0)])
    fields("keen::ItemLevelRange", [("minLevel", "keen::uint32", 0),
                                   ("maxLevel", "keen::uint32", 4)])
    fields("keen::ItemDamageSetup", [("isSet", "keen::bool", 60)])
    fields("keen::ItemArmorSetup", [("distribution", "keen::impact::ArmorDistribution", 0),
                                   ("isSet", "keen::bool", 36)])
    fields("keen::impact::ArmorDistribution",
           [(name, "keen::float32", i * 4) for i, name in enumerate((
               "physical", "blunt", "pierce", "cut", "magical", "fire", "ice", "fog", "lightning"))])
    fields("keen::ecs::TemplateResource", [
        ("name", "keen::string", 0), ("family", "keen::ecs::TemplateFamily", 8),
        ("predictEntity", "keen::bool", 9), ("farCulling", "keen::bool", 10),
        ("omitTransformReplication", "keen::bool", 11),
        ("components", "keen::BlobArray<keen::BlobVariant<keen::ecs::Component>>", 12)])
    fields("keen::BaseAttributeResource", [
        ("type", "keen::HashKey32", 0), ("ids", "keen::BlobArray<keen::HashKey32>", 4),
        ("structure", "keen::BlobArray<keen::AttributeStructure>", 12),
        ("debugNames", "keen::BlobArray<keen::string>", 20)])
    fields("keen::AttributeStructure", [
        ("parentIndex", "keen::impact::AttributeIndex", 0),
        ("childIndex", "keen::impact::AttributeIndex", 2),
        ("siblingIndex", "keen::impact::AttributeIndex", 4),
        ("calculation", "keen::BlobArray<keen::AttributeCommand>", 8)])
    for owner, entries in (
            ("keen::ItemRarity", dict(enumerate(("Common", "Uncommon", "Rare", "Epic", "Legendary")))),
            ("keen::EquipmentSlotType", {
                0: "Invalid", 1: "MeleeWeapon", 13: "Armour_Head", 14: "Armour_UpperBody",
                15: "Armour_Arms", 16: "Armour_LowerBody", 17: "Armour_Feet", 40: "Backpack"})):
        start = allocate(bytes(40 * len(entries)))
        struct.pack_into("<I", data, records[owner] + 72, len(entries))
        struct.pack_into("<Q", data, records[owner] + 96, address(start))
        for i, (value, label) in enumerate(entries.items()):
            struct.pack_into("<QQQ", data, start + 40 * i, *text(label), value)
    struct.pack_into("<IIII", data, 272, len(data) - 512, 0x3000, len(data) - 512, 512)
    return bytes(data), records


def armor_item(key=0xFF224A43, minimum=30, maximum=50, rarity=2, generation=1,
               mask=0, slot=16, damage=0, armor=1, perks=(1, 2, 3, 4, 5),
               guid=TEMPLATE_GUID, sparse=False, category=1, stack=1):
    data = bytearray(item(key, stack, "Synthetic mage legs"))
    data[22:25] = bytes((generation, rarity, mask))
    data[204], data[260] = category, slot
    data[1252:1268] = guid
    for i in range(5):
        data[1436 + i * 16:1452 + i * 16] = bytes(16) if sparse and i == 4 else bytes([i + 1]) * 16
    struct.pack_into("<5I", data, 1516, *perks)
    struct.pack_into("<II", data, 1536, minimum, maximum)
    data[1604], data[1644] = damage, armor
    return bytes(data)


def template(name="Base_Pide", component=0x5458E4A3, count=1):
    encoded = name.encode()
    start = (20 + len(encoded) + 3) // 4 * 4
    data = bytearray(start + 12 * count + count)
    struct.pack_into("<II", data, 0, 20, len(encoded))
    struct.pack_into("<II", data, 12, start - 12, count)
    data[20:20 + len(encoded)] = encoded
    for i in range(count):
        at = start + 12 * i
        struct.pack_into("<III", data, at, component, start + 12 * count + i - at - 4, 1)
    return bytes(data)


def level_root(ids=(0x724BF568, 0x638A979A)):
    data = bytearray(84)
    struct.pack_into("<I", data, 0, 0x98765432)
    struct.pack_into("<II", data, 4, 24, 2)
    struct.pack_into("<II", data, 12, 24, 2)
    struct.pack_into("<II", data, 20, 48, 2)
    struct.pack_into("<2I", data, 28, *ids)
    struct.pack_into("<3H", data, 36, 65535, 1, 65535)
    struct.pack_into("<3H", data, 52, 0, 65535, 65535)
    for i, name in enumerate((b"Level", b"Level_Max")):
        at = 68 + 8 * i
        struct.pack_into("<II", data, at, len(data) - at, len(name))
        data.extend(name)
    return bytes(data)


def armor_assets(items=None, templates=None, roots=None, *, split=None):
    resources = [(0x12345678, value, bytes([100 + i]) * 16)
                 for i, value in enumerate(items or [armor_item()])]
    resources += [(TEMPLATE_HASH, value, guid)
                  for guid, value in (templates if templates is not None else [(TEMPLATE_GUID, template())])]
    resources += [(ROOT_HASH, value, guid)
                  for guid, value in (roots if roots is not None else [(_LEVEL_DEFINITION, level_root())])]
    index, compressed, tables = assets([(kind, value) for kind, value, _ in resources],
                                       split=split)
    index = bytearray(index)
    for i, (_, _, guid) in enumerate(resources):
        index[tables[0] + i * 32:tables[0] + i * 32 + 16] = guid
    return bytes(index), compressed


class ArmorRulesTests(unittest.TestCase):
    def test_interleaved_items_decode_each_chunk_once(self):
        items = [armor_item(key=7000 + i) for i in range(8)]
        index, self.compressed = armor_assets(items, split=sum(map(len, items[:4])))
        self.rules = open_rules(armor_executable()[0], index, self.compressed, verified=True)
        resources = self.rules._kfc.resources
        self.rules._kfc.resources = [
            resource for pair in zip(resources[:4], resources[4:8]) for resource in pair
        ] + resources[8:]
        self.read(self.rules.item_catalogue)
        with patch.object(self.rules, "_armor_level_root", return_value=0x724BF568), \
                patch.object(self.rules, "_armor_template", return_value=True), \
                patch("game_rules.zstd.ZstdDecompressor", wraps=zstd.ZstdDecompressor) as decoder:
            catalogue = self.read()
        self.assertEqual(set(catalogue), set(range(7000, 7008)))
        self.assertEqual(decoder.call_count, 2)

    def load(self, items=None, templates=None, roots=None, exe=None, verified=True):
        index, self.compressed = armor_assets(items, templates, roots)
        if exe is None:
            exe = armor_executable()[0]
        self.rules = open_rules(exe, index, self.compressed, verified=verified)
        return self.rules

    def read(self, operation=None):
        with patch.object(Path, "open", side_effect=lambda *a, **kw: BytesIO(self.compressed)), \
                patch.object(self.rules, "assert_unchanged"):
            return (operation or self.rules.armor_catalogue)()

    def test_requested_shape_frozen_api_and_independent_plain_workflow(self):
        self.load([armor_item(), item()])
        found = self.read()
        rule = found[0xFF224A43]
        self.assertIsInstance(rule, ArmorRule)
        self.assertEqual((rule.min_level, rule.max_level, rule.default_rarity), (30, 50, 2))
        self.assertEqual(rule.template_guid, TEMPLATE_GUID)
        self.assertEqual(rule.perk_ids, (1, 2, 3, 4, 5))
        self.assertEqual(rule.level_definition, _LEVEL_DEFINITION)
        self.assertEqual(rule.level_root_id, 0x724BF568)
        self.assertEqual(rule.level_signature, 0x57B5E058)
        self.assertEqual(rule.rarities, tuple(enumerate(("Common", "Uncommon", "Rare", "Epic", "Legendary"))))
        with self.assertRaises(FrozenInstanceError):
            rule.min_level = 1
        found.clear()
        self.assertEqual(self.read(lambda: self.rules.armor_for(rule.item_id)), rule)
        self.assertEqual(set(self.read(self.rules.addable_items)), {123})

    def test_fixed_rarity_and_duplicate_perks_are_supported(self):
        self.load([armor_item(generation=0, rarity=4, perks=(8,) * 5)])
        rule = self.read()[0xFF224A43]
        self.assertEqual(rule.rarities, ((4, "Legendary"),))
        self.assertEqual(rule.perk_ids, (8,) * 5)

    def test_exclusion_boundaries(self):
        variants = [
            dict(mask=1), dict(mask=255), dict(sparse=True), dict(perks=(1, 2, 0, 4, 5)),
            dict(category=2), dict(stack=2), dict(damage=1), dict(armor=0),
            dict(slot=1), dict(slot=40), dict(guid=bytes(16)),
            dict(minimum=0), dict(minimum=51), dict(maximum=256)]
        for variant in variants:
            with self.subTest(variant=variant):
                self.load([armor_item(**variant)])
                self.assertEqual(self.read(), {})
                with self.assertRaisesRegex(SaveError, "not supported"):
                    self.read(lambda: self.rules.armor_for(0xFF224A43))
        self.load(templates=[(TEMPLATE_GUID, template("Custom_Pide"))])
        self.assertEqual(self.read(), {})
        self.load(templates=[(TEMPLATE_GUID, template(component=0x1234))])
        self.assertEqual(self.read(), {})
        self.load(templates=[(TEMPLATE_GUID, template(count=2))])
        self.assertEqual(self.read(), {})

    def test_level_boundaries(self):
        for minimum, maximum in ((1, 1), (255, 255), (1, 255)):
            with self.subTest(minimum=minimum, maximum=maximum):
                self.load([armor_item(minimum=minimum, maximum=maximum)])
                rule = self.read()[0xFF224A43]
                self.assertEqual((rule.min_level, rule.max_level), (minimum, maximum))

    def test_missing_ambiguous_and_wrong_type_guid_resources(self):
        for templates, roots in (
                ([], None), ([(TEMPLATE_GUID, template())] * 2, None),
                (None, []), (None, [(_LEVEL_DEFINITION, level_root())] * 2),
                (None, [(TEMPLATE_GUID, level_root())])):
            with self.subTest(templates=templates, roots=roots):
                self.load(templates=templates, roots=roots)
                with self.assertRaisesRegex(SaveError, "GUID resource"):
                    self.read()
        self.load()
        resource = self.rules._kfc.resources[1]
        object.__setattr__(resource, "type_hash", ROOT_HASH)
        with self.assertRaisesRegex(SaveError, "does not resolve"):
            self.read()

    def test_root_mismatch_and_bounded_dynamic_data(self):
        for ids in ((0, 0x638A979A), (0x638A979A, 0x724BF568)):
            self.load(roots=[(_LEVEL_DEFINITION, level_root(ids))])
            with self.assertRaisesRegex(SaveError, "root IDs"):
                self.read()
        for at, value in ((4, 0xFFFFFFFF), (8, 3), (12, 0), (36, 0), (44, 0xFFFFFFFF),
                          (48, 1), (68, 0xFFFFFFFF)):
            data = bytearray(level_root())
            struct.pack_into("<I", data, at, value)
            self.load(roots=[(_LEVEL_DEFINITION, bytes(data))])
            with self.subTest(at=at), self.assertRaises(SaveError):
                self.read()
        for at, value in ((0, 0), (12, 0xFFFFFFFF), (16, 645), (36, 0), (40, 200)):
            data = bytearray(template())
            struct.pack_into("<I", data, at, value)
            self.load(templates=[(TEMPLATE_GUID, bytes(data))])
            with self.subTest(at=at), self.assertRaises(SaveError):
                self.read()

    def test_malformed_metadata_blocks_only_armor(self):
        self.load([item()], exe=executable()[0])
        with self.assertRaises(SaveError):
            self.read()
        self.assertEqual(set(self.read(self.rules.addable_items)), {123})
        exe, records = armor_executable()
        for at, value in (
                (records["keen::StaticArray<keen::PerkId,5>"] + 72, 4),
                (records["keen::ItemArmorSetup"] + 64, 41),
                (records["keen::ItemRarity"] + 72, 4)):
            broken = bytearray(exe)
            struct.pack_into("<I", broken, at, value)
            self.load([armor_item(), item()], exe=bytes(broken))
            with self.subTest(at=at), self.assertRaises(SaveError):
                self.read()
            self.assertEqual(set(self.read(self.rules.addable_items)), {123})
        for at, value in ((records["keen::PerkId"] + 56,
                           0x180003000 + records["keen::uint32"] - 512),):
            broken = bytearray(exe)
            struct.pack_into("<Q", broken, at, value)
            self.load(exe=bytes(broken))
            with self.assertRaisesRegex(SaveError, "underlying"):
                self.read()
        owner = records["keen::ItemArmorSetup"]
        fields = struct.unpack_from("<Q", exe, owner + 88)[0] - 0x180003000 + 512
        for at, value in (
                (fields + 24, 4),
                (fields + 16, 0x180003000 + records["keen::ItemDamageSetup"] - 512)):
            broken = bytearray(exe)
            struct.pack_into("<Q", broken, at, value)
            self.load(exe=bytes(broken))
            with self.subTest(at=at), self.assertRaises(SaveError):
                self.read()

    def test_invalid_flags_default_rarity_and_slot_are_fatal(self):
        for values in (dict(generation=2), dict(damage=2), dict(armor=2),
                       dict(rarity=5), dict(slot=255)):
            self.load([armor_item(**values)])
            with self.subTest(values=values), self.assertRaises(SaveError):
                self.read()

    def test_unique_template_is_decoded_once_and_cache_checks_sources(self):
        self.load([armor_item(key=10), armor_item(key=11)])
        with patch.object(self.rules, "_armor_template", wraps=self.rules._armor_template) as load:
            self.assertEqual(len(self.read()), 2)
            self.assertEqual(load.call_count, 1)
        with patch.object(self.rules, "assert_unchanged", side_effect=SaveError("source changed")):
            with self.assertRaisesRegex(SaveError, "source changed"):
                self.rules.armor_catalogue()
        with patch.object(Path, "stat", return_value=source_stat(len(self.compressed), st_mtime_ns=101)):
            with self.assertRaisesRegex(SaveError, "source changed"):
                self.rules.armor_catalogue()

    def test_build_guard_and_invalid_ids(self):
        self.load(verified=False)
        with self.assertRaisesRegex(SaveError, "unverified"):
            self.read()
        for value in (-1, 2 ** 32, True, "1"):
            with self.assertRaises(SaveError):
                self.rules.armor_for(value)
        missing = GameRules()
        with self.assertRaises(SaveError):
            missing.armor_catalogue()
        missing._progression_verified = True
        with self.assertRaisesRegex(SaveError, "metadata"):
            missing.armor_catalogue()

    def test_index_retains_guid_and_old_resource_constructor(self):
        index, compressed = armor_assets()
        with patch.object(Path, "stat", return_value=source_stat(len(compressed))):
            kfc = _Kfc(index, Path("synthetic-resources"))
        self.assertEqual(kfc.resources[1].guid, TEMPLATE_GUID)
        self.assertEqual(kfc.resources[2].guid, _LEVEL_DEFINITION)
        self.assertEqual(_Resource(1, 2, 3, 0).guid, b"")

    def test_conflicting_armor_rules_do_not_hide_behind_plain_equivalence(self):
        for second in (armor_item(minimum=31), armor_item(mask=1), armor_item(sparse=True)):
            self.load([armor_item(), second])
            with self.assertRaisesRegex(SaveError, "Conflicting installed armor"):
                self.read()

    def test_part_zero_is_required_for_template_and_level_definition(self):
        for position in (1, 2):
            self.load()
            object.__setattr__(self.rules._kfc.resources[position], "part", 1)
            with self.subTest(position=position), self.assertRaisesRegex(SaveError, "GUID resource"):
                self.read()


if __name__ == "__main__":
    unittest.main()
