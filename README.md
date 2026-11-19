# ML Dataset Lineage Registry

A small but production-minded backend demo for versioning ML datasets as validated, immutable snapshots. It gives a team a stable record of which schema and records were used for each training or evaluation run, and a parent chain that makes dataset changes traceable. The included sample is synthetic; no customer data or hosted model is involved.

## Why this exists

ML experiments are hard to reproduce when a dataset is silently edited between runs. This service stores each submitted snapshot with a schema, record count, timestamp and SHA-256 fingerprint. A new version may point to its parent, producing a lineage chain without overwriting earlier snapshots.

## What it demonstrates

- FastAPI endpoints for dataset creation, immutable version registration, manifests, paginated record reads and parent lineage.
- JSON Schema Draft 2020-12 validation before a version is committed; validation errors identify a record index and field path.
- Canonical JSON serialization makes fingerprints stable when object keys are reordered; schema and ordered record content are both included in the digest.
- SQLite transactions, foreign keys, unique version names and bounded request sizes protect the registry's core invariants.
- Pytest coverage for validation, immutability, conflicts, parent scope, lineage, pagination and deterministic fingerprints.

## Run locally

Python 3.11+ is required.

```bash
uv sync --group dev
uv run uvicorn registry.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the API. The default database is `registry.db`; set `REGISTRY_DB` to use another file.

Create a dataset:

```bash
curl -X POST http://127.0.0.1:8000/datasets \
  -H 'Content-Type: application/json' \
  -d '{"name":"support-intents","description":"Synthetic support intent labels"}'
```

Then register a version using the returned dataset ID:

```bash
curl -X POST http://127.0.0.1:8000/datasets/DATASET_ID/versions \
  -H 'Content-Type: application/json' \
  -d '{"version":"v1","schema":{"type":"object","required":["id","label"],"properties":{"id":{"type":"string"},"label":{"type":"string"}},"additionalProperties":false},"records":[{"id":"sample-1","label":"billing"}]}'
```

The `examples/` directory contains a fuller schema and two synthetic records. Register another version with the first version's `id` as `parent_version_id`, then inspect `/versions/VERSION_ID/lineage`.

## API overview

- `POST /datasets` and `GET /datasets`
- `POST /datasets/{dataset_id}/versions` and `GET /datasets/{dataset_id}/versions`
- `GET /versions/{version_id}`
- `GET /versions/{version_id}/records?offset=0&limit=100`
- `GET /versions/{version_id}/lineage`

## Important limits

This is a portfolio-scale reference implementation, not a production data lake: SQLite is single-node, records are JSON rows kept in the database, and there is no authentication, tenant isolation, object-store integration, encryption, deletion/retention policy, migrations or concurrent-writer load test. Do not submit confidential data. A fingerprint identifies exact submitted bytes after canonicalization; it does not establish data quality, provenance truth or model performance. Schema validation does not inspect external references or fetch remote schemas.
