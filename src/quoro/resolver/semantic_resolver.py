from __future__ import annotations

"""Resolver semantico singolo segmento.

Combina un mapping generato da LLM con un fallback statico YAML per garantire
robustezza anche in caso di errore rete o bassa confidenza.
"""

import json
import os
import re
from pathlib import Path

from quoro.models import Segment, TypedDocument
from quoro.resolver.schema_loader import load_schemas
from quoro.resolver.static_resolver import resolve_static

_DEFAULT_SCHEMA_DIR = Path(__file__).parent.parent.parent.parent / "schemas"
_CONFIDENCE_THRESHOLD = float(os.environ.get("QUORO_CONFIDENCE_THRESHOLD", "0.65"))

# Canonical names must be snake_case lowercase identifiers — not data values.
_CANONICAL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Fields reserved for the serializer — must not be overridden by LLM mappings.
_RESERVED_CANONICAL = frozenset({"etichetta", "tipo"})


def _is_valid_canonical(name: str) -> bool:
    """True se il nome è un identificatore snake_case valido, non un valore dati."""
    return bool(name) and bool(_CANONICAL_RE.match(name)) and len(name) <= 60


def _segment_label(segment: Segment) -> str:
    """Build a stable label for single-sheet segments using metadata when available."""
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


def _build_prompt(segment: Segment) -> str:
    """Costruisce il prompt LLM includendo contesto, dati campione e istruzioni."""

    meta_str = " | ".join(f"{k}: {v}" for k, v in segment.metadata.items())
    raw_ctx_str = "\n".join(segment.raw_context) if segment.raw_context else "nessuno"

    # Build a lookup: data-row-index → list of summary texts that follow it
    summary_by_index: dict[int, list[str]] = {}
    for s in segment.summary_rows:
        idx = s.get("after_index")
        key = idx if idx is not None else -1
        summary_by_index.setdefault(key, []).append(s["text"])

    # Interleaved view: data rows + summary markers in their original positions
    interleaved_lines: list[str] = []
    for agg_text in summary_by_index.get(-1, []):
        interleaved_lines.append(f"  [AGGREGATO prima dei dati]: {agg_text}")
    for i, row in enumerate(segment.rows[:10]):
        row_str = " | ".join(f"{h}: {v}" for h, v in zip(segment.header, row) if h)
        interleaved_lines.append(f"  Riga {i + 1}: {row_str}")
        for agg_text in summary_by_index.get(i, []):
            interleaved_lines.append(f"  [AGGREGATO dopo riga {i + 1}]: {agg_text}")
    if len(segment.rows) > 10:
        interleaved_lines.append(f"  ... ({len(segment.rows) - 10} righe omesse)")

    interleaved_str = "\n".join(interleaved_lines) if interleaved_lines else "  nessuna"
    warnings_str = "; ".join(segment.warnings) if segment.warnings else "nessuno"

    lines = [
        "Analizza la struttura di questo documento tabulare e rispondi SOLO con un JSON valido.",
        "",
        f"CONTESTO INIZIALE (righe prima dell'intestazione, titoli, note libere):\n{raw_ctx_str}",
        f"METADATI STRUTTURATI (key: value): {meta_str or 'nessuno'}",
        f"COLONNE INTESTAZIONE: {segment.header}",
        f"RIGHE DATI CON AGGREGATI IN POSIZIONE (campione, max 10):\n{interleaved_str}",
        f"TOTALE RIGHE DATI: {len(segment.rows)}",
        f"WARNING PARSER: {warnings_str}",
        "",
        "ISTRUZIONI:",
        "1. Determina il tipo di documento (es. fattura, listino_prezzi, ordine, tariffario, spedizioni, ecc.).",
        "   Usa snake_case per tutti i nomi canonici.",
        "",
        "2. RIGHE — includi TUTTE le righe del documento, anche quelle di intestazione, metadati, totali",
        "   o note. Ogni riga va mappata ai suoi campi canonici. Se una riga ha un solo campo testuale",
        "   (es. titolo del documento, nome fornitore, nota) mappala con una chiave descrittiva.",
        '   Esempi: \'Listino Prezzi Marzo 2026\' → {"titolo": "Listino Prezzi Marzo 2026"}',
        '           \'TOTALE GENERALE | 865,00 | 977,00\' → {"etichetta": "TOTALE GENERALE", "prezzo_scontato": 865.0, "prezzo_intero": 977.0}',
        "   Non scartare nessuna riga: tutto ciò che è nel documento originale deve comparire in 'righe'.",
        "   Analizza sempre la sequenza delle righe (precedenti e successive):",
        "   se trovi una riga sub-item con riferimenti vuoti, collegala alla riga padre precedente",
        "   ereditando order_ref/ordine_riferimento e impostando parent_ref con il codice_articolo padre.",
        "",
        "3. Valuta quanto sei sicuro dell'analisi con un valore da 0.0 a 1.0.",
        "",
        "4. CONTESTO INIZIALE — per ogni riga del CONTESTO INIZIALE che non è già coperta da",
        "   mapping_metadati (es. titoli liberi, periodi, intestazioni senza ':'), assegna un",
        "   nome canonico snake_case in mapping_raw_context.",
        '   Esempio: \'Listino Prezzi Aggiornato - Marzo 2026\' → {"Listino Prezzi Aggiornato - Marzo 2026": "titolo"}',
        "   Se una riga è già in mapping_metadati, non duplicarla qui.",
        "",
        "5. AGGREGATI — per ogni riga aggregata visibile nel documento (SOTTOTALE, TOTALE, GRAND TOTAL, ecc.),",
        "   fornisci mapping_aggregati dove la chiave è il testo etichetta della riga e il valore è il mapping",
        "   colonne → nomi canonici specifici per quella riga. Usa nomi che rispecchino il tipo di aggregazione.",
        '   Esempio: riga "TOTALE GENERALE | € 865,00 | € 977,00" con colonne [Prezzo Listino, Prezzo Scontato]',
        '   → {"TOTALE GENERALE": {"Prezzo Listino": "totale_listino", "Prezzo Scontato": "totale_scontato"}}',
        "   Se la riga è solo un'etichetta senza valori (es. SOTTOTALE MOBILI), mappa solo l'etichetta.",
        '   → {"SOTTOTALE MOBILI": {"Articolo": "etichetta_sezione"}}',
        "",
        "Rispondi ESATTAMENTE in questo formato JSON (nessun testo aggiuntivo):",
        "{",
        '  "tipo": "...",',
        '  "confidence": 0.0,',
        '  "mapping_colonne": {"NomeColonnaOriginale": "nome_canonico", ...},',
        '  "mapping_metadati": {"ChiaveMetadata": "nome_canonico", ...},',
        '  "mapping_raw_context": {"Testo originale riga": "nome_canonico", ...},',
        '  "mapping_aggregati": {"ETICHETTA_RIGA": {"NomeColonna": "nome_canonico", ...}, ...}',
        "}",
    ]
    return "\n".join(lines)


def _call_openai(prompt: str, model: str) -> dict | None:
    """Esegue chiamata al modello OpenAI e restituisce il JSON decodificato."""

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

        print(f"[openai error] {e}", file=sys.stderr)
        return None


def _compute_mapping_coverage(segment: Segment, col_map: dict[str, str]) -> float:
    """Fraction of header columns that the LLM explicitly mapped (not left empty)."""
    if not segment.header:
        return 0.0
    mapped = sum(1 for col in segment.header if col_map.get(col))
    return mapped / len(segment.header)


def _compute_composite_confidence(
    llm_confidence: float,
    mapping_coverage: float,
    tipo: str,
) -> float:
    """
    Composite confidence score (0.0–1.0) combining:
    - llm_confidence (0.5 weight): self-reported certainty from the LLM
    - mapping_coverage (0.4 weight): fraction of columns explicitly mapped
    - tipo_penalty (0.1 weight): penalise generic/unknown types
    """
    tipo_ok = 1.0 if tipo not in ("sconosciuto", "unknown", "", "?") else 0.0
    return llm_confidence * 0.5 + mapping_coverage * 0.4 + tipo_ok * 0.1


def _apply_llm_mapping(
    segment: Segment,
    llm_result: dict,
    model: str,
) -> TypedDocument:
    """Applica al segmento il mapping restituito dal modello LLM."""

    tipo = llm_result.get("tipo", "sconosciuto")
    llm_confidence = float(llm_result.get("confidence", 0.5))
    col_map: dict[str, str] = llm_result.get("mapping_colonne", {})
    meta_map: dict[str, str] = llm_result.get("mapping_metadati", {})

    mapping_coverage = _compute_mapping_coverage(segment, col_map)
    composite = _compute_composite_confidence(llm_confidence, mapping_coverage, tipo)

    canonical_fields: dict = {}
    extra_fields: dict = {}
    column_mapping: dict[str, str] = {}

    for col in segment.header:
        mapped = col_map.get(col)
        if mapped and _is_valid_canonical(mapped) and mapped not in _RESERVED_CANONICAL:
            column_mapping[col] = mapped
            canonical_fields[mapped] = mapped
        else:
            snake = col.lower().replace(" ", "_").replace(".", "_")
            column_mapping[col] = snake
            extra_fields[col] = snake

    mapped_meta: dict[str, str] = {}
    for k, v in segment.metadata.items():
        raw_canonical = meta_map.get(k, "")
        # Valida il nome canonico: se l'LLM ha restituito un valore dati anziché
        # un identificatore snake_case, usa il fallback derivato dalla chiave originale.
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

    # Righe raw_context non coperte da metadata (es. titoli liberi senza ':')
    raw_ctx_map: dict[str, str] = llm_result.get("mapping_raw_context", {})
    metadata_values = set(segment.metadata.values())
    for ctx_line in segment.raw_context:
        # Salta le righe già rappresentate nei metadati key-value
        if any(mv in ctx_line for mv in metadata_values):
            continue
        canonical = raw_ctx_map.get(ctx_line)
        # Valida e filtra nomi riservati come "etichetta"
        if (
            canonical
            and _is_valid_canonical(canonical)
            and canonical not in _RESERVED_CANONICAL
        ):
            canonical_fields[canonical] = ctx_line
        else:
            # Fallback: prima riga libera → 'titolo', successive → 'contesto_N'
            fallback = (
                "titolo"
                if "titolo" not in canonical_fields
                else f"contesto_{len(canonical_fields)}"
            )
            canonical_fields.setdefault(fallback, ctx_line)

    rows = []
    for raw_row in segment.rows:
        row_dict: dict = {}
        for i, cell_val in enumerate(raw_row):
            if i < len(segment.header):
                col_name = segment.header[i]
                mapped_name = column_mapping.get(col_name, col_name)
            else:
                mapped_name = f"col_{i}"
            row_dict[mapped_name] = cell_val
        rows.append(row_dict)

    agg_map: dict[str, dict] = llm_result.get("mapping_aggregati", {})
    # Build a lookup keyed by uppercase label text for case-insensitive matching
    agg_map_upper = {k.strip().upper(): v for k, v in agg_map.items()}

    righe_senza_dati = []
    for s in segment.summary_rows:
        raw = s.get("raw", [])
        if raw:
            # Find the label text in this row (first non-empty cell)
            label = next((v.strip() for v in raw if v.strip()), "")
            # Look up custom aggregate mapping for this row
            row_agg_map: dict = agg_map_upper.get(label.upper(), {})
            etichetta_set = False
            row_dict = {}
            for i, cell_val in enumerate(raw):
                if not cell_val:
                    continue
                if i < len(segment.header):
                    col_name = segment.header[i]
                    custom = row_agg_map.get(col_name)
                    if custom:
                        mapped_name = custom
                    elif not etichetta_set:
                        # No custom mapping for this column: first non-empty cell
                        # is the row label → use "etichetta" as neutral key
                        mapped_name = "etichetta"
                        etichetta_set = True
                    else:
                        mapped_name = column_mapping.get(col_name, col_name)
                else:
                    mapped_name = f"col_{i}"
                if _is_valid_canonical(mapped_name):
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

    return TypedDocument(
        tipo=tipo,
        canonical_fields=canonical_fields,
        extra_fields=extra_fields,
        rows=rows,
        righe_senza_dati=righe_senza_dati,
        confidence_tipo=composite,
        warnings=warnings,
        resolver="openai",
        model=model,
        sheet_label=_segment_label(segment),
    )


def resolve(
    segments: list[Segment],
    schema_dir: Path | None = None,
    model: str | None = None,
    confidence_threshold: float = _CONFIDENCE_THRESHOLD,
) -> list[TypedDocument]:
    """Risoluzione semantica per file a singolo segmento o segmento indipendente."""

    schema_dir = schema_dir or Path(
        os.environ.get("QUORO_SCHEMA_DIR", str(_DEFAULT_SCHEMA_DIR))
    )
    model = model or os.environ.get("QUORO_MODEL", "gpt-5.4-nano")
    schemas = load_schemas(schema_dir)

    results: list[TypedDocument] = []
    for segment in segments:
        prompt = _build_prompt(segment)
        try:
            llm_result = _call_openai(prompt, model)
        except Exception:
            llm_result = None

        if llm_result:
            doc = _apply_llm_mapping(segment, llm_result, model)
            if doc.confidence_tipo < confidence_threshold:
                # Low confidence: enrich with YAML static resolver as second opinion
                static_doc = resolve_static(segment, schemas)
                doc = _enrich_with_static(doc, static_doc, confidence_threshold)
            results.append(doc)
        else:
            doc = resolve_static(segment, schemas)
            doc.sheet_label = _segment_label(segment)
            results.append(doc)

    return results


def _enrich_with_static(
    llm_doc: TypedDocument,
    static_doc: TypedDocument,
    threshold: float,
) -> TypedDocument:
    """
    When LLM confidence is low, use the static resolver as a second opinion:
    - If static found a known tipo with reasonable confidence, prefer it for tipo
    - Fill any unmapped columns (extra_fields) with static mappings where available
    - Keep LLM rows (already built), just fix the column names where static is better
    """
    warnings = list(llm_doc.warnings)

    # Decide final tipo: use static if it matched a known schema better
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

    # Enrich extra_fields: replace snake-cased originals with static canonical names
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

    # Rebuild rows with enriched mapping where static provided a better name
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
    )
