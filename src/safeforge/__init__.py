"""safeforge -- reference implementation and test bench for crash-consistent
file + SQLite ingest, ZIP upload validation and idempotent event handling.

Standard library only. See README.md for scope and claim limits.
"""

from .db import ConfigurationError, connect, transaction
from .durable import CrossDeviceError, fsync_dir, fsync_file, publish, require_same_volume
from .events import EventResult, PayloadMismatch, ensure_schema, process_event
from .store import (
    DigestMismatch,
    IdempotencyConflict,
    IngestAborted,
    IngestError,
    IngestInFlight,
    IngestResult,
    LayoutNotDurable,
    ObjectStore,
    ReconcileReport,
    TooLarge,
)
from .zipcheck import ZipEntry, ZipPolicy, ZipRejected, extract_zip, validate_zip

__version__ = "0.1.0"

__all__ = [
    "ConfigurationError",
    "CrossDeviceError",
    "DigestMismatch",
    "EventResult",
    "IdempotencyConflict",
    "IngestAborted",
    "IngestError",
    "IngestInFlight",
    "IngestResult",
    "LayoutNotDurable",
    "ObjectStore",
    "PayloadMismatch",
    "ReconcileReport",
    "TooLarge",
    "ZipEntry",
    "ZipPolicy",
    "ZipRejected",
    "connect",
    "ensure_schema",
    "extract_zip",
    "fsync_dir",
    "fsync_file",
    "process_event",
    "publish",
    "require_same_volume",
    "transaction",
    "validate_zip",
]
