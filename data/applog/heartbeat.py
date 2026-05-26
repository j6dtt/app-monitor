"""
applog/heartbeat.py
-------------------
Emits a HEARTBEAT log every N seconds on a background daemon thread.
The central monitor raises a DOWN alert if heartbeats stop arriving.

Silence means:
  - Process is hung (deadlock, infinite loop)
  - Container was OOM-killed
  - Container crashed without triggering the crash handler
  - Host network failure
"""

import threading

from .logging_setup import get_logger

log = get_logger("heartbeat")


class Heartbeat:
    def __init__(self, interval_seconds: int = 30):
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="heartbeat",
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # Emit immediately on start, then every interval
        self._emit()
        while not self._stop.wait(self.interval):
            self._emit()

    def _emit(self):
        log.info("HEARTBEAT", extra={"_x_event": "HEARTBEAT"})
