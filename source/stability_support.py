"""Small, dependency-free helpers for long-running bot stability."""
from __future__ import annotations

import ctypes
import threading
import time
from collections import deque

_WINDOWS_MEMORY_API = None


def _windows_memory_api():
    global _WINDOWS_MEMORY_API
    if _WINDOWS_MEMORY_API is None:
        if __import__("os").name != "nt":
            raise OSError("Process RSS sampling requires Windows")
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_memory = psapi.GetProcessMemoryInfo
        get_memory.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_memory.restype = wintypes.BOOL
        _WINDOWS_MEMORY_API = (kernel32, psapi, get_memory, ProcessMemoryCounters)
    return _WINDOWS_MEMORY_API


class ProcessMemoryMonitor:
    """Sample the current process working set without retaining sample history."""

    def __init__(self, interval_seconds: float = 60.0, sampler=None):
        self.interval_seconds = interval_seconds
        self._sampler = sampler or self._windows_working_set_bytes
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._current = None
        self._peak = None
        self._total = 0
        self._sample_count = 0
        self._last_error = None

    @staticmethod
    def _windows_working_set_bytes() -> int:
        kernel32, _psapi, get_memory, counters_type = _windows_memory_api()
        counters = counters_type()
        counters.cb = ctypes.sizeof(counters)
        if not get_memory(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "GetProcessMemoryInfo failed")
        return int(counters.WorkingSetSize)

    def sample_now(self) -> None:
        try:
            value = int(self._sampler())
            if value < 0:
                raise ValueError("RSS sampler returned a negative value")
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:240]
            return

        with self._lock:
            self._current = value
            self._peak = value if self._peak is None else max(self._peak, value)
            self._total += value
            self._sample_count += 1
            self._last_error = None

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self.sample_now()

    def start(self) -> None:
        if self._thread is not None:
            return
        self.sample_now()
        thread = threading.Thread(
            target=self._run, name="BotMemoryMonitor", daemon=True
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.sample_now()

    def snapshot(self) -> dict:
        with self._lock:
            average = (
                self._total / self._sample_count if self._sample_count else None
            )
            return {
                "current": self._current,
                "average": average,
                "peak": self._peak,
                "sample_count": self._sample_count,
                "error": self._last_error,
            }


class StageStatistics:
    """Exact stage aggregates plus a bounded history of the latest wipes."""

    def __init__(self, history_limit: int = 100):
        self.history_limit = history_limit
        self.all_count = 0
        self.all_total = 0
        self.wipe_count = 0
        self.wipe_total = 0
        self.recent_wipes = deque(maxlen=history_limit)

    def record(self, stage: int, wiped: bool = False) -> None:
        self.all_count += 1
        self.all_total += stage
        if wiped:
            self.wipe_count += 1
            self.wipe_total += stage
            self.recent_wipes.append(stage)


def screenshot_as_grayscale(device, numpy_module):
    """Return an owned grayscale array and close both Pillow images."""
    image = device.screenshot()
    converted = None
    try:
        if image is None:
            raise RuntimeError("Empty ADB screenshot")
        converted = image.convert("L")
        gray = numpy_module.array(converted, copy=True)
        if gray.ndim != 2 or gray.size == 0:
            raise RuntimeError("Invalid screenshot array")
        return gray
    finally:
        if converted is not None:
            converted.close()
        if image is not None:
            image.close()


def is_opencv_allocation_error(error) -> bool:
    """Recognize OpenCV's StsNoMem status without masking ordinary cv2 errors."""
    if getattr(error, "code", None) == -4:
        return True
    message = str(error).lower()
    return "insufficient memory" in message or "out of memory" in message


def stop_keyboard_listener(listener, timeout: float = 5.0) -> None:
    """Stop and join a listener, surfacing stop, callback, or timeout errors."""
    errors = []
    try:
        listener.stop()
    except Exception as exc:
        errors.append(exc)

    try:
        listener.join(timeout=timeout)
    except Exception as exc:
        errors.append(exc)

    try:
        if listener.is_alive():
            errors.append(TimeoutError("Keyboard listener did not stop in time"))
    except Exception as exc:
        errors.append(exc)

    if errors:
        details = "; ".join(str(error) for error in errors)
        raise RuntimeError(f"Keyboard listener shutdown failed: {details}") from errors[0]


def start_keyboard_listener(
    keyboard_module, on_pause, on_stop, startup_timeout: float = 5.0
):
    """Register F8/F9 callbacks and wait for pynput's listener thread readiness."""
    def on_press(key):
        if key == keyboard_module.Key.f8:
            on_pause()
        elif key == keyboard_module.Key.f9:
            on_stop()

    listener = keyboard_module.Listener(on_press=on_press)
    listener.daemon = True
    listener.start()

    ready = getattr(listener, "_ready", None)
    if ready is not None:
        if hasattr(ready, "wait"):
            is_ready = ready.wait(startup_timeout)
        else:
            deadline = time.monotonic() + startup_timeout
            while not bool(getattr(listener, "_ready", False)):
                if not listener.is_alive() or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            is_ready = bool(getattr(listener, "_ready", False))
        if not is_ready:
            try:
                stop_keyboard_listener(listener, timeout=startup_timeout)
            except Exception as exc:
                raise RuntimeError(
                    f"Keyboard listener did not become ready; cleanup failed: {exc}"
                ) from exc
            raise TimeoutError("Keyboard listener did not become ready in time")
    else:
        wait = getattr(listener, "wait", None)
        if wait is None:
            stop_keyboard_listener(listener, timeout=startup_timeout)
            raise RuntimeError("Keyboard listener has no readiness signal")
        wait()

    # On Windows pynput marks itself ready immediately before installing its
    # system hook. Give startup failures a short chance to terminate here.
    time.sleep(0.05)
    if not listener.is_alive():
        listener.join(timeout=0.1)
        raise RuntimeError("Keyboard listener exited during startup")
    return listener
