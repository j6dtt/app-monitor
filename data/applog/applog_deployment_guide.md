  ---
  Deploying monitoring on a new container

  1. Drop in the applog package
  Copy applog/ into the container's app directory (same level as the main script). If the Dockerfile already has COPY . ., nothing extra needed. 
  Or map '- /apps/app-monitor/data/applog/:/app/applog:ro'
  2. Add to the top of the main script (after existing imports):
# -- monitoring ----------------------------------------------------------------
import os
_MONITOR = os.getenv("APP_MONITOR", "1") == "1"
if _MONITOR:
    from applog import emit, install_crash_handler, register_shutdown_hooks, Heartbeat
# ------------------------------------------------------------------------------

  3. Add to if __name__ == "__main__": (before the blocking call):
    # -- monitoring -------------------------------------------------------
    if _MONITOR:
        install_crash_handler()
        register_shutdown_hooks()
        emit("STARTUP")
        emit("READY")
        Heartbeat(interval_seconds=30).start()
    # ---------------------------------------------------------------------

  4. Add to docker-compose.yml:
    #-----app-monitor-------------------------------    
    environment:
      APP_MONITOR: "1"          # set to "0" to disable monitoring
    logging:
      driver: gelf
      options:
        gelf-address: "tcp://172.16.0.46:12201"
        tag: "{{.Name}}"
        cache-max-size: "10m"
        cache-max-file: "3"
    #-----------------------------------------------

  Toggle: change APP_MONITOR to "0" and run docker compose up -d.



  +-----------------------------------------------------------------------------------------------------------------+
  � APP_MONITOR � gelf logging block �                                    Effect                                    �
  +-------------+--------------------+------------------------------------------------------------------------------�
  � "1"         � enabled            � Full monitoring � lifecycle events, heartbeats, DOWN detection, log scanning �
  +-------------+--------------------+------------------------------------------------------------------------------�
  � "0"         � enabled            � Partial � log scanning and ERROR/WARNING only, no DOWN detection             �
  +-------------+--------------------+------------------------------------------------------------------------------�
  � "0"         � commented out      � Fully off � nothing reaches app-monitor                                      �
  +-------------+--------------------+------------------------------------------------------------------------------�
  � "1"         � commented out      � Broken � applog emits events but nothing receives them                       �
  +-----------------------------------------------------------------------------------------------------------------+

  So if your intent is a clean on/off toggle, both need to change together. The #-----app-monitor----- comment markers are already there to make that easy � comment out the whole block
  including the environment line and the logging block at the same time.
