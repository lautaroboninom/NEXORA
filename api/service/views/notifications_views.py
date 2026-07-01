from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from service.notifications import (
    add_notification_email_address,
    delete_notification_email_address,
    delete_push_subscription,
    get_current_user_notification_configuration,
    get_user_notification_settings,
    get_push_config_for_user,
    list_notifications_for_user,
    mark_all_notifications_read,
    mark_notification_clicked,
    save_push_subscription,
    save_user_notification_settings,
    update_notification_email_address,
)

from .helpers import _set_audit_user, require_permission, require_roles_strict


def _current_user_id(request):
    return (
        getattr(getattr(request, "user", None), "id", None)
        or getattr(request, "user_id", None)
    )


def _current_user_role(request):
    return (
        getattr(getattr(request, "user", None), "rol", None)
        or getattr(getattr(request, "user_obj", None), "rol", None)
        or getattr(request, "user_role", "")
        or ""
    ).strip().lower()


def _can_manage_extra_emails(request):
    return _current_user_role(request) in {"admin", "supervisor", "cobranzas"}


class NotificacionesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"items": [], "unread_count": 0})
        limit = request.GET.get("limit") or 20
        return Response(list_notifications_for_user(uid, limit=limit))


class NotificacionClickView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, notification_id: int):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        _set_audit_user(request)
        row = mark_notification_clicked(uid, notification_id)
        if not row:
            return Response({"detail": "Notificación no encontrada"}, status=404)
        return Response({"ok": True, "href": row.get("href") or ""})


class NotificacionesReadAllView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        _set_audit_user(request)
        updated = mark_all_notifications_read(uid)
        return Response({"ok": True, "updated": updated})


class NotificacionesPushConfigView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"available": False, "publicKey": "", "active": False})
        return Response(get_push_config_for_user(uid))


class NotificacionesPushSubscriptionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        _set_audit_user(request)
        try:
            data = save_push_subscription(
                uid,
                request.data or {},
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response({"ok": True, **data})

    def delete(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        payload = request.data or {}
        endpoint = payload.get("endpoint") if isinstance(payload, dict) else None
        _set_audit_user(request)
        deleted = delete_push_subscription(uid, endpoint=endpoint)
        return Response({"ok": True, "deleted": deleted})


class NotificacionesConfiguracionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        data = get_current_user_notification_configuration(uid)
        if not data:
            return Response({"detail": "Usuario no encontrado"}, status=404)
        return Response(data)

    def put(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        payload = request.data or {}
        preferences = payload.get("preferences")
        if not isinstance(preferences, dict):
            return Response({"detail": "preferences debe ser un objeto"}, status=400)
        _set_audit_user(request)
        try:
            save_user_notification_settings(uid, preferences, updated_by=uid)
            data = get_current_user_notification_configuration(uid)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response(data)


class NotificacionesConfiguracionEmailsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        if not _can_manage_extra_emails(request):
            return Response({"detail": "No tenés permisos para administrar emails extra."}, status=403)
        payload = request.data or {}
        _set_audit_user(request)
        try:
            row = add_notification_email_address(
                uid,
                payload.get("email"),
                label=payload.get("label", ""),
                updated_by=uid,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response(row, status=201)


class NotificacionesConfiguracionEmailDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, email_id: int):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        if not _can_manage_extra_emails(request):
            return Response({"detail": "No tenés permisos para administrar emails extra."}, status=403)
        _set_audit_user(request)
        try:
            row = update_notification_email_address(uid, email_id, request.data or {}, updated_by=uid)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=503)
        if not row:
            return Response({"detail": "Email no encontrado"}, status=404)
        return Response(row)

    def delete(self, request, email_id: int):
        uid = _current_user_id(request)
        if not uid:
            return Response({"detail": "Usuario inválido"}, status=400)
        if not _can_manage_extra_emails(request):
            return Response({"detail": "No tenés permisos para administrar emails extra."}, status=403)
        _set_audit_user(request)
        deleted = delete_notification_email_address(uid, email_id)
        if not deleted:
            return Response({"detail": "Email no encontrado"}, status=404)
        return Response({"ok": True, "deleted": deleted})


class UsuarioNotificacionesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, uid: int):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage_permissions")
        data = get_user_notification_settings(uid)
        if not data:
            return Response({"detail": "Usuario no encontrado"}, status=404)
        return Response(data)

    def put(self, request, uid: int):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage_permissions")
        payload = request.data or {}
        preferences = payload.get("preferences")
        if not isinstance(preferences, dict):
            return Response({"detail": "preferences debe ser un objeto"}, status=400)
        _set_audit_user(request)
        actor_id = _current_user_id(request)
        try:
            data = save_user_notification_settings(uid, preferences, updated_by=actor_id)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response(data)


__all__ = [
    "NotificacionesView",
    "NotificacionClickView",
    "NotificacionesReadAllView",
    "NotificacionesPushConfigView",
    "NotificacionesPushSubscriptionView",
    "NotificacionesConfiguracionView",
    "NotificacionesConfiguracionEmailsView",
    "NotificacionesConfiguracionEmailDetailView",
    "UsuarioNotificacionesView",
]
