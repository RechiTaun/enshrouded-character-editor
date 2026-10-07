"""Read packaging metadata without requiring Git or network access at runtime."""

from pathlib import Path
import sys


VERSION = (
    (Path(sys._MEIPASS) / "build-version.txt").read_text(encoding="ascii").strip()
    if getattr(sys, "frozen", False) else "0.0.0"
)
