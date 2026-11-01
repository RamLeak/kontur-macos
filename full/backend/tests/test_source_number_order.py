from __future__ import annotations

from pathlib import Path

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(database_url="sqlite+pysqlite:///:memory:", asset_root=tmp_path / "assets",
                        owner_session_secret="TEST_ONLY-number-secret", source_mode="fixture",
                        source_permission_status="private_authorized", testing=True)
    with TestClient(create_app(settings)) as value:
        value.headers["X-Owner-Session"] = issue_session_token("TEST_ONLY", settings.owner_session_secret)
        yield value


def import_task(client: TestClient, number: str) -> None:
    result = client.post("/api/v1/source/import-fixture", json={
        "source_id": number, "source_url": f"https://kompege.ru/task?id={number}",
        "line_number": 14, "statement": "TEST_ONLY arithmetic.", "answer": "1",
        "rights_status": "private_authorized",
    }, headers={"Idempotency-Key": f"number:{number}"})
    assert result.status_code == 201, result.text


def pages(client: TestClient, **filters: object) -> list[str]:
    params = {"line": 14, "catalog": "all", "sort": "number", "limit": 2, **filters}
    numbers = []
    while True:
        result = client.get("/api/v1/tasks", params=params)
        assert result.status_code == 200, result.text
        data = result.json()
        for item in data["items"]:
            assert item["external_id"] == item["revision"]["source_id"]
            numbers.append(item["external_id"])
        if not data["next_cursor"]:
            return numbers
        params["cursor"] = data["next_cursor"]


def test_original_numbers_sort_numerically_without_renumbering_or_cursor_loss(client: TestClient) -> None:
    for number in ["100", "20", "0002", "2", "10", "0", "99999999999999999999999999"]:
        import_task(client, number)
    assert pages(client) == ["0", "0002", "2", "10", "20", "100", "99999999999999999999999999"]


def test_filtered_bookmarks_keep_the_relative_source_number_order(client: TestClient) -> None:
    for number in ["100", "20", "2", "10"]:
        import_task(client, number)
    data = client.get("/api/v1/tasks", params={"catalog": "all", "line": 14}).json()
    for item in data["items"]:
        if item["external_id"] in {"2", "20", "100"}:
            result = client.put(f"/api/v1/bookmarks/{item['id']}", json={"task_revision_id": item["revision"]["id"]})
            assert result.status_code == 200
    assert pages(client, bookmarked_only=True) == ["2", "20", "100"]
