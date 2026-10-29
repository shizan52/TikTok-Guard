"""Be a good neighbour on a shared server: look at how much memory is really available before launching a browser.

The bot starts one headless browser per scan (peak ~1.2 GB private memory). On a small machine that also runs other
people's services, starting it while memory is short would push everything into swap - so the scan is skipped
(and retried at the next slot) instead. Cheap: one syscall, no extra dependency."""
from __future__ import annotations

import ctypes
import os
import time


def available_mb() -> float | None:
    """Memory that can be handed out right now without paging (free + cached/standby), in MB; None if unknown."""
    try:
        if os.name == "nt":
            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _Status()
            st.dwLength = ctypes.sizeof(_Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return st.ullAvailPhys / 1048576
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001 - an unreadable meter must never stop the bot
        return None
    return None


def enough_memory(min_free_mb: float) -> tuple[bool, float | None]:
    """(ok, available MB). Unknown memory counts as ok - the guard only ever holds back on a real reading."""
    free = available_mb()
    return (free is None or free >= float(min_free_mb)), free


def wait_for_memory(min_free_mb: float, max_wait_s: float, step_s: float = 5.0, sleep=time.sleep) -> tuple[bool, float | None]:
    """Give a short-lived dip a chance to pass; (ok, last reading)."""
    deadline = time.monotonic() + max_wait_s
    ok, free = enough_memory(min_free_mb)
    while not ok and time.monotonic() < deadline:
        sleep(step_s)
        ok, free = enough_memory(min_free_mb)
    return ok, free
