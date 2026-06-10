import datetime as dt
import hashlib
import json
from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import (
    _rol,
    _set_audit_user,
    exec_void,
    q,
    require_roles,
    os_label,
)


VIEW_ROLES = ["jefe", "admin", "jefe_veedor", "tecnico", "recepcion", "cobranzas"]
MANAGE_ROLES = ["jefe", "admin", "jefe_veedor"]
TERMINAL_STATES = ("entregado", "alquilado", "baja", "vendido_pendiente_entrega", "vendido_entregado")
TALLER_LOCATION = "taller"

RULE_UNITS = {"horas", "dias", "dias_habiles", "cantidad", "porcentaje"}
RULE_SEVERITIES = {"info", "warning", "critical"}
OBJECTIVE_SCOPES = {"global", "technician"}
OBJECTIVE_PERIODS = {"daily", "weekly"}
OBJECTIVE_DIRECTIONS = {"gte", "lte"}


def _has_table_column(table_name: str, column_name: str) -> bool:
    try:
        if connection.vendor == "postgresql":
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name=%s
                   AND column_name=%s
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                [table_name, column_name],
                one=True,
            )
            return bool(row)
        row = q(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name=%s
               AND column_name=%s
             LIMIT 1
            """,
            [table_name, column_name],
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _mg_list_select_sql(alias: str = "d") -> str:
    if _has_table_column("devices", "mg_estado"):
        return f"""
                   COALESCE({alias}.mg_estado,'activo') AS mg_estado,
                   (COALESCE({alias}.mg_estado,'activo') = 'inactivo_venta') AS mg_inactivo_venta,
        """
    return """
                   'activo' AS mg_estado,
                   FALSE AS mg_inactivo_venta,
    """

DASHBOARD_ALERTS = {
    "jefe": {
        "wip_critico",
        "sin_tecnico",
        "presupuesto_sin_aprobar",
        "liberado_sin_entregar",
        "derivado_sin_devolucion",
        "preventivo_vencido",
        "preventivo_proximo",
    },
    "tecnico": {"wip_critico", "derivado_sin_devolucion"},
    "recepcion": set(),
    "cobranzas": set(),
    "admin": {
        "liberado_sin_entregar",
        "derivado_sin_devolucion",
        "preventivo_vencido",
        "preventivo_proximo",
    },
}

DASHBOARD_KPIS = {
    "jefe": (
        "en_taller",
        "wip_critico",
        "sin_tecnico",
        "presupuestos_demorados",
        "liberados_en_espera",
        "derivados_en_espera",
        "pedidos_abiertos",
    ),
    "tecnico": ("en_taller", "wip_critico", "derivados_en_espera"),
    "recepcion": (
        "pedidos_pendientes_armado",
        "pedidos_listos_entrega",
        "remitos_pendientes_facturacion",
    ),
    "admin": (
        "liberados_en_espera",
        "derivados_en_espera",
        "preventivos_vencidos",
        "preventivos_proximos",
        "pedidos_pendientes_armado",
        "pedidos_listos_entrega",
    ),
    "cobranzas": (
        "remitos_pendientes_facturacion",
        "pedidos_facturados",
    ),
}

DEFAULT_ALERT_RULES = {
    "presupuesto_sin_aprobar": {
        "rule_key": "presupuesto_sin_aprobar",
        "label": "Presupuesto emitido sin aprobar",
        "description": "Presupuestos emitidos que llevan demasiados días sin aprobación.",
        "threshold_value": Decimal("7"),
        "threshold_unit": "dias",
        "severity": "critical",
        "enabled": True,
        "email_enabled": True,
    },
    "liberado_sin_entregar": {
        "rule_key": "liberado_sin_entregar",
        "label": "Liberado sin entregar",
        "description": "Equipos liberados que siguen en taller.",
        "threshold_value": Decimal("3"),
        "threshold_unit": "dias",
        "severity": "warning",
        "enabled": True,
        "email_enabled": True,
    },
    "derivado_sin_devolucion": {
        "rule_key": "derivado_sin_devolucion",
        "label": "Derivado con espera",
        "description": "Equipos derivados a proveedor externo sin devolución.",
        "threshold_value": Decimal("7"),
        "threshold_unit": "dias",
        "severity": "warning",
        "enabled": True,
        "email_enabled": True,
    },
    "wip_critico": {
        "rule_key": "wip_critico",
        "label": "Trabajo con espera crítica",
        "description": "Equipos en taller con demasiados días desde el ingreso.",
        "threshold_value": Decimal("16"),
        "threshold_unit": "dias",
        "severity": "critical",
        "enabled": True,
        "email_enabled": True,
    },
    "sin_tecnico": {
        "rule_key": "sin_tecnico",
        "label": "Sin técnico asignado",
        "description": "Equipos en taller que todavía no tienen técnico responsable.",
        "threshold_value": Decimal("1"),
        "threshold_unit": "dias",
        "severity": "warning",
        "enabled": True,
        "email_enabled": False,
    },
    "preventivo_vencido": {
        "rule_key": "preventivo_vencido",
        "label": "Preventivo vencido",
        "description": "Planes de mantenimiento preventivo con fecha vencida.",
        "threshold_value": Decimal("0"),
        "threshold_unit": "dias",
        "severity": "critical",
        "enabled": True,
        "email_enabled": True,
    },
    "preventivo_proximo": {
        "rule_key": "preventivo_proximo",
        "label": "Preventivo próximo",
        "description": "Planes de mantenimiento preventivo próximos a vencer.",
        "threshold_value": Decimal("30"),
        "threshold_unit": "dias",
        "severity": "warning",
        "enabled": True,
        "email_enabled": True,
    },
}

METRIC_LABELS = {
    "entregados": "Entregas",
    "reparados": "Reparaciones terminadas",
    "diagnosticados": "Diagnósticos",
    "presupuestos_emitidos": "Presupuestos emitidos",
    "presupuestos_aprobados": "Presupuestos aprobados",
    "wip_max": "WIP máximo",
}


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _as_decimal(value, default=0):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(str(default))


def _current_user_id(request):
    return (
        getattr(getattr(request, "user", None), "id", None)
        or getattr(request, "user_id", None)
    )


def _normalize_role_value(value):
    return (value or "").strip().lower()


def _dashboard_variant(request):
    role = _normalize_role_value(_rol(request))
    if role in {"jefe", "jefe_veedor"}:
        return "jefe"
    if role in {"tecnico", "recepcion", "admin", "cobranzas"}:
        return role
    return "jefe"


def _table_exists(table_name):
    try:
        row = q(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_name=%s
               AND table_schema = ANY(current_schemas(true))
             LIMIT 1
            """,
            [table_name],
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _safe_rows(sql, params=None):
    try:
        return q(sql, params or []) or []
    except Exception:
        try:
            transaction.set_rollback(False)
        except Exception:
            pass
        return []


def _safe_one(sql, params=None):
    try:
        return q(sql, params or [], one=True) or {}
    except Exception:
        try:
            transaction.set_rollback(False)
        except Exception:
            pass
        return {}


def _serialize_rule(row):
    out = dict(row or {})
    out["threshold_value"] = float(_as_decimal(out.get("threshold_value"), 0))
    out["enabled"] = _as_bool(out.get("enabled"), True)
    out["email_enabled"] = _as_bool(out.get("email_enabled"), False)
    return out


def load_alert_rules():
    rules = {key: dict(value) for key, value in DEFAULT_ALERT_RULES.items()}
    if _table_exists("work_alert_rules"):
        rows = _safe_rows(
            """
            SELECT rule_key, label, description, threshold_value, threshold_unit,
                   severity, enabled, email_enabled
              FROM work_alert_rules
             ORDER BY rule_key
            """
        )
        for row in rows:
            key = row.get("rule_key")
            if not key:
                continue
            base = rules.get(key, {"rule_key": key})
            base.update(row)
            rules[key] = base
    ordered = []
    for key in DEFAULT_ALERT_RULES:
        ordered.append(_serialize_rule(rules[key]))
    for key in sorted(k for k in rules if k not in DEFAULT_ALERT_RULES):
        ordered.append(_serialize_rule(rules[key]))
    return ordered


def _rule_map():
    return {row["rule_key"]: row for row in load_alert_rules()}


def _rule_days(rule):
    return int(_as_decimal((rule or {}).get("threshold_value"), 0))


def _scope_filter(request, alias="t", include_unassigned=False):
    role = _rol(request)
    user_id = _current_user_id(request)
    if role == "tecnico" and user_id and not include_unassigned:
        return f" AND {alias}.asignado_a = %s", [user_id]
    return "", []


def _period_bounds(periodo):
    now = timezone.localtime()
    today = now.date()
    if (periodo or "").strip().lower() in {"semana", "weekly", "week"}:
        start_date = today - dt.timedelta(days=today.weekday())
        start = timezone.make_aware(dt.datetime.combine(start_date, dt.time.min))
        end = start + dt.timedelta(days=7)
        return "weekly", start, end
    start = timezone.make_aware(dt.datetime.combine(today, dt.time.min))
    end = start + dt.timedelta(days=1)
    return "daily", start, end


def _percent_for_objective(progress, target, direction):
    progress = float(progress or 0)
    target = float(target or 0)
    if target <= 0:
        return 100 if (progress <= 0 if direction == "lte" else progress > 0) else 0
    if direction == "lte":
        if progress <= target:
            return 100
        return max(0, min(100, round((target / max(progress, 1)) * 100)))
    return max(0, min(100, round((progress / target) * 100)))


def _objective_status(progress, target, direction):
    progress = float(progress or 0)
    target = float(target or 0)
    if direction == "lte":
        return "cumplido" if progress <= target else "en_riesgo"
    return "cumplido" if progress >= target else "en_progreso"


def _objective_scope_sql(obj, alias="t"):
    if (obj.get("scope_type") or "") == "technician" and obj.get("technician_id"):
        return f" AND {alias}.asignado_a = %s", [obj.get("technician_id")]
    return "", []


def _metric_progress(metric_key, start, end, obj):
    scope_sql, scope_params = _objective_scope_sql(obj, "t")
    if metric_key == "entregados":
        row = _safe_one(
            f"""
            SELECT COUNT(*) AS total
              FROM ingresos t
             WHERE t.fecha_entrega >= %s
               AND t.fecha_entrega < %s
               {scope_sql}
            """,
            [start, end, *scope_params],
        )
        return int(row.get("total") or 0)

    if metric_key == "reparados":
        row = _safe_one(
            f"""
            SELECT COUNT(DISTINCT ev.ingreso_id) AS total
              FROM ingreso_events ev
              JOIN ingresos t ON t.id = ev.ingreso_id
             WHERE ev.ts >= %s
               AND ev.ts < %s
               AND ev.a_estado IN ('reparado','controlado_sin_defecto','liberado')
               {scope_sql}
            """,
            [start, end, *scope_params],
        )
        return int(row.get("total") or 0)

    if metric_key == "diagnosticados":
        row = _safe_one(
            f"""
            SELECT COUNT(DISTINCT t.id) AS total
              FROM ingresos t
              LEFT JOIN ingreso_events ev
                ON ev.ingreso_id = t.id
               AND ev.a_estado = 'diagnosticado'
             WHERE (
                    (t.fecha_servicio >= %s AND t.fecha_servicio < %s)
                    OR (ev.ts >= %s AND ev.ts < %s)
                   )
               {scope_sql}
            """,
            [start, end, start, end, *scope_params],
        )
        return int(row.get("total") or 0)

    if metric_key == "presupuestos_emitidos":
        row = _safe_one(
            f"""
            SELECT COUNT(*) AS total
              FROM quotes qu
              JOIN ingresos t ON t.id = qu.ingreso_id
             WHERE qu.fecha_emitido >= %s
               AND qu.fecha_emitido < %s
               {scope_sql}
            """,
            [start, end, *scope_params],
        )
        return int(row.get("total") or 0)

    if metric_key == "presupuestos_aprobados":
        row = _safe_one(
            f"""
            SELECT COUNT(*) AS total
              FROM quotes qu
              JOIN ingresos t ON t.id = qu.ingreso_id
             WHERE qu.fecha_aprobado >= %s
               AND qu.fecha_aprobado < %s
               {scope_sql}
            """,
            [start, end, *scope_params],
        )
        return int(row.get("total") or 0)

    if metric_key == "wip_max":
        row = _safe_one(
            f"""
            SELECT COUNT(*) AS total
              FROM ingresos t
              LEFT JOIN locations loc ON loc.id = t.ubicacion_id
             WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
               AND t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
               {scope_sql}
            """,
            [TALLER_LOCATION, *scope_params],
        )
        return int(row.get("total") or 0)

    return 0


def _fetch_objectives(period_type, request=None):
    if not _table_exists("work_objectives"):
        return []
    role = _normalize_role_value(_rol(request)) if request is not None else "jefe"
    user_id = _current_user_id(request) if request is not None else None
    if role in {"admin", "recepcion", "cobranzas"}:
        return []
    where = [
        "wo.active = TRUE",
        "wo.period_type = %s",
        "wo.valid_from <= CURRENT_DATE",
        "(wo.valid_to IS NULL OR wo.valid_to >= CURRENT_DATE)",
    ]
    params = [period_type]
    if role == "tecnico" and user_id:
        where.append("wo.scope_type = 'technician'")
        where.append("wo.technician_id = %s")
        params.append(user_id)
    rows = _safe_rows(
        f"""
        SELECT wo.id, wo.scope_type, wo.technician_id, COALESCE(u.nombre,'') AS technician_name,
               wo.period_type, wo.metric_key, wo.label, wo.target_value, wo.direction,
               wo.valid_from, wo.valid_to, wo.active
          FROM work_objectives wo
          LEFT JOIN users u ON u.id = wo.technician_id
         WHERE {" AND ".join(where)}
         ORDER BY wo.scope_type, COALESCE(u.nombre,''), wo.metric_key, wo.id
        """,
        params,
    )
    return rows


def build_objectives(periodo="hoy", request=None):
    period_type, start, end = _period_bounds(periodo)
    items = []
    for obj in _fetch_objectives(period_type, request=request):
        progress = _metric_progress(obj.get("metric_key"), start, end, obj)
        target = _as_decimal(obj.get("target_value"), 0)
        direction = obj.get("direction") or "gte"
        items.append(
            {
                "id": obj.get("id"),
                "scope_type": obj.get("scope_type"),
                "technician_id": obj.get("technician_id"),
                "technician_name": obj.get("technician_name") or "",
                "period_type": obj.get("period_type"),
                "metric_key": obj.get("metric_key"),
                "label": obj.get("label") or METRIC_LABELS.get(obj.get("metric_key"), obj.get("metric_key")),
                "target_value": float(target),
                "direction": direction,
                "valid_from": obj.get("valid_from"),
                "valid_to": obj.get("valid_to"),
                "active": _as_bool(obj.get("active"), True),
                "progress": progress,
                "percent": _percent_for_objective(progress, target, direction),
                "status": _objective_status(progress, target, direction),
            }
        )
    return {
        "periodo": "semana" if period_type == "weekly" else "hoy",
        "period_type": period_type,
        "start": start,
        "end": end,
        "items": items,
    }


COMMON_INGRESO_SELECT = """
    t.id AS ingreso_id,
    t.id,
    t.estado::text AS estado,
    t.presupuesto_estado::text AS presupuesto_estado,
    t.motivo::text AS motivo,
    c.razon_social,
    c.razon_social AS cliente,
    d.numero_serie,
    COALESCE(d.numero_interno,'') AS numero_interno,
    COALESCE(b.nombre,'') AS marca,
    COALESCE(m.nombre,'') AS modelo,
    COALESCE(m.tipo_equipo,'') AS tipo_equipo,
    COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
    t.fecha_ingreso,
    t.fecha_creacion,
    t.asignado_a,
    COALESCE(u.nombre,'') AS tecnico,
    COALESCE(loc.nombre,'') AS ubicacion_nombre,
    GREATEST(0, (CURRENT_DATE - CAST(COALESCE(t.fecha_ingreso, t.fecha_creacion) AS DATE)))::int AS dias_espera
"""


COMMON_INGRESO_JOINS = """
    FROM ingresos t
    JOIN devices d ON d.id = t.device_id
    JOIN customers c ON c.id = d.customer_id
    LEFT JOIN marcas b ON b.id = d.marca_id
    LEFT JOIN models m ON m.id = d.model_id
    LEFT JOIN users u ON u.id = t.asignado_a
    LEFT JOIN locations loc ON loc.id = t.ubicacion_id
"""


def _format_ingreso_item(row, alert_key=None, next_action=None):
    item = dict(row or {})
    ingreso_id = item.get("ingreso_id") or item.get("id")
    item["id"] = ingreso_id
    item["ingreso_id"] = ingreso_id
    item["os"] = os_label(ingreso_id)
    item["href"] = f"/ingresos/{ingreso_id}" if ingreso_id else ""
    item["alert_key"] = alert_key or ""
    item["next_action"] = next_action or item.get("next_action") or ""
    return item


def _alert_payload(key, title, description, severity, rows, href, next_action):
    items = [_format_ingreso_item(row, key, next_action) for row in rows[:8]]
    return {
        "key": key,
        "title": title,
        "description": description,
        "severity": severity,
        "count": len(rows),
        "href": href,
        "items": items,
    }


def _filter_snoozed(request, key, rows, ref_field="ingreso_id"):
    user_id = _current_user_id(request)
    if not user_id or not _table_exists("work_alert_snoozes"):
        return rows
    refs = [
        str(row.get(ref_field) or row.get("id") or "")
        for row in rows
        if row.get(ref_field) or row.get("id")
    ]
    if not refs:
        return rows
    hidden = _safe_rows(
        """
        SELECT alert_ref
          FROM work_alert_snoozes
         WHERE user_id = %s
           AND alert_key = %s
           AND snoozed_until > CURRENT_TIMESTAMP
           AND alert_ref = ANY(%s)
        """,
        [user_id, key, refs],
    )
    hidden_refs = {str(row.get("alert_ref")) for row in hidden}
    if not hidden_refs:
        return rows
    return [
        row for row in rows
        if str(row.get(ref_field) or row.get("id") or "") not in hidden_refs
    ]


def _alert_rows_sin_tecnico(request, rule):
    days = _rule_days(rule)
    return _safe_rows(
        f"""
        SELECT {COMMON_INGRESO_SELECT}
        {COMMON_INGRESO_JOINS}
         WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
           AND t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
           AND t.asignado_a IS NULL
           AND CAST(COALESCE(t.fecha_ingreso, t.fecha_creacion) AS DATE) <= CURRENT_DATE - %s::int
         ORDER BY dias_espera DESC, t.id ASC
        """,
        [TALLER_LOCATION, days],
    )


def _alert_rows_wip_critico(request, rule):
    scope_sql, scope_params = _scope_filter(request, "t")
    days = _rule_days(rule)
    return _safe_rows(
        f"""
        SELECT {COMMON_INGRESO_SELECT}
        {COMMON_INGRESO_JOINS}
         WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
           AND t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
           AND CAST(COALESCE(t.fecha_ingreso, t.fecha_creacion) AS DATE) <= CURRENT_DATE - %s::int
           {scope_sql}
         ORDER BY dias_espera DESC, t.id ASC
        """,
        [TALLER_LOCATION, days, *scope_params],
    )


def _alert_rows_presupuesto(request, rule):
    scope_sql, scope_params = _scope_filter(request, "t")
    days = _rule_days(rule)
    return _safe_rows(
        f"""
        SELECT {COMMON_INGRESO_SELECT},
               q.fecha_emitido AS presupuesto_fecha_emision,
               COALESCE(q.total, 0) AS presupuesto_total,
               COALESCE(q.moneda, 'ARS') AS presupuesto_moneda,
               GREATEST(0, (CURRENT_DATE - CAST(COALESCE(q.fecha_emitido, t.fecha_servicio, t.fecha_ingreso, t.fecha_creacion) AS DATE)))::int AS dias_presupuesto
        {COMMON_INGRESO_JOINS}
        JOIN quotes q ON q.id = (
          SELECT q2.id FROM quotes q2
           WHERE q2.ingreso_id = t.id
           ORDER BY COALESCE(q2.version_num, 1) DESC, q2.id DESC
           LIMIT 1
        )
         WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
           AND t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
           AND (
                q.estado::text IN ('emitido','enviado','presupuestado')
                OR t.presupuesto_estado = 'presupuestado'
               )
           AND CAST(COALESCE(q.fecha_emitido, t.fecha_servicio, t.fecha_ingreso, t.fecha_creacion) AS DATE) <= CURRENT_DATE - %s::int
           {scope_sql}
         ORDER BY dias_presupuesto DESC, t.id ASC
        """,
        [TALLER_LOCATION, days, *scope_params],
    )


def _alert_rows_liberado(request, rule):
    scope_sql, scope_params = _scope_filter(request, "t")
    days = _rule_days(rule)
    return _safe_rows(
        f"""
        SELECT {COMMON_INGRESO_SELECT},
               ev.fecha_liberacion,
               GREATEST(0, (CURRENT_DATE - CAST(COALESCE(ev.fecha_liberacion, t.fecha_ingreso, t.fecha_creacion) AS DATE)))::int AS dias_liberado
        {COMMON_INGRESO_JOINS}
        LEFT JOIN (
          SELECT ingreso_id, MAX(ts) AS fecha_liberacion
            FROM ingreso_events
           WHERE a_estado = 'liberado'
           GROUP BY ingreso_id
        ) ev ON ev.ingreso_id = t.id
         WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
           AND t.estado IN ('liberado','vendido_pendiente_entrega')
           AND CAST(COALESCE(ev.fecha_liberacion, t.fecha_ingreso, t.fecha_creacion) AS DATE) <= CURRENT_DATE - %s::int
           {scope_sql}
         ORDER BY dias_liberado DESC, t.id ASC
        """,
        [TALLER_LOCATION, days, *scope_params],
    )


def _alert_rows_derivado(request, rule):
    scope_sql, scope_params = _scope_filter(request, "t")
    days = _rule_days(rule)
    return _safe_rows(
        f"""
        SELECT {COMMON_INGRESO_SELECT},
               ed.id AS derivacion_id,
               pe.nombre AS proveedor,
               ed.fecha_deriv,
               GREATEST(0, (CURRENT_DATE - ed.fecha_deriv))::int AS dias_derivado
        {COMMON_INGRESO_JOINS}
        JOIN equipos_derivados ed ON ed.ingreso_id = t.id
        LEFT JOIN proveedores_externos pe ON pe.id = ed.proveedor_id
         WHERE ed.estado IN ('derivado','en_servicio')
           AND ed.fecha_entrega IS NULL
           AND ed.fecha_deriv <= CURRENT_DATE - %s::int
           {scope_sql}
         ORDER BY dias_derivado DESC, ed.fecha_deriv ASC, ed.id ASC
        """,
        [days, *scope_params],
    )


def _preventivo_rows(rule, vencidos=True):
    if not _table_exists("preventivo_planes"):
        return []
    days = _rule_days(rule)
    if vencidos:
        where = "p.proxima_revision_fecha IS NOT NULL AND p.proxima_revision_fecha < CURRENT_DATE"
        params = []
    else:
        where = "p.proxima_revision_fecha IS NOT NULL AND p.proxima_revision_fecha >= CURRENT_DATE AND p.proxima_revision_fecha <= CURRENT_DATE + %s::int"
        params = [days]
    return _safe_rows(
        f"""
        SELECT p.id AS preventivo_plan_id,
               p.scope_type::text AS scope_type,
               p.device_id,
               p.customer_id,
               p.proxima_revision_fecha,
               GREATEST(0, (CURRENT_DATE - p.proxima_revision_fecha))::int AS dias_vencido,
               GREATEST(0, (p.proxima_revision_fecha - CURRENT_DATE))::int AS dias_restantes,
               COALESCE(c.razon_social, cd.razon_social, '') AS cliente,
               COALESCE(d.numero_serie,'') AS numero_serie,
               COALESCE(d.numero_interno,'') AS numero_interno,
               COALESCE(b.nombre,'') AS marca,
               COALESCE(m.nombre,'') AS modelo,
               COALESCE(m.tipo_equipo,'') AS tipo_equipo,
               COALESCE(NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
          FROM preventivo_planes p
          LEFT JOIN devices d ON d.id = p.device_id
          LEFT JOIN customers cd ON cd.id = d.customer_id
          LEFT JOIN customers c ON c.id = p.customer_id
          LEFT JOIN marcas b ON b.id = d.marca_id
          LEFT JOIN models m ON m.id = d.model_id
         WHERE p.activa = TRUE
           AND {where}
         ORDER BY p.proxima_revision_fecha ASC, p.id ASC
        """,
        params,
    )


def _preventivo_alert_payload(key, rule, rows, vencidos=True):
    items = []
    for row in rows[:8]:
        plan_id = row.get("preventivo_plan_id")
        items.append(
            {
                **row,
                "id": plan_id,
                "alert_key": key,
                "href": "/equipos",
                "next_action": "Programar revisión preventiva" if vencidos else "Planificar revisión preventiva",
            }
        )
    return {
        "key": key,
        "title": rule.get("label"),
        "description": rule.get("description"),
        "severity": rule.get("severity"),
        "count": len(rows),
        "href": "/equipos",
        "items": items,
    }


def _active_work_count(request):
    scope_sql, scope_params = _scope_filter(request, "t")
    row = _safe_one(
        f"""
        SELECT COUNT(*) AS total
          FROM ingresos t
          LEFT JOIN locations loc ON loc.id = t.ubicacion_id
         WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
           AND t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
           {scope_sql}
        """,
        [TALLER_LOCATION, *scope_params],
    )
    return int(row.get("total") or 0)


DELIVERY_OPEN_STATUSES = (
    "pendiente_armado",
    "armado_pendiente_entrega",
    "entregado_pendiente_facturacion",
)


def _empty_delivery_counts():
    return {
        "pendiente_armado": 0,
        "armado_pendiente_entrega": 0,
        "entregado_pendiente_facturacion": 0,
        "facturado": 0,
        "cancelado": 0,
        "active": 0,
    }


def _delivery_order_counts():
    counts = _empty_delivery_counts()
    if not _table_exists("delivery_orders"):
        return counts
    rows = _safe_rows(
        """
        SELECT status, COUNT(*) AS total
          FROM delivery_orders
         GROUP BY status
        """
    )
    for row in rows:
        status = row.get("status")
        if status:
            counts[status] = int(row.get("total") or 0)
    counts["active"] = sum(int(counts.get(status) or 0) for status in DELIVERY_OPEN_STATUSES)
    return counts


def _format_delivery_order(row):
    return {
        "id": row.get("id"),
        "orderNumber": row.get("order_number") or "",
        "customerName": row.get("customer_name") or "",
        "deliveryType": row.get("delivery_type") or "",
        "status": row.get("status") or "",
        "priority": row.get("priority") or "",
        "orderDate": row.get("order_date"),
        "equipmentModel": row.get("equipment_model") or "",
        "equipmentSerial": row.get("equipment_serial") or "",
        "equipmentInternalNumber": row.get("equipment_internal_number") or "",
        "remitoNumber": row.get("remito_number") or "",
        "remitoLocation": row.get("remito_location") or "",
        "invoiceNumber": row.get("invoice_number") or "",
        "href": "/administracion/ordenes-entrega",
    }


def _delivery_statuses_for_variant(variant):
    if variant == "cobranzas":
        return ("entregado_pendiente_facturacion",)
    if variant == "admin":
        return ("pendiente_armado", "armado_pendiente_entrega")
    if variant in {"jefe", "recepcion"}:
        return DELIVERY_OPEN_STATUSES
    return ()


def _delivery_order_items(variant, limit=8):
    if not _table_exists("delivery_orders"):
        return []
    statuses = _delivery_statuses_for_variant(variant)
    if not statuses:
        return []
    placeholders = ",".join(["%s"] * len(statuses))
    rows = _safe_rows(
        f"""
        SELECT id, order_number, customer_name, delivery_type, status, priority,
               order_date, equipment_model, equipment_serial, equipment_internal_number,
               remito_number, remito_location, invoice_number, created_at
          FROM delivery_orders
         WHERE status IN ({placeholders})
         ORDER BY
           CASE priority WHEN 'urgente' THEN 0 ELSE 1 END,
           order_date DESC,
           created_at DESC
         LIMIT %s
        """,
        [*statuses, limit],
    )
    return [_format_delivery_order(row) for row in rows]


def _build_delivery_summary(variant):
    return {
        "counts": _delivery_order_counts(),
        "items": _delivery_order_items(variant),
    }


def _build_kpis(request, raw):
    delivery_counts = raw.get("delivery_counts") or {}
    all_kpis = {
        "en_taller": {"key": "en_taller", "label": "En taller", "value": _active_work_count(request), "severity": "info"},
        "wip_critico": {"key": "wip_critico", "label": "WIP crítico", "value": len(raw.get("wip_critico") or []), "severity": "critical"},
        "sin_tecnico": {"key": "sin_tecnico", "label": "Sin técnico", "value": len(raw.get("sin_tecnico") or []), "severity": "warning"},
        "presupuestos_demorados": {"key": "presupuestos_demorados", "label": "Presupuestos demorados", "value": len(raw.get("presupuesto_sin_aprobar") or []), "severity": "critical"},
        "liberados_en_espera": {"key": "liberados_en_espera", "label": "Liberados en espera", "value": len(raw.get("liberado_sin_entregar") or []), "severity": "warning"},
        "derivados_en_espera": {"key": "derivados_en_espera", "label": "Derivados en espera", "value": len(raw.get("derivado_sin_devolucion") or []), "severity": "warning"},
        "preventivos_vencidos": {"key": "preventivos_vencidos", "label": "Preventivos vencidos", "value": len(raw.get("preventivo_vencido") or []), "severity": "critical"},
        "preventivos_proximos": {"key": "preventivos_proximos", "label": "Preventivos próximos", "value": len(raw.get("preventivo_proximo") or []), "severity": "warning"},
        "pedidos_abiertos": {"key": "pedidos_abiertos", "label": "Pedidos abiertos", "value": int(delivery_counts.get("active") or 0), "severity": "info"},
        "pedidos_pendientes_armado": {"key": "pedidos_pendientes_armado", "label": "Pedidos a armar", "value": int(delivery_counts.get("pendiente_armado") or 0), "severity": "warning"},
        "pedidos_listos_entrega": {"key": "pedidos_listos_entrega", "label": "Listos para entrega", "value": int(delivery_counts.get("armado_pendiente_entrega") or 0), "severity": "info"},
        "remitos_pendientes_facturacion": {"key": "remitos_pendientes_facturacion", "label": "Remitos a facturar", "value": int(delivery_counts.get("entregado_pendiente_facturacion") or 0), "severity": "warning"},
        "pedidos_facturados": {"key": "pedidos_facturados", "label": "Pedidos facturados", "value": int(delivery_counts.get("facturado") or 0), "severity": "info"},
    }
    variant = _dashboard_variant(request)
    return [all_kpis[key] for key in DASHBOARD_KPIS.get(variant, DASHBOARD_KPIS["jefe"]) if key in all_kpis]


def _build_alerts(request):
    rules = _rule_map()
    variant = _dashboard_variant(request)
    allowed = DASHBOARD_ALERTS.get(variant, DASHBOARD_ALERTS["jefe"])
    alerts = []
    raw = {}

    rule = rules.get("wip_critico")
    if "wip_critico" in allowed and rule and rule.get("enabled"):
        rows = _filter_snoozed(request, "wip_critico", _alert_rows_wip_critico(request, rule))
        raw["wip_critico"] = rows
        if rows:
            alerts.append(_alert_payload("wip_critico", rule["label"], rule["description"], rule["severity"], rows, "/pendientes", "Revisar avance y próxima acción"))

    rule = rules.get("sin_tecnico")
    if "sin_tecnico" in allowed and rule and rule.get("enabled"):
        rows = _filter_snoozed(request, "sin_tecnico", _alert_rows_sin_tecnico(request, rule))
        raw["sin_tecnico"] = rows
        if rows:
            alerts.append(_alert_payload("sin_tecnico", rule["label"], rule["description"], rule["severity"], rows, "/pendientes-por-tecnico?tecnico_id=sin_asignar", "Asignar técnico"))

    rule = rules.get("presupuesto_sin_aprobar")
    if "presupuesto_sin_aprobar" in allowed and rule and rule.get("enabled"):
        rows = _filter_snoozed(request, "presupuesto_sin_aprobar", _alert_rows_presupuesto(request, rule))
        raw["presupuesto_sin_aprobar"] = rows
        if rows:
            alerts.append(_alert_payload("presupuesto_sin_aprobar", rule["label"], rule["description"], rule["severity"], rows, "/presupuestados", "Llamar al cliente o cerrar decisión"))

    rule = rules.get("liberado_sin_entregar")
    if "liberado_sin_entregar" in allowed and rule and rule.get("enabled"):
        rows = _filter_snoozed(request, "liberado_sin_entregar", _alert_rows_liberado(request, rule))
        raw["liberado_sin_entregar"] = rows
        if rows:
            alerts.append(_alert_payload("liberado_sin_entregar", rule["label"], rule["description"], rule["severity"], rows, "/listos", "Coordinar entrega"))

    rule = rules.get("derivado_sin_devolucion")
    if "derivado_sin_devolucion" in allowed and rule and rule.get("enabled"):
        rows = _filter_snoozed(request, "derivado_sin_devolucion", _alert_rows_derivado(request, rule))
        raw["derivado_sin_devolucion"] = rows
        if rows:
            alerts.append(_alert_payload("derivado_sin_devolucion", rule["label"], rule["description"], rule["severity"], rows, "/derivados", "Consultar proveedor"))

    rule = rules.get("preventivo_vencido")
    if "preventivo_vencido" in allowed and rule and rule.get("enabled"):
        rows = _filter_snoozed(request, "preventivo_vencido", _preventivo_rows(rule, vencidos=True), "preventivo_plan_id")
        raw["preventivo_vencido"] = rows
        if rows:
            alerts.append(_preventivo_alert_payload("preventivo_vencido", rule, rows, vencidos=True))

    rule = rules.get("preventivo_proximo")
    if "preventivo_proximo" in allowed and rule and rule.get("enabled"):
        rows = _filter_snoozed(request, "preventivo_proximo", _preventivo_rows(rule, vencidos=False), "preventivo_plan_id")
        raw["preventivo_proximo"] = rows
        if rows:
            alerts.append(_preventivo_alert_payload("preventivo_proximo", rule, rows, vencidos=False))

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: (severity_order.get(a.get("severity"), 9), -int(a.get("count") or 0), a.get("title") or ""))
    return alerts, raw


def _priority_identity(item):
    if item.get("ingreso_id"):
        return ("ingreso", int(item.get("ingreso_id")))
    if item.get("preventivo_plan_id"):
        return ("preventivo", int(item.get("preventivo_plan_id")))
    if item.get("id"):
        return ("item", int(item.get("id")))
    return None


def _priority_wait_days(item):
    return -int(
        item.get("dias_espera")
        or item.get("dias_presupuesto")
        or item.get("dias_derivado")
        or item.get("dias_liberado")
        or item.get("dias_vencido")
        or 0
    )


def _priority_alert_rank(item):
    return {
        "sin_tecnico": 0,
        "presupuesto_sin_aprobar": 1,
        "liberado_sin_entregar": 2,
        "derivado_sin_devolucion": 3,
        "wip_critico": 4,
        "preventivo_vencido": 5,
        "preventivo_proximo": 6,
    }.get(item.get("alert_key"), 99)


def _dedupe_priorities(items):
    seen = set()
    unique = []
    for item in items:
        identity = _priority_identity(item)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        unique.append(item)
    return unique


def build_work_summary(request, periodo="hoy"):
    alerts, raw = _build_alerts(request)
    variant = _dashboard_variant(request)
    delivery_orders = _build_delivery_summary(variant)
    raw["delivery_counts"] = delivery_orders.get("counts") or {}
    priorities = []
    for alert in alerts:
        priorities.extend(alert.get("items") or [])
    severity_by_key = {alert.get("key"): alert.get("severity") or "info" for alert in alerts}
    priorities.sort(
        key=lambda item: (
            {"critical": 0, "warning": 1, "info": 2}.get(severity_by_key.get(item.get("alert_key"), "info"), 9),
            _priority_alert_rank(item),
            _priority_wait_days(item),
        )
    )
    priorities = _dedupe_priorities(priorities)
    objectives = build_objectives(periodo, request=request)
    return {
        "generated_at": timezone.now(),
        "scope": {
            "role": _normalize_role_value(_rol(request)),
            "user_id": _current_user_id(request),
            "technician_only": variant == "tecnico",
            "dashboard_variant": variant,
        },
        "kpis": _build_kpis(request, raw),
        "alerts": alerts,
        "prioridades": priorities[:16],
        "objetivos": objectives.get("items", []),
        "delivery_orders": delivery_orders,
        "periodo": objectives.get("periodo"),
    }


class WorkResumenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, VIEW_ROLES)
        _set_audit_user(request)
        periodo = (request.GET.get("periodo") or "hoy").strip().lower()
        return Response(build_work_summary(request, periodo=periodo))


class WorkAlertRulesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, MANAGE_ROLES)
        return Response({"items": load_alert_rules()})

    def patch(self, request):
        require_roles(request, MANAGE_ROLES)
        if not _table_exists("work_alert_rules"):
            return Response({"detail": "El esquema de reglas de alerta no está aplicado."}, status=503)

        payload = request.data or {}
        rows = payload.get("rules") if isinstance(payload, dict) else None
        if rows is None:
            rows = [payload]
        if not isinstance(rows, list):
            return Response({"detail": "Formato inválido: se esperaba una lista de reglas."}, status=400)

        defaults = _rule_map()
        for raw in rows:
            if not isinstance(raw, dict):
                return Response({"detail": "Cada regla debe ser un objeto."}, status=400)
            key = (raw.get("rule_key") or "").strip()
            if not key:
                return Response({"detail": "rule_key es obligatorio."}, status=400)
            base = defaults.get(key, DEFAULT_ALERT_RULES.get(key, {"rule_key": key}))
            unit = (raw.get("threshold_unit") or base.get("threshold_unit") or "dias").strip()
            severity = (raw.get("severity") or base.get("severity") or "warning").strip()
            if unit not in RULE_UNITS:
                return Response({"detail": f"Unidad inválida para {key}."}, status=400)
            if severity not in RULE_SEVERITIES:
                return Response({"detail": f"Severidad inválida para {key}."}, status=400)
            threshold = _as_decimal(raw.get("threshold_value", base.get("threshold_value", 0)), 0)
            if threshold < 0:
                return Response({"detail": f"El umbral de {key} no puede ser negativo."}, status=400)
            exec_void(
                """
                INSERT INTO work_alert_rules(
                  rule_key, label, description, threshold_value, threshold_unit,
                  severity, enabled, email_enabled
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (rule_key) DO UPDATE SET
                  label = EXCLUDED.label,
                  description = EXCLUDED.description,
                  threshold_value = EXCLUDED.threshold_value,
                  threshold_unit = EXCLUDED.threshold_unit,
                  severity = EXCLUDED.severity,
                  enabled = EXCLUDED.enabled,
                  email_enabled = EXCLUDED.email_enabled,
                  updated_at = CURRENT_TIMESTAMP
                """,
                [
                    key,
                    (raw.get("label") or base.get("label") or key).strip(),
                    raw.get("description", base.get("description") or ""),
                    threshold,
                    unit,
                    severity,
                    _as_bool(raw.get("enabled"), _as_bool(base.get("enabled"), True)),
                    _as_bool(raw.get("email_enabled"), _as_bool(base.get("email_enabled"), False)),
                ],
            )
        return Response({"items": load_alert_rules()})


class WorkObjectivesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, VIEW_ROLES)
        periodo = (request.GET.get("periodo") or "hoy").strip().lower()
        return Response(build_objectives(periodo, request=request))

    def put(self, request):
        require_roles(request, MANAGE_ROLES)
        if not _table_exists("work_objectives"):
            return Response({"detail": "El esquema de objetivos no está aplicado."}, status=503)

        payload = request.data or {}
        rows = payload.get("objectives") if isinstance(payload, dict) else None
        if rows is None:
            rows = [payload]
        if not isinstance(rows, list):
            return Response({"detail": "Formato inválido: se esperaba una lista de objetivos."}, status=400)

        uid = _current_user_id(request)
        for raw in rows:
            if not isinstance(raw, dict):
                return Response({"detail": "Cada objetivo debe ser un objeto."}, status=400)
            scope = (raw.get("scope_type") or "global").strip()
            period = (raw.get("period_type") or "daily").strip()
            metric = (raw.get("metric_key") or "").strip()
            direction = (raw.get("direction") or "gte").strip()
            if scope not in OBJECTIVE_SCOPES:
                return Response({"detail": "scope_type inválido."}, status=400)
            if period not in OBJECTIVE_PERIODS:
                return Response({"detail": "period_type inválido."}, status=400)
            if direction not in OBJECTIVE_DIRECTIONS:
                return Response({"detail": "direction inválido."}, status=400)
            if not metric:
                return Response({"detail": "metric_key es obligatorio."}, status=400)
            technician_id = raw.get("technician_id")
            if scope == "technician" and not technician_id:
                return Response({"detail": "technician_id es obligatorio para objetivos por técnico."}, status=400)
            if scope == "global":
                technician_id = None
            target = _as_decimal(raw.get("target_value"), 0)
            if target < 0:
                return Response({"detail": "target_value no puede ser negativo."}, status=400)
            label = (raw.get("label") or METRIC_LABELS.get(metric) or metric).strip()
            valid_from = raw.get("valid_from") or timezone.localdate()
            valid_to = raw.get("valid_to") or None
            active = _as_bool(raw.get("active"), True)
            obj_id = raw.get("id")
            if obj_id:
                exec_void(
                    """
                    UPDATE work_objectives
                       SET scope_type=%s, technician_id=%s, period_type=%s, metric_key=%s,
                           label=%s, target_value=%s, direction=%s, active=%s,
                           valid_from=%s, valid_to=%s, updated_by=%s, updated_at=CURRENT_TIMESTAMP
                     WHERE id=%s
                    """,
                    [scope, technician_id, period, metric, label, target, direction, active, valid_from, valid_to, uid, obj_id],
                )
            else:
                exec_void(
                    """
                    INSERT INTO work_objectives(
                      scope_type, technician_id, period_type, metric_key, label,
                      target_value, direction, active, valid_from, valid_to,
                      created_by, updated_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    [scope, technician_id, period, metric, label, target, direction, active, valid_from, valid_to, uid, uid],
                )

        periodo = "semana" if any((r.get("period_type") == "weekly") for r in rows if isinstance(r, dict)) else "hoy"
        return Response(build_objectives(periodo, request=request))


class GlobalSearchView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, VIEW_ROLES)
        term = (request.GET.get("q") or "").strip()
        if len(term) < 2:
            return Response({"q": term, "groups": [], "total": 0})
        like = f"%{term}%"
        mg_select_sql = _mg_list_select_sql("d")
        ingreso_rows = _safe_rows(
            f"""
            SELECT t.id AS ingreso_id,
                   t.estado::text AS estado,
                   t.presupuesto_estado::text AS presupuesto_estado,
                   t.fecha_ingreso,
                   t.fecha_creacion,
                   c.razon_social AS cliente,
                   d.numero_serie,
                   COALESCE(d.numero_interno,'') AS numero_interno,
                   {mg_select_sql}
                   COALESCE(b.nombre,'') AS marca,
                   COALESCE(m.nombre,'') AS modelo,
                   COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                   COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
             WHERE CAST(t.id AS TEXT) ILIKE %s
                OR COALESCE(c.razon_social,'') ILIKE %s
                OR COALESCE(c.cod_empresa,'') ILIKE %s
                OR COALESCE(d.numero_serie,'') ILIKE %s
                OR COALESCE(d.numero_interno,'') ILIKE %s
                OR COALESCE(b.nombre,'') ILIKE %s
                OR COALESCE(m.nombre,'') ILIKE %s
                OR COALESCE(m.tipo_equipo,'') ILIKE %s
             ORDER BY t.id DESC
             LIMIT 12
            """,
            [like, like, like, like, like, like, like, like],
        )
        ingresos = [
            {
                **row,
                "id": row.get("ingreso_id"),
                "os": os_label(row.get("ingreso_id")),
                "href": f"/ingresos/{row.get('ingreso_id')}",
            }
            for row in ingreso_rows
        ]

        devices = _safe_rows(
            f"""
            SELECT d.id AS device_id,
                   c.razon_social AS cliente,
                   d.numero_serie,
                   COALESCE(d.numero_interno,'') AS numero_interno,
                   {mg_select_sql}
                   COALESCE(b.nombre,'') AS marca,
                   COALESCE(m.nombre,'') AS modelo,
                   COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                   COALESCE(NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                   last_ingreso.id AS ultimo_ingreso_id
              FROM devices d
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN LATERAL (
                SELECT t.id
                  FROM ingresos t
                 WHERE t.device_id = d.id
                 ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
                 LIMIT 1
              ) last_ingreso ON TRUE
             WHERE COALESCE(c.razon_social,'') ILIKE %s
                OR COALESCE(d.numero_serie,'') ILIKE %s
                OR COALESCE(d.numero_interno,'') ILIKE %s
                OR COALESCE(b.nombre,'') ILIKE %s
                OR COALESCE(m.nombre,'') ILIKE %s
                OR COALESCE(m.tipo_equipo,'') ILIKE %s
             ORDER BY d.id DESC
             LIMIT 8
            """,
            [like, like, like, like, like, like],
        )
        equipos = [
            {
                **row,
                "id": row.get("device_id"),
                "href": f"/ingresos/{row.get('ultimo_ingreso_id')}" if row.get("ultimo_ingreso_id") else "/equipos",
            }
            for row in devices
        ]

        clientes = _safe_rows(
            """
            SELECT c.id AS customer_id,
                   c.razon_social,
                   COALESCE(c.cod_empresa,'') AS cod_empresa,
                   COALESCE(c.telefono,'') AS telefono,
                   COALESCE(c.email,'') AS email
              FROM customers c
             WHERE COALESCE(c.razon_social,'') ILIKE %s
                OR COALESCE(c.cod_empresa,'') ILIKE %s
                OR COALESCE(c.cuit,'') ILIKE %s
             ORDER BY c.razon_social ASC
             LIMIT 8
            """,
            [like, like, like],
        )
        clientes = [
            {
                **row,
                "id": row.get("customer_id"),
                "href": f"/clientes?customer_id={row.get('customer_id')}",
            }
            for row in clientes
        ]

        groups = [
            {"key": "ingresos", "label": "Hojas de servicio", "items": ingresos},
            {"key": "equipos", "label": "Equipos", "items": equipos},
            {"key": "clientes", "label": "Clientes", "items": clientes},
        ]
        return Response({"q": term, "groups": groups, "total": sum(len(g["items"]) for g in groups)})


def work_summary_email_fingerprint(summary):
    payload = {
        "alerts": [
            {"key": item.get("key"), "count": item.get("count"), "severity": item.get("severity")}
            for item in (summary or {}).get("alerts", [])
        ],
        "objetivos": [
            {"id": item.get("id"), "progress": item.get("progress"), "status": item.get("status")}
            for item in (summary or {}).get("objetivos", [])
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "WorkResumenView",
    "WorkObjectivesView",
    "WorkAlertRulesView",
    "GlobalSearchView",
    "build_work_summary",
    "build_objectives",
    "load_alert_rules",
    "work_summary_email_fingerprint",
]
