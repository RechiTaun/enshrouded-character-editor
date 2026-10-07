from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_release_notes import SECTIONS, validate_notes


class ReleaseNotesTests(unittest.TestCase):
    def notes(self):
        return "# Workshop v1.2.3\n\n" + "\n\n".join(
            f"## {section}\n\nMeaningful release details." for section in SECTIONS)

    def test_complete_notes_and_committed_versions(self):
        validate_notes(self.notes(), "1.2.3")
        root = Path(__file__).resolve().parents[1] / "release-notes"
        for version in ("0.0.1", "0.0.2"):
            validate_notes((root / f"v{version}.md").read_text(encoding="utf-8"), version)

    def test_wrong_version_missing_empty_or_duplicate_sections_fail(self):
        variants = [self.notes().replace("v1.2.3", "v1.2.2"),
                    self.notes().replace("v1.2.3", "v1.2.30"),
                    self.notes().replace("v1.2.3", "v1.2.3-rc1"),
                    self.notes().replace("## Highlights", "## Other"),
                    self.notes().replace("## Highlights\n\nMeaningful release details.",
                                         "## Highlights\n\n"),
                    self.notes() + "\n\n## Highlights\n\nDuplicate."]
        for text in variants:
            with self.subTest(text=text[:50]), self.assertRaises(ValueError):
                validate_notes(text, "1.2.3")
