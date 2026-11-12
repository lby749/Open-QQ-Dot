"""Graceful Shutdown, Task Checkpointing & Resource Cleanup Subsystem.

Coordinates process interruption signals (SIGINT, SIGTERM) to:
- Flush task execution checkpoints.
- Gracefully join background TaskManager workers.
- Release SQLite and file locks.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_shutdown_handlers: list[Callable[[], None]] = []
_shutdown_lock = threading.Lock()
_has_shutdown = False


def register_shutdown_hook(hook_fn: Callable[[], None]):
    """Register a callback executed during graceful shutdown."""
    with _shutdown_lock:
        _shutdown_handlers.append(hook_fn)


def graceful_shutdown(signum=None, frame=None):
    """Execute all registered cleanup handlers and safely flush checkpoints."""
    global _has_shutdown
    with _shutdown_lock:
        if _has_shutdown:
            return
        _has_shutdown = True

    logger.info("Executing graceful shutdown handlers…")
    for fn in _shutdown_handlers:
        try:
            fn()
        except Exception as exc:
            logger.error(f"Error during shutdown handler: {exc}")

    # Stop task manager background worker
    try:
        from task_manager import TaskManager
        TaskManager.get_instance().stop_worker()
        logger.info("TaskManager worker successfully stopped.")
    except Exception:
        pass

    logger.info("Graceful shutdown complete.")


def _on_signal(signum, frame):
    """Clean up, then actually stop the process.

    Only running the cleanup would stop the TaskManager worker while the bridge loop kept chatting and
    telling friends their tasks were picked up; re-raising lets main's KeyboardInterrupt path exit."""
    graceful_shutdown(signum, frame)
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(0)


def install_signal_handlers():
    """Install OS interrupt handlers for SIGINT and SIGTERM."""
    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception as exc:
        logger.warning(f"Unable to install POSIX signal handlers on current runtime: {exc}")


def start_background_services():
    """Start core background task worker and ensure signal handlers and shutdown hooks are installed.

    Decouples background execution from Web server lifecycle so tasks run without Web UI.
    """
    install_signal_handlers()
    import atexit
    atexit.register(graceful_shutdown)
    try:
        from task_manager import TaskManager
        TaskManager.get_instance().start_worker()
        logger.info("Core background TaskManager worker started from app lifecycle.")
    except Exception as exc:
        logger.warning(f"Failed to start core background TaskManager worker: {exc}")

