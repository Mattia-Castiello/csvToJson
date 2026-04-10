"""Tests for Layer 3: Semantic Resolver (static fallback path)"""

from pathlib import Path
from unittest.mock import patch

import pytest

from quoro.analyzer import analyze
from quoro.reader import read_file
from quoro.resolver import resolve
from quoro.resolver.schema_loader import load_schemas
from quoro.resolver.static_resolver import resolve_static

SAMPLES = Path(__file__).parent.parent
SCHEMAS = Path(__file__).parent.parent / "schemas"


def _schemas():
    return load_schemas(SCHEMAS)


def test_static_resolver_identifies_ordine():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)
    doc = resolve_static(segments[0], _schemas())
    assert doc.tipo == "ordine"


def test_static_resolver_identifies_fattura():
    sheets = read_file(SAMPLES / "02-european-invoice.csv")
    segments = analyze(sheets)
    doc = resolve_static(segments[0], _schemas())
    assert doc.tipo == "fattura"


def test_static_resolver_maps_canonical_fields():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)
    doc = resolve_static(segments[0], _schemas())
    # Rows should use canonical field names
    if doc.rows:
        keys = set(doc.rows[0].keys())
        assert "codice_articolo" in keys or "quantita" in keys


def test_static_resolver_unknown_type_preserves_columns():
    from quoro.models import Segment

    seg = Segment(
        sheet_name="test",
        metadata={},
        header=["XYZ_COL_A", "XYZ_COL_B"],
        rows=[["val1", "val2"]],
        confidence=0.5,
    )
    doc = resolve_static(seg, _schemas())
    assert doc.tipo == "sconosciuto"
    assert doc.rows[0].get("xyz_col_a") == "val1"


def test_static_resolver_includes_fallback_in_meta():
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)
    doc = resolve_static(segments[0], _schemas())
    assert doc.resolver == "fallback"


def test_resolve_with_ollama_unavailable_uses_fallback():
    """When Ollama raises ConnectionError, resolve() should fall back to static."""
    sheets = read_file(SAMPLES / "01-simple-order.csv")
    segments = analyze(sheets)

    with patch("quoro.resolver.semantic_resolver._call_openai", return_value=None):
        docs = resolve(segments, schema_dir=SCHEMAS)

    assert len(docs) == 1
    assert docs[0].resolver == "fallback"
    assert docs[0].tipo != ""


def test_resolve_fallback_no_exception_on_connection_error():
    """Ensure no exception even if Ollama is completely unreachable."""
    sheets = read_file(SAMPLES / "03-header-offset.csv")
    segments = analyze(sheets)

    with patch(
        "quoro.resolver.semantic_resolver._call_openai", side_effect=ConnectionError
    ):
        # Should not raise
        docs = resolve(segments, schema_dir=SCHEMAS)

    assert len(docs) >= 1
