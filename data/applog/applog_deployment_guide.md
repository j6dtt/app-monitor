Deploying monitoring on a new container
========================================

Step 1 — Drop in the applog package
-------------------------------------
Copy applog/ into the container's app directory (same level as the main script).
If the Dockerfile already has `COPY . .`, nothing extra needed.
Or map it read-only in docker-compose.yml:

    volumes:
      - /apps/app-monitor/data/applog:/app/applog:ro


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

Or in docker-compose.yml:

    command: gunicorn -c gunicorn_config.py myapp:app

Notes:
  - register_shutdown_hooks() is NOT used — on_exit() replaces it for gunicorn
  - Heartbeat runs in the master process; one heartbeat covers the whole instance
  - post_fork installs the crash handler in each worker process
  - For eventlet/gevent: monkey_patch() must precede ALL other imports in the file,
    including the applog import — patching after import breaks stdlib threading


Step 3 — Add to docker-compose.yml
------------------------------------
    #-----app-monitor-------------------------------
    environment:
      APP_MONITOR: "1"          # set to "0" to disable monitoring
    logging:
      driver: gelf
      options:
        gelf-address: "tcp://172.16.0.46:9000"
        tag: "{{.Name}}"
        cache-max-size: "10m"
        cache-max-file: "3"
    #-----------------------------------------------

Toggle: change APP_MONITOR to "0" and run `docker compose up -d`.


Toggle reference
-----------------
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
the whole block (environment line + logging block) at once.
