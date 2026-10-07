"""Build and smoke-test the standalone Windows x64 executable."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

from release_version import validate_version


ROOT = Path(__file__).resolve().parents[1]
NAME = "enshrouded-character-editor-windows-x64"


def validate_smoke_report(report: dict[str, object], version: str) -> None:
    expected = {"version": version, "frozen": True, "save_loaded": False,
                "title": "Enshrouded Character Workshop", "tk": 8.6,
                "yaml": "6.0.2", "zstandard": "0.25.0"}
    if report != expected:
        raise ValueError(f"Frozen UI smoke report does not match expectations: {report!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.0.0")
    args = parser.parse_args()
    version = validate_version(args.version)
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Build requires 64-bit Windows Python")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    metadata = build / "build-version.txt"
    metadata.write_text(version, encoding="ascii")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "--onefile",
         "--windowed", "--name", NAME, "--distpath", str(ROOT / "dist"),
         "--workpath", str(build / "pyinstaller"), "--specpath", str(build),
         "--add-data", f"{metadata};.", str(ROOT / "character_editor.py")],
        cwd=ROOT, check=True,
        env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(build / "cache")},
    )
    executable = ROOT / "dist" / f"{NAME}.exe"
    report_path = build / "smoke-report.json"
    report_path.unlink(missing_ok=True)
    subprocess.run([str(executable), "--smoke-test", str(report_path)],
                   cwd=build, check=True, timeout=120)
    validate_smoke_report(json.loads(report_path.read_text(encoding="utf-8")), version)
    checksum = executable.with_suffix(".exe.sha256")
    checksum.write_text(f"{sha256(executable.read_bytes()).hexdigest()}  {executable.name}\n",
                        encoding="ascii")
    print(f"Verified executable: {executable}")
    print(f"SHA256 file: {checksum}")


if __name__ == "__main__":
    main()
