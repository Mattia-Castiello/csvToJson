"""Tests for Layer 4: Normalizer + Serializer"""

import pytest

from quoro.models import TypedDocument
from quoro.normalizer.normalizer import _normalize_date, _normalize_number, serialize


def test_eu_number_with_dot_thousands_comma_decimal():
    assert _normalize_number("1.234,56") == 1234.56


def test_eu_number_no_thousands():
    assert _normalize_number("97,09") == 97.09


def test_us_number_with_comma_thousands():
    assert _normalize_number("1,234.56") == 1234.56


def test_integer_value():
    result = _normalize_number("50")
    assert result == 50
    assert isinstance(result, int)


def test_currency_symbol_stripped():
    assert _normalize_number("€ 450,00") == 450.0


def test_non_numeric_string_returned_as_is():
    assert _normalize_number("A PREVENTIVO") == "A PREVENTIVO"


def test_date_dd_mm_yyyy():
    assert _normalize_date("09/02/2026") == "2026-02-09"


def test_date_dd_mm_yy():
    assert _normalize_date("09/03/26") == "2026-03-09"


def test_date_with_dash():
    assert _normalize_date("09-02-2026") == "2026-02-09"


def test_non_date_string_returned_as_is():
    assert _normalize_date("Nordic Design AB") == "Nordic Design AB"


def test_serialize_single_document():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={"fornitore": "Acme"},
        extra_fields={},
        rows=[{"codice_articolo": "X-001", "quantita": "5"}],
        confidence_tipo=0.9,
        resolver="fallback",
    )
    result = serialize([doc])
    assert len(result) == 1
    assert result[0]["tipo"] == "ordine"
    assert result[0]["_meta"]["resolver"] == "fallback"
    assert "righe" in result[0]


def test_serialize_merges_same_tipo_same_schema():
    doc1 = TypedDocument(
        tipo="ordine",
        canonical_fields={"fornitore": "Acme"},
        extra_fields={},
        rows=[{"codice_articolo": "X-001", "quantita": "5"}],
        confidence_tipo=0.8,
        resolver="fallback",
    )
    doc2 = TypedDocument(
        tipo="ordine",
        canonical_fields={"fornitore": "Acme"},
        extra_fields={},
        rows=[{"codice_articolo": "X-002", "quantita": "10"}],
        confidence_tipo=0.8,
        resolver="fallback",
    )
    result = serialize([doc1, doc2])
    assert len(result) == 1
    assert len(result[0]["righe"]) == 2


def test_serialize_keeps_separate_different_tipo():
    doc1 = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[{"codice_articolo": "X-001"}],
        confidence_tipo=0.8,
        resolver="fallback",
    )
    doc2 = TypedDocument(
        tipo="fattura",
        canonical_fields={},
        extra_fields={},
        rows=[{"codice_articolo": "X-002"}],
        confidence_tipo=0.8,
        resolver="fallback",
    )
    result = serialize([doc1, doc2])
    assert len(result) == 2


def test_serialize_empty_returns_empty():
    assert serialize([]) == []


def test_sub_items_inherit_order_and_parent_even_with_italian_order_key():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[
            {
                "ordine_riferimento": "ORD-W11-01",
                "codice_articolo": "ALP-843",
                "descrizione": "Armadio 2 ante scorrevoli",
                "scatole": "2",
                "quantita_per_scato": "3",
                "quantita_totale": "6",
                "peso_kg": "180.5",
            },
            {
                "ordine_riferimento": None,
                "codice_articolo": None,
                "descrizione": "Sub-item: maniglie",
                "scatole": "2",
                "quantita_per_scato": "3",
                "quantita_totale": "6",
                "peso_kg": "2.1",
            },
            {
                "ordine_riferimento": None,
                "codice_articolo": None,
                "descrizione": "Sub item: ripiani",
                "scatole": "2",
                "quantita_per_scato": "6",
                "quantita_totale": "12",
                "peso_kg": "15.0",
            },
        ],
        confidence_tipo=0.8,
        resolver="fallback",
    )

    result = serialize([doc])
    rows = result[0]["righe"]
    assert rows[1]["ordine_riferimento"] == "ORD-W11-01"
    assert rows[2]["ordine_riferimento"] == "ORD-W11-01"
    assert rows[1]["parent_ref"] == "ALP-843"
    assert rows[2]["parent_ref"] == "ALP-843"


def test_serialize_promotes_total_row_to_top_level_field():
    doc = TypedDocument(
        tipo="fattura",
        canonical_fields={},
        extra_fields={},
        rows=[{"codice_articolo": "A-001", "prezzo_listino": "450,00"}],
        righe_senza_dati=[
            {"descrizione": "SOTTOTALE MOBILI"},
            {
                "descrizione": "TOTALE GENERALE",
                "prezzo_listino": "865,00",
                "importo_netto": "977,00",
            },
        ],
        confidence_tipo=0.9,
        resolver="fallback",
    )

    result = serialize([doc])[0]

    assert result["totale_generale"] == {
        "prezzo_listino": 865.0,
        "importo_netto": 977.0,
    }
    assert result["righe_senza_dati"] == [{"descrizione": "SOTTOTALE MOBILI"}]
