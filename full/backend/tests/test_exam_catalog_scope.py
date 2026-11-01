from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.models import CatalogEntry, SourcePolicy, SourceSyncJob
from fastapi.testclient import TestClient
from sqlalchemy import select

OWNER_SECRET = "test-owner-secret"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret=OWNER_SECRET,
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    with TestClient(create_app(settings)) as test_client:
        test_client.headers["X-Owner-Session"] = issue_session_token("owner", OWNER_SECRET)
        yield test_client


def _task(source_id: str, statement: str) -> dict[str, object]:
    return {
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "source_id": source_id,
        "line_number": 1,
        "target_exam_year": 2027,
        "statement": statement,
        "answer": "42",
        "rights_status": "private_authorized",
        "assets": [],
    }


def _import(client: TestClient, source_id: str, statement: str, key: str) -> str:
    response = client.post(
        "/api/v1/source/import-fixture",
        json=_task(source_id, statement),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]["revision"]["id"]


def _publish_catalog(client: TestClient, revision_id: str) -> None:
    session = client.app.state.session_factory()
    try:
        with session.begin():
            policy = session.scalar(select(SourcePolicy).where(SourcePolicy.name == "kompege"))
            assert policy is not None
            job = SourceSyncJob(
                owner_id="owner",
                source_policy_id=policy.id,
                mode="recent-catalog",
                request_hash="a" * 64,
                manifest_hash="b" * 64,
                parser_version="test-catalog-v1",
                status="succeeded",
                counters={"published": True},
                idempotency_key="published-catalog",
            )
            session.add(job)
            session.flush()
            session.add(CatalogEntry(job_id=job.id, revision_id=revision_id))
    finally:
        session.close()


def _blueprint(
    client: TestClient,
    *,
    count: int,
    selection: dict[str, Any] | None = None,
) -> str:
    response = client.post(
        "/api/v1/exam-blueprints",
        json={
            "name": "Catalog scope test",
            "exam_year": 2027,
            "required_lines": {"1": count},
            "constraints": {"selection": selection} if selection is not None else {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_exam(client: TestClient, blueprint_id: str, seed: int, *, mode: str | None = None) -> dict[str, Any]:
    payload: dict[str, object] = {"blueprint_id": blueprint_id, "seed": seed}
    if mode is not None:
        payload["mode"] = mode
    response = client.post(
        "/api/v1/exams",
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_catalog_scope(client: TestClient) -> tuple[str, str, str]:
    published_revision = _import(client, "scope-a", "Published revision", "scope-a-first")
    current_revision = _import(client, "scope-a", "Current local revision", "scope-a-second")
    other_current_revision = _import(client, "scope-b", "Another current local revision", "scope-b-first")
    _publish_catalog(client, published_revision)
    return published_revision, current_revision, other_current_revision


def test_default_exam_scope_uses_published_catalog(client: TestClient) -> None:
    published_revision, _current_revision, _other_current_revision = _seed_catalog_scope(client)

    exam = _create_exam(client, _blueprint(client, count=1), seed=1)

    assert exam["ordered_revision_ids"] == [published_revision]
    assert exam["items"][0]["statement"] == "Published revision"


def test_all_scope_uses_current_revisions_and_include_pins_history(client: TestClient) -> None:
    published_revision, current_revision, other_current_revision = _seed_catalog_scope(client)

    all_exam = _create_exam(
        client,
        _blueprint(client, count=2, selection={"catalog": "all"}),
        seed=2,
        mode="study",
    )
    assert set(all_exam["ordered_revision_ids"]) == {current_revision, other_current_revision}

    history_exam = _create_exam(
        client,
        _blueprint(
            client,
            count=1,
            selection={"catalog": "all", "include_revision_ids": [published_revision]},
        ),
        seed=3,
        mode="study",
    )
    assert history_exam["ordered_revision_ids"] == [published_revision]


@pytest.mark.parametrize("catalog", ["recent", [], {}, None, True])
def test_invalid_exam_catalog_scope_is_rejected(client: TestClient, catalog: object) -> None:
    _seed_catalog_scope(client)
    blueprint_id = _blueprint(client, count=1, selection={"catalog": catalog})

    response = client.post(
        "/api/v1/exams",
        json={"blueprint_id": blueprint_id, "seed": 4, "mode": "study"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "BLUEPRINT_INVALID"
