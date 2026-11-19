import pytest
from fastapi.testclient import TestClient

from registry.main import app

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["id", "text", "label"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "text": {"type": "string"},
        "label": {"type": "string", "enum": ["positive", "negative"]},
    },
    "additionalProperties": False,
}
RECORDS = [
    {"id": "r1", "text": "works well", "label": "positive"},
    {"id": "r2", "text": "not useful", "label": "negative"},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTRY_DB", str(tmp_path / "registry.sqlite"))
    with TestClient(app) as test_client:
        yield test_client


def make_dataset(client, name="support-v1"):
    response = client.post(
        "/datasets", json={"name": name, "description": "Synthetic support labels"}
    )
    assert response.status_code == 201
    return response.json()


def make_version(client, dataset_id, version="v1", parent=None, records=None):
    return client.post(
        f"/datasets/{dataset_id}/versions",
        json={
            "version": version,
            "parent_version_id": parent,
            "schema": SCHEMA,
            "records": RECORDS if records is None else records,
        },
    )


def test_health_and_dataset_creation(client):
    assert client.get("/health").json() == {"status": "ok"}
    created = make_dataset(client)
    assert created["name"] == "support-v1"
    assert client.get("/datasets").json()[0]["id"] == created["id"]


def test_duplicate_dataset_name_conflicts(client):
    make_dataset(client)
    assert client.post("/datasets", json={"name": "support-v1"}).status_code == 409


def test_version_validates_and_stores_canonical_fingerprint(client):
    dataset = make_dataset(client)
    response = make_version(client, dataset["id"])
    assert response.status_code == 201
    version = response.json()
    assert version["record_count"] == 2
    assert len(version["content_sha256"]) == 64
    assert client.get(f"/versions/{version['id']}").json()["schema"] == SCHEMA


def test_record_order_and_key_order_affect_fingerprint_predictably(client):
    dataset = make_dataset(client)
    first = make_version(client, dataset["id"], records=[RECORDS[0]]).json()
    reordered_keys = {"label": "positive", "id": "r1", "text": "works well"}
    second = make_version(client, dataset["id"], "v2", records=[reordered_keys]).json()
    third = make_version(client, dataset["id"], "v3", records=[RECORDS[1], RECORDS[0]]).json()
    assert first["content_sha256"] == second["content_sha256"]
    assert first["content_sha256"] != third["content_sha256"]


def test_invalid_records_return_bounded_validation_errors(client):
    dataset = make_dataset(client)
    response = make_version(client, dataset["id"], records=[{"id": "x", "label": "unknown"}])
    assert response.status_code == 422
    assert response.json()["detail"]["invalid_records"][0]["record"] == 0


def test_invalid_schema_rejected(client):
    dataset = make_dataset(client)
    response = client.post(
        f"/datasets/{dataset['id']}/versions",
        json={"version": "bad", "schema": {"type": "not-a-type"}, "records": []},
    )
    assert response.status_code == 422


def test_external_schema_references_are_rejected_without_network_fetch(client):
    dataset = make_dataset(client)
    response = client.post(
        f"/datasets/{dataset['id']}/versions",
        json={
            "version": "remote-ref",
            "schema": {"$ref": "https://example.invalid/schema.json"},
            "records": [{}],
        },
    )
    assert response.status_code == 422
    assert "External JSON Schema references" in response.json()["detail"]


def test_parent_lineage_and_paginated_records(client):
    dataset = make_dataset(client)
    first = make_version(client, dataset["id"]).json()
    second = make_version(client, dataset["id"], "v2", first["id"]).json()
    lineage = client.get(f"/versions/{second['id']}/lineage").json()["ancestors"]
    assert [entry["version"] for entry in lineage] == ["v2", "v1"]
    page = client.get(f"/versions/{first['id']}/records?offset=1&limit=1").json()
    assert page["total"] == 2
    assert page["records"][0]["record"]["id"] == "r2"


def test_cross_dataset_parent_is_rejected(client):
    first_dataset = make_dataset(client, "dataset-one")
    second_dataset = make_dataset(client, "dataset-two")
    parent = make_version(client, first_dataset["id"]).json()
    response = make_version(client, second_dataset["id"], "v1", parent["id"])
    assert response.status_code == 422


def test_duplicate_version_name_conflicts_and_version_is_immutable(client):
    dataset = make_dataset(client)
    first = make_version(client, dataset["id"]).json()
    duplicate = make_version(client, dataset["id"])
    assert duplicate.status_code == 409
    saved = client.get(f"/versions/{first['id']}/records").json()
    assert saved["records"][0]["record"] == RECORDS[0]


def test_missing_resources_and_bounds(client):
    assert client.get("/versions/nope").status_code == 404
    assert client.get("/versions/nope/lineage").status_code == 404
    assert client.get("/datasets/nope/versions").status_code == 404
    assert client.get("/versions/nope/records").status_code == 404
    assert client.post("/datasets", json={"name": "bad name"}).status_code == 422
    assert client.get("/datasets?limit=101").status_code == 422
    assert client.get("/versions/x/records?limit=501").status_code == 422
