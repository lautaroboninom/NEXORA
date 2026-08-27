import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection, send_mail
from django.db import connection, transaction

from .email_delivery import dispatch_email_delivery
from .notifications import (
    active_user_ids_for_roles,
    filter_email_recipients_by_preference,
    notify_no_se_repara_listo_remito,
    notify_reparacion_lista_remito,
    resolve_notifications,
    user_ids_subscribed_to,
)

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


def _assigned_technician_id(ingreso_id):
    try:
        row = q("SELECT asignado_a FROM ingresos WHERE id = %s", [ingreso_id], one=True) or {}
    except Exception:
        logger.exception(
            "No se pudo resolver el técnico asignado del ingreso",
            extra={"ingreso_id": ingreso_id},
        )
        return None
    try:
        return int(row.get("asignado_a")) if row.get("asignado_a") is not None else None
    except (TypeError, ValueError):
        return None


def _actor_email(actor_user_id):
    if actor_user_id is None:
        return ""
    try:
        row = q("SELECT COALESCE(email,'') AS email FROM users WHERE id = %s", [actor_user_id], one=True) or {}
    except Exception:
        logger.exception(
            "No se pudo resolver el correo del autor del aviso",
            extra={"actor_user_id": actor_user_id},
        )
        return ""
    return str(row.get("email") or "").strip().lower()


def _remito_audience_user_ids(notification_key, ingreso_id=None, actor_user_id=None):
    """Recepción imprime la orden de salida; el técnico asignado sigue su equipo.

    La lista de correos del aviso queda deliberadamente afuera: pasarla como
    ``emails`` a ``emit_notification`` le creaba la campana al usuario dueño de
    esa casilla, sin mirar si la había pedido. Jefatura entra por el camino
    correcto, que es tener el aviso prendido en su configuración.

    ``actor_user_id`` queda fuera de la audiencia: quien aprueba el presupuesto
    o marca el equipo reparado no necesita que le avisen de su propio clic, y
    era la mitad del ruido de este aviso.
    """
    user_ids = []
    try:
        user_ids.extend(active_user_ids_for_roles(["recepcion"]))
    except Exception:
        logger.exception("No se pudieron resolver destinatarios internos de recepción")
    try:
        # Jefatura no entra por la lista de correos, entra si tiene el aviso
        # prendido en el centro de notificaciones.
        for uid in user_ids_subscribed_to(
            notification_key, ["jefe", "jefe_veedor", "admin", "supervisor"]
        ):
            if uid not in user_ids:
                user_ids.append(uid)
    except Exception:
        logger.exception("No se pudieron resolver destinatarios de jefatura suscriptos al aviso")
    if ingreso_id is not None:
        tecnico_id = _assigned_technician_id(ingreso_id)
        if tecnico_id and tecnico_id not in user_ids:
            user_ids.append(tecnico_id)
    try:
        actor_id = int(actor_user_id) if actor_user_id is not None else None
    except (TypeError, ValueError):
        actor_id = None
    if actor_id is not None:
        user_ids = [uid for uid in user_ids if uid != actor_id]
    return user_ids


def _repair_ready_notification_user_ids(ingreso_id=None, actor_user_id=None):
    return _remito_audience_user_ids(
        "reparacion_lista_remito", ingreso_id=ingreso_id, actor_user_id=actor_user_id
    )


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


# Los avisos que sólo tienen sentido mientras el equipo no salió del taller.
# Todos se cierran solos cuando se imprime la orden de salida.
REMITO_PENDING_NOTIFICATION_KEYS = (
    "reparacion_lista_remito",
    "no_se_repara_listo_remito",
    "presupuesto_rechazado_reparado",
)


def resolve_remito_pending_notifications(ingreso_id):
    """La orden de salida ya se imprimió: esos avisos dejan de estar pendientes.

    Sin esto la campana quedaba con "falta imprimir el remito" de equipos que
    ya habían salido — incluso cuando entre el aviso y la impresión pasaban
    segundos, como al aprobar el presupuesto de un equipo ya reparado.
    """
    total = 0
    for key in REMITO_PENDING_NOTIFICATION_KEYS:
        total += resolve_notifications(key, entity_type="ingreso", entity_id=ingreso_id)
    return total


def resolve_presupuesto_rechazado_reparado(ingreso_id):
    """El técnico ya deshizo la reparación: el pedido deja de estar abierto."""
    return resolve_notifications(
        "presupuesto_rechazado_reparado",
        entity_type="ingreso",
        entity_id=ingreso_id,
    )


def notify_no_se_repara_for_remito(ingreso_id, *, request=None, actor_user_id=None):
    """El equipo sale sin reparar: Recepción tiene que imprimir la orden.

    Es el gemelo de ``notify_repair_ready_for_remito`` para el otro final del
    circuito. Va por campana y push: el correo del circuito de reparación
    avisa de trabajo terminado, y este avisa de trabajo que no se va a hacer.
    """
    if actor_user_id is None and request is not None:
        actor_user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
    user_ids = _remito_audience_user_ids(
        "no_se_repara_listo_remito", ingreso_id=ingreso_id, actor_user_id=actor_user_id
    )
    try:
        return notify_no_se_repara_listo_remito(ingreso_id, user_ids=user_ids)
    except Exception:
        logger.exception(
            "No se pudo avisar que el equipo sale sin reparar",
            extra={"ingreso_id": ingreso_id},
        )
        return 0


def notify_repair_ready_for_remito(ingreso_id, *, request=None, actor_name="", recipients=None, actor_user_id=None):
    if actor_user_id is None and request is not None:
        actor_user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
    configured = _normalize_recipients(recipients) or _repair_ready_recipients()
    # El correo también tiene que respetar el toggle "Mail" del centro de
    # notificaciones: antes iba a la lista de entorno pasara lo que pasara.
    recips = filter_email_recipients_by_preference("reparacion_lista_remito", configured)
    # Y tampoco tiene sentido mandarle el correo al que acaba de provocar el
    # hecho: la lista de entorno es la casilla de jefatura, que es justo quien
    # aprueba los presupuestos.
    actor_email = _actor_email(actor_user_id)
    if actor_email:
        recips = [addr for addr in recips if str(addr).strip().lower() != actor_email]
    user_ids = _repair_ready_notification_user_ids(ingreso_id, actor_user_id=actor_user_id)
    inserted = 0
    try:
        # `recips` es sólo para el correo de abajo: si se pasara como `emails`
        # acá, cualquier casilla que coincida con un usuario de NEXORA recibiría
        # la campana sin haberla configurado.
        inserted = notify_reparacion_lista_remito(ingreso_id, user_ids=user_ids)
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
            transaction.on_commit(
                lambda: dispatch_email_delivery(_send_notice, label=f"repair-ready-{ingreso_id}")
            )
        else:
            dispatch_email_delivery(_send_notice, label=f"repair-ready-{ingreso_id}")
    except Exception:
        dispatch_email_delivery(_send_notice, label=f"repair-ready-{ingreso_id}")

    return {"notifications": inserted, "recipients": recips}
