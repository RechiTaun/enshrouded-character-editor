"""Synthetic fixtures only; no executable, game asset or save data is stored."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_rules import (GameRules, ItemRule, MAX_CHUNK, ProgressionRules,
                        _Kfc, _Pe, _Resource)
from save_format import SaveError


def executable(stack_offset=20, stack_type="keen::uint16", duplicate=False):
    """Build a minimal PE with arbitrary image addresses and reflected fields."""
    data = bytearray(512)
    base, rva, raw = 0x180000000, 0x3000, 512
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HH", data, 132, 0x8664, 1)
    struct.pack_into("<H", data, 148, 112)
    struct.pack_into("<H", data, 152, 0x20B)
    struct.pack_into("<Q", data, 176, base)

    def allocate(value):
        at = len(data)
        data.extend(value)
        return at

    def address(at):
        return base + rva + at - raw

    def text(value):
        return address(allocate(value.encode("ascii") + b"\0")), len(value)

    records = {}
    definitions = [
        ("keen::uint32", 4, 6, 0),
        ("keen::uint16", 2, 4, 0),
        ("keen::BlobString", 8, 25, 0),
        ("keen::HashKey32", 4, 18, 0),
        ("keen::ItemId", 4, 17, 0),
        ("keen::ItemInfo", 1944, 18, 0x12345678),
        ("keen::BalancingTable", 624, 18, 0x87654321),
        ("keen::StaticArray<keen::uint32,5>", 20, 19, 0),
        ("keen::uint8", 1, 2, 0),
        ("keen::bool", 1, 1, 0),
        ("keen::ecs::TemplateReference", 16, 17, 0),
        ("keen::ItemCategory", 1, 12, 0),
        ("keen::SkillNodeId", 4, 17, 0),
        ("keen::KnowledgeId", 4, 18, 0),
        ("keen::SkillTreeNode", 140, 18, 0),
        ("keen::SkillPointUnlock", 8, 18, 0),
        ("keen::BlobArray<keen::SkillTreeNode>", 8, 24, 0),
        ("keen::BlobArray<keen::SkillPointUnlock>", 8, 24, 0),
        ("keen::SkillTreeResource", 16, 18, 0x11112222),
        ("keen::GameKnowledgeResource", 32, 18, 0x33334444),
    ]
    for name, size, kind, type_hash in definitions:
        at = allocate(bytes(104))
        records[name] = at
        struct.pack_into("<QQ", data, at, *text(name.split("::")[-1]))
        struct.pack_into("<QQ", data, at + 32, *text(name))
        struct.pack_into("<I", data, at + 64, size)
        data[at + 76] = kind
        struct.pack_into("<I", data, at + 80, type_hash)
    struct.pack_into("<Q", data, records["keen::ItemId"] + 56,
                     address(records["keen::HashKey32"]))
    for name, inner in (
            ("keen::SkillNodeId", "keen::HashKey32"),
            ("keen::StaticArray<keen::uint32,5>", "keen::uint32"),
            ("keen::BlobArray<keen::SkillTreeNode>", "keen::SkillTreeNode"),
            ("keen::BlobArray<keen::SkillPointUnlock>", "keen::SkillPointUnlock")):
        struct.pack_into("<Q", data, records[name] + 56, address(records[inner]))
    struct.pack_into("<I", data, records["keen::StaticArray<keen::uint32,5>"] + 72, 5)

    def fields(owner, values):
        start = allocate(bytes(48 * len(values)))
        at = records[owner]
        struct.pack_into("<I", data, at + 72, len(values))
        struct.pack_into("<Q", data, at + 88, address(start))
        for i, (name, declared, offset) in enumerate(values):
            field = start + i * 48
            struct.pack_into("<QQ", data, field, *text(name))
            struct.pack_into("<QQ", data, field + 16,
                             address(records[declared]), offset)

    fields("keen::HashKey32", [("value", "keen::uint32", 0)])
    fields("keen::ItemInfo", [
        ("itemId", "keen::ItemId", 0),
        ("maxStackSize", stack_type, stack_offset),
        ("debugName", "keen::BlobString", 1936),
        ("pidEntity", "keen::ecs::TemplateReference", 1252),
        ("generateRarity", "keen::bool", 22),
        ("category", "keen::ItemCategory", 204),
    ])
    enum_start = allocate(bytes(17 * 40))
    struct.pack_into("<I", data, records["keen::ItemCategory"] + 72, 17)
    struct.pack_into("<Q", data, records["keen::ItemCategory"] + 96, address(enum_start))
    for i, label in enumerate(("Customization", "Equipment", "Weapons", "Tools", "Instrument",
                               "FishingRod", "BuildTools", "Consumables", "Ammunition",
                               "Materials", "Blueprints", "Currency", "Collectibles", "AnimalFood",
                               "PetFood", "WeaponGem", "ColorPalette")):
        struct.pack_into("<QQQ", data, enum_start + i * 40, *text(label), i)
    fields("keen::BalancingTable", [
        ("playerLevelCap", "keen::uint32", 16),
        ("skillPointsPerLevel", "keen::uint32", 180),
        ("playerLevelMax", "keen::uint32", 12),
        ("killToLevelUPStart", "keen::uint32", 116),
        ("killToLevelUPEnd", "keen::uint32", 120),
        ("xpNeededFromKnowledge", "keen::uint32", 128),
        ("xpSeedEnemy", "keen::uint32", 212),
        ("experienceNeedPerLevel", "keen::StaticArray<keen::uint32,5>", 408),
    ])
    fields("keen::KnowledgeId", [("id", "keen::HashKey32", 0)])
    fields("keen::SkillTreeNode", [
        ("id", "keen::SkillNodeId", 0), ("costs", "keen::uint16", 132),
        ("maxLevel", "keen::uint8", 136), ("levelIndex", "keen::uint8", 137)])
    fields("keen::SkillPointUnlock", [
        ("knowledgeId", "keen::KnowledgeId", 0), ("amount", "keen::uint8", 4)])
    fields("keen::SkillTreeResource", [
        ("nodes", "keen::BlobArray<keen::SkillTreeNode>", 0)])
    fields("keen::GameKnowledgeResource", [
        ("skillPoints", "keen::BlobArray<keen::SkillPointUnlock>", 24)])
    if duplicate:
        at = allocate(bytes(data[records["keen::ItemInfo"]:
                                 records["keen::ItemInfo"] + 104]))
        records["duplicate"] = at
    struct.pack_into("<IIII", data, 264 + 8,
                     len(data) - raw, rva, len(data) - raw, raw)
    return bytes(data), records


def item(item_id=123, maximum=5000, label="Synthetic material", stack_offset=20):
    data = bytearray(1944)
    struct.pack_into("<I", data, 0, item_id)
    struct.pack_into("<H", data, stack_offset, maximum)
    data[204] = 9
    encoded = label.encode("utf-8")
    struct.pack_into("<II", data, 1936, len(data) - 1936, len(encoded))
    data.extend(encoded)
    return bytes(data)


def balance(cap=45, points=2):
    data = bytearray(624)
    # This is a real scalar field, not a resource type tag.
    struct.pack_into("<I", data, 0, 10)
    struct.pack_into("<I", data, 16, cap)
    struct.pack_into("<I", data, 180, points)
    for at, value in ((12, 100), (116, 50), (120, 200), (128, 2342), (212, 50)):
        struct.pack_into("<I", data, at, value)
    struct.pack_into("<5I", data, 408, 1000, 2023, 3046, 4069, 5092)
    return bytes(data)


def progression_rules():
    return ProgressionRules(45, 2, 100, 50, 200, 2342, 50,
                            (1000, 2023, 3046, 4069, 5092))


def skill_table(nodes=((345, 3, 0, 0), (346, 2, 3, 4), (347, 1, 3, 5))):
    data = bytearray(16 + len(nodes) * 140)
    struct.pack_into("<II", data, 0, 16, len(nodes))
    for i, (key, cost, rank, group) in enumerate(nodes):
        at = 16 + i * 140
        struct.pack_into("<I", data, at, key)
        struct.pack_into("<H", data, at + 132, cost)
        data[at + 136:at + 138] = bytes((rank, group))
    return bytes(data)


def reward_table(rewards=tuple((1000 + i, 3) for i in range(19))):
    data = bytearray(32 + len(rewards) * 8)
    struct.pack_into("<II", data, 24, 8, len(rewards))
    for i, (key, amount) in enumerate(rewards):
        struct.pack_into("<IB", data, 32 + i * 8, key, amount)
    return bytes(data)


def assets(resources=None, split=None, content_size=False):
    if resources is None:
        resources = [(0x12345678, item()), (0x87654321, balance()),
                     (0x11112222, skill_table()), (0x33334444, reward_table())]
    index = bytearray(144)
    index[:4] = b"KFC3"
    payload = b"".join(value for _, value in resources)
    payloads = [payload] if split is None else [payload[:split], payload[split:]]
    compressed_parts = [
        zstd.ZstdCompressor(write_content_size=content_size).compress(value)
        for value in payloads
    ]

    def section(field, value, count):
        start = len(index)
        struct.pack_into("<II", index, field, start - field, count)
        index.extend(value)
        return start

    keys = section(96, b"".join(
        bytes(16) + struct.pack("<IIII", kind, 0, 0, 0)
        for kind, _ in resources), len(resources))
    offset, values = 0, bytearray()
    for _, value in resources:
        values.extend(struct.pack("<II", offset, len(value)))
        offset += len(value)
    value_start = section(104, values, len(resources))
    chunks, file_offset, logical = bytearray(), 0, 0
    for value, compressed in zip(payloads, compressed_parts):
        chunks.extend(struct.pack("<IIIII", file_offset, len(compressed),
                                  len(compressed), logical, len(value)))
        file_offset += len(compressed)
        logical += len(value)
    chunk_start = section(136, chunks, len(payloads))
    return bytes(index), b"".join(compressed_parts), (keys, value_start, chunk_start)


def open_rules(exe=None, index=None, resource_data=None, *, verified=True):
    if exe is None:
        exe = executable()[0]
    if index is None:
        index, resource_data, _ = assets()
    with patch("game_rules.read_limited", side_effect=[exe, index]), \
            patch("game_rules.VERIFIED_PROGRESSION_EXECUTABLES",
                  frozenset({sha256(exe).hexdigest()}) if verified else frozenset()), \
            patch.object(Path, "stat",
                         return_value=source_stat(len(resource_data))):
        return GameRules.open(Path("synthetic-game"))


def source_stat(size=1, **changes):
    values = dict(st_dev=1, st_ino=2, st_size=size, st_mtime_ns=100, st_ctime_ns=100)
    values.update(changes)
    return SimpleNamespace(**values)


class GameRulesTests(unittest.TestCase):
    def read(self, rules, data, operation):
        with patch.object(Path, "open", side_effect=lambda *a, **kw: BytesIO(data)):
            return operation(rules)

    def test_public_rules_and_immutable_records(self):
        index, data, _ = assets()
        rules = open_rules(index=index, resource_data=data)
        selected = self.read(rules, data, lambda r: r.items_for({123}))
        self.assertEqual(selected, {123: ItemRule(123, 5000, "Synthetic material", True)})
        self.assertEqual(self.read(rules, data, lambda r: r.progression()),
                         progression_rules())
        with self.assertRaises(FrozenInstanceError):
            selected[123].max_stack = 1
        with self.assertRaises(FrozenInstanceError):
            rules.progression().level_cap = 1

    def test_empty_selection_does_not_read_chunks(self):
        self.assertEqual(open_rules().items_for(set()), {})

    def test_missing_items_do_not_guess(self):
        index, data, _ = assets()
        rules = open_rules(index=index, resource_data=data)
        with self.assertRaisesRegex(SaveError, "Missing authoritative"):
            self.read(rules, data, lambda r: r.items_for({999}))
        for invalid in (-1, 2 ** 32, True, "123"):
            with self.subTest(invalid=invalid), self.assertRaises(SaveError):
                rules.items_for({invalid})

    def test_moved_field_uses_reflection_not_constant(self):
        exe, _ = executable(stack_offset=24)
        index, data, _ = assets([(0x12345678, item(stack_offset=24)),
                                  (0x87654321, balance())])
        rules = open_rules(exe, index, data)
        self.assertEqual(self.read(rules, data, lambda r: r.items_for({123}))[123].max_stack,
                         5000)

    def test_utf8_count_is_bytes_without_terminator(self):
        index, data, _ = assets([(0x12345678, item(label="Synthetic é"))])
        rules = open_rules(index=index, resource_data=data)
        self.assertEqual(self.read(rules, data, lambda r: r.items_for({123}))[123].label,
                         "Synthetic é")

    def test_cross_chunk_resource_is_bounded_and_supported(self):
        index, data, _ = assets(split=1000)
        rules = open_rules(index=index, resource_data=data)
        self.assertEqual(self.read(rules, data, lambda r: r.items_for({123}))[123].max_stack,
                         5000)

    def test_conflicting_item_rules_are_rejected(self):
        index, data, _ = assets([(0x12345678, item()),
                                  (0x12345678, item(maximum=20))])
        rules = open_rules(index=index, resource_data=data)
        with self.assertRaisesRegex(SaveError, "Conflicting"):
            self.read(rules, data, lambda r: r.items_for({123}))

    def test_identical_duplicate_items_are_safe(self):
        index, data, _ = assets([(0x12345678, item()), (0x12345678, item())])
        rules = open_rules(index=index, resource_data=data)
        self.assertEqual(len(self.read(rules, data, lambda r: r.items_for({123}))), 1)

    def test_invalid_labels_and_stack_limits(self):
        bad_string = bytearray(item())
        bad_string[-1] = 255
        bad_offset = bytearray(item())
        struct.pack_into("<I", bad_offset, 1936, 0xFFFFFFFF)
        for value in (item(maximum=0), item(label=""), item(label="bad\0name"),
                      bytes(bad_string), bytes(bad_offset), item()[:1943]):
            with self.subTest(size=len(value)):
                index, data, _ = assets([(0x12345678, value)])
                rules = open_rules(index=index, resource_data=data)
                with self.assertRaises(SaveError):
                    self.read(rules, data, lambda r: r.items_for({123}))

    def test_missing_ambiguous_and_zero_progression(self):
        for resources in ([(0x12345678, item())],
                          [(0x87654321, balance()), (0x87654321, balance())],
                          [(0x87654321, balance(cap=0))],
                          [(0x87654321, balance(points=0))]):
            index, data, _ = assets(resources)
            rules = open_rules(index=index, resource_data=data)
            with self.assertRaises(SaveError):
                self.read(rules, data, lambda r: r.progression())

    def test_invalid_pe_headers_and_sections(self):
        exe, _ = executable()
        for at, value in ((0, b"NO"), (128, b"BAD!"), (132, b"\x4c\x01"),
                          (152, b"\x0b\x01"), (264 + 20, b"\xff" * 4)):
            broken = bytearray(exe)
            broken[at:at + len(value)] = value
            with self.subTest(at=at), self.assertRaises(SaveError):
                _Pe(bytes(broken))
        with self.assertRaises(SaveError):
            _Pe(b"MZ")

    def test_missing_or_ambiguous_metadata(self):
        with self.assertRaisesRegex(SaveError, "ambiguous"):
            open_rules(executable(duplicate=True)[0])
        broken = executable()[0].replace(b"keen::ItemInfo\0", b"keen::NopeInfo\0")
        with self.assertRaises(SaveError):
            open_rules(broken)

    def test_reflection_search_work_is_bounded(self):
        exe, _ = executable()
        broken = bytearray(exe)
        broken.extend(b"keen::ItemInfo\0" * 65)
        struct.pack_into("<I", broken, 264 + 8, len(broken) - 512)
        struct.pack_into("<I", broken, 264 + 16, len(broken) - 512)
        with self.assertRaisesRegex(SaveError, "Too many candidate"):
            _Pe(bytes(broken)).find_type("keen::ItemInfo")

    def test_declared_type_size_kind_and_wrapper_are_checked(self):
        with self.assertRaisesRegex(SaveError, "declared type"):
            open_rules(executable(stack_type="keen::uint32")[0])
        exe, records = executable()
        for at, value in (
                (records["keen::ItemInfo"] + 64, struct.pack("<I", 1952)),
                (records["keen::uint16"] + 76, b"\x06"),
                (records["keen::ItemId"] + 56, bytes(8)),
                (records["keen::ItemInfo"] + 72, struct.pack("<I", 999999))):
            broken = bytearray(exe)
            broken[at:at + len(value)] = value
            with self.subTest(at=at), self.assertRaises(SaveError):
                open_rules(bytes(broken))

    def test_index_header_counts_and_ranges_are_checked(self):
        index, data, _ = assets()
        for at, value in ((0, b"KFC2"), (108, struct.pack("<I", 99)),
                          (96, bytes(4)), (104, b"\xff" * 4)):
            broken = bytearray(index)
            broken[at:at + len(value)] = value
            with self.subTest(at=at), self.assertRaises(SaveError):
                open_rules(index=bytes(broken), resource_data=data)

    def test_parts_and_chunk_gaps_are_rejected(self):
        index, data, (keys, _, chunks) = assets(split=1000)
        for at, value in ((keys + 20, 1), (chunks + 20 + 12, 1001)):
            broken = bytearray(index)
            struct.pack_into("<I", broken, at, value)
            rules = open_rules(index=bytes(broken), resource_data=data)
            with self.subTest(at=at), self.assertRaises(SaveError):
                self.read(rules, data, lambda r: r.items_for({123}))

    def test_overlapping_tables_and_chunks_are_rejected(self):
        index, data, (keys, _, chunks) = assets(split=1000)
        for at, value in ((104, keys - 104), (chunks + 20, 0),
                          (chunks + 20 + 12, 999)):
            broken = bytearray(index)
            struct.pack_into("<I", broken, at, value)
            with self.subTest(at=at), self.assertRaises(SaveError):
                open_rules(index=bytes(broken), resource_data=data)

    def test_extra_frame_data_is_rejected(self):
        index, data, (_, _, chunks) = assets()
        extra = zstd.ZstdCompressor().compress(b"extra")
        broken = bytearray(index)
        struct.pack_into("<II", broken, chunks + 4, len(data + extra), len(data + extra))
        rules = open_rules(index=bytes(broken), resource_data=data + extra)
        with self.assertRaises(SaveError):
            self.read(rules, data + extra, lambda r: r.items_for({123}))

    def test_chunk_size_and_file_bounds_are_checked(self):
        index, data, (_, _, chunks) = assets()
        for at, value in ((chunks + 16, MAX_CHUNK + 1),
                          (chunks, len(data) + 1),
                          (chunks + 8, len(data) + 1)):
            broken = bytearray(index)
            struct.pack_into("<I", broken, at, value)
            with self.subTest(at=at), self.assertRaises(SaveError):
                open_rules(index=bytes(broken), resource_data=data)

    def test_corrupt_short_and_oversized_frames_are_rejected(self):
        index, data, (_, _, chunks) = assets(content_size=True)
        rules = open_rules(index=index, resource_data=data)
        for broken in (data[:-1], bytes(len(data))):
            with self.subTest(size=len(broken)), self.assertRaises(SaveError):
                self.read(rules, broken, lambda r: r.items_for({123}))
        for delta in (-1, 1):
            broken = bytearray(index)
            current = struct.unpack_from("<I", index, chunks + 16)[0]
            struct.pack_into("<I", broken, chunks + 16, current + delta)
            rules = open_rules(index=bytes(broken), resource_data=data)
            with self.assertRaises(SaveError):
                self.read(rules, data, lambda r: r.items_for({123}))

    def test_unknown_size_frame_output_must_match_descriptor(self):
        index, data, (_, _, chunks) = assets(content_size=False)
        current = struct.unpack_from("<I", index, chunks + 16)[0]
        for delta in (-1, 1):
            broken = bytearray(index)
            struct.pack_into("<I", broken, chunks + 16, current + delta)
            rules = open_rules(index=bytes(broken), resource_data=data)
            with self.subTest(delta=delta), self.assertRaises(SaveError):
                self.read(rules, data, lambda r: r.progression())

    def test_extract_rejects_outside_or_zero_size_resource(self):
        index, data, _ = assets()
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=len(data))):
            kfc = _Kfc(index, Path("synthetic-resources"))
        for resource in (_Resource(1, 0, 0, 0),
                         _Resource(1, 0, MAX_CHUNK + 1, 0),
                         _Resource(1, kfc.chunks[-1].offset + kfc.chunks[-1].size, 1, 0)):
            with self.assertRaises(SaveError):
                kfc.extract(resource)

    def test_os_errors_are_not_hidden(self):
        with patch.object(Path, "stat", return_value=source_stat()), \
                patch("game_rules.read_limited", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError):
                GameRules.open(Path("missing-game"))

    def test_unchanged_sources_use_only_bounded_exe_index_reads(self):
        exe, _ = executable()
        index, data, _ = assets()
        rules = open_rules(exe, index, data)
        with patch.object(Path, "stat", return_value=source_stat(len(data))), \
                patch("game_rules.read_limited", side_effect=[exe, index]) as read, \
                patch.object(Path, "open", side_effect=AssertionError("resource read")):
            rules.assert_unchanged()
        self.assertEqual([call.args[0].name for call in read.call_args_list],
                         ["enshrouded.exe", "enshrouded.kfc"])
        self.assertTrue(all(call.args[1] > 0 for call in read.call_args_list))

    def test_each_source_stat_change_rejects_cached_rules(self):
        index, data, _ = assets()
        rules = open_rules(index=index, resource_data=data)
        self.read(rules, data, lambda r: r.items_for({123}))
        self.read(rules, data, lambda r: r.progression())
        for source in rules._source_paths:
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
                def stat(path):
                    baseline = source_stat(len(data))
                    if path == source:
                        setattr(baseline, field, getattr(baseline, field) + 1)
                    return baseline
                with self.subTest(source=source.name, field=field), \
                        patch.object(Path, "stat", autospec=True, side_effect=stat), \
                        patch("game_rules.read_limited",
                              side_effect=AssertionError("read after stat mismatch")), \
                        self.assertRaisesRegex(SaveError, "source changed"):
                    rules.assert_unchanged()

    def test_exe_and_index_changes_with_preserved_stats_are_rejected(self):
        exe, _ = executable()
        index, data, _ = assets()
        rules = open_rules(exe, index, data)
        for position in (0, 1):
            values = [exe, index]
            changed = bytearray(values[position])
            changed[-1] ^= 1
            values[position] = bytes(changed)
            with self.subTest(position=position), \
                    patch.object(Path, "stat", return_value=source_stat(len(data))), \
                    patch("game_rules.read_limited", side_effect=values), \
                    self.assertRaisesRegex(SaveError, "source changed"):
                rules.assert_unchanged()

    def test_source_deletion_is_save_error(self):
        rules = open_rules()
        with patch.object(Path, "stat", side_effect=FileNotFoundError("removed")), \
                self.assertRaisesRegex(SaveError, "source is missing"):
            rules.assert_unchanged()

    def test_source_deleted_between_stat_and_read_is_save_error(self):
        _, data, _ = assets()
        rules = open_rules()
        with patch.object(Path, "stat", return_value=source_stat(len(data))), \
                patch("game_rules.read_limited", side_effect=FileNotFoundError("removed")), \
                self.assertRaisesRegex(SaveError, "source is missing"):
            rules.assert_unchanged()

    def test_import_does_not_open_or_stat_game_sources(self):
        code = (
            "from pathlib import Path; import builtins;"
            "def_guard = lambda *args, **kwargs: (_ for _ in ()).throw("
            "AssertionError('unexpected source access'));"
            "Path.open = def_guard; Path.stat = def_guard; builtins.open = def_guard;"
            "import game_rules;"
            "assert list(game_rules.ProgressionRules.__dataclass_fields__) == "
            "['level_cap', 'points_per_level', 'level_max', 'kill_start', 'kill_end', "
            "'knowledge_xp', 'seed_xp', 'early_xp'];"
            "assert list(game_rules.ItemRule.__dataclass_fields__) == "
            "['item_id', 'max_stack', 'label', 'plain']"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_update_during_open_is_rejected(self):
        exe, _ = executable()
        index, data, _ = assets()
        calls = 0
        def stat(path):
            nonlocal calls
            calls += 1
            return source_stat(len(data), st_mtime_ns=100 if calls <= 4 else 101)
        with patch.object(Path, "stat", autospec=True, side_effect=stat), \
                patch("game_rules.read_limited", side_effect=[exe, index]), \
                self.assertRaisesRegex(SaveError, "source changed"):
            GameRules.open(Path("synthetic-game"))

    def test_update_during_assert_reads_is_rejected(self):
        exe, _ = executable()
        index, data, _ = assets()
        rules = open_rules(exe, index, data)
        calls = 0
        def stat(path):
            nonlocal calls
            calls += 1
            return source_stat(len(data), st_mtime_ns=100 if calls <= 3 else 101)
        with patch.object(Path, "stat", autospec=True, side_effect=stat), \
                patch("game_rules.read_limited", side_effect=[exe, index]), \
                self.assertRaisesRegex(SaveError, "source changed"):
            rules.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
