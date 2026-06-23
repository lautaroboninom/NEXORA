from __future__ import annotations

import html
import logging
from io import BytesIO
from typing import Any

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import connection, transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .notifications import active_users_for_notification, emit_notification

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
               ORDER BY sort_order ASC, created_at ASC
               LIMIT 1
            ) i ON TRUE
           WHERE o.delivery_type = 'service_release'
             AND (
                  o.ingreso_id = t.id
                  OR (o.source_system = 'nexora' AND o.source_external_id = CAST(t.id AS TEXT))
                 )
           ORDER BY o.created_at DESC NULLS LAST, o.id DESC
           LIMIT 1
        ) dor ON TRUE
        """,
    )


def _mapping_join() -> tuple[str, str]:
    if not _table_exists("bejerman_article_mappings"):
        return "'' AS mapped_article_code, '' AS mapped_article_description", ""
    return (
        """
        COALESCE(bam.article_code, '') AS mapped_article_code,
        COALESCE(bam.article_description, '') AS mapped_article_description
        """,
        """
        LEFT JOIN LATERAL (
          SELECT article_code, article_description
            FROM bejerman_article_mappings
           WHERE model_id = d.model_id
           ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END,
                    confirmed_at DESC NULLS LAST,
                    updated_at DESC NULLS LAST,
                    id DESC
           LIMIT 1
        ) bam ON TRUE
        """,
    )


def _service_order_select_sql() -> tuple[str, list[Any]]:
    ingreso_cols = _table_columns("ingresos")
    customer_cols = _table_columns("customers")
    device_cols = _table_columns("devices")
    model_cols = _table_columns("models")
    marca_cols = _table_columns("marcas")
    user_cols = _table_columns("users")
    fecha_liberacion_sql, event_join = _event_liberation_join()
    delivery_select, delivery_join = _delivery_order_join()
    mapping_select, mapping_join = _mapping_join()

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
          {mapping_select}
        FROM ingresos t
        JOIN devices d ON d.id = t.device_id
        JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        LEFT JOIN users u ON u.id = t.asignado_a
        {event_join}
        {delivery_join}
        {mapping_join}
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


def _format_service_order_item(row: dict[str, Any]) -> dict[str, Any]:
    ingreso_id = int(row.get("ingreso_id"))
    remito = _clean(row.get("remito_salida")) or _clean(row.get("service_order_remito_number"))
    return {
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
        "href": f"/cobranzas/facturacion?serviceOrderId={ingreso_id}",
        "pdfUrl": f"/api/cobranzas/os-a-facturar/{ingreso_id}/pdf/",
    }


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
    return _format_service_order_item(row) if row else None


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


def _mail_body(item: dict[str, Any], request: Any = None, actor_name: str = "") -> str:
    link = _frontend_url(request, item.get("href") or f"/cobranzas/facturacion?serviceOrderId={item['ingresoId']}")
    lines = [
        "Se liberó una OS de reparación para facturar.",
        "",
        f"OS: {item.get('os')}",
        f"Cliente: {item.get('cliente') or '-'}",
        f"Código Bejerman cliente: {item.get('bejermanCustomerCode') or '-'}",
        f"Equipo: {item.get('equipo') or '-'}",
        f"N/S: {item.get('numeroSerie') or '-'}",
        f"N° interno: {item.get('numeroInterno') or '-'}",
        f"Resolución: {item.get('resolucion') or '-'}",
        f"RSS: {item.get('rss') or 'RSS pendiente'}",
        f"Concepto Bejerman sugerido: {item.get('conceptCode') or '-'} - {item.get('conceptDescription') or '-'}",
        f"Liberado por: {actor_name or '-'}",
        "",
        "Facturar en Bejerman como Concepto de servicio al 21%. No facturar desde la RSS.",
        "",
        f"Abrir en NEXORA: {link}",
    ]
    footer = _clean(getattr(settings, "EMAIL_LEGAL_FOOTER", ""))
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)


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
    story = [
        Paragraph(f"Resumen OS para facturación - OS {html.escape(item.get('os') or '')}", styles["Title"]),
        Spacer(1, 5 * mm),
        Paragraph("Facturar en Bejerman como Concepto de servicio al 21%. No facturar desde la RSS.", styles["BodyText"]),
        Spacer(1, 5 * mm),
    ]
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
        ("Motivo", item.get("motivo")),
        ("Técnico", item.get("tecnico")),
        ("Fecha liberación", item.get("fechaLiberacion")),
        ("Fecha entrega", item.get("fechaEntrega")),
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


def _send_email_with_optional_fallback(subject: str, body: str, recipients: list[str], attachment: tuple[bytes, str] | None) -> bool:
    if not recipients:
        return False

    def _send(connection_obj=None):
        message = EmailMessage(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipients,
            connection=connection_obj,
        )
        if attachment:
            pdf_bytes, filename = attachment
            message.attach(filename, pdf_bytes, "application/pdf")
        sent = message.send(fail_silently=False)
        return bool(sent and sent > 0)

    try:
        return _send()
    except Exception as exc:
        try:
            port_cfg = int(getattr(settings, "EMAIL_PORT", 0) or 0)
        except Exception:
            port_cfg = 0
        if port_cfg != 587:
            logger.exception("service_order_billing_email failed", extra={"recipients": recipients})
            return False
        try:
            conn = get_connection(
                backend=getattr(settings, "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"),
                host=getattr(settings, "EMAIL_HOST", None),
                port=465,
                username=getattr(settings, "EMAIL_HOST_USER", None),
                password=getattr(settings, "EMAIL_HOST_PASSWORD", None),
                use_tls=False,
                use_ssl=True,
                fail_silently=False,
            )
            return _send(conn)
        except Exception:
            logger.exception("service_order_billing_email fallback failed", extra={"recipients": recipients, "original_error": str(exc)})
            return False


def notify_service_order_ready_to_bill(ingreso_id: int, *, request: Any = None, actor_name: str = "") -> dict[str, Any]:
    item = get_service_order_billing_item(ingreso_id, include_invoiced=False)
    if not item:
        return {"notifications": 0, "emails": 0, "recipients": []}

    users = active_users_for_notification(NOTIFICATION_KEY, roles=["cobranzas"])
    if not users:
        return {"notifications": 0, "emails": 0, "recipients": []}

    subject = f"OS lista para facturar - OS {item.get('os')} - {item.get('cliente') or '-'}"
    body = _mail_body(item, request=request, actor_name=actor_name)
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
                    f"Concepto: {item.get('conceptCode') or '-'} - {item.get('conceptDescription') or '-'}",
                ]
            ),
            href=item.get("href") or f"/cobranzas/facturacion?serviceOrderId={ingreso_id}",
            severity="warning",
            entity_type="ingreso",
            entity_id=ingreso_id,
            dedupe_key=f"ingreso:{ingreso_id}:service_order_ready_to_bill",
            payload={"ingreso_id": ingreso_id, "os": item.get("os"), "rss": item.get("rss")},
        )
        inserted_total += inserted
        email = _clean(user.get("email"))
        if inserted and email:
            email_recipients.append(email)

    def _send_notice():
        if not email_recipients:
            return
        attachment = None
        try:
            attachment = render_service_order_billing_pdf(ingreso_id)
        except Exception:
            logger.exception("service_order_billing_pdf failed", extra={"ingreso_id": ingreso_id})
        ok = _send_email_with_optional_fallback(subject, body, email_recipients, attachment)
        logger.info(
            "service_order_billing_email sent=%s ingreso_id=%s recipients=%s backend=%s",
            bool(ok),
            ingreso_id,
            email_recipients,
            getattr(settings, "EMAIL_BACKEND", ""),
        )

    try:
        conn = transaction.get_connection()
        if getattr(conn, "in_atomic_block", False):
            transaction.on_commit(_send_notice)
        else:
            _send_notice()
    except Exception:
        _send_notice()

    return {"notifications": inserted_total, "emails": len(email_recipients), "recipients": email_recipients}
