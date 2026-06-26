# Vampire Syndicate: Gangs of Moonfall — Gang Trainer

A small Python trainer that pins your gang's **Movement** and **Action Points** to
infinity in *Vampire Syndicate: Gangs of Moonfall* (an Unreal Engine 5 game). Built on
[`pymem`](https://github.com/srounet/Pymem).

> **Single-player, personal use only.** Don't use it in multiplayer/online modes — it's a
> good way to get banned, and that's not what this is for. It writes to the game's memory,
> so use at your own risk and keep a save backup.

## Use

Requires [uv](https://docs.astral.sh/uv/) and a running game.

```sh
uv run vstrainer
```

That's it. Leave it running and play:

- It auto-detects the game process (the game spawns a stray duplicate; it picks the real one).
- It identifies **your** units and pins their move/AP — enemies are never touched.
- It self-heals: it survives turns, new battles, and even a **full game restart**
  (it waits for the new process and re-attaches automatically).

First start takes ~15 s to map the game's objects; after that it's near-instant, and a
restart reuses a cached fingerprint so it starts pinning in about a second.

Stop with **Ctrl+C**.

## How it works

Raw addresses are useless here. The game constantly bulk-copies stats around the heap as
pooled value-types, so no value has a stable pointer. (Static pointer scans and code
patches were both tried, and both are dead ends.) Instead, the trainer uses **Unreal's own
reflection**:

1. **Identify the gang the right way.** It reads UE's `GNames` and walks the live object
   table to find unit objects, then filters by the game's own `bAiControlled` flag
   (`false` = your gang, `true` = enemy). From each gang unit it reads a **stable
   fingerprint** (stats that never change — Initiative, Melee, etc.).
2. **Pin the values that drive the display.** The on-screen move/AP come from
   `Stat.Move` / `Stat.ActionPoints` records, which exist as *many* byte-identical copies;
   only one is authoritative. Each tick the trainer structurally scans the heap and pins
   **every** record whose fingerprint matches a gang member — guaranteeing the
   authoritative copy is hit, while enemies (different fingerprints) are left alone.

A fast hot-region rescan keeps the loop cheap (~0.3 s) between occasional full scans.

## Notes

- The pinned value (`9,999,999`) can persist into your **save file** — convenient, but keep
  a backup if you care about a clean save.
- All game-specific offsets live in [`vstrainer/constants.py`](vstrainer/constants.py); if a
  game patch moves them, that's the one file to update.

## Layout

| Module          | Role                                                |
|-----------------|-----------------------------------------------------|
| `constants.py`  | every reverse-engineered offset / tuning value      |
| `process.py`    | attach to the game, pick the real instance          |
| `memscan.py`    | fast filtered reads of process memory               |
| `scanner.py`    | find stat-record blocks by structure                |
| `uemem.py`      | UE reflection: `GNames` + the cached object table   |
| `ue_trainer.py` | the freeze loop (entry point: `run`)                |

## Development

```sh
uv run ruff check vstrainer/   # lint (ruff "ALL", clean)
uv run ruff format vstrainer/  # format
uv run ty check vstrainer/     # type-check (clean)
```
