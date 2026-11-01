from __future__ import annotations

from pathlib import Path

import pytest
from backend.app.api_common import ensure_owner_row
from backend.app.config import Settings
from backend.app.db import init_schema
from backend.app.main import create_app
from backend.app.models import Asset, Task, TaskRevision
from backend.app.source_catalog import SourceError, import_fixture
from backend.app.source_discovery import (
    NUMBER_API_TEMPLATE,
    NUMBER_DISCOVERY_PARSER_VERSION,
    run_number_discovery,
)
from backend.app.source_transport import SourceFetchError, _normalise_number_payload
from sqlalchemy import func, select


class FakeNumberClient:
    def __init__(self, payloads: dict[str, list[object]]) -> None:
        self.payloads = payloads
        self.json_calls: list[tuple[str, bool]] = []
        self.byte_calls: list[str] = []
        self.fail_urls: set[str] = set()

    def fetch_json_array(self, url: str, *, api_endpoint: bool = False) -> list[object]:
        self.json_calls.append((url, api_endpoint))
        if url in self.fail_urls:
            raise SourceFetchError("SOURCE_HTTP_ERROR", "fake number endpoint failure")
        return self.payloads[url]

    def fetch_bytes(self, url: str, **_kwargs: object) -> bytes:
        self.byte_calls.append(url)
        raise AssertionError("number discovery must never fetch attachment bytes")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        asset_root=tmp_path / "assets",
        owner_session_secret="test-owner-secret",
        source_mode="sync",
        source_network_enabled=True,
        source_permission_status="private_authorized",
        source_api_template="https://kompege.ru/api/v1/task/{id}",
        testing=True,
    )


def _setup(tmp_path: Path) -> tuple[Settings, object, object]:
    settings = _settings(tmp_path)
    app = create_app(settings)
    init_schema(app.state.engine)
    session = app.state.session_factory()
    with session.begin():
        ensure_owner_row(session, "owner")
    return settings, app, session


def _record(
    source_id: int, line: int, *, answer: object = "ok", assets: bool = False, hide: bool = False
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": f"upstream-{source_id}",
        "taskId": source_id,
        "number": line,
        "text": f"number-task-{source_id}",
        "key": answer,
    }
    if assets:
        item["files"] = [{"url": f"/files/{source_id}.txt", "name": f"{source_id}.txt"}]
    if hide:
        item["hide"] = True
    return item


def _number_payloads() -> dict[str, list[object]]:
    result: dict[str, list[object]] = {}
    for line in range(1, 28):
        entries: list[object]
        if line == 1:
            entries = [_record(1001, 1), _record(1002, 1), _record(1003, 1)]
        elif line == 2:
            entries = [_record(2001, 2)]
        elif line == 3:
            entries = [_record(3001, 3), _record(3002, 3, assets=True)]
        else:
            entries = [_record(10_000 + line, line)]
        result[f"https://kompege.ru/api/v1/task/number/{line}"] = entries
    return result


def test_number_payload_materializes_grouped_lines_20_and_21_with_shared_context() -> None:
    normalized = _normalise_number_payload(
        [
            {
                "id": "parent-record",
                "taskId": 1901,
                "number": 19,
                "text": "shared context",
                "key": "19",
                "table": {},
                "subTask": [
                    {"id": "child-20", "number": 20, "text": "question 20", "key": "20", "table": {}},
                    {"id": "child-21", "number": 21, "text": "question 21", "key": "21", "table": {}},
                ],
            }
        ],
        "https://kompege.ru/api/v1/task/number/19",
    )

    assert [item["line_number"] for item in normalized] == [19, 20, 21]
    assert [item["source_id"] for item in normalized] == ["1901", "child-20", "child-21"]
    assert normalized[1]["statement"] == "shared context\nquestion 20"
    assert normalized[2]["statement"] == "shared context\nquestion 21"
    assert normalized[1]["answer"] == "20"
    assert normalized[2]["answer"] == "21"
    assert normalized[1]["source_url"] == "https://kompege.ru/task?id=1901"
    assert normalized[0]["subtasks"][0]["answer"] == "20"


def test_number_discovery_covers_empty_20_21_routes_via_line_19_group(tmp_path: Path) -> None:
    settings, _app, session = _setup(tmp_path)
    payloads: dict[str, list[object]] = {}
    for line in range(1, 28):
        payloads[f"https://kompege.ru/api/v1/task/number/{line}"] = (
            [] if line in {20, 21} else [_record(70_000 + line, line)]
        )
    grouped = _record(70_019, 19)
    grouped["subTask"] = [
        {"id": "group-child-20", "number": 20, "text": "child 20", "key": "20", "table": {}},
        {"id": "group-child-21", "number": 21, "text": "child 21", "key": "21", "table": {}},
    ]
    payloads["https://kompege.ru/api/v1/task/number/19"] = [grouped]
    fake = FakeNumberClient(payloads)
    try:
        job = run_number_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,
            idempotency_key="number-grouped-19-21",
            number_api_template=NUMBER_API_TEMPLATE,
            target_exam_year=2027,
            max_unique_tasks=27,
        )

        assert job.status == "succeeded"
        assert job.counters["line_counts"] == {str(line): 1 for line in range(1, 28)}
        assert fake.byte_calls == []
    finally:
        session.close()


def test_number_discovery_is_fair_exact_and_metadata_only(tmp_path: Path) -> None:
    settings, _app, session = _setup(tmp_path)
    fake = FakeNumberClient(_number_payloads())
    try:
        first = run_number_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,
            idempotency_key="number-fair-2027",
            number_api_template=NUMBER_API_TEMPLATE,
            target_exam_year=2027,
            max_unique_tasks=27,
        )
        assert first.status == "succeeded"
        assert first.counters["current_total"] == 27
        assert first.counters["baseline_existing"] == 0
        assert first.counters["newly_admitted"] == 27
        assert first.counters["attachment_bytes_fetched"] == 0
        assert len(fake.json_calls) == 27
        assert fake.byte_calls == []
        assert first.counters["line_counts"] == {str(line): 1 for line in range(1, 28)}

        lines = dict(
            session.execute(
                select(TaskRevision.line_number, func.count(TaskRevision.id)).group_by(TaskRevision.line_number)
            ).all()
        )
        assert set(lines) == set(range(1, 28))
        assert all(count == 1 for count in lines.values())
        asset_revision = session.scalar(select(TaskRevision).where(TaskRevision.source_id == "3002"))
        assert asset_revision is not None
        assert asset_revision.parser_version == NUMBER_DISCOVERY_PARSER_VERSION
        assert asset_revision.asset_manifest
        assert session.scalar(select(func.count(Asset.id))) == 1

        replay = run_number_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,
            idempotency_key="number-fair-2027",
            number_api_template=NUMBER_API_TEMPLATE,
            target_exam_year=2027,
            max_unique_tasks=27,
        )
        assert replay.id == first.id
        assert replay.status == "succeeded"
        assert replay.counters["current_total"] == 27
        assert len(fake.json_calls) == 27
    finally:
        session.close()


def test_number_discovery_accounts_for_existing_baseline_and_conflicts_on_changed_request(
    tmp_path: Path,
) -> None:
    settings, _app, session = _setup(tmp_path)
    baseline = _record(9000, 1)
    try:
        with session.begin():
            import_fixture(
                session,
                owner_id="owner",
                settings=settings,
                payload={
                    "source_url": "https://kompege.ru/task?id=9000",
                    "source_id": "9000",
                    "line_number": 1,
                    "statement": str(baseline["text"]),
                    "answer": "ok",
                },
                idempotency_key="baseline-import",
                admission_mode="remote",
                admitted_rights_status="private_authorized",
                target_exam_year=2027,
            )
        payloads: dict[str, list[object]] = {
            f"https://kompege.ru/api/v1/task/number/{line}": ([] if line == 1 else [_record(20_000 + line, line)])
            for line in range(1, 28)
        }
        payloads["https://kompege.ru/api/v1/task/number/1"] = [_record(9000, 1)]
        fake = FakeNumberClient(payloads)
        job = run_number_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,
            idempotency_key="number-baseline-2027",
            number_api_template=NUMBER_API_TEMPLATE,
            target_exam_year=2027,
            max_unique_tasks=27,
        )
        assert job.status == "succeeded"
        assert job.counters["baseline_existing"] == 1
        assert job.counters["newly_admitted"] == 26
        assert job.counters["current_total"] == 27
        assert session.scalar(select(func.count(Task.id))) == 27
        with pytest.raises(SourceError, match="another number catalog"):
            run_number_discovery(
                session,
                owner_id="owner",
                settings=settings,
                client=fake,
                idempotency_key="number-baseline-2027",
                number_api_template=NUMBER_API_TEMPLATE,
                target_exam_year=2027,
                max_unique_tasks=26,
            )
        assert len(fake.json_calls) == 27
    finally:
        session.close()


def test_number_discovery_never_reports_success_when_a_line_request_fails(tmp_path: Path) -> None:
    settings, _app, session = _setup(tmp_path)
    payloads = {
        f"https://kompege.ru/api/v1/task/number/{line}": [_record(50_000 + line, line)] for line in range(1, 28)
    }
    fake = FakeNumberClient(payloads)
    fake.fail_urls.add("https://kompege.ru/api/v1/task/number/27")
    try:
        job = run_number_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,
            idempotency_key="number-failed-line",
            number_api_template=NUMBER_API_TEMPLATE,
            target_exam_year=2027,
            max_unique_tasks=26,
        )
        assert job.status == "partial"
        assert job.counters["numbers_completed"] == 26
        assert job.counters["current_total"] == 26
        assert any(failure["code"] == "SOURCE_HTTP_ERROR" for failure in job.failures)
        assert len(fake.json_calls) == 27
        assert fake.byte_calls == []
    finally:
        session.close()


def test_number_discovery_never_reports_success_with_missing_line_coverage(tmp_path: Path) -> None:
    settings, _app, session = _setup(tmp_path)
    payloads = {
        f"https://kompege.ru/api/v1/task/number/{line}": [_record(80_000 + line, line)]
        for line in range(1, 28)
    }
    payloads["https://kompege.ru/api/v1/task/number/27"] = []
    payloads["https://kompege.ru/api/v1/task/number/1"].append(_record(80_101, 1))
    fake = FakeNumberClient(payloads)
    try:
        job = run_number_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,
            idempotency_key="number-missing-line",
            number_api_template=NUMBER_API_TEMPLATE,
            target_exam_year=2027,
            max_unique_tasks=27,
        )

        assert job.counters["current_total"] == 27
        assert job.counters["missing_lines"] == [27]
        assert job.status == "partial"
        assert any(failure["code"] == "SOURCE_LINE_COVERAGE_INCOMPLETE" for failure in job.failures)
    finally:
        session.close()


def test_number_discovery_excludes_hidden_and_unscorable_records(tmp_path: Path) -> None:
    settings, _app, session = _setup(tmp_path)
    payloads = {f"https://kompege.ru/api/v1/task/number/{line}": [] for line in range(1, 28)}
    payloads["https://kompege.ru/api/v1/task/number/1"] = [
        _record(4001, 1, answer="ok"),
        _record(4002, 1, answer="hidden", hide=True),
        _record(4003, 1, answer=""),
    ]
    fake = FakeNumberClient(payloads)
    try:
        job = run_number_discovery(
            session,
            owner_id="owner",
            settings=settings,
            client=fake,
            idempotency_key="number-gates-2027",
            number_api_template=NUMBER_API_TEMPLATE,
            target_exam_year=2027,
            max_unique_tasks=1,
        )
        assert job.status == "succeeded"
        assert job.counters["current_total"] == 1
        assert job.counters["tasks_skipped_hidden"] == 1
        assert job.counters["tasks_skipped_unscorable"] == 1
        assert len(fake.json_calls) == 27
        assert fake.byte_calls == []
        assert session.scalar(select(TaskRevision.source_id)) == "4001"
    finally:
        session.close()


def test_number_discovery_requires_exact_reviewed_template_before_transport(tmp_path: Path) -> None:
    settings, _app, session = _setup(tmp_path)
    fake = FakeNumberClient({})
    try:
        with pytest.raises(SourceError, match="reviewed"):
            run_number_discovery(
                session,
                owner_id="owner",
                settings=settings,
                client=fake,
                idempotency_key="number-invalid-template",
                number_api_template="https://kompege.ru/api/v1/task/{number}",
            )
        assert fake.json_calls == []
        assert fake.byte_calls == []
    finally:
        session.close()
