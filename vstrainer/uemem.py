"""UE5 reflection access: GNames decoding and the cached object table.

The object table {addr -> class name} is built by streaming heap memory in
bounded batches (so it never holds the whole ~5 GB snapshot at once) and resolving
each object's class via deduplicated live reads. It is cached to disk per
(pid, base) -- UObjects are stable within a session, so it rebuilds only on a
cache miss. Name decoding is memoized. Everything else is a targeted live read.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path

import numpy as np
import pymem.exception
import pymem.process

from . import constants as c
from . import memscan
from .process import attach

_NAME_RE = re.compile(c.NAME_PATTERN)
CACHE_DIR = Path(__file__).resolve().parent.parent / ".uecache"


def _write_object_cache(path: Path, objs: dict[int, str]) -> None:
    """Write the object table and prune tables left over from dead PIDs."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(objs, fh)
    for stale in CACHE_DIR.glob("objs_*"):
        if stale != path:
            with contextlib.suppress(OSError):
                stale.unlink()


class UE:
    """Reflection view over a live UE5 process: names and the object table."""

    def __init__(self, pid: int | None = None) -> None:
        """Attach to the game and read the GNames block pointers."""
        self.g = attach(pid=pid)
        self.pm = self.g.pm
        self.base = self.g.base
        mod = pymem.process.module_from_name(self.pm.process_handle, c.BASE_MODULE)
        self.img_lo, self.img_hi = self.base, self.base + mod.SizeOfImage
        name_pool = self.base + c.GNAMES_BLOCKS_REL
        self.name_pool_blocks = [
            self.pm.read_ulonglong(name_pool + c.QWORD * k)
            for k in range(c.GNAMES_BLOCK_COUNT)
        ]
        self._name: dict[int, str | None] = {}
        self.objs: dict[int, str] | None = None  # {addr: classname}

    # -- live reads (targeted, instant) ----------------------------------
    def u64(self, addr: int) -> int | None:
        """Read an unsigned 64-bit value, or None if the read fails."""
        try:
            return self.pm.read_ulonglong(addr)
        except pymem.exception.PymemError:
            return None

    def u32(self, addr: int) -> int | None:
        """Read an unsigned 32-bit value, or None if the read fails."""
        try:
            return self.pm.read_uint(addr)
        except pymem.exception.PymemError:
            return None

    def i32(self, addr: int) -> int | None:
        """Read a signed 32-bit value, or None if the read fails."""
        try:
            return self.pm.read_int(addr)
        except pymem.exception.PymemError:
            return None

    def f64(self, addr: int) -> float | None:
        """Read a 64-bit double, or None if the read fails."""
        try:
            return self.pm.read_double(addr)
        except pymem.exception.PymemError:
            return None

    def _live_bytes(self, addr: int, size: int) -> bytes | None:
        try:
            return self.pm.read_bytes(addr, size)
        except pymem.exception.PymemError:
            return None

    def is_image(self, ptr: int | None) -> bool:
        """Return whether `ptr` points into the game's image (exe) range."""
        return ptr is not None and self.img_lo <= ptr < self.img_hi

    @staticmethod
    def is_heap(ptr: int | None) -> bool:
        """Return whether `ptr` looks like a plausible heap pointer."""
        return ptr is not None and c.HEAP_MIN <= ptr < c.HEAP_MAX

    # -- names (FName index -> string, memoized) -------------------------
    def name(self, idx: int | None) -> str | None:
        """Decode an FName index to its string, or None. Reads are memoized."""
        if idx is None:
            return None
        if idx in self._name:
            return self._name[idx]
        block = idx >> c.FNAME_BLOCK_SHIFT
        if block >= len(self.name_pool_blocks) or not self.name_pool_blocks[block]:
            return None
        entry = self.name_pool_blocks[block] + c.FNAME_ENTRY_STRIDE * (
            idx & c.FNAME_OFFSET_MASK
        )
        header = self._live_bytes(entry, c.FNAME_HEADER_SIZE)
        if not header:
            return None
        flags = int.from_bytes(header, "little")
        length = flags >> c.FNAME_LEN_SHIFT
        if not (1 <= length <= c.FNAME_MAX_LEN) or (flags & c.FNAME_WIDE_BIT):
            return None
        raw = self._live_bytes(entry + c.FNAME_HEADER_SIZE, length)
        value = raw.decode("ascii") if raw and _NAME_RE.match(raw) else None
        self._name[idx] = value

        return value

    # -- object table (cached) -------------------------------------------
    def _cache_path(self) -> Path:
        return CACHE_DIR / f"objs_{self.g.pid}_{self.base:x}.json"

    def _scan_candidates(self) -> list[tuple[int, int]]:
        """Collect (object addr, class ptr) for UObject-like objects in the heap.

        Streams regions (memory-bounded, see memscan.stream) and keeps objects
        whose first qword is an exe vtable.
        """
        cls_qword = c.UOBJECT_CLASS_OFFSET // c.QWORD  # class ptr sits at obj + 0x10
        candidates: list[tuple[int, int]] = []
        for region_base, data in memscan.stream(
            self.pm, private_only=True, max_region=c.OBJECT_BUILD_REGION_CAP
        ):
            count = len(data) // c.QWORD
            if count <= cls_qword:
                continue
            qwords = np.frombuffer(data[: count * c.QWORD], dtype=np.uint64)
            is_vtable = (qwords >= self.img_lo) & (qwords < self.img_hi)
            for i in np.nonzero(is_vtable)[0].tolist():
                if i + cls_qword >= count:
                    continue
                cls = int(qwords[i + cls_qword])
                if c.HEAP_MIN <= cls < c.HEAP_MAX:
                    candidates.append((region_base + i * c.QWORD, cls))

        return candidates

    def _resolve_class_name(self, cls: int) -> str | None:
        """Resolve a class pointer to its class name via live reads, or None."""
        if not self.is_image(self.u64(cls)):  # the class object also has an exe vtable
            return None
        name_idx = self.u32(cls + c.UOBJECT_NAME_OFFSET)
        if name_idx is None:
            return None

        return self.name(name_idx)

    def build_objects(self, *, rebuild: bool = False) -> dict[int, str]:
        """Build (or load from cache) {object address -> class name} for all UObjects.

        The heap is streamed in bounded batches and classes are resolved once each
        (deduplicated), so this stays memory-bounded -- it must never hold the full
        snapshot, which would OOM the machine.
        """
        path = self._cache_path()
        if not rebuild and path.exists():
            with path.open() as fh:
                loaded = {int(addr): name for addr, name in json.load(fh).items()}
            self.objs = loaded

            return loaded
        class_name: dict[int, str | None] = {}
        objs: dict[int, str] = {}
        for obj_addr, cls in self._scan_candidates():
            if cls not in class_name:
                class_name[cls] = self._resolve_class_name(cls)
            name = class_name[cls]
            if name:
                objs[obj_addr] = name
        self.objs = objs
        _write_object_cache(path, objs)

        return objs

    def close(self) -> None:
        """Close the underlying process handle."""
        self.g.close()
