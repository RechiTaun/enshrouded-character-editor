import gc
from pathlib import Path
import sys
import tempfile
from threading import Event, get_ident
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_editor import Editor
from game_rules import GameRules
from save_format import SaveError
from test_save_format import sample
from tk_test_support import create_test_root


class LoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "characters-2"
        self.source.write_bytes(sample())
        self.index = self.folder / "characters-index"
        self.index.write_text('{"latest":2,"deleted":false}', encoding="utf-8")
        self.root = create_test_root()
        self.root.withdraw()
        self.root_closed = False
        self.addCleanup(self.destroy_root)
        self.editor = Editor(self.root)
        self.editor.auto_rules_attempted = True
        self.editor.load(self.index)
        self.main_thread = get_ident()
        self.release = Event()
        self.addCleanup(self.release.set)
        self.root.update()

    def destroy_root(self):
        if not self.root_closed:
            self.root.destroy()
            self.root_closed = True

    def pump_until(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition():
            self.root.update()
            if time.monotonic() > deadline:
                self.fail("Loading did not reach the expected state.")
            time.sleep(0.005)
        self.root.update()

    def wait_loading(self):
        self.pump_until(lambda: not self.editor.loading)

    def slow_reader(self, started, error=None):
        read = self.editor._read_save

        def blocked(path, report):
            self.assertNotEqual(get_ident(), self.main_thread)
            report("Reading synthetic slow save...")
            started.set()
            if not self.release.wait(5):
                raise SaveError("Test worker timed out.")
            if error is not None:
                raise error
            return read(path, report)
        return blocked

    def test_slow_open_keeps_progress_and_heartbeat_responsive(self):
        started, heartbeat = Event(), Event()
        installed = []
        install = self.editor._install_save

        def record_install(loaded):
            installed.append(get_ident())
            return install(loaded)

        original = self.editor.document
        with patch.object(self.editor, "_read_save", side_effect=self.slow_reader(started)), \
                patch.object(self.editor, "_install_save", side_effect=record_install):
            self.editor.load_async(self.index)
            self.root.after(10, heartbeat.set)
            self.pump_until(lambda: started.is_set() and heartbeat.is_set() and
                            "synthetic slow" in self.editor.loading_phase.get())
            self.assertTrue(self.editor.loading)
            self.assertEqual(self.editor.loading_bar.winfo_manager(), "grid")
            self.assertTrue(self.editor.name_entry.instate(("disabled",)))
            self.assertTrue(self.editor.character_choice.instate(("disabled",)))
            self.assertTrue(self.editor.apply_button.instate(("disabled",)))
            self.assertIs(self.editor.document, original)
            self.release.set()
            self.wait_loading()
        self.assertEqual(installed, [self.main_thread])
        self.assertEqual(self.editor.loading_bar.winfo_manager(), "")
        self.assertEqual(self.editor.loading_phase.get(), "")
        self.assertFalse(self.editor.name_entry.instate(("disabled",)))
        self.assertEqual(str(self.editor.level_entry["state"]), "readonly")
        self.assertIsNot(self.editor.document, original)
        self.assertEqual(self.source.read_bytes(), sample())

    def test_failure_restores_controls_and_preserves_staged_document(self):
        self.editor.name.set("Abcd")
        self.editor.stage_character()
        original = self.editor.document
        changes = dict(original.changes)
        started = Event()
        with patch.object(self.editor, "_read_save",
                          side_effect=self.slow_reader(started, RuntimeError("Worker failed"))), \
                patch("character_editor.messagebox.askyesno", return_value=True), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.load_async(self.index)
            self.pump_until(started.is_set)
            self.release.set()
            self.wait_loading()
        error.assert_called_once()
        self.assertIn("Worker failed", str(error.call_args))
        self.assertIs(self.editor.document, original)
        self.assertEqual(original.changes, changes)
        self.assertEqual(self.editor.name.get(), "Abcd")
        self.assertFalse(self.editor.name_entry.instate(("disabled",)))
        self.assertFalse(self.editor.export_button.instate(("disabled",)))
        self.assertEqual(self.editor.loading_bar.winfo_manager(), "")

    def test_duplicate_requests_and_writes_are_rejected(self):
        started = Event()
        original = self.editor.document
        with patch.object(self.editor, "_read_save", side_effect=self.slow_reader(started)) as read, \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.load_async(self.index)
            self.pump_until(started.is_set)
            self.editor.load_async(self.index)
            self.editor.find_saves()
            self.editor.apply()
            self.editor.stage_character()
            self.assertEqual(error.call_count, 4)
            self.assertEqual(read.call_count, 1)
            self.assertFalse(original.changes)
            self.assertEqual(self.source.read_bytes(), sample())
            self.release.set()
            self.wait_loading()

    def test_discard_denial_does_not_start_worker(self):
        self.editor.name.set("Abcd")
        self.editor.stage_character()
        with patch("character_editor.messagebox.askyesno", return_value=False), \
                patch.object(self.editor, "_read_save") as read:
            self.editor.load_async(self.index)
        read.assert_not_called()
        self.assertFalse(self.editor.loading)
        self.assertEqual(self.editor.loading_bar.winfo_manager(), "")

    def test_find_single_save_runs_discovery_off_thread_then_loads(self):
        threads = []
        def discover():
            threads.append(get_ident())
            return [self.index]
        with patch("character_editor.discover_saves", side_effect=discover):
            original = self.editor.document
            self.editor.find_saves()
            self.pump_until(lambda: not self.editor.loading and self.editor.document is not original)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], self.main_thread)

    def test_no_saves_and_discovery_errors_clear_progress(self):
        with patch("character_editor.discover_saves", return_value=[]), \
                patch("character_editor.messagebox.showinfo") as info:
            self.editor.find_saves()
            self.wait_loading()
        info.assert_called_once()
        self.assertIn("No saves found", self.editor.status.get())
        with patch("character_editor.discover_saves", side_effect=OSError("Discovery failed")), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.find_saves()
            self.wait_loading()
        error.assert_called_once()
        self.assertIn("Discovery failed", self.editor.status.get())
        self.assertFalse(self.editor.name_entry.instate(("disabled",)))

    def test_multiple_accounts_offer_chooser_without_loading(self):
        with patch("character_editor.discover_saves", return_value=[self.index, self.index]), \
                patch.object(self.editor, "load_async") as load:
            self.editor.find_saves()
            self.wait_loading()
            chooser = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
            chooser.winfo_children()[0].invoke()
        load.assert_called_once_with(self.index)

    def test_open_index_and_snapshot_use_async_route(self):
        with patch("character_editor.filedialog.askopenfilename", return_value=str(self.index)), \
                patch.object(self.editor, "load_async") as load:
            self.editor.open_index()
            load.assert_called_once_with(self.index)
        with patch("character_editor.filedialog.askopenfilename", return_value=str(self.source)), \
                patch.object(self.editor, "load_async") as load:
            self.editor.open_snapshot()
            load.assert_called_once_with(self.source)

    def test_manual_rules_prepare_in_worker_and_failure_keeps_old_rules(self):
        old = Mock(spec=GameRules)
        self.editor.game_rules = old
        started = Event()

        def failed_open(_folder):
            self.assertNotEqual(get_ident(), self.main_thread)
            started.set()
            if not self.release.wait(5):
                raise SaveError("Test worker timed out.")
            raise SaveError("Unsupported installation")

        with patch("character_editor.filedialog.askdirectory", return_value=str(self.folder)), \
                patch("character_editor.GameRules.open", side_effect=failed_open), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.load_game_rules()
            self.pump_until(started.is_set)
            self.assertTrue(self.editor.loading)
            self.release.set()
            self.wait_loading()
        error.assert_called_once()
        self.assertIs(self.editor.game_rules, old)
        self.assertIn("Unsupported installation", self.editor.rules_status.get())

    def test_changed_source_during_load_cannot_replace_document(self):
        started = Event()
        warm_started = Event()
        original = self.editor.document
        old = Mock(spec=GameRules)
        self.editor.game_rules = old
        read = self.editor._read_save

        def reading(path, report):
            result = read(path, report)
            started.set()
            return result

        def warm():
            warm_started.set()
            if not self.release.wait(5):
                raise SaveError("Test worker timed out.")
            self.index.write_text('{"latest":1,"deleted":false}', encoding="utf-8")

        old.item_catalogue.side_effect = warm
        with patch.object(self.editor, "_read_save", side_effect=reading), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.load_async(self.index)
            self.pump_until(lambda: started.is_set() and warm_started.is_set())
            self.release.set()
            self.wait_loading()
        error.assert_called_once()
        self.assertIs(self.editor.document, original)

    def test_close_stops_polling_and_never_installs_late_result(self):
        started = Event()
        with patch.object(self.editor, "_read_save", side_effect=self.slow_reader(started)), \
                patch.object(self.editor, "_install_save") as install:
            self.editor.load_async(self.index)
            self.pump_until(started.is_set)
            future = self.editor._loading_future
            self.editor.close()
            self.root_closed = True
            self.release.set()
            self.assertIsInstance(future.exception(timeout=5), SaveError)
        install.assert_not_called()
        self.assertTrue(self.editor._closed)
        self.assertIsNone(self.editor._loading_after)
        self.assertFalse(self.editor.loading)

    def test_worker_does_not_retain_closed_tk_objects(self):
        started = Event()
        release = self.release

        def work(report):
            report("Blocked read-only worker")
            started.set()
            if not release.wait(5):
                raise SaveError("Test worker timed out.")

        self.editor._start_loading(work, lambda _result: None, "Loading...")
        self.pump_until(started.is_set)
        future = self.editor._loading_future
        reference = weakref.ref(self.editor)
        self.editor.close()
        self.root_closed = True
        self.editor = None
        gc.collect()
        self.assertIsNone(reference())
        self.release.set()
        self.assertIsInstance(future.exception(timeout=5), SaveError)

    def test_automatic_rule_failure_still_installs_inspectable_save(self):
        self.editor.auto_rules_attempted = False
        original = self.editor.document
        with patch("character_editor.discover_game_directories", return_value=[self.folder]), \
                patch("character_editor.GameRules.open", side_effect=SaveError("Invalid rules")), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.load_async(self.index)
            self.wait_loading()
        error.assert_called_once()
        self.assertIsNot(self.editor.document, original)
        self.assertTrue(self.editor.auto_rules_attempted)
        self.assertIsNone(self.editor.game_rules)
        self.assertIn("Invalid rules", self.editor.rules_status.get())
        self.assertEqual(str(self.editor.level_entry["state"]), "readonly")


if __name__ == "__main__":
    unittest.main()
