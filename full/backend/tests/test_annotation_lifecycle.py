from __future__ import annotations

from copy import deepcopy

from backend.tests.test_source_number_order import client as client
from backend.tests.test_source_number_order import import_task
from fastapi.testclient import TestClient


def setup_session(client: TestClient):
    import_task(client, "9873")
    task = client.get("/api/v1/tasks", params={"catalog": "all", "line": 14}).json()["items"][0]
    session = client.post("/api/v1/solve-sessions", json={"task_revision_id": task["revision"]["id"]}).json()
    return session["id"], {"kind": "ink",
                           "anchor": {"start": 0, "end": 0, "source_revision_hash": task["revision"]["content_hash"]},
                           "payload": {"color": "#0066cc", "points": [{"x": .1, "y": .2}, {"x": .8, "y": .4}]}}


def test_retry_of_same_stroke_has_one_saved_identity_and_rejects_changed_payload(client: TestClient):
    solve, body = setup_session(client)
    url = f"/api/v1/solve-sessions/{solve}/annotations"
    headers = {"Idempotency-Key": "TEST_ONLY-stroke-1"}
    first = client.post(url, json=body, headers=headers)
    second = client.post(url, json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get(url).json()["items"]) == 1
    changed = deepcopy(body)
    changed["payload"]["color"] = "#d92d20"
    conflict = client.post(url, json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_saved_eraser_is_owner_scoped_persistent_and_idempotent(client: TestClient):
    solve, body = setup_session(client)
    url = f"/api/v1/solve-sessions/{solve}/annotations"
    item = client.post(url, json=body, headers={"Idempotency-Key": "TEST_ONLY-stroke-2"}).json()
    delete_url = f"/api/v1/annotations/{item['id']}"
    ack = client.delete(delete_url)
    assert ack.status_code == 200
    assert ack.json() == {"deleted": True, "id": item["id"]}
    assert client.delete(delete_url).json() == ack.json()
    assert client.get(url).json()["items"] == []
    events = client.get("/api/v1/learning-events").json()["items"]
    assert sum(event["event_type"] == "annotation_deleted" for event in events) == 1
    replay = client.post(url, json=body, headers={"Idempotency-Key": "TEST_ONLY-stroke-2"})
    assert replay.status_code == 409
    assert client.patch(delete_url, json={"payload": body["payload"]}).status_code == 409


def test_strict_lock_is_checked_before_any_annotation_mutation_or_replay(client: TestClient, monkeypatch):
    solve, body = setup_session(client)
    url = f"/api/v1/solve-sessions/{solve}/annotations"
    headers = {"Idempotency-Key": "TEST_ONLY-stroke-3"}
    item = client.post(url, json=body, headers=headers).json()
    monkeypatch.setattr("backend.app.api.has_active_sealed_exam", lambda _session, _owner: True)
    for result in [client.post(url, json=body, headers=headers),
                   client.delete(f"/api/v1/annotations/{item['id']}"),
                   client.patch(f"/api/v1/annotations/{item['id']}", json={"payload": body["payload"]})]:
        assert result.status_code == 423, result.text
        assert result.json()["code"] == "EXAM_SEALED"
    assert len(client.get(url).json()["items"]) == 1
