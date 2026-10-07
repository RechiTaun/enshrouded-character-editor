from pathlib import Path
import gc
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_save_format import container, sample
from test_player_state import character, knowledge, payload
from test_steam_storage import catalogue
from test_game_rules import progression_rules
from character_editor import Editor
from game_rules import GameRules, ItemRule, PointBudget
from player_state import PlayerState
from save_format import Bdb, KNOW, SaveDocument, SaveError
from tk_test_support import create_test_root


class UiTests(unittest.TestCase):
    def wait_loading(self):
        deadline = time.monotonic() + 10
        while self.editor.loading:
            self.root.update()
            if time.monotonic() > deadline:
                self.fail("Background loading did not finish.")
            time.sleep(0.005)
        self.root.update()

    def setUp(self):
        # Collect Tcl-owning dialog-mock cycles on the UI thread, not a worker.
        gc.collect()
        self.addCleanup(gc.collect)
        discovery = patch("character_editor.discover_game_directories", return_value=[])
        discovery.start()
        self.addCleanup(discovery.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "characters-2"
        self.source.write_bytes(sample())
        self.index = self.directory / "characters-index"
        self.index.write_text('{"latest":2,"deleted":false}', encoding="utf-8")
        self.root = create_test_root()
        self.addCleanup(self.root.destroy)
        self.editor = Editor(self.root)
        self.editor.load(self.index)
        self.root.update()

    def test_load_character_stage_switch_discard(self):
        self.assertEqual(self.editor.name.get(), "Hero")
        self.assertEqual(len(self.editor.owner_ids), 2)
        self.editor.name.set("Abcd")
        self.editor.stage_character()
        self.assertEqual(len(self.editor.document.changes), 1)
        self.assertEqual(str(self.editor.apply_button["state"]), "normal")
        self.editor.character_choice.current(1)
        self.editor.select_character()
        self.assertEqual(self.editor.name.get(), "Mage")
        with patch("character_editor.messagebox.askyesno", return_value=True):
            self.editor.discard()
        self.assertFalse(self.editor.document.changes)
        self.assertEqual(self.source.read_bytes(), sample())

    def test_toolbar_keeps_discovery_without_manual_open_buttons(self):
        toolbar = self.editor.character_choice.master
        labels = [str(widget["text"]) for widget in toolbar.winfo_children()
                  if "text" in widget.keys()]
        self.assertEqual(labels, ["Find saves"])
        button = next(widget for widget in toolbar.winfo_children()
                      if "text" in widget.keys())
        with patch("character_editor.discover_saves", return_value=[self.index]) as discover:
            button.invoke()
            self.wait_loading()
        discover.assert_called_once()
        self.assertEqual(self.editor.document.index_path, self.index)

    def test_empty_discovery_does_not_recommend_removed_buttons(self):
        with patch("character_editor.messagebox.showinfo") as info:
            self.editor._found_saves([])
        self.assertIn("characters-index path", self.editor.status.get())
        self.assertNotIn("Open active index", self.editor.status.get())
        info.assert_called_once()

    def prepare_sharing(self):
        from test_character_transfer import records, transfer_rules
        values = [(owner, tag,
                   Bdb(data).patch_character("Home", 25) if tag == b"CHAR"
                   else b"FGOW\x00recipient-map" if tag == b"FOWR" else data)
                  for owner, tag, data in records(789)]
        self.source.write_bytes(container(values + [(0, b"KNPL", b"recipient-global")]))
        self.editor.load(self.index)
        rules = transfer_rules()
        self.editor.game_rules = rules
        self.editor.refresh_player_state = Mock(return_value=True)
        self.editor.refresh_sharing_controls()
        return rules

    def test_character_sharing_controls_export_without_edits(self):
        from character_transfer import EXTENSION, read_character
        rules = self.prepare_sharing()
        self.assertEqual(str(self.editor.character_export_button["state"]), "normal")
        self.assertEqual(str(self.editor.character_import_button["state"]), "normal")
        # Share exports must be outside the source save folder.
        with tempfile.TemporaryDirectory() as outside:
            destination = Path(outside) / ("hero" + EXTENSION)
            before = self.source.read_bytes()
            with patch("character_editor.filedialog.asksaveasfilename",
                       return_value=str(destination)), \
                    patch("character_editor.messagebox.showerror") as error:
                self.editor.export_selected_character()
                self.assertTrue(self.editor.loading)
                self.wait_loading()
            error.assert_not_called()
            self.assertEqual(read_character(destination, rules).owner, 789)
            self.assertEqual(self.source.read_bytes(), before)
            self.assertFalse(self.editor.document.changes)

    def test_character_import_stages_selects_and_discard_restores_original(self):
        from test_character_transfer import records
        from character_transfer import EXTENSION, export_character
        rules = self.prepare_sharing()
        incoming = self.directory / ("shared" + EXTENSION)
        incoming.write_bytes(export_character(SaveDocument(container(records())), 123, rules))
        original = self.source.read_bytes()
        self.editor.name.set("Abcd")
        self.assertTrue(self.editor.stage_character())
        with patch("character_editor.filedialog.askopenfilename", return_value=str(incoming)), \
                patch("character_editor.messagebox.askyesno", return_value=True) as confirm, \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.import_character()
            self.assertTrue(self.editor.loading)
            self.assertTrue(self.editor.character_import_button.instate(("disabled",)))
            self.wait_loading()
        error.assert_not_called()
        confirm.assert_called_once()
        self.assertIn("level 25", str(confirm.call_args))
        owner = self.editor.owner
        self.assertNotIn(owner, (None, 0, 123, 789))
        self.assertEqual(set(self.editor.owner_ids), {owner, 789})
        self.assertEqual(self.editor.name.get(), "Hero")
        self.assertEqual(len(self.editor.blob_indices), 9)
        result = SaveDocument(self.editor.document.serialize())
        self.assertEqual(Bdb(result.blob(789, b"CHAR").data).character()[0], "Abcd")
        self.assertIn("Import character", self.editor.pending_text.get("1.0", "end"))
        self.assertEqual(self.source.read_bytes(), original)
        with patch("character_editor.messagebox.askyesno", return_value=True):
            self.editor.discard()
        self.assertEqual(self.editor.owner_ids, [789])
        self.assertFalse(self.editor.document.changes)

    def test_character_import_cancellation_and_bad_file_preserve_selection_and_edits(self):
        from test_character_transfer import records
        from character_transfer import EXTENSION, export_character
        rules = self.prepare_sharing()
        incoming = self.directory / ("shared" + EXTENSION)
        valid = export_character(SaveDocument(container(records())), 123, rules)
        self.editor.name.set("Abcd")
        self.assertTrue(self.editor.stage_character())
        before = self.editor.document.serialize()
        changes = dict(self.editor.document.changes)
        for data, accepted in ((valid, False), (b"invalid-share", True)):
            incoming.write_bytes(data)
            with patch("character_editor.filedialog.askopenfilename", return_value=str(incoming)), \
                    patch("character_editor.messagebox.askyesno", return_value=accepted), \
                    patch("character_editor.messagebox.showerror") as error:
                self.editor.import_character()
                self.wait_loading()
            self.assertEqual(error.call_count, 0 if not accepted else 1)
            self.assertEqual(self.editor.document.serialize(), before)
            self.assertEqual(self.editor.document.changes, changes)
            self.assertEqual(self.editor.owner, 789)
            self.assertFalse(self.editor.character_import_button.instate(("disabled",)))

    def test_character_sharing_dialog_cancellation_and_busy_guard(self):
        self.prepare_sharing()
        with patch("character_editor.filedialog.askopenfilename", return_value="") as opening, \
                patch("character_editor.filedialog.asksaveasfilename", return_value="") as saving:
            self.editor.import_character()
            self.editor.export_selected_character()
            opening.assert_called_once()
            saving.assert_called_once()
            self.assertFalse(self.editor.loading)
        with patch.object(Editor, "loading", new_callable=lambda: property(lambda _self: True)), \
                patch("character_editor.filedialog.askopenfilename") as opening, \
                patch("character_editor.filedialog.asksaveasfilename") as saving, \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.import_character()
            self.editor.export_selected_character()
            opening.assert_not_called()
            saving.assert_not_called()
            self.assertEqual(error.call_count, 2)

    def test_duplicate_character_import_asks_rename_and_adds_independent_copy(self):
        from character_transfer import EXTENSION, export_character
        self.prepare_sharing()
        document = self.editor.document
        original = document.serialize()
        incoming = self.directory / ("same-character" + EXTENSION)
        incoming.write_bytes(export_character(document, 789, self.editor.game_rules))
        with patch("character_editor.filedialog.askopenfilename", return_value=str(incoming)), \
                patch("character_editor.simpledialog.askstring",
                      return_value="My independent longer copy") as rename, \
                patch("character_editor.messagebox.askyesno", return_value=True) as confirm, \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.import_character()
            self.wait_loading()
        rename.assert_called_once()
        confirm.assert_called_once()
        error.assert_not_called()
        copied = self.editor.owner
        self.assertNotIn(copied, (0, 789))
        self.assertEqual(set(self.editor.owner_ids), {789, copied})
        self.assertEqual(self.editor.name.get(), "My independent longer copy")
        self.assertEqual(Bdb(document.blob(789, b"CHAR").data).character()[0], "Home")
        for old in SaveDocument(original).blobs:
            self.assertEqual(document.blob(old.owner, old.tag).data, old.data)
            self.assertEqual(document.blob(old.owner, old.tag).compressed, old.compressed)
        self.assertEqual(self.source.read_bytes(), original)
        with patch("character_editor.filedialog.askopenfilename", return_value=str(incoming)), \
                patch("character_editor.simpledialog.askstring", return_value="A second copy"), \
                patch("character_editor.messagebox.askyesno", return_value=True), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.import_character()
            self.wait_loading()
        error.assert_not_called()
        self.assertNotIn(self.editor.owner, (0, 789, copied))
        self.assertEqual(len(self.editor.owner_ids), 3)

    def test_duplicate_rename_cancel_error_and_confirmation_cancel_do_not_add(self):
        from character_transfer import EXTENSION, export_character
        self.prepare_sharing()
        incoming = self.directory / ("same-character" + EXTENSION)
        incoming.write_bytes(export_character(
            self.editor.document, 789, self.editor.game_rules))
        self.editor.name.set("Abcd")
        self.assertTrue(self.editor.stage_character())
        before = self.editor.document.serialize()
        changes = dict(self.editor.document.changes)
        for name, confirm_result, expected_errors in (
                (None, True, 0), ("HOME", True, 1),
                ("A renamed copy", False, 0), ("bad\nname", True, 1)):
            with patch("character_editor.filedialog.askopenfilename", return_value=str(incoming)), \
                    patch("character_editor.simpledialog.askstring", return_value=name) as rename, \
                    patch("character_editor.messagebox.askyesno",
                          return_value=confirm_result) as confirm, \
                    patch("character_editor.messagebox.showerror") as error:
                self.editor.import_character()
                self.wait_loading()
            rename.assert_called_once()
            self.assertEqual(confirm.call_count, 1 if name == "A renamed copy" else 0)
            self.assertEqual(error.call_count, expected_errors)
            self.assertEqual(self.editor.document.serialize(), before)
            self.assertEqual(self.editor.document.changes, changes)
            self.assertEqual(self.editor.owner, 789)
            self.assertFalse(self.editor.character_import_button.instate(("disabled",)))

    def test_armor_picker_defaults_bounds_staging_and_plain_reset(self):
        from test_armor_editing import armor_character, mocked_rules
        self.source.write_bytes(container([(123, b"CHAR", armor_character())]))
        self.editor.load(self.index)
        rules = mocked_rules()
        rules.progression.side_effect = SaveError("Synthetic progression unavailable")
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        choices = [label for label in self.editor.addable_rules if "mage legs" in label]
        self.assertEqual(len(choices), 1)
        self.editor.item_choice.set(choices[0])
        self.editor.select_add_item()
        self.assertEqual(self.editor.armor_level.get(), "30")
        self.assertEqual(self.editor.armor_rarity.get(), "Rare")
        self.assertEqual(float(self.editor.armor_level_entry["from"]), 30)
        self.assertEqual(float(self.editor.armor_level_entry["to"]), 50)
        self.assertEqual(str(self.editor.armor_rarity["state"]), "readonly")
        self.editor.armor_level.set("50")
        self.editor.armor_rarity.set("Legendary")
        self.editor.add_quantity.set("1")
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.add_inventory()
        error.assert_not_called()
        self.assertTrue(any("level 50, Legendary" in detail
                            for detail in self.editor.document.changes.values()))
        self.assertEqual(self.source.read_bytes(),
                         container([(123, b"CHAR", armor_character())]))
        plain = next(label for label in self.editor.addable_rules if "material" in label)
        self.editor.item_choice.set(plain)
        self.editor.select_add_item()
        self.assertEqual(str(self.editor.armor_level_entry["state"]), "disabled")
        self.assertEqual(str(self.editor.armor_rarity["state"]), "disabled")
        self.editor.item_choice.set("mage")
        self.editor.filter_item_choices()
        self.assertEqual(str(self.editor.armor_rarity["state"]), "disabled")

    def test_armor_picker_invalid_level_is_atomic_and_visible(self):
        from test_armor_editing import armor_character, mocked_rules
        self.source.write_bytes(container([(123, b"CHAR", armor_character())]))
        self.editor.load(self.index)
        rules = mocked_rules()
        rules.progression.side_effect = SaveError("Synthetic progression unavailable")
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.editor.item_choice.set(next(
            label for label in self.editor.addable_rules if "mage legs" in label))
        self.editor.select_add_item()
        self.editor.armor_level.set("29")
        self.editor.add_quantity.set("1")
        original = self.editor.document.serialize()
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.add_inventory()
        error.assert_called_once()
        self.assertIn("between 30 and 50", str(error.call_args))
        self.assertEqual(self.editor.document.serialize(), original)

    def test_missing_perk_schema_is_automatic_without_file_dialog(self):
        from test_armor_editing import armor_character, mocked_rules
        from test_component_schema import generated_definition
        original = container([(123, b"CHAR", armor_character(missing_perks=True))])
        self.source.write_bytes(original)
        self.editor.load(self.index)
        rules = mocked_rules()
        rules.perk_component_definition.return_value = generated_definition()
        rules.progression.side_effect = SaveError("Synthetic progression unavailable")
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.editor.item_choice.set(next(
            label for label in self.editor.addable_rules if "mage legs" in label))
        self.editor.select_add_item()
        self.editor.add_quantity.set("1")
        with patch("character_editor.messagebox.showerror") as error, \
                patch("character_editor.filedialog.askopenfilename") as chooser:
            self.editor.add_inventory()
        error.assert_not_called()
        chooser.assert_not_called()
        rules.perk_component_definition.assert_called_once()
        self.assertIn("generated missing PerkContainerNew",
                      self.editor.pending_text.get("1.0", "end"))
        self.assertEqual(self.source.read_bytes(), original)

    def test_armor_metadata_failure_keeps_plain_picker_available(self):
        from test_armor_editing import armor_character, mocked_rules
        self.source.write_bytes(container([(123, b"CHAR", armor_character())]))
        self.editor.load(self.index)
        rules = mocked_rules()
        rules.progression.side_effect = SaveError("Synthetic progression unavailable")
        rules.armor_catalogue.side_effect = SaveError("Unsupported armor metadata")
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.assertEqual(len(self.editor.addable_rules), 1)
        self.assertIn("Unsupported armor metadata", self.editor.inventory_info.get())
        self.assertEqual(str(self.editor.item_add["state"]), "normal")

    def test_load_prefers_named_character_over_appearance_only_owner(self):
        original = SaveDocument(sample())
        self.source.write_bytes(container([
            (1, b"COUT", b"opaque appearance"),
            (123, b"CHAR", original.blob(123, b"CHAR").data),
        ]))
        self.editor.load(self.index)
        self.assertEqual(self.editor.owner, 123)
        self.assertEqual(self.editor.name.get(), "Hero")
        self.assertEqual(str(self.editor.stage_button["state"]), "normal")

    def test_invalid_level_shows_error(self):
        self.editor.level.set("-1")
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.stage_character()
        error.assert_called_once()
        self.assertFalse(self.editor.document.changes)

    def test_valid_level_change_is_also_blocked_and_entry_is_readonly(self):
        self.assertEqual(str(self.editor.level_entry["state"]), "readonly")
        self.editor.level.set("30")
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.stage_character()
        error.assert_called_once()
        self.assertIn("2 points", str(error.call_args))
        self.assertFalse(self.editor.document.changes)

    def test_progress_selection_and_staging(self):
        self.editor.knowledge_tree.selection_set("7")
        self.editor.select_knowledge()
        self.editor.progress_value.set("10")
        with patch("character_editor.messagebox.askyesno", return_value=True):
            self.editor.stage_knowledge()
        self.assertEqual(len(self.editor.document.changes), 1)
        self.assertIn("KNOW", self.editor.pending_text.get("1.0", "end"))

    def test_export_through_ui(self):
        self.editor.name.set("Abcd")
        self.editor.stage_character()
        with tempfile.TemporaryDirectory() as output:
            destination = Path(output) / "edited.ksc"
            with patch("character_editor.messagebox.askyesno", return_value=True), \
                    patch("character_editor.filedialog.asksaveasfilename",
                          return_value=str(destination)), \
                    patch("character_editor.messagebox.showinfo"):
                self.editor.export()
            edited = SaveDocument.open(destination)
            self.assertEqual(Bdb(edited.blob(123, b"CHAR").data).character(), ("Abcd", 25))
        self.assertEqual(self.source.read_bytes(), sample())

    def test_snapshot_disables_apply(self):
        self.editor.load(self.source)
        self.editor.name.set("Abcd")
        self.editor.stage_character()
        self.assertEqual(str(self.editor.apply_button["state"]), "disabled")

    def test_record_view_and_offset_error(self):
        self.assertIn("Named fields", self.editor.record_text.get("1.0", "end"))
        self.editor.hex_offset.set("999999")
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.show_blob()
        error.assert_called_once()

    def test_apply_through_ui_with_temporary_active_save(self):
        self.editor.name.set("Abcd")
        with tempfile.TemporaryDirectory() as backups:
            with patch("character_editor.messagebox.askyesno", return_value=True), \
                    patch("character_editor.automatic_backup_root",
                          return_value=Path(backups) / "automatic"), \
                    patch("character_editor.filedialog.askdirectory") as chooser, \
                    patch("character_editor.filedialog.asksaveasfilename") as save_chooser, \
                    patch("character_editor.messagebox.showinfo"), \
                    patch("save_format.assert_game_closed"):
                self.editor.apply()
            chooser.assert_not_called()
            save_chooser.assert_not_called()
            folders = list((Path(backups) / "automatic").iterdir())
            self.assertEqual(len(folders), 1)
            self.assertEqual((folders[0] / self.source.name).read_bytes(), sample())
            self.assertEqual((folders[0] / self.index.name).read_bytes(), self.index.read_bytes())
        self.assertEqual(self.editor.level.get(), "25")
        self.assertEqual(self.editor.name.get(), "Abcd")
        self.assertFalse(self.editor.document.changes)
        self.assertEqual(str(self.editor.apply_button["state"]), "normal")

    def test_direct_save_rejects_invalid_form_without_writing(self):
        self.editor.level.set("-1")
        with patch("character_editor.messagebox.showerror") as error, \
                patch("character_editor.automatic_backup_root") as backup_root:
            self.editor.apply()
        error.assert_called_once()
        backup_root.assert_not_called()
        self.assertEqual(self.source.read_bytes(), sample())

    def test_direct_save_backup_failure_does_not_overwrite(self):
        self.editor.name.set("Abcd")
        occupied = self.directory / "not-a-directory"
        occupied.write_text("occupied", encoding="utf-8")
        with patch("character_editor.messagebox.askyesno", return_value=True), \
                patch("character_editor.automatic_backup_root", return_value=occupied), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.apply()
        error.assert_called_once()
        self.assertEqual(self.source.read_bytes(), sample())
        self.assertTrue(self.editor.document.changes)

    def test_direct_save_blocks_steam_folder_before_staging_or_backup(self):
        remote = self.directory / "1203620" / "remote"
        remote.mkdir(parents=True)
        (remote / self.source.name).write_bytes(sample())
        index = remote / "characters-index"
        index.write_bytes(self.index.read_bytes())
        self.editor.load(index)
        self.editor.name.set("Abcd")
        with patch("character_editor.messagebox.showerror") as error, \
                patch("character_editor.automatic_backup_root") as backup_root, \
                patch("character_editor.messagebox.askyesno") as confirm:
            self.editor.apply()
        error.assert_called_once()
        self.assertIn("No Steam catalogue", str(error.call_args))
        backup_root.assert_not_called()
        confirm.assert_not_called()
        self.assertFalse(self.editor.document.changes)
        self.assertEqual((remote / self.source.name).read_bytes(), sample())

    def load_player(self, raw=None):
        contents = container([(123, b"CHAR", character(raw)), (123, KNOW, knowledge()),
                              (456, b"CHAR", character())])
        self.source.write_bytes(contents)
        self.editor.load(self.index)
        rules = Mock(spec=GameRules)
        rules.armor_catalogue.return_value = {}
        rules.items_for.return_value = {1234: ItemRule(1234, 500, "Synthetic wood", True)}
        rules.addable_items.return_value = {
            1234: ItemRule(1234, 500, "Synthetic wood", True),
            5678: ItemRule(5678, 100, "Synthetic stone", True)}
        rules.progression.return_value = progression_rules()
        rules.point_budget.side_effect = lambda level, skills, knowledge: PointBudget(
            (level - 1) * 2, 57, sum(3 for skill in skills if skill.node_id))
        return contents, rules

    def test_inventory_rules_display_and_staging_preserve_other_owner_and_xp(self):
        original, rules = self.load_player()
        self.assertIn("1548 / 4252", self.editor.xp_summary.get())
        self.assertIn("read-only", self.editor.inventory_info.get())
        self.assertEqual(str(self.editor.inventory_stage["state"]), "disabled")
        with patch("character_editor.filedialog.askdirectory", return_value=str(self.directory)), \
                patch("character_editor.GameRules.open", return_value=rules):
            self.editor.load_game_rules()
            self.wait_loading()
        self.editor.inventory_tree.selection_set("100:0")
        self.editor.select_inventory()
        self.assertEqual(str(self.editor.inventory_stage["state"]), "normal")
        self.editor.quantity.set("499")
        self.editor.stage_inventory()
        self.assertIn("17 -> 499", self.editor.pending_text.get("1.0", "end"))
        self.assertEqual(self.source.read_bytes(), original)
        state = PlayerState(self.editor.document.blob(123, b"CHAR").data)
        self.assertEqual(state.inventory()[0].quantity, 499)
        self.assertEqual((state.experience().level, state.experience().current,
                          state.experience().required), (25, 1548, 4252))
        self.assertEqual(self.editor.document.blob(456, b"CHAR").data, character())
        self.assertIn("saved allocation", self.editor.skills_info.get())

    def test_inventory_limits_and_stale_game_rules_reject_without_changes(self):
        original, rules = self.load_player()
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.editor.inventory_tree.selection_set("100:0")
        self.editor.select_inventory()
        for quantity in ("0", "501", "invalid"):
            self.editor.quantity.set(quantity)
            with patch("character_editor.messagebox.showerror") as error:
                self.editor.stage_inventory()
            error.assert_called_once()
        rules.assert_unchanged.side_effect = SaveError("Game rules changed")
        self.editor.quantity.set("100")
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.stage_inventory()
        error.assert_called_once()
        self.assertFalse(self.editor.document.changes)
        self.assertEqual(self.source.read_bytes(), original)

    def test_stateful_inventory_is_disabled(self):
        _, rules = self.load_player(payload(stateful=True))
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.editor.inventory_tree.selection_set("100:0")
        self.editor.select_inventory()
        self.assertEqual(str(self.editor.inventory_stage["state"]), "disabled")
        self.assertEqual(str(self.editor.inventory_remove["state"]), "disabled")

    def test_item_combobox_add_remove_and_yaml_merge_staging(self):
        original, rules = self.load_player()
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.assertEqual(str(self.editor.item_add["state"]), "normal")
        self.editor.item_choice.set("stone")
        self.editor.filter_item_choices()
        choices = self.editor.item_choice["values"]
        self.assertEqual(len(choices), 1)
        self.editor.item_choice.set(choices[0])
        self.editor.add_quantity.set("10")
        self.editor.add_inventory()
        self.assertIn("100:1", self.editor.inventory_tree.get_children())
        self.editor.inventory_tree.selection_set("100:1")
        self.editor.select_inventory()
        with patch("character_editor.messagebox.askyesno", return_value=True):
            self.editor.remove_inventory()
        self.assertFalse(self.editor.document.changes)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.yaml"
            with patch("character_editor.filedialog.asksaveasfilename", return_value=str(path)):
                self.editor.export_inventory()
            self.assertTrue(path.is_file())
            with patch("character_editor.filedialog.askopenfilename", return_value=str(path)), \
                    patch("character_editor.messagebox.askyesno", return_value=True):
                self.editor.import_inventory()
        self.assertEqual(PlayerState(self.editor.document.blob(123, b"CHAR").data).inventory()[0].quantity, 34)
        self.assertEqual(self.source.read_bytes(), original)

    def test_arbitrary_combo_text_and_full_import_do_not_change_document(self):
        original, rules = self.load_player()
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.editor.item_choice.set("unknown item")
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.add_inventory()
        error.assert_called_once()
        with patch("character_editor.filedialog.askopenfilename", return_value="synthetic.yaml"), \
                patch("character_editor.read_inventory_yaml", return_value=[(5678, 201)]), \
                patch("character_editor.messagebox.askyesno", return_value=True), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.import_inventory()
        error.assert_called_once()
        self.assertFalse(self.editor.document.changes)
        self.assertEqual(self.source.read_bytes(), original)

    def test_unresolved_rules_do_not_report_success(self):
        _, rules = self.load_player()
        rules.items_for.side_effect = SaveError("Missing item definitions")
        with patch("character_editor.filedialog.askdirectory", return_value=str(self.directory)), \
                patch("character_editor.GameRules.open", return_value=rules), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.load_game_rules()
            self.wait_loading()
        error.assert_called_once()
        self.assertIn("Error: Missing item definitions", self.editor.status.get())
        self.assertEqual(str(self.editor.inventory_stage["state"]), "disabled")

    def test_level_editing_enabled_by_rules_with_coherent_preview_and_revert(self):
        original, rules = self.load_player(payload(skill_nodes=()))
        self.assertEqual(str(self.editor.level_entry["state"]), "readonly")
        with patch("character_editor.filedialog.askdirectory", return_value=str(self.directory)), \
                patch("character_editor.GameRules.open", return_value=rules):
            self.editor.load_game_rules()
            self.wait_loading()
        self.assertEqual(str(self.editor.level_entry["state"]), "normal")
        self.assertIn("105 earned", self.editor.progression_hint.get())
        self.editor.level.set("30")
        self.assertTrue(self.editor.stage_character())
        self.assertIn("1548 / 9467", self.editor.xp_summary.get())
        self.assertIn("115 earned", self.editor.progression_hint.get())
        self.assertIn("+10 level-earned", self.editor.pending_text.get("1.0", "end"))
        self.assertEqual(self.source.read_bytes(), original)
        self.editor.level.set("25")
        self.assertTrue(self.editor.stage_character())
        self.assertIn("1548 / 4252", self.editor.xp_summary.get())
        self.assertFalse(self.editor.document.changes)

    def test_level_direct_overwrite_includes_form_and_backup_without_destination(self):
        original, rules = self.load_player(payload(skill_nodes=()))
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.editor.level.set("30")
        with tempfile.TemporaryDirectory() as backups:
            backup_root = Path(backups) / "automatic"
            with patch("character_editor.messagebox.askyesno", return_value=True), \
                    patch("character_editor.automatic_backup_root", return_value=backup_root), \
                    patch("character_editor.filedialog.asksaveasfilename") as chooser, \
                    patch("character_editor.messagebox.showinfo"), \
                    patch("save_format.assert_game_closed"):
                self.editor.apply()
            chooser.assert_not_called()
            edited = SaveDocument.open(self.index)
            xp = PlayerState(edited.blob(123, b"CHAR").data).experience()
            self.assertEqual((xp.level, xp.current, xp.gain, xp.required), (30, 1548, 0, 9467))
            folder = next(backup_root.iterdir())
            self.assertEqual((folder / self.source.name).read_bytes(), original)
            self.assertEqual(edited.blob(123, KNOW).data, knowledge())

    def test_discard_clears_progression_rule_tracking(self):
        _, rules = self.load_player(payload(skill_nodes=()))
        self.editor.game_rules = rules
        self.editor.level.set("30")
        self.editor.stage_character()
        with patch("character_editor.messagebox.askyesno", return_value=True):
            self.editor.discard()
        self.assertFalse(self.editor.document.progression_sources)
        self.assertFalse(self.editor.document.rule_sources)
        self.assertIn("1548 / 4252", self.editor.xp_summary.get())

    def test_pending_xp_keeps_level_readonly_with_explicit_reason(self):
        _, rules = self.load_player(payload(xp=(1548, 1, 4252)))
        self.editor.game_rules = rules
        self.editor.refresh_player_state()
        self.assertEqual(str(self.editor.level_entry["state"]), "readonly")
        self.assertIn("pending XP", self.editor.progression_hint.get())
        self.editor.level.set("30")
        with patch("character_editor.messagebox.showerror") as error:
            self.editor.stage_character()
        error.assert_called_once()
        self.assertFalse(self.editor.document.changes)

    def test_rules_auto_load_on_open_without_directory_chooser(self):
        _, rules = self.load_player(payload(skill_nodes=()))
        self.editor.auto_rules_attempted = False
        with patch("character_editor.discover_game_directories", return_value=[self.directory]), \
                patch("character_editor.GameRules.open", return_value=rules) as opened, \
                patch("character_editor.filedialog.askdirectory") as chooser:
            self.editor.load(self.index)
        chooser.assert_not_called()
        opened.assert_called_once_with(self.directory)
        self.assertEqual(str(self.editor.level_entry["state"]), "normal")
        self.assertIn("105 earned", self.editor.progression_hint.get())
        self.assertIn("Game rules loaded from", self.editor.rules_status.get())
        with patch("character_editor.discover_game_directories") as discovery:
            self.editor.load(self.index)
        discovery.assert_not_called()

    def test_auto_discovery_failures_are_explicit_and_manual_selection_still_works(self):
        _, rules = self.load_player(payload(skill_nodes=()))
        for found, text in (([], "No Enshrouded"), ([self.directory, self.directory / "second"],
                                                 "Multiple Enshrouded")):
            self.editor.auto_rules_attempted = False
            with patch("character_editor.discover_game_directories", return_value=found):
                self.editor.load(self.index)
            self.assertIn(text, self.editor.rules_status.get())
            self.assertEqual(str(self.editor.level_entry["state"]), "readonly")
        self.editor.auto_rules_attempted = False
        with patch("character_editor.discover_game_directories", side_effect=SaveError("Invalid manifest")), \
                patch("character_editor.messagebox.showerror") as error:
            self.editor.load(self.index)
        error.assert_called_once()
        self.assertIn("Invalid manifest", self.editor.rules_status.get())
        with patch("character_editor.filedialog.askdirectory", return_value=str(self.directory)), \
                patch("character_editor.GameRules.open", return_value=rules):
            self.editor.load_game_rules()
            self.wait_loading()
        self.assertEqual(str(self.editor.level_entry["state"]), "normal")

    def test_cloud_profile_overwrite_has_no_destination_dialog_or_setting_change(self):
        remote = self.directory / "1203620" / "remote"
        remote.mkdir(parents=True)
        source = remote / self.source.name
        source.write_bytes(sample())
        index = remote / "characters-index"
        index.write_bytes(self.index.read_bytes())
        cache = remote.parent / "remotecache.vdf"
        original_cache = catalogue({source.name: sample(), index.name: index.read_bytes()})
        cache.write_bytes(original_cache)
        self.editor.load(index)
        self.editor.name.set("Abcd")
        with patch("character_editor.messagebox.askyesno", return_value=True) as confirm, \
                patch("character_editor.automatic_backup_root",
                      return_value=self.directory / "backups"), \
                patch("character_editor.filedialog.askdirectory") as directories, \
                patch("character_editor.filedialog.asksaveasfilename") as destinations, \
                patch("character_editor.messagebox.showinfo"), \
                patch("save_format.assert_game_closed"):
            self.editor.apply()
        directories.assert_not_called()
        destinations.assert_not_called()
        self.assertIn("Keep Steam Cloud ENABLED", confirm.call_args_list[-1].args[1])
        self.assertEqual(cache.read_bytes(), original_cache)
        self.assertEqual(Bdb(SaveDocument.open(source).blob(123, b"CHAR").data).character(),
                         ("Abcd", 25))


if __name__ == "__main__":
    unittest.main()
