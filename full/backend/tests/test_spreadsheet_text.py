from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest

from app.spreadsheet_text import (
    CONVERTER_VERSION,
    SpreadsheetTextError,
    convert_spreadsheet,
)


def _xlsx_bytes(
    sheets: list[tuple[str, str, str | None]],
    *,
    external_relationship: bool = False,
    extra_members: dict[str, bytes] | None = None,
) -> bytes:
    content_types = [
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        (
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        ),
        '<Default Extension="xml" ContentType="application/xml"/>',
        (
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        ),
    ]
    root_rels = [
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
        (
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
        ),
        "</Relationships>",
    ]
    workbook_rels = [
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    workbook_sheets: list[str] = []
    members: dict[str, bytes] = {
        "[Content_Types].xml": b"",
        "_rels/.rels": "".join(root_rels).encode(),
    }
    for index, (name, sheet_xml, state) in enumerate(sheets, start=1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        workbook_rels.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        state_attr = f' state="{state}"' if state else ""
        workbook_sheets.append(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"{state_attr}/>')
        members[f"xl/worksheets/sheet{index}.xml"] = sheet_xml.encode()
    workbook_rels.append("</Relationships>")
    content_types.append(
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    )
    content_types.append("</Types>")
    members["[Content_Types].xml"] = "".join(content_types).encode()
    members["xl/_rels/workbook.xml.rels"] = "".join(workbook_rels).encode()
    members["xl/workbook.xml"] = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(workbook_sheets)}</sheets></workbook>"
    ).encode()
    if external_relationship:
        members["xl/worksheets/_rels/sheet1.xml.rels"] = (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId9" TargetMode="External" Target="https://example.invalid/book.xlsx" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>'
            b"</Relationships>"
        )
    if extra_members:
        members.update(extra_members)
    result = BytesIO()
    with ZipFile(result, "w", compression=ZIP_STORED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return result.getvalue()


def _sheet_xml(rows: list[str], *, dimension: str = "A1:C3") -> str:
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/><sheetData>{"".join(rows)}</sheetData></worksheet>'
    )


def _ods_bytes(tables: list[str]) -> bytes:
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">'
        '<manifest:file-entry manifest:full-path="/" '
        'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '</manifest:manifest>'
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content office:version="1.2" '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        f'<office:body><office:spreadsheet>{"".join(tables)}</office:spreadsheet></office:body>'
        '</office:document-content>'
    )
    result = BytesIO()
    with ZipFile(result, "w", compression=ZIP_STORED) as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("META-INF/manifest.xml", manifest)
        archive.writestr("content.xml", content)
    return result.getvalue()


def _ods_table(name: str, rows: str) -> str:
    return f'<table:table table:name="{name}">{rows}</table:table>'


def test_csv_preserves_interior_blanks_and_exports_metadata() -> None:
    source = b"first,second,third\n\n,left,\nlast,,value\n"

    converted = convert_spreadsheet(source, "lesson input.csv")

    assert len(converted) == 1
    export = converted[0]
    assert export.filename == "lesson_input_csv.txt"
    assert export.sheet_name == "Sheet1"
    assert export.content == b"first\tsecond\tthird\n\t\t\n\tleft\t\nlast\t\tvalue\n"
    assert not export.content.startswith(b"\xef\xbb\xbf")
    assert export.source_sha256 == hashlib.sha256(source).hexdigest()
    assert export.converter_version == CONVERTER_VERSION


def test_csv_rejects_tab_or_newline_inside_a_cell() -> None:
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(b"name,notes\nAlice,\"line one\nline two\"\n", "notes.csv")
    assert caught.value.code == "cell_text_control"

    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(b"name,notes\nAlice,\"a\tb\"\n", "notes.csv")
    assert caught.value.code == "cell_text_control"


@pytest.mark.parametrize(
    ("delimiter", "expected"),
    [
        (";", b"1\t2\t3\n4\t5\t6\n"),
        ("\t", b"1\t2\t3\n4\t5\t6\n"),
    ],
)
def test_csv_detects_semicolon_and_tab_delimiters(delimiter: str, expected: bytes) -> None:
    source = f"1{delimiter}2{delimiter}3\n4{delimiter}5{delimiter}6\n".encode()
    assert convert_spreadsheet(source, "delimited.csv")[0].content == expected


def test_csv_keeps_whitespace_only_numeric_rows_as_one_column() -> None:
    source = b"1 2 3\n4 5 6\n"
    assert convert_spreadsheet(source, "space.csv")[0].content == source


def test_csv_rejects_ambiguous_multiple_delimiters() -> None:
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(b"1,2;3,4\n5,6;7,8\n", "ambiguous.csv")
    assert caught.value.code == "ambiguous_csv"


def test_csv_enforces_input_and_output_bounds() -> None:
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(b"a\n", "small.csv", max_input_bytes=0)
    assert caught.value.code == "input_too_large"

    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(b"abcdef\n", "small.csv", max_output_bytes=3)
    assert caught.value.code == "output_too_large"


def test_xlsx_preserves_order_hidden_sheet_and_typed_values() -> None:
    sheet_one = _sheet_xml(
        [
            '<row r="1"><c r="A1" t="inlineStr"><is><t>alpha</t></is></c>'
            '<c r="C1"><v>1.2345678901234567</v></c></row>',
            '<row r="2"></row>',
            '<row r="3"><c r="B3" t="b"><v>1</v></c></row>',
        ]
    )
    sheet_two = _sheet_xml(
        ['<row r="1"><c r="A1" t="inlineStr"><is><t>hidden</t></is></c></row>'],
        dimension="A1",
    )
    converted = convert_spreadsheet(
        _xlsx_bytes([("Data", sheet_one, None), ("Hidden", sheet_two, "hidden")]),
        "Book.xlsx",
    )

    assert [item.filename for item in converted] == ["Book_xlsx_sheet-1.txt", "Book_xlsx_sheet-2.txt"]
    assert [item.sheet_name for item in converted] == ["Data", "Hidden"]
    assert converted[0].content == b"alpha\t\t1.23456789012346\n\t\t\n\tTrue\t\n"
    assert converted[1].content == b"hidden\n"


def test_xlsx_formula_requires_cached_result() -> None:
    with_cache = _sheet_xml(['<row r="1"><c r="A1"><f>1+1</f><v>2</v></c></row>'], dimension="A1")
    converted = convert_spreadsheet(_xlsx_bytes([("Calc", with_cache, None)]), "calc.xlsx")
    assert converted[0].content == b"2\n"

    without_cache = _sheet_xml(['<row r="1"><c r="A1"><f>1+1</f></c></row>'], dimension="A1")
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(_xlsx_bytes([("Calc", without_cache, None)]), "calc.xlsx")
    assert caught.value.code == "formula_no_cache"


def test_xlsx_rejects_external_relationships_before_reader() -> None:
    sheet = _sheet_xml(['<row r="1"><c r="A1" t="inlineStr"><is><t>x</t></is></c></row>'], dimension="A1")
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(
            _xlsx_bytes([("Sheet", sheet, None)], external_relationship=True),
            "external.xlsx",
        )
    assert caught.value.code == "zip_external_relationship"


def test_ods_repeats_are_bounded_and_exported_without_whitespace_splitting() -> None:
    rows = (
        '<table:table-row>'
        '<table:table-cell office:value-type="string"><text:p>alpha</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="2.5"/>'
        '</table:table-row>'
        '<table:table-row><table:table-cell table:number-columns-repeated="2"/></table:table-row>'
        '<table:table-row table:number-rows-repeated="2">'
        '<table:table-cell office:value-type="boolean" office:boolean-value="true"/>'
        '</table:table-row>'
    )
    converted = convert_spreadsheet(_ods_bytes([_ods_table("Data", rows)]), "ods book.ods")

    assert converted[0].filename == "ods_book_ods.txt"
    assert converted[0].content == b"alpha\t2.5\n\t\nTrue\t\nTrue\t\n"


def test_ods_rejects_dates_and_uncached_formulas() -> None:
    date_rows = (
        '<table:table-row><table:table-cell office:value-type="date" office:date-value="2026-10-10"/>'
        "</table:table-row>"
    )
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(_ods_bytes([_ods_table("Dates", date_rows)]), "dates.ods")
    assert caught.value.code == "unsupported_date"

    formula_rows = (
        '<table:table-row><table:table-cell table:formula="of:=1+1"/>'
        "</table:table-row>"
    )
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(_ods_bytes([_ods_table("Calc", formula_rows)]), "calc.ods")
    assert caught.value.code == "formula_no_cache"


def test_ods_ignores_unbounded_empty_sheet_tails_without_expanding_them() -> None:
    rows = (
        '<table:table-row><table:table-cell office:value-type="float" office:value="9"/>'
        '<table:table-cell table:number-columns-repeated="16383"/></table:table-row>'
        '<table:table-row table:number-rows-repeated="1048575">'
        '<table:table-cell table:number-columns-repeated="1000000000000"/>'
        '</table:table-row>'
    )
    converted = convert_spreadsheet(_ods_bytes([_ods_table("Data", rows)]), "9.ods")
    assert converted[0].content == b"9\n"


def test_ods_retains_interior_empty_columns_but_discards_formatted_tail() -> None:
    rows = (
        '<table:table-row><table:table-cell office:value-type="float" office:value="1"/>'
        '<table:table-cell table:number-columns-repeated="2"/>'
        '<table:table-cell office:value-type="float" office:value="4"/>'
        '<table:covered-table-cell table:number-columns-repeated="16380"/></table:table-row>'
        '<table:table-row><table:table-cell office:value-type="float" office:value="2"/>'
        '<table:table-cell table:number-columns-repeated="16383"/></table:table-row>'
    )
    converted = convert_spreadsheet(_ods_bytes([_ods_table("Data", rows)]), "9.ods")
    assert converted[0].content == b"1\t\t\t4\n2\t\t\t\n"


@pytest.mark.parametrize("blank_columns", [256, 1000000000000])
def test_ods_rejects_data_beyond_column_bound_even_after_empty_repeat(blank_columns: int) -> None:
    rows = (
        '<table:table-row>'
        f'<table:table-cell table:number-columns-repeated="{blank_columns}"/>'
        '<table:table-cell office:value-type="float" office:value="1"/>'
        '</table:table-row>'
    )
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(_ods_bytes([_ods_table("Data", rows)]), "wide.ods")
    assert caught.value.code == "column_limit"


def test_ods_rejects_repeated_nonempty_cells_beyond_column_bound() -> None:
    rows = (
        '<table:table-row><table:table-cell table:number-columns-repeated="257" '
        'office:value-type="float" office:value="1"/></table:table-row>'
    )
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(_ods_bytes([_ods_table("Data", rows)]), "wide.ods")
    assert caught.value.code == "column_limit"


def test_ods_empty_interior_rows_still_count_towards_workbook_cell_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.spreadsheet_text.MAX_TOTAL_CELLS", 8)
    rows = (
        '<table:table-row><table:table-cell office:value-type="float" office:value="1"/>'
        '<table:table-cell office:value-type="float" office:value="2"/></table:table-row>'
        '<table:table-row><table:table-cell table:number-columns-repeated="16384"/></table:table-row>'
        '<table:table-row><table:table-cell office:value-type="float" office:value="3"/></table:table-row>'
    )
    with pytest.raises(SpreadsheetTextError) as caught:
        convert_spreadsheet(_ods_bytes([_ods_table("First", rows), _ods_table("Second", rows)]), "budget.ods")
    assert caught.value.code == "cell_limit"


def test_output_names_are_ascii_and_bounded() -> None:
    name = "тест " + ("x" * 400) + ".csv"
    converted = convert_spreadsheet(b"value\n", name)
    assert len(converted[0].filename) <= 255
    assert converted[0].filename.isascii()
    assert Path(converted[0].filename).suffix == ".txt"


def test_real_xls_fixture_is_used_when_repository_provides_one() -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "spreadsheet_text.xls"
    if not fixture.exists():
        pytest.skip("no local real XLS fixture was provided")
    converted = convert_spreadsheet(fixture.read_bytes(), fixture.name)
    assert converted
    assert all(item.filename.endswith(".txt") for item in converted)


def test_large_csv_field_does_not_hide_semicolon_separator() -> None:
    data = ("a" * 70000 + ";b\nc;d\n").encode()
    item = convert_spreadsheet(data, "TEST_ONLY.csv")[0]
    assert item.content == ("a" * 70000 + "\tb\nc\td\n").encode()


def test_parallel_csv_limit_is_restored_without_racing(monkeypatch) -> None:
    import csv
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app import spreadsheet_text as module
    original = module._detect_csv_delimiter
    entered, release = Event(), Event()
    def gate(text):
        if len(text) > 150000:
            entered.set()
            assert release.wait(5)
        return original(text)
    monkeypatch.setattr(module, "_detect_csv_delimiter", gate)
    old_limit = csv.field_size_limit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        large = pool.submit(convert_spreadsheet, b"a" * 200000 + b"\n", "large.csv")
        assert entered.wait(5)
        small = pool.submit(convert_spreadsheet, b"1,2\n", "small.csv")
        release.set()
        assert large.result(timeout=5)[0].content == b"a" * 200000 + b"\n"
        assert small.result(timeout=5)[0].content == b"1\t2\n"
    assert csv.field_size_limit() == old_limit


def test_xlsx_ignores_understated_dimension_without_dropping_cells() -> None:
    rows = ['<row r="1"><c r="A1" t="inlineStr"><is><t>left</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>RIGHT</t></is></c></row>']
    data = _xlsx_bytes([("Sheet1", _sheet_xml(rows, dimension="A1"), None)])
    assert convert_spreadsheet(data, "TEST_ONLY.xlsx")[0].content == b"left\tRIGHT\n"


def test_actual_xlsx_reference_limits_apply_even_with_small_dimension() -> None:
    rows = ['<row r="1"><c r="ZZZ1"><v>3</v></c></row>']
    data = _xlsx_bytes([("Sheet1", _sheet_xml(rows, dimension="A1"), None)])
    with pytest.raises(SpreadsheetTextError, match="column limit"):
        convert_spreadsheet(data, "TEST_ONLY.xlsx")
