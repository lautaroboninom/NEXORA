from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from django.conf import settings
from django.core.mail import send_mail
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from .notifications import active_users_for_notification, emit_notification, notification_email_recipients_for_users


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
DELIVERY_TYPE_LABELS = {
    "sale": "Venta",
    "service_release": "Servicio técnico",
    "rental": "Alquiler",
    "demo": "Demo",
}
BILLABLE_REMITO_TYPES = {"RT"}
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


def _validate_order_partidas_ready(order: dict[str, Any]) -> None:
    delivery_type = normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type"))
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
    from .bejerman_sdk import (
        BejermanSDKClient,
        build_article_stock_lots,
        build_partida_rows,
        build_stock_rows,
    )

    company_cache = cache.setdefault(company_key, {})
    if "partida_rows" not in company_cache:
        client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
        company_cache["client"] = client
        company_cache["partida_rows"] = build_partida_rows(client.obtener_partidas())
    client = company_cache["client"]
    stock_rows_by_deposit = company_cache.setdefault("stock_rows_by_deposit", {})
    if deposit_code not in stock_rows_by_deposit:
        stock_rows_by_deposit[deposit_code] = build_stock_rows(client.obtener_stock_deposito(deposit_code))
    return build_article_stock_lots(
        stock_rows_by_deposit[deposit_code],
        company_cache["partida_rows"],
        article_code,
        deposit_code,
    )


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
        service_reference = service_order_reference(row.get("ingreso_id")) or delivery_source_reference(
            row.get("source_reference"),
            row.get("delivery_type"),
            row.get("ingreso_id"),
        )
        if service_reference:
            return service_reference
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
    return bool(row and row[0] in {"admin", "supervisor", "ventas"})


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


def serialize_order(row: dict[str, Any]) -> dict[str, Any]:
    order_id = str(row.get("id") or "")
    has_remito = bool(_optional_text(row.get("remito_number")))
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
        "remitoPdfUrl": _delivery_order_remito_pdf_path(order_id) if has_remito else "",
        "remitoPrintUrl": _delivery_order_remito_print_path(order_id) if has_remito else "",
        "bejermanRemitoGroupId": row.get("bejerman_remito_group_id") or "",
        "remitoLocation": row.get("remito_location") or "",
        "invoiceNumber": row.get("invoice_number") or "",
        "ingresoId": row.get("ingreso_id"),
        "deviceId": row.get("device_id"),
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
) -> bool | None:
    from .bejerman_sdk import BejermanSDKClient, build_article_filters, build_articles_result

    code_key = _article_code_key(article_code)
    lookup_key = f"{company_key}|{code_key}"
    if lookup_key in cache:
        return cache[lookup_key]

    company_cache = cache.setdefault(f"client:{company_key}", {})
    client = company_cache.get("client")
    if client is None:
        client = BejermanSDKClient(
            company_key=company_key,
            actor_user_id=actor_user_id,
            allow_system_credentials=True,
        )
        company_cache["client"] = client

    data = build_articles_result(
        client.list_articulos(build_article_filters(article_code, field="code"), 50),
        {"search": article_code, "limit": 50},
    )
    for article in data.get("items") or []:
        if _article_code_key(article.get("code")) == code_key:
            result = _nullable_bool(article.get("requiresPartida"))
            cache[lookup_key] = result
            return result
    cache[lookup_key] = None
    return None


def refresh_delivery_order_article_partida_flags(
    order: dict[str, Any],
    actor_user_id: int | None,
    *,
    strict: bool = False,
    action: str = "continuar",
) -> dict[str, Any]:
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
            requires_partida = _lookup_article_requires_partida(article_code, company_key, actor_user_id, cache)
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
    generated = row.get("status") == "generated"
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
        "status": row.get("status") or "",
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


def list_remito_history(limit: Any = 20) -> list[dict[str, Any]]:
    try:
        safe_limit = max(1, min(100, int(limit or 20)))
    except (TypeError, ValueError):
        safe_limit = 20
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
            [safe_limit],
        )
        group_rows = _rows(cur)
    return list(_serialize_remito_groups(group_rows).values())


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
    try:
        return int(
            send_mail(
                subject,
                body,
                getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipients,
                fail_silently=True,
            )
            or 0
        )
    except Exception:
        logger.exception("delivery_order_billing_email_failed", extra={"order_id": order.get("id")})
        return 0


def _delivery_order_notification(order_id: str, actor_user_id: int | None, kind: str) -> int:
    try:
        order = get_delivery_order(order_id, include_events=False)
        totals = delivery_order_billing_totals(order)
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
            dedupe_key=f"{notification_key}:sales_order:{order_id}",
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
            push=kind == "created",
        )
        email_sent = 0
        if inserted and kind == "remito_ready":
            email_users = active_users_for_notification(notification_key, roles=roles, channel="email", require_email=True)
            email_sent = _send_delivery_order_billing_email(order, email_users)
        create_event(
            order_id,
            actor_user_id,
            "notification_sent" if inserted else "notification_skipped",
            metadata={"kind": kind, "roles": roles, "recipients": inserted, "emailSent": email_sent},
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
    pending_billing_requested = normalized_statuses == ["entregado_pendiente_facturacion"] or pending_billing_filter
    if pending_billing_filter:
        where.append("NULLIF(BTRIM(COALESCE(o.remito_number, '')), '') IS NOT NULL")
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
    if pending_billing_requested:
        where.append("NULLIF(BTRIM(COALESCE(o.invoice_number, '')), '') IS NULL")

    limit = max(1, min(200, int(filters.get("limit") or 80)))
    offset = max(0, int(filters.get("offset") or 0))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_sql = (
        """
              COALESCE(g.generated_at, o.order_date::timestamptz, g.created_at, o.delivered_at, o.created_at) ASC,
              o.order_date ASC NULLS LAST,
              o.created_at ASC,
              o.id ASC
        """
        if pending_billing_requested
        else """
              o.created_at DESC,
              o.id DESC
        """
    )
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
    remito_groups_by_id = load_remito_groups_by_id([row.get("bejerman_remito_group_id") for row in rows])
    out = []
    for row in rows:
        row["items"] = items_by_order.get(row["id"], [])
        row["bejerman_remito_group"] = remito_groups_by_id.get(row.get("bejerman_remito_group_id"))
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


def get_delivery_order(
    order_id: str,
    *,
    include_events: bool = True,
    for_update: bool = False,
    actor_user_id: int | None = None,
    refresh_article_flags: bool = False,
) -> dict[str, Any]:
    suffix = "FOR UPDATE" if for_update else ""
    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM delivery_orders WHERE id = %s {suffix}", [order_id])
        row = _one(cur)
        if not row:
            raise DeliveryOrderError("DELIVERY_ORDER_NOT_FOUND", "Orden de entrega no encontrada", status_code=404)

    items_by_order = load_items_by_order([order_id])
    row["items"] = items_by_order.get(order_id, [])
    row["bejerman_remito_group"] = load_remito_groups_by_id([row.get("bejerman_remito_group_id")]).get(row.get("bejerman_remito_group_id"))
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
        order = refresh_delivery_order_article_partida_flags(order, actor_user_id, strict=False)
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


@transaction.atomic
def update_delivery_order(order_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    _assert_order_editable(current)

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

    create_event(
        order_id,
        actor_user_id,
        "delivery_order_updated",
        metadata={"itemCount": len(items), "previousStatus": current.get("status")},
    )
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
    current = refresh_delivery_order_article_partida_flags(current, actor_user_id, strict=True, action="preparar")
    _validate_order_partidas_ready(current)
    if current.get("deliveryType") == "rental":
        validate_rental_delivery_items_ready(
            current.get("items") or [],
            order_id=order_id,
            company_key=current.get("companyKey") or "SEPID",
            actor_user_id=actor_user_id,
        )
    next_status = "armado_pendiente_entrega"
    effective_remito = _optional_text(remito_number)
    manual_remito_metadata: dict[str, Any] = {}
    if effective_remito and not _optional_text(current.get("remitoNumber")) and not _optional_text(current.get("bejermanRemitoGroupId")):
        from .bejerman_delivery import register_delivery_order_manual_remito

        manual_remito_metadata = register_delivery_order_manual_remito(current, actor_user_id, effective_remito)
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
    effective_remito = _optional_text(remito_number) or _optional_text(current.get("remitoNumber"))
    if not effective_remito:
        raise DeliveryOrderError("REMITO_REQUIRED", "Para entregar la orden se necesita un remito")
    if _optional_text(current.get("invoiceNumber")):
        next_status = "facturado"
    elif _billing_suppressed(order_id):
        next_status = "entregado_no_facturable"
    else:
        next_status = remito_status(effective_remito)
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
    if next_status == "entregado_pendiente_facturacion":
        notify_delivery_order_remito_ready(order_id, actor_user_id)
    return get_delivery_order(order_id)


@transaction.atomic
def mark_invoiced(order_id: str, actor_user_id: int | None, invoice_number: str) -> dict[str, Any]:
    invoice = _optional_text(invoice_number)
    if not invoice:
        raise DeliveryOrderError("INVOICE_REQUIRED", "Número de factura requerido")
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if not order_requires_billing(current) or _optional_text(current.get("invoiceNumber")) or _billing_suppressed(order_id):
        raise DeliveryOrderError("INVALID_INVOICE_STATE", "Solo se factura una orden con remito facturable pendiente", status_code=409)
    next_status = "facturado" if current["status"] == "entregado_pendiente_facturacion" or current.get("deliveredAt") else current["status"]
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET status = %s,
                invoice_number = %s,
                invoiced_by_user_id = %s,
                invoiced_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            [next_status, invoice, actor_user_id, order_id],
        )
    synced_ingreso_ids = _sync_service_release_ingreso_invoice(current, invoice)
    event_metadata = {"invoiceNumber": invoice}
    if synced_ingreso_ids:
        if len(synced_ingreso_ids) == 1:
            event_metadata["ingresoId"] = synced_ingreso_ids[0]
        event_metadata["ingresoIds"] = synced_ingreso_ids
    create_event(order_id, actor_user_id, "delivery_order_invoiced", metadata=event_metadata)
    return get_delivery_order(order_id)


@transaction.atomic
def mark_not_billable(order_id: str, actor_user_id: int | None, note: str | None = None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
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
            SET status = %s
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


def _article_mapping_for_equipment(row: dict[str, Any]) -> dict[str, Any] | None:
    model_id = row.get("model_id")
    if not model_id:
        return None
    from .bejerman_sync import normalize_article_variant

    variant = (
        _optional_text(row.get("equipo_variante"))
        or _optional_text(row.get("device_variante"))
        or _optional_text(row.get("modelo_variante"))
        or ""
    )
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT article_code, article_description
                  FROM bejerman_article_mappings
                 WHERE model_id = %s
                   AND variante_norm = %s
                 ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END,
                          confirmed_at DESC NULLS LAST,
                          updated_at DESC
                 LIMIT 1
                """,
                [model_id, normalize_article_variant(variant)],
            )
            return _one(cur)
    except DatabaseError:
        return None


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
        "sourceReference": service_order_reference(row.get("ingreso_id")),
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


def _rental_context_available(row: dict[str, Any]) -> bool:
    estado = _clean_text(row.get("estado")).lower()
    return (
        estado not in RENTAL_UNAVAILABLE_STATES
        and not bool(row.get("alquilado"))
        and is_rental_shelf_location_name(row.get("ubicacion_nombre"))
    )


def _assert_rental_context_available(row: dict[str, Any], label: str) -> None:
    estado = _clean_text(row.get("estado")).lower()
    if estado in RENTAL_UNAVAILABLE_STATES or bool(row.get("alquilado")):
        raise DeliveryOrderError(
            "RENTAL_EQUIPMENT_NOT_AVAILABLE",
            f"NS no vinculado a una OS disponible: {label}",
            status_code=409,
        )
    if not is_rental_shelf_location_name(row.get("ubicacion_nombre")):
        raise DeliveryOrderError(
            "RENTAL_EQUIPMENT_NOT_IN_SHELF",
            f"El NS {label} no está en Estantería de Alquiler",
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
        raise DeliveryOrderError(
            "RENTAL_EQUIPMENT_NOT_FOUND",
            f"NS no vinculado a una OS disponible: {partida_text}",
            status_code=409,
        )
    available = next((row for row in rows if _rental_context_available(row)), None)
    if not available:
        raise DeliveryOrderError(
            "RENTAL_EQUIPMENT_NOT_AVAILABLE",
            f"NS no vinculado a una OS disponible: {partida_text}",
            status_code=409,
        )
    if device_id and int(available.get("device_id") or 0) != int(device_id):
        raise DeliveryOrderError("RENTAL_EQUIPMENT_MISMATCH", f"El NS {partida_text} no corresponde al equipo seleccionado", status_code=409)
    return available


def _rental_duplicate_reservation(ingreso_id: int, order_id: str | None = None) -> str | None:
    partida_column_exists = _has_table_column("delivery_order_item_partidas", "ingreso_id")
    partida_exists_sql = "FALSE"
    params: list[Any] = [ingreso_id]
    if partida_column_exists:
        partida_exists_sql = """
        EXISTS (
          SELECT 1
            FROM delivery_order_items doi_partida
            JOIN delivery_order_item_partidas doip ON doip.order_item_id = doi_partida.id
           WHERE doi_partida.order_id = o.id
             AND doip.ingreso_id = %s
        )
        """
        params.append(ingreso_id)
    params.extend([order_id, order_id])
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT o.order_number
              FROM delivery_orders o
             WHERE (
                    EXISTS (
                      SELECT 1
                        FROM delivery_order_items doi
                       WHERE doi.order_id = o.id
                         AND doi.ingreso_id = %s
                    )
                    OR {partida_exists_sql}
                   )
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
    seen_ingresos: set[int] = set()
    checked_at = timezone.now().isoformat()
    for row in normalized_partidas:
        partida_text = _optional_text(row.get("partida"))
        lot = lots_by_partida.get((partida_text or "").casefold())
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
        ingreso_id = int(context.get("ingreso_id"))
        if ingreso_id in seen_ingresos:
            raise DeliveryOrderError(
                "RENTAL_EQUIPMENT_DUPLICATED",
                f"La OS {ingreso_id} está repetida en la orden",
                status_code=409,
            )
        seen_ingresos.add(ingreso_id)
        duplicate = _rental_duplicate_reservation(ingreso_id, order_id)
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
                "deviceId": int(context.get("device_id")),
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
            context = rental_equipment_context(int(row["ingresoId"]))
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


def validate_rental_delivery_items(items: list[dict[str, Any]], *, order_id: str | None = None) -> list[dict[str, Any]]:
    if not items:
        raise DeliveryOrderError("RENTAL_EQUIPMENT_REQUIRED", "La orden de alquiler necesita al menos un artículo")

    seen_ingresos: set[int] = set()
    contexts: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not _optional_text(item.get("articleCode") or item.get("article_code")):
            raise DeliveryOrderError(
                "RENTAL_ARTICLE_REQUIRED",
                f"El renglón {index} de alquiler necesita un artículo Bejerman",
                status_code=409,
            )

        ingreso_id = item.get("ingresoId")
        device_id = item.get("deviceId")
        partida = _optional_text(item.get("partida"))
        if not ingreso_id and not device_id and not partida:
            continue
        if not ingreso_id or not device_id:
            raise DeliveryOrderError(
                "RENTAL_EQUIPMENT_REQUIRED",
                f"El renglón {index} debe estar vinculado a un equipo de alquiler o quedar sin NS hasta preparación",
                status_code=409,
            )
        if ingreso_id in seen_ingresos:
            raise DeliveryOrderError("RENTAL_EQUIPMENT_DUPLICATED", f"La OS {ingreso_id} está repetida en la orden")
        seen_ingresos.add(ingreso_id)

        if abs(_decimal(item.get("quantity"), "0") - Decimal("1")) > PARTIDA_QUANTITY_TOLERANCE:
            raise DeliveryOrderError("RENTAL_QUANTITY_INVALID", f"El renglón {index} de alquiler debe tener cantidad 1")
        if (_optional_text(item.get("stockDepositCode")) or "").upper() != RENTAL_STOCK_DEPOSIT:
            raise DeliveryOrderError("RENTAL_STL_REQUIRED", f"El renglón {index} debe salir del depósito STL")
        if (item.get("stockAvailableQuantity") is None) or item["stockAvailableQuantity"] <= 0:
            raise DeliveryOrderError("RENTAL_STL_STOCK_REQUIRED", f"La OS {ingreso_id} no tiene stock disponible en STL", status_code=409)

        row = rental_equipment_context(int(ingreso_id))
        if not row:
            raise DeliveryOrderError("RENTAL_EQUIPMENT_NOT_FOUND", f"No se encontró la OS {ingreso_id}", status_code=404)
        if int(row.get("device_id") or 0) != int(device_id):
            raise DeliveryOrderError("RENTAL_EQUIPMENT_MISMATCH", f"La OS {ingreso_id} no corresponde al equipo seleccionado", status_code=409)
        estado = _clean_text(row.get("estado")).lower()
        if estado in RENTAL_UNAVAILABLE_STATES or bool(row.get("alquilado")):
            raise DeliveryOrderError("RENTAL_EQUIPMENT_NOT_AVAILABLE", f"La OS {ingreso_id} no está disponible para alquilar", status_code=409)
        if not is_rental_shelf_location_name(row.get("ubicacion_nombre")):
            raise DeliveryOrderError(
                "RENTAL_EQUIPMENT_NOT_IN_SHELF",
                f"La OS {ingreso_id} no está en Estantería de Alquiler",
                status_code=409,
            )

        serial = _optional_text(row.get("equipment_serial"))
        if not serial:
            raise DeliveryOrderError("RENTAL_SERIAL_REQUIRED", f"La OS {ingreso_id} no tiene número de serie para usar como partida", status_code=409)
        if not partida or partida.casefold() != serial.casefold():
            raise DeliveryOrderError("RENTAL_PARTIDA_MISMATCH", f"La partida del renglón {index} debe ser la serie del equipo", status_code=409)

        duplicate = _rental_duplicate_reservation(int(ingreso_id), order_id)
        if duplicate:
            raise DeliveryOrderError(
                "RENTAL_EQUIPMENT_RESERVED",
                f"La OS {ingreso_id} ya está reservada en la orden {duplicate}",
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
        "sourceReference": service_order_reference(first.get("ingreso_id")) if len(contexts) == 1 else f"Alquiler {len(contexts)} equipos",
    }


def _service_release_context(ingreso_id: int) -> dict[str, Any] | None:
    company_expr = "COALESCE(t.empresa_bejerman, 'SEPID')" if _has_table_column("ingresos", "empresa_bejerman") else "'SEPID'"
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id AS ingreso_id,
                   t.device_id,
                   t.estado,
                   {company_expr} AS company_key,
                   COALESCE(t.resolucion, '') AS resolucion,
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
    from .bejerman_sync import normalize_article_variant

    variant = (
        _optional_text(row.get("equipo_variante"))
        or _optional_text(row.get("device_variante"))
        or _optional_text(row.get("modelo_variante"))
        or ""
    )
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT article_code, article_description
                  FROM bejerman_article_mappings
                 WHERE model_id = %s
                   AND variante_norm = %s
                 ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END,
                          confirmed_at DESC NULLS LAST,
                          updated_at DESC
                 LIMIT 1
                """,
                [model_id, normalize_article_variant(variant)],
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
    service_order = service_order_reference(row.get("ingreso_id"))
    internal_number = _optional_text(row.get("equipment_internal_number"))
    if service_order:
        parts.append(service_order)
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
        "bejermanCustomerCode": _optional_text(row.get("bejerman_customer_code")),
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
    customer_code = _optional_text(row.get("bejerman_customer_code")) or ""
    customer_id = _optional_int(row.get("customer_id"))
    if not customer_code and not customer_id:
        return None
    company_key = normalize_company_key(row.get("company_key") or "SEPID")
    raw_pedido = _service_release_raw_pedido(row)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT o.id
              FROM delivery_orders o
             WHERE o.delivery_type = 'service_release'
               AND o.status IN ('pendiente_armado', 'armado_pendiente_entrega')
               AND NULLIF(BTRIM(COALESCE(o.remito_number, '')), '') IS NULL
               AND o.bejerman_remito_group_id IS NULL
               AND COALESCE(o.company_key, 'SEPID') = %s
               AND LOWER(BTRIM(COALESCE(o.raw_pedido, ''))) = LOWER(BTRIM(%s))
               AND (
                    (%s <> '' AND UPPER(BTRIM(COALESCE(o.bejerman_customer_code, ''))) = UPPER(BTRIM(%s)))
                    OR (%s = '' AND %s IS NOT NULL AND o.customer_id = %s)
                   )
             ORDER BY o.created_at ASC NULLS LAST, o.id ASC
             LIMIT 1
             FOR UPDATE
            """,
            [company_key, raw_pedido, customer_code, customer_code, customer_code, customer_id, customer_id],
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
