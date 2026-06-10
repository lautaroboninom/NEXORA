"""Helpers para comparar y elegir nombres de tipos de equipo."""

from .helpers import _norm_txt

_ACCENTED_CHARS = set("áéíóúüñÁÉÍÓÚÜÑ")


def clean_tipo_equipo(value) -> str:
    return " ".join(str(value or "").split()).strip()


def tipo_equipo_key(value) -> str:
    return _norm_txt(clean_tipo_equipo(value))


def tipo_equipo_score(value) -> int:
    text = clean_tipo_equipo(value)
    return sum(1 for ch in text if ch in _ACCENTED_CHARS)


def matching_rows(rows, value, field: str = "nombre"):
    key = tipo_equipo_key(value)
    if not key:
        return []
    return [row for row in (rows or []) if tipo_equipo_key(row.get(field)) == key]


def preferred_row(rows, value, field: str = "nombre", priority_field: str | None = None):
    key = tipo_equipo_key(value)
    if not key:
        return None

    best = None
    best_rank = None
    for idx, row in enumerate(rows or []):
        text = clean_tipo_equipo(row.get(field))
        if tipo_equipo_key(text) != key:
            continue
        priority = row.get(priority_field) if priority_field else 0
        try:
            priority = int(priority or 0)
        except (TypeError, ValueError):
            priority = 0
        rank = (tipo_equipo_score(text), -priority, -idx, -len(text))
        if best_rank is None or rank > best_rank:
            best = row
            best_rank = rank
    return best


def preferred_name(rows, value_field: str = "nombre", priority_field: str | None = None):
    grouped = {}
    ranks = {}
    for idx, row in enumerate(rows or []):
        text = clean_tipo_equipo(row.get(value_field))
        key = tipo_equipo_key(text)
        if not key:
            continue
        priority = row.get(priority_field) if priority_field else 0
        try:
            priority = int(priority or 0)
        except (TypeError, ValueError):
            priority = 0
        rank = (tipo_equipo_score(text), -priority, -idx, -len(text))
        if key not in grouped or rank > ranks[key]:
            grouped[key] = text
            ranks[key] = rank
    return grouped
