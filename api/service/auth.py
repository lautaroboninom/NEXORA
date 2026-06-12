# service/auth.py
import os, jwt
from datetime import datetime, timedelta, timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework import exceptions
from django.contrib.auth.hashers import check_password, make_password
from django.conf import settings
from django.db import connection
from .models import User

# Use la misma clave en todos los contenedores; para dev vale el default.
JWT_SECRET = os.getenv("DJANGO_SECRET_KEY", "change-me")
JWT_ALG = "HS256"
JWT_TTL_MIN = 60 * 8  # 8 horas
# Overrides por entorno (si están presentes)
JWT_SECRET = os.getenv("JWT_SECRET") or JWT_SECRET
JWT_TTL_MIN = int(os.getenv("JWT_TTL_MIN", str(JWT_TTL_MIN)))


def _set_audit_user(request):
    """Set app.* audit vars without importing service.views (avoids circular import on startup)."""
    if connection.vendor != "postgresql":
        return
    uid = getattr(request, "user_id", None)
    if uid is None:
        uid = getattr(getattr(request, "user_obj", None), "id", None)
    uid = "" if uid is None else str(uid)
    role = getattr(request, "user_role", None)
    if role is None:
        role = getattr(getattr(request, "user_obj", None), "rol", "")
    with connection.cursor() as cur:
        cur.execute("SET app.user_id = %s;", [uid])
        cur.execute("SET app.user_role = %s;", [role])

def make_hash(raw: str) -> str:
    return make_password(raw)

def verify_hash(raw: str, hashed: str) -> bool:
    try:
        return check_password(raw, hashed)
    except Exception:
        return False

def issue_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        # >>> Claves CONSISTENTES con todo tu código <<<
        "uid": user.id,
        "role": user.rol,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_TTL_MIN)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

class JWTAuthentication(BaseAuthentication):
    """Lee Authorization: Bearer <token>, valida y puebla request.user + helpers."""
    keyword = "Bearer"

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith(self.keyword + " "):
            token = auth_header.split(" ", 1)[1].strip()
        else:
            cookie_name = getattr(settings, "AUTH_COOKIE_NAME", "auth_token")
            cookie_token = request.COOKIES.get(cookie_name)
            if cookie_token:
                token = cookie_token.strip()
        if not token:
            return None  # permite endpoints AllowAny y otros autenticadores si hubiera

        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Token expirado")
        except jwt.PyJWTError:
            raise exceptions.AuthenticationFailed("Token inválido")

        uid = payload.get("uid")
        role = payload.get("role")
        if not uid or not role:
            raise exceptions.AuthenticationFailed("Payload inválido")

        try:
            user = User.objects.get(id=uid, activo=True)
        except User.DoesNotExist:
            raise exceptions.AuthenticationFailed("Usuario no encontrado")
        
        
        # Helpers que usás en queries/SET LOCAL
        request.user_id = user.id
        request.user_role = role
        request.user_obj = user
        _set_audit_user(request)

        # DRF verá a 'user' como autenticado
        return (DRFUser(user.id, user.nombre, user.rol), None)


class DRFUser:
    """Usuario mínimo para DRF."""
    __slots__ = ("id", "nombre", "rol")
    def __init__(self, _id, nombre, rol):
        self.id = _id
        self.nombre = nombre
        self.rol = rol

    @property
    def is_authenticated(self): return True
    @property
    def is_anonymous(self):     return False
    @property
    def is_staff(self):         return self.rol == "jefe"  # o el criterio que uses
    @property
    def is_superuser(self):     return self.rol == "jefe"  # opcional
