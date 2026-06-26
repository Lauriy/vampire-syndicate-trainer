"""Read committed process memory into the trainer, fast.

The filters that make it fast: private-heap-only, per-region size caps, a
hot-region predicate, and a threaded read (ReadProcessMemory releases the GIL,
so regions read in parallel). `stream` reads in bounded batches so the whole
snapshot is never held at once (the object-table build would otherwise OOM).
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import functools
from typing import TYPE_CHECKING

from . import constants as c
from .process import _MBI, kernel32

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import pymem


def _iter_regions(
    pm: pymem.Pymem,
    *,
    private_only: bool,
    keep: Callable[[int, int], bool] | None,
    max_region: int | None,
) -> Iterator[tuple[int, int]]:
    """Yield (base, size) for each committed, readable region worth reading.

    VirtualQueryEx is cheap; the RPM of the buffers is the slow part.
    """
    handle = pm.process_handle
    virtual_query = kernel32.VirtualQueryEx
    cap = max_region if max_region is not None else c.DEFAULT_REGION_CAP
    mbi, addr = _MBI(), 0
    while virtual_query(
        handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
    ):
        size, base = mbi.RegionSize, (mbi.BaseAddress or 0)
        if (
            mbi.State == c.MEM_COMMIT
            and (mbi.Protect & c.PAGE_PROTECT_MASK) in c.READABLE_PROTECT
            and not (mbi.Protect & c.PAGE_GUARD)
            and size <= cap
            and not (private_only and mbi.Type != c.MEM_PRIVATE)
            and (keep is None or keep(base, size))
        ):
            yield base, size
        addr = base + size
        if addr > c.ADDRESS_CEILING:
            break


def _read_region(handle: int, meta: tuple[int, int]) -> tuple[int, bytes] | None:
    """Read one (base, size) region's bytes, or None if the read fails."""
    base, size = meta
    buf = (ctypes.c_char * size)()
    got = ctypes.c_size_t(0)
    if (
        kernel32.ReadProcessMemory(
            handle, ctypes.c_void_p(base), buf, size, ctypes.byref(got)
        )
        and got.value
    ):
        return base, buf.raw[: got.value]

    return None


def _read_all(
    read: Callable[[tuple[int, int]], tuple[int, bytes] | None],
    metas: list[tuple[int, int]],
    threads: int,
) -> list[tuple[int, bytes] | None]:
    """Read every region's bytes, threaded when `threads > 1`."""
    if threads <= 1:
        return [read(meta) for meta in metas]
    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        return list(pool.map(read, metas))


def snapshot(
    pm: pymem.Pymem,
    *,
    private_only: bool = False,
    keep: Callable[[int, int], bool] | None = None,
    max_region: int | None = None,
    threads: int = c.SNAPSHOT_THREADS,
) -> list[tuple[int, bytes]]:
    """Read matching regions into a list of (base, bytes), holding them all.

    private_only -- skip MEM_IMAGE/MEM_MAPPED (DLLs, files); stat blocks are heap.
    keep         -- only read regions this predicate accepts (hot-region rescans).
    max_region   -- skip regions larger than this; stat blocks are in small heap
                    regions, while the multi-GB regions are textures/meshes.
    threads      -- parallel RPM workers (1 = sequential).
    """
    handle = pm.process_handle
    if handle is None:
        return []
    read = functools.partial(_read_region, handle)
    metas = list(
        _iter_regions(pm, private_only=private_only, keep=keep, max_region=max_region)
    )
    regions = _read_all(read, metas, threads)

    return [region for region in regions if region is not None]


def stream(
    pm: pymem.Pymem,
    *,
    private_only: bool = False,
    max_region: int | None = None,
    threads: int = c.SNAPSHOT_THREADS,
    batch_bytes: int = c.STREAM_BATCH_BYTES,
) -> Iterator[tuple[int, bytes]]:
    """Yield (base, bytes) regions, reading in bounded threaded batches.

    Never holds more than ~`batch_bytes` of region data at once, so a caller that
    consumes-and-discards (the object-table build) stays memory-bounded.
    """
    handle = pm.process_handle
    if handle is None:
        return
    read = functools.partial(_read_region, handle)
    batch: list[tuple[int, int]] = []
    pending = 0
    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        for meta in _iter_regions(
            pm, private_only=private_only, keep=None, max_region=max_region
        ):
            batch.append(meta)
            pending += meta[1]
            if pending >= batch_bytes:
                yield from (
                    region for region in pool.map(read, batch) if region is not None
                )
                batch, pending = [], 0
        if batch:
            yield from (
                region for region in pool.map(read, batch) if region is not None
            )
