"""Portable plain-stack inventories, with a deliberately restricted YAML schema."""

from __future__ import annotations

from pathlib import Path

import yaml

from save_format import SaveError, read_limited, uint32


MAX_YAML = 256 * 1024
MAX_ITEMS = 4096
FORMAT = "enshrouded-inventory"


class InventoryLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise SaveError("YAML aliases are not supported in inventory files.")
        self.inventory_depth = getattr(self, "inventory_depth", 0) + 1
        if self.inventory_depth > 8:
            raise SaveError("Inventory YAML is too deeply nested.")
        try:
            return super().compose_node(parent, index)
        finally:
            self.inventory_depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise SaveError("Inventory YAML keys must be unique strings.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_inventory_yaml(raw: bytes) -> list[tuple[int, int]]:
    if len(raw) > MAX_YAML:
        raise SaveError("Inventory YAML exceeds the 256 KiB limit.")
    try:
        document = yaml.load(raw.decode("utf-8-sig"), Loader=InventoryLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SaveError(f"Invalid inventory YAML: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"format", "version", "items"}:
        raise SaveError("Inventory YAML requires format, version and items only.")
    if document["format"] != FORMAT or type(document["version"]) is not int or document["version"] != 1:
        raise SaveError("Unsupported inventory YAML format/version.")
    items = document["items"]
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise SaveError("Inventory YAML items must be a list of at most 4096 stacks.")
    result = []
    for entry in items:
        if not isinstance(entry, dict) or set(entry) not in (
                {"item_id", "quantity"}, {"item_id", "quantity", "name"}):
            raise SaveError("Each inventory item requires item_id and quantity, with optional name.")
        item_id, quantity = entry["item_id"], entry["quantity"]
        uint32(item_id, "Item ID")
        uint32(quantity, "Quantity")
        if not item_id or not quantity:
            raise SaveError("Imported item IDs and quantities must be positive.")
        if "name" in entry and (not isinstance(entry["name"], str) or len(entry["name"]) > 4096):
            raise SaveError("Inventory item name must be a string of at most 4096 characters.")
        result.append((item_id, quantity))
    return result


def read_inventory_yaml(path: Path) -> list[tuple[int, int]]:
    return parse_inventory_yaml(read_limited(path, MAX_YAML))


def write_inventory_yaml(path: Path, items: list[dict]) -> None:
    text = yaml.safe_dump({"format": FORMAT, "version": 1, "items": items},
                          sort_keys=False, allow_unicode=True)
    raw = text.encode("utf-8")
    parse_inventory_yaml(raw)
    # Exclusive creation avoids accidentally overwriting a save or existing export.
    with path.open("xb") as stream:
        stream.write(raw)
