from __future__ import annotations

"""Analyzer strutturale.

Trasforma fogli grezzi in segmenti tabellari affidabili, stimando dove
inizia una tabella, quali righe sono metadati e quali vanno trattate
come dati o riepiloghi.
"""

import re

from quoro.models import RawCell, RawSheet, Segment

_SUMMARY_PATTERN = re.compile(
    r"(totale|subtotale|sotto\s*totale|riepilogo|grand\s*total|total|subtotal)",
    re.IGNORECASE,
)
_KV_PATTERN = re.compile(r"^([^:;]+)[;:](.+)$")
_PRODUCT_CODE_PATTERN = re.compile(r"^[A-Z]{1,5}-\d", re.IGNORECASE)
_CURRENCY_PATTERN = re.compile(r"[€$£]")
_SUB_ITEM_PATTERN = re.compile(r"^sub[-\s]?item", re.IGNORECASE)


def _is_numeric(value: str) -> bool:
    """Verifica se una stringa puo essere interpretata come numero."""

    cleaned = value.strip().lstrip("€$£").replace(".", "").replace(",", ".").strip()
    try:
        float(cleaned)
        return True
    except ValueError:
        return False


def _row_values(row: list[RawCell]) -> list[str]:
    """Estrae il valore testuale di tutte le celle di riga."""

    return [cell.value for cell in row]


def _row_density(row: list[RawCell]) -> float:
    """Calcola la densita di celle non vuote in una riga."""

    if not row:
        return 0.0
    non_empty = sum(1 for c in row if c.value.strip())
    return non_empty / len(row)


def _text_ratio(row: list[RawCell]) -> float:
    """Stima la quota di celle testuali rispetto alle celle valorizzate."""

    non_empty = [c for c in row if c.value.strip()]
    if not non_empty:
        return 0.0
    text_cells = sum(1 for c in non_empty if not _is_numeric(c.value))
    return text_cells / len(non_empty)


def _type_consistency(header: list[RawCell], data_rows: list[list[RawCell]]) -> float:
    """Misura la coerenza di tipo per colonna nelle righe sotto l'header.

    Per ogni colonna valuta quanto prevale il tipo numerico o testuale:
    una colonna \"stabile\" (quasi tutta numerica o quasi tutta testuale)
    aumenta la probabilita che l'header individuato sia corretto.
    """
    if not data_rows or not header:
        return 0.0
    n_cols = len(header)
    scores: list[float] = []
    for col_idx in range(n_cols):
        values = [
            row[col_idx].value
            for row in data_rows
            if col_idx < len(row) and row[col_idx].value.strip()
        ]
        if not values:
            continue
        numeric_count = sum(1 for v in values if _is_numeric(v))
        ratio = max(numeric_count, len(values) - numeric_count) / len(values)
        scores.append(ratio)
    return sum(scores) / len(scores) if scores else 0.0


def _header_score(
    row: list[RawCell],
    next_rows: list[list[RawCell]],
) -> float:
    """Assegna uno score di probabilita che la riga sia un header tabellare."""

    density = _row_density(row) * 0.30
    text = _text_ratio(row) * 0.25
    consistency = _type_consistency(row, next_rows) * 0.20
    bold_bonus = 0.15 if any(c.bold for c in row if c.value.strip()) else 0.0
    bg_bonus = (
        0.10
        if any(
            c.bg_color is not None or (c.font_size is not None and c.font_size > 11)
            for c in row
            if c.value.strip()
        )
        else 0.0
    )
    return density + text + consistency + bold_bonus + bg_bonus


def _find_header_row(rows: list[list[RawCell]], start: int = 0) -> tuple[int, float]:
    """Restituisce indice e score del miglior candidato header da `start` in poi."""
    best_idx = start
    best_score = -1.0
    for i in range(start, len(rows)):
        row = rows[i]
        if not any(c.value.strip() for c in row):
            continue
        next_rows = rows[i + 1 : i + 6]
        score = _header_score(row, next_rows)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx, best_score


def _compute_confidence(
    header_row_idx: int,
    header_score: float,
    data_rows: list[list[RawCell]],
    header: list[RawCell],
) -> float:
    """Calcola la confidenza globale del segmento estratto."""
    # Punto di partenza neutro; poi la confidenza viene corretta in base ai segnali.
    confidence = 0.5
    if header_score > 0.7:
        confidence += 0.3
    else:
        confidence -= 0.2
    if len(data_rows) > 1:
        consistency = _type_consistency(header, data_rows)
        if consistency > 0.8:
            confidence += 0.2
    if any(c.bold for c in header if c.value.strip()):
        confidence += 0.15
    return min(max(confidence, 0.0), 1.0)


def _extract_raw_context(rows: list[list[RawCell]]) -> list[str]:
    """Estrae tutte le righe non vuote prima dell'header come contesto libero."""
    lines = []
    for row in rows:
        non_empty = [c.value.strip() for c in row if c.value.strip()]
        if non_empty:
            lines.append(" | ".join(non_empty))
    return lines


def _extract_metadata(rows: list[list[RawCell]]) -> dict[str, str]:
    """Estrae metadati da righe pre-header in formato key-value."""

    metadata: dict[str, str] = {}
    for row in rows:
        # Try joining non-empty cells
        non_empty = [c.value.strip() for c in row if c.value.strip()]
        if not non_empty:
            continue
        line = " ".join(non_empty)
        # Also try each cell individually
        for cell_val in non_empty:
            m = _KV_PATTERN.match(cell_val)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if key and val:
                    metadata[key] = val
                    break
        else:
            m = _KV_PATTERN.match(line)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if key and val:
                    metadata[key] = val
    return metadata


def _is_summary_row(row: list[RawCell], header_width: int) -> bool:
    """Riconosce righe di riepilogo/totale che non vanno trattate come dati."""

    values = [c.value.strip() for c in row]
    if not any(values):
        return False
    # Pattern: first cell empty or summary keyword + last cell numeric
    first_cells_empty = not values[0]
    last_cell_numeric = _is_numeric(values[-1]) if values else False
    has_summary_keyword = any(_SUMMARY_PATTERN.search(v) for v in values if v)
    density = sum(1 for v in values if v) / max(header_width, 1)

    return (
        (first_cells_empty and last_cell_numeric)
        or (has_summary_keyword and density < 0.5)
        or (first_cells_empty and has_summary_keyword)
    )


def _is_nested_item_row(values: list[str]) -> bool:
    """Riconosce righe annidate tipo sub-item (es. `Sub-item: ...`)."""
    for val in values:
        if val and _SUB_ITEM_PATTERN.match(val.strip()):
            return True
    return False


def _has_data_values(row: list[RawCell]) -> bool:
    """Rileva segnali tipici di riga dati (valuta o codice prodotto)."""
    for cell in row:
        val = cell.value.strip()
        if not val:
            continue
        if _CURRENCY_PATTERN.search(val):
            return True
        if _PRODUCT_CODE_PATTERN.match(val):
            return True
    return False


def _is_kv_row(row: list[RawCell]) -> bool:
    """Identifica righe in formato key-value da trattare come metadati."""
    non_empty = [c.value.strip() for c in row if c.value.strip()]
    if not non_empty:
        return False
    for cell_val in non_empty:
        if _KV_PATTERN.match(cell_val):
            return True
    # Also check joined string
    line = " ".join(non_empty)
    return bool(_KV_PATTERN.match(line))


def _is_section_title(row: list[RawCell]) -> bool:
    """Identifica possibili titoli di sezione (testuali e poco densi)."""
    values = [c.value.strip() for c in row]
    non_empty = [v for v in values if v]
    if not non_empty:
        return False
    # A KV row is metadata, not a section title
    if _is_kv_row(row):
        return False
    density = len(non_empty) / len(row)
    all_text = all(not _is_numeric(v) for v in non_empty)
    return all_text and density < 0.4


def _clean_data_rows(
    rows: list[list[RawCell]], header_width: int
) -> tuple[list[list[str]], list[str], list[dict]]:
    """Pulisce righe tabellari separando dati effettivi e summary rows.

    Le righe sub-item vengono preservate anche quando sono sparse, perche
    rappresentano dettagli gerarchici utili al downstream.
    """

    cleaned: list[list[str]] = []
    summary: list[dict] = []
    warnings: list[str] = []
    removed_empty = 0
    data_row_count = 0

    for row in rows:
        values = [c.value.strip() for c in row]
        # Le righe vuote vengono scartate subito.
        if not any(values):
            removed_empty += 1
            continue
        is_nested = _is_nested_item_row(values)

        # I summary vanno catturati prima del filtro densita, perche spesso
        # sono intenzionalmente sparsi (es. solo etichetta + totale).
        if not is_nested and _is_summary_row(row, header_width):
            summary.append(
                {
                    "text": " | ".join(v for v in values if v),
                    "raw": values,
                    "after_index": data_row_count - 1 if data_row_count > 0 else None,
                }
            )
            continue
        # Le righe molto sparse vengono rimosse, tranne i sub-item che devono
        # restare per preservare la gerarchia articolo -> sotto-articolo.
        density = sum(1 for v in values if v) / max(header_width, 1)
        if density < 0.3 and not is_nested:
            removed_empty += 1
            continue
        cleaned.append(values)
        data_row_count += 1

    if summary:
        warnings.append(f"righe riepilogo rilevate: {len(summary)}")
    if removed_empty:
        warnings.append(f"righe vuote/sparse rimosse: {removed_empty}")
    return cleaned, warnings, summary


def _segment_from_block(
    sheet_name: str,
    meta_rows: list[list[RawCell]],
    header_row: list[RawCell],
    data_rows: list[list[RawCell]],
    header_score: float,
) -> Segment:
    """Costruisce un `Segment` a partire da blocco metadata/header/data."""
    # metadata/raw_context vengono estratti dalle righe che precedono l'header.
    metadata = _extract_metadata(meta_rows)
    raw_context = _extract_raw_context(meta_rows)
    header = [c.value.strip() for c in header_row]
    cleaned_rows, warnings, summary_rows = _clean_data_rows(data_rows, len(header))
    confidence = _compute_confidence(0, header_score, data_rows, header_row)
    return Segment(
        sheet_name=sheet_name,
        metadata=metadata,
        raw_context=raw_context,
        header=header,
        rows=cleaned_rows,
        confidence=confidence,
        warnings=warnings,
        summary_rows=summary_rows,
    )


def _analyze_sheet(sheet: RawSheet) -> list[Segment]:
    """Segmenta un foglio in una o piu tabelle strutturalmente coerenti."""

    rows = sheet.rows
    segments: list[Segment] = []

    i = 0
    meta_rows: list[list[RawCell]] = []

    while i < len(rows):
        row = rows[i]
        row_vals = [c.value.strip() for c in row]

        # Skip fully empty rows (but track as potential section boundary)
        if not any(row_vals):
            i += 1
            continue

        # Check if this row is a section title (not a header, not data)
        if _is_section_title(row):
            # Start collecting metadata for next segment
            meta_rows = [row]
            i += 1
            continue

        # Ogni riga non vuota viene valutata come potenziale header.
        next_rows = rows[i + 1 : i + 6]
        score = _header_score(row, next_rows)

        non_empty_count = sum(1 for c in row if c.value.strip())
        if (
            score >= 0.45
            and _text_ratio(row) >= 0.6
            and not _is_kv_row(row)
            and non_empty_count >= 2
        ):
            # Se la riga supera le soglie, inizia la raccolta del blocco dati.
            header_row = row
            data_rows: list[list[RawCell]] = []
            j = i + 1

            while j < len(rows):
                candidate = rows[j]
                candidate_vals = [c.value.strip() for c in candidate]

                # Una riga vuota puo segnare il termine se il lookahead trova
                # un nuovo header forte o un nuovo titolo di sezione.
                if not any(candidate_vals):
                    # Peek ahead — if next non-empty row has high header score, stop
                    lookahead = j + 1
                    while lookahead < len(rows) and not any(
                        c.value.strip() for c in rows[lookahead]
                    ):
                        lookahead += 1
                    if lookahead < len(rows):
                        next_score = _header_score(
                            rows[lookahead], rows[lookahead + 1 : lookahead + 6]
                        )
                        if next_score >= 0.45 or _is_section_title(rows[lookahead]):
                            break
                    j += 1
                    continue

                # I summary nel mezzo vengono mantenuti temporaneamente e filtrati
                # in modo centralizzato da `_clean_data_rows`.
                header_width = len(header_row)
                if _is_summary_row(candidate, header_width):
                    data_rows.append(candidate)
                    j += 1
                    continue

                # Un titolo di sezione chiude il blocco corrente e apre meta
                # per il prossimo segmento.
                if _is_section_title(candidate):
                    meta_rows = [candidate]
                    j += 1
                    break

                # Un nuovo header candidato chiude il blocco solo se non sembra
                # una riga dati (niente segnali valuta/codice).
                cand_score = _header_score(candidate, rows[j + 1 : j + 6])
                if (
                    cand_score >= 0.55
                    and _text_ratio(candidate) >= 0.7
                    and not _has_data_values(candidate)
                    and len(data_rows) > 0
                ):
                    break

                data_rows.append(candidate)
                j += 1

            if data_rows or header_row:
                segments.append(
                    _segment_from_block(
                        sheet.name, meta_rows, header_row, data_rows, score
                    )
                )
            meta_rows = []
            i = j
        else:
            # Se non e header, la riga resta nel preambolo metadati.
            meta_rows.append(row)
            i += 1

    return segments


def analyze(sheets: list[RawSheet]) -> list[Segment]:
    """Entry-point Analyzer: aggrega i segmenti estratti da tutti i fogli."""

    segments: list[Segment] = []
    for sheet in sheets:
        segments.extend(_analyze_sheet(sheet))
    return segments
