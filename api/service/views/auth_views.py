import datetime as dt
import hashlib
import logging
import secrets

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth import issue_token, verify_hash, JWT_TTL_MIN
from ..bejerman_companies import list_ingress_companies
from ..bejerman_sdk import BejermanSDKClient, BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable
from ..bejerman_user_credentials import (
    BejermanUserCredentialsError,
    BejermanUserCredentialsInvalid,
    resolve_user_bejerman_workstation,
    save_user_bejerman_credentials,
    user_bejerman_credentials_status,
)
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

logger = logging.getLogger(__name__)
SELLER_CODE_ROLES = {"ventas"}


@api_view(["GET"])  # público
@permission_classes([AllowAny])
def ping(request):
    return Response({"ok": True})


def _normalize_role(s: str) -> str:
    s = (s or "").strip().lower().replace(" ", "_").replace("-", "_")
    return s


def _normalize_seller_code(value: str) -> str:
    return (value or "").strip().upper()


def _bejerman_seller_code_status(user_id: int | None, role: str | None) -> dict:
    role_key = _normalize_role(role or "")
    eligible = role_key in SELLER_CODE_ROLES
    status = {
        "code": "",
        "eligible": eligible,
        "confirmed": False,
        "required": False,
    }
    if not user_id:
        return status
    try:
        row = q(
            """
            SELECT bejerman_seller_code, bejerman_seller_code_confirmed_at
              FROM users
             WHERE id=%s
             LIMIT 1
            """,
            [user_id],
            one=True,
        ) or {}
    except Exception:
        return status
    status["code"] = _normalize_seller_code(row.get("bejerman_seller_code"))
    status["confirmed"] = bool(row.get("bejerman_seller_code_confirmed_at"))
    status["required"] = bool(eligible and not status["confirmed"])
    return status


def _auth_user_payload(user, permissions_map=None) -> tuple[dict, dict]:
    permissions_map = permissions_map if permissions_map is not None else resolve_effective_permissions(
        user_id=getattr(user, "id", None),
        role=getattr(user, "rol", ""),
    )
    bejerman_credentials = user_bejerman_credentials_status(getattr(user, "id", None))
    bejerman_seller_code = _bejerman_seller_code_status(
        getattr(user, "id", None),
        getattr(user, "rol", ""),
    )
    return (
        {
            "id": user.id,
            "nombre": getattr(user, "nombre", ""),
            "rol": _normalize_role(getattr(user, "rol", "")),
            "email": getattr(user, "email", ""),
            "permissions": permissions_map,
            "bejermanCredentials": bejerman_credentials,
            "bejermanSellerCode": bejerman_seller_code,
        },
        bejerman_credentials,
    )


def _bejerman_credentials_error_message(username: str, exc: Exception) -> str:
    raw = str(exc or "").strip()
    normalized = raw.lower().replace(" ", "")
    if raw.lower().startswith("bejerman rechaz") and (
        "empresas validadas:" in raw.lower() or "puesto bejerman usado:" in raw.lower()
    ):
        return raw
    if "puesto bejerman usado:" in raw.lower() or "empresas validadas:" in raw.lower():
        return f"Bejerman rechazó el usuario {username}. {raw} La clave no fue guardada."
    if "cuentadeshabilitada" in normalized:
        return (
            f"Bejerman rechazó el usuario {username}: la cuenta está deshabilitada en Bejerman. "
            "NEXORA no puede habilitarla; solicitá la activación de esa cuenta en Bejerman "
            "o ingresá otro usuario Bejerman habilitado. La clave no fue guardada."
        )
    if "credential" in normalized or "contrase" in normalized or "clave" in normalized or "login" in normalized:
        return (
            f"Bejerman rechazó el usuario {username}. Verificá usuario y clave Bejerman. "
            "La clave no fue guardada."
        )
    return raw or "Bejerman no aceptó esas credenciales. La clave no fue guardada."


def _credential_validation_companies():
    companies = [company for company in list_ingress_companies() if not company.is_test]
    seen = set()
    ordered = []
    for company in companies:
        code = (company.bejerman_company or company.key or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        ordered.append(company)
    return ordered


def _validate_bejerman_credentials(username: str, password: str, user_id: int | None = None) -> None:
    companies = _credential_validation_companies()
    workstation = resolve_user_bejerman_workstation(user_id)
    if not companies:
        BejermanSDKClient(
            bejerman_username=username,
            bejerman_password=password,
            bejerman_workstation=workstation,
        ).register()
        return

    rejected = []
    validated = []
    last_config_error = None
    last_unavailable = None
    for company in companies:
        try:
            BejermanSDKClient(
                company_key=company.key,
                bejerman_username=username,
                bejerman_password=password,
                bejerman_workstation=workstation,
            ).register()
            validated.append(company.label)
        except BejermanSdkResponseError as exc:
            rejected.append((company.label, exc))
        except BejermanSdkConfigError as exc:
            last_config_error = exc
        except BejermanSdkUnavailable as exc:
            last_unavailable = exc

    if rejected:
        failed = ", ".join(label for label, _ in rejected)
        ok = ", ".join(validated) or "-"
        detail = _bejerman_credentials_error_message(username, rejected[-1][1])
        raise BejermanSdkResponseError(
            f"{detail} Puesto Bejerman usado: {workstation}. "
            f"Empresas rechazadas: {failed}. Empresas validadas: {ok}."
        )
    if last_config_error:
        raise last_config_error
    if last_unavailable:
        raise last_unavailable
    if not validated:
        raise BejermanSdkConfigError("No hay empresas Bejerman configuradas para validar credenciales")


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
        user_payload, bejerman_credentials = _auth_user_payload(user, permissions_map)
        resp = Response(
            {
                "token": token,
                "user": user_payload,
                "bejermanCredentialsRequired": bool(bejerman_credentials.get("required")),
                "bejermanSellerCode": user_payload.get("bejermanSellerCode") or {},
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


class BejermanCredentialsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user_id = getattr(getattr(request, "user", None), "id", None)
        return Response(user_bejerman_credentials_status(user_id))

    def post(self, request):
        user_id = getattr(getattr(request, "user", None), "id", None)
        username = (request.data.get("username") or request.data.get("usuario") or "").strip().upper()
        password = (request.data.get("password") or request.data.get("clave") or "").strip()
        if not user_id:
            return Response({"detail": "Usuario no autenticado"}, status=401)
        if not username or not password:
            return Response({"detail": "Usuario y clave Bejerman requeridos"}, status=400)
        try:
            _validate_bejerman_credentials(username, password, int(user_id))
            status = save_user_bejerman_credentials(int(user_id), username, password)
        except BejermanUserCredentialsInvalid as exc:
            return Response({"detail": str(exc)}, status=400)
        except BejermanUserCredentialsError as exc:
            return Response({"detail": str(exc)}, status=503)
        except BejermanSdkConfigError as exc:
            return Response({"detail": str(exc)}, status=503)
        except BejermanSdkResponseError as exc:
            return Response({"detail": _bejerman_credentials_error_message(username, exc)}, status=400)
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
        return Response({"ok": True, **status})


class BejermanSellerCodeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = getattr(request, "user", None)
        return Response(_bejerman_seller_code_status(getattr(user, "id", None), getattr(user, "rol", "")))

    def post(self, request):
        user = getattr(request, "user", None)
        user_id = getattr(user, "id", None)
        if not user_id:
            return Response({"detail": "Usuario no autenticado"}, status=401)
        if _normalize_role(getattr(user, "rol", "")) not in SELLER_CODE_ROLES:
            return Response({"detail": "El código de vendedor solo aplica a usuarios de Ventas."}, status=403)

        data = request.data or {}
        code = _normalize_seller_code(
            data.get("code")
            or data.get("sellerCode")
            or data.get("bejermanSellerCode")
            or data.get("codigo")
        )
        if len(code) > 4:
            return Response({"detail": "El código de vendedor no puede superar 4 caracteres."}, status=400)

        if code:
            duplicate = q(
                """
                SELECT id, nombre, email
                  FROM users
                 WHERE id <> %s
                   AND NULLIF(TRIM(bejerman_seller_code), '') IS NOT NULL
                   AND UPPER(TRIM(bejerman_seller_code)) = %s
                 LIMIT 1
                """,
                [user_id, code],
                one=True,
            )
            if duplicate:
                return Response(
                    {"detail": "Ese código de vendedor ya está asignado a otro usuario."},
                    status=400,
                )

        _set_audit_user(request)
        try:
            exec_void(
                """
                UPDATE users
                   SET bejerman_seller_code = %s,
                       bejerman_seller_code_confirmed_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [code or None, user_id],
            )
        except IntegrityError:
            return Response(
                {"detail": "Ese código de vendedor ya está asignado a otro usuario."},
                status=400,
            )
        return Response({"ok": True, **_bejerman_seller_code_status(user_id, getattr(user, "rol", ""))})


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
            sent = send_mail(
                subj,
                txt,
                settings.DEFAULT_FROM_EMAIL,
                [email],
                html_message=html,
                fail_silently=False,
            )
            if sent < 1:
                logger.warning(
                    "No se pudo enviar el correo de recuperación de contraseña: el backend no aceptó el mensaje.",
                    extra={"user_id": user["id"], "recipient": email},
                )
        except Exception:
            logger.exception(
                "No se pudo enviar el correo de recuperación de contraseña.",
                extra={"user_id": user["id"], "recipient": email},
            )
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
        user_payload, bejerman_credentials = _auth_user_payload(u, permissions_map)
        return Response(
            {
                "user": user_payload,
                "bejermanCredentialsRequired": bool(bejerman_credentials.get("required")),
                "bejermanSellerCode": user_payload.get("bejermanSellerCode") or {},
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
