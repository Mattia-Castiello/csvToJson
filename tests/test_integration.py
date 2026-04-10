"""Integration tests: full pipeline Reader → Analyzer → Resolver → Serializer"""

from pathlib import Path
from unittest.mock import patch

import pytest

from quoro.analyzer import analyze
from quoro.normalizer import serialize
from quoro.reader import read_file
from quoro.resolver import resolve
from quoro.resolver.schema_loader import load_schemas

SAMPLES = Path(__file__).parent.parent
SCHEMAS = Path(__file__).parent.parent / "schemas"


def _run(filename: str) -> list[dict]:
    schemas = load_schemas(SCHEMAS)
    schema_lookup = {s["tipo"]: s for s in schemas}
    patch_semantic = patch(
        "quoro.resolver.semantic_resolver._call_openai", return_value=None
    )
    patch_multi = patch(
        "quoro.resolver.multi_sheet_resolver._call_openai", return_value=None
    )
    with patch_semantic, patch_multi:
        sheets = read_file(SAMPLES / filename)
        segments = analyze(sheets)
        docs = resolve(segments, schema_dir=SCHEMAS)
        return serialize(docs, schema_lookup=schema_lookup)


def test_01_simple_order_tipo():
    result = _run("01-simple-order.csv")
    assert len(result) >= 1
    assert result[0]["tipo"] == "ordine"


def test_01_simple_order_row_count():
    result = _run("01-simple-order.csv")
    assert len(result[0]["righe"]) == 5


def test_01_simple_order_has_canonical_fields():
    result = _run("01-simple-order.csv")
    row = result[0]["righe"][0]
    assert "codice_articolo" in row or "quantita" in row


def test_02_european_invoice_tipo():
    result = _run("02-european-invoice.csv")
    assert result[0]["tipo"] == "fattura"


def test_02_european_invoice_numbers_normalized():
    result = _run("02-european-invoice.csv")
    rows = result[0]["righe"]
    # 97,09 should become 97.09
    prices = [r.get("prezzo_listino") for r in rows if r.get("prezzo_listino")]
    assert any(isinstance(p, float) and abs(p - 97.09) < 0.01 for p in prices)


def test_03_header_offset_tipo():
    result = _run("03-header-offset.csv")
    assert result[0]["tipo"] == "ordine"


def test_03_header_offset_metadata_present():
    result = _run("03-header-offset.csv")
    out = result[0]
    # Metadata fields should appear at top level
    keys = set(out.keys())
    assert "fornitore" in keys or any("nordica" in str(v).lower() for v in out.values())


def test_04_multi_table_multiple_outputs():
    result = _run("04-multi-table.csv")
    assert len(result) >= 2


def test_06_dirty_data_removes_summary_rows():
    result = _run("06-dirty-data.csv")
    assert len(result) >= 1
    rows = result[0]["righe"]
    row_texts = [str(r) for r in rows]
    assert not any("TOTALE GENERALE" in t for t in row_texts)
    assert not any("SUBTOTALE" in t for t in row_texts)


def test_06_dirty_data_warnings_in_meta():
    result = _run("06-dirty-data.csv")
    warnings = result[0]["_meta"].get("warnings", [])
    assert any("riepilogo" in w.lower() or "rimosse" in w.lower() for w in warnings)


def test_06_dirty_data_promotes_total_to_top_level_field():
    result = _run("06-dirty-data.csv")
    out = result[0]

    assert out["totale_generale"] == {
        "prezzo_listino": 865.0,
        "importo_netto": 977.0,
    }
    assert not any(
        "TOTALE GENERALE" in str(row) for row in out.get("righe_senza_dati", [])
    )


def test_meta_block_always_present():
    for fname in [
        "01-simple-order.csv",
        "02-european-invoice.csv",
        "03-header-offset.csv",
    ]:
        result = _run(fname)
        for doc in result:
            assert "_meta" in doc
            assert "resolver" in doc["_meta"]


def test_09_shipping_tariff_multiple_zones():
    result = _run("09-shipping-tariff.csv")
    # Should produce multiple segments (Zona A, B have same schema; Zona C is different)
    assert len(result) >= 1
    tipos = [r["tipo"] for r in result]
    assert "tariffario" in tipos


def test_07_nested_packing_sub_items_have_parent_ref():
    result = _run("07-nested-packing.csv")
    assert len(result) == 2
    labels = {doc.get("etichetta") for doc in result}
    assert {"DL-7741", "DL-4469"} <= labels
    first_doc_rows = next(doc["righe"] for doc in result if doc.get("etichetta") == "DL-7741")
    sub_rows = [row for row in first_doc_rows if str(row.get("descrizione", "")).lower().startswith("sub-item")]
    assert sub_rows, "Expected at least one sub-item row in nested packing sample"
    for sub_row in sub_rows:
        assert sub_row.get("parent_ref") == "ALP-843"
        assert sub_row.get("order_ref") == "ORD-W11-01"
