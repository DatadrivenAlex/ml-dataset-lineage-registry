"""FastAPI service for immutable, schema-validated ML dataset versions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, Field, field_validator

from registry.store import connect, init_db

app = FastAPI(title="ML Dataset Lineage Registry", version="0.1.0")
MAX_RECORDS = 10_000


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class DatasetCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    description: str = Field(default="", max_length=500)


class VersionCreate(BaseModel):
    version: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    parent_version_id: str | None = None
    record_schema: dict = Field(
        alias="schema", description="JSON Schema Draft 2020-12 for each record"
    )
    records: list[dict] = Field(max_length=MAX_RECORDS)

    @field_validator("records")
    @classmethod
    def finite_json_records(cls, records: list[dict]) -> list[dict]:
        try:
            for record in records:
                canonical(record)
        except (TypeError, ValueError) as exc:
            raise ValueError("Records must contain finite JSON values") from exc
        return records


@app.get("/health")
def health() -> dict[str, str]:
    init_db()
    return {"status": "ok"}


@app.post("/datasets", status_code=201)
def create_dataset(payload: DatasetCreate) -> dict:
    init_db()
    dataset_id = str(uuid4())
    try:
        with connect() as db:
            db.execute(
                "INSERT INTO datasets(id, name, description, created_at) VALUES (?, ?, ?, ?)",
                (dataset_id, payload.name, payload.description, now()),
            )
            row = db.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Dataset name already exists") from exc
    return dict(row)


@app.get("/datasets")
def list_datasets(limit: int = Query(default=50, ge=1, le=100)) -> list[dict]:
    init_db()
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM datasets ORDER BY created_at DESC, id LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/datasets/{dataset_id}/versions", status_code=201)
def create_version(dataset_id: str, payload: VersionCreate) -> dict:
    init_db()
    try:
        Draft202012Validator.check_schema(payload.record_schema)
    except SchemaError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON Schema: {exc.message}") from exc

    def has_remote_reference(node: object) -> bool:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#"):
                return True
            return any(has_remote_reference(value) for value in node.values())
        if isinstance(node, list):
            return any(has_remote_reference(value) for value in node)
        return False

    if has_remote_reference(payload.record_schema):
        raise HTTPException(
            status_code=422, detail="External JSON Schema references are not supported"
        )
    validator = Draft202012Validator(payload.record_schema)
    errors = []
    for index, record in enumerate(payload.records):
        for error in validator.iter_errors(record):
            errors.append(
                {"record": index, "path": list(error.absolute_path), "message": error.message}
            )
            if len(errors) == 20:
                break
        if len(errors) == 20:
            break
    if errors:
        raise HTTPException(status_code=422, detail={"invalid_records": errors})

    content = "\n".join(canonical(record) for record in payload.records)
    digest = hashlib.sha256(
        (canonical(payload.record_schema) + "\n" + content).encode("utf-8")
    ).hexdigest()
    version_id = str(uuid4())
    created_at = now()
    try:
        with connect() as db:
            dataset = db.execute("SELECT id FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
            if dataset is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            if payload.parent_version_id:
                parent = db.execute(
                    "SELECT dataset_id FROM versions WHERE id = ?", (payload.parent_version_id,)
                ).fetchone()
                if parent is None or parent["dataset_id"] != dataset_id:
                    raise HTTPException(
                        status_code=422, detail="Parent version must belong to this dataset"
                    )
            db.execute(
                """INSERT INTO versions
                (id, dataset_id, version, parent_version_id, schema_json, record_count, content_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id,
                    dataset_id,
                    payload.version,
                    payload.parent_version_id,
                    canonical(payload.record_schema),
                    len(payload.records),
                    digest,
                    created_at,
                ),
            )
            db.executemany(
                "INSERT INTO records(version_id, ordinal, record_json) VALUES (?, ?, ?)",
                [
                    (version_id, index, canonical(record))
                    for index, record in enumerate(payload.records)
                ],
            )
            row = db.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="Version name already exists in this dataset"
        ) from exc
    result = dict(row)
    result["schema"] = json.loads(result.pop("schema_json"))
    return result


@app.get("/datasets/{dataset_id}/versions")
def list_versions(dataset_id: str, limit: int = Query(default=50, ge=1, le=100)) -> list[dict]:
    init_db()
    with connect() as db:
        if db.execute("SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        rows = db.execute(
            "SELECT id, dataset_id, version, parent_version_id, record_count, content_sha256, created_at "
            "FROM versions WHERE dataset_id = ? ORDER BY created_at, id LIMIT ?",
            (dataset_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/versions/{version_id}")
def get_version(version_id: str) -> dict:
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    result = dict(row)
    result["schema"] = json.loads(result.pop("schema_json"))
    return result


@app.get("/versions/{version_id}/records")
def get_records(
    version_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    init_db()
    with connect() as db:
        if db.execute("SELECT 1 FROM versions WHERE id = ?", (version_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Version not found")
        rows = db.execute(
            "SELECT ordinal, record_json FROM records WHERE version_id = ? ORDER BY ordinal LIMIT ? OFFSET ?",
            (version_id, limit, offset),
        ).fetchall()
        total = db.execute(
            "SELECT record_count FROM versions WHERE id = ?", (version_id,)
        ).fetchone()[0]
    return {
        "version_id": version_id,
        "offset": offset,
        "limit": limit,
        "total": total,
        "records": [
            {"ordinal": row["ordinal"], "record": json.loads(row["record_json"])} for row in rows
        ],
    }


@app.get("/versions/{version_id}/lineage")
def get_lineage(version_id: str) -> dict:
    init_db()
    chain = []
    seen = set()
    with connect() as db:
        current = version_id
        while current:
            if current in seen:
                raise HTTPException(status_code=500, detail="Lineage cycle detected")
            seen.add(current)
            row = db.execute(
                "SELECT id, dataset_id, version, parent_version_id, record_count, content_sha256, created_at "
                "FROM versions WHERE id = ?",
                (current,),
            ).fetchone()
            if row is None:
                if not chain:
                    raise HTTPException(status_code=404, detail="Version not found")
                raise HTTPException(status_code=500, detail="Lineage parent is missing")
            chain.append(dict(row))
            current = row["parent_version_id"]
    return {"version_id": version_id, "ancestors": chain}
