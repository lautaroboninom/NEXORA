from django.db import connection, transaction
from django.conf import settings
from .activity_audit import should_audit_read_request
from .ip_utils import get_client_ip
from .views.helpers import _set_audit_user
import json


def _safe_clear_rollback():
    try:
        transaction.set_rollback(False)
    except Exception:
        pass


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
                    body_json = json.loads(raw.decode("utf-8", errors="ignore"))
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


