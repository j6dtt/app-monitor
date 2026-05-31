"""
applog/lifecycle.py
-------------------
Emits structured lifecycle events: STARTUP, READY, SHUTDOWN, CRASH.
These give the monitor ground truth about container state transitions.
"""

import atexit
import signal
import sys
import threading
import time
import traceback

from .logging_setup import get_logger
from .gelf_sender import send_gelf

log = get_logger("lifecycle")

_shutdown_called = threading.Event()


def emit(event: str, **kwargs):
    """
    Emit a lifecycle event.

    Usage:
        emit("STARTUP")
        emit("READY", port=8080)
        emit("SHUTDOWN", reason="manual")
    """
    level_map = {
        "STARTUP":  "info",
        "READY":    "info",
        "SHUTDOWN": "info",
        "CRASH":    "critical",
    }
    level = level_map.get(event, "info")
    extra = {"_x_event": event}
    extra.update({f"_x_{k}": v for k, v in kwargs.items()})
    getattr(log, level)(f"Lifecycle: {event}", extra=extra)
    send_gelf(event, level.upper(), event)


def register_shutdown_hooks():
    """
    Log a clean SHUTDOWN on SIGTERM (docker stop) or normal process exit.
    Call once at app startup.
    """
    def on_sigterm(sig, frame):
        if not _shutdown_called.is_set():
            _shutdown_called.set()
            emit("SHUTDOWN", reason="SIGTERM")
            sys.stdout.flush()
            time.sleep(0.5)  # let gelf/log driver ship the last line before pipe closes
        sys.exit(0)

    def on_atexit():
        if not _shutdown_called.is_set():
            _shutdown_called.set()
            emit("SHUTDOWN", reason="process_exit")

    signal.signal(signal.SIGTERM, on_sigterm)
    atexit.register(on_atexit)


def install_crash_handler():
    """
    Intercept unhandled exceptions and log them as CRASH events.
    Covers main thread (sys.excepthook) and background threads
    (threading.excepthook, Python 3.8+). Call once at app startup.
    """
    original_hook = sys.excepthook
    original_thread_hook = threading.excepthook

    def crash_hook(exc_type, exc_value, exc_tb):
        tb_str = "".join(traceback.format_tb(exc_tb))
        if not _shutdown_called.is_set():
            _shutdown_called.set()
            emit("CRASH", error_type=exc_type.__name__, error=str(exc_value), traceback=tb_str)
        original_hook(exc_type, exc_value, exc_tb)

    def thread_crash_hook(args):
        if args.exc_type is SystemExit:
            return
        tb_str = "".join(traceback.format_tb(args.exc_traceback)) if args.exc_traceback else ""
        emit("CRASH", error_type=args.exc_type.__name__, error=str(args.exc_value), traceback=tb_str)
        original_thread_hook(args)

    sys.excepthook = crash_hook
    threading.excepthook = thread_crash_hook
