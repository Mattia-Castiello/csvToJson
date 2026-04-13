"""Dispatcher del layer Reader.

Instrada il file al parser corretto in base all'estensione.
"""

from pathlib import Path

from quoro.models import RawSheet
from quoro.reader.csv_reader import read_csv
from quoro.reader.excel_reader import read_excel


def read_file(path: str | Path) -> list[RawSheet]:
    """Legge un file tabellare e restituisce uno o piu fogli grezzi.

    CSV/TSV/TXT vengono trattati come un unico foglio virtuale.
    I file Excel producono un `RawSheet` per worksheet.
    """

    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xls", ".xlsm"):
        return read_excel(p)
    elif suffix in (".csv", ".tsv", ".txt"):
        return read_csv(p)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")
