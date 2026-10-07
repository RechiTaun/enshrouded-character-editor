from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from release_version import next_version, validate_version
from build_executable import validate_smoke_report


class ReleaseVersionTests(unittest.TestCase):
    def test_first_release(self):
        for bump, expected in (("patch", "0.0.1"), ("minor", "0.1.0"), ("major", "1.0.0")):
            with self.subTest(bump=bump):
                self.assertEqual(next_version([], bump), expected)

    def test_highest_semantic_tag_and_reset_rules(self):
        tags = ["v1.9.9", "v1.10.4", "v0.99.0", "v1.10.3"]
        for bump, expected in (("patch", "1.10.5"), ("minor", "1.11.0"), ("major", "2.0.0")):
            with self.subTest(bump=bump):
                self.assertEqual(next_version(tags, bump), expected)

    def test_nonstable_and_unrelated_tags_are_ignored(self):
        tags = ["v2.3.4", "v99.0.0-rc.1", "v99.0.0+build", "v02.0.0",
                "2.9.0", "unrelated", "v2.3", "v-1.0.0", "v1.0.0\n"]
        self.assertEqual(next_version(tags, "patch"), "2.3.5")

    def test_invalid_version_or_bump_is_explicit(self):
        with self.assertRaises(ValueError):
            next_version([], "auto")
        for value in ("v1.0.0", "01.0.0", "1.0.0\n", "1.0.0-rc1", "", "1; exit"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_version(value)
        self.assertEqual(validate_version("1.2.3"), "1.2.3")

    def test_frozen_report_requires_dependencies_ui_version_and_no_loaded_save(self):
        report = {"version": "1.2.3", "frozen": True, "save_loaded": False,
                  "title": "Enshrouded Character Workshop", "tk": 8.6,
                  "yaml": "6.0.2", "zstandard": "0.25.0"}
        validate_smoke_report(report, "1.2.3")
        for key, value in (("frozen", False), ("save_loaded", True), ("version", "1.2.2"),
                           ("tk", 0), ("yaml", ""), ("zstandard", ""), ("title", "")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_smoke_report({**report, key: value}, "1.2.3")


if __name__ == "__main__":
    unittest.main()
