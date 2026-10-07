# Enshrouded Character Workshop

A local Windows desktop application for inspecting and editing **Enshrouded
character saves**, with automatic backups before applying changes.

The application does not upload saves, use telemetry, access the network,
inject into the game, or change Steam Cloud settings. It is an unofficial tool
for an undocumented save format.

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** - installation, first use, every application
  tab, character and inventory sharing, saving, backup recovery, and troubleshooting.
- **[Detailed Reference](docs/DETAILED_REFERENCE.md)** - the preserved detailed
  workflows, safety checks, save-format research, validation coverage, source
  setup, executable builds, and GitHub release process.

## Download and launch

1. Download `enshrouded-character-editor-windows-x64.exe` and its adjacent
   `.exe.sha256` file from [GitHub Releases](https://github.com/RechiTaun/enshrouded-character-editor/releases).
2. Verify the download using the PowerShell command below.
3. Run the executable. Python and Tk are bundled; no separate Python
   installation is needed.

An installed copy of Enshrouded is required for verified game rules used by
level editing, inventory editing, and character sharing. No game files or saves
are bundled.

The executable is unsigned, so Windows SmartScreen or antivirus software may
warn about it. Check the source and checksum before deciding whether to run it;
do not disable antivirus protections.

```powershell
$exe = '.\enshrouded-character-editor-windows-x64.exe'
$actual = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$expected = (Get-Content -LiteralPath "$exe.sha256" -Raw).Trim()
if ($expected -ne "$actual  enshrouded-character-editor-windows-x64.exe") {
    throw 'Executable checksum mismatch.'
}
```

A checksum verifies download integrity, not authorship or code-signing trust.
Before a release is available, development builds can be downloaded from
**Actions > Build Windows executable > successful run**. Extract the artifact
ZIP containing the executable and checksum. These artifacts last 14 days and
use version `0.0.0`; identify them by the run's commit.

## What can it do?

| Area | Supported functionality |
| --- | --- |
| Characters | Select characters independently and rename supported characters within the existing UTF-8 byte length. |
| Level and XP | Increase levels up to the verified installed cap, with coherent XP and two level-earned skill points per gained level. Existing bonuses and skill allocations are preserved. |
| Character sharing | Export a complete initialized character and import it as a new character without replacing existing ones. Requires matching verified game builds. |
| Inventory | Change plain stack quantities, add/remove supported plain items, create supported unequipped armor with level/rarity choices, and merge/export portable plain-item YAML. |
| Skills | Inspect saved allocations/effects and, with rules, review the skill-point budget. Allocation and refund are not supported. |
| Raw knowledge | Filter and edit existing numeric progression values. IDs have no quest or recipe labels; this is an advanced, potentially unsafe operation. |
| Records and appearance | Inspect record metadata, named fields, and hex previews. Appearance editing is not supported. |
| Save management | Review/discard staged changes, export an edited copy, or overwrite the active save with automatic backups and safety checks. Older snapshots are export-only. |

Level decreases, arbitrary XP edits, weapon/tool/currency creation, existing
equipment editing/removal, inventory resizing, world editing, and profile
migration are not supported.

## First-use checklist

1. Keep the Cloud setting used by your existing profile. **Do not disable Cloud
   just to use this editor**: Cloud-on and Cloud-off use different save folders.
2. Fully close **Enshrouded and Steam**, including Steam's tray process.
3. Launch the editor, click **Find saves**, and choose the intended profile if
   prompted. Check the displayed source path and character list.
4. Select a character. Installed rules load automatically; if needed, use
   **Load installed game rules...** to select the game installation folder.
5. Stage your edits and review **Pending changes**.
6. Choose **Save (overwrite active file)** and confirm both prompts, or use
   **Export edited copy...** to leave the live save unchanged.
7. Keep your backups and verify the result in-game before making further edits.

Read the **[User Guide](docs/USER_GUIDE.md)** before saving or importing.
Binary validation does **not** guarantee in-game acceptance, armor equip/reload
behavior, progression/effect acceptance, or Steam Cloud synchronization.

## Run from source

Use Python 3.10 or later with Tkinter (included in the standard Windows Python
installer). From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe character_editor.py
```

You can pass a `characters-index` or snapshot path as the last argument. See
[opening a specific save](docs/USER_GUIDE.md#open-a-specific-save-or-older-snapshot)
for examples.

For tests, private read-only save checks, executable builds, and release
instructions, see the [Detailed Reference](docs/DETAILED_REFERENCE.md#validation).
