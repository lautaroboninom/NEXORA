from __future__ import annotations

import json
import logging
from collections import OrderedDict
from datetime import timedelta
from email.utils import parseaddr
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives as EmailMessage, get_connection
from django.db import connection, transaction
from django.utils import timezone


logger = logging.getLogger(__name__)

OUTBOX_TABLE = "notification_email_outbox"
OUTBOX_STATUSES = ("pending", "retry", "sending", "sent", "failed")
ATTACHMENT_KINDS = ("", "delivery_remito_group", "ingress_remito", "service_order_billing", "quote_pdf")
RETRY_DELAYS_SECONDS = (15, 30, 60, 120, 300, 600, 1800, 3600, 7200, 14400, 28800, 43200)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _runs_inline() -> bool:
    backend = _clean(getattr(settings, "EMAIL_BACKEND", ""))
    local_backends = (
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
    )
    return backend in local_backends or not bool(getattr(settings, "EMAIL_DELIVERY_ASYNC", True))


def _normalized_recipients(recipients: list[str] | tuple[str, ...]) -> list[str]:
    unique = OrderedDict()
    for raw_email in recipients or []:
        email = parseaddr(_clean(raw_email))[1] or _clean(raw_email)
        if email:
            unique.setdefault(email.lower(), email)
    return list(unique.values())


def queue_notification_email(
    *,
    notification_key: str,
    dedupe_key: str,
    recipients: list[str] | tuple[str, ...],
    subject: str,
    body: str,
    html_body: str = "",
    attachment_kind: str = "",
    attachment_reference: str = "",
    attachment_context: dict[str, Any] | None = None,
    max_attempts: int = 12,
    reactivate_failed: bool = True,
) -> dict[str, Any]:
    clean_notification_key = _clean(notification_key)
    clean_dedupe_key = _clean(dedupe_key)
    clean_attachment_kind = _clean(attachment_kind)
    clean_recipients = _normalized_recipients(recipients)
    if not clean_notification_key or not clean_dedupe_key:
        raise ValueError("notification_key y dedupe_key son obligatorios.")
    if clean_attachment_kind not in ATTACHMENT_KINDS:
        raise ValueError(f"Tipo de adjunto inválido: {clean_attachment_kind}")
    if clean_attachment_kind and not _clean(attachment_reference):
        raise ValueError("attachment_reference es obligatorio cuando hay adjunto.")
    if not clean_recipients:
        return {"queued": 0, "reactivated": 0, "duplicates": 0, "delivery_ids": []}

    queued = 0
    reactivated = 0
    duplicates = 0
    delivery_ids: list[int] = []
    context_payload = dict(attachment_context or {})
    if html_body:
        context_payload["html_body"] = str(html_body)
    context_text = json.dumps(context_payload, ensure_ascii=False, default=str)
    attempts = max(1, min(50, int(max_attempts or 12)))

    with transaction.atomic():
        with connection.cursor() as cur:
            for recipient in clean_recipients:
                cur.execute(
                    f"""
                    INSERT INTO {OUTBOX_TABLE}(
                      notification_key, dedupe_key, recipient, subject, body,
                      attachment_kind, attachment_reference, attachment_context,
                      max_attempts, status, next_attempt_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'pending',CURRENT_TIMESTAMP)
                    ON CONFLICT (notification_key, dedupe_key, recipient) DO NOTHING
                    RETURNING id
                    """,
                    [
                        clean_notification_key,
                        clean_dedupe_key,
                        recipient,
                        _clean(subject),
                        str(body or ""),
                        clean_attachment_kind,
                        _clean(attachment_reference),
                        context_text,
                        attempts,
                    ],
                )
                inserted = cur.fetchone()
                if inserted:
                    delivery_ids.append(int(inserted[0]))
                    queued += 1
                    continue

                cur.execute(
                    f"""
                    SELECT id, status
                      FROM {OUTBOX_TABLE}
                     WHERE notification_key=%s AND dedupe_key=%s AND recipient=%s
                     FOR UPDATE
                    """,
                    [clean_notification_key, clean_dedupe_key, recipient],
                )
                existing = cur.fetchone()
                if not existing:
                    continue
                delivery_id, status = int(existing[0]), _clean(existing[1])
                delivery_ids.append(delivery_id)
                if reactivate_failed and status == "failed":
                    cur.execute(
                        f"""
                        UPDATE {OUTBOX_TABLE}
                           SET subject=%s,
                               body=%s,
                               attachment_kind=%s,
                               attachment_reference=%s,
                               attachment_context=%s::jsonb,
                               max_attempts=%s,
                               status='retry',
                               attempt_count=0,
                               next_attempt_at=CURRENT_TIMESTAMP,
                               last_error=''
                         WHERE id=%s
                        """,
                        [
                            _clean(subject),
                            str(body or ""),
                            clean_attachment_kind,
                            _clean(attachment_reference),
                            context_text,
                            attempts,
                            delivery_id,
                        ],
                    )
                    reactivated += 1
                else:
                    duplicates += 1

    if _runs_inline() and delivery_ids:
        process_notification_email_outbox(delivery_ids=delivery_ids, limit=len(delivery_ids))
    return {
        "queued": queued,
        "reactivated": reactivated,
        "duplicates": duplicates,
        "delivery_ids": delivery_ids,
    }


def describe_email_deliveries(delivery_ids: list[int] | tuple[int, ...]) -> list[dict[str, Any]]:
    """Estado real de cada correo encolado, tal como está guardado en la cola."""
    clean_ids = sorted({int(value) for value in delivery_ids or [] if value is not None})
    if not clean_ids:
        return []
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, notification_key, recipient, subject, status,
                   attempt_count, max_attempts, last_error, sent_at, next_attempt_at
              FROM {OUTBOX_TABLE}
             WHERE id = ANY(%s)
             ORDER BY id
            """,
            [clean_ids],
        )
        columns = [column[0] for column in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    return [
        {
            "id": int(row["id"]),
            "notificationKey": _clean(row.get("notification_key")),
            "recipient": _clean(row.get("recipient")),
            "subject": _clean(row.get("subject")),
            "status": _clean(row.get("status")) or "pending",
            "attempts": int(row.get("attempt_count") or 0),
            "maxAttempts": int(row.get("max_attempts") or 0),
            "lastError": _clean(row.get("last_error")),
            "sentAt": row.get("sent_at"),
            "nextAttemptAt": row.get("next_attempt_at"),
        }
        for row in rows
    ]


def summarize_email_deliveries(delivery_ids: list[int] | tuple[int, ...]) -> dict[str, Any]:
    """Resume el estado real de un grupo de correos encolados.

    `status` es siempre el estado guardado en la cola: `sent` solo cuando el
    servidor de correo confirmó la entrega, nunca por haber encolado el mensaje.
    """
    try:
        deliveries = describe_email_deliveries(delivery_ids)
    except Exception:
        logger.exception("No se pudo leer el estado de la cola de correos")
        return {
            "status": "desconocido",
            "detail": "No se pudo consultar el estado de la cola de correos.",
            "deliveries": [],
        }
    if not deliveries:
        return {
            "status": "sin_envios",
            "detail": "No se registraron correos para esta operación.",
            "deliveries": [],
        }

    by_status: dict[str, list[dict[str, Any]]] = {}
    for delivery in deliveries:
        by_status.setdefault(delivery["status"], []).append(delivery)
    sent = by_status.get("sent", [])
    failed = by_status.get("failed", [])
    retry = by_status.get("retry", [])
    pending = by_status.get("pending", []) + by_status.get("sending", [])
    all_recipients = [delivery["recipient"] for delivery in deliveries]

    if len(sent) == len(deliveries):
        sent_at = max((d["sentAt"] for d in sent if d["sentAt"]), default=None)
        hora = timezone.localtime(sent_at).strftime("%H:%M") if sent_at else ""
        detail = f"Correo enviado a {', '.join(all_recipients)}"
        detail = f"{detail} a las {hora}." if hora else f"{detail}."
        status = "sent"
    elif failed and not retry and not pending:
        error = failed[0]["lastError"] or "el servidor de correo rechazó el envío"
        detail = (
            f"No se pudo enviar el correo a {', '.join(d['recipient'] for d in failed)} "
            f"después de {failed[0]['attempts']} intentos: {error}"
        )
        status = "failed"
    elif failed or retry:
        problem = (failed or retry)[0]
        error = problem["lastError"] or "error de conexión con el servidor de correo"
        detail = (
            f"El correo a {problem['recipient']} falló ({error}) y se está reintentando "
            f"automáticamente (intento {problem['attempts']} de {problem['maxAttempts']})."
        )
        status = "retry"
    else:
        detail = f"Correo encolado para {', '.join(all_recipients)}; se envía en los próximos segundos."
        status = "queued"

    return {
        "status": status,
        "detail": detail,
        "deliveries": deliveries,
        "sentCount": len(sent),
        "pendingCount": len(pending) + len(retry),
        "failedCount": len(failed),
    }


def _claim_deliveries(*, limit: int, delivery_ids: list[int] | None = None) -> list[dict[str, Any]]:
    clean_ids = sorted({int(value) for value in delivery_ids or [] if value is not None})
    id_filter = "AND id = ANY(%s)" if clean_ids else ""
    params: list[Any] = [clean_ids] if clean_ids else []
    params.append(max(1, min(500, int(limit or 50))))
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {OUTBOX_TABLE}
                   SET status='retry',
                       next_attempt_at=CURRENT_TIMESTAMP,
                       last_error=CASE
                         WHEN last_error='' THEN 'Recuperado después de una ejecución interrumpida.'
                         ELSE last_error
                       END
                 WHERE status='sending'
                   AND updated_at < CURRENT_TIMESTAMP - INTERVAL '10 minutes'
                """
            )
            cur.execute(
                f"""
                SELECT id
                  FROM {OUTBOX_TABLE}
                 WHERE status IN ('pending','retry')
                   AND next_attempt_at <= CURRENT_TIMESTAMP
                   {id_filter}
                 ORDER BY next_attempt_at, id
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
                """,
                params,
            )
            claimed_ids = [int(row[0]) for row in cur.fetchall()]
            if not claimed_ids:
                return []
            cur.execute(
                f"""
                UPDATE {OUTBOX_TABLE}
                   SET status='sending',
                       attempt_count=attempt_count + 1
                 WHERE id = ANY(%s)
                 RETURNING id, notification_key, dedupe_key, recipient, subject, body,
                           attachment_kind, attachment_reference, attachment_context,
                           status, attempt_count, max_attempts, next_attempt_at,
                           last_error, sent_at, created_at, updated_at
                """,
                [claimed_ids],
            )
            columns = [column[0] for column in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    rows_by_id = {int(row["id"]): row for row in rows}
    return [rows_by_id[delivery_id] for delivery_id in claimed_ids if delivery_id in rows_by_id]


def _delivery_context(delivery: dict[str, Any]) -> dict[str, Any]:
    raw_context = delivery.get("attachment_context")
    if isinstance(raw_context, dict):
        return raw_context
    elif isinstance(raw_context, str):
        try:
            parsed_context = json.loads(raw_context)
        except Exception:
            parsed_context = {}
        return parsed_context if isinstance(parsed_context, dict) else {}
    return {}


def _attachment_for_delivery(delivery: dict[str, Any]) -> tuple[bytes, str, str] | None:
    kind = _clean(delivery.get("attachment_kind"))
    reference = _clean(delivery.get("attachment_reference"))
    context = _delivery_context(delivery)
    if not kind:
        return None
    try:
        if kind == "delivery_remito_group":
            from .bejerman_delivery import get_remito_group_pdf

            return get_remito_group_pdf(reference, actor_user_id=context.get("actor_user_id"))
        if kind == "ingress_remito":
            from .bejerman_ris import _load_ingress_remito_pdf_attachment

            return _load_ingress_remito_pdf_attachment(int(reference), context.get("actor_user_id"))
        if kind == "service_order_billing":
            from .service_order_billing import render_service_order_billing_pdf

            pdf_bytes, filename = render_service_order_billing_pdf(int(reference))
            return pdf_bytes, "application/pdf", filename
        if kind == "quote_pdf":
            from .pdf import render_quote_pdf

            # La referencia es "<ingreso_id>:<quote_id>": la versión importa, porque
            # un mismo ingreso puede tener varias versiones de presupuesto.
            ingreso_raw, _, quote_raw = reference.partition(":")
            # Ojo: render_quote_pdf devuelve (filename, pdf), al revés que las otras.
            filename, pdf_bytes = render_quote_pdf(
                int(ingreso_raw),
                quote_id=int(quote_raw) if quote_raw.strip() else None,
            )
            if not pdf_bytes:
                raise ValueError(f"Sin presupuesto para adjuntar: {reference}")
            return pdf_bytes, "application/pdf", filename
    except Exception:
        if bool(context.get("optional")):
            logger.exception(
                "No se pudo generar el adjunto opcional; el correo se enviará sin adjunto",
                extra={"delivery_id": delivery.get("id"), "attachment_kind": kind, "reference": reference},
            )
            return None
        raise
    raise ValueError(f"Tipo de adjunto no soportado: {kind}")


def _send_delivery(delivery: dict[str, Any], attachment: tuple[bytes, str, str] | None) -> None:
    context = _delivery_context(delivery)
    html_body = str(context.get("html_body") or "").strip()

    def _message(connection_obj=None) -> EmailMessage:
        message = EmailMessage(
            delivery.get("subject") or "",
            delivery.get("body") or "",
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            [delivery.get("recipient")],
            connection=connection_obj,
        )
        if html_body:
            message.attach_alternative(html_body, "text/html")
        if attachment:
            pdf_bytes, content_type, filename = attachment
            message.attach(filename or "adjunto.pdf", pdf_bytes, content_type or "application/octet-stream")
        return message

    try:
        sent = _message().send(fail_silently=False)
        if not sent:
            raise RuntimeError("El backend de correo no confirmó la entrega.")
        return
    except Exception as primary_error:
        try:
            configured_port = int(getattr(settings, "EMAIL_PORT", 0) or 0)
        except Exception:
            configured_port = 0
        if configured_port != 587:
            raise
        fallback = get_connection(
            backend=getattr(settings, "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"),
            host=getattr(settings, "EMAIL_HOST", None),
            port=465,
            username=getattr(settings, "EMAIL_HOST_USER", None),
            password=getattr(settings, "EMAIL_HOST_PASSWORD", None),
            use_tls=False,
            use_ssl=True,
            fail_silently=False,
        )
        try:
            sent = _message(fallback).send(fail_silently=False)
            if not sent:
                raise RuntimeError("El backend alternativo no confirmó la entrega.")
        except Exception as fallback_error:
            raise RuntimeError(
                f"Falló SMTP principal ({type(primary_error).__name__}) y alternativo ({type(fallback_error).__name__})."
            ) from fallback_error


def _mark_sent(delivery_id: int) -> None:
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {OUTBOX_TABLE}
               SET status='sent',
                   sent_at=CURRENT_TIMESTAMP,
                   next_attempt_at=CURRENT_TIMESTAMP,
                   last_error=''
             WHERE id=%s
            """,
            [delivery_id],
        )


def _mark_failed_attempt(delivery: dict[str, Any], exc: Exception) -> str:
    attempt_count = int(delivery.get("attempt_count") or 1)
    max_attempts = int(delivery.get("max_attempts") or 12)
    error = _clean(exc)[:4000] or type(exc).__name__
    if attempt_count >= max_attempts:
        status = "failed"
        next_attempt_at = timezone.now()
    else:
        status = "retry"
        delay_index = min(max(attempt_count - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)
        next_attempt_at = timezone.now() + timedelta(seconds=RETRY_DELAYS_SECONDS[delay_index])
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {OUTBOX_TABLE}
               SET status=%s,
                   next_attempt_at=%s,
                   last_error=%s
             WHERE id=%s
            """,
            [status, next_attempt_at, error, delivery.get("id")],
        )
    return status


def process_notification_email_outbox(
    *,
    limit: int = 50,
    delivery_ids: list[int] | None = None,
) -> dict[str, int]:
    deliveries = _claim_deliveries(limit=limit, delivery_ids=delivery_ids)
    result = {"claimed": len(deliveries), "sent": 0, "retry": 0, "failed": 0}
    attachment_cache: dict[tuple[str, str, str], tuple[bytes, str, str] | None | Exception] = {}
    for delivery in deliveries:
        try:
            raw_context = delivery.get("attachment_context")
            if isinstance(raw_context, str):
                context_key = raw_context
            else:
                context_key = json.dumps(raw_context or {}, ensure_ascii=False, sort_keys=True, default=str)
            attachment_key = (
                _clean(delivery.get("attachment_kind")),
                _clean(delivery.get("attachment_reference")),
                context_key,
            )
            if attachment_key not in attachment_cache:
                try:
                    attachment_cache[attachment_key] = _attachment_for_delivery(delivery)
                except Exception as exc:
                    attachment_cache[attachment_key] = exc
            cached_attachment = attachment_cache[attachment_key]
            if isinstance(cached_attachment, Exception):
                raise cached_attachment
            attachment = cached_attachment
            _send_delivery(delivery, attachment)
            _mark_sent(int(delivery["id"]))
            result["sent"] += 1
        except Exception as exc:
            status = _mark_failed_attempt(delivery, exc)
            result[status] += 1
            logger.warning(
                "El correo quedó pendiente de reintento",
                extra={
                    "delivery_id": delivery.get("id"),
                    "notification_key": delivery.get("notification_key"),
                    "attempt": delivery.get("attempt_count"),
                    "status": status,
                    "error": _clean(exc),
                },
            )
    return result


def retry_failed_notification_emails(
    *,
    notification_keys: list[str] | None = None,
    failed_within_hours: float | int | None = None,
) -> int:
    """Devuelve a la cola los correos que agotaron sus intentos.

    Doce intentos se reparten en unas 28 horas. Un servidor de correo caído más
    tiempo que eso —o mal configurado un fin de semana largo— mandaba a `failed`
    todo lo de esa ventana, y `failed` no lo volvía a mirar nadie: los avisos se
    perdían en silencio, que es la peor forma de perderlos.

    `failed_within_hours` acota a los que fallaron hace poco. Es lo que evita que
    reactivar en bucle se convierta en insistirle para siempre a una casilla que
    no existe: pasada la ventana, el correo queda muerto de verdad y hay que
    mirarlo a mano.
    """

    clean_keys = sorted({_clean(key) for key in notification_keys or [] if _clean(key)})
    filters = ""
    params: list[Any] = []
    if clean_keys:
        filters += " AND notification_key = ANY(%s)"
        params.append(clean_keys)
    if failed_within_hours is not None:
        filters += " AND updated_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')"
        params.append(float(failed_within_hours))
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {OUTBOX_TABLE}
               SET status='retry',
                   attempt_count=0,
                   next_attempt_at=CURRENT_TIMESTAMP,
                   last_error=''
             WHERE status='failed'
               {filters}
            """,
            params,
        )
        return int(cur.rowcount or 0)
