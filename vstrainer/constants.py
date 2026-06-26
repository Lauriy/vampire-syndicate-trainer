"""All reverse-engineered constants for the trainer, in one place.

Centralizing the magic numbers/strings lets the rest of the code read in domain
terms. Offsets are for this specific game build (UE5).
"""

# --- process ---------------------------------------------------------------
PROCESS_NAME = "VampireSyndicate.exe"  # UE5 (window title reports "PCD3D_SM6")
BASE_MODULE = PROCESS_NAME  # pointers/offsets are relative to the exe

# --- Win32 memory (VirtualQueryEx / ReadProcessMemory) ---------------------
MEM_COMMIT = 0x1000  # MEMORY_BASIC_INFORMATION.State
MEM_PRIVATE = 0x20000  # .Type: heap/VirtualAlloc (not image/mapped)
PAGE_GUARD = 0x100  # .Protect guard bit (skip these regions)
PAGE_PROTECT_MASK = 0xFF  # low byte of .Protect = base protection
READABLE_PROTECT = frozenset({0x02, 0x04, 0x20, 0x40})  # R / RW / ExecR / ExecRW
ADDRESS_CEILING = 0x7FFFFFFFFFFF  # top of the user-mode address walk
DEFAULT_REGION_CAP = 256 * 1024 * 1024  # default per-region read cap

# Plausible heap-pointer range (heap sits below the module space on Win64).
HEAP_MIN = 0x1000000
HEAP_MAX = 0x700000000000

QWORD = 8
DWORD = 4

# --- stat-record block: contiguous { id:u64, value:f64, flags:u64 } records --
STAT_RECORD_SIZE = 0x18
STAT_VALUE_OFFSET = 0x08  # value sits right after the id
STAT_FLAGS_OFFSET = 0x10  # flags after the value
RECORD_FLAGS_LOW = 0xFFFFFFFF  # every real record's flags low 32 bits
RECORD_STRIDE_QWORDS = STAT_RECORD_SIZE // QWORD  # 3 qwords per record
STAT_VALUE_QWORD = STAT_VALUE_OFFSET // QWORD  # value is 1 qword past the id
STAT_FLAGS_QWORD = STAT_FLAGS_OFFSET // QWORD  # flags is 2 qwords past the id
MIN_RECORDS = 6  # contiguous records required to accept a block
STAT_POSITIONS = {0: "health", 1: "movement", 2: "action", 3: "reserve", 5: "melee"}
STAT_REGION_MAX = 4 * 1024 * 1024  # blocks live in small heap regions
# sane-value bounds to reject 0xFF-filled false matches
MAX_DEPLETING_STAT = 1e7  # move/action are small
MAX_HEALTH_STAT = 1e9


def stat_value_addr(object_start: int, index: int) -> int:
    """Value address of stat record `index` within a block at `object_start`."""
    return object_start + index * STAT_RECORD_SIZE + STAT_VALUE_OFFSET


# --- UE reflection: UObject / FName / GNames -------------------------------
UOBJECT_CLASS_OFFSET = 0x10  # UObject.ClassPrivate (UClass*)
UOBJECT_NAME_OFFSET = 0x18  # UObject.NamePrivate (FName ComparisonIndex, u32)
GNAMES_BLOCKS_REL = 0x10132910  # exe + this = FNamePool.Blocks[0]
GNAMES_BLOCK_COUNT = 64  # block pointers to read from the pool
FNAME_BLOCK_SHIFT = 16  # FName id -> block = id >> 16
FNAME_OFFSET_MASK = 0xFFFF  #          -> offset = id & 0xFFFF
FNAME_ENTRY_STRIDE = 2  # entry = block + 2 * offset
FNAME_HEADER_SIZE = 2  # uint16 header before the string bytes
FNAME_LEN_SHIFT = 6  # header: Len = hdr >> 6
FNAME_WIDE_BIT = 0x1  #         bIsWide = hdr & 1
FNAME_MAX_LEN = 64
NAME_PATTERN = rb"^[A-Za-z_][A-Za-z0-9_/.]{1,63}$"
OBJECT_BUILD_REGION_CAP = 64 * 1024 * 1024  # cap for the object-table snapshot
UNIT_CLASS_SUBSTR = "Unit"  # class-name filter for candidate unit objects

# --- BP_Unit_C properties (this build) -------------------------------------
UNIT_AI_CONTROLLED = 0x310  # bAiControlled (0 = player gang, 1 = enemy)
UNIT_RECORD_PTR = 0x4C8  # -> the unit's live stat-record block

# --- pin / fingerprint -----------------------------------------------------
SENTINEL = 9_999_999.0  # frozen move/AP value
SENTINEL_MIN = 900_000.0  # "already pinned" threshold
FP_INDICES = (4, 5, 6, 7)  # Initiative/Melee/Ranged/PHY — stable per unit
FP_PRECISION = 1  # rounding digits for fingerprint match

# --- freeze-loop tuning ----------------------------------------------------
FULL_RESCAN_EVERY = 15  # ticks between full heap rescans
FULL_SCAN_REGION_CAP = 256 * 1024  # cap for the full-scan snapshot
TICK_FAST = 0.4  # interval right after a change
TICK_SLOW = 1.5  # interval when stable
STABLE_TICKS_FOR_SLOW = 4  # quiet ticks before slowing down
DEBUG_INITIAL_TICKS = 3  # print the first few ticks regardless, for visibility
REBUILD_MIN_INTERVAL = 20.0  # s between fallback object-table rebuilds
EMPTY_FULLS_BEFORE_REBUILD = 2
SNAPSHOT_THREADS = 8  # parallel RPM workers for big (build) reads
STREAM_BATCH_BYTES = (
    256 * 1024 * 1024
)  # max region bytes held at once while streaming the build
GAME_POLL_SECS = 3.0  # while waiting for the game process / retrying attach
REATTACH_DELAY_SECS = 2.0  # pause before re-attaching after a restart
