from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import CURRENT_SCHEMA_VERSION, init_schema, make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.models import ExamSession, TaskRevision
from fastapi.testclient import TestClient
from sqlalchemy import select, text

OWNER_SECRET = "test-owner-secret"
DIFFICULTIES = ("basic", "medium", "hard")


@pytest.fixture()
def variant_client(tmp_path: Path):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret=OWNER_SECRET,
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    with TestClient(create_app(settings)) as client:
        _as_owner(client, "owner-a")
        yield client


def _as_owner(client: TestClient, owner_id: str) -> None:
    client.headers["X-Owner-Session"] = issue_session_token(owner_id, OWNER_SECRET)


def _task_payload(source_id: str, line: int, source_year: int) -> dict[str, object]:
    answer = "1 2" if line in {26, 27} else "42"
    return {
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "source_id": source_id,
        "line_number": line,
        "exam_year": source_year,
        "target_exam_year": 2027,
        "statement": f"Synthetic TEST_ONLY task {source_id}",
        "answer": answer,
        "rights_status": "private_authorized",
        "assets": [
            {
                "url": f"https://kompege.ru/files/{source_id}.txt",
                "filename": f"{source_id}.txt",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(f"input for {source_id}\n".encode()).decode("ascii"),
            }
        ],
    }


def _seed_pool(client: TestClient) -> None:
    """Import 27 numbered TEST_ONLY rows for each known difficulty.

    The fixture importer intentionally does not claim upstream metadata. The
    test then sets the source-year/difficulty fields directly in this synthetic
    database so the variant selector is exercised with the same provenance
    shape as a published catalog.
    """

    imported: list[tuple[str, int, str]] = []
    for line in range(1, 28):
        for difficulty_index, difficulty in enumerate(DIFFICULTIES):
            source_year = 2024 + ((line + difficulty_index) % 4)
            source_id = f"variant-{difficulty}-{line}"
            response = client.post(
                "/api/v1/source/import-fixture",
                json=_task_payload(source_id, line, source_year),
                headers={"Idempotency-Key": source_id},
            )
            assert response.status_code == 201, response.text
            imported.append((response.json()["task"]["revision"]["id"], source_year, difficulty))

    session = client.app.state.session_factory()
    try:
        with session.begin():
            for revision_id, source_year, difficulty in imported:
                revision = session.get(TaskRevision, revision_id)
                assert revision is not None
                revision.source_year = source_year
                revision.source_year_basis = "fixture-test"
                revision.difficulty = "easy" if difficulty == "basic" else difficulty
    finally:
        session.close()


def _create_variant(client: TestClient, difficulty: str = "basic", seed: int = 17) -> dict[str, Any]:
    response = client.post("/api/v1/variants", json={"difficulty": difficulty, "seed": seed})
    assert response.status_code == 201, response.text
    return response.json()


def _assert_error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_variant_requires_all_numbers_assets_and_minimum_target_difficulty(
    variant_client: TestClient, difficulty: str
) -> None:
    _seed_pool(variant_client)

    payload = _create_variant(variant_client, difficulty=difficulty, seed=2027)

    assert payload["difficulty"] == difficulty
    assert payload["lifecycle"] == "active"
    assert payload["sealed"] is True
    assert len(payload["items"]) == 27
    assert {item["line_number"] for item in payload["items"]} == set(range(1, 28))
    assert payload["target_difficulty_count"] >= 22
    assert all(item["answer"] is None for item in payload["items"])
    assert all(item["assets"] and item["assets"][0]["canonical_url"].startswith("https://kompege.ru/")
               for item in payload["items"])


def test_variant_seed_is_deterministic_for_the_same_owner_pool(variant_client: TestClient) -> None:
    _seed_pool(variant_client)

    first = _create_variant(variant_client, difficulty="medium", seed=314159)
    _as_owner(variant_client, "owner-b")
    second = _create_variant(variant_client, difficulty="medium", seed=314159)

    assert second["ordered_revision_ids"] == first["ordered_revision_ids"]
    assert [item["line_number"] for item in second["items"]] == [item["line_number"] for item in first["items"]]


def test_variant_deficit_is_honest_and_rolls_back_blueprint_and_exam(variant_client: TestClient) -> None:
    _seed_pool(variant_client)
    session = variant_client.app.state.session_factory()
    try:
        with session.begin():
            basic_revisions = list(session.scalars(select(TaskRevision).where(TaskRevision.difficulty == "easy")))
            for revision in basic_revisions:
                if revision.line_number > 21:
                    revision.difficulty = "hard"
    finally:
        session.close()

    response = variant_client.post("/api/v1/variants", json={"difficulty": "basic", "seed": 1})

    _assert_error(response, 422, "EXAM_DIFFICULTY_POOL_TOO_SMALL")
    assert variant_client.get("/api/v1/exams").json()["items"] == []
    assert variant_client.get("/api/v1/exam-blueprints").json()["items"] == []


def test_paused_variant_saves_restores_and_freezes_remaining_time(
    variant_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pool(variant_client)
    exam = _create_variant(variant_client)
    item_id = exam["items"][0]["id"]
    saved = variant_client.put(f"/api/v1/exams/{exam['id']}/answers/{item_id}", json={"content": "42"})
    assert saved.status_code == 200, saved.text

    clock = [datetime(2027, 1, 1, 12, 0, 0)]
    session = variant_client.app.state.session_factory()
    try:
        with session.begin():
            persisted = session.get(ExamSession, exam["id"])
            assert persisted is not None
            persisted.started_at = clock[0] - timedelta(seconds=75)
    finally:
        session.close()

    monkeypatch.setattr("backend.app.exams.utcnow", lambda: clock[0])
    monkeypatch.setattr("backend.app.api.utcnow", lambda: clock[0])
    paused = variant_client.post(f"/api/v1/exams/{exam['id']}/pause")
    assert paused.status_code == 200, paused.text
    paused_view = paused.json()
    assert paused_view["lifecycle"] == "paused"
    assert paused_view["deadline_at"] is None
    remaining_at_pause = paused_view["remaining_seconds"]

    paused_answer = variant_client.put(f"/api/v1/exams/{exam['id']}/answers/{item_id}", json={"content": "43"})
    _assert_error(paused_answer, 423, "EXAM_NOT_ACTIVE")

    restored = variant_client.get("/api/v1/exams/active")
    assert restored.status_code == 200
    assert restored.json()["active"]["answers"][item_id]["content"] == "42"

    clock[0] += timedelta(hours=2)
    still_paused = variant_client.get("/api/v1/exams/active")
    assert still_paused.status_code == 200
    assert still_paused.json()["active"]["remaining_seconds"] == remaining_at_pause

    paused_again = variant_client.post(f"/api/v1/exams/{exam['id']}/pause")
    assert paused_again.status_code == 200, paused_again.text
    assert paused_again.json()["paused_at"] == paused_view["paused_at"]
    assert paused_again.json()["remaining_seconds"] == remaining_at_pause

    resumed = variant_client.post(f"/api/v1/exams/{exam['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["lifecycle"] == "active"
    assert resumed.json()["remaining_seconds"] == remaining_at_pause
    assert resumed.json()["answers"][item_id]["content"] == "42"


def test_expired_variant_rejects_answer_changes(variant_client: TestClient) -> None:
    _seed_pool(variant_client)
    exam = _create_variant(variant_client)
    session = variant_client.app.state.session_factory()
    try:
        with session.begin():
            persisted = session.get(ExamSession, exam["id"])
            assert persisted is not None
            persisted.started_at = datetime(2020, 1, 1)
    finally:
        session.close()

    response = variant_client.put(
        f"/api/v1/exams/{exam['id']}/answers/{exam['items'][0]['id']}",
        json={"content": "42"},
    )
    _assert_error(response, 423, "EXAM_TIME_EXPIRED")


def test_paused_sealed_variant_blocks_ai_and_another_server_start(variant_client: TestClient) -> None:
    _seed_pool(variant_client)
    conversation = variant_client.post("/api/v1/conversations", json={"title": "Paused variant"}).json()
    exam = _create_variant(variant_client)
    paused = variant_client.post(f"/api/v1/exams/{exam['id']}/pause")
    assert paused.status_code == 200, paused.text

    another_variant = variant_client.post("/api/v1/variants", json={"difficulty": "hard", "seed": 18})
    _assert_error(another_variant, 423, "EXAM_SEALED")
    context = variant_client.post(
        "/api/v1/assistant/context-preview",
        json={"conversation_id": conversation["id"], "message": "TEST_ONLY hint"},
    )
    _assert_error(context, 423, "EXAM_SEALED")
    assert variant_client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()["items"] == []


def test_submit_uses_29_primary_points_and_partial_26_27_with_immutable_report(variant_client: TestClient) -> None:
    _seed_pool(variant_client)
    exam = _create_variant(variant_client, difficulty="hard", seed=29)

    report_before = variant_client.get(f"/api/v1/exams/{exam['id']}/report")
    _assert_error(report_before, 404, "NOT_FOUND")

    for item in exam["items"]:
        line = item["line_number"]
        content = "42" if line <= 25 else ("1" if line == 26 else "2 1")
        saved = variant_client.put(f"/api/v1/exams/{exam['id']}/answers/{item['id']}", json={"content": content})
        assert saved.status_code == 200, saved.text

    submitted = variant_client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    first = submitted.json()
    report = first["report"]
    assert report["primary_score"] == 27
    assert report["max_primary_score"] == 29
    assert report["test_score"] == 95
    assert report["correct_count"] == 25
    by_line = {item["line_number"]: item for item in report["items"]}
    assert by_line[26]["status"] == "partial"
    assert by_line[26]["points"] == 1
    assert by_line[27]["status"] == "partial"
    assert by_line[27]["points"] == 1

    fetched = variant_client.get(f"/api/v1/exams/{exam['id']}/report")
    assert fetched.status_code == 200
    assert fetched.json() == first
    repeated = variant_client.post(f"/api/v1/exams/{exam['id']}/submit")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first

    changed_after_submit = variant_client.put(
        f"/api/v1/exams/{exam['id']}/answers/{exam['items'][0]['id']}", json={"content": "wrong"}
    )
    _assert_error(changed_after_submit, 423, "EXAM_NOT_ACTIVE")


def test_exam_listing_resume_and_report_are_owner_scoped(variant_client: TestClient) -> None:
    _seed_pool(variant_client)
    owner_exam = _create_variant(variant_client, seed=101)
    submitted = variant_client.post(f"/api/v1/exams/{owner_exam['id']}/submit")
    assert submitted.status_code == 200, submitted.text

    _as_owner(variant_client, "owner-b")
    foreign_exam = _create_variant(variant_client, seed=102)
    listed_b = variant_client.get("/api/v1/exams")
    assert listed_b.status_code == 200
    assert [item["id"] for item in listed_b.json()["items"]] == [foreign_exam["id"]]
    _assert_error(variant_client.get(f"/api/v1/exams/{owner_exam['id']}"), 404, "NOT_FOUND")
    _assert_error(variant_client.post(f"/api/v1/exams/{owner_exam['id']}/resume"), 404, "NOT_FOUND")
    _assert_error(variant_client.get(f"/api/v1/exams/{owner_exam['id']}/report"), 404, "NOT_FOUND")

    _as_owner(variant_client, "owner-a")
    listed_a = variant_client.get("/api/v1/exams")
    assert listed_a.status_code == 200
    assert [item["id"] for item in listed_a.json()["items"]] == [owner_exam["id"]]


def test_schema_v5_to_v6_adds_pause_columns_without_losing_existing_exam(tmp_path: Path) -> None:
    database = tmp_path / "exam-v5.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        init_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM schema_migrations WHERE version > 5"))
            connection.exec_driver_sql("ALTER TABLE exam_sessions DROP COLUMN paused_at")
            connection.exec_driver_sql("ALTER TABLE exam_sessions DROP COLUMN paused_seconds")
            connection.execute(
                text("INSERT INTO users(id, role, created_at) VALUES (:id, 'owner', :created_at)"),
                {"id": "legacy-owner", "created_at": "2026-09-06 00:00:00"},
            )
            connection.execute(
                text(
                    "INSERT INTO exam_blueprints"
                    "(id, owner_id, name, exam_year, required_lines, constraints, version, created_at)"
                    " VALUES (:id, :owner_id, :name, 2027, :required_lines, :constraints, 1, :created_at)"
                ),
                {
                    "id": "legacy-blueprint",
                    "owner_id": "legacy-owner",
                    "name": "Legacy variant",
                    "required_lines": json.dumps({"1": 1}),
                    "constraints": json.dumps({}),
                    "created_at": "2026-09-06 00:00:00",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO exam_sessions"
                    "(id, owner_id, blueprint_id, seed, rng_algorithm, rng_version, scoring_version,"
                    " pool_manifest_hash, ordered_revision_ids, lifecycle, sealed, started_at, completed_at)"
                    " VALUES (:id, :owner_id, :blueprint_id, 7, 'sha256-sort', 'v1', 'score-v1',"
                    " :pool_hash, :ordered_ids, 'active', 1, :started_at, NULL)"
                ),
                {
                    "id": "legacy-exam",
                    "owner_id": "legacy-owner",
                    "blueprint_id": "legacy-blueprint",
                    "pool_hash": "legacy-pool-hash",
                    "ordered_ids": json.dumps([]),
                    "started_at": "2026-09-06 00:00:00",
                },
            )

        init_schema(engine)
        init_schema(engine)
        with engine.connect() as connection:
            version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info('exam_sessions')")}
        assert version == CURRENT_SCHEMA_VERSION
        assert {"paused_at", "paused_seconds"} <= columns

        with make_session_factory(engine)() as session:
            preserved = session.get(ExamSession, "legacy-exam")
            assert preserved is not None
            assert preserved.owner_id == "legacy-owner"
            assert preserved.blueprint_id == "legacy-blueprint"
            assert preserved.lifecycle == "active"
            assert preserved.paused_at is None
            assert preserved.paused_seconds == 0
    finally:
        engine.dispose()
