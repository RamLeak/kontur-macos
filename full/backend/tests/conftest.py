"""Offline integration fixtures built exclusively from synthetic source rows."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from offline_app import OfflineApplication


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_factory(tmp_path):
    source = Path(__file__).resolve().parents[3] / "ops" / "catalog-offline-20261029"
    exporter = _load("export_catalog", source / "export_catalog.py")
    fixture = _load("_independent_catalog_fixture", source / "test_export_catalog.py")
    cases = []

    def create(task_count=1):
        # Keep SHA-256 asset filenames below Windows MAX_PATH without changing
        # production paths or writing outside this explicitly owned workspace.
        short_parent = Path(__file__).resolve().parents[1] / ".t4"
        short_parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="seed-", dir=short_parent)
        owned = Path(temporary.name).resolve()
        fixture.OWNED = owned
        case = fixture.ExportTests("test_complete_faithful_records_and_privacy")
        cases.append((case, temporary))
        case.setUp()
        # Clone only the fixture's public catalog columns. No real DB is opened.
        case.connection.row_factory = sqlite3.Row
        task = dict(case.connection.execute("SELECT * FROM tasks WHERE id='t-1'").fetchone())
        revision = dict(case.connection.execute("SELECT * FROM task_revisions WHERE id='r-2'").fetchone())
        link = dict(case.connection.execute("SELECT * FROM task_assets WHERE id='l-2'").fetchone())
        for index in range(2, task_count + 1):
            task_id, revision_id = f"synthetic-task-{index}", f"synthetic-revision-{index}"
            case.insert(
                "tasks", {**task, "id": task_id, "external_id": str(1000 + index), "current_revision_id": revision_id}
            )
            case.insert(
                "task_revisions",
                {
                    **revision,
                    "id": revision_id,
                    "task_id": task_id,
                    "revision_number": 1,
                    "source_id": str(1000 + index),
                },
            )
            case.insert("task_assets", {**link, "id": f"synthetic-link-{index}", "revision_id": revision_id})
        case.connection.commit()
        bundle = owned / "bundle"
        (bundle / "catalog").mkdir(parents=True)
        receipt = exporter.export_catalog(case.database, case.cache, bundle / "catalog" / "catalog.sqlite3")
        assert receipt["complete"] is True
        return bundle, receipt

    yield create
    for case, temporary in reversed(cases):
        fixture.OWNED = case.root.parent
        case.doCleanups()
        target = Path(temporary.name).resolve()
        assert target.parent == Path(__file__).resolve().parents[1] / ".t4"
        assert target.name.startswith("seed-")
        temporary.cleanup()


@pytest.fixture
def offline_factory(tmp_path):
    applications = []

    def create(bundle, profile=None, gateway=None):
        if gateway is None:

            def gateway(operation, _payload):
                assert operation == "codex_status"
                return {
                    "gatewayProtocol": 1,
                    "state": "unavailable",
                    "loginState": "unknown",
                    "manualModelSupported": False,
                    "manualModels": [],
                }

        application = OfflineApplication(profile or tmp_path / f"profile-{len(applications)}", bundle, gateway)
        applications.append(application)
        application.start()
        return application

    yield create
    for application in reversed(applications):
        if not application._loop.is_closed():
            application.close()
