import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AppCliTests(unittest.TestCase):
    def run_editor(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "character_editor.py"), *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def test_version_does_not_open_ui(self):
        result = self.run_editor("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0.0.0")

    def test_smoke_creates_real_ui_without_loading_save(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            result = self.run_editor("--smoke-test", str(report))
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(data["save_loaded"])
            self.assertFalse(data["frozen"])
            self.assertEqual(data["title"], "Enshrouded Character Workshop")
            self.assertEqual(data["yaml"], "6.0.2")
            self.assertEqual(data["zstandard"], "0.25.0")

    def test_smoke_refuses_save_argument_and_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            result = self.run_editor("--smoke-test", str(report), "characters-index")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(report.exists())
            report.write_text("keep existing content", encoding="utf-8")
            result = self.run_editor("--smoke-test", str(report))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(report.read_text(encoding="utf-8"), "keep existing content")


if __name__ == "__main__":
    unittest.main()
