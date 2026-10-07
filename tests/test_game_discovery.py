from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from game_rules import discover_game_directories
from save_format import SaveError


class GameDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.steam = self.base / "Steam"
        (self.steam / "steamapps").mkdir(parents=True)
        roots = patch("game_rules.steam_roots", return_value=[self.steam])
        roots.start()
        self.addCleanup(roots.stop)

    def install(self, library, directory="Enshrouded"):
        apps = library / "steamapps"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "appmanifest_1203620.acf").write_text(
            f'"AppState" {{ "appid" "1203620" "installdir" "{directory}" }}',
            encoding="utf-8")
        game = apps / "common" / directory
        game.mkdir(parents=True)
        for name in ("enshrouded.exe", "enshrouded.kfc", "enshrouded.kfc_resources"):
            (game / name).write_bytes(b"synthetic discovery placeholder")
        return game.resolve()

    def libraries(self, paths, modern=True):
        entries = []
        for i, path in enumerate(paths):
            value = str(path).replace("\\", "\\\\")
            entries.append(f'"{i}" {{ "path" "{value}" "apps" {{ "1203620" "1" }} }}'
                           if modern else f'"{i}" "{value}"')
        (self.steam / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders" { ' + " ".join(entries) + " }", encoding="utf-8")

    def test_secondary_library_and_escaped_windows_paths(self):
        library = self.base / "Another library"
        expected = self.install(library)
        self.libraries([self.steam, library, library])
        self.assertEqual(discover_game_directories(), [expected])

    def test_default_library_without_library_index(self):
        self.assertEqual(discover_game_directories(), [])
        expected = self.install(self.steam)
        self.assertEqual(discover_game_directories(), [expected])

    def test_legacy_libraries_and_multiple_installations(self):
        other = self.base / "Other"
        expected = [self.install(self.steam), self.install(other)]
        self.libraries([other], modern=False)
        self.assertEqual(discover_game_directories(), sorted(expected))

    def test_incomplete_installation_is_explicit(self):
        game = self.install(self.steam)
        (game / "enshrouded.kfc").unlink()
        with self.assertRaisesRegex(SaveError, "incomplete"):
            discover_game_directories()

    def test_corrupt_manifest_and_path_traversal_are_rejected(self):
        manifest = self.steam / "steamapps" / "appmanifest_1203620.acf"
        for value in ('"AppState" {', '"AppState" { "appid" "1" "installdir" "Enshrouded" }',
                      '"AppState" { "appid" "1203620" "installdir" ".." }',
                      '"AppState" { "appid" "1203620" "installdir" "C:\\\\private" }',
                      '"AppState" { "appid" "1203620" "installdir" "../private" }'):
            manifest.write_text(value, encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(SaveError):
                discover_game_directories()

    def test_invalid_library_index_and_relative_path_reject(self):
        index = self.steam / "steamapps" / "libraryfolders.vdf"
        for value in ('"wrong" {}', '"libraryfolders" { "0" { "path" "relative" } }'):
            index.write_text(value, encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(SaveError):
                discover_game_directories()

    def test_permission_errors_are_not_hidden(self):
        self.install(self.steam)
        with patch("game_rules.read_limited", side_effect=PermissionError("denied")), \
                self.assertRaisesRegex(PermissionError, "denied"):
            discover_game_directories()


if __name__ == "__main__":
    unittest.main()
