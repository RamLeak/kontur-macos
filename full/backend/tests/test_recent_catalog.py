from __future__ import annotations

from pathlib import Path

import pytest
from backend.app.api_common import ensure_owner_row
from backend.app.auth import issue_session_token
from backend.app.config import Settings
from backend.app.db import CURRENT_SCHEMA_VERSION, init_schema, make_engine
from backend.app.main import create_app
from backend.app.models import CatalogEntry, SourceSyncJob, TaskRevision
from backend.app.recent_catalog import run_recent_catalog
from backend.app.source_catalog import import_fixture
from backend.app.source_transport import SourceFetchError, _normalise_remote_record
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text


class NumberClient:
    def __init__(self) -> None:
        self.payloads: dict[int, list[dict[str, object]]] = {line: [] for line in range(1, 28)}
        self.calls: list[int] = []
        self.fail_line: int | None = None

    def fetch_json_array(self, url: str, *, api_endpoint: bool = False) -> list[dict[str, object]]:
        assert api_endpoint
        line = int(url.rsplit("/", 1)[-1])
        self.calls.append(line)
        if line == self.fail_line:
            raise SourceFetchError("SOURCE_HTTP_ERROR", "synthetic failure")
        return self.payloads[line]

    def fetch_bytes(self, *_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("catalog imports must not download attachment bytes")


def record(source_id: int, line: int, year: int, level: int) -> dict[str, object]:
    return {"taskId": source_id, "number": line, "text": f"Synthetic task {source_id}",
            "key": "42", "difficulty": level, "createdAt": f"{year}-03-01T10:00:00Z"}


@pytest.fixture()
def catalog_app(tmp_path: Path):
    settings = Settings(database_url="sqlite+pysqlite:///:memory:", asset_root=tmp_path / "assets",
                        owner_session_secret="test-only", testing=True, source_mode="sync",
                        source_network_enabled=True, source_permission_status="private_authorized")
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers.update({"X-Owner-Session": issue_session_token("owner", "test-only")})
        yield settings, app, client


def import_catalog(app, settings, source, key="recent-one", owner="owner"):
    with app.state.session_factory() as session:
        with session.begin():
            ensure_owner_row(session, owner)
        return run_recent_catalog(session, owner_id=owner, settings=settings, client=source, idempotency_key=key)


@pytest.mark.parametrize("failures", [2, 3])
def test_transient_network_retries_are_bounded(catalog_app, failures) -> None:
    settings, app, _client = catalog_app

    class TransientClient(NumberClient):
        attempts = 0

        def fetch_json_array(self, url, *, api_endpoint=False):
            if url.endswith("/number/1"):
                self.attempts += 1
                if self.attempts <= failures:
                    raise SourceFetchError("SOURCE_FETCH_FAILED", "synthetic connection reset")
            return super().fetch_json_array(url, api_endpoint=api_endpoint)

    source = TransientClient()
    source.payloads[1] = [record(9100, 1, 2026, 0)]
    if failures == 2:
        result = import_catalog(app, settings, source)
        assert result.counters["published"] is True
    else:
        with pytest.raises(SourceFetchError):
            import_catalog(app, settings, source)
    assert source.attempts == 3


def test_published_catalog_applies_cell_fallback_and_preserves_revisions(catalog_app) -> None:
    settings, app, client = catalog_app
    source = NumberClient()
    source.payloads[1] = ([record(1000 + i, 1, 2025, 0) for i in range(29)]
                          + [record(2000 + i, 1, 2024, 0) for i in range(25)]
                          + [record(3000 + i, 1, 2025, 1) for i in range(30)]
                          + [record(4000 + i, 1, 2024, 1) for i in range(25)]
                          + [record(5000, 1, 2023, 2)])
    first = import_catalog(app, settings, source)
    assert first.status == "partial"
    coverage = client.get("/api/v1/tasks/coverage").json()
    assert coverage["published"] is True
    assert coverage["total_selected"] == 80
    assert len(coverage["cells"]) == 81
    cells = {(cell["line_number"], cell["difficulty"]): cell for cell in coverage["cells"]}
    assert cells[(1, "easy")]["selected_fallback"] == 21
    assert cells[(1, "medium")]["fallback_allowed"] is False
    assert cells[(1, "medium")]["shortfall"] == 20
    response = client.get("/api/v1/tasks", params={"catalog": "recent", "limit": 100}).json()
    assert response["total"] == 80
    assert all(item["revision"]["source_year"] >= 2024 for item in response["items"])
    old_revision = response["items"][0]["revision"]["id"]
    calls = len(source.calls)
    replay = import_catalog(app, settings, source)
    assert replay.id == first.id and len(source.calls) == calls
    source.payloads[1] = [record(9000, 1, 2026, 2)]
    second = import_catalog(app, settings, source, key="recent-two")
    assert second.id != first.id
    assert client.get("/api/v1/tasks", params={"catalog": "recent"}).json()["total"] == 1
    with app.state.session_factory() as session:
        assert session.get(TaskRevision, old_revision) is not None
        assert session.scalar(select(func.count(CatalogEntry.id))) == 81


def test_source_failure_does_not_replace_published_catalog(catalog_app) -> None:
    settings, app, client = catalog_app
    source = NumberClient()
    source.payloads[1] = [record(1001, 1, 2026, 0)]
    first = import_catalog(app, settings, source)
    source.fail_line = 12
    with pytest.raises(SourceFetchError):
        import_catalog(app, settings, source, key="failed-fetch")
    assert client.get("/api/v1/tasks/coverage").json()["total_selected"] == 1
    with app.state.session_factory() as session:
        assert session.get(SourceSyncJob, first.id).counters["published"] is True
        assert session.scalar(select(func.count(CatalogEntry.id))) == 1


def test_published_catalog_keeps_its_pinned_revision_after_later_task_import(catalog_app) -> None:
    settings, app, client = catalog_app
    source = NumberClient()
    source.payloads[1] = [record(1001, 1, 2026, 0)]
    import_catalog(app, settings, source)
    original = client.get("/api/v1/tasks", params={"catalog": "recent"}).json()["items"][0]

    changed = _normalise_remote_record(
        {
            "taskId": 1001,
            "number": 1,
            "text": "Changed after catalog publication",
            "key": "84",
            "difficulty": 2,
            "createdAt": "2026-04-01T10:00:00Z",
        },
        "https://kompege.ru/task?id=1001",
    )
    with app.state.session_factory() as session:
        with session.begin():
            import_fixture(
                session,
                owner_id="owner",
                settings=settings,
                payload=changed,
                idempotency_key="later-task-import",
                admission_mode="remote",
                admitted_rights_status="private_authorized",
            )

    pinned = client.get("/api/v1/tasks", params={"catalog": "recent"}).json()["items"][0]
    current = client.get("/api/v1/tasks", params={"catalog": "all"}).json()["items"][0]
    assert pinned["revision"]["id"] == original["revision"]["id"]
    assert current["revision"]["id"] != original["revision"]["id"]


@pytest.mark.parametrize("sort", ["number", "newest", "difficulty_asc", "difficulty_desc"])
def test_filters_sort_pagination_and_cursor_scope(catalog_app, sort: str) -> None:
    settings, app, client = catalog_app
    source = NumberClient()
    source.payloads[1] = [record(1000 + i, 1, 2025 + i % 2, i % 3) for i in range(15)]
    source.payloads[2] = [record(2000 + i, 2, 2026, i % 3) for i in range(6)]
    import_catalog(app, settings, source)
    params = {"catalog": "recent", "sort": sort, "limit": 4}
    ids: list[str] = []
    levels: list[str] = []
    years: list[int] = []
    first_cursor = None
    while True:
        result = client.get("/api/v1/tasks", params=params)
        assert result.status_code == 200, result.text
        page = result.json()
        assert page["total"] == 21
        ids += [item["id"] for item in page["items"]]
        levels += [item["revision"]["difficulty"] for item in page["items"]]
        years += [item["revision"]["source_year"] for item in page["items"]]
        cursor = page["next_cursor"]
        first_cursor = first_cursor or cursor
        if cursor is None:
            break
        params["cursor"] = cursor
    assert len(ids) == len(set(ids)) == 21
    rank = {"easy": 0, "medium": 1, "hard": 2}
    if sort.startswith("difficulty"):
        assert [rank[level] for level in levels] == sorted(map(rank.get, levels), reverse=sort.endswith("desc"))
    if sort == "newest":
        assert years == sorted(years, reverse=True)
    changed = client.get("/api/v1/tasks", params={"catalog": "recent", "sort": sort, "line": 2,
                                                  "cursor": first_cursor})
    assert changed.status_code == 422
    filtered = client.get("/api/v1/tasks", params={"catalog": "recent", "source_year": 2026,
                                                  "difficulty": "hard", "line": 2}).json()
    assert filtered["total"] == 2
    assert all(item["revision"]["difficulty"] == "hard" for item in filtered["items"])


def test_recent_catalog_is_owner_scoped_and_unknown_values_fail_closed(catalog_app) -> None:
    settings, app, client = catalog_app
    assert client.get("/api/v1/tasks/coverage").json()["published"] is False
    source = NumberClient()
    source.payloads[1] = [record(1001, 1, 2026, 0)]
    import_catalog(app, settings, source, owner="other")
    assert client.get("/api/v1/tasks", params={"catalog": "recent"}).json()["total"] == 0
    assert client.get("/api/v1/tasks/coverage").json()["published"] is False
    for params in ({"sort": "drop table"}, {"difficulty": "legendary"}, {"catalog": "other-owner"}):
        assert client.get("/api/v1/tasks", params=params).status_code == 422


def test_v4_schema_upgrade_preserves_old_task_and_adds_unknown_metadata(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'upgrade.sqlite').as_posix()}")
    init_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version > 4"))
        for column in ("source_year", "source_year_basis", "source_created_at", "source_difficulty",
                       "difficulty", "source_label", "source_metadata"):
            # Existing v4 databases simply lack these additive columns; emulate
            # a legacy table independently of the new model definitions.
            indexes = connection.exec_driver_sql("PRAGMA index_list('task_revisions')").fetchall()
            for index in indexes:
                if column in str(index[1]):
                    connection.exec_driver_sql(f'DROP INDEX "{index[1]}"')
            connection.exec_driver_sql(f"ALTER TABLE task_revisions DROP COLUMN {column}")
    init_schema(engine)
    init_schema(engine)
    with engine.connect() as connection:
        stored_version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
        assert stored_version == CURRENT_SCHEMA_VERSION
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info('task_revisions')")}
        assert {"source_year", "difficulty", "source_metadata"} <= columns
    engine.dispose()
