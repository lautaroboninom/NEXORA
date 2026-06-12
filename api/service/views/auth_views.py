import datetime as dt
import hashlib
import secrets

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.utils import timezone
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth import issue_token, verify_hash, JWT_TTL_MIN
from ..ip_utils import get_client_ip
from ..models import User
from ..permissions import resolve_effective_permissions
from .helpers import (
    COOLDOWN_MIN,
    TOKEN_TTL_MIN,
    exec_void,
    q,
    _set_audit_user,
    _is_login_locked,
    _login_rate_key,
    _register_login_failure,
    _reset_login_failure,
)


@api_view(["GET"])  # público
@permission_classes([AllowAny])
def ping(request):
    return Response({"ok": True})


def _normalize_role(s: str) -> str:
    s = (s or "").strip().lower().replace(" ", "_").replace("-", "_")
    return s


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        password = (request.data.get("password") or "")

        if not email or not password:
            raise AuthenticationFailed("Email y contraseña requeridos.")

        ip = get_client_ip(request.META) or ""
        key = _login_rate_key(email, ip)
        if _is_login_locked(key):
            raise AuthenticationFailed("Demasiados intentos. Probá más tarde.")

        try:
            user = User.objects.get(email=email, activo=True)
        except User.DoesNotExist:
            _register_login_failure(key)
            raise AuthenticationFailed("Usuario o contraseña inválidos.")

        if not getattr(user, "hash_pw", ""):
            raise AuthenticationFailed(
                "El usuario aún no tiene contraseña. Use \"Olvidé mi contraseña\" para inicializarla."
            )

        if not verify_hash(password, user.hash_pw):
            _register_login_failure(key)
            raise AuthenticationFailed("Usuario o contraseña inválidos.")

        _reset_login_failure(key)
        token = issue_token(user)
        permissions_map = resolve_effective_permissions(user_id=user.id, role=user.rol)
        resp = Response(
            {
                "token": token,
                "user": {
                    "id": user.id,
                    "nombre": user.nombre,
                    "rol": _normalize_role(user.rol),
                    "email": getattr(user, "email", ""),
                    "permissions": permissions_map,
                },
                # Mantener la misma forma que /auth/session/
                "features": {},
            }
        )
        try:
            cookie_name = getattr(settings, "AUTH_COOKIE_NAME", "auth_token")
            cookie_secure = getattr(settings, "AUTH_COOKIE_SECURE", (not getattr(settings, "DEBUG", False)))
            cookie_samesite = getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax")
            cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", None) or None
            max_age = int(JWT_TTL_MIN) * 60
            resp.set_cookie(
                cookie_name,
                token,
                max_age=max_age,
                httponly=True,
                secure=bool(cookie_secure),
                samesite=cookie_samesite,
                domain=cookie_domain,
            )
        except Exception:
            # No romper login por problemas seteando cookie (el token igual se devuelve en el body)
            pass
        return resp


class ForgotPasswordView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        ua = request.META.get("HTTP_USER_AGENT", "")
        ip = get_client_ip(request.META) or ""

        ok_response = Response({"ok": True})
        if not email:
            return ok_response

        user = q(
            "SELECT id, email, nombre, activo FROM users WHERE LOWER(email)=%s",
            [email],
            one=True,
        )
        if not user or not user.get("activo"):
            return ok_response

        recent = q(
            """
            SELECT id FROM password_reset_tokens
             WHERE user_id=%(uid)s AND used_at IS NULL AND expires_at>NOW()
               AND created_at > NOW() - (%(mins)s || ' minutes')::interval
             ORDER BY id DESC LIMIT 1
            """,
            {"uid": user["id"], "mins": COOLDOWN_MIN},
            one=True,
        )
        if recent:
            return ok_response

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        exp = timezone.now() + dt.timedelta(minutes=TOKEN_TTL_MIN)

        _set_audit_user(request)
        exec_void(
            """
            INSERT INTO password_reset_tokens(user_id, token_hash, expires_at, ip, user_agent)
            VALUES (%s,%s,%s,%s,%s)
            """,
            [user["id"], token_hash, exp, ip, ua],
        )

        origin = getattr(settings, "FRONTEND_ORIGIN", "http://localhost:5173")
        url = f"{origin}/restablecer?t={token}"
        subj = "Recuperación de contraseña"
        txt = (
            f"Hola {user['nombre']},\n\n"
            f"Use este enlace para restablecer su contraseña (válido {TOKEN_TTL_MIN} minutos):\n{url}\n\n"
            "Si no fue usted, ignore este correo."
        )
        html = (
            f"<p>Hola {user['nombre']},</p>"
            f"<p>Use este enlace para restablecer su contraseña (válido {TOKEN_TTL_MIN} minutos):</p>"
            f"<p><a href=\"{url}\">{url}</a></p>"
            "<p>Si no fue usted, ignore este correo.</p>"
        )
        try:
            send_mail(
                subj,
                txt,
                settings.DEFAULT_FROM_EMAIL,
                [email],
                html_message=html,
                fail_silently=True,
            )
        except Exception:
            pass
        return ok_response


class ResetPasswordView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        token = (request.data.get("token") or "").strip()
        password = (request.data.get("password") or "").strip()
        if not token or not password:
            return Response({"detail": "token y password requeridos"}, status=400)

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = q(
            """
            SELECT prt.id, prt.user_id
              FROM password_reset_tokens prt
             WHERE prt.token_hash=%s AND prt.used_at IS NULL AND prt.expires_at>NOW()
            """,
            [token_hash],
            one=True,
        )
        if not row:
            return Response({"detail": "Token inválido o vencido"}, status=400)

        hashed = make_password(password)
        _set_audit_user(request)
        exec_void("UPDATE users SET hash_pw=%s WHERE id=%s", [hashed, row["user_id"]])
        exec_void(
            "UPDATE password_reset_tokens SET used_at=NOW() WHERE id=%s",
            [row["id"]],
        )
        return Response({"ok": True})


class SessionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u = getattr(request, "user", None)
        if not getattr(u, "id", None):
            return Response({"detail": "no autenticado"}, status=401)
        permissions_map = resolve_effective_permissions(
            user_id=getattr(u, "id", None),
            role=getattr(u, "rol", ""),
        )
        return Response(
            {
                "user": {
                    "id": u.id,
                    "nombre": getattr(u, "nombre", ""),
                    "rol": _normalize_role(getattr(u, "rol", "")),
                    "email": getattr(u, "email", ""),
                    "permissions": permissions_map,
                },
                "features": {},
            }
        )


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        resp = Response({"ok": True, "time": timezone.now().isoformat()})
        try:
            cookie_name = getattr(settings, "AUTH_COOKIE_NAME", "auth_token")
            cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", None) or None
            cookie_samesite = getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax")
            resp.delete_cookie(cookie_name, domain=cookie_domain, samesite=cookie_samesite)
        except Exception:
            pass
        return resp
