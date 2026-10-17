"""Return unused native heap pages before starting another heavy workload."""
from __future__ import annotations

import ctypes
import gc
import sys


def release_unused_memory() -> None:
    """Keep live caches intact; unsupported allocators remain a no-op."""
    if sys.platform != "linux":
        return
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return
    # Polars/DuckDB temporary allocations can leave free pages in thread arenas.
    # Dropping Python references alone does not return those pages to the cgroup.
    gc.collect()
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    trim(0)
