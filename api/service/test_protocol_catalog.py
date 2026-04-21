from __future__ import annotations

import copy
import json
import re
import unicodedata
from typing import Any


VALID_ENTRY_MODES = {"", "result_only", "measured_only"}
_TYPE_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_TEMPLATE_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def _norm(value: str) -> str:
    s = (value or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9\s/+-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def safe_json_doc(value: Any, default: Any):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return default
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default
    return default


def _unique_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen = set()
    for item in values:
        value = str(item or "").strip()
        key = _norm(value)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _normalize_aliases(raw_aliases: Any, type_key: str, display_name: str) -> list[str]:
    if not isinstance(raw_aliases, list):
        raise ValueError("aliases debe ser lista")
    aliases = _unique_strings([type_key, display_name, *raw_aliases])
    if not aliases:
        raise ValueError("aliases no puede quedar vacio")
    return aliases


def _normalize_references(raw_refs: Any, field_name: str = "references") -> list[dict[str, Any]]:
    if not isinstance(raw_refs, list):
        raise ValueError(f"{field_name} debe ser lista")
    out: list[dict[str, Any]] = []
    seen = set()
    for idx, ref in enumerate(raw_refs):
        if not isinstance(ref, dict):
            raise ValueError(f"{field_name}[{idx}] debe ser objeto")
        ref_id = str(ref.get("ref_id") or "").strip()
        if not ref_id:
            raise ValueError(f"{field_name}[{idx}].ref_id es requerido")
        if ref_id in seen:
            raise ValueError(f"{field_name} tiene ref_id duplicado: {ref_id}")
        seen.add(ref_id)
        out.append(
            {
                "ref_id": ref_id,
                "tipo": str(ref.get("tipo") or "").strip(),
                "titulo": str(ref.get("titulo") or "").strip(),
                "edicion": str(ref.get("edicion") or "").strip(),
                "anio": ref.get("anio"),
                "organismo_o_fabricante": str(ref.get("organismo_o_fabricante") or "").strip(),
                "url": str(ref.get("url") or "").strip(),
                "aplica_a": str(ref.get("aplica_a") or "").strip(),
            }
        )
    return out


def _normalize_sections(
    raw_sections: Any,
    valid_ref_ids: set[str],
    field_name: str = "sections",
) -> list[dict[str, Any]]:
    if not isinstance(raw_sections, list):
        raise ValueError(f"{field_name} debe ser lista")
    if not raw_sections:
        raise ValueError(f"{field_name} no puede estar vacio")
    out: list[dict[str, Any]] = []
    section_ids = set()
    item_keys = set()
    for s_idx, section in enumerate(raw_sections):
        if not isinstance(section, dict):
            raise ValueError(f"{field_name}[{s_idx}] debe ser objeto")
        sec_id = str(section.get("id") or "").strip()
        if not sec_id:
            raise ValueError(f"{field_name}[{s_idx}].id es requerido")
        if sec_id in section_ids:
            raise ValueError(f"{field_name} tiene id duplicado: {sec_id}")
        section_ids.add(sec_id)
        title = str(section.get("title") or "").strip()
        if not title:
            raise ValueError(f"{field_name}[{s_idx}].title es requerido")
        entry_mode = str(section.get("entry_mode") or "").strip().lower()
        if entry_mode not in VALID_ENTRY_MODES:
            raise ValueError(f"{field_name}[{s_idx}].entry_mode invalido")
        raw_items = section.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError(f"{field_name}[{s_idx}].items debe ser lista no vacia")
        items_out: list[dict[str, Any]] = []
        for i_idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f"{field_name}[{s_idx}].items[{i_idx}] debe ser objeto")
            key = str(item.get("key") or "").strip()
            if not key:
                raise ValueError(f"{field_name}[{s_idx}].items[{i_idx}].key es requerido")
            if key in item_keys:
                raise ValueError(f"key de item duplicada: {key}")
            item_keys.add(key)
            label = str(item.get("label") or "").strip()
            if not label:
                raise ValueError(f"{field_name}[{s_idx}].items[{i_idx}].label es requerido")
            raw_ref_ids = item.get("ref_ids")
            raw_ref_ids = raw_ref_ids if isinstance(raw_ref_ids, list) else []
            ref_ids: list[str] = []
            seen_refs = set()
            for rid in raw_ref_ids:
                ref_id = str(rid or "").strip()
                if not ref_id or ref_id in seen_refs:
                    continue
                if valid_ref_ids and ref_id not in valid_ref_ids:
                    raise ValueError(f"ref_id inexistente en item {key}: {ref_id}")
                seen_refs.add(ref_id)
                ref_ids.append(ref_id)
            items_out.append(
                {
                    "key": key,
                    "label": label,
                    "target": str(item.get("target") or "").strip(),
                    "unit": str(item.get("unit") or "").strip(),
                    "ref_ids": ref_ids,
                }
            )
        out.append(
            {
                "id": sec_id,
                "title": title,
                "entry_mode": entry_mode,
                "items": items_out,
            }
        )
    return out


def _normalize_item_ref_ids(raw_item_ref_ids: Any) -> dict[str, list[str]]:
    if raw_item_ref_ids is None:
        return {}
    if not isinstance(raw_item_ref_ids, dict):
        raise ValueError("override.item_ref_ids debe ser objeto")
    out: dict[str, list[str]] = {}
    for key, val in raw_item_ref_ids.items():
        item_key = str(key or "").strip()
        if not item_key:
            continue
        if not isinstance(val, list):
            raise ValueError(f"override.item_ref_ids[{item_key}] debe ser lista")
        out[item_key] = _unique_strings(val)
    return out


def _normalize_override_set_fields(raw_set_fields: Any, base_ref_ids: set[str]) -> dict[str, Any]:
    if raw_set_fields is None:
        return {}
    if not isinstance(raw_set_fields, dict):
        raise ValueError("override.set_fields debe ser objeto")
    allowed = {
        "template_key",
        "template_version",
        "display_name",
        "default_instrumentos",
        "references",
        "sections",
    }
    unknown = [k for k in raw_set_fields.keys() if k not in allowed]
    if unknown:
        raise ValueError(f"override.set_fields tiene campos no permitidos: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    if "template_key" in raw_set_fields:
        out["template_key"] = str(raw_set_fields.get("template_key") or "").strip()
    if "template_version" in raw_set_fields:
        out["template_version"] = str(raw_set_fields.get("template_version") or "").strip()
    if "display_name" in raw_set_fields:
        out["display_name"] = str(raw_set_fields.get("display_name") or "").strip()
    if "default_instrumentos" in raw_set_fields:
        out["default_instrumentos"] = str(raw_set_fields.get("default_instrumentos") or "").strip()
    refs_out: list[dict[str, Any]] = []
    if "references" in raw_set_fields:
        refs_out = _normalize_references(raw_set_fields.get("references"), field_name="override.set_fields.references")
        out["references"] = refs_out
    valid_ref_ids = set(base_ref_ids)
    valid_ref_ids.update({r.get("ref_id") for r in refs_out if r.get("ref_id")})
    if "sections" in raw_set_fields:
        out["sections"] = _normalize_sections(
            raw_set_fields.get("sections"),
            valid_ref_ids=valid_ref_ids,
            field_name="override.set_fields.sections",
        )
    return out


def _normalize_overrides(raw_overrides: Any, base_ref_ids: set[str]) -> list[dict[str, Any]]:
    if raw_overrides is None:
        return []
    if not isinstance(raw_overrides, list):
        raise ValueError("overrides debe ser lista")
    out: list[dict[str, Any]] = []
    for idx, override in enumerate(raw_overrides):
        if not isinstance(override, dict):
            raise ValueError(f"overrides[{idx}] debe ser objeto")

        allowed = {
            "name",
            "active",
            "priority",
            "match",
            "set_fields",
            "references",
            "append_ref_to_all_items",
            "item_ref_ids",
        }
        unknown = [k for k in override.keys() if k not in allowed]
        if unknown:
            raise ValueError(f"overrides[{idx}] tiene campos no permitidos: {', '.join(sorted(unknown))}")

        match = override.get("match")
        if not isinstance(match, dict):
            raise ValueError(f"overrides[{idx}].match debe ser objeto")
        marca_contains = str(match.get("marca_contains") or "").strip()
        modelo_contains = str(match.get("modelo_contains") or "").strip()
        if not marca_contains and not modelo_contains:
            raise ValueError(f"overrides[{idx}] requiere marca_contains o modelo_contains")

        set_fields = _normalize_override_set_fields(override.get("set_fields"), base_ref_ids)
        override_refs = _normalize_references(
            override.get("references") or [],
            field_name=f"overrides[{idx}].references",
        )
        valid_ref_ids = set(base_ref_ids)
        valid_ref_ids.update({r.get("ref_id") for r in override_refs if r.get("ref_id")})
        valid_ref_ids.update({r.get("ref_id") for r in (set_fields.get("references") or []) if r.get("ref_id")})

        append_ref = str(override.get("append_ref_to_all_items") or "").strip()
        if append_ref and valid_ref_ids and append_ref not in valid_ref_ids:
            raise ValueError(f"overrides[{idx}].append_ref_to_all_items no existe en referencias")

        item_ref_ids = _normalize_item_ref_ids(override.get("item_ref_ids"))
        for item_key, refs in item_ref_ids.items():
            for ref_id in refs:
                if valid_ref_ids and ref_id not in valid_ref_ids:
                    raise ValueError(f"overrides[{idx}].item_ref_ids[{item_key}] usa ref inexistente: {ref_id}")

        try:
            priority = int(override.get("priority") if "priority" in override else 0)
        except Exception:
            raise ValueError(f"overrides[{idx}].priority debe ser entero")

        out.append(
            {
                "name": str(override.get("name") or "").strip(),
                "active": bool(override.get("active", True)),
                "priority": priority,
                "match": {
                    "marca_contains": marca_contains,
                    "modelo_contains": modelo_contains,
                },
                "set_fields": set_fields,
                "references": override_refs,
                "append_ref_to_all_items": append_ref,
                "item_ref_ids": item_ref_ids,
            }
        )
    return out


def normalize_protocol_document(
    payload: dict[str, Any],
    *,
    partial: bool = False,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload invalido")

    allowed = {
        "type_key",
        "template_key",
        "template_version",
        "display_name",
        "default_instrumentos",
        "aliases",
        "references",
        "sections",
        "overrides",
        "active",
    }
    unknown = [k for k in payload.keys() if k not in allowed]
    if unknown:
        raise ValueError(f"Campos no permitidos: {', '.join(sorted(unknown))}")

    merged = copy.deepcopy(existing or {})
    for key in allowed:
        if key in payload:
            merged[key] = payload[key]

    type_key = str(merged.get("type_key") or "").strip().lower()
    if not type_key or not _TYPE_KEY_RE.match(type_key):
        raise ValueError("type_key invalido (usar solo a-z, 0-9 y _)")

    template_key = str(merged.get("template_key") or "").strip().lower()
    if not template_key or not _TEMPLATE_KEY_RE.match(template_key):
        raise ValueError("template_key invalido (usar solo a-z, 0-9 y _)")

    template_version = str(merged.get("template_version") or "").strip()
    if not template_version:
        raise ValueError("template_version es requerido")

    display_name = str(merged.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("display_name es requerido")

    if "aliases" not in merged and not partial:
        merged["aliases"] = []
    if "references" not in merged:
        raise ValueError("references es requerido")
    if "sections" not in merged:
        raise ValueError("sections es requerido")

    aliases = _normalize_aliases(merged.get("aliases") or [], type_key, display_name)
    references = _normalize_references(merged.get("references"), field_name="references")
    base_ref_ids = {r.get("ref_id") for r in references if r.get("ref_id")}
    sections = _normalize_sections(merged.get("sections"), valid_ref_ids=base_ref_ids, field_name="sections")
    overrides = _normalize_overrides(merged.get("overrides") or [], base_ref_ids=base_ref_ids)

    return {
        "type_key": type_key,
        "template_key": template_key,
        "template_version": template_version,
        "display_name": display_name,
        "default_instrumentos": str(merged.get("default_instrumentos") or "").strip(),
        "aliases": aliases,
        "references": references,
        "sections": sections,
        "overrides": overrides,
        "active": bool(merged.get("active", True)),
    }


def serialize_protocol_row(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    doc = safe_json_doc((row or {}).get("doc"), {})
    if not isinstance(doc, dict):
        doc = {}
    data = {
        "id": row.get("id"),
        "type_key": str(doc.get("type_key") or row.get("type_key") or "").strip(),
        "template_key": str(doc.get("template_key") or row.get("template_key") or "").strip(),
        "template_version": str(doc.get("template_version") or "").strip(),
        "display_name": str(doc.get("display_name") or "").strip(),
        "active": bool(row.get("active", doc.get("active", True))),
    }
    if detail:
        data["default_instrumentos"] = str(doc.get("default_instrumentos") or "").strip()
        data["aliases"] = doc.get("aliases") if isinstance(doc.get("aliases"), list) else []
        data["references"] = doc.get("references") if isinstance(doc.get("references"), list) else []
        data["sections"] = doc.get("sections") if isinstance(doc.get("sections"), list) else []
        data["overrides"] = doc.get("overrides") if isinstance(doc.get("overrides"), list) else []
    return data

