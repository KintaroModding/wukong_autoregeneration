# Auto Regeneration

Health and mana recover 1.25% of their maximum every second; focus, vessel and transformation energy recover too.

A mod for Black Myth: Wukong (Unreal Engine 5.0), made with game version `1.0.21.23831`. This README is
for developers. What the mod does, how to install it and its compatibility are in [openmods.md](openmods.md), the mod
page on [openmods](https://openmods.nexus).

## How it works

The mod adds one permanent buff and gives it to the player through the gourd, which is always equipped:

- `b1/Content/00Main/PBTable/NoneRuntime/FUStBuffDesc-Talent.data` (`BtlShare.TBFUStBuffDesc`): new buff 998001,
  `Duration` -1 (permanent), `Interval` 1000 (every second), `TargetBase` Owner, with these effects on each tick:

  | Effect | Parameters | Meaning |
  |---|---|---|
  | `RecoverAttr` | 151, 125, 0, 1 | health (`Hp`) +1.25% of its maximum (the last parameter 1 = percent, in 1/10000) |
  | `RecoverAttr` | 152, 125, 0, 1 | mana (`Mp`) +1.25% of its maximum |
  | `IncreasePevalue` | 0, float 5.0 | focus +5 |
  | `CostAttr` | 8, float -5.0 | vessel energy (`FabaoEnergy`) +5 (a negative cost) |
  | `CostAttr` | 9, float -5.0 | transformation energy (`VigorEnergy`) +5 |

- `b1/Content/00Main/PBTable/Runtime/TalentSDesc.data` (`ResB1.TBTalentSDesc`): new talent 998002 (`Type` Suit,
  `AddBuffIDs` "998001"); the talents of the four gourds with an effect of their own (108011 to 108014) get buff
  998001 added to their `AddBuffIDs`.
- `b1/Content/00Main/PBTable/Runtime/EquipDesc.data` (`ResB1.TBEquipDesc`): the 13 gourds without an effect of their
  own (18001 to 18010, 18015 to 18017) get `EquipEffectId` 998002.

Every other row of the three tables is the game's own. The parameters of `RecoverAttr` follow the game's buffs: for
example its revival buff 103 restores `[151, 10000, 0, 1]`, 100% of the maximum health.

Black Myth: Wukong keeps its game data (stats, buffs, levels, drops) in protobuf tables, `.data` files below
`b1/Content/00Main/PBTable/`, inside its own variant of the UE pak format. The mod replaces whole tables. A mod that
changes the same tables conflicts with this one, and a game update that changes them needs a new build of the mod.
The game loads mods from `Paks\~mods` only with the launch option `-fileopenlog`.

## Build

Requirements: Windows PowerShell 5.1 (part of Windows) and Python 3 (no extra package).

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1             # dist\wukong_autoregeneration_P.pak
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Deploy     # build, then copy the pak to Paks\~mods
powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Wait      # copy the pak when the game is closed
```

The build encodes every table in `src` (JSON with field names, at the table's game path) into its protobuf form
(`scripts\pbtable.py`, with the field numbers and types from `scripts\pbschema.json`) and checks that it decodes back
to its source. It then packs the tables into a pak in the game's format (`scripts\b1pak.py`) and checks that every file
in the pak matches its table.

`deploy.ps1` copies the pak to `<game folder>\b1\Content\Paks\~mods`. The game folder is
`$env:UE5_GAMES_DIR\Black Myth Wukong`; `-GameDir` or `-ModsDir` override it. The running game locks the file, so the
script stops (exit code 2) or, with `-Wait`, waits until the game is closed.

Versioning: every change raises the second number in `mod.json` (1.0 -> 1.1): `python scripts\bump_version.py`.

## Project layout

```
src/                               Mod source: the changed tables as JSON with field names, at their game path
dist/                              Build output: the .pak for Paks\~mods (git-ignored)
docs/                              Cover and screenshots for openmods.md
scripts/pbtable.py                 Encodes and decodes the protobuf tables
scripts/pbschema.json              Field numbers, names and types of the tables in src
scripts/b1pak.py                   Writes and checks the game's pak format
scripts/bump_version.py            Raises the version in mod.json
build.ps1                          Builds dist\ from src\ and checks the result
deploy.ps1                         Copies the build to the game's Paks\~mods folder
mod.json                           Name, version, game and file names
```

The first commit with a table holds the original table, so `git diff` against it shows exactly what the mod changes.

## License

MIT, see [LICENSE](LICENSE).
