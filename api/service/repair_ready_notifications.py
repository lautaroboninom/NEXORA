import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection, send_mail
from django.db import connection, transaction

from .notifications import active_user_ids_for_roles, notify_reparacion_lista_remito

logger = logging.getLogger(__name__)


def q(sql, params=None, one=False):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        if not cur.description:
            return None
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if one:
            return rows[0] if rows else None
        return rows


def os_label(value):
    try:
        return str(int(value)).zfill(5)
    except Exception:
        return str(value)


def _frontend_url(request, path):
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


def _email_append_footer_text(body):
    footer = str(getattr(settings, "EMAIL_LEGAL_FOOTER", "") or "").strip()
    if not footer:
        return body
    return f"{body}\n\n{footer}"


def _normalize_recipients(recipients):
    if not recipients:
        return []
    if isinstance(recipients, (list, tuple, set)):
        values = recipients
    else:
        values = [recipients]
    return [str(value).strip() for value in values if str(value or "").strip()]


def _repair_ready_recipients():
    recips = _normalize_recipients(getattr(settings, "REPAIR_READY_RECIPIENTS", None))
    if not recips:
        recips = _normalize_recipients(getattr(settings, "ASSIGNMENT_REQUEST_RECIPIENTS", []))
    if recips:
        return recips
    return _normalize_recipients(
        [
            getattr(settings, "COMPANY_FOOTER_EMAIL_2", None),
            getattr(settings, "COMPANY_FOOTER_EMAIL", None),
        ]
    )


def _repair_ready_notification_user_ids():
    try:
        return active_user_ids_for_roles(["recepcion"])
    except Exception:
        logger.exception("No se pudieron resolver destinatarios internos de recepción")
        return []


def _send_mail_with_fallback(subject, body, recipients):
    debug = {}
    try:
        debug.update(
            {
                "backend": getattr(settings, "EMAIL_BACKEND", None),
                "host": getattr(settings, "EMAIL_HOST", None),
                "port": getattr(settings, "EMAIL_PORT", None),
                "use_tls": getattr(settings, "EMAIL_USE_TLS", None),
                "use_ssl": getattr(settings, "EMAIL_USE_SSL", None),
                "from": getattr(settings, "DEFAULT_FROM_EMAIL", None),
                "recipients": list(recipients or []),
            }
        )
    except Exception:
        pass
    if not recipients:
        logger.warning("repair_ready_email no recipients configured")
        return False, debug
    try:
        sent = send_mail(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipients,
            fail_silently=False,
        )
        return bool(sent and sent > 0), debug
    except Exception as exc:
        try:
            debug["error"] = str(exc)
            debug["exception"] = exc.__class__.__name__
        except Exception:
            pass
        try:
            port_cfg = int(getattr(settings, "EMAIL_PORT", 0) or 0)
        except Exception:
            port_cfg = 0
        if port_cfg == 587:
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
                msg = EmailMessage(
                    subject,
                    body,
                    getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    recipients,
                    connection=conn,
                )
                sent = msg.send()
                ok = bool(sent and sent > 0)
                debug["fallback"] = {"mode": "ssl_465", "sent": ok}
                return ok, debug
            except Exception as fallback_exc:
                try:
                    debug.setdefault("fallback", {})["error"] = str(fallback_exc)
                    debug.setdefault("fallback", {})["exception"] = fallback_exc.__class__.__name__
                except Exception:
                    pass
        return False, debug


def _repair_ready_context(ingreso_id):
    try:
        return (
            q(
                """
                SELECT c.razon_social AS cliente,
                       COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                       COALESCE(b.nombre,'') AS marca,
                       COALESCE(m.nombre,'') AS modelo,
                       COALESCE(d.numero_serie,'') AS numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno
                  FROM ingresos t
                  JOIN devices d   ON d.id = t.device_id
                  JOIN customers c ON c.id = d.customer_id
                  LEFT JOIN marcas b ON b.id = d.marca_id
                  LEFT JOIN models m ON m.id = d.model_id
                 WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            )
            or {}
        )
    except Exception:
        logger.exception("No se pudo preparar el contexto de aviso de reparación lista", extra={"ingreso_id": ingreso_id})
        return {}


def _repair_ready_email(ingreso_id, request=None, actor_name=""):
    info = _repair_ready_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    cliente = info.get("cliente") or ""
    equipo = " | ".join(
        [
            value
            for value in [
                info.get("tipo_equipo") or "",
                info.get("marca") or "",
                info.get("modelo") or "",
            ]
            if value
        ]
    )
    ns = info.get("numero_serie") or ""

    subject = f"Equipo reparado - falta imprimir remito - {os_txt} - {cliente}"
    lines = [
        "El equipo fue marcado como reparado.",
        "Solo falta imprimir la orden de salida (remito).",
        f"Marcado por: {actor_name or '-'}",
        f"OS: {os_txt}",
        f"Cliente: {cliente}",
        f"Equipo: {equipo or '-'}",
        f"N/S: {ns or '-'}",
    ]
    try:
        url = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=principal"
        lines.extend(["", f"Abrir hoja: {url}"])
    except Exception:
        pass
    body = "\n".join(lines)
    try:
        body = _email_append_footer_text(body)
    except Exception:
        pass
    return subject, body


def notify_repair_ready_for_remito(ingreso_id, *, request=None, actor_name="", recipients=None):
    recips = _normalize_recipients(recipients) or _repair_ready_recipients()
    user_ids = _repair_ready_notification_user_ids()
    inserted = 0
    try:
        inserted = notify_reparacion_lista_remito(ingreso_id, user_ids=user_ids, emails=recips)
    except Exception:
        logger.exception("No se pudo crear la notificación de reparación lista", extra={"ingreso_id": ingreso_id})

    subject, body = _repair_ready_email(ingreso_id, request=request, actor_name=actor_name)

    def _send_notice():
        try:
            ok, _debug = _send_mail_with_fallback(subject, body, recips)
            logger.info(
                "repair_ready_email sent=%s ingreso_id=%s recipients=%s backend=%s",
                bool(ok),
                ingreso_id,
                recips,
                getattr(settings, "EMAIL_BACKEND", ""),
            )
        except Exception:
            logger.exception("repair_ready_email failed", extra={"ingreso_id": ingreso_id, "recipients": recips})

    try:
        conn = transaction.get_connection()
        if getattr(conn, "in_atomic_block", False):
            transaction.on_commit(_send_notice)
        else:
            _send_notice()
    except Exception:
        _send_notice()

    return {"notifications": inserted, "recipients": recips}
