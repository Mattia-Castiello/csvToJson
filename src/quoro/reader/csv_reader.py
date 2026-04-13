from __future__ import annotations

"""Reader per file CSV/TSV/TXT.

Questo modulo normalizza encoding e separatore prima di creare la
rappresentazione interna uniforme (`RawSheet` / `RawCell`).
"""

import csv
import io
from pathlib import Path

import chardet

from quoro.models import RawCell, RawSheet


def _detect_encoding(path: Path) -> str:
    """Stima l'encoding del file usando chardet con fallback prudente.

    Se la confidenza e bassa, prova esplicitamente UTF-8 e poi latin-1
    per ridurre i falsi positivi su export legacy.
    """

    raw = path.read_bytes()
    result = chardet.detect(raw)
    encoding = result.get("encoding") or "utf-8"
    # Fallback chain: if chardet is uncertain, try utf-8 then latin-1
    if result.get("confidence", 0) < 0.7:
        for enc in ("utf-8", "latin-1"):
            try:
                raw.decode(enc)
                return enc
            except UnicodeDecodeError:
                continue
    return encoding


def _detect_separator(lines: list[str]) -> str:
    """Individua il separatore piu probabile tra virgola, punto e virgola e tab."""
    totals: dict[str, int] = {",": 0, ";": 0, "\t": 0}
    checked = 0
    for line in lines:
        if not line.strip():
            continue
        for sep in totals:
            totals[sep] += line.count(sep)
        checked += 1
        if checked >= 10:
            break
    return max(totals, key=lambda s: totals[s])


def read_csv(path: Path) -> list[RawSheet]:
    """Legge un file testuale delimitato e restituisce un singolo `RawSheet`.

    Ogni cella viene convertita in `RawCell` senza metadati grafici, perche i
    formati testuali non hanno informazioni di stile affidabili.
    """

    encoding = _detect_encoding(path)
    try:
        text = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")

    lines = text.splitlines()
    separator = _detect_separator(lines)

    reader = csv.reader(io.StringIO(text), delimiter=separator)
    rows: list[list[RawCell]] = []
    for row in reader:
        cells = [RawCell(value=cell.strip()) for cell in row]
        rows.append(cells)

    return [RawSheet(name="sheet1", rows=rows, separator=separator)]
