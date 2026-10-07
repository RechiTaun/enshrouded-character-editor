"""Schema-driven access to saved entities; unknown bytes remain unchanged."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import struct
from typing import TYPE_CHECKING

from entity_format import EntityArchive, MAX_PLAYER_BYTES
from save_format import Bdb, MAX_BLOB, SaveError, bounded, u32, uint32

if TYPE_CHECKING:
    from game_rules import ProgressionRules


def unpack(data: bytes, offset: int, fmt: str) -> tuple:
    bounded(data, offset, struct.calcsize(fmt))
    return struct.unpack_from(fmt, data, offset)


@dataclass(frozen=True)
class DataType:
    name: str
    inner: int
    size: int
    count: int
    kind: int
    start: int


class Schema:
    def __init__(self, data: bytes):
        bounded(data, 0, 40)
        if data[:4] != b"CTCB" or u32(data, 4) != len(data):
            raise SaveError("Invalid embedded CTCB schema.")
        self.data = data
        types, count = self.section(16, 32)
        self.pool, self.pool_size = self.section(24, 1)
        self.fields, self.field_count = self.section(32, 8)
        ranges = sorted((at, at + size) for at, size in (
            (types, count * 32), (self.pool, self.pool_size),
            (self.fields, self.field_count * 8)) if size)
        if any(end > next_at for (_, end), (next_at, _) in zip(ranges, ranges[1:])):
            raise SaveError("Overlapping embedded schema sections.")
        if not 1 <= count <= 2048:
            raise SaveError("Unsupported embedded type count.")
        self.types: list[DataType] = []
        for i in range(count):
            name, qualified, _, inner, size, items, kind, _, _, start = unpack(
                data, types + 32 * i, "<HHHHIIIIII")
            self.string(qualified)
            if inner > count:
                raise SaveError("Invalid embedded inner-type reference.")
            self.types.append(DataType(self.string(name), inner, size, items, kind, start))
        for type_id, item in enumerate(self.types, 1):
            if item.kind == 18:
                names = set()
                occupied = []
                for name, child, offset in self.members(type_id):
                    if name in names or offset + self.type(child).size > item.size:
                        raise SaveError("Duplicate or out-of-bounds embedded field.")
                    names.add(name)
                    if self.type(child).size:
                        occupied.append((offset, offset + self.type(child).size))
                occupied.sort()
                if any(end > next_at for (_, end), (next_at, _) in
                       zip(occupied, occupied[1:])):
                    raise SaveError("Overlapping embedded fields; editing is unsafe.")
            if item.kind == 19:
                if not item.inner or self.type(item.inner).size * item.count != item.size:
                    raise SaveError("Invalid embedded static-array layout.")

    def section(self, field: int, width: int) -> tuple[int, int]:
        start, count = field + u32(self.data, field), u32(self.data, field + 4)
        bounded(self.data, start, count * width)
        if count and start < 40:
            raise SaveError("Embedded schema section overlaps its header.")
        return start, count

    def string(self, reference: int) -> str:
        if not 1 <= reference <= self.pool_size:
            raise SaveError("Invalid embedded string reference.")
        at = self.pool + reference - 1
        size = self.data[at] + 1
        if at + 1 + size > self.pool + self.pool_size:
            raise SaveError("Embedded string exceeds its pool.")
        try:
            return self.data[at + 1:at + 1 + size].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SaveError("Invalid embedded UTF-8 string.") from exc

    def type(self, type_id: int) -> DataType:
        if not 1 <= type_id <= len(self.types):
            raise SaveError("Invalid embedded type reference.")
        return self.types[type_id - 1]

    def members(self, type_id: int) -> list[tuple[str, int, int]]:
        item = self.type(type_id)
        if not item.count:
            return []
        if not 1 <= item.start <= self.field_count:
            raise SaveError("Invalid embedded field-table reference.")
        if item.start - 1 + item.count > self.field_count:
            raise SaveError("Embedded field table is truncated.")
        result = []
        for i in range(item.count):
            name, child, offset = unpack(
                self.data, self.fields + 8 * (item.start - 1 + i), "<HHI")
            self.type(child)
            result.append((self.string(name), child, offset))
        return result

    def field(self, name: str, type_id: int = 1) -> tuple[int, int]:
        visited = set()
        while type_id:
            if type_id in visited:
                raise SaveError("Cyclic embedded type inheritance.")
            visited.add(type_id)
            item = self.type(type_id)
            if item.kind == 18:
                for label, child, offset in self.members(type_id):
                    if label == name:
                        return offset, child
            type_id = item.inner
        raise SaveError(f"Unsupported embedded layout: missing {name}.")

    def path(self, names: tuple[str, ...], type_id: int = 1) -> tuple[int, int]:
        offset = 0
        for name in names:
            relative, type_id = self.field(name, type_id)
            offset += relative
        return offset, type_id

    def array(self, name: str) -> tuple[int, int, int]:
        offset, type_id = self.field(name)
        item = self.type(type_id)
        if item.kind != 19 or not item.inner:
            raise SaveError(f"Unsupported {name}: expected static array.")
        return offset, item.inner, item.count

    def scalar(self, type_id: int, size: int, kind: int) -> None:
        visited = set()
        while self.type(type_id).kind in (17, 14):
            if type_id in visited:
                raise SaveError("Cyclic embedded scalar type.")
            visited.add(type_id)
            type_id = self.type(type_id).inner
        item = self.type(type_id)
        if item.size != size or item.kind != kind:
            raise SaveError("Unsupported embedded scalar storage.")

    def enum(self, type_id: int) -> dict[int, str]:
        item = self.type(type_id)
        if item.kind != 12 or not item.start >> 16:
            raise SaveError("Unsupported embedded enum layout.")
        end = self.fields + 8 * self.field_count
        at = end + (-end) % 8 + 16 * ((item.start >> 16) - 1)
        result = {}
        for i in range(item.count):
            name, value = unpack(self.data, at + 16 * i, "<QQ")
            if value in result:
                raise SaveError("Duplicate embedded enum value.")
            result[value] = self.string(name)
        return result


@dataclass(frozen=True)
class Component:
    name: str
    offset: int
    size: int
    schema: Schema


@dataclass(frozen=True)
class Stack:
    entity: int
    slot: int
    category: str
    item_id: int
    quantity: int
    item_entity: int
    quantity_offset: int
    accessible: bool

    @property
    def key(self) -> tuple[int, int]:
        return self.entity, self.slot


@dataclass(frozen=True)
class Experience:
    level: int
    current: int
    gain: int
    required: int


@dataclass(frozen=True)
class LevelPlan:
    experience: Experience
    xp_granted: int
    points_added: int


@dataclass(frozen=True)
class Skill:
    slot: int
    node_id: int
    impact_entity: int
    unlock_level: int


class PlayerState:
    def __init__(self, character: bytes):
        self.character = character
        self.positions, self.payload = self.read_saved_data(character)
        self.entities: dict[int, dict[str, Component]] = {}
        self.references: dict[int, int] = {}
        self.parse_entities()

    @staticmethod
    def read_saved_data(character: bytes, *,
                        allow_empty: bool = False) -> tuple[tuple[int, ...], bytes]:
        db = Bdb(character)
        kind, reference = db.node(db.field("data"))
        descriptors, descriptor_count = db.section(28, 8)
        children, child_count = db.section(76, 4)
        if allow_empty and kind == 21 and reference == 0:
            return (), b""
        if kind != 21 or not 1 <= reference <= descriptor_count:
            raise SaveError("Unsupported character data-array descriptor.")
        count, start = unpack(character, descriptors + 8 * (reference - 1), "<II")
        if allow_empty and count == 0:
            return (), b""
        if not count or not 1 <= start or start - 1 + count > child_count:
            raise SaveError("Character has no initialized saved entity data.")
        if count > MAX_PLAYER_BYTES or child_count > MAX_PLAYER_BYTES:
            raise SaveError("Saved player data exceeds the decoder safety limit.")
        keys, key_count = db.section(100, 8)
        links, link_count = db.section(108, 4)
        ranges = [(db.types, db.count), (db.values, db.count * 4),
                  (db.pool, db.pool_size), (descriptors, descriptor_count * 8),
                  (children, child_count * 4), (keys, key_count * 8),
                  (links, link_count * 4)]
        occupied = sorted((at, at + size) for at, size in ranges if size)
        if any(end > next_at for (_, end), (next_at, _) in
               zip(occupied, occupied[1:])):
            raise SaveError("Character array overlaps BDB storage.")
        nodes = [u32(character, children + 4 * (start - 1 + i)) - 1
                 for i in range(count)]
        occurrences = Counter(u32(character, children + 4 * i) - 1
                              for i in range(child_count))
        object_children = {child for fields in db.edges.values() for child in fields.values()}
        if any(occurrences[node] != 1 or node in object_children for node in nodes):
            raise SaveError("Character bytes share BDB node storage; editing is unsafe.")
        values = [db.node(node) for node in nodes]
        if any(kind != 6 or value > 255 for kind, value in values):
            raise SaveError("Character data is not a uint8 array.")
        return (tuple(db.values + 4 * node for node in nodes),
                bytes(value for _, value in values))

    def parse_entities(self) -> None:
        self.archive = EntityArchive(self.payload)
        self.references = dict(self.archive.references)
        schema_cache: dict[int, Schema] = {}
        for entity in self.archive.entities:
            components: dict[str, Component] = {}
            self.entities[entity.entity_id] = components
            for saved in (*entity.server, *entity.client):
                component_id = saved.component_id
                definition = self.archive.definitions[component_id]
                name, size = definition.name, len(saved.data)
                if name in ("Experience", "Level", "Inventory", "InventorySetup",
                            "UnlockedSkillNodes"):
                    if size != definition.declared_size:
                        raise SaveError(f"Unsupported saved {name} component size.")
                    if component_id not in schema_cache:
                        schema_cache[component_id] = Schema(definition.schema)
                    schema = schema_cache[component_id]
                    if schema.type(1).name != name or schema.type(1).size != size:
                        raise SaveError(f"Saved {name} does not match its schema.")
                    components[name] = Component(name, saved.offset, size, schema)

    def player(self) -> tuple[int, dict[str, Component]]:
        matches = [(entity, components) for entity, components in self.entities.items()
                   if "Experience" in components and "Level" in components]
        if len(matches) != 1:
            raise SaveError("Cannot identify exactly one saved player actor.")
        return matches[0]

    def field(self, component: Component, names: tuple[str, ...],
              width: int, kind: int) -> int:
        offset, type_id = component.schema.path(names)
        component.schema.scalar(type_id, width, kind)
        if offset + width > component.size:
            raise SaveError("Named field exceeds its saved component.")
        return component.offset + offset

    def attribute(self, component: Component, count: int) -> tuple[int, ...]:
        offset, child, actual = component.schema.array("dataStorage")
        component.schema.scalar(child, 4, 6)
        if actual != count:
            raise SaveError(f"Unsupported {component.name} attribute count.")
        storage_offset = u32(self.payload, self.field(
            component, ("storageOffset",), 4, 6))
        storage_size = u32(self.payload, self.field(
            component, ("storageSize",), 4, 6))
        if storage_offset != offset or storage_size != count:
            raise SaveError(f"Inconsistent {component.name} attribute storage.")
        return unpack(self.payload, component.offset + offset, f"<{count}I")

    def experience(self) -> Experience:
        _, components = self.player()
        level, _ = self.attribute(components["Level"], 2)
        current, gain, required = self.attribute(components["Experience"], 3)
        if Bdb(self.character).character()[1] != level:
            raise SaveError("Summary and actor levels differ; coherent editing is unavailable.")
        return Experience(level, current, gain, required)

    def level_plan(self, level: int, rules: ProgressionRules,
                   baseline: Experience) -> LevelPlan:
        uint32(level, "Level")
        rules.required_xp(level)
        _, components = self.player()
        _, maximum = self.attribute(components["Level"], 2)
        if level < baseline.level:
            raise SaveError("Lowering the saved level is not supported.")
        if not 1 <= baseline.level <= level <= maximum:
            raise SaveError("Level is outside the saved actor's supported range.")
        if level == baseline.level:
            return LevelPlan(baseline, 0, 0)
        if baseline.gain:
            raise SaveError("Pending XP gain must be processed in-game before editing levels.")
        threshold = baseline.required or rules.required_xp(baseline.level)
        if baseline.current >= threshold:
            raise SaveError("Saved XP already requires a level-up; process it in-game first.")
        granted = 0
        for next_level in range(baseline.level + 1, level + 1):
            granted += threshold
            threshold = rules.required_xp(next_level)
            if baseline.current >= threshold:
                raise SaveError("Current XP exceeds a target threshold; exact level editing is unsafe.")
        return LevelPlan(Experience(level, baseline.current, 0, threshold), granted,
                         (level - baseline.level) * rules.points_per_level)

    def patch_level(self, plan: LevelPlan) -> bytes:
        self.experience()
        _, components = self.player()
        raw = bytearray(self.payload)
        for name, values in (
                ("Level", (plan.experience.level,)),
                ("Experience", (plan.experience.current, plan.experience.gain,
                                plan.experience.required))):
            component = components[name]
            self.attribute(component, 2 if name == "Level" else 3)
            offset, _, _ = component.schema.array("dataStorage")
            struct.pack_into(f"<{len(values)}I", raw, component.offset + offset, *values)
        db = Bdb(self.character)
        node = db.field("level")
        if sum(child == node for fields in db.edges.values() for child in fields.values()) != 1:
            raise SaveError("Summary level shares storage with another field.")
        output = bytearray(self._patch_payload(bytes(raw)))
        struct.pack_into("<I", output, db.values + 4 * node, plan.experience.level)
        verified = PlayerState(bytes(output))
        if verified.experience() != plan.experience:
            raise SaveError("Progression patch failed coherent level/XP verification.")
        return bytes(output)

    def _patch_payload(self, raw: bytes) -> bytes:
        if len(raw) != len(self.payload):
            raise SaveError("Saved player payload cannot be resized.")
        output = bytearray(self.character)
        for i, value in enumerate(raw):
            if value != self.payload[i]:
                struct.pack_into("<I", output, self.positions[i], value)
        if PlayerState(bytes(output)).payload != raw:
            raise SaveError("Player patch failed byte-preserving verification.")
        return bytes(output)

    def replace_payload(self, raw: bytes) -> bytes:
        """Replace structurally valid state; growth does not initialize game items."""
        EntityArchive(raw)
        if len(raw) == len(self.payload):
            return self._patch_payload(raw)
        if len(raw) < len(self.payload):
            raise SaveError("Shrinking saved player data is unsupported.")
        db = Bdb(self.character)
        data_node = db.field("data")
        _, reference = db.node(data_node)
        array_nodes: dict[int, list[int]] = {}
        for index in range(db.count):
            kind, value = db.node(index)
            if kind == 21:
                array_nodes.setdefault(value, []).append(index)
        if array_nodes[reference] != [data_node]:
            raise SaveError("Character data shares its array descriptor; resize is unsafe.")
        descriptors, descriptor_count = db.section(28, 8)
        children, child_count = db.section(76, 4)
        count, start = unpack(self.character, descriptors + 8 * (reference - 1), "<II")
        end = start - 1 + count
        extra = len(raw) - count
        new_child_count = child_count + extra
        if new_child_count > MAX_PLAYER_BYTES:
            raise SaveError("Resized child table exceeds the decoder safety limit.")
        descriptor_data = bytearray(
            self.character[descriptors:descriptors + descriptor_count * 8])
        new_array_references: dict[int, int] = {}
        for array_reference, indices in array_nodes.items():
            if not 1 <= array_reference <= descriptor_count:
                raise SaveError("Invalid sibling array descriptor reference; resize is unsafe.")
            items, first = unpack(descriptor_data, (array_reference - 1) * 8, "<II")
            if array_reference == reference:
                items = len(raw)
            else:
                if items:
                    if not first or first - 1 + items > child_count:
                        raise SaveError("Invalid sibling array descriptor; resize is unsafe.")
                    if first - 1 < end and start - 1 < first - 1 + items:
                        raise SaveError("Character data shares array children; resize is unsafe.")
                if not first or first - 1 < end:
                    continue
                uint32(first + extra, "Resized array start")
                first += extra
            # This 64-bit storage table also holds non-array values: copy on write.
            descriptor_data.extend(struct.pack("<II", items, first))
            for index in indices:
                new_array_references[index] = len(descriptor_data) // 8
        new_descriptor_count = len(descriptor_data) // 8
        new_count = db.count + extra
        estimated_size = len(self.character)
        for size, alignment in ((new_count, 1), (new_count * 4, 4),
                                (len(descriptor_data), 4), (new_child_count * 4, 4)):
            estimated_size += (-estimated_size) % alignment + size
        if estimated_size > MAX_BLOB:
            raise SaveError("Resized character exceeds the save blob safety limit.")
        type_data = self.character[db.types:db.types + db.count] + bytes([6]) * extra
        value_data = bytearray(self.character[db.values:db.values + db.count * 4])
        for i, position in enumerate(self.positions):
            struct.pack_into("<I", value_data, position - db.values, raw[i])
        for index, new_reference in new_array_references.items():
            struct.pack_into("<I", value_data, index * 4, new_reference)
        value_data.extend(b"".join(struct.pack("<I", value) for value in raw[count:]))
        insertion = children + 4 * end
        child_data = (self.character[children:insertion] +
                      b"".join(struct.pack("<I", db.count + i + 1) for i in range(extra)) +
                      self.character[insertion:children + child_count * 4])
        output = bytearray(self.character)

        def append_table(field: int, data: bytes | bytearray,
                         items: int, alignment: int) -> None:
            output.extend(bytes((-len(output)) % alignment))
            struct.pack_into("<II", output, field, len(output) - field, items)
            output.extend(data)

        # Old tables stay intact for opaque sections; existing node IDs never move.
        append_table(4, type_data, new_count, 1)
        append_table(12, value_data, new_count, 4)
        append_table(28, descriptor_data, new_descriptor_count, 4)
        append_table(76, child_data, new_child_count, 4)
        result = bytes(output)
        if PlayerState(result).payload != raw or Bdb(result).character() != db.character():
            raise SaveError("Resized player data failed preservation verification.")
        return result

    def inventory(self, *, include_empty: bool = False) -> list[Stack]:
        entity, _ = self.player()
        visited = set()
        result: list[Stack] = []
        pending = [(entity, frozenset())]
        while pending:
            setup_entity, ancestors = pending.pop()
            if setup_entity in ancestors:
                raise SaveError("Cyclic linked inventory setup.")
            if setup_entity in visited:
                continue
            visited.add(setup_entity)
            ancestors = ancestors | {setup_entity}
            components = self.entities[setup_entity]
            if "InventorySetup" not in components:
                continue
            setup = components["InventorySetup"]
            links_at, link_type, link_count = setup.schema.array("linksEntities")
            categories_at, category_type, category_count = setup.schema.array("linksCategories")
            if link_count != category_count:
                raise SaveError("Inconsistent inventory link/category counts.")
            link_size = setup.schema.type(link_type).size
            link_id_at, link_id_type = setup.schema.path(("id",), link_type)
            setup.schema.scalar(link_id_type, 4, 6)
            if link_size != 4 or link_id_at:
                raise SaveError("Unsupported inventory entity-reference layout.")
            category = setup.schema.type(category_type)
            if category.size != 1 or category.name != "InventoryCategory":
                raise SaveError("Unsupported inventory category storage.")
            categories = setup.schema.enum(category_type)
            available = self.payload[self.field(setup, ("availableSlotCount",), 1, 2)]
            generic = self.payload[self.field(setup, ("genericSlotCount",), 1, 2)]
            initialized = self.payload[self.field(setup, ("isInitialized",), 1, 1)]
            if initialized != 1 or available > generic:
                raise SaveError("Inventory setup is uninitialized or inconsistent.")
            generic_seen = 0
            for i in range(link_count):
                reference = u32(self.payload, setup.offset + links_at + 4 * i)
                category_id = self.payload[setup.offset + categories_at + i]
                if not reference:
                    continue
                if category_id not in categories or reference not in self.references:
                    raise SaveError("Unresolved inventory category or entity link.")
                inventory_entity = self.references[reference]
                inventory = self.entities[inventory_entity].get("Inventory")
                if categories[category_id] == "Virtual" and (
                        "InventorySetup" in self.entities[inventory_entity]):
                    pending.append((inventory_entity, ancestors))
                    continue
                if inventory is None:
                    raise SaveError("Linked inventory entity has no Inventory component.")
                slots_at, stack_type, slot_count = inventory.schema.array("slots")
                stack_size = inventory.schema.type(stack_type).size
                id_at, id_type = inventory.schema.path(("id", "value"), stack_type)
                count_at, count_type = inventory.schema.path(("data", "count"), stack_type)
                entity_at, entity_type = inventory.schema.path(("data", "pide", "id"), stack_type)
                if (stack_size, id_at, count_at, entity_at) != (12, 0, 4, 8):
                    raise SaveError("Unsupported saved ItemStack storage layout.")
                for scalar in (id_type, count_type, entity_type):
                    inventory.schema.scalar(scalar, 4, 6)
                category_name = categories[category_id]
                for slot in range(slot_count):
                    base = inventory.offset + slots_at + slot * stack_size
                    item_id = u32(self.payload, base + id_at)
                    quantity = u32(self.payload, base + count_at)
                    item_entity = u32(self.payload, base + entity_at)
                    accessible = category_name == "Generic" and generic_seen + slot < available
                    if item_id or quantity or item_entity:
                        if not item_id or not quantity:
                            raise SaveError("Inconsistent nonempty inventory stack.")
                        if item_entity and item_entity not in self.references:
                            raise SaveError("Inventory item refers to a missing saved entity.")
                    if item_id or quantity or item_entity or include_empty:
                        result.append(Stack(inventory_entity, slot, category_name, item_id,
                                            quantity, item_entity, base + count_at, accessible))
                    if item_entity and item_entity in self.references:
                        nested = self.references[item_entity]
                        if "InventorySetup" in self.entities[nested]:
                            pending.append((nested, ancestors))
                if category_name == "Generic":
                    generic_seen += slot_count
            if generic_seen < available:
                raise SaveError("Available inventory slots exceed linked generic capacity.")
        if len({stack.key for stack in result}) != len(result):
            raise SaveError("Inventory slots share multiple setup links.")
        return result

    def skills(self) -> list[Skill]:
        _, components = self.player()
        if "UnlockedSkillNodes" not in components:
            raise SaveError("No saved skill-node component.")
        component = components["UnlockedSkillNodes"]
        schema = component.schema
        nodes_at, node_type, count = schema.array("knownSkillNodes")
        node_at, node_scalar = schema.path(("value",), node_type)
        impacts_at, impact_type, impact_count = schema.array("activeSkillImpacts")
        impact_at, impact_scalar = schema.path(("id",), impact_type)
        levels_at, level_type, level_count = schema.array("unlockLevel")
        if schema.type(level_type).name == "SkillUnlockLevel":
            value_at, value_type = schema.path(("value",), level_type)
            if value_at or schema.type(level_type).size != 1:
                raise SaveError("Unsupported skill unlock-level wrapper.")
            level_type = value_type
        schema.scalar(node_scalar, 4, 6)
        schema.scalar(impact_scalar, 4, 6)
        schema.scalar(level_type, 1, 2)
        if node_at or impact_at or schema.type(node_type).size != 4 or (
                schema.type(impact_type).size != 4) or count != impact_count or count != level_count:
            raise SaveError("Unsupported skill-node array layout.")
        result = []
        for i in range(count):
            node = u32(self.payload, component.offset + nodes_at + 4 * i)
            impact = u32(self.payload, component.offset + impacts_at + 4 * i)
            level = self.payload[component.offset + levels_at + i]
            if node or impact or level:
                result.append(Skill(i, node, impact, level))
        return result

    def patch_quantity(self, entity: int, slot: int, quantity: int, *,
                       item_id: int, max_stack: int) -> bytes:
        uint32(quantity, "Quantity")
        uint32(max_stack, "Maximum stack")
        matches = [stack for stack in self.inventory() if stack.key == (entity, slot)]
        if len(matches) != 1:
            raise SaveError("Select an existing inventory stack.")
        stack = matches[0]
        if not stack.accessible or stack.item_entity:
            raise SaveError("Only accessible, plain generic-inventory stacks can be edited.")
        if stack.item_id != item_id:
            raise SaveError("Item rule does not match the selected stack.")
        if not 1 <= quantity <= max_stack or not max_stack:
            raise SaveError(f"Quantity must be between 1 and the verified limit {max_stack}.")
        raw = bytearray(self.payload)
        struct.pack_into("<I", raw, stack.quantity_offset, quantity)
        return self._patch_payload(bytes(raw))

    def patch_inventory_slots(self, updates: dict[tuple[int, int], tuple[int, int]]) -> bytes:
        slots = {stack.key: stack for stack in self.inventory(include_empty=True)}
        raw = bytearray(self.payload)
        for key, (item_id, quantity) in updates.items():
            uint32(item_id, "Item ID")
            uint32(quantity, "Quantity")
            stack = slots.get(key)
            if stack is None or not stack.accessible or stack.item_entity:
                raise SaveError("Only accessible plain Generic slots may be changed.")
            if bool(item_id) != bool(quantity):
                raise SaveError("Empty slots require both item ID and quantity to be zero.")
            struct.pack_into("<III", raw, stack.quantity_offset - 4, item_id, quantity, 0)
        result = self._patch_payload(bytes(raw))
        PlayerState(result).inventory(include_empty=True)
        return result
