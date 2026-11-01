"""File-backed SQLite checks for the single-writer exam cutover."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, local

import pytest
from backend.app import api
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import event, text


@pytest.fixture()
def exam_client(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'race.sqlite').as_posix()}",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    with TestClient(create_app(settings)) as client:
        client.headers["X-Owner-Session"] = issue_session_token("owner", "test-owner-secret")
        imported = client.post("/api/v1/source/import-fixture", json={
            "source_url": "https://kompege.ru/task?id=TEST_ONLY-race",
            "source_id": "TEST_ONLY-race", "line_number": 1,
            "statement": "TEST_ONLY isolated concurrency fixture", "answer": "42",
            "rights_status": "private_authorized",
        }, headers={"Idempotency-Key": "TEST_ONLY-race"})
        assert imported.status_code == 201
        blueprint = client.post("/api/v1/exam-blueprints", json={
            "name": "TEST_ONLY concurrency", "required_lines": {"1": 1},
        }).json()
        started = client.post("/api/v1/exams", json={"blueprint_id": blueprint["id"], "seed": 1})
        assert started.status_code == 201
        yield client, started.json()


@pytest.mark.parametrize("action", ["start", "submit", "pause"])
def test_parallel_exam_mutations_have_a_serial_writer_cutover(exam_client, monkeypatch, action):
    client, exam = exam_client
    if action == "start":
        assert client.post(f"/api/v1/exams/{exam['id']}/submit").status_code == 200
    thread_state = local()
    with client.app.state.session_factory() as session:
        engine = session.get_bind()

    def observe_begin(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            thread_state.writer_locked = True

    event.listen(engine, "before_cursor_execute", observe_begin)
    # Check the actual route boundary, not a source-text approximation. A barrier
    # inside the domain lock would deadlock a correct serialized implementation.
    def guarded(original):
        def call(*args, **kwargs):
            assert getattr(thread_state, "writer_locked", False), "domain read occurred before writer lock"
            return original(*args, **kwargs)
        return call

    for name in ("assemble_exam", "save_answer", "submit_exam", "set_exam_paused"):
        monkeypatch.setattr(api, name, guarded(getattr(api, name)))
    barrier = Barrier(2)

    def request(index):
        barrier.wait(timeout=5)
        if action == "start":
            return client.post("/api/v1/exams", json={"blueprint_id": exam["blueprint_id"], "seed": index + 2})
        if index == 0:
            return client.put(f"/api/v1/exams/{exam['id']}/answers/{exam['items'][0]['id']}",
                              json={"content": "42"})
        return client.post(f"/api/v1/exams/{exam['id']}/{action}")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(request, (0, 1)))
        if action == "start":
            assert sorted((first.status_code, second.status_code)) == [201, 423]
            with client.app.state.session_factory() as session:
                count = session.execute(text("SELECT COUNT(*) FROM exam_sessions WHERE owner_id='owner' "
                                             "AND lifecycle IN ('active', 'paused') AND sealed=1")).scalar_one()
            assert count == 1
        else:
            assert second.status_code == 200, second.text
            assert first.status_code in (200, 423), first.text
            if action == "submit":
                submitted = second.json()["report"]["items"][0]["submitted_answer"]
            else:
                answer = second.json()["answers"].get(exam["items"][0]["id"], {})
                submitted = answer.get("content", "")
            assert submitted == ("42" if first.status_code == 200 else "")
    finally:
        event.remove(engine, "before_cursor_execute", observe_begin)


def test_old_reports_are_locked_during_active_and_paused_exam(exam_client):
    client, old = exam_client
    old_report = client.post(f"/api/v1/exams/{old['id']}/submit")
    assert old_report.status_code == 200
    current = client.post("/api/v1/exams", json={"blueprint_id": old["blueprint_id"], "seed": 2}).json()
    for paused in (False, True):
        if paused:
            assert client.post(f"/api/v1/exams/{current['id']}/pause").status_code == 200
        assert client.get(f"/api/v1/exams/{old['id']}/report").status_code == 423
        assert client.post(f"/api/v1/exams/{old['id']}/submit").status_code == 423
    assert client.post(f"/api/v1/exams/{current['id']}/submit").status_code == 200
    assert client.get(f"/api/v1/exams/{old['id']}/report").json() == old_report.json()
