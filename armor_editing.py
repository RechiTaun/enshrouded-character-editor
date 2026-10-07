"""Construct verified fresh armor state without copying existing item entities."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from entity_format import ComponentDefinition, EntityArchive
from player_state import PlayerState, Schema
from save_format import SaveError, uint32

if TYPE_CHECKING:
    from game_rules import ArmorRule


COMPONENT_HASHES = {
    "ItemState": 0x3A2FAC75,
    "Level": 0xE550DA37,
    "OwnerRelationship": 0xD7361788,
    "PerkContainerNew": 0xE288D50C,
    "UsedItem": 0x0B02361D,
    "StaticTransform": 0x5458E4A3,
}


def _schema(archive: EntityArchive, name: str, size: int) -> tuple[int, Schema]:
    matches = [(key, definition) for key, definition in archive.definitions.items()
               if definition.name == name]
    if len(matches) != 1:
        raise SaveError(f"Armor creation needs the saved {name} component schema.")
    component_id, definition = matches[0]
    if (definition.declared_size != size or
            definition.hash_a != COMPONENT_HASHES[name] or
            definition.hash_b != COMPONENT_HASHES[name]):
        raise SaveError(f"Unsupported saved armor {name} definition.")
    schema = Schema(definition.schema)
    root = schema.type(1)
    if root.name != name or root.size != size or root.kind != 18:
        raise SaveError(f"Unsupported saved armor {name} schema.")
    return component_id, schema


def _field(schema: Schema, names: tuple[str, ...], offset: int,
           size: int, kind: int) -> None:
    actual, type_id = schema.path(names)
    if actual != offset:
        raise SaveError(f"Unsupported armor field offset: {'.'.join(names)}.")
    schema.scalar(type_id, size, kind)


def _members(schema: Schema, expected: set[str]) -> None:
    type_id, visited, names = 1, set(), []
    while type_id:
        if type_id in visited or schema.type(type_id).kind != 18:
            raise SaveError("Unsupported armor component inheritance.")
        visited.add(type_id)
        names.extend(name for name, _, _ in schema.members(type_id))
        type_id = schema.type(type_id).inner
    if len(names) != len(expected) or set(names) != expected:
        raise SaveError("Unsupported armor component fields.")


def _mask(schema: Schema, name: str, offset: int, flag_name: str,
          bit: int) -> int:
    actual, type_id = schema.field(name)
    visited = set()
    while schema.type(type_id).kind == 17:
        if type_id in visited:
            raise SaveError(f"Cyclic armor {name} bitmask schema.")
        visited.add(type_id)
        type_id = schema.type(type_id).inner
    item = schema.type(type_id)
    if actual != offset or item.kind != 14 or item.size != 2:
        raise SaveError(f"Unsupported armor {name} bitmask.")
    flags = schema.enum(item.inner)
    if flags.get(bit) != flag_name:
        raise SaveError(f"Unsupported armor {flag_name} flag.")
    return 1 << bit


def add_armor(state: PlayerState, rule: ArmorRule, level: int, rarity: int,
              quantity: int = 1, *, perk_definition: ComponentDefinition | None = None) -> bytes:
    """Return an atomic character patch; the item is placed, never equipped."""
    uint32(level, "Armor level")
    uint32(rarity, "Armor rarity")
    uint32(quantity, "Armor quantity")
    uint32(rule.item_id, "Armor item ID")
    if not quantity:
        raise SaveError("Armor quantity must be positive.")
    if not rule.min_level <= level <= rule.max_level <= 255:
        raise SaveError(f"Armor level must be between {rule.min_level} and {rule.max_level}.")
    if rarity > 4 or rarity not in dict(rule.rarities):
        raise SaveError("Select a rarity allowed by the installed armor rules.")
    state.experience()
    slots = [s for s in state.inventory(include_empty=True)
             if s.accessible and not s.item_entity and not s.item_id and not s.quantity]
    if quantity > len(slots):
        raise SaveError("Not enough accessible empty inventory slots. Nothing was staged.")
    templates = [i for i, template in enumerate(state.archive.templates)
                 if template.guid == rule.template_guid and template.name == "Base_Pide"]
    if len(templates) != 1:
        raise SaveError("Armor creation requires exactly one matching saved Base_Pide template.")
    template_index = templates[0]
    archive = state.archive
    if not any(d.name == "PerkContainerNew" for d in archive.definitions.values()):
        if perk_definition is None:
            raise SaveError("Armor creation needs the saved PerkContainerNew component schema.")
        if not isinstance(perk_definition, ComponentDefinition) or perk_definition.name != "PerkContainerNew":
            raise SaveError("Invalid generated armor perk component definition.")
        archive = EntityArchive(archive.append_definition(
            archive.next_component_id(), perk_definition))
    ids, schemas = {}, {}
    for name, size in (("ItemState", 20), ("Level", 44), ("OwnerRelationship", 4),
                       ("PerkContainerNew", 2), ("UsedItem", 4), ("StaticTransform", 1)):
        ids[name], schemas[name] = _schema(archive, name, size)
    static_id = ids["StaticTransform"]
    if static_id >= 640:
        raise SaveError("Saved StaticTransform ID exceeds the supported template bitmap.")
    mask = bytearray(80)
    mask[static_id // 8] = 1 << (static_id % 8)
    if state.archive.templates[template_index].component_mask != bytes(mask):
        raise SaveError("Saved Base_Pide has unsupported base components.")
    _members(schemas["StaticTransform"], set())
    item_schema = schemas["ItemState"]
    _members(item_schema, {"containedInSlotId", "itemId", "baseDamageUi",
                           "itemState", "itemRarityUi", "itemLevelUi"})
    _field(item_schema, ("containedInSlotId", "entityId", "id"), 0, 4, 6)
    _field(item_schema, ("containedInSlotId", "slotIndex"), 4, 1, 2)
    _field(item_schema, ("itemId", "value"), 8, 4, 6)
    _field(item_schema, ("baseDamageUi",), 12, 2, 4)
    _field(item_schema, ("itemLevelUi",), 17, 1, 2)
    item_flags = _mask(item_schema, "itemState", 14, "HasLevel", 4)
    rarity_at, rarity_type = item_schema.field("itemRarityUi")
    if rarity_at != 16 or item_schema.type(rarity_type).size != 1:
        raise SaveError("Unsupported armor rarity cache storage.")
    saved_rarities = item_schema.enum(rarity_type)
    if any(saved_rarities.get(value) != label for value, label in rule.rarities):
        raise SaveError("Saved and installed armor rarity enums disagree.")
    _field(schemas["OwnerRelationship"], ("ownerEntityId", "id"), 0, 4, 6)
    _field(schemas["UsedItem"], ("itemId", "value"), 0, 4, 6)
    _members(schemas["OwnerRelationship"], {"ownerEntityId"})
    _members(schemas["UsedItem"], {"itemId"})
    _members(schemas["PerkContainerNew"], {"availablePerks", "unlockedPerks"})
    _field(schemas["PerkContainerNew"], ("availablePerks",), 0, 1, 2)
    _field(schemas["PerkContainerNew"], ("unlockedPerks",), 1, 1, 2)
    level_schema = schemas["Level"]
    _members(level_schema, {"rootId", "signature", "storageOffset", "storageSize",
                            "flags", "definition", "dataStorage"})
    for field, at in (("rootId", 0), ("signature", 4)):
        _field(level_schema, (field, "value"), at, 4, 6)
    for field, at in (("storageOffset", 8), ("storageSize", 12)):
        _field(level_schema, (field,), at, 4, 6)
    level_flags = _mask(level_schema, "flags", 16, "Initialized", 0)
    definition_at, definition_type = level_schema.field("definition")
    if (definition_at != 20 or level_schema.type(definition_type).size != 16 or
            level_schema.type(definition_type).name != "AttributeRootReference" or
            level_schema.type(definition_type).kind != 17 or
            level_schema.type(level_schema.type(definition_type).inner).kind != 28 or
            len(rule.level_definition) != 16):
        raise SaveError("Unsupported armor Level attribute definition.")
    storage_at, scalar, count = level_schema.array("dataStorage")
    level_schema.scalar(scalar, 4, 6)
    if storage_at != 36 or count != 2:
        raise SaveError("Unsupported armor Level attribute storage.")
    level_data = (struct.pack("<IIIIH2x", rule.level_root_id, rule.level_signature,
                              36, 2, level_flags) + rule.level_definition +
                  struct.pack("<II", level, rule.max_level))
    initial_experience, initial_skills = state.experience(), state.skills()
    original_entities = state.archive.entities
    additions = []
    for slot in slots[:quantity]:
        container_reference = slot.entity & 0xFFFFFFFF
        if (not container_reference or state.references.get(container_reference) != slot.entity
                or not 0 <= slot.slot <= 255):
            raise SaveError("Armor target slot has an unsupported saved reference.")
        item_state = struct.pack("<IB3xIHHBB2x", container_reference, slot.slot,
                                 rule.item_id, 0, item_flags, rarity, level)
        server = tuple(sorted((
            (ids["ItemState"], item_state), (ids["Level"], level_data),
            (ids["OwnerRelationship"], struct.pack("<I", container_reference)),
            (ids["PerkContainerNew"], bytes((rarity + 1, 0))),
            (ids["UsedItem"], struct.pack("<I", rule.item_id)),
        )))
        entity_id = archive.next_entity_reference()
        archive = EntityArchive(archive.append_entity(entity_id, template_index, server,
                                                       ((static_id, b""),)))
        additions.append((slot, entity_id))
    grown = PlayerState(state.replace_payload(archive.payload))
    raw = bytearray(grown.payload)
    for slot, entity_id in additions:
        target = next(s for s in grown.inventory(include_empty=True) if s.key == slot.key)
        struct.pack_into("<III", raw, target.quantity_offset - 4, rule.item_id, 1, entity_id)
    working = PlayerState(grown.replace_payload(bytes(raw)))
    expected_slots = {s.key: (s.item_id, s.quantity, s.item_entity)
                      for s in state.inventory(include_empty=True)}
    for slot, entity_id in additions:
        expected_slots[slot.key] = (rule.item_id, 1, entity_id)
    if {s.key: (s.item_id, s.quantity, s.item_entity)
        for s in working.inventory(include_empty=True)} != expected_slots:
        raise SaveError("Armor inventory references failed verification; staging aborted.")
    if working.experience() != initial_experience or working.skills() != initial_skills:
        raise SaveError("Armor creation changed actor progression; staging aborted.")
    for before, after in zip(original_entities, working.archive.entities):
        if ((before.entity_id, before.template_index) != (after.entity_id, after.template_index)
                or tuple((c.component_id, c.data) for c in (*before.server, *before.client)
                         if state.archive.definitions[c.component_id].name != "Inventory") !=
                tuple((c.component_id, c.data) for c in (*after.server, *after.client)
                      if state.archive.definitions[c.component_id].name != "Inventory")):
            raise SaveError("Armor creation changed existing entity state; staging aborted.")
    return working.character
