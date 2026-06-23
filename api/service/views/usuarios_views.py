import secrets
import hashlib
import datetime as dt
import logging

from django.conf import settings
from django.db import connection, transaction, IntegrityError
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions
from rest_framework.exceptions import ValidationError, PermissionDenied
from django.core.mail import send_mail

from .helpers import (
    q,
    exec_void,
    require_roles,
    require_roles_strict,
    _set_audit_user,
    _email_append_footer_text,
    _email_append_footer_html,
    TOKEN_TTL_MIN,
)
from ..permission_catalog import get_catalog, PERMISSION_CODES
from ..permissions import (
    EFFECT_ALLOW,
    EFFECT_DENY,
    EFFECT_INHERIT,
    apply_overrides,
    require_any_permission,
    require_permission,
    reset_overrides,
    resolve_effective_permissions,
)
from ..roles import ROLE_KEYS, ROLE_CHOICES

logger = logging.getLogger(__name__)


class UsuariosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin","jefe_veedor"])
        try:
            rows = q(
                """
                SELECT
                  u.id,
                  u.nombre,
                  u.email,
                  u.rol,
                  u.activo,
                  u.bejerman_seller_code,
                  (ubc.user_id IS NOT NULL) AS bejerman_credentials_configured,
                  COALESCE(ubc.is_valid, FALSE) AS bejerman_credentials_valid,
                  CASE
                    WHEN COALESCE(ubc.bejerman_username, '') = '' THEN ''
                    WHEN LENGTH(ubc.bejerman_username) <= 2 THEN ubc.bejerman_username
                    ELSE SUBSTRING(ubc.bejerman_username FROM 1 FOR 1)
                         || REPEAT('*', GREATEST(LENGTH(ubc.bejerman_username) - 2, 1))
                         || SUBSTRING(ubc.bejerman_username FROM LENGTH(ubc.bejerman_username) FOR 1)
                  END AS bejerman_credentials_username,
                  COALESCE(ubc.last_error, '') AS bejerman_credentials_last_error,
                  COALESCE(up.cnt, 0) AS permisos_personalizados
                FROM users u
                LEFT JOIN user_bejerman_credentials ubc ON ubc.user_id = u.id
                LEFT JOIN (
                  SELECT user_id, COUNT(*) AS cnt
                  FROM user_permission_overrides
                  GROUP BY user_id
                ) up ON up.user_id = u.id
                ORDER BY u.id ASC
                """
            )
        except Exception:
            rows = q(
                """
                SELECT id, nombre, email, rol, activo
                FROM users
                ORDER BY id ASC
                """
            ) or []
            for item in rows:
                item["permisos_personalizados"] = 0
        return Response(rows)

    @transaction.atomic
    def post(self, request):
        require_roles_strict(request, ["jefe", "admin"])
        data = request.data or {}
        nombre = (data.get("nombre") or "").strip()
        email = (data.get("email") or "").strip().lower()
        rol_raw = (data.get("rol") or "tecnico")
        rol = rol_raw.strip().lower().replace(" ", "_").replace("-", "_")
        bejerman_seller_code = (data.get("bejerman_seller_code") or data.get("bejermanSellerCode") or "").strip().upper()

        if not nombre or not email:
            raise ValidationError("Nombre y email son requeridos")
        if rol not in ROLE_KEYS:
            raise ValidationError("Rol inválido")

        _set_audit_user(request)
        existed = q("SELECT id FROM users WHERE email=%s", [email], one=True)
        q(
            """
            INSERT INTO users(nombre, email, rol, activo)
            VALUES (%(n)s, %(e)s, %(r)s, true)
            ON CONFLICT (email) DO UPDATE
            SET nombre = EXCLUDED.nombre,
                rol = EXCLUDED.rol
            """,
            {"n": nombre, "e": email, "r": rol},
        )
        try:
            q(
                "UPDATE users SET bejerman_seller_code = %s WHERE email = %s",
                [bejerman_seller_code or None, email],
            )
        except Exception:
            pass

        if not existed:
            try:
                user = q("SELECT id, nombre, email FROM users WHERE email=%s", [email], one=True)
                if user:
                    token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(token.encode()).hexdigest()
                    exp = timezone.now() + dt.timedelta(minutes=TOKEN_TTL_MIN)
                    ua = request.META.get("HTTP_USER_AGENT", "")
                    ip = request.META.get("REMOTE_ADDR", "")
                    exec_void(
                        """
                        INSERT INTO password_reset_tokens(user_id, token_hash, expires_at, ip, user_agent)
                        VALUES (%s,%s,%s,%s,%s)
                        """,
                        [user["id"], token_hash, exp, ip, ua],
                    )
                    base = getattr(settings, "PUBLIC_WEB_URL", None) or getattr(settings, "FRONTEND_ORIGIN", "http://localhost:5173")
                    url = f"{(base or '').rstrip('/')}/restablecer?t={token}"
                    subj = "Bienvenido a NEXORA - Configure su contraseña"
                    txt = (
                        f"Hola {user['nombre']},\n\n"
                        f"Te damos la bienvenida a NEXORA. "
                        f"Use este enlace para establecer su contraseña (válido {TOKEN_TTL_MIN} minutos):\n{url}\n\n"
                        f"Si no esperaba este correo, ignórelo."
                    )
                    html = f"""
                        <p>Hola {user['nombre']},</p>
                        <p>Bienvenido a <strong>NEXORA</strong>.</p>
                        <p>Use este enlace para establecer su contraseña (válido {TOKEN_TTL_MIN} minutos):</p>
                        <p><a href="{url}">{url}</a></p>
                        <p>Si no esperaba este correo, ignórelo.</p>
                    """
                    try:
                        txt = _email_append_footer_text(txt)
                        html = _email_append_footer_html(html)
                        sent = send_mail(
                            subj,
                            txt,
                            settings.DEFAULT_FROM_EMAIL,
                            [user["email"]],
                            html_message=html,
                            fail_silently=False,
                        )
                        if sent < 1:
                            logger.warning(
                                "No se pudo enviar el correo de invitación: el backend no aceptó el mensaje.",
                                extra={"user_id": user["id"], "recipient": user["email"]},
                            )
                    except Exception:
                        logger.exception(
                            "No se pudo enviar el correo de invitación.",
                            extra={"user_id": user["id"], "recipient": user["email"]},
                        )
            except Exception:
                pass

        return Response({"ok": True, "invited": not existed})


class UsuarioActivoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, uid):
        require_roles_strict(request, ["jefe", "admin"])
        _set_audit_user(request)
        activo = bool(request.data.get("activo"))
        q("UPDATE users SET activo = %(a)s WHERE id = %(id)s", {"a": activo, "id": uid})
        return Response({"ok": True, "activo": activo})


class UsuarioResetPassView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, uid):
        require_roles_strict(request, ["jefe", "admin"])
        user = q("SELECT id, email, nombre, activo FROM users WHERE id=%s", [uid], one=True)
        if not user or not user.get("activo"):
            return Response({"detail": "Usuario inexistente o inactivo"}, status=404)

        _set_audit_user(request)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        exp = timezone.now() + dt.timedelta(minutes=TOKEN_TTL_MIN)
        ua = request.META.get("HTTP_USER_AGENT", "")
        ip = request.META.get("REMOTE_ADDR", "")
        exec_void(
            """
            INSERT INTO password_reset_tokens(user_id, token_hash, expires_at, ip, user_agent)
            VALUES (%s,%s,%s,%s,%s)
            """,
            [user["id"], token_hash, exp, ip, ua],
        )
        base = getattr(settings, "PUBLIC_WEB_URL", None) or getattr(settings, "FRONTEND_ORIGIN", "http://localhost:5173")
        url = f"{(base or '').rstrip('/')}/restablecer?t={token}"
        subj = "NEXORA - Enlace para establecer su contraseña"
        txt = (
            f"Hola {user['nombre']},\n\n"
            f"Solicitaron un enlace para establecer o restablecer su contraseña. "
            f"Use este enlace (válido {TOKEN_TTL_MIN} minutos):\n{url}\n\n"
            f"Si no fue usted, ignore este correo."
        )
        html = f"""
            <p>Hola {user['nombre']},</p>
            <p>Solicitaron un enlace para establecer o restablecer su contraseña.</p>
            <p>Use este enlace (válido {TOKEN_TTL_MIN} minutos):</p>
            <p><a href="{url}">{url}</a></p>
            <p>Si no fue usted, ignore este correo.</p>
        """
        email_sent = False
        try:
            txt = _email_append_footer_text(txt)
            html = _email_append_footer_html(html)
            sent = send_mail(
                subj,
                txt,
                settings.DEFAULT_FROM_EMAIL,
                [user["email"]],
                html_message=html,
                fail_silently=False,
            )
            email_sent = sent > 0
            if not email_sent:
                logger.warning(
                    "No se pudo enviar el correo de restablecimiento: el backend no aceptó el mensaje.",
                    extra={"user_id": user["id"], "recipient": user["email"]},
                )
        except Exception:
            logger.exception(
                "No se pudo enviar el correo de restablecimiento.",
                extra={"user_id": user["id"], "recipient": user["email"]},
            )

        return Response({"ok": True, "sent": email_sent})


class UsuarioRolePermView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, uid):
        require_roles_strict(request, ["jefe", "admin"])
        rol = request.data.get("rol")

        _set_audit_user(request)
        sets, params = [], {"id": uid}
        prev = q("SELECT rol FROM users WHERE id=%s", [uid], one=True) or {}
        prev_role = (prev.get("rol") or "").strip().lower()
        next_role = prev_role
        if rol is not None:
            r = (rol or "").strip().lower()
            if r not in ROLE_KEYS:
                raise ValidationError("Rol inválido")
            sets.append("rol = %(rol)s")
            params["rol"] = r
            next_role = r

        if not sets:
            return Response({"ok": True})
        q(f"UPDATE users SET {', '.join(sets)} WHERE id = %(id)s", params)

        # Jefe no admite overrides; al entrar/salir de jefe limpiar estado granular.
        if next_role == "jefe" or prev_role == "jefe":
            reset_overrides(uid)
        return Response({"ok": True})


class UsuarioDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, uid):
        require_roles_strict(request, ["jefe", "admin"])
        try:
            with transaction.atomic():
                _set_audit_user(request)
                exec_void("UPDATE ingresos SET asignado_a = NULL WHERE asignado_a = %s", [uid])
                exec_void("UPDATE ingresos SET recibido_por = NULL WHERE recibido_por = %s", [uid])
                exec_void("UPDATE models   SET tecnico_id  = NULL WHERE tecnico_id  = %s", [uid])
                exec_void("UPDATE marcas   SET tecnico_id  = NULL WHERE tecnico_id  = %s", [uid])
                exec_void("UPDATE ingreso_events SET usuario_id = NULL WHERE usuario_id = %s", [uid])
                exec_void("DELETE FROM users WHERE id = %s", [uid])
        except IntegrityError:
            return Response(
                {"detail": "No se pudo eliminar: el usuario esta referenciado por otros registros. Reasigne/desasigne esas referencias o desactive el usuario."},
                status=409,
            )
        return Response({"ok": True})


class CatalogoRolesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles_strict(request, ["jefe", "admin"])  # mantener restriccion como en legacy
        return Response([{"value": k, "label": v} for k, v in ROLE_CHOICES])


class CatalogoTecnicosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_any_permission(
            request,
            ["action.ingreso.change_assignment", "action.ingreso.create", "page.new_ingreso"],
        )
        rows = q(
            """
            SELECT id, nombre
            FROM users
            WHERE activo=true AND rol IN ('tecnico','jefe','jefe_veedor')
            ORDER BY nombre
            """
        ) or []
        return Response(rows)


def _load_user_for_permissions(uid):
    row = q(
        """
        SELECT id, nombre, email, rol, activo
        FROM users
        WHERE id=%s
        """,
        [uid],
        one=True,
    )
    if not row:
        raise ValidationError("Usuario no encontrado")
    row["rol"] = (row.get("rol") or "").strip().lower()
    return row


def _load_override_map(uid):
    rows = q(
        """
        SELECT permission_code, effect
        FROM user_permission_overrides
        WHERE user_id=%s
        ORDER BY permission_code
        """,
        [uid],
    ) or []
    out = {}
    for item in rows:
        code = (item.get("permission_code") or "").strip()
        effect = (item.get("effect") or "").strip().lower()
        if code and effect in (EFFECT_ALLOW, EFFECT_DENY):
            out[code] = effect
    return out


def _serialize_user_permissions(uid):
    user_row = _load_user_for_permissions(uid)
    override_map = _load_override_map(uid)
    effective = resolve_effective_permissions(
        user_id=user_row["id"],
        role=user_row["rol"],
        overrides=override_map,
    )
    merged_states = {code: EFFECT_INHERIT for code in PERMISSION_CODES}
    for code, effect in override_map.items():
        merged_states[code] = effect
    return {
        "user": user_row,
        "editable": user_row["rol"] != "jefe",
        "overrides": merged_states,
        "effective_permissions": effective,
        "raw_overrides": override_map,
    }


class CatalogoPermisosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage_permissions")
        return Response(
            {
                "permissions": get_catalog(),
                "effects": [EFFECT_INHERIT, EFFECT_ALLOW, EFFECT_DENY],
            }
        )


class UsuarioPermisosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, uid):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage_permissions")
        return Response(_serialize_user_permissions(uid))

    @transaction.atomic
    def put(self, request, uid):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage_permissions")
        payload = request.data or {}
        overrides = payload.get("overrides")
        if not isinstance(overrides, dict):
            raise ValidationError("overrides requerido y debe ser un objeto")

        user_row = _load_user_for_permissions(uid)
        if user_row["rol"] == "jefe":
            raise PermissionDenied("No se pueden editar permisos para rol jefe")

        _set_audit_user(request)
        try:
            apply_overrides(
                user_id=uid,
                raw_overrides=overrides,
                updated_by=getattr(request.user, "id", None),
            )
        except ValueError as exc:
            raise ValidationError(str(exc))
        return Response(_serialize_user_permissions(uid))


class UsuarioPermisosResetView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, uid):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage_permissions")
        user_row = _load_user_for_permissions(uid)
        if user_row["rol"] == "jefe":
            raise PermissionDenied("No se pueden editar permisos para rol jefe")
        _set_audit_user(request)
        reset_overrides(uid)
        return Response(_serialize_user_permissions(uid))


__all__ = [
    'UsuariosView',
    'UsuarioActivoView',
    'UsuarioResetPassView',
    'UsuarioRolePermView',
    'UsuarioDeleteView',
    'CatalogoRolesView',
    'CatalogoPermisosView',
    'UsuarioPermisosView',
    'UsuarioPermisosResetView',
    'CatalogoTecnicosView',
]
