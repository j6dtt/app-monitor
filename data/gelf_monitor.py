#!/usr/bin/env python3
"""
gelf_monitor.py  —  Central Log Monitor with Downtime Detection
---------------------------------------------------------------
Receives GELF (JSON log packets) over UDP and TCP from Docker containers
running on any number of hosts. Detects ERROR/WARNING in log messages
and raises a DOWN alert when a container stops sending logs entirely.

How it works:
  1. Docker containers are configured with the gelf log driver, which
     automatically wraps every stdout/stderr line as a JSON packet and
     sends it to this monitor over UDP or TCP.
  2. This monitor receives those packets, parses them, and checks each
     message for ERROR/WARNING keywords.
  3. A watchdog thread tracks the last time each container sent any log.
     If silence exceeds --heartbeat-timeout seconds, the container is
     declared DOWN. When it comes back, a RECOVERED alert is emitted.

What Docker actually sends:
  Your app writes this to stdout:
    {"timestamp": "...", "level": "INFO", "message": "HEARTBEAT", "event": "HEARTBEAT"}

  Docker wraps it in a GELF envelope and sends this to the monitor:
    {
      "version": "1.1",
      "host": "ubt2",
      "short_message": "{\"level\": \"INFO\", \"message\": \"HEARTBEAT\", \"event\": \"HEARTBEAT\"}",
      "timestamp": 1748012530.274,
      "level": 6,
      "_container_name": "/your-container",
      "_tag": "your-container"
    }

  Your app's JSON becomes the value of short_message (a plain string).
  This monitor parses short_message as JSON to extract your app's fields.

Container setup on every Docker host — add to docker-compose.yml:
  logging:
    driver: gelf
    options:
      gelf-address: "udp://<THIS-SERVER-IP>:12201"
      tag: "{{.Name}}"

Usage:
  # Run with all defaults (UDP+TCP on port 12201, 90s DOWN timeout)
  python gelf_monitor.py

  # Change the port (must match gelf-address in your app docker-compose)
  python gelf_monitor.py --udp-port 12202 --tcp-port 12202

  # Declare a container DOWN after 2 minutes of silence instead of 90s
  python gelf_monitor.py --heartbeat-timeout 120

  # Write all ERROR/WARNING/DOWN alerts to a file in addition to stdout
  python gelf_monitor.py --alert-log /var/log/docker-alerts.log

  # Only listen on UDP (lighter, no connection overhead)
  python gelf_monitor.py --no-tcp

  # Only listen on TCP (reliable delivery, no packet loss)
  python gelf_monitor.py --no-udp

  # Add custom keywords to detect beyond the defaults
  python gelf_monitor.py --keywords ERROR WARNING CRITICAL FATAL PANIC TIMEOUT

  # Combine options
  python gelf_monitor.py --udp-port 12202 --heartbeat-timeout 120 --alert-log /var/log/alerts.log
"""

import argparse
import functools
import json
import logging
import queue
import re
import signal
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta


# ── Severity keywords ─────────────────────────────────────────────────────────
# These are the words the monitor scans for in every log line.
# Any line containing one of these words triggers a coloured alert.
# Override with --keywords if your apps use different terminology.

DEFAULT_KEYWORDS = ["ERROR", "WARNING", "WARN", "CRITICAL", "FATAL", "EXCEPTION", "PANIC", "TRACEBACK"]

# Maps keyword variants to a canonical severity level used internally.
SEVERITY_MAP = {
    "CRITICAL":  "CRITICAL",
    "FATAL":     "CRITICAL",
    "PANIC":     "CRITICAL",
    "TRACEBACK": "CRITICAL",
    "ERROR":     "ERROR",
    "EXCEPTION": "ERROR",
    "WARNING":   "WARNING",
    "WARN":      "WARNING",
}

# GELF numeric syslog levels → internal severity labels.
# Levels 5, 6, 7 (NOTICE, INFO, DEBUG) are non-alerts.
GELF_LEVEL = {
    0: "CRITICAL",  # EMERG
    1: "CRITICAL",  # ALERT
    2: "CRITICAL",  # CRIT
    3: "ERROR",     # ERROR
    4: "WARNING",   # WARNING
    5: None,        # NOTICE
    6: None,        # INFO
    7: None,        # DEBUG
}

# ANSI colour codes for terminal output
C = {
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "red":     "\033[91m",
    "yellow":  "\033[93m",
    "cyan":    "\033[96m",
    "magenta": "\033[95m",
    "green":   "\033[92m",
    "grey":    "\033[90m",
    "blue":    "\033[94m",
    "white":   "\033[97m",
}

# Colour assigned to each severity level in the output
SEV_COLOR = {
    "CRITICAL": C["magenta"] + C["bold"],
    "ERROR":    C["red"]     + C["bold"],
    "WARNING":  C["yellow"]  + C["bold"],
    "DOWN":     C["red"]     + C["bold"],
    "UP":       C["green"]   + C["bold"],
}


# ── Inner JSON extraction ─────────────────────────────────────────────────────

def parse_inner(msg: dict) -> dict:
    """
    Your app writes structured JSON to stdout. Docker does not parse it —
    it treats the entire line as a plain string and puts it in short_message.

    This function parses short_message back into a dict so the monitor
    can access your app's fields (message, level, event, etc.).

    If short_message is not JSON (e.g. a third-party container writing
    plain text), this returns an empty dict and the monitor falls back
    to scanning the raw string.
    """
    raw = msg.get("short_message") or msg.get("message") or ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


# ── Pattern matching ──────────────────────────────────────────────────────────

def build_pattern(keywords):
    """
    Compiles a single regex that matches any of the configured keywords
    as whole words, case-insensitively. Called once at startup.
    """
    return re.compile(
        r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b",
        re.IGNORECASE
    )


def classify_message(msg, pattern):
    """
    Determines the severity of an incoming GELF message.

    Checks in this priority order:
      1. Lifecycle event field inside your app's JSON (CRASH, SHUTDOWN)
      2. GELF numeric level field in the envelope
      3. Keyword scan of your app's message text and level field

    Returns severity string ("CRITICAL", "ERROR", "WARNING") or None.
    """
    # Parse your app's JSON out of short_message
    inner = parse_inner(msg)

    # Priority 1: lifecycle event set by the applog package
    event = inner.get("event") or inner.get("_x_event") or \
            msg.get("event") or msg.get("_x_event") or ""
    if event == "CRASH":
        return "CRITICAL"
    if event == "SHUTDOWN":
        return None    # expected event, not an alert

    # Priority 2: numeric syslog level in the GELF envelope.
    # Only apply when short_message is JSON (applog-style app). Plain-text
    # containers writing to stderr get GELF level 3 (ERROR) regardless of
    # actual severity — Docker maps stream, not content — causing false alerts.
    level = msg.get("level")
    if level is not None and inner:
        sev = GELF_LEVEL.get(int(level))
        if sev:
            return sev

    # Priority 3: keyword scan
    # Check both your app's message field and the raw short_message string,
    # and also your app's level field (e.g. "ERROR") as a keyword source
    text = " ".join(filter(None, [
        inner.get("message", ""),
        inner.get("level", ""),
        msg.get("short_message", ""),
    ]))
    matches = pattern.findall(text)
    if matches:
        order = ["CRITICAL", "ERROR", "WARNING"]
        found = {SEVERITY_MAP.get(m.upper(), "WARNING") for m in matches}
        for sev in order:
            if sev in found:
                return sev
    return None


# ── GELF packet parsing ───────────────────────────────────────────────────────

def parse_gelf(data: bytes) -> dict | None:
    """
    Parses a raw GELF packet (bytes) into a Python dict.

    GELF packets are JSON, optionally zlib-compressed.
    Chunked GELF (magic bytes 0x1e 0x0f) is skipped — chunking is rare
    and only occurs for very large single log lines.

    Returns None if the packet cannot be parsed.
    """
    try:
        if len(data) >= 2 and data[:2] == b'\x1e\x0f':
            return None
        try:
            payload = zlib.decompress(data)
        except zlib.error:
            payload = data
        return json.loads(payload.decode("utf-8", errors="replace"))
    except Exception:
        return None


def extract_fields(msg: dict, sender_ip: str) -> tuple:
    """
    Pulls the key fields out of a parsed GELF envelope.

    Docker's gelf log driver sets these envelope fields automatically:
      host             — hostname of the Docker host
      _container_name  — name of the container (e.g. /my-api)
      tag              — value of the 'tag' option in docker-compose
      timestamp        — Unix epoch float
      short_message    — your app's stdout line (your JSON string)

    Your app's fields (message, event, level) are inside short_message
    and are extracted via parse_inner().

    Returns: (host, container, ts_str, message, event)
    """
    # Parse your app's JSON out of short_message
    inner = parse_inner(msg)

    host = msg.get("host") or sender_ip

    container = (
        msg.get("_container_name")
        or msg.get("tag")
        or msg.get("_tag")
        or "unknown"
    ).lstrip("/")

    # Prefer Docker's envelope timestamp (more reliable than app timestamp)
    ts_unix = msg.get("timestamp")
    try:
        ts_str = datetime.fromtimestamp(float(ts_unix), tz=timezone.utc) \
                         .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Use your app's message field from inner JSON, fall back to raw string
    message = inner.get("message") or msg.get("short_message") or ""

    # Event field lives inside your app's JSON
    event = inner.get("event") or inner.get("_x_event") or \
            msg.get("event") or msg.get("_x_event") or ""

    return host, container, ts_str, message, event


# ── Heartbeat tracker — core downtime detection ───────────────────────────────

class HeartbeatTracker:
    """
    Tracks liveness of each container by recording the last time any log
    was received from it. Any log line counts as proof of life.

    A background watchdog thread calls check() every --watchdog-interval
    seconds. If a container has been silent for longer than
    --heartbeat-timeout seconds, a DOWN alert is emitted.
    When that container sends logs again, a RECOVERED alert is emitted.

    This detects:
      - Containers that crashed without logging (OOM kill, kernel kill)
      - Hung processes (deadlock, infinite loop with no output)
      - Network failures between the app host and this monitor
    """

    def __init__(self, timeout_seconds: int = 90, watchdog_interval: int = 15):
        self.timeout = timeout_seconds
        self.watchdog_interval = watchdog_interval
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}
        self._down: set[str] = set()
        self._metadata: dict[str, dict] = {}

    def seen(self, host: str, container: str):
        """
        Called on every received log line.
        Resets the silence timer for this container.
        Returns True if the container was previously DOWN (triggers RECOVERED).
        """
        key = f"{host}:{container}"
        now = time.monotonic()
        with self._lock:
            prev_down = key in self._down
            self._last_seen[key] = now
            self._metadata[key] = {"host": host, "container": container}
            if prev_down:
                self._down.discard(key)
                return True
        return False

    def check(self, log_queue: queue.Queue):
        """
        Called periodically by the watchdog thread.
        Emits a DOWN alert for any container that has been silent
        longer than timeout_seconds. Each container gets one DOWN
        alert until it recovers.
        """
        now = time.monotonic()
        with self._lock:
            for key, last in self._last_seen.items():
                silence = now - last
                meta = self._metadata[key]
                host, container = meta["host"], meta["container"]

                if silence > self.timeout and key not in self._down:
                    self._down.add(key)
                    log_queue.put({
                        "type":      "synthetic",
                        "host":      host,
                        "container": container,
                        "ts":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "message":   f"No heartbeat for {int(silence)}s — container is DOWN",
                        "severity":  "DOWN",
                        "proto":     "MONITOR",
                        "event":     "DOWN",
                    })

    def remove(self, key: str):
        with self._lock:
            self._last_seen.pop(key, None)
            self._down.discard(key)
            self._metadata.pop(key, None)

    def start_watchdog(self, log_queue: queue.Queue, stop_event: threading.Event):
        """Starts the background thread that periodically runs check()."""
        def _loop():
            while not stop_event.wait(self.watchdog_interval):
                self.check(log_queue)
        threading.Thread(target=_loop, daemon=True, name="watchdog").start()


# ── Stats ─────────────────────────────────────────────────────────────────────

class Stats:
    """
    Counts messages and alerts per container for the session summary
    printed on shutdown.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.counts = {}
        self.totals = {}
        self.hosts = set()
        self.start_time = datetime.now(timezone.utc)

    def record(self, host, container, severity):
        key = f"{host}:{container}"
        with self._lock:
            self.hosts.add(host)
            if key not in self.counts:
                self.counts[key] = {"CRITICAL": 0, "ERROR": 0, "WARNING": 0, "DOWN": 0}
                self.totals[key] = 0
            self.totals[key] += 1
            if severity in self.counts[key]:
                self.counts[key][severity] += 1

    def remove(self, key: str):
        with self._lock:
            self.counts.pop(key, None)
            self.totals.pop(key, None)

    def summary(self) -> str:
        with self._lock:
            lines = [
                "",
                C["bold"] + "=" * 72 + C["reset"],
                "  SESSION SUMMARY",
                f"  Hosts seen: {', '.join(sorted(self.hosts)) or 'none'}",
                C["bold"] + "=" * 72 + C["reset"],
            ]
            for key in sorted(self.counts):
                s = self.counts[key]
                t = self.totals[key]
                lines.append(
                    f"  {C['cyan']}{key:<42}{C['reset']}"
                    f"  msgs={t:>6}"
                    f"  {SEV_COLOR['CRITICAL']}CRIT={s['CRITICAL']}{C['reset']}"
                    f"  {SEV_COLOR['ERROR']}ERR={s['ERROR']}{C['reset']}"
                    f"  {SEV_COLOR['WARNING']}WARN={s['WARNING']}{C['reset']}"
                    f"  {SEV_COLOR['DOWN']}DOWN={s['DOWN']}{C['reset']}"
                )
            lines.append(C["bold"] + "=" * 72 + C["reset"])
            return "\n".join(lines)


# ── Dashboard state ───────────────────────────────────────────────────────────

class DashboardState:
    """
    Shared state between the monitor core and the web dashboard.
    Thread-safe. Fed by the printer thread; read by HTTP handler threads.
    """

    # Heartbeats without a new alert needed to auto-clear the badge.
    # CRITICAL is absent — always requires STARTUP/READY, RECOVERED, or manual Clear.
    _HB_TO_CLEAR = {"WARNING": 1, "ERROR": 3}
    _VALID_GROUPS = {"NIPR", "SIPR"}

    def __init__(self, max_history: int = 50000):
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._alerts: list = []
        self._max = max_history
        self._last_severity: dict = {}          # key -> last alert severity
        self._last_alert_msg: dict = {}         # key -> message text of last alert
        self._hb_since_alert: dict = {}         # key -> heartbeat count since last alert
        self._acks: dict = {}                   # key -> {"note": str, "ts": str}
        self._groups: dict    = self._load_groups()    # key -> {"group": str, "subgroup": str}
        self._subgroups: dict = {}                      # populated from _groups on load
        self._subscribers: dict = {}            # container key (or "*") -> [Queue, ...]
        self._load_history()
        self._start_midnight_reset()

    def record_event(self, host: str, container: str, ts: str,
                     message: str, severity, event: str, proto: str):
        key = f"{host}:{container}"
        entry = {
            "host": host, "container": container, "key": key,
            "ts": ts, "message": message or "",
            "severity": severity or "", "event": event or "", "proto": proto or "",
        }
        with self._lock:
            self._alerts.append(entry)
            if len(self._alerts) > self._max:
                del self._alerts[0]
            if severity in ("CRITICAL", "ERROR", "WARNING") or event == "DOWN":
                self._last_severity[key] = severity or event
                self._last_alert_msg[key] = message or ""
                self._hb_since_alert[key] = 0
            elif event in ("UP", "STARTUP", "READY"):
                self._last_severity.pop(key, None)
                self._last_alert_msg.pop(key, None)
                self._hb_since_alert.pop(key, None)
            elif event == "HEARTBEAT":
                last = self._last_severity.get(key)
                threshold = self._HB_TO_CLEAR.get(last)
                if threshold is not None:
                    count = self._hb_since_alert.get(key, 0) + 1
                    if count >= threshold:
                        self._last_severity.pop(key, None)
                        self._last_alert_msg.pop(key, None)
                        self._hb_since_alert.pop(key, None)
                    else:
                        self._hb_since_alert[key] = count
            if event == "UP":
                self._acks.pop(key, None)       # only watchdog RECOVERED clears ack
        self._push(key, entry)
        self._append_history(entry)

    def get_last_severity(self, key: str):
        with self._lock:
            return self._last_severity.get(key)

    def get_last_alert_msg(self, key: str):
        with self._lock:
            return self._last_alert_msg.get(key, "")

    def get_recent_alerts(self, key: str, n: int = 5) -> list:
        with self._lock:
            result = []
            for a in reversed(self._alerts):
                if a["key"] != key:
                    continue
                if a["severity"] in ("CRITICAL", "ERROR", "WARNING") or a["event"] == "DOWN":
                    result.append({
                        "ts":       a["ts"],
                        "severity": a["severity"] or a["event"],
                        "message":  a["message"],
                    })
                    if len(result) >= n:
                        break
            return result  # newest first

    # ── File-backed 24 h history ──────────────────────────────────────────────

    def _load_history(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _os.makedirs(_os.path.dirname(_HISTORY_FILE), exist_ok=True)
        try:
            with open(_HISTORY_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("ts", "") >= cutoff:
                            self._alerts.append(entry)
                    except Exception:
                        continue
        except FileNotFoundError:
            # Create the file so its presence confirms the path is correct
            open(_HISTORY_FILE, "w").close()

    def _append_history(self, entry: dict):
        if entry.get("event") == "HEARTBEAT":
            return  # heartbeats are noise historically; omit from file
        try:
            with self._file_lock:
                with open(_HISTORY_FILE, "a") as f:
                    f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _midnight_reset(self):
        with self._lock:
            self._alerts.clear()
        try:
            with self._file_lock:
                open(_HISTORY_FILE, "w").close()
        except Exception:
            pass

    def _start_midnight_reset(self):
        def _run():
            while True:
                now  = datetime.now(timezone.utc)
                next_midnight = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                time.sleep((next_midnight - now).total_seconds())
                self._midnight_reset()
        threading.Thread(target=_run, daemon=True, name="history-reset").start()

    def remove_container(self, key: str):
        with self._lock:
            self._alerts = [a for a in self._alerts if a["key"] != key]
            self._last_severity.pop(key, None)
            self._last_alert_msg.pop(key, None)
            self._hb_since_alert.pop(key, None)
            self._acks.pop(key, None)

    def ack_container(self, key: str, note: str = ""):
        with self._lock:
            self._acks[key] = {
                "note": note,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

    def unack_container(self, key: str):
        with self._lock:
            self._acks.pop(key, None)

    def get_ack(self, key: str):
        with self._lock:
            return self._acks.get(key)

    # ── Container groups (persisted to groups.json) ───────────────────────────

    def _load_groups(self) -> dict:
        try:
            with open(_GROUPS_FILE) as f:
                raw = json.load(f)
            result = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    g = v.get("group", "")
                    s = v.get("subgroup", "")
                    result[k] = {"group": g if g in self._VALID_GROUPS else "",
                                 "subgroup": s}
                elif isinstance(v, str) and v in self._VALID_GROUPS:
                    # migrate old format: "NIPR" → {"group": "NIPR", "subgroup": ""}
                    result[k] = {"group": v, "subgroup": ""}
            return result
        except Exception:
            return {}

    def _save_groups(self):
        try:
            tmp = _GROUPS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._groups, f)
            _os.replace(tmp, _GROUPS_FILE)
        except Exception:
            pass

    def set_group(self, key: str, group: str):
        with self._lock:
            entry = self._groups.get(key, {"group": "", "subgroup": ""}).copy()
            entry["group"] = group if group in self._VALID_GROUPS else ""
            self._groups[key] = entry
        self._save_groups()

    def get_group(self, key: str) -> str:
        with self._lock:
            return self._groups.get(key, {}).get("group", "")

    def set_subgroup(self, key: str, subgroup: str):
        with self._lock:
            entry = self._groups.get(key, {"group": "", "subgroup": ""}).copy()
            entry["subgroup"] = subgroup.strip()
            self._groups[key] = entry
        self._save_groups()

    def get_subgroup(self, key: str) -> str:
        with self._lock:
            return self._groups.get(key, {}).get("subgroup", "")

    def get_history(self, container_key: str = "*", limit: int = 200) -> list:
        with self._lock:
            alerts = list(self._alerts)
        if container_key and container_key != "*":
            alerts = [a for a in alerts if a["key"] == container_key]
        return alerts[-limit:]

    def subscribe(self, container_key: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.setdefault(container_key, []).append(q)
        return q

    def unsubscribe(self, container_key: str, q: queue.Queue):
        with self._lock:
            lst = self._subscribers.get(container_key, [])
            if q in lst:
                lst.remove(q)

    def _push(self, key: str, entry: dict):
        with self._lock:
            targets = (list(self._subscribers.get(key, [])) +
                       list(self._subscribers.get("*", [])))
        for q in targets:
            try:
                q.put_nowait(entry)
            except queue.Full:
                pass


# ── Web dashboard HTML path ───────────────────────────────────────────────────
# dashboard.html lives alongside this script; read from disk on each request
# so you can edit the UI without restarting the monitor.

import os as _os
_DASHBOARD_HTML  = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard.html")
_GROUPS_FILE     = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "groups.json")
_HISTORY_FILE    = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "log", "alerts_history.jsonl")


# ── Web dashboard HTTP server ─────────────────────────────────────────────────

class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):

    def __init__(self, dashboard: DashboardState, tracker: HeartbeatTracker,
                 stats: Stats, *args, **kwargs):
        self.dashboard = dashboard
        self.tracker = tracker
        self.stats = stats
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        p = parsed.path

        if   p == "/":               self._html()
        elif p == "/api/status":     self._status()
        elif p == "/api/history":    self._history(params)
        elif p == "/api/stats":      self._stats()
        elif p == "/api/stream":     self._stream(params)
        else:                        self.send_error(404)

    def _html(self):
        try:
            with open(_DASHBOARD_HTML, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            body = b"<h1>dashboard.html not found</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _status(self):
        now = time.monotonic()
        containers = []
        with self.tracker._lock:
            for key, last in self.tracker._last_seen.items():
                meta  = self.tracker._metadata[key]
                age   = int(now - last)
                is_dn = key in self.tracker._down
                sc    = self.stats.counts.get(key, {})
                ack   = self.dashboard.get_ack(key)
                containers.append({
                    "key":           key,
                    "host":          meta["host"],
                    "container":     meta["container"],
                    "age":           age,
                    "status":        "DOWN" if is_dn else "UP",
                    "last_severity":  self.dashboard.get_last_severity(key),
                    "last_alert_msg": self.dashboard.get_last_alert_msg(key),
                    "recent_alerts":  self.dashboard.get_recent_alerts(key),
                    "err_count":     sc.get("ERROR",    0),
                    "warn_count":    sc.get("WARNING",  0),
                    "down_count":    sc.get("DOWN",     0),
                    "acked":         ack is not None,
                    "ack_note":      ack["note"] if ack else "",
                    "ack_ts":        ack["ts"]   if ack else "",
                    "group":         self.dashboard.get_group(key),
                    "subgroup":      self.dashboard.get_subgroup(key),
                })
        self._json({"containers": containers})

    def _history(self, params):
        key = params.get("container", ["*"])[0]
        self._json({"alerts": self.dashboard.get_history(key)})

    def _stats(self):
        with self.stats._lock:
            rows = [
                {"key": k, "total": self.stats.totals[k],
                 "critical": v["CRITICAL"], "error": v["ERROR"],
                 "warning": v["WARNING"],   "down":  v["DOWN"]}
                for k, v in sorted(self.stats.counts.items())
            ]
            hosts = sorted(self.stats.hosts)
        self._json({"hosts": hosts, "rows": rows,
                    "since": self.stats.start_time.strftime("%Y-%m-%dT%H:%M:%SZ")})

    def _stream(self, params):
        key = params.get("container", ["*"])[0]
        q   = self.dashboard.subscribe(key)
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    entry = q.get(timeout=20)
                    self.wfile.write(
                        f"data: {json.dumps(entry)}\n\n".encode()
                    )
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.dashboard.unsubscribe(key, q)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}
        if parsed.path == "/api/ack":
            key  = data.get("key",  "").strip()
            note = data.get("note", "").strip()
            if key:
                self.dashboard.ack_container(key, note)
                self._json({"ok": True})
            else:
                self.send_error(400, "Missing key")
        elif parsed.path == "/api/unack":
            key = data.get("key", "").strip()
            if key:
                self.dashboard.unack_container(key)
                self._json({"ok": True})
            else:
                self.send_error(400, "Missing key")
        elif parsed.path == "/api/remove":
            key = data.get("key", "").strip()
            if key:
                self.tracker.remove(key)
                self.dashboard.remove_container(key)
                self.stats.remove(key)
                self._json({"ok": True})
            else:
                self.send_error(400, "Missing key")
        elif parsed.path == "/api/group":
            key   = data.get("key",   "").strip()
            group = data.get("group", "").strip().upper()
            if key:
                self.dashboard.set_group(key, group)
                self._json({"ok": True})
            else:
                self.send_error(400, "Missing key")
        elif parsed.path == "/api/subgroup":
            key      = data.get("key",      "").strip()
            subgroup = data.get("subgroup", "").strip()
            if key:
                self.dashboard.set_subgroup(key, subgroup)
                self._json({"ok": True})
            else:
                self.send_error(400, "Missing key")
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # suppress HTTP access log noise


def web_server(bind: str, port: int, dashboard: DashboardState,
               tracker: HeartbeatTracker, stats: Stats, logger: logging.Logger,
               certfile: str = "", keyfile: str = ""):
    handler = functools.partial(DashboardHandler, dashboard, tracker, stats)
    srv = _ThreadingHTTPServer((bind, port), handler)
    if certfile and keyfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        proto = "https"
    else:
        proto = "http"
    logger.info(f"{C['green']}Web dashboard on {proto}://{bind}:{port}{C['reset']}")
    srv.serve_forever()


# ── Printer ───────────────────────────────────────────────────────────────────

def printer(log_queue: queue.Queue, logger: logging.Logger,
            stats: Stats, stop_event: threading.Event,
            dashboard: "DashboardState | None" = None):
    """
    Single thread that reads from log_queue and writes formatted lines
    to stdout and the alert log file.

    All receiver threads feed into this one queue so output lines
    are never interleaved or garbled from concurrent writes.

    Output format:
      TIMESTAMP  HOST/CONTAINER  [PROTO]  [SEVERITY]  message

    Severity colours:
      [STARTUP] [READY] [SHUTDOWN]  green
      [CRASH]                       magenta bold
      ♥ HEARTBEAT                   grey (debug level, not in alert file)
      [WARNING]                     yellow bold
      [ERROR]                       red bold
      [CRITICAL]                    magenta bold
      [DOWN]                        red bold
      [RECOVERED]                   green bold
    """
    while not stop_event.is_set() or not log_queue.empty():
        try:
            item = log_queue.get(timeout=0.3)
        except queue.Empty:
            continue

        host      = item["host"]
        container = item["container"]
        ts        = item["ts"]
        message   = item["message"]
        severity  = item.get("severity")
        proto     = item.get("proto", "UDP")
        event     = item.get("event", "")

        stats.record(host, container, severity)
        if dashboard and (event or severity):
            dashboard.record_event(host, container, ts, message, severity, event, proto)

        host_lbl  = f"{C['blue']}{host}{C['reset']}"
        cont_lbl  = f"{C['cyan']}{container}{C['reset']}"
        proto_lbl = f"{C['grey']}[{proto}]{C['reset']}"
        ts_lbl    = f"{C['grey']}{ts}{C['reset']}"

        if event == "DOWN":
            line = (f"{ts_lbl}  {host_lbl}/{cont_lbl}  {proto_lbl}  "
                    f"{SEV_COLOR['DOWN']}[DOWN]{C['reset']}  {message}")
            logger.critical(line)

        elif event == "UP":
            line = (f"{ts_lbl}  {host_lbl}/{cont_lbl}  {proto_lbl}  "
                    f"{SEV_COLOR['UP']}[RECOVERED]{C['reset']}  {message}")
            logger.warning(line)

        elif event in ("STARTUP", "READY", "SHUTDOWN"):
            line = (f"{ts_lbl}  {host_lbl}/{cont_lbl}  {proto_lbl}  "
                    f"{C['green']}[{event}]{C['reset']}  {message}")
            logger.info(line)

        elif event == "CRASH":
            line = (f"{ts_lbl}  {host_lbl}/{cont_lbl}  {proto_lbl}  "
                    f"{SEV_COLOR['CRITICAL']}[CRASH]{C['reset']}  {message}")
            logger.critical(line)

        elif event == "HEARTBEAT":
            # Heartbeats printed at DEBUG — visible in stdout, not in alert file
            line = f"{ts_lbl}  {host_lbl}/{cont_lbl}  {proto_lbl}  ♥ HEARTBEAT"
            logger.debug(line)

        elif severity:
            color = SEV_COLOR.get(severity, "")
            line = (f"{ts_lbl}  {host_lbl}/{cont_lbl}  {proto_lbl}  "
                    f"{color}[{severity}]{C['reset']}  {message}")
            logger.warning(line)

        log_queue.task_done()


# ── CEF receiver helpers ──────────────────────────────────────────────────────

def parse_cef(cef_str: str) -> dict | None:
    """
    Parse a CEF message string into a dict of extension key/value pairs.

    CEF format:
      CEF:version|Vendor|Product|Version|SignatureID|Name|Severity|extension
    Extension:
      key=value; key=value;   (backslash-escapes: \\  \=  \|  \;)

    Returns the extension as a dict, or None if the string is not CEF.
    """
    cef_str = cef_str.strip()
    if not cef_str.upper().startswith("CEF:"):
        return None
    # Split on unescaped pipes — CEF header has exactly 7 pipes
    parts = []
    current = []
    i = 0
    while i < len(cef_str):
        c = cef_str[i]
        if c == '\\' and i + 1 < len(cef_str):
            current.append(cef_str[i + 1])
            i += 2
            continue
        if c == '|':
            parts.append(''.join(current))
            current = []
            if len(parts) == 7:
                # Everything from here is the extension
                parts.append(cef_str[i + 1:])
                break
        else:
            current.append(c)
        i += 1

    if len(parts) < 8:
        return None

    ext = parts[7]
    fields: dict = {}
    i = 0
    key_buf: list = []
    val_buf: list = []
    in_val = False
    while i < len(ext):
        c = ext[i]
        if c == '\\' and i + 1 < len(ext):
            (val_buf if in_val else key_buf).append(ext[i + 1])
            i += 2
            continue
        if c == '=' and not in_val:
            in_val = True
            i += 1
            continue
        if c == ';' and in_val:
            k = ''.join(key_buf).strip()
            v = ''.join(val_buf).strip()
            if k:
                fields[k] = v
            key_buf, val_buf = [], []
            in_val = False
            i += 1
            if i < len(ext) and ext[i] == ' ':
                i += 1
            continue
        (val_buf if in_val else key_buf).append(c)
        i += 1
    # Final field (no trailing semicolon)
    k = ''.join(key_buf).strip()
    v = ''.join(val_buf).strip()
    if k and in_val:
        fields[k] = v

    return fields if fields else None


def cef_to_gelf(fields: dict, sender_ip: str) -> dict:
    """
    Reconstruct a minimal GELF-compatible dict from parsed CEF extension fields.
    Fields written by logship: ts, host, container, level, msg, event (optional).
    """
    inner: dict = {
        "level":   fields.get("level", ""),
        "message": fields.get("msg", ""),
    }
    event = fields.get("event", "")
    if event:
        inner["event"] = event

    host      = fields.get("host") or sender_ip
    container = fields.get("container", "unknown")
    ts_str    = fields.get("ts", "")
    try:
        ts_unix = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ") \
                          .replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        ts_unix = datetime.now(timezone.utc).timestamp()

    return {
        "version":         "1.1",
        "host":            host,
        "_container_name": container,
        "_tag":            container,
        "short_message":   json.dumps(inner),
        "timestamp":       ts_unix,
        "level":           6,
    }


def cef_tcp_receiver(bind, port, pattern, tracker, log_queue, stop_event, logger):
    """
    Accepts TCP connections carrying one CEF message each.
    Reads until the connection closes, parses the CEF string,
    converts to a synthetic GELF packet, and enqueues it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    sock.bind((bind, port))
    sock.listen(128)
    logger.info(f"{C['green']}CEF/TCP listening on {bind}:{port}{C['reset']}")

    def handle(conn, addr):
        data = b""
        conn.settimeout(5.0)
        try:
            while True:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
        finally:
            conn.close()
        cef_str = data.decode("utf-8", errors="replace").strip()
        if not cef_str:
            return
        fields = parse_cef(cef_str)
        if fields is None:
            logger.warning(f"CEF parse failed from {addr[0]}: {cef_str[:120]}")
            return
        gelf = cef_to_gelf(fields, addr[0])
        enqueue(gelf, addr[0], "CEF", pattern, tracker, log_queue)

    while not stop_event.is_set():
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except Exception as e:
            if not stop_event.is_set():
                logger.error(f"CEF TCP error: {e}")
            continue
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()

    sock.close()


# ── Receive helpers ───────────────────────────────────────────────────────────

def enqueue(msg: dict, sender_ip: str, proto: str,
            pattern, tracker: HeartbeatTracker, log_queue: queue.Queue):
    """
    Called by both UDP and TCP receivers for every valid GELF packet.
    Extracts fields, updates the heartbeat tracker, checks for recovery,
    classifies severity, and puts the result on the print queue.
    """
    host, container, ts_str, message, event = extract_fields(msg, sender_ip)

    recovered = tracker.seen(host, container)
    if recovered:
        log_queue.put({
            "host": host, "container": container,
            "ts": ts_str,
            "message": "Heartbeat resumed — container is back UP",
            "severity": "UP", "proto": proto, "event": "UP",
        })

    severity = classify_message(msg, pattern)

    log_queue.put({
        "host": host, "container": container,
        "ts": ts_str, "message": message,
        "severity": severity, "proto": proto, "event": event,
    })


# ── UDP receiver ──────────────────────────────────────────────────────────────

def udp_receiver(bind, port, pattern, tracker, log_queue, stop_event, logger):
    """
    Listens for GELF packets over UDP.

    UDP is fire-and-forget — packets may be lost under heavy load or
    network issues but has zero connection overhead. Each UDP packet
    is one complete GELF message (one log line).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    sock.bind((bind, port))
    logger.info(f"{C['green']}UDP listening on {bind}:{port}{C['reset']}")

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        except Exception as e:
            if not stop_event.is_set():
                logger.error(f"UDP error: {e}")
            continue

        msg = parse_gelf(data)
        if msg:
            enqueue(msg, addr[0], "UDP", pattern, tracker, log_queue)

    sock.close()


# ── TCP receiver ──────────────────────────────────────────────────────────────

def handle_tcp_client(conn, addr, pattern, tracker, log_queue, stop_event):
    """
    Handles one TCP client connection (one container).

    GELF over TCP sends messages delimited by a null byte or newline.
    A buffer accumulates bytes until a delimiter is found, at which
    point the complete message is parsed and processed.
    """
    buf = b""
    conn.settimeout(5.0)
    try:
        while not stop_event.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            while True:
                found = False
                for delim in (b"\x00", b"\n"):
                    idx = buf.find(delim)
                    if idx != -1:
                        raw, buf = buf[:idx], buf[idx + 1:]
                        msg = parse_gelf(raw)
                        if msg:
                            enqueue(msg, addr[0], "TCP", pattern, tracker, log_queue)
                        found = True
                        break
                if not found:
                    break
    finally:
        conn.close()


def tcp_receiver(bind, port, pattern, tracker, log_queue, stop_event, logger):
    """
    Listens for incoming TCP connections from containers.
    Each connected container gets its own handler thread.
    TCP guarantees delivery — no log loss under network pressure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    sock.bind((bind, port))
    sock.listen(128)
    logger.info(f"{C['green']}TCP listening on {bind}:{port}{C['reset']}")

    while not stop_event.is_set():
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except Exception as e:
            if not stop_event.is_set():
                logger.error(f"TCP error: {e}")
            continue
        threading.Thread(
            target=handle_tcp_client,
            args=(conn, addr, pattern, tracker, log_queue, stop_event),
            daemon=True,
        ).start()

    sock.close()


# ── Logger setup ──────────────────────────────────────────────────────────────

def setup_logger(alert_log=None):
    """
    Configures two log outputs:
      stdout     — all messages including INFO/DEBUG (heartbeats, normal logs)
      alert file — WARNING and above only (ERROR, CRITICAL, DOWN, RECOVERED)
    """
    logger = logging.getLogger("gelf_monitor")
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    if alert_log:
        fh = logging.FileHandler(alert_log)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        ))
        fh.setLevel(logging.WARNING)
        logger.addHandler(fh)

    return logger


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Central GELF log monitor with downtime detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python gelf_monitor.py
  python gelf_monitor.py --udp-port 12202 --tcp-port 12202
  python gelf_monitor.py --heartbeat-timeout 120 --alert-log /var/log/alerts.log
  python gelf_monitor.py --no-tcp --keywords ERROR CRITICAL FATAL
        """
    )

    p.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Network interface to listen on. "
             "0.0.0.0 accepts connections from any host (default). "
             "Use 127.0.0.1 to accept local connections only."
    )
    p.add_argument(
        "--udp-port",
        type=int,
        default=12201,
        help="UDP port to listen on. Must match gelf-address port in your "
             "app docker-compose.yml. (default: 12201)"
    )
    p.add_argument(
        "--tcp-port",
        type=int,
        default=12201,
        help="TCP port to listen on. Must match gelf-address port in your "
             "app docker-compose.yml. (default: 12201)"
    )
    p.add_argument(
        "--no-udp",
        action="store_true",
        help="Disable the UDP listener. Use when all containers send via tcp://."
    )
    p.add_argument(
        "--no-tcp",
        action="store_true",
        help="Disable the TCP listener. Use when all containers send via udp://."
    )
    p.add_argument(
        "--heartbeat-timeout",
        type=int,
        default=60,
        help="Seconds of silence before a container is declared DOWN. "
             "Should be at least 2x your app heartbeat interval (default interval "
             "is 30s, so default timeout is 60s). (default: 60)"
    )
    p.add_argument(
        "--watchdog-interval",
        type=int,
        default=15,
        help="How often in seconds the monitor checks all containers for silence. "
             "(default: 15)"
    )
    p.add_argument(
        "--keywords",
        nargs="*",
        default=DEFAULT_KEYWORDS,
        help="Words to scan for in log messages. Any match triggers a coloured "
             "alert. Replaces the default list entirely if specified. "
             f"(default: {' '.join(DEFAULT_KEYWORDS)})"
    )
    p.add_argument(
        "--alert-log",
        metavar="PATH",
        help="File to write ERROR/WARNING/DOWN alerts to in addition to stdout. "
             "Normal INFO logs and heartbeats are not written to this file. "
             "Example: --alert-log /var/log/docker-alerts.log"
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw GELF packet received from Docker for every message, "
             "before any parsing. Use this to verify the monitor is receiving "
             "packets and to see exactly what Docker is sending."
    )
    p.add_argument(
        "--web-port",
        type=int,
        default=4443,
        help="Port for the web dashboard. (default: 4443)"
    )
    p.add_argument(
        "--no-web",
        action="store_true",
        help="Disable the web dashboard."
    )
    p.add_argument(
        "--cert",
        metavar="PATH",
        default="",
        help="TLS certificate file for the web dashboard (PEM). "
             "Example: --cert certs/ssl.lab.int.crt"
    )
    p.add_argument(
        "--key",
        metavar="PATH",
        default="",
        help="TLS private key file for the web dashboard (PEM). "
             "Example: --key certs/ssl.lab.int.key"
    )
    p.add_argument(
        "--cef-port",
        type=int,
        default=12202,
        help="TCP port to receive CEF messages from the HSG pipeline and convert "
             "back to GELF for the dashboard. (default: 12202)"
    )
    p.add_argument(
        "--no-cef",
        action="store_true",
        help="Disable the CEF-to-GELF TCP listener."
    )

    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    logger = setup_logger(args.alert_log)
    pattern = build_pattern(args.keywords)
    stats = Stats()
    log_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    tracker = HeartbeatTracker(
        timeout_seconds=args.heartbeat_timeout,
        watchdog_interval=args.watchdog_interval,
    )

    # Startup banner
    print(f"\n{C['bold']}{'=' * 72}{C['reset']}")
    print(f"  {C['white']}Central Docker Log Monitor{C['reset']}")
    print(f"  Bind               : {args.bind}")
    print(f"  UDP port           : {args.udp_port}")
    print(f"  TCP port           : {args.tcp_port}")
    print(f"  Heartbeat timeout  : {args.heartbeat_timeout}s")
    print(f"  Watchdog interval  : {args.watchdog_interval}s")
    if args.alert_log:
        print(f"  Alert log          : {args.alert_log}")
    if not args.no_web:
        _proto = "https" if (args.cert and args.key) else "http"
        print(f"  Web dashboard      : {_proto}://localhost:{args.web_port}")
    if not args.no_cef:
        print(f"  CEF/TCP port       : {args.cef_port}")
    if args.raw:
        print(f"  {C['yellow']}Raw mode ON — printing every GELF packet as received{C['reset']}")
    print(f"  Keywords           : {' '.join(args.keywords)}")
    print(f"{C['bold']}{'=' * 72}{C['reset']}\n")

    # Patch enqueue to print raw packets if --raw is set
    original_enqueue = enqueue
    def enqueue_with_raw(msg, sender_ip, proto, pattern, tracker, log_queue):
        print(
            f"{C['grey']}[RAW {proto} from {sender_ip}]{C['reset']} "
            f"{json.dumps(msg, indent=2)}"
        )
        original_enqueue(msg, sender_ip, proto, pattern, tracker, log_queue)

    active_enqueue = enqueue_with_raw if args.raw else enqueue

    # Override udp/tcp receivers to use active_enqueue
    def udp_recv():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind((args.bind, args.udp_port))
        logger.info(f"{C['green']}UDP listening on {args.bind}:{args.udp_port}{C['reset']}")
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except Exception as e:
                if not stop_event.is_set():
                    logger.error(f"UDP error: {e}")
                continue
            msg = parse_gelf(data)
            if msg:
                active_enqueue(msg, addr[0], "UDP", pattern, tracker, log_queue)
        sock.close()

    def tcp_recv():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind((args.bind, args.tcp_port))
        sock.listen(128)
        logger.info(f"{C['green']}TCP listening on {args.bind}:{args.tcp_port}{C['reset']}")
        while not stop_event.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except Exception as e:
                if not stop_event.is_set():
                    logger.error(f"TCP error: {e}")
                continue
            def handle(c=conn, a=addr):
                buf = b""
                c.settimeout(5.0)
                try:
                    while not stop_event.is_set():
                        try:
                            chunk = c.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while True:
                            found = False
                            for delim in (b"\x00", b"\n"):
                                idx = buf.find(delim)
                                if idx != -1:
                                    raw, buf = buf[:idx], buf[idx + 1:]
                                    m = parse_gelf(raw)
                                    if m:
                                        active_enqueue(m, a[0], "TCP", pattern, tracker, log_queue)
                                    found = True
                                    break
                            if not found:
                                break
                finally:
                    c.close()
            threading.Thread(target=handle, daemon=True).start()
        sock.close()

    # Dashboard state (shared between printer and web server)
    dashboard = DashboardState()

    # Start watchdog
    tracker.start_watchdog(log_queue, stop_event)

    # Start listeners
    if not args.no_udp:
        threading.Thread(target=udp_recv, daemon=True, name="udp").start()
    if not args.no_tcp:
        threading.Thread(target=tcp_recv, daemon=True, name="tcp").start()

    # Start CEF-to-GELF listener
    if not args.no_cef:
        threading.Thread(
            target=cef_tcp_receiver,
            args=(args.bind, args.cef_port, pattern, tracker, log_queue,
                  stop_event, logger),
            daemon=True, name="cef"
        ).start()

    # Start web dashboard
    if not args.no_web:
        threading.Thread(
            target=web_server,
            args=(args.bind, args.web_port, dashboard, tracker, stats, logger,
                  args.cert, args.key),
            daemon=True, name="web"
        ).start()

    # Start printer
    pt = threading.Thread(
        target=printer,
        args=(log_queue, logger, stats, stop_event, dashboard),
        daemon=True, name="printer"
    )
    pt.start()

    # Graceful shutdown on Ctrl+C or docker stop
    def shutdown(sig, frame):
        print(f"\n{C['yellow']}Shutting down...{C['reset']}")
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    stop_event.wait()
    pt.join(timeout=2)
    print(stats.summary())


if __name__ == "__main__":
    main()
