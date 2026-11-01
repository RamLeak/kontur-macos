from __future__ import annotations

import base64
import hashlib
import json
import runpy
from dataclasses import replace
from pathlib import Path

import pytest
from backend.app.config import Settings
from backend.app.db import init_schema, make_engine, make_session_factory
from backend.app.kege_current_bank import (
    KegeCurrentBankError,
    KegeCurrentBankValidationError,
    apply_verified_kege_current_bank,
    kege_current_record_idempotency_key,
    validate_kege_current_bank,
)
from backend.app.models import Task, TaskRevision
from sqlalchemy import func, select

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
_PNG_BYTES = base64.b64decode(_PNG_B64)


def _classification(kind: str = "n01.synthetic") -> dict[str, object]:
    return {
        "primary_kind": kind,
        "title": "TEST_ONLY title",
        "secondary_tags": ["TEST_ONLY"],
        "evidence": "TEST_ONLY declared metadata",
        "review_status": "semantic_reviewed",
    }


def _task(
    source_id: str = "0007",
    *,
    line_number: int = 1,
    statement: str = "<p>TEST_ONLY condition</p>",
    answer: str = "42",
    sub_tasks: list[dict[str, object]] | None = None,
    sub_classification: list[dict[str, object]] | None = None,
    images: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "task_id": source_id,
        "source_number": line_number,
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "text_html": statement,
        "answer_html": answer,
        "answer_table": {},
        "sub_tasks": sub_tasks or [],
        "files": [],
        "images": images or [],
        "source_issues": [],
        "classification": _classification(),
        "sub_question_classification": sub_classification or [],
        "difficulty": 1,
        "comment": "TEST_ONLY comment",
        "solution_link": {"type": "test", "id": "TEST_ONLY"},
        "raw_sha256": "",
        "source_record_sha256": "",
    }


def _envelope(
    tasks: list[dict[str, object]],
    *,
    refs: dict[int, list[tuple[object, object]]] | None = None,
) -> dict[str, object]:
    refs = refs or {1: [(tasks[0]["task_id"], None)]}
    exam_types: list[dict[str, object]] = []
    for exam_number in range(1, 28):
        raw_refs = [
            {"task_id": task_id, "sub_task_number": sub_task_number}
            for task_id, sub_task_number in refs.get(exam_number, [])
        ]
        exam_types.append(
            {
                "exam_number": exam_number,
                "question_count": len(raw_refs),
                "question_refs": raw_refs,
                "assignments": [],
                "observed_varieties": [],
                "title": f"TEST_ONLY {exam_number}",
                "unrepresented_kinds": [],
                "unresolved": [],
            }
        )
    source_numbers = {str(task["source_number"]): 0 for task in tasks}
    for task in tasks:
        key = str(task["source_number"])
        source_numbers[key] += 1
    child_count = sum(len(task.get("sub_tasks") or []) for task in tasks)
    return {
        "schema_version": "kege-current-bank/2.0",
        "source_url": "https://kompege.ru/task",
        "scope": "current",
        "tasks": tasks,
        "exam_types": exam_types,
        "counts": {
            "records": len(tasks),
            "nested_questions": child_count,
            "total_questions": len(tasks) + child_count,
            "source_categories": len(source_numbers),
            "exam_numbers": 27,
            "by_source_number": source_numbers,
        },
    }


def _child_envelope() -> dict[str, object]:
    parent = _task(
        line_number=19,
        statement="<p>TEST_ONLY parent context</p><p><b>without answer</b></p>",
        answer="parent-answer",
        sub_tasks=[
            {"number": 20, "key": "20-answer", "text": "<p>TEST_ONLY child 20</p>", "table": {}, "video": "v20"},
            {"number": 21, "key": "21-answer", "text": "<p>TEST_ONLY child 21</p>", "table": {}},
        ],
        sub_classification=[
            {"exam_number": 20, **_classification("n20.synthetic")},
            {"exam_number": 21, **_classification("n21.synthetic")},
        ],
    )
    parent["source_issues"] = [{"code": "TEST_ONLY-source-issue"}]
    return _envelope(
        [parent],
        refs={
            19: [(7, None)],
            20: [(7, 20)],
            21: [(7, 21)],
        },
    )


def test_parent_and_children_keep_literal_ids_context_and_order() -> None:
    bundle = validate_kege_current_bank(_child_envelope())

    assert [record.source_id for record in bundle.records] == ["0007", "0007~20", "0007~21"]
    assert [record.line_number for record in bundle.records] == [19, 20, 21]
    assert [record.source_order for record in bundle.records] == [0, 0, 0]
    parent, child20, child21 = bundle.records
    assert parent.source_url == child20.source_url == "https://kompege.ru/task?id=0007"
    assert parent.answer == "parent-answer"
    assert child20.answer == "20-answer"
    assert child21.answer == "21-answer"
    assert "Условие задания 19 (контекст)" in child20.statement
    assert "TEST_ONLY parent context" in child20.statement
    assert "parent-answer" not in child20.statement
    assert child20.source_metadata["kege_record_id"] == "0007"
    assert child20.source_metadata["kege_question_number"] == 20
    assert child20.source_metadata["kege_schema"] == "kege-current-bank/2.0"
    assert child20.source_metadata["primary_kind"] == "n20.synthetic"
    assert "kege_sub_task_key" not in child20.source_metadata
    assert "kege_video" not in child20.source_metadata
    assert "source_issues" not in child20.source_metadata
    assert "source_issues" not in parent.source_metadata
    assert json.loads(parent.explanation or "{}")["source_issues"][0]["code"] == "TEST_ONLY-source-issue"


def test_embedded_is_digest_checked_and_external_asset_is_retained_as_unavailable() -> None:
    image = {
        "url": f"data:image/png;base64,{_PNG_B64}",
        "embedded": True,
        "bytes": len(_PNG_BYTES),
        "sha256": hashlib.sha256(_PNG_BYTES).hexdigest(),
        "status": "ok",
        "local_path": "../../TEST_ONLY-secret.png",
    }
    external = {
        "url": "https://example.invalid/image.png",
        "bytes": 7,
        "sha256": hashlib.sha256(b"TEST_ONLY").hexdigest(),
        "content_type": "image/png",
        "status": "ok",
        "local_path": "C:\\TEST_ONLY-secret.png",
    }
    task = _task(images=[image, external])
    bundle = validate_kege_current_bank(_envelope([task]))
    record = bundle.records[0]

    assert bundle.report.embedded_assets == 1
    assert bundle.report.embedded_bytes == len(_PNG_BYTES)
    assert bundle.report.unavailable_resources == 1
    assert len(record.assets) == 1
    assert record.assets[0]["url"].startswith("https://kompege.ru/api/v1/task/number/1#inline-image-v1=")
    assert "content_base64" not in record.assets[0]
    unavailable = record.source_metadata["kege_unavailable_resources"]
    assert unavailable[0]["url"] == external["url"]
    assert unavailable[0]["reason"] == "origin_not_admitted"
    assert "недоступна" in record.statement
    assert "TEST_ONLY-secret" not in json.dumps(record.source_metadata, ensure_ascii=False)


def test_empty_answer_is_ungradable_and_manifest_is_stable() -> None:
    task = _task(answer="")
    first = validate_kege_current_bank(_envelope([task]))
    second = validate_kege_current_bank(_envelope([task]))
    assert first.records[0].answer is None
    assert first.manifest_hash == second.manifest_hash
    assert first.records[0].record_hash == second.records[0].record_hash


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda value: value["tasks"][0].update(source_url="https://kompege.ru/task?id=8"), "SOURCE_URL_ID_MISMATCH"),
        (
            lambda value: value["tasks"][0]["images"].append(
                {"url": "https://kompege.ru/images/x.png", "bytes": 1, "sha256": "0" * 64, "unknown": True}
            ),
            "ASSET_UNKNOWN_FIELD",
        ),
        (
            lambda value: value["exam_types"][0]["question_refs"].append({"task_id": 999, "sub_task_number": None}),
            "QUESTION_ORDER_INVALID",
        ),
        (lambda value: value["counts"].update(total_questions=99), "COUNTS_MISMATCH"),
    ],
)
def test_invalid_reference_asset_and_count_fail_whole_bundle(mutate, code: str) -> None:
    value = _envelope([_task()])
    mutate(value)
    with pytest.raises(KegeCurrentBankValidationError) as raised:
        validate_kege_current_bank(value)
    assert any(issue.code == code for issue in raised.value.report.errors)
    assert raised.value.report.manifest_hash is None
    assert "TEST_ONLY condition" not in str(raised.value)


def test_nested_input_is_count_only_validation_error() -> None:
    value = _envelope([_task()])
    nested: list[object] = []
    for _ in range(1_200):
        nested = [nested]
    value["tasks"][0]["classification"]["secondary_tags"] = nested
    with pytest.raises(KegeCurrentBankValidationError) as raised:
        validate_kege_current_bank(value)
    assert raised.value.report.errors
    assert all(
        issue.code in {"TASK_RECORD_INVALID", "CLASSIFICATION_INVALID", "REFERENCE_COVERAGE_INVALID"}
        for issue in raised.value.report.errors
    )
    assert "TEST_ONLY condition" not in str(raised.value)


def test_target_year_replay_and_changed_target_preserve_source_identity(tmp_path: Path) -> None:
    database = tmp_path / "TEST_ONLY-kege-current.sqlite"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=tmp_path / "TEST_ONLY-assets",
        owner_id="TEST_ONLY-owner",
        source_mode="manual",
        source_network_enabled=False,
        source_permission_status="private_authorized",
        testing=True,
    )
    engine = make_engine(settings.database_url)
    init_schema(engine)
    factory = make_session_factory(engine)
    bundle = validate_kege_current_bank(_envelope([_task()]))
    with factory() as session:
        first = apply_verified_kege_current_bank(
            session,
            bundle,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            rights_status="private_authorized",
            target_exam_year=2027,
            assets_root=tmp_path / "TEST_ONLY-assets",
        )
        replay = apply_verified_kege_current_bank(
            session,
            bundle,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            rights_status="private_authorized",
            target_exam_year=2027,
            assets_root=tmp_path / "TEST_ONLY-assets",
        )
        changed_target = apply_verified_kege_current_bank(
            session,
            bundle,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            rights_status="private_authorized",
            target_exam_year=2026,
            assets_root=tmp_path / "TEST_ONLY-assets",
        )
        task = session.scalar(select(Task).where(Task.external_id == "0007"))
        assert task is not None
        revisions = list(
            session.scalars(
                select(TaskRevision).where(TaskRevision.task_id == task.id).order_by(TaskRevision.revision_number)
            )
        )
    engine.dispose()

    assert first.revisions_created == 1
    assert replay.replayed_records == 1
    assert changed_target.revisions_created == 1
    assert [revision.target_exam_year for revision in revisions] == [2027, 2026]
    assert all(revision.source_id == "0007" for revision in revisions)
    assert all(revision.source_year is None and revision.exam_year is None for revision in revisions)
    assert all(revision.source_url == "https://kompege.ru/task?id=0007" for revision in revisions)
    assert kege_current_record_idempotency_key("a" * 64, target_exam_year=2027) != kege_current_record_idempotency_key(
        "a" * 64, target_exam_year=2026
    )


def test_apply_rejects_tampered_admitted_field_before_database_write(tmp_path: Path) -> None:
    database = tmp_path / "TEST_ONLY-kege-tamper.sqlite"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=tmp_path / "TEST_ONLY-assets",
        owner_id="TEST_ONLY-owner",
        source_mode="manual",
        source_network_enabled=False,
        source_permission_status="private_authorized",
        testing=True,
    )
    engine = make_engine(settings.database_url)
    init_schema(engine)
    factory = make_session_factory(engine)
    bundle = validate_kege_current_bank(_envelope([_task()]))
    tampered = replace(bundle, records=(replace(bundle.records[0], explanation="TEST_ONLY tampered explanation"),))
    with factory() as session:
        with pytest.raises(KegeCurrentBankError) as raised:
            apply_verified_kege_current_bank(
                session,
                tampered,
                owner_id="TEST_ONLY-owner",
                settings=settings,
                rights_status="private_authorized",
            )
        assert raised.value.code == "BUNDLE_TAMPERED"
        assert session.scalar(select(func.count(TaskRevision.id))) == 0
    engine.dispose()


def test_cli_apply_fresh_database_and_replay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = runpy.run_path(
        str(Path(__file__).parent / "fixtures" / "import_kege_current_bank.py"),
        run_name="TEST_ONLY_kege_current_bank_cli",
    )
    input_path = tmp_path / "TEST_ONLY-bank.json"
    input_path.write_text(json.dumps(_envelope([_task()])), encoding="utf-8")
    database = tmp_path / "TEST_ONLY-cli.sqlite"
    assets_root = tmp_path / "TEST_ONLY-cli-assets"
    arguments = [
        str(input_path),
        "--apply",
        "--database",
        str(database),
        "--owner",
        "TEST_ONLY-cli-owner",
        "--rights",
        "private_authorized",
        "--assets-root",
        str(assets_root),
    ]

    assert cli["main"](arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "applied"
    assert first["revisions_created"] == 1
    assert first["replayed_records"] == 0

    assert cli["main"](arguments) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["status"] == "applied"
    assert replay["revisions_created"] == 0
    assert replay["replayed_records"] == 1


def test_apply_rejects_invalid_target_before_database_write(tmp_path: Path) -> None:
    database = tmp_path / "TEST_ONLY-kege-invalid-year.sqlite"
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=tmp_path / "TEST_ONLY-assets",
        owner_id="TEST_ONLY-owner",
        source_mode="manual",
        source_network_enabled=False,
        source_permission_status="private_authorized",
        testing=True,
    )
    engine = make_engine(settings.database_url)
    init_schema(engine)
    factory = make_session_factory(engine)
    bundle = validate_kege_current_bank(_envelope([_task()]))
    with factory() as session:
        with pytest.raises(Exception) as raised:
            apply_verified_kege_current_bank(
                session,
                bundle,
                owner_id="TEST_ONLY-owner",
                settings=settings,
                rights_status="private_authorized",
                target_exam_year=2101,
            )
        assert getattr(raised.value, "code", None) == "TARGET_YEAR_INVALID"
        assert session.scalar(select(func.count(TaskRevision.id))) == 0
    engine.dispose()


def test_readable_math_and_literal_markup_survive_admission(tmp_path: Path) -> None:
    raw = _envelope([_task(statement=r"<p>TEST_ONLY \(27x98876_{22}\); \(x \lt y\); &lt;b&gt; &amp;amp;</p>")])
    bundle = validate_kege_current_bank(raw)
    expected = "TEST_ONLY 27x98876₂₂; x < y; <b> &amp;"
    assert bundle.records[0].statement == expected
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'math.sqlite3'}", asset_root=tmp_path / 'assets',
        source_mode="manual", source_network_enabled=False,
    )
    engine = make_engine(settings.database_url)
    init_schema(engine)
    with make_session_factory(engine)() as session:
        apply_verified_kege_current_bank(
            session, bundle, owner_id="TEST_ONLY", settings=settings, rights_status="private_authorized",
        )
        revision = session.scalar(select(TaskRevision))
        assert revision is not None
        assert revision.statement == expected
