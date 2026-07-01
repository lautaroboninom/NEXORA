from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import connection
from django.utils import timezone


BEJERMAN_CREDENTIALS_REQUIRED = (
    "Debe cargar sus credenciales de Bejerman para operar con el SDK desde NEXORA."
)

TECHNICAL_ROLES = {"tecnico", "jefe", "jefe_veedor"}
ADMINISTRATIVE_ROLES = {"admin", "supervisor", "ventas", "recepcion", "cobranzas"}


class BejermanUserCredentialsError(RuntimeError):
    pass


class BejermanUserCredentialsRequired(BejermanUserCredentialsError):
    pass


class BejermanUserCredentialsInvalid(BejermanUserCredentialsError):
    pass


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalize_role(value: Any) -> str:
    return _clean(value).lower().replace(" ", "_").replace("-", "_")


def bejerman_workstation_for_role(role: Any) -> str:
    role_key = _normalize_role(role)
    if role_key in ADMINISTRATIVE_ROLES:
        return (
            _clean(getattr(settings, "BEJERMAN_ADMIN_WORKSTATION", ""))
            or _clean(getattr(settings, "BEJERMAN_WORKSTATION", ""))
            or _clean(getattr(settings, "BEJERMAN_SERVICE_WORKSTATION", ""))
            or "ADMV"
        )
    if role_key in TECHNICAL_ROLES:
        return (
            _clean(getattr(settings, "BEJERMAN_SERVICE_WORKSTATION", ""))
            or _clean(getattr(settings, "BEJERMAN_WORKSTATION", ""))
            or "STEC"
        )
    return _clean(getattr(settings, "BEJERMAN_WORKSTATION", "")) or "STEC"


def resolve_user_bejerman_workstation(user_id: int | None) -> str:
    if not user_id:
        return bejerman_workstation_for_role("")
    with connection.cursor() as cur:
        cur.execute("SELECT rol FROM users WHERE id = %s LIMIT 1", [int(user_id)])
        row = cur.fetchone()
    return bejerman_workstation_for_role(row[0] if row else "")


def _fernet_key() -> bytes:
    configured = _clean(getattr(settings, "BEJERMAN_CREDENTIALS_KEY", ""))
    if configured:
        key = configured.encode("utf-8")
        Fernet(key)
        return key
    secret = _clean(getattr(settings, "BEJERMAN_CREDENTIALS_SECRET", "")) or _clean(
        getattr(settings, "SECRET_KEY", "")
    )
    if not secret:
        raise BejermanUserCredentialsError("Falta clave de cifrado para credenciales Bejerman")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_fernet_key())


def encrypt_bejerman_password(raw_password: str) -> str:
    password = _clean(raw_password)
    if not password:
        raise BejermanUserCredentialsInvalid("Clave Bejerman requerida")
    return _fernet().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_bejerman_password(encrypted_password: str) -> str:
    raw = _clean(encrypted_password)
    if not raw:
        raise BejermanUserCredentialsRequired(BEJERMAN_CREDENTIALS_REQUIRED)
    try:
        return _fernet().decrypt(raw.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise BejermanUserCredentialsInvalid(
            "No se pudieron leer las credenciales Bejerman guardadas"
        ) from exc


def _table_exists() -> bool:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = 'user_bejerman_credentials'
                 LIMIT 1
                """
            )
            return bool(cur.fetchone())
    except Exception:
        return False


def _fetch_row(user_id: int | None) -> dict[str, Any] | None:
    if not user_id or not _table_exists():
        return None
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT user_id, bejerman_username, encrypted_password, is_valid,
                   validated_at, last_error, created_at, updated_at
              FROM user_bejerman_credentials
             WHERE user_id = %s
             LIMIT 1
            """,
            [int(user_id)],
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [col[0] for col in cur.description]
        return dict(zip(cols, row))


def mask_bejerman_username(username: str) -> str:
    value = _clean(username)
    if len(value) <= 2:
        return value
    return f"{value[:1]}{'*' * max(len(value) - 2, 1)}{value[-1:]}"


def user_bejerman_credentials_status(user_id: int | None) -> dict[str, Any]:
    row = _fetch_row(user_id)
    configured = bool(row and row.get("bejerman_username") and row.get("encrypted_password"))
    valid = bool(configured and row.get("is_valid"))
    return {
        "configured": configured,
        "valid": valid,
        "required": not valid,
        "username": mask_bejerman_username(row.get("bejerman_username") if row else ""),
        "lastError": row.get("last_error") if row else "",
        "validatedAt": row.get("validated_at").isoformat() if row and row.get("validated_at") else None,
        "updatedAt": row.get("updated_at").isoformat() if row and row.get("updated_at") else None,
    }


def has_valid_bejerman_credentials(user_id: int | None) -> bool:
    return bool(user_bejerman_credentials_status(user_id).get("valid"))


def get_user_bejerman_credentials(user_id: int | None) -> tuple[str, str]:
    row = _fetch_row(user_id)
    if not row or not row.get("is_valid"):
        raise BejermanUserCredentialsRequired(BEJERMAN_CREDENTIALS_REQUIRED)
    username = _clean(row.get("bejerman_username"))
    password = decrypt_bejerman_password(_clean(row.get("encrypted_password")))
    if not username or not password:
        raise BejermanUserCredentialsRequired(BEJERMAN_CREDENTIALS_REQUIRED)
    return username, password


def save_user_bejerman_credentials(user_id: int, username: str, password: str) -> dict[str, Any]:
    clean_user = int(user_id or 0)
    clean_username = _clean(username).upper()
    if clean_user <= 0:
        raise BejermanUserCredentialsInvalid("Usuario NEXORA requerido")
    if not clean_username:
        raise BejermanUserCredentialsInvalid("Usuario Bejerman requerido")
    encrypted = encrypt_bejerman_password(password)
    now = timezone.now()
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_bejerman_credentials (
              user_id, bejerman_username, encrypted_password, is_valid,
              validated_at, last_error, created_at, updated_at
            )
            VALUES (%s, %s, %s, TRUE, %s, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
               SET bejerman_username = EXCLUDED.bejerman_username,
                   encrypted_password = EXCLUDED.encrypted_password,
                   is_valid = TRUE,
                   validated_at = EXCLUDED.validated_at,
                   last_error = NULL,
                   updated_at = CURRENT_TIMESTAMP
            """,
            [clean_user, clean_username, encrypted, now],
        )
    return user_bejerman_credentials_status(clean_user)


def mark_user_bejerman_credentials_invalid(user_id: int | None, error: str) -> None:
    if not user_id or not _table_exists():
        return
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE user_bejerman_credentials
               SET is_valid = FALSE,
                   last_error = %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE user_id = %s
            """,
            [_clean(error)[:500], int(user_id)],
        )
