"""Locate every character's stat block by its STRUCTURAL signature, each session.

Why structural (not stat IDs): the stat-id values are FName indices that SHIFT by
a constant every launch, so they can't be matched across runs. What IS stable is
the record shape -- a stat block is a contiguous run of 24-byte records
    { stat_id: u64, value: double, flags: u64 }
where every record's `flags` low 32 bits == 0xFFFFFFFF. We find runs of those and
read stats by POSITION (the index -> stat map lives in constants.STAT_POSITIONS).

This sidesteps dynamic addresses (re-scan whenever needed) AND ASLR/launch shifts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from . import constants as c
from . import memscan

if TYPE_CHECKING:
    from .process import Game

_FLAG = np.uint64(c.RECORD_FLAGS_LOW)
_LOW32 = np.uint64(0xFFFFFFFF)
_STRIDE = c.RECORD_STRIDE_QWORDS


@dataclass
class StatBlock:
    """One character's stat-record block found in the heap."""

    object_start: int  # address of record 0's id qword
    region_base: int  # MEM region it was found in (for hot rescans)
    values: dict[int, float] = field(default_factory=dict)  # stat index -> value

    @property
    def movement(self) -> float:
        """Current movement points (stat index 1)."""
        return self.values.get(1, float("nan"))

    @property
    def action(self) -> float:
        """Current action points (stat index 2)."""
        return self.values.get(2, float("nan"))

    def addr(self, index: int) -> int:
        """Return the address of stat `index`'s value within this block."""
        return c.stat_value_addr(self.object_start, index)

    @property
    def move_addr(self) -> int:
        """Address of the movement value."""
        return self.addr(1)

    @property
    def action_addr(self) -> int:
        """Address of the action-points value."""
        return self.addr(2)


# stat indices we read out of every block (must be < MIN_RECORDS)
_READ_INDICES = tuple(i for i in c.STAT_POSITIONS if i < c.MIN_RECORDS)


def _value_qword_from_flags(index: int) -> int:
    """Return the qword index of stat `index`'s value, relative to a flags qword."""
    return index * _STRIDE + c.STAT_VALUE_QWORD - c.STAT_FLAGS_QWORD


def _blocks_in_region(region_base: int, data: bytes) -> list[StatBlock]:
    """Find every stat block in one region's bytes."""
    qword_count = len(data) // c.QWORD
    if qword_count < c.MIN_RECORDS * _STRIDE:
        return []
    qwords = np.frombuffer(data[: qword_count * c.QWORD], dtype=np.uint64)
    doubles = qwords.view(np.float64)  # same buffer, read values as doubles
    is_flag = (qwords & _LOW32) == _FLAG  # every record's flags field is True here

    # A block's record-0 flags sit at qword f (>= STAT_FLAGS_QWORD so object_start
    # exists). Require `is_flag` at f, f+STRIDE, ... for MIN_RECORDS records, and
    # that f-STRIDE is NOT a flag (so f is the run START = record 0, not mid-block).
    starts_mask = is_flag.copy()
    for step in range(1, c.MIN_RECORDS):  # records 1 to N-1 must also flag (in-place)
        limit = qword_count - _STRIDE * step
        starts_mask[:limit] &= is_flag[_STRIDE * step :]
        starts_mask[limit:] = False
    starts_mask[_STRIDE:] &= ~is_flag[:-_STRIDE]  # f-STRIDE must NOT be a flag
    starts_mask[: c.STAT_FLAGS_QWORD] = (
        False  # object_start = f - STAT_FLAGS_QWORD must exist
    )
    starts_mask[qword_count - _STRIDE * c.MIN_RECORDS :] = (
        False  # room to read all records
    )

    starts = np.nonzero(starts_mask)[0]
    if not starts.size:
        return []
    # Reject false matches (e.g. runs of 0xFF-filled memory whose low32 is also
    # 0xFFFFFFFF): a real stat block's values are FINITE, sane doubles.
    health = doubles[starts + _value_qword_from_flags(0)]
    move = doubles[starts + _value_qword_from_flags(1)]
    action = doubles[starts + _value_qword_from_flags(2)]
    is_real = (
        np.isfinite(health)
        & np.isfinite(move)
        & np.isfinite(action)
        & (np.abs(move) < c.MAX_DEPLETING_STAT)
        & (np.abs(action) < c.MAX_DEPLETING_STAT)
        & (np.abs(health) < c.MAX_HEALTH_STAT)
    )
    starts = starts[is_real]

    blocks = []
    for start in starts.tolist():
        obj_qword = start - c.STAT_FLAGS_QWORD
        values = {
            i: float(doubles[obj_qword + i * _STRIDE + c.STAT_VALUE_QWORD])
            for i in _READ_INDICES
        }
        blocks.append(StatBlock(region_base + obj_qword * c.QWORD, region_base, values))

    return blocks


def find_blocks(
    game: Game, regions: list[tuple[int, bytes]] | None = None
) -> list[StatBlock]:
    """Return all character stat blocks.

    `regions` (list of (base, bytes)) restricts the scan to a pre-fetched snapshot;
    otherwise snapshot private heap memory. Single-threaded on purpose: the many
    small-region numpy passes are dominated by per-task overhead, so a thread pool
    measured ~2x slower than this loop.
    """
    if regions is None:
        regions = memscan.snapshot(
            game.pm, private_only=True, max_region=c.STAT_REGION_MAX, threads=1
        )
    blocks: list[StatBlock] = []
    for region_base, data in regions:
        blocks.extend(_blocks_in_region(region_base, data))

    return blocks
