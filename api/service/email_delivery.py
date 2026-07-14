"""Entrega de correo que no debe demorar operaciones interactivas."""

from __future__ import annotations

import logging
from threading import Thread
from typing import Callable

from django.conf import settings
from django.db import close_old_connections


logger = logging.getLogger(__name__)


def _must_run_inline() -> bool:
    """Conserva la entrega determinística para backends locales y pruebas."""
    backend = str(getattr(settings, "EMAIL_BACKEND", "") or "")
    local_backends = (
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
    )
    return (not bool(getattr(settings, "EMAIL_DELIVERY_ASYNC", True))) or backend in local_backends


def dispatch_email_delivery(callback: Callable[[], None], *, label: str) -> None:
    """Ejecuta correo fuera de la respuesta HTTP cuando el backend es remoto.

    La notificación interna ya quedó registrada antes de invocar esta función.
    Así una falla o demora del SMTP no bloquea una aprobación o la descarga de
    una orden de salida.
    """
    if _must_run_inline():
        callback()
        return

    def _run() -> None:
        close_old_connections()
        try:
            callback()
        except Exception:
            logger.exception("Falló la entrega de correo en segundo plano", extra={"label": label})
        finally:
            close_old_connections()

    Thread(target=_run, name=f"nexora-email-{label}", daemon=True).start()
