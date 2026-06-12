from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from .notifications import active_user_ids_for_roles, emit_notification


logger = logging.getLogger(__name__)

DELIVERY_STATUSES = {
    "pendiente_armado",
    "armado_pendiente_entrega",
    "entregado_pendiente_facturacion",
    "facturado",
    "cancelado",
}
DELIVERY_TYPES = {"sale", "service_release", "rental"}
PRIORITIES = {"normal", "urgente"}
REMITO_LOCATIONS = {"recepcion", "oficina"}
CLOSED_STATUSES = {"facturado", "cancelado"}
DELIVERY_TYPE_LABELS = {
    "sale": "Venta",
    "service_release": "Servicio técnico",
    "rental": "Alquiler",
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


def _decimal(value: Any, default: str = "1") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise DeliveryOrderError("INVALID_DECIMAL", "Cantidad o precio inválido") from exc


def _effective_equipment_partida(item: dict[str, Any], equipment_serial: str | None, quantity: Decimal) -> str | None:
    explicit = _optional_text(item.get("partida"))
    if explicit:
        return explicit
    if item.get("partidas"):
        return None
    if quantity > Decimal("1"):
        return None
    return equipment_serial


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _decode_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def order_number_for_now(delivery_type: str) -> str:
    prefix = {
        "sale": "OE",
        "service_release": "OES",
        "rental": "OEA",
    }.get(delivery_type, "OE")
    stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid4().hex[:4].upper()}"


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


def remito_status(remito_number: Any) -> str:
    return "entregado_pendiente_facturacion" if _optional_text(remito_number) else "armado_pendiente_entrega"


def serialize_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "orderNumber": row.get("order_number"),
        "customerId": row.get("customer_id"),
        "customerName": row.get("customer_name") or "",
        "bejermanCustomerCode": row.get("bejerman_customer_code") or "",
        "deliveryType": row.get("delivery_type"),
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
        "commercialCondition": row.get("commercial_condition") or "",
        "commercialDeadline": row.get("commercial_deadline") or "",
        "remitoNumber": row.get("remito_number") or "",
        "bejermanRemitoGroupId": row.get("bejerman_remito_group_id") or "",
        "remitoLocation": row.get("remito_location") or "",
        "invoiceNumber": row.get("invoice_number") or "",
        "ingresoId": row.get("ingreso_id"),
        "deviceId": row.get("device_id"),
        "sourceSystem": row.get("source_system") or "",
        "sourceExternalId": row.get("source_external_id") or "",
        "sourceReference": row.get("source_reference") or "",
        "sourceSheet": row.get("source_sheet") or "",
        "sourceRow": row.get("source_row"),
        "sourceColor": row.get("source_color") or "",
        "preparedAt": _iso(row.get("prepared_at")),
        "deliveredAt": _iso(row.get("delivered_at")),
        "invoicedAt": _iso(row.get("invoiced_at")),
        "cancelledAt": _iso(row.get("cancelled_at")),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
        "bejermanRemitoGroup": row.get("bejerman_remito_group") or None,
        "items": row.get("items") or [],
        "events": row.get("events") or [],
    }


def _serialize_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "orderId": row.get("order_id"),
        "articleCode": row.get("article_code") or "",
        "articleName": row.get("article_name") or "",
        "description": row.get("description") or "",
        "quantity": float(row.get("quantity") or 0),
        "unitPrice": float(row["unit_price"]) if row.get("unit_price") is not None else None,
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


def _serialize_partida(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "orderItemId": row.get("order_item_id"),
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
        "sellerCode": row.get("seller_code") or "",
        "paymentTermCode": row.get("payment_term_code") or "",
        "operationCode": row.get("operation_code") or "",
        "depositCode": row.get("deposit_code") or "",
        "createdAt": _iso(row.get("created_at")),
        "generatedAt": _iso(row.get("generated_at")),
        "orderCount": len(order_ids_by_group.get(group_id, [])) or len(orders),
        "pdfUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/" if generated else None,
        "printUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/print/" if generated else None,
        "orders": [
            {
                "id": order.get("id"),
                "orderNumber": order.get("order_number") or "",
                "deliveryType": order.get("delivery_type") or "",
                "sourceReference": order.get("source_reference") or "",
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
            SELECT id, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                   comprobante_numero, remito_number, customer_code, customer_name,
                   seller_code, payment_term_code, operation_code, deposit_code,
                   status, order_ids, created_at, generated_at
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
            SELECT id, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                   comprobante_numero, remito_number, customer_code, customer_name,
                   seller_code, payment_term_code, operation_code, deposit_code,
                   status, order_ids, created_at, generated_at
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


def _delivery_order_notification(order_id: str, actor_user_id: int | None, kind: str) -> int:
    try:
        order = get_delivery_order(order_id, include_events=False)
        if kind == "created":
            notification_key = "sales_order_created"
            roles = ["recepcion"]
            title = f"Nueva orden de entrega {order.get('orderNumber') or order_id}"
            body = (
                f"{order.get('customerName') or '-'} - "
                f"{DELIVERY_TYPE_LABELS.get(order.get('deliveryType'), order.get('deliveryType') or '-')}. "
                "Pendiente de preparación."
            )
            href = "/administracion/ordenes-entrega"
        elif kind == "remito_ready":
            notification_key = "sales_order_remito_ready"
            roles = ["cobranzas"]
            title = f"Entrega lista para facturar {order.get('orderNumber') or order_id}"
            body = (
                f"{order.get('customerName') or '-'} - remito {order.get('remitoNumber') or '-'}. "
                "Registrar la factura cuando corresponda."
            )
            href = "/cobranzas/facturacion"
        else:
            return 0

        recipients = active_user_ids_for_roles(roles)
        if not recipients:
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
            },
        )
        create_event(
            order_id,
            actor_user_id,
            "notification_sent" if inserted else "notification_skipped",
            metadata={"kind": kind, "roles": roles, "recipients": inserted},
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


def list_delivery_orders(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    where = []
    params: list[Any] = []

    statuses = [s.strip() for s in _clean_text(filters.get("status")).split(",") if s.strip()]
    if statuses:
        normalized = [normalize_status(s) for s in statuses]
        where.append("o.status = ANY(%s)")
        params.append(normalized)

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
            "o.raw_pedido",
            "o.remito_number",
            "o.invoice_number",
            "o.equipment_model",
            "o.equipment_serial",
            "o.equipment_internal_number",
            "o.source_system",
            "o.source_external_id",
            "o.source_reference",
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
            "o.commercial_condition",
            "o.commercial_deadline",
        ]
        item_search_expressions = [
            "oi.article_code",
            "oi.article_name",
            "oi.description",
            "oi.source_text",
            "oi.partida",
            "oi.stock_deposit_code",
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

    if str(filters.get("pending_billing") or filters.get("pendingBilling") or "").lower() in ("1", "true", "yes"):
        where.append("o.status = 'entregado_pendiente_facturacion'")

    limit = max(1, min(200, int(filters.get("limit") or 80)))
    offset = max(0, int(filters.get("offset") or 0))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM delivery_orders o
            {where_sql}
            """,
            params,
        )
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT o.*
            FROM delivery_orders o
            {where_sql}
            ORDER BY
              o.order_date DESC,
              o.source_row DESC NULLS LAST,
              o.created_at DESC,
              o.id DESC
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


def get_delivery_order(order_id: str, *, include_events: bool = True, for_update: bool = False) -> dict[str, Any]:
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
    return serialize_order(row)


@transaction.atomic
def create_delivery_order(payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    delivery_type = normalize_delivery_type(payload.get("deliveryType") or payload.get("delivery_type"))
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
    order_number = _optional_text(payload.get("orderNumber") or payload.get("order_number")) or order_number_for_now(delivery_type)
    remito_number = _optional_text(payload.get("remitoNumber") or payload.get("remito_number"))
    equipment_serial = _optional_text(payload.get("equipmentSerial") or payload.get("equipment_serial"))
    if remito_number and status in {"pendiente_armado", "armado_pendiente_entrega"}:
        status = "entregado_pendiente_facturacion"

    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise DeliveryOrderError("ITEMS_REQUIRED", "La orden necesita al menos un ítem")

    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO delivery_orders (
              id, order_number, customer_id, bejerman_customer_code, customer_name,
              delivery_type, source_system, source_external_id, source_reference, source_company_id,
              source_sheet, source_row, source_color,
              ingreso_id, device_id, equipment_model, equipment_serial, equipment_internal_number,
              seller_name, seller_code, order_date, operation_company_label, raw_pedido,
              commercial_terms, commercial_price, commercial_exchange_rate, commercial_condition, commercial_deadline,
              status, priority, remito_number, remito_location, invoice_number,
              created_by_user_id, prepared_at, delivered_at, invoiced_at
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s,
              %s,
              CASE WHEN %s IN ('armado_pendiente_entrega','entregado_pendiente_facturacion','facturado') THEN CURRENT_TIMESTAMP ELSE NULL END,
              CASE WHEN %s IN ('entregado_pendiente_facturacion','facturado') THEN CURRENT_TIMESTAMP ELSE NULL END,
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
                _optional_text(payload.get("sourceSystem")) or "nexora",
                _optional_text(payload.get("sourceExternalId")),
                _optional_text(payload.get("sourceReference")),
                _optional_text(payload.get("sourceCompanyId")),
                _optional_text(payload.get("sourceSheet")),
                _optional_int(payload.get("sourceRow")),
                _optional_text(payload.get("sourceColor")),
                _optional_int(payload.get("ingresoId")),
                _optional_int(payload.get("deviceId")),
                _optional_text(payload.get("equipmentModel")),
                equipment_serial,
                _optional_text(payload.get("equipmentInternalNumber")),
                _clean_text(payload.get("sellerName")) or "",
                _optional_text(payload.get("sellerCode")),
                payload.get("orderDate") or date.today().isoformat(),
                _clean_text(payload.get("operationCompanyLabel")),
                _clean_text(payload.get("rawPedido")),
                _optional_text(payload.get("commercialTerms")),
                _optional_text(payload.get("commercialPrice")),
                _optional_text(payload.get("commercialExchangeRate")),
                _optional_text(payload.get("commercialCondition")),
                _optional_text(payload.get("commercialDeadline")),
                status,
                priority,
                remito_number,
                "recepcion" if remito_number else _optional_text(payload.get("remitoLocation")),
                _optional_text(payload.get("invoiceNumber")),
                actor_user_id,
                status,
                status,
                status,
            ],
        )
        for idx, item in enumerate(items):
            item_id = _optional_text(item.get("id")) or f"doi-{uuid4()}"
            item_quantity = _decimal(item.get("quantity"))
            cur.execute(
                """
                INSERT INTO delivery_order_items (
                  id, order_id, article_code, article_name, description, quantity, unit_price,
                  source_text, partida, partida_expiration_date, stock_deposit_code,
                  stock_available_quantity, stock_checked_at, sort_order
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    item_id,
                    order_id,
                    _optional_text(item.get("articleCode")),
                    _optional_text(item.get("articleName")),
                    _clean_text(item.get("description") or item.get("sourceText")),
                    item_quantity,
                    _decimal(item.get("unitPrice"), "0") if item.get("unitPrice") not in (None, "") else None,
                    _optional_text(item.get("sourceText")),
                    _effective_equipment_partida(item, equipment_serial, item_quantity),
                    item.get("partidaExpirationDate") or None,
                    _optional_text(item.get("stockDepositCode")),
                    _decimal(item.get("stockAvailableQuantity"), "0") if item.get("stockAvailableQuantity") not in (None, "") else None,
                    item.get("stockCheckedAt") or None,
                    item.get("sortOrder") if item.get("sortOrder") is not None else idx,
                ],
            )
            insert_item_partidas(cur, item_id, item.get("partidas") or [])

    create_event(order_id, actor_user_id, "delivery_order_created", metadata={"status": status})
    notify_delivery_order_created(order_id, actor_user_id)
    if remito_number:
        notify_delivery_order_remito_ready(order_id, actor_user_id)
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
              id, order_item_id, partida, assigned_quantity, partida_expiration_date,
              stock_deposit_code, stock_available_quantity, stock_checked_at, sort_order
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                _optional_text(partida.get("id")) or f"doip-{uuid4()}",
                item_id,
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
    if current["status"] in CLOSED_STATUSES:
        raise DeliveryOrderError("DELIVERY_ORDER_CLOSED", "La orden ya está cerrada", status_code=409)
    should_notify_billing = bool(_optional_text(remito_number)) and not bool(_optional_text(current.get("remitoNumber")))
    next_status = remito_status(remito_number or current.get("remitoNumber"))
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
                delivered_by_user_id = CASE WHEN %s = 'entregado_pendiente_facturacion' THEN COALESCE(delivered_by_user_id, %s) ELSE delivered_by_user_id END,
                prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP),
                delivered_at = CASE WHEN %s = 'entregado_pendiente_facturacion' THEN COALESCE(delivered_at, CURRENT_TIMESTAMP) ELSE delivered_at END
            WHERE id = %s
            """,
            [
                next_status,
                _optional_text(remito_number),
                _optional_text(remito_number),
                _optional_text(remito_number),
                actor_user_id,
                _optional_text(remito_number),
                actor_user_id,
                next_status,
                actor_user_id,
                next_status,
                order_id,
            ],
        )
    create_event(
        order_id,
        actor_user_id,
        "delivery_order_prepared",
        metadata={"status": next_status, "remitoNumber": _optional_text(remito_number)},
    )
    if should_notify_billing:
        notify_delivery_order_remito_ready(order_id, actor_user_id)
    return get_delivery_order(order_id)


@transaction.atomic
def mark_delivered(order_id: str, actor_user_id: int | None, remito_number: str | None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if current["status"] in CLOSED_STATUSES:
        raise DeliveryOrderError("DELIVERY_ORDER_CLOSED", "La orden ya está cerrada", status_code=409)
    effective_remito = _optional_text(remito_number) or _optional_text(current.get("remitoNumber"))
    if not effective_remito:
        raise DeliveryOrderError("REMITO_REQUIRED", "Para entregar la orden se necesita un remito")
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET status = 'entregado_pendiente_facturacion',
                remito_number = %s,
                remito_location = COALESCE(remito_location, 'recepcion'),
                remito_location_updated_by = COALESCE(remito_location_updated_by, %s),
                remito_location_updated_at = COALESCE(remito_location_updated_at, CURRENT_TIMESTAMP),
                prepared_by_user_id = COALESCE(prepared_by_user_id, %s),
                delivered_by_user_id = %s,
                prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP),
                delivered_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            [effective_remito, actor_user_id, actor_user_id, actor_user_id, order_id],
        )
    create_event(order_id, actor_user_id, "delivery_order_delivered", metadata={"remitoNumber": effective_remito})
    notify_delivery_order_remito_ready(order_id, actor_user_id)
    return get_delivery_order(order_id)


@transaction.atomic
def mark_invoiced(order_id: str, actor_user_id: int | None, invoice_number: str) -> dict[str, Any]:
    invoice = _optional_text(invoice_number)
    if not invoice:
        raise DeliveryOrderError("INVOICE_REQUIRED", "Número de factura requerido")
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if current["status"] != "entregado_pendiente_facturacion":
        raise DeliveryOrderError("INVALID_INVOICE_STATE", "Solo se factura una orden entregada con remito", status_code=409)
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
            SET status = 'facturado',
                invoice_number = %s,
                invoiced_by_user_id = %s,
                invoiced_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            [invoice, actor_user_id, order_id],
        )
    create_event(order_id, actor_user_id, "delivery_order_invoiced", metadata={"invoiceNumber": invoice})
    return get_delivery_order(order_id)


@transaction.atomic
def cancel_order(order_id: str, actor_user_id: int | None, note: str | None = None) -> dict[str, Any]:
    current = get_delivery_order(order_id, include_events=False, for_update=True)
    if current["status"] == "facturado":
        raise DeliveryOrderError("DELIVERY_ORDER_INVOICED", "No se puede cancelar una orden facturada", status_code=409)
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
    if current["status"] in CLOSED_STATUSES or current.get("remitoNumber"):
        raise DeliveryOrderError("DELIVERY_ORDER_LOCKED", "No se puede editar un ítem de una orden cerrada o con remito", status_code=409)
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_order_items
            SET article_code = %s,
                article_name = %s,
                unit_price = %s,
                partida = %s,
                partida_expiration_date = %s,
                stock_deposit_code = %s,
                stock_available_quantity = %s,
                stock_checked_at = %s
            WHERE id = %s AND order_id = %s
            """,
            [
                _optional_text(payload.get("articleCode")),
                _optional_text(payload.get("articleName")),
                _decimal(payload.get("unitPrice"), "0") if payload.get("unitPrice") not in (None, "") else None,
                _optional_text(payload.get("partida")),
                payload.get("partidaExpirationDate") or None,
                _optional_text(payload.get("stockDepositCode")),
                _decimal(payload.get("stockAvailableQuantity"), "0")
                if payload.get("stockAvailableQuantity") not in (None, "")
                else None,
                payload.get("stockCheckedAt") or None,
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
    if current["status"] in CLOSED_STATUSES or current.get("remitoNumber"):
        raise DeliveryOrderError("DELIVERY_ORDER_LOCKED", "No se pueden editar partidas de una orden cerrada o con remito", status_code=409)
    with connection.cursor() as cur:
        cur.execute("SELECT id FROM delivery_order_items WHERE id = %s AND order_id = %s", [item_id, order_id])
        if not cur.fetchone():
            raise DeliveryOrderError("DELIVERY_ORDER_ITEM_NOT_FOUND", "Ítem de orden no encontrado", status_code=404)
        cur.execute("DELETE FROM delivery_order_item_partidas WHERE order_item_id = %s", [item_id])
        insert_item_partidas(cur, item_id, partidas)
    create_event(
        order_id,
        actor_user_id,
        "delivery_order_item_partidas_updated",
        metadata={"itemId": item_id, "count": len(partidas or [])},
    )
    return get_delivery_order(order_id)


def _date_for_order(value: Any) -> str:
    if hasattr(value, "date"):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return date.today().isoformat()


def _service_release_context(ingreso_id: int) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT t.id AS ingreso_id,
                   t.device_id,
                   t.estado,
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


def _existing_service_release_order_id(ingreso_id: int) -> str | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id
              FROM delivery_orders
             WHERE source_system = 'nexora'
               AND source_external_id = %s
               AND delivery_type = 'service_release'
             LIMIT 1
            """,
            [str(ingreso_id)],
        )
        row = cur.fetchone()
    return row[0] if row else None


@transaction.atomic
def _refresh_service_release_order(order_id: str, row: dict[str, Any], mapping: dict[str, Any] | None, actor_user_id: int | None) -> dict[str, Any]:
    equipment_label = _service_release_equipment_label(row)
    equipment_serial = _optional_text(row.get("equipment_serial"))
    article_code = _optional_text((mapping or {}).get("article_code"))
    article_name = _optional_text((mapping or {}).get("article_description")) or "Liberación servicio técnico"
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_orders
               SET customer_id = %s,
                   bejerman_customer_code = %s,
                   customer_name = %s,
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
               AND status NOT IN ('facturado','cancelado')
               AND remito_number IS NULL
            """,
            [
                row.get("customer_id"),
                _optional_text(row.get("bejerman_customer_code")),
                row.get("customer_name") or "",
                f"OS {row['ingreso_id']}",
                row.get("ingreso_id"),
                row.get("device_id"),
                equipment_label,
                equipment_serial,
                _optional_text(row.get("equipment_internal_number")),
                _date_for_order(row.get("order_date")),
                _optional_text(row.get("resolucion")) or "reparado",
                order_id,
            ],
        )
        if article_code:
            cur.execute(
                """
                UPDATE delivery_order_items
                   SET article_code = COALESCE(NULLIF(article_code, ''), %s),
                       article_name = CASE
                         WHEN NULLIF(article_name, '') IS NULL THEN %s
                         ELSE article_name
                       END,
                       partida = COALESCE(NULLIF(partida, ''), %s)
                 WHERE id = (
                   SELECT id
                     FROM delivery_order_items
                    WHERE order_id = %s
                    ORDER BY sort_order, created_at
                    LIMIT 1
                 )
                """,
                [article_code, article_name, equipment_serial, order_id],
            )
        elif equipment_serial:
            cur.execute(
                """
                UPDATE delivery_order_items
                   SET partida = COALESCE(NULLIF(partida, ''), %s)
                 WHERE id = (
                   SELECT id
                     FROM delivery_order_items
                    WHERE order_id = %s
                    ORDER BY sort_order, created_at
                    LIMIT 1
                 )
                """,
                [equipment_serial, order_id],
            )
    create_event(order_id, actor_user_id, "service_release_order_synced", metadata={"ingresoId": row.get("ingreso_id")})
    return get_delivery_order(order_id)


def ensure_service_release_order_for_ingreso(ingreso_id: int, actor_user_id: int | None = None) -> dict[str, Any] | None:
    row = _service_release_context(ingreso_id)
    if not row:
        return None
    if _clean_text(row.get("estado")).lower() not in {"liberado", "vendido_pendiente_entrega"}:
        return None

    mapping = _article_mapping_for_service_release(row)
    existing_id = _existing_service_release_order_id(ingreso_id)
    if existing_id:
        return _refresh_service_release_order(existing_id, row, mapping, actor_user_id)

    equipment_label = _service_release_equipment_label(row)
    article_code = _optional_text((mapping or {}).get("article_code"))
    article_name = _optional_text((mapping or {}).get("article_description")) or "Liberación servicio técnico"
    description = f"Liberación servicio técnico - {equipment_label}"
    payload = {
        "customerId": row.get("customer_id"),
        "customerName": row.get("customer_name") or "",
        "bejermanCustomerCode": _optional_text(row.get("bejerman_customer_code")),
        "deliveryType": "service_release",
        "sourceSystem": "nexora",
        "sourceExternalId": str(ingreso_id),
        "sourceReference": f"OS {ingreso_id}",
        "ingresoId": ingreso_id,
        "deviceId": row.get("device_id"),
        "equipmentModel": equipment_label,
        "equipmentSerial": _optional_text(row.get("equipment_serial")),
        "equipmentInternalNumber": _optional_text(row.get("equipment_internal_number")),
        "sellerName": "Servicio Técnico",
        "orderDate": _date_for_order(row.get("order_date")),
        "operationCompanyLabel": "REPARACION",
        "rawPedido": _optional_text(row.get("resolucion")) or "reparado",
        "priority": "normal",
        "status": "armado_pendiente_entrega",
        "items": [
            {
                "articleCode": article_code,
                "articleName": article_name,
                "description": description,
                "quantity": 1,
                "sourceText": description,
                "partida": _optional_text(row.get("equipment_serial")),
            }
        ],
    }
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
    return _optional_text(row[0]) if row else None


def remito_profile_for_type(delivery_type: str) -> RemitoProfile:
    delivery_type = normalize_delivery_type(delivery_type)
    if delivery_type == "rental":
        return RemitoProfile("RTA", "00001", "ALQ", "STL")
    if delivery_type == "service_release":
        return RemitoProfile("RSS", "00004", "REP", "STC")
    return RemitoProfile("RT", "00002", "MC", "VAL")
