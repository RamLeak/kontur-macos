"""Windows DPAPI-backed storage for offline AI API keys.

Only the new provider settings path owns this store.  It never reads existing
OpenCode/Codex auth files, environment files, browser cookies, or the app
database.  A caller supplies an explicit path outside transfer containers,
SQLite files, and client state.  The plaintext API key exists only in memory
between the UI call to :meth:`SecretStore.put` and DPAPI encryption/decryption.
"""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol

MAX_SECRET_BYTES = 4_096
MAX_SECRET_ENTRIES = 64
_FORMAT = "kontur-offline-ai-secrets-v1"
_SAFE_REF = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
_FORBIDDEN_COMPONENTS = frozenset({".kontur", "export", "sqlite", "clientstate"})
_FORBIDDEN_SUFFIXES = frozenset({".kontur", ".sqlite", ".sqlite3", ".db"})


class SecretStoreError(RuntimeError):
    code = "AI_SECRET_STORE_ERROR"


class SecretStoreUnavailable(SecretStoreError):
    code = "AI_SECRET_STORE_UNAVAILABLE"


class SecretProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


def _safe_ref(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(char not in _SAFE_REF for char in value)
        or value.startswith(".")
    ):
        raise SecretStoreError("API key reference is invalid")
    return value


def validate_secret_path(path: Path | str) -> Path:
    """Reject ambiguous paths that could place secrets in app data/state."""

    candidate = Path(path)
    if not candidate.is_absolute() or "\x00" in str(candidate):
        raise SecretStoreError("API key store path must be absolute")
    resolved = Path(os.path.abspath(candidate))
    components = {part.casefold() for part in resolved.parts}
    if components & _FORBIDDEN_COMPONENTS or resolved.suffix.casefold() in _FORBIDDEN_SUFFIXES:
        raise SecretStoreError("API key store must be outside transfer, SQLite, and client-state paths")
    if resolved.name.casefold() in {"export", "clientstate", "secrets.json"}:
        raise SecretStoreError("API key store path is not allowed")
    if not resolved.name.casefold().endswith((".dpapi", ".dpapi.json", ".secret")):
        raise SecretStoreError("API key store must use a dedicated encrypted-file suffix")
    return resolved


class WindowsDPAPI:
    """Minimal stdlib ctypes wrapper for CryptProtectData."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    @staticmethod
    def _api() -> Any:
        if os.name != "nt":
            raise SecretStoreUnavailable("Windows DPAPI is unavailable on this platform")
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            protect = crypt32.CryptProtectData
            unprotect = crypt32.CryptUnprotectData
            free = kernel32.LocalFree
            blob_pointer = ctypes.POINTER(WindowsDPAPI._Blob)
            protect.argtypes = [
                blob_pointer,
                wintypes.LPCWSTR,
                blob_pointer,
                wintypes.LPVOID,
                ctypes.POINTER(WindowsDPAPI._Blob),
                wintypes.DWORD,
                blob_pointer,
            ]
            protect.restype = wintypes.BOOL
            unprotect.argtypes = [
                blob_pointer,
                ctypes.POINTER(wintypes.LPWSTR),
                blob_pointer,
                wintypes.LPVOID,
                ctypes.POINTER(WindowsDPAPI._Blob),
                wintypes.DWORD,
                blob_pointer,
            ]
            unprotect.restype = wintypes.BOOL
            free.argtypes = [wintypes.HLOCAL]
            free.restype = wintypes.HLOCAL
        except (AttributeError, OSError) as exc:
            raise SecretStoreUnavailable("Windows DPAPI is unavailable") from exc
        return protect, unprotect, free

    @classmethod
    def _transform(cls, value: bytes, *, decrypt: bool) -> bytes:
        if not isinstance(value, bytes) or not value or len(value) > MAX_SECRET_BYTES * MAX_SECRET_ENTRIES * 2:
            raise SecretStoreError("encrypted secret payload is invalid")
        protect, unprotect, free = cls._api()
        source_buffer = ctypes.create_string_buffer(value)
        source = cls._Blob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char)))
        output = cls._Blob()
        description = ctypes.c_wchar_p("Kontur Offline AI provider key")
        if decrypt:
            ok = unprotect(
                ctypes.byref(source),
                None,
                None,
                None,
                None,
                cls._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )
        else:
            ok = protect(
                ctypes.byref(source),
                description,
                None,
                None,
                None,
                cls._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )
        if not ok or not output.pbData or not output.cbData:
            raise SecretStoreError("Windows DPAPI operation failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            free(ctypes.cast(output.pbData, ctypes.c_void_p))

    def protect(self, value: bytes) -> bytes:
        return self._transform(value, decrypt=False)

    def unprotect(self, value: bytes) -> bytes:
        return self._transform(value, decrypt=True)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class SecretStore:
    """Encrypted mapping of opaque provider references to API keys."""

    def __init__(self, path: Path | str, *, protector: SecretProtector | None = None) -> None:
        self.path = validate_secret_path(path)
        self._protector = protector or WindowsDPAPI()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            maximum = MAX_SECRET_BYTES * MAX_SECRET_ENTRIES * 2
            with self.path.open("rb") as source:
                encrypted = source.read(maximum + 1)
            if len(encrypted) > maximum:
                raise SecretStoreError("API key store is too large")
            plain = self._protector.unprotect(encrypted)
            value = json.loads(plain.decode("utf-8"))
        except Exception as exc:
            if isinstance(exc, SecretStoreError):
                raise
            raise SecretStoreError("API key store could not be opened") from exc
        if (
            not isinstance(value, Mapping)
            or value.get("format") != _FORMAT
            or not isinstance(value.get("entries"), Mapping)
        ):
            raise SecretStoreError("API key store format is invalid")
        entries: dict[str, str] = {}
        for key, encoded in value["entries"].items():
            reference = _safe_ref(key)
            if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > MAX_SECRET_BYTES:
                raise SecretStoreError("API key store entry is invalid")
            secret = encoded
            if not secret or len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
                raise SecretStoreError("API key store entry exceeds the byte limit")
            entries[reference] = secret
        if len(entries) > MAX_SECRET_ENTRIES:
            raise SecretStoreError("API key store contains too many entries")
        return entries

    def _write(self, entries: Mapping[str, str]) -> None:
        if len(entries) > MAX_SECRET_ENTRIES:
            raise SecretStoreError("API key store contains too many entries")
        normalized: dict[str, str] = {}
        for key, secret in entries.items():
            reference = _safe_ref(key)
            if (
                not isinstance(secret, str)
                or not secret
                or "\x00" in secret
                or any(ord(char) < 32 or ord(char) == 127 for char in secret)
                or len(secret.encode("utf-8")) > MAX_SECRET_BYTES
            ):
                raise SecretStoreError("API key is invalid")
            normalized[reference] = secret
        plain = json.dumps(
            {"format": _FORMAT, "entries": normalized},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = self._protector.protect(plain)
        if (
            not isinstance(encrypted, bytes)
            or not encrypted
            or len(encrypted) > MAX_SECRET_BYTES * MAX_SECRET_ENTRIES * 2
        ):
            raise SecretStoreError("encrypted API key store is invalid")
        _atomic_write(self.path, encrypted)

    def put(self, reference: str, secret: str) -> None:
        reference = _safe_ref(reference)
        if (
            not isinstance(secret, str)
            or not secret
            or "\x00" in secret
            or any(ord(char) < 32 or ord(char) == 127 for char in secret)
            or len(secret.encode("utf-8")) > MAX_SECRET_BYTES
        ):
            raise SecretStoreError("API key is invalid")
        entries = self._read()
        entries[reference] = secret
        self._write(entries)

    def get(self, reference: str) -> str:
        reference = _safe_ref(reference)
        try:
            return self._read()[reference]
        except KeyError as exc:
            raise SecretStoreError("API key is not configured") from exc

    def delete(self, reference: str) -> None:
        reference = _safe_ref(reference)
        entries = self._read()
        if reference in entries:
            del entries[reference]
            if entries:
                self._write(entries)
            else:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError as exc:
                    raise SecretStoreError("API key store could not be removed") from exc

    def has(self, reference: str) -> bool:
        return _safe_ref(reference) in self._read()

    def references(self) -> tuple[str, ...]:
        return tuple(sorted(self._read()))


__all__ = [
    "MAX_SECRET_BYTES",
    "MAX_SECRET_ENTRIES",
    "SecretProtector",
    "SecretStore",
    "SecretStoreError",
    "SecretStoreUnavailable",
    "WindowsDPAPI",
    "validate_secret_path",
]
