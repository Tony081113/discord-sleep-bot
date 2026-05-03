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
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Redis log handler
# ---------------------------------------------------------------------------


class _AsyncRedisWriter:
    """Background Redis writer to keep logging off the event loop hot path."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=10_000)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="redis-log-writer",
            daemon=True,
        )
        self._thread.start()

    def submit(self, redis_client, list_key: str, max_entries: int, payload: str) -> None:
        self._ensure_started()
        try:
            self._queue.put_nowait((redis_client, list_key, max_entries, payload))
        except queue.Full:
            # Drop oldest item first so fresh logs can still pass through.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._queue.put_nowait((redis_client, list_key, max_entries, payload))
            except queue.Full:
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                redis_client, list_key, max_entries, payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                pipe = redis_client.pipeline()
                pipe.lpush(list_key, payload)
                pipe.ltrim(list_key, 0, max_entries - 1)
                pipe.execute()
            except Exception:
                # Swallow worker errors to avoid recursive logging crashes.
                pass

    def close(self, timeout_seconds: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        started = time.monotonic()
        while thread.is_alive() and (time.monotonic() - started) < timeout_seconds:
            thread.join(timeout=0.1)


_REDIS_WRITER = _AsyncRedisWriter()

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
        self._exc_formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
            if record.exc_info:
                # logging.Handler has no formatException; delegate to Formatter.
                entry["exc_info"] = self._exc_formatter.formatException(record.exc_info)

            payload = json.dumps(entry, ensure_ascii=False)
            _REDIS_WRITER.submit(self._redis, self.LIST_KEY, self.MAX_ENTRIES, payload)
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
