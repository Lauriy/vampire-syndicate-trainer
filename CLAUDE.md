# CLAUDE.md

Memory trainer for *Vampire Syndicate: Gangs of Moonfall* (UE5). Pins the player gang's
Move/AP to a sentinel by writing the game's process memory. Python + `pymem` + `numpy`,
managed with `uv`. Windows-only (Win32 + ctypes). Entry point: `uv run vstrainer`
(= `vstrainer.ue_trainer:run`).

## Working here

- **Verifying a change needs the game running.** There is no mock. After edits, run the
  fast checks below, then a live smoke test (attach + `build_objects` + `find_blocks` +
  `_gang_fingerprints`). `_gang_fingerprints` returns `set()` when you're not in a battle —
  that's expected, not a bug.
- **There is a pure-logic test you can run without the game:** `scanner._blocks_in_region`
  against a hand-built buffer (see git history / the smoke snippets). Use it to check the
  structural scan after touching `scanner.py` or the record-layout constants.
- **Quality gates (must stay green):**
  ```sh
  uv run ruff check vstrainer/   # ruff select = ["ALL"]
  uv run ruff format vstrainer/
  uv run ty check vstrainer/
  ```

## Architecture (data flow)

`process` (attach, pick real PID) → `memscan` (filtered, optionally threaded RPM reads) →
`uemem.UE` (UE reflection: GNames decode + cached object table) and `scanner` (find
stat-record blocks by structure) → `ue_trainer` (the freeze loop). **All magic numbers and
offsets live in `constants.py`** (imported everywhere as `from . import constants as c`).

The loop: derive each gang member's **fingerprint** (attribute stats that don't *deplete*
in combat — see the staleness note below), then every tick structurally scan the heap and
write the sentinel to **every** record block whose fingerprint matches. Pinning *all*
copies is deliberate — records exist as many byte-identical copies and only the
authoritative one drives the display, so a broad fingerprint pin is what reliably hits it.

## Non-obvious facts — READ before "improving" the approach

- **Static pointer paths and code patches are DEAD ENDS for this game** (both thoroughly
  tried). Stats are pooled value-types bulk-copied via CRT `memcpy`/`memset`; there is no
  persistent pointer to a value, and "what writes to it" is the generic CRT, not game code.
  Don't reopen these.
- **The display is driven by the `Stat.Move`/`Stat.ActionPoints` *double* records, not by
  the `BP_Unit_C` Int properties.** The unit object's `Move`/`ActionPoints` ints are a
  separate cache that does *not* update the screen — writing them looks like it works only
  because of leftover record-level pins. Pin the records.
- **`unit + 0x4c8` points to only ONE (scratch) copy** of a unit's record block — fine for
  reading a member's fingerprint, useless as the pin target. Hence, the broad fingerprint pin.
- **Gang identity = `bAiControlled` (BP_Unit_C +0x310): 0 = player, 1 = enemy.** Derive
  fingerprints by scanning the object table's `"Unit"`-named objects directly; do **not**
  rely on UClass super-chain derivation — it flakily drops `BP_Unit_Stats_Anim_C` (the
  record-bearing class) across process restarts.
- **Unit objects churn:** a fresh object spawns when a unit becomes active each turn, so a
  static unit list goes stale. The fingerprint approach sidesteps this (it scans records,
  not units, each tick).
- **A fingerprint is NOT permanently stable — it survives a battle, not leveling.** The
  `FP_INDICES` stats (Initiative/Melee/Ranged/PHY) don't *deplete* in combat, so they hold
  for a whole battle, but they *do* change when you level a unit between battles. So a cached
  fingerprint (and `gang_fps.json`) can go stale. `_self_heal` handles this: on a full scan
  it watches for a cached fingerprint that never matches while others do (one member leveled)
  and re-derives via `_refresh_gang(merge=True)` — merge so a glitchy rebuild can't drop a
  live member. Total-gang-loss (new battle) instead re-derives with `merge=False` (replace).
  Don't "simplify" this back to deriving fingerprints once.

## Gotchas

- **Memory / OOM (this once froze the machine, twice).** Never retain a full snapshot.
  `build_objects` *streams* the heap via `memscan.stream` in bounded batches — at most
  `STREAM_BATCH_BYTES` (256 MB) of region data is held at once, capped at
  `OBJECT_BUILD_REGION_CAP` (64 MB) per region; it never materializes the whole ~5 GB heap.
  The per-tick full scan is capped at 256 KB/region. Run **one** trainer instance, and don't
  reintroduce a snapshot that holds every region (an earlier `UE.regs` did, and it froze the
  machine).
- **Threading is workload-dependent — always measure.** Threaded RPM is ~3× faster for the
  build (large regions, ~5 GB) but ~2× *slower* for the tiny-region per-tick scans; those
  use `threads=1`. `find_blocks` is single-threaded on purpose. Don't blanket-thread.
- **Write-after-free.** At battle teardown record blocks get freed; the write loop
  re-checks the flags qword immediately before each write (`_is_record`) to avoid corrupting
  reused heap (which previously crashed the game).
- **Offsets are build-specific.** Everything in `constants.py` (UObject/FName/GNames offsets,
  `GNAMES_BLOCKS_REL`, `UNIT_*`) is for the current game build. A patch can move them.
- **The game launches two instances** (one stray); `pick_game_pid` chooses the one with the
  big private heap. **The sentinel persists into the save file.** Debugging the game under a
  breakpoint tends to crash it.

## Python 3.14 (don't mistake new syntax for bugs)

This project runs on **Python 3.14** (`uv run python --version`). A couple of 3.14 features
appear in the code and look like mistakes if you're used to older Python:

- **PEP 758 — `except A, B:` without parentheses.** `except OSError, json.JSONDecodeError:`
  in `ue_trainer._load_gang_fps` is valid 3.14 and catches *both* (equivalent to
  `except (OSError, json.JSONDecodeError):`). It is **not** the Python-2 `except E, name:`
  bind form (that needed `as`). Don't "fix" it — `ruff`/`ty` target 3.14 and accept it.
- **PEP 649/749 — deferred annotation evaluation.** Annotations are lazily evaluated now.
  We still keep `from __future__ import annotations` at the top of every module (stringizes
  annotations at compile time); it's harmless alongside PEP 649 and keeps `TYPE_CHECKING`-only
  imports cheap. Keep it for consistency.
- **PEP 765 — control flow in `finally`.** 3.14 now *warns* on `return`/`break`/`continue`
  that leaves a `finally`. `run()`'s `finally` only does cleanup (`ue.close()`), so it's fine
  — but don't add a `return`/`continue` inside a `finally`.

If a `requires-python` bump is ever needed, it lives in `pyproject.toml`.

## Conventions (keep these)

- `ruff` is set to `ALL`. The only ignores are justified in `pyproject.toml`
  (D203/D213 rule-conflicts, COM812 = formatter, T201 = it's a CLI, BLE001 = best-effort
  memory reads). Don't add ignores casually; fix the finding instead.
- **Fully typed** (`ty` clean), **descriptive names** (no single-letter locals), docstrings
  on public API, `pathlib` not `os.path`, JSON not pickle for caches, keyword-only boolean
  params.
- **Owner style preference: a blank line before a `return`** unless it's the function's only
  line / a one-line guard. No lint rule enforces this — apply it by hand.
- Caches live in `.uecache/` (gitignored): `objs_<pid>_<base>.json` (object table) and
  `gang_fps.json` (fingerprints, lets a restart skip the build). `build_objects` prunes
  dead-PID tables.
