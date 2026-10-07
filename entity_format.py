"""Lossless structural access to EHD0 entities and their ESC2 base templates."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from types import MappingProxyType

from save_format import SaveError


MAX_PLAYER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ComponentDefinition:
    name: str
    declared_size: int
    schema: bytes
    hash_a: int = 0
    hash_b: int = 0


@dataclass(frozen=True)
class SavedComponent:
    component_id: int
    offset: int
    data: bytes


@dataclass(frozen=True)
class SavedEntity:
    entity_id: int
    template_index: int
    server: tuple[SavedComponent, ...]
    client: tuple[SavedComponent, ...]


@dataclass(frozen=True)
class SavedTemplate:
    guid: bytes
    name: str
    component_mask: bytes
    offset: int


class _Reader:
    def __init__(self, data: bytes, offset: int, end: int):
        if not 0 <= offset <= end <= len(data):
            raise SaveError("Invalid saved entity section boundary.")
        self.data, self.offset, self.end = data, offset, end

    def take(self, size: int) -> bytes:
        if size < 0 or size > self.end - self.offset:
            raise SaveError("Truncated saved entity record or invalid data range.")
        start = self.offset
        self.offset += size
        return self.data[start:self.offset]

    def unpack(self, fmt: str) -> tuple:
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))

    def name(self, encoding: str, maximum: int) -> str:
        length, = self.unpack("<I")
        if not 1 <= length <= maximum:
            raise SaveError("Invalid saved name length.")
        try:
            name = self.take(length).decode(encoding)
        except UnicodeDecodeError as exc:
            raise SaveError(f"Invalid saved {encoding} name.") from exc
        if "\0" in name:
            raise SaveError("Invalid saved name: embedded NUL.")
        return name


def _integer(value: int, maximum: int, label: str, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SaveError(f"{label} must be an integer between {minimum} and {maximum}.")


class EntityArchive:
    """Decode framing only; component schemas and template masks remain opaque."""

    def __init__(self, payload: bytes):
        if not isinstance(payload, bytes):
            raise SaveError("Saved entity payload must be bytes.")
        if not 43 <= len(payload) <= MAX_PLAYER_BYTES:
            raise SaveError("Saved entity payload exceeds its supported size range.")
        if payload[8:12] != b"EHD0" or struct.unpack_from("<I", payload, 12)[0] != 1:
            raise SaveError("Unsupported saved entity header.")
        schema_at, = struct.unpack_from("<I", payload, 0)
        section_size, = struct.unpack_from("<I", payload, 37)
        if schema_at < 43 or 41 + section_size != schema_at:
            raise SaveError("Inconsistent saved entity section size.")

        schema = _Reader(payload, schema_at, len(payload))
        if schema.take(4) != b"ESC2":
            raise SaveError("Unsupported saved schema version (requires ESC2).")
        definition_count, template_count = schema.unpack("<HH")
        schema.take(12)
        definitions = {}
        names = set()
        for _ in range(definition_count):
            name = schema.name("ascii", 255)
            hash_a, hash_b, component_id, size, schema_size = schema.unpack("<IIHHH")
            schema_bytes = schema.take(schema_size)
            if component_id in definitions or name in names:
                raise SaveError("Duplicate saved component definition.")
            names.add(name)
            definitions[component_id] = ComponentDefinition(
                name, size, schema_bytes, hash_a, hash_b)
        definitions_end = schema.offset
        templates = []
        for _ in range(template_count):
            offset = schema.offset
            guid = schema.take(16)
            name = schema.name("utf-8", MAX_PLAYER_BYTES)
            templates.append(SavedTemplate(guid, name, schema.take(80), offset))
        if schema.offset != len(payload):
            raise SaveError("Saved template records do not end at the payload boundary.")

        section = _Reader(payload, 41, schema_at)
        entity_count, = section.unpack("<H")
        table = [section.unpack("<QH") for _ in range(entity_count)]
        groups_at = section.offset
        entities, references = [], {}
        full_ids = set()
        for entity_id, template_index in table:
            reference = entity_id & 0xFFFFFFFF
            if entity_id in full_ids or reference in references:
                raise SaveError("Duplicate or ambiguous saved entity reference.")
            if template_index >= len(templates):
                raise SaveError("Invalid saved base-template index.")
            full_ids.add(entity_id)
            references[reference] = entity_id
            server_count, client_count = section.unpack("<HH")
            partitions = []
            seen = set()
            for count in (server_count, client_count):
                components = []
                for _ in range(count):
                    component_id, size = section.unpack("<HH")
                    if component_id not in definitions or component_id in seen:
                        raise SaveError("Unknown or duplicate saved component ID.")
                    seen.add(component_id)
                    offset = section.offset
                    components.append(SavedComponent(component_id, offset, section.take(size)))
                partitions.append(tuple(components))
            entities.append(SavedEntity(entity_id, template_index, *partitions))
        if section.offset != schema_at:
            raise SaveError("Saved entity groups do not end at the schema boundary.")

        self.payload = payload
        self.schema_at = schema_at
        self.definitions = MappingProxyType(definitions)
        self.templates = tuple(templates)
        self.entities = tuple(entities)
        self.references = MappingProxyType(references)
        self._groups_at = groups_at
        self._definitions_end = definitions_end

    def next_component_id(self) -> int:
        """Saved IDs map by hash at load; template-only IDs remain reserved."""
        for component_id in range(1, 640):
            if component_id not in self.definitions and not any(
                    t.component_mask[component_id // 8] & (1 << (component_id % 8))
                    for t in self.templates):
                return component_id
        raise SaveError("No free saved component ID remains in the 640-component namespace.")

    def append_definition(self, component_id: int, definition: ComponentDefinition) -> bytes:
        _integer(component_id, 639, "New component ID")
        if not isinstance(definition, ComponentDefinition):
            raise SaveError("New component definition must be a ComponentDefinition.")
        if component_id in self.definitions or any(
                t.component_mask[component_id // 8] & (1 << (component_id % 8))
                for t in self.templates):
            raise SaveError("New component ID conflicts with a definition or template bit.")
        if len(self.definitions) >= 640:
            raise SaveError("Saved component definition count exceeds 640.")
        if any(key >= 640 for key in self.definitions):
            raise SaveError("Existing component IDs exceed the supported 640-component namespace.")
        if not isinstance(definition.name, str):
            raise SaveError("New component name must be ASCII text.")
        try:
            name = definition.name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SaveError("New component name must be ASCII text.") from exc
        if not 1 <= len(name) <= 255 or b"\0" in name:
            raise SaveError("Invalid new component name.")
        if any(d.name == definition.name for d in self.definitions.values()):
            raise SaveError("Duplicate saved component name.")
        _integer(definition.declared_size, 0xFFFF, "New component size")
        for value in (definition.hash_a, definition.hash_b):
            _integer(value, 0xFFFFFFFF, "New component hash", 1)
        if any(d.hash_a == definition.hash_a or d.hash_b == definition.hash_b
               for d in self.definitions.values()):
            raise SaveError("New component hash conflicts with an existing definition.")
        if not isinstance(definition.schema, bytes) or not 1 <= len(definition.schema) <= 0xFFFF:
            raise SaveError("New component schema must contain 1..65535 bytes.")
        total = sum(len(d.schema) for d in self.definitions.values())
        if struct.unpack_from("<I", self.payload, self.schema_at + 8)[0] != total:
            raise SaveError("Saved aggregate schema allocation is inconsistent; generation blocked.")
        record = (struct.pack("<I", len(name)) + name
                  + struct.pack("<IIHHH", definition.hash_a, definition.hash_b,
                                component_id, definition.declared_size, len(definition.schema))
                  + definition.schema)
        if len(self.payload) + len(record) > MAX_PLAYER_BYTES:
            raise SaveError("Appended definition exceeds the 1 MiB player safety limit.")
        prefix = bytearray(self.payload[:self._definitions_end])
        struct.pack_into("<H", prefix, self.schema_at + 4, len(self.definitions) + 1)
        struct.pack_into("<I", prefix, self.schema_at + 8, total + len(definition.schema))
        result = bytes(prefix) + record + self.payload[self._definitions_end:]
        return EntityArchive(result).payload

    def next_entity_reference(self) -> int:
        reference = max(self.references, default=0) + 1
        if reference > 0xFFFFFFFF:
            raise SaveError("Saved entity reference space is exhausted.")
        return reference

    def append_entity(
        self,
        entity_id: int,
        template_index: int,
        server: tuple[tuple[int, bytes], ...],
        client: tuple[tuple[int, bytes], ...] = (),
    ) -> bytes:
        """Append state using an existing template, without initializing gameplay."""
        _integer(entity_id, 0xFFFFFFFF, "New entity reference", 1)
        _integer(template_index, 0xFFFF, "Base-template index")
        if template_index >= len(self.templates):
            raise SaveError("Invalid saved base-template index.")
        if entity_id in self.references or any(e.entity_id == entity_id for e in self.entities):
            raise SaveError("Duplicate or ambiguous saved entity reference.")
        if len(self.entities) >= 0xFFFF:
            raise SaveError("Saved entity count exceeds 65535.")
        seen, records = set(), []
        growth = 14
        for partition in (server, client):
            if not isinstance(partition, tuple) or len(partition) > 0xFFFF:
                raise SaveError("Component partition must be a tuple with at most 65535 entries.")
            for entry in partition:
                if not isinstance(entry, tuple) or len(entry) != 2:
                    raise SaveError("Component entry must be an (ID, bytes) tuple.")
                component_id, data = entry
                _integer(component_id, 0xFFFF, "Component ID")
                if component_id not in self.definitions or component_id in seen:
                    raise SaveError("Unknown or duplicate saved component ID.")
                if not isinstance(data, bytes) or len(data) > 0xFFFF:
                    raise SaveError("Component payload must be bytes of at most 65535 bytes.")
                growth += 4 + len(data)
                if len(self.payload) + growth > MAX_PLAYER_BYTES:
                    raise SaveError("Appended entity payload exceeds the 1 MiB safety limit.")
                seen.add(component_id)
                records.append(struct.pack("<HH", component_id, len(data)) + data)

        group = struct.pack("<HH", len(server), len(client)) + b"".join(records)
        if len(self.payload) + growth > MAX_PLAYER_BYTES:
            raise SaveError("Appended entity payload exceeds the 1 MiB safety limit.")
        header = bytearray(self.payload[:43])
        struct.pack_into("<I", header, 0, self.schema_at + growth)
        struct.pack_into("<I", header, 37, self.schema_at + growth - 41)
        struct.pack_into("<H", header, 41, len(self.entities) + 1)
        result = (
            bytes(header) + self.payload[43:self._groups_at]
            + struct.pack("<QH", entity_id, template_index)
            + self.payload[self._groups_at:self.schema_at]
            + group + self.payload[self.schema_at:]
        )
        return EntityArchive(result).payload
