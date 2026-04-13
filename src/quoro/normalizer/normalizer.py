from __future__ import annotations

import re
from datetime import datetime

from quoro.models import TypedDocument

_DATE_FORMATS = [
    ("%d/%m/%Y", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("%d-%m-%Y", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("%m/%d/%Y", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("%d/%m/%y", re.compile(r"^\d{1,2}/\d{1,2}/\d{2}$")),
    ("%d-%m-%y", re.compile(r"^\d{1,2}-\d{1,2}-\d{2}$")),
]

_CURRENCY_SYMBOLS = re.compile(r"[€$£\s]")
_SUB_ITEM_PATTERN = re.compile(r"^sub[-\s]?item\b", re.IGNORECASE)
_TOTAL_ROW_PATTERN = re.compile(
    r"\b(totale(?:\s+generale)?|grand\s*total|total)\b",
    re.IGNORECASE,
)
_SUMMARY_LABEL_KEYS = ("etichetta", "descrizione", "aggregato")


def _normalize_number(value: str) -> float | int | str:
    """Try to parse as EU or US number; return original string if not numeric."""
    cleaned = _CURRENCY_SYMBOLS.sub("", value).strip()
    if not cleaned:
        return value

    # EU format: 1.234,56 → dot=thousands, comma=decimal
    eu_pattern = re.compile(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$")
    # US format: 1,234.56 → comma=thousands, dot=decimal
    us_pattern = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$")
    # Simple decimal with comma
    simple_comma = re.compile(r"^-?\d+(,\d+)$")
    # Simple decimal with dot
    simple_dot = re.compile(r"^-?\d+(\.\d+)?$")

    try:
        if eu_pattern.match(cleaned):
            normalized = cleaned.replace(".", "").replace(",", ".")
            num = float(normalized)
            return int(num) if num == int(num) and "," not in cleaned else num
        elif simple_comma.match(cleaned):
            normalized = cleaned.replace(",", ".")
            return float(normalized)
        elif us_pattern.match(cleaned) or simple_dot.match(cleaned):
            num = float(cleaned.replace(",", ""))
            return int(num) if num == int(num) and "." not in cleaned else num
    except (ValueError, OverflowError):
        pass
    return value


def _normalize_date(value: str) -> str:
    """Try to parse as date; return ISO 8601 string or original."""
    stripped = value.strip()
    for fmt, pattern in _DATE_FORMATS:
        if pattern.match(stripped):
            try:
                dt = datetime.strptime(stripped, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return value


def _to_snake_case(name: str) -> str:
    return re.sub(r"[\s\.\-/]+", "_", name.strip()).lower()


def _coerce_value(value: str, field_type: str | None) -> object:
    if not value or value.strip() == "":
        return None
    if field_type == "integer":
        result = _normalize_number(value)
        if isinstance(result, (int, float)):
            return int(result)
        return value
    if field_type == "float":
        result = _normalize_number(value)
        return result if isinstance(result, (int, float)) else value
    if field_type == "date":
        return _normalize_date(value)
    if field_type == "string":
        return value.strip() or None
    # Auto-detect
    num = _normalize_number(value)
    if isinstance(num, (int, float)):
        return num
    date = _normalize_date(value)
    if date != value:
        return date
    return value.strip() or None


def _find_summary_label(row: dict) -> str | None:
    for key in _SUMMARY_LABEL_KEYS:
        value = row.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    for value in row.values():
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None


def _split_top_level_totals(
    rows: list[dict],
) -> tuple[dict[str, dict], list[dict]]:
    top_level_totals: dict[str, dict] = {}
    remaining_rows: list[dict] = []

    for row in rows:
        label = _find_summary_label(row)
        if not label or not _TOTAL_ROW_PATTERN.search(label):
            remaining_rows.append(row)
            continue

        payload = {
            key: value
            for key, value in row.items()
            if key not in _SUMMARY_LABEL_KEYS and value is not None
        }
        if not payload:
            remaining_rows.append(row)
            continue

        base_key = _to_snake_case(label)
        top_level_key = base_key
        suffix = 2
        while top_level_key in top_level_totals:
            top_level_key = f"{base_key}_{suffix}"
            suffix += 1
        top_level_totals[top_level_key] = payload

    return top_level_totals, remaining_rows


_ORDER_FIELD_CANDIDATES = (
    "order_ref",
    "ordine_riferimento",
    "riferimento_ordine",
    "ordine_di_riferimento",
    "ordine_rif",
)
_CODE_FIELD_CANDIDATES = (
    "codice_articolo",
    "item_code",
    "codice",
    "article_code",
    "product_code",
    "sku",
)
_DESCRIPTION_FIELD_CANDIDATES = (
    "descrizione",
    "description",
    "desc",
    "item_description",
)


def _extract_order_field(row: dict) -> tuple[str | None, str | None]:
    for key in _ORDER_FIELD_CANDIDATES:
        if key in row:
            val = row.get(key)
            if val is None:
                return key, None
            text = str(val).strip()
            if text:
                return key, text
            return key, None
    return None, None


def _set_order_field(row: dict, key: str | None, value: str) -> None:
    target_key = (
        key if key in row else (_ORDER_FIELD_CANDIDATES[0] if key is None else key)
    )
    row[target_key] = value


def _extract_text_field(
    row: dict, candidates: tuple[str, ...]
) -> tuple[str | None, str | None]:
    for key in candidates:
        if key in row:
            val = row.get(key)
            if val is None:
                return key, None
            text = str(val).strip()
            if text:
                return key, text
            return key, None
    return None, None


def _has_sub_item_rows(rows: list[dict]) -> bool:
    for row in rows:
        _, descr = _extract_text_field(row, _DESCRIPTION_FIELD_CANDIDATES)
        if descr and _SUB_ITEM_PATTERN.match(descr):
            return True
    return False


def serialize(
    documents: list[TypedDocument],
    schema_lookup: dict[str, dict] | None = None,
) -> list[dict]:
    """Convert TypedDocuments to flat JSON dicts, merging same-type/same-schema ones."""
    if not documents:
        return []

    # Group by (tipo, sorted canonical field keys, sheet_label) for merge decision
    groups: dict[tuple, list[TypedDocument]] = {}
    for doc in documents:
        key = (doc.tipo, tuple(sorted(doc.canonical_fields.keys())), doc.sheet_label)
        groups.setdefault(key, []).append(doc)

    results: list[dict] = []
    for group_docs in groups.values():
        if len(group_docs) == 1:
            results.append(_serialize_single(group_docs[0], schema_lookup))
        else:
            # Same tipo + same canonical fields → merge rows
            merged = _merge_documents(group_docs, schema_lookup)
            results.append(merged)

    return results


def _get_field_types(
    doc: TypedDocument, schema_lookup: dict[str, dict] | None
) -> dict[str, str]:
    if not schema_lookup or doc.tipo not in schema_lookup:
        return {}
    campi = schema_lookup[doc.tipo].get("campi", {})
    return {fname: fdef.get("tipo", "string") for fname, fdef in campi.items()}


def _serialize_single(
    doc: TypedDocument, schema_lookup: dict[str, dict] | None
) -> dict:
    field_types = _get_field_types(doc, schema_lookup)
    meta: dict = {
        k: v for k, v in doc.canonical_fields.items() if k not in _row_keys(doc)
    }
    rows = []
    for raw_row in doc.rows:
        coerced: dict = {}
        for k, v in raw_row.items():
            ftype = field_types.get(k)
            coerced[k] = _coerce_value(str(v) if v is not None else "", ftype)
        rows.append(coerced)

    if _has_sub_item_rows(rows):
        _attach_parent_refs(rows)

    out: dict = {"tipo": doc.tipo}
    if doc.sheet_label:
        out["etichetta"] = doc.sheet_label
    meta.pop("etichetta", None)  # etichetta è gestita da sheet_label
    out.update(meta)
    out["righe"] = rows
    if doc.righe_senza_dati:
        # Coerce values in summary/total rows too
        coerced_rsd = []
        for raw_row in doc.righe_senza_dati:
            coerced: dict = {}
            for k, v in raw_row.items():
                ftype = field_types.get(k)
                coerced[k] = _coerce_value(str(v) if v is not None else "", ftype)
            coerced_rsd.append(coerced)
        top_level_totals, remaining_rsd = _split_top_level_totals(coerced_rsd)
        out.update(top_level_totals)
        if remaining_rsd:
            out["righe_senza_dati"] = remaining_rsd
    out["_meta"] = {
        "confidence_struttura": (
            round(sum(r.get("_conf", 0) for r in rows) / max(len(rows), 1), 2)
            if rows and "_conf" in rows[0]
            else None
        ),
        "confidence_tipo": round(doc.confidence_tipo, 2),
        "resolver": doc.resolver,
        "modello": doc.model,
        "warnings": doc.warnings,
    }
    # Clean None confidence_struttura
    if out["_meta"]["confidence_struttura"] is None:
        del out["_meta"]["confidence_struttura"]
    return out


def _row_keys(doc: TypedDocument) -> set[str]:
    if not doc.rows:
        return set()
    return set(doc.rows[0].keys())


def _merge_documents(
    docs: list[TypedDocument], schema_lookup: dict[str, dict] | None
) -> dict:
    field_types = _get_field_types(docs[0], schema_lookup)
    meta: dict = {
        k: v for k, v in docs[0].canonical_fields.items() if k not in _row_keys(docs[0])
    }
    all_rows = []
    all_warnings: list[str] = []
    for doc in docs:
        for raw_row in doc.rows:
            coerced: dict = {}
            for k, v in raw_row.items():
                ftype = field_types.get(k)
                coerced[k] = _coerce_value(str(v) if v is not None else "", ftype)
            all_rows.append(coerced)
        all_warnings.extend(doc.warnings)

    if _has_sub_item_rows(all_rows):
        _attach_parent_refs(all_rows)

    out: dict = {"tipo": docs[0].tipo}
    if docs[0].sheet_label:
        out["etichetta"] = docs[0].sheet_label
    meta.pop("etichetta", None)  # etichetta è gestita da sheet_label
    out.update(meta)
    out["righe"] = all_rows
    all_righe_senza_dati = []
    for doc in docs:
        for raw_row in doc.righe_senza_dati:
            coerced = {}
            for k, v in raw_row.items():
                ftype = field_types.get(k)
                coerced[k] = _coerce_value(str(v) if v is not None else "", ftype)
            all_righe_senza_dati.append(coerced)
    if all_righe_senza_dati:
        top_level_totals, remaining_rsd = _split_top_level_totals(all_righe_senza_dati)
        out.update(top_level_totals)
        if remaining_rsd:
            out["righe_senza_dati"] = remaining_rsd
    out["_meta"] = {
        "confidence_tipo": round(sum(d.confidence_tipo for d in docs) / len(docs), 2),
        "resolver": docs[0].resolver,
        "modello": docs[0].model,
        "warnings": all_warnings,
    }
    return out


def _attach_parent_refs(rows: list[dict]) -> None:
    """Assign parent_ref for sub-item rows lacking their own codice_articolo."""
    last_code: str | None = None
    last_order_key: str | None = None
    last_order_value: str | None = None
    for row in rows:
        _, code = _extract_text_field(row, _CODE_FIELD_CANDIDATES)
        order_key, order_val = _extract_order_field(row)
        if order_val:
            last_order_key = order_key or last_order_key or _ORDER_FIELD_CANDIDATES[0]
            last_order_value = order_val

        if code:
            last_code = code
            row.pop("parent_ref", None)
            if not order_val and last_order_value:
                _set_order_field(row, last_order_key or order_key, last_order_value)
            continue

        order_empty = (
            not (order_val and order_val.strip())
            if isinstance(order_val, str)
            else order_val in (None, "")
        )
        _, descr = _extract_text_field(row, _DESCRIPTION_FIELD_CANDIDATES)
        if last_code and order_empty and descr and _SUB_ITEM_PATTERN.match(descr):
            row["parent_ref"] = last_code
            if last_order_value:
                _set_order_field(row, last_order_key or order_key, last_order_value)
