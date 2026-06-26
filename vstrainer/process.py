"""Process attachment and game-instance selection, built on pymem."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import TYPE_CHECKING

import pymem
import pymem.exception
import pymem.process

from . import constants as c

if TYPE_CHECKING:
    from typing import Any

# kernel32's functions are resolved dynamically; type the handle as Any so static
# analysis doesn't flag VirtualQueryEx / ReadProcessMemory as unresolved.
kernel32: Any = ctypes.windll.kernel32


class Game:
    """A pymem handle to the running game process."""

    def __init__(
        self, process_name: str = c.PROCESS_NAME, pid: int | None = None
    ) -> None:
        """Attach to `pid` if given, else to the first process named `process_name`."""
        self.process_name = process_name
        if pid is not None:
            self.pm = pymem.Pymem()
            self.pm.open_process_from_id(pid)
        else:
            self.pm = pymem.Pymem(process_name)
        self.pid = self.pm.process_id
        self.base = pymem.process.module_from_name(
            self.pm.process_handle, c.BASE_MODULE
        ).lpBaseOfDll

    def close(self) -> None:
        """Close the process handle."""
        self.pm.close_process()


def find_pids(process_name: str = c.PROCESS_NAME) -> list[int]:
    """Return every PID whose exe matches `process_name` (the game runs >1 instance)."""
    return [
        proc.th32ProcessID
        for proc in pymem.process.list_processes()
        if proc.szExeFile.decode(errors="ignore").lower() == process_name.lower()
    ]


class _MBI(ctypes.Structure):
    """MEMORY_BASIC_INFORMATION, as filled by VirtualQueryEx."""

    _fields_ = (
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__pad", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    )


def _private_bytes(pid: int) -> int:
    """Estimate a process's committed private-memory footprint (no full read)."""
    try:
        game = Game(pid=pid)
    except pymem.exception.PymemError:
        return 0
    handle = game.pm.process_handle
    virtual_query = kernel32.VirtualQueryEx
    mbi, addr, total = _MBI(), 0, 0
    while virtual_query(
        handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
    ):
        if mbi.State == c.MEM_COMMIT and mbi.Type == c.MEM_PRIVATE:
            total += mbi.RegionSize
        addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        if addr > c.ADDRESS_CEILING:
            break
    game.close()

    return total


def pick_game_pid() -> int:
    """Pick the real game instance: the one with the big private heap.

    The game launches a tiny stray duplicate alongside the real process.
    """
    pids = find_pids()
    if not pids:
        msg = f"Could not find process {c.PROCESS_NAME!r}. Is the game running?"
        raise SystemExit(msg)
    if len(pids) == 1:
        return pids[0]

    return max(pids, key=_private_bytes)


def attach(pid: int | None = None) -> Game:
    """Attach to the game; with no pid, auto-pick the real instance."""
    if pid is None:
        pid = pick_game_pid()

    return Game(pid=pid)
