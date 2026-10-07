# Enshrouded Character Workshop

A small, local Windows desktop tool for inspecting Enshrouded character saves,
editing conservative fields, and applying edits with a backup. No uploads,
telemetry, server, game injection, or network access are used by the application.

## Download the Windows executable

Download `enshrouded-character-editor-windows-x64.exe` from this repository's
[GitHub Releases](https://github.com/RechiTaun/enshrouded-character-editor/releases).
The Windows x64 executable includes Python, Tk and its runtime dependencies;
no separate Python installation is required. Your installed Enshrouded game
is still needed for verified game rules. No game files or saves are bundled.

The executable is unsigned. Windows SmartScreen or antivirus software may
warn about an unfamiliar executable. Verify the source and checksum before
deciding whether to run it; do not disable antivirus protections.
Download the adjacent `.exe.sha256` asset and verify it in PowerShell:

```powershell
$exe = '.\enshrouded-character-editor-windows-x64.exe'
$actual = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$expected = (Get-Content -LiteralPath "$exe.sha256" -Raw).Trim()
if ($expected -ne "$actual  enshrouded-character-editor-windows-x64.exe") {
    throw 'Executable checksum mismatch.'
}
```

This verifies download integrity, not authorship or code-signing trust.
Before a release is published, development executables are available as
14-day artifacts under **Actions > Build Windows executable > successful run**.
Download and extract the artifact ZIP containing the executable and checksum.
Development artifacts embed version `0.0.0`; use the run's commit to identify them.

## Run from source

Use Python 3.10 or later with Tkinter (included in the standard Windows Python
installer). From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe character_editor.py
```

Alternatively, pass the full path to a `characters-index` or character snapshot
as the last argument. **Find saves** discovers Steam's registered install and
the local `Saved Games\Enshrouded` directory. If discovery fails, use **Open
active index** and choose the file manually.

In Git Bash/UCRT64, use this launch command instead of the PowerShell command:

```bash
./.venv/Scripts/python.exe character_editor.py
```

### Loading progress

**Find saves**, opening an index/snapshot, command-line startup and manually
loading installed rules run their read-only preparation in a background worker.
An animated progress bar shows the current phase: save discovery, checksum/frame
validation, character names, installed rules, item names and armor choices.
It is not a percentage estimate: these stages have different, unpredictable costs.
The window continues processing events while preparation runs.

Opening, editing and save/export controls are temporarily disabled, so only one
load can run at a time. A failed load restores the previous document and staged
edits, displays an error and re-enables the appropriate controls. Missing game
rules still allow save inspection, with the existing read-only explanations.
Closing the window stops result delivery; background loading never writes saves.

Armor definitions are decoded in resource-offset order to reuse the bounded
chunk cache instead of repeatedly decompressing the same chunks. Decoded rules
are reused in memory on subsequent loads, with installation/source checks still
enforced. There is no persistent cache, new dependency or skipped validation.
Restart the editor after updating to use this loading behavior.

## Supported operations

| Data | Capability |
| ---- | ---------- |
| Multiple character owners | Select characters independently; preserve other owners. |
| Character sharing | Export one complete initialized character; every import creates a fresh character identity without replacing existing characters. Matching imports ask for a new name. Includes appearance, equipment, skills and character-local quest/map data. Requires matching verified game builds. |
| Character name | Ordinary edits require the same UTF-8 byte length; shared string storage blocks in-place renaming. Import-time copy names may have a different byte length using isolated string storage. |
| Character level and XP | Increase levels up to the installed playable cap after loading game rules. Update summary/actor levels and XP coherently; automatically grant two level-earned skill points per gained level. Preserve bonus rewards and existing skill allocations. Lowering the saved level and unresolved pending XP/level-ups remain blocked. |
| Progression (`KNOW`) | Filter and edit existing raw uint32 values. IDs have no quest/recipe labels. |
| Appearance (`COUT`) | Inspect named fields and opaque references; no appearance editing. |
| Inventory | Add plain items or supported fresh armor using a filterable combobox. Armor has selectable installed level/rarity bounds and is inserted unequipped. Edit plain quantities, remove plain stacks, and export/import plain YAML. Other equipment, currency, hidden slots and unsupported persistent/stateful creation remain protected. |
| Skills | Inspect saved allocation/effect entries and, with installed rules, the earned/bonus/spent/available budget. Allocation/refund remains blocked pending effect-rebuild and active-ability acceptance checks. Level edits preserve these entries. |
| Older snapshots | Inspect and export edits; direct apply is disabled. |
| Active save | Export, overwrite a local Cloud-disabled profile, or overwrite a registered Steam profile offline while keeping Cloud enabled. Automatic backups and stale-source/process checks are mandatory. Steam synchronization remains unverified. |

The tool checks storage bounds, not gameplay limits. Even a valid integer can be
an invalid game level or progression value. Unknown KNOW values can represent
booleans, counters or bitmasks: do not assume every value should become `1`.
This is an unofficial editor for an undocumented, changing format.

Level increases grant **2 skill points per gained level**, derived by the game
from actor level rather than stored in a guessed point counter. The editor
updates both saved levels and the current/gain/required XP together. It computes
the equivalent XP grant using the existing saved threshold first, then the
installed curve for each new level, retaining current XP within the level.
Bonus knowledge rewards and learned skills/effect handles are not rewritten.
The legacy summary-only patch remains blocked.

For example, with the researched installation and level **25, XP 1548/4252**,
raising to **30** grants the equivalent of **40996 XP**, leaves **1548/9467**,
and adds **10 level-earned points**. With 57 bonus points, the earned budget
changes from **105 to 115**; available points subtract existing invested costs.
These values depend on your installed rules, not a hardcoded public XP table.
An existing cached threshold is not normalized on load or on a name/inventory edit.
Pending XP gains, XP already due for a level-up, unknown/duplicate skill IDs,
invalid ranks, missing knowledge/rules and overspent budgets block level edits
with an explicit reason. Skill allocation/refund is still unfinished.

## Editing workflow

**Steam Cloud changes the save location, not just synchronization.** Cloud-on
characters use Steam's `userdata\<account>\1203620\remote` directory. Disabling
Cloud makes the game use `%USERPROFILE%\Saved Games\Enshrouded` instead. If that
local profile is empty, your characters can appear to disappear even though the
Steam files are intact. Do not create another character or save over backups
while investigating. To return to an unchanged Steam profile, re-enable Cloud;
handle any synchronization conflict deliberately.

The tool does not migrate profiles or world saves or change Cloud settings.
For a Steam profile, **keep Cloud enabled** and close Steam/the game before
writing. The active snapshot and index must already be registered in
`remotecache.vdf`, with sizes/hashes matching the loaded bytes. Missing or stale
registrations block saving; the tool never invents or rewrites them.

1. Open `characters-index` to resolve the current active save. Use **Open
   snapshot** only when intentionally exploring an older generation.
2. Select a character. Owners without a supported CHAR record remain read-only.
3. Rules load automatically when you first open a save, using Steam's registered
   libraries and Enshrouded install manifest (including secondary drives).
   No game is launched. The **Character** tab shows the selected folder or an
   explicit discovery error. If no installation is found, multiple installations
   exist, or automatic loading fails, click **Load installed game rules...** on
   **Character** (or **Inventory**) and choose the folder containing `enshrouded.exe`,
   `enshrouded.kfc` and `enshrouded.kfc_resources`. This is a game-folder chooser,
   not a save destination. Names and limits are resolved locally; no binary is
   executed. The **Level** field becomes editable when progression checks pass;
   the text below it displays the point budget or the exact blocker. Enter a
   higher level within the displayed cap. For inventory, select an editable
   stack, enter a quantity, and click **Stage quantity**.
4. Change the level/name if desired, then click **Save (overwrite active file)** to save into
   the active file that was opened. No save-location or backup-location chooser
   appears. Save includes the current level/name form and all previously staged
   edits. Inventory and raw progression changes must be staged separately.
5. Confirm the change preview and provider-specific safety warning. Backups are automatic.
6. Alternatively, use **Stage character edits**, review **Pending changes**, and
   choose **Export edited copy**. Export includes only staged edits.
   Switching characters retains staged edits; stage form edits before switching.

### Sharing a complete character

On **Character**, select the character and click **Export character...**.
Choose a location outside the save folder. Share the resulting
`.enshrouded-character` file with the recipient. Export works without pending
edits and includes any edits already staged for that character; stage name/level
form changes first. It does not modify your save or export your other characters.

The recipient opens their own **characters-index**, then clicks
**Import character (add)...**. The editor validates the file, its game build and
character identity, then prepares a fresh GUID/owner for the imported copy.
If the original identity, display name or saved gameplay matches an existing
character, the editor asks for a **new, unused name**, then confirms its name and level.
The character becomes selectable and appears in **Pending changes**. Existing
characters and staged edits are retained. Click **Save (overwrite active file)**
to apply the addition using the normal provider checks and automatic backups.
**Discard staged changes** also discards imported characters.

This transfers complete saved character records, including appearance, level/XP,
skills and effects, progression knowledge, inventory, equipped/persistent items,
and character-local quest/map/exploration data. It does not create missing items
or merge inventories, so equipment transfer is not limited to the editor's item
creation catalogue. World saves, global known-player records and Steam registration
are not copied. Map/quest data is retained, not rebound to a recipient's world.

**Every import adds a new character**, even when importing the same file again
or reimporting your own character. Original characters and earlier imported
copies are never replaced. Repeated imports are also detected by matching
level, entity state, appearance, knowledge and explored-map data, even after
the first copy has a different name and identity. This compares saved gameplay,
not all outer metadata; matching names alone also prompt a rename.

Import-time names may have a different UTF-8 byte length and include Unicode.
They must be nonempty, contain no control characters and not match an existing
name (ignoring case/surrounding whitespace). The old strings and references are
preserved through copy-on-write storage. Ordinary Character-tab name editing
still requires the same byte length. Cancelling the name or final confirmation
adds nothing; invalid names report an error and leave staged edits unchanged.

The format is a versioned binary archive, **not inventory YAML**. Both people need
the same verified installed executable. Incomplete/uninitialized characters,
unsupported layouts, extra unknown record types and non-null character cloud
bindings are rejected rather than silently omitted or guessed. Copies update
only the verified CHAR root GUID and the four owner keys; inventory/equipment
entity references remain untouched. Additional identity references or aliased
identity/name nodes block unsupported rebinding explicitly. Appearance-only
owners and all staged copies also reserve their IDs.

Import **only files from people you trust**. The file includes character names,
identifiers and personal character/world progress; opaque game payloads remain
intact. It contains no source filesystem path or unrelated character records.
Checksums detect corruption, not authenticity or arbitrary game-payload safety.
Python roundtrip checks do not prove in-game load/equip or Cloud acceptance;
retain the automatic backup until you verify the imported character in-game.

### Adding, removing and sharing inventory

On **Inventory**, type part of an item debug name into **Add item**, select an
entry from its combobox, enter a positive quantity and click **Stage add**.
Stacks are filled up to their installed limits; excess uses accessible empty
backpack/hotbar slots. Creation is restricted to items whose installed definition
has no persistent item-entity template and no rarity generation, on the verified
game executable. Allowed categories are materials, consumables, ammunition,
blueprints, collectibles, animal/pet food and color palettes. Weapons,
tools, currency, customization and weapon gems are not offered for creation.
This does not resize your inventory.
If there is insufficient capacity, the entire operation is rejected.

Supported **armor** also appears in the combobox. Select its exact entry, choose
**Armor level** and **Rarity**, enter a quantity and click **Stage add**.
The level defaults to your character's level clamped to the item's installed
range; rarity defaults to the installed base rarity. For example,
`Armor_T5-TX_Mage_Wizard_DPS_Legs_Loot` supports levels **30..50** on the
researched installation. A level-25 character therefore sees an initial armor
level of 30, not an invalid 25. Each armor piece consumes one accessible empty
Generic slot; it is **not equipped automatically**. Multiple pieces are staged
atomically, with distinct persistent references.

Created armor has the selected base level and rarity, the installed number of
available perks, and **zero unlocked upgrades**. Native equipment systems derive
armor stats when equipped; the editor does not write invented actor stats.
Creation requires matching saved component schemas and the installed
`Base_Pide` template. If **PerkContainerNew** is missing, the editor now generates
that specific schema automatically from the verified installed game's reflection
metadata when you click **Stage add**. No donor character, schema file or extra
folder selection is needed when automatic game discovery succeeds. Pending
changes explicitly show the generated schema; it is saved only together with
the staged armor through the normal Save/export and backup safeguards.

Existing schemas retain their bytes and identifiers. A malformed existing schema
is not silently replaced. Generation reserves an unused save-local component ID,
including avoiding template-only IDs, and updates the schema table's count and
allocation size. Other missing component schemas or templates, unsupported game
builds and incompatible metadata still block creation with an explicit error.
If armor cannot be added, no schema-only change is staged.

Weapons, bags, custom templates, missing perk references
and definitions with nonzero rarity-generation masks are excluded.
Missing armor metadata shows an explicit message without blocking plain items.
Equipment quantity editing, removal and YAML import/export remain protected.
Static rule/save validation has passed; **in-game equip/reload behavior and
Steam Cloud synchronization have not been verified**. Keep backups until you
have confirmed the new item in-game.

Select an accessible plain stack and click **Remove selected item** to stage
clearing its ID/count. Stateful stacks and their linked entities cannot be removed.
Use **Save (overwrite active file)** to apply additions/removals with the normal
automatic backup. Merely staging changes does not modify the live save.

**Export inventory YAML** exports the selected character's current staged
portable plain backpack/hotbar stacks, excluding equipment, hidden slots,
stateful items and definitions requiring persistent/randomized state.
The UI confirms the number excluded before export. This is a portable subset,
not a complete inventory backup (use the normal automatic save backups for that).
It requires a new filename and never overwrites an existing file.
**Import YAML (merge)** adds those items to the selected character; it does not
replace or clear existing items. Repeated imports add quantities again. Multiple
entries of the same item are merged, quantities split at installed stack limits.
Unsupported IDs, invalid quantities or insufficient capacity reject the entire
import without losing previously staged changes.

```yaml
format: enshrouded-inventory
version: 1
items:
  - item_id: 1234
    name: Synthetic example only
    quantity: 17
```

Use real IDs from an exported file or the dropdown, not the synthetic example.
The optional name is descriptive; numeric `item_id` identifies the installed item.
YAML cannot supply slot addresses, entity references, equipment or skill data.
Files are bounded to 256 KiB/4096 entries, with unique keys, safe YAML types and
no aliases. Inventory import/export is distinct from whole-save binary export.

After pulling this update, install the new YAML dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Automatic discovery runs once per editor session; loaded rules are shared across
characters and remain subject to source/build checks. The manual button can
select or reload an installation at any time. No personal installation path is
hardcoded or saved, no drive-wide scan is performed, and no Cloud settings change.

### Export

Choose a new filename outside the source save directory, such as
`characters-edited.ksc`. The tool writes the edited container and a
`characters-edited.ksc.original.bak` containing the exact original bytes.
Existing output/backup files are never overwritten.

An exported file is **not** installed in the game. Do not just drop a new
filename into Steam's remote directory: Steam Remote Storage may not recognize
it. Use the guarded active-save operation instead.

### Save directly (overwrite active file)

Before applying:

1. Keep the Cloud setting appropriate for the profile you already use. For Steam
   remote saves, **leave Cloud enabled**; do not disable it or migrate.
2. Fully exit Enshrouded **and Steam**, including Steam's system tray process.
3. After Steam has exited, open the intended profile's `characters-index`.
   Cloud-on uses Steam's remote folder; Cloud-off uses Saved Games. Verify the
   displayed profile and characters before editing.
4. Stage inventory quantities or change the level/name, choose **Save (overwrite
   active file)**, and confirm the changes and provider warning. No destination
   selection is required.

The tool automatically stores backups under
`%USERPROFILE%\Enshrouded Character Workshop\backups`. Each save creates a new
timestamped backup directory with all existing character
ring files and their index (plus a copy of the Steam catalogue for a Cloud profile),
verifies those backup bytes, and checks the original
files/index/catalogue and loaded game rules for changes. It checks running processes before preparing the backup
and again before replacing the active file via an atomic same-directory rename.
It does **not** alter the index, other snapshots, world saves, Steam catalogue or Cloud settings.
If any safety check fails, reopen the save after resolving the reported issue.
An interrupted/failed operation may leave a backup directory: keep it, inspect
it; a retry automatically uses a new timestamped directory.

Process checks are safeguards, not locks: do not start Steam or the game while
applying. The tool cannot verify your Cloud setting. Keep the same setting/profile
while testing the result in-game. Steam's cache and Cloud reconciliation remain
outside the tool's control; a valid binary roundtrip is not proof the game will
accept or synchronize the save. A second Cloud-profile edit is blocked until
Steam's catalogue again matches the saved file: reopen Steam, verify the result,
exit both applications, and reopen the index. Never bypass a metadata mismatch.

### Restore a backup

Keep the original profile's Cloud setting and fully close Steam and Enshrouded. Copy the original
ring files **and** `characters-index` from the successful timestamped backup to
their original save directory, replacing the corresponding files. Keep the
backup itself intact. For an exported `.original.bak`, restore it only to its
original selected ring member; it is not a complete save-history backup.
The backed-up catalogue is diagnostic/recovery evidence; do not blindly replace
the live catalogue, which can also contain world-file metadata.

Never edit the save while the game is running. Verify the character in-game
before deleting backups. Handle any Steam
Cloud conflict deliberately rather than automatically accepting cloud data.

## Format findings

These facts were checked read-only against all ten supplied character snapshots.
No personal save contents are included in this repository.

- `characters-index` is JSON. `latest: 0` selects `characters`; values `1..9`
  select `characters-N`. The numbered files form a rotating history, not one
  file per character. A deleted or invalid index is rejected.
- A character container begins with `KSC1`. Offset 4 holds a little-endian
  uint32 record count. Offsets 8 and 16 hold separate checksums for the record
  table and concatenated compressed payloads.
- The table starts at offset 24. Each 12-byte entry contains a uint32 owner,
  a four-byte tag and a uint32 compressed size. Zstandard frames follow in table
  order. The checksum algorithm is reflected CRC-64 with ECMA polynomial,
  initialization and final XOR both all ones (CRC-64/XZ).
- Owner IDs associate a character's records. `CHAR` contains character state,
  `COUT` appearance, `FOWR` explored-map data, and `KNPL` known-player data.
  An appearance owner may have no initialized CHAR record.
- The physical KNOW tag is `dc 8e d5 f0`, not the ASCII text `KNOW`. Its version-2
  payload has three uint32 header words followed by separate arrays of IDs and
  values. The second header word is preserved; IDs are not assumed sorted.
- CHAR and COUT use `BDB1`. Section locations are relative to their header
  fields. BDB stores node types, raw scalar/reference words, a UTF-8 string pool,
  and maps keyed by FNV-1a field hashes and one-based parent node references.
  Map values are one-based child node references.
- The editor locates root `name` (type 14 string-pool reference) and `level`
  (type 8 uint32) structurally, not by searching byte patterns or assuming fixed
  character offsets. Same-width patches leave the unknown schema untouched.
- Untouched records retain their original compressed bytes. Changed records
  are recompressed, lengths and checksums rebuilt, and the result reloaded to
  verify its decompressed bytes before saving.

Public references used to cross-check container/progression facts:

- [KSC1 format notes](https://github.com/o-shabashov/enshrouded-save-transfer/blob/main/FORMAT.md)
- [Save structure observations](https://github.com/Fab550010/EnshroudedSaveExplorer/blob/main/docs/Enshrouded_Game_Data_and_Save_File_Structure.md)
- [Enshrouded modding tools](https://github.com/Brabb3l/kfc-parser)

This implementation is independent. Older documentation can disagree or describe
different game builds; for example, the 16 header bytes after the count are
checksums, not an arbitrary save ID, and appearance sizes are not constant.
Unsupported layouts fail visibly rather than being guessed.

### Nested state and local rules

The BDB `data` array is decoded as uint8 nodes containing an `EHD0` entity
section and embedded `ESC2`/`CTCB` component schemas. Entity/component ranges,
inheritance, field offsets, static arrays and inventory links/categories are
validated. Shared byte-node storage and overlapping layouts reject writes.
Older `ESC0` state remains unsupported rather than being treated as `ESC2`.

The structural backend also retains every serialized component (including opaque
and zero-byte components), server/client partitions, and the `ESC2` base-template
table. A template record contains a GUID, UTF-8 name and 80-byte component bitmap.
It describes a **base template**, not a whitelist: persistent items can add saved
state components beyond that bitmap. Template references and complete section
boundaries are validated.

Internal APIs can append an entity using an existing saved template and grow
the isolated BDB uint8 array. Appending preserves the original entity groups
and schema/template suffix. Growth retains existing node IDs, leaves old tables
and opaque bytes intact, and appends updated storage tables. The 64-bit storage
table also contains non-array data: only entries referenced by array nodes are
interpreted, and changed array descriptors are copied rather than overwritten.
Shared character-array descriptors or array children block resizing.
Existing same-width edits still use exact byte-value patches. These are
the foundation of the bounded armor factory. The factory constructs named,
schema-checked ItemState, Level, OwnerRelationship, PerkContainerNew and UsedItem
components and an empty client StaticTransform marker. It sets HasLevel only,
zero damage, inventory ownership/backlinks, zero unlocked upgrades, and the
installed Level attribute root/maximum. It does not copy a donor, synthesize a
PideRebalanced marker, or inject actor effects. CTCB enum tables are read at
their eight-byte-aligned boundary. New saved references are unique within their
entity archive; runtime game handles are not allocated by the editor.

For the verified build, the native schema loader maps saved component IDs to
installed components by hash. The bounded PerkContainerNew generator emits CTCB
namespace, type, string and field tables from installed metadata, including both
reflected type hashes and Component inheritance. Definition insertion preserves
old records and template bytes, updates the ESC2 definition count and aggregate
CTCB allocation (header offset 8), and leaves its registry identity untouched.
No game-produced schema bytes or donor state are embedded in the application.

Quantity edits modify only the four saved count bytes and their corresponding
BDB node words. Addition/removal also patches the ID/count in validated Generic
slots; entity references remain zero, and protected slots/linked entities are
preserved. Changed payloads are reparsed. Item names/limits and the level cap/points-per-level are read from
local PE reflection metadata and KFC resources, without fixed executable addresses.
Unsupported/missing rules or changes to the installation block editing.
Labels currently use debug names, not localized game UI strings.

Progression additionally resolves the early-level lookup and later float32 XP
calculation inputs from BalancingTable, reward IDs/amounts from
GameKnowledgeResource, and invested costs/ranks from SkillTreeResource.
Point accounting is `2 * (level - 1) + active bonuses - spent costs`; packed
upgrade ranks use their high three bits for cost, preserving the low five
effect-strength bits. The misleading `SkillpointsSpent` knowledge record is
not an authoritative spend counter and is never rewritten by level edits.
The native progression calculation is also gated by the SHA-256 fingerprint
of the executable that was researched. An unverified updated/modded executable
keeps level editing read-only even if its metadata looks identical; matching
field names alone do not prove the calculation stayed unchanged. Inventory
rules remain independently usable. New builds require fresh native verification,
not adding an unchecked fingerprint to the allowlist.
Lowering the saved level is unavailable. Retargeting a staged increase, including
returning to the original level, recalculates from the original XP and preserves
separately staged name/inventory edits. Source-rule checks and point budgets are
revalidated before export/overwrite.

### Portable character format

Version 1 uses a 76-byte little-endian `<8sI32s32s>` header: magic
`ENCHAR\x00\x01`, KSC1 payload length, verified executable SHA-256 and payload
SHA-256. The payload is a normal KSC1 container with exactly one owner and its
CHAR, COUT, KNOW and FOWR records. The envelope adds no paths or account metadata.
Existing container CRC64 checks and bounded Zstandard decompression still apply.
Files are limited to the normal 64 MiB container limit plus the envelope header.

The CHAR root `id` is a kind-15 GUID reference into BDB section 36 (16-byte
entries); its first uint32 matches the container owner. The supported `cloudId`
is the null GUID reference. Every import allocates a fresh UUID with a nonzero
low32 owner, checking all existing owners including appearance-only entries.
Only an unshared root GUID with no additional saved references is changed.
Import-time renaming appends a copied string pool with a new uint16-length
UTF-8 string and updates the unshared name node; other string/key references
still resolve to the unchanged prefix. Container
serialization detects changes in record membership as well as payload bytes.
Discard removes newly appended records and restores original payloads.
Static tracing of the verified executable confirms that its character loader
enumerates CHAR records by tag and allocates an entry for previously unseen
owner IDs. It does not require creating a placeholder character first. This
native-path verification is not an in-game acceptance test.

## Validation

Character-copy tests cover fresh GUIDs, zero/colliding owner retries, staged-copy
reservations, repeated gameplay matches, Unicode name growth and the exact
uint16 UTF-8 byte limit. Existing compressed frames, equipment/entity data and
other string users must remain unchanged. Rename/confirmation cancellation and
invalid names are exercised through actual Tk controls.
Loading and UI fixtures share main-thread teardown of destroyed test
interpreters, including widgets retained by mock exception tracebacks; no
production garbage-collection policy is changed.

From the repository root, using the environment above:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q -x '(^|[\\/])(\.venv|build|dist)([\\/]|$)' .
```

Tests use synthetic saves and temporary directories. UI tests instantiate actual
Tk widgets, so they require a desktop display. Active-save tests simulate process
state and write only into temporary test folders.

Opt-in checks for a private local save directory:

```powershell
$env:ENSHROUDED_TEST_SAVE_DIR = 'C:\path\to\1203620\remote'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:ENSHROUDED_TEST_SAVE_DIR
```

These additional tests read all ten snapshots and write edited roundtrips only
into temporary directories. They never apply changes to your live saves.
Binary validity and preservation have automated coverage; loading the edited
character in Enshrouded is a separate manual check, not an automated guarantee.
The nested decoder, item-rule resolution and in-memory quantity edit/revert
were also checked against all ten supplied snapshots without modifying them.
No in-game inventory edit or Cloud synchronization has been verified yet.
Inventory tests additionally cover combobox selection, add/split/merge/removal,
empty/hidden/stateful slots, atomic capacity failures, portable YAML roundtrips,
duplicate keys, unsafe tags, aliases, excessive depth/size and staging preservation.
The real installed catalogue and actual Tk picker were checked locally; additions
and removals were roundtripped/reverted strictly in memory without live writes.
Entity-archive tests cover opaque schemas, base-template overrides, server/client
partitions, exact size/count limits, duplicate references and malformed records.
Byte-array growth tests cover copy-on-write descriptors, unchanged non-array
storage and sibling arrays, repeated growth, and preserved compressed frames.
Structural append/growth was also checked against the supplied snapshots strictly
in memory; empty test entities are not generated or validated game equipment.
Progression tests cover exact thresholds, cached-threshold grants, the 105/115
earned budgets, invested versus effective ranks, staged retarget/revert,
byte/owner/frame preservation, invalid/stale inputs and direct overwrite with
automatic backups. Level editing is enabled, but in-game progression/effect
acceptance and Steam Cloud synchronization still require a backed-up manual check.

## Build a Windows executable

Use 64-bit Windows Python 3.12. From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\build_executable.py --version 0.0.0
```

The build produces the executable and SHA256 file in `dist`. PyInstaller bundles
only application dependencies, not the tests, private saves or installed-game
metadata. The build launches the actual frozen executable, creates its Tk UI,
verifies bundled dependency versions and embedded version, and checks that no
save was loaded. The smoke report remains in `build\smoke-report.json`.
This startup test does not verify in-game acceptance or Steam synchronization.

## GitHub Actions and releases

- [Build Windows executable](.github/workflows/build.yml) runs on main pushes,
  pull requests and manual dispatch. It runs synthetic regression tests on
  Windows, compiles sources, builds with pinned PyInstaller, smoke-tests the
  executable and uploads the executable/checksum artifact. It has read-only
  repository permissions and uses GitHub-hosted runners.
- [Release from main](.github/workflows/release.yml) is **manual only**. Open
  **Actions > Release from main > Run workflow**, select **main**, and choose
  **patch** (default), **minor** or **major**. Running from another branch fails.
  The same tested build workflow is reused before release publication.

Versions automatically increment the highest stable `vMAJOR.MINOR.PATCH` Git
tag. Prerelease and unrelated tags do not influence stable versions. With no
stable tags, patch produces `v0.0.1`, minor `v0.1.0`, and major `v1.0.0`.
For example, from `v1.2.3`, patch produces `v1.2.4`, minor `v1.3.0`, and major
`v2.0.0`. Version metadata is embedded at build time; no version-bump commit
is needed. Release tags target the exact source commit tested in that run.

Releases are serialized with a concurrency group. GitHub keeps at most one
pending run in a group, so a newer request can replace an older pending one.
Only the publish job receives `contents: write`. It checks the artifact's
checksum, creates a draft with executable/checksum assets and generated release
notes, then publishes it. A test/build/smoke failure prevents publication.
If publication fails after draft creation, inspect the draft before retrying;
the workflow does not silently overwrite an existing release or delete drafts.
