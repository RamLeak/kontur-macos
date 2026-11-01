"""Typed private UI state, stored in the existing OwnerSyncRecord table."""

from __future__ import annotations

import calendar
import json
import math
import re
from typing import Any
from urllib.parse import quote, unquote

MAX_BYTES = 8 * 1024 * 1024
MAX_SAFE = 9_007_199_254_740_991
RECORD_ID = "offline-client-state-v1"
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
COMMANDS = frozenset(
    (
        "assistant.toggle panel.toggle focus.toggle surface.toggle code.run answer.check "
        "task.previous task.next task.bookmark board.pen board.marker board.pan board.line "
        "board.rect board.ellipse board.text board.eraser board.undo board.redo board.clear "
        "board.zoom.in board.zoom.out board.zoom.reset"
    ).split()
)
ENUMS = {
    "theme": {"system", "light", "dark"},
    "textSize": {"standard", "large"},
    "density": {"comfortable", "compact"},
    "editorTheme": {"interface", "light", "charcoal"},
    "boardBackground": {"blank", "grid", "dots"},
    "assistantWidth": {"narrow", "standard", "wide"},
    "hintMode": {"hint", "explain"},
    "model": {"auto", "luna", "terra", "sol"},
    "effort": {"low", "medium", "high", "xhigh"},
    "tutorMode": {"hint", "hint_ladder", "feynman", "socratic", "debug", "review", "compare", "full_solution"},
}
BOOLS = frozenset(
    "reducedMotion autoHideNavigation rightPanelVisible restoreWorkspace editorWrap editorLineNumbers".split()
)
RANGES = {
    "outputHeight": (100, 360),
    "boardWidth": (1, 24),
    "boardMarkerWidth": (1, 24),
    "boardOpacity": (0.1, 1),
    "boardMarkerOpacity": (0.1, 1),
    "drawingSmoothing": (0, 1),
}


class StateValidationError(ValueError):
    def __init__(self, status: int = 422) -> None:
        self.status = status
        super().__init__(
            "Локальные настройки имеют недопустимый формат."
            if status != 413
            else "Локальные настройки превышают допустимый размер."
        )


def _need(condition: bool) -> None:
    if not condition:
        raise StateValidationError()


def _size(value: Any, limit: int) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise StateValidationError() from None
    if size > limit:
        raise StateValidationError(413)


def _id(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(ID.fullmatch(value)) and value not in {"__proto__", "prototype", "constructor"}
    )


def _text(value: Any, limit: int) -> bool:
    try:
        return isinstance(value, str) and len(value.encode("utf-16-le")) // 2 <= limit
    except UnicodeError:
        return False


def _number(value: Any, low: float, high: float, integer: bool = False) -> bool:
    return type(value) in ({int} if integer else {int, float}) and math.isfinite(value) and low <= value <= high


def _shape(
    value: Any, allowed: set[str] | frozenset[str], required: set[str] | frozenset[str] = frozenset()
) -> dict[str, Any]:
    _need(isinstance(value, dict) and set(value) <= allowed and required <= set(value))
    return value


def _preferences(value: Any) -> None:
    fields = (
        set(ENUMS)
        | set(BOOLS)
        | set(RANGES)
        | {"defaultTaskType", "editorFontSize", "editorTabSize", "fileCacheMb", "boardColor", "boardMarkerColor"}
    )
    for key, item in _shape(value, fields).items():
        if key in ENUMS:
            _need(isinstance(item, str) and item in ENUMS[key])
        elif key in BOOLS:
            _need(type(item) is bool)
        elif key in RANGES:
            _need(_number(item, *RANGES[key]))
        elif key in {"boardColor", "boardMarkerColor"}:
            _need(isinstance(item, str) and bool(re.fullmatch(r"#[0-9a-fA-F]{6}", item)))
        elif key == "defaultTaskType":
            _need(_number(item, 1, 27, True))
        else:
            _need(
                type(item) is int
                and item
                in {
                    "editorFontSize": {12, 14, 16},
                    "editorTabSize": {2, 4, 8},
                    "fileCacheMb": {32, 64, 128, 256, 512, 1024},
                }[key]
            )


def _filters(value: Any) -> None:
    fields = {"line", "query", "year", "difficulty", "files", "learningStatus", "bookmarksOnly"}
    enums = {
        "year": {"Все годы", "2027", "2026", "2025", "2024"},
        "difficulty": {"Все сложности", "easy", "medium", "hard"},
        "files": {"Все файлы", "С файлами", "Без файлов"},
        "learningStatus": {"", "neutral", "started", "incorrect", "correct"},
    }
    for key, item in _shape(value, fields).items():
        if key == "line":
            _need(_number(item, 1, 27, True))
        elif key == "query":
            _need(_text(item, 500))
        elif key == "bookmarksOnly":
            _need(type(item) is bool)
        else:
            _need(isinstance(item, str) and item in enums[key])


def _shortcut(value: Any, platform: str) -> None:
    if value is None:
        return
    _need(isinstance(value, str) and 0 < len(value) <= 64)
    parts = value.split("+")
    modifiers, key = parts[:-1], parts[-1]
    _need(
        len(modifiers) <= 4
        and len(set(modifiers)) == len(modifiers)
        and set(modifiers) <= {"Ctrl", "Alt", "Shift", "Cmd"}
    )
    _need(0 < len(key) <= 20 and not any(c.isspace() or ord(c) < 32 or 127 <= ord(c) < 160 for c in key))
    _need(
        key.lower()
        not in {"ctrl", "control", "ctl", "alt", "option", "opt", "shift", "cmd", "command", "meta", "win", "windows"}
    )
    aliases = {
        "esc": "Escape",
        "escape": "Escape",
        "return": "Enter",
        "enter": "Enter",
        "spacebar": "Space",
        "space": "Space",
        "left": "ArrowLeft",
        "right": "ArrowRight",
        "up": "ArrowUp",
        "down": "ArrowDown",
        "backspace": "Backspace",
        "delete": "Delete",
        "del": "Delete",
        "tab": "Tab",
        "home": "Home",
        "end": "End",
        "pageup": "PageUp",
        "pagedown": "PageDown",
        "insert": "Insert",
        "plus": "Plus",
        "minus": "Minus",
        **{x.lower(): x for x in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown")},
    }
    if key.lower() in aliases:
        _need(key == aliases[key.lower()])
    if re.fullmatch(r"key[a-z]", key, re.I):
        _need(bool(re.fullmatch(r"Key[A-Z]", key)))
    if re.fullmatch(r"(?:digit|numpad)[0-9]", key, re.I):
        _need(bool(re.fullmatch(r"(?:Digit|Numpad)[0-9]", key)))
    if re.fullmatch(r"f(?:[1-9]|1[0-2])", key, re.I):
        _need(key == key.upper())
    _need(
        not re.fullmatch(r"[a-z0-9]", key, re.I)
        and not (len(key) == 1 and key.lower() in "ёйцукенгшщзхъфывапролджэячсмитьбю")
    )
    ctrl, alt, shift, cmd = (x in modifiers for x in ("Ctrl", "Alt", "Shift", "Cmd"))
    reserved = False
    if platform == "windows":
        reserved = (
            (alt and not (ctrl or cmd or shift) and key in {"F4", "Tab"})
            or (ctrl and not (alt or cmd or shift) and key in {"Key" + c for c in "LTNWRPSFDHJKU"})
            or (ctrl and shift and not (alt or cmd) and key == "Escape")
            or (ctrl and alt and key == "Delete")
            or (cmd and not (ctrl or alt or shift) and key == "KeyL")
        )
    else:
        reserved = (
            (ctrl and alt)
            or (cmd and not (ctrl or alt or shift) and key in {"Tab", "Space", *{"Key" + c for c in "QWTNLRPSFGHJKU"}})
            or (cmd and alt and key == "Escape")
        )
    _need(not reserved)


def _runs(value: Any) -> None:
    _need(isinstance(value, list) and len(value) <= 20)
    required = {"id", "status", "stdout", "stderr", "duration_ms", "exit_code", "created_at"}
    for run in value:
        _shape(run, required | {"code_version", "reason"}, required)
        _need(
            _id(run["id"])
            and run["status"] in {"queued", "running", "succeeded", "completed", "failed", "timed_out", "unavailable"}
        )
        _need(_text(run["stdout"], 200000) and _text(run["stderr"], 200000))
        _need(run["duration_ms"] is None or _number(run["duration_ms"], 0, 1e12))
        _need(run["exit_code"] is None or _number(run["exit_code"], -2147483648, 2147483647, True))
        stamp = run["created_at"]
        _need(isinstance(stamp, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", stamp)))
        year, month, day, hour, minute, second = (
            int(stamp[a:b]) for a, b in ((0, 4), (5, 7), (8, 10), (11, 13), (14, 16), (17, 19))
        )
        _need(
            1 <= month <= 12
            and 1 <= day <= calendar.monthrange(year, month)[1]
            and hour <= 23
            and minute <= 59
            and second <= 59
        )
        if "code_version" in run:
            _need(run["code_version"] is None or _number(run["code_version"], 0, MAX_SAFE, True))
        if "reason" in run:
            _need(run["reason"] is None or _text(run["reason"], 2000))


def _validate_client_state(value: Any) -> dict[str, Any]:
    _shape(value, {"version", "entries"}, {"version", "entries"})
    _need(type(value["version"]) is int and value["version"] == 1)
    entries = value["entries"]
    _need(isinstance(entries, dict) and len(entries) <= 4096)
    for key, item in entries.items():
        _need(_text(key, 512))
        if key == "kontur:interface-preferences:v1":
            _preferences(item)
            _size(item, 16 * 1024)
        elif key in {"kontur:shortcut-profile:windows:v1", "kontur:shortcut-profile:macos:v1"}:
            for binding in _shape(item, COMMANDS).values():
                _shortcut(binding, "macos" if ":macos:" in key else "windows")
            _size(item, 16 * 1024)
        elif key in {
            "kontur:workspace-split:v3",
            "kontur:workspace-split:v2",
            "kontur:assistant-split:v1:narrow",
            "kontur:assistant-split:v1:standard",
            "kontur:assistant-split:v1:wide",
        }:
            _need(_number(item, 0, 100))
        elif key == "kontur:assistant-technical-mode:v1":
            _need(type(item) is bool)
        elif key == "kontur:ui:task-bank:filters:v1":
            _filters(item)
            _size(item, 8 * 1024)
        elif key == "kontur:ui:task-bank:return:v1":
            _shape(item, {"filterKey", "revisionId", "scrollTop"}, {"filterKey", "revisionId", "scrollTop"})
            _need(_text(item["filterKey"], 4096) and _id(item["revisionId"]) and _number(item["scrollTop"], 0, 1e9))
            _filters(json.loads(item["filterKey"]))
            _size(item, 8 * 1024)
        elif key.startswith("kontur:exam-ui:v1:"):
            _need(_id(key[len("kontur:exam-ui:v1:") :]))
            _shape(item, {"itemId", "flags", "surfaces"}, {"itemId", "flags", "surfaces"})
            _need(item["itemId"] is None or _id(item["itemId"]))
            for field in ("flags", "surfaces"):
                _need(isinstance(item[field], dict) and len(item[field]) <= 512)
                for identifier, hint in item[field].items():
                    _need(_id(identifier) and (type(hint) is bool if field == "flags" else hint in {"code", "board"}))
            _size(item, 64 * 1024)
        elif key.startswith("kontur:study-checkpoint:v1:exam:"):
            parts = key[len("kontur:study-checkpoint:v1:exam:") :].split(":item:")
            _need(len(parts) == 2 and all(_id(x) for x in parts))
            _shape(item, {"runId", "question"}, {"runId", "question"})
            _need(_id(item["runId"]) and _text(item["question"], 20000))
        elif key.startswith("kontur:device-python-runs:v1:"):
            encoded = key[len("kontur:device-python-runs:v1:") :]
            scope = unquote(encoded)
            practice_match = re.fullmatch(r"practice:(.+):browser", scope)
            exam_match = re.fullmatch(r"exam:([^:]+):([^:]+):item:([^:]+):browser", scope)
            _need(
                quote(scope, safe="-_.!~*'()") == encoded
                and (
                    (practice_match is not None and _id(practice_match.group(1)))
                    or (exam_match is not None and all(_id(identifier) for identifier in exam_match.groups()))
                )
            )
            _runs(item)
        else:
            raise StateValidationError()
    _size(value, MAX_BYTES)
    return value


def validate_client_state(value: Any) -> dict[str, Any]:
    try:
        return _validate_client_state(value)
    except StateValidationError:
        raise
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError, OverflowError):
        raise StateValidationError() from None


def install_client_state_routes(app: Any) -> None:
    from fastapi import APIRouter, Depends, HTTPException, Request
    from sqlalchemy import select

    from app.api_common import get_db, owner_session
    from app.auth import ensure_owner
    from app.models import OwnerSyncRecord, utcnow

    router = APIRouter()
    db_dependency = Depends(get_db)
    owner_dependency = Depends(owner_session)

    def get_state(session=db_dependency, principal=owner_dependency):
        record = session.get(OwnerSyncRecord, RECORD_ID)
        if record is None:
            return {"version": 0, "value": {"version": 1, "entries": {}}}
        if (
            record.owner_id != principal.owner_id
            or record.kind != "offline_client_state"
            or record.context != {"version": 1}
        ):
            raise HTTPException(
                409, {"code": "CLIENT_STATE_CONFLICT", "message": "Контекст настроек изменился.", "details": {}}
            )
        try:
            validate_client_state(record.value)
        except StateValidationError as error:
            raise HTTPException(
                error.status, {"code": "CLIENT_STATE_INVALID", "message": str(error), "details": {}}
            ) from None
        return {"version": record.version, "value": record.value}

    async def put_state(request, session=db_dependency, principal=owner_dependency):
        raw = await request.body()
        try:
            if len(raw) > MAX_BYTES:
                raise StateValidationError(413)
            payload = json.loads(raw)
            _shape(payload, {"expectedVersion", "value"}, {"expectedVersion", "value"})
            expected = payload["expectedVersion"]
            _need(_number(expected, 0, MAX_SAFE - 1, True))
            value = validate_client_state(payload["value"])
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            status = error.status if isinstance(error, StateValidationError) else 422
            raise HTTPException(
                status,
                {
                    "code": "CLIENT_STATE_TOO_LARGE" if status == 413 else "CLIENT_STATE_INVALID",
                    "message": "Локальные настройки не сохранены: проверьте формат и размер.",
                    "details": {},
                },
            ) from None
        with session.begin():
            # One writer lease prevents deferred-read/write snapshot races.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            ensure_owner(session, principal.owner_id)
            record = session.scalar(select(OwnerSyncRecord).where(OwnerSyncRecord.id == RECORD_ID))
            current = record.version if record is not None else 0
            if current != expected or (
                record is not None
                and (
                    record.owner_id != principal.owner_id
                    or record.kind != "offline_client_state"
                    or record.context != {"version": 1}
                )
            ):
                raise HTTPException(
                    409,
                    {
                        "code": "CLIENT_STATE_CONFLICT",
                        "message": "Настройки уже изменены. Загрузите сохранённую версию.",
                        "details": {"currentVersion": current},
                    },
                )
            if record is None:
                record = OwnerSyncRecord(
                    id=RECORD_ID,
                    owner_id=principal.owner_id,
                    kind="offline_client_state",
                    context={"version": 1},
                    value=value,
                    version=1,
                )
                session.add(record)
            else:
                record.value = value
                record.version += 1
                record.updated_at = utcnow()
            result = {"version": current + 1, "value": value}
        return result

    put_state.__annotations__["request"] = Request
    router.add_api_route("/api/v1/local/client-state", get_state, methods=["GET"])
    router.add_api_route("/api/v1/local/client-state", put_state, methods=["PUT"])
    app.include_router(router)
