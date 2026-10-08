"""Conservative, byte-preserving access to Enshrouded character saves."""

from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import zstandard as zstd

if TYPE_CHECKING:
    from character_transfer import CharacterImport, SharedCharacter
    from game_rules import GameRules, PointBudget


MAX_FILE = 64 * 1024 * 1024
MAX_BLOB = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
KNOW = bytes.fromhex("dc8ed5f0")
RING_NAMES = ("characters", *(f"characters-{i}" for i in range(1, 10)),
              "characters-index")


class SaveError(ValueError):
    """Invalid/unsupported input or an unsafe save operation."""


def bounded(data: bytes | bytearray, offset: int, size: int) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise SaveError(f"Invalid data range: offset {offset}, size {size}.")


def u32(data: bytes | bytearray, offset: int) -> int:
    bounded(data, offset, 4)
    return struct.unpack_from("<I", data, offset)[0]


def uint32(value: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise SaveError(f"{label} must be an integer between 0 and 4294967295.")


def _crc_table() -> tuple[int, ...]:
    result = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0xC96C5795D7870F42 if value & 1 else 0)
        result.append(value)
    return tuple(result)


CRC_TABLE = _crc_table()


def crc64(data: bytes) -> int:
    value = 0xFFFFFFFFFFFFFFFF
    for byte in data:
        value = CRC_TABLE[(value ^ byte) & 255] ^ (value >> 8)
    return value ^ 0xFFFFFFFFFFFFFFFF


def fnv1a(text: str) -> int:
    value = 2166136261
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def read_limited(path: Path, limit: int = MAX_FILE) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SaveError(f"{path.name} exceeds the {limit}-byte safety limit.")
    return data


@dataclass
class Blob:
    owner: int
    tag: bytes
    compressed: bytes
    original: bytes
    data: bytes

    @property
    def label(self) -> str:
        return "KNOW" if self.tag == KNOW else (
            self.tag.decode("ascii") if all(32 <= c < 127 for c in self.tag)
            else self.tag.hex()
        )


class Bdb:
    """Only decode named object links and verified scalar storage layouts."""

    def __init__(self, data: bytes):
        self.data = data
        bounded(data, 0, 112)
        if data[:4] != b"BDB1":
            raise SaveError("Unsupported character payload: expected BDB1.")
        self.types, self.count = self.section(4, 1)
        self.values, value_count = self.section(12, 4)
        self.pool, self.pool_size = self.section(60, 1)
        self.keys, key_count = self.section(100, 8)
        self.links, link_count = self.section(108, 4)
        if self.count != value_count or key_count != link_count or not self.count:
            raise SaveError("Inconsistent BDB node/map tables.")
        ranges = [(self.types, self.count), (self.values, self.count * 4),
                  (self.pool, self.pool_size), (self.keys, key_count * 8),
                  (self.links, link_count * 4)]
        occupied = sorted((start, start + size) for start, size in ranges if size)
        if any(end > next_start for (_, end), (next_start, _) in
               zip(occupied, occupied[1:])):
            raise SaveError("Overlapping BDB sections; editing is unsafe.")
        if self.data[self.types] != 20:
            raise SaveError("Unsupported BDB root: expected object node.")
        self.edges: dict[int, dict[int, int]] = {}
        for i in range(key_count):
            key_hash, parent = struct.unpack_from("<II", data, self.keys + 8 * i)
            child = u32(data, self.links + 4 * i)
            if not 1 <= parent <= self.count or not 1 <= child <= self.count:
                raise SaveError("BDB object contains an invalid node reference.")
            if self.node(parent - 1)[0] != 20:
                raise SaveError("BDB object link refers to a non-object parent.")
            fields = self.edges.setdefault(parent - 1, {})
            if key_hash in fields:
                raise SaveError("Duplicate BDB field hash in an object.")
            fields[key_hash] = child - 1

    def section(self, field: int, width: int) -> tuple[int, int]:
        offset, count = u32(self.data, field), u32(self.data, field + 4)
        start = field + offset
        bounded(self.data, start, count * width)
        if count and start < 112:
            raise SaveError("BDB section overlaps the header.")
        return start, count

    def node(self, index: int) -> tuple[int, int]:
        if not 0 <= index < self.count:
            raise SaveError("Invalid BDB node index.")
        return self.data[self.types + index], u32(self.data, self.values + 4 * index)

    def field(self, name: str, parent: int = 0) -> int:
        fields = self.edges.get(parent, {})
        key = fnv1a(name)
        if key not in fields:
            raise SaveError(f"Unsupported BDB layout: missing {name}.")
        return fields[key]

    def string(self, index: int) -> tuple[str, int, int]:
        kind, value = self.node(index)
        if kind != 14 or not 1 <= value <= self.pool_size - 1:
            raise SaveError("Unsupported BDB string reference.")
        start = self.pool + value - 1
        size = struct.unpack_from("<H", self.data, start)[0]
        if start + 2 + size > self.pool + self.pool_size:
            raise SaveError("BDB string exceeds its pool.")
        try:
            text = self.data[start + 2:start + 2 + size].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SaveError("BDB string is not valid UTF-8.") from exc
        return text, start + 2, size

    def character(self) -> tuple[str, int]:
        name, _, _ = self.string(self.field("name"))
        kind, level = self.node(self.field("level"))
        if kind != 8:
            raise SaveError("Unsupported level storage: expected uint32 node.")
        return name, level

    def patch_character(self, name: str, level: int) -> bytes:
        uint32(level, "Level")
        if level == 0:
            raise SaveError("Level must be positive.")
        if level != self.character()[1]:
            raise SaveError(
                "Level editing is unavailable until the XP curve and saved skill-point "
                "budget are verified. Each gained level grants 2 points, but changing "
                "only the summary level would leave the character inconsistent."
            )
        if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise SaveError("Name must not be empty or contain control characters.")
        name_node = self.field("name")
        old_name, start, size = self.string(name_node)
        try:
            encoded = name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SaveError("Name is not valid UTF-8 text.") from exc
        if len(encoded) != size:
            raise SaveError(f"Name must occupy exactly {size} UTF-8 bytes; "
                            f"the new name occupies {len(encoded)}.")
        if name != old_name:
            reference = self.node(name_node)[1]
            if any(index != name_node and self.node(index) == (14, reference)
                   for index in range(self.count)):
                raise SaveError("Name shares storage with another field; rename is unsafe.")
            keys, count = self.section(68, 8)
            if any(u32(self.data, keys + i * 8) == reference for i in range(count)):
                raise SaveError("Name shares storage with an object key; rename is unsafe.")
        self.character()
        output = bytearray(self.data)
        output[start:start + size] = encoded
        struct.pack_into("<I", output, self.values + 4 * self.field("level"), level)
        return bytes(output)

    def describe_fields(self) -> list[tuple[str, str]]:
        labels: dict[int, str] = {}
        key_strings, count = self.section(68, 8)
        for i in range(count):
            reference = u32(self.data, key_strings + i * 8)
            if not 1 <= reference <= self.pool_size - 1:
                raise SaveError("Invalid BDB key string reference.")
            start = self.pool + reference - 1
            size = struct.unpack_from("<H", self.data, start)[0]
            if start + 2 + size > self.pool + self.pool_size:
                raise SaveError("Invalid BDB key string length.")
            try:
                label = self.data[start + 2:start + 2 + size].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SaveError("Invalid UTF-8 in BDB key string.") from exc
            labels[fnv1a(label)] = label
        result = []
        for parent, fields in self.edges.items():
            for key, index in fields.items():
                kind, value = self.node(index)
                label = labels.get(key, f"hash:{key:08x}")
                if kind == 14:
                    summary = repr(self.string(index)[0])
                elif kind == 8:
                    summary = f"uint32 {value}"
                elif kind == 20:
                    summary = f"object ({len(self.edges.get(index, {}))} named fields)"
                else:
                    summary = f"type {kind}, raw word 0x{value:08x} (opaque)"
                result.append((f"node {parent + 1}/{label}", summary))
        return result


class Knowledge:
    def __init__(self, data: bytes):
        self.data = data
        bounded(data, 0, 12)
        if u32(data, 0) != 2:
            raise SaveError("Unsupported KNOW version (expected 2).")
        self.count = u32(data, 8)
        if len(data) != 12 + self.count * 8:
            raise SaveError("Invalid KNOW table length.")
        self.entries: dict[int, tuple[int, int]] = {}
        for i in range(self.count):
            key = u32(data, 12 + 4 * i)
            value_offset = 12 + 4 * self.count + 4 * i
            if key in self.entries:
                raise SaveError("Duplicate KNOW IDs; editing is unsafe.")
            self.entries[key] = (u32(data, value_offset), value_offset)

    def patch(self, key: int, value: int) -> bytes:
        uint32(key, "Progression ID")
        uint32(value, "Progression value")
        if key not in self.entries:
            raise SaveError("Only existing progression IDs can be edited.")
        output = bytearray(self.data)
        struct.pack_into("<I", output, self.entries[key][1], value)
        return bytes(output)


def resolve_index(path: Path) -> tuple[Path, bytes]:
    if path.name != "characters-index":
        raise SaveError("Select characters-index to open the active save.")
    raw = read_limited(path, 4096)
    try:
        index = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SaveError("Invalid characters-index JSON.") from exc
    if not isinstance(index, dict) or type(index.get("latest")) is not int:
        raise SaveError("Index latest must be an integer from 0 to 9.")
    latest = index["latest"]
    if not 0 <= latest <= 9 or index.get("deleted", False) is not False:
        raise SaveError("Character index is deleted or has an invalid latest slot.")
    target = path.with_name("characters" if latest == 0 else f"characters-{latest}")
    return target, raw


class SaveDocument:
    def __init__(self, raw: bytes):
        if len(raw) > MAX_FILE:
            raise SaveError("Container exceeds the safety limit.")
        bounded(raw, 0, 24)
        if raw[:4] != b"KSC1":
            raise SaveError("Not an Enshrouded KSC1 save.")
        count = u32(raw, 4)
        if not 1 <= count <= 1024:
            raise SaveError("Invalid container blob count.")
        start = 24 + count * 12
        bounded(raw, 24, count * 12)
        table, payload = raw[24:start], raw[start:]
        if struct.unpack_from("<QQ", raw, 8) != (crc64(table), crc64(payload)):
            raise SaveError("Container checksum mismatch; file may be corrupt.")
        self.original = raw
        self.source: Path | None = None
        self.index_path: Path | None = None
        self.index_original: bytes | None = None
        self.catalogue_path: Path | None = None
        self.catalogue_original: bytes | None = None
        self.rule_sources: list[GameRules] = []
        self.progression_sources: dict[int, GameRules] = {}
        self.blobs: list[Blob] = []
        self.changes: dict[tuple[int, str], str] = {}
        total = 0
        seen = set()
        for i in range(count):
            owner, tag, size = struct.unpack_from("<I4sI", table, 12 * i)
            bounded(raw, start, size)
            compressed = raw[start:start + size]
            start += size
            if (owner, tag) in seen:
                raise SaveError("Duplicate owner/tag records; editing is unsafe.")
            seen.add((owner, tag))
            try:
                expected = zstd.frame_content_size(compressed)
                if expected not in (zstd.CONTENTSIZE_UNKNOWN, zstd.CONTENTSIZE_ERROR):
                    if expected > MAX_BLOB:
                        raise SaveError("Decompressed record exceeds the safety limit.")
                data = zstd.ZstdDecompressor().decompress(
                    compressed, max_output_size=MAX_BLOB, allow_extra_data=False
                )
            except zstd.ZstdError as exc:
                raise SaveError(f"Invalid Zstandard frame in record {i + 1}.") from exc
            total += len(data)
            if len(data) > MAX_BLOB or total > MAX_TOTAL:
                raise SaveError("Decompressed save exceeds the safety limit.")
            self.blobs.append(Blob(owner, tag, compressed, data, data))
        if start != len(raw):
            raise SaveError("Unexpected trailing container data.")
        self._original_records = tuple((b.owner, b.tag) for b in self.blobs)

    @classmethod
    def open(cls, path: Path) -> SaveDocument:
        path = path.absolute()
        index_path = path if path.name == "characters-index" else None
        index_raw = None
        if index_path:
            path, index_raw = resolve_index(index_path)
        result = cls(read_limited(path))
        result.source = path
        result.index_path, result.index_original = index_path, index_raw
        from steam_storage import MAX_CATALOGUE, catalogue_for
        result.catalogue_path = catalogue_for(path)
        if result.catalogue_path and result.catalogue_path.exists():
            result.catalogue_original = read_limited(result.catalogue_path, MAX_CATALOGUE)
        result.assert_unchanged()
        return result

    @property
    def owners(self) -> list[int]:
        return sorted({b.owner for b in self.blobs if b.tag in (b"CHAR", b"COUT")})

    def blob(self, owner: int, tag: bytes) -> Blob:
        for blob in self.blobs:
            if blob.owner == owner and blob.tag == tag:
                return blob
        raise SaveError(f"No {tag.hex()} record for character {owner:08x}.")

    def progression_budget(self, owner: int, rules: GameRules) -> PointBudget:
        from player_state import PlayerState
        rules.assert_unchanged()
        state = PlayerState(self.blob(owner, b"CHAR").data)
        return rules.point_budget(state.experience().level, state.skills(),
                                  Knowledge(self.blob(owner, KNOW).data))

    def edit_character(self, owner: int, name: str, level: int,
                       rules: GameRules | None = None) -> None:
        from player_state import PlayerState
        blob = self.blob(owner, b"CHAR")
        original_name, original_level = Bdb(blob.original).character()
        current_level = Bdb(blob.data).character()[1]
        uint32(level, "Level")
        new = Bdb(blob.data).patch_character(name, current_level)
        plan = None
        if level != original_level or level != current_level:
            rules = rules or self.progression_sources.get(owner)
            if rules is None:
                raise SaveError("Load installed game rules to edit levels and grant 2 points per level.")
            rules.assert_unchanged()
            progression = rules.progression()
            baseline = PlayerState(blob.original).experience()
            state = PlayerState(new)
            knowledge = Knowledge(self.blob(owner, KNOW).data)
            rules.point_budget(original_level, PlayerState(blob.original).skills(),
                               Knowledge(self.blob(owner, KNOW).original))
            plan = state.level_plan(level, progression, baseline)
            rules.point_budget(level, state.skills(), knowledge)
            new = state.patch_level(plan)
        elif level == 0:
            raise SaveError("Level must be positive.")
        blob.data = new
        if plan is not None and rules is not None:
            if rules not in self.rule_sources:
                self.rule_sources.append(rules)
            if level == original_level:
                self.progression_sources.pop(owner, None)
            else:
                self.progression_sources[owner] = rules
        for field, old, value in (("name", original_name, name),
                                  ("level", original_level, level)):
            key = (owner, field)
            if old == value:
                self.changes.pop(key, None)
            else:
                self.changes[key] = f"{owner:08x} {field}: {old!r} -> {value!r}"
        if plan is not None and level != original_level:
            xp = plan.experience
            self.changes[(owner, "level")] += (
                f"; XP grant {plan.xp_granted}, current/required {xp.current}/{xp.required}, "
                f"+{plan.points_added} level-earned skill points (bonuses/allocations preserved)")

    def edit_knowledge(self, owner: int, key: int, value: int) -> None:
        blob = self.blob(owner, KNOW)
        original = Knowledge(blob.original)
        new = Knowledge(blob.data).patch(key, value)
        if owner in self.progression_sources:
            from player_state import PlayerState
            state = PlayerState(self.blob(owner, b"CHAR").data)
            self.progression_sources[owner].point_budget(
                state.experience().level, state.skills(), Knowledge(new))
        blob.data = new
        change_key = (owner, f"KNOW {key:08x}")
        old = original.entries[key][0]
        if old == value:
            self.changes.pop(change_key, None)
        else:
            self.changes[change_key] = f"{owner:08x} KNOW {key:08x}: {old} -> {value}"

    def serialize(self) -> bytes:
        for owner, rules in self.progression_sources.items():
            self.progression_budget(owner, rules)
        if (tuple((b.owner, b.tag) for b in self.blobs) == self._original_records
                and all(b.data == b.original for b in self.blobs)):
            return self.original
        return self.encode_records(self.blobs)

    @staticmethod
    def encode_records(blobs: list[Blob] | tuple[Blob, ...]) -> bytes:
        if not 1 <= len(blobs) <= 1024:
            raise SaveError("Invalid container blob count.")
        if sum(len(b.data) for b in blobs) > MAX_TOTAL:
            raise SaveError("Decompressed save exceeds the safety limit.")
        if any(len(b.data) > MAX_BLOB for b in blobs):
            raise SaveError("Decompressed record exceeds the safety limit.")
        table, frames = [], []
        output_size = 24 + len(blobs) * 12
        compressor = zstd.ZstdCompressor(level=3)
        for blob in blobs:
            frame = (blob.compressed if blob.data == blob.original
                     else compressor.compress(blob.data))
            output_size += len(frame)
            if output_size > MAX_FILE:
                raise SaveError("Container exceeds the safety limit.")
            table.append(struct.pack("<I4sI", blob.owner, blob.tag, len(frame)))
            frames.append(frame)
        table_bytes, data_bytes = b"".join(table), b"".join(frames)
        output = (struct.pack("<4sIQQ", b"KSC1", len(blobs),
                              crc64(table_bytes), crc64(data_bytes))
                  + table_bytes + data_bytes)
        verified = SaveDocument(output)
        if any((a.owner, a.tag, a.data) != (b.owner, b.tag, b.data)
               for a, b in zip(blobs, verified.blobs)):
            raise SaveError("Edited output did not roundtrip correctly.")
        return output

    def prepare_character_import(self, character: SharedCharacter,
                                 rules: GameRules, *, name: str | None = None) -> CharacterImport:
        from character_transfer import (CharacterImport, character_identity,
                                        TAGS, allocate_identity, clone_character,
                                        matching_character_state, record_state,
                                        validate_character)
        if character.build != rules.character_transfer_build():
            raise SaveError("Shared character was exported with a different game build.")
        character = validate_character(character.archive, character.build)
        source_character = character
        reserved = {b.owner for b in self.blobs} | {character.owner}
        duplicate = any(b.owner == character.owner for b in self.blobs)
        names = set()
        incoming_records = SaveDocument(character.archive).blobs
        incoming_state = matching_character_state(incoming_records, character.owner)
        for blob in self.blobs:
            if blob.tag == b"CHAR":
                identity = character_identity(blob.data)
                if u32(identity, 0) != blob.owner:
                    raise SaveError("Existing character GUID does not match its saved owner ID.")
                if identity == character.identity:
                    duplicate = True
                existing_name = Bdb(blob.data).character()[0].strip().casefold()
                names.add(existing_name)
                if existing_name == character.name.strip().casefold():
                    duplicate = True
                tags = {b.tag for b in self.blobs if b.owner == blob.owner}
                if TAGS <= tags and matching_character_state(self.blobs, blob.owner) == incoming_state:
                    duplicate = True
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise SaveError("Choose a nonempty new character name.")
            name = name.strip()
            if (name.casefold() in names
                    or (duplicate and name.casefold() == character.name.strip().casefold())):
                raise SaveError("Choose a different name that is not already used by a saved character.")
        character = clone_character(character, allocate_identity(reserved), name)
        additions = SaveDocument(character.archive).blobs
        records = (*self.blobs, *additions)
        self.encode_records(records)
        rules.assert_unchanged()
        return CharacterImport(character, record_state(self.blobs), records, rules,
                               source_character, duplicate and name is None)

    def stage_character_import(self, prepared: CharacterImport) -> int:
        from character_transfer import record_state
        if record_state(self.blobs) != prepared.baseline:
            raise SaveError("Staged save changed while preparing the character import; retry.")
        if prepared.rename_required:
            raise SaveError("A matching character exists. Choose a new name before importing its copy.")
        prepared.rules.assert_unchanged()
        character = prepared.character
        self.blobs = list(prepared.records)
        self.changes[(character.owner, "import")] = (
            f"{character.owner:08x} Import character {character.name!r}, level "
            f"{character.level}: appearance, progression, skills, inventory, equipment "
            "and character-local quest/map data; existing characters preserved.")
        if prepared.rules not in self.rule_sources:
            self.rule_sources.append(prepared.rules)
        return character.owner

    def discard_changes(self) -> None:
        originals = set(self._original_records)
        self.blobs = [b for b in self.blobs if (b.owner, b.tag) in originals]
        for blob in self.blobs:
            blob.data = blob.original
        self.changes.clear()
        self.progression_sources.clear()
        self.rule_sources.clear()

    def edit_inventory(self, owner: int, entity: int, slot: int, quantity: int,
                       rules: GameRules) -> None:
        from player_state import PlayerState
        blob = self.blob(owner, b"CHAR")
        state = PlayerState(blob.data)
        matches = [stack for stack in state.inventory() if stack.key == (entity, slot)]
        if len(matches) != 1:
            raise SaveError("Select an existing inventory stack.")
        stack = matches[0]
        state.experience()
        rules.assert_unchanged()
        rule = rules.items_for({stack.item_id})[stack.item_id]
        new = state.patch_quantity(entity, slot, quantity,
                                   item_id=rule.item_id, max_stack=rule.max_stack)
        self._stage_inventory(owner, new, rules)

    def _stage_inventory(self, owner: int, new: bytes, rules: GameRules) -> None:
        from player_state import PlayerState, Schema
        blob = self.blob(owner, b"CHAR")
        original_state = PlayerState(blob.original)
        original = {s.key: s for s in original_state.inventory(include_empty=True)}
        state = PlayerState(new)
        current = {s.key: s for s in state.inventory(include_empty=True)}
        if original.keys() != current.keys():
            raise SaveError("Inventory slot structure changed; staging aborted.")
        changes = {}
        original_names = {d.name for d in original_state.archive.definitions.values()}
        if "PerkContainerNew" not in original_names and any(
                d.name == "PerkContainerNew" for d in state.archive.definitions.values()):
            changes[(owner, "inventory schema PerkContainerNew")] = (
                f"{owner:08x} inventory: generated missing PerkContainerNew schema "
                "from verified installed game metadata")
        for key, stack in current.items():
            old = original[key]
            if (old.item_id, old.quantity, old.item_entity) != (
                    stack.item_id, stack.quantity, stack.item_entity):
                detail = (f"item {stack.item_id:08x}: {old.quantity} -> {stack.quantity}"
                          if old.item_id == stack.item_id else
                          f"item {old.item_id:08x} x{old.quantity} -> "
                          f"item {stack.item_id:08x} x{stack.quantity}")
                if stack.item_entity and stack.item_entity != old.item_entity:
                    armor = rules.armor_for(stack.item_id)
                    saved_entity = next(e for e in state.archive.entities
                                        if e.entity_id == state.references[stack.item_entity])
                    components = {state.archive.definitions[c.component_id].name: c
                                  for c in (*saved_entity.server, *saved_entity.client)}
                    item_state = components["ItemState"]
                    schema = Schema(next(d.schema for d in state.archive.definitions.values()
                                         if d.name == "ItemState"))
                    level_at, _ = schema.field("itemLevelUi")
                    rarity_at, _ = schema.field("itemRarityUi")
                    detail += (f"; {armor.label}, level {item_state.data[level_at]}, "
                               f"{dict(armor.rarities)[item_state.data[rarity_at]]}, "
                               "0 unlocked upgrades")
                changes[(owner, f"inventory {key[0]:x}/{key[1]}")] = (
                    f"{owner:08x} inventory {key[0]:x} slot {key[1] + 1}: "
                    f"{detail}")
        blob.data = new
        for key in list(self.changes):
            if key[0] == owner and key[1].startswith("inventory "):
                del self.changes[key]
        self.changes.update(changes)
        if rules not in self.rule_sources:
            self.rule_sources.append(rules)

    def merge_inventory(self, owner: int, items: list[tuple[int, int]],
                        rules: GameRules) -> None:
        from player_state import PlayerState
        rules.assert_unchanged()
        catalogue = rules.addable_items()
        state = PlayerState(self.blob(owner, b"CHAR").data)
        state.experience()
        slots = [s for s in state.inventory(include_empty=True) if s.accessible and not s.item_entity]
        planned = {s.key: (s.item_id, s.quantity) for s in slots}
        for item_id, quantity in items:
            uint32(item_id, "Item ID")
            uint32(quantity, "Quantity")
            rule = catalogue.get(item_id)
            if not quantity or rule is None or not rule.plain:
                raise SaveError("Select a verified plain item with a positive quantity.")
            remaining = quantity
            for slot in slots:
                key = slot.key
                saved_id, saved_quantity = planned[key]
                if saved_id == item_id:
                    if saved_quantity > rule.max_stack:
                        raise SaveError("Existing stack exceeds its installed limit.")
                    amount = min(remaining, rule.max_stack - saved_quantity)
                    planned[key] = (item_id, saved_quantity + amount)
                    remaining -= amount
            for slot in slots:
                if not remaining:
                    break
                if planned[slot.key] == (0, 0):
                    amount = min(remaining, rule.max_stack)
                    planned[slot.key] = (item_id, amount)
                    remaining -= amount
            if remaining:
                raise SaveError("Not enough accessible empty inventory slots. Nothing was staged.")
        updates = {s.key: planned[s.key] for s in slots
                   if planned[s.key] != (s.item_id, s.quantity)}
        new = state.patch_inventory_slots(updates)
        self._stage_inventory(owner, new, rules)

    def add_armor(self, owner: int, item_id: int, level: int, rarity: int,
                  rules: GameRules, quantity: int = 1) -> None:
        from armor_editing import add_armor
        from player_state import PlayerState
        rules.assert_unchanged()
        rule = rules.armor_for(item_id)
        state = PlayerState(self.blob(owner, b"CHAR").data)
        definition = None
        if not any(d.name == "PerkContainerNew" for d in state.archive.definitions.values()):
            definition = rules.perk_component_definition()
        new = add_armor(state, rule, level, rarity, quantity, perk_definition=definition)
        self._stage_inventory(owner, new, rules)

    def remove_inventory(self, owner: int, entity: int, slot: int,
                         rules: GameRules) -> None:
        from player_state import PlayerState
        rules.assert_unchanged()
        state = PlayerState(self.blob(owner, b"CHAR").data)
        state.experience()
        matches = [s for s in state.inventory() if s.key == (entity, slot)]
        if len(matches) != 1 or not matches[0].accessible or matches[0].item_entity:
            raise SaveError("Only accessible plain Generic stacks can be removed.")
        self._stage_inventory(owner, state.patch_inventory_slots({(entity, slot): (0, 0)}), rules)

    def inventory_export(self, owner: int, rules: GameRules) -> list[dict]:
        from player_state import PlayerState
        rules.assert_unchanged()
        rules.addable_items()
        state = PlayerState(self.blob(owner, b"CHAR").data)
        state.experience()
        stacks = [s for s in state.inventory() if s.accessible and not s.item_entity]
        catalogue = rules.items_for({s.item_id for s in stacks})
        items = []
        for stack in stacks:
            rule = catalogue.get(stack.item_id)
            if rule is None:
                raise SaveError("Editable inventory contains an unsupported item/quantity; "
                                "export aborted rather than omit it.")
            if not rule.plain:
                continue
            if stack.quantity > rule.max_stack:
                raise SaveError("Inventory stack exceeds its installed limit; export aborted.")
            items.append({"item_id": stack.item_id, "name": rule.label, "quantity": stack.quantity})
        return items

    def assert_unchanged(self) -> None:
        if self.source is None:
            raise SaveError("This document has no source file.")
        if read_limited(self.source) != self.original:
            raise SaveError("Source save changed. Reopen it before saving.")
        if self.index_path:
            target, raw = resolve_index(self.index_path)
            if raw != self.index_original or target != self.source:
                raise SaveError("Active index changed. Reopen it before saving.")
        if self.catalogue_path and self.catalogue_original is not None:
            from steam_storage import MAX_CATALOGUE
            if read_limited(self.catalogue_path, MAX_CATALOGUE) != self.catalogue_original:
                raise SaveError("Steam catalogue changed. Reopen it before saving.")
        for rules in self.rule_sources:
            rules.assert_unchanged()

    def assert_save_provider(self, *, cloud_disabled: bool, cloud_enabled: bool) -> None:
        if type(cloud_disabled) is not bool or type(cloud_enabled) is not bool:
            raise SaveError("Save-provider confirmations must be boolean.")
        if (cloud_disabled is True) == (cloud_enabled is True):
            raise SaveError("Confirm exactly one save provider: Cloud enabled or disabled.")
        if cloud_disabled is True:
            self.assert_cloud_off_target()
            return
        if self.catalogue_path is None or self.source is None or self.index_original is None:
            raise SaveError("Cloud-enabled overwrite requires a registered Steam active profile.")
        from steam_storage import validate_registration
        validate_registration(self.catalogue_original, self.source.name, self.original)
        validate_registration(self.catalogue_original, "characters-index", self.index_original)
        self.assert_unchanged()

    def export(self, destination: Path) -> Path:
        if not self.changes:
            raise SaveError("No pending changes to export.")
        self.assert_unchanged()
        destination = destination.absolute()
        if self.source is None:
            raise SaveError("Missing source path.")
        if (destination.parent.resolve() == self.source.parent.resolve()
                or destination.name in RING_NAMES):
            raise SaveError("Export outside the save folder under a new, non-ring name.")
        backup = destination.with_name(destination.name + ".original.bak")
        if destination.exists() or backup.exists():
            raise SaveError("Export or backup already exists. Choose a new filename.")
        output = self.serialize()
        created = []
        try:
            for path, data in ((backup, self.original), (destination, output)):
                with path.open("xb") as stream:
                    created.append(path)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            for path in created:
                path.unlink()
            raise
        return backup

    def assert_cloud_off_target(self) -> None:
        if self.source is None:
            raise SaveError("Missing source path.")
        folder = self.source.parent.resolve()
        if folder.name.lower() == "remote" and folder.parent.name == "1203620":
            raise SaveError(
                "This is a Steam Cloud save folder. Disabling Steam Cloud switches "
                "Enshrouded to a separate profile in Saved Games\\Enshrouded; "
                "it does not use this file. Direct overwrite is blocked to avoid "
                "editing the wrong profile. Export is still available. Back up and "
                "migrate your saves explicitly before opening the local "
                "characters-index for Cloud-disabled editing."
            )

    def apply_active(self, backup_directory: Path, *, cloud_disabled: bool = False,
                     cloud_enabled: bool = False) -> Path:
        if self.index_path is None or self.source is None:
            raise SaveError("Direct apply requires opening characters-index.")
        if not self.changes:
            raise SaveError("No pending changes to apply.")
        self.assert_save_provider(cloud_disabled=cloud_disabled, cloud_enabled=cloud_enabled)
        assert_game_closed()
        self.assert_unchanged()
        backup_directory = backup_directory.absolute()
        save_directory = self.source.parent.resolve()
        if backup_directory.resolve().is_relative_to(save_directory):
            raise SaveError("The backup directory must be outside the save folder.")
        if backup_directory.exists():
            raise SaveError("Backup directory already exists. Choose a new directory.")
        output = self.serialize()
        originals = {name: read_limited(self.source.parent / name)
                     for name in RING_NAMES if (self.source.parent / name).exists()}
        if (originals.get(self.source.name) != self.original
                or originals.get("characters-index") != self.index_original):
            raise SaveError("Save history changed while preparing the backup.")
        history_names = set(originals)
        if cloud_enabled is True:
            if self.catalogue_original is None:
                raise SaveError("Missing Steam catalogue; active save not changed.")
            originals["remotecache.vdf"] = self.catalogue_original
        backup_directory.mkdir()
        for name, data in originals.items():
            with (backup_directory / name).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for name, data in originals.items():
            if read_limited(backup_directory / name) != data:
                raise SaveError("Backup verification failed; active save not changed.")
        fd, temporary = tempfile.mkstemp(prefix=".character-editor-", suffix=".tmp",
                                         dir=self.source.parent)
        temp_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            assert_game_closed()
            self.assert_unchanged()
            current_names = {n for n in RING_NAMES if (self.source.parent / n).exists()}
            if current_names != history_names:
                raise SaveError("Save history changed; active save not changed.")
            for name, data in originals.items():
                path = self.catalogue_path if name == "remotecache.vdf" else self.source.parent / name
                if path is None or read_limited(path) != data:
                    raise SaveError("Save history changed; active save not changed.")
            os.replace(temp_path, self.source)
            if read_limited(self.source) != output:
                raise SaveError("Saved-file verification failed. Restore the backup at "
                                f"{backup_directory} before starting the game.")
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return backup_directory


BLOCKING_PROCESSES = frozenset({"steam", "enshrouded", "enshrouded_server"})


def assert_game_closed() -> None:
    if os.name != "nt":
        raise SaveError("Direct apply process checks are supported only on Windows.")
    command = ("$ErrorActionPreference='Stop'; "
               "Get-Process | Select-Object -ExpandProperty ProcessName")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True, capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SaveError("Cannot verify running processes. Active save not changed.") from exc
    names = {line.strip() for line in result.stdout.lower().splitlines() if line.strip()}
    if not names:
        raise SaveError("Process enumeration returned no data; active save not changed.")
    # Exact names only: a prefix match would also block this editor's own executable.
    blocked = sorted(names & BLOCKING_PROCESSES)
    if blocked:
        raise SaveError("Close Steam and Enshrouded before applying: " + ", ".join(blocked))


def steam_roots() -> list[Path]:
    roots = []
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Valve\Steam") as key:
                value, _ = winreg.QueryValueEx(key, "SteamPath")
                if not isinstance(value, str) or not value:
                    raise SaveError("Steam registry install path is invalid.")
                roots.append(Path(value))
        except FileNotFoundError:
            pass  # Steam may not be installed for the current Windows user.
    return roots


def discover_saves() -> list[Path]:
    candidates = [Path.home() / "Saved Games" / "Enshrouded" / "characters-index"]
    for root in steam_roots():
        userdata = root / "userdata"
        if userdata.is_dir():
            candidates.extend(userdata.glob(r"*\1203620\remote\characters-index"))
    return sorted({p.absolute() for p in candidates if p.is_file()})
