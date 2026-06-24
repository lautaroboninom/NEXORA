from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Callable

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

MANDATORY_REMITO_PDF_EMAIL_ROLES = ("admin", "cobranzas", "recepcion")
PdfLoader = Callable[[], tuple[bytes, str] | tuple[bytes, str, str]]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _append_footer(body: str) -> str:
    footer = _clean(getattr(settings, "EMAIL_LEGAL_FOOTER", ""))
    base = _clean(body)
    return f"{base}\n\n{footer}" if footer else base


def _mandatory_remito_pdf_recipients() -> list[str]:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT email
                  FROM users
                 WHERE COALESCE(activo, TRUE) = TRUE
                   AND LOWER(TRIM(COALESCE(rol, ''))) = ANY(%s)
                   AND NULLIF(TRIM(COALESCE(email, '')), '') IS NOT NULL
                 ORDER BY CASE LOWER(TRIM(COALESCE(rol, '')))
                            WHEN 'admin' THEN 1
                            WHEN 'cobranzas' THEN 2
                            WHEN 'recepcion' THEN 3
                            ELSE 9
                          END,
                          LOWER(TRIM(email))
                """,
                [list(MANDATORY_REMITO_PDF_EMAIL_ROLES)],
            )
            rows = cur.fetchall()
    except Exception:
        logger.exception("remito_pdf_email_recipients_failed")
        return []

    recipients: OrderedDict[str, str] = OrderedDict()
    for (email,) in rows:
        value = _clean(email)
        if value:
            recipients.setdefault(value.lower(), value)
    return list(recipients.values())


def _actor_label(actor_user_id: int | None) -> str:
    if not actor_user_id:
        return ""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT nombre, email FROM users WHERE id = %s", [actor_user_id])
            row = cur.fetchone()
    except Exception:
        logger.exception("remito_pdf_email_actor_lookup_failed", extra={"actor_user_id": actor_user_id})
        return ""
    if not row:
        return ""
    name = _clean(row[0])
    email = _clean(row[1])
    if name and email:
        return f"{name} ({email})"
    return name or email


def _normalize_pdf_attachment(result: tuple[bytes, str] | tuple[bytes, str, str]) -> tuple[bytes, str, str]:
    if len(result) == 2:
        pdf_bytes, filename = result
        return pdf_bytes, "application/pdf", filename
    pdf_bytes, content_type, filename = result
    return pdf_bytes, content_type or "application/pdf", filename


def _send_email_with_fallback(
    subject: str,
    body: str,
    recipients: list[str],
    attachment: tuple[bytes, str, str],
) -> bool:
    if not recipients:
        return False

    pdf_bytes, content_type, filename = attachment

    def _send(connection_obj=None) -> bool:
        message = EmailMessage(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipients,
            connection=connection_obj,
        )
        message.attach(filename or "remito-bejerman.pdf", pdf_bytes, content_type or "application/pdf")
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
            logger.exception("remito_pdf_email_failed", extra={"recipients": recipients})
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
            logger.exception("remito_pdf_email_fallback_failed", extra={"recipients": recipients, "original_error": str(exc)})
            return False


def _email_body(
    *,
    remito_number: str,
    document_type: str,
    company_key: str,
    customer_name: str,
    source: str,
    details: list[str],
    actor_user_id: int | None,
) -> str:
    lines = [
        "Se emitió un remito en Bejerman.",
        "",
        f"Remito: {remito_number or '-'}",
        f"Tipo: {document_type or '-'}",
        f"Empresa: {company_key or '-'}",
        f"Cliente: {customer_name or '-'}",
        f"Origen: {source or '-'}",
        f"Fecha: {timezone.localtime().strftime('%Y-%m-%d %H:%M')}",
    ]
    actor = _actor_label(actor_user_id)
    if actor:
        lines.append(f"Emitido por: {actor}")
    clean_details = [_clean(line) for line in details or [] if _clean(line)]
    if clean_details:
        lines.extend(["", "Detalle:", *clean_details])
    lines.extend(["", "Se adjunta el PDF emitido por Bejerman."])
    return _append_footer("\n".join(lines))


def notify_bejerman_remito_pdf_issued(
    *,
    remito_number: str,
    document_type: str = "",
    company_key: str = "",
    customer_name: str = "",
    source: str = "",
    details: list[str] | None = None,
    actor_user_id: int | None = None,
    pdf_loader: PdfLoader | None = None,
) -> dict[str, Any]:
    remito = _clean(remito_number)
    if not remito or pdf_loader is None:
        return {"emails": 0, "recipients": []}

    recipients = _mandatory_remito_pdf_recipients()
    if not recipients:
        logger.warning("remito_pdf_email no recipients", extra={"remito_number": remito})
        return {"emails": 0, "recipients": []}

    subject_parts = ["Remito Bejerman", _clean(document_type), remito, _clean(customer_name)]
    subject = " - ".join(part for part in subject_parts if part)
    body = _email_body(
        remito_number=remito,
        document_type=_clean(document_type),
        company_key=_clean(company_key),
        customer_name=_clean(customer_name),
        source=_clean(source),
        details=details or [],
        actor_user_id=actor_user_id,
    )

    def _send_notice() -> None:
        try:
            attachment = _normalize_pdf_attachment(pdf_loader())
        except Exception:
            logger.exception("remito_pdf_email_pdf_load_failed", extra={"remito_number": remito})
            return
        ok = _send_email_with_fallback(subject, body, recipients, attachment)
        logger.info(
            "remito_pdf_email sent=%s remito=%s recipients=%s backend=%s",
            bool(ok),
            remito,
            recipients,
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

    return {"emails": len(recipients), "recipients": recipients}
