# ruff: noqa: N999
"""Validate or explicitly apply the current КЕГЭ bank snapshot.

The default command is a count-only dry-run.  Applying requires every local
destination and the server-authorized rights status explicitly; this script
never calls ``Settings.from_env`` and never discovers a database or asset
directory.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from backend.app.config import Settings
from backend.app.db import init_schema, make_engine, make_session_factory
from backend.app.kege_current_bank import (
    KegeCurrentBankError,
    KegeCurrentBankValidationError,
    apply_verified_kege_current_bank,
    load_kege_current_bank,
    validate_target_exam_year,
)
from backend.app.source_catalog import SourceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a kege-current-bank/2.0 JSON snapshot; use --apply only with explicit local destinations."
    )
    parser.add_argument("input", type=Path, help="explicit local current-bank JSON snapshot")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically admit the already verified snapshot",
    )
    parser.add_argument("--database", type=Path, help="explicit local SQLite database path for --apply")
    parser.add_argument("--owner", help="explicit owner id for --apply")
    parser.add_argument(
        "--rights",
        choices=("private_authorized", "public_authorized"),
        help="server-authorized rights status for --apply",
    )
    parser.add_argument("--assets-root", type=Path, help="explicit local asset root for --apply")
    parser.add_argument(
        "--target-exam-year",
        type=int,
        help="owner-selected preparation target year (only with --apply; 2000..2100)",
    )
    return parser


def _json_output(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _failure_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else "APPLY_FAILED"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    apply_options = (
        arguments.database,
        arguments.owner,
        arguments.rights,
        arguments.assets_root,
    )
    if not arguments.apply and any(option is not None for option in apply_options):
        parser.error("--database/--owner/--rights/--assets-root require --apply")
    if not arguments.apply and arguments.target_exam_year is not None:
        parser.error("--target-exam-year requires --apply")
    if arguments.apply and any(option is None for option in apply_options):
        parser.error("--apply requires --database, --owner, --rights and --assets-root")
    try:
        target_exam_year = validate_target_exam_year(arguments.target_exam_year)
    except KegeCurrentBankError as error:
        parser.error(error.message)
    if arguments.apply and arguments.database is not None:
        database_text = str(arguments.database)
        if "://" in database_text:
            parser.error("--database must be a local filesystem path, not a database URL")
        if arguments.assets_root is not None and arguments.assets_root.exists() and arguments.assets_root.is_file():
            parser.error("--assets-root must be a directory path")

    try:
        bundle = load_kege_current_bank(arguments.input)
    except KegeCurrentBankValidationError as error:
        _json_output({"status": "invalid", **error.report.summary()})
        return 2
    except KegeCurrentBankError as error:
        _json_output({"status": "invalid", "error_count": 1, "errors_by_code": {error.code: 1}})
        return 2

    if not arguments.apply:
        _json_output({"status": "validated", **bundle.report.summary()})
        return 0

    assert arguments.database is not None
    assert arguments.owner is not None
    assert arguments.rights is not None
    assert arguments.assets_root is not None
    database = arguments.database
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        asset_root=arguments.assets_root,
        owner_id=arguments.owner,
        source_mode="manual",
        source_network_enabled=False,
        source_permission_status=arguments.rights,
    )
    engine = make_engine(settings.database_url)
    try:
        init_schema(engine)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            result = apply_verified_kege_current_bank(
                session,
                bundle,
                owner_id=arguments.owner,
                settings=settings,
                rights_status=arguments.rights,
                target_exam_year=target_exam_year,
                assets_root=arguments.assets_root,
            )
    except (KegeCurrentBankError, SourceError) as error:
        _json_output(
            {
                "status": "apply_failed",
                "error_count": 1,
                "errors_by_code": {_failure_code(error): 1},
            }
        )
        return 2
    except Exception:  # noqa: BLE001 - CLI must return a count-only failure
        _json_output(
            {
                "status": "apply_failed",
                "error_count": 1,
                "errors_by_code": {"APPLY_FAILED": 1},
            }
        )
        return 2
    finally:
        engine.dispose()
    _json_output({"status": "applied", **result.summary()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
