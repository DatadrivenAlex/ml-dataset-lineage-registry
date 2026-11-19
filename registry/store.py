"""SQLite persistence for immutable dataset versions."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def database_path() -> str:
    return os.environ.get("REGISTRY_DB", "registry.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = database_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions (
                id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES datasets(id),
                version TEXT NOT NULL,
                parent_version_id TEXT REFERENCES versions(id),
                schema_json TEXT NOT NULL,
                record_count INTEGER NOT NULL CHECK(record_count >= 0),
                content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(dataset_id, version)
            );
            CREATE INDEX IF NOT EXISTS versions_dataset_created
                ON versions(dataset_id, created_at, id);
            CREATE TABLE IF NOT EXISTS records (
                version_id TEXT NOT NULL REFERENCES versions(id),
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                record_json TEXT NOT NULL,
                PRIMARY KEY(version_id, ordinal)
            );
            """
        )
