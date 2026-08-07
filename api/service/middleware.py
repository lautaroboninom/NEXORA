from django.db import connection, transaction
from django.conf import settings
from .activity_audit import should_audit_read_request
from .ip_utils import get_client_ip
from .views.helpers import _set_audit_user
import json
import logging
import time

logger = logging.getLogger(__name__)


SENSITIVE_AUDIT_KEYS = {
    "password",
    "clave",
    "token",
    "secret",
    "secreto",
    "encrypted_password",
    "current_password",
    "new_password",
    "password_confirmation",
}


def redact_sensitive_audit_data(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized = str(key or "").strip().lower().replace("-", "_")
            redacted[key] = (
                "[REDACTADO]"
                if normalized in SENSITIVE_AUDIT_KEYS
                else redact_sensitive_audit_data(item)
            )
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_audit_data(item) for item in value]
    return value


def _safe_clear_rollback():
    try:
        transaction.set_rollback(False)
    except Exception:
        pass


class RequestTimingMiddleware:
    """Registra los requests que superan un umbral de duración.

    Sin esto, un request lento pero exitoso es invisible: el log sólo muestra los
    que fallan, así que no hay forma de saber qué pantalla está tardando ni por
    qué. Va lo más arriba posible de la cadena para medir el request completo,
    middlewares incluidos.

    Umbral en `REQUEST_TIMING_LOG_THRESHOLD_MS`; 0 lo desactiva.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        try:
            self.threshold_ms = int(getattr(settings, "REQUEST_TIMING_LOG_THRESHOLD_MS", 1000))
        except (TypeError, ValueError):
            self.threshold_ms = 1000

    def __call__(self, request):
        if self.threshold_ms <= 0:
            return self.get_response(request)
        started = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # Una excepción que tardó también es información: distingue "falló
            # rápido" (config/credenciales) de "falló por timeout" (red).
            logger.warning(
                "slow_request method=%s path=%s status=exception ms=%s",
                request.method,
                request.path,
                elapsed_ms,
            )
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms >= self.threshold_ms:
            # Los datos van en el mensaje: el formatter configurado no imprime `extra`.
            logger.warning(
                "slow_request method=%s path=%s status=%s ms=%s",
                request.method,
                request.path,
                getattr(response, "status_code", "-"),
                elapsed_ms,
            )
        return response


class RLSMiddleware:
    """No-op (RLS era solo para Postgres)."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


class AuditUserMiddleware:
    """Reset audit user per request to avoid leaking session-level settings."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            _set_audit_user(request)
        except Exception:
            _safe_clear_rollback()
            pass
        return self.get_response(request)


class ActivityLogMiddleware:
    """
    Auditoría a nivel aplicación (HTTP). Registra SOLO métodos de escritura
    (POST/PATCH/PUT/DELETE) con metadata mínima. Append-only.
    """
    WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    READ_METHODS = {"GET"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, "AUDIT_LOG_ENABLED", False):
            return self.get_response(request)

        path = request.path or ""
        for pref in getattr(settings, "AUDIT_LOG_EXCLUDE_PREFIXES", []):
            if path.startswith(pref):
                return self.get_response(request)

        is_write = request.method in self.WRITE_METHODS
        is_read_open = request.method in self.READ_METHODS and should_audit_read_request(path, request.GET)
        should_log = is_write or is_read_open
        user = getattr(request, "user", None)
        user_id = getattr(user, "id", None)
        role = getattr(user, "rol", None)
        ip = get_client_ip(request.META)
        ua = request.META.get("HTTP_USER_AGENT", "")[:512]

        body_json = None
        if is_write:
            try:
                if request.body:
                    raw = request.body[: getattr(settings, "AUDIT_LOG_MAX_BODY", 4096)]
                    body_json = redact_sensitive_audit_data(
                        json.loads(raw.decode("utf-8", errors="ignore"))
                    )
            except Exception:
                _safe_clear_rollback()
                body_json = None

        response = self.get_response(request)

        if should_log:
            try:
                with transaction.atomic():
                    with connection.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO audit_log (ts, user_id, role, method, path, ip, user_agent, status_code, body)
                            VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                            """,
                            [
                                user_id,
                                role,
                                request.method,
                                path,
                                ip,
                                ua,
                                getattr(response, "status_code", None),
                                json.dumps(body_json) if body_json is not None else None,
                            ],
                        )
            except Exception:
                _safe_clear_rollback()
                pass  # nunca romper la request por problemas de log

        return response


