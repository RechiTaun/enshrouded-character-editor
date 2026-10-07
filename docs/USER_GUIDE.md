# Enshrouded Character Workshop - User Guide

[README](../README.md) | [Detailed Reference](DETAILED_REFERENCE.md)

This guide explains how to inspect a save, stage changes, share characters or
plain inventory items, and safely apply or discard edits.

## Contents

- [Before you start](#before-you-start)
- [Launch and load a save](#launch-and-load-a-save)
- [Understand the window and staged changes](#understand-the-window-and-staged-changes)
- [Character tab](#character-tab)
- [Inventory tab](#inventory-tab)
- [Skills inspection tab](#skills-inspection-tab)
- [Raw knowledge advanced tab](#raw-knowledge-advanced-tab)
- [Records and appearance tab](#records-and-appearance-tab)
- [Pending changes, saving, and export](#pending-changes-saving-and-export)
- [Backups and recovery](#backups-and-recovery)
- [Troubleshooting](#troubleshooting)
- [Limits and unsupported operations](#limits-and-unsupported-operations)

## Before you start

Download and verify the Windows executable, or install the source dependencies,
using the [README instructions](../README.md#download-and-launch).

**Protect your existing profile:**

- Fully exit **Enshrouded and Steam**, including Steam's system tray process,
  before loading the save you intend to overwrite. Do not restart either while
  saving.
- Keep the Cloud setting you already use. The editor does not change it or
  migrate characters between profiles.
- Keep backups until you have checked the edited character in-game.
- Import character files only from people you trust.

### Steam Cloud and save locations

| Existing profile | Character save location | Setting to keep |
| --- | --- | --- |
| Steam Cloud enabled | Steam's `userdata\<account>\1203620\remote` folder | Leave Cloud enabled; exit Steam before applying changes. |
| Local, Cloud disabled | `%USERPROFILE%\Saved Games\Enshrouded` | Leave Cloud disabled for this existing local profile. |

**Disabling Cloud changes the folder the game uses.** Characters can appear to
disappear if the local folder is empty, even though the Steam files still exist.
Do not create replacement characters or overwrite backups while investigating.
To return to an unchanged Steam profile, re-enable Cloud and handle any
synchronization conflict deliberately.

The editor validates save structure and supported edits, but in-game acceptance
and Steam synchronization remain unverified. Armor equip/reload and
progression/effect behavior also require a backed-up manual check.

## Launch and load a save

### Find your active profile

1. Launch `enshrouded-character-editor-windows-x64.exe`.
2. Click **Find saves**. The editor searches registered Steam locations and the
   local Saved Games folder.
3. If multiple profiles are found, choose the intended path in **Choose a save
   account**. If only one is found, it loads automatically.
4. On **Character**, check the source path, active-save/snapshot mode, and profile
   information. Verify the expected characters appear.
5. Use the character dropdown at the top right to select a character.

`characters-index` selects the current active file. Files named `characters`
and `characters-1` through `characters-9` are rotating save history, **not one
file per character**. Each can contain multiple characters.

### Open a specific save or older snapshot

The toolbar has no manual save-file opening button. If discovery does not find
the right profile, pass its full path when launching from PowerShell:

```powershell
.\enshrouded-character-editor-windows-x64.exe "C:\path\to\1203620\remote\characters-index"
```

From a source checkout:

```powershell
.\.venv\Scripts\python.exe character_editor.py "C:\path\to\1203620\remote\characters-index"
```

For inspection or export of an older snapshot, pass that file instead:

```powershell
.\enshrouded-character-editor-windows-x64.exe "C:\path\to\1203620\remote\characters-3"
```

Opening a snapshot directly is **export-only**, even if it happens to be the
current ring member. Open `characters-index` to use active-save overwrite.
For Git Bash/UCRT64 source launching, see the [Detailed Reference](DETAILED_REFERENCE.md#run-from-source).

### Load installed game rules

Rules normally load automatically when you first open a save, using Steam's
registered libraries, including secondary drives. The game is not launched.

If discovery fails or multiple installations exist:

1. Open **Character** or **Inventory**.
2. Click **Load installed game rules...**.
3. Select the installation folder containing `enshrouded.exe`,
   `enshrouded.kfc`, and `enshrouded.kfc_resources`.
4. Read the resulting status and any read-only explanation.

This selects a **game installation**, not a save destination. Rules supply
item debug names, stack limits, armor choices, level limits, XP calculations,
and point accounting. Loaded rules are shared across characters.

Not every installed executable is supported for every operation. An unverified
updated or modified build can leave level editing or character sharing blocked,
even if some inventory rules work. Do not bypass a build/source mismatch.

### Loading feedback

The animated progress bar shows the current preparation phase; it is not a
percentage estimate. Editing and save/export controls are temporarily disabled.
Wait for completion rather than starting a second load. Failed loading restores
the previous document and staged edits. Loading does not write saves.

## Understand the window and staged changes

The character dropdown selects the character being edited. The bottom status
line reports loading, pending edits, export results, or backup locations.

| Tab | Purpose |
| --- | --- |
| **Character** | Name, level/XP, point budget, rules, complete-character sharing. |
| **Inventory** | Inspect stacks; stage supported quantity, addition, removal, and YAML operations. |
| **Skills (inspection)** | View saved skill allocation/effect entries; no allocation or refund. |
| **Raw knowledge (advanced)** | Filter and edit existing raw progression values. |
| **Records & appearance** | Read-only fields, record sizes, and hex previews. |
| **Pending changes** | Review all staged changes before export, saving, or discard. |

**Staging changes only updates the editor's in-memory document.** It does not
modify the live save. Staged changes for different characters are retained when
you switch characters, and saving/exporting the document includes all of them.

Text typed into the name or level fields is not staged automatically when you
switch characters. Click **Stage character edits** first. Inventory quantities,
item additions, and raw knowledge values also need their own staging buttons.

**Save (overwrite active file)** stages the selected character's current
name/level form before saving. Exports include **only already-staged edits**.

## Character tab

### Rename a character

1. Select a supported character.
2. Read **Required UTF-8 byte length** below **Character name**.
3. Enter a name using exactly that many UTF-8 bytes.
4. Click **Stage character edits** and review **Pending changes**.

For example, `Alice` and `Robin` both use five ASCII bytes. Non-ASCII characters
can use multiple bytes, so equal visible length is not always enough.
Shared string storage or unsupported layouts can block renaming.

The different-length naming supported during character import is a separate
operation; it does not remove the ordinary rename restriction.

### Increase level

1. Load verified installed game rules.
2. Read the actor level, current/required XP, pending XP gain, playable cap,
   and point-budget explanation.
3. Enter a higher level within the displayed cap.
4. Click **Stage character edits**.
5. Review the level, XP, and point changes in **Pending changes**.

The editor updates summary and actor levels and XP coherently. Each gained level
adds **two level-earned skill points**. Bonus rewards and existing learned
skills/effects are preserved; XP is calculated from the saved threshold and
installed curve while retaining current XP within the level.

The budget is `2 * (level - 1) + active bonus points - spent costs`. Available
points therefore depend on existing allocations, not just level.

Lowering the saved level and direct XP editing are unavailable. You can retarget
a staged increase or return to the original saved level; calculations use the
original baseline and preserve separately staged name/inventory edits.

Pending XP/level-ups must be processed in-game first. Missing rules/knowledge,
invalid skill IDs/ranks, or an overspent budget can also block level edits.
The text below **Level** explains the specific blocker.

### Export a complete character

1. Select the character and load verified rules.
2. Stage any desired name/level changes first.
3. Click **Export character...**.
4. Choose a new file outside the source save folder.
5. Share the resulting `.enshrouded-character` file.

Export works without pending edits and includes already-staged changes for that
character. It leaves your source save and other characters unchanged.

The file includes appearance, level/XP, skills/effects, knowledge, inventory,
equipped/persistent items, and character-local quest/map/exploration data.
It does **not** include world saves, global known-player records, Steam
registration, other characters, or the source filesystem path.
Quest/map data is retained, not rebound to the recipient's world.

### Import a character as a new addition

Both people must use the **same verified installed game executable/build**.

1. Open your own destination `characters-index` and load installed rules.
2. Click **Import character (add)...** and choose a trusted
   `.enshrouded-character` file.
3. If an existing identity, name, or saved gameplay matches, enter a new unused
   name. This also applies to repeat imports and reimporting your own character.
4. Confirm the displayed name and level.
5. Select the newly added character and review **Pending changes**.
6. Use **Save (overwrite active file)** to apply the addition with a backup.

**Every import creates a fresh character identity. It never replaces an
existing character.** You do not need to create a placeholder character in-game.
Existing characters and staged edits are retained.

Import-time names may have a different UTF-8 byte length and include Unicode.
They must be nonempty, contain no control characters, and be unique ignoring
case and surrounding whitespace. Canceling either prompt adds nothing.
Invalid names or unsupported files report an error without losing staged edits.

Incomplete/uninitialized characters, unsupported layouts, or incompatible
cloud/identity bindings are rejected. Checksums detect corruption, not
authenticity or safety of arbitrary game payloads. Keep the backup until you
have verified the imported character in-game.

## Inventory tab

The table shows container/category, slot, item, quantity, stack limit, and item
entity reference. Names are installed **debug names**, not localized game names.
Without rules, unresolved items are shown by numeric ID and inventory is read-only.

### Change a plain stack quantity

1. Select an accessible plain stack.
2. Read the allowed quantity range below the table.
3. Enter a decimal integer in **New quantity**, from `1` to the installed limit.
4. Click **Stage quantity**.

Use **Remove selected item** rather than setting quantity to zero.
Equipment/stateful items, hidden slots, non-Generic categories, and unresolved
rules are protected. Disabled controls and the hint explain why.

### Add plain items

1. Type part of the item's debug name into **Add item (type to filter)**.
2. Select the exact entry from the dropdown; arbitrary typed names/IDs do not work.
3. Enter a positive quantity next to the picker.
4. Click **Stage add** and review **Pending changes**.

Addition fills matching stacks to their installed limits, then uses accessible
empty backpack/hotbar slots. It does not resize inventory. Insufficient space
rejects the **entire addition**, not just the excess.

The catalogue includes supported materials, consumables, ammunition, blueprints,
collectibles, animal/pet food, and color palettes. Plain creation excludes items
requiring persistent or randomized state. Weapons, tools, currency,
customization, and weapon gems are not offered for creation.

### Add supported armor

1. Select an exact armor entry in the same item dropdown.
2. Choose **Armor level** within its displayed installed range.
3. Choose an available **Rarity**.
4. Enter the number of pieces to create and click **Stage add**.

The initial armor level is your character's level clamped to the item's range;
it can be higher than your character if the item has a higher minimum.
Rarity defaults to the installed base rarity.

Each piece uses one accessible empty Generic slot and is **added unequipped**.
It has the selected base level/rarity, installed available perks, and **zero
unlocked upgrades**. Multiple pieces are staged together or rejected together.

Supported creation requires compatible saved schemas and the installed template.
If only the supported `PerkContainerNew` schema is missing, the editor generates
it from verified installed metadata during **Stage add**. No donor character or
extra folder selection is required. Generated schema changes appear in
**Pending changes** and are saved only with the armor. Other missing/malformed
schemas or unsupported builds can still block creation.

Existing equipment quantity editing, removal, and YAML transfer remain
protected. Armor creation is not a guarantee of in-game equip/reload acceptance.

### Remove a plain stack

Select an editable plain stack, click **Remove selected item**, and confirm.
This stages removal of the **whole stack**. To keep part of it, stage a lower
positive quantity instead. Linked stateful items cannot be removed.

### Export and import inventory YAML

**Export inventory YAML...** writes the selected character's current staged
portable plain backpack/hotbar stacks to a new `.yaml` file. It never overwrites
an existing file. If stacks are excluded, the editor asks you to confirm.

Equipment, hidden slots, persistent/stateful items, and unsupported definitions
are excluded. YAML is a **portable subset, not a complete inventory backup**.
Use complete-character export to share equipment, or save-history backups for recovery.

To import:

1. Select the destination character and load rules.
2. Click **Import YAML (merge)...** and select an exported inventory file.
3. Confirm the merge.
4. Review **Pending changes**, then save or export the edited document.

Import **adds** quantities; it does not clear or replace existing inventory.
Repeated imports add quantities again. Duplicate item entries are merged and
split at installed stack limits. Invalid quantities, unsupported IDs, or
insufficient capacity reject the whole import and preserve earlier staged edits.

For manual YAML editing, use numeric `item_id` values from a real export; the
optional `name` is descriptive, not the identifier. The format is
`enshrouded-inventory`, version `1`, with an `items` list of `item_id`, optional
`name`, and `quantity`. Files are limited to 256 KiB and 4096 entries.
Duplicate mapping keys, unsafe types/tags, and aliases are rejected. YAML cannot
specify equipment, entity references, slot addresses, or skills.
See the [reference example](DETAILED_REFERENCE.md#adding-removing-and-sharing-inventory).

## Skills inspection tab

**Skills (inspection)** displays saved slot, node ID, impact entity, and unlock
level for saved allocation/effect entries. With installed rules, **Character**
also shows earned, level-earned, bonus, spent, and available skill points.

This is read-only: there is no skill allocation or refund action. Level
increases preserve the existing allocation/effect entries. Numeric node IDs
are not a labeled skill-tree editor.

## Raw knowledge advanced tab

**Warning:** knowledge IDs have no quest/recipe labels. A value may be a
boolean, counter, or bitmask. An integer that fits storage can still break
progression. Do not assume every value should be `1`.

To edit a value whose meaning you have independently verified:

1. Open **Raw knowledge (advanced)**.
2. Enter part of an ID or value in **Filter ID/value**, then click **Filter**.
   IDs are displayed in both hexadecimal and decimal. Only the first 500
   matches are shown; narrow the filter when needed.
3. Select an existing entry.
4. Enter the new value as a decimal unsigned 32-bit integer
   (`0` through `4294967295`).
5. Click **Stage raw value** and confirm the warning.
6. Review **Pending changes** before saving.

Only existing IDs can be changed; this does not add/remove knowledge entries.
Missing or unsupported knowledge records remain read-only. Raw edits affecting
progression can change the point budget or block a staged level increase.

## Records and appearance tab

**Records & appearance** is an inspection tool, not a raw-byte editor.

1. Choose a record from the dropdown. This list covers the loaded container,
   not only the character selected in the toolbar; check the displayed owner.
2. Read its tag and compressed/decompressed sizes.
3. For supported BDB records, inspect the named fields shown.
4. Enter a decompressed **Hex offset** as decimal (for example, `256`) or
   hexadecimal (for example, `0x100`) and click **Inspect**.

The hex preview displays up to 512 bytes from the requested offset, alongside
an ASCII preview. Offsets outside the record report an error.

Appearance (`COUT`) fields and opaque references can be inspected but not
edited. Raw type codes and unknown fields are not gameplay labels. For record
tags and container structure, see [Format findings](DETAILED_REFERENCE.md#format-findings).

## Pending changes, saving, and export

### Review or discard edits

Open **Pending changes** to review changes across all characters, including
imported characters and any generated armor schema.

**Discard staged changes** resets the in-memory document to the loaded original
and removes imported characters. It does not undo a save already written or
delete exported files. There is no per-change undo button; edit a supported
value back or discard the whole staged set. Loading another save or closing
with pending changes prompts before discarding.

### Choose the right output

| Action | What it writes | Changes the live save? |
| --- | --- | --- |
| **Save (overwrite active file)** | The active ring member selected by `characters-index`, plus automatic history backups. | Yes, after checks and confirmations. |
| **Export edited copy...** | A complete edited container and its original-byte `.original.bak`. | No. |
| **Export character...** | One complete character in `.enshrouded-character` format. | No. |
| **Export inventory YAML...** | The selected character's portable plain-item subset. | No. |

### Overwrite the active save

1. Fully close Steam and Enshrouded. Keep the existing profile's Cloud setting.
2. Open the intended `characters-index` **after** both applications have exited.
3. Stage inventory/raw changes. Stage name/level changes before switching
   characters; Save also stages the currently selected character's form.
4. Review **Pending changes** for every character.
5. Click **Save (overwrite active file)**.
6. Confirm the change preview and provider-specific safety warning.
7. Note the backup path from the completion dialog.
8. Keep the same Cloud setting/profile when reopening the game, and verify the result.

No destination or backup chooser appears. Saving checks processes, unchanged
source/index/catalogue bytes, and applicable loaded rules before replacing the
active file. It does not modify the index, other snapshots, world saves,
Steam catalogue, or Cloud settings.

For Steam profiles, both the active file and index must already be registered
with matching sizes/hashes in `remotecache.vdf`. Missing/stale registrations
block saving rather than being rewritten.

After a Cloud-profile edit, another edit is blocked until Steam's catalogue
matches the saved file again. Reopen Steam, verify the result, exit Steam and
the game, and reopen the index before further editing. Handle Cloud conflicts
deliberately; never bypass a metadata mismatch.

### Export an edited container without applying it

1. Stage at least one change, including any desired name/level form edits.
2. Click **Export edited copy...** and confirm the preview.
3. Choose a new filename outside the source save directory, for example
   `characters-edited.ksc`.

The editor also writes `characters-edited.ksc.original.bak` with the exact
original bytes. Existing output or backup files are never overwritten.
Export does not apply the result and does not clear pending changes.

An exported container is **not installed in the game**. Do not drop it under
a new filename in Steam's remote directory; Steam may not recognize it.
Use the guarded active-save operation to apply supported edits.

## Backups and recovery

Active-save backups are stored under:

```text
%USERPROFILE%\Enshrouded Character Workshop\backups
```

Each save uses a new timestamped directory containing all existing character
ring files and `characters-index`. Steam-profile backups also contain a copy
of the catalogue as diagnostic/recovery evidence. Backup bytes are verified
before the active file is replaced.

To restore:

1. Keep the original profile's Cloud setting and fully close Steam and Enshrouded.
2. Locate the timestamped backup from the save completion dialog.
3. Copy its original character ring files **and** `characters-index` to their
   original save directory, replacing the corresponding files.
4. Keep the backup itself intact.
5. Reopen the same profile, deliberately resolve any Cloud conflict, and verify
   the character in-game.

Do **not** blindly restore `remotecache.vdf`; it can also contain world-file
metadata. An exported `.original.bak` contains only the originally selected
ring member, not full save history. Restore it only to that original member.

An interrupted/failed save may leave a backup directory. Keep and inspect it;
a retry uses a new directory. Do not delete backups merely because a binary
roundtrip or save operation succeeded.

## Troubleshooting

| Symptom | What to check or do |
| --- | --- |
| No saves found / wrong characters | Check the original Cloud setting and account. Launch with the intended full `characters-index` path. Do not switch profiles to make discovery work. |
| Save is disabled | A directly opened snapshot is export-only. Open `characters-index` for active overwrite. Controls are also temporarily disabled while loading. |
| Level is read-only | Read the explanation below Level. Load verified rules; process pending XP/level-ups in-game; resolve the reported rules, knowledge, or skill-budget issue. |
| Name edit rejected | Match the displayed UTF-8 byte length. Shared strings/unsupported layouts can also block it. Import-time renaming has separate rules. |
| Inventory controls are disabled | Load rules and select an accessible plain stack. Hidden, equipment/stateful, non-Generic, and unresolved slots are protected. |
| Item does not appear / addition rejected | Type a debug-name fragment and select the exact dropdown entry. Check supported categories, quantity, installed bounds, and available capacity. |
| Armor cannot be created | Read the specific metadata/schema/build error. Some armor is excluded; malformed existing schemas and other missing components are not repaired automatically. |
| Character import/export fails | Both sides need the same verified executable. Use the correct `.enshrouded-character` format and a supported initialized character. YAML is not interchangeable with it. |
| Import asks for a new name again | Every import adds a fresh character. Identity, name, or saved-gameplay matches trigger a rename, including repeated imports. |
| Save reports running processes | Fully exit the game and Steam, including the tray process. Do not restart either during saving. |
| Source/rules/catalogue changed | Resolve the reported issue, then reopen the index and restage edits. Never bypass stale-source or registration checks. |
| Export destination already exists | Choose a new filename. Existing output and backup files are protected. |
| Export missed a name/level change | Click Stage character edits before exporting. Export does not read unstaged form values. |

## Limits and unsupported operations

- Characters without a supported initialized `CHAR` record may be inspectable
  but cannot use ordinary character editing or complete-character sharing.
- Lowering saved levels, direct XP edits, and skill allocation/refund are unavailable.
- Appearance inspection is read-only.
- Item creation is limited to the offered plain items and supported fresh armor.
  Existing equipment/stateful-item editing/removal and YAML transfer remain protected.
- Inventory resizing, world editing, profile migration, and Cloud-setting
  changes are not provided.
- Numeric bounds do not prove a raw knowledge value is valid gameplay data.
- Unsupported formats/builds fail with explicit explanations rather than
  guessed writes. Static validation is not an in-game or Cloud guarantee.

For implementation details, test coverage, known validation gaps, and build
instructions, see the [Detailed Reference](DETAILED_REFERENCE.md).
