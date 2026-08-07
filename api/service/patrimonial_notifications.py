"""Avisos patrimoniales por correo (baja, alta, alta MG y venta MG).

Todos los avisos se encolan en la cola durable de correos, de manera que la
operación queda registrada aunque el SMTP esté caído y el estado real de cada
envío se pueda consultar después. Las vistas devuelven el resultado real de la
operación (destinatarios, ids de entrega y estado), nunca un mensaje optimista.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.db import connection
from django.utils import timezone

from .notification_email_outbox import (
    queue_notification_email,
    summarize_email_deliveries,
)
from .notifications import notify_estado_patrimonial

logger = logging.getLogger(__name__)

PATRIMONIAL_EVENTS = ("baja", "alta", "alta_mg", "venta_mg")

NOTIFICATION_KEY_BY_EVENT = {
    "baja": "baja_patrimonial",
    "alta": "alta_patrimonial",
    "alta_mg": "alta_patrimonial",
    "venta_mg": "venta_patrimonial",
}

EVENT_LABELS = {
    "baja": "baja",
    "alta": "alta",
    "alta_mg": "alta de MG",
    "venta_mg": "venta de MG",
}

RESOLUCION_LABELS = {
    "reparado": "Reparado",
    "no_reparado": "No reparado",
    "no_se_encontro_falla": "No se encontro falla",
    "presupuesto_rechazado": "Presupuesto rechazado",
    "cambio": "Cambio",
}


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


def _frontend_url(request, path: str) -> str:
    try:
        base = (
            getattr(settings, "PUBLIC_WEB_URL", "")
            or getattr(settings, "FRONTEND_ORIGIN", "")
        ).strip()
        if base:
            return f"{base.rstrip('/')}{path}"
        if request is not None:
            return request.build_absolute_uri(path)
    except Exception:
        pass
    return path


def _email_append_footer_text(body: str) -> str:
    footer = _clean(getattr(settings, "EMAIL_LEGAL_FOOTER", ""))
    if not footer:
        return body
    return f"{(body or '').rstrip()}\n\n{footer}"


def patrimonial_recipients() -> list[str]:
    """Destinatarios configurados para los avisos patrimoniales."""
    configured = (
        getattr(settings, "PATRIMONIAL_NOTIFY_RECIPIENTS", None)
        or getattr(settings, "BAJA_NOTIFY_RECIPIENTS", None)
        or []
    )
    if not isinstance(configured, (list, tuple, set)):
        configured = [configured]
    seen: dict[str, str] = {}
    for raw in configured:
        email = _clean(raw)
        if email:
            seen.setdefault(email.lower(), email)
    return list(seen.values())


def _actor_line(request) -> str:
    user = getattr(request, "user", None)
    nombre = _clean(getattr(user, "nombre", None) or getattr(user, "username", ""))
    email = _clean(getattr(user, "email", ""))
    if nombre and email:
        return f"{nombre} ({email})"
    return nombre or email or "-"


def _fecha_txt(fecha_evento=None) -> str:
    if fecha_evento is None:
        return timezone.localtime().strftime("%Y-%m-%d %H:%M")
    try:
        return timezone.localtime(fecha_evento).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(fecha_evento)


def _ingreso_info(ingreso_id: int) -> dict:
    return (
        q(
            """
            SELECT c.razon_social AS cliente,
                   COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                   COALESCE(b.nombre,'') AS marca,
                   COALESCE(m.nombre,'') AS modelo,
                   COALESCE(d.numero_serie,'') AS numero_serie,
                   COALESCE(d.numero_interno,'') AS numero_interno,
                   COALESCE(t.informe_preliminar,'') AS informe_preliminar,
                   COALESCE(t.descripcion_problema,'') AS descripcion_problema,
                   COALESCE(t.trabajos_realizados,'') AS trabajos_realizados,
                   COALESCE(t.comentarios,'') AS comentarios,
                   COALESCE(t.resolucion,'') AS resolucion,
                   COALESCE(CAST(t.motivo AS TEXT), '') AS motivo,
                   COALESCE(loc.nombre,'') AS ubicacion_nombre
              FROM ingresos t
              JOIN devices d   ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN locations loc ON loc.id = t.ubicacion_id
             WHERE t.id=%s
            """,
            [ingreso_id],
            one=True,
        )
        or {}
    )


def _device_info(device_id: int) -> dict:
    return (
        q(
            """
            SELECT COALESCE(c.razon_social,'') AS cliente,
                   COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                   COALESCE(b.nombre,'') AS marca,
                   COALESCE(m.nombre,'') AS modelo,
                   COALESCE(d.numero_serie,'') AS numero_serie,
                   COALESCE(d.numero_interno,'') AS numero_interno
              FROM devices d
              LEFT JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
             WHERE d.id=%s
            """,
            [device_id],
            one=True,
        )
        or {}
    )


def _equipo_txt(info: dict) -> str:
    return " | ".join(
        part
        for part in [
            _clean(info.get("tipo_equipo")),
            _clean(info.get("marca")),
            _clean(info.get("modelo")),
        ]
        if part
    )


def _build_estado_email(request, ingreso_id: int, evento: str, fecha_evento=None) -> tuple[str, str]:
    info = _ingreso_info(ingreso_id)
    os_txt = os_label(ingreso_id)
    cliente = _clean(info.get("cliente"))
    resolucion = _clean(info.get("resolucion"))
    resolucion_label = RESOLUCION_LABELS.get(
        resolucion,
        resolucion.replace("_", " ").capitalize() if resolucion else "",
    )

    if evento == "baja":
        subject = f"Notificación de baja de equipo - {os_txt} - {cliente or 'Sin cliente'}"
        intro = (
            "Se registró la baja de un equipo en NEXORA. "
            "Por favor reflejar la baja en el sistema patrimonial."
        )
        fecha_label = "Fecha de baja"
        diag_title = "Diagnóstico / motivo de baja:"
    elif evento == "alta_mg":
        subject = f"Notificación de alta de MG - {os_txt} - {cliente or 'Sin cliente'}"
        intro = (
            "Se registró el alta de MG de un equipo en NEXORA. "
            "Por favor reflejar el alta en el sistema patrimonial."
        )
        fecha_label = "Fecha de alta de MG"
        diag_title = "Diagnóstico / estado actual:"
    else:
        subject = f"Notificación de alta de equipo - {os_txt} - {cliente or 'Sin cliente'}"
        intro = (
            "Se registró el alta de un equipo en NEXORA. "
            "Por favor reflejar el alta en el sistema patrimonial."
        )
        fecha_label = "Fecha de alta"
        diag_title = "Diagnóstico / estado actual:"

    lines = [
        intro,
        "",
        "Detalle del ingreso:",
        f"- OS: {os_txt}",
        f"- Cliente: {cliente or '-'}",
        f"- Equipo: {_equipo_txt(info) or '-'}",
        f"- Número de serie: {_clean(info.get('numero_serie')) or '-'}",
        f"- Número interno: {_clean(info.get('numero_interno')) or '-'}",
        f"- Ubicación actual: {_clean(info.get('ubicacion_nombre')) or '-'}",
        f"- {fecha_label}: {_fecha_txt(fecha_evento)}",
        f"- Registrado por: {_actor_line(request)}",
        "",
        diag_title,
        f"- Diagnóstico / descripción del problema: {_clean(info.get('descripcion_problema')) or '-'}",
        f"- Trabajos realizados: {_clean(info.get('trabajos_realizados')) or '-'}",
        f"- Comentarios: {_clean(info.get('comentarios')) or '-'}",
    ]
    if _clean(info.get("informe_preliminar")):
        lines.append(f"- Informe preliminar: {_clean(info.get('informe_preliminar'))}")
    if resolucion_label:
        lines.append(f"- Resolución: {resolucion_label}")
    if _clean(info.get("motivo")):
        lines.append(f"- Motivo de ingreso: {_clean(info.get('motivo'))}")
    lines.append("")
    lines.append(f"Hoja de servicio: {_frontend_url(request, f'/ingresos/{ingreso_id}')}?tab=principal")
    return subject, _email_append_footer_text("\n".join(lines))


def _build_venta_mg_email(
    request,
    *,
    device_id: int,
    ingreso_id: int | None,
    fecha_evento=None,
    venta: dict | None = None,
) -> tuple[str, str]:
    venta = venta or {}
    info = _device_info(device_id)
    if ingreso_id:
        ingreso_info = _ingreso_info(ingreso_id)
        for key, value in ingreso_info.items():
            if not _clean(info.get(key)):
                info[key] = value
    numero_interno = _clean(info.get("numero_interno"))
    comprador = _clean(venta.get("comprador"))
    subject_parts = [
        "Notificación de venta de equipo MG",
        numero_interno or (os_label(ingreso_id) if ingreso_id else f"Equipo {device_id}"),
        comprador or "Sin comprador",
    ]
    subject = " - ".join(subject_parts)

    lines = [
        "Se registró la venta de un equipo propio de MG BIO en NEXORA. "
        "Por favor reflejar la baja patrimonial por venta en el sistema patrimonial.",
        "",
        "Detalle de la venta:",
        f"- Número interno MG: {numero_interno or '-'}",
        f"- Equipo: {_equipo_txt(info) or '-'}",
        f"- Número de serie: {_clean(info.get('numero_serie')) or '-'}",
        f"- Comprador: {comprador or '-'}",
        f"- Factura: {_clean(venta.get('factura_numero')) or '-'}",
        f"- Remito: {_clean(venta.get('remito_numero')) or '-'}",
        f"- Número alternativo: {_clean(venta.get('numero_alternativo')) or '-'}",
        f"- Fecha de venta: {_fecha_txt(fecha_evento)}",
        f"- Registrado por: {_actor_line(request)}",
        f"- Observaciones: {_clean(venta.get('observaciones')) or '-'}",
    ]
    if ingreso_id:
        lines.insert(3, f"- OS: {os_label(ingreso_id)}")
        lines.append("")
        lines.append(f"Hoja de servicio: {_frontend_url(request, f'/ingresos/{ingreso_id}')}?tab=principal")
    else:
        lines.append("")
        lines.append(f"Equipo: {_frontend_url(request, '/equipos')}?device_id={device_id}")
    return subject, _email_append_footer_text("\n".join(lines))


def _dedupe_key(evento: str, *, ingreso_id: int | None, device_id: int | None, fecha_evento=None) -> str:
    scope = f"ingreso:{ingreso_id}" if ingreso_id else f"device:{device_id}"
    stamp = timezone.localtime(fecha_evento) if fecha_evento else timezone.localtime()
    try:
        stamp_txt = stamp.strftime("%Y%m%d%H%M")
    except Exception:
        stamp_txt = timezone.localtime().strftime("%Y%m%d%H%M")
    return f"{scope}:{NOTIFICATION_KEY_BY_EVENT[evento]}:{stamp_txt}"


def _send_direct(subject: str, body: str, recipients: list[str]) -> dict[str, Any]:
    """Último recurso cuando la cola durable no está disponible."""
    try:
        sent = send_mail(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipients,
            fail_silently=False,
        )
        if sent:
            return {
                "status": "sent",
                "detail": f"Correo enviado a {', '.join(recipients)}.",
            }
        return {
            "status": "failed",
            "detail": "El servidor de correo no confirmó la entrega.",
        }
    except Exception as exc:
        logger.exception("patrimonial_email_direct_failed", extra={"recipients": recipients})
        return {
            "status": "failed",
            "detail": f"No se pudo enviar el correo: {type(exc).__name__}: {exc}",
        }


def notify_patrimonial_email(
    request,
    *,
    evento: str,
    ingreso_id: int | None = None,
    device_id: int | None = None,
    fecha_evento=None,
    venta: dict | None = None,
) -> dict[str, Any]:
    """Encola el aviso patrimonial y devuelve el estado real de la operación.

    El resultado se expone en la respuesta de la API para que la interfaz pueda
    informar qué pasó de verdad con el correo (encolado, enviado o fallado) en
    lugar de un cartel fijo.
    """
    evento_norm = _clean(evento).lower()
    if evento_norm not in PATRIMONIAL_EVENTS:
        return {
            "status": "no_aplica",
            "detail": f"Evento patrimonial desconocido: {evento or '-'}",
            "event": evento_norm,
            "recipients": [],
            "deliveryIds": [],
        }
    if not ingreso_id and not device_id:
        return {
            "status": "error",
            "detail": "No se pudo identificar el equipo del aviso patrimonial.",
            "event": evento_norm,
            "recipients": [],
            "deliveryIds": [],
        }

    label = EVENT_LABELS.get(evento_norm, evento_norm)
    notification_key = NOTIFICATION_KEY_BY_EVENT[evento_norm]
    recipients = patrimonial_recipients()

    if ingreso_id:
        try:
            notify_estado_patrimonial(ingreso_id, evento_norm, emails=recipients)
        except Exception:
            logger.exception(
                "patrimonial_bell_failed",
                extra={"ingreso_id": ingreso_id, "evento": evento_norm},
            )

    if not recipients:
        logger.warning(
            "patrimonial_email_sin_destinatarios",
            extra={"evento": evento_norm, "ingreso_id": ingreso_id, "device_id": device_id},
        )
        return {
            "status": "sin_destinatarios",
            "detail": (
                f"El aviso de {label} no se envió: no hay destinatarios configurados "
                "en BAJA_NOTIFY_RECIPIENTS."
            ),
            "event": evento_norm,
            "recipients": [],
            "deliveryIds": [],
            "notificationKey": notification_key,
        }

    try:
        if evento_norm == "venta_mg":
            subject, body = _build_venta_mg_email(
                request,
                device_id=int(device_id),
                ingreso_id=int(ingreso_id) if ingreso_id else None,
                fecha_evento=fecha_evento,
                venta=venta,
            )
        else:
            subject, body = _build_estado_email(request, int(ingreso_id), evento_norm, fecha_evento)
    except Exception as exc:
        logger.exception(
            "patrimonial_email_build_failed",
            extra={"evento": evento_norm, "ingreso_id": ingreso_id, "device_id": device_id},
        )
        return {
            "status": "error",
            "detail": f"No se pudo preparar el aviso de {label}: {type(exc).__name__}: {exc}",
            "event": evento_norm,
            "recipients": recipients,
            "deliveryIds": [],
            "notificationKey": notification_key,
        }

    try:
        queued = queue_notification_email(
            notification_key=notification_key,
            dedupe_key=_dedupe_key(
                evento_norm,
                ingreso_id=ingreso_id,
                device_id=device_id,
                fecha_evento=fecha_evento,
            ),
            recipients=recipients,
            subject=subject,
            body=body,
        )
    except Exception as exc:
        logger.exception(
            "patrimonial_email_queue_failed",
            extra={"evento": evento_norm, "ingreso_id": ingreso_id, "device_id": device_id},
        )
        direct = _send_direct(subject, body, recipients)
        direct.update(
            {
                "event": evento_norm,
                "recipients": recipients,
                "deliveryIds": [],
                "notificationKey": notification_key,
                "queueError": f"{type(exc).__name__}: {exc}",
            }
        )
        return direct

    delivery_ids = [int(value) for value in queued.get("delivery_ids") or []]
    result = summarize_email_deliveries(delivery_ids)
    result.update(
        {
            "event": evento_norm,
            "recipients": recipients,
            "deliveryIds": delivery_ids,
            "notificationKey": notification_key,
        }
    )
    logger.info(
        "patrimonial_email evento=%s ingreso_id=%s device_id=%s status=%s recipients=%s delivery_ids=%s",
        evento_norm,
        ingreso_id,
        device_id,
        result.get("status"),
        recipients,
        delivery_ids,
    )
    return result
