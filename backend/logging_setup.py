"""Centralised logging setup for the EEG analysis backend.

All backend modules should get their logger via:

    from backend.logging_setup import get_logger
    logger = get_logger(__name__)

`configure_logging()` is called once from `server.py` at import time.
It installs a single stream handler that writes to stderr (which is
what uvicorn captures and shows in the terminal), with a format that
includes the module name so the source of each message is obvious:

    2026-07-10 14:23:45 INFO     eeg.parser       S1P002.txt parsed in 812 ms
    2026-07-10 14:23:46 ERROR    eeg.server       /api/upload failed: ...

The verbosity can be tuned with the EEG_LOG_LEVEL environment variable
(DEBUG / INFO / WARNING / ERROR). Default is INFO. Setting it to DEBUG
turns on very chatty output from every module — useful when
troubleshooting a specific subject.
"""
from __future__ import annotations

import logging
import os
import sys

_ROOT = "eeg"
_CONFIGURED = False


def configure_logging() -> None:
    """Install our stream handler on the 'eeg' logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.environ.get("EEG_LOG_LEVEL", "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger(_ROOT)
    root.setLevel(level)
    # Do not propagate up to the root logger; uvicorn owns that
    # and would duplicate our messages otherwise.
    root.propagate = False

    # Only add a handler if none of ours is present yet.
    if not any(getattr(h, "_eeg_handler", False) for h in root.handlers):
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setLevel(level)
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        handler._eeg_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    _CONFIGURED = True


def get_logger(module_name: str) -> logging.Logger:
    """Get a child logger under the 'eeg' namespace.

    Callers pass `__name__`, which typically looks like `backend.parser`;
    we strip the `backend.` prefix so log lines read `eeg.parser`
    instead of `eeg.backend.parser`.
    """
    configure_logging()
    short = module_name
    if short.startswith("backend."):
        short = short[len("backend."):]
    return logging.getLogger(f"{_ROOT}.{short}")
