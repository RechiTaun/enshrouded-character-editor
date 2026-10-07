"""Calculate the next stable version from Git tags; never modify the worktree."""

import argparse
from pathlib import Path
import re
import subprocess


VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def validate_version(value: str) -> str:
    if VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Invalid stable semantic version: {value!r}")
    return value


def next_version(tags: list[str], bump: str) -> str:
    if bump not in ("patch", "minor", "major"):
        raise ValueError(f"Unsupported version increment: {bump!r}")
    versions = [tuple(int(part) for part in tag[1:].split("."))
                for tag in tags if tag.startswith("v")
                and VERSION_PATTERN.fullmatch(tag[1:])]
    major, minor, patch = max(versions, default=(0, 0, 0))
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    parser.add_argument("--output", type=Path, help="append GitHub Actions outputs")
    args = parser.parse_args()
    tags = subprocess.run(["git", "tag", "--list"], check=True,
                          capture_output=True, text=True).stdout.splitlines()
    version = next_version(tags, args.bump)
    if args.output:
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(f"version={version}\ntag=v{version}\n")
    print(f"v{version}")


if __name__ == "__main__":
    main()
