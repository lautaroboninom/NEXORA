from django.db import connection
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.template.loader import get_template
from urllib import request as urlrequest
import json
import datetime as dt

import unicodedata
import os
from decimal import Decimal, ROUND_HALF_UP

from ..constants import DEFAULT_LOCATION_NAMES, LOCATION_NAME_REMAPS
from ..permission_policy import resolve_permission_code_for_request
from ..permissions import permissions_v2_enabled, require_any_permission, require_permission


# ---- Auth/login throttling constants ----
TOKEN_TTL_MIN = 30
COOLDOWN_MIN = 1
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "5"))
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))
LOGIN_LOCKOUT_SECONDS = max(1, LOGIN_LOCKOUT_MINUTES) * 60


def _login_rate_key(email: str, ip: str) -> str:
    email = (email or "").strip().lower()
    ip = ip or ""
    return f"login-attempt:{email}:{ip}"


def _is_login_locked(key: str) -> bool:
    try:
        attempts = cache.get(key, 0) or 0
        return attempts >= getattr(settings, "LOGIN_MAX_ATTEMPTS", LOGIN_MAX_ATTEMPTS)
    except Exception:
        return False


def _register_login_failure(key: str) -> None:
    try:
        attempts = (cache.get(key, 0) or 0) + 1
        cache.set(key, attempts, getattr(settings, "LOGIN_LOCKOUT_SECONDS", LOGIN_LOCKOUT_SECONDS))
    except Exception:
        pass


def _reset_login_failure(key: str) -> None:
    try:
        cache.delete(key)
    except Exception:
        pass


def _validate_password_strength(password: str) -> None:
    if len(password or "") < getattr(settings, "PASSWORD_MIN_LENGTH", PASSWORD_MIN_LENGTH):
        raise ValueError("weak password")
    classes = 0
    if any(c.islower() for c in password):
        classes += 1
    if any(c.isupper() for c in password):
        classes += 1
    if any(c.isdigit() for c in password):
        classes += 1
    if any(not c.isalnum() for c in password):
        classes += 1
    if classes < 3:
        raise ValueError("weak password")


# ---- DB helpers ----

TWO = Decimal("0.01")


def money(x):
    if x is None:
        return Decimal("0.00")
    if isinstance(x, Decimal):
        return x.quantize(TWO, rounding=ROUND_HALF_UP)
    return Decimal(str(x)).quantize(TWO, rounding=ROUND_HALF_UP)


def _extract_insert_table(sql: str) -> str | None:
    try:
        s = (sql or "").strip()
        lower = s.lower()
        if not lower.startswith("insert into"):
            return None
        # naive parse: INSERT INTO <table>[ ( ...] or space
        after = lower[len("insert into"):].lstrip()
        # table name until first space or opening parenthesis
        table = ""
        for ch in after:
            if ch.isspace() or ch == "(":
                break
            table += ch
        # allow schema-qualified form schema.table
        if table and all(c.isalnum() or c in ("_", ".") for c in table):
            return table
    except Exception:
        return None
    return None


def _reset_pk_sequence(table_name: str) -> None:
    if not table_name:
        return
    if connection.vendor != "postgresql":
        return
    # Safety: only allow simple [schema.]name form
    if not all(c.isalnum() or c in ("_", ".") for c in table_name):
        return
    with connection.cursor() as cur:
        # set last_value to MAX(id) so nextval yields MAX+1
        try:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM {table_name}), 1))",
                [table_name],
            )
        except Exception:
            # ignore – best effort
            pass


def repair_pk_sequence(table_name: str) -> None:
    """Best-effort: align a table's id sequence to MAX(id).

    Only runs on PostgreSQL. Silently no-ops on errors.
    """
    if not table_name:
        return
    if connection.vendor != "postgresql":
        return
    # Safety: allow only simple [schema.]name form
    if not all(c.isalnum() or c in ("_", ".") for c in table_name):
        return
    with connection.cursor() as cur:
        try:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM {table_name}), 1))",
                [table_name],
            )
        except Exception:
            # ignore – best effort
            pass


def _maybe_fix_seq_and_retry(cur, sql, params, err) -> bool:
    # Only for Postgres duplicate key on primary key id during INSERT
    try:
        if connection.vendor != "postgresql":
            return False
        msg = str(err) if err is not None else ""
        low = msg.lower()
        if (
            "duplicate key value violates unique constraint" in low
            and "pkey" in low
            and "key (id)=" in low
        ):
            table = _extract_insert_table(sql)
            if table:
                _reset_pk_sequence(table)
                try:
                    # Ensure we clear aborted transaction state before retry
                    try:
                        from django.db import transaction as _txn
                        _txn.set_rollback(True)
                    except Exception:
                        pass
                    # Retry using the SAME cursor so callers like exec_returning
                    # can fetch() from it (e.g., INSERT ... RETURNING id)
                    cur.execute(sql, params or [])
                except Exception:
                    return False
                return True
        return False
    except Exception:
        return False


def exec_void(sql, params=None):
    with connection.cursor() as cur:
        try:
            cur.execute(sql, params or [])
        except Exception as e:
            # Clear transaction state so subsequent queries work
            try:
                from django.db import transaction as _txn
                _txn.set_rollback(True)
            except Exception:
                pass
            if _extract_insert_table(sql) and _maybe_fix_seq_and_retry(cur, sql, params, e):
                return
            raise


def _set_audit_user(request):
    if connection.vendor == "postgresql":
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


_SUSPECT_UTF8 = ("\ufffd", "Ã", "Â")  # U+FFFD replacement char and common mojibake lead bytes


def _fix_text_value(val):
    """Best-effort decode/fix for mojibake coming from legacy DB enum/latin1.

    - If bytes, try utf-8 then latin1, finally utf-8 with ignore.
    - If str contains obvious mojibake (� / Ã / Â), attempt latin1->utf8 roundtrip.
    """
    if isinstance(val, bytes):
        for enc in ("utf-8", "latin1"):
            try:
                return val.decode(enc)
            except Exception:
                continue
        try:
            return val.decode("utf-8", errors="ignore")
        except Exception:
            return str(val)
    if isinstance(val, str):
        s = val
        if any(ch in s for ch in _SUSPECT_UTF8):
            try:
                fixed = s.encode("latin1").decode("utf-8")
                return fixed
            except Exception:
                return s
        return s
    return val


def _fix_row(row):
    if isinstance(row, dict):
        return {k: _fix_text_value(v) for k, v in row.items()}
    return row


def q(sql, params=None, one=False):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        if cur.description:
            cols = [c[0] for c in cur.description]
            rows = [_fix_row(dict(zip(cols, r))) for r in cur.fetchall()]
            if one:
                return rows[0] if rows else None
            return rows
        return None


def exec_returning(sql, params=None):
    with connection.cursor() as cur:
        try:
            cur.execute(sql, params or [])
        except Exception as e:
            # Clear transaction state so subsequent queries work
            try:
                from django.db import transaction as _txn
                _txn.set_rollback(True)
            except Exception:
                pass
            if _extract_insert_table(sql) and _maybe_fix_seq_and_retry(cur, sql, params, e):
                pass
            else:
                raise
        row = cur.fetchone()
        return row[0] if row else None


def last_insert_id():
    if connection.vendor == "postgresql":
        row = q("SELECT LASTVAL() AS id", one=True)
    else:
        row = q("SELECT LAST_INSERT_ID() AS id", one=True)
    return row and row.get("id")


def _norm_txt(val: str) -> str:
    try:
        s = "" if val is None else str(val)
        s = s.strip().lower()
        s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
        return s
    except Exception:
        return (val or "").strip().lower()


def _get_motivo_enum_values() -> list:
    try:
        if connection.vendor == "postgresql":
            rows = q(
                """
                SELECT e.enumlabel AS v
                  FROM pg_type t
                  JOIN pg_enum e ON e.enumtypid = t.oid
                 WHERE t.typname = 'motivo_ingreso'
                """
            ) or []
            vals = [(_fix_text_value(r.get("v"))) for r in rows]
        else:
            row = q(
                """
                SELECT COLUMN_TYPE AS ct
                  FROM information_schema.columns
                 WHERE table_schema = DATABASE()
                   AND table_name = 'ingresos'
                   AND column_name = 'motivo'
                """,
                one=True,
            )
            vals = []
            if row and (row.get("ct") or "").lower().startswith("enum("):
                ct = row["ct"][5:-1]
                for p in ct.split(","):
                    v = p.strip().strip("'")
                    if v:
                        vals.append(_fix_text_value(v))
        vals = [v for v in (vals or []) if v]
        if vals:
            return vals
    except Exception:
        pass
    return [
        "urgente control",
        "reparacion",
        "service preventivo",
        "baja alquiler",
        "reparacion alquiler",
        "devolucion demo",
        "cotizacion de equipo",
        "otros",
    ]


def _get_motivo_enum_values_raw() -> list:
    try:
        if connection.vendor == "postgresql":
            rows = q(
                """
                SELECT e.enumlabel AS v
                  FROM pg_type t
                  JOIN pg_enum e ON e.enumtypid = t.oid
                 WHERE t.typname = 'motivo_ingreso'
                """
            ) or []
            return [r.get("v") for r in rows if r.get("v")]
        else:
            row = q(
                """
                SELECT COLUMN_TYPE AS ct
                  FROM information_schema.columns
                 WHERE table_schema = DATABASE()
                   AND table_name = 'ingresos'
                   AND column_name = 'motivo'
                """,
                one=True,
            )
            vals = []
            if row and (row.get("ct") or "").lower().startswith("enum("):
                ct = row["ct"][5:-1]
                for p in ct.split(","):
                    v = p.strip().strip("'")
                    if v:
                        vals.append(v)
            return vals
    except Exception:
        return []


def _map_motivo_to_db_label(user_value: str):
    user_value = (user_value or "").strip()
    if not user_value:
        return None
    raw_vals = _get_motivo_enum_values_raw()
    if not raw_vals:
        return None
    by_key = {}
    for raw in raw_vals:
        disp = _fix_text_value(raw)
        for cand in (raw, disp):
            k1 = (cand or "").strip().lower()
            k2 = _norm_txt(cand)
            if k1 and k1 not in by_key:
                by_key[k1] = raw
            if k2 and k2 not in by_key:
                by_key[k2] = raw
    k_user1 = user_value.strip().lower()
    k_user2 = _norm_txt(user_value)
    return by_key.get(k_user1) or by_key.get(k_user2)


def _motivo_is_cotizacion_equipo(value: str) -> bool:
    key = _norm_txt(value or "")
    target = _norm_txt("cotización de equipo")
    return key == target


def _fetchall_dicts(cur):
    cols = [c[0] for c in cur.description]
    return [_fix_row(dict(zip(cols, row))) for row in cur.fetchall()]


# ---- Roles/auth helpers ----

def _rol(request):
    return getattr(request.user, "rol", None) or (getattr(request.user, "data", {}) or {}).get("rol")


def _require_mapped_permission(request):
    if not permissions_v2_enabled():
        return False
    code = resolve_permission_code_for_request(request)
    if not code:
        return False
    if isinstance(code, (list, tuple, set)):
        require_any_permission(request, code)
        return True
    require_permission(request, code)
    return True


def require_roles(request, roles):
    if _require_mapped_permission(request):
        return
    r = _rol(request)
    expanded = set(roles)
    if "jefe" in expanded:
        expanded.add("jefe_veedor")
    if r not in expanded:
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("No autorizado")


def require_roles_strict(request, roles):
    if _require_mapped_permission(request):
        return
    r = _rol(request)
    if r not in set(roles):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("No autorizado")


def require_jefe(request):
    if _rol(request) not in ("jefe", "jefe_veedor"):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("Solo Jefe")


def _is(role, request):
    return getattr(getattr(request, "user", None), "rol", None) == role


def _in(roles, request):
    return getattr(getattr(request, "user", None), "rol", None) in roles


def ensure_default_locations():
    with connection.cursor() as cur:
        for alias, target in LOCATION_NAME_REMAPS.items():
            cur.execute(
                "UPDATE locations SET nombre=%s WHERE LOWER(nombre)=LOWER(%s)",
                [target, alias],
            )
        for name in DEFAULT_LOCATION_NAMES:
            cur.execute(
                """
                INSERT INTO locations (nombre)
                SELECT %s
                FROM (SELECT 1) AS _seed
                WHERE NOT EXISTS (
                    SELECT 1 FROM locations WHERE LOWER(nombre)=LOWER(%s)
                )
                """,
                [name, name],
            )


def _frontend_url(request, path: str) -> str:
    try:
        base = (
            getattr(settings, 'PUBLIC_WEB_URL', '')
            or getattr(settings, 'FRONTEND_ORIGIN', '')
        ).strip()
        if base:
            return f"{base.rstrip('/')}{path}"
        return request.build_absolute_uri(path)
    except Exception:
        return path


# ---- Email footer helpers ----
EMAIL_LEGAL_FOOTER = getattr(settings, "EMAIL_LEGAL_FOOTER", (
    "La información de este correo es confidencial y concierne unicamente a la persona a la que está dirigida. "
    "Si este mensaje no está dirigido a usted, por favor ignórelo."
))


def _load_email_footer_text() -> str:
    try:
        tpl = get_template("email/_legal_footer.txt")
        text = (tpl.render({}) or "").strip()
        if text:
            return text
    except Exception:
        pass
    return EMAIL_LEGAL_FOOTER


def _email_append_footer_text(txt: str) -> str:
    try:
        base = (txt or "").rstrip()
        footer = _load_email_footer_text()
        if not footer:
            return base
        return f"{base}\n\n{footer}"
    except Exception:
        return txt


def _email_append_footer_html(html: str) -> str:
    try:
        try:
            tpl = get_template("email/_legal_footer.html")
            footer_html = (tpl.render({}) or "").strip()
        except Exception:
            footer_html = ""
        if not footer_html:
            text = _load_email_footer_text()
            if text:
                style = "font-size:10px;color:#6b7280;text-align:justify;line-height:1.3;margin-top:12px;"
                footer_html = f"<hr style=\"border:none;border-top:1px solid #e5e7eb;margin:12px 0;\"/><div style=\"{style}\">{text}</div>"
        return (html or "") + (footer_html or "")
    except Exception:
        return html


def os_label(_id: int) -> str:
    try:
        return f"{str(int(_id)).zfill(5)}"
    except Exception:
        return f"{str(_id)}"


# ---- Business-hours and metrics helpers (kept for compat) ----
WORKDAY_START_HOUR = int(os.getenv("WORKDAY_START_HOUR", "9"))
WORKDAY_END_HOUR = int(os.getenv("WORKDAY_END_HOUR", "17"))
WORKDAYS = set(int(x) for x in os.getenv("WORKDAYS", "0,1,2,3,4").split(",") if x != "")


def _tz_aware(dtobj: dt.datetime):
    if not isinstance(dtobj, dt.datetime):
        return None
    if timezone.is_naive(dtobj):
        return timezone.make_aware(dtobj, timezone.get_current_timezone())
    return dtobj


def _clamp_to_work_window_forward(ts: dt.datetime) -> dt.datetime:
    ts = _tz_aware(ts)
    if ts is None:
        return ts
    d = ts
    while d.weekday() not in WORKDAYS:
        d = (d + dt.timedelta(days=1)).replace(hour=WORKDAY_START_HOUR, minute=0, second=0, microsecond=0)
    start = d.replace(hour=WORKDAY_START_HOUR, minute=0, second=0, microsecond=0)
    end = d.replace(hour=WORKDAY_END_HOUR, minute=0, second=0, microsecond=0)
    if d < start:
        return start
    if d > end:
        d = d + dt.timedelta(days=1)
        while d.weekday() not in WORKDAYS:
            d = d + dt.timedelta(days=1)
        return d.replace(hour=WORKDAY_START_HOUR, minute=0, second=0, microsecond=0)
    return d


def _clamp_to_work_window_backward(ts: dt.datetime) -> dt.datetime:
    ts = _tz_aware(ts)
    if ts is None:
        return ts
    d = ts
    while d.weekday() not in WORKDAYS:
        d = (d - dt.timedelta(days=1)).replace(hour=WORKDAY_END_HOUR, minute=0, second=0, microsecond=0)
    start = d.replace(hour=WORKDAY_START_HOUR, minute=0, second=0, microsecond=0)
    end = d.replace(hour=WORKDAY_END_HOUR, minute=0, second=0, microsecond=0)
    if d > end:
        return end
    if d < start:
        d = d - dt.timedelta(days=1)
        while d.weekday() not in WORKDAYS:
            d = d - dt.timedelta(days=1)
        return d.replace(hour=WORKDAY_END_HOUR, minute=0, second=0, microsecond=0)
    return d


def _holidays_country():
    try:
        return getattr(settings, 'HOLIDAYS_COUNTRY', None) or os.getenv('HOLIDAYS_COUNTRY', 'AR')
    except Exception:
        return 'AR'


def _parse_extra_holidays_env():
    s = os.getenv('HOLIDAYS_EXTRA_DATES', '')
    out = set()
    if not s:
        return out
    for part in s.split(','):
        part = (part or '').strip()
        if not part:
            continue
        date_s = part.split(':', 1)[0].strip()
        try:
            y, m, d = [int(x) for x in date_s.split('-')]
            out.add(dt.date(y, m, d))
        except Exception:
            continue
    return out


def _fetch_nager_year(country: str, year: int):
    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
    try:
        with urlrequest.urlopen(url, timeout=8) as resp:
            if getattr(resp, 'status', 200) != 200:
                return []
            data = json.loads(resp.read().decode('utf-8'))
            dates = []
            for it in data or []:
                ds = (it.get('date') or '').strip()
                try:
                    y, m, d = [int(x) for x in ds.split('-')]
                    dates.append(dt.date(y, m, d))
                except Exception:
                    continue
            return dates
    except Exception:
        return []


def _get_year_holidays(country: str, year: int):
    ck = f"holidays:year:{country}:{year}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    dates = set(_fetch_nager_year(country, year))
    try:
        if getattr(connection, 'vendor', '') == 'postgresql':
            rows = q("SELECT fecha FROM feriados WHERE EXTRACT(YEAR FROM fecha) = %s", [year]) or []
        else:
            rows = q("SELECT fecha FROM feriados WHERE YEAR(fecha)=%s", [year]) or []
        dates |= {r.get('fecha') for r in rows if r.get('fecha')}
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
    dates |= _parse_extra_holidays_env()
    cache.set(ck, dates, 12 * 3600)
    return dates


def _holidays_between(d1: dt.date, d2: dt.date):
    try:
        country = _holidays_country()
        k = f"holidays:{country}:{d1.isoformat()}:{d2.isoformat()}"
        cached = cache.get(k)
        if cached is not None:
            return cached
        cur = d1
        out = set()
        seen_years = set()
        while cur <= d2:
            if cur.year not in seen_years:
                out |= _get_year_holidays(country, cur.year)
                seen_years.add(cur.year)
            cur += dt.timedelta(days=1)
        out = {d for d in out if d1 <= d <= d2}
        cache.set(k, out, 6 * 3600)
        return out
    except Exception:
        return set()


def business_minutes_between(start, end, holidays=None):
    if not start or not end:
        return 0
    s = _clamp_to_work_window_forward(_tz_aware(start))
    e = _clamp_to_work_window_backward(_tz_aware(end))
    if not s or not e or e <= s:
        return 0
    total = 0
    day = s.date()
    end_day = e.date()
    if holidays is None:
        holidays = _holidays_between(day, end_day)
    while day <= end_day:
        if (day.weekday() in WORKDAYS) and (day not in (holidays or set())):
            ws = dt.datetime.combine(day, dt.time(hour=WORKDAY_START_HOUR, tzinfo=s.tzinfo))
            we = dt.datetime.combine(day, dt.time(hour=WORKDAY_END_HOUR, tzinfo=s.tzinfo))
            seg_ini = max(ws, s)
            seg_fin = min(we, e)
            if seg_fin > seg_ini:
                total += int((seg_fin - seg_ini).total_seconds() // 60)
        day = day + dt.timedelta(days=1)
    return max(0, total)


__all__ = [
    # constants
    'TOKEN_TTL_MIN','COOLDOWN_MIN','LOGIN_MAX_ATTEMPTS','LOGIN_LOCKOUT_MINUTES','LOGIN_LOCKOUT_SECONDS','PASSWORD_MIN_LENGTH',
    # auth
    '_login_rate_key','_is_login_locked','_register_login_failure','_reset_login_failure','_validate_password_strength',
    # db
    'q','exec_void','exec_returning','last_insert_id','_set_audit_user',
    # text/encoding
    '_fix_text_value','_fix_row','_norm_txt',
    # motivo
    '_get_motivo_enum_values','_get_motivo_enum_values_raw','_map_motivo_to_db_label','_motivo_is_cotizacion_equipo','_fetchall_dicts',
    # roles
    'require_roles','require_roles_strict','require_jefe','_rol','_is','_in',
    # misc
    'ensure_default_locations','money','_frontend_url',
    # email footer helpers
    '_load_email_footer_text','_email_append_footer_text','_email_append_footer_html',
    # misc labels
    'os_label',
]
