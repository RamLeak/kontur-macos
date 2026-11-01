"""Bounded spreadsheet to UTF-8 tab-separated text conversion.

The converter intentionally has no I/O side effects.  Optional spreadsheet
readers are imported only for the format being converted; callers get a
structured error when the corresponding dependency is unavailable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import PurePath
from threading import Lock
from typing import Any

CONVERTER_VERSION = "type9-tsv-v3"
_CSV_LOCK = Lock()

MAX_ROWS = 200_000
MAX_COLUMNS = 256
MAX_TOTAL_CELLS = 2_000_000
MAX_ZIP_ENTRIES = 1_024
MAX_ZIP_UNCOMPRESSED = 64 * 1024 * 1024
MAX_ZIP_MEMBER = 32 * 1024 * 1024
MAX_ZIP_RATIO = 200

_SUPPORTED_EXTENSIONS = frozenset({".csv", ".xlsx", ".xls", ".ods"})
_FORMULA_TYPES = frozenset({"f", "formula"})
_XML_MEMBER_SUFFIXES = (".xml", ".rels")
_FORBIDDEN_ZIP_MARKERS = (
    b"vbaproject",
    b"vba_project",
    b"/vba/",
    b"/macros/",
    b"macroproject",
    b"oleobject",
    b"embeddedole",
    b"/embeddings/",
    b"/activex/",
    b"activex",
    b"externalreference",
    b"externallink",
)


@dataclass(frozen=True, slots=True)
class ConvertedText:
    """One canonical text export derived from one nonempty worksheet."""

    filename: str
    content: bytes
    sheet_name: str
    source_sha256: str
    converter_version: str


class SpreadsheetTextError(ValueError):
    """A safe, machine-readable conversion failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _CellBudget:
    def __init__(self) -> None:
        self.total_cells = 0

    def add(self, count: int) -> None:
        if count < 0:
            raise SpreadsheetTextError("invalid_cell_count", "cell count cannot be negative")
        self.total_cells += count
        if self.total_cells > MAX_TOTAL_CELLS:
            raise SpreadsheetTextError("cell_limit", "workbook exceeds the total cell limit")


def convert_spreadsheet(
    data: bytes,
    filename: str,
    *,
    max_input_bytes: int = 10_000_000,
    max_output_bytes: int = 10_000_000,
) -> list[ConvertedText]:
    """Convert a bounded CSV, XLSX, XLS, or ODS payload to canonical TSV.

    Formula cells are read from stored cached results only.  No network,
    Office process, subprocess, or formula evaluator is used.
    """

    _validate_bound("max_input_bytes", max_input_bytes)
    _validate_bound("max_output_bytes", max_output_bytes)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise SpreadsheetTextError("invalid_input", "spreadsheet data must be bytes")
    raw = bytes(data)
    if len(raw) > max_input_bytes:
        raise SpreadsheetTextError("input_too_large", "spreadsheet exceeds the input byte limit")
    if not isinstance(filename, str) or not filename.strip():
        raise SpreadsheetTextError("invalid_filename", "a nonempty filename is required")

    extension = PurePath(filename).suffix.lower()
    if extension not in _SUPPORTED_EXTENSIONS:
        raise SpreadsheetTextError("unsupported_extension", "only CSV, XLSX, XLS, and ODS are supported")

    budget = _CellBudget()
    if extension == ".csv":
        sheets = _read_csv(raw, budget)
    elif extension == ".xlsx":
        sheets = _read_xlsx(raw, budget)
    elif extension == ".xls":
        sheets = _read_xls(raw, budget)
    else:
        sheets = _read_ods(raw, budget)

    nonempty: list[tuple[str, list[list[str]]]] = []
    canonical_budget = _CellBudget()
    for sheet_name, rows in sheets:
        matrix = _canonicalize_rows(rows)
        if matrix is not None:
            canonical_budget.add(len(matrix) * len(matrix[0]))
            nonempty.append((sheet_name, matrix))

    source_sha256 = hashlib.sha256(raw).hexdigest()
    # Source alternatives often share a basename (9.csv/9.xlsx/9.ods).
    # Keep their Python filenames distinct even before a task is selected.
    stem = f"{_safe_stem(filename)[:220]}_{extension[1:]}"
    converted: list[ConvertedText] = []
    output_size = 0
    multiple = len(nonempty) > 1
    for index, (sheet_name, matrix) in enumerate(nonempty, start=1):
        output = _render_tsv(matrix)
        output_size += len(output)
        if output_size > max_output_bytes:
            raise SpreadsheetTextError("output_too_large", "converted text exceeds the output byte limit")
        output_name = _output_filename(stem, index if multiple else None)
        converted.append(
            ConvertedText(
                filename=output_name,
                content=output,
                sheet_name=sheet_name,
                source_sha256=source_sha256,
                converter_version=CONVERTER_VERSION,
            )
        )
    return converted


def _validate_bound(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SpreadsheetTextError("invalid_bound", f"{name} must be a nonnegative integer")


def _read_csv(data: bytes, budget: _CellBudget) -> list[tuple[str, list[list[str]]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SpreadsheetTextError("invalid_encoding", "CSV must be valid UTF-8") from exc

    with _CSV_LOCK:
        rows: list[list[str]] = []
        old_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(max(old_limit, len(data) + 1))
            delimiter = _detect_csv_delimiter(text)
            if delimiter is None:
                reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            else:
                reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            for row in reader:
                _check_row_width(len(row))
                budget.add(len(row))
                for value in row:
                    _check_text(value)
                rows.append(row)
        except csv.Error as exc:
            raise SpreadsheetTextError("invalid_csv", "CSV is malformed") from exc
        finally:
            csv.field_size_limit(old_limit)
    return [("Sheet1", rows)]


def _detect_csv_delimiter(text: str) -> str | None:
    """Select an unambiguous delimiter from the bounded CSV sample.

    A file with no consistently detected comma, semicolon, or tab delimiter is
    kept as a one-column CSV.  If a candidate would produce multiple columns
    but no candidate is unambiguous, fail closed rather than silently joining
    or dropping fields.
    """

    valid: dict[str, int] = {}
    candidate_has_multiple = False
    # Scan full, already byte-bounded records. Streaming candidates avoids
    # truncating a quoted/large field or materializing three full matrices.
    for delimiter in (",", ";", "\t"):
        width: int | None = None
        consistent = True
        row_count = 0
        try:
            for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True):
                row_count += 1
                if row_count > MAX_ROWS:
                    raise SpreadsheetTextError("row_limit", "CSV exceeds the row limit")
                if not row:
                    continue
                current = len(row)
                candidate_has_multiple |= current > 1
                if width is None:
                    width = current
                elif width != current:
                    consistent = False
                if current > MAX_COLUMNS:
                    consistent = False
            if consistent and width is not None and width > 1:
                valid[delimiter] = width
        except csv.Error:
            continue

    if len(valid) == 1:
        return next(iter(valid))
    if len(valid) > 1:
        raise SpreadsheetTextError("ambiguous_csv", "CSV delimiter is ambiguous")
    if candidate_has_multiple:
        raise SpreadsheetTextError("ambiguous_csv", "CSV delimiter is inconsistent")
    return None


def _read_xlsx(data: bytes, budget: _CellBudget) -> list[tuple[str, list[list[str]]]]:
    _preflight_zip(data, kind="xlsx")
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise SpreadsheetTextError("dependency_unavailable", "XLSX conversion requires openpyxl") from exc

    from openpyxl.utils.exceptions import InvalidFileException

    workbook = None
    cached_workbook = None
    try:
        try:
            workbook = load_workbook(
                filename=io.BytesIO(data),
                read_only=True,
                data_only=False,
                keep_links=False,
            )
            cached_workbook = load_workbook(
                filename=io.BytesIO(data),
                read_only=True,
                data_only=True,
                keep_links=False,
            )
        except (InvalidFileException, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            raise SpreadsheetTextError("invalid_workbook", "XLSX workbook could not be read") from exc

        if len(workbook.worksheets) != len(cached_workbook.worksheets):
            raise SpreadsheetTextError("invalid_workbook", "XLSX worksheet views differ")
        result: list[tuple[str, list[list[str]]]] = []
        for worksheet, cached_sheet in zip(workbook.worksheets, cached_workbook.worksheets, strict=True):
            # Some generators understate <dimension>; actual streamed cells
            # have passed reference/budget checks before any row allocation.
            worksheet.reset_dimensions()
            cached_sheet.reset_dimensions()
            rows: list[list[str]] = []
            rows_iter = worksheet.iter_rows(min_row=1, min_col=1)
            cached_iter = cached_sheet.iter_rows(min_row=1, min_col=1)
            for index, (row, cached_row) in enumerate(zip(rows_iter, cached_iter, strict=True), start=1):
                _check_dimensions(index, len(row), budget, count_product=True)
                if len(row) != len(cached_row):
                    raise SpreadsheetTextError("invalid_workbook", "XLSX row shape changed while reading")
                values: list[str] = []
                for cell, cached_cell in zip(row, cached_row, strict=True):
                    if cell.data_type in _FORMULA_TYPES:
                        if cached_cell.value is None:
                            raise SpreadsheetTextError("formula_no_cache", "XLSX formula has no stored cached result")
                        values.append(_format_openpyxl_cell(cached_cell))
                    elif cell.data_type == "e":
                        raise SpreadsheetTextError("unsupported_error", "XLSX error cells are unsupported")
                    else:
                        values.append(_format_openpyxl_cell(cell))
                budget.add(len(values))
                rows.append(values)
            result.append((str(worksheet.title), rows))
        return result
    except SpreadsheetTextError:
        raise
    except Exception as exc:
        raise SpreadsheetTextError("invalid_workbook", "XLSX workbook could not be converted") from exc
    finally:
        if workbook is not None:
            workbook.close()
        if cached_workbook is not None:
            cached_workbook.close()


def _read_xls(data: bytes, budget: _CellBudget) -> list[tuple[str, list[list[str]]]]:
    if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise SpreadsheetTextError("invalid_xls_signature", "XLS must be an OLE compound document")
    try:
        from xlrd import (
            XL_CELL_BLANK,
            XL_CELL_BOOLEAN,
            XL_CELL_DATE,
            XL_CELL_EMPTY,
            XL_CELL_ERROR,
            XL_CELL_NUMBER,
            XL_CELL_TEXT,
            open_workbook,
        )
        from xlrd.compdoc import CompDoc
    except ImportError as exc:
        raise SpreadsheetTextError("dependency_unavailable", "XLS conversion requires xlrd") from exc

    directory = None
    book = None
    try:
        try:
            directory = CompDoc(data, logfile=io.StringIO(), DEBUG=0)
        except Exception as exc:
            # ``CompDoc`` has historically raised several built-in exceptions
            # for malformed directory chains; all are fail-closed here.
            raise SpreadsheetTextError("invalid_xls_container", "XLS compound directory is invalid") from exc
        _validate_xls_directory(directory)
        try:
            book = open_workbook(
                file_contents=data,
                on_demand=False,
                formatting_info=False,
                ragged_rows=False,
                logfile=io.StringIO(),
            )
        except Exception as exc:
            raise SpreadsheetTextError("invalid_workbook", "XLS workbook could not be read") from exc

        result: list[tuple[str, list[list[str]]]] = []
        for sheet in book.sheets():
            _check_dimensions(sheet.nrows, sheet.ncols, budget, count_product=True)
            rows: list[list[str]] = []
            for row_index in range(sheet.nrows):
                values: list[str] = []
                for column_index in range(sheet.ncols):
                    cell = sheet.cell(row_index, column_index)
                    if cell.ctype in (XL_CELL_EMPTY, XL_CELL_BLANK):
                        values.append("")
                    elif cell.ctype == XL_CELL_TEXT:
                        values.append(_format_scalar(cell.value))
                    elif cell.ctype == XL_CELL_NUMBER:
                        values.append(_format_scalar(cell.value))
                    elif cell.ctype == XL_CELL_BOOLEAN:
                        values.append("True" if bool(cell.value) else "False")
                    elif cell.ctype == XL_CELL_DATE:
                        raise SpreadsheetTextError("unsupported_date", "XLS date cells are unsupported")
                    elif cell.ctype == XL_CELL_ERROR:
                        raise SpreadsheetTextError("unsupported_error", "XLS error cells are unsupported")
                    else:
                        raise SpreadsheetTextError("unsupported_cell", "XLS cell type is unsupported")
                budget.add(len(values))
                rows.append(values)
            result.append((str(sheet.name), rows))
        return result
    finally:
        if book is not None:
            book.release_resources()


def _validate_xls_directory(directory: Any) -> None:
    """Allow only the workbook and standard metadata streams in an XLS OLE file."""

    allowed_streams = {
        "workbook",
        "book",
        "summaryinformation",
        "documentsummaryinformation",
        "compobj",
    }
    found_workbook = False
    for node in directory.dirlist:
        name = str(node.name)
        normalized = name.lstrip("\x01\x05").lower()
        marker = normalized.replace(" ", "")
        if any(token in marker for token in ("vba", "macro", "embedding", "objectpool", "activex", "encryption")):
            raise SpreadsheetTextError("xls_embedded_content", "XLS macros or embedded content are unsupported")
        if node.etype == 5:
            continue
        if node.etype == 0:
            continue
        if node.etype == 1:
            raise SpreadsheetTextError("xls_unknown_storage", "XLS contains an unsupported compound storage")
        if node.etype != 2:
            raise SpreadsheetTextError("xls_unknown_stream", "XLS contains an unsupported compound entry")
        if normalized not in allowed_streams:
            raise SpreadsheetTextError("xls_unknown_stream", "XLS contains an unsupported compound stream")
        if normalized in {"workbook", "book"}:
            found_workbook = True
    if not found_workbook:
        raise SpreadsheetTextError("xls_missing_workbook", "XLS workbook stream is missing")


def _read_ods(data: bytes, budget: _CellBudget) -> list[tuple[str, list[list[str]]]]:
    _preflight_zip(data, kind="ods")
    try:
        from odf import opendocument, table, teletype
    except ImportError as exc:
        raise SpreadsheetTextError("dependency_unavailable", "ODS conversion requires odfpy") from exc

    try:
        document = opendocument.load(io.BytesIO(data))
    except Exception as exc:
        raise SpreadsheetTextError("invalid_workbook", "ODS workbook could not be read") from exc

    table_namespace = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
    table_qname = (table_namespace, "table")
    cell_qname = (table_namespace, "table-cell")
    covered_cell_qname = (table_namespace, "covered-table-cell")
    try:
        tables = [node for node in document.spreadsheet.childNodes if getattr(node, "qname", None) == table_qname]
    except Exception as exc:
        raise SpreadsheetTextError("invalid_workbook", "ODS spreadsheet body is invalid") from exc

    result: list[tuple[str, list[list[str]]]] = []
    for index, sheet in enumerate(tables, start=1):
        sheet_name = str(sheet.getAttribute("name") or f"Sheet{index}")
        rows: list[list[str]] = []
        pending_blank_rows: list[tuple[int, list[str]]] = []
        pending_blank_count = 0
        try:
            row_nodes = sheet.getElementsByType(table.TableRow)
            for row_node in row_nodes:
                row_repeat = _ods_repeat(row_node.getAttribute("numberrowsrepeated"), "row")
                row_values: list[str] = []
                pending_blank_columns = 0
                for cell in row_node.childNodes:
                    qname = getattr(cell, "qname", None)
                    if qname not in {cell_qname, covered_cell_qname}:
                        continue
                    cell_repeat = _ods_repeat(
                        cell.getAttribute("numbercolumnsrepeated"),
                        "column",
                    )
                    cell_value = "" if qname == covered_cell_qname else _format_ods_cell(cell, teletype)
                    if cell_value == "":
                        # LibreOffice writes a formatted empty tail out to the
                        # sheet's full column count.  Retain its position only
                        # if another nonempty cell follows, never expand it.
                        pending_blank_columns = min(MAX_COLUMNS + 1, pending_blank_columns + cell_repeat)
                        continue
                    if len(row_values) + pending_blank_columns + cell_repeat > MAX_COLUMNS:
                        raise SpreadsheetTextError("column_limit", "ODS sheet exceeds the column limit")
                    row_values.extend([""] * pending_blank_columns)
                    pending_blank_columns = 0
                    row_values.extend([cell_value] * cell_repeat)
                _check_row_width(len(row_values))
                if not any(row_values):
                    # Huge repeated blank ranges are common at the end of ODS
                    # sheets.  Defer them so canonical trailing-empty trimming
                    # does not expand an unbounded range in memory.  If a
                    # later nonempty row appears, the full range is still
                    # counted and rejected when it exceeds the row bound.
                    pending_blank_rows.append((row_repeat, row_values))
                    pending_blank_count += row_repeat
                    continue
                if len(rows) + pending_blank_count + row_repeat > MAX_ROWS:
                    raise SpreadsheetTextError("row_limit", "ODS sheet exceeds the row limit")
                for repeat, blank_row in pending_blank_rows:
                    budget.add(len(blank_row) * repeat)
                    rows.extend([blank_row.copy() for _ in range(repeat)])
                pending_blank_rows.clear()
                pending_blank_count = 0
                budget.add(len(row_values) * row_repeat)
                rows.extend([row_values.copy() for _ in range(row_repeat)])
        except SpreadsheetTextError:
            raise
        except Exception as exc:
            raise SpreadsheetTextError("invalid_workbook", "ODS sheet could not be converted") from exc
        result.append((sheet_name, rows))
    return result


def _ods_repeat(value: Any, axis: str) -> int:
    if value in (None, ""):
        return 1
    try:
        repeat = int(value)
    except (TypeError, ValueError) as exc:
        raise SpreadsheetTextError("invalid_repeat", f"ODS {axis} repeat is invalid") from exc
    if repeat < 1:
        raise SpreadsheetTextError("invalid_repeat", f"ODS {axis} repeat must be positive")
    return repeat


def _format_ods_cell(cell: Any, teletype: Any) -> str:
    value_type = cell.getAttribute("valuetype")
    formula = cell.getAttribute("formula")
    if formula and not value_type:
        raise SpreadsheetTextError("formula_no_cache", "ODS formula has no stored cached result")
    if value_type in (None, ""):
        return ""
    if value_type == "string":
        return _format_scalar(teletype.extractText(cell))
    if value_type == "boolean":
        raw = cell.getAttribute("booleanvalue")
        if raw is None:
            code = "formula_no_cache" if formula else "invalid_cell"
            raise SpreadsheetTextError(code, "ODS boolean value is missing")
        if str(raw).lower() == "true":
            return "True"
        if str(raw).lower() == "false":
            return "False"
        raise SpreadsheetTextError("invalid_cell", "ODS boolean value is invalid")
    if value_type in {"float", "percentage", "currency"}:
        raw = cell.getAttribute("value")
        if raw in (None, ""):
            code = "formula_no_cache" if formula else "invalid_cell"
            raise SpreadsheetTextError(code, "ODS numeric value is missing")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise SpreadsheetTextError("invalid_cell", "ODS numeric value is invalid") from exc
        return _format_scalar(number)
    if value_type in {"date", "time"}:
        raise SpreadsheetTextError("unsupported_date", "ODS date/time cells are unsupported")
    if value_type == "error":
        raise SpreadsheetTextError("unsupported_error", "ODS error cells are unsupported")
    raise SpreadsheetTextError("unsupported_cell", "ODS cell type is unsupported")


def _preflight_zip(data: bytes, *, kind: str) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SpreadsheetTextError("invalid_container", f"{kind.upper()} ZIP container is invalid") from exc

    try:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise SpreadsheetTextError("zip_entry_limit", "ZIP contains too many members")
        seen: set[str] = set()
        total_size = 0
        for info in infos:
            name = _validate_zip_name(info.filename, seen)
            if info.flag_bits & 0x1:
                raise SpreadsheetTextError("zip_encrypted", "encrypted ZIP members are unsupported")
            if info.file_size > MAX_ZIP_MEMBER:
                raise SpreadsheetTextError("zip_member_limit", "ZIP member exceeds the per-member limit")
            total_size += info.file_size
            if total_size > MAX_ZIP_UNCOMPRESSED:
                raise SpreadsheetTextError("zip_size_limit", "ZIP exceeds the uncompressed size limit")
            if info.file_size and info.compress_size == 0:
                raise SpreadsheetTextError("zip_ratio_limit", "ZIP member compression ratio is unsafe")
            if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
                raise SpreadsheetTextError("zip_ratio_limit", "ZIP member compression ratio is unsafe")
            lowered_name = name.lower().encode("utf-8", "ignore")
            if any(marker in lowered_name for marker in _FORBIDDEN_ZIP_MARKERS):
                raise SpreadsheetTextError("zip_forbidden_member", "ZIP contains unsupported embedded content")
            if name.lower().endswith(_XML_MEMBER_SUFFIXES):
                payload = archive.read(info)
                _scan_xml_member(payload, name, kind=kind)
    except SpreadsheetTextError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SpreadsheetTextError("invalid_container", f"{kind.upper()} ZIP container is invalid") from exc
    finally:
        archive.close()


def _validate_zip_name(raw_name: str, seen: set[str]) -> str:
    if not raw_name or "\x00" in raw_name or "\\" in raw_name:
        raise SpreadsheetTextError("zip_path", "ZIP member path is invalid")
    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise SpreadsheetTextError("zip_path", "ZIP member path is absolute")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts[:-1]) or ".." in parts:
        raise SpreadsheetTextError("zip_path", "ZIP member path traverses directories")
    key = normalized.casefold()
    if key in seen:
        raise SpreadsheetTextError("zip_duplicate", "ZIP contains duplicate member paths")
    seen.add(key)
    return normalized


def _scan_xml_member(payload: bytes, name: str, *, kind: str) -> None:
    # Inspect XML declarations through a real hardened parser before readers
    # see them. Also bound actual XLSX cell references, not declared dimension.
    _check_xml_references(payload)
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered or b"<![" in lowered:
        raise SpreadsheetTextError("zip_xml_unsafe", "XML DTD and entity declarations are unsupported")
    lowered_name = name.lower()
    if lowered_name.endswith(".rels"):
        if (
            re.search(rb"targetmode\s*=\s*['\"]external['\"]", lowered) is not None
            or re.search(rb"target\s*=\s*['\"](?:https?|file):", lowered) is not None
        ):
            raise SpreadsheetTextError("zip_external_relationship", "external relationships are unsupported")
    if kind == "ods" and re.search(rb"(?:xlink:)?href\s*=\s*['\"](?:https?|file):", lowered):
        raise SpreadsheetTextError("zip_external_relationship", "external relationships are unsupported")
    if any(marker in lowered for marker in _FORBIDDEN_ZIP_MARKERS):
        raise SpreadsheetTextError("zip_forbidden_member", "XML contains unsupported embedded content")


def _check_xml_references(payload: bytes) -> None:
    from defusedxml.ElementTree import iterparse

    worksheet = False
    row_number = 0
    column_number = 0
    max_column = 0
    try:
        for event, element in iterparse(io.BytesIO(payload), events=("start", "end"),
                                        forbid_dtd=True, forbid_entities=True, forbid_external=True):
            tag = element.tag.rsplit("}", 1)[-1]
            if event == "start":
                if tag == "worksheet":
                    worksheet = True
                elif worksheet and tag == "row":
                    raw = element.get("r")
                    next_row = int(raw) if raw is not None else row_number + 1
                    if next_row <= row_number or next_row > MAX_ROWS:
                        raise SpreadsheetTextError("row_limit", "XLSX row references are invalid or exceed bounds")
                    row_number = next_row
                    column_number = 0
                elif worksheet and tag == "c":
                    raw = element.get("r")
                    if raw is None:
                        next_column = column_number + 1
                    else:
                        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,5})", raw.upper())
                        if not match or int(match[2]) != row_number:
                            raise SpreadsheetTextError("invalid_cell", "XLSX cell reference is invalid")
                        next_column = 0
                        for letter in match[1]:
                            next_column = next_column * 26 + ord(letter) - ord("A") + 1
                    if next_column <= column_number:
                        raise SpreadsheetTextError("invalid_cell", "XLSX duplicate or unordered cell reference")
                    column_number = next_column
                    max_column = max(max_column, column_number)
                    _check_dimensions(row_number, max_column, _CellBudget(), count_product=True)
            else:
                element.clear()
    except SpreadsheetTextError:
        raise
    except Exception as exc:
        raise SpreadsheetTextError("zip_xml_unsafe", "XML is invalid or contains unsafe declarations") from exc


def _check_dimensions(rows: int, columns: int, budget: _CellBudget, *, count_product: bool) -> None:
    if rows < 0 or columns < 0:
        raise SpreadsheetTextError("invalid_dimensions", "sheet dimensions are invalid")
    if rows > MAX_ROWS:
        raise SpreadsheetTextError("row_limit", "sheet exceeds the row limit")
    if columns > MAX_COLUMNS:
        raise SpreadsheetTextError("column_limit", "sheet exceeds the column limit")
    if count_product and rows and columns:
        product = rows * columns
        if product > MAX_TOTAL_CELLS:
            raise SpreadsheetTextError("cell_limit", "sheet exceeds the total cell limit")


def _check_row_width(width: int) -> None:
    if width > MAX_COLUMNS:
        raise SpreadsheetTextError("column_limit", "sheet exceeds the column limit")


def _canonicalize_rows(rows: Sequence[Sequence[str]]) -> list[list[str]] | None:
    if not rows:
        return None
    last_row = -1
    last_column = -1
    normalized: list[list[str]] = []
    for row_index, row in enumerate(rows):
        _check_row_width(len(row))
        values = ["" if value is None else str(value) for value in row]
        normalized.append(values)
        for column_index, value in enumerate(values):
            if value != "":
                last_row = max(last_row, row_index)
                last_column = max(last_column, column_index)
    if last_row < 0 or last_column < 0:
        return None
    output_rows = last_row + 1
    output_columns = last_column + 1
    if output_rows > MAX_ROWS or output_columns > MAX_COLUMNS or output_rows * output_columns > MAX_TOTAL_CELLS:
        raise SpreadsheetTextError("cell_limit", "canonical sheet exceeds the conversion limits")
    return [row[:output_columns] + [""] * (output_columns - len(row)) for row in normalized[:output_rows]]


def _render_tsv(rows: Sequence[Sequence[str]]) -> bytes:
    return ("\n".join("\t".join(row) for row in rows) + "\n").encode("utf-8")


def _format_openpyxl_cell(cell: Any) -> str:
    if cell.value is None:
        return ""
    if getattr(cell, "data_type", None) == "e":
        raise SpreadsheetTextError("unsupported_error", "XLSX error cells are unsupported")
    if getattr(cell, "is_date", False) or isinstance(cell.value, (datetime, date, time)):
        raise SpreadsheetTextError("unsupported_date", "XLSX date/time cells are unsupported")
    return _format_scalar(cell.value)


def _format_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SpreadsheetTextError("unsupported_number", "non-finite numeric cells are unsupported")
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    if isinstance(value, (datetime, date, time)):
        raise SpreadsheetTextError("unsupported_date", "date/time cells are unsupported")
    if isinstance(value, str):
        _check_text(value)
        return value
    raise SpreadsheetTextError("unsupported_cell", "cell value type is unsupported")


def _check_text(value: str) -> None:
    if "\t" in value or "\n" in value or "\r" in value:
        raise SpreadsheetTextError("cell_text_control", "cell strings may not contain tabs or newlines")


def _safe_stem(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip(" .")
    return safe or "spreadsheet"


def _output_filename(stem: str, sheet_index: int | None) -> str:
    suffix = ".txt" if sheet_index is None else f"_sheet-{sheet_index}.txt"
    available = max(1, 255 - len(suffix))
    return f"{stem[:available]}{suffix}"
