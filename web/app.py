"""Web UI per Quoro — upload file → pipeline → JSON."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Rende importabile il package quoro da src/
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from dotenv import find_dotenv, load_dotenv  # noqa: E402

load_dotenv(find_dotenv(usecwd=True))

from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, Response  # noqa: E402

from quoro.analyzer import analyze  # noqa: E402
from quoro.normalizer import serialize  # noqa: E402
from quoro.reader import read_file  # noqa: E402
from quoro.resolver import resolve  # noqa: E402
from quoro.resolver.schema_loader import load_schemas  # noqa: E402

app = FastAPI(title="Quoro Web")

_HTML = Path(__file__).parent / "index.html"
_SCHEMAS = _ROOT / "schemas"
_ALLOWED = {".csv", ".tsv", ".xlsx", ".xls"}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_HTML.read_text(encoding="utf-8"))


@app.post("/parse")
async def parse(file: UploadFile) -> Response:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED:
        raise HTTPException(
            400,
            f"Tipo file non supportato: '{ext}'. "
            f"Accettati: {', '.join(sorted(_ALLOWED))}",
        )

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        sheets = read_file(tmp_path)
        segments = analyze(sheets)
        documents = resolve(segments, schema_dir=_SCHEMAS)
        schema_lookup = {s["tipo"]: s for s in load_schemas(_SCHEMAS)}
        result = serialize(documents, schema_lookup=schema_lookup)
        output = result[0] if len(result) == 1 else result if result else {}
        # Serializzazione esplicita per preservare caratteri non-ASCII
        body = json.dumps(output, ensure_ascii=False, indent=2)
        return Response(content=body, media_type="application/json")
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
