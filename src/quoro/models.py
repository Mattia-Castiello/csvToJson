from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawCell:
    """Rappresenta una cella grezza letta dal file sorgente.

    Oltre al valore testuale conserva alcuni metadati di formattazione utili
    all'Analyzer per stimare header, titoli di sezione e struttura tabellare.
    """

    value: str
    bold: bool = False
    bg_color: str | None = None
    font_size: float | None = None
    merged: bool = False


@dataclass
class RawSheet:
    """Rappresenta un foglio tabellare grezzo prima dell'analisi strutturale."""

    name: str
    rows: list[list[RawCell]]
    separator: str | None = None  # only CSV: "," ";" "\t"


@dataclass
class Segment:
    """Blocco tabellare strutturato estratto da un foglio.

    Un foglio puo contenere piu segmenti (es. tabelle multiple separate da
    righe vuote o titoli). Ogni segmento ha un header, righe dati pulite,
    metadati pre-header e warning diagnostici.
    """

    sheet_name: str
    metadata: dict[str, str]
    header: list[str]
    rows: list[list[str]]
    confidence: float
    warnings: list[str] = field(default_factory=list)
    raw_context: list[str] = field(default_factory=list)  # free-text pre-header lines
    summary_rows: list[dict] = field(
        default_factory=list
    )  # subtotals/totals: each {"text": str, "after_index": int | None}


@dataclass
class TypedDocument:
    """Documento semantico prodotto dal Resolver.

    Contiene tipo documento, campi canonici, righe mappate e metadati su
    confidenza e strategia di risoluzione usata (LLM, fallback statico o ibrido).
    """

    tipo: str
    canonical_fields: dict
    extra_fields: dict
    rows: list[dict]
    confidence_tipo: float
    warnings: list[str] = field(default_factory=list)
    resolver: str = "fallback"
    model: str | None = None
    righe_senza_dati: list[dict] = field(
        default_factory=list
    )  # rows disconnected from data context: summary rows, totals, stray labels
    sheet_label: str | None = (
        None  # label assigned by LLM from sheet name (multi-sheet files)
    )
