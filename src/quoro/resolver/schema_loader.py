from __future__ import annotations

"""Utility per il caricamento degli schemi YAML."""

from pathlib import Path

import yaml


def load_schemas(schema_dir: Path) -> list[dict]:
    """Carica tutti gli schemi `*.yaml` in ordine alfabetico di filename."""

    schemas = []
    for path in sorted(schema_dir.glob("*.yaml")):
        with path.open(encoding="utf-8") as f:
            schemas.append(yaml.safe_load(f))
    return schemas
