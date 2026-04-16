"""
Standardized logging module.

All application logs should be emitted through this module.
Logs are written to:
  - Console (stdout)
  - Local rotating file (retained for 30 days)
  - Redis list (key: ``bot:logs``, one JSON entry per record)
"""

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Redis log handler
# ---------------------------------------------------------------------------

class _RedisLogHandler(logging.Handler):
    """Asynchronous-safe handler that pushes log records to a Redis list."""

    LIST_KEY = "bot:logs"
    MAX_ENTRIES = 100_000  # cap the list so Redis memory stays bounded

    def __init__(self, redis_client) -> None:
        """
        Parameters
        ----------
        redis_client:
            A *synchronous* ``redis.Redis`` instance.  Async clients are not
            supported here because ``logging.Handler.emit`` is called from
            synchronous code and cannot ``await`` coroutines.
        """
        super().__init__()
        self._redis = redis_client

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
            if record.exc_info:
                entry["exc_info"] = self.formatException(record.exc_info)

            payload = json.dumps(entry, ensure_ascii=False)
            pipe = self._redis.pipeline()
            pipe.lpush(self.LIST_KEY, payload)
            pipe.ltrim(self.LIST_KEY, 0, self.MAX_ENTRIES - 1)
            pipe.execute()
        except Exception:  # noqa: BLE001
            self.handleError(record)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

_configured_loggers: dict[str, logging.Logger] = {}


def setup_logger(
    name: str,
    *,
    log_dir: Optional[str] = None,
    log_level: Optional[str] = None,
    redis_client=None,
) -> logging.Logger:
    """Create (or retrieve) a named logger with standardised handlers.

    Parameters
    ----------
    name:
        Logger name, usually ``__name__`` of the calling module.
    log_dir:
        Directory for log files.  Falls back to the ``LOG_DIR`` env var
        then to ``"logs"``.
    log_level:
        Minimum level string (e.g. ``"DEBUG"``).  Falls back to
        ``LOG_LEVEL`` env var then to ``"INFO"``.
    redis_client:
        An *already-connected* synchronous ``redis.Redis`` instance.
        When supplied, log records are also pushed to Redis.

    Returns
    -------
    logging.Logger
    """
    if name in _configured_loggers:
        return _configured_loggers[name]

    resolved_dir = log_dir or os.getenv("LOG_DIR", "logs")
    resolved_level_str = log_level or os.getenv("LOG_LEVEL", "INFO")
    resolved_level = getattr(logging, resolved_level_str.upper(), logging.INFO)

    os.makedirs(resolved_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(resolved_level)

    # Prevent propagation to the root logger to avoid duplicate output when
    # multiple loggers are configured.
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- Console handler ---
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(resolved_level)
    logger.addHandler(console_handler)

    # --- Rotating file handler (one file per day, keep 30 days) ---
    log_file = os.path.join(resolved_dir, f"{name}.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(resolved_level)
    logger.addHandler(file_handler)

    # --- Redis handler (optional) ---
    if redis_client is not None:
        redis_handler = _RedisLogHandler(redis_client)
        redis_handler.setFormatter(formatter)
        redis_handler.setLevel(resolved_level)
        logger.addHandler(redis_handler)

    _configured_loggers[name] = logger
    return logger


def get_configured_logger_names() -> list[str]:
    """Return the names of all loggers set up via :func:`setup_logger`."""
    return list(_configured_loggers)


def attach_redis(logger_name: str, redis_client) -> None:
    """Attach a Redis handler to an already-configured logger.

    Useful when the Redis connection is established after the logger has
    been set up (e.g. during bot start-up).  Safe to call multiple times —
    a second handler is not added if one is already attached.
    """
    logger = _configured_loggers.get(logger_name) or logging.getLogger(logger_name)

    # Guard against duplicates when called more than once (e.g. on reconnect).
    for handler in logger.handlers:
        if isinstance(handler, _RedisLogHandler):
            return

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redis_handler = _RedisLogHandler(redis_client)
    redis_handler.setFormatter(formatter)
    logger.addHandler(redis_handler)
