# CLAUDE.md — App-Monitor Project

This file provides full context for continuing this project in Claude Code.
Do not delete or modify this file — it is the source of truth for the project.

---

## Project Goal

Build a centralized log monitoring system for Docker containers and Kubernetes pods
running across multiple hosts. The end goal is to know in real time if any application
or container is down, and to detect ERROR/WARNING conditions across all services.

---

## System Architecture

```
Docker hosts (any network)
  App stdout → Docker gelf log driver → GELF
                                          └─→ logship (GELF→CEF filter)
                                                └─→ HSG security device (UDP)
                                                      └─→ CEF→GELF conversion
                                                            └─→ app-monitor :12202 (TCP)

Kubernetes pods (same cluster as app-monitor)
  App stdout + applog GELF sender → GELF UDP direct → app-monitor :12201

app-monitor
  └── gelf_monitor.py — receives GELF, classifies events, serves HTTPS dashboard
```

---

## What Has Been Built

### 1. `applog/` — Python logging package (dropped into each app)

A reusable Python package that every app imports to emit structured logs,
lifecycle events, and heartbeats to stdout. Docker captures stdout and the
gelf log driver ships it to the central monitor.

**Files:**
```
applog/
├── __init__.py        — package entry point, exports all public symbols
├── logging_setup.py   — JSON formatter, get_logger() function
├── lifecycle.py       — emit(), install_crash_handler(), register_shutdown_hooks()
└── heartbeat.py       — Heartbeat class, emits HEARTBEAT log every N seconds
```

**How it is used in any Python app:**
```python
from applog import emit, install_crash_handler, register_shutdown_hooks, Heartbeat

if __name__ == "__main__":
    install_crash_handler()               # logs CRASH on unhandled exception
    register_shutdown_hooks()             # logs SHUTDOWN on docker stop / SIGTERM
    emit("STARTUP")                       # logs that the app is starting
    emit("READY")                         # logs that the app finished initialising
    Heartbeat(interval_seconds=30).start() # emits HEARTBEAT every 30s in background
    your_existing_blocking_call()          # your app runs here
```

**For gunicorn apps** — use `gunicorn_config.py` hooks instead of `__main__`:

Gunicorn has its own lifecycle hooks — do **not** use `if __name__ == "__main__":`.
Instead, create or extend `gunicorn_config.py` in your project root and wire applog
into gunicorn's server hooks:

```python
# gunicorn_config.py
# If using eventlet/gevent workers, monkey_patch() must come first:
#   import eventlet; eventlet.monkey_patch()

from applog import emit, install_crash_handler, Heartbeat

_heartbeat = None

def on_starting(server):
    emit("STARTUP")

def when_ready(server):
    global _heartbeat
    emit("READY")
    _heartbeat = Heartbeat(interval_seconds=30)
    _heartbeat.start()

def on_exit(server):
    emit("SHUTDOWN")

def post_fork(server, worker):
    # Install crash handler in each worker process
    install_crash_handler()
```

Tell gunicorn to use this file — either via the command line:
```
gunicorn -c gunicorn_config.py myapp:app
```
or in `docker-compose.yml`:
```yaml
command: gunicorn -c gunicorn_config.py myapp:app
```

**Gunicorn-specific notes:**
- `register_shutdown_hooks()` is NOT used — gunicorn's `on_exit` hook replaces it
- `on_starting` fires in the master process before workers fork — emit STARTUP there
- `when_ready` fires when the master is ready to accept connections — emit READY + start heartbeat
- `on_exit` fires when the master exits (gunicorn stop / `docker stop`) — emit SHUTDOWN
- `post_fork` fires in each worker after fork — install crash handler per worker
- Heartbeat runs in the master process; one heartbeat covers the whole gunicorn instance
- The `_heartbeat` global prevents the object from being garbage-collected

**With `APP_MONITOR` toggle for gunicorn:**
```python
# gunicorn_config.py
import os
_MONITOR = os.getenv("APP_MONITOR", "1") == "1"

if _MONITOR:
    from applog import emit, install_crash_handler, Heartbeat
    _heartbeat = None

def on_starting(server):
    if _MONITOR: emit("STARTUP")

def when_ready(server):
    if _MONITOR:
        global _heartbeat
        emit("READY")
        _heartbeat = Heartbeat(interval_seconds=30)
        _heartbeat.start()

def on_exit(server):
    if _MONITOR: emit("SHUTDOWN")

def post_fork(server, worker):
    if _MONITOR: install_crash_handler()
```

**Important rules:**
- These five lines always go inside `if __name__ == "__main__":`, never at module level
- Order is always: install_crash_handler → register_shutdown_hooks → emit STARTUP →
  emit READY → Heartbeat → blocking call
- `emit()` takes no required arguments beyond the event name — always just `emit("READY")`
- The package directory is named `applog` (not `logging` — that shadows stdlib)
- No pip dependencies — stdlib only

**What each module does:**

`logging_setup.py`
- Provides `get_logger(name)` which returns a standard Python logger
- All log output is formatted as a single JSON object per line to stdout
- JSON fields: timestamp, level, logger, message, service, version, host
- Extra fields passed via `extra={"_x_key": value}` appear as `"key": value` in JSON

`lifecycle.py`
- `install_crash_handler()` — replaces sys.excepthook to catch unhandled exceptions
  and log them as CRASH events before the process dies
- `register_shutdown_hooks()` — installs SIGTERM handler and atexit hook to log
  SHUTDOWN when docker stop is called or the process exits cleanly
- `emit(event, **kwargs)` — logs a structured lifecycle event (STARTUP, READY,
  SHUTDOWN, CRASH)

`heartbeat.py`
- `Heartbeat(interval_seconds=30)` — creates a daemon thread that calls
  `log.info("HEARTBEAT", extra={"_x_event": "HEARTBEAT"})` every N seconds
- The monitor declares a container DOWN if it stops receiving any logs for
  longer than --heartbeat-timeout seconds (default 90s)
- Call `.start()` after creating it
- `_emit()` is wrapped in `try/except` — a transient logging error skips one
  beat rather than killing the thread

`lifecycle.py`
- `install_crash_handler()` covers both the main thread (`sys.excepthook`) and
  background threads (`threading.excepthook`, Python 3.8+) — both emit CRASH
- SIGTERM handler flushes stdout and sleeps 0.5s before exiting to let the
  Docker gelf driver ship the final SHUTDOWN log before the pipe closes

**Pending — K8s direct GELF sender:**
A `gelf_sender.py` module is planned but not yet built. When `GELF_HOST` env var is
set, applog will send GELF packets directly over UDP to app-monitor in addition to
writing to stdout — enabling K8s pod monitoring without a log collector DaemonSet.

---

### 2. `data/gelf_monitor.py` — Central log receiver + web dashboard

A standalone Python script that runs on a dedicated host (Docker or Kubernetes).
Listens for GELF packets and serves a live HTTPS web dashboard.

**No pip dependencies — stdlib only.**

**Key behaviour:**

- Listens on UDP port 12101 for standard GELF (K8s pods via gelf_sender, Docker udp:// containers)
- Listens on TCP port 12201 for standard GELF (direct Docker or K8s TCP clients)
- Listens on TCP port 12202 for CEF→GELF inbound (from HSG in production)
- Parses GELF packets: plain JSON, gzip-compressed (Docker UDP gelf driver), or zlib-compressed
- Parses `short_message` as JSON to extract your app's structured fields
- Detects ERROR/WARNING by scanning the message text and your app's level field
- GELF numeric level (stderr=ERROR) is only trusted when `short_message` is JSON —
  prevents false positives from containers writing plain text to stderr
- Detects lifecycle events (STARTUP, READY, SHUTDOWN, CRASH) from the event field
- Tracks last-seen time per container — declares DOWN after silence > timeout
- Emits RECOVERED when a DOWN container resumes sending logs
- Prints coloured output to stdout
- Optionally writes alerts to a file (--alert-log)
- Serves web dashboard on HTTPS port 4443 (configurable)

**Command line arguments:**
```
--bind               Network interface (default: 0.0.0.0 = all interfaces)
--udp-port           UDP listen port (default: 12201, Docker uses 12101)
--tcp-port           TCP listen port (default: 12201)
--cef-port           CEF→GELF TCP listen port (default: 12202)
--no-udp             Disable UDP listener
--no-tcp             Disable TCP listener
--no-cef             Disable CEF listener
--heartbeat-timeout  Seconds of silence before DOWN alert (default: 90)
--watchdog-interval  How often to check for silent containers (default: 15s)
--keywords           Words to scan for — replaces default list entirely
--alert-log          File path for alert-only output (WARNING and above)
--raw                Print every raw GELF packet as received — use for debugging
--web-port           Web dashboard port (default: 4443)
--no-web             Disable the web dashboard
--cert               TLS certificate file (PEM) for HTTPS dashboard
--key                TLS private key file (PEM) for HTTPS dashboard
```

**The `--raw` flag is the primary debugging tool.** Run with `--raw` when first
deploying to verify packets are arriving and to see exactly what Docker sends.

---

### 3. `data/dashboard.html` — Web dashboard UI

A separate HTML file read from disk on every request — edit the UI and refresh
the browser without restarting the monitor.

**Dashboard sections (top to bottom):**

1. **Container Status** — header bar + tile grid:
   - Header line 1: KPI pills centered (TOTAL/UP/ERR/WARN/DOWN with glow), title left
   - Header line 2: filter chips (HOST / GROUP / SUB-GROUP) — updates KPI counts when active
   - Tiles sorted: active alerts first (DOWN→CRIT→ERR→WARN), then NIPR→SIPR→ungrouped,
     then sub-group α, then container name α
   - 6 tiles per row (minmax 230px on 1600px max-width panel)
   - Each tile: colour left stripe (green/amber/red), group stripe top (NIPR/SIPR),
     container name, sub-group label, host, ♥ age counter, alert badge, Ack/Clear/Remove
   - Alert badge hover tooltip shows last alert: UTC timestamp + severity + message
   - Group tag button (bottom-right) — cycles none → NIPR → SIPR → none
   - Click tile to jump to that container's stream

2. **Alert Stream** — real-time log feed for one selected container at a time:

3. **Session Stats** — collapsible (click header to toggle); per-container message and
   alert counts with "since HH:MM" session start timestamp

**Alert Stream** — real-time log feed for one selected container at a time:
   - Container selector dropdown + Connect/Disconnect button
   - Filter chips: ALL / DOWN / RECOVERED / ERR / WARN / CRIT / CRASH /
     STARTUP-READY-SHUTDOWN / HB
   - History (past events from this session) loads first, newest at top;
     live events prepend above history as they arrive
   - Disconnect clears the feed

**Acknowledge feature:**

Used to mark an alert as a known external/upstream issue rather than an app fault.

- Cards with an active alert show an **Ack** button
- Clicking Ack opens a modal for an optional note (e.g. "upstream API outage")
- Acknowledged cards show a blue **ACK** badge + **Clear** button
- Ack persists through new incoming alerts for that container — it does NOT
  auto-clear on new errors or on STARTUP/READY
- Ack auto-clears only on **RECOVERED** (watchdog UP event) — the issue is gone
- Users can manually clear with the **Clear** button at any time
- Ack state is server-side (shared across browser sessions, survives page refresh)
- Ack state is in-memory and resets when the monitor process restarts

**Container grouping:**
- Each container can be assigned a **group** (NIPR or SIPR) and a free-text **sub-group**
- Group is set by clicking the bottom-right tag on each card (cycles none→NIPR→SIPR→none)
- Sub-group is typed directly on the card (click the label next to container name, type, Enter)
- Both are persisted to `data/groups.json` (excluded from git — created automatically on first save)
- `groups.json` format: `{"host:container": {"group": "NIPR", "subgroup": "web"}, ...}`
- Old format `{"host:container": "NIPR"}` is auto-migrated on load

**Login overlay:**
- Dashboard shows a full-screen access code prompt before displaying any data
- Code is checked client-side only — suitable for internal networks
- Session is stored in `sessionStorage` — survives page refresh, cleared on tab close

**API endpoints served by gelf_monitor.py:**
```
GET  /               — serve dashboard.html
GET  /api/status     — container list with status, last_severity, acked, ack_note, group, subgroup
GET  /api/history    — stored alert events for a container (?container=host:name)
GET  /api/stats      — per-container counts + session start time
GET  /api/stream     — SSE stream for a container (?container=host:name)
POST /api/ack        — acknowledge container {"key":"host:name","note":"..."}
POST /api/unack      — clear acknowledgement {"key":"host:name"}
POST /api/remove     — remove container from session {"key":"host:name"}
POST /api/group      — set NIPR/SIPR group {"key":"host:name","group":"NIPR"}
POST /api/subgroup   — set sub-group label {"key":"host:name","subgroup":"web"}
```

**TLS certificates** live in `data/certs/`:
- `ssl.lab.int.crt` — server cert (CN=ssl.lab.int, valid to 2028-07-13)
- `ssl.lab.int.key` — private key (not committed to git)
- `lab.int-ca.crt`  — Lab Internal CA (for browser trust, not used server-side)

---

### 4. `logship/` — GELF→CEF middleware (companion project at `/apps/logship/`)

A Docker container that sits between monitored apps and the HSG security device.
Required because the HSG only accepts CEF format, not GELF.

**Flow:** App → Docker gelf driver → GELF → logship → CEF UDP → HSG → CEF→GELF TCP → app-monitor

**Files:**
```
/apps/logship/
├── docker-compose.yml     — logship container (network_mode: host, HSG_HOST env var)
└── data/
    └── logship.py         — GELF receiver, INFO filter, CEF converter, HSG sender
```

**Key behaviour:**
- Listens on UDP+TCP port 9000 for GELF from Docker containers
- Filters out INFO/DEBUG logs — only forwards ERROR/WARNING/lifecycle/keyword events
- Converts forwarded packets to CEF using `message_to_cef()` with normalised field names
- Sends CEF over UDP to `HSG_HOST:HSG_PORT`
- Self-monitors: emits STARTUP, READY, HEARTBEAT, SHUTDOWN via synthetic packets
  through its own pipeline (no Docker gelf driver — avoids circular dependency)
- Logs forwarded packets: `FWD container@host EVENT` and self-events: `SELF logship EVENT`

**Environment variables:**
```
HSG_HOST   IP/hostname of HSG (required)
HSG_PORT   UDP destination port on HSG (default: 514)
BIND       Listen interface (default: 0.0.0.0)
UDP_PORT   GELF UDP listen port (default: 9000)
TCP_PORT   GELF TCP listen port (default: 9000)
```

**docker-compose.yml:**
```yaml
services:
  logship:
    image: python:3.13-slim-u9
    container_name: logship
    working_dir: /app
    network_mode: host
    volumes:
      - ./data:/app
    environment:
      - HSG_HOST=172.16.0.46
      - HSG_PORT=7001
    command: python logship.py
    restart: unless-stopped
```

**`network_mode: host`** is required — Docker's UDP proxy has a hairpin NAT limitation
that drops UDP packets sent from the same host. Host network bypasses this.

**Key function — `gelf_to_record()`:**
Normalises GELF field names to a clean dict before CEF conversion:
```python
{
  "ts":        "2025-...",
  "host":      "docker-host-1",
  "container": "my-app",        # from _container_name, stripped of leading /
  "level":     "ERROR",
  "msg":       "database timeout",
  "event":     "",
}
```
CEF keys map directly from this dict — `container` is what app-monitor's `cef_to_gelf()`
looks for when reconstructing the GELF envelope on port 12202.

---

### 5. `APP_MONITOR` env-var toggle (per monitored app)

Any Python app using `applog` can be toggled without code changes:

```python
# In app.py — at the top, after stdlib imports:
import os
_MONITOR = os.getenv("APP_MONITOR", "1") == "1"
if _MONITOR:
    from applog import emit, install_crash_handler, register_shutdown_hooks, Heartbeat

# In __main__:
if _MONITOR:
    install_crash_handler()
    register_shutdown_hooks()
    emit("STARTUP")
    emit("READY")
    Heartbeat(interval_seconds=30).start()
```

Set `APP_MONITOR=0` in the container's environment to disable monitoring without
removing the logging block.

---

## Deployment

### Docker — app-monitor

Current `docker-compose.yml` (at project root, `./data` is the working dir):
```yaml
services:
  app-monitor:
    image: python:3.13-slim-u9
    container_name: app-monitor
    working_dir: /app
    volumes:
      - ./data:/app
      - ./data/log/alerts.log:/var/log/gelf-alerts.log
    command: >
      python gelf_monitor.py
      --alert-log /var/log/gelf-alerts.log
      --udp-port 12101
      --cert certs/ssl.lab.int.crt
      --key  certs/ssl.lab.int.key
    ports:
      - "12101:12101/udp"
      - "12201:12201/tcp"
      - "12202:12202/tcp"
      - "4443:4443/tcp"
    restart: unless-stopped
```

### Kubernetes — app-monitor

Manifests in `k8s/` — files are numbered in apply order. See `k8s/DEPLOY.md` for
full instructions. Quick reference:
```bash
microk8s kubectl apply -f k8s/01-namespace.yaml
microk8s kubectl apply -f k8s/02-pv.yaml && microk8s kubectl apply -f k8s/03-pvc.yaml
microk8s kubectl apply -f k8s/04-applog-pv.yaml && microk8s kubectl apply -f k8s/05-applog-pvc.yaml
microk8s kubectl apply -f k8s/06-deployment.yaml
microk8s kubectl apply -f k8s/07-service.yaml        # two ClusterIPs: TCP + UDP (separate)
microk8s kubectl apply -f k8s/08-service-external.yaml  # LoadBalancer: TCP 12201+12202 + UDP 12101
microk8s kubectl create secret tls app-monitor-tls \
  --cert data/certs/ssl.lab.int.crt --key data/certs/ssl.lab.int.key -n monitoring
microk8s kubectl apply -f k8s/09-frontend-ingress.yaml  # dashboard Service + Ingress
```

Dashboard: `https://monitor.lab.int` (DNS → 172.16.0.91, nginx ingress LoadBalancer IP)
GELF/CEF external: `<MetalLB-IP>:12101/udp`, `<MetalLB-IP>:12201/tcp`, `<MetalLB-IP>:12202/tcp`

- PV pinned to node `ubt2`; data at `/apps/app-monitor/data` (= `/app` in container)
- TLS terminated by nginx ingress; gelf_monitor serves plain HTTP in K8s
- `k8s/07-service.yaml` has TWO ClusterIP services: `app-monitor` (TCP) and
  `app-monitor-udp` (UDP 12101) — split to avoid kube-proxy mixed-protocol issues
- UDP on K8s is still under investigation (VKS may not route UDP ClusterIP correctly)

**Running Docker and K8s simultaneously on the same host is not supported** —
ports conflict.

### Each app host — docker-compose.yml (Docker path)

Add logging block to every service:
```yaml
services:
  your-service:
    image: your-image:tag
    logging:
      driver: gelf
      options:
        gelf-address: "tcp://<logship-host-ip>:9000"
        tag: "{{.Name}}"
```

### Each K8s pod — applog GELF sender

`gelf_sender.py` is built and integrated. Add to pod spec:
```yaml
env:
  - name: GELF_HOST
    value: "app-monitor-udp.monitoring.svc.cluster.local"
  - name: GELF_PORT
    value: "12101"
  - name: CONTAINER_NAME      # stable name — survives pod restarts
    value: "my-service-name"
  - name: GELF_SOURCE_HOST
    valueFrom:
      fieldRef:
        fieldPath: spec.nodeName
volumeMounts:
  - name: applog
    mountPath: /app/applog
    readOnly: true
volumes:
  - name: applog
    persistentVolumeClaim:
      claimName: applog-pvc
      readOnly: true
```
See `data/applog/applog_deployment_guide.md` for full instructions.

---

## Verifying the System Works

```bash
# 1. Start monitor with --raw to see all incoming packets
python3 gelf_monitor.py --raw

# 2. Send a manual GELF test packet (use Python — nc has UDP issues)
python3 -c "
import socket,json
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.sendto(json.dumps({'version':'1.1','host':'test','_container_name':'test',
  'short_message':json.dumps({'level':'ERROR','message':'test','event':''}),'level':3}).encode(),
  ('<monitor-ip>',12101))
"

# 3. Check a container is using gelf driver
docker inspect <container> | grep -A5 LogConfig

# 4. Test DOWN detection — stop a container and wait 60s
docker compose stop your-service
# Monitor should emit [DOWN] after 60s

# 5. Test RECOVERED — restart it
docker compose start your-service
# Monitor should emit [RECOVERED]
```

---

## Output Colour Key

```
[STARTUP]    green        — container just started
[READY]      green        — container finished initialising
[SHUTDOWN]   green        — container stopping cleanly (expected)
[CRASH]      magenta      — unhandled exception before death
♥ HEARTBEAT  grey         — periodic alive signal (debug level, not in alert file)
[WARNING]    yellow bold  — WARNING keyword in log line
[ERROR]      red bold     — ERROR keyword in log line
[CRITICAL]   magenta bold — CRITICAL/FATAL keyword
[DOWN]       red bold     — no heartbeat for timeout duration
[RECOVERED]  green bold   — container back after DOWN
```

---

## File Locations

```
/apps/app-monitor/               ← git repo root (j6dtt/app-monitor)
├── CLAUDE.md                    ← this file
├── docker-compose.yml           ← Docker deployment
├── k8s/                              ← Kubernetes manifests (numbered in apply order)
│   ├── 01-namespace.yaml
│   ├── 02-pv.yaml                    ← local PV pinned to node ubt2
│   ├── 03-pvc.yaml
│   ├── 04-applog-pv.yaml             ← shared read-only applog PV
│   ├── 05-applog-pvc.yaml            ← applog PVC (apply per app namespace)
│   ├── 06-deployment.yaml
│   ├── 07-service.yaml               ← two ClusterIPs: app-monitor (TCP) + app-monitor-udp (UDP)
│   ├── 08-service-external.yaml      ← LoadBalancer: TCP 12201+12202, UDP 12101
│   ├── 09-frontend-ingress.yaml      ← dashboard ClusterIP + nginx ingress
│   └── DEPLOY.md                     ← step-by-step deployment instructions
└── data/                             ← mounted as /app in container
    ├── gelf_monitor.py               ← monitor + web server (run this)
    ├── dashboard.html                ← web dashboard UI (read on each request)
    ├── applog/                       ← drop into each monitored Python app
    │   ├── __init__.py
    │   ├── logging_setup.py
    │   ├── lifecycle.py
    │   ├── heartbeat.py
    │   ├── gelf_sender.py            ← K8s UDP GELF sender (activated by GELF_HOST)
    │   └── applog_deployment_guide.md
    ├── certs/
    │   ├── ssl.lab.int.crt           ← TLS cert (CN=ssl.lab.int, valid to 2028-07-13)
    │   ├── ssl.lab.int.key           ← private key (not in git)
    │   └── lab.int-ca.crt            ← Lab Internal CA (import into browser for trust)
    ├── log/
    │   ├── alerts.log                ← WARNING+ alerts (not in git)
    │   └── alerts_history.jsonl      ← 24h rolling alert history (not in git, auto-created)
    └── groups.json                   ← group/subgroup assignments (not in git, auto-created)

/apps/logship/                   ← companion project (not in this repo)
├── docker-compose.yml
└── data/
    └── logship.py
```

---

## Known Decisions and Constraints

- Port 12101 is app-monitor's GELF UDP port (changed from 12201 to avoid mixed-protocol kube-proxy conflict)
- Port 12201 is app-monitor's GELF TCP port; port 9000 is logship's GELF receive port
- Port 12202 is app-monitor's CEF→GELF inbound port (TCP only, one message per connection)
- The package is named `applog` not `logging` — `logging` shadows Python stdlib
- `emit()` takes no arguments beyond the event name — no extras needed
- Heartbeat interval is 30s; DOWN timeout is 60s default (2× interval)
- The monitor parses `short_message` as JSON to find your app's fields because
  Docker does not parse your JSON — it treats the entire stdout line as a string
- No external dependencies anywhere — everything is Python stdlib only
- Apps that cannot be modified (third-party images) get ERROR/WARNING detection
  only — no DOWN detection without heartbeats
- `--raw` flag is for debugging only — remove it in production (very verbose)
- GELF level check is gated on `inner` (JSON-parsed short_message) — prevents
  false ERRORs from plain-text containers that write to stderr
- Docker UDP gelf log driver gzip-compresses packets (`0x1f 0x8b` magic); TCP is
  uncompressed; `parse_gelf()` handles gzip, zlib, and plain JSON — all three formats
- Dashboard web server uses `socketserver.ThreadingMixIn` + stdlib `ssl` — no
  external web framework or TLS library needed
- `dashboard.html` is read from disk on each HTTP request — UI changes take
  effect on browser refresh, no monitor restart required
- All dashboard and ack state is in-memory — resets on monitor restart; this is
  intentional (session-scoped data, not an operational database)
- Ack persists through new incoming alerts; only clears on watchdog RECOVERED
  (UP event) or manual Clear by the user — STARTUP/READY do not clear ack
- Web dashboard port 4443; TLS cert is for ssl.lab.int (Lab Internal CA)
- K8s deployment uses `local` PV type with nodeAffinity to pin to node ubt2
- K8s ingress uses `ingressClassName: public`, host `monitor.lab.int`,
  DNS → 172.16.0.91 (MicroK8s nginx ingress LoadBalancer IP)
- logship uses `network_mode: host` — Docker UDP proxy hairpin NAT drops packets
  sent from the same host otherwise
- logship self-monitoring bypasses Docker gelf driver (circular dependency) —
  synthetic GELF packets are built internally and sent directly through the pipeline
- Heartbeat thread uses try/except around each emit — transient errors skip a beat
  rather than killing the thread silently
- `threading.excepthook` installed alongside `sys.excepthook` in `install_crash_handler()`
  — background thread crashes also emit CRASH events (Python 3.8+)
- SIGTERM handler sleeps 0.5s after stdout flush — gives Docker gelf driver time to
  ship the SHUTDOWN log before the pipe closes
- TRACEBACK keyword maps to CRITICAL severity — catches asyncio task tracebacks
  printed to stderr that don't go through logging.critical()
- Dashboard login is client-side access code only — not a real auth barrier;
  fine for internal networks, not for public exposure
- `groups.json` is excluded from git — contains runtime state, not configuration
- `groups.json` auto-migrates old format `{key: "NIPR"}` to new `{key: {group, subgroup}}`
- ERR/WARN badge auto-clears after N heartbeats: WARNING=1, ERROR=3, CRITICAL=never
- Heartbeat timeout (DOWN detection): 60s default (2× the 30s heartbeat interval)

---

## Deferred / Not Yet Done

- **Docker UDP gelf driver gzip-compresses packets** (`0x1f 0x8b`) — `parse_gelf()` in
  gelf_monitor.py and logship.py both handle gzip, zlib, and plain JSON
- UDP from Docker containers with `gelf-address: udp://` now works correctly
- UDP from K8s pods (gelf_sender, plain JSON) also handled — VKS routing still unconfirmed
- logship.py has the same gzip fix (see `/apps/logship/data/logship.py`)
- Alert forwarding (email, Slack, PagerDuty) when DOWN or CRITICAL detected
- Persistent ack state across monitor restarts (alert history is persisted via alerts_history.jsonl)
