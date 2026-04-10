# Multi-Sheet Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a file has more than one sheet, send all sheets to the LLM in a single call so it can unify column names across sheets and use sheet names as labels.

**Architecture:** Add a `MultiSheetResolver` that builds a single combined prompt with all segments, calls the LLM once, and applies a global column mapping so equivalent columns (e.g. `Item` on sheet 1, `Codigo` on sheet 2) always get the same canonical name. Single-sheet files continue using the existing `SemanticResolver` unchanged. The `resolve()` entrypoint in `__init__.py` routes based on segment count.

**Tech Stack:** Python 3.11+, openai SDK, pytest, existing quoro models (`Segment`, `TypedDocument`).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `src/quoro/models.py` | Add `sheet_label: str \| None` field to `TypedDocument` |
| Create | `src/quoro/resolver/multi_sheet_resolver.py` | Prompt builder, LLM parsing, `resolve_multi_sheet()` |
| Modify | `src/quoro/resolver/__init__.py` | Route to `resolve_multi_sheet` when `len(segments) > 1` |
| Modify | `src/quoro/normalizer/normalizer.py` | Output `sheet_label` in JSON; prevent merging sheets with different labels |
| Create | `tests/test_multi_sheet_resolver.py` | All tests for the new resolver |

---

## Task 1: Add `sheet_label` to `TypedDocument`

**Files:**
- Modify: `src/quoro/models.py`
- Test: `tests/test_multi_sheet_resolver.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_multi_sheet_resolver.py`:

```python
"""Tests for MultiSheetResolver."""
from quoro.models import TypedDocument


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/mattiacastiello/Desktop/cosepython/quoro
pytest tests/test_multi_sheet_resolver.py -v
```

Expected: FAIL — `TypedDocument.__init__() got an unexpected keyword argument 'sheet_label'`

- [ ] **Step 3: Add `sheet_label` field to `TypedDocument` in `src/quoro/models.py`**

In `src/quoro/models.py`, after the `riepilogo` field (line 51), add:

```python
    sheet_label: str | None = None  # label assigned by LLM from sheet name (multi-sheet files)
```

Full `TypedDocument` dataclass should now end with:
```python
    riepilogo: dict = field(
        default_factory=dict
    )  # summary/total values with descriptive keys: {"totale_prezzi_scontati": 865.0, ...}
    sheet_label: str | None = None  # label assigned by LLM from sheet name (multi-sheet files)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_multi_sheet_resolver.py -v
```

Expected: PASS for both tests.

- [ ] **Step 5: Commit**

```bash
git add src/quoro/models.py tests/test_multi_sheet_resolver.py
git commit -m "feat: add sheet_label field to TypedDocument"
```

---

## Task 2: Prompt Builder for Multi-Sheet

**Files:**
- Create: `src/quoro/resolver/multi_sheet_resolver.py`
- Test: `tests/test_multi_sheet_resolver.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multi_sheet_resolver.py`:

```python
from quoro.models import Segment
from quoro.resolver.multi_sheet_resolver import _build_multi_prompt


def _make_segment(sheet_name: str, header: list[str], rows: list[list[str]]) -> Segment:
    return Segment(
        sheet_name=sheet_name,
        metadata={},
        header=header,
        rows=rows,
        confidence=0.8,
    )


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_multi_sheet_resolver.py::test_multi_prompt_includes_all_sheet_names -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'quoro.resolver.multi_sheet_resolver'`

- [ ] **Step 3: Create `src/quoro/resolver/multi_sheet_resolver.py` with prompt builder**

```python
from __future__ import annotations

import json
import os
from pathlib import Path

from quoro.models import Segment, TypedDocument
from quoro.resolver.schema_loader import load_schemas
from quoro.resolver.static_resolver import resolve_static

_DEFAULT_SCHEMA_DIR = Path(__file__).parent.parent.parent.parent / "schemas"
_CONFIDENCE_THRESHOLD = float(os.environ.get("QUORO_CONFIDENCE_THRESHOLD", "0.65"))


def _build_multi_prompt(segments: list[Segment]) -> str:
    lines = [
        "Analizza la struttura di questo file multi-foglio e rispondi SOLO con un JSON valido.",
        "",
        "ISTRUZIONI SPECIALI PER FILE MULTI-FOGLIO:",
        "- Usa SEMPRE gli stessi nomi canonici per colonne che rappresentano la stessa informazione",
        "  tra fogli diversi (es. 'Item' e 'Codigo' sono entrambi codici articolo → 'codice_articolo').",
        "- Usa il nome del foglio come base per l'etichetta (etichetta) di ogni foglio.",
        "- Se un foglio è chiaramente un riepilogo di altri fogli, indicalo nel tipo e nell'etichetta.",
        "",
    ]

    for i, seg in enumerate(segments):
        meta_str = " | ".join(f"{k}: {v}" for k, v in seg.metadata.items())
        raw_ctx_str = "\n  ".join(seg.raw_context) if seg.raw_context else "nessuno"

        # Build interleaved view with aggregates (max 5 rows per sheet to keep prompt concise)
        summary_by_index: dict[int, list[str]] = {}
        for s in seg.summary_rows:
            idx = s.get("after_index")
            key = idx if idx is not None else -1
            summary_by_index.setdefault(key, []).append(s["text"])

        interleaved_lines: list[str] = []
        for agg_text in summary_by_index.get(-1, []):
            interleaved_lines.append(f"  [AGGREGATO prima dei dati]: {agg_text}")
        for j, row in enumerate(seg.rows[:5]):
            row_str = " | ".join(f"{h}: {v}" for h, v in zip(seg.header, row) if h)
            interleaved_lines.append(f"  Riga {j + 1}: {row_str}")
            for agg_text in summary_by_index.get(j, []):
                interleaved_lines.append(f"  [AGGREGATO dopo riga {j + 1}]: {agg_text}")
        if len(seg.rows) > 5:
            interleaved_lines.append(f"  ... ({len(seg.rows) - 5} righe omesse)")

        interleaved_str = "\n".join(interleaved_lines) if interleaved_lines else "  nessuna"
        warnings_str = "; ".join(seg.warnings) if seg.warnings else "nessuno"

        lines += [
            f'--- FOGLIO {i + 1}: "{seg.sheet_name}" ---',
            f"CONTESTO INIZIALE: {raw_ctx_str}",
            f"METADATI STRUTTURATI: {meta_str or 'nessuno'}",
            f"COLONNE INTESTAZIONE: {seg.header}",
            f"RIGHE DATI CON AGGREGATI (campione, max 5):\n{interleaved_str}",
            f"TOTALE RIGHE DATI: {len(seg.rows)}",
            f"WARNING PARSER: {warnings_str}",
            "",
        ]

    lines += [
        "ISTRUZIONI:",
        "1. mapping_colonne_globale: mappa OGNI colonna di OGNI foglio a un nome canonico snake_case.",
        "   Usa lo STESSO nome canonico per colonne diverse che rappresentano la stessa informazione.",
        "   Includi le colonne di TUTTI i fogli in questo dizionario.",
        "",
        "2. Per ogni foglio: determina tipo, etichetta leggibile basata sul nome del foglio,",
        "   confidenza, mapping metadati, gruppi, riepilogo.",
        "   etichetta: stringa leggibile in italiano, lowercase.",
        "   Es.: foglio 'Sheet Nordic' → etichetta 'ordini nordic'.",
        "   Es.: foglio 'Riepilogo' che somma altri fogli → etichetta 'riepilogo tabella fornitori'.",
        "",
        "3. GRUPPI: usa questo campo quando righe fungono da intestazione di sezione.",
        "   Ogni gruppo: 'etichetta' (testo verbatim) + 'righe' (array oggetti con valori canonici).",
        "   Se non ci sono sezioni, lascia 'gruppi' come [].",
        "",
        "4. RIEPILOGO: usa per righe totale/riepilogo. Chiavi descrittive in dict piatto.",
        "   Esempio: {totale_prezzi_scontati: 865.0}. Se nessuna riga totale, lascia {} .",
        "",
        "Rispondi ESATTAMENTE in questo formato JSON (nessun testo aggiuntivo):",
        "{",
        '  "mapping_colonne_globale": {"NomeColonnaFoglio1": "nome_canonico", "NomeColonnaFoglio2": "nome_canonico", ...},',
        '  "fogli": [',
        '    {',
        '      "indice": 0,',
        '      "nome_foglio": "...",',
        '      "etichetta": "...",',
        '      "tipo": "...",',
        '      "confidence": 0.0,',
        '      "mapping_metadati": {},',
        '      "gruppi": [],',
        '      "riepilogo": {}',
        '    }',
        '  ]',
        "}",
    ]

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_multi_sheet_resolver.py::test_multi_prompt_includes_all_sheet_names \
       tests/test_multi_sheet_resolver.py::test_multi_prompt_includes_all_headers \
       tests/test_multi_sheet_resolver.py::test_multi_prompt_includes_sample_rows \
       tests/test_multi_sheet_resolver.py::test_multi_prompt_instructs_unified_mapping -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quoro/resolver/multi_sheet_resolver.py tests/test_multi_sheet_resolver.py
git commit -m "feat: add multi-sheet prompt builder"
```

---

## Task 3: LLM Response Parsing for Multi-Sheet

**Files:**
- Modify: `src/quoro/resolver/multi_sheet_resolver.py`
- Test: `tests/test_multi_sheet_resolver.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multi_sheet_resolver.py`:

```python
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
                "gruppi": [],
                "riepilogo": {},
            },
            {
                "indice": 1,
                "nome_foglio": "Alpine",
                "etichetta": "ordini alpine",
                "tipo": "ordine",
                "confidence": 0.88,
                "mapping_metadati": {},
                "gruppi": [],
                "riepilogo": {},
            },
        ],
    }

    docs = _apply_multi_llm_mapping([seg1, seg2], llm_result, model="gpt-4o-mini")

    assert len(docs) == 2
    # Both sheets use the same canonical name for the item column
    assert "codice_articolo" in docs[0].rows[0]
    assert "codice_articolo" in docs[1].rows[0]
    # Labels are set from LLM etichetta
    assert docs[0].sheet_label == "ordini nordic"
    assert docs[1].sheet_label == "ordini alpine"
    # Types are set
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
                "gruppi": [],
                "riepilogo": {},
            }
        ],
    }
    docs = _apply_multi_llm_mapping([seg1], llm_result, model="gpt-4o-mini")
    # Falls back to sheet_name when etichetta is empty
    assert docs[0].sheet_label == "Foglio1"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_multi_sheet_resolver.py::test_apply_multi_llm_mapping_unifies_column_names -v
```

Expected: FAIL — `ImportError: cannot import name '_apply_multi_llm_mapping'`

- [ ] **Step 3: Add `_apply_multi_llm_mapping` to `multi_sheet_resolver.py`**

Add these helper functions and the mapping function after `_build_multi_prompt`:

```python
def _compute_mapping_coverage(segment: Segment, col_map: dict[str, str]) -> float:
    if not segment.header:
        return 0.0
    mapped = sum(1 for col in segment.header if col_map.get(col))
    return mapped / len(segment.header)


def _compute_composite_confidence(
    llm_confidence: float,
    mapping_coverage: float,
    tipo: str,
) -> float:
    tipo_ok = 1.0 if tipo not in ("sconosciuto", "unknown", "", "?") else 0.0
    return llm_confidence * 0.5 + mapping_coverage * 0.4 + tipo_ok * 0.1


def _apply_multi_llm_mapping(
    segments: list[Segment],
    llm_result: dict,
    model: str,
) -> list[TypedDocument]:
    global_col_map: dict[str, str] = llm_result.get("mapping_colonne_globale", {})
    fogli_results: list[dict] = llm_result.get("fogli", [])

    documents: list[TypedDocument] = []
    for i, segment in enumerate(segments):
        foglio_result = next(
            (f for f in fogli_results if f.get("indice") == i),
            {},
        )

        tipo = foglio_result.get("tipo", "sconosciuto")
        llm_confidence = float(foglio_result.get("confidence", 0.5))
        etichetta = foglio_result.get("etichetta") or segment.sheet_name
        meta_map: dict[str, str] = foglio_result.get("mapping_metadati", {})
        gruppi: list[dict] = foglio_result.get("gruppi", [])
        riepilogo: dict = foglio_result.get("riepilogo", {})

        # Apply global mapping for this sheet's columns
        col_map = {col: global_col_map.get(col, "") for col in segment.header}
        mapping_coverage = _compute_mapping_coverage(segment, col_map)
        composite = _compute_composite_confidence(llm_confidence, mapping_coverage, tipo)

        canonical_fields: dict = {}
        extra_fields: dict = {}
        column_mapping: dict[str, str] = {}

        for col in segment.header:
            mapped = col_map.get(col)
            if mapped:
                column_mapping[col] = mapped
                canonical_fields[mapped] = mapped
            else:
                snake = col.lower().replace(" ", "_").replace(".", "_")
                column_mapping[col] = snake
                extra_fields[col] = snake

        mapped_meta: dict[str, str] = {}
        for k, v in segment.metadata.items():
            canonical_key = meta_map.get(k, k.lower().replace(" ", "_").replace(".", "_"))
            mapped_meta[canonical_key] = v
        canonical_fields.update(mapped_meta)

        rows = []
        for raw_row in segment.rows:
            row_dict: dict = {}
            for j, cell_val in enumerate(raw_row):
                if j < len(segment.header):
                    col_name = segment.header[j]
                    mapped_name = column_mapping.get(col_name, col_name)
                else:
                    mapped_name = f"col_{j}"
                row_dict[mapped_name] = cell_val
            rows.append(row_dict)

        warnings = list(segment.warnings)
        if mapping_coverage < 0.5:
            warnings.append(
                f"copertura mapping bassa: {mapping_coverage:.0%} delle colonne mappate"
            )

        documents.append(
            TypedDocument(
                tipo=tipo,
                canonical_fields=canonical_fields,
                extra_fields=extra_fields,
                rows=rows,
                confidence_tipo=composite,
                warnings=warnings,
                resolver="openai",
                model=model,
                gruppi=gruppi,
                riepilogo=riepilogo,
                sheet_label=etichetta,
            )
        )

    return documents
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_multi_sheet_resolver.py::test_apply_multi_llm_mapping_unifies_column_names \
       tests/test_multi_sheet_resolver.py::test_apply_multi_llm_mapping_falls_back_to_sheet_name_when_no_etichetta -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quoro/resolver/multi_sheet_resolver.py tests/test_multi_sheet_resolver.py
git commit -m "feat: add multi-sheet LLM response parsing with global column unification"
```

---

## Task 4: Full `resolve_multi_sheet()` with LLM Call and Fallback

**Files:**
- Modify: `src/quoro/resolver/multi_sheet_resolver.py`
- Test: `tests/test_multi_sheet_resolver.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_sheet_resolver.py`:

```python
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
                "gruppi": [],
                "riepilogo": {},
            },
            {
                "indice": 1,
                "nome_foglio": "Alpine",
                "etichetta": "ordini alpine",
                "tipo": "ordine",
                "confidence": 0.88,
                "mapping_metadati": {},
                "gruppi": [],
                "riepilogo": {},
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

    # Falls back to one static doc per segment
    assert len(docs) == 2
    assert docs[0].resolver == "fallback"
    assert docs[1].resolver == "fallback"
    # Static fallback assigns sheet_label from sheet_name
    assert docs[0].sheet_label == "Nordic"
    assert docs[1].sheet_label == "Alpine"
```

Also add the `Path` import at the top of the test file:

```python
from pathlib import Path
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_multi_sheet_resolver.py::test_resolve_multi_sheet_returns_one_doc_per_segment -v
```

Expected: FAIL — `ImportError: cannot import name 'resolve_multi_sheet'`

- [ ] **Step 3: Add `_call_openai` and `resolve_multi_sheet` to `multi_sheet_resolver.py`**

Add at the end of `multi_sheet_resolver.py`:

```python
def _call_openai(prompt: str, model: str) -> dict | None:
    try:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return None
        return json.loads(text)
    except Exception as e:
        import sys

        print(f"[openai multi-sheet error] {e}", file=sys.stderr)
        return None


def resolve_multi_sheet(
    segments: list[Segment],
    schema_dir: Path | None = None,
    model: str | None = None,
    confidence_threshold: float = _CONFIDENCE_THRESHOLD,
) -> list[TypedDocument]:
    schema_dir = schema_dir or Path(
        os.environ.get("QUORO_SCHEMA_DIR", str(_DEFAULT_SCHEMA_DIR))
    )
    model = model or os.environ.get("QUORO_MODEL", "gpt-4o-mini")
    schemas = load_schemas(schema_dir)

    prompt = _build_multi_prompt(segments)
    llm_result = _call_openai(prompt, model)

    if llm_result is None:
        # LLM failed: fall back to per-sheet static resolver, preserving sheet names as labels
        fallback_docs: list[TypedDocument] = []
        for seg in segments:
            doc = resolve_static(seg, schemas)
            # Attach sheet name as label since LLM is unavailable
            fallback_docs.append(
                TypedDocument(
                    tipo=doc.tipo,
                    canonical_fields=doc.canonical_fields,
                    extra_fields=doc.extra_fields,
                    rows=doc.rows,
                    confidence_tipo=doc.confidence_tipo,
                    warnings=doc.warnings,
                    resolver=doc.resolver,
                    model=doc.model,
                    gruppi=doc.gruppi,
                    riepilogo=doc.riepilogo,
                    sheet_label=seg.sheet_name,
                )
            )
        return fallback_docs

    docs = _apply_multi_llm_mapping(segments, llm_result, model)

    # Per-sheet YAML enrichment for low-confidence sheets
    enriched: list[TypedDocument] = []
    for doc, seg in zip(docs, segments):
        if doc.confidence_tipo < confidence_threshold:
            static_doc = resolve_static(seg, schemas)
            doc = _enrich_with_static(doc, static_doc, confidence_threshold)
        enriched.append(doc)

    return enriched


def _enrich_with_static(
    llm_doc: TypedDocument,
    static_doc: TypedDocument,
    threshold: float,
) -> TypedDocument:
    """When LLM confidence is low, use static resolver as second opinion (same logic as single-sheet)."""
    warnings = list(llm_doc.warnings)

    if (
        static_doc.confidence_tipo > llm_doc.confidence_tipo
        and static_doc.tipo != "sconosciuto"
    ):
        final_tipo = static_doc.tipo
        final_confidence = static_doc.confidence_tipo
        warnings.append(
            f"tipo corretto da YAML: {llm_doc.tipo!r} → {static_doc.tipo!r} "
            f"(llm={llm_doc.confidence_tipo:.2f}, yaml={static_doc.confidence_tipo:.2f})"
        )
    else:
        final_tipo = llm_doc.tipo
        final_confidence = llm_doc.confidence_tipo

    static_col_map = {
        orig: mapped
        for orig, mapped in static_doc.canonical_fields.items()
        if orig in llm_doc.extra_fields
    }
    enriched_extra = {**llm_doc.extra_fields}
    enriched_canonical = {**llm_doc.canonical_fields}
    for orig, static_mapped in static_col_map.items():
        enriched_canonical[static_mapped] = static_mapped
        enriched_extra.pop(orig, None)

    enriched_rows = []
    for llm_row in llm_doc.rows:
        new_row = {}
        for key, val in llm_row.items():
            new_row[static_col_map.get(key, key)] = val
        enriched_rows.append(new_row)

    warnings.append(
        f"arricchimento YAML applicato (confidence llm={llm_doc.confidence_tipo:.2f} < soglia={threshold:.2f})"
    )

    return TypedDocument(
        tipo=final_tipo,
        canonical_fields=enriched_canonical,
        extra_fields=enriched_extra,
        rows=enriched_rows,
        confidence_tipo=final_confidence,
        warnings=warnings,
        resolver="openai+yaml",
        model=llm_doc.model,
        sheet_label=llm_doc.sheet_label,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_multi_sheet_resolver.py::test_resolve_multi_sheet_returns_one_doc_per_segment \
       tests/test_multi_sheet_resolver.py::test_resolve_multi_sheet_falls_back_to_static_when_llm_fails -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quoro/resolver/multi_sheet_resolver.py tests/test_multi_sheet_resolver.py
git commit -m "feat: add resolve_multi_sheet() with LLM call and static fallback"
```

---

## Task 5: Route in `resolver/__init__.py`

**Files:**
- Modify: `src/quoro/resolver/__init__.py`
- Test: `tests/test_multi_sheet_resolver.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multi_sheet_resolver.py`:

```python
from unittest.mock import patch
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
    with patch(
        "quoro.resolver.multi_sheet_resolver.resolve_multi_sheet"
    ) as mock_multi:
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_multi_sheet_resolver.py::test_resolver_routes_multi_sheet_to_multi_sheet_resolver -v
```

Expected: FAIL — routing not implemented, single resolver is always called.

- [ ] **Step 3: Update `src/quoro/resolver/__init__.py`**

Replace the entire file:

```python
from __future__ import annotations

from pathlib import Path

from quoro.models import Segment, TypedDocument
from quoro.resolver import semantic_resolver
from quoro.resolver import multi_sheet_resolver


def resolve(
    segments: list[Segment],
    schema_dir: Path | None = None,
    model: str | None = None,
    confidence_threshold: float | None = None,
) -> list[TypedDocument]:
    kwargs: dict = {}
    if schema_dir is not None:
        kwargs["schema_dir"] = schema_dir
    if model is not None:
        kwargs["model"] = model
    if confidence_threshold is not None:
        kwargs["confidence_threshold"] = confidence_threshold

    if len(segments) > 1:
        return multi_sheet_resolver.resolve_multi_sheet(segments, **kwargs)
    return semantic_resolver.resolve(segments, **kwargs)


__all__ = ["resolve"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_multi_sheet_resolver.py::test_resolver_routes_single_sheet_to_semantic \
       tests/test_multi_sheet_resolver.py::test_resolver_routes_multi_sheet_to_multi_sheet_resolver -v
```

Expected: 2 PASS.

- [ ] **Step 5: Verify existing single-sheet tests still pass**

```bash
pytest tests/test_resolver.py -v
```

Expected: all existing tests PASS (the patch target `quoro.resolver.semantic_resolver._call_ollama` in `test_resolver.py` may need updating — if tests fail because the patch path changed, update those two patch calls from `quoro.resolver.semantic_resolver._call_ollama` to `quoro.resolver.semantic_resolver._call_openai`).

- [ ] **Step 6: Commit**

```bash
git add src/quoro/resolver/__init__.py tests/test_multi_sheet_resolver.py
git commit -m "feat: route multi-sheet files to MultiSheetResolver in resolver entrypoint"
```

---

## Task 6: Normalizer — Output `sheet_label` and Prevent Cross-Sheet Merging

**Files:**
- Modify: `src/quoro/normalizer/normalizer.py`
- Test: `tests/test_multi_sheet_resolver.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_multi_sheet_resolver.py`:

```python
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
    # Must produce 2 separate outputs, not 1 merged
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_multi_sheet_resolver.py::test_serialize_includes_sheet_label_in_output \
       tests/test_multi_sheet_resolver.py::test_serialize_keeps_sheets_separate_when_different_labels -v
```

Expected: FAIL — normalizer does not output `etichetta` and merges same-tipo docs.

- [ ] **Step 3: Update `src/quoro/normalizer/normalizer.py`**

In `serialize()`, update the grouping key to include `sheet_label` (line 103):

```python
    # Group by (tipo, sorted canonical field keys, sheet_label) for merge decision
    groups: dict[tuple, list[TypedDocument]] = {}
    for doc in documents:
        key = (doc.tipo, tuple(sorted(doc.canonical_fields.keys())), doc.sheet_label)
        groups.setdefault(key, []).append(doc)
```

In `_serialize_single()`, add `etichetta` to output after `tipo` (after line 142):

```python
    out: dict = {"tipo": doc.tipo}
    if doc.sheet_label:
        out["etichetta"] = doc.sheet_label
    out.update(meta)
```

In `_merge_documents()`, add `etichetta` to merged output after `tipo` (after line 194):

```python
    out: dict = {"tipo": docs[0].tipo}
    if docs[0].sheet_label:
        out["etichetta"] = docs[0].sheet_label
    out.update(meta)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_multi_sheet_resolver.py -v
```

Expected: all tests in the file PASS.

- [ ] **Step 5: Run the full test suite**

```bash
pytest -v
```

Expected: all tests pass. If `test_resolver.py` has failures due to the old `_call_ollama` patch path, fix those two test functions by replacing:
```python
patch("quoro.resolver.semantic_resolver._call_ollama", ...)
```
with:
```python
patch("quoro.resolver.semantic_resolver._call_openai", ...)
```

- [ ] **Step 6: Commit**

```bash
git add src/quoro/normalizer/normalizer.py tests/test_multi_sheet_resolver.py
git commit -m "feat: output etichetta from sheet_label in normalizer, prevent cross-sheet merging"
```

---

## Self-Review Checklist

- [x] **Spec coverage**: sheet name as label ✓, unified column names across sheets ✓, inter-sheet relationship understanding ✓, sheets stay separate in output ✓, single-sheet unchanged ✓
- [x] **No placeholders**: all steps have complete code
- [x] **Type consistency**: `sheet_label: str | None` used consistently in models, resolver, normalizer
- [x] **Routing**: single-sheet → `semantic_resolver.resolve()`, multi-sheet → `resolve_multi_sheet()` — no breakage
- [x] **Fallback**: LLM failure → per-sheet static resolver with sheet_name as label ✓
- [x] **`_enrich_with_static`**: preserves `sheet_label` in enriched doc ✓
- [x] **Normalizer grouping key**: includes `sheet_label` to prevent cross-sheet merge ✓
