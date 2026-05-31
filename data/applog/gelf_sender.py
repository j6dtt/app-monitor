"""
applog/gelf_sender.py
---------------------
Fire-and-forget GELF UDP sender for Kubernetes pods.

Activated by setting GELF_HOST in the pod environment. When unset (Docker
containers that use the gelf log driver), every call is a no-op — no socket
is opened, no overhead.

Docker containers must NOT set GELF_HOST — their gelf log driver already
delivers logs to app-monitor. Setting GELF_HOST on a Docker container with
a gelf log driver causes double-reporting.

Environment variables:
  GELF_HOST         — app-monitor IP or hostname (empty = disabled)
  GELF_PORT         — destination UDP port (default: 12201)
  CONTAINER_NAME    — stable service name shown on dashboard (default: pod hostname)
  GELF_SOURCE_HOST  — host field in GELF envelope (default: pod hostname).
                      Use spec.nodeName via downward API or a fixed cluster name
                      so the dashboard key host:container is stable across pod restarts.
"""

import json
import os
import socket
import time

_HOST      = os.getenv("GELF_HOST", "")
_PORT      = int(os.getenv("GELF_PORT", "12201"))
_CONTAINER = os.getenv("CONTAINER_NAME", socket.gethostname())
_SRC_HOST  = os.getenv("GELF_SOURCE_HOST", socket.gethostname())
_SERVICE   = os.getenv("SERVICE_NAME", "unknown")
_VERSION   = os.getenv("SERVICE_VERSION", "unknown")

_SOCK: socket.socket | None = None
if _HOST:
    _SOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

_LEVEL_MAP = {
    "CRITICAL": 2,
    "ERROR":    3,
    "WARNING":  4,
    "INFO":     6,
    "DEBUG":    7,
}


def send_gelf(message: str, level_str: str, event: str) -> None:
    """Send a GELF UDP packet to app-monitor. No-op if GELF_HOST is not set."""
    if _SOCK is None:
        return
    try:
        now = time.time()
        ts  = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
        ts += ".%03dZ" % (now % 1 * 1000)

        # short_message mirrors logging_setup.JSONFormatter output so that
        # parse_inner() in gelf_monitor works identically for Docker and K8s packets.
        inner = {
            "timestamp": ts,
            "level":     level_str.upper(),
            "logger":    "gelf_sender",
            "message":   message,
            "service":   _SERVICE,
            "version":   _VERSION,
            "host":      _SRC_HOST,
            "event":     event,
        }

        envelope = {
            "version":        "1.1",
            "host":           _SRC_HOST,
            "short_message":  json.dumps(inner),
            "timestamp":      now,
            "level":          _LEVEL_MAP.get(level_str.upper(), 6),
            "_container_name": _CONTAINER,
            "_tag":           _CONTAINER,
        }

        _SOCK.sendto(json.dumps(envelope).encode(), (_HOST, _PORT))
    except Exception:
        pass
