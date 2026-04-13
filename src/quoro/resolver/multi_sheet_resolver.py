from __future__ import annotations

import json
import os
import re
from pathlib import Path

from quoro.models import Segment, TypedDocument
from quoro.resolver.schema_loader import load_schemas
from quoro.resolver.semantic_resolver import _RESERVED_CANONICAL, _is_valid_canonical
from quoro.resolver.static_resolver import resolve_static

_DEFAULT_SCHEMA_DIR = Path(__file__).parent.parent.parent.parent / "schemas"
_CONFIDENCE_THRESHOLD = float(os.environ.get("QUORO_CONFIDENCE_THRESHOLD", "0.65"))


def _segment_label(segment: Segment) -> str:
    """Build a label for multi-sheet fallback segments from metadata when possible."""
    candidate_keys = {
        "delivery note",
        "delivery_note",
        "ddt",
        "bolla",
        "etichetta",
        "label",
    }
    for key, value in segment.metadata.items():
        if key.lower() in candidate_keys and value:
            return str(value)
    return segment.sheet_name


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

        interleaved_str = (
            "\n".join(interleaved_lines) if interleaved_lines else "  nessuna"
        )
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
        "2. Per ogni foglio: determina tipo, etichetta leggibile basata sul NOME DEL FOGLIO,",
        "   confidenza, mapping metadati.",
        "   etichetta: stringa leggibile in italiano, lowercase, derivata dal nome del foglio.",
        "   NON usare righe del CONTESTO INIZIALE come etichetta.",
        "   Es.: foglio 'Sheet Nordic' → etichetta 'ordini nordic'.",
        "   Es.: foglio 'Riepilogo' che somma altri fogli → etichetta 'riepilogo tabella fornitori'.",
        "",
        "3. RIGHE — includi TUTTE le righe di ogni foglio, anche intestazioni, metadati, totali, note.",
        "   Ogni riga va mappata ai suoi campi canonici usando mapping_colonne_globale.",
        "   Non scartare nessuna riga: tutto ciò che è nel documento deve comparire in 'righe'.",
        "   Usa sempre il contesto precedente/successivo: se una riga sub-item ha riferimenti vuoti,",
        "   collegala alla riga padre precedente ereditando order_ref/ordine_riferimento e parent_ref.",
        "",
        "4. mapping_raw_context (per foglio): per ogni riga del CONTESTO INIZIALE che non è già",
        "   coperta da mapping_metadati (es. titoli liberi, periodi), assegna un nome canonico",
        "   snake_case in mapping_raw_context.",
        '   Esempio: \'Listino Prezzi Aggiornato - Marzo 2026\' → {"Listino Prezzi Aggiornato - Marzo 2026": "titolo"}',
        "   Se una riga è già in mapping_metadati, non duplicarla.",
        "",
        "Rispondi ESATTAMENTE in questo formato JSON (nessun testo aggiuntivo):",
        "{",
        '  "mapping_colonne_globale": {"NomeColonnaFoglio1": "nome_canonico", "NomeColonnaFoglio2": "nome_canonico", ...},',
        '  "fogli": [',
        "    {",
        '      "indice": 0,',
        '      "nome_foglio": "...",',
        '      "etichetta": "...",',
        '      "tipo": "...",',
        '      "confidence": 0.0,',
        '      "mapping_metadati": {},',
        '      "mapping_raw_context": {}',
        "    }",
        "  ]",
        "}",
    ]

    return "\n".join(lines)


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
        llm_etichetta = foglio_result.get("etichetta", "")
        # Use LLM etichetta only when it looks like a real label (not a raw_context line used as label)
        etichetta = (
            llm_etichetta
            if llm_etichetta and llm_etichetta != segment.sheet_name
            else _segment_label(segment)
        )
        meta_map: dict[str, str] = foglio_result.get("mapping_metadati", {})

        # Validate global column mappings — reject data values used as canonical names
        col_map = {
            col: (
                global_col_map.get(col, "")
                if _is_valid_canonical(global_col_map.get(col, ""))
                and global_col_map.get(col, "") not in _RESERVED_CANONICAL
                else ""
            )
            for col in segment.header
        }
        mapping_coverage = _compute_mapping_coverage(segment, col_map)
        composite = _compute_composite_confidence(
            llm_confidence, mapping_coverage, tipo
        )

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
            raw_canonical = meta_map.get(k, "")
            if (
                raw_canonical
                and _is_valid_canonical(raw_canonical)
                and raw_canonical not in _RESERVED_CANONICAL
            ):
                canonical_key = raw_canonical
            else:
                canonical_key = re.sub(r"[\s\.\-/]+", "_", k.strip()).lower()
            mapped_meta[canonical_key] = v
        canonical_fields.update(mapped_meta)

        # Process raw_context lines not already covered by metadata
        raw_ctx_map: dict[str, str] = foglio_result.get("mapping_raw_context", {})
        metadata_values = set(segment.metadata.values())
        for ctx_line in segment.raw_context:
            if any(mv in ctx_line for mv in metadata_values):
                continue
            canonical = raw_ctx_map.get(ctx_line)
            if (
                canonical
                and _is_valid_canonical(canonical)
                and canonical not in _RESERVED_CANONICAL
            ):
                canonical_fields[canonical] = ctx_line
            else:
                fallback = (
                    "titolo"
                    if "titolo" not in canonical_fields
                    else f"contesto_{len(canonical_fields)}"
                )
                canonical_fields.setdefault(fallback, ctx_line)

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

        righe_senza_dati = []
        for s in segment.summary_rows:
            raw = s.get("raw", [])
            if raw:
                row_dict = {}
                for j, cell_val in enumerate(raw):
                    if j < len(segment.header):
                        col_name = segment.header[j]
                        mapped_name = column_mapping.get(col_name, col_name)
                    else:
                        mapped_name = f"col_{j}"
                    if cell_val:
                        row_dict[mapped_name] = cell_val
                if row_dict:
                    righe_senza_dati.append(row_dict)
            elif s.get("text"):
                righe_senza_dati.append({"aggregato": s["text"]})

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
                righe_senza_dati=righe_senza_dati,
                confidence_tipo=composite,
                warnings=warnings,
                resolver="openai",
                model=model,
                sheet_label=etichetta,
            )
        )

    return documents


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
        fallback_docs: list[TypedDocument] = []
        for seg in segments:
            doc = resolve_static(seg, schemas)
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
                    sheet_label=_segment_label(seg),
                )
            )
        return fallback_docs

    docs = _apply_multi_llm_mapping(segments, llm_result, model)

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
    """When LLM confidence is low, use static resolver as second opinion."""
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
        righe_senza_dati=llm_doc.righe_senza_dati,
        confidence_tipo=final_confidence,
        warnings=warnings,
        resolver="openai+yaml",
        model=llm_doc.model,
        sheet_label=llm_doc.sheet_label,
    )
