from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from django.db import IntegrityError, connection, transaction

from .delivery_orders import (
    CLOSED_STATUSES,
    DeliveryOrderError,
    delivery_order_number,
    delivery_source_reference,
    get_delivery_order,
    list_delivery_orders,
    mark_delivered,
)
from .notifications import active_users_for_notification, emit_notification


ROUTE_STOP_STATUSES = {"pendiente", "completado", "pospuesto", "cancelado"}
ROUTE_STOP_ACTIVE_STATUSES = {"pendiente", "pospuesto"}
ROUTE_STOP_CREATED_NOTIFICATION_KEY = "route_stop_created"
ROUTE_SHEET_NOTIFICATION_ROLES = ("logistica",)


class RouteSheetError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


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


def _bool_value(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    if text in {"0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def _date_value(value: Any, *, field: str = "date", required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise RouteSheetError("ROUTE_DATE_REQUIRED", "La fecha de viaje es obligatoria")
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean_text(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise RouteSheetError("INVALID_DATE", f"Fecha inválida en {field}") from exc


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _decode_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def route_location_key(value: Any) -> str:
    text = _clean_text(value).upper()
    return re.sub(r"\s+", " ", text)


def route_address_key(value: Any) -> str:
    return route_location_key(value)


def _metadata(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "{}"


def _status(value: Any) -> str:
    text = _clean_text(value).lower() or "pendiente"
    if text not in ROUTE_STOP_STATUSES:
        raise RouteSheetError("INVALID_ROUTE_STOP_STATUS", "Estado de Hoja de ruta inválido")
    return text


def _delivery_order_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row or not row.get("order_id"):
        return None
    return {
        "id": row.get("order_id"),
        "orderNumber": delivery_order_number(
            {
                "order_number": row.get("order_number"),
                "delivery_type": row.get("order_delivery_type"),
                "ingreso_id": row.get("order_ingreso_id"),
            }
        ),
        "customerName": row.get("order_customer_name") or "",
        "status": row.get("order_status") or "",
        "remitoNumber": row.get("order_remito_number") or "",
        "sourceReference": delivery_source_reference(
            row.get("order_source_reference"),
            row.get("order_delivery_type"),
            row.get("order_ingreso_id"),
        ),
        "rawPedido": row.get("order_raw_pedido") or "",
    }


def _serialize_location(row: dict[str, Any]) -> dict[str, Any]:
    customer_id = row.get("customer_id")
    source_type = row.get("source_type")
    if not source_type:
        source_type = "customer_location" if customer_id else "route_location"
    return {
        "id": row.get("id"),
        "sourceType": source_type,
        "kind": source_type,
        "name": row.get("name") or "",
        "nameKey": row.get("name_key") or route_location_key(row.get("name")),
        "addressKey": row.get("address_key") or route_address_key(row.get("address")),
        "address": row.get("address") or "",
        "notes": row.get("notes") or "",
        "customerId": customer_id,
        "customerName": row.get("customer_name") or "",
        "customerCode": row.get("customer_code") or "",
        "active": row.get("active") if row.get("active") is not None else True,
        "lastUsedAt": _iso(row.get("last_used_at")),
        "usageCount": row.get("usage_count") or 0,
        "sourceSystem": row.get("source_system") or "",
        "sourceSheet": row.get("source_sheet") or "",
        "sourceRow": row.get("source_row"),
        "metadata": _decode_json(row.get("metadata"), {}),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


def _serialize_stop(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "routeDate": _iso(row.get("route_date")),
        "requestedDate": _iso(row.get("requested_date")),
        "requesterName": row.get("requester_name") or "",
        "timeWindow": row.get("time_window") or "",
        "locationId": row.get("location_id"),
        "locationSourceType": row.get("location_source_type") or "",
        "locationCustomerId": row.get("location_customer_id"),
        "placeName": row.get("place_name") or "",
        "address": row.get("address") or "",
        "task": row.get("task") or "",
        "sortOrder": row.get("sort_order") or 0,
        "status": row.get("status") or "pendiente",
        "deliveryOrderId": row.get("delivery_order_id") or "",
        "deliveryOrder": _delivery_order_summary(row),
        "sourceSystem": row.get("source_system") or "",
        "sourceSheet": row.get("source_sheet") or "",
        "sourceRow": row.get("source_row"),
        "metadata": _decode_json(row.get("metadata"), {}),
        "completedNote": row.get("completed_note") or "",
        "postponeNote": row.get("postpone_note") or "",
        "cancelledNote": row.get("cancelled_note") or "",
        "createdByUserId": row.get("created_by_user_id"),
        "updatedByUserId": row.get("updated_by_user_id"),
        "completedByUserId": row.get("completed_by_user_id"),
        "postponedByUserId": row.get("postponed_by_user_id"),
        "cancelledByUserId": row.get("cancelled_by_user_id"),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
        "completedAt": _iso(row.get("completed_at")),
        "postponedAt": _iso(row.get("postponed_at")),
        "cancelledAt": _iso(row.get("cancelled_at")),
    }


def _route_date_label(value: Any) -> str:
    raw = (_iso(value) or "")[:10]
    try:
        year, month, day = raw.split("-")
        return f"{day}/{month}/{year}"
    except ValueError:
        return raw


def _notify_route_stop_created(stop: dict[str, Any] | None) -> int:
    if not stop:
        return 0
    recipients = active_users_for_notification(
        ROUTE_STOP_CREATED_NOTIFICATION_KEY,
        roles=ROUTE_SHEET_NOTIFICATION_ROLES,
        channel="any",
    )
    user_ids = [int(row["id"]) for row in recipients]
    if not user_ids:
        return 0

    stop_id = _clean_text(stop.get("id"))
    route_date = (_iso(stop.get("route_date")) or "")[:10]
    place = _clean_text(stop.get("place_name")) or "Sin lugar"
    task = _clean_text(stop.get("task"))
    address = _clean_text(stop.get("address"))
    requester = _clean_text(stop.get("requester_name"))
    body_lines = [
        f"Fecha: {_route_date_label(stop.get('route_date'))}",
        f"Tarea: {task}" if task else "",
        f"Lugar: {place}" if place else "",
        f"Dirección: {address}" if address else "",
        f"Solicitante: {requester}" if requester else "",
    ]
    return emit_notification(
        ROUTE_STOP_CREATED_NOTIFICATION_KEY,
        user_ids=user_ids,
        title=f"Nueva parada de Hoja de ruta - {place}",
        body="\n".join(line for line in body_lines if line),
        href=f"/hoja-de-ruta?date={quote(route_date, safe='')}",
        severity="info",
        entity_type="route_stop",
        entity_id=stop_id,
        dedupe_key=f"route_stop:{stop_id}:created",
        payload={
            "stopId": stop_id,
            "routeDate": route_date,
            "placeName": place,
            "address": address,
            "task": task,
            "requesterName": requester,
            "deliveryOrderId": _clean_text(stop.get("delivery_order_id")),
        },
        push=True,
    )


def get_route_location(location_id: int) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT l.*,
                   c.razon_social AS customer_name,
                   COALESCE(c.cod_empresa, c.bejerman_cod_empresa, '') AS customer_code
              FROM route_locations l
              LEFT JOIN customers c ON c.id = l.customer_id
             WHERE l.id = %s
            """,
            [location_id],
        )
        row = _one(cur)
    return row


def _touch_route_location(location_id: int | None) -> None:
    if not location_id:
        return
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE route_locations
               SET active = TRUE,
                   last_used_at = CURRENT_TIMESTAMP,
                   usage_count = COALESCE(usage_count, 0) + 1
             WHERE id = %s
            """,
            [location_id],
        )


def upsert_route_location(
    name: str,
    address: str = "",
    *,
    customer_id: int | None = None,
    notes: str | None = None,
    source_system: str = "nexora",
    source_sheet: str | None = None,
    source_row: int | None = None,
    metadata: dict[str, Any] | None = None,
    touch_usage: bool = False,
) -> int | None:
    clean_name = _clean_text(name)
    if not clean_name:
        return None
    name_key = route_location_key(clean_name)
    clean_address = _clean_text(address)
    address_key = route_address_key(clean_address)
    clean_customer_id = _optional_int(customer_id)
    clean_notes = _clean_text(notes)
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_locations (
              name_key, address_key, customer_id, name, address, notes, active, last_used_at, usage_count,
              source_system, source_sheet, source_row, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, TRUE,
                    CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                    CASE WHEN %s THEN 1 ELSE 0 END,
                    %s, %s, %s, %s::jsonb)
            ON CONFLICT (name_key, address_key) DO UPDATE
            SET name = EXCLUDED.name,
                address = CASE
                  WHEN NULLIF(TRIM(EXCLUDED.address), '') IS NOT NULL THEN EXCLUDED.address
                  ELSE route_locations.address
                END,
                notes = COALESCE(NULLIF(TRIM(EXCLUDED.notes), ''), route_locations.notes),
                customer_id = COALESCE(EXCLUDED.customer_id, route_locations.customer_id),
                active = TRUE,
                last_used_at = CASE
                  WHEN %s THEN CURRENT_TIMESTAMP
                  ELSE route_locations.last_used_at
                END,
                usage_count = COALESCE(route_locations.usage_count, 0) + CASE WHEN %s THEN 1 ELSE 0 END,
                source_system = COALESCE(NULLIF(EXCLUDED.source_system, ''), route_locations.source_system),
                source_sheet = COALESCE(EXCLUDED.source_sheet, route_locations.source_sheet),
                source_row = COALESCE(EXCLUDED.source_row, route_locations.source_row),
                metadata = route_locations.metadata || EXCLUDED.metadata,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            [
                name_key,
                address_key,
                clean_customer_id,
                clean_name,
                clean_address,
                clean_notes,
                touch_usage,
                touch_usage,
                source_system,
                source_sheet,
                source_row,
                _metadata(metadata or {}),
                touch_usage,
                touch_usage,
            ],
        )
        return cur.fetchone()[0]


def _customer_route_address(row: dict[str, Any]) -> str:
    parts = [
        row.get("bejerman_domicilio"),
        row.get("bejerman_localidad"),
        row.get("bejerman_provincia"),
        row.get("bejerman_codigo_postal"),
    ]
    return ", ".join(_clean_text(part) for part in parts if _clean_text(part))


def list_route_locations(query: str = "", limit: int = 30, customer_id: int | None = None) -> dict[str, Any]:
    clean_query = _clean_text(query)
    limit = max(1, min(500, int(limit or 30)))
    clean_customer_id = _optional_int(customer_id)
    params: list[Any] = []
    order_params: list[Any] = []
    where_parts = ["l.active = TRUE"]
    order_sql = "l.usage_count DESC, l.last_used_at DESC NULLS LAST, l.name ASC"
    if clean_query:
        like = f"%{clean_query}%"
        where_parts.append(
            "(l.name ILIKE %s OR l.address ILIKE %s OR c.razon_social ILIKE %s OR COALESCE(c.cod_empresa, c.bejerman_cod_empresa, '') ILIKE %s)"
        )
        params.extend([like, like, like, like])
        order_sql = "CASE WHEN l.name ILIKE %s THEN 0 WHEN l.address ILIKE %s THEN 1 ELSE 2 END, l.usage_count DESC, l.name ASC"
        order_params.extend([f"{clean_query}%", f"{clean_query}%"])
    if clean_customer_id:
        where_parts.append("l.customer_id = %s")
        params.append(clean_customer_id)
    where = "WHERE " + " AND ".join(where_parts)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT l.*,
                   c.razon_social AS customer_name,
                   COALESCE(c.cod_empresa, c.bejerman_cod_empresa, '') AS customer_code
              FROM route_locations l
              LEFT JOIN customers c ON c.id = l.customer_id
              {where}
             ORDER BY {order_sql}
             LIMIT %s
            """,
            [*params, *order_params, limit],
        )
        items = [_serialize_location(row) for row in _rows(cur)]

        remaining = max(0, limit - len(items))
        if remaining and not clean_customer_id and clean_query:
            like = f"%{clean_query}%"
            cur.execute(
                """
                SELECT c.id AS customer_id,
                       c.razon_social AS name,
                       c.razon_social AS customer_name,
                       COALESCE(c.cod_empresa, c.bejerman_cod_empresa, '') AS customer_code,
                       c.bejerman_domicilio,
                       c.bejerman_localidad,
                       c.bejerman_provincia,
                       c.bejerman_codigo_postal
                  FROM customers c
                 WHERE c.razon_social ILIKE %s
                    OR COALESCE(c.cod_empresa, c.bejerman_cod_empresa, '') ILIKE %s
                    OR COALESCE(c.alias_interno, '') ILIKE %s
                    OR COALESCE(c.bejerman_domicilio, '') ILIKE %s
                 ORDER BY CASE WHEN c.razon_social ILIKE %s THEN 0 ELSE 1 END, c.razon_social ASC
                 LIMIT %s
                """,
                [like, like, like, like, f"{clean_query}%", remaining],
            )
            seen_customer_keys = {
                (item.get("customerId"), route_address_key(item.get("address"))) for item in items
            }
            for row in _rows(cur):
                address = _customer_route_address(row)
                if not address:
                    continue
                key = (row.get("customer_id"), route_address_key(address))
                if key in seen_customer_keys:
                    continue
                items.append(
                    _serialize_location(
                        {
                            "id": None,
                            "source_type": "customer",
                            "name": row.get("name") or "",
                            "address": address,
                            "customer_id": row.get("customer_id"),
                            "customer_name": row.get("customer_name") or "",
                            "customer_code": row.get("customer_code") or "",
                            "active": True,
                            "usage_count": 0,
                            "metadata": {},
                        }
                    )
                )
                if len(items) >= limit:
                    break
    if clean_query and len(items) < limit:
        items.append(
            _serialize_location(
                {
                    "id": None,
                    "source_type": "manual",
                    "name": clean_query,
                    "address": "",
                    "active": True,
                    "usage_count": 0,
                    "metadata": {},
                }
            )
        )
    return {"items": items}


def create_route_location(payload: dict[str, Any]) -> dict[str, Any]:
    name = _clean_text(payload.get("name") or payload.get("placeName") or payload.get("place_name"))
    address = _clean_text(payload.get("address"))
    if not name:
        raise RouteSheetError("ROUTE_LOCATION_NAME_REQUIRED", "El nombre del lugar es obligatorio")
    location_id = upsert_route_location(
        name,
        address,
        customer_id=_optional_int(payload.get("customerId") or payload.get("customer_id")),
        notes=_clean_text(payload.get("notes")),
        source_system="nexora",
        metadata={"source": "route_location_form"},
    )
    if not location_id:
        raise RouteSheetError("ROUTE_LOCATION_NOT_CREATED", "No se pudo crear el lugar de Hoja de ruta")
    return _serialize_location(get_route_location(location_id) or {})


def update_route_location(location_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    location_id = _optional_int(location_id)
    if not location_id:
        raise RouteSheetError("ROUTE_LOCATION_NOT_FOUND", "Lugar de Hoja de ruta no encontrado", status_code=404)
    current = get_route_location(location_id)
    if not current:
        raise RouteSheetError("ROUTE_LOCATION_NOT_FOUND", "Lugar de Hoja de ruta no encontrado", status_code=404)

    name = _clean_text(payload.get("name") or payload.get("placeName") or current.get("name"))
    address = _clean_text(payload.get("address") if "address" in payload else current.get("address"))
    if not name:
        raise RouteSheetError("ROUTE_LOCATION_NAME_REQUIRED", "El nombre del lugar es obligatorio")
    active = payload.get("active")
    if active is None:
        active = current.get("active") if current.get("active") is not None else True
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE route_locations
                   SET name = %s,
                       name_key = %s,
                       address = %s,
                       address_key = %s,
                       customer_id = %s,
                       notes = %s,
                       active = %s
                 WHERE id = %s
                """,
                [
                    name,
                    route_location_key(name),
                    address,
                    route_address_key(address),
                    _optional_int(payload.get("customerId") or payload.get("customer_id"))
                    if ("customerId" in payload or "customer_id" in payload)
                    else current.get("customer_id"),
                    _clean_text(payload.get("notes")) if "notes" in payload else current.get("notes"),
                    _bool_value(active, default=True),
                    location_id,
                ],
            )
    except IntegrityError as exc:
        raise RouteSheetError(
            "ROUTE_LOCATION_CONFLICT",
            "Ya existe un lugar con el mismo nombre y dirección",
            status_code=409,
        ) from exc
    return _serialize_location(get_route_location(location_id) or {})


def _stop_select_sql(suffix: str = "") -> str:
    return f"""
        SELECT s.*,
               o.id AS order_id,
               o.order_number,
               o.customer_name AS order_customer_name,
               o.status AS order_status,
               o.remito_number AS order_remito_number,
               o.delivery_type AS order_delivery_type,
               o.ingreso_id AS order_ingreso_id,
               o.source_reference AS order_source_reference,
               o.raw_pedido AS order_raw_pedido,
               l.customer_id AS location_customer_id,
               CASE
                 WHEN l.id IS NULL THEN ''
                 WHEN l.customer_id IS NOT NULL THEN 'customer_location'
                 ELSE 'route_location'
               END AS location_source_type
          FROM route_stops s
          LEFT JOIN route_locations l ON l.id = s.location_id
          LEFT JOIN delivery_orders o ON o.id = s.delivery_order_id
          {suffix}
    """


def get_route_stop(stop_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = "FOR UPDATE OF s" if for_update else ""
    with connection.cursor() as cur:
        cur.execute(
            _stop_select_sql("WHERE s.id = %s") + f" {lock}",
            [stop_id],
        )
        row = _one(cur)
    if not row:
        raise RouteSheetError("ROUTE_STOP_NOT_FOUND", "Parada de Hoja de ruta no encontrada", status_code=404)
    return row


def list_route_stops(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    route_date = _date_value(filters.get("date") or filters.get("routeDate"), required=True)
    with connection.cursor() as cur:
        cur.execute(
            _stop_select_sql(
                """
                WHERE s.route_date = %s
                  AND s.status <> 'cancelado'
                ORDER BY s.sort_order ASC, s.created_at ASC, s.id ASC
                """
            ),
            [route_date],
        )
        rows = _rows(cur)
    return {"items": [_serialize_stop(row) for row in rows], "date": route_date.isoformat()}


def _next_sort_order(route_date: date) -> int:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM route_stops WHERE route_date = %s",
            [route_date],
        )
        return int(cur.fetchone()[0] or 0)


def _create_event(stop_id: str, actor_user_id: int | None, event_type: str, *, note: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_stop_events (stop_id, actor_user_id, event_type, note, metadata)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            [stop_id, actor_user_id, event_type, note, _metadata(metadata or {})],
        )


def _delivery_order_row(order_id: str) -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute("SELECT * FROM delivery_orders WHERE id = %s", [order_id])
        row = _one(cur)
    if not row:
        raise RouteSheetError("DELIVERY_ORDER_NOT_FOUND", "Orden de entrega no encontrada", status_code=404)
    return row


def _ensure_delivery_order_available(order_id: str | None, *, exclude_stop_id: str | None = None) -> None:
    if not order_id:
        return
    params: list[Any] = [order_id, sorted(ROUTE_STOP_ACTIVE_STATUSES)]
    extra = ""
    if exclude_stop_id:
        extra = "AND id <> %s"
        params.append(exclude_stop_id)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id
              FROM route_stops
             WHERE delivery_order_id = %s
               AND status = ANY(%s)
               {extra}
             LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
    if row:
        raise RouteSheetError(
            "DELIVERY_ORDER_ALREADY_ROUTED",
            "La orden ya está vinculada a una parada activa de Hoja de ruta",
            status_code=409,
        )


def _fields_from_payload(payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    route_date = _date_value(
        payload.get("routeDate") if "routeDate" in payload else payload.get("date") if "date" in payload else (current or {}).get("route_date"),
        field="routeDate",
        required=True,
    )
    requested_date = _date_value(
        payload.get("requestedDate") if "requestedDate" in payload else (current or {}).get("requested_date"),
        field="requestedDate",
    )
    delivery_order_id = (
        _optional_text(payload.get("deliveryOrderId") or payload.get("delivery_order_id"))
        if ("deliveryOrderId" in payload or "delivery_order_id" in payload)
        else (current or {}).get("delivery_order_id")
    )
    order_row = _delivery_order_row(delivery_order_id) if delivery_order_id else None
    if order_row and order_row.get("status") == "pendiente_stock":
        raise RouteSheetError(
            "DELIVERY_ORDER_STOCK_PENDING",
            "La orden está pendiente de stock",
            status_code=409,
        )

    customer_id = (
        _optional_int(payload.get("customerId") or payload.get("customer_id"))
        if ("customerId" in payload or "customer_id" in payload)
        else None
    )
    location_id = (
        _optional_int(payload.get("locationId") or payload.get("location_id"))
        if ("locationId" in payload or "location_id" in payload)
        else (current or {}).get("location_id")
    )
    location = get_route_location(location_id) if location_id else None
    if location_id and not location:
        raise RouteSheetError("ROUTE_LOCATION_NOT_FOUND", "Lugar de Hoja de ruta no encontrado", status_code=404)

    place_name = _clean_text(payload.get("placeName") if "placeName" in payload else payload.get("place_name") if "place_name" in payload else "")
    address = _clean_text(payload.get("address") if "address" in payload else "")
    task = _clean_text(payload.get("task") if "task" in payload else "")

    if current:
        place_name = place_name if ("placeName" in payload or "place_name" in payload) else current.get("place_name") or ""
        address = address if "address" in payload else current.get("address") or ""
        task = task if "task" in payload else current.get("task") or ""

    if location:
        place_name = place_name or location.get("name") or ""
        address = address or location.get("address") or ""
        customer_id = customer_id or location.get("customer_id")
    if order_row:
        place_name = place_name or order_row.get("customer_name") or ""
        task = task or f"Entregar {delivery_order_number(order_row)}"
        customer_id = customer_id or order_row.get("customer_id")

    if not place_name and not task:
        raise RouteSheetError("ROUTE_STOP_CONTENT_REQUIRED", "La parada necesita lugar o tarea")

    if location_id and location:
        location_name_key = route_location_key(location.get("name"))
        location_address_key = route_address_key(location.get("address"))
        if (
            route_location_key(place_name) != location_name_key
            or route_address_key(address) != location_address_key
        ):
            location_id = upsert_route_location(
                place_name,
                address,
                customer_id=customer_id,
                source_system="nexora",
                metadata={"source": "route_stop", "previousLocationId": location.get("id")},
                touch_usage=True,
            )
        else:
            _touch_route_location(location_id)
    elif place_name:
        location_id = upsert_route_location(
            place_name,
            address,
            customer_id=customer_id,
            source_system="nexora",
            metadata={"source": "route_stop"},
            touch_usage=True,
        )

    return {
        "route_date": route_date,
        "requested_date": requested_date,
        "requester_name": _clean_text(
            payload.get("requesterName")
            if "requesterName" in payload
            else payload.get("requester_name")
            if "requester_name" in payload
            else (current or {}).get("requester_name")
        ),
        "time_window": _clean_text(
            payload.get("timeWindow")
            if "timeWindow" in payload
            else payload.get("time_window")
            if "time_window" in payload
            else (current or {}).get("time_window")
        ),
        "location_id": location_id,
        "place_name": place_name,
        "address": address,
        "task": task,
        "sort_order": (
            _optional_int(payload.get("sortOrder") if "sortOrder" in payload else payload.get("sort_order"))
            if ("sortOrder" in payload or "sort_order" in payload)
            else (current or {}).get("sort_order")
        ),
        "delivery_order_id": delivery_order_id,
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else (current or {}).get("metadata") or {},
    }


@transaction.atomic
def create_route_stop(payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    fields = _fields_from_payload(payload or {})
    _ensure_delivery_order_available(fields.get("delivery_order_id"))
    stop_id = _optional_text((payload or {}).get("id")) or f"rs-{uuid4()}"
    sort_order = fields["sort_order"] if fields["sort_order"] is not None else _next_sort_order(fields["route_date"])
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO route_stops (
                  id, route_date, requested_date, requester_name, time_window, location_id,
                  place_name, address, task, sort_order, delivery_order_id, metadata, created_by_user_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                [
                    stop_id,
                    fields["route_date"],
                    fields["requested_date"],
                    fields["requester_name"],
                    fields["time_window"],
                    fields["location_id"],
                    fields["place_name"],
                    fields["address"],
                    fields["task"],
                    sort_order,
                    fields["delivery_order_id"],
                    _metadata(fields["metadata"]),
                    actor_user_id,
                ],
            )
    except IntegrityError as exc:
        raise RouteSheetError(
            "ROUTE_STOP_CONFLICT",
            "No se pudo crear la parada porque ya existe un vínculo activo o una fila de origen duplicada",
            status_code=409,
        ) from exc
    _create_event(stop_id, actor_user_id, "route_stop_created", metadata={"deliveryOrderId": fields["delivery_order_id"]})
    created = get_route_stop(stop_id)
    _notify_route_stop_created(created)
    return _serialize_stop(created)


@transaction.atomic
def update_route_stop(stop_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    current = get_route_stop(stop_id, for_update=True)
    if current.get("status") in {"completado", "cancelado"}:
        raise RouteSheetError("ROUTE_STOP_CLOSED", "No se puede editar una parada cerrada", status_code=409)
    fields = _fields_from_payload(payload or {}, current)
    _ensure_delivery_order_available(fields.get("delivery_order_id"), exclude_stop_id=stop_id)
    sort_order = fields["sort_order"] if fields["sort_order"] is not None else current.get("sort_order") or 0
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE route_stops
                   SET route_date = %s,
                       requested_date = %s,
                       requester_name = %s,
                       time_window = %s,
                       location_id = %s,
                       place_name = %s,
                       address = %s,
                       task = %s,
                       sort_order = %s,
                       delivery_order_id = %s,
                       metadata = %s::jsonb,
                       updated_by_user_id = %s
                 WHERE id = %s
                """,
                [
                    fields["route_date"],
                    fields["requested_date"],
                    fields["requester_name"],
                    fields["time_window"],
                    fields["location_id"],
                    fields["place_name"],
                    fields["address"],
                    fields["task"],
                    sort_order,
                    fields["delivery_order_id"],
                    _metadata(fields["metadata"]),
                    actor_user_id,
                    stop_id,
                ],
            )
    except IntegrityError as exc:
        raise RouteSheetError(
            "ROUTE_STOP_CONFLICT",
            "No se pudo actualizar la parada porque ya existe un vínculo activo o una fila de origen duplicada",
            status_code=409,
        ) from exc
    _create_event(stop_id, actor_user_id, "route_stop_updated", metadata={"deliveryOrderId": fields["delivery_order_id"]})
    return _serialize_stop(get_route_stop(stop_id))


@transaction.atomic
def complete_route_stop(stop_id: str, actor_user_id: int | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    current = get_route_stop(stop_id, for_update=True)
    if current.get("status") == "cancelado":
        raise RouteSheetError("ROUTE_STOP_CLOSED", "No se puede completar una parada cancelada", status_code=409)
    if current.get("status") == "completado":
        return _serialize_stop(current)

    delivered_order = None
    delivery_order_id = _optional_text(current.get("delivery_order_id"))
    if delivery_order_id:
        order = get_delivery_order(delivery_order_id, include_events=False, for_update=True)
        if order.get("status") == "cancelado":
            raise RouteSheetError("DELIVERY_ORDER_CLOSED", "La orden vinculada está cancelada", status_code=409)
        if order.get("status") not in CLOSED_STATUSES:
            try:
                delivered_order = mark_delivered(delivery_order_id, actor_user_id, payload.get("remitoNumber"))
            except DeliveryOrderError:
                raise

    note = _optional_text(payload.get("note") or payload.get("completedNote"))
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE route_stops
               SET status = 'completado',
                   completed_by_user_id = %s,
                   completed_at = CURRENT_TIMESTAMP,
                   completed_note = %s,
                   updated_by_user_id = %s
             WHERE id = %s
            """,
            [actor_user_id, note, actor_user_id, stop_id],
        )
    _create_event(
        stop_id,
        actor_user_id,
        "route_stop_completed",
        note=note,
        metadata={"deliveryOrderId": delivery_order_id, "deliveredOrder": delivered_order or None},
    )
    if delivered_order:
        _create_event(
            stop_id,
            actor_user_id,
            "route_stop_delivery_order_delivered",
            metadata={"deliveryOrderId": delivery_order_id, "status": delivered_order.get("status")},
        )
    return _serialize_stop(get_route_stop(stop_id))


@transaction.atomic
def postpone_route_stop(stop_id: str, actor_user_id: int | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    current = get_route_stop(stop_id, for_update=True)
    if current.get("status") in {"completado", "cancelado"}:
        raise RouteSheetError("ROUTE_STOP_CLOSED", "No se puede posponer una parada cerrada", status_code=409)
    target_date = _date_value(
        payload.get("routeDate") or payload.get("date") or payload.get("postponeDate"),
        field="routeDate",
        required=True,
    )
    current_date = _date_value(current.get("route_date"), field="routeDate", required=True)
    if target_date <= current_date:
        raise RouteSheetError(
            "ROUTE_POSTPONE_DATE_INVALID",
            "La nueva fecha debe ser posterior a la fecha actual",
            status_code=400,
        )
    note = _optional_text(payload.get("note") or payload.get("postponeNote"))
    sort_order = _next_sort_order(target_date)
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE route_stops
               SET route_date = %s,
                   status = 'pendiente',
                   sort_order = %s,
                   postponed_by_user_id = %s,
                   postponed_at = CURRENT_TIMESTAMP,
                   postpone_note = %s,
                   updated_by_user_id = %s
             WHERE id = %s
            """,
            [target_date, sort_order, actor_user_id, note, actor_user_id, stop_id],
        )
    _create_event(
        stop_id,
        actor_user_id,
        "route_stop_postponed",
        note=note,
        metadata={
            "fromRouteDate": current_date.isoformat(),
            "toRouteDate": target_date.isoformat(),
            "sortOrder": sort_order,
        },
    )
    return _serialize_stop(get_route_stop(stop_id))


@transaction.atomic
def cancel_route_stop(stop_id: str, actor_user_id: int | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    current = get_route_stop(stop_id, for_update=True)
    if current.get("status") == "completado":
        raise RouteSheetError("ROUTE_STOP_CLOSED", "No se puede cancelar una parada completada", status_code=409)
    note = _optional_text(payload.get("note") or payload.get("cancelledNote"))
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE route_stops
               SET status = 'cancelado',
                   cancelled_by_user_id = %s,
                   cancelled_at = CURRENT_TIMESTAMP,
                   cancelled_note = %s,
                   updated_by_user_id = %s
             WHERE id = %s
            """,
            [actor_user_id, note, actor_user_id, stop_id],
        )
    _create_event(stop_id, actor_user_id, "route_stop_cancelled", note=note)
    return _serialize_stop(get_route_stop(stop_id))


@transaction.atomic
def reorder_route_stops(payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    route_date = _date_value(payload.get("routeDate") or payload.get("date"), required=True)
    raw_ids = payload.get("ids") or payload.get("stopIds") or []
    ids = [_optional_text(item) for item in raw_ids if _optional_text(item)]
    if not ids:
        raise RouteSheetError("ROUTE_STOP_IDS_REQUIRED", "No hay paradas para reordenar")
    with connection.cursor() as cur:
        for index, stop_id in enumerate(ids):
            cur.execute(
                """
                UPDATE route_stops
                   SET sort_order = %s,
                       updated_by_user_id = %s
                 WHERE id = %s
                   AND route_date = %s
                   AND status <> 'cancelado'
                """,
                [index, actor_user_id, stop_id, route_date],
            )
            if cur.rowcount:
                _create_event(stop_id, actor_user_id, "route_stop_reordered", metadata={"sortOrder": index})
    return list_route_stops({"date": route_date.isoformat()})


def list_suggested_delivery_orders(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    limit = max(1, min(200, int(filters.get("limit") or 80)))
    plannable_statuses = "pendiente_armado,armado_pendiente_entrega"
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT delivery_order_id
              FROM route_stops
             WHERE delivery_order_id IS NOT NULL
               AND status = ANY(%s)
            """,
            [sorted(ROUTE_STOP_ACTIVE_STATUSES)],
        )
        active_ids = {row[0] for row in cur.fetchall()}
    orders = list_delivery_orders({"status": plannable_statuses, "limit": limit}).get("items") or []
    return {
        "items": [order for order in orders if order.get("id") not in active_ids],
        "limit": limit,
    }
