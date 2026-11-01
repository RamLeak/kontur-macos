from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import SchemaVersionError, init_schema, make_engine
from backend.app.main import create_app
from backend.app.models import ExamBlueprint, ExamSession, User
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture()
def clients(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'TEST_ONLY.sqlite').as_posix()}",
        asset_root=tmp_path / "assets",
        owner_session_secret="TEST_ONLY-sync-secret",
        source_mode="fixture",
        source_permission_status="private_authorized",
        testing=True,
    )
    app = create_app(settings)
    with TestClient(app) as first:
        second = TestClient(app)
        try:
            for client in (first, second):
                client.headers["X-Owner-Session"] = issue_session_token("owner", settings.owner_session_secret)
            yield first, second
        finally:
            second.close()


def context(client: TestClient, source_id: str = "TEST_ONLY-sync-task") -> dict:
    imported = client.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": f"https://kompege.ru/task?id={source_id}",
            "source_id": source_id,
            "line_number": 1,
            "statement": "TEST_ONLY synthetic sync task",
            "answer": "42",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": source_id},
    )
    assert imported.status_code == 201, imported.text
    task = imported.json()["task"]
    revision = task["revision"]
    conversation = client.post("/api/v1/conversations", json={"title": "TEST_ONLY sync"})
    assert conversation.status_code == 201, conversation.text
    solve = client.post(
        "/api/v1/solve-sessions",
        json={"task_revision_id": revision["id"], "conversation_id": conversation.json()["id"]},
    )
    assert solve.status_code == 201, solve.text
    return {
        "kind": "practice_answer",
        "solve_session_id": solve.json()["id"],
        "task_id": task["id"],
        "task_revision_id": revision["id"],
        "content_hash": revision["content_hash"],
    }


def write(client: TestClient, bound: dict, value: dict, base: int, key: str):
    return client.put(
        "/api/v1/sync/records", json={**bound, "value": value, "base_version": base, "idempotency_key": key}
    )


def test_two_clients_conflict_retry_revision_and_owner(clients) -> None:
    first, second = clients
    bound = context(first)
    assert second.get("/api/v1/sync/records", params=bound).json()["version"] == 0
    saved = write(first, bound, {"answer": "local A"}, 0, "save-a")
    assert saved.status_code == 200, saved.text
    assert second.get("/api/v1/sync/records", params=bound).json() == saved.json()
    conflict = write(second, bound, {"answer": "local B"}, 0, "save-b")
    assert conflict.status_code == 409
    assert conflict.json()["details"]["current"]["value"] == {"answer": "local A"}
    updated = write(second, bound, {"answer": "local B"}, 1, "save-b")
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert write(first, bound, {"answer": "local A"}, 0, "save-a").json() == saved.json()
    mismatch = write(first, bound, {"answer": "changed retry"}, 0, "save-a")
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"
    other = context(first, "TEST_ONLY-other-task")
    assert write(first, other, {"answer": "local A"}, 0, "save-a").json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert write(first, {**bound, "content_hash": "0" * 64}, {"answer": "bad"}, 2, "bad-hash").status_code == 422
    assert (
        write(
            first, {**bound, "task_revision_id": other["task_revision_id"]}, {"answer": "bad"}, 2, "bad-revision"
        ).status_code
        == 422
    )
    second.headers["X-Owner-Session"] = issue_session_token("foreign", "TEST_ONLY-sync-secret")
    assert second.get("/api/v1/sync/records", params=bound).status_code == 404
    assert write(second, bound, {"answer": "foreign"}, 2, "foreign").status_code == 404
    assert second.get("/api/v1/sync/changes").json()["changes"] == []
    # Saving an unsubmitted answer must never create grading evidence.
    progress = first.get("/api/v1/task-progress", params={"revision_ids": bound["task_revision_id"]})
    assert progress.status_code == 200, progress.text
    with first.app.state.session_factory() as db:
        assert db.execute(text("SELECT COUNT(*) FROM attempts")).scalar() == 0
        assert db.execute(text("SELECT COUNT(*) FROM learning_events WHERE event_type LIKE '%graded%'")).scalar() == 0


def test_shared_whitelist_changes_auth_and_old_api_compat(clients) -> None:
    first, second = clients
    bound = {"kind": "preferences"}
    saved = write(first, bound, {"theme": "dark", "editorWrap": True}, 0, "prefs")
    assert saved.status_code == 200, saved.text
    assert second.get("/api/v1/sync/records", params=bound).json() == saved.json()
    assert write(first, bound, {"fileCacheMb": 1024}, 1, "cache").status_code == 422
    assert write(first, bound, {"owner_id": "foreign"}, 1, "owner").status_code == 422
    assert write(first, {**bound, "owner_id": "foreign"}, {}, 1, "authority").status_code == 422
    filters = write(first, {"kind": "catalog_filters"}, {"unsolvedOnly": True, "taskType": 14}, 0, "filters")
    assert filters.status_code == 200
    before = second.get("/api/v1/sync/changes").json()
    assert "sync_records" in {row["topic"] for row in before["changes"]}
    task = context(first)
    bookmark = first.put(f"/api/v1/bookmarks/{task['task_id']}", json={"task_revision_id": task["task_revision_id"]})
    assert bookmark.status_code == 200
    feed = second.get("/api/v1/sync/changes", params={"after": before["cursor"], "limit": 1}).json()
    assert len(feed["changes"]) == 1 and feed["has_more"] is True
    rest = second.get("/api/v1/sync/changes", params={"after": feed["cursor"]}).json()
    assert "bookmarks" in {row["topic"] for row in rest["changes"]}
    assert second.get("/api/v1/bookmarks").json() == [bookmark.json()]
    assert all(set(row) == {"revision", "topic"} for row in rest["changes"])
    second.headers.pop("X-Owner-Session")
    assert second.get("/api/v1/sync/changes").status_code == 401
    assert write(second, bound, {"theme": "light"}, 1, "unauthorized").status_code == 401


def test_simultaneous_base_version_has_one_winner(clients) -> None:
    first, second = clients
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write, client, {"kind": "preferences"}, {"theme": theme}, 0, theme)
            for client, theme in ((first, "dark"), (second, "light"))
        ]
        results = [future.result() for future in futures]
    assert sorted(result.status_code for result in results) == [200, 409]
    assert first.get("/api/v1/sync/records", params={"kind": "preferences"}).json()["version"] == 1


def test_composer_context_and_sealed_exam_lock(clients) -> None:
    first, second = clients
    bound = context(first)
    solve = first.get(f"/api/v1/solve-sessions/{bound['solve_session_id']}").json()
    composer = {**bound, "kind": "composer", "conversation_id": solve["conversation_id"]}
    saved = write(first, composer, {"text": "unsent TEST_ONLY"}, 0, "composer")
    assert saved.status_code == 200, saved.text
    assert second.get("/api/v1/sync/records", params=composer).json() == saved.json()
    assert write(first, {**composer, "conversation_id": "missing"}, {"text": "bad"}, 0, "bad-conv").status_code == 404
    with first.app.state.session_factory() as db:
        db.add(User(id="exam-owner"))
        blueprint = ExamBlueprint(owner_id="owner", name="TEST_ONLY", version=1, required_lines={}, constraints={})
        db.add(blueprint)
        db.flush()
        db.add(
            ExamSession(
                owner_id="owner",
                blueprint_id=blueprint.id,
                seed=1,
                pool_manifest_hash="0" * 64,
                ordered_revision_ids=[],
                lifecycle="active",
                sealed=True,
            )
        )
        db.commit()
    assert second.get("/api/v1/sync/records", params=composer).status_code == 403
    assert write(first, bound, {"answer": "42"}, 0, "exam-bypass").status_code == 403
    assert write(first, {"kind": "preferences"}, {"theme": "light"}, 0, "exam-prefs").status_code == 200


def test_v12_upgrade_history_and_transactional_notifications(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+pysqlite:///{(tmp_path / 'TEST_ONLY-migration.sqlite').as_posix()}")
    init_schema(engine)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id,role,created_at) VALUES ('history','owner',CURRENT_TIMESTAMP)"))
        db.execute(
            text(
                "INSERT INTO learning_events(event_id,owner_id,aggregate_type,aggregate_id,event_type,occurred_at,"
                "recorded_at,schema_version,policy_version,idempotency_key,payload,payload_hash) VALUES "
                "('TEST_ONLY-history','history','test','test','TEST_ONLY_preserved',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,"
                "'v1','1','TEST_ONLY-history','{}','TEST_ONLY-hash')"
            )
        )
        db.execute(text("DELETE FROM schema_migrations WHERE version=13"))
        for table in (
            "owner_sync_records",
            "solve_sessions",
            "attempts",
            "code_documents",
            "board_documents",
            "annotations",
            "bookmarks",
            "projects",
            "conversations",
            "messages",
            "exam_sessions",
            "exam_answers",
            "exam_workspaces",
            "learning_events",
        ):
            for operation in ("insert", "update", "delete"):
                db.exec_driver_sql(f"DROP TRIGGER sync_{table}_{operation}")
        for table in ("owner_sync_changes", "owner_sync_writes", "owner_sync_records"):
            db.exec_driver_sql(f"DROP TABLE {table}")
    init_schema(engine)
    init_schema(engine)
    with engine.connect() as db:
        assert db.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar() == 13
        assert db.execute(text("SELECT COUNT(*) FROM users WHERE id='history'")).scalar() == 1
        assert db.execute(text("SELECT event_type FROM learning_events")).scalar() == "TEST_ONLY_preserved"
        db.execute(
            text(
                "INSERT INTO projects(id,owner_id,name,pinned,archived,order_index,created_at,updated_at) "
                "VALUES ('rollback','history','TEST_ONLY',0,0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        assert db.execute(text("SELECT COUNT(*) FROM owner_sync_changes")).scalar() == 1
        db.rollback()
        assert db.execute(text("SELECT COUNT(*) FROM owner_sync_changes")).scalar() == 0
    with engine.begin() as db:
        db.execute(text("INSERT INTO schema_migrations(version,applied_at) VALUES (99,CURRENT_TIMESTAMP)"))
    with pytest.raises(SchemaVersionError):
        init_schema(engine)


def test_invalidation_retention_is_bounded_and_requests_full_refresh(clients) -> None:
    first, second = clients
    assert write(first, {"kind": "preferences"}, {"theme": "dark"}, 0, "retention").status_code == 200
    baseline = second.get("/api/v1/sync/changes").json()["cursor"]
    with first.app.state.engine.begin() as db:
        db.execute(
            text("INSERT INTO owner_sync_changes(owner_id,topic) VALUES ('owner','progress')"),
            [{} for _ in range(1005)],
        )
        assert db.execute(text("SELECT COUNT(*) FROM owner_sync_changes WHERE owner_id='owner'")).scalar() == 1000
    response = second.get("/api/v1/sync/changes", params={"after": baseline, "limit": 200})
    assert response.status_code == 200
    assert response.json()["reset_required"] is True
    assert response.json()["has_more"] is True
    assert len(response.json()["changes"]) == 200
    future = second.get("/api/v1/sync/changes", params={"after": response.json()["latest_cursor"] + 10}).json()
    assert future["reset_required"] is True
    assert future["cursor"] == future["latest_cursor"]


def test_owner_expectation_rejects_cross_tab_account_switch_without_selecting_owner(clients) -> None:
    first, second = clients
    first.headers["X-Sync-Owner"] = "owner"
    assert write(first, {"kind": "preferences"}, {"theme": "dark"}, 0, "owner-bound").status_code == 200
    second.headers["X-Sync-Owner"] = "owner"
    import time

    second.headers.pop("X-Owner-Session")
    second.cookies.set(
        "ege_owner_session",
        issue_session_token(
            "foreign",
            "TEST_ONLY-sync-secret",
            expires_at=int(time.time()) + 1800,
        ),
    )
    response = write(second, {"kind": "preferences"}, {"theme": "light"}, 0, "stale-write")
    assert response.status_code == 401 and response.json()["code"] == "OWNER_SESSION_CHANGED"
    assert second.get("/api/v1/sync/records", params={"kind": "preferences"}).status_code == 401
    assert second.get("/api/v1/sync/changes").status_code == 401
    second.headers.pop("X-Sync-Owner")
    assert second.get("/api/v1/sync/records", params={"kind": "preferences"}).json()["version"] == 0
    assert first.get("/api/v1/sync/records", params={"kind": "preferences"}).json()["value"] == {"theme": "dark"}


def test_successful_retry_window_is_bounded_expired_stale_cannot_write_and_foreign_rows_survive(clients) -> None:
    from datetime import timedelta

    from backend.app.models import OwnerSyncWrite, utcnow

    first, second = clients
    saved = write(first, {"kind": "preferences"}, {"theme": "dark"}, 0, "expiring")
    assert saved.status_code == 200
    with first.app.state.session_factory() as db:
        db.execute(text("UPDATE owner_sync_writes SET created_at='2000-01-01' WHERE idempotency_key='expiring'"))
        db.add(User(id="retention-foreign"))
        db.flush()
        db.add(
            OwnerSyncWrite(
                owner_id="retention-foreign",
                idempotency_key="foreign",
                request_hash="foreign",
                response={"foreign": True},
                created_at=utcnow() - timedelta(days=8),
            )
        )
        db.add_all(
            [
                OwnerSyncWrite(
                    owner_id="owner",
                    idempotency_key=f"seed-{index}",
                    request_hash="TEST_ONLY",
                    response={"TEST_ONLY": index},
                    created_at=utcnow(),
                )
                for index in range(4097)
            ]
        )
        db.commit()
    # Expiry is checked before replay: original base0 is now stale, so no second mutation.
    expired = write(second, {"kind": "preferences"}, {"theme": "dark"}, 0, "expiring")
    assert expired.status_code == 409 and expired.json()["code"] == "VERSION_CONFLICT"
    current = write(second, {"kind": "preferences"}, {"theme": "light"}, 1, "retained-live")
    assert current.status_code == 200
    assert write(first, {"kind": "preferences"}, {"theme": "light"}, 1, "retained-live").json() == current.json()
    with first.app.state.session_factory() as db:
        assert db.execute(text("SELECT COUNT(*) FROM owner_sync_writes WHERE owner_id='owner'")).scalar() == 4096
        assert (
            db.execute(text("SELECT COUNT(*) FROM owner_sync_writes WHERE owner_id='retention-foreign'")).scalar() == 1
        )
        assert db.execute(text("SELECT COUNT(*) FROM owner_sync_records WHERE owner_id='owner'")).scalar() == 1


def test_change_cursor_rejects_integer_overflow_and_negative_values(clients) -> None:
    first, _ = clients
    for cursor in (-1, 2**53, 2**63, 2**100):
        assert first.get("/api/v1/sync/changes", params={"after": cursor}).status_code == 422
    maximum = first.get("/api/v1/sync/changes", params={"after": 2**53 - 1})
    assert maximum.status_code == 200 and maximum.json()["reset_required"] is True


def test_interrupted_v13_ddl_is_atomic_and_retry_preserves_old_history(tmp_path: Path, monkeypatch) -> None:
    from backend.app import sync_records

    engine = make_engine(f"sqlite+pysqlite:///{(tmp_path / 'TEST_ONLY-ddl-rollback.sqlite').as_posix()}")
    init_schema(engine)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id,role,created_at) VALUES ('old-owner','owner',CURRENT_TIMESTAMP)"))
        db.execute(text("DELETE FROM schema_migrations WHERE version=13"))
        triggers = db.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'sync_%'"
        ).all()
        for row in triggers:
            db.exec_driver_sql(f'DROP TRIGGER "{row[0]}"')
        for table in ("owner_sync_changes", "owner_sync_writes", "owner_sync_records"):
            db.exec_driver_sql(f"DROP TABLE {table}")
    original = sync_records.install_change_triggers

    def interrupted(connection):
        original(connection)
        raise RuntimeError("TEST_ONLY fault after all DDL before marker13")

    monkeypatch.setattr(sync_records, "install_change_triggers", interrupted)
    with pytest.raises(RuntimeError, match="TEST_ONLY fault"):
        init_schema(engine)
    with engine.connect() as db:
        assert db.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar() == 12
        assert (
            db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'owner_sync_%'").all()
            == []
        )
        assert (
            db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'sync_%'").all() == []
        )
        assert db.execute(text("SELECT COUNT(*) FROM users WHERE id='old-owner'")).scalar() == 1
    monkeypatch.setattr(sync_records, "install_change_triggers", original)
    init_schema(engine)
    init_schema(engine)
    with engine.connect() as db:
        assert db.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar() == 13
        assert db.execute(text("SELECT COUNT(*) FROM users WHERE id='old-owner'")).scalar() == 1


def test_draft_only_work_is_resumable_without_grading_or_cross_revision_transfer(clients) -> None:
    first, second = clients
    bound = context(first, "TEST_ONLY-resume-draft")

    def progress(client, revision_id):
        response = client.get("/api/v1/task-progress", params={"revision_ids": revision_id})
        assert response.status_code == 200, response.text
        return response.json()[0]

    assert progress(first, bound["task_revision_id"])["status"] == "neutral"
    assert write(first, bound, {"answer": "42"}, 0, "draft-start").status_code == 200
    started = progress(second, bound["task_revision_id"])
    assert started["status"] == "started" and started["solve_session_id"] == bound["solve_session_id"]
    assert write(first, bound, {"answer": "   "}, 1, "draft-clear").status_code == 200
    assert progress(second, bound["task_revision_id"])["status"] == "neutral"
    solve = first.get(f"/api/v1/solve-sessions/{bound['solve_session_id']}").json()
    composer = {**bound, "kind": "composer", "conversation_id": solve["conversation_id"]}
    assert write(first, composer, {"text": "unsent work"}, 0, "composer-resume").status_code == 200
    assert progress(second, bound["task_revision_id"])["solve_session_id"] == bound["solve_session_id"]
    assert write(first, composer, {"text": ""}, 1, "composer-clear").status_code == 200
    assert progress(second, bound["task_revision_id"])["status"] == "neutral"
    second.headers["X-Owner-Session"] = issue_session_token("foreign", "TEST_ONLY-sync-secret")
    assert progress(second, bound["task_revision_id"])["status"] == "neutral"
    assert progress(second, bound["task_revision_id"])["solve_session_id"] is None
    checked = first.post(
        f"/api/v1/solve-sessions/{bound['solve_session_id']}/check-answer",
        json={"answer": "42"},
        headers={"Idempotency-Key": "TEST_ONLY-verified-grade"},
    )
    assert checked.status_code == 200 and checked.json()["correct"] is True
    assert write(first, bound, {"answer": "wrong unsubmitted draft"}, 2, "draft-after-grade").status_code == 200
    assert progress(first, bound["task_revision_id"])["status"] == "correct"
    replaced = first.post(
        "/api/v1/source/import-fixture",
        json={
            "source_url": "https://kompege.ru/task?id=TEST_ONLY-resume-draft",
            "source_id": "TEST_ONLY-resume-draft",
            "line_number": 1,
            "statement": "TEST_ONLY replaced revision",
            "answer": "42",
            "rights_status": "private_authorized",
        },
        headers={"Idempotency-Key": "TEST_ONLY-replacement"},
    )
    assert replaced.status_code == 201, replaced.text
    revision2 = replaced.json()["task"]["revision"]["id"]
    assert revision2 != bound["task_revision_id"]
    assert progress(first, revision2)["status"] == "neutral"
    assert progress(first, revision2)["solve_session_id"] is None
    assert (
        write(
            first, {**bound, "task_revision_id": revision2}, {"answer": "bad binding"}, 3, "wrong-revision-resume"
        ).status_code
        == 422
    )
