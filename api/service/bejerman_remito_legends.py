"""Composición visual de las leyendas de remitos Bejerman.

Este módulo reúne los textos visibles que acompañan a los renglones de
artículos. Cada circuito conserva su información propia, pero todos usan la
misma presentación de la OS y evitan repetir la partida/serie que ya aparece
en su columna del remito.

No construye el payload del SDK ni modifica stock: esa serialización permanece
en :mod:`service.bejerman_sdk`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


SERVICE_INGRESS_TITLE = "Se recibe para servicio técnico:"

SERVICE_RELEASE_RESOLUTION_LABELS = {
    "controlado sin defecto": "controlado sin defecto",
    "controlado_sin_defecto": "controlado sin defecto",
    "no se encontro falla": "controlado sin defecto",
    "no_se_encontro_falla": "controlado sin defecto",
    "no reparado": "sin reparar",
    "no_reparado": "sin reparar",
    "sin reparar": "sin reparar",
    "sin_reparar": "sin reparar",
    "reparado": "reparado",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalized(value: Any) -> str:
    decomposed = unicodedata.normalize("NFD", _text(value).lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", without_accents).strip()


def format_os_reference(value: Any) -> str:
    """Normaliza un identificador de orden de servicio a ``OS 00013``."""

    raw = _text(value)
    if not raw:
        return ""
    match = re.fullmatch(r"(?:OS\s*[-:]?\s*)?(\d+)", raw, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\bOS[\s-]*(\d+)\b", raw, flags=re.IGNORECASE)
    if not match:
        return ""
    return f"OS {int(match.group(1)):05d}"


def format_os_badge(value: Any) -> str:
    """Devuelve la referencia de OS destacada, sin separadores sueltos."""

    reference = format_os_reference(value)
    return f"- {reference} -" if reference else ""


def service_ingress_main_legend(ingreso_id: Any) -> str:
    """Leyenda principal para un RIS/RDA/RDN de un solo equipo."""

    return " ".join(part for part in ("Ingreso a servicio técnico", format_os_badge(ingreso_id)) if part)


def service_ingress_reason_legend(
    equipment: dict[str, Any] | None,
    *,
    payload: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> str:
    """Motivo visible de un ingreso, respetando perfiles RDA/RDN."""

    equipment = equipment or {}
    payload = payload or {}
    profile = profile or {}
    reason = (
        _text(equipment.get("repairReason"))
        or _text(equipment.get("motivo"))
        or _text(payload.get("repairReason"))
        or _text(payload.get("motivo"))
    )
    profile_type = _text(profile.get("type")).upper()
    profile_key = _text(profile.get("key")).lower()
    if not reason and (profile_key in {"rda", "rdn"} or profile_type in {"RDA", "RDN"}):
        reason = _text(profile.get("reason"))
    return f"Motivo: {reason}" if reason else ""


def service_ingress_equipment_legend_lines(
    equipment: dict[str, Any] | None,
    *,
    os_text: Any = "",
    partida: Any = "",
    reason: str = "",
) -> list[str]:
    """Datos complementarios de un equipo recibido.

    La serie o partida nunca se agrega a la leyenda: ya se imprime en la
    columna ``Item_Partida`` del artículo. El número interno se conserva cuando
    aporta un identificador distinto.
    """

    equipment = equipment or {}
    serial = _text(equipment.get("serial"))
    internal = _text(equipment.get("internalNumber"))
    identifier = _text(partida) or serial or internal
    lines: list[str] = []
    os_badge = format_os_badge(os_text)
    if os_badge:
        lines.append(os_badge)
    if internal and internal != identifier:
        lines.append(f"Interno: {internal}")
    if _text(reason):
        lines.append(_text(reason))
    return lines


def accessory_legend_lines(accessories: Any) -> list[str]:
    text = _text(accessories)
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"\s*[;,]\s*", text) if part.strip()]
    if not parts:
        parts = [text]
    return [f"Accesorios: {part}" for part in parts]


def delivery_order_legend(order: dict[str, Any]) -> str:
    """Encabezado breve para RT/RTA/RTN sin un renglón separador artificial."""

    order_number = _text(order.get("orderNumber"))
    return f"- Orden {order_number} -" if order_number else ""


def service_release_resolution_label(value: Any) -> str:
    raw = _normalized(value).replace("-", " ").replace("_", " ")
    compact = raw.replace(" ", "_")
    return SERVICE_RELEASE_RESOLUTION_LABELS.get(compact) or SERVICE_RELEASE_RESOLUTION_LABELS.get(raw) or "reparado"


def service_release_legend(order: dict[str, Any]) -> str:
    resolution = (
        order.get("serviceResolution")
        or order.get("rawPedido")
        or order.get("resolution")
        or order.get("resolucion")
    )
    return f"Se entrega {service_release_resolution_label(resolution)}"


def service_release_order_reference(order: dict[str, Any], item: dict[str, Any] | None = None) -> str:
    item = item or {}
    candidates = (
        item.get("ingresoId"),
        item.get("ingreso_id"),
        item.get("sourceReference"),
        item.get("source_reference"),
        order.get("ingresoId"),
        order.get("ingreso_id"),
        order.get("sourceReference"),
        order.get("source_reference"),
        order.get("orderNumber"),
        order.get("order_number"),
    )
    for candidate in candidates:
        reference = format_os_reference(candidate)
        if reference:
            return reference
    return ""


def service_release_item_legend(order: dict[str, Any], item: dict[str, Any]) -> str:
    """Mantiene la resolución propia de RSS y destaca su OS en el mismo renglón."""

    return " ".join(
        part
        for part in (service_release_legend(order), format_os_badge(service_release_order_reference(order, item)))
        if part
    )
