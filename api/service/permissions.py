"""Permission resolution and per-user override utilities."""

from collections import defaultdict

from django.conf import settings
from django.db import connection
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .permission_catalog import PERMISSION_CODES_SET, get_catalog, get_role_defaults, normalize_role
from .permission_policy import VIEW_PERMISSION_MATRIX


EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"
EFFECT_INHERIT = "inherit"
VALID_EFFECTS = {EFFECT_ALLOW, EFFECT_DENY, EFFECT_INHERIT}


def permissions_v2_enabled():
    return bool(getattr(settings, "PERMISSIONS_V2_ENABLED", True))


def _fetch_overrides(user_id):
    if not user_id:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT permission_code, effect
            FROM user_permission_overrides
            WHERE user_id = %s
            """,
            [user_id],
        )
        rows = cur.fetchall()
    out = {}
    for code, effect in rows:
        c = (code or "").strip()
        e = (effect or "").strip().lower()
        if c in PERMISSION_CODES_SET and e in (EFFECT_ALLOW, EFFECT_DENY):
            out[c] = e
    return out


def resolve_effective_permissions(user_id=None, role=None, overrides=None):
    role_key = normalize_role(role)
    if role_key == "jefe":
        return {item["code"]: True for item in get_catalog()}

    effective = get_role_defaults(role_key)
    src = overrides if overrides is not None else _fetch_overrides(user_id)
    for code, effect in (src or {}).items():
        if code not in effective:
            continue
        if effect == EFFECT_ALLOW:
            effective[code] = True
        elif effect == EFFECT_DENY:
            effective[code] = False
    return effective


def get_request_effective_permissions(request):
    if request is None:
        return {}
    cache_attr = "_effective_permissions_v2"
    cached = getattr(request, cache_attr, None)
    if isinstance(cached, dict):
        return cached
    user = getattr(request, "user", None)
    role = getattr(user, "rol", None)
    uid = getattr(user, "id", None)
    eff = resolve_effective_permissions(user_id=uid, role=role)
    setattr(request, cache_attr, eff)
    return eff


def user_has_permission(request_or_user, permission_code):
    if not permission_code:
        return True
    if permission_code not in PERMISSION_CODES_SET:
        return False

    if hasattr(request_or_user, "user"):
        request = request_or_user
        eff = get_request_effective_permissions(request)
        return bool(eff.get(permission_code, False))

    user = request_or_user
    role = getattr(user, "rol", None)
    uid = getattr(user, "id", None)
    eff = resolve_effective_permissions(user_id=uid, role=role)
    return bool(eff.get(permission_code, False))


def require_permission(request, permission_code, message="No autorizado"):
    if not permissions_v2_enabled():
        return
    if not user_has_permission(request, permission_code):
        raise PermissionDenied(message)


def user_has_any_permission(request_or_user, permission_codes):
    if isinstance(permission_codes, str):
        codes = [permission_codes]
    else:
        codes = list(permission_codes or [])
    if not codes:
        return False
    return any(user_has_permission(request_or_user, code) for code in codes)


def require_any_permission(request, permission_codes, message="No autorizado"):
    if not permissions_v2_enabled():
        return
    if not user_has_any_permission(request, permission_codes):
        raise PermissionDenied(message)


def apply_overrides(user_id, raw_overrides, updated_by=None):
    if not user_id:
        raise ValueError("user_id requerido")

    normalized = defaultdict(lambda: EFFECT_INHERIT)
    for code, effect in (raw_overrides or {}).items():
        c = (code or "").strip()
        e = (effect or "").strip().lower()
        if c not in PERMISSION_CODES_SET:
            # Un código que salió del catálogo y vuelve como "heredar" (el editor manda el
            # mapa completo) no puede tumbar el guardado de todos los demás.
            if e in ("", EFFECT_INHERIT):
                continue
            raise ValueError(f"permission_code invalido: {c}")
        if e not in VALID_EFFECTS:
            raise ValueError(f"effect invalido para {c}: {effect}")
        normalized[c] = e

    with connection.cursor() as cur:
        for code, effect in normalized.items():
            if effect == EFFECT_INHERIT:
                cur.execute(
                    """
                    DELETE FROM user_permission_overrides
                    WHERE user_id=%s AND permission_code=%s
                    """,
                    [user_id, code],
                )
                continue
            cur.execute(
                """
                INSERT INTO user_permission_overrides (user_id, permission_code, effect, updated_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, permission_code) DO UPDATE
                SET effect = EXCLUDED.effect,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                [user_id, code, effect, updated_by],
            )


def reset_overrides(user_id):
    if not user_id:
        return
    with connection.cursor() as cur:
        cur.execute("DELETE FROM user_permission_overrides WHERE user_id=%s", [user_id])


class MappedPermissionGuard(BasePermission):
    message = "No autorizado"

    @staticmethod
    def _extract_codes(required):
        if required is None:
            return []
        if isinstance(required, str):
            return [required]
        if isinstance(required, (list, tuple, set)):
            return [c for c in required if isinstance(c, str) and c]
        return []

    def has_permission(self, request, view):
        if not permissions_v2_enabled():
            return True
        class_name = getattr(getattr(view, "__class__", None), "__name__", "")
        if not class_name:
            return True
        method = (getattr(request, "method", "") or "").upper()
        class_map = VIEW_PERMISSION_MATRIX.get(class_name, {})
        required = class_map.get(method)
        codes = self._extract_codes(required)
        if not codes:
            return True
        if len(codes) == 1:
            return user_has_permission(request, codes[0])
        return user_has_any_permission(request, codes)
