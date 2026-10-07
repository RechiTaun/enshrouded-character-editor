"""Require curated, version-specific notes before building a release."""

import argparse
from pathlib import Path
import re

from release_version import validate_version


SECTIONS = ("Highlights", "Get started", "Compatibility & safety",
            "Verification", "Learn more")


def validate_notes(text: str, version: str) -> None:
    validate_version(version)
    if not text.startswith("# ") or re.search(
            rf"(?<!\w)v{re.escape(version)}(?![\w.+-])", text.splitlines()[0]) is None:
        raise ValueError("Release-note title must include the exact release version.")
    for section in SECTIONS:
        headings = [line for line in text.splitlines()
                    if line.startswith("## ") and line.endswith(section)]
        if len(headings) != 1:
            raise ValueError(f"Release notes require exactly one {section!r} section.")
        content = text.split(headings[0], 1)[1].split("\n## ", 1)[0].strip()
        if not content:
            raise ValueError(f"Release-note section {section!r} is empty.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    version = validate_version(args.version)
    path = Path(__file__).resolve().parents[1] / "release-notes" / f"v{version}.md"
    validate_notes(path.read_text(encoding="utf-8"), version)
    print(f"Verified curated notes for v{version}")


if __name__ == "__main__":
    main()
