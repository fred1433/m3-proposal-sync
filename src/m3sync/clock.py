"""Clocks. The harness uses FakeClock so that simulated outages are labelled
as simulated elapsed time, not wall-clock time."""

from __future__ import annotations

import threading
import time


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic clock. sleep() advances simulated time instantly."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self._t = float(start)
        self._start = float(start)
        self._lock = threading.Lock()
        self.slept: list[float] = []

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        self.slept.append(max(0.0, seconds))
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))

    def label(self) -> str:
        """Simulated elapsed time since start, as T+mm:ss."""
        elapsed = int(round(self.now() - self._start))
        return f"T+{elapsed // 60:02d}:{elapsed % 60:02d}"
