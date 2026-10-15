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

from .auth_views import (
    _bejerman_credentials_error_message,
    _discover_bejerman_workstations,
    _validate_bejerman_credentials,
)
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
from ..bejerman_sdk import (
    BejermanSdkConfigError,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
)
from ..bejerman_user_credentials import (
    BejermanUserCredentialsError,
    BejermanUserCredentialsInvalid,
    delete_user_bejerman_credentials,
    normalize_bejerman_workstation,
    resolve_user_bejerman_workstation,
    save_user_bejerman_credentials,
    set_user_bejerman_credentials_prompt_dismissed,
)
from ..permission_catalog import PERMISSION_CODES, PERMISSION_CODES_SET, get_catalog, get_role_defaults
from ..permissions import (
    EFFECT_ALLOW,
    EFFECT_DENY,
    EFFECT_INHERIT,
    apply_overrides,
    require_any_permission,
    require_permission,
    reset_overrides,
    resolve_effective_permissions,
    user_has_permission,
)
from ..roles import ROLE_KEYS, ROLE_CHOICES

logger = logging.getLogger(__name__)
SELLER_CODE_ROLES = {"ventas"}


def _normalize_role(value):
    return (value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _normalize_seller_code(value):
    return (value or "").strip().upper()


def _load_user(uid):
    row = q(
        """
        SELECT id, nombre, email, rol, activo
          FROM users
         WHERE id = %s
        """,
        [uid],
        one=True,
    )
    if not row:
        raise ValidationError("Usuario no encontrado")
    row["rol"] = _normalize_role(row.get("rol"))
    return row


def _can_view_sensitive_access(request):
    return bool(
        _normalize_role(getattr(getattr(request, "user", None), "rol", "")) == "jefe"
        or user_has_permission(request, "action.users.manage")
    )


def _serialize_users(*, uid=None, include_sensitive=False):
    filters = []
    params = []
    if uid is not None:
        filters.append("u.id = %s")
        params.append(int(uid))
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = q(
        f"""
        SELECT
          u.id,
          u.nombre,
          u.email,
          u.rol,
          u.activo,
          u.creado_en,
          (NULLIF(COALESCE(u.hash_pw, ''), '') IS NOT NULL) AS password_configured,
          u.last_login_at,
          HOST(u.last_login_ip) AS last_login_ip,
          COALESCE(u.last_login_user_agent, '') AS last_login_user_agent,
          COALESCE(u.bejerman_workstation, '') AS bejerman_workstation,
          COALESCE(u.bejerman_credentials_prompt_dismissed, FALSE)
            AS bejerman_credentials_prompt_dismissed,
          COALESCE(u.bejerman_seller_code, '') AS bejerman_seller_code,
          u.bejerman_seller_code_confirmed_at,
          (ubc.user_id IS NOT NULL) AS bejerman_credentials_configured,
          COALESCE(ubc.is_valid, FALSE) AS bejerman_credentials_valid,
          CASE
            WHEN COALESCE(ubc.bejerman_username, '') = '' THEN ''
            WHEN LENGTH(ubc.bejerman_username) <= 2 THEN ubc.bejerman_username
            ELSE SUBSTRING(ubc.bejerman_username FROM 1 FOR 1)
                 || REPEAT('*', GREATEST(LENGTH(ubc.bejerman_username) - 2, 1))
                 || SUBSTRING(
                      ubc.bejerman_username
                      FROM LENGTH(ubc.bejerman_username)
                      FOR 1
                    )
          END AS bejerman_credentials_username,
          COALESCE(ubc.last_error, '') AS bejerman_credentials_last_error,
          ubc.validated_at AS bejerman_credentials_validated_at,
          ubc.updated_at AS bejerman_credentials_updated_at,
          COALESCE(up.cnt, 0) AS permisos_personalizados,
          COALESCE(up.allowed_count, 0) AS permisos_permitidos,
          COALESCE(up.denied_count, 0) AS permisos_bloqueados,
          COALESCE(np.cnt, 0) AS notificaciones_personalizadas,
          prt.created_at AS invitation_created_at,
          prt.expires_at AS invitation_expires_at,
          prt.used_at AS invitation_used_at,
          CASE
            WHEN prt.id IS NULL THEN 'none'
            WHEN prt.used_at IS NOT NULL THEN 'used'
            WHEN prt.expires_at <= CURRENT_TIMESTAMP THEN 'expired'
            ELSE 'pending'
          END AS invitation_status
        FROM users u
        LEFT JOIN user_bejerman_credentials ubc ON ubc.user_id = u.id
        LEFT JOIN (
          SELECT
            user_id,
            COUNT(*) AS cnt,
            COUNT(*) FILTER (WHERE effect = 'allow') AS allowed_count,
            COUNT(*) FILTER (WHERE effect = 'deny') AS denied_count
          FROM user_permission_overrides
          GROUP BY user_id
        ) up ON up.user_id = u.id
        LEFT JOIN (
          SELECT user_id, COUNT(*) AS cnt
            FROM notification_user_preferences
           WHERE enabled IS NOT NULL
              OR bell_enabled IS NOT NULL
              OR email_enabled IS NOT NULL
              OR push_enabled IS NOT NULL
           GROUP BY user_id
        ) np ON np.user_id = u.id
        LEFT JOIN LATERAL (
          SELECT id, created_at, expires_at, used_at
            FROM password_reset_tokens
           WHERE user_id = u.id
           ORDER BY created_at DESC, id DESC
           LIMIT 1
        ) prt ON TRUE
        {where_sql}
        ORDER BY u.id ASC
        """,
        params,
    ) or []
    if not include_sensitive:
        for item in rows:
            item["last_login_ip"] = ""
            item["last_login_user_agent"] = ""
    return rows


def _serialize_user(uid, *, include_sensitive=False):
    rows = _serialize_users(uid=uid, include_sensitive=include_sensitive)
    return rows[0] if rows else None


def _guard_last_active_jefe(target, *, next_role=None, next_active=None, deleting=False):
    current_role = _normalize_role(target.get("rol"))
    current_active = bool(target.get("activo"))
    resolved_role = current_role if next_role is None else _normalize_role(next_role)
    resolved_active = current_active if next_active is None else bool(next_active)
    removes_active_jefe = current_active and current_role == "jefe" and (
        deleting or resolved_role != "jefe" or not resolved_active
    )
    if not removes_active_jefe:
        return
    remaining = q(
        """
        SELECT COUNT(*) AS count
          FROM users
         WHERE activo = TRUE
           AND LOWER(TRIM(rol)) = 'jefe'
           AND id <> %s
        """,
        [target["id"]],
        one=True,
    ) or {}
    if int(remaining.get("count") or 0) <= 0:
        raise ValidationError("Debe quedar al menos un usuario Jefe activo")


JEFATURA_ROLES = {"jefe", "jefe_veedor", "jefe_suplente"}


def _guard_role_assignment(request, target, *, next_role=None):
    """Qué puede hacer sobre una cuenta quien administra usuarios sin ser Jefe.

    `action.users.manage` se puede otorgar por usuario desde la pantalla de permisos, y el
    rol se cambia con ese mismo permiso: sin este corte, a quien se lo dieran le alcanzaba
    para ponerse rol Jefe y quedarse con todo. Por eso las cuentas de Jefe y los roles de
    jefatura sólo los toca un Jefe, y nadie cambia su propio rol.
    """
    target = target or {}
    current_role = _normalize_role(target.get("rol"))
    resolved_role = current_role if next_role is None else _normalize_role(next_role)
    changes_role = resolved_role != current_role
    actor = getattr(request, "user", None)
    actor_id = getattr(actor, "id", None)
    target_id = target.get("id")
    if (
        changes_role
        and actor_id is not None
        and target_id is not None
        and int(actor_id) == int(target_id)
    ):
        raise ValidationError("No podés cambiar tu propio rol")
    if _normalize_role(getattr(actor, "rol", "")) == "jefe":
        return
    if current_role == "jefe":
        raise PermissionDenied("Solo un usuario Jefe puede modificar la cuenta de otro Jefe")
    if changes_role and (current_role in JEFATURA_ROLES or resolved_role in JEFATURA_ROLES):
        raise PermissionDenied("Solo un usuario Jefe puede asignar o quitar roles de jefatura")


def _validate_seller_code(uid, role, raw_code):
    code = _normalize_seller_code(raw_code)
    if len(code) > 4:
        raise ValidationError("El código de vendedor no puede superar 4 caracteres")
    if code and _normalize_role(role) not in SELLER_CODE_ROLES:
        raise ValidationError("El código de vendedor solo aplica al rol Ventas")
    if code:
        duplicate = q(
            """
            SELECT id
              FROM users
             WHERE id <> %s
               AND UPPER(TRIM(COALESCE(bejerman_seller_code, ''))) = %s
             LIMIT 1
            """,
            [uid, code],
            one=True,
        )
        if duplicate:
            raise ValidationError("Ese código de vendedor ya está asignado")
    return code


def _issue_password_link(request, user, *, welcome=False):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = timezone.now() + dt.timedelta(minutes=TOKEN_TTL_MIN)
    ua = request.META.get("HTTP_USER_AGENT", "")
    ip = request.META.get("REMOTE_ADDR", "")
    exec_void(
        """
        INSERT INTO password_reset_tokens(
          user_id, token_hash, expires_at, ip, user_agent
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        [user["id"], token_hash, expires_at, ip, ua],
    )
    base = getattr(settings, "PUBLIC_WEB_URL", None) or getattr(
        settings,
        "FRONTEND_ORIGIN",
        "http://localhost:5173",
    )
    url = f"{(base or '').rstrip('/')}/restablecer?t={token}"
    if welcome:
        subject = "Bienvenido a NEXORA - Configurá tu contraseña"
        intro = "Te damos la bienvenida a NEXORA."
        action = "Usá este enlace para establecer tu contraseña"
    else:
        subject = "NEXORA - Enlace para restablecer tu contraseña"
        intro = "Se solicitó un enlace para establecer o restablecer tu contraseña."
        action = "Usá este enlace"
    text = (
        f"Hola {user['nombre']},\n\n"
        f"{intro}\n"
        f"{action} (válido {TOKEN_TTL_MIN} minutos):\n{url}\n\n"
        "Si no esperabas este correo, ignoralo."
    )
    html = f"""
        <p>Hola {user['nombre']},</p>
        <p>{intro}</p>
        <p>{action} (válido {TOKEN_TTL_MIN} minutos):</p>
        <p><a href="{url}">{url}</a></p>
        <p>Si no esperabas este correo, ignoralo.</p>
    """
    email_sent = False
    try:
        sent = send_mail(
            subject,
            _email_append_footer_text(text),
            settings.DEFAULT_FROM_EMAIL,
            [user["email"]],
            html_message=_email_append_footer_html(html),
            fail_silently=False,
        )
        email_sent = sent > 0
        if not email_sent:
            logger.warning(
                "El backend no aceptó el correo de acceso.",
                extra={"user_id": user["id"], "recipient": user["email"]},
            )
    except Exception:
        logger.exception(
            "No se pudo enviar el correo de acceso.",
            extra={"user_id": user["id"], "recipient": user["email"]},
        )
    return {
        "generated": True,
        "sent": email_sent,
        "status": "sent" if email_sent else "send_failed",
        "expires_at": expires_at.isoformat(),
    }


class UsuariosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        return Response(
            _serialize_users(include_sensitive=_can_view_sensitive_access(request))
        )

    @transaction.atomic
    def post(self, request):
        require_roles_strict(request, ["jefe", "admin"])
        data = request.data or {}
        nombre = (data.get("nombre") or "").strip()
        email = (data.get("email") or "").strip().lower()
        rol = _normalize_role(data.get("rol") or "tecnico")
        bejerman_seller_code = _normalize_seller_code(
            data.get("bejerman_seller_code") or data.get("bejermanSellerCode")
        )

        if not nombre or not email:
            raise ValidationError("Nombre y email son requeridos")
        if rol not in ROLE_KEYS:
            raise ValidationError("Rol inválido")
        _guard_role_assignment(request, {"id": None, "rol": ""}, next_role=rol)

        _set_audit_user(request)
        if q("SELECT id FROM users WHERE email=%s", [email], one=True):
            return Response(
                {"detail": "Ya existe un usuario con ese email"},
                status=409,
            )
        try:
            created = q(
                """
                INSERT INTO users(
                  nombre, email, rol, activo, bejerman_seller_code,
                  bejerman_seller_code_confirmed_at
                )
                VALUES (%s, %s, %s, TRUE, %s, %s)
                RETURNING id, nombre, email
                """,
                [
                    nombre,
                    email,
                    rol,
                    _validate_seller_code(0, rol, bejerman_seller_code) or None,
                    timezone.now() if bejerman_seller_code else None,
                ],
                one=True,
            )
        except IntegrityError:
            return Response(
                {"detail": "Ya existe un usuario con ese email o código de vendedor"},
                status=409,
            )
        invitation = _issue_password_link(request, created, welcome=True)
        return Response(
            {
                "ok": True,
                "created": True,
                "invitation": invitation,
                "user": _serialize_user(
                    created["id"],
                    include_sensitive=_can_view_sensitive_access(request),
                ),
            },
            status=201,
        )


class UsuarioActivoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, uid):
        require_roles_strict(request, ["jefe", "admin"])
        _set_audit_user(request)
        activo = request.data.get("activo")
        if not isinstance(activo, bool):
            raise ValidationError("activo debe ser verdadero o falso")
        target = _load_user(uid)
        _guard_role_assignment(request, target)
        _guard_last_active_jefe(target, next_active=activo)
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
        invitation = _issue_password_link(request, user, welcome=False)
        return Response({"ok": True, **invitation})


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
            r = _normalize_role(rol)
            if r not in ROLE_KEYS:
                raise ValidationError("Rol inválido")
            target = _load_user(uid)
            _guard_role_assignment(request, target, next_role=r)
            _guard_last_active_jefe(target, next_role=r)
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


class UsuarioDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, uid):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        user = _serialize_user(
            uid,
            include_sensitive=_can_view_sensitive_access(request),
        )
        if not user:
            return Response({"detail": "Usuario no encontrado"}, status=404)
        return Response(user)

    @transaction.atomic
    def patch(self, request, uid):
        require_roles_strict(request, ["jefe", "admin"])
        payload = request.data or {}
        target = _load_user(uid)
        actor_role = _normalize_role(
            getattr(getattr(request, "user", None), "rol", "")
        )
        next_role = target["rol"]
        next_active = bool(target["activo"])
        sets = []
        params = {"id": uid}

        if "nombre" in payload:
            nombre = (payload.get("nombre") or "").strip()
            if not nombre:
                raise ValidationError("Nombre requerido")
            sets.append("nombre = %(nombre)s")
            params["nombre"] = nombre
        if "email" in payload:
            email = (payload.get("email") or "").strip().lower()
            if not email:
                raise ValidationError("Email requerido")
            duplicate = q(
                "SELECT id FROM users WHERE email = %s AND id <> %s",
                [email, uid],
                one=True,
            )
            if duplicate:
                raise ValidationError("Ya existe un usuario con ese email")
            sets.append("email = %(email)s")
            params["email"] = email
        if "rol" in payload:
            next_role = _normalize_role(payload.get("rol"))
            if next_role not in ROLE_KEYS:
                raise ValidationError("Rol inválido")
            sets.append("rol = %(rol)s")
            params["rol"] = next_role
        if "activo" in payload:
            if not isinstance(payload.get("activo"), bool):
                raise ValidationError("activo debe ser verdadero o falso")
            next_active = payload["activo"]
            sets.append("activo = %(activo)s")
            params["activo"] = next_active

        bejerman_keys = {
            "bejerman_workstation",
            "bejermanWorkstation",
            "bejerman_credentials_prompt_dismissed",
            "bejermanCredentialsPromptDismissed",
            "bejerman_seller_code",
            "bejermanSellerCode",
        }
        if bejerman_keys.intersection(payload.keys()) and actor_role != "jefe":
            raise PermissionDenied(
                "Solo un usuario Jefe puede administrar la configuración Bejerman"
            )

        if "bejerman_workstation" in payload or "bejermanWorkstation" in payload:
            workstation = normalize_bejerman_workstation(
                payload.get(
                    "bejerman_workstation",
                    payload.get("bejermanWorkstation"),
                )
            )
            sets.append("bejerman_workstation = %(workstation)s")
            params["workstation"] = workstation or None

        prompt_key = (
            "bejerman_credentials_prompt_dismissed"
            if "bejerman_credentials_prompt_dismissed" in payload
            else "bejermanCredentialsPromptDismissed"
        )
        if prompt_key in payload:
            dismissed = payload.get(prompt_key)
            if not isinstance(dismissed, bool):
                raise ValidationError(
                    "bejermanCredentialsPromptDismissed debe ser verdadero o falso"
                )
            sets.append(
                "bejerman_credentials_prompt_dismissed = %(prompt_dismissed)s"
            )
            params["prompt_dismissed"] = dismissed

        if "bejerman_seller_code" in payload or "bejermanSellerCode" in payload:
            code = _validate_seller_code(
                uid,
                next_role,
                payload.get(
                    "bejerman_seller_code",
                    payload.get("bejermanSellerCode"),
                ),
            )
            sets.append("bejerman_seller_code = %(seller_code)s")
            sets.append(
                "bejerman_seller_code_confirmed_at = %(seller_confirmed_at)s"
            )
            params["seller_code"] = code or None
            params["seller_confirmed_at"] = timezone.now() if code else None

        _guard_role_assignment(request, target, next_role=next_role)
        _guard_last_active_jefe(
            target,
            next_role=next_role,
            next_active=next_active,
        )
        if sets:
            _set_audit_user(request)
            q(f"UPDATE users SET {', '.join(sets)} WHERE id = %(id)s", params)
            if next_role == "jefe" or target["rol"] == "jefe":
                reset_overrides(uid)
        return Response(
            _serialize_user(
                uid,
                include_sensitive=_can_view_sensitive_access(request),
            )
        )

    def delete(self, request, uid):
        require_roles_strict(request, ["jefe", "admin"])
        target = _load_user(uid)
        actor_id = getattr(getattr(request, "user", None), "id", None)
        if actor_id is not None and int(actor_id) == int(uid):
            raise ValidationError("No podés eliminar tu propio usuario")
        _guard_role_assignment(request, target)
        _guard_last_active_jefe(target, deleting=True)
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
                {"detail": "No se pudo eliminar: el usuario está referenciado por otros registros. Reasigná esas referencias o desactivá el usuario."},
                status=409,
            )
        return Response({"ok": True})


class UsuarioBejermanWorkstationsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, uid):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage")
        _load_user(uid)
        username = (request.query_params.get("username") or "").strip().upper()
        try:
            return Response(_discover_bejerman_workstations(username, int(uid)))
        except BejermanUserCredentialsInvalid as exc:
            return Response({"detail": str(exc)}, status=400)


class UsuarioBejermanCredentialsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def put(self, request, uid):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage")
        _load_user(uid)
        username = (
            request.data.get("username") or request.data.get("usuario") or ""
        ).strip().upper()
        password = (
            request.data.get("password") or request.data.get("clave") or ""
        ).strip()
        if not username or not password:
            return Response(
                {"detail": "Usuario y clave Bejerman requeridos"},
                status=400,
            )
        try:
            workstation = normalize_bejerman_workstation(
                request.data.get("workstation")
                or request.data.get("puesto")
                or resolve_user_bejerman_workstation(int(uid)),
                required=True,
            )
            _validate_bejerman_credentials(
                username,
                password,
                int(uid),
                workstation=workstation,
            )
            _set_audit_user(request)
            status_data = save_user_bejerman_credentials(
                int(uid),
                username,
                password,
                workstation=workstation,
            )
        except BejermanUserCredentialsInvalid as exc:
            return Response({"detail": str(exc)}, status=400)
        except (BejermanUserCredentialsError, BejermanSdkConfigError) as exc:
            return Response({"detail": str(exc)}, status=503)
        except BejermanSdkResponseError as exc:
            return Response(
                {"detail": _bejerman_credentials_error_message(username, exc)},
                status=400,
            )
        except BejermanSdkUnavailable as exc:
            return Response(
                {
                    "detail": (
                        "No se pudo validar contra Bejerman en este momento. "
                        f"Detalle técnico: {str(exc)}"
                    )
                },
                status=503,
            )
        return Response(
            {
                "ok": True,
                "bejerman": status_data,
                "user": _serialize_user(uid, include_sensitive=True),
            }
        )

    @transaction.atomic
    def delete(self, request, uid):
        require_roles_strict(request, ["jefe"])
        require_permission(request, "action.users.manage")
        _load_user(uid)
        _set_audit_user(request)
        status_data = delete_user_bejerman_credentials(int(uid))
        return Response(
            {
                "ok": True,
                "bejerman": status_data,
                "user": _serialize_user(uid, include_sensitive=True),
            }
        )


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
            WHERE activo=true AND rol IN ('tecnico','jefe','jefe_veedor','jefe_suplente')
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
    stored_overrides = _load_override_map(uid)
    # Un código que ya no está en el catálogo no viaja en `overrides`: el editor manda el
    # mapa completo al guardar y ese código hacía fallar el PUT entero.
    override_map = {
        code: effect for code, effect in stored_overrides.items() if code in PERMISSION_CODES_SET
    }
    effective = resolve_effective_permissions(
        user_id=user_row["id"],
        role=user_row["rol"],
        overrides=override_map,
    )
    merged_states = {code: EFFECT_INHERIT for code in PERMISSION_CODES}
    merged_states.update(override_map)
    return {
        "user": user_row,
        "editable": user_row["rol"] != "jefe",
        "overrides": merged_states,
        "effective_permissions": effective,
        # Lo que el rol trae de fábrica: el editor lo usa para mostrar qué hereda cada
        # permiso y calcular el efectivo antes de guardar.
        "role_defaults": get_role_defaults(user_row["rol"]),
        "raw_overrides": override_map,
        "unknown_overrides": sorted(
            code for code in stored_overrides if code not in PERMISSION_CODES_SET
        ),
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
    'UsuarioDetailView',
    'UsuarioBejermanWorkstationsView',
    'UsuarioBejermanCredentialsView',
    'CatalogoRolesView',
    'CatalogoPermisosView',
    'UsuarioPermisosView',
    'UsuarioPermisosResetView',
    'CatalogoTecnicosView',
]
