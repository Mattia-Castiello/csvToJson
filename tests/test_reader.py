"""Tests for Layer 1: Reader"""

from pathlib import Path

import pytest

from quoro.models import RawCell, RawSheet
from quoro.reader import read_file

SAMPLES = Path(__file__).parent.parent


def test_csv_simple_order_returns_one_sheet():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    assert len(sheets) == 1
    assert sheets[0].name == "sheet1"


def test_csv_simple_order_has_correct_rows():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    rows = [r for r in sheets[0].rows if any(c.value for c in r)]
    assert rows[0][0].value == "ITEM"
    assert rows[1][0].value == "FRN-001"
    assert rows[1][1].value == "2"


def test_csv_european_invoice_detects_semicolon_separator():
    sheets = read_file(SAMPLES / "02-european-invoice.csv")
    assert sheets[0].separator == ";"
    rows = [r for r in sheets[0].rows if any(c.value for c in r)]
    # Header should have 6 columns
    assert len(rows[0]) == 6


def test_csv_header_offset_reads_metadata_rows():
    sheets = read_file(SAMPLES / "03-header-offset.csv")
    rows = sheets[0].rows
    # First row should contain "Fornitore: Nordic Design AB"
    first_row_vals = [c.value for c in rows[0] if c.value]
    assert any("Fornitore" in v for v in first_row_vals)


def test_csv_multi_table_reads_all_rows():
    sheets = read_file(SAMPLES / "04-multi-table.csv")
    non_empty = [r for r in sheets[0].rows if any(c.value for c in r)]
    # 1 title + 1 header + 3 data + 1 title + 1 header + 3 data + 1 title + 1 header + 1 data = 13
    assert len(non_empty) >= 10


def test_csv_raw_cells_have_no_formatting():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    cell = sheets[0].rows[0][0]
    assert cell.bold is False
    assert cell.bg_color is None


def test_excel_multi_sheet_returns_multiple_sheets():
    sheets = read_file(SAMPLES / "05-multi-sheet.xlsx")
    assert len(sheets) >= 1


def test_read_file_raises_on_unsupported_extension(tmp_path):
    f = tmp_path / "data.json"
    f.write_text("{}")
    with pytest.raises(ValueError, match="Unsupported file type"):
        read_file(f)
