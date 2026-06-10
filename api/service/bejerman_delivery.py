from __future__ import annotations

import base64
import json
import os
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction

from .bejerman_bridge import (
    BejermanBridgeClient,
    BejermanBridgeConfigError,
    BejermanBridgeResponseError,
    BejermanBridgeUnavailable,
)
from .delivery_orders import (
    DeliveryOrderError,
    _json_param,
    _optional_text,
    create_event,
    get_delivery_order,
    get_user_seller_code,
    list_delivery_orders,
    remito_profile_for_type,
    serialize_order,
)


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _bridge_error(exc: Exception) -> BillingError | DeliveryOrderError:
    if isinstance(exc, BejermanBridgeConfigError):
        return BillingError("BEJERMAN_BRIDGE_NOT_CONFIGURED", str(exc), status_code=503)
    if isinstance(exc, BejermanBridgeUnavailable):
        return BillingError("BEJERMAN_BRIDGE_UNAVAILABLE", str(exc), status_code=503)
    if isinstance(exc, BejermanBridgeResponseError):
        return BillingError("BEJERMAN_BRIDGE_ERROR", str(exc), status_code=exc.status_code or 502)
    return BillingError("BEJERMAN_ERROR", str(exc), status_code=502)


def list_facturacion_company_options() -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id,
                   razon_social,
                   TRIM(COALESCE(cod_empresa, '')) AS cod_empresa
            FROM customers
            WHERE TRIM(COALESCE(cod_empresa, '')) <> ''
            ORDER BY razon_social ASC
            """
        )
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return [
        {
            "id": row["id"],
            "name": row.get("razon_social") or "",
            "bejermanCustomerCode": row.get("cod_empresa") or "",
        }
        for row in rows
    ]


def list_facturacion_from_bejerman(customer_code: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    code = _optional_text(customer_code)
    if not code:
        raise BillingError("CUSTOMER_CODE_REQUIRED", "Código Bejerman requerido")
    params = {}
    for key in ("desde", "hasta", "tipo", "page", "pageSize", "search"):
        value = (filters or {}).get(key)
        if value not in (None, ""):
            params[key] = value
    try:
        data = BejermanBridgeClient.from_settings().get_json(
            f"/api/portal/clientes/{code}/facturacion",
            params,
        )
    except Exception as exc:
        raise _bridge_error(exc) from exc
    return data or {"items": [], "pagination": {"page": 1, "pageSize": 50, "total": 0}}


def get_facturacion_pdf(customer_code: str, document_id: str) -> tuple[bytes, str]:
    code = _optional_text(customer_code)
    doc = _optional_text(document_id)
    if not code or not doc:
        raise BillingError("DOCUMENT_REQUIRED", "Documento y cliente requeridos")
    try:
        return BejermanBridgeClient.from_settings().get_pdf(
            f"/api/portal/facturacion/{doc}/pdf",
            {"clienteCodigo": code},
        )
    except Exception as exc:
        raise _bridge_error(exc) from exc


def _env_text(name: str, default: str) -> str:
    value = getattr(settings, name, None)
    if value is None:
        value = os.getenv(name, default)
    text = str(value or "").strip()
    return text or default


def _profile_for_order(delivery_type: str):
    profile = remito_profile_for_type(delivery_type)
    if delivery_type == "rental":
        return {
            "type": _env_text("BEJERMAN_REMITO_RENTAL_TYPE", profile.comprobante_tipo),
            "pointOfSale": _env_text("BEJERMAN_REMITO_RENTAL_POINT_OF_SALE", profile.point_of_sale),
            "operation": _env_text("BEJERMAN_REMITO_RENTAL_OPERATION", profile.operation_code),
            "deposit": _env_text("BEJERMAN_REMITO_RENTAL_DEPOSIT", profile.deposit_code),
        }
    if delivery_type == "service_release":
        return {
            "type": _env_text("BEJERMAN_REMITO_SERVICE_TYPE", profile.comprobante_tipo),
            "pointOfSale": _env_text("BEJERMAN_REMITO_SERVICE_POINT_OF_SALE", profile.point_of_sale),
            "operation": _env_text("BEJERMAN_REMITO_SERVICE_OPERATION", profile.operation_code),
            "deposit": _env_text("BEJERMAN_REMITO_SERVICE_DEPOSIT", profile.deposit_code),
        }
    return {
        "type": _env_text("BEJERMAN_REMITO_SALE_TYPE", profile.comprobante_tipo),
        "pointOfSale": _env_text("BEJERMAN_REMITO_SALE_POINT_OF_SALE", profile.point_of_sale),
        "operation": _env_text("BEJERMAN_REMITO_SALE_OPERATION", profile.operation_code),
        "deposit": _env_text("BEJERMAN_REMITO_SALE_DEPOSIT", profile.deposit_code),
    }


def _validate_remito_orders(orders: list[dict[str, Any]]) -> dict[str, Any]:
    if not orders:
        raise DeliveryOrderError("DELIVERY_ORDER_NOT_FOUND", "No se encontraron órdenes", status_code=404)
    customer_codes = {(_optional_text(order.get("bejermanCustomerCode")) or "") for order in orders}
    if len(customer_codes) != 1 or not next(iter(customer_codes)):
        raise DeliveryOrderError("CUSTOMER_MAPPING_REQUIRED", "Las órdenes necesitan el mismo código Bejerman", status_code=409)
    delivery_types = {order.get("deliveryType") for order in orders}
    if len(delivery_types) != 1:
        raise DeliveryOrderError(
            "INCOMPATIBLE_DELIVERY_REMITO_PROFILE",
            "Las órdenes requieren distinto tipo u operación de remito",
            status_code=409,
        )
    for order in orders:
        if order.get("status") in {"facturado", "cancelado"}:
            raise DeliveryOrderError("DELIVERY_ORDER_CLOSED", "Una de las órdenes ya está cerrada", status_code=409)
        if _optional_text(order.get("remitoNumber")):
            raise DeliveryOrderError("SALES_ORDER_ALREADY_HAS_REMITO", "Una de las órdenes ya tiene remito cargado", status_code=409)
    return {
        "customerCode": next(iter(customer_codes)),
        "customerName": orders[0].get("customerName") or "",
        "deliveryType": next(iter(delivery_types)),
    }


def _bridge_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "orderNumber": order.get("orderNumber"),
        "deliveryType": order.get("deliveryType"),
        "customerName": order.get("customerName"),
        "operationCompanyLabel": order.get("operationCompanyLabel"),
        "equipmentModel": order.get("equipmentModel"),
        "equipmentSerial": order.get("equipmentSerial"),
        "equipmentInternalNumber": order.get("equipmentInternalNumber"),
        "items": [
            {
                "id": item.get("id"),
                "articleCode": item.get("articleCode") or None,
                "articleName": item.get("articleName") or None,
                "description": item.get("description") or item.get("sourceText") or "",
                "quantity": item.get("quantity") or 1,
                "unitPrice": item.get("unitPrice"),
                "partida": item.get("partida") or None,
                "partidas": item.get("partidas") or [],
            }
            for item in (order.get("items") or [])
        ],
    }


@transaction.atomic
def reserve_remito_group(
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
    *,
    seller_code: str,
    payment_term_code: str,
) -> tuple[str, dict[str, Any]]:
    compatibility = _validate_remito_orders(orders)
    profile = _profile_for_order(compatibility["deliveryType"])
    group_id = f"brg-{uuid4()}"
    order_ids = [order["id"] for order in orders]
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_remito_groups (
              id, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
              customer_code, customer_name, seller_code, payment_term_code,
              operation_code, deposit_code, status, order_ids, response_summary, created_by_user_id
            )
            VALUES (%s, %s, 'R', %s, %s, %s, %s, %s, %s, %s, 'pending', %s::jsonb, '{}'::jsonb, %s)
            """,
            [
                group_id,
                profile["type"],
                profile["pointOfSale"],
                compatibility["customerCode"],
                compatibility["customerName"],
                seller_code,
                payment_term_code,
                profile["operation"],
                profile["deposit"],
                _json_param(order_ids),
                actor_user_id,
            ],
        )
        cur.execute(
            """
            UPDATE delivery_orders
            SET bejerman_remito_group_id = %s
            WHERE id = ANY(%s)
            """,
            [group_id, order_ids],
        )
    return group_id, {**compatibility, "profile": profile}


def _mark_group_failed(group_id: str, order_ids: list[str], actor_user_id: int | None, exc: Exception):
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                SET status = 'failed',
                    response_summary = %s::jsonb
                WHERE id = %s
                """,
                [_json_param({"error": str(exc)}), group_id],
            )
            cur.execute(
                """
                UPDATE delivery_orders
                SET bejerman_remito_group_id = NULL
                WHERE id = ANY(%s) AND bejerman_remito_group_id = %s
                """,
                [order_ids, group_id],
            )
        for order_id in order_ids:
            create_event(order_id, actor_user_id, "bejerman_remito_failed", metadata={"groupId": group_id, "error": str(exc)})


def generate_bejerman_remito(order_ids: list[str], actor_user_id: int | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ids = [str(item).strip() for item in (order_ids or []) if str(item).strip()]
    if not ids:
        raise DeliveryOrderError("ORDER_IDS_REQUIRED", "Hay que seleccionar órdenes")
    orders = [get_delivery_order(order_id, include_events=False) for order_id in ids]
    compatibility = _validate_remito_orders(orders)
    seller_code = (
        _optional_text((payload or {}).get("sellerCode"))
        or get_user_seller_code(actor_user_id)
        or (
            _env_text("BEJERMAN_REMITO_SERVICE_SELLER", "ADM")
            if compatibility["deliveryType"] == "service_release"
            else _env_text("BEJERMAN_REMITO_SELLER", "ADM")
        )
    )
    payment_term_code = (
        _optional_text((payload or {}).get("paymentTermCode"))
        or _env_text("BEJERMAN_REMITO_PAYMENT_TERM", _env_text("BEJERMAN_RIS_PAYMENT_TERM", "30"))
    )

    group_id, compatibility = reserve_remito_group(
        orders,
        actor_user_id,
        seller_code=seller_code,
        payment_term_code=payment_term_code,
    )
    bridge_request = {
        "groupId": group_id,
        "issueDate": (payload or {}).get("issueDate"),
        "customerCode": compatibility["customerCode"],
        "customerName": compatibility["customerName"],
        "sellerCode": seller_code,
        "paymentTermCode": payment_term_code,
        "notes": _optional_text((payload or {}).get("notes")),
        "orders": [_bridge_order(order) for order in orders],
    }
    if not bridge_request["issueDate"]:
        from django.utils import timezone

        bridge_request["issueDate"] = timezone.localdate().isoformat()

    try:
        bridge_data = BejermanBridgeClient.from_settings().post_json(
            "/api/portal/remitos/delivery-order",
            bridge_request,
        )
    except Exception as exc:
        _mark_group_failed(group_id, ids, actor_user_id, exc)
        converted = _bridge_error(exc)
        raise DeliveryOrderError(converted.code, str(converted), status_code=converted.status_code) from exc

    remito_number = _optional_text(bridge_data.get("remitoNumber"))
    response = bridge_data.get("response") or {}
    profile = bridge_data.get("profile") or compatibility["profile"]
    if not remito_number:
        exc = DeliveryOrderError("BEJERMAN_REMITO_RESPONSE_INCOMPLETE", "El bridge no devolvió número de remito", status_code=502)
        _mark_group_failed(group_id, ids, actor_user_id, exc)
        raise exc

    response_summary = {
        **response,
        "profile": profile,
        "lineCount": bridge_data.get("lineCount"),
        "stockWarnings": bridge_data.get("stockWarnings") or [],
    }
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                SET status = 'generated',
                    comprobante_tipo = %s,
                    comprobante_letra = %s,
                    comprobante_pto_venta = %s,
                    comprobante_numero = %s,
                    remito_number = %s,
                    operation_code = %s,
                    deposit_code = %s,
                    response_summary = %s::jsonb,
                    generated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                [
                    response.get("comprobanteTipo") or profile.get("type") or compatibility["profile"]["type"],
                    response.get("comprobanteLetra") or "R",
                    response.get("comprobantePtoVenta") or profile.get("pointOfSale"),
                    response.get("comprobanteNumero"),
                    remito_number,
                    profile.get("operation") or compatibility["profile"]["operation"],
                    profile.get("deposit") or compatibility["profile"]["deposit"],
                    _json_param(response_summary),
                    group_id,
                ],
            )
            cur.execute(
                """
                UPDATE delivery_orders
                SET status = 'entregado_pendiente_facturacion',
                    remito_number = %s,
                    remito_location = COALESCE(remito_location, 'recepcion'),
                    remito_location_updated_by = COALESCE(remito_location_updated_by, %s),
                    remito_location_updated_at = COALESCE(remito_location_updated_at, CURRENT_TIMESTAMP),
                    prepared_by_user_id = COALESCE(prepared_by_user_id, %s),
                    delivered_by_user_id = COALESCE(delivered_by_user_id, %s),
                    prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP),
                    delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP)
                WHERE id = ANY(%s) AND bejerman_remito_group_id = %s
                """,
                [remito_number, actor_user_id, actor_user_id, actor_user_id, ids, group_id],
            )
        for order_id in ids:
            create_event(
                order_id,
                actor_user_id,
                "bejerman_remito_generated",
                metadata={
                    "groupId": group_id,
                    "remitoNumber": remito_number,
                    "sellerCode": seller_code,
                    "paymentTermCode": payment_term_code,
                    "profile": profile,
                    "stockWarnings": bridge_data.get("stockWarnings") or [],
                },
            )

    updated = list_delivery_orders({"limit": len(ids)})["items"]
    updated_by_id = {order["id"]: order for order in updated if order["id"] in ids}
    return {
        "groupId": group_id,
        "remitoNumber": remito_number,
        "profile": profile,
        "orders": [updated_by_id.get(order_id) or get_delivery_order(order_id, include_events=False) for order_id in ids],
        "stockWarnings": bridge_data.get("stockWarnings") or [],
        "pdfUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/",
    }


def _document_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def get_remito_group_pdf(group_id: str) -> tuple[bytes, str, str]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                   customer_code, remito_number, status, created_at, generated_at
            FROM bejerman_remito_groups
            WHERE id = %s
            """,
            [group_id],
        )
        cols = [col[0] for col in cur.description]
        row_raw = cur.fetchone()
        row = dict(zip(cols, row_raw)) if row_raw else None
    if not row or row.get("status") != "generated" or not row.get("comprobante_numero") or not row.get("comprobante_pto_venta"):
        raise BillingError("REMITO_PDF_NOT_AVAILABLE", "El PDF del remito todavía no está disponible", status_code=404)
    doc_id = _document_id(
        {
            "t": row.get("comprobante_tipo"),
            "n": row.get("comprobante_numero"),
            "l": row.get("comprobante_letra") or "R",
            "p": row.get("comprobante_pto_venta"),
            "f": (row.get("generated_at") or row.get("created_at")).date().isoformat()
            if hasattr(row.get("generated_at") or row.get("created_at"), "date")
            else None,
            "c": row.get("customer_code"),
        }
    )
    bytes_, content_type = get_facturacion_pdf(row["customer_code"], doc_id)
    filename = f"remito-{row.get('remito_number') or group_id}.pdf"
    return bytes_, content_type, filename
