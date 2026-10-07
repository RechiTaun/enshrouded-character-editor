"""Validate existing Steam registrations without changing Cloud settings or metadata."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from save_format import SaveError


MAX_CATALOGUE = 8 * 1024 * 1024
MAX_TOKENS = 32768
TOKEN = re.compile(r'\s+|//[^\r\n]*|"(?:\\["\\]|[^"\\])*"|[{}]|[^\s{}"\\]+')


def catalogue_for(source: Path) -> Path | None:
    folder = source.parent.resolve()
    if folder.name.lower() == "remote" and folder.parent.name == "1203620":
        return folder.parent / "remotecache.vdf"
    return None


def parse_catalogue(raw: bytes) -> dict:
    if len(raw) > MAX_CATALOGUE:
        raise SaveError("Steam catalogue exceeds the safety limit.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SaveError("Steam catalogue is not valid UTF-8.") from exc
    tokens = []
    cursor = 0
    for match in TOKEN.finditer(text):
        if match.start() != cursor:
            raise SaveError("Unsupported Steam catalogue syntax.")
        cursor = match.end()
        token = match.group()
        if token.isspace() or token.startswith("//"):
            continue
        if token.startswith('"'):
            token = re.sub(r'\\(["\\])', r"\1", token[1:-1])
            tokens.append(("text", token))
        else:
            tokens.append((token if token in "{}" else "text", token))
        if len(tokens) > MAX_TOKENS:
            raise SaveError("Steam catalogue has too many tokens.")
    if cursor != len(text):
        raise SaveError("Unsupported Steam catalogue syntax.")
    position = 0

    def object_at(depth: int) -> dict:
        nonlocal position
        if depth > 8:
            raise SaveError("Steam catalogue is too deeply nested.")
        result = {}
        while position < len(tokens) and tokens[position][0] != "}":
            kind, key = tokens[position]
            position += 1
            if kind != "text" or position == len(tokens):
                raise SaveError("Invalid Steam catalogue key/value pair.")
            key = key.casefold()
            if key in result:
                raise SaveError("Duplicate Steam catalogue key.")
            kind, value = tokens[position]
            position += 1
            if kind == "{":
                value = object_at(depth + 1)
                if position == len(tokens) or tokens[position][0] != "}":
                    raise SaveError("Unclosed Steam catalogue object.")
                position += 1
            elif kind != "text":
                raise SaveError("Invalid Steam catalogue value.")
            result[key] = value
        return result

    result = object_at(0)
    if position != len(tokens):
        raise SaveError("Unexpected closing Steam catalogue brace.")
    return result


def validate_registration(raw: bytes | None, filename: str, contents: bytes) -> None:
    if raw is None:
        raise SaveError("No Steam catalogue was captured. Close Steam, then reopen the index.")
    root = parse_catalogue(raw)
    app = root.get("1203620")
    entry = app.get(filename.casefold()) if isinstance(app, dict) else None
    if not isinstance(entry, dict):
        raise SaveError(f"Steam has not registered {filename}; direct overwrite is blocked.")
    size, digest = entry.get("size"), entry.get("sha")
    if not isinstance(size, str) or re.fullmatch(r"[0-9]{1,20}", size) is None:
        raise SaveError(f"Invalid Steam size metadata for {filename}.")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{40}", digest) is None:
        raise SaveError(f"Invalid Steam hash metadata for {filename}.")
    if int(size) != len(contents) or digest.lower() != hashlib.sha1(
            contents, usedforsecurity=False).hexdigest():
        raise SaveError(f"Steam catalogue does not match {filename}. Close Steam and reopen.")
