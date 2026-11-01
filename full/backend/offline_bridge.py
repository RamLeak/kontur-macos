"""Application-owned FastAPI child over bounded JSONL, without a network server."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

# -I intentionally excludes the script/project path. Add this known resource
# directory only; no cwd, .env, user startup hooks or inherited Python paths.
BACKEND_ROOT = Path(__file__).resolve().parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from offline_protocol import MAX_FRAME_BYTES, BridgeError, GatewayCalls, JsonlWriter, invalid_request  # noqa: E402


def safe_error(error: BaseException) -> dict[str, str]:
    code = getattr(error, "code", None)
    if isinstance(error, BridgeError):
        return {"code": error.code, "message": error.message}
    # Transfer errors are a separately owned typed module. Its safe codes are
    # the only foreign messages admitted; never serialize SQL/OS/CLI exceptions.
    if isinstance(code, str) and code in {
        "invalid_data",
        "unsupported_format",
        "checksum_mismatch",
        "limit_exceeded",
        "record_conflict",
        "import_changed",
        "preview_required",
        "file_error",
        "backup_failed",
        "destination_exists",
    }:
        return {"code": code, "message": "Операция с файлом данных не выполнена."}
    return {"code": "backend_error", "message": "Локальная операция не выполнена."}


class Dispatcher:
    def __init__(
        self, application: Any, writer: JsonlWriter, gateway: GatewayCalls, *, workers: int = 4, pending: int = 16
    ) -> None:
        self.application = application
        self.writer = writer
        self.gateway = gateway
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kontur-request")
        self.condition = threading.Condition()
        self.pending_limit = pending
        self.ids: set[int] = set()
        self.active_requests = 0
        self.barrier = False
        self.closed = False
        self.futures: set[concurrent.futures.Future[Any]] = set()

    def submit(self, frame: dict[str, Any]) -> None:
        op = frame.get("op")
        if op == "gateway_result":
            allowed = {"op", "callId", "ok", "result", "error"}
            if set(frame) - allowed:
                raise invalid_request()
            self.gateway.resolve(frame)
            return
        request_id = frame.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int) or not 0 <= request_id <= 2**63 - 1:
            raise invalid_request()
        schemas = {
            "request": {"id", "op", "method", "path", "body", "headers"},
            "flush": {"id", "op"},
            "import_preview": {"id", "op", "path"},
            "import_bundle": {"id", "op", "path", "expectedSha256"},
            "export_bundle": {"id", "op", "path"},
        }
        if op not in schemas or set(frame) - schemas[op]:
            self.writer.write(
                {
                    "id": request_id,
                    "ok": False,
                    "error": {"code": "invalid_request", "message": "Некорректный локальный запрос."},
                }
            )
            return
        exclusive = op in {"flush", "import_bundle", "export_bundle"}
        with self.condition:
            if request_id in self.ids:
                raise invalid_request()
            if self.closed or self.barrier or len(self.ids) >= self.pending_limit:
                self.writer.write(
                    {
                        "id": request_id,
                        "ok": False,
                        "error": {"code": "backend_busy", "message": "Дождитесь завершения локальной операции."},
                    }
                )
                return
            self.ids.add(request_id)
            if exclusive:
                self.barrier = True
            else:
                self.active_requests += 1
            future = self.executor.submit(self._execute, frame, exclusive)
            self.futures.add(future)
        future.add_done_callback(self._done)

    def _done(self, future: concurrent.futures.Future[Any]) -> None:
        with self.condition:
            self.futures.discard(future)
            self.condition.notify_all()

    def _execute(self, frame: dict[str, Any], exclusive: bool) -> None:
        request_id = frame["id"]
        op = frame["op"]
        try:
            if exclusive:
                deadline = time.monotonic() + 180
                with self.condition:
                    while self.active_requests and not self.closed:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise BridgeError("backend_busy", "Дождитесь завершения сохранения.")
                        self.condition.wait(remaining)
                    if self.closed:
                        raise BridgeError("backend_stopped", "Локальная операция прервана.")
            if op == "request":
                result = self.application.request(
                    frame.get("method"), frame.get("path"), frame.get("body", ""), frame.get("headers")
                )
                response = {"id": request_id, "ok": True, **result}
            elif op == "flush":
                response = {"id": request_id, "ok": True, "result": self.application.flush()}
            else:
                path = frame.get("path")
                if not isinstance(path, str) or not Path(path).is_absolute() or "\0" in path:
                    raise invalid_request()
                if op == "import_preview":
                    result = self.application.transfer.preview(Path(path))
                elif op == "import_bundle":
                    expected = frame.get("expectedSha256")
                    if (
                        not isinstance(expected, str)
                        or len(expected) != 64
                        or any(c not in "0123456789abcdef" for c in expected)
                    ):
                        raise invalid_request()
                    self.application.flush()
                    result = self.application.transfer.import_bundle(Path(path), expected_sha256=expected)
                else:
                    self.application.flush()
                    result = self.application.transfer.export_bundle(Path(path))
                response = {"id": request_id, "ok": True, "result": result}
            self.writer.write(response)
        except Exception as error:
            try:
                self.writer.write({"id": request_id, "ok": False, "error": safe_error(error)})
            except (OSError, BridgeError):
                self.gateway.close()
        finally:
            with self.condition:
                self.ids.discard(request_id)
                if exclusive:
                    self.barrier = False
                else:
                    self.active_requests -= 1
                self.condition.notify_all()

    def close(self, timeout: float = 30.0) -> None:
        self.gateway.close()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
            pending = list(self.futures)
        _, unfinished = concurrent.futures.wait(pending, timeout=timeout)
        self.executor.shutdown(wait=not unfinished, cancel_futures=True)
        if unfinished:
            raise BridgeError("shutdown_pending", "Локальные операции ещё завершаются.")


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    line = stream.readline(MAX_FRAME_BYTES + 2)
    if not line:
        return None
    if len(line) > MAX_FRAME_BYTES + 1 or not line.endswith(b"\n"):
        raise invalid_request()
    try:

        def invalid_constant(_value: str) -> None:
            raise ValueError("non-finite JSON")

        frame = json.loads(line.decode("utf-8"), parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise invalid_request() from None
    if not isinstance(frame, dict):
        raise invalid_request()
    return frame


def run(profile: Path, bundle: Path, legacy_store: Path | None, input_stream: BinaryIO, output_stream: BinaryIO) -> int:
    writer = JsonlWriter(output_stream)
    gateway = GatewayCalls(writer.write)
    from offline_app import OfflineApplication

    application = OfflineApplication(profile, bundle, gateway.call, legacy_store, gateway.quiesce)
    dispatcher = Dispatcher(application, writer, gateway)
    startup = dispatcher.executor.submit(application.start)
    # Startup and the LocalCodexClient constructor perform no reverse call.
    # Capabilities are probed only after ready, on normal request workers.
    try:
        startup.result(timeout=180)
        writer.write({"event": "ready", "protocol": 1})
        while True:
            frame = read_frame(input_stream)
            if frame is None:
                break
            dispatcher.submit(frame)
        dispatcher.close()
        application.close()
        return 0
    except Exception:
        # No ready frame means startup failed. EOF/process status lets native
        # fail closed without an unknown protocol event or a raw exception dump.
        gateway.close()
        try:
            dispatcher.close()
            application.close()
        except Exception:
            pass
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--stdio", action="store_true", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--legacy-store", type=Path)
    options = parser.parse_args()
    if (
        not options.profile.is_absolute()
        or not options.bundle.is_absolute()
        or (options.legacy_store and not options.legacy_store.is_absolute())
    ):
        return 2
    output_stream = sys.stdout.buffer
    # Original domain/library print statements cannot corrupt the protocol.
    sys.stdout = sys.stderr
    return run(options.profile, options.bundle, options.legacy_store, sys.stdin.buffer, output_stream)


if __name__ == "__main__":
    raise SystemExit(main())
