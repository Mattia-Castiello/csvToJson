from __future__ import annotations

"""Resolver statico basato su matching fuzzy contro schemi YAML."""

import difflib

from quoro.models import Segment, TypedDocument


def _similarity(a: str, b: str) -> float:
    """Similarita testuale case-insensitive tra due stringhe."""

    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_match(col: str, synonyms: list[str]) -> float:
    """Restituisce il punteggio migliore tra una colonna e i suoi sinonimi."""

    return max((_similarity(col, syn) for syn in synonyms), default=0.0)


def _mandatory_matches(header: list[str], schema: dict, threshold: float) -> int:
    """Conta quanti campi obbligatori dello schema risultano coperti dall'header."""

    campi = schema.get("campi", {})
    count = 0
    for header_col in header:
        for field_name, field_def in campi.items():
            if not field_def.get("obbligatorio", False):
                continue
            synonyms = field_def.get("sinonimi", [field_name])
            if _best_match(header_col, synonyms) >= threshold:
                count += 1
                break
    return count


def resolve_static(
    segment: Segment,
    schemas: list[dict],
    threshold: float = 0.75,
) -> TypedDocument:
    """Risoluzione semantica deterministica senza LLM.

    Il tipo documento viene scelto tramite fuzzy matching delle intestazioni
    rispetto ai sinonimi definiti negli schemi YAML.
    """

    best_tipo = "sconosciuto"
    best_score = 0.0
    best_mandatory = -1
    best_schema: dict | None = None

    for schema in schemas:
        campi = schema.get("campi", {})
        matches = 0
        for header_col in segment.header:
            for field_name, field_def in campi.items():
                synonyms = field_def.get("sinonimi", [field_name])
                if _best_match(header_col, synonyms) >= threshold:
                    matches += 1
                    break
        score = matches / max(len(segment.header), 1)
        mandatory = _mandatory_matches(segment.header, schema, threshold)
        if score > best_score or (
            score > 0 and score == best_score and mandatory > best_mandatory
        ):
            best_score = score
            best_mandatory = mandatory
            best_tipo = schema.get("tipo", "sconosciuto")
            best_schema = schema

    canonical_fields: dict = {}
    extra_fields: dict = {}
    column_mapping: dict[str, str] = {}

    if best_schema and best_score > 0:
        campi = best_schema.get("campi", {})
        for header_col in segment.header:
            matched_field = None
            for field_name, field_def in campi.items():
                synonyms = field_def.get("sinonimi", [field_name])
                if _best_match(header_col, synonyms) >= threshold:
                    matched_field = field_name
                    break
            if matched_field:
                column_mapping[header_col] = matched_field
                canonical_fields[matched_field] = matched_field
            else:
                snake = header_col.lower().replace(" ", "_").replace(".", "_")
                extra_fields[header_col] = snake
                column_mapping[header_col] = snake

        # Metadata mapping
        meta_canonici = best_schema.get("metadati_canonici", {})
        mapped_meta: dict[str, str] = {}
        for meta_key, meta_val in segment.metadata.items():
            matched = None
            for canonical_meta, synonyms in meta_canonici.items():
                if _best_match(meta_key, synonyms) >= threshold:
                    matched = canonical_meta
                    break
            if matched:
                mapped_meta[matched] = meta_val
            else:
                snake = meta_key.lower().replace(" ", "_").replace(".", "_")
                mapped_meta[snake] = meta_val
        canonical_fields.update(mapped_meta)
    else:
        # No match — preserve original column names as snake_case
        for header_col in segment.header:
            snake = header_col.lower().replace(" ", "_").replace(".", "_")
            column_mapping[header_col] = snake
            extra_fields[header_col] = snake
        # Preserve metadata as-is
        for k, v in segment.metadata.items():
            snake = k.lower().replace(" ", "_").replace(".", "_")
            extra_fields[snake] = v

    # Build typed rows
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

    righe_senza_dati = []
    for s in segment.summary_rows:
        raw = s.get("raw", [])
        if raw:
            row_dict = {}
            for i, cell_val in enumerate(raw):
                if i < len(segment.header):
                    col_name = segment.header[i]
                    mapped_name = column_mapping.get(col_name, col_name)
                else:
                    mapped_name = f"col_{i}"
                if cell_val:
                    row_dict[mapped_name] = cell_val
            if row_dict:
                righe_senza_dati.append(row_dict)
        elif s.get("text"):
            righe_senza_dati.append({"aggregato": s["text"]})

    return TypedDocument(
        tipo=best_tipo,
        canonical_fields=canonical_fields,
        extra_fields=extra_fields,
        rows=rows,
        righe_senza_dati=righe_senza_dati,
        confidence_tipo=best_score,
        warnings=list(segment.warnings),
        resolver="fallback",
    )
