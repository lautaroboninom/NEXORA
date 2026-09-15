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
import textwrap
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
    if profile_type == "RDA" or profile_key == "rda":
        reason = reason or "Baja alquiler"
        internal = _text(equipment.get("internalNumber"))
        if internal:
            reason = f"{reason} ({internal})"
    return f"Motivo: {reason}" if reason else ""


def service_ingress_equipment_legend_lines(
    equipment: dict[str, Any] | None,
    *,
    os_text: Any = "",
    partida: Any = "",
    reason: str = "",
    include_internal: bool = True,
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
    if include_internal and internal and internal != identifier:
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


def rental_item_legend(order: dict[str, Any], item: dict[str, Any], partida: str) -> str:
    """Identifica el equipo alquilado sin modificar la descripción del artículo."""
    internal = ""
    for assigned in item.get("partidas") or []:
        if not isinstance(assigned, dict):
            continue
        serial = _text(assigned.get("partida") or assigned.get("code") or assigned.get("numeroPartida") or assigned.get("serial"))
        if serial == partida:
            internal = _text(assigned.get("equipmentInternalNumber") or assigned.get("internalNumber"))
            break
    item_serial = _text(item.get("equipmentSerial") or item.get("partida"))
    single_partida = len(item.get("partidas") or []) <= 1
    if not internal and (item_serial == partida or (not item_serial and single_partida)):
        internal = _text(item.get("equipmentInternalNumber") or item.get("internalNumber"))
    order_serial = _text(order.get("equipmentSerial"))
    if not internal and (order_serial == partida or (not order_serial and len(order.get("items") or []) == 1 and single_partida)):
        internal = _text(order.get("equipmentInternalNumber"))
    return "Se entrega en Alquiler" + (f" ({internal})" if internal else "")


def wrap_remito_legend_lines(text: Any, max_width: int = 50) -> list[str]:
    """Divide un texto de leyenda en renglones de hasta ``max_width`` caracteres.

    Aprovecha hasta el límite máximo por línea sin partir palabras por la mitad.
    Si una palabra individual supera por sí misma el ancho máximo, se quiebra
    de forma forzada para nunca desbordar el campo de Bejerman.
    """
    raw = _text(text)
    if not raw:
        return []
    lines: list[str] = []
    for paragraph in raw.splitlines():
        trimmed = paragraph.strip()
        if not trimmed:
            continue
        # Asegurar espacio tras coma o punto y coma no seguidos de espacio ni dígito
        normalized = re.sub(r"([,;])(?=[^\s\d])", r"\1 ", trimmed)
        wrapped = textwrap.wrap(
            normalized,
            width=max_width,
            break_long_words=True,
            break_on_hyphens=False,
        )
        for line in wrapped:
            clean_line = line.strip()
            if clean_line:
                lines.append(clean_line)
    return lines


def delivery_order_general_legend_lines(order: dict[str, Any]) -> list[str]:
    """Líneas de leyenda general al principio del remito.

    Para alta de alquiler (RTA), incluye 'Se entrega en alquiler' una sola vez
    arriba de todo. Si el usuario ingresó leyendas generales adicionales en la
    orden, se incorporan; si no especificó ninguna, se emite únicamente
    'Se entrega en alquiler'. Cada línea se limita a un máximo de 50 caracteres.
    """
    delivery_type = _text(order.get("deliveryType") or order.get("delivery_type")).lower()
    raw = _text(order.get("remitoGeneralLegend") or order.get("remito_general_legend"))
    custom_lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if delivery_type == "rental":
        if not custom_lines:
            base_lines = ["Se entrega en Alquiler"]
        elif not any("alquiler" in _normalized(line) for line in custom_lines):
            base_lines = ["Se entrega en Alquiler", *custom_lines]
        else:
            base_lines = custom_lines
    else:
        base_lines = custom_lines

    result: list[str] = []
    for line in base_lines:
        result.extend(wrap_remito_legend_lines(line, max_width=50))
    return result


def format_rental_article_accessories_legend(accessories: Any) -> str:
    """Formatea la leyenda de accesorios para un tipo/artículo de equipos en alquiler.

    Produce 'Los siguientes equipos se entregan con: <accesorios>'.
    Si el texto ya contiene el prefijo, se conserva sin duplicar.
    Si no hay accesorios especificados (vacío o sólo el prefijo), devuelve cadena vacía.
    """
    text = _text(accessories)
    if not text:
        return ""
    parts = [part.strip() for part in re.split(r"[\r\n]+", text) if part.strip()]
    cleaned = ", ".join(parts) if len(parts) > 1 else text.strip()
    if not cleaned:
        return ""
    prefix_match = re.match(
        r"^los\s+siguientes\s+equipos\s+se\s+entrega[ns]?\s+con\s*[:\-]?\s*",
        cleaned,
        flags=re.IGNORECASE,
    )
    if prefix_match:
        content = cleaned[prefix_match.end():].strip()
        if not content:
            return ""
        matched_prefix = cleaned[:prefix_match.end()]
        content = re.sub(r"([,;])(?=[^\s\d])", r"\1 ", content)
        return f"{matched_prefix}{content}"
    cleaned = re.sub(r"([,;])(?=[^\s\d])", r"\1 ", cleaned)
    return f"Los siguientes equipos se entregan con: {cleaned}"


def delivery_article_legend_lines(item: dict[str, Any], order: dict[str, Any] | None = None) -> list[str]:
    """Leyenda por artículo agregado para aclarar accesorios u observaciones de ese grupo.

    Si no hay accesorios ni observaciones cargadas para el artículo, no emite ninguna línea.
    Cada línea producida respeta un máximo de 50 caracteres aprovechando el renglón
    sin cortar palabras.
    """
    raw = (
        _text(item.get("remitoArticleLegend"))
        or _text(item.get("remito_article_legend"))
        or _text(item.get("accessories"))
        or _text(item.get("equipmentAccessories"))
    )
    if not raw and order:
        article_code = _text(item.get("articleCode") or item.get("article_code"))
        if article_code:
            for other in order.get("items") or []:
                if not isinstance(other, dict):
                    continue
                other_code = _text(other.get("articleCode") or other.get("article_code"))
                if other_code and other_code.lower() == article_code.lower():
                    other_raw = (
                        _text(other.get("remitoArticleLegend"))
                        or _text(other.get("remito_article_legend"))
                        or _text(other.get("accessories"))
                        or _text(other.get("equipmentAccessories"))
                    )
                    if other_raw:
                        raw = other_raw
                        break
    if not raw:
        return []

    delivery_type = ""
    if order:
        delivery_type = _text(order.get("deliveryType") or order.get("delivery_type")).lower()

    if delivery_type == "rental" or not delivery_type:
        legend = format_rental_article_accessories_legend(raw)
    else:
        if re.match(r"^los\s+siguientes\s+equipos\s+se\s+entrega[ns]?\s+con\s*[:\-]?\s*", raw, flags=re.IGNORECASE):
            legend = format_rental_article_accessories_legend(raw)
        else:
            legend = raw.strip()

    if not legend:
        return []

    return wrap_remito_legend_lines(legend, max_width=50)


def group_rental_order_items_by_article(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agrupa de forma contigua los renglones del mismo artículo para que en el remito
    aparezcan juntos bajo su encabezado de accesorios. Conserva el orden relativo de
    aparición del primer artículo visto."""
    order_of_keys: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            _text(item.get("articleCode"))
            or _text(item.get("article_code"))
            or _text(item.get("description"))
        ).strip().lower()
        if key not in groups:
            order_of_keys.append(key)
            groups[key] = []
        groups[key].append(item)
    grouped: list[dict[str, Any]] = []
    for key in order_of_keys:
        grouped.extend(groups[key])
    return grouped


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
