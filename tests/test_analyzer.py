"""Tests for Layer 2: Structural Analyzer"""

from pathlib import Path

import pytest

from quoro.analyzer import analyze
from quoro.reader import read_file

SAMPLES = Path(__file__).parent.parent


def test_simple_order_produces_one_segment():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)
    assert len(segments) == 1


def test_simple_order_header_detected():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)
    assert "ITEM" in segments[0].header or "Codice" in segments[0].header
    assert len(segments[0].rows) == 5


def test_header_offset_extracts_metadata():
    sheets = read_file(SAMPLES / "03-header-offset.csv")
    segments = analyze(sheets)
    assert len(segments) == 1
    meta = segments[0].metadata
    assert any("Fornitore" in k for k in meta)
    assert any("Nordic Design" in v for v in meta.values())


def test_header_offset_header_found_after_metadata():
    sheets = read_file(SAMPLES / "03-header-offset.csv")
    segments = analyze(sheets)
    header = segments[0].header
    assert len(header) >= 4
    assert any(h in ("Codice", "Quantità", "Prezzo") for h in header)


def test_multi_table_produces_multiple_segments():
    sheets = read_file(SAMPLES / "04-multi-table.csv")
    segments = analyze(sheets)
    assert len(segments) >= 2


def test_dirty_data_removes_summary_rows():
    sheets = read_file(SAMPLES / "06-dirty-data.csv")
    segments = analyze(sheets)
    assert len(segments) >= 1
    # Summary rows must not appear in any segment's data rows
    all_row_texts = [" ".join(r) for seg in segments for r in seg.rows]
    assert not any("TOTALE GENERALE" in t.upper() for t in all_row_texts)
    assert not any("SOTTOTALE" in t.upper() for t in all_row_texts)


def test_confidence_is_within_range():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)
    for seg in segments:
        assert 0.0 <= seg.confidence <= 1.0


def test_high_confidence_for_clean_file():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)
    assert segments[0].confidence >= 0.5
