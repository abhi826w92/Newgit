"""
Python Ctypes Integration for C++ Native Core Engine
Provides microsecond fuzzy search scoring and high-performance app sorting.
"""

import os
import ctypes
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CANDIDATE_PATHS = [
    "/data/data/com.termux/files/home/.mystore_native/libengine.so",
    "/data/data/com.termux/files/usr/lib/libengine.so",
    os.path.join(BASE_DIR, "cpp_core", "libengine.so"),
]

_cpp_lib = None
HAS_CPP = False

for path in CANDIDATE_PATHS:
    if os.path.exists(path):
        try:
            _cpp_lib = ctypes.CDLL(path)
            _cpp_lib.cpp_fuzzy_score.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            _cpp_lib.cpp_fuzzy_score.restype = ctypes.c_double
            _cpp_lib.cpp_crc32.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            _cpp_lib.cpp_crc32.restype = ctypes.c_uint32
            _cpp_lib.cpp_engine_ping.argtypes = []
            _cpp_lib.cpp_engine_ping.restype = ctypes.c_int

            if _cpp_lib.cpp_engine_ping() == 200:
                HAS_CPP = True
                print(f"🚀 [C++ Core Engine] Native Acceleration Library Loaded: {path}")
                break
        except Exception:
            continue


def fast_fuzzy_score(query: str, target: str) -> float:
    """Calculate string similarity score (0.0 to 1.0) using C++ native engine"""
    if not query or not target:
        return 0.0
    if HAS_CPP and _cpp_lib:
        try:
            return float(_cpp_lib.cpp_fuzzy_score(query.encode("utf-8"), target.encode("utf-8")))
        except Exception:
            pass
    # Pure Python Fallback
    q_low, t_low = query.lower(), target.lower()
    if q_low in t_low or t_low in q_low:
        return 0.9
    return 0.0


def fast_crc32_hash(data_bytes: bytes) -> str:
    """Calculate CRC32 checksum of bytes using C++ native engine"""
    if HAS_CPP and _cpp_lib:
        try:
            val = _cpp_lib.cpp_crc32(data_bytes, len(data_bytes))
            return f"{val:08X}"
        except Exception:
            pass
    import zlib
    return f"{zlib.crc32(data_bytes) & 0xffffffff:08X}"


def sort_apps_by_priority(apps: list) -> list:
    """
    High-performance comparator:
    1. Pinned apps on Top (sorted by pinnedOrder asc)
    2. Featured apps next
    3. Recently updated apps (updatedDate desc)
    """
    def app_sort_key(app):
        is_pinned = 0 if app.get("pinned") else 1
        pinned_order = app.get("pinnedOrder", 9999) if app.get("pinned") else 9999
        is_featured = 0 if app.get("featured") else 1
        updated = app.get("updatedDate", "1970-01-01")
        return (is_pinned, pinned_order, is_featured, -int(updated.replace("-", "") if updated.replace("-", "").isdigit() else 0))

    return sorted(apps, key=app_sort_key)
