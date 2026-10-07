"""Local desktop UI. Start with: python character_editor.py [characters-index]."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Event
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, TypeVar

from app_version import VERSION

try:
    from save_format import Bdb, KNOW, Knowledge, SaveDocument, SaveError, discover_saves
    from game_rules import ArmorRule, GameRules, ItemRule, discover_game_directories
    from player_state import PlayerState, Stack
    from inventory_yaml import read_inventory_yaml, write_inventory_yaml
    from character_transfer import (CharacterImport, EXTENSION, export_character,
                                    read_character, write_character)
except ModuleNotFoundError as exc:
    if exc.name not in ("zstandard", "yaml"):
        raise
    raise SystemExit(
        "Missing editor dependency. Run: python -m pip install -r requirements.txt"
    ) from exc


def automatic_backup_root() -> Path:
    return Path.home() / "Enshrouded Character Workshop" / "backups"


Result = TypeVar("Result")
Progress = Callable[[str], None]


@dataclass(frozen=True)
class PreparedRules:
    rules: GameRules | None
    status: str
    error: OSError | SaveError | None = None
    item_error: OSError | SaveError | None = None
    armor_error: OSError | SaveError | None = None


@dataclass(frozen=True)
class PreparedSave:
    document: SaveDocument
    labels: list[str]
    first_editable: int
    rules: PreparedRules | None = None


class Editor(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=14)
        self.root = root
        self.document: SaveDocument | None = None
        self.owner_ids: list[int] = []
        self.owner: int | None = None
        self.blob_indices: list[int] = []
        self.editable = False
        self.game_rules: GameRules | None = None
        self.auto_rules_attempted = False
        self.item_rules: dict[int, ItemRule] = {}
        self.stacks: dict[str, Stack] = {}
        self.addable_rules: dict[str, ItemRule | ArmorRule] = {}
        self._loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="save-loader")
        self._loading_future: Future[None] | None = None
        self._loading_after: str | None = None
        self._loading_stop = Event()
        self._loading_phases: Queue[str] = Queue()
        self._loading_complete: Callable[[], None] | None = None
        self._loading_controls: list[tuple[ttk.Widget | tk.Text, bool]] = []
        self._closed = False
        root.title("Enshrouded Character Workshop")
        root.geometry("1120x780")
        root.minsize(880, 620)
        self.pack(fill="both", expand=True)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("TLabel", font=("Segoe UI", 10))

        ttk.Label(self, text="Enshrouded Character Workshop",
                  style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="Local only  |  Verified field patches  |  Backup before apply"
                  ).grid(row=1, column=0, sticky="w", pady=(2, 12))
        toolbar = ttk.Frame(self)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(toolbar, text="Open active index...", command=self.open_index
                   ).pack(side="left")
        ttk.Button(toolbar, text="Open snapshot...", command=self.open_snapshot
                   ).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Find saves", command=self.find_saves).pack(side="left")
        self.character_choice = ttk.Combobox(toolbar, state="readonly", width=43)
        self.character_choice.pack(side="right")
        self.character_choice.bind("<<ComboboxSelected>>", self.select_character)

        notebook = ttk.Notebook(self)
        notebook.grid(row=3, column=0, sticky="nsew")
        character = ttk.Frame(notebook, padding=15)
        progression = ttk.Frame(notebook, padding=15)
        inventory = ttk.Frame(notebook, padding=15)
        skills = ttk.Frame(notebook, padding=15)
        explorer = ttk.Frame(notebook, padding=15)
        pending = ttk.Frame(notebook, padding=15)
        for frame, label in ((character, "Character"), (inventory, "Inventory"),
                             (skills, "Skills (inspection)"), (progression, "Raw knowledge (advanced)"),
                             (explorer, "Records & appearance"), (pending, "Pending changes")):
            notebook.add(frame, text=label)
        character.columnconfigure(1, weight=1)
        self.summary = tk.StringVar(value="Open characters-index to load the active save.")
        ttk.Label(character, textvariable=self.summary, wraplength=900).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 20))
        self.name = tk.StringVar()
        self.level = tk.StringVar()
        ttk.Label(character, text="Character name").grid(row=1, column=0, sticky="w")
        self.name_entry = ttk.Entry(character, textvariable=self.name, width=42)
        self.name_entry.grid(row=1, column=1, sticky="ew", padx=10)
        self.name_hint = tk.StringVar()
        ttk.Label(character, textvariable=self.name_hint).grid(
            row=2, column=1, sticky="w", padx=10, pady=5)
        ttk.Label(character, text="Level").grid(row=3, column=0, sticky="w", pady=12)
        self.level_entry = ttk.Entry(character, textvariable=self.level, width=12)
        self.level_entry.grid(row=3, column=1, sticky="w", padx=10)
        self.xp_summary = tk.StringVar(value="Saved actor XP is unavailable.")
        ttk.Label(character, textvariable=self.xp_summary, wraplength=870).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.stage_button = ttk.Button(character, text="Stage character edits",
                                       command=self.stage_character)
        self.stage_button.grid(row=5, column=1, sticky="w", padx=10, pady=12)
        for widget in (self.name_entry, self.level_entry, self.stage_button):
            widget.configure(state="disabled")
        self.progression_hint = tk.StringVar(value=(
            "Load installed game rules to enable level increases with coherent XP "
            "and 2 skill points per gained level. Bonuses and learned skills are preserved."))
        ttk.Label(character, wraplength=870, textvariable=self.progression_hint).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=10)
        self.rules_status = tk.StringVar(
            value="Installed game rules are discovered automatically when you open a save.")
        ttk.Label(character, textvariable=self.rules_status, wraplength=870).grid(
            row=9, column=0, columnspan=2, sticky="w")
        ttk.Button(character, text="Load installed game rules...",
                   command=self.load_game_rules).grid(row=7, column=1, sticky="w", padx=10)
        ttk.Label(character, wraplength=870, text=(
            "Lowering levels, pending XP edits, unsupported item creation and skill allocation "
            "remain unsupported. Names must retain their UTF-8 byte length. "
            "Review staged edits before saving. In-game acceptance is not yet verified."
        )).grid(row=8, column=0, columnspan=2, sticky="w", pady=15)
        sharing = ttk.Frame(character)
        sharing.grid(row=10, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.character_export_button = ttk.Button(
            sharing, text="Export character...", command=self.export_selected_character,
            state="disabled")
        self.character_export_button.pack(side="left")
        self.character_import_button = ttk.Button(
            sharing, text="Import character (add)...", command=self.import_character,
            state="disabled")
        self.character_import_button.pack(side="left", padx=8)
        ttk.Label(character, wraplength=870, text=(
            "Share one complete character, including equipment and character-local map/quest data. "
            "Export includes staged edits; stage form edits first. Import adds a character "
            "without replacing existing ones, and stays pending until Save."
        )).grid(row=11, column=0, columnspan=2, sticky="w", pady=8)

        inventory.columnconfigure(0, weight=1)
        inventory.rowconfigure(2, weight=1)
        self.inventory_info = tk.StringVar(value="Open a supported character save.")
        ttk.Label(inventory, textvariable=self.inventory_info, wraplength=950).grid(
            row=0, column=0, sticky="w")
        ttk.Button(inventory, text="Load installed game rules...",
                   command=self.load_game_rules).grid(row=1, column=0, sticky="w", pady=10)
        self.inventory_tree = self.make_tree(
            inventory, ("Container / category", "Slot", "Item", "Quantity", "Limit", "Item entity"))
        self.inventory_tree.master.grid(row=2, column=0, sticky="nsew")
        self.inventory_tree.bind("<<TreeviewSelect>>", self.select_inventory)
        inventory_edit = ttk.Frame(inventory)
        inventory_edit.grid(row=3, column=0, sticky="w", pady=10)
        ttk.Label(inventory_edit, text="New quantity").pack(side="left")
        self.quantity = tk.StringVar()
        self.quantity_entry = ttk.Entry(inventory_edit, textvariable=self.quantity,
                                        width=12, state="disabled")
        self.quantity_entry.pack(side="left", padx=8)
        self.inventory_stage = ttk.Button(inventory_edit, text="Stage quantity",
                                          command=self.stage_inventory, state="disabled")
        self.inventory_stage.pack(side="left")
        self.inventory_remove = ttk.Button(
            inventory_edit, text="Remove selected item", command=self.remove_inventory, state="disabled")
        self.inventory_remove.pack(side="left", padx=8)
        self.inventory_hint = tk.StringVar(value="Select an existing stack.")
        ttk.Label(inventory, textvariable=self.inventory_hint, wraplength=950).grid(
            row=4, column=0, sticky="w")
        add_row = ttk.Frame(inventory)
        add_row.grid(row=5, column=0, sticky="ew", pady=10)
        ttk.Label(add_row, text="Add item (type to filter)").pack(side="left")
        self.item_choice = ttk.Combobox(add_row, width=48, state="disabled")
        self.item_choice.pack(side="left", padx=8)
        self.item_choice.bind("<KeyRelease>", self.filter_item_choices)
        self.item_choice.bind("<<ComboboxSelected>>", self.select_add_item)
        self.add_quantity = tk.StringVar(value="1")
        ttk.Entry(add_row, textvariable=self.add_quantity, width=8).pack(side="left")
        self.item_add = ttk.Button(add_row, text="Stage add", command=self.add_inventory,
                                   state="disabled")
        self.item_add.pack(side="left", padx=8)
        armor_row = ttk.Frame(inventory)
        armor_row.grid(row=6, column=0, sticky="w", pady=5)
        ttk.Label(armor_row, text="Armor level").pack(side="left")
        self.armor_level = tk.StringVar()
        self.armor_level_entry = ttk.Spinbox(
            armor_row, textvariable=self.armor_level, width=8, state="disabled")
        self.armor_level_entry.pack(side="left", padx=8)
        ttk.Label(armor_row, text="Rarity").pack(side="left")
        self.armor_rarity = ttk.Combobox(armor_row, width=14, state="disabled")
        self.armor_rarity.pack(side="left", padx=8)
        self.armor_hint = tk.StringVar(value="Select supported armor to choose level and rarity.")
        ttk.Label(armor_row, textvariable=self.armor_hint, wraplength=550).pack(side="left")
        yaml_row = ttk.Frame(inventory)
        yaml_row.grid(row=7, column=0, sticky="w", pady=5)
        self.inventory_export_button = ttk.Button(
            yaml_row, text="Export inventory YAML...", command=self.export_inventory, state="disabled")
        self.inventory_export_button.pack(side="left")
        self.inventory_import_button = ttk.Button(
            yaml_row, text="Import YAML (merge)...", command=self.import_inventory, state="disabled")
        self.inventory_import_button.pack(side="left", padx=8)
        ttk.Label(inventory, wraplength=950, text=(
            "Add/import fills matching stacks first, then accessible empty slots. "
            "YAML includes plain backpack/hotbar stacks only, not equipment or stateful items. "
            "Supported armor is added unequipped with zero unlocked upgrades. "
            "Equipment removal and YAML transfer remain protected. "
            "Changes are staged; Save applies them with a backup."
        )).grid(row=8, column=0, sticky="w", pady=8)

        skills.columnconfigure(0, weight=1)
        skills.rowconfigure(1, weight=1)
        self.skills_info = tk.StringVar(value=(
            "Open a supported character to inspect saved skill allocations/effects. "
            "Load installed rules for point accounting. Allocation/refund remains read-only."))
        ttk.Label(skills, textvariable=self.skills_info, wraplength=950).grid(
            row=0, column=0, sticky="w", pady=(0, 10))
        self.skills_tree = self.make_tree(
            skills, ("Saved slot", "Node ID", "Impact entity", "Unlock level"))
        self.skills_tree.master.grid(row=1, column=0, sticky="nsew")

        progression.columnconfigure(0, weight=1)
        progression.rowconfigure(2, weight=1)
        ttk.Label(progression, text=(
            "WARNING: Raw KNOW IDs have no quest/recipe labels. A value may be a count, "
            "boolean or bitmask. Wrong edits can break progression. Only existing IDs "
            "can be changed."), wraplength=950).grid(row=0, column=0, sticky="w")
        search_row = ttk.Frame(progression)
        search_row.grid(row=1, column=0, sticky="ew", pady=10)
        self.filter = tk.StringVar()
        ttk.Label(search_row, text="Filter ID/value").pack(side="left")
        ttk.Entry(search_row, textvariable=self.filter, width=28).pack(side="left", padx=8)
        ttk.Button(search_row, text="Filter", command=self.refresh_knowledge).pack(side="left")
        self.progress_count = tk.StringVar()
        ttk.Label(search_row, textvariable=self.progress_count).pack(side="right")
        self.knowledge_tree = self.make_tree(progression, ("ID (hex)", "ID (decimal)", "Value"))
        self.knowledge_tree.master.grid(row=2, column=0, sticky="nsew")
        self.knowledge_tree.bind("<<TreeviewSelect>>", self.select_knowledge)
        edit_row = ttk.Frame(progression)
        edit_row.grid(row=3, column=0, sticky="ew", pady=10)
        self.progress_id = tk.StringVar(value="Select an entry")
        self.progress_value = tk.StringVar()
        ttk.Label(edit_row, textvariable=self.progress_id, width=25).pack(side="left")
        ttk.Entry(edit_row, textvariable=self.progress_value, width=18).pack(side="left")
        ttk.Button(edit_row, text="Stage raw value", command=self.stage_knowledge
                   ).pack(side="left", padx=10)

        explorer.columnconfigure(0, weight=1)
        explorer.rowconfigure(2, weight=1)
        ttk.Label(explorer, text=(
            "Select any record, including appearance (COUT). Named BDB fields are "
            "read-only; CHAR data arrays and unknown types remain opaque."
        ), wraplength=950).grid(row=0, column=0, sticky="w")
        blob_row = ttk.Frame(explorer)
        blob_row.grid(row=1, column=0, sticky="ew", pady=10)
        self.blob_choice = ttk.Combobox(blob_row, state="readonly", width=65)
        self.blob_choice.pack(side="left")
        self.blob_choice.bind("<<ComboboxSelected>>", self.show_blob)
        ttk.Label(blob_row, text="Hex offset (decimal or 0x...)").pack(side="left", padx=8)
        self.hex_offset = tk.StringVar(value="0")
        ttk.Entry(blob_row, textvariable=self.hex_offset, width=12).pack(side="left")
        ttk.Button(blob_row, text="Inspect", command=self.show_blob).pack(side="left", padx=8)
        self.record_text = tk.Text(explorer, wrap="none", font=("Consolas", 10))
        self.record_text.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(explorer, orient="vertical", command=self.record_text.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.record_text.configure(yscrollcommand=scroll.set, state="disabled")

        pending.columnconfigure(0, weight=1)
        pending.rowconfigure(1, weight=1)
        ttk.Label(pending, text="Export saves staged edits. Save also includes the current name/level form."
                  ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.pending_text = tk.Text(pending, wrap="word", font=("Consolas", 11),
                                    state="disabled")
        self.pending_text.grid(row=1, column=0, sticky="nsew")
        self.status = tk.StringVar(value="No file loaded.")
        progress_row = ttk.Frame(self)
        progress_row.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        progress_row.columnconfigure(1, weight=1)
        self.loading_bar = ttk.Progressbar(progress_row, mode="indeterminate", length=180)
        self.loading_bar.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.loading_phase = tk.StringVar()
        self.loading_label = ttk.Label(progress_row, textvariable=self.loading_phase,
                                       wraplength=780)
        self.loading_label.grid(row=0, column=1, sticky="w")
        self.loading_bar.grid_remove()
        self.loading_label.grid_remove()
        ttk.Label(self, textvariable=self.status, wraplength=1000).grid(
            row=5, column=0, sticky="w", pady=10)
        actions = ttk.Frame(self)
        actions.grid(row=6, column=0, sticky="ew")
        ttk.Button(actions, text="Discard staged changes", command=self.discard).pack(side="left")
        self.export_button = ttk.Button(actions, text="Export edited copy...", command=self.export)
        self.export_button.pack(side="right")
        self.apply_button = ttk.Button(actions, text="Save (overwrite active file)", command=self.apply)
        self.apply_button.pack(side="right", padx=8)
        self.refresh_pending()
        root.protocol("WM_DELETE_WINDOW", self.close)

    @property
    def loading(self) -> bool:
        return self._loading_future is not None

    def require_idle(self) -> bool:
        if self.loading:
            self.error(SaveError("A loading operation is already running. Please wait."))
            return False
        return not self._closed

    def _disable_loading_controls(self) -> None:
        def visit(parent: tk.Misc) -> None:
            for widget in parent.winfo_children():
                if isinstance(widget, (ttk.Button, ttk.Entry, ttk.Treeview)):
                    self._loading_controls.append((widget, widget.instate(("disabled",))))
                    widget.state(("disabled",))
                elif isinstance(widget, tk.Text):
                    self._loading_controls.append((widget, str(widget["state"]) == "disabled"))
                    widget.configure(state="disabled")
                visit(widget)
        self._loading_controls.clear()
        visit(self)

    def _start_loading(self, work: Callable[[Progress], Result],
                       complete: Callable[[Result], None], phase: str) -> None:
        if not self.require_idle():
            return
        phases: Queue[str] = Queue()
        outcome: Queue[Result] = Queue()
        stopped = Event()
        self._loading_phases = phases
        self._loading_complete = lambda: complete(outcome.get_nowait())
        self._loading_stop = stopped

        def report(text: str) -> None:
            if stopped.is_set():
                raise SaveError("Loading stopped because the editor closed.")
            phases.put(text)

        def run() -> None:
            result = work(report)
            report("Preparing character display...")
            outcome.put(result)

        self._disable_loading_controls()
        self.loading_phase.set(phase)
        self.loading_bar.grid()
        self.loading_label.grid()
        self.loading_bar.start(20)
        self.status.set("Loading... Existing files and staged edits are unchanged.")
        self._loading_future = self._loader.submit(run)
        self._loading_after = self.root.after(30, self._poll_loading)

    def _poll_loading(self) -> None:
        self._loading_after = None
        if self._closed or self._loading_future is None:
            return
        while True:
            try:
                phase = self._loading_phases.get_nowait()
            except Empty:
                break
            self.loading_phase.set(phase)
        if not self._loading_future.done():
            self._loading_after = self.root.after(30, self._poll_loading)
            return
        error = self._loading_future.exception()
        complete = self._loading_complete
        self._loading_complete = None
        self._loading_future = None
        self.loading_bar.stop()
        self.loading_bar.grid_remove()
        self.loading_label.grid_remove()
        self.loading_phase.set("")
        for widget, disabled in self._loading_controls:
            if isinstance(widget, ttk.Widget):
                widget.state(("disabled",) if disabled else ("!disabled",))
            else:
                widget.configure(state="disabled" if disabled else "normal")
        self._loading_controls.clear()
        if error is not None:
            self.error(error)
            return
        try:
            if complete is None:
                raise SaveError("Loading completed without a result handler.")
            complete()
        except (OSError, SaveError) as exc:
            self.error(exc)

    def _shutdown_loading(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loading_stop.set()
        self._loading_complete = None
        self._loading_controls.clear()
        self.loading_bar.stop()
        if self._loading_after is not None:
            self.root.after_cancel(self._loading_after)
            self._loading_after = None
        if self._loading_future is not None:
            self._loading_future.cancel()
            self._loading_future = None
        self._loader.shutdown(wait=False, cancel_futures=True)

    def destroy(self) -> None:
        self._shutdown_loading()
        super().destroy()
        # Release Tcl-owning objects here, never during a worker's garbage collection.
        for name, value in tuple(vars(self).items()):
            if isinstance(value, (tk.Misc, tk.Variable)):
                delattr(self, name)

    @staticmethod
    def _read_save(path: Path, report: Progress) -> PreparedSave:
        report("Reading save, validating checksums and decompressing records...")
        document = SaveDocument.open(path)
        return Editor._describe_save(document, report)

    @staticmethod
    def _describe_save(document: SaveDocument, report: Progress) -> PreparedSave:
        if not document.owners:
            raise SaveError("No character or appearance records in this save.")
        report("Reading character names and levels...")
        labels = []
        first_editable = None
        for owner in document.owners:
            try:
                name, level = Bdb(document.blob(owner, b"CHAR").data).character()
                labels.append(f"{name}  |  level {level}  |  {owner:08x}")
                if first_editable is None:
                    first_editable = len(labels) - 1
            except SaveError as exc:
                labels.append(f"{owner:08x}  |  read-only: {exc}")
        return PreparedSave(document, labels, first_editable or 0)

    @staticmethod
    def _prepare_rules(folder: Path, report: Progress) -> PreparedRules:
        report("Reading installed executable metadata and progression rules...")
        try:
            rules = GameRules.open(folder)
            progression = rules.progression()
            if progression.points_per_level != 2:
                raise SaveError("Installed progression rules differ from the supported 2-point rule.")
        except (OSError, SaveError) as exc:
            return PreparedRules(None, f"Game rules not loaded: {exc}", error=exc)
        status = (f"Game rules loaded from {folder}; "
                  f"playable cap {progression.level_cap}, {progression.points_per_level} points/level.")
        return Editor._warm_rules(rules, status, report)

    @staticmethod
    def _warm_rules(rules: GameRules, status: str, report: Progress) -> PreparedRules:
        report("Preparing installed item names and stack limits...")
        try:
            rules.assert_unchanged()
            rules.item_catalogue()
        except (OSError, SaveError) as exc:
            return PreparedRules(rules, status, item_error=exc)
        report("Preparing supported armor and rarity choices...")
        try:
            rules.armor_catalogue()
        except (OSError, SaveError) as exc:
            return PreparedRules(rules, status, armor_error=exc)
        return PreparedRules(rules, status)

    @classmethod
    def _discover_rules(cls, report: Progress) -> PreparedRules:
        report("Finding the installed Enshrouded game...")
        try:
            folders = discover_game_directories()
        except (OSError, SaveError) as exc:
            return PreparedRules(None, f"Automatic game-rule discovery failed: {exc}", error=exc)
        if len(folders) == 1:
            return cls._prepare_rules(folders[0], report)
        status = ("Multiple Enshrouded installations found. "
                  "Use Load installed game rules to choose the one you play.") if folders else (
                      "No Enshrouded installation found in Steam's registered libraries. "
                      "Use Load installed game rules to choose the folder manually.")
        return PreparedRules(None, status)

    def load_async(self, path: Path) -> None:
        if not self.require_idle() or not self.confirm_discard():
            return
        discover_rules = self.game_rules is None and not self.auto_rules_attempted
        existing_rules = self.game_rules
        existing_status = self.rules_status.get()
        read_save, discover, warm_rules = self._read_save, self._discover_rules, self._warm_rules

        def work(report: Progress) -> PreparedSave:
            loaded = read_save(path, report)
            prepared = discover(report) if discover_rules else None
            if existing_rules is not None:
                prepared = warm_rules(existing_rules, existing_status, report)
            report("Rechecking save sources before displaying characters...")
            loaded.document.assert_unchanged()
            return PreparedSave(loaded.document, loaded.labels, loaded.first_editable, prepared)

        def complete(loaded: PreparedSave) -> None:
            if discover_rules:
                self.auto_rules_attempted = True
                if loaded.rules is not None:
                    self.rules_status.set(loaded.rules.status)
                    self.game_rules = loaded.rules.rules
            self._install_save(loaded)
            if loaded.rules is not None and loaded.rules.error is not None:
                self.error(loaded.rules.error)

        self._start_loading(work, complete, "Opening character save...")

    @staticmethod
    def make_tree(parent: ttk.Frame, columns: tuple[str, ...]) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=160)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        return tree

    def error(self, exc: BaseException) -> None:
        self.status.set(f"Error: {exc}")
        messagebox.showerror("Operation failed", str(exc), parent=self.root)

    def confirm_discard(self) -> bool:
        return not (self.document and self.document.changes) or messagebox.askyesno(
            "Discard staged changes?", "Staged edits have not been saved. Discard them?",
            parent=self.root)

    def open_index(self) -> None:
        if not self.require_idle():
            return
        path = filedialog.askopenfilename(title="Select characters-index", parent=self.root)
        if path:
            if Path(path).name != "characters-index":
                self.error(SaveError("Select characters-index, not a snapshot."))
            else:
                self.load_async(Path(path))

    def open_snapshot(self) -> None:
        if not self.require_idle():
            return
        path = filedialog.askopenfilename(title="Select a character snapshot", parent=self.root)
        if path:
            self.load_async(Path(path))

    def find_saves(self) -> None:
        def work(report: Progress) -> list[Path]:
            report("Looking for local and registered Steam character saves...")
            return discover_saves()
        self._start_loading(work, self._found_saves, "Finding saves...")

    def _found_saves(self, paths: list[Path]) -> None:
        self.status.set("Save discovery finished. Existing files are unchanged.")
        if len(paths) == 1:
            self.load_async(paths[0])
        elif paths:
            chooser = tk.Toplevel(self.root)
            chooser.title("Choose a save account")
            for path in paths:
                def choose(selected: Path = path) -> None:
                    chooser.destroy()
                    self.load_async(selected)
                ttk.Button(chooser, text=str(path), command=choose).pack(
                    fill="x", padx=10, pady=5)
            self.status.set("Select a save account to load.")
        else:
            self.status.set("No saves found. Use Open active index to choose a folder.")
            messagebox.showinfo("No saves found", "Use Open active index to choose a folder.",
                                parent=self.root)

    def load(self, path: Path) -> None:
        if not self.require_idle() or not self.confirm_discard():
            return
        try:
            loaded = self._read_save(path, lambda _phase: None)
        except (OSError, SaveError) as exc:
            self.error(exc)
            return
        self.document = loaded.document
        if self.game_rules is None and not self.auto_rules_attempted:
            self.auto_rules_attempted = True
            self.auto_load_game_rules()
        self._install_save(loaded)

    def _install_save(self, loaded: PreparedSave) -> None:
        document = loaded.document
        self.document = document
        self.owner_ids = document.owners
        self.character_choice.configure(values=loaded.labels)
        self.character_choice.current(loaded.first_editable)
        self.blob_indices = list(range(len(document.blobs)))
        self.blob_choice.configure(values=[
            f"{blob.owner:08x} / {blob.label} / {len(blob.data):,} decompressed bytes"
            for blob in document.blobs])
        self.blob_choice.current(0)
        self.hex_offset.set("0")
        self.filter.set("")
        self.refresh_pending()
        self.select_character(preparation=loaded.rules)
        self.show_blob()

    def select_character(self, _event: object = None, *,
                         preparation: PreparedRules | None = None) -> None:
        if self.loading or not self.document:
            return
        self.owner = self.owner_ids[self.character_choice.current()]
        try:
            name, level = Bdb(self.document.blob(self.owner, b"CHAR").data).character()
            self.name.set(name)
            self.level.set(str(level))
            self.name_hint.set(f"Required UTF-8 byte length: {len(name.encode('utf-8'))}")
            self.editable = True
            details = f"Character {self.owner:08x}. Stored level: {level}."
        except SaveError as exc:
            self.name.set("")
            self.level.set("")
            self.name_hint.set("Character fields unavailable for this layout.")
            self.editable = False
            details = f"Read-only character: {exc}"
        for widget in (self.name_entry, self.stage_button):
            widget.configure(state="normal" if self.editable else "disabled")
        self.level_entry.configure(state="readonly" if self.editable else "disabled")
        mode = "Active save" if self.document.index_path else "Snapshot (export only)"
        provider = ("Steam profile: keep Cloud enabled" if self.document.catalogue_path
                    else "Non-Steam profile: Cloud-disabled workflow")
        self.summary.set(f"{mode}: {self.document.source}\n{provider}\n{details}")
        self.refresh_sharing_controls()
        self.refresh_knowledge()
        self.refresh_player_state(preparation=preparation)

    def load_game_rules(self) -> None:
        if not self.require_idle():
            return
        folder = filedialog.askdirectory(
            title="Select installed Enshrouded folder (contains enshrouded.exe)",
            parent=self.root)
        if not folder:
            return
        def complete(prepared: PreparedRules) -> None:
            self.rules_status.set(prepared.status)
            if prepared.error is not None:
                self.error(prepared.error)
                return
            self.game_rules = prepared.rules
            if self.refresh_player_state(preparation=prepared):
                self.status.set(self.rules_status.get())
        prepare = self._prepare_rules
        self._start_loading(lambda report: prepare(Path(folder), report),
                            complete, "Loading installed game rules...")

    def set_game_rules(self, folder: Path) -> bool:
        try:
            rules = GameRules.open(folder)
            progression = rules.progression()
            if progression.points_per_level != 2:
                raise SaveError("Installed progression rules differ from the supported 2-point rule.")
        except (OSError, SaveError) as exc:
            self.rules_status.set(f"Game rules not loaded: {exc}")
            self.error(exc)
            return False
        self.game_rules = rules
        self.rules_status.set(
            f"Game rules loaded from {folder}. Cap {progression.level_cap}, "
            f"{progression.points_per_level} skill points per gained level.")
        return True

    def auto_load_game_rules(self) -> None:
        try:
            folders = discover_game_directories()
        except (OSError, SaveError) as exc:
            self.rules_status.set(f"Automatic rule discovery failed: {exc}. "
                                  "Use Load installed game rules.")
            self.error(exc)
            return
        if len(folders) == 1:
            self.set_game_rules(folders[0])
        elif folders:
            self.rules_status.set("Multiple Enshrouded installations found. "
                                  "Use Load installed game rules to choose the one you play.")
        else:
            self.rules_status.set("No Enshrouded installation found in Steam's registered libraries. "
                                  "Use Load installed game rules to choose the folder manually.")

    def refresh_player_state(self, *, preparation: PreparedRules | None = None) -> bool:
        self.refresh_sharing_controls()
        self.level_entry.configure(state="readonly" if self.editable else "disabled")
        self.progression_hint.set("Load installed game rules to enable level increases.")
        self.inventory_tree.delete(*self.inventory_tree.get_children())
        self.skills_tree.delete(*self.skills_tree.get_children())
        self.stacks.clear()
        self.item_rules.clear()
        self.addable_rules.clear()
        self.item_choice.set("")
        self.item_choice.configure(values=(), state="disabled")
        self.select_add_item()
        for button in (self.item_add, self.inventory_export_button, self.inventory_import_button):
            button.configure(state="disabled")
        self.quantity.set("")
        self.select_inventory()
        if not self.document or self.owner is None:
            return True
        try:
            state = PlayerState(self.document.blob(self.owner, b"CHAR").data)
            xp = state.experience()
            self.xp_summary.set(f"Actor level: {xp.level}  |  XP: {xp.current} / "
                                f"{xp.required}  |  Pending XP gain: {xp.gain}")
            if self.game_rules:
                try:
                    budget = self.document.progression_budget(self.owner, self.game_rules)
                    progression = self.game_rules.progression()
                    original = self.document.blob(self.owner, b"CHAR").original
                    baseline = (state if state.character == original else
                                PlayerState(original)).experience()
                    state.level_plan(baseline.level, progression, baseline)
                    if baseline.gain or baseline.current >= (
                            baseline.required or progression.required_xp(baseline.level)):
                        raise SaveError("Process pending XP/level-ups in-game before editing levels.")
                except (OSError, SaveError) as exc:
                    self.progression_hint.set(f"Level read-only: {exc}")
                else:
                    self.level_entry.configure(state="normal")
                    self.progression_hint.set(
                        f"Level increases enabled (saved level {baseline.level} to "
                        f"{progression.level_cap}). Skill points: {budget.available} available / "
                        f"{budget.earned} earned = {budget.level_points} level + "
                        f"{budget.bonus_points} bonus; {budget.spent} spent. "
                        "XP is granted automatically; existing bonus rewards and skills are preserved.")
            stacks = state.inventory()
            for stack in stacks:
                self.stacks[f"{stack.entity}:{stack.slot}"] = stack
        except SaveError as exc:
            self.stacks.clear()
            self.xp_summary.set(f"Player state read-only: {exc}")
            self.inventory_info.set(f"Player state unavailable: {exc}")
            self.status.set(f"Player state read-only: {exc}")
            self.level_entry.configure(state="readonly" if self.editable else "disabled")
            self.progression_hint.set(f"Level read-only: {exc}")
            return False
        try:
            saved_skills = state.skills()
        except SaveError as exc:
            self.skills_info.set(f"Saved skill cache unavailable: {exc}. Allocation is blocked.")
        else:
            self.skills_info.set(
                f"{len(saved_skills)} saved allocation/effect entries. Purchased upgrade "
                "ranks are stored in the high 3 bits; the low 5 bits affect strength. "
                "Allocation/refund remains read-only pending effect-rebuild and active-ability "
                "acceptance checks. Level increases preserve these entries.")
            for skill in saved_skills:
                self.skills_tree.insert("", "end", values=(
                    skill.slot + 1, f"0x{skill.node_id:08x}",
                    f"0x{skill.impact_entity:08x}", skill.unlock_level))
        rules_valid = True
        if self.game_rules:
            try:
                if preparation is not None and preparation.item_error is not None:
                    raise preparation.item_error
                self.game_rules.assert_unchanged()
                self.item_rules = dict(self.game_rules.items_for({stack.item_id for stack in stacks}))
            except (OSError, SaveError) as exc:
                rules_valid = False
                self.inventory_info.set(f"Inventory read-only: {exc}")
                self.error(exc)
            else:
                self.inventory_info.set(
                    "Rules loaded. Edit/add/remove plain items in accessible Generic slots. "
                    "Equipment, currency, stateful items and hidden slots are protected. "
                    "Labels are game debug names.")
                try:
                    catalogue = self.game_rules.addable_items()
                except (OSError, SaveError) as exc:
                    self.inventory_info.set(f"Quantity editing available; add/import/export blocked: {exc}")
                else:
                    self.addable_rules = {
                        f"{rule.label} [0x{rule.item_id:08x}]": rule
                        for rule in sorted(catalogue.values(), key=lambda r: (r.label.casefold(), r.item_id))
                    }
                    try:
                        if preparation is not None and preparation.armor_error is not None:
                            raise preparation.armor_error
                        armor = self.game_rules.armor_catalogue()
                    except (OSError, SaveError) as exc:
                        self.inventory_info.set(
                            f"Plain items available; armor creation unavailable: {exc}")
                    else:
                        self.addable_rules.update({
                            f"{rule.label} [0x{rule.item_id:08x}]": rule
                            for rule in sorted(armor.values(),
                                               key=lambda r: (r.label.casefold(), r.item_id))
                        })
                        self.inventory_info.set(
                            f"Rules loaded: {len(armor)} supported armor definitions plus plain items. "
                            "Armor requires empty Generic slots and is not auto-equipped. "
                            "Existing equipment and YAML transfers remain protected.")
                    self.item_choice.configure(values=tuple(self.addable_rules), state="normal")
                    for button in (self.item_add, self.inventory_export_button, self.inventory_import_button):
                        button.configure(state="normal")
        else:
            self.inventory_info.set(
                "Load installed game rules to resolve item names and verified stack limits. "
                "Until then, inventory is read-only.")
        for iid, stack in self.stacks.items():
            rule = self.item_rules.get(stack.item_id)
            self.inventory_tree.insert("", "end", iid=iid, values=(
                f"{stack.entity:x} / {stack.category}", stack.slot + 1,
                rule.label if rule else f"Unresolved ID 0x{stack.item_id:08x}",
                stack.quantity, rule.max_stack if rule else "Unknown",
                f"0x{stack.item_entity:08x}" if stack.item_entity else "None"))
        return rules_valid

    def select_inventory(self, _event: object = None) -> None:
        if self.loading:
            return
        selection = self.inventory_tree.selection()
        stack = self.stacks.get(selection[0]) if selection else None
        rule = self.item_rules.get(stack.item_id) if stack else None
        editable = bool(stack and stack.accessible and not stack.item_entity and rule)
        for widget in (self.quantity_entry, self.inventory_stage):
            widget.configure(state="normal" if editable else "disabled")
        self.inventory_remove.configure(state="normal" if editable else "disabled")
        self.quantity.set(str(stack.quantity) if stack else "")
        if editable and rule:
            self.inventory_hint.set(f"Allowed quantity: 1..{rule.max_stack}; stage, then Save.")
        elif stack:
            self.inventory_hint.set(
                "Read-only: unresolved rule, stateful item, non-Generic category or hidden slot.")
        else:
            self.inventory_hint.set("Select an existing stack.")

    def stage_inventory(self) -> None:
        if not self.require_idle():
            return
        selection = self.inventory_tree.selection()
        if not self.document or self.owner is None or not selection or not self.game_rules:
            self.error(SaveError("Select an inventory stack and load installed game rules."))
            return
        try:
            stack = self.stacks[selection[0]]
            quantity = int(self.quantity.get(), 10)
            self.document.edit_inventory(self.owner, stack.entity, stack.slot,
                                         quantity, self.game_rules)
        except (ValueError, OSError) as exc:
            self.error(exc)
            return
        self.refresh_pending()
        if not self.refresh_player_state():
            return
        self.inventory_tree.selection_set(selection[0])
        self.select_inventory()
        self.status.set("Quantity staged. Review Pending changes, then Save.")

    def filter_item_choices(self, _event: object = None) -> None:
        if self.loading:
            return
        query = self.item_choice.get().casefold()
        self.item_choice.configure(values=tuple(
            label for label in self.addable_rules if query in label.casefold()))
        self.select_add_item()

    def select_add_item(self, _event: object = None) -> None:
        if self.loading:
            return
        rule = self.addable_rules.get(self.item_choice.get())
        self.armor_level_entry.configure(state="disabled")
        self.armor_rarity.configure(state="disabled", values=())
        self.armor_level.set("")
        self.armor_rarity.set("")
        self.armor_hint.set("Select supported armor to choose level and rarity.")
        if isinstance(rule, ArmorRule) and self.document and self.owner is not None:
            try:
                level = PlayerState(self.document.blob(self.owner, b"CHAR").data).experience().level
            except SaveError as exc:
                self.armor_hint.set(f"Armor unavailable: {exc}")
                return
            self.armor_level_entry.configure(state="normal", from_=rule.min_level,
                                              to=rule.max_level)
            self.armor_level.set(str(max(rule.min_level, min(level, rule.max_level))))
            self.armor_rarity.configure(state="readonly",
                                         values=tuple(label for _, label in rule.rarities))
            self.armor_rarity.set(dict(rule.rarities)[rule.default_rarity])
            self.armor_hint.set(
                f"Level {rule.min_level}..{rule.max_level}; fresh armor, "
                "0 unlocked upgrades. Added to backpack, not equipped.")

    def add_inventory(self) -> None:
        if not self.require_idle():
            return
        if not self.document or self.owner is None or not self.game_rules:
            self.error(SaveError("Open a character and load installed game rules."))
            return
        try:
            rule = self.addable_rules.get(self.item_choice.get())
            if rule is None:
                raise SaveError("Select an item from the dropdown, not an arbitrary name or ID.")
            quantity = int(self.add_quantity.get(), 10)
            if isinstance(rule, ArmorRule):
                rarity = next((value for value, label in rule.rarities
                               if label == self.armor_rarity.get()), None)
                if rarity is None:
                    raise SaveError("Select an installed armor rarity.")
                self.document.add_armor(self.owner, rule.item_id,
                                        int(self.armor_level.get(), 10), rarity,
                                        self.game_rules, quantity)
            else:
                self.document.merge_inventory(self.owner, [(rule.item_id, quantity)], self.game_rules)
        except (ValueError, OSError) as exc:
            self.error(exc)
            return
        self.refresh_pending()
        self.refresh_player_state()
        self.status.set("Item addition staged. Review Pending changes, then Save.")

    def remove_inventory(self) -> None:
        if not self.require_idle():
            return
        selection = self.inventory_tree.selection()
        if not self.document or self.owner is None or not self.game_rules or not selection:
            self.error(SaveError("Select an inventory stack and load installed game rules."))
            return
        try:
            stack = self.stacks[selection[0]]
            if not messagebox.askyesno("Remove selected stack?",
                                      f"Stage removal of item 0x{stack.item_id:08x} "
                                      f"x{stack.quantity}?", parent=self.root):
                return
            self.document.remove_inventory(self.owner, stack.entity, stack.slot, self.game_rules)
        except (ValueError, OSError) as exc:
            self.error(exc)
            return
        self.refresh_pending()
        self.refresh_player_state()
        self.status.set("Removal staged. Review Pending changes, then Save.")

    def export_inventory(self) -> None:
        if not self.require_idle():
            return
        if not self.document or self.owner is None or not self.game_rules:
            self.error(SaveError("Open a character and load installed game rules."))
            return
        try:
            items = self.document.inventory_export(self.owner, self.game_rules)
            total = len(PlayerState(self.document.blob(self.owner, b"CHAR").data).inventory())
            excluded = total - len(items)
            if excluded and not messagebox.askyesno(
                    "Export portable inventory subset?",
                    f"Export {len(items)} portable plain stacks.\n"
                    f"Exclude {excluded} equipment, hidden, persistent or unsupported stacks.\n"
                    "This YAML is not a complete inventory backup. Continue?", parent=self.root):
                return
            path = filedialog.asksaveasfilename(
                title="Export plain inventory as YAML (new file)", initialfile="inventory.yaml",
                defaultextension=".yaml", filetypes=[("YAML", "*.yaml *.yml")],
                parent=self.root)
            if not path:
                return
            write_inventory_yaml(Path(path), items)
        except (ValueError, OSError) as exc:
            self.error(exc)
            return
        self.status.set(f"Inventory YAML exported: {path}. {len(items)} stacks exported, "
                        f"{excluded} protected stacks excluded. Live save unchanged.")

    def import_inventory(self) -> None:
        if not self.require_idle():
            return
        if not self.document or self.owner is None or not self.game_rules:
            self.error(SaveError("Open a character and load installed game rules."))
            return
        path = filedialog.askopenfilename(title="Import inventory YAML (merge)",
                                         filetypes=[("YAML", "*.yaml *.yml")], parent=self.root)
        if not path:
            return
        try:
            items = read_inventory_yaml(Path(path))
            if not messagebox.askyesno(
                    "Merge inventory YAML?",
                    f"Merge {len(items)} item entries into the selected character?\n"
                    "Existing items remain; matching stacks fill first. Insufficient space "
                    "or an unsupported item aborts the entire import. Changes are staged only.",
                    parent=self.root):
                return
            self.document.merge_inventory(self.owner, items, self.game_rules)
        except (ValueError, OSError) as exc:
            self.error(exc)
            return
        self.refresh_pending()
        self.refresh_player_state()
        self.status.set("YAML inventory merge staged. Review Pending changes, then Save.")

    def stage_character(self) -> bool:
        if not self.require_idle():
            return False
        if not self.document or self.owner is None or not self.editable:
            return False
        try:
            level = int(self.level.get(), 10)
            level_changed = level != Bdb(self.document.blob(self.owner, b"CHAR").data).character()[1]
            self.document.edit_character(self.owner, self.name.get(), level, self.game_rules)
        except (ValueError, OSError) as exc:
            self.error(exc)
            return False
        self.refresh_pending()
        if level_changed or self.owner in self.document.progression_sources:
            self.refresh_player_state()
        self.status.set("Character edits staged. Review Pending changes before saving.")
        return True

    def refresh_knowledge(self) -> None:
        self.knowledge_tree.delete(*self.knowledge_tree.get_children())
        self.progress_id.set("Select an entry")
        self.progress_value.set("")
        if not self.document or self.owner is None:
            return
        if not any(b.owner == self.owner and b.tag == KNOW for b in self.document.blobs):
            self.progress_count.set("No KNOW record for this character.")
            return
        try:
            knowledge = Knowledge(self.document.blob(self.owner, KNOW).data)
        except SaveError as exc:
            self.progress_count.set(f"Read-only: {exc}")
            self.error(exc)
            return
        query = self.filter.get().lower().strip()
        matches = [(key, value) for key, (value, _) in knowledge.entries.items()
                   if query in f"{key:08x} {key} {value}".lower()]
        for key, value in matches[:500]:
            self.knowledge_tree.insert("", "end", iid=str(key),
                                       values=(f"0x{key:08x}", key, value))
        self.progress_count.set(f"{min(len(matches), 500)} shown / {len(matches)} matches "
                                f"/ {knowledge.count} total; filter to narrow")

    def select_knowledge(self, _event: object = None) -> None:
        if self.loading:
            return
        selection = self.knowledge_tree.selection()
        if selection:
            values = self.knowledge_tree.item(selection[0], "values")
            self.progress_id.set(f"ID {values[0]}")
            self.progress_value.set(str(values[2]))

    def stage_knowledge(self) -> None:
        if not self.require_idle():
            return
        if not self.document or self.owner is None:
            return
        selection = self.knowledge_tree.selection()
        if not selection:
            self.error(SaveError("Select an existing progression entry first."))
            return
        try:
            value = int(self.progress_value.get(), 10)
            key = int(selection[0])
            if not messagebox.askyesno("Stage raw progression value?",
                                      f"Change raw ID 0x{key:08x} to {value}?\n"
                                      "Its gameplay meaning is unknown.", parent=self.root):
                return
            self.document.edit_knowledge(self.owner, key, value)
        except ValueError as exc:
            self.error(exc)
            return
        self.refresh_knowledge()
        self.refresh_pending()
        if self.owner in self.document.progression_sources:
            self.refresh_player_state()

    @staticmethod
    def set_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def show_blob(self, _event: object = None) -> None:
        if not self.document or self.blob_choice.current() < 0:
            return
        blob = self.document.blobs[self.blob_indices[self.blob_choice.current()]]
        try:
            offset_text = self.hex_offset.get().strip()
            offset = int(offset_text, 16 if offset_text.lower().startswith("0x") else 10)
            if not 0 <= offset < len(blob.data):
                raise SaveError("Hex offset is outside the decompressed record.")
            lines = [f"Owner: {blob.owner:08x}   Tag: {blob.label}",
                     f"Compressed: {len(blob.compressed):,}  Decompressed: {len(blob.data):,}",
                     "Read-only data. Raw type codes are not gameplay labels.", ""]
            if blob.data.startswith(b"BDB1"):
                bdb = Bdb(blob.data)
                lines.append(f"BDB nodes: {bdb.count:,}. Named fields:")
                lines.extend(f"  {label}: {value}" for label, value in bdb.describe_fields())
                lines.append("")
            lines.append(f"Hex preview: {offset}..{min(offset + 512, len(blob.data))}")
            for position in range(offset, min(offset + 512, len(blob.data)), 16):
                part = blob.data[position:position + 16]
                text = "".join(chr(c) if 32 <= c < 127 else "." for c in part)
                lines.append(f"{position:08x}  {part.hex(' '):47}  {text}")
        except (ValueError, OSError) as exc:
            self.error(exc)
            return
        self.set_text(self.record_text, "\n".join(lines))

    def refresh_pending(self) -> None:
        changes = list(self.document.changes.values()) if self.document else []
        self.set_text(self.pending_text, "\n".join(changes) if changes else "No staged changes.")
        self.export_button.configure(state="normal" if changes else "disabled")
        self.apply_button.configure(state=(
            "normal" if self.document and self.document.index_path else "disabled"))
        self.status.set(f"{len(changes)} staged change(s). Original file is unchanged.")
        self.refresh_sharing_controls()

    def refresh_sharing_controls(self) -> None:
        available = self.document is not None and self.game_rules is not None
        self.character_import_button.configure(state="normal" if available else "disabled")
        self.character_export_button.configure(
            state="normal" if available and self.owner is not None and self.editable else "disabled")

    def export_selected_character(self) -> None:
        if not self.require_idle():
            return
        document, owner, rules = self.document, self.owner, self.game_rules
        if document is None or owner is None or rules is None:
            self.error(SaveError("Open a character save and load installed game rules before sharing."))
            return
        destination = filedialog.asksaveasfilename(
            title="Export selected character outside the save folder",
            initialfile=f"character-{owner:08x}{EXTENSION}",
            defaultextension=EXTENSION, confirmoverwrite=False,
            filetypes=[("Enshrouded character", f"*{EXTENSION}")], parent=self.root)
        if not destination:
            return

        def work(report: Progress) -> Path:
            report("Validating complete character and preparing share file...")
            raw = export_character(document, owner, rules)
            report("Writing selected character outside the save folder...")
            path = Path(destination)
            write_character(path, raw, document, rules)
            return path

        def complete(path: Path) -> None:
            self.status.set(
                f"Character exported to {path}. Source save and pending edits are unchanged.")

        self._start_loading(work, complete, "Exporting selected character...")

    def import_character(self) -> None:
        if not self.require_idle():
            return
        document, rules = self.document, self.game_rules
        if document is None or rules is None:
            self.error(SaveError("Open the destination save and load installed game rules first."))
            return
        source = filedialog.askopenfilename(
            title="Import a trusted character file (add, not replace)",
            filetypes=[("Enshrouded character", f"*{EXTENSION}")], parent=self.root)
        if not source:
            return

        def work(report: Progress) -> CharacterImport:
            report("Reading shared character and validating format/build...")
            shared = read_character(Path(source), rules)
            report("Checking matching characters and preparing a fresh character identity...")
            return document.prepare_character_import(shared, rules)

        def confirm_import(prepared: CharacterImport) -> None:
            if self.document is not document:
                raise SaveError("Destination save changed while preparing the import; retry.")
            shared = prepared.character
            if not messagebox.askyesno(
                    "Add shared character?",
                    f"Add {shared.name!r}, level {shared.level}?\n\n"
                    "Appearance, skills, progression, inventory, equipment and character-local "
                    "quest/map data are included. Existing characters are preserved.\n\n"
                    "Import only files from people you trust. This stages the addition; "
                    "nothing is written to your save until Save.",
                    parent=self.root):
                self.status.set("Character import cancelled. Existing staged edits are unchanged.")
                return
            owner = document.stage_character_import(prepared)
            loaded = self._describe_save(document, lambda _phase: None)
            loaded = PreparedSave(document, loaded.labels, document.owners.index(owner))
            self._install_save(loaded)
            self.status.set("Character addition staged. Review Pending changes, then Save.")

        def complete(prepared: CharacterImport) -> None:
            if self.document is not document:
                raise SaveError("Destination save changed while preparing the import; retry.")
            if not prepared.rename_required:
                confirm_import(prepared)
                return
            source_character = prepared.source_character
            name = simpledialog.askstring(
                "Matching character found - name the new copy",
                f"A character matching {source_character.name!r} already exists "
                "(identity, name or saved gameplay).\n\n"
                "Choose a different, unused name for a separate new character. "
                "The existing character will not be changed.",
                initialvalue=f"{source_character.name} (copy)", parent=self.root)
            if name is None:
                self.status.set("Character import cancelled. Existing staged edits are unchanged.")
                return

            def rename_work(report: Progress) -> CharacterImport:
                report("Validating the new name and preparing the independent character copy...")
                return document.prepare_character_import(source_character, rules, name=name)

            self._start_loading(rename_work, confirm_import, "Preparing renamed character copy...")

        self._start_loading(work, complete, "Preparing shared character import...")

    def confirm_save(self, action: str) -> bool:
        if not self.document or not self.document.changes:
            self.error(SaveError("Stage at least one edit first."))
            return False
        changes = list(self.document.changes.values())
        preview = "\n".join(changes[:20])
        if len(changes) > 20:
            preview += f"\n... and {len(changes) - 20} more; see Pending changes."
        return messagebox.askyesno(f"{action} staged changes?",
                                  preview + "\n\nSave only these staged edits?",
                                  parent=self.root)

    def export(self) -> None:
        if not self.require_idle():
            return
        if not self.confirm_save("Export") or not self.document:
            return
        destination = filedialog.asksaveasfilename(
            title="Export outside the Steam save folder", initialfile="characters-edited.ksc",
            defaultextension=".ksc", confirmoverwrite=False, parent=self.root)
        if not destination:
            return
        try:
            backup = self.document.export(Path(destination))
        except (OSError, SaveError) as exc:
            self.error(exc)
            return
        self.status.set(f"Exported: {destination} | Original backup: {backup}")
        messagebox.showinfo("Export complete",
                            f"Edited copy: {destination}\nOriginal backup: {backup}\n"
                            "Your live save was not changed.", parent=self.root)

    def apply(self) -> None:
        if not self.require_idle():
            return
        if not self.document or not self.document.index_path:
            self.error(SaveError("Open characters-index to overwrite the active save."))
            return
        try:
            cloud_enabled = self.document.catalogue_path is not None
            self.document.assert_save_provider(cloud_disabled=not cloud_enabled,
                                               cloud_enabled=cloud_enabled)
        except (OSError, SaveError) as exc:
            self.error(exc)
            return
        if self.editable and not self.stage_character():
            return
        if not self.confirm_save("Overwrite active file"):
            return
        provider_warning = (
            "Keep Steam Cloud ENABLED for Enshrouded. Do not switch profiles.\n"
            "Fully exit Steam and Enshrouded before saving.\n\n"
            "This overwrites the registered local Steam file, not the Cloud copy. "
            "Steam synchronization and in-game acceptance are NOT verified. "
            "Handle any Cloud conflict deliberately; do not discard the backup.\n\n"
            "Have you kept Cloud enabled and closed both applications?"
        ) if cloud_enabled else (
            "This is a LOCAL, Cloud-disabled profile in Saved Games, not Steam remote.\n"
            "Confirm Cloud was already disabled for this intended profile, and fully "
            "exit Steam and Enshrouded. No profile migration is performed.\n\n"
            "Have you confirmed the profile and closed both applications?"
        )
        if not messagebox.askyesno(
            "Steam Cloud safety",
            provider_warning, parent=self.root):
            return
        try:
            parent = automatic_backup_root()
            parent.mkdir(parents=True, exist_ok=True)
            backup = parent / f"enshrouded-backup-{datetime.now():%Y%m%d-%H%M%S-%f}"
            source_index = self.document.index_path
            saved = self.document.apply_active(backup, cloud_disabled=not cloud_enabled,
                                               cloud_enabled=cloud_enabled)
            self.document.changes.clear()
            if source_index is not None:
                self.load(source_index)
        except (OSError, SaveError) as exc:
            self.error(exc)
            return
        self.status.set(f"Active save replaced. Full history backup: {saved}")
        messagebox.showinfo("Active save updated",
                            f"Backup: {saved}\nKeep the same Cloud setting/profile and "
                            "verify in-game. Steam synchronization remains unverified.",
                            parent=self.root)

    def discard(self) -> None:
        if not self.require_idle():
            return
        if self.document and self.confirm_discard():
            old_count, owner = len(self.document.blobs), self.owner
            self.document.discard_changes()
            if len(self.document.blobs) != old_count:
                loaded = self._describe_save(self.document, lambda _phase: None)
                if owner in self.document.owners:
                    loaded = PreparedSave(
                        self.document, loaded.labels, self.document.owners.index(owner))
                self._install_save(loaded)
            else:
                self.select_character()
                self.show_blob()
                self.refresh_pending()

    def close(self) -> None:
        if self.confirm_discard():
            self._shutdown_loading()
            self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Enshrouded character editor")
    parser.add_argument("save", nargs="?", type=Path,
                        help="characters-index or character snapshot")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--smoke-test", type=Path, metavar="REPORT",
                        help="verify the UI and bundled dependencies without loading saves")
    args = parser.parse_args()
    if args.smoke_test and args.save:
        parser.error("--smoke-test cannot be combined with a save path")
    root = tk.Tk()
    editor = Editor(root)
    if args.smoke_test:
        try:
            import yaml
            import zstandard
            root.withdraw()
            root.update()
            report = {"version": VERSION, "frozen": bool(getattr(sys, "frozen", False)),
                      "title": root.title(), "tk": tk.TkVersion,
                      "yaml": yaml.__version__, "zstandard": zstandard.__version__,
                      "save_loaded": editor.document is not None}
            with args.smoke_test.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2)
        finally:
            editor._shutdown_loading()
            root.destroy()
        return
    if args.save:
        root.after(0, lambda: editor.load_async(args.save))
    root.mainloop()


if __name__ == "__main__":
    main()
