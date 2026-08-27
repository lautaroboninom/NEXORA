from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from .bejerman_sdk import bejerman_customer_code_for, is_occasional_customer_name
from .bejerman_particular_customers import disable_particular_customer_if_idle
from .notifications import active_users_for_notification, emit_notification, notification_email_recipients_for_users
from .notification_email_outbox import queue_notification_email
from .permissions import resolve_effective_permissions


logger = logging.getLogger(__name__)

DELIVERY_STATUSES = {
    "pendiente_stock",
    "pendiente_armado",
    "armado_pendiente_entrega",
    "entregado_pendiente_facturacion",
    "entregado_no_facturable",
    "facturado",
    "cancelado",
}
DELIVERY_TYPES = {"sale", "service_release", "rental", "demo"}
DELIVERY_COMPANY_KEYS = {"SEPID", "MGBIO"}
DELIVERY_PRICE_CURRENCIES = {"ARS", "USD"}
PRIORITIES = {"normal", "urgente"}
REMITO_LOCATIONS = {"recepcion", "oficina"}
CLOSED_STATUSES = {"entregado_no_facturable", "facturado", "cancelado"}
NON_CANCELABLE_STATUSES = CLOSED_STATUSES | {"entregado_pendiente_facturacion"}
EDITABLE_STATUSES = {"pendiente_stock", "pendiente_armado", "armado_pendiente_entrega"}
MANUAL_PLANNING_STATUSES = {"pendiente_stock", "pendiente_armado"}
PREPARABLE_STATUSES = {"pendiente_armado", "armado_pendiente_entrega"}
PARTIDA_QUANTITY_TOLERANCE = Decimal("0.0001")
RENTAL_STOCK_DEPOSIT = "STL"
RENTAL_UNAVAILABLE_STATES = {"entregado", "alquilado", "baja", "vendido_pendiente_entrega", "vendido_entregado"}
# Cada motivo va en dos géneros porque el sujeto cambia: "la OS ..." cuando el equipo
# pasó por taller, "el NS ..." cuando es un equipo dado de alta con su MG y sin OS.
RENTAL_UNAVAILABLE_STATE_REASONS = {
    "alquilado": ("ya figura alquilada", "ya figura alquilado"),
    "entregado": ("ya figura entregada", "ya figura entregado"),
    "baja": ("figura dada de baja", "figura dado de baja"),
    "vendido_pendiente_entrega": ("figura vendida y pendiente de entrega", "figura vendido y pendiente de entrega"),
    "vendido_entregado": ("figura vendida y entregada", "figura vendido y entregado"),
}
DELIVERY_TYPE_LABELS = {
    "sale": "Venta",
    "service_release": "Servicio técnico",
    "rental": "Alquiler",
    "demo": "Demo",
}
BILLABLE_REMITO_TYPES = {"RT"}
BILLING_COMPANY_BY_POINT_OF_SALE = {
    "00004": "SEPID",
    "00007": "MGBIO",
}
BILLING_POSTPONEMENT_NOTIFICATION_KEY = "billing_remito_postponement_due"
SELLER_CODE_MAX_LENGTH = 4
RENTAL_SELLER_CODE = "ADM"
SELLER_LABELS = {
    "ADM": "ADM Administración",
    "EZE": "EZE Ezequiel Merino",
    "MAX": "MAX Maximiliano Pereletegui",
    "MER": "MER Mercado Libre",
    "TOM": "TOM Tomas Perez Avila",
}


class DeliveryOrderError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RemitoProfile:
    comprobante_tipo: str
    point_of_sale: str
    operation_code: str
    deposit_code: str


def _rows(cur) -> list[dict[str, Any]]:
    cols = [col[0] for col in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _one(cur) -> dict[str, Any] | None:
    cols = [col[0] for col in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    text = _clean_text(value)
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _price_currency(value: Any, default: str = "ARS") -> str:
    text = _clean_text(value).upper()
    if not text:
        text = default
    text = text.replace(" ", "")
    if text in {"$", "PESO", "PESOS"}:
        text = "ARS"
    if text in {"U$S", "U$D", "US$", "DOLAR", "DOLARES"}:
        text = "USD"
    if text not in DELIVERY_PRICE_CURRENCIES:
        raise DeliveryOrderError("INVALID_PRICE_CURRENCY", "La moneda debe ser ARS o USD")
    return text


def _item_price_currency_input(item: dict[str, Any]) -> tuple[bool, Any]:
    if "priceCurrency" in item:
        return True, item.get("priceCurrency")
    if "price_currency" in item:
        return True, item.get("price_currency")
    return False, None


def _decimal(value: Any, default: str = "1") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise DeliveryOrderError("INVALID_DECIMAL", "Cantidad o precio inválido") from exc


def _positive_decimal(value: Any, default: str = "1") -> Decimal:
    number = _decimal(value, default)
    if number <= 0:
        raise DeliveryOrderError("INVALID_DECIMAL", "La cantidad debe ser mayor a cero")
    return number


def _discount_percent(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise DeliveryOrderError("INVALID_DISCOUNT_PERCENT", "El descuento debe ser numérico") from exc
    if number < 0 or number > 100:
        raise DeliveryOrderError("INVALID_DISCOUNT_PERCENT", "El descuento debe estar entre 0 y 100")
    return number.quantize(Decimal("0.01"))


def _item_discount_input(item: dict[str, Any]) -> tuple[bool, Any]:
    if "discountPercent" in item:
        return True, item.get("discountPercent")
    if "discount_percent" in item:
        return True, item.get("discount_percent")
    return False, None


def _normalize_item_partidas(partidas: Any, item_quantity: Decimal) -> list[dict[str, Any]]:
    if partidas in (None, ""):
        return []
    if not isinstance(partidas, list):
        raise DeliveryOrderError("INVALID_PARTIDAS", "Las partidas deben enviarse como lista")

    rows: list[dict[str, Any]] = []
    seen_partidas: set[str] = set()
    for idx, partida in enumerate(partidas):
        if not isinstance(partida, dict):
            raise DeliveryOrderError("INVALID_PARTIDAS", "Cada partida debe ser un objeto")
        partida_text = _optional_text(partida.get("partida"))
        assigned_quantity = _decimal(partida.get("assignedQuantity") or partida.get("quantity"), "0")
        if not partida_text or assigned_quantity <= 0:
            raise DeliveryOrderError("INVALID_PARTIDAS", "Cada partida necesita número y cantidad mayor a cero")
        partida_key = partida_text.casefold()
        if partida_key in seen_partidas:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_PARTIDAS_DUPLICATED",
                f"La partida {partida_text} está duplicada en el renglón",
            )
        seen_partidas.add(partida_key)
        rows.append(
            {
                "id": _optional_text(partida.get("id")),
                "partida": partida_text,
                "ingresoId": _optional_int(partida.get("ingresoId") or partida.get("ingreso_id")),
                "deviceId": _optional_int(partida.get("deviceId") or partida.get("device_id")),
                "assignedQuantity": assigned_quantity,
                "partidaExpirationDate": partida.get("partidaExpirationDate") or None,
                "stockDepositCode": _optional_text(partida.get("stockDepositCode")),
                "stockAvailableQuantity": (
                    _decimal(partida.get("stockAvailableQuantity"), "0")
                    if partida.get("stockAvailableQuantity") not in (None, "")
                    else None
                ),
                "stockCheckedAt": partida.get("stockCheckedAt") or None,
                "sortOrder": partida.get("sortOrder") if partida.get("sortOrder") is not None else idx,
            }
        )

    if rows:
        assigned_total = sum((row["assignedQuantity"] for row in rows), Decimal("0"))
        if abs(assigned_total - item_quantity) > PARTIDA_QUANTITY_TOLERANCE:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_PARTIDAS_QUANTITY_MISMATCH",
                "La suma de las cantidades de las partidas debe coincidir con la cantidad del artículo",
            )
    return rows


def _partida_rows_for_prepared_validation(item: dict[str, Any]) -> list[dict[str, Any]]:
    partidas = item.get("partidas")
    if isinstance(partidas, list) and partidas:
        return partidas
    partida = _optional_text(item.get("partida"))
    if partida:
        return [
            {
                "partida": partida,
                "ingresoId": item.get("ingresoId") or item.get("ingreso_id"),
                "deviceId": item.get("deviceId") or item.get("device_id"),
                "assignedQuantity": item.get("quantity"),
                "partidaExpirationDate": item.get("partidaExpirationDate") or item.get("partida_expiration_date"),
                "stockDepositCode": item.get("stockDepositCode") or item.get("stock_deposit_code"),
                "stockAvailableQuantity": item.get("stockAvailableQuantity") or item.get("stock_available_quantity"),
                "stockCheckedAt": item.get("stockCheckedAt") or item.get("stock_checked_at"),
            }
        ]
    return []


def _nullable_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "s", "si", "sí", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "n", "no", "false", "f", "off"}:
        return False
    return None


def _user_role(user_id: int | None) -> str:
    if not user_id:
        return ""
    with connection.cursor() as cur:
        cur.execute("SELECT rol FROM users WHERE id = %s AND activo = TRUE", [user_id])
        row = cur.fetchone()
    return _clean_text(row[0] if row else "").lower()


def _require_commercial_correction_permission(actor_user_id: int | None) -> None:
    role = _user_role(actor_user_id)
    permissions = resolve_effective_permissions(user_id=actor_user_id, role=role)
    if not permissions.get("action.delivery_order.correct_commercial_terms", False):
        raise DeliveryOrderError(
            "DELIVERY_ORDER_COMMERCIAL_CORRECTION_FORBIDDEN",
            "No tenés permiso para corregir el precio o el plazo de una orden de entrega.",
            status_code=403,
        )


def _item_article_requires_partida(item: dict[str, Any]) -> bool | None:
    if "articleRequiresPartida" in item:
        return _nullable_bool(item.get("articleRequiresPartida"))
    return _nullable_bool(item.get("article_requires_partida"))


def _item_can_omit_catalog_partida(order: dict[str, Any], item: dict[str, Any]) -> bool:
    return (
        normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type")) in {"sale", "demo"}
        and _item_article_requires_partida(item) is False
    )


def _partida_policy_unknown_error(item: dict[str, Any], index: int, action: str) -> DeliveryOrderError:
    article_code = _optional_text(item.get("articleCode") or item.get("article_code")) or f"renglón {index}"
    return DeliveryOrderError(
        "DELIVERY_ORDER_ARTICLE_PARTIDA_POLICY_UNKNOWN",
        f"No se pudo verificar si el artículo {article_code} requiere partida. Reintente la verificación Bejerman o cargue partidas antes de {action}.",
        status_code=409,
    )


def _validate_order_article_stock_ready(
    order: dict[str, Any],
    item: dict[str, Any],
    index: int,
    quantity: Decimal,
    actor_user_id: int | None,
    cache: dict[str, Any],
) -> None:
    article_code = _optional_text(item.get("articleCode") or item.get("article_code"))
    if not article_code or quantity <= 0:
        return
    delivery_type = normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type"))
    company_key = normalize_company_key(order.get("companyKey") or order.get("company_key"))
    default_deposit = remito_profile_for_type(delivery_type).deposit_code
    deposit_code = (_optional_text(item.get("stockDepositCode")) or default_deposit).upper()
    try:
        available = _delivery_article_available_quantity(article_code, deposit_code, company_key, actor_user_id, cache)
    except Exception as exc:
        logger.warning("delivery_order_article_stock_validation_failed", exc_info=True)
        raise DeliveryOrderError(
            "DELIVERY_ORDER_ARTICLE_STOCK_UNAVAILABLE",
            "No se pudo verificar stock Bejerman para preparar la orden.",
            status_code=502,
        ) from exc
    if quantity - available > PARTIDA_QUANTITY_TOLERANCE:
        raise DeliveryOrderError(
            "DELIVERY_ORDER_ARTICLE_STOCK_INSUFFICIENT",
            (
                f"El artículo {article_code} tiene stock disponible {available} en "
                f"{deposit_code}, pero el renglón {index} pide {quantity}."
            ),
            status_code=409,
        )


def _validate_order_partidas_ready(order: dict[str, Any], actor_user_id: int | None = None) -> None:
    delivery_type = normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type"))
    stock_cache: dict[str, Any] = {}
    for index, item in enumerate(order.get("items") or [], start=1):
        quantity = _decimal(item.get("quantity"), "0")
        rows = _partida_rows_for_prepared_validation(item)
        if not rows:
            if delivery_type == "rental":
                raise DeliveryOrderError(
                    "DELIVERY_ORDER_PARTIDAS_REQUIRED",
                    f"Faltan NS del renglón {index}.",
                    status_code=409,
                )
            if _item_can_omit_catalog_partida(order, item):
                # El artículo no lleva partida, así que su stock nunca pasa por
                # `_validate_delivery_partidas_stock` (valida por partida). Sin este chequeo
                # el renglón llega sin stock hasta la emisión y el que avisa es Bejerman,
                # con el remito rechazado entero y el cliente esperando: el 12/08/2026 el
                # artículo 1208009 tenía 18 en VAL para un renglón de 19 y el RT recién
                # falló al cargar el remito.
                _validate_order_article_stock_ready(order, item, index, quantity, actor_user_id, stock_cache)
                continue
            if (
                delivery_type in {"sale", "demo"}
                and _optional_text(item.get("articleCode") or item.get("article_code"))
                and _item_article_requires_partida(item) is None
            ):
                raise _partida_policy_unknown_error(item, index, "preparar")
            raise DeliveryOrderError(
                "DELIVERY_ORDER_PARTIDAS_REQUIRED",
                f"Complete las partidas del renglón {index} antes de preparar.",
                status_code=409,
            )

        assigned_total = Decimal("0")
        seen_partidas: set[str] = set()
        for row in rows:
            partida_text = _optional_text(row.get("partida"))
            assigned_quantity = _decimal(row.get("assignedQuantity") or row.get("quantity"), "0")
            if not partida_text or assigned_quantity <= 0:
                raise DeliveryOrderError(
                    "DELIVERY_ORDER_PARTIDAS_REQUIRED",
                    f"Cada partida del renglón {index} necesita número y cantidad.",
                    status_code=409,
                )
            partida_key = partida_text.casefold()
            if partida_key in seen_partidas:
                raise DeliveryOrderError(
                    "DELIVERY_ORDER_PARTIDAS_DUPLICATED",
                    f"La partida {partida_text} está duplicada en el renglón {index}.",
                    status_code=409,
                )
            seen_partidas.add(partida_key)
            assigned_total += assigned_quantity

        if abs(assigned_total - quantity) > PARTIDA_QUANTITY_TOLERANCE:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_PARTIDAS_QUANTITY_MISMATCH",
                f"La suma de las partidas del renglón {index} debe coincidir con la cantidad del artículo.",
                status_code=409,
            )


def _normalize_order_items(
    items: Any,
    equipment_serial: str | None,
    existing_items_by_id: dict[str, dict[str, Any]] | None = None,
    default_price_currency: str = "ARS",
) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise DeliveryOrderError("ITEMS_REQUIRED", "La orden necesita al menos un ítem")

    existing_items_by_id = existing_items_by_id or {}
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise DeliveryOrderError("INVALID_ITEM", "Cada renglón debe ser un objeto")
        item_id = _optional_text(item.get("id"))
        current_item = existing_items_by_id.get(item_id or "")
        has_currency, currency_input = _item_price_currency_input(item)
        price_currency = (
            _price_currency(currency_input, default_price_currency)
            if has_currency
            else _price_currency(current_item.get("priceCurrency"), default_price_currency) if current_item else default_price_currency
        )
        has_discount, discount_input = _item_discount_input(item)
        discount_percent = (
            _discount_percent(discount_input)
            if has_discount
            else _discount_percent(current_item.get("discountPercent") if current_item else None)
        )
        quantity = _positive_decimal(item.get("quantity"))
        partidas = _normalize_item_partidas(item.get("partidas") or [], quantity)
        description = _clean_text(item.get("description") or item.get("sourceText") or item.get("articleName") or item.get("articleCode"))
        if not description:
            raise DeliveryOrderError("ITEM_DESCRIPTION_REQUIRED", "Cada renglón necesita un detalle")
        article_requires_input_present = "articleRequiresPartida" in item or "article_requires_partida" in item
        article_requires_partida = _nullable_bool(
            item.get("articleRequiresPartida") if "articleRequiresPartida" in item else item.get("article_requires_partida")
        )
        if not article_requires_input_present and current_item:
            current_code = _optional_text(current_item.get("articleCode") or current_item.get("article_code"))
            next_code = _optional_text(item.get("articleCode") or item.get("article_code"))
            if _article_code_key(current_code) == _article_code_key(next_code):
                article_requires_partida = _item_article_requires_partida(current_item)
        effective_item = dict(item)
        effective_item["partidas"] = partidas
        if partidas:
            effective_item["partida"] = None
        normalized.append(
            {
                "id": item_id,
                "ingresoId": _optional_int(item.get("ingresoId") or item.get("ingreso_id")),
                "deviceId": _optional_int(item.get("deviceId") or item.get("device_id")),
                "articleCode": _optional_text(item.get("articleCode")),
                "articleName": _optional_text(item.get("articleName")),
                "articleRequiresPartida": article_requires_partida,
                "description": description,
                "quantity": quantity,
                "unitPrice": _decimal(item.get("unitPrice"), "0") if item.get("unitPrice") not in (None, "") else None,
                "priceCurrency": price_currency,
                "discountPercent": discount_percent,
                "sourceText": _optional_text(item.get("sourceText")),
                "partida": _effective_equipment_partida(effective_item, equipment_serial, quantity),
                "partidaExpirationDate": item.get("partidaExpirationDate") or None,
                "stockDepositCode": _optional_text(item.get("stockDepositCode")),
                "stockAvailableQuantity": (
                    _decimal(item.get("stockAvailableQuantity"), "0")
                    if item.get("stockAvailableQuantity") not in (None, "")
                    else None
                ),
                "stockCheckedAt": item.get("stockCheckedAt") or None,
                "sortOrder": item.get("sortOrder") if item.get("sortOrder") is not None else idx,
                "partidas": partidas,
            }
        )
    return normalized


def _effective_equipment_partida(item: dict[str, Any], equipment_serial: str | None, quantity: Decimal) -> str | None:
    explicit = _optional_text(item.get("partida"))
    if explicit:
        return explicit
    if item.get("partidas"):
        return None
    if quantity > Decimal("1"):
        return None
    return equipment_serial


def _stock_quantity(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _stock_request_key(article_code: str, deposit_code: str, partida: str) -> str:
    return "|".join([article_code.casefold(), deposit_code.upper(), partida.casefold()])


def _delivery_partida_stock_lots(
    article_code: str,
    deposit_code: str,
    company_key: str,
    actor_user_id: int | None,
    cache: dict[str, Any],
) -> list[dict[str, Any]]:
    from .bejerman_sdk import BejermanSDKClient, build_article_stock_lots
    from . import bejerman_catalog_cache

    company_cache = cache.setdefault(company_key, {})
    client = company_cache.get("client")
    if client is None:
        client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
        company_cache["client"] = client
    return build_article_stock_lots(
        bejerman_catalog_cache.stock_rows(client, company_key, deposit_code),
        bejerman_catalog_cache.partida_rows(client, company_key),
        article_code,
        deposit_code,
    )


def _delivery_article_available_quantity(
    article_code: str,
    deposit_code: str,
    company_key: str,
    actor_user_id: int | None,
    cache: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> Decimal:
    """Stock real disponible de un artículo en un depósito, sumado en crudo por
    articleCode (sin agrupar por partida) — a diferencia de `_delivery_partida_stock_lots`,
    sirve para artículos que no llevan partida (article_requires_partida=false), que
    build_article_stock_lots descarta por no tener partida informada.

    `force_refresh` saltea el cache de stock (30 s, y uno por worker de gunicorn):
    para *mostrar* números alcanza con el cache, pero para *bloquear* una emisión no,
    porque el flujo normal del depósito es corregir el stock a mano en Bejerman y
    volver enseguida a NEXORA. Se baja el depósito una sola vez por tanda: los demás
    artículos del mismo depósito ya leen el volcado recién traído."""
    from .bejerman_sdk import BejermanSDKClient
    from . import bejerman_catalog_cache

    company_cache = cache.setdefault(company_key, {})
    client = company_cache.get("client")
    if client is None:
        client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
        company_cache["client"] = client
    refreshed_deposits = company_cache.setdefault("refreshed_deposits", set())
    refresh_now = force_refresh and deposit_code not in refreshed_deposits
    if refresh_now:
        refreshed_deposits.add(deposit_code)
    target = article_code.casefold()
    total = Decimal("0")
    for row in bejerman_catalog_cache.stock_rows(client, company_key, deposit_code, force_refresh=refresh_now):
        if str(row.get("articleCode") or "").casefold() != target:
            continue
        total += _decimal(row.get("availableQuantity"), "0")
    return total


def _validate_delivery_partidas_stock(
    items: list[dict[str, Any]],
    company_key: str,
    delivery_type: str,
    actor_user_id: int | None,
) -> None:
    if delivery_type not in {"sale", "demo"}:
        return

    default_deposit = remito_profile_for_type(delivery_type).deposit_code
    requests: list[dict[str, Any]] = []
    for item_index, item in enumerate(items, start=1):
        item_rows = item.get("partidas") if isinstance(item.get("partidas"), list) else []
        explicit_partida = _optional_text(item.get("partida"))
        if explicit_partida and not item_rows:
            item_rows = [
                {
                    "partida": explicit_partida,
                    "assignedQuantity": item.get("quantity"),
                    "partidaExpirationDate": item.get("partidaExpirationDate"),
                    "stockDepositCode": item.get("stockDepositCode"),
                    "stockAvailableQuantity": item.get("stockAvailableQuantity"),
                    "stockCheckedAt": item.get("stockCheckedAt"),
                    "_target": item,
                }
            ]

        for row in item_rows:
            partida = _optional_text(row.get("partida"))
            if not partida:
                continue
            article_code = _optional_text(item.get("articleCode"))
            if not article_code:
                raise DeliveryOrderError(
                    "DELIVERY_ORDER_PARTIDA_ARTICLE_REQUIRED",
                    f"Seleccione un artículo Bejerman antes de cargar partidas en el renglón {item_index}.",
                )
            deposit_code = (
                _optional_text(row.get("stockDepositCode"))
                or _optional_text(item.get("stockDepositCode"))
                or default_deposit
            ).upper()
            requests.append(
                {
                    "itemIndex": item_index,
                    "articleCode": article_code,
                    "depositCode": deposit_code,
                    "partida": partida,
                    "quantity": _decimal(row.get("assignedQuantity") or row.get("quantity"), "0"),
                    "target": row.get("_target") or row,
                }
            )

    if not requests:
        return

    cache: dict[str, Any] = {}
    lots_by_key: dict[str, dict[str, Any]] = {}
    requested_by_key: dict[str, Decimal] = {}
    try:
        for request in requests:
            lookup_key = f"{company_key}|{request['depositCode']}|{request['articleCode']}"
            if lookup_key not in lots_by_key:
                lots_by_key[lookup_key] = {
                    _optional_text(lot.get("partida")).casefold(): lot
                    for lot in _delivery_partida_stock_lots(
                        request["articleCode"],
                        request["depositCode"],
                        company_key,
                        actor_user_id,
                        cache,
                    )
                    if _optional_text(lot.get("partida"))
                }
            lot = lots_by_key[lookup_key].get(request["partida"].casefold())
            if not lot or _stock_quantity(lot.get("availableQuantity")) <= 0:
                raise DeliveryOrderError(
                    "DELIVERY_ORDER_PARTIDA_NOT_FOUND",
                    (
                        f"La partida {request['partida']} no figura con stock positivo para el artículo "
                        f"{request['articleCode']} en el depósito {request['depositCode']}."
                    ),
                    status_code=409,
                )
            request["lot"] = lot
            stock_key = _stock_request_key(request["articleCode"], request["depositCode"], request["partida"])
            requested_by_key[stock_key] = requested_by_key.get(stock_key, Decimal("0")) + request["quantity"]
    except DeliveryOrderError:
        raise
    except Exception as exc:
        logger.warning("delivery_order_partida_stock_validation_failed", exc_info=True)
        raise DeliveryOrderError(
            "DELIVERY_ORDER_PARTIDA_STOCK_UNAVAILABLE",
            "No se pudo verificar stock Bejerman para validar las partidas.",
            status_code=502,
        ) from exc

    checked_at = timezone.now().isoformat()
    available_by_key = {
        _stock_request_key(request["articleCode"], request["depositCode"], request["partida"]): _stock_quantity(
            request["lot"].get("availableQuantity")
        )
        for request in requests
    }
    for stock_key, requested_quantity in requested_by_key.items():
        available_quantity = available_by_key.get(stock_key, Decimal("0"))
        if requested_quantity - available_quantity > PARTIDA_QUANTITY_TOLERANCE:
            sample = next(request for request in requests if _stock_request_key(request["articleCode"], request["depositCode"], request["partida"]) == stock_key)
            raise DeliveryOrderError(
                "DELIVERY_ORDER_PARTIDA_STOCK_INSUFFICIENT",
                (
                    f"La partida {sample['partida']} tiene stock disponible {available_quantity} en "
                    f"{sample['depositCode']}, pero la orden pide {requested_quantity}."
                ),
                status_code=409,
            )

    for request in requests:
        lot = request["lot"]
        target = request["target"]
        target["partida"] = _optional_text(lot.get("partida")) or request["partida"]
        target["partidaExpirationDate"] = lot.get("expirationDate") or target.get("partidaExpirationDate")
        target["stockDepositCode"] = _optional_text(lot.get("depositCode")) or request["depositCode"]
        target["stockAvailableQuantity"] = _stock_quantity(lot.get("availableQuantity"))
        target["stockCheckedAt"] = checked_at


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _frontend_link(path: str) -> str:
    base = (
        str(getattr(settings, "PUBLIC_WEB_URL", "") or "").strip()
        or str(getattr(settings, "FRONTEND_ORIGIN", "") or "").strip()
    )
    if base:
        return f"{base.rstrip('/')}{path}"
    return path


def _email_append_footer_text(body: str) -> str:
    footer = str(getattr(settings, "EMAIL_LEGAL_FOOTER", "") or "").strip()
    base = str(body or "").rstrip()
    if not footer:
        return base
    return f"{base}\n\n{footer}"


def _currency_symbol(currency: Any) -> str:
    if str(currency or "").strip().upper() == "MIXED":
        return "$ / U$S"
    return "U$S" if _price_currency(currency) == "USD" else "$"


def _money_label(value: Decimal | None, currency: Any = "ARS") -> str:
    if value is None:
        return "-"
    amount = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{_currency_symbol(currency)} {amount}"


def _totals_money_label(totals: dict[str, Any], key: str) -> str:
    if not totals.get("mixedCurrency"):
        return _money_label(totals.get(key), totals.get("currency"))
    labels = []
    for currency in totals.get("currencies") or []:
        bucket = (totals.get("totalsByCurrency") or {}).get(currency) or {}
        labels.append(_money_label(bucket.get(key), currency))
    return " / ".join(labels) if labels else "-"


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _decode_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _os_number_label(value: Any) -> str:
    text = _optional_text(value)
    if not text:
        return ""
    try:
        return f"{int(text):05d}"
    except (TypeError, ValueError):
        return text


def service_order_reference(ingreso_id: Any) -> str:
    label = _os_number_label(ingreso_id)
    return f"OS-{label}" if label else ""


def delivery_source_reference(source_reference: Any, delivery_type: Any, ingreso_id: Any = None) -> str:
    reference = _optional_text(source_reference) or ""
    if delivery_type != "service_release" and not (reference and re.fullmatch(r"OS[\s-]*\d+", reference, re.IGNORECASE)):
        return reference
    if ingreso_id:
        return service_order_reference(ingreso_id)
    match = re.fullmatch(r"OS[\s-]*(\d+)", reference, re.IGNORECASE)
    return service_order_reference(match.group(1)) if match else reference


def _append_unique_int(values: list[int], value: Any) -> None:
    parsed = _optional_int(value)
    if parsed and parsed not in values:
        values.append(parsed)


def service_release_ingreso_ids_from_order(order: dict[str, Any]) -> list[int]:
    delivery_type = normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type") or "")
    if delivery_type != "service_release":
        return []

    ids: list[int] = []
    _append_unique_int(ids, order.get("ingresoId") or order.get("ingreso_id"))
    if _clean_text(order.get("sourceSystem") or order.get("source_system")).lower() == "nexora":
        _append_unique_int(ids, order.get("sourceExternalId") or order.get("source_external_id"))

    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        _append_unique_int(ids, item.get("ingresoId") or item.get("ingreso_id"))
        for partida in item.get("partidas") or []:
            if isinstance(partida, dict):
                _append_unique_int(ids, partida.get("ingresoId") or partida.get("ingreso_id"))
    return ids


def service_release_references_from_order(order: dict[str, Any]) -> list[str]:
    return [service_order_reference(ingreso_id) for ingreso_id in service_release_ingreso_ids_from_order(order)]


def service_release_max_items_per_order() -> int:
    value = getattr(settings, "BEJERMAN_RSS_MAX_PRINT_LINES", 22)
    try:
        max_lines = int(value)
    except (TypeError, ValueError):
        max_lines = 22
    max_lines = max(4, min(22, max_lines))
    try:
        item_lines = int(getattr(settings, "BEJERMAN_RSS_PRINT_LINES_PER_ITEM", 3))
    except (TypeError, ValueError):
        item_lines = 3
    item_lines = max(1, item_lines)
    return max(1, max_lines // item_lines)


def delivery_order_number(row: dict[str, Any]) -> str:
    if row.get("delivery_type") == "service_release":
        service_references = service_release_references_from_order(
            {
                **row,
                "deliveryType": row.get("delivery_type"),
                "ingresoId": row.get("ingreso_id"),
                "sourceSystem": row.get("source_system"),
                "sourceExternalId": row.get("source_external_id"),
            }
        )
        if len(service_references) > 1:
            return f"{service_references[0]} + {len(service_references) - 1} OS"
        if len(service_references) == 1:
            return service_references[0]
        legacy_reference = (
            service_order_reference(row.get("ingreso_id"))
            or delivery_source_reference(
                row.get("source_reference"),
                row.get("delivery_type"),
                row.get("ingreso_id"),
            )
            or row.get("order_number")
            or ""
        )
        if legacy_reference and not str(legacy_reference).upper().startswith("OS-"):
            # No hay ingreso_id vinculado (típicamente filas migradas del Portal Sepid):
            # el valor es una referencia externa, no un número de OS real. Se marca
            # como tal para no confundirla con "OS-00000".
            return f"REF-{legacy_reference}"
        return legacy_reference
    return row.get("order_number") or ""


def order_number_for_now(delivery_type: str, ingreso_id: Any = None, source_reference: Any = None) -> str:
    if delivery_type == "service_release":
        reference = service_order_reference(ingreso_id) or delivery_source_reference(source_reference, delivery_type, ingreso_id)
        if reference and reference.upper().startswith("OS-"):
            return reference
        raise DeliveryOrderError("SERVICE_ORDER_REQUIRED", "La liberación necesita una OS para numerar la orden de entrega")
    if connection.vendor != "postgresql":
        return f"OE-{uuid4().hex[:6].upper()}"
    with connection.cursor() as cur:
        cur.execute("LOCK TABLE delivery_orders IN EXCLUSIVE MODE")
        cur.execute(
            """
            SELECT COALESCE(MAX((substring(order_number FROM '^OE-0*([0-9]+)$'))::bigint), 0) + 1
              FROM delivery_orders
             WHERE order_number ~ '^OE-[0-9]+$'
            """
        )
        next_number = int(cur.fetchone()[0] or 1)
    return f"OE-{next_number:05d}"



def normalize_delivery_type(value: Any) -> str:
    delivery_type = _clean_text(value or "sale").lower()
    if delivery_type not in DELIVERY_TYPES:
        raise DeliveryOrderError("INVALID_DELIVERY_TYPE", "Tipo de orden inválido")
    return delivery_type


def normalize_status(value: Any) -> str:
    status = _clean_text(value or "pendiente_armado").lower()
    if status not in DELIVERY_STATUSES:
        raise DeliveryOrderError("INVALID_STATUS", "Estado de orden inválido")
    return status


def normalize_priority(value: Any) -> str:
    priority = _clean_text(value or "normal").lower()
    if priority not in PRIORITIES:
        raise DeliveryOrderError("INVALID_PRIORITY", "Prioridad inválida")
    return priority


def normalize_company_key(value: Any, *, default: str = "SEPID") -> str:
    company_key = _clean_text(value or default).upper()
    if company_key not in DELIVERY_COMPANY_KEYS:
        raise DeliveryOrderError("INVALID_COMPANY_KEY", "Empresa Bejerman inválida")
    return company_key


def normalize_seller_code(value: Any) -> str | None:
    code = _optional_text(value)
    if not code:
        return None
    code = code.upper()
    if len(code) > SELLER_CODE_MAX_LENGTH:
        raise DeliveryOrderError(
            "INVALID_SELLER_CODE_LENGTH",
            "El código de vendedor no puede superar 4 caracteres",
        )
    return code


def seller_label_for_code(code: Any) -> str:
    normalized = normalize_seller_code(code)
    if not normalized:
        return ""
    return SELLER_LABELS.get(normalized, normalized)


def seller_fields_for_delivery_order(
    delivery_type: str,
    payload_seller_name: Any,
    payload_seller_code: Any,
    actor_user_id: int | None,
    *,
    use_actor_default: bool = True,
) -> tuple[str, str | None]:
    if delivery_type == "rental":
        return seller_label_for_code(RENTAL_SELLER_CODE), RENTAL_SELLER_CODE

    seller_code = normalize_seller_code(payload_seller_code)
    if not seller_code and use_actor_default and delivery_type in {"sale", "demo"}:
        seller_code = normalize_seller_code(get_user_seller_code(actor_user_id))
    seller_name = _clean_text(payload_seller_name) or seller_label_for_code(seller_code)
    return seller_name, seller_code


def actor_is_delivery_discount_admin(actor_user_id: int | None) -> bool:
    if actor_user_id is None:
        return False
    with connection.cursor() as cur:
        cur.execute("SELECT LOWER(TRIM(COALESCE(rol, ''))) FROM users WHERE id = %s", [actor_user_id])
        row = cur.fetchone()
    return bool(row and row[0] in {"jefe", "admin", "supervisor", "ventas"})


def _assert_item_discounts_allowed(
    items: list[dict[str, Any]],
    actor_user_id: int | None,
    existing_items_by_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    if actor_is_delivery_discount_admin(actor_user_id):
        return
    existing_items_by_id = existing_items_by_id or {}
    for item in items:
        previous = _discount_percent(
            existing_items_by_id.get(item.get("id") or "", {}).get("discountPercent")
        )
        if item.get("discountPercent") != previous:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_DISCOUNT_ADMIN_REQUIRED",
                "Solo un usuario de Administración, Supervisor o Ventas puede cargar descuentos en órdenes de entrega",
                status_code=403,
            )


def remito_type_from_number(value: Any) -> str:
    text = _optional_text(value).upper()
    if not text:
        return ""
    match = re.match(r"^([A-Z]+)", text)
    return match.group(1) if match else ""


def remito_type_requires_billing(value: Any) -> bool:
    return (_optional_text(value) or "").upper() in BILLABLE_REMITO_TYPES


def remito_number_requires_billing(value: Any) -> bool:
    return remito_type_requires_billing(remito_type_from_number(value))


def remito_status(remito_number: Any, *, billing_required: bool | None = None) -> str:
    if not _optional_text(remito_number):
        return "armado_pendiente_entrega"
    if billing_required is None:
        billing_required = remito_number_requires_billing(remito_number)
    return "entregado_pendiente_facturacion" if billing_required else "entregado_no_facturable"


def _service_release_ingreso_id(order: dict[str, Any]) -> int | None:
    delivery_type = normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type") or "")
    if delivery_type != "service_release":
        return None
    ingreso_id = _optional_int(order.get("ingresoId") or order.get("ingreso_id"))
    if ingreso_id:
        return ingreso_id
    if _clean_text(order.get("sourceSystem") or order.get("source_system")).lower() == "nexora":
        return _optional_int(order.get("sourceExternalId") or order.get("source_external_id"))
    return None


def _service_release_ingreso_ids_for_order_ids(order_ids: list[str]) -> list[int]:
    ids = [str(order_id).strip() for order_id in order_ids if str(order_id or "").strip()]
    if not ids:
        return []
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ingreso_id
              FROM (
                    SELECT ingreso_id
                      FROM delivery_orders
                     WHERE id = ANY(%s)
                       AND delivery_type = 'service_release'
                       AND ingreso_id IS NOT NULL
                    UNION
                    SELECT doi.ingreso_id
                      FROM delivery_order_items doi
                      JOIN delivery_orders o ON o.id = doi.order_id
                     WHERE o.id = ANY(%s)
                       AND o.delivery_type = 'service_release'
                       AND doi.ingreso_id IS NOT NULL
                    UNION
                    SELECT doip.ingreso_id
                      FROM delivery_order_item_partidas doip
                      JOIN delivery_order_items doi ON doi.id = doip.order_item_id
                      JOIN delivery_orders o ON o.id = doi.order_id
                     WHERE o.id = ANY(%s)
                       AND o.delivery_type = 'service_release'
                       AND doip.ingreso_id IS NOT NULL
                   ) linked_ingresos
             ORDER BY ingreso_id
            """,
            [ids, ids, ids],
        )
        return [int(row[0]) for row in cur.fetchall() if row[0] is not None]


def _sync_service_release_ingresos_invoice(ingreso_ids: list[int], invoice: str) -> list[int]:
    ids: list[int] = []
    for ingreso_id in ingreso_ids:
        _append_unique_int(ids, ingreso_id)
    if not ids:
        return []
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, factura_numero
              FROM ingresos
             WHERE id = ANY(%s)
             ORDER BY id
             FOR UPDATE
            """,
            [ids],
        )
        rows = _rows(cur)
        found_ids = [int(row["id"]) for row in rows]
        for row in rows:
            current_invoice = _optional_text(row.get("factura_numero"))
            if current_invoice and current_invoice != invoice:
                raise DeliveryOrderError(
                    "SERVICE_ORDER_INVOICE_CONFLICT",
                    "Una hoja de servicio del grupo ya tiene otra factura registrada.",
                    status_code=409,
                )
        if found_ids:
            cur.execute(
                """
                UPDATE ingresos
                   SET factura_numero = %s
                 WHERE id = ANY(%s)
                   AND NULLIF(TRIM(COALESCE(factura_numero, '')), '') IS NULL
                """,
                [invoice, found_ids],
            )
    return found_ids


def _sync_service_release_ingreso_invoice(order: dict[str, Any], invoice: str) -> list[int]:
    ingreso_ids = service_release_ingreso_ids_from_order(order)
    if not ingreso_ids:
        ingreso_id = _service_release_ingreso_id(order)
        if ingreso_id:
            ingreso_ids = [ingreso_id]
    return _sync_service_release_ingresos_invoice(ingreso_ids, invoice)


_ENTREGA_SOURCE_ESTADOS = ("liberado", "vendido_pendiente_entrega")


def _sync_service_release_ingresos_delivery(
    ingreso_ids: list[int], remito: str, actor_user_id: int | None
) -> list[int]:
    """Refleja en `ingresos` la entrega ya registrada en una orden de entrega.

    Espejo de `_sync_service_release_ingresos_invoice`, pero para el momento
    de entregar en vez de facturar. Solo toca ingresos que todavia estan en
    un estado "de partida" (liberado / vendido pendiente de entrega) y nunca
    pisa un remito_salida que ya haya sido cargado por otra via.
    """
    ids: list[int] = []
    for ingreso_id in ingreso_ids:
        _append_unique_int(ids, ingreso_id)
    remito = _optional_text(remito)
    if not ids or not remito:
        return []

    from .views.ingresos_views import resolve_ingreso_entrega_estado

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, estado
              FROM ingresos
             WHERE id = ANY(%s)
             ORDER BY id
             FOR UPDATE
            """,
            [ids],
        )
        rows = _rows(cur)
        eligible_ids = [
            int(row["id"])
            for row in rows
            if (row.get("estado") or "").strip().lower() in _ENTREGA_SOURCE_ESTADOS
        ]

        synced_ids: list[int] = []
        for ingreso_id in eligible_ids:
            deteccion = resolve_ingreso_entrega_estado(ingreso_id)
            estado_to = deteccion["estado_to"]
            sets = [
                "estado = %s",
                "remito_salida = COALESCE(NULLIF(remito_salida, ''), %s)",
                "fecha_entrega = COALESCE(fecha_entrega, CURRENT_TIMESTAMP)",
            ]
            params: list[Any] = [estado_to, remito]
            if deteccion["set_alquilado"]:
                sets.append("alquilado = true")
            if deteccion["clear_alquilado"]:
                sets.append("alquilado = false")
                sets.append("alquiler_a = NULL")
                sets.append("alquiler_remito = NULL")
                sets.append("alquiler_fecha = NULL")
            params.append(ingreso_id)
            cur.execute(
                "UPDATE ingresos SET " + ", ".join(sets) + " WHERE id = %s",
                params,
            )

            comentario = (
                "Alquiler registrado (orden de entrega)"
                if estado_to == "alquilado"
                else "Entrega de venta registrada (orden de entrega)"
                if estado_to == "vendido_entregado"
                else "Entrega registrada (orden de entrega)"
            )
            cur.execute(
                """
                INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, comentario)
                SELECT %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM ingreso_events
                     WHERE ingreso_id=%s AND a_estado=%s
                )
                """,
                [ingreso_id, estado_to, actor_user_id, comentario, ingreso_id, estado_to],
            )
            synced_ids.append(ingreso_id)
    return synced_ids


def _delivery_order_ids_linked_to_service_ingreso(cur, ingreso_id: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT o.id, o.invoice_number
          FROM delivery_orders o
         WHERE o.delivery_type = 'service_release'
           AND o.status <> 'cancelado'
           AND (
                o.ingreso_id = %s
                OR (o.source_system = 'nexora' AND o.source_external_id = %s)
                OR EXISTS (
                    SELECT 1
                      FROM delivery_order_items doi
                     WHERE doi.order_id = o.id
                       AND doi.ingreso_id = %s
                )
                OR EXISTS (
                    SELECT 1
                      FROM delivery_order_item_partidas doip
                      JOIN delivery_order_items doi ON doi.id = doip.order_item_id
                     WHERE doi.order_id = o.id
                       AND doip.ingreso_id = %s
                )
               )
         ORDER BY o.created_at DESC NULLS LAST, o.id DESC
         FOR UPDATE
        """,
        [ingreso_id, str(ingreso_id), ingreso_id, ingreso_id],
    )
    return _rows(cur)


def sync_service_release_orders_invoice_from_ingreso(
    ingreso_id: int,
    invoice_number: str,
    actor_user_id: int | None = None,
) -> list[str]:
    invoice = _optional_text(invoice_number)
    if not invoice:
        raise DeliveryOrderError("INVOICE_REQUIRED", "Número de factura requerido")
    ingreso_id = int(ingreso_id)
    with connection.cursor() as cur:
        rows = _delivery_order_ids_linked_to_service_ingreso(cur, ingreso_id)
        conflicting = [row for row in rows if _optional_text(row.get("invoice_number")) not in (None, invoice)]
        if conflicting:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_INVOICE_CONFLICT",
                "La orden de entrega vinculada ya tiene otra factura registrada.",
                status_code=409,
            )
        target_ids = [row["id"] for row in rows if not _optional_text(row.get("invoice_number"))]
        if target_ids:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET invoice_number = %s,
                       invoiced_by_user_id = COALESCE(invoiced_by_user_id, %s),
                       invoiced_at = COALESCE(invoiced_at, CURRENT_TIMESTAMP)
                 WHERE id = ANY(%s)
                """,
                [invoice, actor_user_id, target_ids],
            )
        linked_ingreso_ids = _service_release_ingreso_ids_for_order_ids(target_ids or [row["id"] for row in rows])
    if linked_ingreso_ids:
        _sync_service_release_ingresos_invoice(linked_ingreso_ids, invoice)
    for order_id in target_ids:
        create_event(
            order_id,
            actor_user_id,
            "delivery_order_invoiced",
            metadata={
                "invoiceNumber": invoice,
                "source": "service_order_billing",
                "ingresoId": ingreso_id,
                "ingresoIds": linked_ingreso_ids,
            },
        )
    return target_ids


def order_requires_billing(order: dict[str, Any]) -> bool:
    remito_number = _optional_text(order.get("remitoNumber") or order.get("remito_number"))
    if not remito_number:
        return False
    group = order.get("bejermanRemitoGroup") if isinstance(order.get("bejermanRemitoGroup"), dict) else {}
    response_summary = group.get("responseSummary") if isinstance(group.get("responseSummary"), dict) else {}
    billing_required = response_summary.get("billingRequired")
    if isinstance(billing_required, bool):
        return billing_required
    if isinstance(billing_required, str) and billing_required.strip().lower() in ("true", "false"):
        return billing_required.strip().lower() == "true"
    return remito_number_requires_billing(remito_number)


def _billing_point_of_sale(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits or len(digits) > 5 or not int(digits):
        return None
    return digits.zfill(5)


def _billing_point_of_sale_from_remito(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    matches = re.findall(r"(?<!\d)(\d{1,5})\s*-\s*\d{1,8}(?!\d)", text)
    return _billing_point_of_sale(matches[-1]) if matches else None


def _billing_company_key_for_marker(value: Any) -> str | None:
    marker = re.sub(r"[^A-Z0-9]", "", _clean_text(value).upper())
    if not marker:
        return None
    if marker in {"MG", "MGB", "MGBI", "MGBIO", "MGBIOSA", "PORMG"}:
        return "MGBIO"
    if "MGBIO" in marker or marker.startswith("MGBI"):
        return "MGBIO"
    if marker in {
        "SEPID",
        "SEP",
        "SEPIDSA",
        "CEPIL",
        "ALTAALQUILER",
        "ALQUILERES",
        "REPARACION",
        "MARKETING",
        "DEMO",
        "RESPIFLOW",
        "NOVAMED",
    }:
        return "SEPID"
    return None


def resolve_delivery_order_billing_company(order: dict[str, Any]) -> tuple[str | None, str | None]:
    """Resuelve empresa y PV efectivos sin asumir SEPID ante datos insuficientes."""
    group = order.get("bejermanRemitoGroup") or order.get("bejerman_remito_group")
    if not isinstance(group, dict):
        group = {}
    response_summary = group.get("responseSummary") or group.get("response_summary")
    response_summary = _decode_json(response_summary, {})
    if not isinstance(response_summary, dict):
        response_summary = {}
    profile = response_summary.get("profile")
    if not isinstance(profile, dict):
        profile = {}

    point_of_sale = next(
        (
            point
            for point in (
                _billing_point_of_sale(group.get("comprobantePtoVenta") or group.get("comprobante_pto_venta")),
                _billing_point_of_sale(
                    response_summary.get("comprobantePtoVenta")
                    or response_summary.get("comprobante_pto_venta")
                    or response_summary.get("pointOfSale")
                    or response_summary.get("point_of_sale")
                ),
                _billing_point_of_sale(profile.get("pointOfSale") or profile.get("point_of_sale")),
                _billing_point_of_sale_from_remito(group.get("remitoNumber") or group.get("remito_number")),
                _billing_point_of_sale_from_remito(
                    response_summary.get("remitoNumber")
                    or response_summary.get("remito_number")
                    or response_summary.get("manualRemitoNumber")
                ),
                _billing_point_of_sale_from_remito(order.get("remitoNumber") or order.get("remito_number")),
            )
            if point
        ),
        None,
    )
    company_for_point = BILLING_COMPANY_BY_POINT_OF_SALE.get(point_of_sale or "")
    if company_for_point:
        return company_for_point, point_of_sale

    company_key = next(
        (
            company
            for company in (
                _billing_company_key_for_marker(group.get("companyKey") or group.get("company_key")),
                _billing_company_key_for_marker(
                    response_summary.get("companyKey") or response_summary.get("company_key")
                ),
                _billing_company_key_for_marker(order.get("companyKey") or order.get("company_key")),
                _billing_company_key_for_marker(
                    order.get("operationCompanyLabel") or order.get("operation_company_label")
                ),
                _billing_company_key_for_marker(order.get("sourceCompanyId") or order.get("source_company_id")),
            )
            if company
        ),
        None,
    )
    return company_key, point_of_sale


def suggested_remito_count(row: dict[str, Any]) -> int:
    """Cantidad conservadora de comprobantes que requiere la impresión.

    La orden no se parte: esta cifra solo guía al usuario y a la emisión, que
    construye subconjuntos transitorios de sus artículos.
    """
    from .bejerman_delivery import _remito_print_item_chunks

    try:
        chunks = _remito_print_item_chunks(
            {
                "deliveryType": row.get("delivery_type") or row.get("deliveryType"),
                "items": row.get("items") or [],
            }
        )
        return max(1, len(chunks))
    except Exception:
        logger.warning("delivery_order_remito_count_check_failed", exc_info=True)
        return 1


def remito_set_status(remitos: list[dict[str, Any]]) -> str:
    if not remitos:
        return "none"
    statuses = {_clean_text(remito.get("status")).lower() for remito in remitos}
    if statuses & {"sending", "sent_unknown", "failed_review"}:
        return "review"

    batch_ids = [_optional_text(remito.get("batchId")) for remito in remitos]
    active_batch_id = next((batch_id for batch_id in reversed(batch_ids) if batch_id), None)
    active = [remito for remito in remitos if not active_batch_id or remito.get("batchId") == active_batch_id]
    expected_count = max((int(remito.get("expectedCount") or 1) for remito in active), default=1)
    completed_sequences = {
        int(remito.get("sequence") or 1)
        for remito in active
        if _clean_text(remito.get("status")).lower() in {"generated", "reconciled"}
    }
    if len(completed_sequences) >= expected_count and all(
        _clean_text(remito.get("status")).lower() in {"generated", "reconciled"}
        for remito in active
    ):
        return "complete"
    return "partial"


def serialize_order(row: dict[str, Any]) -> dict[str, Any]:
    order_id = str(row.get("id") or "")
    remitos = row.get("remito_links") if isinstance(row.get("remito_links"), list) else []
    remito_numbers = [
        number
        for number in (_optional_text(remito.get("remitoNumber")) for remito in remitos)
        if number
    ]
    legacy_remito_number = _optional_text(row.get("remito_number"))
    if not remitos and legacy_remito_number:
        remito_numbers = [legacy_remito_number]
    has_remito = bool(remito_numbers or legacy_remito_number)
    set_status = "complete" if not remitos and legacy_remito_number else remito_set_status(remitos)
    billing_company_key, billing_point_of_sale = resolve_delivery_order_billing_company(row)
    service_release_references = service_release_references_from_order(
        {
            **row,
            "deliveryType": row.get("delivery_type"),
            "ingresoId": row.get("ingreso_id"),
            "sourceSystem": row.get("source_system"),
            "sourceExternalId": row.get("source_external_id"),
        }
    )
    return {
        "id": row.get("id"),
        "orderNumber": delivery_order_number(row),
        "customerId": row.get("customer_id"),
        "customerName": row.get("customer_name") or "",
        "bejermanCustomerCode": row.get("bejerman_customer_code") or "",
        "deliveryType": row.get("delivery_type"),
        "companyKey": row.get("company_key") or "",
        "billingCompanyKey": billing_company_key or "",
        "billingPointOfSale": billing_point_of_sale or "",
        "status": row.get("status"),
        "priority": row.get("priority"),
        "orderDate": _iso(row.get("order_date")),
        "sellerName": row.get("seller_name") or "",
        "sellerCode": row.get("seller_code") or "",
        "equipmentModel": row.get("equipment_model") or "",
        "equipmentSerial": row.get("equipment_serial") or "",
        "equipmentInternalNumber": row.get("equipment_internal_number") or "",
        "operationCompanyLabel": row.get("operation_company_label") or "",
        "rawPedido": row.get("raw_pedido") or "",
        "commercialTerms": row.get("commercial_terms") or "",
        "commercialPrice": row.get("commercial_price") or "",
        "commercialExchangeRate": row.get("commercial_exchange_rate") or "",
        "priceCurrency": _price_currency(row.get("price_currency")),
        "commercialCondition": row.get("commercial_condition") or "",
        "commercialDeadline": row.get("commercial_deadline") or "",
        "remitoNumber": row.get("remito_number") or "",
        "remitoNumbers": remito_numbers,
        "remitos": remitos,
        "remitoSetStatus": set_status,
        "suggestedRemitoCount": suggested_remito_count(row),
        "remitoPdfUrl": _delivery_order_remito_pdf_path(order_id) if has_remito else "",
        "remitoPrintUrl": _delivery_order_remito_print_path(order_id) if has_remito else "",
        "bejermanRemitoGroupId": row.get("bejerman_remito_group_id") or "",
        "remitoLocation": row.get("remito_location") or "",
        "invoiceNumber": row.get("invoice_number") or "",
        "billingPostponedUntil": _iso(row.get("billing_postponed_until")),
        "billingPostponedByUserId": row.get("billing_postponed_by_user_id"),
        "billingPostponedAt": _iso(row.get("billing_postponed_at")),
        "billingPostponementNotifiedAt": _iso(row.get("billing_postponement_notified_at")),
        "ingresoId": row.get("ingreso_id"),
        "deviceId": row.get("device_id"),
        "propietarioNombre": row.get("propietario_nombre") or "",
        "propietarioContacto": row.get("propietario_contacto") or "",
        "propietarioDoc": row.get("propietario_doc") or "",
        "sourceSystem": row.get("source_system") or "",
        "sourceExternalId": row.get("source_external_id") or "",
        "sourceReference": delivery_source_reference(row.get("source_reference"), row.get("delivery_type"), row.get("ingreso_id")),
        "sourceCompanyId": row.get("source_company_id") or "",
        "sourceSheet": row.get("source_sheet") or "",
        "sourceRow": row.get("source_row"),
        "sourceColor": row.get("source_color") or "",
        "preparedAt": _iso(row.get("prepared_at")),
        "deliveredAt": _iso(row.get("delivered_at")),
        "invoicedAt": _iso(row.get("invoiced_at")),
        "cancelledAt": _iso(row.get("cancelled_at")),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
        "serviceReleaseCount": len(service_release_references),
        "serviceReleaseReferences": service_release_references,
        "bejermanRemitoGroup": row.get("bejerman_remito_group") or None,
        "lastRemitoFailure": (
            row.get("last_remito_failure")
            if not has_remito and not row.get("bejerman_remito_group_id")
            else None
        ),
        "items": row.get("items") or [],
        "events": row.get("events") or [],
    }


def delivery_order_item_amounts(item: dict[str, Any]) -> dict[str, Decimal] | None:
    unit_price = _decimal_or_none(item.get("unitPrice"))
    if unit_price is None:
        return None
    quantity = _decimal_or_none(item.get("quantity")) or Decimal("0")
    discount_percent = _discount_percent(item.get("discountPercent"))
    gross_subtotal = quantity * unit_price
    discount_amount = gross_subtotal * discount_percent / Decimal("100")
    net_subtotal = gross_subtotal - discount_amount
    return {
        "grossSubtotal": gross_subtotal,
        "discountAmount": discount_amount,
        "netSubtotal": net_subtotal,
    }


def _serialize_item(row: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": row.get("id"),
        "orderId": row.get("order_id"),
        "ingresoId": row.get("ingreso_id"),
        "deviceId": row.get("device_id"),
        "articleCode": row.get("article_code") or "",
        "articleName": row.get("article_name") or "",
        "articleRequiresPartida": _nullable_bool(row.get("article_requires_partida")),
        "description": row.get("description") or "",
        "quantity": float(row.get("quantity") or 0),
        "unitPrice": float(row["unit_price"]) if row.get("unit_price") is not None else None,
        "priceCurrency": _price_currency(row.get("price_currency")),
        "discountPercent": float(row.get("discount_percent") or 0),
        "sourceText": row.get("source_text") or "",
        "partida": row.get("partida") or "",
        "partidaExpirationDate": _iso(row.get("partida_expiration_date")),
        "stockDepositCode": row.get("stock_deposit_code") or "",
        "stockAvailableQuantity": (
            float(row["stock_available_quantity"]) if row.get("stock_available_quantity") is not None else None
        ),
        "stockCheckedAt": _iso(row.get("stock_checked_at")),
        "sortOrder": row.get("sort_order") or 0,
        "partidas": row.get("partidas") or [],
    }
    amounts = delivery_order_item_amounts(item)
    if amounts is None:
        item.update({"grossSubtotal": None, "discountAmount": None, "netSubtotal": None})
    else:
        item.update({key: float(value) for key, value in amounts.items()})
    return item


def _serialize_partida(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "orderItemId": row.get("order_item_id"),
        "ingresoId": row.get("ingreso_id"),
        "deviceId": row.get("device_id"),
        "partida": row.get("partida") or "",
        "assignedQuantity": float(row.get("assigned_quantity") or 0),
        "partidaExpirationDate": _iso(row.get("partida_expiration_date")),
        "stockDepositCode": row.get("stock_deposit_code") or "",
        "stockAvailableQuantity": (
            float(row["stock_available_quantity"]) if row.get("stock_available_quantity") is not None else None
        ),
        "stockCheckedAt": _iso(row.get("stock_checked_at")),
        "sortOrder": row.get("sort_order") or 0,
    }


def _item_needs_partida_policy_resolution(order: dict[str, Any], item: dict[str, Any]) -> bool:
    if normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type")) not in {"sale", "demo"}:
        return False
    if not _optional_text(item.get("articleCode") or item.get("article_code")):
        return False
    if _item_article_requires_partida(item) is not None:
        return False
    return not _partida_rows_for_prepared_validation(item)


def _lookup_article_requires_partida(
    article_code: str,
    company_key: str,
    actor_user_id: int | None,
    cache: dict[str, Any],
    *,
    allow_remote_fetch: bool = True,
) -> bool | None:
    from .bejerman_sdk import BejermanSDKClient
    from . import bejerman_catalog_cache

    code_key = _article_code_key(article_code)
    lookup_key = f"{company_key}|{code_key}"
    if lookup_key in cache:
        return cache[lookup_key]

    # ObtenerArticulos devuelve el catálogo completo aunque se le pase un filtro
    # por código, así que se indexa una vez por empresa en lugar de repetir la
    # descarga por cada artículo de la orden.
    company_cache = cache.setdefault(f"client:{company_key}", {})
    articles_by_code = company_cache.get("articles_by_code")
    if articles_by_code is None:
        if allow_remote_fetch:
            client = company_cache.get("client")
            if client is None:
                client = BejermanSDKClient(
                    company_key=company_key,
                    actor_user_id=actor_user_id,
                    allow_system_credentials=True,
                )
                company_cache["client"] = client
            catalog = bejerman_catalog_cache.article_catalog(client, company_key)
        else:
            catalog = bejerman_catalog_cache.article_catalog_if_cached(company_key)
            if catalog is None:
                # Sin catálogo caliente se deja el flag sin resolver en vez de
                # bloquear al usuario con una descarga completa por SOAP.
                return None
        articles_by_code = {}
        for article in catalog.items:
            articles_by_code.setdefault(_article_code_key(article.get("code")), article)
        company_cache["articles_by_code"] = articles_by_code

    article = articles_by_code.get(code_key)
    result = _nullable_bool(article.get("requiresPartida")) if article else None
    cache[lookup_key] = result
    return result


def refresh_delivery_order_article_partida_flags(
    order: dict[str, Any],
    actor_user_id: int | None,
    *,
    strict: bool = False,
    action: str = "continuar",
    allow_remote_fetch: bool = True,
) -> dict[str, Any]:
    """Resuelve contra Bejerman si los artículos de la orden llevan partida.

    `allow_remote_fetch=False` limita la resolución al catálogo ya cacheado: sirve
    para las lecturas de pantalla, donde conviene mostrar el dato si está a mano
    pero no hacer esperar al usuario una descarga completa del catálogo."""
    if normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type")) not in {"sale", "demo"}:
        return order

    company_key = normalize_company_key(order.get("companyKey") or order.get("company_key") or "SEPID")
    cache: dict[str, Any] = {}
    updates: list[tuple[str, bool]] = []
    for index, item in enumerate(order.get("items") or [], start=1):
        if not _item_needs_partida_policy_resolution(order, item):
            continue
        article_code = _optional_text(item.get("articleCode") or item.get("article_code"))
        try:
            requires_partida = _lookup_article_requires_partida(
                article_code,
                company_key,
                actor_user_id,
                cache,
                allow_remote_fetch=allow_remote_fetch,
            )
        except Exception as exc:
            logger.warning(
                "delivery_order_article_partida_policy_lookup_failed",
                extra={"article_code": article_code, "company_key": company_key},
                exc_info=True,
            )
            if strict:
                raise DeliveryOrderError(
                    "DELIVERY_ORDER_ARTICLE_PARTIDA_POLICY_UNAVAILABLE",
                    f"No se pudo verificar en Bejerman si el artículo {article_code} requiere partida. Reintente o cargue partidas antes de {action}.",
                    status_code=502,
                ) from exc
            continue
        if requires_partida is None:
            if strict:
                raise _partida_policy_unknown_error(item, index, action)
            continue

        item["articleRequiresPartida"] = requires_partida
        item_id = _optional_text(item.get("id"))
        if item_id:
            updates.append((item_id, requires_partida))

    if updates:
        with connection.cursor() as cur:
            for item_id, requires_partida in updates:
                cur.execute(
                    """
                    UPDATE delivery_order_items
                       SET article_requires_partida = %s
                     WHERE id = %s
                    """,
                    [requires_partida, item_id],
                )
    return order


def _remito_group_order_ids(row: dict[str, Any]) -> list[str]:
    return [
        order_id
        for order_id in (
            _optional_text(order_id)
            for order_id in (_decode_json(row.get("order_ids"), []) or [])
        )
        if order_id
    ]


def _serialize_remito_group(
    row: dict[str, Any],
    orders_by_group: dict[str, list[dict[str, Any]]],
    order_ids_by_group: dict[str, list[str]],
) -> dict[str, Any]:
    group_id = row.get("id")
    orders = orders_by_group.get(group_id, [])
    status = row.get("status") or ""
    generated = status in {"generated", "reconciled"}
    uncertain = status in {"sent_unknown", "failed_review"}
    response_summary = _decode_json(row.get("response_summary"), {}) or {}
    registered_mode = str(
        response_summary.get("documentMode")
        or response_summary.get("document_mode")
        or response_summary.get("mode")
        or ""
    ).strip().lower() in {"register", "registered", "registrar", "registrado"}
    printable = generated and not registered_mode
    return {
        "id": group_id,
        "comprobanteTipo": row.get("comprobante_tipo") or "",
        "comprobanteLetra": row.get("comprobante_letra") or "",
        "comprobantePtoVenta": row.get("comprobante_pto_venta") or "",
        "comprobanteNumero": row.get("comprobante_numero") or "",
        "status": status,
        "generated": generated,
        "reconciled": status == "reconciled",
        "uncertain": uncertain,
        "reviewRequired": status == "failed_review",
        "remitoNumber": row.get("remito_number") or "",
        "customerCode": row.get("customer_code") or "",
        "customerName": row.get("customer_name") or "",
        "companyKey": row.get("company_key") or "",
        "sellerCode": row.get("seller_code") or "",
        "paymentTermCode": row.get("payment_term_code") or "",
        "operationCode": row.get("operation_code") or "",
        "depositCode": row.get("deposit_code") or "",
        "responseSummary": response_summary if isinstance(response_summary, dict) else {},
        "createdAt": _iso(row.get("created_at")),
        "generatedAt": _iso(row.get("generated_at")),
        "orderCount": len(order_ids_by_group.get(group_id, [])) or len(orders),
        "pdfUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/" if printable else None,
        "printUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/print/" if printable else None,
        "orders": [
            {
                "id": order.get("id"),
                "orderNumber": order.get("order_number") or "",
                "deliveryType": order.get("delivery_type") or "",
                "sourceReference": delivery_source_reference(order.get("source_reference"), order.get("delivery_type"), order.get("ingreso_id")),
                "customerName": order.get("customer_name") or "",
                "equipmentModel": order.get("equipment_model") or "",
                "equipmentSerial": order.get("equipment_serial") or "",
                "rawPedido": order.get("raw_pedido") or "",
            }
            for order in orders
        ],
    }


def _serialize_remito_groups(group_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    order_ids_by_group: dict[str, list[str]] = {}
    all_order_ids: set[str] = set()
    for group in group_rows:
        order_ids = _remito_group_order_ids(group)
        order_ids_by_group[group["id"]] = order_ids
        all_order_ids.update(order_ids)

    orders_by_id: dict[str, dict[str, Any]] = {}
    if all_order_ids:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_number, delivery_type, source_reference, customer_name,
                       equipment_model, equipment_serial, raw_pedido
                FROM delivery_orders
                WHERE id = ANY(%s)
                """,
                [list(all_order_ids)],
            )
            orders_by_id = {row["id"]: row for row in _rows(cur)}

    orders_by_group = {
        group_id: [orders_by_id[order_id] for order_id in order_ids if order_id in orders_by_id]
        for group_id, order_ids in order_ids_by_group.items()
    }
    return {
        group["id"]: _serialize_remito_group(group, orders_by_group, order_ids_by_group)
        for group in group_rows
    }


def load_remito_groups_by_id(group_ids: list[str]) -> dict[str, dict[str, Any]]:
    clean_ids = sorted({_optional_text(group_id) for group_id in group_ids if _optional_text(group_id)})
    if not clean_ids:
        return {}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                   comprobante_numero, remito_number, customer_code, customer_name,
                   seller_code, payment_term_code, operation_code, deposit_code,
                   status, order_ids, response_summary, created_at, generated_at
            FROM bejerman_remito_groups
            WHERE id = ANY(%s)
            """,
            [clean_ids],
        )
        group_rows = _rows(cur)
    return _serialize_remito_groups(group_rows)


def load_delivery_order_remitos(order_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    clean_ids = sorted({_optional_text(order_id) for order_id in order_ids if _optional_text(order_id)})
    if not clean_ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT l.id AS link_id, l.order_id, l.remito_group_id, l.batch_id,
                   l.sequence, l.expected_count, l.allocation_status,
                   l.created_at AS link_created_at, l.updated_at AS link_updated_at
              FROM delivery_order_remito_links l
             WHERE l.order_id = ANY(%s)
             ORDER BY l.order_id, l.created_at, l.batch_id, l.sequence, l.id
            """,
            [clean_ids],
        )
        link_rows = _rows(cur)

        link_ids = [row["link_id"] for row in link_rows]
        allocation_rows_by_link: dict[str, list[dict[str, Any]]] = {}
        if link_ids:
            cur.execute(
                """
                SELECT a.id, a.link_id, a.order_item_id, a.order_item_partida_id,
                       a.assigned_quantity, a.sort_order, p.partida
                  FROM delivery_order_remito_item_allocations a
                  LEFT JOIN delivery_order_item_partidas p ON p.id = a.order_item_partida_id
                 WHERE a.link_id = ANY(%s)
                 ORDER BY a.link_id, a.sort_order, a.id
                """,
                [link_ids],
            )
            for allocation in _rows(cur):
                allocation_rows_by_link.setdefault(allocation["link_id"], []).append(
                    {
                        "id": allocation.get("id"),
                        "itemId": allocation.get("order_item_id"),
                        "partidaId": allocation.get("order_item_partida_id"),
                        "partida": allocation.get("partida") or "",
                        "quantity": float(allocation.get("assigned_quantity") or 0),
                        "sortOrder": allocation.get("sort_order") or 0,
                    }
                )

    allocations_by_link: dict[str, list[dict[str, Any]]] = {}
    for link_id, allocation_rows in allocation_rows_by_link.items():
        allocations_by_item: dict[str, dict[str, Any]] = {}
        for allocation_row in allocation_rows:
            item_id = _optional_text(allocation_row.get("itemId"))
            if not item_id:
                continue
            item_allocation = allocations_by_item.setdefault(
                item_id,
                {
                    "itemId": item_id,
                    "quantity": 0.0,
                    "partidas": [],
                    "sortOrder": allocation_row.get("sortOrder") or 0,
                },
            )
            quantity = float(allocation_row.get("quantity") or 0)
            item_allocation["quantity"] += quantity
            if allocation_row.get("partidaId") or allocation_row.get("partida"):
                item_allocation["partidas"].append(
                    {
                        "id": allocation_row.get("id"),
                        "partidaId": allocation_row.get("partidaId"),
                        "partida": allocation_row.get("partida") or "",
                        "quantity": quantity,
                        "sortOrder": allocation_row.get("sortOrder") or 0,
                    }
                )
        allocations_by_link[link_id] = sorted(
            allocations_by_item.values(),
            key=lambda allocation: (allocation.get("sortOrder") or 0, allocation.get("itemId") or ""),
        )

    groups_by_id = load_remito_groups_by_id([row["remito_group_id"] for row in link_rows])
    result: dict[str, list[dict[str, Any]]] = {order_id: [] for order_id in clean_ids}
    for link in link_rows:
        group = groups_by_id.get(link["remito_group_id"]) or {}
        response_summary = group.get("responseSummary") if isinstance(group.get("responseSummary"), dict) else {}
        document_mode = _clean_text(
            response_summary.get("documentMode")
            or response_summary.get("document_mode")
            or response_summary.get("mode")
        ).lower()
        result.setdefault(link["order_id"], []).append(
            {
                "id": link.get("link_id"),
                "linkId": link.get("link_id"),
                "groupId": link.get("remito_group_id"),
                "batchId": link.get("batch_id"),
                "sequence": link.get("sequence") or 1,
                "expectedCount": link.get("expected_count") or 1,
                "allocationStatus": link.get("allocation_status") or "external_unknown",
                "documentMode": document_mode or ("existing" if group.get("reconciled") else "emit"),
                "status": group.get("status") or "",
                "generated": bool(group.get("generated")),
                "uncertain": bool(group.get("uncertain")),
                "remitoNumber": group.get("remitoNumber") or "",
                "companyKey": group.get("companyKey") or "",
                "pdfUrl": group.get("pdfUrl"),
                "printUrl": group.get("printUrl"),
                "allocations": allocations_by_link.get(link["link_id"], []),
                "createdAt": _iso(link.get("link_created_at")),
                "updatedAt": _iso(link.get("link_updated_at")),
            }
        )
    return result


def sync_delivery_order_remito_projection(
    order_ids: list[str],
    actor_user_id: int | None = None,
) -> dict[str, str]:
    """Sincroniza los campos escalares legados desde las asociaciones canónicas."""
    clean_ids = sorted({_optional_text(order_id) for order_id in order_ids if _optional_text(order_id)})
    remitos_by_order = load_delivery_order_remitos(clean_ids)
    statuses: dict[str, str] = {}
    with connection.cursor() as cur:
        for order_id in clean_ids:
            remitos = remitos_by_order.get(order_id, [])
            set_status = remito_set_status(remitos)
            statuses[order_id] = set_status
            remito_numbers = [
                number
                for number in (_optional_text(remito.get("remitoNumber")) for remito in remitos)
                if number
            ]
            first_group_id = next(
                (_optional_text(remito.get("groupId")) for remito in remitos if _optional_text(remito.get("groupId"))),
                None,
            )
            complete = set_status == "complete"
            cur.execute(
                """
                UPDATE delivery_orders
                   SET remito_number = %s,
                       bejerman_remito_group_id = %s,
                       status = CASE
                           WHEN %s AND status IN ('pendiente_armado', 'armado_pendiente_entrega')
                           THEN 'armado_pendiente_entrega'
                           ELSE status
                       END,
                       remito_location = CASE
                           WHEN %s THEN COALESCE(remito_location, 'recepcion')
                           ELSE remito_location
                       END,
                       remito_location_updated_by = CASE
                           WHEN %s THEN COALESCE(remito_location_updated_by, %s)
                           ELSE remito_location_updated_by
                       END,
                       remito_location_updated_at = CASE
                           WHEN %s THEN COALESCE(remito_location_updated_at, CURRENT_TIMESTAMP)
                           ELSE remito_location_updated_at
                       END,
                       prepared_by_user_id = CASE
                           WHEN %s THEN COALESCE(prepared_by_user_id, %s)
                           ELSE prepared_by_user_id
                       END,
                       prepared_at = CASE
                           WHEN %s THEN COALESCE(prepared_at, CURRENT_TIMESTAMP)
                           ELSE prepared_at
                       END
                 WHERE id = %s
                """,
                [
                    " / ".join(remito_numbers) or None,
                    first_group_id,
                    complete,
                    complete,
                    complete,
                    actor_user_id,
                    complete,
                    complete,
                    actor_user_id,
                    complete,
                    order_id,
                ],
            )
    return statuses


def load_last_remito_failures_by_order(order_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Último rechazo terminal de emisión (_mark_group_failed en bejerman_delivery.py
    crea el evento bejerman_remito_failed) por orden. Se usa para explicar en la fila
    de Órdenes de entrega por qué una orden sin remito ni grupo activo quedó ahí, y
    permitir reintentar sin ir al panel de revisión de Bejerman."""
    clean_ids = sorted({_optional_text(order_id) for order_id in order_ids if _optional_text(order_id)})
    if not clean_ids:
        return {}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (order_id) order_id, metadata, created_at
              FROM delivery_order_events
             WHERE order_id = ANY(%s) AND event_type = 'bejerman_remito_failed'
             ORDER BY order_id, created_at DESC, id DESC
            """,
            [clean_ids],
        )
        rows = _rows(cur)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        metadata = _decode_json(row.get("metadata"), {})
        message = _optional_text(metadata.get("error")) if isinstance(metadata, dict) else ""
        if not message:
            continue
        out[row["order_id"]] = {
            "message": message,
            "occurredAt": _iso(row.get("created_at")),
        }
    return out


def _history_param(filters: Any, *names: str) -> str:
    if not hasattr(filters, "get"):
        return ""
    for name in names:
        value = _optional_text(filters.get(name))
        if value:
            return value
    return ""


def _history_limit(filters: Any) -> int:
    if hasattr(filters, "get"):
        raw_limit = filters.get("limit") or filters.get("pageSize") or filters.get("page_size") or 20
    else:
        raw_limit = filters
    try:
        return max(1, min(100, int(raw_limit or 20)))
    except (TypeError, ValueError):
        return 20


def _history_match_text(group: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in (
        "id",
        "comprobanteTipo",
        "comprobanteLetra",
        "comprobantePtoVenta",
        "comprobanteNumero",
        "status",
        "remitoNumber",
        "customerCode",
        "customerName",
        "companyKey",
        "sellerCode",
        "paymentTermCode",
        "operationCode",
        "depositCode",
    ):
        chunks.append(_optional_text(group.get(key)))
    response_summary = group.get("responseSummary")
    if isinstance(response_summary, dict):
        chunks.append(json.dumps(response_summary, ensure_ascii=False, default=str))
    for order in group.get("orders") or []:
        if not isinstance(order, dict):
            continue
        for key in (
            "id",
            "orderNumber",
            "deliveryType",
            "sourceReference",
            "customerName",
            "equipmentModel",
            "equipmentSerial",
            "rawPedido",
        ):
            chunks.append(_optional_text(order.get(key)))
    return " ".join(chunk for chunk in chunks if chunk)


def _history_matches(group: dict[str, Any], search: str, status: str, company_key: str) -> bool:
    if status and _optional_text(group.get("status")).lower() != status.lower():
        return False
    if company_key and _optional_text(group.get("companyKey")).upper() != company_key.upper():
        return False
    if not search:
        return True
    haystack = _history_match_text(group).casefold()
    needle = search.casefold()
    if needle in haystack:
        return True
    compact_haystack = re.sub(r"[\s\-./]+", "", haystack)
    compact_needle = re.sub(r"[\s\-./]+", "", needle)
    if compact_needle and compact_needle in compact_haystack:
        return True
    digit_needle = re.sub(r"\D+", "", needle)
    return bool(digit_needle and digit_needle in re.sub(r"\D+", "", haystack))


def list_remito_history(limit: Any = 20) -> list[dict[str, Any]]:
    filters = limit if hasattr(limit, "get") else None
    safe_limit = _history_limit(limit)
    search = _history_param(filters, "search", "q", "remito", "remitoNumber")
    status = _history_param(filters, "status", "estado")
    company_key = _history_param(filters, "companyKey", "company_key", "empresa")
    query_limit = 500 if search or status or company_key else safe_limit
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                   comprobante_numero, remito_number, customer_code, customer_name,
                   seller_code, payment_term_code, operation_code, deposit_code,
                   status, order_ids, response_summary, created_at, generated_at
            FROM bejerman_remito_groups
            ORDER BY COALESCE(generated_at, created_at) DESC, id DESC
            LIMIT %s
            """,
            [query_limit],
        )
        group_rows = _rows(cur)
    groups = list(_serialize_remito_groups(group_rows).values())
    return [group for group in groups if _history_matches(group, search, status, company_key)][:safe_limit]


def create_event(
    order_id: str,
    actor_user_id: int | None,
    event_type: str,
    *,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
    source_actor_user_id: str | None = None,
):
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO delivery_order_events (
              order_id, actor_user_id, source_actor_user_id, event_type, note, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            [order_id, actor_user_id, source_actor_user_id, event_type, note, _json_param(metadata or {})],
        )


def delivery_order_item_subtotal(item: dict[str, Any]) -> Decimal | None:
    amounts = delivery_order_item_amounts(item)
    if amounts is None:
        return None
    return amounts["netSubtotal"]


def delivery_order_item_price_currency(item: dict[str, Any], fallback: str = "ARS") -> str:
    return _price_currency(item.get("priceCurrency") if isinstance(item, dict) else None, fallback)


def delivery_order_billing_totals(order: dict[str, Any]) -> dict[str, Any]:
    items = order.get("items") if isinstance(order, dict) else []
    if not isinstance(items, list):
        items = []
    default_currency = _price_currency(order.get("priceCurrency") if isinstance(order, dict) else None)
    gross_total = Decimal("0")
    discount_total = Decimal("0")
    total = Decimal("0")
    priced_items = 0
    missing_price_items = 0
    totals_by_currency: dict[str, dict[str, Any]] = {}
    for item in items:
        item_dict = item if isinstance(item, dict) else {}
        item_currency = delivery_order_item_price_currency(item_dict, default_currency)
        bucket = totals_by_currency.setdefault(
            item_currency,
            {
                "currency": item_currency,
                "grossTotal": Decimal("0"),
                "discountTotal": Decimal("0"),
                "total": Decimal("0"),
                "pricedItems": 0,
                "missingPriceItems": 0,
            },
        )
        amounts = delivery_order_item_amounts(item_dict)
        if amounts is None:
            missing_price_items += 1
            bucket["missingPriceItems"] += 1
            continue
        gross_total += amounts["grossSubtotal"]
        discount_total += amounts["discountAmount"]
        total += amounts["netSubtotal"]
        priced_items += 1
        bucket["grossTotal"] += amounts["grossSubtotal"]
        bucket["discountTotal"] += amounts["discountAmount"]
        bucket["total"] += amounts["netSubtotal"]
        bucket["pricedItems"] += 1
    active_currencies = [
        currency
        for currency, bucket in totals_by_currency.items()
        if bucket["pricedItems"] > 0 or bucket["missingPriceItems"] > 0
    ]
    if not active_currencies:
        active_currencies = [default_currency]
        totals_by_currency.setdefault(
            default_currency,
            {
                "currency": default_currency,
                "grossTotal": Decimal("0"),
                "discountTotal": Decimal("0"),
                "total": Decimal("0"),
                "pricedItems": 0,
                "missingPriceItems": 0,
            },
        )
    active_currencies = [currency for currency in ("ARS", "USD") if currency in active_currencies]
    mixed_currency = len(active_currencies) > 1
    return {
        "itemCount": len(items),
        "pricedItems": priced_items,
        "missingPriceItems": missing_price_items,
        "hasMissingPrices": missing_price_items > 0,
        "currency": active_currencies[0] if not mixed_currency else "MIXED",
        "currencies": active_currencies,
        "mixedCurrency": mixed_currency,
        "totalsByCurrency": {currency: totals_by_currency[currency] for currency in active_currencies},
        "grossTotal": gross_total,
        "discountTotal": discount_total,
        "total": total,
    }


def _billing_order_path(order_id: str) -> str:
    return f"/cobranzas/facturacion?orderId={quote(str(order_id or ''), safe='')}"


def _delivery_order_remito_pdf_path(order_id: str) -> str:
    return f"/api/ordenes-entrega/{quote(str(order_id or ''), safe='')}/remito/pdf/"


def _delivery_order_remito_print_path(order_id: str) -> str:
    return f"/api/ordenes-entrega/{quote(str(order_id or ''), safe='')}/remito/print/"


def delivery_order_billing_url(order: dict[str, Any]) -> str:
    return _frontend_link(_billing_order_path(str(order.get("id") or "")))


def delivery_order_remito_print_url(order: dict[str, Any]) -> str:
    direct_print_url = _optional_text(order.get("remitoPrintUrl") or order.get("remito_print_url"))
    if direct_print_url:
        return _frontend_link(direct_print_url)
    group = order.get("bejermanRemitoGroup") if isinstance(order.get("bejermanRemitoGroup"), dict) else {}
    print_url = _optional_text(group.get("printUrl"))
    return _frontend_link(print_url) if print_url else ""


def _order_item_email_label(item: dict[str, Any], currency: Any = "ARS") -> str:
    quantity = _decimal_or_none(item.get("quantity")) or Decimal("1")
    quantity_label = f"{quantity.normalize():f}".rstrip("0").rstrip(".") if quantity == quantity.to_integral() else f"{quantity:f}"
    code = _optional_text(item.get("articleCode"))
    name = _optional_text(item.get("articleName")) or _optional_text(item.get("description")) or _optional_text(item.get("sourceText"))
    label = f"{code} - {name}" if code and name and code != name else code or name or "Artículo sin identificar"
    unit_price = _decimal_or_none(item.get("unitPrice"))
    amounts = delivery_order_item_amounts(item)
    discount_percent = _discount_percent(item.get("discountPercent"))
    price_label = "sin precio" if unit_price is None else f"{_money_label(unit_price, currency)} c/u"
    if unit_price is not None and discount_percent > 0:
        price_label = f"{price_label}, desc. {discount_percent.normalize():f}%"
    subtotal_label = "" if amounts is None else f" = {_money_label(amounts['netSubtotal'], currency)}"
    return f"{quantity_label} x {label} ({price_label}{subtotal_label})"


def _delivery_order_email_lines(order: dict[str, Any], *, max_items: int = 8) -> list[str]:
    totals = delivery_order_billing_totals(order)
    currency = totals["currency"]
    currency_label = "Mixta ($ / U$S)" if totals.get("mixedCurrency") else _currency_symbol(currency)
    lines = [
        f"Remito: {order.get('remitoNumber') or '-'}",
        f"Orden: {order.get('orderNumber') or '-'}",
        f"Cliente: {order.get('customerName') or '-'}",
        f"Moneda: {currency_label}",
        f"Código Bejerman: {order.get('bejermanCustomerCode') or '-'}",
    ]
    if totals.get("discountTotal") and totals["discountTotal"] > 0:
        lines.append(f"Subtotal lista: {_totals_money_label(totals, 'grossTotal')}")
        lines.append(f"Descuentos: {_totals_money_label(totals, 'discountTotal')}")
    lines.append(f"Total estimado: {_totals_money_label(totals, 'total')}")
    if totals["hasMissingPrices"]:
        lines.append(f"Atención: {totals['missingPriceItems']} ítem(s) sin precio cargado.")
    items = order.get("items") if isinstance(order.get("items"), list) else []
    if items:
        lines.extend(["", "Ítems:"])
        for item in items[:max_items]:
            if isinstance(item, dict):
                item_currency = delivery_order_item_price_currency(item, currency if currency != "MIXED" else "ARS")
                lines.append(f"- {_order_item_email_label(item, item_currency)}")
        if len(items) > max_items:
            lines.append(f"- +{len(items) - max_items} ítem(s) más")
    raw_pedido = _optional_text(order.get("rawPedido"))
    if raw_pedido:
        lines.extend(["", "Detalle completo de la entrega:", raw_pedido])
    lines.extend(["", f"Ver en NEXORA: {delivery_order_billing_url(order)}"])
    print_url = delivery_order_remito_print_url(order)
    if print_url:
        lines.append(f"Remito imprimible: {print_url}")
    return lines


def _send_delivery_order_billing_email(order: dict[str, Any], users: list[dict[str, Any]]) -> int:
    recipients = notification_email_recipients_for_users(users)
    if not recipients:
        return 0
    subject = f"Remito pendiente de facturación {order.get('remitoNumber') or order.get('orderNumber') or ''}".strip()
    body = _email_append_footer_text(
        "\n".join(
            [
                "Hay un remito pendiente de facturación.",
                "",
                *_delivery_order_email_lines(order),
            ]
        )
    )
    queued = queue_notification_email(
        notification_key="sales_order_remito_ready",
        dedupe_key=f"sales_order_remito_ready:sales_order:{order.get('id')}",
        recipients=recipients,
        subject=subject,
        body=body,
    )
    return int(queued["queued"] + queued["reactivated"])


def _send_delivery_order_commercial_correction_email(
    order: dict[str, Any],
    users: list[dict[str, Any]],
    correction: dict[str, Any],
    dedupe_key: str,
) -> int:
    recipients = notification_email_recipients_for_users(users)
    if not recipients:
        return 0
    subject = f"Corrección comercial en {order.get('orderNumber') or order.get('id') or ''}".strip()
    body = _email_append_footer_text(
        "\n".join(
            [
                "Se modificaron precio o plazo de una orden ya entregada con remito.",
                "La corrección se registró únicamente en NEXORA; el RT emitido en Bejerman no fue modificado.",
                "",
                f"Orden: {order.get('orderNumber') or '-'}",
                f"Remito: {order.get('remitoNumber') or '-'}",
                f"Cliente: {order.get('customerName') or '-'}",
                f"Plazo anterior: {correction.get('previousDeadline') or '-'}",
                f"Plazo nuevo: {correction.get('newDeadline') or '-'}",
                f"Total anterior: {correction.get('previousTotalLabel') or '-'}",
                f"Total nuevo: {correction.get('newTotalLabel') or '-'}",
                "",
                f"Ver en NEXORA: {delivery_order_billing_url(order)}",
            ]
        )
    )
    queued = queue_notification_email(
        notification_key="sales_order_commercial_terms_updated",
        dedupe_key=dedupe_key,
        recipients=recipients,
        subject=subject,
        body=body,
    )
    return int(queued["queued"] + queued["reactivated"])


def _delivery_order_notification(order_id: str, actor_user_id: int | None, kind: str) -> int:
    try:
        order = get_delivery_order(order_id, include_events=False)
        totals = delivery_order_billing_totals(order)
        # Una orden se crea una sola vez, pero puede quedar lista para facturar
        # más de una vez (remito nuevo, reemisión). Sin discriminar por remito el
        # ON CONFLICT del segundo aviso lo descartaba para siempre.
        dedupe_suffix = ""
        if kind == "created":
            notification_key = "sales_order_created"
            roles = ["recepcion"]
            title = f"Nueva orden de entrega {order.get('orderNumber') or order_id}"
            body = (
                f"{order.get('customerName') or '-'} - "
                f"{DELIVERY_TYPE_LABELS.get(order.get('deliveryType'), order.get('deliveryType') or '-')}. "
                "Pendiente de preparación."
            )
            href = f"/administracion/ordenes-entrega?orderId={quote(str(order_id), safe='')}"
        elif kind == "remito_ready":
            notification_key = "sales_order_remito_ready"
            roles = ["cobranzas"]
            dedupe_suffix = f":{str(order.get('remitoNumber') or '-').strip()}"
            title = f"Entrega lista para facturar {order.get('orderNumber') or order_id}"
            body = (
                f"{order.get('customerName') or '-'} - remito {order.get('remitoNumber') or '-'}. "
                f"Total estimado: {_totals_money_label(totals, 'total')}. "
                "Registrar la factura cuando corresponda."
            )
            href = _billing_order_path(order_id)
        else:
            return 0

        recipient_users = active_users_for_notification(notification_key, roles=roles, channel="any")
        recipients = [int(row["id"]) for row in recipient_users]
        if not recipient_users:
            create_event(
                order_id,
                actor_user_id,
                "notification_failed",
                metadata={"kind": kind, "roles": roles, "reason": "no_active_recipients"},
            )
            return 0

        inserted = emit_notification(
            notification_key,
            user_ids=recipients,
            title=title,
            body=body,
            href=href,
            entity_type="delivery_order",
            entity_id=order_id,
            dedupe_key=f"{notification_key}:sales_order:{order_id}{dedupe_suffix}",
            payload={
                "orderId": order_id,
                "orderNumber": order.get("orderNumber"),
                "customerName": order.get("customerName"),
                "deliveryType": order.get("deliveryType"),
                "remitoNumber": order.get("remitoNumber"),
                "priority": order.get("priority"),
                "estimatedTotal": str(totals["total"]),
                "estimatedCurrency": totals["currency"],
                "billingUrl": delivery_order_billing_url(order),
                "remitoPrintUrl": delivery_order_remito_print_url(order),
            },
            push=True,
        )
        email_queued = 0
        if kind == "remito_ready":
            email_users = active_users_for_notification(notification_key, roles=roles, channel="email", require_email=True)
            email_queued = _send_delivery_order_billing_email(order, email_users)
        create_event(
            order_id,
            actor_user_id,
            "notification_sent" if inserted else "notification_skipped",
            metadata={"kind": kind, "roles": roles, "recipients": inserted, "emailQueued": email_queued},
        )
        return inserted
    except Exception:
        logger.exception("delivery_order_notification_failed", extra={"order_id": order_id, "kind": kind})
        try:
            create_event(
                order_id,
                actor_user_id,
                "notification_failed",
                metadata={"kind": kind, "reason": "exception"},
            )
        except Exception:
            pass
        return 0


def notify_delivery_order_created(order_id: str, actor_user_id: int | None = None) -> int:
    return _delivery_order_notification(order_id, actor_user_id, "created")


def notify_delivery_order_remito_ready(order_id: str, actor_user_id: int | None = None) -> int:
    return _delivery_order_notification(order_id, actor_user_id, "remito_ready")


def notify_delivery_order_commercial_correction(
    order_id: str,
    actor_user_id: int | None,
    correction: dict[str, Any],
) -> int:
    notification_key = "sales_order_commercial_terms_updated"
    roles = ["cobranzas"]
    try:
        order = get_delivery_order(order_id, include_events=False)
        title = f"Corrección comercial en {order.get('orderNumber') or order_id}"
        body = (
            f"{order.get('customerName') or '-'} - remito {order.get('remitoNumber') or '-'}. "
            f"Plazo: {correction.get('previousDeadline') or '-'} -> {correction.get('newDeadline') or '-'}. "
            f"Total: {correction.get('previousTotalLabel') or '-'} -> {correction.get('newTotalLabel') or '-'}. "
            "El RT de Bejerman no fue modificado. Revisar en NEXORA antes de facturar."
        )
        recipient_users = active_users_for_notification(notification_key, roles=roles, channel="any")
        recipients = [int(row["id"]) for row in recipient_users]
        if not recipient_users:
            create_event(
                order_id,
                actor_user_id,
                "notification_failed",
                metadata={"kind": "commercial_correction", "roles": roles, "reason": "no_active_recipients"},
            )
            return 0
        notification_dedupe_key = f"{notification_key}:sales_order:{order_id}:{uuid4()}"
        inserted = emit_notification(
            notification_key,
            user_ids=recipients,
            title=title,
            body=body,
            href=_billing_order_path(order_id),
            severity="warning",
            entity_type="delivery_order",
            entity_id=order_id,
            dedupe_key=notification_dedupe_key,
            payload={
                "orderId": order_id,
                "orderNumber": order.get("orderNumber"),
                "customerName": order.get("customerName"),
                "remitoNumber": order.get("remitoNumber"),
                "previousDeadline": correction.get("previousDeadline"),
                "newDeadline": correction.get("newDeadline"),
                "previousTotalLabel": correction.get("previousTotalLabel"),
                "newTotalLabel": correction.get("newTotalLabel"),
                "correctionScope": correction.get("correctionScope"),
                "bejermanRemitoModified": correction.get("bejermanRemitoModified"),
                "billingUrl": delivery_order_billing_url(order),
            },
            push=True,
        )
        email_users = active_users_for_notification(notification_key, roles=roles, channel="email", require_email=True)
        email_queued = _send_delivery_order_commercial_correction_email(
            order,
            email_users,
            correction,
            notification_dedupe_key,
        )
        create_event(
            order_id,
            actor_user_id,
            "notification_sent" if inserted else "notification_skipped",
            metadata={"kind": "commercial_correction", "roles": roles, "recipients": inserted, "emailQueued": email_queued},
        )
        return inserted
    except Exception:
        logger.exception("delivery_order_commercial_correction_notification_failed", extra={"order_id": order_id})
        try:
            create_event(
                order_id,
                actor_user_id,
                "notification_failed",
                metadata={"kind": "commercial_correction", "reason": "exception"},
            )
        except Exception:
            pass
        return 0


def _billing_suppressed(order_id: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM delivery_order_events
             WHERE order_id = %s
               AND event_type = 'delivery_order_not_billable'
             LIMIT 1
            """,
            [order_id],
        )
        return bool(cur.fetchone())


def _billing_postponement_due_at(return_date: Any) -> tuple[date, datetime]:
    raw_date = _clean_text(return_date)
    try:
        selected_date = date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        raise DeliveryOrderError("INVALID_POSTPONEMENT_DATE", "La fecha de postergación no es válida")
    if raw_date != selected_date.isoformat():
        raise DeliveryOrderError("INVALID_POSTPONEMENT_DATE", "La fecha de postergación debe tener formato AAAA-MM-DD")
    if selected_date <= timezone.localdate():
        raise DeliveryOrderError(
            "POSTPONEMENT_DATE_MUST_BE_FUTURE",
            "La fecha de postergación debe ser posterior a hoy.",
        )
    due_at = timezone.make_aware(datetime.combine(selected_date, time(hour=9)), timezone.get_current_timezone())
    return selected_date, due_at


def _assert_billing_postponable(order: dict[str, Any]) -> None:
    if (
        (order.get("remitos") and order.get("remitoSetStatus") != "complete")
        or
        not order_requires_billing(order)
        or _optional_text(order.get("invoiceNumber"))
        or order.get("status") in CLOSED_STATUSES
        or _billing_suppressed(str(order.get("id") or ""))
    ):
        raise DeliveryOrderError(
            "INVALID_BILLING_POSTPONEMENT_STATE",
            "Solo se puede posponer un remito facturable pendiente.",
            status_code=409,
        )


@transaction.atomic
def postpone_delivery_order_billing(order_id: str, actor_user_id: int | None, return_date: Any) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    _assert_billing_postponable(current)
    selected_date, due_at = _billing_postponement_due_at(return_date)
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
               SET billing_postponed_until = %s,
                   billing_postponed_by_user_id = %s,
                   billing_postponed_at = CURRENT_TIMESTAMP,
                   billing_postponement_notified_at = NULL
             WHERE id = %s
            """,
            [due_at, actor_user_id, order_id],
        )
    create_event(
        order_id,
        actor_user_id,
        "delivery_order_billing_postponed",
        metadata={"returnDate": selected_date.isoformat(), "returnAt": due_at.isoformat()},
    )
    return get_delivery_order(order_id)


@transaction.atomic
def reactivate_delivery_order_billing(order_id: str, actor_user_id: int | None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    _assert_billing_postponable(current)
    if not current.get("billingPostponedUntil"):
        raise DeliveryOrderError(
            "BILLING_POSTPONEMENT_NOT_FOUND",
            "El remito no está postergado.",
            status_code=409,
        )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
               SET billing_postponed_until = NULL,
                   billing_postponed_by_user_id = NULL,
                   billing_postponed_at = NULL,
                   billing_postponement_notified_at = NULL
             WHERE id = %s
            """,
            [order_id],
        )
    create_event(order_id, actor_user_id, "delivery_order_billing_reactivated")
    return get_delivery_order(order_id)


def process_due_delivery_order_billing_postponements(limit: int = 100) -> dict[str, int]:
    safe_limit = max(1, min(500, int(limit or 100)))
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, o.billing_postponed_until
                  FROM delivery_orders o
                  LEFT JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
                 WHERE o.billing_postponed_until IS NOT NULL
                   AND o.billing_postponed_until <= CURRENT_TIMESTAMP
                   AND o.billing_postponement_notified_at IS NULL
                   AND NULLIF(BTRIM(COALESCE(o.remito_number, '')), '') IS NOT NULL
                   AND NULLIF(BTRIM(COALESCE(o.invoice_number, '')), '') IS NULL
                   AND o.status NOT IN ('facturado', 'cancelado', 'entregado_no_facturable')
                   AND NOT EXISTS (
                     SELECT 1
                       FROM delivery_order_events e
                      WHERE e.order_id = o.id
                        AND e.event_type = 'delivery_order_not_billable'
                   )
                   AND (
                     COALESCE(g.response_summary ->> 'billingRequired', '') = 'true'
                     OR UPPER(SPLIT_PART(BTRIM(COALESCE(o.remito_number, '')), ' ', 1)) = ANY(%s)
                   )
                 ORDER BY o.billing_postponed_until ASC, o.id ASC
                 LIMIT %s
                 FOR UPDATE OF o SKIP LOCKED
                """,
                [sorted(BILLABLE_REMITO_TYPES), safe_limit],
            )
            due_rows = _rows(cur)

        recipients = active_users_for_notification(
            BILLING_POSTPONEMENT_NOTIFICATION_KEY,
            roles=["cobranzas"],
            channel="bell",
        )
        recipient_ids = [int(user["id"]) for user in recipients]
        inserted = 0
        for row in due_rows:
            order_id = str(row["id"])
            order = get_delivery_order(order_id, include_events=False)
            due_at = row["billing_postponed_until"]
            notification_key = f"{BILLING_POSTPONEMENT_NOTIFICATION_KEY}:{order_id}:{_iso(due_at)}"
            inserted_for_order = emit_notification(
                BILLING_POSTPONEMENT_NOTIFICATION_KEY,
                user_ids=recipient_ids,
                title=f"Remito pendiente para facturar {order.get('remitoNumber') or order.get('orderNumber') or order_id}",
                body=f"{order.get('customerName') or '-'}: la postergación venció y el remito volvió a Pendientes.",
                href=_billing_order_path(order_id),
                severity="warning",
                entity_type="delivery_order",
                entity_id=order_id,
                dedupe_key=notification_key,
                payload={
                    "orderId": order_id,
                    "orderNumber": order.get("orderNumber"),
                    "remitoNumber": order.get("remitoNumber"),
                    "customerName": order.get("customerName"),
                    "billingPostponedUntil": _iso(due_at),
                },
            )
            inserted += inserted_for_order
            with connection.cursor() as cur:
                cur.execute(
                    """
                    UPDATE delivery_orders
                       SET billing_postponement_notified_at = CURRENT_TIMESTAMP
                     WHERE id = %s
                       AND billing_postponed_until = %s
                       AND billing_postponement_notified_at IS NULL
                    """,
                    [order_id, due_at],
                )
            create_event(
                order_id,
                None,
                "delivery_order_billing_postponement_due",
                metadata={"returnAt": _iso(due_at), "recipients": inserted_for_order},
            )
    return {"due": len(due_rows), "notifications": inserted}


DELIVERY_ORDER_SORT_MAP = {
    "orden": "NULLIF(LOWER(BTRIM(COALESCE(o.order_number, ''))), '')",
    "cliente": "NULLIF(LOWER(BTRIM(COALESCE(o.customer_name, ''))), '')",
    "tipo": "NULLIF(LOWER(BTRIM(COALESCE(o.delivery_type, ''))), '')",
    "estado": "NULLIF(LOWER(BTRIM(COALESCE(o.status, ''))), '')",
    "articulos": """
        NULLIF(LOWER(COALESCE((
            SELECT COALESCE(
                NULLIF(BTRIM(oi.article_name), ''),
                NULLIF(BTRIM(oi.description), ''),
                NULLIF(BTRIM(oi.article_code), '')
            )
              FROM delivery_order_items oi
             WHERE oi.order_id = o.id
             ORDER BY oi.sort_order ASC, oi.created_at ASC
             LIMIT 1
        ), '')), '')
    """,
    "comercial": """
        NULLIF(LOWER(COALESCE(
            NULLIF(BTRIM(o.commercial_terms), ''),
            NULLIF(BTRIM(o.commercial_condition), ''),
            NULLIF(BTRIM(o.commercial_deadline), ''),
            NULLIF(BTRIM(o.commercial_price), ''),
            ''
        )), '')
    """,
    "remito": "NULLIF(LOWER(BTRIM(COALESCE(o.remito_number, ''))), '')",
    "factura": "NULLIF(LOWER(BTRIM(COALESCE(o.invoice_number, ''))), '')",
    "fecha": "COALESCE(o.order_date::timestamptz, o.created_at)",
}


def delivery_order_order_sql(sort_raw: Any, pending_billing_requested: bool = False) -> str:
    raw = _optional_text(sort_raw) or ""
    sort_field = raw[1:] if raw.startswith("-") else raw
    sort_expression = DELIVERY_ORDER_SORT_MAP.get(sort_field)
    if sort_expression:
        direction = "DESC" if raw.startswith("-") else "ASC"
        return f"{sort_expression} {direction} NULLS LAST, o.id {direction}"
    if pending_billing_requested:
        return """
          COALESCE(g.generated_at, o.order_date::timestamptz, g.created_at, o.delivered_at, o.created_at) ASC,
          o.order_date ASC NULLS LAST,
          o.created_at ASC,
          o.id ASC
        """
    return """
          o.created_at DESC,
          o.id DESC
        """


def list_delivery_orders(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    where = []
    params: list[Any] = []

    statuses = [s.strip() for s in _clean_text(filters.get("status")).split(",") if s.strip()]
    normalized_statuses: list[str] = []
    if statuses:
        normalized_statuses = [normalize_status(s) for s in statuses]
        where.append("o.status = ANY(%s)")
        params.append(normalized_statuses)
    elif str(filters.get("include_cancelled") or filters.get("includeCancelled") or "").lower() not in ("1", "true", "yes"):
        where.append("o.status <> 'cancelado'")

    delivery_type = _optional_text(filters.get("delivery_type") or filters.get("deliveryType"))
    if delivery_type:
        where.append("o.delivery_type = %s")
        params.append(normalize_delivery_type(delivery_type))

    customer_id = _optional_int(filters.get("customer_id") or filters.get("customerId"))
    if customer_id:
        where.append("o.customer_id = %s")
        params.append(customer_id)

    customer_code = _optional_text(filters.get("customer_code") or filters.get("bejermanCustomerCode"))
    if customer_code:
        where.append("o.bejerman_customer_code = %s")
        params.append(customer_code)

    remito_location = _optional_text(filters.get("remito_location") or filters.get("remitoLocation"))
    if remito_location:
        if remito_location not in REMITO_LOCATIONS:
            raise DeliveryOrderError("INVALID_REMITO_LOCATION", "Ubicación de remito inválida")
        where.append("o.remito_location = %s")
        params.append(remito_location)

    q = _optional_text(filters.get("q"))
    if q:
        like = f"%{q}%"
        order_search_expressions = [
            "o.order_number",
            "o.customer_name",
            "o.bejerman_customer_code",
            "o.company_key",
            "o.raw_pedido",
            "o.remito_number",
            "o.invoice_number",
            "o.equipment_model",
            "o.equipment_serial",
            "o.equipment_internal_number",
            "o.source_system",
            "o.source_external_id",
            "o.source_reference",
            "CASE WHEN o.ingreso_id IS NOT NULL THEN CONCAT('OS-', LPAD(CAST(o.ingreso_id AS TEXT), 5, '0')) ELSE '' END",
            "o.source_company_id",
            "o.source_sheet",
            "CAST(o.source_row AS TEXT)",
            "o.source_color",
            "o.seller_name",
            "o.seller_code",
            "o.operation_company_label",
            "o.commercial_terms",
            "o.commercial_price",
            "o.commercial_exchange_rate",
            "o.price_currency",
            "o.commercial_condition",
            "o.commercial_deadline",
        ]
        item_search_expressions = [
            "CAST(oi.ingreso_id AS TEXT)",
            "CASE WHEN oi.ingreso_id IS NOT NULL THEN CONCAT('OS-', LPAD(CAST(oi.ingreso_id AS TEXT), 5, '0')) ELSE '' END",
            "oi.article_code",
            "oi.article_name",
            "oi.description",
            "oi.source_text",
            "oi.partida",
            "oi.stock_deposit_code",
            "oi.price_currency",
        ]
        order_search_sql = " OR ".join(f"COALESCE({expr}, '') ILIKE %s" for expr in order_search_expressions)
        item_search_sql = " OR ".join(f"COALESCE({expr}, '') ILIKE %s" for expr in item_search_expressions)
        where.append(
            f"""
            (
              {order_search_sql}
              OR EXISTS (
                SELECT 1
                  FROM delivery_order_items oi
                 WHERE oi.order_id = o.id
                   AND ({item_search_sql})
              )
            )
            """
        )
        params.extend([like] * (len(order_search_expressions) + len(item_search_expressions)))

    pending_billing_filter = str(filters.get("pending_billing") or filters.get("pendingBilling") or "").lower() in ("1", "true", "yes")
    postponed_billing_filter = str(filters.get("postponed_billing") or filters.get("postponedBilling") or "").lower() in ("1", "true", "yes")
    if pending_billing_filter and postponed_billing_filter:
        raise DeliveryOrderError("INVALID_BILLING_FILTER", "No se pueden consultar pendientes y pospuestos al mismo tiempo.")
    pending_billing_requested = normalized_statuses == ["entregado_pendiente_facturacion"] or pending_billing_filter or postponed_billing_filter
    if pending_billing_filter or postponed_billing_filter:
        where.append("NULLIF(BTRIM(COALESCE(o.remito_number, '')), '') IS NOT NULL")
        where.append(
            """
            NOT EXISTS (
              SELECT 1
                FROM delivery_order_remito_links rl
                JOIN bejerman_remito_groups rg ON rg.id = rl.remito_group_id
               WHERE rl.order_id = o.id
                 AND rl.batch_id = (
                   SELECT latest_rl.batch_id
                     FROM delivery_order_remito_links latest_rl
                    WHERE latest_rl.order_id = o.id
                    ORDER BY latest_rl.created_at DESC, latest_rl.id DESC
                    LIMIT 1
                 )
               GROUP BY rl.batch_id
              HAVING COUNT(DISTINCT rl.sequence) FILTER (WHERE rg.status IN ('generated', 'reconciled'))
                       < MAX(rl.expected_count)
                  OR BOOL_OR(rg.status NOT IN ('generated', 'reconciled'))
            )
            """
        )
        where.append("o.status NOT IN ('facturado', 'cancelado', 'entregado_no_facturable')")
        where.append(
            """
            NOT EXISTS (
              SELECT 1
                FROM delivery_order_events e
               WHERE e.order_id = o.id
                 AND e.event_type = 'delivery_order_not_billable'
            )
            """
        )
        where.append(
            """
            (
              COALESCE(g.response_summary ->> 'billingRequired', '') = 'true'
              OR UPPER(SPLIT_PART(BTRIM(COALESCE(o.remito_number, '')), ' ', 1)) = ANY(%s)
            )
            """
        )
        params.append(sorted(BILLABLE_REMITO_TYPES))
        if pending_billing_filter:
            where.append("(o.billing_postponed_until IS NULL OR o.billing_postponed_until <= CURRENT_TIMESTAMP)")
        else:
            where.append("o.billing_postponed_until > CURRENT_TIMESTAMP")
    if pending_billing_requested:
        where.append("NULLIF(BTRIM(COALESCE(o.invoice_number, '')), '') IS NULL")

    limit = max(1, min(200, int(filters.get("limit") or 80)))
    offset = max(0, int(filters.get("offset") or 0))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_sql = delivery_order_order_sql(filters.get("sort"), pending_billing_requested)
    join_sql = "LEFT JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id" if pending_billing_requested else ""

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM delivery_orders o
            {join_sql}
            {where_sql}
            """,
            params,
        )
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT o.*
            FROM delivery_orders o
            {join_sql}
            {where_sql}
            ORDER BY
              {order_sql}
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        rows = _rows(cur)

    ids = [row["id"] for row in rows]
    items_by_order = load_items_by_order(ids)
    remitos_by_order = load_delivery_order_remitos(ids)
    remito_groups_by_id = load_remito_groups_by_id([row.get("bejerman_remito_group_id") for row in rows])
    retryable_ids = [
        row["id"]
        for row in rows
        if not _optional_text(row.get("remito_number")) and not row.get("bejerman_remito_group_id")
    ]
    last_failures_by_order = load_last_remito_failures_by_order(retryable_ids)
    owner_by_ingreso = load_owner_fields_by_ingreso([row.get("ingreso_id") for row in rows])
    out = []
    for row in rows:
        row["items"] = items_by_order.get(row["id"], [])
        row["remito_links"] = remitos_by_order.get(row["id"], [])
        row["bejerman_remito_group"] = remito_groups_by_id.get(row.get("bejerman_remito_group_id"))
        row["last_remito_failure"] = last_failures_by_order.get(row["id"])
        _apply_owner_fields(row, owner_by_ingreso)
        out.append(serialize_order(row))
    return {"items": out, "total": total, "limit": limit, "offset": offset}


def load_items_by_order(order_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not order_ids:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM delivery_order_items
            WHERE order_id = ANY(%s)
            ORDER BY order_id, sort_order, created_at
            """,
            [order_ids],
        )
        item_rows = _rows(cur)
        item_ids = [row["id"] for row in item_rows]
        partidas_by_item: dict[str, list[dict[str, Any]]] = {}
        if item_ids:
            cur.execute(
                """
                SELECT *
                FROM delivery_order_item_partidas
                WHERE order_item_id = ANY(%s)
                ORDER BY order_item_id, sort_order, created_at
                """,
                [item_ids],
            )
            for partida in _rows(cur):
                partidas_by_item.setdefault(partida["order_item_id"], []).append(_serialize_partida(partida))

    out: dict[str, list[dict[str, Any]]] = {}
    for row in item_rows:
        row["partidas"] = partidas_by_item.get(row["id"], [])
        out.setdefault(row["order_id"], []).append(_serialize_item(row))
    return out


def load_owner_fields_by_ingreso(ingreso_ids: list[int]) -> dict[int, dict[str, str]]:
    """Datos del propietario ocasional que capturo el ingreso. Son los que la emision usa
    como Cliente_* cuando la orden no tiene codigo Bejerman (particulares), asi que viajan
    tanto en el detalle como en el listado para que ambos evaluen la misma condicion."""
    unique_ids = sorted({int(ingreso_id) for ingreso_id in ingreso_ids if ingreso_id})
    if not unique_ids or not _has_table_column("ingresos", "propietario_doc"):
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id,
                   COALESCE(propietario_nombre, '') AS propietario_nombre,
                   COALESCE(propietario_contacto, '') AS propietario_contacto,
                   COALESCE(propietario_doc, '') AS propietario_doc
              FROM ingresos
             WHERE id = ANY(%s)
            """,
            [unique_ids],
        )
        return {row["id"]: row for row in _rows(cur)}


def _apply_owner_fields(row: dict[str, Any], owner_by_ingreso: dict[int, dict[str, str]]) -> None:
    owner = owner_by_ingreso.get(row.get("ingreso_id")) or {}
    row["propietario_nombre"] = owner.get("propietario_nombre") or ""
    row["propietario_contacto"] = owner.get("propietario_contacto") or ""
    row["propietario_doc"] = owner.get("propietario_doc") or ""


def _refresh_bejerman_customer_code(row: dict[str, Any]) -> None:
    """`delivery_orders.bejerman_customer_code` es una foto tomada al crear la
    orden; si el cliente se sincroniza/corrige en Bejerman despues, esa foto
    queda vieja y la emision del remito sigue fallando con
    BEJERMAN_CUSTOMER_NOT_FOUND aunque el cliente ya este bien mapeado. Ac'a se
    prefiere siempre el codigo vivo de customers.bejerman_cod_empresa por sobre
    la foto guardada, cuando la empresa Bejerman coincide."""
    customer_id = row.get("customer_id")
    if not customer_id:
        return
    with connection.cursor() as cur:
        cur.execute(
            "SELECT razon_social, bejerman_cod_empresa, bejerman_company_key FROM customers WHERE id = %s",
            [customer_id],
        )
        customer = _one(cur)
    if not customer:
        return
    if is_occasional_customer_name(customer.get("razon_social")):
        # Particular: se emite como cliente ocasional, cualquier codigo guardado en la foto (o
        # cargado a mano en customers) no existe en Bejerman y romperia la emision.
        row["bejerman_customer_code"] = ""
        return
    live_code = _clean_text(customer.get("bejerman_cod_empresa"))
    if not live_code:
        return
    live_company_key = _clean_text(customer.get("bejerman_company_key")).upper()
    order_company_key = _clean_text(row.get("company_key")).upper()
    if live_company_key and order_company_key and live_company_key != order_company_key:
        return
    if row.get("bejerman_customer_code") != live_code:
        row["bejerman_customer_code"] = live_code


def get_delivery_order(
    order_id: str,
    *,
    include_events: bool = True,
    for_update: bool = False,
    actor_user_id: int | None = None,
    refresh_article_flags: bool = False,
    refresh_article_flags_allow_fetch: bool = True,
) -> dict[str, Any]:
    suffix = "FOR UPDATE" if for_update else ""
    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM delivery_orders WHERE id = %s {suffix}", [order_id])
        row = _one(cur)
        if not row:
            raise DeliveryOrderError("DELIVERY_ORDER_NOT_FOUND", "Orden de entrega no encontrada", status_code=404)
    _refresh_bejerman_customer_code(row)

    items_by_order = load_items_by_order([order_id])
    row["items"] = items_by_order.get(order_id, [])
    row["remito_links"] = load_delivery_order_remitos([order_id]).get(order_id, [])
    row["bejerman_remito_group"] = load_remito_groups_by_id([row.get("bejerman_remito_group_id")]).get(row.get("bejerman_remito_group_id"))
    if not _optional_text(row.get("remito_number")) and not row.get("bejerman_remito_group_id"):
        row["last_remito_failure"] = load_last_remito_failures_by_order([order_id]).get(order_id)
    if row.get("ingreso_id"):
        _apply_owner_fields(row, load_owner_fields_by_ingreso([row["ingreso_id"]]))
    if include_events:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, actor_user_id, source_actor_user_id, event_type, note, metadata, created_at
                FROM delivery_order_events
                WHERE order_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                [order_id],
            )
            row["events"] = [
                {
                    "id": item.get("id"),
                    "actorUserId": item.get("actor_user_id"),
                    "sourceActorUserId": item.get("source_actor_user_id") or "",
                    "eventType": item.get("event_type"),
                    "note": item.get("note") or "",
                    "metadata": _decode_json(item.get("metadata"), {}),
                    "createdAt": _iso(item.get("created_at")),
                }
                for item in _rows(cur)
            ]
    order = serialize_order(row)
    if refresh_article_flags:
        order = refresh_delivery_order_article_partida_flags(
            order,
            actor_user_id,
            strict=False,
            allow_remote_fetch=refresh_article_flags_allow_fetch,
        )
    return order


@transaction.atomic
def create_delivery_order(payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    delivery_type = normalize_delivery_type(payload.get("deliveryType") or payload.get("delivery_type"))
    company_key = normalize_company_key(payload.get("companyKey") or payload.get("company_key"))
    status = normalize_status(payload.get("status") or "pendiente_armado")
    priority = normalize_priority(payload.get("priority"))
    customer_id = _optional_int(payload.get("customerId") or payload.get("customer_id"))
    customer_name = _clean_text(payload.get("customerName") or payload.get("customer_name"))
    bejerman_customer_code = _optional_text(payload.get("bejermanCustomerCode") or payload.get("bejerman_customer_code"))

    if customer_id and (not customer_name or not bejerman_customer_code):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT razon_social, cod_empresa FROM customers WHERE id = %s",
                [customer_id],
            )
            customer = _one(cur) or {}
        customer_name = customer_name or customer.get("razon_social") or ""
        bejerman_customer_code = bejerman_customer_code or customer.get("cod_empresa")

    if not customer_name:
        raise DeliveryOrderError("CUSTOMER_REQUIRED", "La orden necesita un cliente")
    bejerman_customer_code = bejerman_customer_code_for(customer_name, bejerman_customer_code)

    order_id = _optional_text(payload.get("id")) or f"do-{uuid4()}"
    ingreso_id = _optional_int(payload.get("ingresoId") or payload.get("ingreso_id"))
    device_id = _optional_int(payload.get("deviceId") or payload.get("device_id"))
    payload_order_number = _optional_text(payload.get("orderNumber") or payload.get("order_number"))
    source_reference_input = _optional_text(payload.get("sourceReference") or payload.get("source_reference"))
    order_number = (
        order_number_for_now(delivery_type, ingreso_id, source_reference_input or payload_order_number)
        if delivery_type == "service_release"
        else payload_order_number or order_number_for_now(delivery_type)
    )
    remito_number = _optional_text(payload.get("remitoNumber") or payload.get("remito_number"))
    invoice_number = _optional_text(payload.get("invoiceNumber"))
    equipment_serial = _optional_text(payload.get("equipmentSerial") or payload.get("equipment_serial"))
    price_currency = _price_currency(payload.get("priceCurrency") or payload.get("price_currency"))
    if remito_number and status not in {"facturado", "cancelado"}:
        status = remito_status(remito_number)

    items = _normalize_order_items(payload.get("items") or [], equipment_serial, default_price_currency=price_currency)
    _assert_item_discounts_allowed(items, actor_user_id)
    rental_header: dict[str, Any] = {}
    if delivery_type == "rental":
        rental_contexts = validate_rental_delivery_items(items, order_id=order_id)
        items = resolve_present_rental_item_partidas(
            items,
            order_id=order_id,
            company_key=company_key,
            actor_user_id=actor_user_id,
        )
        rental_header = rental_header_from_contexts(rental_contexts)
        equipment_serial = rental_header.get("equipmentSerial")
    else:
        _validate_delivery_partidas_stock(items, company_key, delivery_type, actor_user_id)
    payload_has_seller_code = "sellerCode" in payload or "seller_code" in payload
    payload_seller_code = payload.get("sellerCode") if "sellerCode" in payload else payload.get("seller_code")
    seller_name, seller_code = seller_fields_for_delivery_order(
        delivery_type,
        payload.get("sellerName") or payload.get("seller_name"),
        payload_seller_code,
        actor_user_id,
        use_actor_default=not payload_has_seller_code,
    )

    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO delivery_orders (
              id, order_number, customer_id, bejerman_customer_code, customer_name,
              delivery_type, company_key, source_system, source_external_id, source_reference, source_company_id,
              source_sheet, source_row, source_color,
              ingreso_id, device_id, equipment_model, equipment_serial, equipment_internal_number,
              seller_name, seller_code, order_date, operation_company_label, raw_pedido,
              commercial_terms, commercial_price, commercial_exchange_rate, price_currency, commercial_condition, commercial_deadline,
              status, priority, remito_number, remito_location, invoice_number,
              created_by_user_id, prepared_at, delivered_at, invoiced_at
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s,
              %s, %s, %s,
              %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s,
              CASE WHEN %s IN ('armado_pendiente_entrega','entregado_pendiente_facturacion','entregado_no_facturable','facturado') THEN CURRENT_TIMESTAMP ELSE NULL END,
              CASE WHEN %s IN ('entregado_pendiente_facturacion','entregado_no_facturable','facturado') THEN CURRENT_TIMESTAMP ELSE NULL END,
              CASE WHEN %s = 'facturado' THEN CURRENT_TIMESTAMP ELSE NULL END
            )
            """,
            [
                order_id,
                order_number,
                customer_id,
                bejerman_customer_code,
                customer_name,
                delivery_type,
                company_key,
                _optional_text(payload.get("sourceSystem")) or "nexora",
                _optional_text(payload.get("sourceExternalId")),
                delivery_source_reference(
                    source_reference_input or rental_header.get("sourceReference"),
                    delivery_type,
                    rental_header.get("ingresoId") if delivery_type == "rental" else ingreso_id,
                ),
                _optional_text(payload.get("sourceCompanyId")),
                _optional_text(payload.get("sourceSheet")),
                _optional_int(payload.get("sourceRow")),
                _optional_text(payload.get("sourceColor")),
                rental_header.get("ingresoId") if delivery_type == "rental" else ingreso_id,
                rental_header.get("deviceId") if delivery_type == "rental" else device_id,
                rental_header.get("equipmentModel") if delivery_type == "rental" else _optional_text(payload.get("equipmentModel")),
                equipment_serial,
                rental_header.get("equipmentInternalNumber") if delivery_type == "rental" else _optional_text(payload.get("equipmentInternalNumber")),
                seller_name,
                seller_code,
                payload.get("orderDate") or date.today().isoformat(),
                _clean_text(payload.get("operationCompanyLabel")),
                _clean_text(payload.get("rawPedido")),
                _optional_text(payload.get("commercialTerms")),
                _optional_text(payload.get("commercialPrice")),
                _optional_text(payload.get("commercialExchangeRate")),
                price_currency,
                _optional_text(payload.get("commercialCondition")),
                _optional_text(payload.get("commercialDeadline")),
                status,
                priority,
                remito_number,
                "recepcion" if remito_number else _optional_text(payload.get("remitoLocation")),
                invoice_number,
                actor_user_id,
                status,
                status,
                status,
            ],
        )
        for item in items:
            item_id = item["id"] or f"doi-{uuid4()}"
            cur.execute(
                """
                INSERT INTO delivery_order_items (
                  id, order_id, ingreso_id, device_id, article_code, article_name, article_requires_partida, description, quantity, unit_price, price_currency, discount_percent,
                  source_text, partida, partida_expiration_date, stock_deposit_code,
                  stock_available_quantity, stock_checked_at, sort_order
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    item_id,
                    order_id,
                    item.get("ingresoId"),
                    item.get("deviceId"),
                    _optional_text(item.get("articleCode")),
                    _optional_text(item.get("articleName")),
                    item["articleRequiresPartida"],
                    item["description"],
                    item["quantity"],
                    item["unitPrice"],
                    item["priceCurrency"],
                    item["discountPercent"],
                    _optional_text(item.get("sourceText")),
                    item["partida"],
                    item["partidaExpirationDate"],
                    _optional_text(item.get("stockDepositCode")),
                    item["stockAvailableQuantity"],
                    item["stockCheckedAt"],
                    item["sortOrder"],
                ],
            )
            insert_item_partidas(cur, item_id, item["partidas"])

    if invoice_number:
        _sync_service_release_ingreso_invoice(
            {
                "delivery_type": delivery_type,
                "ingreso_id": rental_header.get("ingresoId") if delivery_type == "rental" else ingreso_id,
                "source_system": _optional_text(payload.get("sourceSystem")) or "nexora",
                "source_external_id": _optional_text(payload.get("sourceExternalId")),
            },
            invoice_number,
        )

    create_event(order_id, actor_user_id, "delivery_order_created", metadata={"status": status})
    notify_delivery_order_created(order_id, actor_user_id)
    if remito_number and status == "entregado_pendiente_facturacion":
        notify_delivery_order_remito_ready(order_id, actor_user_id)
    return get_delivery_order(order_id)


def _assert_order_editable(order: dict[str, Any]) -> None:
    if (
        order.get("status") not in EDITABLE_STATUSES
        or _optional_text(order.get("remitoNumber"))
        or _optional_text(order.get("bejermanRemitoGroupId"))
    ):
        raise DeliveryOrderError(
            "DELIVERY_ORDER_LOCKED",
            "No se puede editar una orden cerrada, con remito o con emisión Bejerman",
            status_code=409,
        )


def _review_group_for_order_correction(order_id: str) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, response_summary
              FROM bejerman_remito_groups
             WHERE status = 'failed_review'
               AND order_ids @> %s::jsonb
               AND COALESCE(response_summary->>'reviewState', '') IN ('ready_for_correction', 'ready_to_reissue')
             ORDER BY created_at DESC
             LIMIT 1
             FOR UPDATE
            """,
            [_json_param([order_id])],
        )
        row = cur.fetchone()
    if not row:
        return None
    summary = row[1] if isinstance(row[1], dict) else {}
    return {"id": str(row[0]), "responseSummary": summary}


def _review_correction_snapshot(order: dict[str, Any]) -> dict[str, Any]:
    items = []
    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "id": _optional_text(item.get("id")),
                "article": _optional_text(item.get("articleCode")),
                "quantity": str(item.get("quantity") or ""),
                "unitPrice": str(item.get("unitPrice") or ""),
                "currency": _optional_text(item.get("priceCurrency")),
                "discount": str(item.get("discountPercent") or ""),
                "partida": _optional_text(item.get("partida")),
                "deposit": _optional_text(item.get("stockDepositCode")),
                "partidas": [
                    {
                        "partida": _optional_text(partida.get("partida")),
                        "quantity": str(partida.get("assignedQuantity") or ""),
                        "deposit": _optional_text(partida.get("stockDepositCode")),
                    }
                    for partida in item.get("partidas") or []
                    if isinstance(partida, dict)
                ],
            }
        )
    return {
        "customer": {
            "id": order.get("customerId"),
            "code": _optional_text(order.get("bejermanCustomerCode")),
            "name": _optional_text(order.get("customerName")),
        },
        "company": _optional_text(order.get("companyKey")),
        "seller": _optional_text(order.get("sellerCode")),
        "commercial": {
            "currency": _optional_text(order.get("priceCurrency")),
            "exchangeRate": _optional_text(order.get("commercialExchangeRate")),
            "condition": _optional_text(order.get("commercialCondition")),
            "deadline": _optional_text(order.get("commercialDeadline")),
        },
        "items": items,
    }


def _review_correction_signature(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _review_correction_categories(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    categories = []
    for key, label in (("customer", "cliente"), ("company", "empresa"), ("seller", "vendedor"), ("commercial", "condiciones_comerciales")):
        if before.get(key) != after.get(key):
            categories.append(label)
    before_items = before.get("items") or []
    after_items = after.get("items") or []
    if [item.get("article") for item in before_items] != [item.get("article") for item in after_items]:
        categories.append("artículos")
    if [item.get("quantity") for item in before_items] != [item.get("quantity") for item in after_items]:
        categories.append("cantidades")
    price_keys = ("unitPrice", "currency", "discount")
    if [[item.get(key) for key in price_keys] for item in before_items] != [[item.get(key) for key in price_keys] for item in after_items]:
        categories.append("precios")
    stock_keys = ("partida", "deposit", "partidas")
    if [[item.get(key) for key in stock_keys] for item in before_items] != [[item.get(key) for key in stock_keys] for item in after_items]:
        categories.append("partidas_y_depósitos")
    return categories


def _record_review_order_correction(
    review_group: dict[str, Any],
    order_id: str,
    actor_user_id: int | None,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    group_id = review_group["id"]
    summary = review_group.get("responseSummary") if isinstance(review_group.get("responseSummary"), dict) else {}
    previous_snapshot = _review_correction_snapshot(before)
    new_snapshot = _review_correction_snapshot(after)
    previous_signature = _review_correction_signature(previous_snapshot)
    new_signature = _review_correction_signature(new_snapshot)
    categories = _review_correction_categories(previous_snapshot, new_snapshot)
    corrections = summary.get("orderCorrections") if isinstance(summary.get("orderCorrections"), dict) else {}
    corrections = {
        **corrections,
        order_id: {
            "savedAt": timezone.now().isoformat(),
            "actorUserId": actor_user_id,
            "previousSignature": previous_signature,
            "newSignature": new_signature,
            "categories": categories,
        },
    }
    patch = {
        "reviewState": "ready_to_reissue",
        "operatorCorrectionRecorded": True,
        "correctionSavedAt": timezone.now().isoformat(),
        "hasPayloadChanges": None,
        "orderCorrections": corrections,
    }
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_remito_groups
               SET response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
             WHERE id = %s AND status = 'failed_review'
            """,
            [_json_param(patch), group_id],
        )
    create_event(
        order_id,
        actor_user_id,
        "bejerman_remito_correction_saved",
        metadata={
            "previousGroupId": group_id,
            "previousSignature": previous_signature,
            "newSignature": new_signature,
            "categories": categories,
        },
    )


@transaction.atomic
def update_delivery_order(order_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    _assert_order_editable(current)
    review_group = _review_group_for_order_correction(order_id)

    delivery_type = normalize_delivery_type(payload.get("deliveryType") or current.get("deliveryType"))
    if "status" in payload or "estado" in payload:
        next_status = normalize_status(payload.get("status") if "status" in payload else payload.get("estado"))
    else:
        next_status = current.get("status") or "pendiente_armado"
    if next_status != current.get("status") and (
        next_status not in MANUAL_PLANNING_STATUSES or current.get("status") not in MANUAL_PLANNING_STATUSES
    ):
        raise DeliveryOrderError(
            "INVALID_DELIVERY_ORDER_STATUS_CHANGE",
            "Solo se puede cambiar manualmente entre pendiente de stock y pendiente de armado",
            status_code=409,
        )
    company_key = normalize_company_key(
        payload.get("companyKey") if "companyKey" in payload else current.get("companyKey") or "SEPID"
    )
    price_currency = _price_currency(
        payload.get("priceCurrency")
        if "priceCurrency" in payload
        else payload.get("price_currency") if "price_currency" in payload else current.get("priceCurrency")
    )
    priority = normalize_priority(payload.get("priority") or current.get("priority"))
    customer_id = _optional_int(payload.get("customerId") if "customerId" in payload else current.get("customerId"))
    customer_name = _clean_text(payload.get("customerName") if "customerName" in payload else current.get("customerName"))
    bejerman_customer_code = _optional_text(
        payload.get("bejermanCustomerCode") if "bejermanCustomerCode" in payload else current.get("bejermanCustomerCode")
    )

    if customer_id and (not customer_name or not bejerman_customer_code):
        with connection.cursor() as cur:
            cur.execute("SELECT razon_social, cod_empresa FROM customers WHERE id = %s", [customer_id])
            customer = _one(cur) or {}
        customer_name = customer_name or customer.get("razon_social") or ""
        bejerman_customer_code = bejerman_customer_code or customer.get("cod_empresa")
    if not customer_name:
        raise DeliveryOrderError("CUSTOMER_REQUIRED", "La orden necesita un cliente")
    bejerman_customer_code = bejerman_customer_code_for(customer_name, bejerman_customer_code)

    existing_items_by_id = {str(item.get("id")): item for item in (current.get("items") or []) if item.get("id")}
    items = _normalize_order_items(
        payload.get("items") or [],
        _optional_text(current.get("equipmentSerial")),
        existing_items_by_id,
        default_price_currency=price_currency,
    )
    _assert_item_discounts_allowed(items, actor_user_id, existing_items_by_id)
    rental_header: dict[str, Any] = {}
    if delivery_type == "rental":
        rental_contexts = validate_rental_delivery_items(items, order_id=order_id)
        items = resolve_present_rental_item_partidas(
            items,
            order_id=order_id,
            company_key=company_key,
            actor_user_id=actor_user_id,
        )
        rental_header = rental_header_from_contexts(rental_contexts)
    else:
        _validate_delivery_partidas_stock(items, company_key, delivery_type, actor_user_id)
    payload_seller_code = payload.get("sellerCode") if "sellerCode" in payload else current.get("sellerCode")
    seller_name, seller_code = seller_fields_for_delivery_order(
        delivery_type,
        payload.get("sellerName") if "sellerName" in payload else current.get("sellerName"),
        payload_seller_code,
        actor_user_id,
        use_actor_default=False,
    )
    existing_item_ids = set(existing_items_by_id)
    seen_item_ids: set[str] = set()

    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
               SET customer_id = %s,
                   bejerman_customer_code = %s,
                   customer_name = %s,
                   delivery_type = %s,
                   company_key = %s,
                   source_reference = %s,
                   ingreso_id = %s,
                   device_id = %s,
                   equipment_model = %s,
                   equipment_serial = %s,
                   equipment_internal_number = %s,
                   seller_name = %s,
                   seller_code = %s,
                   order_date = %s,
                   operation_company_label = %s,
                   raw_pedido = %s,
                   commercial_terms = %s,
                   commercial_price = %s,
                   commercial_exchange_rate = %s,
                   price_currency = %s,
                   commercial_condition = %s,
                   commercial_deadline = %s,
                   priority = %s,
                   status = %s
             WHERE id = %s
            """,
            [
                customer_id,
                bejerman_customer_code,
                customer_name,
                delivery_type,
                company_key,
                delivery_source_reference(
                    rental_header.get("sourceReference") if delivery_type == "rental" else current.get("sourceReference"),
                    delivery_type,
                    rental_header.get("ingresoId") if delivery_type == "rental" else current.get("ingresoId"),
                ),
                rental_header.get("ingresoId") if delivery_type == "rental" else current.get("ingresoId"),
                rental_header.get("deviceId") if delivery_type == "rental" else current.get("deviceId"),
                rental_header.get("equipmentModel") if delivery_type == "rental" else current.get("equipmentModel"),
                rental_header.get("equipmentSerial") if delivery_type == "rental" else current.get("equipmentSerial"),
                rental_header.get("equipmentInternalNumber") if delivery_type == "rental" else current.get("equipmentInternalNumber"),
                seller_name,
                seller_code,
                payload.get("orderDate") or current.get("orderDate") or date.today().isoformat(),
                _clean_text(
                    payload.get("operationCompanyLabel")
                    if "operationCompanyLabel" in payload
                    else current.get("operationCompanyLabel")
                ),
                _clean_text(payload.get("rawPedido") if "rawPedido" in payload else current.get("rawPedido")),
                _optional_text(payload.get("commercialTerms") if "commercialTerms" in payload else current.get("commercialTerms")),
                _optional_text(payload.get("commercialPrice") if "commercialPrice" in payload else current.get("commercialPrice")),
                _optional_text(
                    payload.get("commercialExchangeRate")
                    if "commercialExchangeRate" in payload
                    else current.get("commercialExchangeRate")
                ),
                price_currency,
                _optional_text(
                    payload.get("commercialCondition")
                    if "commercialCondition" in payload
                    else current.get("commercialCondition")
                ),
                _optional_text(payload.get("commercialDeadline") if "commercialDeadline" in payload else current.get("commercialDeadline")),
                priority,
                next_status,
                order_id,
            ],
        )

        for item in items:
            item_id = item["id"]
            if item_id:
                if item_id not in existing_item_ids:
                    raise DeliveryOrderError("DELIVERY_ORDER_ITEM_NOT_FOUND", "Ítem de orden no encontrado", status_code=404)
                seen_item_ids.add(item_id)
                cur.execute(
                    """
                    UPDATE delivery_order_items
                       SET ingreso_id = %s,
                           device_id = %s,
                           article_code = %s,
                           article_name = %s,
                           article_requires_partida = %s,
                           description = %s,
                           quantity = %s,
                           unit_price = %s,
                           price_currency = %s,
                           discount_percent = %s,
                           source_text = %s,
                           partida = %s,
                           partida_expiration_date = %s,
                           stock_deposit_code = %s,
                           stock_available_quantity = %s,
                           stock_checked_at = %s,
                           sort_order = %s
                     WHERE id = %s AND order_id = %s
                    """,
                    [
                        item["ingresoId"],
                        item["deviceId"],
                        item["articleCode"],
                        item["articleName"],
                        item["articleRequiresPartida"],
                        item["description"],
                        item["quantity"],
                        item["unitPrice"],
                        item["priceCurrency"],
                        item["discountPercent"],
                        item["sourceText"],
                        item["partida"],
                        item["partidaExpirationDate"],
                        item["stockDepositCode"],
                        item["stockAvailableQuantity"],
                        item["stockCheckedAt"],
                        item["sortOrder"],
                        item_id,
                        order_id,
                    ],
                )
                cur.execute("DELETE FROM delivery_order_item_partidas WHERE order_item_id = %s", [item_id])
            else:
                item_id = f"doi-{uuid4()}"
                seen_item_ids.add(item_id)
                cur.execute(
                    """
                    INSERT INTO delivery_order_items (
                      id, order_id, ingreso_id, device_id, article_code, article_name, article_requires_partida, description, quantity, unit_price, price_currency, discount_percent,
                      source_text, partida, partida_expiration_date, stock_deposit_code,
                      stock_available_quantity, stock_checked_at, sort_order
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        item_id,
                        order_id,
                        item["ingresoId"],
                        item["deviceId"],
                        item["articleCode"],
                        item["articleName"],
                        item["articleRequiresPartida"],
                        item["description"],
                        item["quantity"],
                        item["unitPrice"],
                        item["priceCurrency"],
                        item["discountPercent"],
                        item["sourceText"],
                        item["partida"],
                        item["partidaExpirationDate"],
                        item["stockDepositCode"],
                        item["stockAvailableQuantity"],
                        item["stockCheckedAt"],
                        item["sortOrder"],
                    ],
                )
            insert_item_partidas(cur, item_id, item["partidas"])

        delete_ids = sorted(existing_item_ids - seen_item_ids)
        if delete_ids:
            cur.execute("DELETE FROM delivery_order_item_partidas WHERE order_item_id = ANY(%s)", [delete_ids])
            cur.execute("DELETE FROM delivery_order_items WHERE order_id = %s AND id = ANY(%s)", [order_id, delete_ids])

    updated = get_delivery_order(order_id, include_events=False)
    create_event(
        order_id,
        actor_user_id,
        "delivery_order_updated",
        metadata={"itemCount": len(items), "previousStatus": current.get("status")},
    )
    if review_group:
        _record_review_order_correction(review_group, order_id, actor_user_id, current, updated)
    return get_delivery_order(order_id)


COMMERCIAL_CORRECTION_TOP_LEVEL_KEYS = {"commercialDeadline", "items"}
COMMERCIAL_CORRECTION_ITEM_KEYS = {"id", "unitPrice", "priceCurrency", "discountPercent"}


def _assert_commercial_correction_state(order: dict[str, Any]) -> None:
    if not order.get("status") or order.get("status") in CLOSED_STATUSES:
        raise DeliveryOrderError(
            "INVALID_COMMERCIAL_CORRECTION_STATE",
            "Solo se corrigen pedidos con remito pendientes de facturación.",
            status_code=409,
        )
    if not _optional_text(order.get("remitoNumber")):
        raise DeliveryOrderError("REMITO_REQUIRED", "La orden necesita remito para corregir precio o plazo.", status_code=409)
    if not (
        order_requires_billing(order)
        or remito_number_requires_billing(order.get("remitoNumber"))
    ):
        raise DeliveryOrderError(
            "DELIVERY_ORDER_NOT_BILLABLE",
            "La orden no tiene un remito pendiente de facturación.",
            status_code=409,
        )
    if _optional_text(order.get("invoiceNumber")):
        raise DeliveryOrderError(
            "DELIVERY_ORDER_INVOICED",
            "No se puede corregir una orden que ya tiene factura.",
            status_code=409,
        )


def _commercial_item_payload_by_id(payload: dict[str, Any], current_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise DeliveryOrderError("INVALID_COMMERCIAL_CORRECTION_PAYLOAD", "Payload inválido")
    invalid_keys = sorted(set(payload) - COMMERCIAL_CORRECTION_TOP_LEVEL_KEYS)
    if invalid_keys:
        raise DeliveryOrderError(
            "INVALID_COMMERCIAL_CORRECTION_PAYLOAD",
            "La corrección solo acepta plazo comercial e ítems de precio.",
        )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise DeliveryOrderError("ITEMS_REQUIRED", "La corrección necesita ítems de precio")
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise DeliveryOrderError("INVALID_ITEM", "Cada renglón debe ser un objeto")
        invalid_item_keys = sorted(set(item) - COMMERCIAL_CORRECTION_ITEM_KEYS)
        if invalid_item_keys:
            raise DeliveryOrderError(
                "INVALID_COMMERCIAL_CORRECTION_PAYLOAD",
                "La corrección no permite cambiar datos técnicos, cantidades, artículos ni partidas.",
                status_code=409,
            )
        item_id = _optional_text(item.get("id"))
        if not item_id:
            raise DeliveryOrderError("DELIVERY_ORDER_ITEM_REQUIRED", "Cada renglón necesita id")
        if item_id in by_id:
            raise DeliveryOrderError("DELIVERY_ORDER_ITEM_DUPLICATED", "Hay renglones repetidos en la corrección")
        by_id[item_id] = item
    current_ids = {str(item.get("id")) for item in current_items if item.get("id")}
    if set(by_id) != current_ids:
        raise DeliveryOrderError(
            "DELIVERY_ORDER_ITEMS_MISMATCH",
            "La corrección debe incluir exactamente los renglones existentes.",
            status_code=409,
        )
    return by_id


def _commercial_correction_next_items(order: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    current_items = [item for item in (order.get("items") or []) if isinstance(item, dict)]
    by_id = _commercial_item_payload_by_id(payload, current_items)
    next_items: list[dict[str, Any]] = []
    for item in current_items:
        item_payload = by_id[str(item.get("id"))]
        unit_price = _decimal_or_none(item_payload.get("unitPrice"))
        if unit_price is None or unit_price < 0:
            raise DeliveryOrderError("INVALID_DECIMAL", "Precio inválido")
        current_currency = delivery_order_item_price_currency(item, _price_currency(order.get("priceCurrency")))
        next_currency = _price_currency(item_payload.get("priceCurrency"), current_currency)
        if next_currency != current_currency:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_CURRENCY_LOCKED",
                "No se puede cambiar la moneda de un remito ya emitido desde esta corrección.",
                status_code=409,
            )
        discount_percent = _discount_percent(item_payload.get("discountPercent"))
        next_items.append(
            {
                **item,
                "unitPrice": unit_price,
                "priceCurrency": next_currency,
                "discountPercent": discount_percent,
            }
        )
    return next_items


def _commercial_correction_metadata(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    def decimal_text(value: Any) -> str:
        parsed = _decimal_or_none(value)
        return "" if parsed is None else str(parsed)

    before_totals = delivery_order_billing_totals(before)
    after_totals = delivery_order_billing_totals(after)
    before_items = {str(item.get("id")): item for item in before.get("items") or [] if isinstance(item, dict)}
    item_changes = []
    for item in after.get("items") or []:
        if not isinstance(item, dict):
            continue
        previous = before_items.get(str(item.get("id"))) or {}
        item_changes.append(
            {
                "id": item.get("id"),
                "articleCode": item.get("articleCode"),
                "description": item.get("articleName") or item.get("description"),
                "previousUnitPrice": decimal_text(previous.get("unitPrice")),
                "newUnitPrice": decimal_text(item.get("unitPrice")),
                "previousDiscountPercent": str(_discount_percent(previous.get("discountPercent"))),
                "newDiscountPercent": str(_discount_percent(item.get("discountPercent"))),
                "priceCurrency": delivery_order_item_price_currency(item, _price_currency(after.get("priceCurrency"))),
            }
        )
    return {
        "previousDeadline": before.get("commercialDeadline") or "",
        "newDeadline": after.get("commercialDeadline") or "",
        "previousTotal": str(before_totals.get("total")),
        "newTotal": str(after_totals.get("total")),
        "previousTotalLabel": _totals_money_label(before_totals, "total"),
        "newTotalLabel": _totals_money_label(after_totals, "total"),
        "items": item_changes,
        "remitoNumber": before.get("remitoNumber") or "",
        "correctionScope": "nexora_only",
        "bejermanRemitoModified": False,
    }


@transaction.atomic
def correct_delivery_order_commercial_terms(order_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    _require_commercial_correction_permission(actor_user_id)
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    _assert_commercial_correction_state(current)

    next_deadline = _optional_text(
        payload.get("commercialDeadline") if "commercialDeadline" in payload else current.get("commercialDeadline")
    )
    next_items = _commercial_correction_next_items(current, payload)

    header_currency = delivery_order_item_price_currency(next_items[0], _price_currency(current.get("priceCurrency"))) if next_items else _price_currency(current.get("priceCurrency"))
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
               SET commercial_deadline = %s,
                   price_currency = %s
             WHERE id = %s
            """,
            [next_deadline, header_currency, order_id],
        )
        for item in next_items:
            cur.execute(
                """
                UPDATE delivery_order_items
                   SET unit_price = %s,
                       price_currency = %s,
                       discount_percent = %s
                 WHERE id = %s AND order_id = %s
                """,
                [
                    item.get("unitPrice"),
                    item.get("priceCurrency"),
                    item.get("discountPercent"),
                    item.get("id"),
                    order_id,
                ],
            )

    updated = get_delivery_order(order_id, include_events=False)
    metadata = _commercial_correction_metadata(current, updated)
    create_event(order_id, actor_user_id, "delivery_order_commercial_correction", metadata=metadata)
    notify_delivery_order_commercial_correction(order_id, actor_user_id, metadata)
    return get_delivery_order(order_id)


def insert_item_partidas(cur, item_id: str, partidas: list[dict[str, Any]]):
    if not isinstance(partidas, list):
        return
    for idx, partida in enumerate(partidas):
        partida_text = _optional_text(partida.get("partida"))
        if not partida_text:
            continue
        cur.execute(
            """
            INSERT INTO delivery_order_item_partidas (
              id, order_item_id, ingreso_id, device_id, partida, assigned_quantity, partida_expiration_date,
              stock_deposit_code, stock_available_quantity, stock_checked_at, sort_order
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                _optional_text(partida.get("id")) or f"doip-{uuid4()}",
                item_id,
                _optional_int(partida.get("ingresoId") or partida.get("ingreso_id")),
                _optional_int(partida.get("deviceId") or partida.get("device_id")),
                partida_text,
                _decimal(partida.get("assignedQuantity") or partida.get("quantity")),
                partida.get("partidaExpirationDate") or None,
                _optional_text(partida.get("stockDepositCode")),
                _decimal(partida.get("stockAvailableQuantity"), "0")
                if partida.get("stockAvailableQuantity") not in (None, "")
                else None,
                partida.get("stockCheckedAt") or None,
                partida.get("sortOrder") if partida.get("sortOrder") is not None else idx,
            ],
        )


@transaction.atomic
def mark_prepared(order_id: str, actor_user_id: int | None, remito_number: str | None = None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if current["status"] == "pendiente_stock":
        raise DeliveryOrderError(
            "DELIVERY_ORDER_STOCK_PENDING",
            "La orden está pendiente de stock",
            status_code=409,
        )
    if current["status"] not in PREPARABLE_STATUSES:
        raise DeliveryOrderError("DELIVERY_ORDER_CLOSED", "La orden ya está cerrada", status_code=409)
    next_status = "armado_pendiente_entrega"
    effective_remito = _optional_text(remito_number)
    current_group_id = _optional_text(current.get("bejermanRemitoGroupId"))
    manual_recovery_group_id = ""
    if effective_remito and not _optional_text(current.get("remitoNumber")) and current_group_id:
        with connection.cursor() as cur:
            cur.execute("SELECT status FROM bejerman_remito_groups WHERE id = %s", [current_group_id])
            row = cur.fetchone()
        group_status = _optional_text(row[0] if row else "")
        if group_status in {"sent_unknown", "failed_review", "failed"}:
            manual_recovery_group_id = current_group_id
    should_register_manual_remito = (
        bool(effective_remito)
        and not _optional_text(current.get("remitoNumber"))
        and (not current_group_id or bool(manual_recovery_group_id))
    )
    manual_remito_document: dict[str, Any] | None = None
    manual_remito_is_existing = False
    if should_register_manual_remito:
        from .bejerman_delivery import normalize_delivery_order_manual_remito_number

        manual_remito_document = normalize_delivery_order_manual_remito_number(current, effective_remito)
        manual_remito_is_existing = manual_remito_document.get("documentMode") == "existing"

    if not manual_remito_is_existing:
        current = refresh_delivery_order_article_partida_flags(current, actor_user_id, strict=True, action="preparar")
        _validate_order_partidas_ready(current, actor_user_id)
        if current.get("deliveryType") == "rental":
            validate_rental_delivery_items_ready(
                current.get("items") or [],
                order_id=order_id,
                company_key=current.get("companyKey") or "SEPID",
                actor_user_id=actor_user_id,
            )
    manual_remito_metadata: dict[str, Any] = {}
    if should_register_manual_remito:
        from .bejerman_delivery import register_delivery_order_manual_remito

        manual_remito_metadata = register_delivery_order_manual_remito(
            current,
            actor_user_id,
            effective_remito,
            document=manual_remito_document,
            group_id=manual_recovery_group_id or None,
        )
        effective_remito = _optional_text(manual_remito_metadata.get("remitoNumber")) or effective_remito
    should_notify_billing = (
        bool(effective_remito)
        and not bool(_optional_text(current.get("remitoNumber")))
        and remito_number_requires_billing(effective_remito)
    )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET status = %s,
                remito_number = COALESCE(%s, remito_number),
                remito_location = CASE WHEN %s IS NOT NULL AND remito_location IS NULL THEN 'recepcion' ELSE remito_location END,
                remito_location_updated_by = CASE WHEN %s IS NOT NULL AND remito_location_updated_by IS NULL THEN %s ELSE remito_location_updated_by END,
                remito_location_updated_at = CASE WHEN %s IS NOT NULL AND remito_location_updated_at IS NULL THEN CURRENT_TIMESTAMP ELSE remito_location_updated_at END,
                prepared_by_user_id = COALESCE(prepared_by_user_id, %s),
                prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP)
            WHERE id = %s
            """,
            [
                next_status,
                effective_remito,
                effective_remito,
                effective_remito,
                actor_user_id,
                effective_remito,
                actor_user_id,
                order_id,
            ],
        )
    create_event(
        order_id,
        actor_user_id,
        "delivery_order_prepared",
        metadata={"status": next_status, "remitoNumber": effective_remito, **manual_remito_metadata},
    )
    if should_notify_billing:
        notify_delivery_order_remito_ready(order_id, actor_user_id)
    return get_delivery_order(order_id)


@transaction.atomic
def mark_delivered(order_id: str, actor_user_id: int | None, remito_number: str | None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if current["status"] == "pendiente_stock":
        raise DeliveryOrderError(
            "DELIVERY_ORDER_STOCK_PENDING",
            "La orden está pendiente de stock",
            status_code=409,
        )
    if current["status"] in CLOSED_STATUSES:
        raise DeliveryOrderError("DELIVERY_ORDER_CLOSED", "La orden ya está cerrada", status_code=409)
    remitos = current.get("remitos") if isinstance(current.get("remitos"), list) else []
    remito_group = current.get("bejermanRemitoGroup") if isinstance(current.get("bejermanRemitoGroup"), dict) else {}
    remito_group_status = _optional_text(remito_group.get("status"))
    if (
        current.get("remitoSetStatus") == "review"
        or any(remito.get("uncertain") for remito in remitos)
        or remito_group.get("uncertain")
        or remito_group_status in {"sent_unknown", "failed_review"}
    ):
        raise DeliveryOrderError(
            "BEJERMAN_REMITO_REQUIRES_REVIEW",
            "El remito Bejerman está en revisión. Verifique Bejerman antes de entregar la orden.",
            status_code=409,
        )
    if remitos and current.get("remitoSetStatus") != "complete":
        raise DeliveryOrderError(
            "DELIVERY_REMITO_SET_INCOMPLETE",
            "Todavía falta completar uno de los remitos de la orden.",
            status_code=409,
        )
    effective_remito = (
        _optional_text(current.get("remitoNumber"))
        if remitos
        else (_optional_text(remito_number) or _optional_text(current.get("remitoNumber")))
    )
    if not effective_remito:
        raise DeliveryOrderError("REMITO_REQUIRED", "Para entregar la orden se necesita un remito")
    if _optional_text(current.get("invoiceNumber")):
        next_status = "facturado"
    elif _billing_suppressed(order_id):
        next_status = "entregado_no_facturable"
    else:
        next_status = remito_status(
            effective_remito,
            billing_required=order_requires_billing(
                {**current, "remitoNumber": effective_remito}
            ),
        )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET status = %s,
                remito_number = %s,
                remito_location = COALESCE(remito_location, 'recepcion'),
                remito_location_updated_by = COALESCE(remito_location_updated_by, %s),
                remito_location_updated_at = COALESCE(remito_location_updated_at, CURRENT_TIMESTAMP),
                prepared_by_user_id = COALESCE(prepared_by_user_id, %s),
                delivered_by_user_id = COALESCE(delivered_by_user_id, %s),
                prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP),
                delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP)
            WHERE id = %s
            """,
            [next_status, effective_remito, actor_user_id, actor_user_id, actor_user_id, order_id],
        )
    create_event(order_id, actor_user_id, "delivery_order_delivered", metadata={"remitoNumber": effective_remito})
    if current.get("deliveryType") == "service_release":
        _sync_service_release_ingresos_delivery(
            service_release_ingreso_ids_from_order(current), effective_remito, actor_user_id
        )
    if next_status == "entregado_pendiente_facturacion":
        notify_delivery_order_remito_ready(order_id, actor_user_id)
    # Fuera de la transacción que cerró la orden y después del evento: una llamada a Bejerman no
    # puede tirar abajo una entrega ya confirmada. El helper es best-effort y no levanta.
    disable_particular_customer_if_idle(
        current.get("companyKey") or current.get("company_key"),
        current.get("propietarioDoc"),
        exclude_ingreso_id=current.get("ingresoId"),
        actor_user_id=actor_user_id,
    )
    return get_delivery_order(order_id)


def _normalize_invoice_batch_order_ids(order_ids: Any) -> list[str]:
    if not isinstance(order_ids, (list, tuple)):
        raise DeliveryOrderError(
            "DELIVERY_ORDER_IDS_REQUIRED",
            "Debe seleccionar al menos un remito para facturar.",
        )
    normalized: list[str] = []
    for value in order_ids:
        order_id = _optional_text(value)
        if order_id and order_id not in normalized:
            normalized.append(order_id)
    if not normalized:
        raise DeliveryOrderError(
            "DELIVERY_ORDER_IDS_REQUIRED",
            "Debe seleccionar al menos un remito para facturar.",
        )
    return normalized


def _lock_invoice_batch_orders(order_ids: list[str], *, expand_remito_groups: bool) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        if expand_remito_groups:
            cur.execute(
                """
                WITH RECURSIVE remito_edges(source_kind, source_id, target_kind, target_id) AS (
                    SELECT 'order', l.order_id, 'group', l.remito_group_id
                      FROM delivery_order_remito_links l
                    UNION ALL
                    SELECT 'group', l.remito_group_id, 'order', l.order_id
                      FROM delivery_order_remito_links l
                    UNION ALL
                    SELECT 'order', o.id, 'group', o.bejerman_remito_group_id
                      FROM delivery_orders o
                     WHERE o.bejerman_remito_group_id IS NOT NULL
                    UNION ALL
                    SELECT 'group', o.bejerman_remito_group_id, 'order', o.id
                      FROM delivery_orders o
                     WHERE o.bejerman_remito_group_id IS NOT NULL
                ), connected_nodes(node_kind, node_id) AS (
                    SELECT 'order', selected_id
                      FROM UNNEST(%s::text[]) AS selected_id
                    UNION
                    SELECT edge.target_kind, edge.target_id
                      FROM connected_nodes node
                      JOIN remito_edges edge
                        ON edge.source_kind = node.node_kind
                       AND edge.source_id = node.node_id
                )
                SELECT o.*
                  FROM delivery_orders o
                  JOIN connected_nodes node
                    ON node.node_kind = 'order'
                   AND node.node_id = o.id
                 ORDER BY o.id
                 FOR UPDATE OF o
                """,
                [order_ids],
            )
        else:
            cur.execute(
                """
                SELECT o.*
                  FROM delivery_orders o
                 WHERE o.id = ANY(%s)
                 ORDER BY o.id
                 FOR UPDATE
                """,
                [order_ids],
            )
        rows = _rows(cur)

    found_ids = {str(row.get("id") or "") for row in rows}
    missing_ids = [order_id for order_id in order_ids if order_id not in found_ids]
    if missing_ids:
        missing_label = ", ".join(missing_ids)
        raise DeliveryOrderError(
            "DELIVERY_ORDER_NOT_FOUND",
            f"No se encontraron las órdenes de entrega: {missing_label}.",
            status_code=404,
        )

    expanded_ids = [str(row["id"]) for row in rows]
    items_by_order = load_items_by_order(expanded_ids)
    remitos_by_order = load_delivery_order_remitos(expanded_ids)
    groups_by_id = load_remito_groups_by_id([row.get("bejerman_remito_group_id") for row in rows])
    # El propietario es la identidad de facturacion de un particular: sin el, _validate_invoice_batch_identity
    # no puede distinguir dos particulares distintos de dos ordenes de la misma persona.
    owner_by_ingreso = load_owner_fields_by_ingreso([row.get("ingreso_id") for row in rows])
    orders: list[dict[str, Any]] = []
    for row in rows:
        row["items"] = items_by_order.get(row["id"], [])
        row["remito_links"] = remitos_by_order.get(row["id"], [])
        row["bejerman_remito_group"] = groups_by_id.get(row.get("bejerman_remito_group_id"))
        _apply_owner_fields(row, owner_by_ingreso)
        order = serialize_order(row)
        postponed_until = row.get("billing_postponed_until")
        order["_billingPostponementActive"] = bool(
            postponed_until and postponed_until > timezone.now()
        )
        orders.append(order)
    return orders


def _load_invoice_batch_response_orders(order_ids: list[str]) -> list[dict[str, Any]]:
    if not order_ids:
        return []
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT *
              FROM delivery_orders
             WHERE id = ANY(%s)
             ORDER BY id
            """,
            [order_ids],
        )
        rows = _rows(cur)

    rows_by_id = {str(row["id"]): row for row in rows}
    if len(rows_by_id) != len(order_ids):
        raise DeliveryOrderError(
            "DELIVERY_ORDER_NOT_FOUND",
            "No se pudieron recuperar todas las órdenes facturadas.",
            status_code=404,
        )

    items_by_order = load_items_by_order(order_ids)
    remitos_by_order = load_delivery_order_remitos(order_ids)
    groups_by_id = load_remito_groups_by_id([row.get("bejerman_remito_group_id") for row in rows])
    response_by_id: dict[str, dict[str, Any]] = {}
    for order_id, row in rows_by_id.items():
        row["items"] = items_by_order.get(order_id, [])
        row["remito_links"] = remitos_by_order.get(order_id, [])
        row["bejerman_remito_group"] = groups_by_id.get(row.get("bejerman_remito_group_id"))
        response_by_id[order_id] = serialize_order(row)
    return [response_by_id[order_id] for order_id in order_ids]


def _invoice_batch_suppressed_order_ids(order_ids: list[str]) -> set[str]:
    if not order_ids:
        return set()
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT order_id
              FROM delivery_order_events
             WHERE order_id = ANY(%s)
               AND event_type = 'delivery_order_not_billable'
            """,
            [order_ids],
        )
        return {str(row[0]) for row in cur.fetchall()}


def _invoice_order_identity(order: dict[str, Any]) -> tuple[str | None, str | None]:
    group = order.get("bejermanRemitoGroup") if isinstance(order.get("bejermanRemitoGroup"), dict) else {}
    customer_code = _optional_text(group.get("customerCode")) or _optional_text(order.get("bejermanCustomerCode"))
    company_key, _point_of_sale = resolve_delivery_order_billing_company(order)
    return customer_code, company_key


def is_occasional_customer_order(order: dict[str, Any]) -> bool:
    """Un particular: sin código Bejerman y con la razón social placeholder. Se emite y se
    factura como cliente ocasional, identificado por el documento del propietario."""
    code = _optional_text(order.get("bejermanCustomerCode") or order.get("bejerman_customer_code"))
    return not code and is_occasional_customer_name(order.get("customerName") or order.get("customer_name"))


def occasional_customer_document(order: dict[str, Any]) -> str:
    return re.sub(r"\D", "", _clean_text(order.get("propietarioDoc") or order.get("propietario_doc")))


def _validate_occasional_invoice_batch_identity(orders: list[dict[str, Any]]) -> None:
    """Sin código Bejerman sólo se factura en grupo si son todas del mismo particular: el número
    de factura es uno solo y no puede quedar estampado sobre equipos de dos personas distintas."""
    if not all(is_occasional_customer_order(order) for order in orders):
        raise DeliveryOrderError(
            "CUSTOMER_MAPPING_REQUIRED",
            "Las órdenes necesitan un código Bejerman para facturarse en grupo.",
            status_code=409,
        )
    documents = {occasional_customer_document(order) for order in orders}
    if len(documents) != 1 or not next(iter(documents)):
        raise DeliveryOrderError(
            "INCOMPATIBLE_DELIVERY_INVOICE_CUSTOMER",
            "Las órdenes de particulares deben corresponder al mismo propietario para facturarse en grupo.",
            status_code=409,
        )


def _validate_invoice_batch_identity(orders: list[dict[str, Any]]) -> None:
    identities = [_invoice_order_identity(order) for order in orders]
    missing_code_orders = [
        str(order.get("orderNumber") or order.get("id") or "")
        for order, (customer_code, _company_key) in zip(orders, identities)
        if not customer_code
    ]
    if missing_code_orders:
        # Mezclar una orden con código y otra sin él sigue siendo un error de mapeo; el camino
        # ocasional sólo aplica cuando todas son de particulares.
        if len(missing_code_orders) != len(orders):
            raise DeliveryOrderError(
                "CUSTOMER_MAPPING_REQUIRED",
                "Las órdenes necesitan un código Bejerman para facturarse en grupo.",
                status_code=409,
            )
        _validate_occasional_invoice_batch_identity(orders)
    else:
        customer_codes = {str(customer_code).upper() for customer_code, _company_key in identities}
        if len(customer_codes) != 1:
            raise DeliveryOrderError(
                "INCOMPATIBLE_DELIVERY_INVOICE_CUSTOMER",
                "Las órdenes seleccionadas corresponden a clientes Bejerman distintos.",
                status_code=409,
            )

    missing_company_orders = [
        str(order.get("orderNumber") or order.get("id") or "")
        for order, (_customer_code, company_key) in zip(orders, identities)
        if not company_key
    ]
    if missing_company_orders:
        raise DeliveryOrderError(
            "DELIVERY_ORDER_BILLING_COMPANY_REQUIRED",
            "No se pudo determinar la empresa Bejerman de una orden seleccionada.",
            status_code=409,
        )

    company_keys = {company_key for _customer_code, company_key in identities}
    if len(company_keys) != 1:
        raise DeliveryOrderError(
            "INCOMPATIBLE_DELIVERY_INVOICE_COMPANY",
            "Las órdenes seleccionadas corresponden a empresas Bejerman distintas.",
            status_code=409,
        )


def _mark_invoiced_batch_core(
    order_ids: Any,
    actor_user_id: int | None,
    invoice_number: str,
    *,
    expand_remito_groups: bool,
    require_batch_identity: bool,
    allow_existing_invoice: bool,
    reject_postponed: bool,
    include_batch_event_metadata: bool,
) -> dict[str, Any]:
    invoice = _optional_text(invoice_number)
    if not invoice:
        raise DeliveryOrderError("INVOICE_REQUIRED", "Número de factura requerido")

    requested_ids = _normalize_invoice_batch_order_ids(order_ids)
    orders = _lock_invoice_batch_orders(requested_ids, expand_remito_groups=expand_remito_groups)
    expanded_ids = [str(order["id"]) for order in orders]
    if require_batch_identity:
        _validate_invoice_batch_identity(orders)

    suppressed_ids = _invoice_batch_suppressed_order_ids(expanded_ids)
    modified_orders: list[dict[str, Any]] = []
    for order in orders:
        order_id = str(order["id"])
        current_invoice = _optional_text(order.get("invoiceNumber"))
        if current_invoice:
            if allow_existing_invoice and current_invoice == invoice:
                continue
            code = "DELIVERY_ORDER_INVOICE_CONFLICT" if allow_existing_invoice else "INVALID_INVOICE_STATE"
            message = (
                "Una orden seleccionada ya tiene otra factura registrada."
                if allow_existing_invoice
                else "Solo se factura una orden con remito facturable pendiente"
            )
            raise DeliveryOrderError(code, message, status_code=409)
        if reject_postponed and order.get("_billingPostponementActive"):
            raise DeliveryOrderError(
                "DELIVERY_ORDER_BILLING_POSTPONED",
                "Una orden seleccionada tiene la facturación pospuesta. Reactívela antes de facturar el lote.",
                status_code=409,
            )
        if order.get("remitoSetStatus") != "complete":
            raise DeliveryOrderError(
                "DELIVERY_REMITO_SET_INCOMPLETE",
                "No se puede facturar hasta completar todos los remitos de la orden.",
                status_code=409,
            )
        if (
            not order_requires_billing(order)
            or order.get("status") in CLOSED_STATUSES
            or order_id in suppressed_ids
        ):
            raise DeliveryOrderError(
                "INVALID_INVOICE_STATE",
                "Solo se facturan órdenes con remito facturable pendiente.",
                status_code=409,
            )
        modified_orders.append(order)

    with connection.cursor() as cur:
        for order in modified_orders:
            next_status = (
                "facturado"
                if order["status"] == "entregado_pendiente_facturacion" or order.get("deliveredAt")
                else order["status"]
            )
            cur.execute(
                """
                UPDATE delivery_orders
                   SET status = %s,
                       invoice_number = %s,
                       invoiced_by_user_id = %s,
                       invoiced_at = CURRENT_TIMESTAMP,
                       billing_postponed_until = NULL,
                       billing_postponed_by_user_id = NULL,
                       billing_postponed_at = NULL,
                       billing_postponement_notified_at = NULL
                 WHERE id = %s
                """,
                [next_status, invoice, actor_user_id, order["id"]],
            )

    synced_ingreso_ids: list[int] = []
    for order in orders:
        for ingreso_id in service_release_ingreso_ids_from_order(order):
            _append_unique_int(synced_ingreso_ids, ingreso_id)
    synced_ingreso_ids.sort()
    _sync_service_release_ingresos_invoice(synced_ingreso_ids, invoice)
    for order in modified_orders:
        order_ingreso_ids = service_release_ingreso_ids_from_order(order)
        event_metadata: dict[str, Any] = {"invoiceNumber": invoice}
        if include_batch_event_metadata:
            event_metadata["orderIds"] = expanded_ids
        if order_ingreso_ids:
            if len(order_ingreso_ids) == 1:
                event_metadata["ingresoId"] = order_ingreso_ids[0]
            event_metadata["ingresoIds"] = order_ingreso_ids
        create_event(
            str(order["id"]),
            actor_user_id,
            "delivery_order_invoiced",
            metadata=event_metadata,
        )

    updated_orders = (
        _load_invoice_batch_response_orders(expanded_ids)
        if include_batch_event_metadata
        else [get_delivery_order(order_id) for order_id in expanded_ids]
    )
    return {
        "invoiceNumber": invoice,
        "count": len(expanded_ids),
        "orderIds": expanded_ids,
        "orders": updated_orders,
    }


@transaction.atomic
def mark_invoiced_batch(order_ids: Any, actor_user_id: int | None, invoice_number: str) -> dict[str, Any]:
    return _mark_invoiced_batch_core(
        order_ids,
        actor_user_id,
        invoice_number,
        expand_remito_groups=True,
        require_batch_identity=True,
        allow_existing_invoice=True,
        reject_postponed=True,
        include_batch_event_metadata=True,
    )


@transaction.atomic
def mark_invoiced(order_id: str, actor_user_id: int | None, invoice_number: str) -> dict[str, Any]:
    result = _mark_invoiced_batch_core(
        [order_id],
        actor_user_id,
        invoice_number,
        expand_remito_groups=False,
        require_batch_identity=False,
        allow_existing_invoice=False,
        reject_postponed=False,
        include_batch_event_metadata=False,
    )
    return result["orders"][0]


@transaction.atomic
def mark_not_billable(order_id: str, actor_user_id: int | None, note: str | None = None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if current.get("remitoSetStatus") != "complete":
        raise DeliveryOrderError(
            "DELIVERY_REMITO_SET_INCOMPLETE",
            "No se puede cerrar la facturación hasta completar todos los remitos de la orden.",
            status_code=409,
        )
    if not order_requires_billing(current) or _optional_text(current.get("invoiceNumber")):
        raise DeliveryOrderError("INVALID_NOT_BILLABLE_STATE", "Solo se marca como no facturable una orden con remito facturable pendiente", status_code=409)
    next_status = (
        "entregado_no_facturable"
        if current["status"] == "entregado_pendiente_facturacion" or current.get("deliveredAt")
        else current["status"]
    )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET status = %s,
                billing_postponed_until = NULL,
                billing_postponed_by_user_id = NULL,
                billing_postponed_at = NULL,
                billing_postponement_notified_at = NULL
            WHERE id = %s
            """,
            [next_status, order_id],
        )
    create_event(order_id, actor_user_id, "delivery_order_not_billable", note=note)
    return get_delivery_order(order_id)


@transaction.atomic
def cancel_order(order_id: str, actor_user_id: int | None, note: str | None = None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if current["status"] == "facturado":
        raise DeliveryOrderError("DELIVERY_ORDER_INVOICED", "No se puede cancelar una orden facturada", status_code=409)
    if current["status"] in {"entregado_pendiente_facturacion", "entregado_no_facturable"}:
        raise DeliveryOrderError("DELIVERY_ORDER_DELIVERED", "No se puede cancelar una orden entregada", status_code=409)
    if _optional_text(current.get("remitoNumber")):
        raise DeliveryOrderError("DELIVERY_ORDER_REMITO_EMITTED", "No se puede cancelar una orden con remito emitido", status_code=409)
    if current["status"] in NON_CANCELABLE_STATUSES:
        raise DeliveryOrderError("DELIVERY_ORDER_CLOSED", "La orden ya está cerrada", status_code=409)
    stuck_group_id = _optional_text(current.get("bejermanRemitoGroupId"))
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET status = 'cancelado',
                cancelled_by_user_id = %s,
                cancelled_at = COALESCE(cancelled_at, CURRENT_TIMESTAMP)
            WHERE id = %s
            """,
            [actor_user_id, order_id],
        )
        if stuck_group_id:
            # Un grupo en sent_unknown/failed_review necesita reconciliación manual con
            # Bejerman; si la orden se cancela igual quedaría huérfano para siempre en el
            # panel de revisión. Lo retiramos acá en vez de dejarlo colgado.
            retirement_patch = {
                "orchestrationStatus": "failed",
                "reviewRequired": False,
                "reconciliationRequired": False,
                "retiredReason": "order_cancelled",
                "retiredAt": timezone.now().isoformat(),
            }
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET status = 'failed',
                       response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
                 WHERE id = %s AND status IN ('sent_unknown', 'failed_review')
                """,
                [_json_param(retirement_patch), stuck_group_id],
            )
            if cur.rowcount:
                cur.execute(
                    "UPDATE delivery_orders SET bejerman_remito_group_id = NULL WHERE id = %s",
                    [order_id],
                )
    create_event(order_id, actor_user_id, "delivery_order_cancelled", note=note)
    return get_delivery_order(order_id)


@transaction.atomic
def update_remito_location(order_id: str, actor_user_id: int | None, remito_location: str) -> dict[str, Any]:
    location = _clean_text(remito_location).lower()
    if location not in REMITO_LOCATIONS:
        raise DeliveryOrderError("INVALID_REMITO_LOCATION", "Ubicación de remito inválida")
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if not current.get("remitoNumber"):
        raise DeliveryOrderError("REMITO_REQUIRED", "La orden no tiene remito cargado", status_code=409)
    if current.get("remitos") and current.get("remitoSetStatus") != "complete":
        raise DeliveryOrderError(
            "DELIVERY_REMITO_SET_INCOMPLETE",
            "No se puede mover el conjunto físico hasta completar todos sus remitos.",
            status_code=409,
        )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET remito_location = %s,
                remito_location_updated_by = %s,
                remito_location_updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            [location, actor_user_id, order_id],
        )
    create_event(
        order_id,
        actor_user_id,
        "remito_location_updated",
        metadata={"previousRemitoLocation": current.get("remitoLocation"), "remitoLocation": location},
    )
    return get_delivery_order(order_id)


@transaction.atomic
def update_item_article(order_id: str, item_id: str, actor_user_id: int | None, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    _assert_order_editable(current)
    if current.get("deliveryType") == "rental":
        raise DeliveryOrderError(
            "RENTAL_ITEMS_READ_ONLY",
            "Los renglones de alquiler se editan seleccionando equipos disponibles",
            status_code=409,
        )
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, article_code, quantity, stock_deposit_code
            FROM delivery_order_items
            WHERE id = %s AND order_id = %s
            """,
            [item_id, order_id],
        )
        item_row = _one(cur)
        if not item_row:
            raise DeliveryOrderError("DELIVERY_ORDER_ITEM_NOT_FOUND", "Ítem de orden no encontrado", status_code=404)
    price_currency_update = (
        _price_currency(payload.get("priceCurrency") if "priceCurrency" in payload else payload.get("price_currency"))
        if "priceCurrency" in payload or "price_currency" in payload
        else None
    )
    stock_payload = dict(payload)
    if _optional_text(stock_payload.get("partida")):
        validation_item = {
            "articleCode": _optional_text(stock_payload.get("articleCode")),
            "quantity": item_row.get("quantity"),
            "partida": _optional_text(stock_payload.get("partida")),
            "partidaExpirationDate": stock_payload.get("partidaExpirationDate"),
            "stockDepositCode": stock_payload.get("stockDepositCode"),
            "stockAvailableQuantity": stock_payload.get("stockAvailableQuantity"),
            "stockCheckedAt": stock_payload.get("stockCheckedAt"),
        }
        _validate_delivery_partidas_stock(
            [validation_item],
            normalize_company_key(current.get("companyKey") or "SEPID"),
            normalize_delivery_type(current.get("deliveryType")),
            actor_user_id,
        )
        stock_payload.update(
            {
                "partida": validation_item.get("partida"),
                "partidaExpirationDate": validation_item.get("partidaExpirationDate"),
                "stockDepositCode": validation_item.get("stockDepositCode"),
                "stockAvailableQuantity": validation_item.get("stockAvailableQuantity"),
                "stockCheckedAt": validation_item.get("stockCheckedAt"),
            }
        )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_order_items
            SET article_code = %s,
                article_name = %s,
                article_requires_partida = %s,
                unit_price = %s,
                price_currency = COALESCE(%s, price_currency),
                partida = %s,
                partida_expiration_date = %s,
                stock_deposit_code = %s,
                stock_available_quantity = %s,
                stock_checked_at = %s
            WHERE id = %s AND order_id = %s
            """,
            [
                _optional_text(stock_payload.get("articleCode")),
                _optional_text(stock_payload.get("articleName")),
                _nullable_bool(stock_payload.get("articleRequiresPartida") if "articleRequiresPartida" in stock_payload else stock_payload.get("article_requires_partida")),
                _decimal(stock_payload.get("unitPrice"), "0") if stock_payload.get("unitPrice") not in (None, "") else None,
                price_currency_update,
                _optional_text(stock_payload.get("partida")),
                stock_payload.get("partidaExpirationDate") or None,
                _optional_text(stock_payload.get("stockDepositCode")),
                _decimal(stock_payload.get("stockAvailableQuantity"), "0")
                if stock_payload.get("stockAvailableQuantity") not in (None, "")
                else None,
                stock_payload.get("stockCheckedAt") or None,
                item_id,
                order_id,
            ],
        )
        if cur.rowcount < 1:
            raise DeliveryOrderError("DELIVERY_ORDER_ITEM_NOT_FOUND", "Ítem de orden no encontrado", status_code=404)
    create_event(order_id, actor_user_id, "delivery_order_item_article_updated", metadata={"itemId": item_id})
    return get_delivery_order(order_id)


@transaction.atomic
def update_item_partidas(order_id: str, item_id: str, actor_user_id: int | None, partidas: list[dict[str, Any]]) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    _assert_order_editable(current)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, article_code, quantity, stock_deposit_code
            FROM delivery_order_items
            WHERE id = %s AND order_id = %s
            """,
            [item_id, order_id],
        )
        item_row = _one(cur)
        if not item_row:
            raise DeliveryOrderError("DELIVERY_ORDER_ITEM_NOT_FOUND", "Ítem de orden no encontrado", status_code=404)
        normalized_partidas = _normalize_item_partidas(partidas or [], _decimal(item_row.get("quantity")))
        if current.get("deliveryType") == "rental":
            normalized_partidas = resolve_rental_item_partidas(
                {
                    "id": item_row.get("id"),
                    "articleCode": item_row.get("article_code"),
                    "quantity": item_row.get("quantity"),
                    "partidas": normalized_partidas,
                },
                normalized_partidas,
                company_key=current.get("companyKey") or "SEPID",
                actor_user_id=actor_user_id,
                order_id=order_id,
            )
        else:
            _validate_delivery_partidas_stock(
                [
                    {
                        "articleCode": item_row.get("article_code"),
                        "quantity": item_row.get("quantity"),
                        "stockDepositCode": item_row.get("stock_deposit_code"),
                        "partidas": normalized_partidas,
                    }
                ],
                normalize_company_key(current.get("companyKey") or "SEPID"),
                normalize_delivery_type(current.get("deliveryType")),
                actor_user_id,
            )
        cur.execute("DELETE FROM delivery_order_item_partidas WHERE order_item_id = %s", [item_id])
        if normalized_partidas:
            cur.execute(
                """
                UPDATE delivery_order_items
                   SET partida = NULL,
                       partida_expiration_date = NULL,
                       stock_deposit_code = NULL,
                       stock_available_quantity = NULL,
                       stock_checked_at = NULL
                 WHERE id = %s AND order_id = %s
                """,
                [item_id, order_id],
            )
        insert_item_partidas(cur, item_id, normalized_partidas)
    create_event(
        order_id,
        actor_user_id,
        "delivery_order_item_partidas_updated",
        metadata={"itemId": item_id, "count": len(normalized_partidas)},
    )
    return get_delivery_order(order_id)


def _date_for_order(value: Any) -> str:
    if hasattr(value, "date"):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return date.today().isoformat()


def _has_table_column(table_name: str, column_name: str) -> bool:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = %s
                   AND column_name = %s
                 LIMIT 1
                """,
                [table_name, column_name],
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _article_code_key(value: Any) -> str:
    return "".join(char for char in _clean_text(value).upper() if char not in {" ", "-"})


def is_rental_shelf_location_name(value: Any) -> bool:
    text = _clean_text(value).casefold()
    return "estanter" in text and "alquiler" in text


def _equipment_label(row: dict[str, Any]) -> str:
    model_text = " ".join(
        part
        for part in [
            _optional_text(row.get("modelo")),
            _optional_text(row.get("equipo_variante")) or _optional_text(row.get("device_variante")) or _optional_text(row.get("modelo_variante")),
        ]
        if part
    )
    return " | ".join(
        part
        for part in [
            _optional_text(row.get("tipo_equipo")),
            _optional_text(row.get("marca")),
            model_text,
        ]
        if part
    ) or "Equipo"


def _equipment_detail(row: dict[str, Any]) -> str:
    parts = [_equipment_label(row)]
    serial = _optional_text(row.get("equipment_serial"))
    internal_number = _optional_text(row.get("equipment_internal_number"))
    if serial:
        parts.append(f"N° serie {serial}")
    if internal_number:
        parts.append(f"N° interno (MG) {internal_number}")
    return " - ".join(part for part in parts if part) or "Equipo"


def _equipment_mapping_variant(row: dict[str, Any]) -> str:
    return (
        _optional_text(row.get("equipo_variante"))
        or _optional_text(row.get("device_variante"))
        or _optional_text(row.get("modelo_variante"))
        or ""
    )


def article_mappings_for_equipment_rows(
    rows: list[dict[str, Any]], company_key: str = "SEPID"
) -> dict[tuple[Any, str], dict[str, Any]]:
    """Mapeo modelo+variante -> artículo Bejerman para un lote de filas de equipo.

    Resolverlos fila por fila era un N+1 en los listados de equipos, que trabajan
    sobre cientos de candidatos. Se indexa por `(model_id, variante_norm)`;
    `article_mapping_from_rows` resuelve cada fila contra el índice.
    """
    from .bejerman_companies import require_company

    model_ids = sorted({row.get("model_id") for row in rows if row.get("model_id")})
    if not model_ids:
        return {}
    try:
        resolved_company = require_company(company_key or "SEPID").key
    except ValueError:
        resolved_company = "SEPID"
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (model_id, variante_norm)
                       model_id, variante_norm, article_code, article_description
                  FROM bejerman_article_mappings
                 WHERE model_id = ANY(%s)
                   AND company_key = %s
                 ORDER BY model_id,
                          variante_norm,
                          CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END,
                          confirmed_at DESC NULLS LAST,
                          updated_at DESC
                """,
                [list(model_ids), resolved_company],
            )
            return {
                (model_id, variante_norm): {
                    "article_code": article_code,
                    "article_description": article_description,
                }
                for model_id, variante_norm, article_code, article_description in cur.fetchall()
            }
    except DatabaseError:
        return {}


def article_mapping_from_rows(
    mappings: dict[tuple[Any, str], dict[str, Any]], row: dict[str, Any]
) -> dict[str, Any] | None:
    """Mapeo de una fila de equipo dentro del índice de `article_mappings_for_equipment_rows`."""
    from .bejerman_sync import normalize_article_variant

    model_id = row.get("model_id")
    if not model_id:
        return None
    return mappings.get((model_id, normalize_article_variant(_equipment_mapping_variant(row))))


def rental_equipment_context(ingreso_id: int) -> dict[str, Any] | None:
    company_expr = "COALESCE(t.empresa_bejerman, 'SEPID')" if _has_table_column("ingresos", "empresa_bejerman") else "'SEPID'"
    has_location = _has_table_column("ingresos", "ubicacion_id")
    has_alquilado = _has_table_column("ingresos", "alquilado")
    location_select = "t.ubicacion_id, COALESCE(loc.nombre, '') AS ubicacion_nombre" if has_location else "NULL AS ubicacion_id, '' AS ubicacion_nombre"
    location_join = "LEFT JOIN locations loc ON loc.id = t.ubicacion_id" if has_location else ""
    alquilado_expr = "COALESCE(t.alquilado, FALSE)" if has_alquilado else "FALSE"
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id AS ingreso_id,
                   t.device_id,
                   t.estado,
                   {alquilado_expr} AS alquilado,
                   {company_expr} AS company_key,
                   {location_select},
                   COALESCE(t.equipo_variante, '') AS equipo_variante,
                   c.id AS owner_customer_id,
                   COALESCE(c.razon_social, '') AS owner_customer_name,
                   COALESCE(d.numero_serie, '') AS equipment_serial,
                   COALESCE(d.numero_interno, '') AS equipment_internal_number,
                   d.model_id,
                   COALESCE(d.variante, '') AS device_variante,
                   COALESCE(m.nombre, '') AS modelo,
                   COALESCE(m.tipo_equipo, '') AS tipo_equipo,
                   COALESCE(m.variante, '') AS modelo_variante,
                   COALESCE(b.nombre, '') AS marca
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              {location_join}
             WHERE t.id = %s
            """,
            [ingreso_id],
        )
        row = _one(cur)
    return row


def rental_item_from_context(row: dict[str, Any], mapping: dict[str, Any], *, stock: dict[str, Any] | None = None) -> dict[str, Any]:
    stock = stock or {}
    detail = _equipment_detail(row)
    return {
        "ingresoId": row.get("ingreso_id"),
        "deviceId": row.get("device_id"),
        # El equipo dado de alta con su MG y sin OS se nombra por el MG.
        "sourceReference": service_order_reference(row.get("ingreso_id")) or _optional_text(row.get("equipment_internal_number")),
        "companyKey": normalize_company_key(row.get("company_key") or "SEPID"),
        "ownerCustomerId": row.get("owner_customer_id"),
        "ownerCustomerName": row.get("owner_customer_name") or "",
        "equipmentModel": _equipment_label(row),
        "equipmentDetail": detail,
        "equipmentSerial": row.get("equipment_serial") or "",
        "equipmentInternalNumber": row.get("equipment_internal_number") or "",
        "tipoEquipo": row.get("tipo_equipo") or "",
        "marca": row.get("marca") or "",
        "modelo": row.get("modelo") or "",
        "equipoVariante": row.get("equipo_variante") or row.get("device_variante") or row.get("modelo_variante") or "",
        "ubicacionId": row.get("ubicacion_id"),
        "ubicacionNombre": row.get("ubicacion_nombre") or "",
        "estado": row.get("estado") or "",
        "articleCode": mapping.get("article_code") or "",
        "articleName": mapping.get("article_description") or detail,
        "description": detail,
        "quantity": 1,
        "partida": row.get("equipment_serial") or "",
        "stockDepositCode": RENTAL_STOCK_DEPOSIT,
        "stockAvailableQuantity": stock.get("stockAvailableQuantity"),
        "partidaExpirationDate": stock.get("partidaExpirationDate") or "",
        "stockCheckedAt": stock.get("stockCheckedAt") or _iso(timezone.now()),
    }


def _rental_equipment_contexts_by_serial(serial: str) -> list[dict[str, Any]]:
    serial_text = _optional_text(serial)
    if not serial_text:
        return []
    company_expr = "COALESCE(t.empresa_bejerman, 'SEPID')" if _has_table_column("ingresos", "empresa_bejerman") else "'SEPID'"
    has_location = _has_table_column("ingresos", "ubicacion_id")
    has_alquilado = _has_table_column("ingresos", "alquilado")
    location_select = "t.ubicacion_id, COALESCE(loc.nombre, '') AS ubicacion_nombre" if has_location else "NULL AS ubicacion_id, '' AS ubicacion_nombre"
    location_join = "LEFT JOIN locations loc ON loc.id = t.ubicacion_id" if has_location else ""
    alquilado_expr = "COALESCE(t.alquilado, FALSE)" if has_alquilado else "FALSE"
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id AS ingreso_id,
                   t.device_id,
                   t.estado,
                   {alquilado_expr} AS alquilado,
                   {company_expr} AS company_key,
                   {location_select},
                   COALESCE(t.equipo_variante, '') AS equipo_variante,
                   c.id AS owner_customer_id,
                   COALESCE(c.razon_social, '') AS owner_customer_name,
                   COALESCE(d.numero_serie, '') AS equipment_serial,
                   COALESCE(d.numero_interno, '') AS equipment_internal_number,
                   d.model_id,
                   COALESCE(d.variante, '') AS device_variante,
                   COALESCE(m.nombre, '') AS modelo,
                   COALESCE(m.tipo_equipo, '') AS tipo_equipo,
                   COALESCE(m.variante, '') AS modelo_variante,
                   COALESCE(b.nombre, '') AS marca
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              {location_join}
             WHERE LOWER(TRIM(COALESCE(d.numero_serie, ''))) = LOWER(TRIM(%s))
             ORDER BY t.id DESC
             LIMIT 20
            """,
            [serial_text],
        )
        return _rows(cur)


def _rental_device_context_sql(where_sql: str) -> str:
    """Mismo shape que `rental_equipment_context` pero armado desde `devices`, sin OS.

    Alquilar no exige ingreso: un equipo nuevo se da de alta en Equipos con su MG y se
    marca alquilado ahí. Sólo se resuelven por acá los equipos que **no** tienen ningún
    ingreso; el que ya pasó por taller sigue resolviéndose por su OS, que es la que lleva
    el estado del circuito.
    """
    alquilado_expr = "COALESCE(d.alquilado, FALSE)" if _has_table_column("devices", "alquilado") else "FALSE"
    mg_estado_expr = "COALESCE(d.mg_estado, 'activo')" if _has_table_column("devices", "mg_estado") else "'activo'"
    has_location = _has_table_column("devices", "ubicacion_id")
    location_select = "d.ubicacion_id, COALESCE(loc.nombre, '') AS ubicacion_nombre" if has_location else "NULL AS ubicacion_id, '' AS ubicacion_nombre"
    location_join = "LEFT JOIN locations loc ON loc.id = d.ubicacion_id" if has_location else ""
    return f"""
        SELECT NULL::integer AS ingreso_id,
               d.id AS device_id,
               '' AS estado,
               {alquilado_expr} AS alquilado,
               {mg_estado_expr} AS mg_estado,
               'SEPID' AS company_key,
               {location_select},
               COALESCE(d.variante, '') AS equipo_variante,
               c.id AS owner_customer_id,
               COALESCE(c.razon_social, '') AS owner_customer_name,
               COALESCE(d.numero_serie, '') AS equipment_serial,
               COALESCE(d.numero_interno, '') AS equipment_internal_number,
               d.model_id,
               COALESCE(d.variante, '') AS device_variante,
               COALESCE(m.nombre, '') AS modelo,
               COALESCE(m.tipo_equipo, '') AS tipo_equipo,
               COALESCE(m.variante, '') AS modelo_variante,
               COALESCE(b.nombre, '') AS marca
          FROM devices d
          LEFT JOIN customers c ON c.id = d.customer_id
          LEFT JOIN models m ON m.id = d.model_id
          LEFT JOIN marcas b ON b.id = d.marca_id
          {location_join}
         WHERE {where_sql}
           AND NOT EXISTS (SELECT 1 FROM ingresos i WHERE i.device_id = d.id)
    """


def _rental_device_contexts_by_serial(serial: str) -> list[dict[str, Any]]:
    serial_text = _optional_text(serial)
    if not serial_text:
        return []
    with connection.cursor() as cur:
        cur.execute(
            _rental_device_context_sql("LOWER(TRIM(COALESCE(d.numero_serie, ''))) = LOWER(TRIM(%s))")
            + " ORDER BY d.id DESC LIMIT 20",
            [serial_text],
        )
        return _rows(cur)


def rental_device_context(device_id: int) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(_rental_device_context_sql("d.id = %s"), [device_id])
        return _one(cur)


def _rental_location_label(row: dict[str, Any]) -> str:
    # La ubicación "-" (id 10) es la de los equipos que ya no están en la casa: nombrarla
    # tal cual deja el mensaje diciendo "está en -".
    name = _clean_text(row.get("ubicacion_nombre"))
    return name if name and name != "-" else "sin ubicación"


def _rental_context_blocker(row: dict[str, Any]) -> tuple[str, str, str] | None:
    """Por qué ese equipo no sirve para alquilar, o None si sirve.

    Devuelve el código de error y el motivo ya redactado, en femenino (sujeto "la OS") y
    en masculino (sujeto "el NS", para el equipo sin OS). Son cortes distintos —
    alquilado, OS cerrada, otra ubicación, MG vendido— y hasta el 25/08/2026 todos
    contestaban "NS no vinculado a una OS disponible", que no decía cuál era ni qué hacer.
    """
    if bool(row.get("alquilado")):
        return ("RENTAL_EQUIPMENT_NOT_AVAILABLE", *RENTAL_UNAVAILABLE_STATE_REASONS["alquilado"])
    mg_estado = _clean_text(row.get("mg_estado")).lower()
    if mg_estado and mg_estado != "activo":
        motivo = "figura dado de baja del parque MG por venta"
        return "RENTAL_EQUIPMENT_MG_INACTIVE", motivo, motivo
    estado = _clean_text(row.get("estado")).lower()
    if estado in RENTAL_UNAVAILABLE_STATES:
        generico = f"figura en estado {estado}"
        return ("RENTAL_EQUIPMENT_NOT_AVAILABLE", *RENTAL_UNAVAILABLE_STATE_REASONS.get(estado, (generico, generico)))
    if not is_rental_shelf_location_name(row.get("ubicacion_nombre")):
        motivo = f"está en {_rental_location_label(row)}, no en Estantería de Alquiler"
        return "RENTAL_EQUIPMENT_NOT_IN_SHELF", motivo, motivo
    return None


def _rental_context_available(row: dict[str, Any]) -> bool:
    return _rental_context_blocker(row) is None


def _rental_blocker_hint(code: str) -> str:
    if code == "RENTAL_EQUIPMENT_NOT_IN_SHELF":
        return "Movelo a Estantería de Alquiler o elegí otro equipo."
    if code == "RENTAL_EQUIPMENT_MG_INACTIVE":
        return "Reactivá el MG desde Equipos o elegí otro equipo."
    return "Elegí un equipo disponible en Estantería de Alquiler."


def _rental_equipment_label(row: dict[str, Any]) -> str:
    """Cómo nombrar el equipo en un mensaje: por su OS si la tiene, si no por su MG o serie.

    Devuelve la frase con artículo ("La OS-00011" / "El equipo MG 7283") porque el género
    cambia según el caso; los mensajes que la usan están redactados para no depender de él.
    """
    order_label = service_order_reference(row.get("ingreso_id"))
    if order_label:
        return f"La {order_label}"
    identifier = _optional_text(row.get("equipment_internal_number")) or _optional_text(row.get("equipment_serial"))
    return f"El equipo {identifier}" if identifier else "El equipo"


def _rental_context_blocker_error(row: dict[str, Any], serial: str | None = None, extra: str = "") -> DeliveryOrderError | None:
    blocker = _rental_context_blocker(row)
    if not blocker:
        return None
    code, reason_os, reason_equipo = blocker
    order_label = service_order_reference(row.get("ingreso_id"))
    if order_label:
        subject = f"El NS {serial} está en la {order_label}, que {reason_os}" if serial else f"La {order_label} {reason_os}"
    else:
        subject = f"El NS {serial} {reason_equipo}" if serial else f"{_rental_equipment_label(row)} {reason_equipo}"
    return DeliveryOrderError(code, f"{subject}.{extra} {_rental_blocker_hint(code)}", status_code=409)


def _assert_rental_context_available(row: dict[str, Any], label: str) -> None:
    error = _rental_context_blocker_error(row, label)
    if error:
        raise error


def _serial_for_internal_number(identifier: str) -> str:
    """Traduce un MG a la serie. Bejerman sólo conoce la serie como partida, pero el
    operador suele tener a mano el MG, que es lo que está pegado en el equipo."""
    text = _optional_text(identifier)
    if not text:
        return ""
    with connection.cursor() as cur:
        cur.execute(
            r"""
            SELECT COALESCE(numero_serie, '')
              FROM devices
             WHERE UPPER(REGEXP_REPLACE(COALESCE(numero_interno, ''), '\s+', '', 'g'))
                 = UPPER(REGEXP_REPLACE(%s, '\s+', '', 'g'))
               AND COALESCE(numero_serie, '') <> ''
             LIMIT 2
            """,
            [text],
        )
        rows = cur.fetchall()
    # Con dos equipos bajo el mismo MG no se adivina: que el operador escriba la serie.
    return _optional_text(rows[0][0]) if len(rows) == 1 else ""


def _device_exists_by_serial(serial: str) -> bool:
    """Sólo para redactar el error: separa "esa serie no existe en NEXORA" de
    "existe como equipo pero nunca se le hizo un ingreso"."""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM devices
             WHERE LOWER(TRIM(COALESCE(numero_serie, ''))) = LOWER(TRIM(%s))
             LIMIT 1
            """,
            [serial],
        )
        return cur.fetchone() is not None


def _rental_serial_unlinked_error(serial: str) -> DeliveryOrderError:
    # Los equipos sin OS ya se resuelven por `_rental_device_contexts_by_serial`, así que
    # acá sólo cae el equipo que tiene OS pero que ninguna de las dos consultas puede
    # armar: le falta el cliente, que es lo único que ambas necesitan.
    if _device_exists_by_serial(serial):
        return DeliveryOrderError(
            "RENTAL_EQUIPMENT_WITHOUT_CUSTOMER",
            f"El NS {serial} está dado de alta como equipo pero no tiene cliente cargado. Completalo en Equipos.",
            status_code=409,
        )
    # Se llega acá con el stock de Bejerman ya verificado (resolve_rental_item_partidas
    # valida STL antes), así que el NS existe allá y no acá: el caso típico es el equipo
    # nuevo que alguien pasó a STL por transferencia sin darlo de alta en Equipos.
    return DeliveryOrderError(
        "RENTAL_EQUIPMENT_NOT_REGISTERED",
        (
            f"El NS {serial} tiene stock en {RENTAL_STOCK_DEPOSIT} pero no está dado de alta como equipo en NEXORA. "
            "Dalo de alta en Equipos con su MG."
        ),
        status_code=409,
    )


def _rental_serial_unavailable_error(serial: str, rows: list[dict[str, Any]]) -> DeliveryOrderError:
    others = f" Las otras {len(rows) - 1} OS con ese NS tampoco están disponibles." if len(rows) > 1 else ""
    # rows viene ordenado por OS descendente: el motivo que importa es el de la última.
    error = _rental_context_blocker_error(rows[0], serial, others) if rows else None
    return error or DeliveryOrderError(
        "RENTAL_EQUIPMENT_NOT_AVAILABLE",
        f"El NS {serial} no está disponible para alquilar.",
        status_code=409,
    )


def _resolve_rental_context_for_partida(partida: str, ingreso_id: int | None, device_id: int | None) -> dict[str, Any]:
    partida_text = _optional_text(partida)
    if not partida_text:
        raise DeliveryOrderError("RENTAL_SERIAL_REQUIRED", "Cada NS de alquiler debe tener número de serie", status_code=409)
    if ingreso_id:
        row = rental_equipment_context(int(ingreso_id))
        if not row:
            raise DeliveryOrderError("RENTAL_EQUIPMENT_NOT_FOUND", f"No se encontró la OS {ingreso_id}", status_code=404)
        if device_id and int(row.get("device_id") or 0) != int(device_id):
            raise DeliveryOrderError("RENTAL_EQUIPMENT_MISMATCH", f"La OS {ingreso_id} no corresponde al equipo seleccionado", status_code=409)
        serial = _optional_text(row.get("equipment_serial"))
        if not serial or serial.casefold() != partida_text.casefold():
            raise DeliveryOrderError("RENTAL_PARTIDA_MISMATCH", f"La partida {partida_text} no coincide con la serie de la OS {ingreso_id}", status_code=409)
        _assert_rental_context_available(row, partida_text)
        return row

    rows = _rental_equipment_contexts_by_serial(partida_text)
    if not rows:
        # Equipo nuevo dado de alta con su MG y sin OS: alquilar no exige ingreso.
        rows = _rental_device_contexts_by_serial(partida_text)
    if not rows:
        raise _rental_serial_unlinked_error(partida_text)
    available = next((row for row in rows if _rental_context_available(row)), None)
    if not available:
        raise _rental_serial_unavailable_error(partida_text, rows)
    if device_id and int(available.get("device_id") or 0) != int(device_id):
        raise DeliveryOrderError("RENTAL_EQUIPMENT_MISMATCH", f"El NS {partida_text} no corresponde al equipo seleccionado", status_code=409)
    return available


def rental_partida_from_identifier(order_id: str, identifier: Any, actor_user_id: int | None) -> dict[str, Any]:
    """Cuelga el equipo de un renglón de alquiler a partir de su NS o su MG.

    Es para el remito que Bejerman **ya** emitió: el stock salió de STL en ese momento, así
    que pedir stock ahora dejaría sin registrar justamente el caso para el que existe esta
    pantalla. Por eso no pasa por `resolve_rental_item_partidas`, que valida STL.
    """
    text = _optional_text(identifier)
    if not text:
        raise DeliveryOrderError("RENTAL_SERIAL_REQUIRED", "Indicá el N/S o el MG del equipo que salió", status_code=409)
    serial = text
    rows = _rental_equipment_contexts_by_serial(serial)
    if not rows:
        rows = _rental_device_contexts_by_serial(serial)
    if not rows:
        alternative = _serial_for_internal_number(text)
        if alternative:
            serial = alternative
            rows = _rental_equipment_contexts_by_serial(serial) or _rental_device_contexts_by_serial(serial)
    if not rows:
        raise _rental_serial_unlinked_error(serial)
    # Se toma el contexto disponible si lo hay; si no, el más reciente. No se aplica el
    # bloqueo de disponibilidad: el equipo ya salió, describimos un hecho pasado.
    context = next((row for row in rows if _rental_context_available(row)), rows[0])

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id
              FROM delivery_order_items
             WHERE order_id = %s
               AND NOT EXISTS (
                 SELECT 1 FROM delivery_order_item_partidas p WHERE p.order_item_id = delivery_order_items.id
               )
             ORDER BY sort_order, id
             LIMIT 1
            """,
            [order_id],
        )
        item_row = _one(cur)
        if not item_row:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_ITEM_NOT_FOUND",
                "Todos los renglones de la orden ya tienen su equipo cargado.",
                status_code=409,
            )
        item_id = item_row["id"]
        insert_item_partidas(
            cur,
            item_id,
            [
                {
                    "partida": _optional_text(context.get("equipment_serial")) or serial,
                    "assignedQuantity": Decimal("1"),
                    "stockDepositCode": RENTAL_STOCK_DEPOSIT,
                    "ingresoId": _optional_int(context.get("ingreso_id")),
                    "deviceId": _optional_int(context.get("device_id")),
                    "sortOrder": 0,
                }
            ],
        )
    create_event(
        order_id,
        actor_user_id,
        "delivery_order_item_partidas_updated",
        metadata={
            "itemId": item_id,
            "count": 1,
            "source": "manual_remito_lookup",
            "partida": _optional_text(context.get("equipment_serial")) or serial,
        },
    )
    return context


def rental_items_missing_equipment(order: dict[str, Any]) -> list[int]:
    """Renglones de alquiler sin equipo identificado, por número de renglón."""
    faltantes: list[int] = []
    for index, item in enumerate(order.get("items") or [], start=1):
        if not isinstance(item, dict):
            continue
        partidas = [row for row in (item.get("partidas") or []) if isinstance(row, dict) and _optional_text(row.get("partida"))]
        if not partidas and not _optional_text(item.get("partida")):
            faltantes.append(index)
    return faltantes


def _rental_duplicate_reservation(
    ingreso_id: int | None,
    order_id: str | None = None,
    *,
    device_id: int | None = None,
) -> str | None:
    """Si el equipo ya está tomado por otra orden de alquiler, devuelve su número.

    Se busca por OS cuando el equipo la tiene y por `device_id` cuando no (equipo dado de
    alta con su MG y sin ingreso). Se mantienen separados a propósito: buscar siempre por
    device haría saltar como "reservado" a equipos con varias OS que hoy conviven.
    """
    key_column = "ingreso_id" if ingreso_id else "device_id"
    key_value = ingreso_id if ingreso_id else device_id
    if not key_value:
        return None
    params: list[Any] = []
    items_exists_sql = "FALSE"
    if _has_table_column("delivery_order_items", key_column):
        items_exists_sql = f"""
        EXISTS (
          SELECT 1
            FROM delivery_order_items doi
           WHERE doi.order_id = o.id
             AND doi.{key_column} = %s
        )
        """
        params.append(key_value)
    partida_column_exists = _has_table_column("delivery_order_item_partidas", key_column)
    partida_exists_sql = "FALSE"
    if partida_column_exists:
        partida_exists_sql = f"""
        EXISTS (
          SELECT 1
            FROM delivery_order_items doi_partida
            JOIN delivery_order_item_partidas doip ON doip.order_item_id = doi_partida.id
           WHERE doi_partida.order_id = o.id
             AND doip.{key_column} = %s
        )
        """
        params.append(key_value)
    params.extend([order_id, order_id])
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT o.order_number
              FROM delivery_orders o
             WHERE ({items_exists_sql} OR {partida_exists_sql})
               AND o.delivery_type = 'rental'
               AND o.status NOT IN ('facturado', 'cancelado')
               AND (%s IS NULL OR o.id <> %s)
             ORDER BY o.created_at DESC
             LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
    return row[0] if row else None


def _stock_lot_available_quantity(lot: dict[str, Any]) -> Decimal:
    return _stock_quantity(lot.get("availableQuantity") or lot.get("stockAvailableQuantity") or lot.get("realQuantity"))


def resolve_rental_item_partidas(
    item: dict[str, Any],
    partidas: list[dict[str, Any]],
    *,
    company_key: str,
    actor_user_id: int | None,
    order_id: str | None = None,
) -> list[dict[str, Any]]:
    quantity = _decimal(item.get("quantity"), "0")
    normalized_partidas = _normalize_item_partidas(partidas or [], quantity)
    if not normalized_partidas:
        return []

    article_code = _optional_text(item.get("articleCode") or item.get("article_code"))
    if not article_code:
        raise DeliveryOrderError(
            "RENTAL_ARTICLE_REQUIRED",
            "Cada renglón de alquiler necesita un artículo Bejerman",
            status_code=409,
        )

    try:
        lots = _delivery_partida_stock_lots(
            article_code,
            RENTAL_STOCK_DEPOSIT,
            normalize_company_key(company_key or "SEPID"),
            actor_user_id,
            {},
        )
    except Exception as exc:
        raise DeliveryOrderError(
            "RENTAL_STL_STOCK_UNAVAILABLE",
            "No se pudo verificar stock Bejerman para los NS de alquiler.",
            status_code=502,
        ) from exc

    lots_by_partida = {
        (_optional_text(lot.get("partida")) or "").casefold(): lot
        for lot in lots
        if _optional_text(lot.get("partida"))
    }
    resolved: list[dict[str, Any]] = []
    seen_equipment: set[tuple[str, int | None]] = set()
    checked_at = timezone.now().isoformat()
    for row in normalized_partidas:
        partida_text = _optional_text(row.get("partida"))
        lot = lots_by_partida.get((partida_text or "").casefold())
        if not lot:
            # Puede haber escrito el MG en vez de la serie: para Bejerman la partida es
            # siempre la serie, así que se traduce antes de darlo por sin stock.
            serial_from_mg = _serial_for_internal_number(partida_text or "")
            if serial_from_mg:
                partida_text = serial_from_mg
                lot = lots_by_partida.get(serial_from_mg.casefold())
        if not lot:
            raise DeliveryOrderError(
                "RENTAL_STL_STOCK_REQUIRED",
                f"NS sin stock en STL: {partida_text}",
                status_code=409,
            )
        assigned_quantity = _decimal(row.get("assignedQuantity"), "0")
        available_quantity = _stock_lot_available_quantity(lot)
        if available_quantity < assigned_quantity:
            raise DeliveryOrderError(
                "RENTAL_STL_STOCK_REQUIRED",
                f"NS sin stock suficiente en STL: {partida_text}",
                status_code=409,
            )

        context = _resolve_rental_context_for_partida(
            partida_text or "",
            _optional_int(row.get("ingresoId") or row.get("ingreso_id")),
            _optional_int(row.get("deviceId") or row.get("device_id")),
        )
        ingreso_id = _optional_int(context.get("ingreso_id"))
        context_device_id = _optional_int(context.get("device_id"))
        # El equipo sin OS se identifica por su device: la clave de repetido y de reserva
        # tiene que seguir al equipo físico, tenga OS o no.
        equipment_key = ("ingreso", ingreso_id) if ingreso_id else ("device", context_device_id)
        if equipment_key in seen_equipment:
            raise DeliveryOrderError(
                "RENTAL_EQUIPMENT_DUPLICATED",
                f"{_rental_equipment_label(context)} aparece dos veces en la orden",
                status_code=409,
            )
        seen_equipment.add(equipment_key)
        duplicate = _rental_duplicate_reservation(ingreso_id, order_id, device_id=None if ingreso_id else context_device_id)
        if duplicate:
            raise DeliveryOrderError(
                "RENTAL_EQUIPMENT_RESERVED",
                f"NS ya reservado en la orden {duplicate}: {partida_text}",
                status_code=409,
            )

        resolved.append(
            {
                **row,
                "ingresoId": ingreso_id,
                "deviceId": context_device_id,
                "partida": _optional_text(lot.get("partida")) or partida_text,
                "partidaExpirationDate": lot.get("expirationDate") or row.get("partidaExpirationDate"),
                "stockDepositCode": RENTAL_STOCK_DEPOSIT,
                "stockAvailableQuantity": available_quantity,
                "stockCheckedAt": row.get("stockCheckedAt") or checked_at,
            }
        )
    return resolved


def validate_rental_delivery_items_ready(
    items: list[dict[str, Any]],
    *,
    order_id: str | None,
    company_key: str,
    actor_user_id: int | None,
) -> list[dict[str, Any]]:
    if not items:
        raise DeliveryOrderError("RENTAL_EQUIPMENT_REQUIRED", "La orden de alquiler necesita al menos un artículo")
    contexts: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not _optional_text(item.get("articleCode") or item.get("article_code")):
            raise DeliveryOrderError(
                "RENTAL_ARTICLE_REQUIRED",
                f"El renglón {index} de alquiler necesita un artículo Bejerman",
                status_code=409,
            )
        rows = _partida_rows_for_prepared_validation(item)
        if not rows:
            raise DeliveryOrderError(
                "DELIVERY_ORDER_PARTIDAS_REQUIRED",
                f"Faltan NS del renglón {index}.",
                status_code=409,
            )
        resolved_rows = resolve_rental_item_partidas(
            item,
            rows,
            company_key=company_key,
            actor_user_id=actor_user_id,
            order_id=order_id,
        )
        for row in resolved_rows:
            ingreso_id = _optional_int(row.get("ingresoId"))
            device_id = _optional_int(row.get("deviceId"))
            context = (
                rental_equipment_context(int(ingreso_id))
                if ingreso_id
                else (rental_device_context(int(device_id)) if device_id else None)
            )
            if context:
                contexts.append(context)
    return contexts


def resolve_present_rental_item_partidas(
    items: list[dict[str, Any]],
    *,
    order_id: str | None,
    company_key: str,
    actor_user_id: int | None,
) -> list[dict[str, Any]]:
    resolved_items: list[dict[str, Any]] = []
    for item in items:
        next_item = dict(item)
        if next_item.get("partidas"):
            next_item["partidas"] = resolve_rental_item_partidas(
                next_item,
                next_item.get("partidas") or [],
                company_key=company_key,
                actor_user_id=actor_user_id,
                order_id=order_id,
            )
            next_item["partida"] = None
        resolved_items.append(next_item)
    return resolved_items


def _rental_equipment_lines(item: dict[str, Any]) -> list[tuple[dict[str, Any], bool]]:
    """Los datos del equipo pueden venir en el propio renglón (un NS por renglón, formato viejo)
    o en sus filas de partida, que es lo que manda la UI desde que un renglón admite varios NS:
    ahí el renglón viaja con partida/depósito/stock en null y hay que mirar fila por fila.
    El booleano marca ese origen, porque una fila cargada a mano todavía no tiene OS asociada."""
    rows = [row for row in (item.get("partidas") or []) if isinstance(row, dict)]
    if not rows:
        return [(item, False)]
    return [
        (
            {
                "ingresoId": row.get("ingresoId") or row.get("ingreso_id"),
                "deviceId": row.get("deviceId") or row.get("device_id"),
                "partida": row.get("partida"),
                "quantity": row.get("assignedQuantity"),
                "stockDepositCode": row.get("stockDepositCode"),
                "stockAvailableQuantity": row.get("stockAvailableQuantity"),
            },
            True,
        )
        for row in rows
    ]


def validate_rental_delivery_items(items: list[dict[str, Any]], *, order_id: str | None = None) -> list[dict[str, Any]]:
    if not items:
        raise DeliveryOrderError("RENTAL_EQUIPMENT_REQUIRED", "La orden de alquiler necesita al menos un artículo")

    seen_equipment: set[tuple[str, int]] = set()
    contexts: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not _optional_text(item.get("articleCode") or item.get("article_code")):
            raise DeliveryOrderError(
                "RENTAL_ARTICLE_REQUIRED",
                f"El renglón {index} de alquiler necesita un artículo Bejerman",
                status_code=409,
            )

        for line, from_partida_row in _rental_equipment_lines(item):
            ingreso_id = line.get("ingresoId")
            device_id = line.get("deviceId")
            partida = _optional_text(line.get("partida"))
            if not ingreso_id and not device_id and not partida:
                continue
            # El equipo alcanza: un equipo nuevo dado de alta con su MG no tiene OS.
            if not device_id:
                if from_partida_row:
                    # NS tipeado a mano: no trae equipo y lo resuelve por serie
                    # resolve_rental_item_partidas, que corre inmediatamente después.
                    continue
                raise DeliveryOrderError(
                    "RENTAL_EQUIPMENT_REQUIRED",
                    f"El renglón {index} debe estar vinculado a un equipo de alquiler o quedar sin NS hasta preparación",
                    status_code=409,
                )
            equipment_key = ("ingreso", int(ingreso_id)) if ingreso_id else ("device", int(device_id))
            row = (
                rental_equipment_context(int(ingreso_id))
                if ingreso_id
                else rental_device_context(int(device_id))
            )
            equipment_label = _rental_equipment_label(row or {"ingreso_id": ingreso_id}) or f"El renglón {index}"
            if equipment_key in seen_equipment:
                raise DeliveryOrderError("RENTAL_EQUIPMENT_DUPLICATED", f"{equipment_label} aparece dos veces en la orden")
            seen_equipment.add(equipment_key)

            if abs(_decimal(line.get("quantity"), "0") - Decimal("1")) > PARTIDA_QUANTITY_TOLERANCE:
                raise DeliveryOrderError("RENTAL_QUANTITY_INVALID", f"El renglón {index} de alquiler debe tener cantidad 1")
            if (_optional_text(line.get("stockDepositCode")) or "").upper() != RENTAL_STOCK_DEPOSIT:
                raise DeliveryOrderError("RENTAL_STL_REQUIRED", f"El renglón {index} debe salir del depósito STL")
            if (line.get("stockAvailableQuantity") is None) or line["stockAvailableQuantity"] <= 0:
                raise DeliveryOrderError("RENTAL_STL_STOCK_REQUIRED", f"{equipment_label} no tiene stock disponible en STL", status_code=409)

            if not row:
                raise DeliveryOrderError(
                    "RENTAL_EQUIPMENT_NOT_FOUND",
                    f"No se encontró la OS {ingreso_id}" if ingreso_id else f"No se encontró el equipo {device_id}",
                    status_code=404,
                )
            if int(row.get("device_id") or 0) != int(device_id):
                raise DeliveryOrderError("RENTAL_EQUIPMENT_MISMATCH", f"{equipment_label} no corresponde al equipo seleccionado", status_code=409)
            blocker_error = _rental_context_blocker_error(row, partida or None)
            if blocker_error:
                raise blocker_error

            serial = _optional_text(row.get("equipment_serial"))
            if not serial:
                raise DeliveryOrderError("RENTAL_SERIAL_REQUIRED", f"{equipment_label} no tiene número de serie para usar como partida", status_code=409)
            if not partida or partida.casefold() != serial.casefold():
                raise DeliveryOrderError("RENTAL_PARTIDA_MISMATCH", f"La partida del renglón {index} debe ser la serie del equipo", status_code=409)

            duplicate = _rental_duplicate_reservation(
                int(ingreso_id) if ingreso_id else None,
                order_id,
                device_id=None if ingreso_id else int(device_id),
            )
            if duplicate:
                raise DeliveryOrderError(
                    "RENTAL_EQUIPMENT_RESERVED",
                    f"{equipment_label} ya figura en la orden {duplicate}",
                    status_code=409,
                )
            contexts.append(row)
    return contexts


def rental_header_from_contexts(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    if not contexts:
        return {}
    first = contexts[0]
    return {
        "ingresoId": first.get("ingreso_id"),
        "deviceId": first.get("device_id"),
        "equipmentModel": _equipment_label(first),
        "equipmentSerial": _optional_text(first.get("equipment_serial")),
        "equipmentInternalNumber": _optional_text(first.get("equipment_internal_number")),
        "sourceReference": (
            # El equipo sin OS no tiene número de orden de servicio: se lo nombra por su MG.
            (service_order_reference(first.get("ingreso_id")) or _optional_text(first.get("equipment_internal_number")))
            if len(contexts) == 1
            else f"Alquiler {len(contexts)} equipos"
        ),
    }


def _service_release_context(ingreso_id: int) -> dict[str, Any] | None:
    company_expr = "COALESCE(t.empresa_bejerman, 'SEPID')" if _has_table_column("ingresos", "empresa_bejerman") else "'SEPID'"
    owner_doc_expr = (
        "COALESCE(t.propietario_doc, '')" if _has_table_column("ingresos", "propietario_doc") else "''"
    )
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id AS ingreso_id,
                   t.device_id,
                   t.estado,
                   {company_expr} AS company_key,
                   COALESCE(t.resolucion, '') AS resolucion,
                   {owner_doc_expr} AS propietario_doc,
                   COALESCE(t.equipo_variante, '') AS equipo_variante,
                   COALESCE(t.fecha_servicio, t.fecha_ingreso, t.fecha_creacion) AS order_date,
                   c.id AS customer_id,
                   COALESCE(c.razon_social, '') AS customer_name,
                   COALESCE(c.cod_empresa, '') AS bejerman_customer_code,
                   COALESCE(d.numero_serie, '') AS equipment_serial,
                   COALESCE(d.numero_interno, '') AS equipment_internal_number,
                   d.model_id,
                   COALESCE(d.variante, '') AS device_variante,
                   COALESCE(m.nombre, '') AS modelo,
                   COALESCE(m.tipo_equipo, '') AS tipo_equipo,
                   COALESCE(m.variante, '') AS modelo_variante,
                   COALESCE(b.nombre, '') AS marca
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN marcas b ON b.id = d.marca_id
             WHERE t.id = %s
            """,
            [ingreso_id],
        )
        row = _one(cur)
    return row


def _article_mapping_for_service_release(row: dict[str, Any]) -> dict[str, Any] | None:
    model_id = row.get("model_id")
    if not model_id:
        return None
    from .bejerman_companies import require_company
    from .bejerman_sync import normalize_article_variant

    variant = (
        _optional_text(row.get("equipo_variante"))
        or _optional_text(row.get("device_variante"))
        or _optional_text(row.get("modelo_variante"))
        or ""
    )
    try:
        resolved_company = require_company(row.get("company_key") or "SEPID").key
    except ValueError:
        resolved_company = "SEPID"
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT article_code, article_description
                  FROM bejerman_article_mappings
                 WHERE model_id = %s
                   AND variante_norm = %s
                   AND company_key = %s
                 ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END,
                          confirmed_at DESC NULLS LAST,
                          updated_at DESC
                 LIMIT 1
                """,
                [model_id, normalize_article_variant(variant), resolved_company],
            )
            return _one(cur)
    except DatabaseError:
        return None


def _service_release_equipment_label(row: dict[str, Any]) -> str:
    model_text = " ".join(
        part
        for part in [
            _optional_text(row.get("modelo")),
            _optional_text(row.get("equipo_variante")) or _optional_text(row.get("device_variante")) or _optional_text(row.get("modelo_variante")),
        ]
        if part
    )
    return " | ".join(
        part
        for part in [
            _optional_text(row.get("tipo_equipo")),
            _optional_text(row.get("marca")),
            model_text,
        ]
        if part
    ) or "Equipo"


def _service_release_equipment_detail(row: dict[str, Any]) -> str:
    parts = [_service_release_equipment_label(row)]
    internal_number = _optional_text(row.get("equipment_internal_number"))
    if internal_number:
        parts.append(f"N° interno (MG) {internal_number}")
    return " - ".join(part for part in parts if part) or "Equipo"


def _service_release_raw_pedido(row: dict[str, Any]) -> str:
    return _optional_text(row.get("resolucion")) or "reparado"


def _service_release_item_payload(row: dict[str, Any], mapping: dict[str, Any] | None) -> dict[str, Any]:
    equipment_detail = _service_release_equipment_detail(row)
    return {
        "ingresoId": row.get("ingreso_id"),
        "deviceId": row.get("device_id"),
        "articleCode": _optional_text((mapping or {}).get("article_code")) or "",
        "articleName": equipment_detail,
        "description": equipment_detail,
        "quantity": 1,
        "sourceText": equipment_detail,
        "partida": _optional_text(row.get("equipment_serial")) or "",
    }


def _service_release_order_payload(
    ingreso_id: int,
    row: dict[str, Any],
    mapping: dict[str, Any] | None,
) -> dict[str, Any]:
    equipment_label = _service_release_equipment_label(row)
    return {
        "customerId": row.get("customer_id"),
        "customerName": row.get("customer_name") or "",
        "bejermanCustomerCode": bejerman_customer_code_for(
            row.get("customer_name"), _optional_text(row.get("bejerman_customer_code"))
        ),
        "deliveryType": "service_release",
        "companyKey": normalize_company_key(row.get("company_key") or "SEPID"),
        "sourceSystem": "nexora",
        "sourceExternalId": str(ingreso_id),
        "sourceReference": service_order_reference(ingreso_id),
        "ingresoId": ingreso_id,
        "deviceId": row.get("device_id"),
        "equipmentModel": equipment_label,
        "equipmentSerial": _optional_text(row.get("equipment_serial")),
        "equipmentInternalNumber": _optional_text(row.get("equipment_internal_number")),
        "sellerName": "Servicio Técnico",
        "orderDate": _date_for_order(row.get("order_date")),
        "operationCompanyLabel": "REPARACION",
        "rawPedido": _service_release_raw_pedido(row),
        "priority": "normal",
        "status": "armado_pendiente_entrega",
        "items": [_service_release_item_payload(row, mapping)],
    }


def _existing_service_release_order_id(ingreso_id: int) -> str | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT o.id
              FROM delivery_orders o
             WHERE o.delivery_type = 'service_release'
               AND o.status <> 'cancelado'
               AND (
                    o.ingreso_id = %s
                    OR (o.source_system = 'nexora' AND o.source_external_id = %s)
                    OR EXISTS (
                        SELECT 1
                          FROM delivery_order_items doi
                         WHERE doi.order_id = o.id
                           AND doi.ingreso_id = %s
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM delivery_order_item_partidas doip
                          JOIN delivery_order_items doi ON doi.id = doip.order_item_id
                         WHERE doi.order_id = o.id
                           AND doip.ingreso_id = %s
                    )
                   )
             ORDER BY
                   CASE
                     WHEN o.ingreso_id = %s THEN 0
                     WHEN o.source_system = 'nexora' AND o.source_external_id = %s THEN 1
                     ELSE 2
                   END,
                   o.created_at DESC NULLS LAST,
                   o.id DESC
             LIMIT 1
            """,
            [ingreso_id, str(ingreso_id), ingreso_id, ingreso_id, ingreso_id, str(ingreso_id)],
        )
        row = cur.fetchone()
    return row[0] if row else None


def _find_compatible_open_service_release_order_id(row: dict[str, Any]) -> str | None:
    customer_name = _clean_text(row.get("customer_name"))
    # Sin normalizar, la fila 'Particular' entra con su cod_empresa local ('PTL'), que no existe en
    # Bejerman: la rama por codigo no matchearia nunca y la de customer_id agruparia a TODOS los
    # particulares entre si, porque comparten la misma fila de customers.
    customer_code = bejerman_customer_code_for(customer_name, row.get("bejerman_customer_code"))
    customer_id = _optional_int(row.get("customer_id"))
    occasional = is_occasional_customer_name(customer_name)
    propietario_doc = re.sub(r"\D", "", _clean_text(row.get("propietario_doc")))
    if occasional and not propietario_doc:
        # Sin documento no hay forma de distinguir un particular de otro: no se agrupa.
        return None
    if not customer_code and not customer_id:
        return None
    company_key = normalize_company_key(row.get("company_key") or "SEPID")
    raw_pedido = _service_release_raw_pedido(row)
    params: list[Any] = [
        company_key,
        raw_pedido,
        service_release_max_items_per_order(),
        customer_code,
        customer_code,
        customer_code,
        customer_id,
        customer_id,
    ]
    # Dos equipos del mismo particular si van juntos; los de dos particulares distintos no,
    # porque el remito viaja con un solo propietario como cliente ocasional.
    occasional_sql = ""
    if occasional:
        occasional_sql = """
               AND EXISTS (
                    SELECT 1
                      FROM ingresos i
                     WHERE i.id = o.ingreso_id
                       AND regexp_replace(COALESCE(i.propietario_doc, ''), '[^0-9]', '', 'g') = %s
                   )
        """
        params.append(propietario_doc)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT o.id
              FROM delivery_orders o
             WHERE o.delivery_type = 'service_release'
               AND o.status IN ('pendiente_armado', 'armado_pendiente_entrega')
               AND NULLIF(BTRIM(COALESCE(o.remito_number, '')), '') IS NULL
               AND o.bejerman_remito_group_id IS NULL
               AND COALESCE(o.company_key, 'SEPID') = %s
               AND LOWER(BTRIM(COALESCE(o.raw_pedido, ''))) = LOWER(BTRIM(%s))
               AND (
                    SELECT COUNT(*)
                      FROM delivery_order_items doi
                     WHERE doi.order_id = o.id
                   ) < %s
               AND (
                    (%s <> '' AND UPPER(BTRIM(COALESCE(o.bejerman_customer_code, ''))) = UPPER(BTRIM(%s)))
                    OR (%s = '' AND %s IS NOT NULL AND o.customer_id = %s)
                   )
               {occasional_sql}
             ORDER BY o.created_at ASC NULLS LAST, o.id ASC
             LIMIT 1
             FOR UPDATE
            """,
            params,
        )
        found = cur.fetchone()
    return found[0] if found else None


def _upsert_service_release_order_item(
    cur,
    order_id: str,
    row: dict[str, Any],
    mapping: dict[str, Any] | None,
) -> str:
    item = _service_release_item_payload(row, mapping)
    ingreso_id = _optional_int(item.get("ingresoId"))
    article_code = _optional_text(item.get("articleCode"))
    partida = _optional_text(item.get("partida"))
    cur.execute(
        """
        SELECT doi.id
          FROM delivery_order_items doi
          JOIN delivery_orders o ON o.id = doi.order_id
         WHERE doi.order_id = %s
           AND (
                doi.ingreso_id = %s
                OR EXISTS (
                    SELECT 1
                      FROM delivery_order_item_partidas doip
                     WHERE doip.order_item_id = doi.id
                       AND doip.ingreso_id = %s
                )
                OR (doi.ingreso_id IS NULL AND o.ingreso_id = %s)
               )
         ORDER BY
               CASE
                 WHEN doi.ingreso_id = %s THEN 0
                 WHEN doi.ingreso_id IS NULL THEN 1
                 ELSE 2
               END,
               doi.sort_order ASC,
               doi.created_at ASC
         LIMIT 1
        """,
        [order_id, ingreso_id, ingreso_id, ingreso_id, ingreso_id],
    )
    existing = cur.fetchone()
    if existing:
        item_id = existing[0]
        cur.execute(
            """
            UPDATE delivery_order_items
               SET ingreso_id = %s,
                   device_id = %s,
                   article_code = COALESCE(NULLIF(%s, ''), article_code),
                   article_name = %s,
                   description = %s,
                   quantity = 1,
                   unit_price = NULL,
                   discount_percent = 0,
                   source_text = %s,
                   partida = %s,
                   partida_expiration_date = NULL,
                   stock_deposit_code = NULL,
                   stock_available_quantity = NULL,
                   stock_checked_at = NULL
             WHERE id = %s
            """,
            [
                ingreso_id,
                _optional_int(item.get("deviceId")),
                article_code,
                item["articleName"],
                item["description"],
                item["sourceText"],
                partida,
                item_id,
            ],
        )
        return item_id

    cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM delivery_order_items WHERE order_id = %s", [order_id])
    sort_order = int(cur.fetchone()[0] or 0)
    item_id = f"doi-{uuid4()}"
    cur.execute(
        """
        INSERT INTO delivery_order_items (
          id, order_id, ingreso_id, device_id, article_code, article_name, article_requires_partida,
          description, quantity, unit_price, price_currency, discount_percent, source_text, partida,
          partida_expiration_date, stock_deposit_code, stock_available_quantity, stock_checked_at, sort_order
        )
        VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, 1, NULL, 'ARS', 0, %s, %s, NULL, NULL, NULL, NULL, %s)
        """,
        [
            item_id,
            order_id,
            ingreso_id,
            _optional_int(item.get("deviceId")),
            article_code,
            item["articleName"],
            item["description"],
            item["sourceText"],
            partida,
            sort_order,
        ],
    )
    return item_id


@transaction.atomic
def _refresh_service_release_order(order_id: str, row: dict[str, Any], mapping: dict[str, Any] | None, actor_user_id: int | None) -> dict[str, Any]:
    equipment_label = _service_release_equipment_label(row)
    equipment_serial = _optional_text(row.get("equipment_serial"))
    ingreso_id = _optional_int(row.get("ingreso_id"))
    with connection.cursor() as cur:
        cur.execute("SELECT ingreso_id, source_system, source_external_id FROM delivery_orders WHERE id = %s FOR UPDATE", [order_id])
        current = _one(cur)
        current_ingreso_id = _optional_int((current or {}).get("ingreso_id"))
        current_source_external_id = _optional_text((current or {}).get("source_external_id"))
        should_refresh_header = (
            current_ingreso_id in (None, ingreso_id)
            or (
                _clean_text((current or {}).get("source_system")).lower() == "nexora"
                and current_source_external_id == str(ingreso_id)
            )
        )
        if should_refresh_header:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET customer_id = %s,
                       order_number = %s,
                       bejerman_customer_code = %s,
                       customer_name = %s,
                       company_key = %s,
                       source_reference = %s,
                       ingreso_id = %s,
                       device_id = %s,
                       equipment_model = %s,
                       equipment_serial = %s,
                       equipment_internal_number = %s,
                       order_date = %s,
                       raw_pedido = %s,
                       status = CASE WHEN status = 'pendiente_armado' THEN 'armado_pendiente_entrega' ELSE status END
                 WHERE id = %s
                   AND status NOT IN ('entregado_no_facturable','facturado','cancelado')
                   AND remito_number IS NULL
                """,
                [
                    row.get("customer_id"),
                    service_order_reference(row.get("ingreso_id")),
                    _optional_text(row.get("bejerman_customer_code")),
                    row.get("customer_name") or "",
                    normalize_company_key(row.get("company_key") or "SEPID"),
                    service_order_reference(row.get("ingreso_id")),
                    row.get("ingreso_id"),
                    row.get("device_id"),
                    equipment_label,
                    equipment_serial,
                    _optional_text(row.get("equipment_internal_number")),
                    _date_for_order(row.get("order_date")),
                    _service_release_raw_pedido(row),
                    order_id,
                ],
            )
        else:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET status = CASE WHEN status = 'pendiente_armado' THEN 'armado_pendiente_entrega' ELSE status END
                 WHERE id = %s
                   AND status NOT IN ('entregado_no_facturable','facturado','cancelado')
                   AND remito_number IS NULL
                """,
                [order_id],
            )
        _upsert_service_release_order_item(cur, order_id, row, mapping)
    create_event(order_id, actor_user_id, "service_release_order_synced", metadata={"ingresoId": row.get("ingreso_id")})
    return get_delivery_order(order_id)


@transaction.atomic
def ensure_service_release_order_for_ingreso(ingreso_id: int, actor_user_id: int | None = None) -> dict[str, Any] | None:
    row = _service_release_context(ingreso_id)
    if not row:
        return None
    if _clean_text(row.get("estado")).lower() not in {"liberado", "vendido_pendiente_entrega"}:
        return None

    mapping = _article_mapping_for_service_release(row)
    with connection.cursor() as cur:
        cur.execute("LOCK TABLE delivery_orders IN SHARE ROW EXCLUSIVE MODE")
    existing_id = _existing_service_release_order_id(ingreso_id)
    if existing_id:
        return _refresh_service_release_order(existing_id, row, mapping, actor_user_id)

    compatible_id = _find_compatible_open_service_release_order_id(row)
    if compatible_id:
        return _refresh_service_release_order(compatible_id, row, mapping, actor_user_id)

    payload = _service_release_order_payload(ingreso_id, row, mapping)
    try:
        return create_delivery_order(payload, actor_user_id)
    except IntegrityError:
        existing_id = _existing_service_release_order_id(ingreso_id)
        if existing_id:
            return _refresh_service_release_order(existing_id, row, mapping, actor_user_id)
        raise


def get_user_seller_code(user_id: int | None) -> str | None:
    if not user_id:
        return None
    with connection.cursor() as cur:
        cur.execute("SELECT bejerman_seller_code FROM users WHERE id = %s", [user_id])
        row = cur.fetchone()
    return normalize_seller_code(row[0]) if row else None


def remito_profile_for_type(delivery_type: str) -> RemitoProfile:
    delivery_type = normalize_delivery_type(delivery_type)
    if delivery_type == "rental":
        return RemitoProfile("RTA", "00004", "ALQ", "STL")
    if delivery_type == "demo":
        return RemitoProfile("RTN", "00004", "DEMO", "VAL")
    if delivery_type == "service_release":
        return RemitoProfile("RSS", "00004", "REP", "STC")
    return RemitoProfile("RT", "00004", "MC", "VAL")
