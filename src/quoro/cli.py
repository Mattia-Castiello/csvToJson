"""CLI di Quoro.

Questo modulo espone il comando `quoro parse` e orchestra l'intera pipeline:
lettura -> analisi -> risoluzione -> normalizzazione -> output JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from dotenv import find_dotenv, load_dotenv

from quoro.analyzer import analyze
from quoro.normalizer import serialize
from quoro.reader import read_file
from quoro.resolver import resolve
from quoro.resolver.schema_loader import load_schemas

load_dotenv(find_dotenv(usecwd=True))


# Definisce il gruppo comandi principale `quoro`.
@click.group()
def cli() -> None:
    """Entry-point gruppo comandi."""


@cli.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output",  
    "-o",  
    type=click.Path(path_type=Path),  
    default=None,  
    help="Save output to file instead of stdout", 
)
@click.option(
    "--verbose", "-v", is_flag=True, help="Show confidence scores and decisions"
)
@click.option(
    "--model",  
    default=None,  
    envvar="QUORO_MODEL",  
    help="Model override for semantic resolver", 
)
@click.option(
    "--schema",  
    "schema_dir",  
    type=click.Path(exists=True, file_okay=False, path_type=Path), 
    default=None,  
    envvar="QUORO_SCHEMA_DIR",  
    help="Custom schemas directory",  
)

def parse(
    file: Path,  # File input.
    output: Path | None,  # File output opzionale.
    verbose: bool,  # Flag log dettagliati.
    model: str | None,  # Override modello LLM.
    schema_dir: Path | None,  # Override directory schemi.
) -> None:
    """Esegue pipeline completa su FILE e stampa/salva JSON normalizzato."""
    try:
        # 1) Legge il file sorgente e lo converte in fogli grezzi uniformi.
        sheets = read_file(file)
        # Se verbose, stampa quante tabelle/fogli sono stati letti.
        if verbose:
            click.echo(
                f"[reader] {len(sheets)} sheet(s) read from {file.name}", err=True
            )

        # 2) Analizza la struttura (header, metadati, segmenti).
        segments = analyze(sheets)
        # Se verbose, stampa riepilogo di ogni segmento estratto.
        if verbose:
            for i, seg in enumerate(segments):
                click.echo(
                    f"[analyzer] segment {i+1}: sheet={seg.sheet_name!r} "
                    f"header={seg.header} confidence={seg.confidence:.2f}",
                    err=True,
                )

        # 3) Risolve semanticamente ogni segmento (LLM + fallback statico).
        documents = resolve(segments, schema_dir=schema_dir, model=model)
        # Se verbose, stampa tipo/confidenza/resolver scelto per ogni documento.
        if verbose:
            for i, doc in enumerate(documents):
                click.echo(
                    f"[resolver] doc {i+1}: tipo={doc.tipo!r} "
                    f"confidence_tipo={doc.confidence_tipo:.2f} resolver={doc.resolver}",
                    err=True,
                )

        # 4) Determina la directory schemi effettiva (opzione o default progetto).
        effective_schema_dir = (
            schema_dir or Path(__file__).parent.parent.parent / "schemas"
        )
        # Carica gli schemi YAML disponibili.
        schemas = load_schemas(effective_schema_dir)
        # Costruisce lookup `tipo -> schema` usato dal normalizer.
        schema_lookup = {s["tipo"]: s for s in schemas}

        # 5) Serializza i documenti in struttura JSON finale.
        result = serialize(documents, schema_lookup=schema_lookup)

        # Converte in stringa JSON pretty-printed.
        json_output = json.dumps(
            # Se un solo documento stampa oggetto; altrimenti array.
            result if len(result) > 1 else result[0] if result else {},
            ensure_ascii=False,  # Mantiene caratteri unicode leggibili.
            indent=2,  # Indentazione leggibile.
        )

        # Se e stato richiesto un file output, scrive su disco.
        if output:
            output.write_text(json_output, encoding="utf-8")
            # In verbose, conferma il path di salvataggio.
            if verbose:
                click.echo(f"[output] saved to {output}", err=True)
        else:
            # Altrimenti stampa il JSON su stdout.
            click.echo(json_output)

    # Gestione errori globale del comando.
    except Exception as exc:
        # Messaggio errore su stderr.
        click.echo(f"Error: {exc}", err=True)
        # Exit code non-zero per segnalare fallimento al chiamante.
        sys.exit(1)
