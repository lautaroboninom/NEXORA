from __future__ import annotations

import html
import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .delivery_orders import sync_service_release_orders_invoice_from_ingreso
from .notifications import active_users_for_notification, emit_notification, notification_email_recipients_for_users
from .notification_email_outbox import queue_notification_email

logger = logging.getLogger(__name__)

NOTIFICATION_KEY = "service_order_ready_to_bill"


class ServiceOrderBillingError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def q(sql: str, params: list[Any] | None = None, one: bool = False):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        if not cur.description:
            return None
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if one:
            return rows[0] if rows else None
        return rows


def os_label(value: Any) -> str:
    try:
        return str(int(value)).zfill(5)
    except Exception:
        return str(value or "")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(table_name: str) -> bool:
    try:
        row = q(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_name=%s
               AND table_schema = ANY(current_schemas(true))
             LIMIT 1
            """,
            [table_name],
            one=True,
        )
        return bool(row)
    except Exception:
        try:
            transaction.set_rollback(False)
        except Exception:
            pass
        return False


def _table_columns(table_name: str) -> set[str]:
    try:
        rows = q(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_name=%s
               AND table_schema = ANY(current_schemas(true))
            """,
            [table_name],
        ) or []
        return {str(row.get("column_name")) for row in rows}
    except Exception:
        try:
            transaction.set_rollback(False)
        except Exception:
            pass
        return set()


def _col(columns: set[str], alias: str, name: str, default: str = "''", *, cast_text: bool = False) -> str:
    if name not in columns:
        return f"{default} AS {name}"
    expr = f"{alias}.{name}"
    if cast_text:
        expr = f"CAST({expr} AS TEXT)"
    return f"{expr} AS {name}"


def _event_liberation_join() -> tuple[str, str]:
    if not _table_exists("ingreso_events"):
        return "NULL AS fecha_liberacion", ""
    cols = _table_columns("ingreso_events")
    refs = []
    if "ticket_id" in cols:
        refs.append("ev.ticket_id = t.id")
    if "ingreso_id" in cols:
        refs.append("ev.ingreso_id = t.id")
    if not refs or "a_estado" not in cols or "ts" not in cols:
        return "NULL AS fecha_liberacion", ""
    return (
        "ev_liberado.fecha_liberacion",
        f"""
        LEFT JOIN LATERAL (
          SELECT MAX(ev.ts) AS fecha_liberacion
            FROM ingreso_events ev
           WHERE ({" OR ".join(refs)})
             AND ev.a_estado = 'liberado'
        ) ev_liberado ON TRUE
        """,
    )


def _delivery_order_join() -> tuple[str, str]:
    if not (_table_exists("delivery_orders") and _table_exists("delivery_order_items")):
        return (
            """
            dor.service_order_id,
            dor.service_order_number,
            dor.service_order_status,
            dor.service_order_remito_number,
            dor.article_code,
            dor.article_name,
            dor.article_description
            """,
            """
            LEFT JOIN LATERAL (
              SELECT NULL::TEXT AS service_order_id,
                     ''::TEXT AS service_order_number,
                     ''::TEXT AS service_order_status,
                     ''::TEXT AS service_order_remito_number,
                     ''::TEXT AS article_code,
                     ''::TEXT AS article_name,
                     ''::TEXT AS article_description
            ) dor ON TRUE
            """,
        )
    return (
        """
        dor.service_order_id,
        COALESCE(dor.service_order_number, '') AS service_order_number,
        COALESCE(dor.service_order_status, '') AS service_order_status,
        COALESCE(dor.service_order_remito_number, '') AS service_order_remito_number,
        COALESCE(dor.article_code, '') AS article_code,
        COALESCE(dor.article_name, '') AS article_name,
        COALESCE(dor.article_description, '') AS article_description
        """,
        """
        LEFT JOIN LATERAL (
          SELECT o.id AS service_order_id,
                 o.order_number AS service_order_number,
                 o.status AS service_order_status,
                 o.remito_number AS service_order_remito_number,
                 i.article_code,
                 i.article_name,
                 i.description AS article_description
            FROM delivery_orders o
            LEFT JOIN LATERAL (
              SELECT article_code, article_name, description
                FROM delivery_order_items
               WHERE order_id = o.id
               ORDER BY
                     CASE WHEN ingreso_id = t.id THEN 0 ELSE 1 END,
                     sort_order ASC,
                     created_at ASC
               LIMIT 1
            ) i ON TRUE
           WHERE o.delivery_type = 'service_release'
             AND (
                  o.ingreso_id = t.id
                  OR (o.source_system = 'nexora' AND o.source_external_id = CAST(t.id AS TEXT))
                  OR EXISTS (
                      SELECT 1
                        FROM delivery_order_items doi
                       WHERE doi.order_id = o.id
                         AND doi.ingreso_id = t.id
                  )
                  OR EXISTS (
                      SELECT 1
                        FROM delivery_order_item_partidas doip
                        JOIN delivery_order_items doi ON doi.id = doip.order_item_id
                       WHERE doi.order_id = o.id
                         AND doip.ingreso_id = t.id
                  )
                 )
           ORDER BY o.created_at DESC NULLS LAST, o.id DESC
           LIMIT 1
        ) dor ON TRUE
        """,
    )


def _mapping_join(ingreso_cols: set[str] | None = None) -> tuple[str, str]:
    if not _table_exists("bejerman_article_mappings"):
        return "'' AS mapped_article_code, '' AS mapped_article_description", ""
    company_expr = "COALESCE(t.empresa_bejerman, 'SEPID')" if ingreso_cols and "empresa_bejerman" in ingreso_cols else "'SEPID'"
    return (
        """
        COALESCE(bam.article_code, '') AS mapped_article_code,
        COALESCE(bam.article_description, '') AS mapped_article_description
        """,
        f"""
        LEFT JOIN LATERAL (
          SELECT article_code, article_description
            FROM bejerman_article_mappings
           WHERE model_id = d.model_id
             AND company_key = {company_expr}
           ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END,
                    confirmed_at DESC NULLS LAST,
                    updated_at DESC NULLS LAST,
                    id DESC
           LIMIT 1
        ) bam ON TRUE
        """,
    )


def _quote_join(ingreso_columns: set[str]) -> tuple[str, str]:
    if not _table_exists("quotes"):
        return (
            """
            NULL AS presupuesto_quote_id,
            NULL AS presupuesto_version,
            '' AS presupuesto_estado,
            '' AS presupuesto_moneda,
            NULL AS presupuesto_neto,
            NULL AS presupuesto_iva_21,
            NULL AS presupuesto_total,
            '' AS presupuesto_forma_pago,
            '' AS presupuesto_plazo_entrega
            """,
            "",
        )

    quote_columns = _table_columns("quotes")
    has_quote_items = _table_exists("quote_items")
    if has_quote_items:
        items_join = "LEFT JOIN quote_items qi ON qi.quote_id = q.id"
        net_expr = "COALESCE(SUM(ROUND(qi.qty * qi.precio_u, 2)), q.subtotal, 0)"
        group_by = "GROUP BY q.id"
    else:
        items_join = ""
        net_expr = "COALESCE(q.subtotal, 0)"
        group_by = ""

    version_expr = "COALESCE(q.version_num, 1)" if "version_num" in quote_columns else "1"
    currency_expr = "COALESCE(NULLIF(TRIM(q.moneda), ''), 'ARS')" if "moneda" in quote_columns else "'ARS'"
    payment_expr = "COALESCE(q.forma_pago, '')" if "forma_pago" in quote_columns else "''"
    deadline_expr = "COALESCE(q.plazo_entrega_txt, '')" if "plazo_entrega_txt" in quote_columns else "''"
    preferred_rejected_quote_order = (
        "CASE WHEN t.resolucion = 'presupuesto_rechazado' "
        "AND q.id = t.presupuesto_rechazado_quote_id THEN 0 ELSE 1 END,"
        if {"presupuesto_rechazado_quote_id", "resolucion"}.issubset(ingreso_columns)
        else ""
    )
    rejected_quote_filter = (
        "AND (COALESCE(CAST(t.resolucion AS TEXT), '') <> 'presupuesto_rechazado' "
        "OR CAST(q.estado AS TEXT) = 'rechazado')"
        if "resolucion" in ingreso_columns
        else ""
    )
    return (
        """
        presupuesto.presupuesto_quote_id,
        presupuesto.presupuesto_version,
        presupuesto.presupuesto_estado,
        presupuesto.presupuesto_moneda,
        presupuesto.presupuesto_neto,
        presupuesto.presupuesto_iva_21,
        presupuesto.presupuesto_total,
        presupuesto.presupuesto_forma_pago,
        presupuesto.presupuesto_plazo_entrega
        """,
        f"""
        LEFT JOIN LATERAL (
          SELECT q.id AS presupuesto_quote_id,
                 {version_expr} AS presupuesto_version,
                 CAST(q.estado AS TEXT) AS presupuesto_estado,
                 {currency_expr} AS presupuesto_moneda,
                 {net_expr} AS presupuesto_neto,
                 ROUND(({net_expr}) * 0.21, 2) AS presupuesto_iva_21,
                 ROUND(({net_expr}) * 1.21, 2) AS presupuesto_total,
                 {payment_expr} AS presupuesto_forma_pago,
                 {deadline_expr} AS presupuesto_plazo_entrega
            FROM quotes q
            {items_join}
           WHERE q.ingreso_id = t.id
           {rejected_quote_filter}
           {group_by}
           ORDER BY {preferred_rejected_quote_order} {version_expr} DESC, q.id DESC
           LIMIT 1
        ) presupuesto ON TRUE
        """,
    )


def _quote_items(quote_id: int | None) -> list[dict[str, Any]]:
    if not quote_id or not _table_exists("quote_items"):
        return []
    rows = q(
        """
        SELECT CAST(tipo AS TEXT) AS tipo,
               COALESCE(descripcion, '') AS descripcion,
               qty,
               precio_u,
               ROUND(qty * precio_u, 2) AS subtotal
          FROM quote_items
         WHERE quote_id = %s
         ORDER BY id
        """,
        [int(quote_id)],
    ) or []
    return [
        {
            "tipo": _clean(row.get("tipo")),
            "descripcion": _clean(row.get("descripcion")),
            "cantidad": row.get("qty"),
            "precioUnitario": row.get("precio_u"),
            "subtotal": row.get("subtotal"),
        }
        for row in rows
    ]


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(value: Any, currency: Any = "ARS") -> str:
    amount = _decimal_or_none(value)
    if amount is None:
        return "-"
    formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    currency_code = _clean(currency).upper() or "ARS"
    symbol = {"ARS": "$", "USD": "U$S"}.get(currency_code, currency_code)
    return f"{symbol} {formatted}"


def _quantity(value: Any) -> str:
    amount = _decimal_or_none(value)
    if amount is None:
        return "-"
    if amount == amount.to_integral():
        return str(int(amount))
    return f"{amount:f}".rstrip("0").rstrip(".").replace(".", ",")


def _status_label(value: Any) -> str:
    raw = _clean(value).replace("_", " ")
    return raw[:1].upper() + raw[1:] if raw else "-"


def _date_time(value: Any) -> str:
    if not value:
        return "-"
    try:
        resolved = timezone.localtime(value) if timezone.is_aware(value) else value
        return resolved.strftime("%d/%m/%Y %H:%M")
    except (AttributeError, TypeError, ValueError):
        return _clean(value) or "-"


def _service_order_select_sql() -> tuple[str, list[Any]]:
    ingreso_cols = _table_columns("ingresos")
    customer_cols = _table_columns("customers")
    device_cols = _table_columns("devices")
    model_cols = _table_columns("models")
    marca_cols = _table_columns("marcas")
    user_cols = _table_columns("users")
    fecha_liberacion_sql, event_join = _event_liberation_join()
    delivery_select, delivery_join = _delivery_order_join()
    mapping_select, mapping_join = _mapping_join(ingreso_cols)
    quote_select, quote_join = _quote_join(ingreso_cols)

    return (
        f"""
        SELECT
          t.id AS id,
          t.id AS ingreso_id,
          CAST(t.estado AS TEXT) AS estado,
          {_col(ingreso_cols, "t", "motivo", "''", cast_text=True)},
          {_col(ingreso_cols, "t", "resolucion", "''", cast_text=True)},
          {_col(ingreso_cols, "t", "fecha_ingreso", "NULL")},
          {_col(ingreso_cols, "t", "fecha_creacion", "NULL")},
          {_col(ingreso_cols, "t", "fecha_servicio", "NULL")},
          {_col(ingreso_cols, "t", "fecha_entrega", "NULL")},
          {_col(ingreso_cols, "t", "remito_salida", "''")},
          {_col(ingreso_cols, "t", "factura_numero", "''")},
          {_col(ingreso_cols, "t", "descripcion_problema", "''")},
          {_col(ingreso_cols, "t", "trabajos_realizados", "''")},
          {_col(ingreso_cols, "t", "equipo_variante", "''")},
          {_col(ingreso_cols, "t", "presupuesto_rechazado_cobro_neto", "NULL")},
          {fecha_liberacion_sql},
          c.id AS customer_id,
          {_col(customer_cols, "c", "razon_social", "''")},
          {_col(customer_cols, "c", "cod_empresa", "''")},
          {_col(customer_cols, "c", "cuit", "''")},
          d.id AS device_id,
          {_col(device_cols, "d", "numero_serie", "''")},
          {_col(device_cols, "d", "numero_interno", "''")},
          {_col(device_cols, "d", "variante", "''")},
          COALESCE(b.nombre, '') AS marca,
          {_col(model_cols, "m", "nombre", "''")},
          {_col(model_cols, "m", "tipo_equipo", "''")},
          {_col(model_cols, "m", "variante", "''")},
          {"u.nombre AS tecnico_nombre" if "nombre" in user_cols else "'' AS tecnico_nombre"},
          {delivery_select},
          {mapping_select},
          {quote_select}
        FROM ingresos t
        JOIN devices d ON d.id = t.device_id
        JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        LEFT JOIN users u ON u.id = t.asignado_a
        {event_join}
        {delivery_join}
        {mapping_join}
        {quote_join}
        """,
        [],
    )


def _concept_code(row: dict[str, Any]) -> str:
    return _clean(row.get("article_code")) or _clean(row.get("mapped_article_code"))


def _concept_description(row: dict[str, Any]) -> str:
    return (
        _clean(row.get("article_name"))
        or _clean(row.get("article_description"))
        or _clean(row.get("mapped_article_description"))
        or equipment_label(row)
    )


def equipment_label(row: dict[str, Any]) -> str:
    model_text = " ".join(
        part
        for part in [
            _clean(row.get("nombre")),
            _clean(row.get("equipo_variante")) or _clean(row.get("variante")),
        ]
        if part
    )
    return " | ".join(
        part
        for part in [_clean(row.get("tipo_equipo")), _clean(row.get("marca")), model_text]
        if part
    ) or "Equipo"


def _format_service_order_item(
    row: dict[str, Any],
    *,
    include_budget_items: bool = False,
) -> dict[str, Any]:
    ingreso_id = int(row.get("ingreso_id"))
    remito = _clean(row.get("remito_salida")) or _clean(row.get("service_order_remito_number"))
    quote_id = row.get("presupuesto_quote_id")
    item = {
        "id": ingreso_id,
        "ingresoId": ingreso_id,
        "os": os_label(ingreso_id),
        "estado": _clean(row.get("estado")),
        "cliente": _clean(row.get("razon_social")),
        "customerId": row.get("customer_id"),
        "bejermanCustomerCode": _clean(row.get("cod_empresa")),
        "cuit": _clean(row.get("cuit")),
        "equipo": equipment_label(row),
        "marca": _clean(row.get("marca")),
        "modelo": _clean(row.get("nombre")),
        "tipoEquipo": _clean(row.get("tipo_equipo")),
        "numeroSerie": _clean(row.get("numero_serie")),
        "numeroInterno": _clean(row.get("numero_interno")),
        "conceptCode": _concept_code(row),
        "conceptDescription": _concept_description(row),
        "resolucion": _clean(row.get("resolucion")),
        "motivo": _clean(row.get("motivo")),
        "descripcionProblema": _clean(row.get("descripcion_problema")),
        "trabajosRealizados": _clean(row.get("trabajos_realizados")),
        "tecnico": _clean(row.get("tecnico_nombre")),
        "fechaLiberacion": row.get("fecha_liberacion"),
        "fechaEntrega": row.get("fecha_entrega"),
        "fechaServicio": row.get("fecha_servicio"),
        "remitoSalida": remito,
        "rss": remito or "RSS pendiente",
        "facturaNumero": _clean(row.get("factura_numero")),
        "serviceOrderId": row.get("service_order_id"),
        "serviceOrderNumber": _clean(row.get("service_order_number")),
        "serviceOrderStatus": _clean(row.get("service_order_status")),
        "presupuestoQuoteId": quote_id,
        "presupuestoVersion": row.get("presupuesto_version"),
        "presupuestoEstado": _clean(row.get("presupuesto_estado")),
        "presupuestoMoneda": _clean(row.get("presupuesto_moneda")) or "ARS",
        "presupuestoNeto": row.get("presupuesto_neto"),
        "presupuestoIva21": row.get("presupuesto_iva_21"),
        "presupuestoTotal": row.get("presupuesto_total"),
        "presupuestoFormaPago": _clean(row.get("presupuesto_forma_pago")),
        "presupuestoPlazoEntrega": _clean(row.get("presupuesto_plazo_entrega")),
        "presupuestoRechazadoCobroNeto": row.get("presupuesto_rechazado_cobro_neto"),
        "presupuestoItems": [],
        "href": f"/cobranzas/facturacion?serviceOrderId={ingreso_id}",
        "pdfUrl": f"/api/cobranzas/os-a-facturar/{ingreso_id}/pdf/",
    }
    if include_budget_items:
        item["presupuestoItems"] = _quote_items(quote_id)
    return item


def list_service_orders_to_bill(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    sql, params = _service_order_select_sql()
    where = [
        "CAST(t.estado AS TEXT) IN ('liberado', 'entregado')",
        "NULLIF(TRIM(COALESCE(t.factura_numero, '')), '') IS NULL",
    ]
    search = _clean(filters.get("q") or filters.get("search"))
    if search:
        where.append(
            """
            (
              CAST(t.id AS TEXT) ILIKE %s OR
              COALESCE(c.razon_social, '') ILIKE %s OR
              COALESCE(d.numero_serie, '') ILIKE %s OR
              COALESCE(d.numero_interno, '') ILIKE %s OR
              COALESCE(dor.article_code, '') ILIKE %s OR
              COALESCE(dor.article_name, '') ILIKE %s
            )
            """
        )
        term = f"%{search}%"
        params.extend([term, term, term, term, term, term])
    try:
        limit = int(filters.get("limit") or 200)
    except Exception:
        limit = 200
    limit = max(1, min(500, limit))
    rows = q(
        f"""
        {sql}
         WHERE {" AND ".join(where)}
         ORDER BY COALESCE(fecha_liberacion, t.fecha_entrega, t.fecha_servicio, t.fecha_ingreso, t.fecha_creacion) DESC NULLS LAST,
                  t.id DESC
         LIMIT %s
        """,
        [*params, limit],
    ) or []
    items = [_format_service_order_item(row) for row in rows]
    return {"items": items, "total": len(items)}


def get_service_order_billing_item(ingreso_id: int, *, include_invoiced: bool = True) -> dict[str, Any] | None:
    sql, params = _service_order_select_sql()
    where = ["t.id = %s", "CAST(t.estado AS TEXT) IN ('liberado', 'entregado')"]
    params.append(int(ingreso_id))
    if not include_invoiced:
        where.append("NULLIF(TRIM(COALESCE(t.factura_numero, '')), '') IS NULL")
    row = q(f"{sql} WHERE {' AND '.join(where)} LIMIT 1", params, one=True)
    return _format_service_order_item(row, include_budget_items=True) if row else None


@transaction.atomic
def register_service_order_invoice(ingreso_id: int, factura_numero: str, actor_user_id: int | None = None) -> dict[str, Any]:
    invoice = _clean(factura_numero)
    if not invoice:
        raise ServiceOrderBillingError("INVOICE_REQUIRED", "Ingrese el número de factura.", status_code=400)

    item = get_service_order_billing_item(ingreso_id, include_invoiced=True)
    if not item:
        raise ServiceOrderBillingError("SERVICE_ORDER_NOT_BILLABLE", "La OS no está lista para facturar.", status_code=404)
    current = _clean(item.get("facturaNumero"))
    if current:
        if current == invoice:
            sync_service_release_orders_invoice_from_ingreso(ingreso_id, invoice, actor_user_id)
            return item
        raise ServiceOrderBillingError("SERVICE_ORDER_ALREADY_INVOICED", "La OS ya tiene una factura registrada.", status_code=409)

    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE ingresos
               SET factura_numero = %s
             WHERE id = %s
               AND NULLIF(TRIM(COALESCE(factura_numero, '')), '') IS NULL
            """,
            [invoice, int(ingreso_id)],
        )
        if not cur.rowcount:
            raise ServiceOrderBillingError("SERVICE_ORDER_ALREADY_INVOICED", "La OS ya tiene una factura registrada.", status_code=409)

    sync_service_release_orders_invoice_from_ingreso(ingreso_id, invoice, actor_user_id)
    updated = get_service_order_billing_item(ingreso_id, include_invoiced=True)
    return updated or {**item, "facturaNumero": invoice}


def _frontend_url(request: Any, path: str) -> str:
    base = _clean(getattr(settings, "PUBLIC_WEB_URL", "") or getattr(settings, "FRONTEND_ORIGIN", ""))
    if base:
        return f"{base.rstrip('/')}{path}"
    try:
        if request is not None:
            return request.build_absolute_uri(path)
    except Exception:
        pass
    return path


def _billing_amounts(item: dict[str, Any]) -> dict[str, Any]:
    currency = _clean(item.get("presupuestoMoneda")).upper() or "ARS"
    quote_net = _decimal_or_none(item.get("presupuestoNeto"))
    quote_vat = _decimal_or_none(item.get("presupuestoIva21"))
    quote_total = _decimal_or_none(item.get("presupuestoTotal"))
    rejected_charge = _decimal_or_none(item.get("presupuestoRechazadoCobroNeto"))
    is_rejected_charge = (
        _clean(item.get("resolucion")).lower() == "presupuesto_rechazado"
        and rejected_charge is not None
    )
    if is_rejected_charge:
        billing_net = rejected_charge
        billing_vat = (billing_net * Decimal("0.21")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        billing_total = billing_net + billing_vat
        billing_label = "Cobro autorizado por presupuesto rechazado"
    else:
        billing_net = quote_net
        billing_vat = quote_vat
        billing_total = quote_total
        billing_label = "Importe del presupuesto"
    return {
        "currency": currency,
        "quoteNet": quote_net,
        "quoteVat": quote_vat,
        "quoteTotal": quote_total,
        "billingNet": billing_net,
        "billingVat": billing_vat,
        "billingTotal": billing_total,
        "billingLabel": billing_label,
        "usesRejectedCharge": is_rejected_charge,
    }


def _budget_item_lines(item: dict[str, Any]) -> list[str]:
    currency = _clean(item.get("presupuestoMoneda")) or "ARS"
    rows = item.get("presupuestoItems") if isinstance(item.get("presupuestoItems"), list) else []
    lines = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        description = _clean(row.get("descripcion")) or "Ítem sin descripción"
        lines.append(
            f"- {_quantity(row.get('cantidad'))} x {description} | "
            f"Unitario: {_money(row.get('precioUnitario'), currency)} | "
            f"Subtotal: {_money(row.get('subtotal'), currency)}"
        )
    if len(rows) > 20:
        lines.append(f"- ... y {len(rows) - 20} ítem(s) más en el PDF adjunto.")
    return lines


def _mail_body(item: dict[str, Any], request: Any = None, actor_name: str = "") -> str:
    link = _frontend_url(request, item.get("href") or f"/cobranzas/facturacion?serviceOrderId={item['ingresoId']}")
    amounts = _billing_amounts(item)
    currency = amounts["currency"]
    budget_version = item.get("presupuestoVersion")
    budget_reference = f"Versión {budget_version}" if budget_version else "Sin versión"
    budget_state = _status_label(item.get("presupuestoEstado"))
    lines = [
        "ORDEN DE SALIDA LISTA PARA FACTURAR",
        "====================================",
        f"OS {item.get('os')} | {item.get('cliente') or 'Cliente sin identificar'}",
        "",
        "RESUMEN ECONÓMICO",
        "------------------",
        f"Presupuesto: {budget_reference} | Estado: {budget_state}",
        f"Moneda: {currency}",
        f"Neto: {_money(amounts['billingNet'], currency)}",
        f"IVA 21 %: {_money(amounts['billingVat'], currency)}",
        f"TOTAL A FACTURAR: {_money(amounts['billingTotal'], currency)}",
    ]
    if amounts["usesRejectedCharge"]:
        lines.extend(
            [
                f"Criterio: {amounts['billingLabel']}.",
                f"Total del presupuesto rechazado: {_money(amounts['quoteTotal'], currency)}",
            ]
        )
    elif amounts["billingTotal"] is None:
        lines.append("Aviso: no hay un presupuesto asociado con importes disponibles.")
    if item.get("presupuestoFormaPago"):
        lines.append(f"Forma de pago: {item.get('presupuestoFormaPago')}")
    if item.get("presupuestoPlazoEntrega"):
        lines.append(f"Plazo: {item.get('presupuestoPlazoEntrega')}")

    budget_items = _budget_item_lines(item)
    if budget_items:
        lines.extend(["", "DETALLE DEL PRESUPUESTO", "-----------------------", *budget_items])

    lines.extend(
        [
        "",
        "DATOS DE LA ORDEN DE SALIDA",
        "----------------------------",
        f"OS: {item.get('os')}",
        f"Cliente: {item.get('cliente') or '-'}",
        f"Código Bejerman cliente: {item.get('bejermanCustomerCode') or '-'}",
        f"CUIT: {item.get('cuit') or '-'}",
        f"Equipo: {item.get('equipo') or '-'}",
        f"N/S: {item.get('numeroSerie') or '-'}",
        f"N° interno: {item.get('numeroInterno') or '-'}",
        f"Motivo: {item.get('motivo') or '-'}",
        f"Resolución: {_status_label(item.get('resolucion'))}",
        f"Técnico: {item.get('tecnico') or '-'}",
        f"Fecha de liberación: {_date_time(item.get('fechaLiberacion'))}",
        f"Fecha de entrega: {_date_time(item.get('fechaEntrega'))}",
        f"RSS: {item.get('rss') or 'RSS pendiente'}",
        "",
        "DATOS PARA FACTURAR EN BEJERMAN",
        "---------------------------------",
        f"Concepto sugerido: {item.get('conceptCode') or '-'} - {item.get('conceptDescription') or '-'}",
        "IVA: 21 %",
        "Facturar en Bejerman como Concepto de servicio al 21%. No facturar desde la RSS.",
        "Comprobante base: orden de salida",
        f"Liberado por: {actor_name or '-'}",
        "",
        f"ABRIR Y FACTURAR EN NEXORA: {link}",
        ]
    )
    footer = _clean(getattr(settings, "EMAIL_LEGAL_FOOTER", ""))
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)


def _mail_html(item: dict[str, Any], request: Any = None, actor_name: str = "") -> str:
    link = _frontend_url(request, item.get("href") or f"/cobranzas/facturacion?serviceOrderId={item['ingresoId']}")
    amounts = _billing_amounts(item)
    currency = amounts["currency"]
    budget_version = item.get("presupuestoVersion")
    budget_reference = f"Versión {budget_version}" if budget_version else "Sin versión"
    budget_state = _status_label(item.get("presupuestoEstado"))

    def esc(value: Any) -> str:
        return html.escape(_clean(value) or "-")

    def detail_row(label: str, value: Any) -> str:
        return (
            "<tr>"
            f"<td style=\"padding:7px 10px;color:#64748b;font-size:12px;width:190px;\">{html.escape(label)}</td>"
            f"<td style=\"padding:7px 10px;color:#0f172a;font-size:13px;font-weight:600;\">{esc(value)}</td>"
            "</tr>"
        )

    item_rows = []
    budget_items = item.get("presupuestoItems") if isinstance(item.get("presupuestoItems"), list) else []
    for row in budget_items[:20]:
        if not isinstance(row, dict):
            continue
        item_rows.append(
            "<tr>"
            f"<td style=\"padding:8px;border-top:1px solid #e2e8f0;text-align:center;\">{esc(_quantity(row.get('cantidad')))}</td>"
            f"<td style=\"padding:8px;border-top:1px solid #e2e8f0;\">{esc(row.get('descripcion'))}</td>"
            f"<td style=\"padding:8px;border-top:1px solid #e2e8f0;text-align:right;white-space:nowrap;\">{esc(_money(row.get('precioUnitario'), currency))}</td>"
            f"<td style=\"padding:8px;border-top:1px solid #e2e8f0;text-align:right;white-space:nowrap;font-weight:700;\">{esc(_money(row.get('subtotal'), currency))}</td>"
            "</tr>"
        )
    budget_table = ""
    if item_rows:
        omitted_items = len(budget_items) - len(item_rows)
        omitted_row = (
            "<tr><td colspan=\"4\" style=\"padding:9px;border-top:1px solid #e2e8f0;"
            "color:#64748b;text-align:center;font-style:italic;\">"
            f"... y {omitted_items} ítem(s) más en el PDF adjunto.</td></tr>"
            if omitted_items > 0
            else ""
        )
        budget_table = (
            "<h3 style=\"margin:24px 0 8px;color:#0f172a;font-size:15px;\">Detalle del presupuesto</h3>"
            "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" "
            "style=\"border:1px solid #e2e8f0;border-radius:8px;border-collapse:separate;border-spacing:0;overflow:hidden;font-size:12px;\">"
            "<tr style=\"background:#f8fafc;color:#475569;\"><th style=\"padding:8px;\">Cant.</th>"
            "<th style=\"padding:8px;text-align:left;\">Descripción</th><th style=\"padding:8px;text-align:right;\">Unitario</th>"
            "<th style=\"padding:8px;text-align:right;\">Subtotal</th></tr>"
            + "".join(item_rows)
            + omitted_row
            + "</table>"
        )

    rejected_note = ""
    if amounts["usesRejectedCharge"]:
        rejected_note = (
            "<div style=\"margin-top:10px;padding:10px 12px;background:#fff7ed;border:1px solid #fed7aa;border-radius:7px;color:#9a3412;font-size:12px;\">"
            f"Se usa el cobro autorizado por presupuesto rechazado. Total original del presupuesto: {esc(_money(amounts['quoteTotal'], currency))}."
            "</div>"
        )
    elif amounts["billingTotal"] is None:
        rejected_note = (
            "<div style=\"margin-top:10px;padding:10px 12px;background:#fff7ed;border:1px solid #fed7aa;"
            "border-radius:7px;color:#9a3412;font-size:12px;\">"
            "No hay un presupuesto asociado con importes disponibles. Revisar la OS antes de facturar."
            "</div>"
        )

    footer = _clean(getattr(settings, "EMAIL_LEGAL_FOOTER", ""))
    footer_html = f"<div style=\"margin-top:22px;color:#94a3b8;font-size:10px;\">{esc(footer)}</div>" if footer else ""
    return f"""
<!doctype html>
<html lang="es">
  <body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#0f172a;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f1f5f9;padding:24px 12px;">
      <tr><td align="center">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:100%;max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 6px 20px rgba(15,23,42,.08);">
          <tr><td style="padding:22px 26px;background:#0f172a;color:#ffffff;">
            <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#93c5fd;">Servicio Técnico · Cobranzas</div>
            <h1 style="margin:7px 0 0;font-size:22px;line-height:1.25;">OS {esc(item.get('os'))} lista para facturar</h1>
            <div style="margin-top:5px;color:#cbd5e1;font-size:14px;">{esc(item.get('cliente'))}</div>
          </td></tr>
          <tr><td style="padding:24px 26px;">
            <div style="padding:18px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;">
              <div style="color:#1d4ed8;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;">Resumen económico</div>
              <div style="margin-top:6px;color:#475569;font-size:12px;">{esc(budget_reference)} · {esc(budget_state)} · {esc(currency)}</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:14px;">
                <tr>
                  <td style="color:#64748b;font-size:12px;">Neto<br><strong style="color:#0f172a;font-size:16px;">{esc(_money(amounts['billingNet'], currency))}</strong></td>
                  <td style="color:#64748b;font-size:12px;text-align:center;">IVA 21 %<br><strong style="color:#0f172a;font-size:16px;">{esc(_money(amounts['billingVat'], currency))}</strong></td>
                  <td style="color:#1d4ed8;font-size:12px;text-align:right;">Total a facturar<br><strong style="font-size:21px;">{esc(_money(amounts['billingTotal'], currency))}</strong></td>
                </tr>
              </table>
              {rejected_note}
            </div>
            {budget_table}
            <h3 style="margin:24px 0 8px;color:#0f172a;font-size:15px;">Datos de la orden de salida</h3>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e2e8f0;border-radius:8px;border-collapse:separate;border-spacing:0;overflow:hidden;">
              {detail_row('Cliente', item.get('cliente'))}
              {detail_row('Código Bejerman', item.get('bejermanCustomerCode'))}
              {detail_row('CUIT', item.get('cuit'))}
              {detail_row('Equipo', item.get('equipo'))}
              {detail_row('N/S', item.get('numeroSerie'))}
              {detail_row('N° interno', item.get('numeroInterno'))}
              {detail_row('Motivo', item.get('motivo'))}
              {detail_row('Resolución', _status_label(item.get('resolucion')))}
              {detail_row('Técnico', item.get('tecnico'))}
              {detail_row('Fecha de liberación', _date_time(item.get('fechaLiberacion')))}
              {detail_row('Fecha de entrega', _date_time(item.get('fechaEntrega')))}
              {detail_row('RSS', item.get('rss') or 'RSS pendiente')}
              {detail_row('Concepto sugerido', f"{item.get('conceptCode') or '-'} - {item.get('conceptDescription') or '-'}")}
              {detail_row('Forma de pago', item.get('presupuestoFormaPago'))}
              {detail_row('Plazo', item.get('presupuestoPlazoEntrega'))}
              {detail_row('Liberado por', actor_name)}
            </table>
            <div style="margin:18px 0;padding:12px 14px;background:#fffbeb;border-left:4px solid #f59e0b;color:#92400e;font-size:13px;line-height:1.45;">
              Facturar en Bejerman como concepto de servicio al 21 %. No facturar desde la RSS.
            </div>
            <div style="text-align:center;margin-top:22px;">
              <a href="{html.escape(link, quote=True)}" style="display:inline-block;padding:12px 20px;background:#2563eb;color:#ffffff;text-decoration:none;border-radius:8px;font-size:14px;font-weight:700;">Abrir y facturar en NEXORA</a>
            </div>
            {footer_html}
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>
""".strip()


def render_service_order_billing_pdf(ingreso_id: int) -> tuple[bytes, str]:
    item = get_service_order_billing_item(ingreso_id, include_invoiced=True)
    if not item:
        raise ServiceOrderBillingError("SERVICE_ORDER_NOT_FOUND", "No se encontró la OS.", status_code=404)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    amounts = _billing_amounts(item)
    currency = amounts["currency"]
    story = [
        Paragraph(f"Resumen OS para facturación - OS {html.escape(item.get('os') or '')}", styles["Title"]),
        Spacer(1, 5 * mm),
        Paragraph("Facturar en Bejerman como Concepto de servicio al 21%. No facturar desde la RSS.", styles["BodyText"]),
        Spacer(1, 5 * mm),
    ]
    budget_version = item.get("presupuestoVersion")
    budget_header = f"Presupuesto versión {budget_version}" if budget_version else "Presupuesto sin versión"
    budget_summary = Table(
        [
            [budget_header, _status_label(item.get("presupuestoEstado")), currency],
            ["Neto", "IVA 21 %", "TOTAL A FACTURAR"],
            [
                _money(amounts["billingNet"], currency),
                _money(amounts["billingVat"], currency),
                _money(amounts["billingTotal"], currency),
            ],
        ],
        colWidths=[56 * mm, 56 * mm, 56 * mm],
    )
    budget_summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#eff6ff")),
                ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#64748b")),
                ("TEXTCOLOR", (0, 2), (-1, 2), colors.HexColor("#0f172a")),
                ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("FONTSIZE", (0, 2), (-1, 2), 11),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#93c5fd")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bfdbfe")),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend([budget_summary, Spacer(1, 5 * mm)])
    if amounts["usesRejectedCharge"]:
        story.extend(
            [
                Paragraph(
                    "Se usa el cobro autorizado por presupuesto rechazado. "
                    f"Total original del presupuesto: {html.escape(_money(amounts['quoteTotal'], currency))}.",
                    styles["BodyText"],
                ),
                Spacer(1, 3 * mm),
            ]
        )
    rows = [
        ("OS", item.get("os")),
        ("Cliente", item.get("cliente")),
        ("Código Bejerman", item.get("bejermanCustomerCode")),
        ("CUIT", item.get("cuit")),
        ("Equipo", item.get("equipo")),
        ("N/S", item.get("numeroSerie")),
        ("N° interno", item.get("numeroInterno")),
        ("Concepto sugerido", f"{item.get('conceptCode') or '-'} - {item.get('conceptDescription') or '-'}"),
        ("Resolución", item.get("resolucion")),
        ("Forma de pago", item.get("presupuestoFormaPago")),
        ("Plazo", item.get("presupuestoPlazoEntrega")),
        ("Motivo", item.get("motivo")),
        ("Técnico", item.get("tecnico")),
        ("Fecha liberación", _date_time(item.get("fechaLiberacion"))),
        ("Fecha entrega", _date_time(item.get("fechaEntrega"))),
        ("RSS", item.get("rss")),
        ("Factura registrada", item.get("facturaNumero")),
    ]
    table_data = [["Dato", "Valor"], *[[label, _clean(value) or "-"] for label, value in rows]]
    table = Table(table_data, colWidths=[42 * mm, 126 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9ca3af")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
            ]
        )
    )
    story.append(table)

    budget_items = item.get("presupuestoItems") if isinstance(item.get("presupuestoItems"), list) else []
    if budget_items:
        story.extend([Spacer(1, 5 * mm), Paragraph("Detalle del presupuesto", styles["Heading3"])])
        budget_item_data = [["Cant.", "Descripción", "Precio unitario", "Subtotal"]]
        for row in budget_items:
            if not isinstance(row, dict):
                continue
            budget_item_data.append(
                [
                    _quantity(row.get("cantidad")),
                    Paragraph(html.escape(_clean(row.get("descripcion")) or "-"), styles["BodyText"]),
                    _money(row.get("precioUnitario"), currency),
                    _money(row.get("subtotal"), currency),
                ]
            )
        budget_item_table = Table(
            budget_item_data,
            colWidths=[15 * mm, 87 * mm, 33 * mm, 33 * mm],
            repeatRows=1,
        )
        budget_item_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ALIGN", (0, 1), (0, -1), "CENTER"),
                    ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#94a3b8")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(budget_item_table)

    details = []
    if _clean(item.get("descripcionProblema")):
        details.append(("Descripción del problema", item.get("descripcionProblema")))
    if _clean(item.get("trabajosRealizados")):
        details.append(("Trabajos realizados", item.get("trabajosRealizados")))
    for title, text in details:
        story.extend(
            [
                Spacer(1, 5 * mm),
                Paragraph(html.escape(title), styles["Heading3"]),
                Paragraph(html.escape(_clean(text)).replace("\n", "<br/>"), styles["BodyText"]),
            ]
        )

    doc.build(story)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf, f"OS-{item.get('os')}-facturacion.pdf"


def notify_service_order_ready_to_bill(ingreso_id: int, *, request: Any = None, actor_name: str = "") -> dict[str, Any]:
    item = get_service_order_billing_item(ingreso_id, include_invoiced=False)
    if not item:
        return {"notifications": 0, "emails": 0, "recipients": []}

    users = active_users_for_notification(NOTIFICATION_KEY, roles=["cobranzas"], channel="any")
    if not users:
        return {"notifications": 0, "emails": 0, "recipients": []}

    amounts = _billing_amounts(item)
    currency = amounts["currency"]
    total_label = _money(amounts["billingTotal"], currency)
    subject = f"OS lista para facturar - OS {item.get('os')} - {item.get('cliente') or '-'}"
    if amounts["billingTotal"] is not None:
        subject = f"{subject} - Total {total_label}"
    body = _mail_body(item, request=request, actor_name=actor_name)
    html_body = _mail_html(item, request=request, actor_name=actor_name)
    email_recipients: list[str] = []
    inserted_total = 0

    for user in users:
        inserted = emit_notification(
            NOTIFICATION_KEY,
            user_ids=[user.get("id")],
            title=f"OS lista para facturar - OS {item.get('os')}",
            body="\n".join(
                [
                    f"Cliente: {item.get('cliente') or '-'}",
                    f"Equipo: {item.get('equipo') or '-'}",
                    f"Total a facturar: {total_label}",
                    f"Concepto: {item.get('conceptCode') or '-'} - {item.get('conceptDescription') or '-'}",
                ]
            ),
            href=item.get("href") or f"/cobranzas/facturacion?serviceOrderId={ingreso_id}",
            severity="warning",
            entity_type="ingreso",
            entity_id=ingreso_id,
            dedupe_key=f"ingreso:{ingreso_id}:service_order_ready_to_bill",
            payload={
                "ingreso_id": ingreso_id,
                "os": item.get("os"),
                "rss": item.get("rss"),
                "presupuesto_quote_id": item.get("presupuestoQuoteId"),
                "presupuesto_version": item.get("presupuestoVersion"),
                "presupuesto_moneda": currency,
                "presupuesto_neto": amounts["quoteNet"],
                "presupuesto_iva_21": amounts["quoteVat"],
                "presupuesto_total": amounts["quoteTotal"],
                "total_a_facturar": amounts["billingTotal"],
            },
        )
        inserted_total += inserted
    email_users = active_users_for_notification(
        NOTIFICATION_KEY,
        roles=["cobranzas"],
        channel="email",
        require_email=True,
    )
    email_recipients = notification_email_recipients_for_users(email_users)
    queued = queue_notification_email(
        notification_key=NOTIFICATION_KEY,
        dedupe_key=f"ingreso:{ingreso_id}:service_order_ready_to_bill",
        recipients=email_recipients,
        subject=subject,
        body=body,
        html_body=html_body,
        attachment_kind="service_order_billing",
        attachment_reference=str(ingreso_id),
        attachment_context={"optional": True},
    )
    queued_total = int(queued["queued"] + queued["reactivated"])
    return {
        "notifications": inserted_total,
        "emails": queued_total,
        "recipients": email_recipients if queued_total else [],
    }
