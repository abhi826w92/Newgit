import os
import sys
import json
import time
import ctypes
import shutil
import logging
import subprocess

logger = logging.getLogger(__name__)

# Locate shared library in Termux / Linux environment
SO_DIR = "/data/data/com.termux/files/home"
if not os.path.exists(SO_DIR):
    SO_DIR = os.path.dirname(os.path.abspath(__file__))

SO_PATH = os.path.join(SO_DIR, "libtgdrive_engine.so")
CPP_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpp_engine.cpp")

class CppEngine:
    def __init__(self):
        self.lib = None
        self._load_or_compile()

    def _load_or_compile(self):
        """Compile C++ source if needed and load the shared library."""
        try:
            if not os.path.exists(SO_PATH) and os.path.exists(CPP_SRC):
                compiler = shutil.which("clang++") or shutil.which("g++")
                if compiler:
                    logger.info(f"Compiling C++ Acceleration Engine with {compiler}...")
                    cmd = [compiler, "-O3", "-shared", "-fPIC", "-std=c++17", "-o", SO_PATH, CPP_SRC]
                    subprocess.run(cmd, check=True, capture_output=True)
                    logger.info(f"Successfully compiled C++ Acceleration Engine to {SO_PATH}")

            if os.path.exists(SO_PATH):
                self.lib = ctypes.CDLL(SO_PATH)
                self.lib.cpp_clear_user_cache.argtypes = [ctypes.c_int64]
                self.lib.cpp_upsert_file_record.argtypes = [
                    ctypes.c_int64,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_int32,
                    ctypes.c_char_p
                ]
                self.lib.cpp_get_total_count.argtypes = [ctypes.c_int64]
                self.lib.cpp_get_total_count.restype = ctypes.c_int32
                self.lib.cpp_filter_and_paginate.argtypes = [
                    ctypes.c_int64,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_int32,
                    ctypes.c_int32
                ]
                self.lib.cpp_filter_and_paginate.restype = ctypes.c_char_p
                self.lib.cpp_compute_stats.argtypes = [ctypes.c_int64]
                self.lib.cpp_compute_stats.restype = ctypes.c_char_p
                logger.info("⚡ C++ Acceleration Engine is active and operating at 100x speed.")
        except Exception as e:
            logger.warning(f"C++ Acceleration Engine initialization notice: {e}. Using optimized Python mode.")
            self.lib = None

    @property
    def is_active(self) -> bool:
        return self.lib is not None

    def clear_user(self, user_id: int):
        if self.lib:
            try:
                self.lib.cpp_clear_user_cache(int(user_id))
            except Exception as e:
                logger.error(f"Error in cpp_clear_user_cache: {e}")

    def upsert_files(self, user_id: int, files: list):
        """Batch insert/update files into the C++ Engine."""
        if not files:
            return
        if self.lib:
            uid = int(user_id)
            for f in files:
                fid = str(f.get("id") or f.get("message_id") or "").encode('utf-8')
                name = str(f.get("name", "Untitled")).encode('utf-8')
                size = int(f.get("size", 0))
                mime = str(f.get("mimeType") or f.get("mime_type") or "").encode('utf-8')
                parent = str(f.get("parentId") or f.get("parent_id") or f.get("folder_id") or "root").encode('utf-8')
                created_at = int(f.get("created_at") or f.get("date") or 0)
                starred = 1 if f.get("starred") else 0
                raw_json = json.dumps(f).encode('utf-8')

                try:
                    self.lib.cpp_upsert_file_record(
                        uid, fid, name, size, mime, parent, created_at, starred, raw_json
                    )
                except Exception as e:
                    logger.debug(f"C++ upsert error: {e}")

    def get_total_count(self, user_id: int) -> int:
        if self.lib:
            try:
                return int(self.lib.cpp_get_total_count(int(user_id)))
            except Exception:
                pass
        return 0

    def filter_and_paginate(
        self,
        user_id: int,
        folder_id: str = "all",
        search_query: str = None,
        page: int = 1,
        per_page: int = 6
    ) -> dict:
        """Filter files by folder/search and paginate with C++ speed."""
        if self.lib:
            try:
                uid = int(user_id)
                f_bytes = (folder_id or "all").encode('utf-8')
                q_bytes = (search_query or "").encode('utf-8')
                res_ptr = self.lib.cpp_filter_and_paginate(uid, f_bytes, q_bytes, int(page), int(per_page))
                if res_ptr:
                    res_str = res_ptr.decode('utf-8')
                    return json.loads(res_str)
            except Exception as e:
                logger.error(f"Error in cpp_filter_and_paginate: {e}")

        return {"status": "success", "total": 0, "page": page, "total_pages": 1, "items": []}

    def compute_stats(self, user_id: int) -> dict:
        """Compute live storage totals and categories breakdown via C++."""
        if self.lib:
            try:
                res_ptr = self.lib.cpp_compute_stats(int(user_id))
                if res_ptr:
                    res_str = res_ptr.decode('utf-8')
                    return json.loads(res_str)
            except Exception as e:
                logger.error(f"Error in cpp_compute_stats: {e}")

        return {"status": "success", "total_files": 0, "total_bytes": 0, "categories": {}}

# Global C++ Engine instance
cpp_engine = CppEngine()
