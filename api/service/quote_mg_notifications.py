"""Aviso a Cobranzas cuando se aprueba el presupuesto de un MG propio.

Los equipos propios de MG BIO (los "MG") se reparan igual que los de cliente,
pero el presupuesto aprobado es información que Cobranzas necesita en el
momento, no cuando aparece el remito. Este módulo manda el PDF del presupuesto
por correo apenas jefatura aprueba, y sólo para los MG vigentes del patrimonio:
los MG históricos (vendidos) quedan afuera.

Es un aviso sólo de correo: el tipo se declara con ``channels=("email",)`` en el
catálogo de notificaciones y nunca genera campana.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from .notifications import (
    active_users_for_notification,
    notification_allowed_roles,
    notification_email_recipients_for_users,
)
from .notification_email_outbox import queue_notification_email
from .repair_ready_notifications import _email_append_footer_text, _frontend_url, q

logger = logging.getLogger(__name__)

NOTIFICATION_KEY = "presupuesto_mg_aprobado"
ATTACHMENT_KIND = "quote_pdf"

IVA_RATE = Decimal("0.21")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _os_label(ingreso_id: Any) -> str:
    try:
        return f"OS {str(int(ingreso_id)).zfill(6)}"
    except Exception:
        return f"OS {_clean(ingreso_id)}"


def _money(value: Any, currency: Any = "ARS") -> str:
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return "-"
    formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    code = _clean(currency).upper() or "ARS"
    symbol = {"ARS": "$", "USD": "U$S"}.get(code, code)
    return f"{symbol} {formatted}"


def _is_mg_propio(ingreso_id: int) -> bool:
    """MG del patrimonio y vigente: excluye los vendidos (MG históricos).

    Reusa el gate canónico de la integración contable, que además de
    ``mg_estado='inactivo_venta'`` descarta cualquier rastro de venta MG.
    """
    # Import diferido: bejerman_sync es pesado y no hace falta al importar las views.
    from .bejerman_sync import ingreso_is_internal_equipment

    return bool(ingreso_is_internal_equipment(ingreso_id))


def _quote_context(ingreso_id: int, quote_id: int) -> dict[str, Any]:
    row = q(
        """
        SELECT
          t.id AS ingreso_id,
          COALESCE(c.razon_social,'') AS cliente,
          COALESCE(b.nombre,'')       AS marca,
          COALESCE(m.nombre,'')       AS modelo,
          COALESCE(m.tipo_equipo,'')  AS tipo_equipo,
          COALESCE(d.numero_serie,'')   AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno,
          qt.version_num,
          COALESCE(qt.moneda,'ARS') AS moneda,
          qt.fecha_aprobado
        FROM ingresos t
        JOIN devices   d ON d.id = t.device_id
        LEFT JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas    b ON b.id = d.marca_id
        LEFT JOIN models    m ON m.id = d.model_id
        LEFT JOIN quotes   qt ON qt.id = %s
        WHERE t.id = %s
        """,
        [quote_id, ingreso_id],
        one=True,
    )
    return row or {}


def _quote_amounts(quote_id: int) -> dict[str, Decimal]:
    """Totales recalculados desde los ítems, igual que el PDF.

    ``quotes.subtotal`` puede quedar desactualizado; ``render_quote_pdf`` ya
    recalcula desde ``quote_items``, así que el cuerpo del mail hace lo mismo
    para no mostrar un número distinto al del adjunto.
    """
    row = q(
        "SELECT COALESCE(SUM(qty*precio_u),0) AS subtotal FROM quote_items WHERE quote_id=%s",
        [quote_id],
        one=True,
    ) or {}
    subtotal = Decimal(str(row.get("subtotal") or 0))
    iva = subtotal * IVA_RATE
    return {"subtotal": subtotal, "iva_21": iva, "total": subtotal + iva}


def _recipients() -> list[str]:
    users = active_users_for_notification(
        NOTIFICATION_KEY,
        roles=notification_allowed_roles(NOTIFICATION_KEY),
        channel="email",
        require_email=True,
    )
    return notification_email_recipients_for_users(users)


def _body(context: dict[str, Any], amounts: dict[str, Decimal], *, actor_name: str, link: str) -> str:
    moneda = _clean(context.get("moneda")) or "ARS"
    mg = _clean(context.get("numero_interno"))
    serie = _clean(context.get("numero_serie"))
    version = context.get("version_num")
    lines = [
        f"Se aprobó el presupuesto de un equipo propio de MG BIO ({_os_label(context.get('ingreso_id'))}).",
        "",
        "Equipo:",
        f"- N° interno (MG): {mg or '-'}",
        f"- N° de serie: {serie or '-'}",
        f"- Marca/Modelo: {_clean(context.get('marca')) or '-'} / {_clean(context.get('modelo')) or '-'}",
        f"- Tipo: {_clean(context.get('tipo_equipo')) or '-'}",
        f"- Cliente de la orden: {_clean(context.get('cliente')) or '-'}",
        "",
        "Presupuesto:",
        f"- Versión: {version if version is not None else '-'}",
        f"- Neto: {_money(amounts['subtotal'], moneda)}",
        f"- IVA 21%: {_money(amounts['iva_21'], moneda)}",
        f"- Total: {_money(amounts['total'], moneda)}",
    ]
    if _clean(actor_name):
        lines.append(f"- Aprobado por: {_clean(actor_name)}")
    lines += [
        "",
        "El PDF del presupuesto va adjunto.",
        "",
        f"Abrir hoja de servicio: {link}",
        "",
        "Aviso automático - no responder a este correo.",
    ]
    return _email_append_footer_text("\n".join(lines))


def notify_quote_mg_approved(
    ingreso_id: int,
    quote_id: int,
    *,
    request: Any = None,
    actor_name: str = "",
) -> dict[str, Any]:
    """Encola el mail con el PDF del presupuesto para Cobranzas.

    No hace nada si el equipo no es un MG propio vigente. Devuelve un dict con
    el motivo del salteo para que el llamador pueda loguearlo.
    """
    if not _is_mg_propio(ingreso_id):
        return {"emails": 0, "skipped": "no_mg"}

    context = _quote_context(ingreso_id, quote_id)
    if not context:
        return {"emails": 0, "skipped": "sin_ingreso"}

    recipients = _recipients()
    if not recipients:
        return {"emails": 0, "skipped": "sin_destinatarios"}

    amounts = _quote_amounts(quote_id)
    mg = _clean(context.get("numero_interno")) or _clean(context.get("numero_serie"))
    subject = f"{_os_label(ingreso_id)} - Presupuesto aprobado"
    if mg:
        subject = f"{subject} ({mg})"

    result = queue_notification_email(
        notification_key=NOTIFICATION_KEY,
        # La versión entra en la clave: aprobar una versión nueva es un hecho
        # nuevo y tiene que volver a salir, no quedar tapado por el dedupe.
        dedupe_key=f"{NOTIFICATION_KEY}:{ingreso_id}:{quote_id}",
        recipients=recipients,
        subject=subject,
        body=_body(
            context,
            amounts,
            actor_name=actor_name,
            link=_frontend_url(request, f"/ingresos/{ingreso_id}"),
        ),
        attachment_kind=ATTACHMENT_KIND,
        attachment_reference=f"{ingreso_id}:{quote_id}",
        # Si el PDF falla, el mail sale igual: el cuerpo ya trae el resumen y el
        # link, y es preferible eso a que Cobranzas no se entere.
        attachment_context={"optional": True},
    )
    logger.info(
        "presupuesto_mg_aprobado_email_encolado",
        extra={
            "ingreso_id": ingreso_id,
            "quote_id": quote_id,
            "recipients": len(recipients),
            "queued": result.get("queued"),
        },
    )
    return {"emails": len(recipients), **result}
