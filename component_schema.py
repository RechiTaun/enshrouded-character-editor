"""Generate only the verified armor perk schema from local PE reflection."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from entity_format import ComponentDefinition
from player_state import Schema
from save_format import SaveError, u32

if TYPE_CHECKING:
    from game_rules import _Pe, _Type


def _namespaces(pe: _Pe, owner: _Type) -> tuple[str, ...]:
    pointer, = struct.unpack_from("<Q", pe.data, owner.at + 48)
    parts, seen = [], set()
    while pointer:
        if pointer in seen or len(parts) >= 8:
            raise SaveError("Unsupported perk schema namespace ancestry.")
        seen.add(pointer)
        at = pe.offset(pointer, 24)
        parts.append(pe.string(at))
        pointer, = struct.unpack_from("<Q", pe.data, at + 16)
    return tuple(reversed(parts))


def generate_perk_schema(pe: _Pe) -> ComponentDefinition:
    root = pe.find_type("keen::ecs::PerkContainerNew")
    base = pe.find_type("keen::ecs::Component")
    scalar = pe.find_type("keen::uint8")
    expected = (
        (root, "PerkContainerNew", "ecs.PerkContainerNew", ("keen", "ecs"), 2, 2, 18,
         0xE288D50C),
        (base, "Component", "ecs.Component", ("keen", "ecs"), 1, 0, 18, 0xB4B5CB62),
        (scalar, "uint8", "uint8", ("keen",), 1, 0, 2, 0xD297A9C6),
    )
    for owner, name, serialized, namespaces, size, count, kind, type_hash in expected:
        if (pe.string(owner.at), pe.string(owner.at + 16), _namespaces(pe, owner),
                owner.size, owner.count, owner.kind, owner.type_hash) != (
                    name, serialized, namespaces, size, count, kind, type_hash):
            raise SaveError(f"Unsupported installed perk schema type: {name}.")
        if struct.unpack_from("<HH", pe.data, owner.at + 68) != (1, 1):
            raise SaveError(f"Unsupported installed perk schema alignment: {name}.")
        if not u32(pe.data, owner.at + 84):
            raise SaveError(f"Missing installed perk schema hash: {name}.")
    if root.inner != pe.address(base.at) or base.inner or scalar.inner:
        raise SaveError("Unsupported installed perk schema inheritance.")
    fields = []
    for i in range(root.count):
        at = root.fields + i * 48
        name = pe.string(at)
        pointer, offset = struct.unpack_from("<QQ", pe.data, at + 16)
        if pointer != pe.address(scalar.at):
            raise SaveError("Unsupported installed perk schema field type.")
        fields.append((name, offset))
    if fields != [("availablePerks", 0), ("unlockedPerks", 1)]:
        raise SaveError("Unsupported installed perk schema fields.")

    pool, references = bytearray(), {}

    def string(value: str) -> int:
        if value not in references:
            encoded = value.encode("ascii")
            if not 1 <= len(encoded) <= 256 or len(pool) + len(encoded) + 1 > 0xFFFF:
                raise SaveError("Generated perk schema string pool exceeds its bounds.")
            references[value] = len(pool) + 1
            pool.extend(bytes((len(encoded) - 1,)) + encoded)
        return references[value]

    namespace_data = struct.pack("<HHHH", string("keen"), 0, string("ecs"), 1)
    records, members = bytearray(), bytearray()
    for owner, _, _, namespaces, _, _, _, _ in expected:
        name = string(pe.string(owner.at))
        qualified = string(pe.string(owner.at + 16))
        start = len(members) // 8 + 1 if owner.kind == 18 else 0
        records.extend(struct.pack(
            "<HHHHIIIIII", name, qualified, len(namespaces), 2 if owner is root else 0,
            owner.size, owner.count, owner.kind, owner.type_hash,
            u32(pe.data, owner.at + 84), start))
        if owner is root:
            for label, offset in fields:
                members.extend(struct.pack("<HHI", string(label), 3, offset))

    output = bytearray(48)
    output[:4] = b"CTCB"

    def section(field: int, data: bytes | bytearray, count: int) -> None:
        struct.pack_into("<II", output, field, len(output) - field, count)
        output.extend(data)

    section(8, namespace_data, 2)
    section(16, records, 3)
    section(24, pool, len(pool))
    output.extend(bytes((-len(output)) % 4))
    section(32, members, 2)
    output.extend(bytes((-len(output)) % 8))
    struct.pack_into("<I", output, 4, len(output))
    schema = bytes(output)
    verified = Schema(schema)
    if verified.field("availablePerks") != (0, 3) or verified.field("unlockedPerks") != (1, 3):
        raise SaveError("Generated perk schema failed field verification.")
    return ComponentDefinition(pe.string(root.at), root.size, schema,
                               root.type_hash, root.type_hash)
