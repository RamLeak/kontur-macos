from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import init_schema, make_engine, make_session_factory
from backend.app.local_bank import (
    LocalBankError,
    LocalBankValidationError,
    apply_verified_bundle,
    load_local_bank,
    validate_bundle,
)
from backend.app.main import create_app
from backend.app.models import Asset, Task, TaskRevision, User
from backend.app.source_catalog import SourceError
from fastapi.testclient import TestClient
from sqlalchemy import func, select


def row(
    source_id: str = "000123",
    *,
    line_number: int = 1,
    statement: str = "TEST_ONLY condition",
    answer: str = "42",
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "source_url": f"https://kompege.ru/task?id={source_id}",
        "line_number": line_number,
        "statement": statement,
        "answer": answer,
        "explanation": "TEST_ONLY explanation",
        "source_year": 2025,
        "source_year_basis": "local-file-declared",
        "source_metadata": {"declared_by": "TEST_ONLY"},
        "assets": [
            {
                "url": "https://kompege.ru/files/input.txt",
                "filename": "input.txt",
                "media_type": "text/plain",
                "role": "file",
            }
        ],
    }


def envelope(*rows_to_add: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "source": "kompege", "tasks": list(rows_to_add)}


@pytest.fixture()
def test_database(tmp_path: Path):
    database = tmp_path / "TEST_ONLY-local-bank.sqlite"
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
    with factory() as session:
        with session.begin():
            session.add(User(id="TEST_ONLY-owner"))
        yield session, settings
    engine.dispose()


def test_envelope_preserves_leading_zero_year_metadata_and_hides_server_fields(tmp_path: Path) -> None:
    input_path = tmp_path / "TEST_ONLY-bank.json"
    input_path.write_text(json.dumps(envelope(row()), ensure_ascii=False), encoding="utf-8")

    bundle = load_local_bank(input_path)

    assert bundle.records[0].source_id == "000123"
    assert bundle.records[0].source_year == 2025
    assert bundle.records[0].source_metadata == {"declared_by": "TEST_ONLY"}
    assert bundle.records[0].answer == "42"
    assert bundle.records[0].source_year_basis == "local-file-declared"
    summary = json.dumps(bundle.report.summary(), ensure_ascii=False)
    assert "TEST_ONLY condition" not in summary
    assert "42" not in summary
    assert "TEST_ONLY explanation" not in summary


def test_jsonl_and_identical_duplicates_are_deterministic(tmp_path: Path) -> None:
    first = row("000002", line_number=2)
    second = row("000001", line_number=1)
    input_path = tmp_path / "TEST_ONLY-bank.jsonl"
    input_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in (first, second, first)) + "\n",
        encoding="utf-8",
    )

    bundle = load_local_bank(input_path)
    reordered = validate_bundle(envelope(second, first))

    assert bundle.report.duplicate_records == 1
    assert bundle.report.unique_records == 2
    assert [item.source_id for item in bundle.records] == ["000001", "000002"]
    assert bundle.manifest_hash == reordered.manifest_hash


def test_conflicting_duplicate_rejects_whole_bundle_and_reports_counts_only() -> None:
    conflicting = row(statement="TEST_ONLY changed")

    with pytest.raises(LocalBankValidationError) as raised:
        validate_bundle(envelope(row(), conflicting))

    report = raised.value.report
    assert report.manifest_hash is None
    assert any(issue.code == "DUPLICATE_SOURCE_ID_CONFLICT" for issue in report.errors)
    assert "TEST_ONLY" not in json.dumps(report.summary(), ensure_ascii=False)
    assert "42" not in str(raised.value)


@pytest.mark.parametrize(
    "asset",
    [
        {"url": "https://kompege.ru/files/input.txt", "data": "forbidden"},
        {"url": "https://kompege.ru/files/input.txt", "content_base64": "Zm9v"},
        {"url": "https://kompege.ru/files/input.txt", "path": "C:\\secret\\input.txt"},
        {"url": "https://kompege.ru/files/input.txt", "unknown": True},
    ],
)
def test_assets_are_metadata_only_and_reject_unknown_or_path_fields(asset: dict[str, object]) -> None:
    invalid = row()
    invalid["assets"] = [asset]

    with pytest.raises(LocalBankValidationError) as raised:
        validate_bundle(envelope(invalid))

    assert any(issue.code == "ASSET_UNKNOWN_FIELD" for issue in raised.value.report.errors)


def test_apply_replay_is_idempotent_changed_payload_creates_revision_and_assets_stay_metadata_only(
    test_database,
) -> None:
    session, settings = test_database
    first_bundle = validate_bundle(envelope(row()))

    first = apply_verified_bundle(
        session,
        first_bundle,
        owner_id="TEST_ONLY-owner",
        settings=settings,
        rights_status="private_authorized",
    )
    replay = apply_verified_bundle(
        session,
        first_bundle,
        owner_id="TEST_ONLY-owner",
        settings=settings,
        rights_status="private_authorized",
    )

    changed = row(statement="TEST_ONLY second revision", answer="43")
    changed_bundle = validate_bundle(envelope(changed))
    second = apply_verified_bundle(
        session,
        changed_bundle,
        owner_id="TEST_ONLY-owner",
        settings=settings,
        rights_status="private_authorized",
    )

    assert first.revisions_created == 1
    assert replay.revisions_created == 0
    assert replay.replayed_records == 1
    assert second.revisions_created == 1
    assert second.replayed_records == 0
    assert session.scalar(select(func.count(TaskRevision.id))) == 2
    task = session.scalar(select(Task).where(Task.external_id == "000123"))
    assert task is not None
    revisions = list(
        session.scalars(
            select(TaskRevision).where(TaskRevision.task_id == task.id).order_by(TaskRevision.revision_number)
        )
    )
    assert [revision.answer for revision in revisions] == ["42", "43"]
    assert revisions[0].source_id == revisions[1].source_id == "000123"
    asset = session.scalar(select(Asset))
    assert asset is not None
    assert asset.cache_state == "metadata_only"
    assert asset.storage_key is None
    assert asset.size_bytes == 0
    assert asset.hash_verified is False


def test_apply_target_override_replays_and_changes_revision_without_rewriting_source_fields(test_database) -> None:
    session, settings = test_database
    bundle = validate_bundle(envelope(row()))
    record = bundle.records[0]

    first = apply_verified_bundle(
        session,
        bundle,
        owner_id="TEST_ONLY-owner",
        settings=settings,
        rights_status="private_authorized",
        target_exam_year=2027,
    )
    replay = apply_verified_bundle(
        session,
        bundle,
        owner_id="TEST_ONLY-owner",
        settings=settings,
        rights_status="private_authorized",
        target_exam_year=2027,
    )
    changed_target = apply_verified_bundle(
        session,
        bundle,
        owner_id="TEST_ONLY-owner",
        settings=settings,
        rights_status="private_authorized",
        target_exam_year=2026,
    )

    assert record.target_exam_year is None
    assert bundle.manifest_hash == first.manifest_hash == replay.manifest_hash == changed_target.manifest_hash
    assert first.revisions_created == 1
    assert replay.revisions_created == 0
    assert replay.replayed_records == 1
    assert changed_target.revisions_created == 1
    assert changed_target.replayed_records == 0

    task = session.scalar(select(Task).where(Task.external_id == record.source_id))
    assert task is not None
    revisions = list(
        session.scalars(
            select(TaskRevision).where(TaskRevision.task_id == task.id).order_by(TaskRevision.revision_number)
        )
    )
    assert len(revisions) == 2
    assert [revision.target_exam_year for revision in revisions] == [2027, 2026]
    assert all(revision.source_id == record.source_id for revision in revisions)
    assert all(revision.source_year == record.source_year == 2025 for revision in revisions)
    assert all(revision.exam_year == record.exam_year is None for revision in revisions)
    assert all(revision.source_metadata == record.source_metadata for revision in revisions)
    assert revisions[0].id != revisions[1].id
    assert revisions[0].task_id == revisions[1].task_id == task.id


@pytest.mark.parametrize("invalid_target", [True, 1999, 2101])
def test_apply_target_override_validates_before_database_write(test_database, invalid_target: object) -> None:
    session, settings = test_database
    bundle = validate_bundle(envelope(row()))

    with pytest.raises(LocalBankError) as raised:
        apply_verified_bundle(
            session,
            bundle,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            rights_status="private_authorized",
            target_exam_year=invalid_target,  # type: ignore[arg-type]
        )

    assert raised.value.code == "TARGET_YEAR_INVALID"
    assert session.scalar(select(func.count(Task.id))) == 0


def test_explicit_target_2027_enrolls_in_actual_exam_selection(test_database) -> None:
    session, settings = test_database
    bundle = validate_bundle(envelope(row()))
    apply_verified_bundle(
        session,
        bundle,
        owner_id="TEST_ONLY-owner",
        settings=settings,
        rights_status="private_authorized",
        target_exam_year=2027,
    )

    app_settings = replace(
        settings,
        owner_session_secret="TEST_ONLY-session-secret",
        security_mode="testing",
    )
    with TestClient(create_app(app_settings)) as client:
        client.headers["X-Owner-Session"] = issue_session_token(
            "TEST_ONLY-owner", "TEST_ONLY-session-secret"
        )
        blueprint = client.post(
            "/api/v1/exam-blueprints",
            json={
                "name": "TEST_ONLY EGE 2027",
                "exam_year": 2027,
                "required_lines": {"1": 1},
            },
        )
        assert blueprint.status_code == 201, blueprint.text
        exam = client.post(
            "/api/v1/exams",
            json={"blueprint_id": blueprint.json()["id"], "seed": 2027},
        )

    assert exam.status_code == 201, exam.text
    assert [item["source_id"] for item in exam.json()["items"]] == ["000123"]


def test_apply_requires_manual_network_disabled_and_explicit_rights(test_database) -> None:
    session, settings = test_database
    bundle = validate_bundle(envelope(row()))

    with pytest.raises(LocalBankError) as invalid_rights:
        apply_verified_bundle(
            session,
            bundle,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            rights_status="review_required",
        )
    assert invalid_rights.value.code == "RIGHTS_INVALID"

    fixture_settings = Settings(
        database_url=settings.database_url,
        asset_root=settings.asset_root,
        source_mode="fixture",
        source_network_enabled=False,
        testing=True,
    )
    with pytest.raises(LocalBankError) as invalid_mode:
        apply_verified_bundle(
            session,
            bundle,
            owner_id="TEST_ONLY-owner",
            settings=fixture_settings,
            rights_status="private_authorized",
        )
    assert invalid_mode.value.code == "SOURCE_DISABLED"


def test_source_catalog_local_file_seam_keeps_server_rights_and_mode_gates(test_database) -> None:
    session, settings = test_database
    with pytest.raises(SourceError) as missing_rights:
        from backend.app.source_catalog import import_fixture

        import_fixture(
            session,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            payload=row(),
            idempotency_key="TEST_ONLY-local-file",
            admission_mode="local-file",
            admitted_rights_status="review_required",
        )
    assert missing_rights.value.code == "SOURCE_RIGHTS_REQUIRED"


def test_source_catalog_local_file_seam_rejects_bytes_before_asset_side_effects(test_database) -> None:
    session, settings = test_database
    payload = row()
    payload["assets"] = [
        {
            "url": "https://kompege.ru/files/input.txt",
            "filename": "input.txt",
            "media_type": "text/plain",
            "role": "file",
            "content_hash": hashlib.sha256(b"42\n").hexdigest(),
            "content_base64": "NDIK",
        }
    ]
    from backend.app.source_catalog import import_fixture

    with pytest.raises(SourceError) as raised:
        with session.begin():
            import_fixture(
                session,
                owner_id="TEST_ONLY-owner",
                settings=settings,
                payload=payload,
                idempotency_key="TEST_ONLY-local-bytes",
                admission_mode="local-file",
                admitted_rights_status="private_authorized",
            )
    assert raised.value.code == "ASSET_DATA_INVALID"
    assert session.scalar(select(func.count(Asset.id))) == 0


def test_metadata_asset_promotes_in_place_on_verified_remote_bytes(test_database) -> None:
    session, settings = test_database
    digest = hashlib.sha256(b"42\n").hexdigest()
    local_payload = row()
    local_payload["assets"] = [
        {
            "url": "https://kompege.ru/files/input.txt",
            "filename": "input.txt",
            "media_type": "text/plain",
            "role": "file",
            "content_hash": digest,
        }
    ]
    from backend.app.source_catalog import import_fixture

    with session.begin():
        _task, _revision, local_assets, _duplicate = import_fixture(
            session,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            payload=local_payload,
            idempotency_key="TEST_ONLY-local-metadata-hash",
            admission_mode="local-file",
            admitted_rights_status="private_authorized",
        )
    local_asset = local_assets[0]
    assert local_asset.cache_state == "metadata_only"
    assert local_asset.size_bytes == 0

    remote_settings = replace(settings, source_network_enabled=True)
    remote_payload = dict(local_payload)
    remote_payload["assets"] = [dict(local_payload["assets"][0], content_base64="NDIK")]
    with session.begin():
        _task, _revision, remote_assets, _duplicate = import_fixture(
            session,
            owner_id="TEST_ONLY-owner",
            settings=remote_settings,
            payload=remote_payload,
            idempotency_key="TEST_ONLY-remote-bytes",
            admission_mode="remote",
            admitted_rights_status="private_authorized",
        )

    promoted = remote_assets[0]
    assert promoted.id == local_asset.id
    assert promoted.cache_state == "ready"
    assert promoted.hash_verified is True
    assert promoted.size_bytes == 3
    assert promoted.content_hash == digest
    assert promoted.storage_key
    assert session.scalar(select(func.count(Asset.id))) == 1


def test_metadata_promotion_quota_failure_keeps_placeholder(test_database) -> None:
    session, settings = test_database
    settings = replace(settings, max_asset_cache_bytes=1)
    digest = hashlib.sha256(b"42\n").hexdigest()
    local_payload = row()
    local_payload["assets"] = [
        {
            "url": "https://kompege.ru/files/input.txt",
            "filename": "input.txt",
            "media_type": "text/plain",
            "role": "file",
            "content_hash": digest,
        }
    ]
    from backend.app.source_catalog import import_fixture

    with session.begin():
        _task, _revision, local_assets, _duplicate = import_fixture(
            session,
            owner_id="TEST_ONLY-owner",
            settings=settings,
            payload=local_payload,
            idempotency_key="TEST_ONLY-local-quota",
            admission_mode="local-file",
            admitted_rights_status="private_authorized",
        )
    asset_id = local_assets[0].id

    remote_settings = replace(settings, source_network_enabled=True)
    remote_payload = dict(local_payload)
    remote_payload["assets"] = [dict(local_payload["assets"][0], content_base64="NDIK")]
    with pytest.raises(SourceError) as raised:
        with session.begin():
            import_fixture(
                session,
                owner_id="TEST_ONLY-owner",
                settings=remote_settings,
                payload=remote_payload,
                idempotency_key="TEST_ONLY-remote-quota",
                admission_mode="remote",
                admitted_rights_status="private_authorized",
            )
    assert raised.value.code == "ASSET_CACHE_QUOTA"
    unchanged = session.get(Asset, asset_id)
    assert unchanged is not None
    assert unchanged.cache_state == "metadata_only"
    assert unchanged.size_bytes == 0
    assert unchanged.storage_key is None


def test_nested_source_metadata_is_rejected_with_count_only_error() -> None:
    metadata: dict[str, object] = {}
    cursor = metadata
    for _ in range(10_000):
        nested: dict[str, object] = {}
        cursor["nested"] = nested
        cursor = nested
    cursor["value"] = "TEST_ONLY private text"
    invalid = row()
    invalid["source_metadata"] = metadata

    with pytest.raises(LocalBankValidationError) as raised:
        validate_bundle(envelope(invalid))

    report = raised.value.report
    assert any(issue.code == "SOURCE_METADATA_TOO_DEEP" for issue in report.errors)
    assert "TEST_ONLY private text" not in str(raised.value)


def test_nested_json_parse_recursion_is_converted_to_validation_error(tmp_path: Path) -> None:
    input_path = tmp_path / "TEST_ONLY-deep.jsonl"
    input_path.write_text(
        '{"source_id":"000123","source_url":"https://kompege.ru/task?id=000123",'
        '"line_number":1,"statement":"TEST_ONLY",' + '"source_metadata":{' * 10_000 + "null" + "}" * 10_000 + "}\n",
        encoding="utf-8",
    )

    with pytest.raises(LocalBankValidationError) as raised:
        load_local_bank(input_path)

    assert raised.value.report.errors
    assert "TEST_ONLY" not in str(raised.value)
