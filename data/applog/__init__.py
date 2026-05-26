"""
applog — container logging package
Provides structured JSON logging, lifecycle events, and heartbeat.
"""

from .logging_setup import get_logger
from .lifecycle import emit, install_crash_handler, register_shutdown_hooks
from .heartbeat import Heartbeat

__all__ = [
    "get_logger",
    "emit",
    "install_crash_handler",
    "register_shutdown_hooks",
    "Heartbeat",
]
