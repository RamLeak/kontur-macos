from __future__ import annotations

import pytest
from backend.app.auth import ensure_owner
from backend.app.models import TaskRevision
from backend.app.source_catalog import import_fixture
from backend.app.source_identity import display_source_id
from backend.tests.test_source_number_order import client as client
from backend.tests.test_source_number_order import pages
from fastapi.testclient import TestClient


def admit(client: TestClient, number: str, line: int, order: int, *, child: bool = False,
          difficulty: int | None = None, unavailable: bool = False) -> dict:
    internal = f"{number}~{line}" if child else number
    payload = {
        "source_id": internal, "source_url": f"https://kompege.ru/task?id={number}",
        "line_number": line, "statement": "TEST_ONLY independent question.", "answer": str(line),
        "rights_status": "private_authorized", "source_metadata": {
            "kege_schema": "kege-current-bank/2.0", "kege_record_id": number,
            "kege_question_number": line, "kege_source_order": order,
        },
    }
    payload["target_exam_year"] = 2027
    if unavailable:
        payload["source_metadata"]["kege_unavailable_resources"] = [{
            "url": "https://example.invalid/required.png", "filename": "required.png",
            "role": "image", "media_type": "image/png", "reason": "origin_not_admitted",
        }]
    if difficulty is not None:
        payload["source_difficulty"] = difficulty
        payload["difficulty"] = {0: "easy", 1: "medium", 2: "hard", 3: "hard"}[difficulty]
    # Snapshot evidence is admitted server-side. The browser fixture schema
    # intentionally ignores arbitrary metadata and cannot assign source ranks.
    with client.app.state.session_factory() as session, session.begin():
        ensure_owner(session, "TEST_ONLY")
        task, revision, _, _ = import_fixture(
            session, payload=payload, owner_id="TEST_ONLY", settings=client.app.state.settings,
            idempotency_key=f"snapshot:{internal}", target_exam_year=2027,
        )
        return {"id": task.id, "revision_id": revision.id}


def test_snapshot_order_and_bookmark_filter_keep_source_sequence(client: TestClient) -> None:
    for number, order in [("2", 2), ("100", 0), ("20", 1)]:
        admit(client, number, 14, order)
    assert pages(client) == ["100", "20", "2"]
    data = client.get("/api/v1/tasks", params={"catalog": "all", "line": 14}).json()
    for item in data["items"]:
        if item["external_id"] in {"100", "2"}:
            saved = client.put(f"/api/v1/bookmarks/{item['id']}",
                               json={"task_revision_id": item["revision"]["id"]})
            assert saved.status_code == 200
    assert pages(client, bookmarked_only=True) == ["100", "2"]


def test_game_questions_share_original_number_but_have_independent_identities(client: TestClient) -> None:
    for line in (19, 20, 21):
        admit(client, "00031516", line, 0, child=line != 19)
    records = client.get("/api/v1/tasks", params={"catalog": "all"}).json()["items"]
    assert len(records) == 3
    assert len({item["id"] for item in records}) == 3
    assert len({item["revision"]["id"] for item in records}) == 3
    for item in records:
        assert item["external_id"] == item["revision"]["source_id"] == "00031516"
        assert item["revision"]["source_url"] == "https://kompege.ru/task?id=00031516"
        assert "answer" not in item["revision"]
    assert {item["line_number"] for item in records} == {19, 20, 21}
    sessions = {}
    for item in records:
        created = client.post("/api/v1/solve-sessions", json={"task_revision_id": item["revision"]["id"]})
        assert created.status_code == 201
        sessions[item["line_number"]] = created.json()["id"]
    assert len(set(sessions.values())) == 3
    saved = client.put(f"/api/v1/solve-sessions/{sessions[19]}/code",
                       json={"content": "print(19)", "version": 0})
    assert saved.status_code == 200
    assert client.get(f"/api/v1/solve-sessions/{sessions[20]}/code").json()["content"] == ""
    assert client.get(f"/api/v1/solve-sessions/{sessions[21]}/code").json()["content"] == ""


def test_source_superscripts_are_not_concatenated_with_the_base(client: TestClient) -> None:
    result = client.post("/api/v1/source/import-fixture", json={
        "source_id": "700001", "source_url": "https://kompege.ru/task?id=700001",
        "line_number": 14, "statement": "<p>2<sup>10</sup> + 10<sup>n+1</sup>, число 101<sub>2</sub>.</p>",
        "answer": "1", "rights_status": "private_authorized",
    }, headers={"Idempotency-Key": "snapshot:power"})
    assert result.status_code == 201, result.text
    data = client.get("/api/v1/tasks", params={"catalog": "all", "line": 14}).json()
    statement = data["items"][0]["revision"]["statement"]
    assert "210" not in statement
    assert "2^(10)" in statement
    assert "10^(n+1)" in statement
    assert "101_(2)" in statement


def test_declared_level_three_is_preserved_in_hard_filter(client: TestClient) -> None:
    admit(client, "700003", 14, 0, difficulty=3)
    data = client.get("/api/v1/tasks", params={"catalog": "all", "line": 14, "difficulty": "hard"}).json()
    assert data["total"] == 1
    assert data["items"][0]["revision"]["source_difficulty"] == 3
    assert data["items"][0]["revision"]["difficulty"] == "hard"


@pytest.mark.parametrize("question", [20.0, "20", True, None])
def test_public_alias_rejects_noninteger_question_number(question: object) -> None:
    revision = TaskRevision(source_id="31516~20", line_number=20,
                            source_url="https://kompege.ru/task?id=31516", source_metadata={
                                "kege_schema": "kege-current-bank/2.0", "kege_record_id": "31516",
                                "kege_question_number": question,
                            })
    assert display_source_id(revision) == "31516~20"


def test_blocked_source_resources_remain_visible_but_cannot_enter_exam(client: TestClient) -> None:
    blocked = admit(client, "710001", 14, 0, unavailable=True)
    catalogue = client.get("/api/v1/tasks", params={"catalog": "all", "line": 14}).json()
    assert catalogue["total"] == 1
    blueprint = client.post("/api/v1/exam-blueprints", json={
        "name": "TEST_ONLY bank readiness", "exam_year": 2027, "required_lines": {"14": 1},
        "constraints": {"selection": {"catalog": "all"}},
    })
    assert blueprint.status_code == 201, blueprint.text
    response = client.post("/api/v1/exams", json={
        "blueprint_id": blueprint.json()["id"], "seed": 3, "mode": "study",
    })
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "EXAM_POOL_TOO_SMALL"

    available = admit(client, "710002", 14, 1)
    response = client.post("/api/v1/exams", json={
        "blueprint_id": blueprint.json()["id"], "seed": 3, "mode": "study",
    })
    assert response.status_code == 201, response.text
    assert response.json()["ordered_revision_ids"] == [available["revision_id"]]
    assert blocked["revision_id"] not in response.json()["ordered_revision_ids"]
