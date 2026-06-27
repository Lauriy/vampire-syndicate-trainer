"""Set-and-forget gang trainer.

Identify the gang via UE reflection, then pin the values that actually drive the
display (the stat-record doubles), using a fingerprint to catch the authoritative
record among its many byte-identical copies:

  1. From the object table, find unit objects with bAiControlled == 0 (your gang)
     that have a stat-record pointer at unit+UNIT_RECORD_PTR, and read each member's
     fingerprint from attribute stats (Initiative/Melee/Ranged/PHY). These don't
     DEPLETE in combat, so a fingerprint holds for a whole battle -- but they DO
     change when you level a unit, so a stale one gets re-derived (see step 3).
  2. Each tick, structurally scan the heap for stat-record blocks (fast: hot-region
     rescans between occasional full scans) and pin Stat.Move / Stat.ActionPoints to
     the sentinel in EVERY block whose fingerprint matches a gang member. Pinning all
     copies guarantees the authoritative one (the display driver) is hit; enemies
     (different fingerprints) are never touched.
  3. Self-heal: re-derive fingerprints when the gang goes missing (new battle) or a
     member stops matching (it was leveled, so its fingerprint went stale), and
     re-attach when the game restarts.

No value typing, no PID hunting, no enemy collisions.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pymem.exception

from . import constants as c
from . import memscan, scanner
from .process import find_pids, pick_game_pid
from .uemem import CACHE_DIR, UE

if TYPE_CHECKING:
    from .scanner import StatBlock

GANG_FPS_PATH = CACHE_DIR / "gang_fps.json"

Fingerprint = tuple[float, ...]


class _ProcessGoneError(Exception):
    """The attached game process died (e.g. a restart)."""


@dataclass
class _Tick:
    """Outcome of one scan-and-pin pass."""

    gang_blocks: list[StatBlock] = field(default_factory=list)
    reverted: int = 0
    addresses: set[int] = field(default_factory=set)
    matched_fps: set[Fingerprint] = field(default_factory=set)
    hot: set[int] = field(default_factory=set)
    n_blocks: int = 0
    snap_s: float = 0.0
    scan_s: float = 0.0
    appeared: int = 0  # copies that appeared since last tick (heap churn)
    gone: int = 0  # copies that disappeared since last tick


@dataclass
class _HealState:
    """Full-scan counters that decide when to re-derive fingerprints."""

    empty_fulls: int = 0  # consecutive full scans with the gang fully absent
    incomplete_fulls: int = 0  # consecutive full scans missing a cached fingerprint
    last_rebuild: float = 0.0  # rate-limits rebuilds (REBUILD_MIN_INTERVAL)


def _u8(ue: UE, addr: int) -> int | None:
    try:
        return ue.pm.read_uchar(addr)
    except pymem.exception.PymemError:
        return None


def _is_record(flags: int | None) -> bool:
    """Return whether `flags` is a live stat-record flags qword."""
    return flags is not None and (flags & c.RECORD_FLAGS_LOW) == c.RECORD_FLAGS_LOW


def _record_block(ue: UE, unit: int) -> int | None:
    """Return the unit's live stat-record block address, or None."""
    ptr = ue.u64(unit + c.UNIT_RECORD_PTR)
    if ptr and ue.is_heap(ptr) and _is_record(ue.u64(ptr + c.STAT_FLAGS_OFFSET)):
        return ptr

    return None


def _fp_of_block(block: StatBlock, region_data: dict[int, bytes]) -> Fingerprint:
    """Compute a block's stable identity fingerprint (stats that don't deplete)."""
    data = region_data[block.region_base]
    count = len(data) // c.QWORD
    start = (block.object_start - block.region_base) // c.QWORD
    doubles = np.frombuffer(data[: count * c.QWORD], dtype=np.uint64).view(np.float64)

    return tuple(
        round(
            float(doubles[start + i * c.RECORD_STRIDE_QWORDS + c.STAT_VALUE_QWORD]),
            c.FP_PRECISION,
        )
        for i in c.FP_INDICES
    )


def _gang_fingerprints(ue: UE) -> set[Fingerprint]:
    """Derive each gang member's stable fingerprint from the object table.

    Robust: scan 'Unit'-ish objects directly for bAiControlled==0 with a record
    block (class derivation misses BP_Unit_Stats_Anim_C). Re-derived from ground
    truth, so it tracks a member whose attribute stats changed (leveling) -- the
    loop calls this again once a member's old fingerprint stops matching.
    """
    zero = tuple(0.0 for _ in c.FP_INDICES)
    fingerprints: set[Fingerprint] = set()
    for addr, class_name in (ue.objs or {}).items():
        if (
            c.UNIT_CLASS_SUBSTR not in class_name
            or _u8(ue, addr + c.UNIT_AI_CONTROLLED) != 0
        ):
            continue
        block = _record_block(ue, addr)
        if not block:
            continue
        fingerprint = tuple(
            round(ue.f64(c.stat_value_addr(block, i)) or 0.0, c.FP_PRECISION)
            for i in c.FP_INDICES
        )
        if fingerprint != zero:
            fingerprints.add(fingerprint)

    return fingerprints


def _load_gang_fps() -> set[Fingerprint]:
    """Load last session's fingerprints as a warm start (may be stale; self-heals)."""
    if not GANG_FPS_PATH.exists():
        return set()
    try:
        with GANG_FPS_PATH.open() as fh:
            data = json.load(fh)
    except OSError, json.JSONDecodeError:
        return set()

    return {tuple(item) for item in data}


def _save_gang_fps(fingerprints: set[Fingerprint]) -> None:
    """Persist gang fingerprints so a re-attach can skip the object-table build."""
    with contextlib.suppress(OSError):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with GANG_FPS_PATH.open("w") as fh:
            json.dump([list(fp) for fp in fingerprints], fh)


def _alive(ue: UE) -> bool:
    """Return whether the process is still alive (one tiny read)."""
    try:
        ue.pm.read_uchar(ue.base)
    except pymem.exception.PymemError:
        return False

    return True


def _wait_for_game() -> int:
    """Block until a game process exists; return the real instance's pid."""
    announced = False
    while True:
        if find_pids():
            return pick_game_pid()
        if not announced:
            print("waiting for game process...")
            announced = True
        time.sleep(c.GAME_POLL_SECS)


def _setup_fingerprints(ue: UE) -> set[Fingerprint]:
    """Load cached fingerprints, or build the object table to derive them."""
    gang_fps = _load_gang_fps()
    if gang_fps:
        print(
            f"loaded cached gang fingerprints {sorted(gang_fps)} "
            "-- skipping object-table build"
        )

        return gang_fps
    print("building object table (threaded, cached)...")
    started = time.time()
    ue.build_objects(rebuild=True)
    gang_fps = _gang_fingerprints(ue)
    _save_gang_fps(gang_fps)
    print(f"setup {time.time() - started:.1f}s. Gang fingerprints: {sorted(gang_fps)}")

    return gang_fps


def _scan_and_pin(
    ue: UE, gang_fps: set[Fingerprint], hot: set[int], *, full: bool
) -> _Tick:
    """Snapshot, find blocks, and pin every block matching a gang fingerprint."""
    cap = c.FULL_SCAN_REGION_CAP if full else c.STAT_REGION_MAX
    keep = None if full else (lambda base, _size: base in hot)
    started = time.time()
    regions = memscan.snapshot(
        ue.pm, private_only=True, max_region=cap, keep=keep, threads=1
    )
    snap_s = time.time() - started
    started = time.time()
    blocks = scanner.find_blocks(ue.g, regions)
    region_data = dict(regions)
    scan_s = time.time() - started

    result = _Tick(n_blocks=len(blocks), snap_s=snap_s, scan_s=scan_s)
    for block in blocks:
        fingerprint = _fp_of_block(block, region_data)
        if fingerprint in gang_fps:
            result.matched_fps.add(fingerprint)
            result.gang_blocks.append(block)
            result.addresses.add(block.object_start)
            result.hot.add(block.region_base)
            if block.movement < c.SENTINEL_MIN or block.action < c.SENTINEL_MIN:
                result.reverted += 1
    for block in result.gang_blocks:
        with contextlib.suppress(pymem.exception.PymemError):
            # write-after-free guard: re-confirm the block is still a stat record
            # (flags intact) right before writing -- at battle-end it may have been
            # freed/reused, and a stray write corrupts the heap.
            if _is_record(ue.u64(block.object_start + c.STAT_FLAGS_OFFSET)):
                ue.pm.write_double(block.move_addr, c.SENTINEL)
                ue.pm.write_double(block.action_addr, c.SENTINEL)

    return result


def _refresh_gang(
    ue: UE, gang_fps: set[Fingerprint], last_rebuild: float, *, merge: bool
) -> tuple[set[Fingerprint], float]:
    """Rebuild the object table and re-derive gang fingerprints (rate-limited).

    merge -- union the fresh fingerprints into the existing set instead of
    replacing it, so a partial or glitchy rebuild can never drop a member that is
    currently being pinned. Used for the stale-fingerprint (leveled) case.
    """
    if time.time() - last_rebuild <= c.REBUILD_MIN_INTERVAL or not _alive(ue):
        return gang_fps, last_rebuild
    ue.build_objects(rebuild=True)
    fresh = _gang_fingerprints(ue)
    if fresh:
        gang_fps = (gang_fps | fresh) if merge else fresh
        _save_gang_fps(gang_fps)

    return gang_fps, time.time()


def _self_heal(
    ue: UE, gang_fps: set[Fingerprint], result: _Tick, heal: _HealState
) -> set[Fingerprint]:
    """On a full scan, re-derive fingerprints when the gang looks lost or stale.

    Counters advance only on full scans (hot ticks leave them alone), so the
    thresholds count consecutive full scans. Two failure modes self-heal:
      * gang fully absent (battle/gang changed) -> rebuild and REPLACE the set.
      * a cached fingerprint never matched while others did -> that member was
        likely leveled (its 'stable' stats changed), so its fingerprint is stale;
        rebuild and MERGE, adding the new fingerprint without dropping anyone.
    """
    if not result.gang_blocks:
        heal.empty_fulls += 1
        heal.incomplete_fulls = 0
    elif gang_fps - result.matched_fps:
        heal.incomplete_fulls += 1
        heal.empty_fulls = 0
    else:
        heal.empty_fulls = heal.incomplete_fulls = 0

    if heal.empty_fulls >= c.EMPTY_FULLS_BEFORE_REBUILD:
        gang_fps, heal.last_rebuild = _refresh_gang(
            ue, gang_fps, heal.last_rebuild, merge=False
        )
    elif heal.incomplete_fulls >= c.INCOMPLETE_FULLS_BEFORE_REBUILD:
        gang_fps, heal.last_rebuild = _refresh_gang(
            ue, gang_fps, heal.last_rebuild, merge=True
        )

    return gang_fps


def _print_tick(tick: int, result: _Tick, hot: set[int], *, full: bool) -> None:
    """Print one debug line for the current tick."""
    non_sentinel = sorted(
        {
            (round(block.movement), round(block.action))
            for block in result.gang_blocks
            if block.movement < c.SENTINEL_MIN or block.action < c.SENTINEL_MIN
        }
    )
    print(
        f"t{tick:>4} {'FULL' if full else 'hot '} "
        f"snap={result.snap_s:5.2f}s scan={result.scan_s:4.2f}s | "
        f"blocks={result.n_blocks:>5} gang_copies={len(result.gang_blocks):>3} "
        f"reverted={result.reverted:>2} regions={len(hot):>3} "
        f"churn(+{result.appeared}/-{result.gone}) "
        f"{'pinned non-sentinel=' + str(non_sentinel) if non_sentinel else ''}"
    )


def _freeze_loop(ue: UE, *, debug: bool) -> None:
    """Pin the gang until the process dies (-> raises _ProcessGoneError)."""
    gang_fps = _setup_fingerprints(ue)
    print("Pinning ALL record copies matching a gang fingerprint.\n")

    hot, prev_addresses, tick, stable = set(), set(), 0, 0
    heal = _HealState()
    while True:
        loop_start = time.time()
        if not _alive(ue):
            raise _ProcessGoneError
        # full rescan every N ticks (catches new copies after a turn change);
        # hot-region rescans between. Adaptive interval self-tunes the rate.
        full = (tick % c.FULL_RESCAN_EVERY == 0) or not hot
        result = _scan_and_pin(ue, gang_fps, hot, full=full)
        if result.hot:
            hot = result.hot
        if full:  # only full scans see the whole gang, so heal off them
            gang_fps = _self_heal(ue, gang_fps, result, heal)
        result.appeared = len(result.addresses - prev_addresses)
        result.gone = len(prev_addresses - result.addresses)
        prev_addresses = result.addresses
        # adaptive cadence: changes (turn/battle) -> stay fast; quiet -> slow down
        changed = (
            result.reverted > 0
            or result.appeared
            or result.gone
            or not result.gang_blocks
        )
        stable = 0 if changed else stable + 1
        if debug and (full or changed or tick < c.DEBUG_INITIAL_TICKS):
            _print_tick(tick, result, hot, full=full)
        tick += 1
        interval = c.TICK_SLOW if stable >= c.STABLE_TICKS_FOR_SLOW else c.TICK_FAST
        time.sleep(max(0, interval - (time.time() - loop_start)))


def run(pid: int | None = None, *, debug: bool = True) -> None:
    """Attach and pin the gang, re-attaching automatically when the game restarts."""
    try:
        while True:
            target = pid if pid is not None else _wait_for_game()
            try:
                ue = UE(pid=target)
            except pymem.exception.PymemError as exc:
                print(f"attach failed ({exc}); retrying...")
                time.sleep(c.GAME_POLL_SECS)
                continue
            print(f"attached pid={ue.g.pid} base={ue.base:#x}")
            try:
                _freeze_loop(ue, debug=debug)
            except _ProcessGoneError:
                print("game process gone (restart?) -- re-detecting...")
            finally:
                with contextlib.suppress(pymem.exception.PymemError):
                    ue.close()
            pid = None  # after the first death, always auto-detect the new instance
            time.sleep(c.REATTACH_DELAY_SECS)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    run()
