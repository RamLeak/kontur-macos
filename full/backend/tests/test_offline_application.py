"""Original API + real schema, synthetic catalog, no HTTP listener or model."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import threading
import time

import pytest
from fastapi import BackgroundTasks

from offline_protocol import BridgeError


def decoded(response):
    return json.loads(base64.b64decode(response["bodyBase64"]))


def test_independent_catalog_startup_and_private_original_api(seed_factory, offline_factory):
    from app.models import Task

    bundle, _receipt = seed_factory()
    application = offline_factory(bundle)
    assert application.request("GET", "/api/v1/auth/session")["status"] == 200
    capabilities = decoded(application.request("GET", "/api/v1/capabilities"))
    assert capabilities["auth"] == {"available": True, "mode": "local-process"}
    catalog = decoded(application.request("GET", "/api/v1/tasks?limit=100"))
    assert catalog["total"] == 2
    assert {row["revision"]["id"] for row in catalog["items"]} == {"r-1", "r-2"}
    asset = application.request("GET", "/api/v1/assets/a-1/download")
    assert asset["status"] == 200
    assert base64.b64decode(asset["bodyBase64"]) == b"1 2 3\n"
    with application.app.state.session_factory() as session:
        assert session.get(Task, "t-1").updated_at.isoformat() == "2026-10-29T00:00:00"
    assert application.flush() == {"committed": True, "pendingMutations": 0}


def test_missing_and_corrupt_bank_fail_startup(seed_factory, offline_factory, tmp_path):
    empty = tmp_path / "empty-bundle"
    empty.mkdir()
    with pytest.raises(BridgeError, match="каталог") as missing:
        offline_factory(empty)
    assert missing.value.code == "catalog_missing"
    bundle, _receipt = seed_factory()
    seed = bundle / "catalog" / "catalog.sqlite3"
    seed.write_bytes(b"not a SQLite catalog")
    with pytest.raises(BridgeError) as corrupt:
        offline_factory(bundle)
    assert corrupt.value.code == "catalog_invalid"


def test_catalog_cold_warm_1001_tasks(seed_factory, offline_factory, tmp_path, record_property):
    bundle, _receipt = seed_factory(task_count=1001)
    profile = tmp_path / "large-profile"
    started = time.perf_counter()
    cold = offline_factory(bundle, profile)
    cold_seconds = time.perf_counter() - started
    first = decoded(cold.request("GET", "/api/v1/tasks?limit=100"))
    assert first["total"] == 1002 and len(first["items"]) == 100
    assert first["next_cursor"]
    assert cold.flush()["committed"] is True
    cold.close()
    started = time.perf_counter()
    warm = offline_factory(bundle, profile)
    warm_seconds = time.perf_counter() - started
    second = decoded(warm.request("GET", "/api/v1/tasks?limit=100"))
    assert second == first
    assert warm.flush() == {"committed": True, "pendingMutations": 0}
    record_property("synthetic_tasks", 1001)
    record_property("cold_start_seconds", round(cold_seconds, 4))
    record_property("warm_start_seconds", round(warm_seconds, 4))


def test_flush_waits_for_response_background_database_write(seed_factory, offline_factory):
    from app.models import OwnerSyncRecord

    bundle, _receipt = seed_factory()
    application = offline_factory(bundle)
    entered, release = threading.Event(), threading.Event()

    def save_later():
        entered.set()
        assert release.wait(timeout=5)
        with application.app.state.session_factory.begin() as session:
            session.add(
                OwnerSyncRecord(
                    id="synthetic-background",
                    owner_id="owner",
                    kind="synthetic",
                    context={},
                    value={"saved": True},
                    version=1,
                )
            )

    def response(background: BackgroundTasks):
        background.add_task(save_later)
        return {"accepted": True}

    application.app.add_api_route("/api/v1/synthetic-background", response, methods=["POST"])
    assert application.request("POST", "/api/v1/synthetic-background")["status"] == 200
    assert entered.wait(timeout=2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(application.flush)
        try:
            time.sleep(0.05)
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=5) == {"committed": True, "pendingMutations": 0}
    with application.app.state.session_factory() as session:
        assert session.get(OwnerSyncRecord, "synthetic-background").value == {"saved": True}


@pytest.mark.parametrize("restart", [False, True])
def test_flush_terminates_assistant_and_persists_interrupted_restart(seed_factory, offline_factory, tmp_path, restart):
    from test_local_codex import Native

    from app.models import AgentRun

    bundle, _receipt = seed_factory()
    native = Native()
    profile = tmp_path / "ai-profile"
    application = offline_factory(bundle, profile, native)
    conversation = decoded(application.request("POST", "/api/v1/conversations", '{"title":"Synthetic flush"}'))
    body = json.dumps({"conversation_id": conversation["id"], "message": "Synthetic learning question"})
    response = application.request("POST", "/api/v1/assistant/runs", body)
    assert response["status"] == 201, decoded(response)
    run = decoded(response)
    assert run["status"] in {"queued", "running"}
    if restart:
        application.close()
        application = offline_factory(bundle, profile, native)
    assert application.flush() == {"committed": True, "pendingMutations": 0}
    assert native.state == "cancelled"
    with application.app.state.session_factory() as session:
        stored = session.get(AgentRun, run["id"])
        assert stored.status == ("failed" if restart else "cancelled")
        if restart:
            assert stored.error_code == "LOCAL_RUNTIME_INTERRUPTED"
    # A non-closing flush must permit a later, explicit Send.
    assert application.request("POST", "/api/v1/assistant/runs", body)["status"] == 201
    assert len(native.starts) == 2
    application.flush()


def test_flush_cancels_original_study_ai_route(seed_factory, offline_factory):
    from test_local_codex import Native

    from app.models import AgentRun, ExamStudyAiRun

    bundle, _receipt = seed_factory()
    native = Native()
    application = offline_factory(bundle, gateway=native)
    blueprint_response = application.request(
        "POST",
        "/api/v1/exam-blueprints",
        json.dumps(
            {"name": "Synthetic study", "required_lines": {"17": 1}, "constraints": {"exam": {"duration_minutes": 60}}}
        ),
    )
    assert blueprint_response["status"] == 201, decoded(blueprint_response)
    exam_response = application.request(
        "POST",
        "/api/v1/exams",
        json.dumps({"blueprint_id": decoded(blueprint_response)["id"], "seed": 31, "mode": "study"}),
    )
    assert exam_response["status"] == 201, decoded(exam_response)
    exam = decoded(exam_response)
    path = f"/api/v1/exams/{exam['id']}/items/{exam['items'][0]['id']}"
    assert application.request("PUT", path + "/workspace/code", '{"content":"print(42)","version":0}')["status"] == 200
    response = application.request(
        "POST", path + "/study-ai/runs", '{"message":"Synthetic study question","workspace_version":1}'
    )
    assert response["status"] == 201, decoded(response)
    study_run = decoded(response)
    assert application.flush()["committed"] is True
    with application.app.state.session_factory() as session:
        study = session.get(ExamStudyAiRun, study_run["id"])
        assert session.get(AgentRun, study.agent_run_id).status == "cancelled"


def test_warm_catalog_rechecks_published_edition(seed_factory, offline_factory, tmp_path):
    from sqlalchemy import select

    from app.models import SourceSyncJob

    bundle, _receipt = seed_factory()
    profile = tmp_path / "invalid-warm-profile"
    application = offline_factory(bundle, profile)
    with application.app.state.session_factory.begin() as session:
        job = session.scalar(select(SourceSyncJob))
        job.status = "failed"
    application.close()
    with pytest.raises(BridgeError) as invalid:
        offline_factory(bundle, profile)
    assert invalid.value.code == "catalog_invalid"
