"""Tests for MultiSheetResolver."""

from pathlib import Path

from quoro.models import Segment, TypedDocument


def _make_segment(sheet_name: str, header: list[str], rows: list[list[str]]) -> Segment:
    return Segment(
        sheet_name=sheet_name,
        metadata={},
        header=header,
        rows=rows,
        confidence=0.8,
    )


def test_typed_document_accepts_sheet_label():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[],
        confidence_tipo=0.9,
        sheet_label="ordini nordic",
    )
    assert doc.sheet_label == "ordini nordic"


def test_typed_document_sheet_label_defaults_to_none():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[],
        confidence_tipo=0.9,
    )
    assert doc.sheet_label is None


# ---------------------------------------------------------------------------
# Task 2: Prompt builder
# ---------------------------------------------------------------------------
from quoro.resolver.multi_sheet_resolver import _build_multi_prompt


def test_multi_prompt_includes_all_sheet_names():
    seg1 = _make_segment("Nordic", ["Item", "Qty"], [["A001", "10"]])
    seg2 = _make_segment("Alpine", ["Codigo", "Cantidad"], [["B002", "5"]])
    prompt = _build_multi_prompt([seg1, seg2])
    assert "Nordic" in prompt
    assert "Alpine" in prompt


def test_multi_prompt_includes_all_headers():
    seg1 = _make_segment("Nordic", ["Item", "Qty"], [["A001", "10"]])
    seg2 = _make_segment("Alpine", ["Codigo", "Cantidad"], [["B002", "5"]])
    prompt = _build_multi_prompt([seg1, seg2])
    assert "Item" in prompt
    assert "Codigo" in prompt


def test_multi_prompt_includes_sample_rows():
    seg1 = _make_segment("Nordic", ["Item", "Qty"], [["A001", "10"]])
    prompt = _build_multi_prompt([seg1])
    assert "A001" in prompt


def test_multi_prompt_instructs_unified_mapping():
    seg1 = _make_segment("Nordic", ["Item", "Qty"], [["A001", "10"]])
    seg2 = _make_segment("Alpine", ["Codigo", "Cantidad"], [["B002", "5"]])
    prompt = _build_multi_prompt([seg1, seg2])
    assert "mapping_colonne_globale" in prompt


# ---------------------------------------------------------------------------
# Task 3: LLM response parsing
# ---------------------------------------------------------------------------
from quoro.resolver.multi_sheet_resolver import _apply_multi_llm_mapping


def test_apply_multi_llm_mapping_unifies_column_names():
    """'Item' on sheet 1 and 'Codigo' on sheet 2 → same canonical name."""
    seg1 = _make_segment("Nordic", ["Item", "Qty"], [["A001", "10"]])
    seg2 = _make_segment("Alpine", ["Codigo", "Cantidad"], [["B002", "5"]])

    llm_result = {
        "mapping_colonne_globale": {
            "Item": "codice_articolo",
            "Qty": "quantita",
            "Codigo": "codice_articolo",
            "Cantidad": "quantita",
        },
        "fogli": [
            {
                "indice": 0,
                "nome_foglio": "Nordic",
                "etichetta": "ordini nordic",
                "tipo": "ordine",
                "confidence": 0.9,
                "mapping_metadati": {},
            },
            {
                "indice": 1,
                "nome_foglio": "Alpine",
                "etichetta": "ordini alpine",
                "tipo": "ordine",
                "confidence": 0.88,
                "mapping_metadati": {},
            },
        ],
    }

    docs = _apply_multi_llm_mapping([seg1, seg2], llm_result, model="gpt-4o-mini")

    assert len(docs) == 2
    assert "codice_articolo" in docs[0].rows[0]
    assert "codice_articolo" in docs[1].rows[0]
    assert docs[0].sheet_label == "ordini nordic"
    assert docs[1].sheet_label == "ordini alpine"
    assert docs[0].tipo == "ordine"
    assert docs[1].tipo == "ordine"


def test_apply_multi_llm_mapping_falls_back_to_sheet_name_when_no_etichetta():
    seg1 = _make_segment("Foglio1", ["Col"], [["val"]])
    llm_result = {
        "mapping_colonne_globale": {"Col": "colonna"},
        "fogli": [
            {
                "indice": 0,
                "nome_foglio": "Foglio1",
                "etichetta": "",
                "tipo": "sconosciuto",
                "confidence": 0.3,
                "mapping_metadati": {},
            }
        ],
    }
    docs = _apply_multi_llm_mapping([seg1], llm_result, model="gpt-4o-mini")
    assert docs[0].sheet_label == "Foglio1"


# ---------------------------------------------------------------------------
# Task 4: resolve_multi_sheet() with LLM call and fallback
# ---------------------------------------------------------------------------
from unittest.mock import patch

from quoro.resolver.multi_sheet_resolver import resolve_multi_sheet

SCHEMAS = Path(__file__).parent.parent / "schemas"


def test_resolve_multi_sheet_returns_one_doc_per_segment():
    seg1 = _make_segment("Nordic", ["Item", "Qty"], [["A001", "10"]])
    seg2 = _make_segment("Alpine", ["Codigo", "Cantidad"], [["B002", "5"]])

    mock_llm_result = {
        "mapping_colonne_globale": {
            "Item": "codice_articolo",
            "Qty": "quantita",
            "Codigo": "codice_articolo",
            "Cantidad": "quantita",
        },
        "fogli": [
            {
                "indice": 0,
                "nome_foglio": "Nordic",
                "etichetta": "ordini nordic",
                "tipo": "ordine",
                "confidence": 0.9,
                "mapping_metadati": {},
            },
            {
                "indice": 1,
                "nome_foglio": "Alpine",
                "etichetta": "ordini alpine",
                "tipo": "ordine",
                "confidence": 0.88,
                "mapping_metadati": {},
            },
        ],
    }

    with patch(
        "quoro.resolver.multi_sheet_resolver._call_openai",
        return_value=mock_llm_result,
    ):
        docs = resolve_multi_sheet([seg1, seg2], schema_dir=SCHEMAS)

    assert len(docs) == 2
    assert docs[0].sheet_label == "ordini nordic"
    assert docs[1].sheet_label == "ordini alpine"
    assert docs[0].resolver == "openai"


def test_resolve_multi_sheet_falls_back_to_static_when_llm_fails():
    seg1 = _make_segment("Nordic", ["Item", "Qty"], [["A001", "10"]])
    seg2 = _make_segment("Alpine", ["Codigo", "Cantidad"], [["B002", "5"]])

    with patch(
        "quoro.resolver.multi_sheet_resolver._call_openai",
        return_value=None,
    ):
        docs = resolve_multi_sheet([seg1, seg2], schema_dir=SCHEMAS)

    assert len(docs) == 2
    assert docs[0].resolver == "fallback"
    assert docs[1].resolver == "fallback"
    assert docs[0].sheet_label == "Nordic"
    assert docs[1].sheet_label == "Alpine"


# ---------------------------------------------------------------------------
# Task 5: Routing in resolver/__init__.py
# ---------------------------------------------------------------------------
from quoro.resolver import resolve as resolver_resolve


def test_resolver_routes_single_sheet_to_semantic():
    seg = _make_segment("Sheet1", ["Item"], [["A001"]])
    with patch("quoro.resolver.semantic_resolver.resolve") as mock_single:
        mock_single.return_value = [
            TypedDocument(
                tipo="ordine",
                canonical_fields={},
                extra_fields={},
                rows=[],
                confidence_tipo=0.9,
            )
        ]
        docs = resolver_resolve([seg], schema_dir=SCHEMAS)
    mock_single.assert_called_once()
    assert len(docs) == 1


def test_resolver_routes_multi_sheet_to_multi_sheet_resolver():
    seg1 = _make_segment("Nordic", ["Item"], [["A001"]])
    seg2 = _make_segment("Alpine", ["Codigo"], [["B002"]])
    with patch("quoro.resolver.multi_sheet_resolver.resolve_multi_sheet") as mock_multi:
        mock_multi.return_value = [
            TypedDocument(
                tipo="ordine",
                canonical_fields={},
                extra_fields={},
                rows=[],
                confidence_tipo=0.9,
                sheet_label="ordini nordic",
            ),
            TypedDocument(
                tipo="ordine",
                canonical_fields={},
                extra_fields={},
                rows=[],
                confidence_tipo=0.88,
                sheet_label="ordini alpine",
            ),
        ]
        docs = resolver_resolve([seg1, seg2], schema_dir=SCHEMAS)
    mock_multi.assert_called_once()
    assert len(docs) == 2


# ---------------------------------------------------------------------------
# Task 6: Normalizer — sheet_label output and cross-sheet merge prevention
# ---------------------------------------------------------------------------
from quoro.normalizer import serialize


def test_serialize_includes_sheet_label_in_output():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[],
        confidence_tipo=0.9,
        sheet_label="ordini nordic",
    )
    result = serialize([doc])
    assert result[0].get("etichetta") == "ordini nordic"


def test_serialize_keeps_sheets_separate_when_different_labels():
    """Two docs with same tipo+fields but different sheet_label must NOT be merged."""
    doc1 = TypedDocument(
        tipo="ordine",
        canonical_fields={"codice_articolo": "codice_articolo"},
        extra_fields={},
        rows=[{"codice_articolo": "A001"}],
        confidence_tipo=0.9,
        sheet_label="ordini nordic",
    )
    doc2 = TypedDocument(
        tipo="ordine",
        canonical_fields={"codice_articolo": "codice_articolo"},
        extra_fields={},
        rows=[{"codice_articolo": "B002"}],
        confidence_tipo=0.88,
        sheet_label="ordini alpine",
    )
    result = serialize([doc1, doc2])
    assert len(result) == 2
    labels = {r.get("etichetta") for r in result}
    assert "ordini nordic" in labels
    assert "ordini alpine" in labels


def test_serialize_no_sheet_label_does_not_add_etichetta_key():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[],
        confidence_tipo=0.9,
        sheet_label=None,
    )
    result = serialize([doc])
    assert "etichetta" not in result[0]


def test_riepilogo_field_no_longer_exists_on_typed_document():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[],
        confidence_tipo=0.9,
    )
    assert not hasattr(doc, "riepilogo")


def test_gruppi_field_no_longer_exists_on_typed_document():
    doc = TypedDocument(
        tipo="ordine",
        canonical_fields={},
        extra_fields={},
        rows=[],
        confidence_tipo=0.9,
    )
    assert not hasattr(doc, "gruppi")
    assert hasattr(doc, "righe_senza_dati")
    assert doc.righe_senza_dati == []
