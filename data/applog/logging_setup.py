"""
applog/logging_setup.py
-----------------------
Structured JSON logger. Every log line goes to stdout as a single JSON
object. Docker captures stdout and the gelf log driver ships it to the
central monitor.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "service":   os.getenv("SERVICE_NAME", "unknown"),
            "version":   os.getenv("SERVICE_VERSION", "unknown"),
            "host":      os.getenv("HOSTNAME", "unknown"),
        }

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        # Attach extra fields passed by the caller via extra={"_x_key": val}
        for key, val in record.__dict__.items():
            if key.startswith("_x_"):
                entry[key[3:]] = val

        return json.dumps(entry)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger
