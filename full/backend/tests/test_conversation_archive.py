from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture()
def archive_client(tmp_path: Path):
    """Use only a pytest-owned database; never the repository runtime database."""

    database = tmp_path / "conversation-archive.sqlite"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", settings.owner_session_secret)})
        yield client


def _task_payload(source_id: str) -> dict[str, Any]:
    return {
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "source_id": source_id,
        "line_number": 1,
        "exam_year": 2026,
        "statement": "Find the answer.",
        "answer": "42",
        "rights_status": "private_authorized",
    }


def _make_graph(client: TestClient, source_id: str = "archive-task") -> dict[str, Any]:
    project = client.post("/api/v1/projects", json={"name": "Archive project"})
    assert project.status_code == 201, project.text

    conversation = client.post(
        "/api/v1/conversations",
        json={"project_id": project.json()["id"], "title": "Archive conversation"},
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]

    message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"role": "user", "content": "Keep this message."},
    )
    assert message.status_code == 201, message.text

    imported = client.post(
        "/api/v1/source/import-fixture",
        json=_task_payload(source_id),
        headers={"Idempotency-Key": f"{source_id}-import"},
    )
    assert imported.status_code == 201, imported.text
    revision_id = imported.json()["task"]["revision"]["id"]

    solve = client.post(
        "/api/v1/solve-sessions",
        json={"task_revision_id": revision_id, "conversation_id": conversation_id},
    )
    assert solve.status_code == 201, solve.text
    solve_id = solve.json()["id"]

    code = client.put(
        f"/api/v1/solve-sessions/{solve_id}/code",
        json={"content": "print(42)", "version": 0},
    )
    assert code.status_code == 200, code.text
    board = client.put(
        f"/api/v1/solve-sessions/{solve_id}/board",
        json={"snapshot": {"strokes": [{"tool": "pencil", "points": [[1, 2]]}]}, "version": 0},
    )
    assert board.status_code == 200, board.text

    return {
        "project_id": project.json()["id"],
        "conversation_id": conversation_id,
        "solve_id": solve_id,
    }


def _read_graph(client: TestClient, graph: dict[str, Any]) -> dict[str, Any]:
    conversations = client.get("/api/v1/conversations", params={"project_id": graph["project_id"]})
    assert conversations.status_code == 200, conversations.text
    conversation = next(
        item for item in conversations.json()["items"] if item["id"] == graph["conversation_id"]
    )

    messages = client.get(f"/api/v1/conversations/{graph['conversation_id']}/messages")
    assert messages.status_code == 200, messages.text

    solve = client.get(f"/api/v1/solve-sessions/{graph['solve_id']}")
    assert solve.status_code == 200, solve.text
    code = client.get(f"/api/v1/solve-sessions/{graph['solve_id']}/code")
    assert code.status_code == 200, code.text
    board = client.get(f"/api/v1/solve-sessions/{graph['solve_id']}/board")
    assert board.status_code == 200, board.text

    return {
        "conversation": conversation,
        "messages": messages.json()["items"],
        "solve": solve.json(),
        "code": code.json(),
        "board": board.json(),
    }


def _assert_graph_content_equal(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert after["conversation"]["project_id"] == before["conversation"]["project_id"]
    assert after["conversation"]["title"] == before["conversation"]["title"]
    assert after["messages"] == before["messages"]
    assert after["solve"]["conversation_id"] == before["solve"]["conversation_id"]
    assert after["solve"]["task_revision_id"] == before["solve"]["task_revision_id"]
    assert after["code"]["content"] == before["code"]["content"]
    assert after["code"]["version"] == before["code"]["version"]
    assert after["board"]["snapshot"] == before["board"]["snapshot"]
    assert after["board"]["version"] == before["board"]["version"]


def test_archive_restore_preserves_conversation_graph_and_get_views(archive_client: TestClient) -> None:
    graph = _make_graph(archive_client)
    before = _read_graph(archive_client, graph)
    assert before["conversation"]["archived"] is False

    archived = archive_client.patch(
        f"/api/v1/conversations/{graph['conversation_id']}", json={"archived": True}
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True

    archived_view = _read_graph(archive_client, graph)
    assert archived_view["conversation"]["archived"] is True
    _assert_graph_content_equal(before, archived_view)

    restored = archive_client.patch(
        f"/api/v1/conversations/{graph['conversation_id']}", json={"archived": False}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived"] is False

    restored_view = _read_graph(archive_client, graph)
    assert restored_view["conversation"]["archived"] is False
    _assert_graph_content_equal(before, restored_view)


def test_archive_is_owner_scoped_for_list_and_mutation(archive_client: TestClient) -> None:
    graph = _make_graph(archive_client, source_id="archive-owner-scope")
    archive_client.headers.update(
        {"X-Owner-Session": issue_session_token("other-owner", "test-owner-secret")}
    )

    listed = archive_client.get("/api/v1/conversations", params={"project_id": graph["project_id"]})
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []

    foreign_patch = archive_client.patch(
        f"/api/v1/conversations/{graph['conversation_id']}", json={"archived": True}
    )
    assert foreign_patch.status_code == 404, foreign_patch.text
    assert foreign_patch.json()["code"] == "NOT_FOUND"

    foreign_messages = archive_client.get(
        f"/api/v1/conversations/{graph['conversation_id']}/messages"
    )
    assert foreign_messages.status_code == 404, foreign_messages.text
    assert foreign_messages.json()["code"] == "NOT_FOUND"

    archive_client.headers.update(
        {"X-Owner-Session": issue_session_token("owner", "test-owner-secret")}
    )
    owner_view = archive_client.get("/api/v1/conversations", params={"project_id": graph["project_id"]})
    assert owner_view.status_code == 200, owner_view.text
    assert owner_view.json()["items"][0]["archived"] is False


def test_archive_restore_archive_keeps_transition_events(archive_client: TestClient) -> None:
    graph = _make_graph(archive_client, source_id="archive-events")
    conversation_id = graph["conversation_id"]

    for archived in (True, False, True):
        response = archive_client.patch(
            f"/api/v1/conversations/{conversation_id}", json={"archived": archived}
        )
        assert response.status_code == 200, response.text
        assert response.json()["archived"] is archived

    events_response = archive_client.get("/api/v1/learning-events")
    assert events_response.status_code == 200, events_response.text
    events = [
        event
        for event in events_response.json()["items"]
        if event["aggregate_type"] == "conversation" and event["aggregate_id"] == conversation_id
    ]

    assert [event["event_type"] for event in events] == [
        "conversation_created",
        "conversation_updated",
        "conversation_updated",
        "conversation_updated",
    ]
    assert [event["payload"].get("archived") for event in events[1:]] == [True, False, True]
    assert len({event["idempotency_key"] for event in events}) == 4


def test_archive_noop_returns_view_without_appending_event(archive_client: TestClient) -> None:
    graph = _make_graph(archive_client, source_id="archive-noop")
    conversation_id = graph["conversation_id"]

    initial_noop = archive_client.patch(
        f"/api/v1/conversations/{conversation_id}", json={"archived": False}
    )
    assert initial_noop.status_code == 200, initial_noop.text
    assert initial_noop.json()["archived"] is False

    archived = archive_client.patch(
        f"/api/v1/conversations/{conversation_id}", json={"archived": True}
    )
    assert archived.status_code == 200, archived.text
    repeated_noop = archive_client.patch(
        f"/api/v1/conversations/{conversation_id}", json={"archived": True}
    )
    assert repeated_noop.status_code == 200, repeated_noop.text
    assert repeated_noop.json()["archived"] is True

    events_response = archive_client.get("/api/v1/learning-events")
    assert events_response.status_code == 200, events_response.text
    events = [
        event
        for event in events_response.json()["items"]
        if event["aggregate_type"] == "conversation" and event["aggregate_id"] == conversation_id
    ]
    assert [event["event_type"] for event in events] == [
        "conversation_created",
        "conversation_updated",
    ]
    assert events[-1]["payload"] == {"archived": True}
