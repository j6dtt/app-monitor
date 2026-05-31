Deploying applog monitoring
============================

Choose the path that matches your environment:
  - Docker container  →  Steps 1 + 2a or 2b + 3A
  - Kubernetes pod    →  Steps 1 + 2a or 2b + 3B

Do NOT combine 3A and 3B on the same container — that causes double-reporting.


Step 1 — Drop in the applog package
-------------------------------------
Copy applog/ into the app directory (same level as the main script).
If the Dockerfile already has `COPY . .`, nothing extra needed.
Or map it read-only:

  Docker:
    volumes:
      - /apps/app-monitor/data/applog:/app/applog:ro

  Kubernetes (initContainer or ConfigMap mount, or bake into image):
    # Simplest: add to your Dockerfile
    COPY applog/ /app/applog/


Step 2a — Standard Python app (if __name__ == "__main__")
----------------------------------------------------------
Add to the top of the main script (after existing imports):

# -- monitoring ----------------------------------------------------------------
import os
_MONITOR = os.getenv("APP_MONITOR", "1") == "1"
if _MONITOR:
    from applog import emit, install_crash_handler, register_shutdown_hooks, Heartbeat
# ------------------------------------------------------------------------------

Add inside `if __name__ == "__main__":` before the blocking call:

    # -- monitoring -------------------------------------------------------
    if _MONITOR:
        install_crash_handler()
        register_shutdown_hooks()
        emit("STARTUP")
        emit("READY")
        Heartbeat(interval_seconds=30).start()
    # ---------------------------------------------------------------------


Step 2b — Gunicorn app (gunicorn_config.py)
-------------------------------------------
Do NOT use `if __name__ == "__main__":` — gunicorn never runs that block.
Create or extend gunicorn_config.py in the project root.

Worker type affects what goes at the TOP of the file only — the hooks are identical.

  sync / gthread workers (no monkey patching needed):

    # gunicorn_config.py
    # -- monitoring -------------------------------------------------------
    import os
    _MONITOR = os.getenv("APP_MONITOR", "1") == "1"
    if _MONITOR:
        from applog import emit, install_crash_handler, Heartbeat
        _heartbeat = None   # keep reference so it isn't garbage-collected
    # ---------------------------------------------------------------------

  eventlet workers (monkey_patch MUST come before applog import):

    # gunicorn_config.py
    # -- monitoring -------------------------------------------------------
    import eventlet
    eventlet.monkey_patch()

    import os
    _MONITOR = os.getenv("APP_MONITOR", "1") == "1"
    if _MONITOR:
        from applog import emit, install_crash_handler, Heartbeat
        _heartbeat = None
    # ---------------------------------------------------------------------

  gevent workers (monkey_patch MUST come before applog import):

    # gunicorn_config.py
    # -- monitoring -------------------------------------------------------
    from gevent import monkey
    monkey.patch_all()

    import os
    _MONITOR = os.getenv("APP_MONITOR", "1") == "1"
    if _MONITOR:
        from applog import emit, install_crash_handler, Heartbeat
        _heartbeat = None
    # ---------------------------------------------------------------------

Then add these hooks (same for all worker types):

    # -- monitoring -------------------------------------------------------
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
    # ---------------------------------------------------------------------

Tell gunicorn to use the file:

    gunicorn -c gunicorn_config.py myapp:app

Notes:
  - register_shutdown_hooks() is NOT used — on_exit() replaces it for gunicorn
  - Heartbeat runs in the master process; one heartbeat covers the whole instance
  - post_fork installs the crash handler in each worker process
  - For eventlet/gevent: monkey_patch() must precede ALL other imports in the file,
    including the applog import — patching after import breaks stdlib threading


Step 3A — Docker container (gelf log driver)
---------------------------------------------
The Docker gelf log driver ships all container stdout to app-monitor.
Do NOT set GELF_HOST — the log driver handles delivery.

Add to docker-compose.yml:

    #-----app-monitor-------------------------------
    environment:
      APP_MONITOR: "1"          # set to "0" to disable monitoring
    logging:
      driver: gelf
      options:
        gelf-address: "tcp://172.16.0.46:9000"   # logship address
        tag: "{{.Name}}"
        cache-max-size: "10m"
        cache-max-file: "3"
    #-----------------------------------------------

Toggle: change APP_MONITOR to "0" and run `docker compose up -d`.

Toggle reference:
+-------------+--------------------+------------------------------------------------------------------------------+
| APP_MONITOR | gelf logging block |                                Effect                                        |
+-------------+--------------------+------------------------------------------------------------------------------+
| "1"         | enabled            | Full monitoring — lifecycle events, heartbeats, DOWN detection, log scanning |
| "0"         | enabled            | Partial — log scanning and ERROR/WARNING only, no DOWN detection             |
| "0"         | commented out      | Fully off — nothing reaches app-monitor                                      |
| "1"         | commented out      | Broken — applog emits events but nothing receives them                       |
+-------------+--------------------+------------------------------------------------------------------------------+

For a clean on/off toggle, both APP_MONITOR and the logging block need to change
together. The #-----app-monitor----- comment markers make it easy to comment out
both at once.


Step 3B — Kubernetes pod (direct GELF sender)
----------------------------------------------
K8s pods have no gelf log driver. applog sends GELF packets directly to
app-monitor via UDP. Do NOT configure a gelf log driver — that causes double-reporting.

Three things are required — all three must be done:

  [1] applog baked into the image (Step 1)
  [2] monitoring hooks in gunicorn_conf.py or __main__ (Step 2a / 2b)
  [3] app-monitor env vars in the pod spec (this step)

---- [1] PV/PVC — mount applog from the host ----

applog lives at /apps/app-monitor/data/applog on node ubt2 and is shared
read-only across all pods. Apply once per cluster, then once per namespace:

    # Apply once (cluster-wide PV):
    kubectl apply -f k8s/applog-pv.yaml

    # Apply once per namespace the app lives in:
    kubectl apply -f k8s/applog-pvc.yaml -n <namespace>

The PVC (applog-pvc) is then referenced in the pod spec — see [3] below.
No Dockerfile changes needed — applog is never baked into the image.

---- [2] gunicorn_conf.py — monitoring hooks ----

For a gunicorn app (e.g. apiv2 which runs gunicorn -c gunicorn_conf.py):

    # gunicorn_conf.py  (sync/gthread workers)
    # -- monitoring -------------------------------------------------------
    import os
    _MONITOR = os.getenv("APP_MONITOR", "1") == "1"
    if _MONITOR:
        from applog import emit, install_crash_handler, Heartbeat
        _heartbeat = None
    # ---------------------------------------------------------------------

    # ... your existing gunicorn settings (bind, workers, etc.) ...

    # -- monitoring -------------------------------------------------------
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
    # ---------------------------------------------------------------------

See Step 2b for eventlet/gevent worker variants.

---- [3] pod spec — env vars ----

Add the #-----app-monitor----- block to the container's env section.
The Service and Ingress manifests do not need any changes.

Example — based on a real gunicorn deployment (apiv2):

    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: apiv2
      labels:
        app: apiv2
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: apiv2
      template:
        metadata:
          labels:
            app: apiv2
        spec:
          containers:
            - name: apiv2
              image: th0pham/python:3.13-slim-u8
              imagePullPolicy: IfNotPresent
              workingDir: /app
              command: ["gunicorn", "-c", "gunicorn_conf.py", "app:app"]
              ports:
                - containerPort: 443
                  protocol: TCP
              env:
                - name: NFS_BASE_DIR
                  value: /nfs/shared/api
                #-----app-monitor-------------------------------
                - name: APP_MONITOR
                  value: "1"              # set to "0" to disable monitoring
                - name: GELF_HOST
                  value: "app-monitor-udp.monitoring.svc.cluster.local"
                - name: GELF_PORT
                  value: "12201"
                - name: CONTAINER_NAME
                  value: "apiv2"          # stable name — must match across restarts
                - name: GELF_SOURCE_HOST
                  valueFrom:
                    fieldRef:
                      fieldPath: spec.nodeName   # K8s node name (downward API)
                #-----------------------------------------------
              volumeMounts:
                - name: apiv2-data
                  mountPath: /app
                  subPath: data
                - name: apiv2-data
                  mountPath: /nfs/shared/api
                  subPath: nfs/shared/api
                #-----app-monitor-------------------------------
                - name: applog
                  mountPath: /app/applog
                  readOnly: true
                #-----------------------------------------------
              livenessProbe:
                tcpSocket:
                  port: 443
                initialDelaySeconds: 10
                periodSeconds: 30
                timeoutSeconds: 5
                failureThreshold: 3
          volumes:
            - name: apiv2-data
              persistentVolumeClaim:
                claimName: apiv2-app-data
            #-----app-monitor-------------------------------
            - name: applog
              persistentVolumeClaim:
                claimName: applog-pvc
                readOnly: true
            #-----------------------------------------------

Toggle: set APP_MONITOR to "0" and roll the deployment:

    kubectl set env deployment/apiv2 APP_MONITOR=0 -n <namespace>

Toggle reference:
+-------------+------------------+------------------------------------------+
| APP_MONITOR | GELF_HOST set    |                Effect                    |
+-------------+------------------+------------------------------------------+
| "1"         | yes              | Full monitoring via direct GELF UDP      |
| "0"         | yes              | Disabled — no events sent                |
| "1"         | no               | Broken — applog loaded but nothing sent  |
| "1"         | yes (+ log drv)  | Double-reporting — DO NOT DO THIS        |
+-------------+------------------+------------------------------------------+

CONTAINER_NAME must be set to a fixed string (e.g. "apiv2"). Without it, each pod
restart uses the pod hostname which changes every time, creating a new dashboard card
on every restart instead of showing SHUTDOWN → STARTUP on the same card.
