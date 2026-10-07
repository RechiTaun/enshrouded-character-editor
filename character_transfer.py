"""Bounded, lossless sharing of one complete initialized character."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import struct
from typing import TYPE_CHECKING
from uuid import uuid4

from player_state import PlayerState
from save_format import (Bdb, Blob, KNOW, Knowledge, MAX_BLOB, MAX_FILE, RING_NAMES,
                         SaveDocument, SaveError, read_limited, u32)

if TYPE_CHECKING:
    from game_rules import GameRules


EXTENSION = ".enshrouded-character"
MAGIC = b"ENCHAR\x00\x01"
HEADER = struct.Struct("<8sI32s32s")
MAX_SHARE = HEADER.size + MAX_FILE
TAGS = frozenset((b"CHAR", b"COUT", KNOW, b"FOWR"))


@dataclass(frozen=True)
class SharedCharacter:
    archive: bytes
    build: bytes
    owner: int
    identity: bytes
    name: str
    level: int


@dataclass(frozen=True)
class CharacterImport:
    character: SharedCharacter
    baseline: tuple[tuple[int, bytes, bytes], ...]
    records: tuple[Blob, ...]
    rules: GameRules
    source_character: SharedCharacter
    rename_required: bool


def record_state(records: list[Blob] | tuple[Blob, ...]) -> tuple[tuple[int, bytes, bytes], ...]:
    return tuple((b.owner, b.tag, b.data) for b in records)


def character_identity(data: bytes) -> bytes:
    db = Bdb(data)
    kind, reference = db.node(db.field("id"))
    start, count = db.section(36, 16)
    if kind != 15 or not 1 <= reference <= count:
        raise SaveError("Unsupported character identity: expected a saved GUID.")
    occupied = [(db.types, db.count), (db.values, db.count * 4),
                (db.pool, db.pool_size)]
    for field, width in ((28, 8), (76, 4), (100, 8), (108, 4)):
        at, items = db.section(field, width)
        occupied.append((at, items * width))
    if any(size and at < start + count * 16 and start < at + size
           for at, size in occupied):
        raise SaveError("Character GUID table overlaps other BDB storage.")
    identity = data[start + 16 * (reference - 1):start + 16 * reference]
    if not u32(identity, 0):
        raise SaveError("Character identity has a reserved zero owner ID.")
    return identity


def matching_character_state(records: list[Blob] | tuple[Blob, ...], owner: int) -> bytes:
    selected = {b.tag: b for b in records if b.owner == owner}
    if not TAGS <= selected.keys():
        raise SaveError("Matching character state requires all four character records.")
    raw = selected[b"CHAR"].data
    digest = sha256(struct.pack("<I", Bdb(raw).character()[1]))
    digest.update(PlayerState.read_saved_data(raw, allow_empty=True)[1])
    for tag in (b"COUT", KNOW, b"FOWR"):
        digest.update(sha256(selected[tag].data).digest())
    return digest.digest()


def allocate_identity(reserved_owners: set[int]) -> bytes:
    for _ in range(128):
        identity = uuid4().bytes_le
        owner = u32(identity, 0)
        if owner and owner not in reserved_owners:
            return identity
    raise SaveError("Could not allocate a unique character identity; retry the import.")


def _unshared_node(db: Bdb, node: int, label: str) -> None:
    children, child_count = db.section(76, 4)
    if sum(child == node for fields in db.edges.values()
           for child in fields.values()) != 1 or any(
            u32(db.data, children + 4 * i) == node + 1 for i in range(child_count)):
        raise SaveError(f"Character {label} shares a BDB node; cloning is unsupported.")


def clone_character(character: SharedCharacter, identity: bytes,
                    name: str | None = None) -> SharedCharacter:
    source = SaveDocument(character.archive)
    original = source.blob(character.owner, b"CHAR").data
    db = Bdb(original)
    id_node = db.field("id")
    _unshared_node(db, id_node, "identity")
    reference = db.node(id_node)[1]
    if any(i != id_node and db.node(i) == (15, reference) for i in range(db.count)):
        raise SaveError("Character GUID storage is shared; cloning is unsupported.")
    if (original.count(character.identity) != 1
            or any(b.tag != b"CHAR" and character.identity in b.data for b in source.blobs)
            or character.identity in PlayerState.read_saved_data(original)[1]):
        raise SaveError("Additional character identity references require verified rebinding.")
    if len(identity) != 16 or not u32(identity, 0) or identity == character.identity:
        raise SaveError("A cloned character requires a fresh nonzero GUID.")
    output = bytearray(original)
    id_at = db.section(36, 16)[0] + 16 * (reference - 1)
    output[id_at:id_at + 16] = identity
    if name is not None and name != character.name:
        if not isinstance(name, str) or not name.strip() or any(
                ord(c) < 32 or ord(c) == 127 for c in name):
            raise SaveError("Name must not be empty or contain control characters.")
        try:
            encoded = name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SaveError("Name is not valid UTF-8 text.") from exc
        if len(encoded) > 65535:
            raise SaveError("Name exceeds the saved UTF-8 string limit.")
        name_node = db.field("name")
        _unshared_node(db, name_node, "name")
        size = db.pool_size + 2 + len(encoded)
        if len(output) + size > MAX_BLOB:
            raise SaveError("Renamed character exceeds the record safety limit.")
        pool_at = len(output)
        output.extend(original[db.pool:db.pool + db.pool_size])
        output.extend(struct.pack("<H", len(encoded)) + encoded)
        struct.pack_into("<II", output, 60, pool_at - 60, size)
        struct.pack_into("<I", output, db.values + 4 * name_node, db.pool_size + 1)
    owner = u32(identity, 0)
    records = [Blob(owner, b.tag, b.compressed, b.original,
                    bytes(output) if b.tag == b"CHAR" else b.data) for b in source.blobs]
    result = validate_character(SaveDocument.encode_records(records), character.build)
    if PlayerState.read_saved_data(bytes(output))[1] != PlayerState.read_saved_data(original)[1]:
        raise SaveError("Character cloning changed saved entity data.")
    return result


def validate_character(archive: bytes, build: bytes) -> SharedCharacter:
    document = SaveDocument(archive)
    if len(document.blobs) != len(TAGS) or {b.tag for b in document.blobs} != TAGS:
        raise SaveError("Sharing requires exactly CHAR, COUT, KNOW and FOWR records.")
    owners = {b.owner for b in document.blobs}
    if len(owners) != 1 or not next(iter(owners)):
        raise SaveError("A share must contain exactly one nonzero character owner.")
    owner = next(iter(owners))
    raw = document.blob(owner, b"CHAR").data
    identity = character_identity(raw)
    if u32(identity, 0) != owner:
        raise SaveError("Character GUID does not match its saved owner ID.")
    db = Bdb(raw)
    if db.node(db.field("version")) != (8, 8):
        raise SaveError("Unsupported shared character version.")
    if db.node(db.field("cloudId")) != (15, 0):
        raise SaveError("Non-null character cloudId cannot be shared without verified rebinding.")
    name, level = db.character()
    if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise SaveError("Shared character name is empty or contains control characters.")
    state = PlayerState(raw)
    if state.experience().level != level:
        raise SaveError("Shared character summary and actor levels disagree.")
    state.skills()
    state.inventory()
    Knowledge(document.blob(owner, KNOW).data)
    appearance = Bdb(document.blob(owner, b"COUT").data)
    if appearance.node(appearance.field("version")) not in ((8, 1), (8, 2)):
        raise SaveError("Unsupported shared appearance version.")
    if any(appearance.node(appearance.field(field))[0] != 20 for field in ("items", "setup")):
        raise SaveError("Unsupported shared appearance layout.")
    fog = document.blob(owner, b"FOWR").data
    if len(fog) < 5 or fog[:4] != b"FGOW":
        raise SaveError("Unsupported shared exploration record.")
    if not isinstance(build, bytes) or len(build) != 32:
        raise SaveError("Invalid character-sharing build identifier.")
    return SharedCharacter(archive, build, owner, identity, name, level)


def export_character(document: SaveDocument, owner: int, rules: GameRules) -> bytes:
    build = rules.character_transfer_build()
    archive = SaveDocument.encode_records([b for b in document.blobs if b.owner == owner])
    shared = validate_character(archive, build)
    rules.assert_unchanged()
    return HEADER.pack(MAGIC, len(archive), shared.build, sha256(archive).digest()) + archive


def parse_character(raw: bytes, rules: GameRules) -> SharedCharacter:
    if not HEADER.size <= len(raw) <= MAX_SHARE:
        raise SaveError("Shared character file is truncated or exceeds the safety limit.")
    magic, size, build, checksum = HEADER.unpack_from(raw)
    if magic != MAGIC:
        raise SaveError("Unsupported shared character format/version.")
    if size > MAX_FILE or len(raw) != HEADER.size + size:
        raise SaveError("Invalid shared character payload size.")
    if build != rules.character_transfer_build():
        raise SaveError("Shared character was exported with a different game build.")
    archive = raw[HEADER.size:]
    if sha256(archive).digest() != checksum:
        raise SaveError("Shared character checksum mismatch.")
    result = validate_character(archive, build)
    rules.assert_unchanged()
    return result


def read_character(path: Path, rules: GameRules) -> SharedCharacter:
    return parse_character(read_limited(path, MAX_SHARE), rules)


def write_character(path: Path, raw: bytes, document: SaveDocument,
                    rules: GameRules) -> None:
    parse_character(raw, rules)
    document.assert_unchanged()
    path = path.absolute()
    if document.source is None:
        raise SaveError("Missing source path.")
    if (path.parent.resolve().is_relative_to(document.source.parent.resolve())
            or path.name in (*RING_NAMES, "remotecache.vdf")
            or path.suffix.lower() != EXTENSION):
        raise SaveError("Export outside the save folder using an .enshrouded-character filename.")
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        if created:
            path.unlink()
        raise
