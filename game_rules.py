"""Read authoritative rules from local PE reflection and KFC3 assets.

Nothing is executed or written. Resource objects are tagged by the index, not
by an extra type-hash word: their reflected fields start at byte zero.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import struct
import uuid
from typing import TYPE_CHECKING

import zstandard as zstd

from save_format import SaveError, bounded, read_limited, steam_roots, u32, uint32

if TYPE_CHECKING:
    from entity_format import ComponentDefinition
    from player_state import Skill
    from save_format import Knowledge


MAX_EXE = 128 * 1024 * 1024
MAX_INDEX = 32 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
MAX_FIELDS = 1024
VERIFIED_PROGRESSION_EXECUTABLES = frozenset({
    "af2f5a1227911d8aa06b3908d6bd0211838211cae14ea91099cb57d0df990781",
})


def discover_game_directories() -> list[Path]:
    """Read Steam's library/manifests; never scan drives or launch the game."""
    from steam_storage import MAX_CATALOGUE, parse_catalogue

    libraries = set()
    for root in steam_roots():
        libraries.add(root.absolute())
        index = root / "steamapps" / "libraryfolders.vdf"
        if not index.is_file():
            continue
        document = parse_catalogue(read_limited(index, MAX_CATALOGUE))
        entries = document.get("libraryfolders")
        if not isinstance(entries, dict):
            raise SaveError("Steam library index has no libraryfolders object.")
        for key, entry in entries.items():
            if not key.isdecimal():
                continue
            location = entry.get("path") if isinstance(entry, dict) else entry
            if not isinstance(location, str) or not location or not Path(location).is_absolute():
                raise SaveError("Steam library path is invalid.")
            libraries.add(Path(location).absolute())
    found = set()
    for library in sorted(libraries):
        manifest = library / "steamapps" / "appmanifest_1203620.acf"
        if not manifest.is_file():
            continue
        document = parse_catalogue(read_limited(manifest, MAX_CATALOGUE))
        app = document.get("appstate")
        if not isinstance(app, dict) or app.get("appid") != "1203620":
            raise SaveError("Enshrouded Steam manifest has an invalid app ID.")
        directory = app.get("installdir")
        if not isinstance(directory, str) or directory in ("", ".", "..") or (
                any(c in directory for c in '\\/:') or Path(directory).is_absolute()):
            raise SaveError("Enshrouded Steam manifest has an invalid install directory.")
        game = library / "steamapps" / "common" / directory
        if not all((game / name).is_file() for name in (
                "enshrouded.exe", "enshrouded.kfc", "enshrouded.kfc_resources")):
            raise SaveError("Steam's Enshrouded installation is incomplete. "
                            "Use Load installed game rules to select a valid folder.")
        found.add(game.resolve())
    return sorted(found)


@dataclass(frozen=True)
class ItemRule:
    item_id: int
    max_stack: int
    label: str
    plain: bool = False


@dataclass(frozen=True)
class ArmorRule:
    item_id: int
    label: str
    template_guid: bytes
    min_level: int
    max_level: int
    rarities: tuple[tuple[int, str], ...]
    default_rarity: int
    level_definition: bytes
    level_root_id: int
    level_signature: int
    perk_ids: tuple[int, ...]


_LEVEL_DEFINITION = uuid.UUID("bbf879a8-9af2-4516-830d-e201d7370f02").bytes_le
_LEVEL_SIGNATURE = 0x57B5E058


@dataclass(frozen=True)
class ProgressionRules:
    level_cap: int
    points_per_level: int
    level_max: int
    kill_start: int
    kill_end: int
    knowledge_xp: int
    seed_xp: int
    early_xp: tuple[int, ...]

    def required_xp(self, level: int) -> int:
        uint32(level, "Level")
        if not 1 <= level <= self.level_cap:
            raise SaveError(f"Level must be between 1 and {self.level_cap}.")
        if level <= len(self.early_xp):
            result = self.early_xp[level - 1]
        else:
            def f32(value: float) -> float:
                return struct.unpack("<f", struct.pack("<f", value))[0]

            ratio = f32(f32(level) / f32(self.level_max))
            kills = f32(f32(min(self.kill_start, self.kill_end))
                        + f32(f32(abs(self.kill_end - self.kill_start)) * ratio))
            base = f32(kills * f32(self.seed_xp))
            value = f32(f32(base + f32(base * 0.5)) + f32(self.knowledge_xp))
            result = int(value + 0.5)
        uint32(result, "Required XP")
        if not result:
            raise SaveError("Installed XP rules produced a zero threshold.")
        return result


@dataclass(frozen=True)
class SkillRule:
    node_id: int
    cost: int
    max_rank: int
    level_index: int


@dataclass(frozen=True)
class PointBudget:
    level_points: int
    bonus_points: int
    spent: int

    @property
    def earned(self) -> int:
        return self.level_points + self.bonus_points

    @property
    def available(self) -> int:
        return max(self.earned - self.spent, 0)


def _unpack(fmt: str, data: bytes, offset: int) -> tuple:
    bounded(data, offset, struct.calcsize(fmt))
    return struct.unpack_from(fmt, data, offset)


@dataclass(frozen=True)
class _Type:
    at: int
    name: str
    size: int
    kind: int
    type_hash: int
    fields: int
    count: int
    inner: int


class _Pe:
    """Resolve exact qualified metadata names through section-mapped pointers."""

    def __init__(self, data: bytes):
        self.data = data
        bounded(data, 0, 64)
        if data[:2] != b"MZ":
            raise SaveError("Expected a Windows PE executable.")
        nt = u32(data, 60)
        bounded(data, nt, 24)
        if data[nt:nt + 4] != b"PE\0\0":
            raise SaveError("Invalid PE signature.")
        machine, count = _unpack("<HH", data, nt + 4)
        optional = _unpack("<H", data, nt + 20)[0]
        if machine != 0x8664 or not 1 <= count <= 96 or optional < 112:
            raise SaveError("Unsupported PE64 layout.")
        bounded(data, nt + 24, optional + count * 40)
        if _unpack("<H", data, nt + 24)[0] != 0x20B:
            raise SaveError("Expected PE64 optional header.")
        self.base = _unpack("<Q", data, nt + 48)[0]
        self.sections: list[tuple[int, int, int]] = []
        for i in range(count):
            at = nt + 24 + optional + 40 * i
            _, rva, size, start = _unpack("<IIII", data, at + 8)
            if not size:
                continue
            bounded(data, start, size)
            for old_start, old_size, old_rva in self.sections:
                if (start < old_start + old_size and old_start < start + size
                        or rva < old_rva + old_size and old_rva < rva + size):
                    raise SaveError("Overlapping PE sections.")
            self.sections.append((start, size, rva))

    def offset(self, pointer: int, size: int = 1) -> int:
        for start, length, rva in self.sections:
            relative = pointer - self.base - rva
            if 0 <= relative and relative + size <= length:
                return start + relative
        raise SaveError("Reflection pointer is outside a file-backed PE section.")

    def address(self, offset: int) -> int:
        for start, size, rva in self.sections:
            if start <= offset < start + size:
                return self.base + rva + offset - start
        raise SaveError("Reflection string is outside a PE section.")

    def string(self, at: int) -> str:
        pointer, length = _unpack("<QQ", self.data, at)
        if not 0 < length <= 512:
            raise SaveError("Unsupported reflection string length.")
        start = self.offset(pointer, length)
        try:
            return self.data[start:start + length].decode("ascii")
        except UnicodeDecodeError as exc:
            raise SaveError("Invalid reflection string encoding.") from exc

    def type_at(self, at: int) -> _Type:
        bounded(self.data, at, 104)
        self.offset(self.address(at), 104)
        name = self.string(at + 32)
        count = u32(self.data, at + 72)
        if count > MAX_FIELDS:
            raise SaveError("Too many reflected fields.")
        fields_pointer = _unpack("<Q", self.data, at + 88)[0]
        fields = (self.offset(fields_pointer, count * 48)
                  if count and self.data[at + 76] == 18 else 0)
        return _Type(at, name, u32(self.data, at + 64),
                     self.data[at + 76], u32(self.data, at + 80),
                     fields, count, _unpack("<Q", self.data, at + 56)[0])

    def find_type(self, name: str) -> _Type:
        needle = name.encode("ascii") + b"\0"
        found: dict[int, _Type] = {}
        start, strings, references = 0, 0, 0
        while (string_at := self.data.find(needle, start)) != -1:
            start = string_at + len(needle)
            strings += 1
            if strings > 64:
                raise SaveError("Too many candidate reflection strings.")
            try:
                pointer = struct.pack("<Q", self.address(string_at))
            except SaveError:
                continue
            ref_start = 0
            while (ref := self.data.find(pointer, ref_start)) != -1:
                ref_start = ref + 1
                references += 1
                if references > 4096:
                    raise SaveError("Too many candidate reflection references.")
                try:
                    candidate = self.type_at(ref - 32)
                    if candidate.name == name:
                        found[candidate.at] = candidate
                except SaveError:
                    # Most pointer occurrences are not TypeMetadata records.
                    continue
        if len(found) != 1:
            raise SaveError(f"Missing or ambiguous reflection metadata for {name}.")
        return next(iter(found.values()))

    def field(self, owner: _Type, name: str, expected: str,
              size: int, kind: int) -> int:
        matches = []
        for i in range(owner.count):
            at = owner.fields + 48 * i
            if self.string(at) != name:
                continue
            pointer = _unpack("<Q", self.data, at + 16)[0]
            declared = self.type_at(self.offset(pointer, 104))
            offset = _unpack("<Q", self.data, at + 24)[0]
            if (declared.name, declared.size, declared.kind) != (expected, size, kind):
                raise SaveError(f"Unsupported declared type for {owner.name}.{name}.")
            if offset + size > owner.size:
                raise SaveError(f"Field {name} exceeds its reflected object.")
            matches.append(offset)
        if len(matches) != 1:
            raise SaveError(f"Missing or duplicate field {owner.name}.{name}.")
        return matches[0]

    def enum_values(self, owner: _Type) -> dict[int, str]:
        if owner.kind != 12 or not 1 <= owner.count <= 256:
            raise SaveError("Unsupported reflected enum layout.")
        pointer = _unpack("<Q", self.data, owner.at + 96)[0]
        start = self.offset(pointer, owner.count * 40)
        result = {}
        for i in range(owner.count):
            at = start + i * 40
            value = _unpack("<Q", self.data, at + 16)[0]
            label = self.string(at)
            if value in result or label in result.values():
                raise SaveError("Duplicate reflected enum entry.")
            result[value] = label
        return result


@dataclass(frozen=True)
class _Resource:
    type_hash: int
    offset: int
    size: int
    part: int
    guid: bytes = b""


@dataclass(frozen=True)
class _Chunk:
    file_offset: int
    stored_size: int
    compressed_size: int
    offset: int
    size: int


class _Kfc:
    def __init__(self, data: bytes, path: Path):
        bounded(data, 0, 144)
        if data[:4] != b"KFC3":
            raise SaveError("Unsupported resource index: expected KFC3.")
        keys, count = self.section(data, 96, 32)
        values, value_count = self.section(data, 104, 8)
        chunks, chunk_count = self.section(data, 136, 20)
        if count != value_count or not count or not chunk_count:
            raise SaveError("Inconsistent KFC3 index tables.")
        ranges = sorted(((keys, count * 32), (values, value_count * 8),
                         (chunks, chunk_count * 20)))
        if any(start + size > next_start
               for (start, size), (next_start, _) in zip(ranges, ranges[1:])):
            raise SaveError("Overlapping KFC3 index tables.")
        self.path = path
        file_size = path.stat().st_size
        self.resources = [
            _Resource(u32(data, keys + i * 32 + 16),
                      u32(data, values + i * 8), u32(data, values + i * 8 + 4),
                      u32(data, keys + i * 32 + 20),
                      data[keys + i * 32:keys + i * 32 + 16])
            for i in range(count)
        ]
        self.chunks = [
            _Chunk(*_unpack("<IIIII", data, chunks + i * 20))
            for i in range(chunk_count)
        ]
        self.chunks.sort(key=lambda c: c.offset)
        end = 0
        for chunk in self.chunks:
            if (not 0 < chunk.size <= MAX_CHUNK
                    or not 0 < chunk.compressed_size <= chunk.stored_size <= MAX_CHUNK
                    or chunk.file_offset + chunk.stored_size > file_size
                    or chunk.offset < end):
                raise SaveError("Unsupported KFC3 chunk bounds or flags.")
            end = chunk.offset + chunk.size
        physical = sorted(self.chunks, key=lambda c: c.file_offset)
        if any(c.file_offset + c.stored_size > following.file_offset
               for c, following in zip(physical, physical[1:])):
            raise SaveError("Overlapping KFC3 physical chunks.")
        self.starts = [c.offset for c in self.chunks]
        self.cached: tuple[_Chunk, bytes] | None = None

    @staticmethod
    def section(data: bytes, field: int, width: int) -> tuple[int, int]:
        relative, count = _unpack("<II", data, field)
        start = field + relative
        bounded(data, start, count * width)
        if start < 144:
            raise SaveError("KFC3 table overlaps its header.")
        return start, count

    def extract(self, resource: _Resource) -> bytes:
        if resource.part != 0:
            raise SaveError("Unsupported KFC3 resource part.")
        if not 0 < resource.size <= MAX_CHUNK:
            raise SaveError("Unsupported resource size.")
        i = bisect_right(self.starts, resource.offset) - 1
        if i < 0:
            raise SaveError("Resource has no containing chunk.")
        remaining, position = resource.size, resource.offset
        parts = []
        while remaining:
            if i >= len(self.chunks):
                raise SaveError("Resource extends beyond the chunk table.")
            chunk = self.chunks[i]
            relative = position - chunk.offset
            if not 0 <= relative < chunk.size:
                raise SaveError("Resource crosses a gap in the chunk table.")
            size = min(remaining, chunk.size - relative)
            parts.append(self.read_chunk(chunk)[relative:relative + size])
            position += size
            remaining -= size
            i += 1
        return b"".join(parts)

    def read_chunk(self, chunk: _Chunk) -> bytes:
        if self.cached is None or self.cached[0] != chunk:
            with self.path.open("rb") as stream:
                stream.seek(chunk.file_offset)
                compressed = stream.read(chunk.compressed_size)
            if len(compressed) != chunk.compressed_size:
                raise SaveError("Truncated resource chunk.")
            try:
                # Reject excessive declared output before allocating it. Frames
                # without content size still use the descriptor's strict limit.
                params = zstd.get_frame_parameters(compressed)
                if (params.content_size != zstd.CONTENTSIZE_UNKNOWN
                        and params.content_size != chunk.size):
                    raise SaveError("Zstandard frame size disagrees with chunk.")
                if params.window_size > MAX_CHUNK:
                    raise SaveError("Unsupported Zstandard window size.")
                output = zstd.ZstdDecompressor(
                    max_window_size=MAX_CHUNK).decompress(
                        compressed, max_output_size=chunk.size,
                        allow_extra_data=False)
            except zstd.ZstdError as exc:
                raise SaveError("Invalid compressed resource chunk.") from exc
            if len(output) != chunk.size:
                raise SaveError("Decompressed resource chunk size mismatch.")
            self.cached = (chunk, output)
        return self.cached[1]


class GameRules:
    """Local rules for the reflected ItemInfo/BalancingTable object layouts."""

    @classmethod
    def open(cls, directory: Path) -> GameRules:
        directory = directory.absolute()
        paths = tuple(directory / name for name in
                      ("enshrouded.exe", "enshrouded.kfc", "enshrouded.kfc_resources"))
        fingerprints = tuple(cls._fingerprint(path) for path in paths)
        exe_data = cls._read_source(paths[0], MAX_EXE)
        pe = _Pe(exe_data)
        items = pe.find_type("keen::ItemInfo")
        balance = pe.find_type("keen::BalancingTable")
        if (items.kind, items.size, balance.kind, balance.size) != (18, 1944, 18, 624):
            raise SaveError("Unsupported game rule object layout.")
        if (not items.type_hash or not balance.type_hash
                or items.type_hash == balance.type_hash):
            raise SaveError("Unsupported rule type hashes.")
        result = cls()
        result._item_type = items
        result._balance_type = balance
        result._id = pe.field(items, "itemId", "keen::ItemId", 4, 17)
        item_id = pe.find_type("keen::ItemId")
        underlying = pe.type_at(pe.offset(item_id.inner, 104))
        if (underlying.name, underlying.kind, underlying.size) != (
                "keen::HashKey32", 18, 4):
            raise SaveError("Unsupported ItemId representation.")
        if pe.field(underlying, "value", "keen::uint32", 4, 6) != 0:
            raise SaveError("Unsupported ItemId value offset.")
        result._stack = pe.field(items, "maxStackSize", "keen::uint16", 2, 4)
        result._label = pe.field(items, "debugName", "keen::BlobString", 8, 25)
        result._pid = pe.field(items, "pidEntity", "keen::ecs::TemplateReference", 16, 17)
        result._rarity = pe.field(items, "generateRarity", "keen::bool", 1, 1)
        result._category = pe.field(items, "category", "keen::ItemCategory", 1, 12)
        result._categories = pe.enum_values(pe.find_type("keen::ItemCategory"))
        result._cap = pe.field(balance, "playerLevelCap", "keen::uint32", 4, 6)
        result._points = pe.field(balance, "skillPointsPerLevel", "keen::uint32", 4, 6)
        result._xp_fields = {
            name: pe.field(balance, name, "keen::uint32", 4, 6)
            for name in ("playerLevelMax", "killToLevelUPStart", "killToLevelUPEnd",
                         "xpNeededFromKnowledge", "xpSeedEnemy")
        }
        result._early_xp = pe.field(
            balance, "experienceNeedPerLevel", "keen::StaticArray<keen::uint32,5>", 20, 19)
        early_type = pe.find_type("keen::StaticArray<keen::uint32,5>")
        early_inner = pe.type_at(pe.offset(early_type.inner, 104))
        if early_type.count != 5 or (early_inner.name, early_inner.size,
                                     early_inner.kind) != ("keen::uint32", 4, 6):
            raise SaveError("Unsupported early-level XP array.")
        result._pe = pe
        index_data = cls._read_source(paths[1], MAX_INDEX)
        result._kfc = _Kfc(index_data, paths[2])
        result._items: dict[int, ItemRule] | None = None
        result._progression: ProgressionRules | None = None
        result._skill_rules: dict[int, SkillRule] | None = None
        result._bonus_rewards: dict[int, int] | None = None
        result._source_paths = paths
        result._source_fingerprints = fingerprints
        result._source_hashes = (sha256(exe_data).digest(), sha256(index_data).digest())
        result._progression_verified = (
            result._source_hashes[0].hex() in VERIFIED_PROGRESSION_EXECUTABLES)
        result._assert_source_stats()
        return result

    @staticmethod
    def _read_source(path: Path, limit: int) -> bytes:
        try:
            return read_limited(path, limit)
        except FileNotFoundError as exc:
            raise SaveError(f"Game rule source is missing: {path.name}.") from exc

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, ...]:
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise SaveError(f"Game rule source is missing: {path.name}.") from exc
        return (stat.st_dev, stat.st_ino, stat.st_size,
                stat.st_mtime_ns, stat.st_ctime_ns)

    def _assert_source_stats(self) -> None:
        for path, expected in zip(self._source_paths, self._source_fingerprints):
            if self._fingerprint(path) != expected:
                raise SaveError(f"Game rule source changed: {path.name}; reopen game rules.")

    def assert_unchanged(self) -> None:
        """Reject updates since open, including when decoded rules are cached.

        Executable/index contents are hashed with bounded reads. The potentially
        huge resources file uses identity, size and nanosecond stat timestamps;
        it is never read in full. Call immediately before staging/overwriting.
        This detects ordinary updates, not adversarial timestamp/identity spoofing.
        """
        self._assert_source_stats()
        for path, limit, expected in zip(self._source_paths[:2],
                                         (MAX_EXE, MAX_INDEX), self._source_hashes):
            if sha256(self._read_source(path, limit)).digest() != expected:
                raise SaveError(f"Game rule source changed: {path.name}; reopen game rules.")
        self._assert_source_stats()

    def character_transfer_build(self) -> bytes:
        if not getattr(self, "_progression_verified", False):
            raise SaveError("Character sharing requires a verified game executable.")
        self.assert_unchanged()
        return self._source_hashes[0]

    def item_catalogue(self) -> dict[int, ItemRule]:
        if self._items is None:
            rules: dict[int, ItemRule] = {}
            resources = sorted(
                (r for r in self._kfc.resources
                 if r.type_hash == self._item_type.type_hash),
                key=lambda r: r.offset)
            if not resources:
                raise SaveError("No ItemInfo resources found.")
            for resource in resources:
                data = self._kfc.extract(resource)
                bounded(data, 0, self._item_type.size)
                item_id = u32(data, self._id)
                maximum = _unpack("<H", data, self._stack)[0]
                relative, count = _unpack("<II", data, self._label)
                start = self._label + relative
                if not maximum or not 0 < count <= 4096 or start < self._item_type.size:
                    raise SaveError("Unsupported ItemInfo limit or debug-name layout.")
                bounded(data, start, count)
                try:
                    label = data[start:start + count].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SaveError("Invalid ItemInfo debug-name encoding.") from exc
                if any(ord(c) < 32 or ord(c) == 127 for c in label):
                    raise SaveError("Unsupported ItemInfo debug-name characters.")
                rarity = data[self._rarity]
                if rarity not in (0, 1):
                    raise SaveError("Unsupported item rarity-generation flag.")
                category = self._categories.get(data[self._category])
                if category is None:
                    raise SaveError("Unknown installed item category.")
                plain = (rarity == 0 and data[self._pid:self._pid + 16] == bytes(16)
                         and category in {"Consumables", "Ammunition", "Materials", "Blueprints",
                                          "Collectibles", "AnimalFood", "PetFood", "ColorPalette"})
                rule = ItemRule(item_id, maximum, label, plain)
                if item_id in rules and rules[item_id] != rule:
                    raise SaveError(f"Conflicting ItemInfo rules for item {item_id}.")
                rules[item_id] = rule
            self._items = rules
            self._kfc.cached = None
        return dict(self._items)

    def items_for(self, item_ids: set[int]) -> dict[int, ItemRule]:
        for item_id in item_ids:
            uint32(item_id, "Item ID")
        if not item_ids:
            return {}
        catalogue = self.item_catalogue()
        missing = item_ids - catalogue.keys()
        if missing:
            raise SaveError("Missing authoritative item rules for IDs: "
                            + ", ".join(map(str, sorted(missing))))
        return {item_id: catalogue[item_id] for item_id in item_ids}

    def addable_items(self) -> dict[int, ItemRule]:
        if not self._progression_verified:
            raise SaveError("Item creation is unavailable for an unverified game executable.")
        return {key: rule for key, rule in self.item_catalogue().items() if rule.plain}

    def _armor_layout(self) -> tuple[dict[str, int], dict[int, str], dict[int, str]]:
        """Load extra reflection lazily: missing armor metadata never blocks plain items."""
        pe = getattr(self, "_pe", None)
        if pe is None:
            raise SaveError("Armor creation requires installed armor reflection metadata.")
        types: dict[str, _Type] = {}

        def declared(name: str, size: int, kind: int) -> _Type:
            if name not in types:
                types[name] = pe.find_type(name)
            result = types[name]
            if (result.size, result.kind) != (size, kind):
                raise SaveError(f"Unsupported armor metadata type {name}.")
            return result

        def inner(name: str, size: int, kind: int,
                  child: str, child_size: int, child_kind: int) -> _Type:
            owner = declared(name, size, kind)
            value = pe.type_at(pe.offset(owner.inner, 104))
            expected = declared(child, child_size, child_kind)
            if value.at != expected.at:
                raise SaveError(f"Unsupported underlying armor type {name}.")
            return owner

        def field(owner: str, owner_size: int, name: str, child: str,
                  size: int, kind: int, offset: int) -> None:
            if pe.field(declared(owner, owner_size, 18), name, child, size, kind) != offset:
                raise SaveError(f"Unsupported armor field offset {owner}.{name}.")

        inner("keen::ItemRarity", 1, 12, "keen::uint8", 1, 2)
        inner("keen::EquipmentSlotType", 1, 12, "keen::uint8", 1, 2)
        inner("keen::ecs::TemplateFamily", 1, 12, "keen::uint8", 1, 2)
        inner("keen::ItemRarityMask", 1, 17, "keen::Bitmask8<keen::ItemRarity>", 1, 13)
        inner("keen::Bitmask8<keen::ItemRarity>", 1, 13, "keen::ItemRarity", 1, 12)
        inner("keen::ecs::TemplateReference", 16, 17,
              "keen::ObjectReference<keen::ecs::Template>", 16, 28)
        inner("keen::ObjectReference<keen::ecs::Template>", 16, 28,
              "keen::ecs::Template", 6, 18)
        inner("keen::PerkReference", 16, 17, "keen::ObjectReference<keen::Perk>", 16, 28)
        inner("keen::ObjectReference<keen::Perk>", 16, 28, "keen::Perk", 204, 18)
        inner("keen::PerkId", 4, 17, "keen::HashKey32", 4, 18)
        field("keen::HashKey32", 4, "value", "keen::uint32", 4, 6, 0)
        for name, child, size, child_size, child_kind in (
                ("keen::StaticArray<keen::PerkReference,5>", "keen::PerkReference", 80, 16, 17),
                ("keen::StaticArray<keen::PerkId,5>", "keen::PerkId", 20, 4, 17)):
            if inner(name, size, 19, child, child_size, child_kind).count != 5:
                raise SaveError("Unsupported armor perk array length.")
        offsets = {}
        for name, child, size, kind, offset in (
                ("rarity", "keen::ItemRarity", 1, 12, 23),
                ("disableRarityGeneration", "keen::ItemRarityMask", 1, 17, 24),
                ("equipment", "keen::EquipmentSetup", 984, 18, 260),
                ("pidEntity", "keen::ecs::TemplateReference", 16, 17, 1252),
                ("perkReferences", "keen::StaticArray<keen::PerkReference,5>", 80, 19, 1436),
                ("perkIds", "keen::StaticArray<keen::PerkId,5>", 20, 19, 1516),
                ("itemLevelRange", "keen::ItemLevelRange", 8, 18, 1536),
                ("damageSetup", "keen::ItemDamageSetup", 64, 18, 1544),
                ("armorSetup", "keen::ItemArmorSetup", 40, 18, 1608)):
            field("keen::ItemInfo", 1944, name, child, size, kind, offset)
            offsets[name] = offset
        for owner, size, name, child, width, kind, offset in (
                ("keen::EquipmentSetup", 984, "slot", "keen::EquipmentSlotType", 1, 12, 0),
                ("keen::ItemLevelRange", 8, "minLevel", "keen::uint32", 4, 6, 0),
                ("keen::ItemLevelRange", 8, "maxLevel", "keen::uint32", 4, 6, 4),
                ("keen::ItemDamageSetup", 64, "isSet", "keen::bool", 1, 1, 60),
                ("keen::ItemArmorSetup", 40, "isSet", "keen::bool", 1, 1, 36),
                ("keen::ItemArmorSetup", 40, "distribution",
                 "keen::impact::ArmorDistribution", 36, 18, 0)):
            field(owner, size, name, child, width, kind, offset)
        for i, name in enumerate(("physical", "blunt", "pierce", "cut", "magical",
                                  "fire", "ice", "fog", "lightning")):
            field("keen::impact::ArmorDistribution", 36, name, "keen::float32", 4, 10, i * 4)
        inner("keen::string", 8, 17, "keen::BlobString", 8, 25)
        for name, child, size, kind in (
                ("keen::BlobArray<keen::BlobVariant<keen::ecs::Component>>",
                 "keen::BlobVariant<keen::ecs::Component>", 12, 27),
                ("keen::BlobArray<keen::HashKey32>", "keen::HashKey32", 4, 18),
                ("keen::BlobArray<keen::AttributeStructure>", "keen::AttributeStructure", 16, 18),
                ("keen::BlobArray<keen::string>", "keen::string", 8, 17),
                ("keen::BlobArray<keen::AttributeCommand>", "keen::AttributeCommand", 4, 17)):
            inner(name, 8, 24, child, size, kind)
        inner("keen::BlobVariant<keen::ecs::Component>", 12, 27, "keen::ecs::Component", 1, 18)
        inner("keen::AttributeCommand", 4, 17, "keen::uint32", 4, 6)
        inner("keen::impact::AttributeIndex", 2, 17, "keen::uint16", 2, 4)
        for owner, size, name, child, width, kind, offset in (
                ("keen::ecs::TemplateResource", 20, "name", "keen::string", 8, 17, 0),
                ("keen::ecs::TemplateResource", 20, "family", "keen::ecs::TemplateFamily", 1, 12, 8),
                ("keen::ecs::TemplateResource", 20, "predictEntity", "keen::bool", 1, 1, 9),
                ("keen::ecs::TemplateResource", 20, "farCulling", "keen::bool", 1, 1, 10),
                ("keen::ecs::TemplateResource", 20, "omitTransformReplication", "keen::bool", 1, 1, 11),
                ("keen::ecs::TemplateResource", 20, "components",
                 "keen::BlobArray<keen::BlobVariant<keen::ecs::Component>>", 8, 24, 12),
                ("keen::BaseAttributeResource", 28, "type", "keen::HashKey32", 4, 18, 0),
                ("keen::BaseAttributeResource", 28, "ids", "keen::BlobArray<keen::HashKey32>", 8, 24, 4),
                ("keen::BaseAttributeResource", 28, "structure",
                 "keen::BlobArray<keen::AttributeStructure>", 8, 24, 12),
                ("keen::BaseAttributeResource", 28, "debugNames", "keen::BlobArray<keen::string>", 8, 24, 20),
                ("keen::AttributeStructure", 16, "parentIndex", "keen::impact::AttributeIndex", 2, 17, 0),
                ("keen::AttributeStructure", 16, "childIndex", "keen::impact::AttributeIndex", 2, 17, 2),
                ("keen::AttributeStructure", 16, "siblingIndex", "keen::impact::AttributeIndex", 2, 17, 4),
                ("keen::AttributeStructure", 16, "calculation",
                 "keen::BlobArray<keen::AttributeCommand>", 8, 24, 8)):
            field(owner, size, name, child, width, kind, offset)
        static = declared("keen::ecs::StaticTransform", 1, 18)
        if static.type_hash != 0x5458E4A3 or static.count:
            raise SaveError("Unsupported StaticTransform template component.")
        rarities = pe.enum_values(types["keen::ItemRarity"])
        if rarities != dict(enumerate(("Common", "Uncommon", "Rare", "Epic", "Legendary"))):
            raise SaveError("Unsupported installed armor rarity enum.")
        slots = pe.enum_values(types["keen::EquipmentSlotType"])
        for value, name in enumerate(("Armour_Head", "Armour_UpperBody", "Armour_Arms",
                                      "Armour_LowerBody", "Armour_Feet"), 13):
            if slots.get(value) != name:
                raise SaveError("Unsupported installed armor equipment slot enum.")
        return offsets, rarities, slots

    @staticmethod
    def _armor_array(data: bytes, at: int, width: int, floor: int,
                     limit: int = 4096) -> tuple[int, int]:
        relative, count = _unpack("<II", data, at)
        start = at + relative
        if count > limit or (count and start < floor):
            raise SaveError("Unsupported armor resource array bounds.")
        bounded(data, start, count * width)
        return start, count

    @classmethod
    def _armor_text(cls, data: bytes, at: int, floor: int) -> str:
        start, count = cls._armor_array(data, at, 1, floor)
        if not count:
            raise SaveError("Empty armor resource name.")
        try:
            text = data[start:start + count].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SaveError("Invalid armor resource name encoding.") from exc
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise SaveError("Invalid armor resource name characters.")
        return text

    def _armor_guid_resources(self) -> dict[bytes, list[_Resource]]:
        resources: dict[bytes, list[_Resource]] = {}
        for resource in self._kfc.resources:
            if resource.part == 0 and len(resource.guid) == 16:
                resources.setdefault(resource.guid, []).append(resource)
        return resources

    def _armor_resource(self, guid: bytes, type_name: str,
                        resources: dict[bytes, list[_Resource]]) -> bytes:
        found = resources.get(guid, [])
        owner = self._pe.find_type(type_name)
        if len(found) != 1:
            raise SaveError(f"Missing or ambiguous armor {type_name} GUID resource.")
        if not owner.type_hash or found[0].type_hash != owner.type_hash:
            raise SaveError(f"Armor GUID does not resolve to {type_name}.")
        data = self._kfc.extract(found[0])
        bounded(data, 0, owner.size)
        return data

    def _armor_level_root(self, resources: dict[bytes, list[_Resource]]) -> int:
        root = 2166136261
        for byte in _LEVEL_DEFINITION:
            root = ((root ^ byte) * 16777619) & 0xFFFFFFFF
        if root != 0x724BF568:
            raise SaveError("Unsupported armor Level root GUID hash.")
        data = self._armor_resource(_LEVEL_DEFINITION, "keen::BaseAttributeResource", resources)
        ids, count = self._armor_array(data, 4, 4, 28, 2)
        structure, size = self._armor_array(data, 12, 16, 28, 2)
        names, name_count = self._armor_array(data, 20, 8, 28, 2)
        if count != 2 or size != 2 or name_count != 2 or (
                _unpack("<2I", data, ids) != (root, 0x638A979A)):
            raise SaveError("Unsupported armor Level root IDs or resource shape.")
        ranges = sorted(((ids, 8), (structure, 32), (names, 16)))
        if any(start + length > following
               for (start, length), (following, _) in zip(ranges, ranges[1:])):
            raise SaveError("Overlapping armor Level root arrays.")
        for i, expected in enumerate(((65535, 1, 65535), (0, 65535, 65535))):
            at = structure + i * 16
            if _unpack("<3H", data, at) != expected:
                raise SaveError("Unsupported armor Level root structure indices.")
            self._armor_array(data, at + 8, 4, structure + size * 16)
            if self._armor_text(data, names + i * 8, names + name_count * 8) != ("Level", "Level_Max")[i]:
                raise SaveError("Unsupported armor Level root debug names.")
        return root

    def _armor_template(self, guid: bytes, resources: dict[bytes, list[_Resource]]) -> bool:
        data = self._armor_resource(guid, "keen::ecs::TemplateResource", resources)
        name = self._armor_text(data, 0, 20)
        start, count = self._armor_array(data, 12, 12, 20, 644)
        if any(value not in (0, 1) for value in data[9:12]):
            raise SaveError("Invalid armor template boolean flags.")
        if name != "Base_Pide" or count != 1 or data[8:12] != bytes(4):
            return False
        type_hash, relative, size = _unpack("<III", data, start)
        target = start + 4 + relative
        if target < start + 12:
            raise SaveError("Armor template component overlaps its variant.")
        bounded(data, target, size)
        return type_hash == 0x5458E4A3 and size == 1 and data[target] == 0

    def armor_catalogue(self) -> dict[int, ArmorRule]:
        """Only verified fresh armor; masks, sparse perks and custom templates are excluded."""
        if not getattr(self, "_progression_verified", False):
            raise SaveError("Armor creation is unavailable for an unverified game executable.")
        if any(not hasattr(self, name) for name in (
                "_pe", "_kfc", "_source_paths", "_source_fingerprints", "_source_hashes")):
            raise SaveError("Armor creation requires installed armor reflection and source metadata.")
        self.assert_unchanged()
        if getattr(self, "_armor_rules", None) is None:
            offsets, rarities, slots = self._armor_layout()
            resources = self._armor_guid_resources()
            root = self._armor_level_root(resources)
            items = self.item_catalogue()
            rules: dict[int, ArmorRule] = {}
            templates: dict[bytes, bool] = {}
            definitions: dict[int, bytes] = {}
            item_resources = sorted(
                (r for r in self._kfc.resources if r.type_hash == self._item_type.type_hash),
                key=lambda r: r.offset)
            for resource in item_resources:
                data = self._kfc.extract(resource)
                bounded(data, 0, self._item_type.size)
                item_id = u32(data, self._id)
                item = items[item_id]
                if self._categories.get(data[self._category]) != "Equipment" or item.max_stack != 1:
                    continue
                fingerprint = sha256(data).digest()
                if item_id in definitions and definitions[item_id] != fingerprint:
                    raise SaveError(f"Conflicting installed armor definitions for item {item_id}.")
                definitions[item_id] = fingerprint
                damage, armor = data[offsets["damageSetup"] + 60], data[offsets["armorSetup"] + 36]
                if damage not in (0, 1) or armor not in (0, 1):
                    raise SaveError("Invalid armor/damage setup flags.")
                if damage or not armor or data[offsets["disableRarityGeneration"]]:
                    continue
                slot = data[offsets["equipment"]]
                if slot not in slots:
                    raise SaveError("Unknown installed armor equipment slot.")
                if not 13 <= slot <= 17:
                    continue
                minimum, maximum = _unpack("<II", data, offsets["itemLevelRange"])
                if not 1 <= minimum <= maximum <= 255:
                    continue
                perks = _unpack("<5I", data, offsets["perkIds"])
                references = offsets["perkReferences"]
                if not all(perks) or any(data[references + i * 16:references + (i + 1) * 16]
                                         == bytes(16) for i in range(5)):
                    continue
                rarity = data[offsets["rarity"]]
                if rarity not in rarities:
                    raise SaveError("Unknown installed armor default rarity.")
                guid = data[offsets["pidEntity"]:offsets["pidEntity"] + 16]
                if guid == bytes(16):
                    continue
                if guid not in templates:
                    templates[guid] = self._armor_template(guid, resources)
                if not templates[guid]:
                    continue
                rule = ArmorRule(item_id, item.label, guid, minimum, maximum,
                                 tuple(rarities.items()) if data[self._rarity] else ((rarity, rarities[rarity]),),
                                 rarity, _LEVEL_DEFINITION, root, _LEVEL_SIGNATURE, perks)
                if item_id in rules and rules[item_id] != rule:
                    raise SaveError(f"Conflicting installed armor rules for item {item_id}.")
                rules[item_id] = rule
            self.assert_unchanged()
            self._armor_rules = rules
            self._kfc.cached = None
        return dict(self._armor_rules)

    def armor_for(self, item_id: int) -> ArmorRule:
        uint32(item_id, "Item ID")
        try:
            return self.armor_catalogue()[item_id]
        except KeyError as exc:
            raise SaveError(f"Item {item_id} is not supported for verified armor creation.") from exc

    def perk_component_definition(self) -> ComponentDefinition:
        """Generate lazily; existing saved schemas never need this metadata."""
        from component_schema import generate_perk_schema
        if not getattr(self, "_progression_verified", False):
            raise SaveError("Perk schema generation requires a verified game executable.")
        if not hasattr(self, "_pe"):
            raise SaveError("Perk schema generation requires installed reflection metadata.")
        self.assert_unchanged()
        cached = getattr(self, "_generated_perk_schema", None)
        if cached is None:
            cached = generate_perk_schema(self._pe)
            self.assert_unchanged()
            self._generated_perk_schema = cached
        return cached

    def progression(self) -> ProgressionRules:
        if self._progression is None:
            resources = [r for r in self._kfc.resources
                         if r.type_hash == self._balance_type.type_hash]
            if len(resources) != 1:
                raise SaveError("Missing or ambiguous BalancingTable resource.")
            data = self._kfc.extract(resources[0])
            bounded(data, 0, self._balance_type.size)
            cap, points = u32(data, self._cap), u32(data, self._points)
            values = {name: u32(data, offset) for name, offset in self._xp_fields.items()}
            maximum = values["playerLevelMax"]
            if not 1 <= cap <= maximum <= 1000 or points != 2:
                raise SaveError("Unsupported progression rules: expected 2 points per level "
                                "and a valid playable cap/actor maximum.")
            rules = ProgressionRules(
                cap, points, maximum, values["killToLevelUPStart"],
                values["killToLevelUPEnd"], values["xpNeededFromKnowledge"],
                values["xpSeedEnemy"], _unpack("<5I", data, self._early_xp))
            for level in range(1, cap + 1):
                rules.required_xp(level)
            self._progression = rules
        return self._progression

    def _table(self, owner_name: str, field: str, element_name: str,
               element_size: int, limit: int) -> tuple[bytes, int, int, _Type]:
        owner = self._pe.find_type(owner_name)
        element = self._pe.find_type(element_name)
        array_name = f"keen::BlobArray<{element_name}>"
        at = self._pe.field(owner, field, array_name, 8, 24)
        array = self._pe.find_type(array_name)
        inner = self._pe.type_at(self._pe.offset(array.inner, 104))
        if owner.kind != 18 or element.kind != 18 or element.size != element_size or (
                inner.at != element.at):
            raise SaveError(f"Unsupported {owner_name}.{field} layout.")
        resources = [r for r in self._kfc.resources if r.type_hash == owner.type_hash]
        if not owner.type_hash or len(resources) != 1:
            raise SaveError(f"Missing or ambiguous {owner_name} resource.")
        data = self._kfc.extract(resources[0])
        bounded(data, 0, owner.size)
        relative, count = _unpack("<II", data, at)
        start = at + relative
        if not 1 <= count <= limit or start < owner.size:
            raise SaveError(f"Unsupported {owner_name}.{field} array bounds.")
        bounded(data, start, count * element_size)
        return data, start, count, element

    def _point_tables(self) -> tuple[dict[int, SkillRule], dict[int, int]]:
        if self._skill_rules is None or self._bonus_rewards is None:
            pe = self._pe
            node_id = pe.find_type("keen::SkillNodeId")
            inner = pe.type_at(pe.offset(node_id.inner, 104))
            if inner.name != "keen::HashKey32" or pe.field(
                    inner, "value", "keen::uint32", 4, 6) != 0:
                raise SaveError("Unsupported SkillNodeId representation.")
            data, start, count, node = self._table(
                "keen::SkillTreeResource", "nodes", "keen::SkillTreeNode", 140, 256)
            id_at = pe.field(node, "id", "keen::SkillNodeId", 4, 17)
            cost_at = pe.field(node, "costs", "keen::uint16", 2, 4)
            rank_at = pe.field(node, "maxLevel", "keen::uint8", 1, 2)
            group_at = pe.field(node, "levelIndex", "keen::uint8", 1, 2)
            skills: dict[int, SkillRule] = {}
            groups = set()
            for i in range(count):
                at = start + i * node.size
                key, cost = u32(data, at + id_at), _unpack("<H", data, at + cost_at)[0]
                rank, group = data[at + rank_at], data[at + group_at]
                if not key or key in skills or rank not in (0, 3) or (
                        rank and (group >= 100 or group in groups)):
                    raise SaveError("Unsupported or duplicate installed skill/rank rule.")
                if rank:
                    groups.add(group)
                skills[key] = SkillRule(key, cost, rank, group)
            data, start, count, reward = self._table(
                "keen::GameKnowledgeResource", "skillPoints", "keen::SkillPointUnlock", 8, 65536)
            id_at = pe.field(reward, "knowledgeId", "keen::KnowledgeId", 4, 18)
            amount_at = pe.field(reward, "amount", "keen::uint8", 1, 2)
            knowledge_id = pe.find_type("keen::KnowledgeId")
            if pe.field(knowledge_id, "id", "keen::HashKey32", 4, 18) != 0:
                raise SaveError("Unsupported KnowledgeId representation.")
            rewards: dict[int, int] = {}
            for i in range(count):
                at = start + i * reward.size
                key, amount = u32(data, at + id_at), data[at + amount_at]
                if not key or key in rewards or amount not in (1, 3):
                    raise SaveError("Unsupported or duplicate installed bonus reward.")
                rewards[key] = amount
            self._skill_rules, self._bonus_rewards = skills, rewards
        return self._skill_rules, self._bonus_rewards

    def point_budget(self, level: int, skills: list[Skill],
                     knowledge: Knowledge) -> PointBudget:
        if not self._progression_verified:
            raise SaveError("This executable's native progression rules are not verified. "
                            "Level editing is read-only; inventory rules can still be used.")
        progression = self.progression()
        progression.required_xp(level)
        definitions, rewards = self._point_tables()
        bonus = sum(amount for key, amount in rewards.items()
                    if key in knowledge.entries and knowledge.entries[key][0] != 0)
        spent, seen = 0, set()
        for skill in skills:
            if not skill.node_id:
                if skill.impact_entity or skill.unlock_level:
                    raise SaveError("Orphaned skill state; level editing is unsafe.")
                continue
            if skill.node_id in seen or skill.node_id not in definitions:
                raise SaveError("Unknown or duplicate saved skill node; level editing is unsafe.")
            seen.add(skill.node_id)
            node = definitions[skill.node_id]
            invested = skill.unlock_level >> 5
            if node.max_rank:
                if invested > node.max_rank:
                    raise SaveError("Saved invested skill rank exceeds its installed maximum.")
                spent += node.cost * invested
            else:
                spent += node.cost
        budget = PointBudget((level - 1) * progression.points_per_level, bonus, spent)
        if spent > budget.earned:
            raise SaveError("Allocated skill costs exceed the earned point budget.")
        return budget
