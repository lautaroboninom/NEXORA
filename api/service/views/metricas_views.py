import datetime as dt
from collections import defaultdict
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from ..activity_audit import classify_read_path
from ..rejected_budget import has_rejected_budget_charge_schema
from .helpers import (
    WORKDAY_END_HOUR,
    WORKDAY_START_HOUR,
    WORKDAYS,
    _holidays_between,
    _set_audit_user,
    business_minutes_between,
    exec_void,
    os_label,
    q,
    require_permission,
    require_roles,
)

# Global cutoff for metrics: only include ingresos with fecha_ingreso >= 2025-06-26 (inclusive)
METRICAS_INGRESO_CUTOFF_DATE = dt.date(2025, 6, 26)

def _cutoff_ts():
    tz = timezone.get_current_timezone()
    return timezone.make_aware(dt.datetime.combine(METRICAS_INGRESO_CUTOFF_DATE, dt.time.min), tz)


def _parse_range_params(request):
    tz = timezone.get_current_timezone()
    now = timezone.now()
    from_s = request.GET.get("from") or request.GET.get("desde")
    to_s = request.GET.get("to") or request.GET.get("hasta")
    if from_s:
        try:
            d = parse_date(from_s)
            since = timezone.make_aware(dt.datetime.combine(d, dt.time.min), tz)
        except Exception:
            since = now - dt.timedelta(days=30)
    else:
        since = now - dt.timedelta(days=30)
    # Enforce global cutoff (inclusive)
    try:
        cutoff = _cutoff_ts()
        if since < cutoff:
            since = cutoff
    except Exception:
        pass
    if to_s:
        try:
            d = parse_date(to_s)
            until = timezone.make_aware(dt.datetime.combine(d, dt.time.max), tz)
        except Exception:
            until = now
    else:
        until = now
    return since, until


def _sql_ym(expr: str) -> str:
    return f"to_char({expr}, 'YYYY-MM')"


def _sql_mins(a_expr: str, b_expr: str) -> str:
    # En PostgreSQL restar dos DATE devuelve integer (días), lo que rompe EXTRACT(EPOCH ...).
    # Casteamos ambos operandos a timestamp para obtener un intervalo y medir en minutos.
    return f"EXTRACT(EPOCH FROM ({b_expr}::timestamp - {a_expr}::timestamp)) / 60.0"


def _sql_now_minus_days(days: int) -> str:
    return f"NOW() - INTERVAL '{int(days)} day'"


def _filters_join_where(req):
    joins = []
    wh = []
    params = []
    t_id = req.GET.get("tecnico_id")
    m_id = req.GET.get("marca_id")
    tipo = (req.GET.get("tipo_equipo") or "").strip()
    # Always apply global cutoff by ingreso date
    try:
        params.append(_cutoff_ts())
        wh.append(" i.fecha_ingreso >= %s ")
    except Exception:
        pass
    if t_id:
        try:
            params.append(int(t_id))
            wh.append(" i.asignado_a = %s ")
        except Exception:
            pass
    if m_id or tipo:
        joins.append(" JOIN devices d ON d.id=i.device_id ")
        joins.append(" LEFT JOIN models m ON m.id=d.model_id ")
    if m_id:
        try:
            params.append(int(m_id))
            wh.append(" d.marca_id = %s ")
        except Exception:
            pass
    if tipo:
        params.append(tipo)
        wh.append(" UPPER(TRIM(m.tipo_equipo)) = UPPER(TRIM(%s)) ")
    join_sql = "".join(joins)
    where_sql = (" AND " + " AND ".join(wh)) if wh else ""
    return join_sql, where_sql, params


def _quote_total_join(alias="qt"):
    return (
        "LEFT JOIN (SELECT quote_id, ROUND(SUM(qi.qty*qi.precio_u),2) AS subtotal_calc "
        "FROM quote_items qi GROUP BY quote_id) "
        f"{alias} ON {alias}.quote_id=q.id\n"
    )


def _quote_total_expr(alias="qt"):
    return (
        f"COALESCE(q.total, COALESCE(q.subtotal,0) + COALESCE(q.iva_21,0), "
        f"ROUND({alias}.subtotal_calc * 1.21, 2), 0)"
    )


def _presu_audit_meta():
    if getattr(connection, "vendor", "") == "postgresql":
        join_sql = (
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS presu_ts "
            "FROM audit.change_log "
            "WHERE table_name='ingresos' AND column_name='presupuesto_estado' AND new_value='presupuestado' "
            "GROUP BY ingreso_id) presu_log ON presu_log.ingreso_id=i.id\n"
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS dec_ts "
            "FROM audit.change_log "
            "WHERE table_name='ingresos' AND column_name='presupuesto_estado' AND new_value IN ('aprobado','rechazado') "
            "GROUP BY ingreso_id) dec_log ON dec_log.ingreso_id=i.id\n"
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS noap_ts "
            "FROM audit.change_log "
            "WHERE table_name='ingresos' AND column_name='presupuesto_estado' AND new_value='no_aplica' "
            "GROUP BY ingreso_id) noap_log ON noap_log.ingreso_id=i.id\n"
        )
        return {
            "join": join_sql,
            "presu_ts_expr": "COALESCE(presu_log.presu_ts, q.fecha_emitido)",
            "dec_ts_expr": "COALESCE(dec_log.dec_ts, q.fecha_aprobado)",
            "noap_filter": " AND noap_log.noap_ts IS NULL",
        }
    return {
        "join": "",
        "presu_ts_expr": "q.fecha_emitido",
        "dec_ts_expr": "q.fecha_aprobado",
        "noap_filter": " AND i.presupuesto_estado <> 'no_aplica'",
    }


def _pctiles(values, percent_list=(50, 75, 90, 95)):
    arr = sorted([float(v) for v in values if v is not None])
    n = len(arr)
    out = {f"p{p}": None for p in percent_list}
    if n == 0:
        return out | {"avg": None, "count": 0}
    def pct(p):
        if n == 1:
            return arr[0]
        k = (p/100.0) * (n - 1)
        f = int(k)
        c = min(f + 1, n - 1)
        if f == c:
            return arr[f]
        d0 = arr[f] * (c - k)
        d1 = arr[c] * (k - f)
        return d0 + d1
    out.update({f"p{p}": pct(p) for p in percent_list})
    out["avg"] = sum(arr) / n
    out["count"] = n
    return out


ACTIVITY_TYPE_LABELS = {
    "apertura_hoja": "Apertura de hoja",
    "apertura_presupuesto": "Apertura de presupuesto",
    "apertura_cola": "Apertura de cola",
    "apertura_historico": "Apertura de histórico",
    "apertura_repuestos": "Apertura de repuestos",
    "apertura_movimientos": "Apertura de movimientos",
    "apertura_equipos": "Apertura de equipos",
    "cambio_estado": "Cambio de estado",
    "evento_estado": "Evento de estado",
    "asignacion_tecnico": "Asignación de técnico",
    "edicion_diagnostico": "Edición de diagnóstico",
    "edicion_comentarios": "Edición de comentarios",
    "edicion_presupuesto": "Edición de presupuesto",
    "cambio_presupuesto_estado": "Cambio de presupuesto",
    "cambio_ubicacion": "Cambio de ubicación",
    "movimiento_repuesto": "Movimiento de repuesto",
    "derivacion": "Derivación",
    "devolucion_derivacion": "Devolución de derivación",
    "edicion_equipo": "Edición de equipo",
    "edicion_accesorios": "Edición de accesorios",
    "edicion_registro": "Edición de registro",
}

ACTIVITY_FIELD_LABELS = {
    "estado": "Estado",
    "asignado_a": "Técnico asignado",
    "ubicacion_id": "Ubicación",
    "descripcion_problema": "Descripción del problema",
    "trabajos_realizados": "Trabajos realizados",
    "informe_preliminar": "Informe preliminar",
    "comentarios": "Comentarios",
    "resolucion": "Resolución",
    "presupuesto_estado": "Estado de presupuesto",
    "total": "Total",
    "subtotal": "Subtotal",
    "iva_21": "IVA",
    "precio_u": "Precio unitario",
    "qty": "Cantidad",
    "descripcion": "Descripción",
    "fecha_servicio": "Fecha de servicio",
    "fecha_emitido": "Fecha emitido",
    "fecha_aprobado": "Fecha aprobado",
    "fecha_entrega": "Fecha de entrega",
}

ACTIVITY_TABLE_LABELS = {
    "ingresos": "Ingreso",
    "devices": "Equipo",
    "quotes": "Presupuesto",
    "quote_items": "Ítem de presupuesto",
    "ingreso_accesorios": "Accesorio",
    "ingreso_alquiler_accesorios": "Accesorio de alquiler",
    "catalogo_repuestos": "Repuesto",
}

ACTIVITY_STATE_LABELS = {
    "ingresado": "Ingresado",
    "asignado": "Asignado",
    "diagnosticado": "Diagnosticado",
    "presupuestado": "Presupuestado",
    "reparar": "Reparar",
    "controlado_sin_defecto": "Controlado sin defecto",
    "reparado": "Reparado",
    "liberado": "Liberado",
    "entregado": "Entregado",
    "baja": "Baja",
    "derivado": "Derivado",
    "alquilado": "Alquilado",
}

ACTIVITY_PRESUPUESTO_STATE_LABELS = {
    "pendiente": "Pendiente",
    "presupuestado": "Presupuestado",
    "aprobado": "Aprobado",
    "rechazado": "Rechazado",
    "no_aplica": "No aplica",
}

ACTIVITY_REPUESTO_MOVIMIENTO_LABELS = {
    "ingreso_compra": "Ingreso por compra",
    "egreso_aprobado": "Egreso por presupuesto aprobado",
    "ingreso_anulacion_aprobado": "Ingreso por anulación de presupuesto",
    "ajuste": "Ajuste manual",
}

ACTIVITY_DIAG_FIELDS = {
    "descripcion_problema",
    "trabajos_realizados",
    "informe_preliminar",
    "resolucion",
    "fecha_servicio",
}

ACTIVITY_COMMENT_FIELDS = {"comentarios"}
ACTIVITY_STATE_EVENT_TYPES = {"cambio_estado", "evento_estado", "derivacion", "devolucion_derivacion"}
ACTIVITY_ACTOR_ROLES = ("tecnico", "jefe", "jefe_veedor")
ACTIVITY_CHANGE_TABLES = (
    "ingresos",
    "devices",
    "quotes",
    "quote_items",
    "ingreso_accesorios",
    "ingreso_alquiler_accesorios",
    "catalogo_repuestos",
)


def _table_exists_local(table_name: str, schema_name: str | None = None) -> bool:
    try:
        if connection.vendor == "postgresql":
            params = [table_name]
            sql = (
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = %s
                 LIMIT 1
                """
            )
            if schema_name:
                sql = (
                    """
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_schema = %s
                       AND table_name = %s
                     LIMIT 1
                    """
                )
                params = [schema_name, table_name]
            row = q(sql, params, one=True)
            return bool(row)
        row = q(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_name = %s
             LIMIT 1
            """,
            [table_name],
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _request_role(request) -> str:
    return (
        getattr(getattr(request, "user", None), "rol", None)
        or getattr(request, "user_role", None)
        or ""
    ).strip().lower()


def _require_metricas_activity_viewer(request):
    require_permission(request, "page.metrics")
    if _request_role(request) not in {"jefe", "jefe_veedor"}:
        raise PermissionDenied("Solo jefe y jefe veedor pueden ver la actividad de técnicos.")


def _parse_activity_range_params(request):
    tz = timezone.get_current_timezone()
    now = timezone.now()
    today = timezone.localtime(now, tz).date()
    preset = (request.GET.get("preset") or "").strip().lower()
    from_s = request.GET.get("from") or request.GET.get("desde")
    to_s = request.GET.get("to") or request.GET.get("hasta")

    if preset == "today":
        since = timezone.make_aware(dt.datetime.combine(today, dt.time.min), tz)
        until = now
    elif preset == "yesterday":
        target = today - dt.timedelta(days=1)
        since = timezone.make_aware(dt.datetime.combine(target, dt.time.min), tz)
        until = timezone.make_aware(dt.datetime.combine(target, dt.time.max), tz)
    elif preset == "week" or (not preset and not from_s and not to_s):
        start = today - dt.timedelta(days=today.weekday())
        since = timezone.make_aware(dt.datetime.combine(start, dt.time.min), tz)
        until = now
    else:
        since, until = _parse_range_params(request)

    try:
        cutoff = _cutoff_ts()
        if since < cutoff:
            since = cutoff
    except Exception:
        pass
    return since, until


def _clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _truncate_text(value, max_len=180):
    text = _clean_text(value)
    if not text:
        return "-"
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


def _display_value(value):
    text = _clean_text(value)
    return text or "-"


def _humanize_key(value):
    text = _clean_text(value)
    if not text:
        return "-"
    base = text.replace("_", " ")
    return base[:1].upper() + base[1:]


def _activity_type_label(value):
    return ACTIVITY_TYPE_LABELS.get(value, _humanize_key(value))


def _activity_field_label(value):
    raw = _clean_text(value).lower()
    return ACTIVITY_FIELD_LABELS.get(raw, _humanize_key(raw))


def _activity_table_label(value):
    raw = _clean_text(value).lower()
    return ACTIVITY_TABLE_LABELS.get(raw, _humanize_key(raw))


def _activity_state_label(value):
    raw = _clean_text(value).lower()
    if not raw:
        return "-"
    return ACTIVITY_STATE_LABELS.get(raw, _humanize_key(raw))


def _activity_presupuesto_state_label(value):
    raw = _clean_text(value).lower()
    if not raw:
        return "-"
    return ACTIVITY_PRESUPUESTO_STATE_LABELS.get(raw, _humanize_key(raw))


def _activity_decimal_label(value):
    if value is None:
        return "-"
    try:
        number = float(value)
    except Exception:
        return _display_value(value)
    if abs(number - int(number)) < 0.000001:
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _activity_ts_sort_value(item):
    ts = item.get("ts") if isinstance(item, dict) else None
    if hasattr(ts, "timestamp"):
        try:
            return float(ts.timestamp())
        except Exception:
            return 0.0
    return 0.0


def _activity_id_sort_value(item):
    try:
        return int(item.get("_id") or item.get("id") or 0)
    except Exception:
        return 0


def _sql_in_clause(values):
    seq = [value for value in values if value is not None]
    if not seq:
        return None, []
    return ",".join(["%s"] * len(seq)), seq


def _load_ingreso_context(ingreso_ids):
    placeholders, params = _sql_in_clause(sorted(set(ingreso_ids or [])))
    if not placeholders:
        return {}
    try:
        rows = q(
            f"""
            SELECT
              i.id AS ingreso_id,
              c.razon_social AS cliente,
              COALESCE(b.nombre, '') AS marca,
              COALESCE(m.nombre, '') AS modelo,
              COALESCE(m.tipo_equipo, '') AS tipo_equipo,
              COALESCE(d.numero_serie, '') AS numero_serie,
              COALESCE(d.numero_interno, '') AS numero_interno
            FROM ingresos i
            LEFT JOIN devices d ON d.id = i.device_id
            LEFT JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            WHERE i.id IN ({placeholders})
            """,
            params,
        ) or []
    except Exception:
        return {}

    out = {}
    for row in rows:
        rid = row.get("ingreso_id")
        if rid is None:
            continue
        tipo = _clean_text(row.get("tipo_equipo"))
        marca = _clean_text(row.get("marca"))
        modelo = _clean_text(row.get("modelo"))
        parts = [part for part in [tipo, marca, modelo] if part]
        row["equipo_label"] = " | ".join(parts)
        row["os"] = os_label(rid)
        out[int(rid)] = row
    return out


def _load_repuesto_context(repuesto_ids):
    placeholders, params = _sql_in_clause(sorted(set(repuesto_ids or [])))
    if not placeholders:
        return {}
    try:
        rows = q(
            f"""
            SELECT id AS repuesto_id, COALESCE(codigo, '') AS codigo, COALESCE(nombre, '') AS nombre
            FROM catalogo_repuestos
            WHERE id IN ({placeholders})
            """,
            params,
        ) or []
    except Exception:
        return {}
    return {
        int(row["repuesto_id"]): row
        for row in rows
        if row.get("repuesto_id") is not None
    }


def _ingreso_os_suffix(context):
    os_value = _clean_text((context or {}).get("os"))
    return f" de {os_value}" if os_value else ""


def _ingreso_os_target_suffix(context):
    os_value = _clean_text((context or {}).get("os"))
    return f" para {os_value}" if os_value else ""


def _activity_ingreso_reference(context):
    if not context:
        return ""
    parts = [context.get("os"), context.get("equipo_label")]
    return " | ".join([_clean_text(part) for part in parts if _clean_text(part)])


def _activity_make_row(
    *,
    source,
    raw_row,
    activity_type,
    title,
    detail,
    ingreso_context=None,
    repuesto_context=None,
    meta=None,
):
    ingreso_id = raw_row.get("ingreso_id")
    repuesto_id = raw_row.get("repuesto_id")
    ingreso_ref = _activity_ingreso_reference(ingreso_context or {})
    repuesto_codigo = _clean_text((repuesto_context or {}).get("codigo") or raw_row.get("codigo") or raw_row.get("repuesto_codigo"))
    repuesto_nombre = _clean_text((repuesto_context or {}).get("nombre") or raw_row.get("repuesto_nombre") or raw_row.get("nombre"))
    repuesto_ref = " - ".join([part for part in [repuesto_codigo, repuesto_nombre] if part])
    reference = " | ".join([part for part in [ingreso_ref, repuesto_ref] if part]) or None
    return {
        "_id": raw_row.get("_id") or raw_row.get("id"),
        "ts": raw_row.get("ts"),
        "tecnico_id": raw_row.get("user_id"),
        "tecnico_nombre": _clean_text(raw_row.get("user_nombre")) or _display_value(raw_row.get("user_id")),
        "source": source,
        "activity_type": activity_type,
        "activity_type_label": _activity_type_label(activity_type),
        "title": title,
        "detail": detail,
        "ingreso_id": ingreso_id,
        "os": (ingreso_context or {}).get("os") if ingreso_id else None,
        "ingreso_ref": ingreso_ref or None,
        "repuesto_id": repuesto_id,
        "repuesto_codigo": repuesto_codigo or None,
        "repuesto_nombre": repuesto_nombre or None,
        "reference": reference,
        "path": raw_row.get("path"),
        "meta": meta or {},
    }


def _normalize_change_log_row(row, ingreso_context_map, repuesto_context_map):
    table_name = _clean_text(row.get("table_name")).lower()
    column_name = _clean_text(row.get("column_name")).lower()
    ingreso_context = ingreso_context_map.get(int(row["ingreso_id"])) if row.get("ingreso_id") is not None else None
    compact_count = int(row.get("_compact_count") or 1)

    if table_name == "ingresos" and column_name == "estado":
        old_label = _activity_state_label(row.get("old_value"))
        new_label = _activity_state_label(row.get("new_value"))
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="cambio_estado",
            title=f"Cambió el estado{_ingreso_os_suffix(ingreso_context)}",
            detail=f"{old_label} -> {new_label}",
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "state_to": _clean_text(row.get("new_value")).lower(),
                "compact_count": compact_count,
            },
        )

    if table_name == "ingresos" and column_name == "asignado_a":
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="asignacion_tecnico",
            title=f"Actualizó la asignación{_ingreso_os_suffix(ingreso_context)}",
            detail=f"{_display_value(row.get('old_value'))} -> {_display_value(row.get('new_value'))}",
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "compact_count": compact_count,
            },
        )

    if table_name == "ingresos" and column_name == "ubicacion_id":
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="cambio_ubicacion",
            title=f"Cambió la ubicación{_ingreso_os_suffix(ingreso_context)}",
            detail=f"{_display_value(row.get('old_value'))} -> {_display_value(row.get('new_value'))}",
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "compact_count": compact_count,
            },
        )

    if column_name == "presupuesto_estado":
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="cambio_presupuesto_estado",
            title=f"Actualizó el estado del presupuesto{_ingreso_os_suffix(ingreso_context)}",
            detail=(
                f"{_activity_presupuesto_state_label(row.get('old_value'))} -> "
                f"{_activity_presupuesto_state_label(row.get('new_value'))}"
            ),
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "compact_count": compact_count,
            },
        )

    if table_name in {"quotes", "quote_items"}:
        detail = f"{_activity_field_label(column_name)}: {_display_value(row.get('old_value'))} -> {_display_value(row.get('new_value'))}"
        if compact_count > 1:
            detail = f"{detail} ({compact_count} cambios compactados)"
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="edicion_presupuesto",
            title=f"Editó el presupuesto{_ingreso_os_suffix(ingreso_context)}",
            detail=_truncate_text(detail),
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "compact_count": compact_count,
            },
        )

    if column_name in ACTIVITY_DIAG_FIELDS:
        detail = f"{_activity_field_label(column_name)}: {_truncate_text(row.get('new_value'))}"
        if compact_count > 1:
            detail = f"{detail} ({compact_count} cambios compactados)"
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="edicion_diagnostico",
            title=f"Actualizó diagnóstico/trabajos{_ingreso_os_suffix(ingreso_context)}",
            detail=detail,
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "compact_count": compact_count,
            },
        )

    if column_name in ACTIVITY_COMMENT_FIELDS:
        detail = f"{_activity_field_label(column_name)}: {_truncate_text(row.get('new_value'))}"
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="edicion_comentarios",
            title=f"Actualizó comentarios{_ingreso_os_suffix(ingreso_context)}",
            detail=detail,
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "compact_count": compact_count,
            },
        )

    if table_name in {"ingreso_accesorios", "ingreso_alquiler_accesorios"}:
        detail = f"{_activity_field_label(column_name)}: {_display_value(row.get('old_value'))} -> {_display_value(row.get('new_value'))}"
        return _activity_make_row(
            source="change_log",
            raw_row=row,
            activity_type="edicion_accesorios",
            title=f"Actualizó accesorios{_ingreso_os_suffix(ingreso_context)}",
            detail=_truncate_text(detail),
            ingreso_context=ingreso_context,
            repuesto_context=None,
            meta={
                "table_name": table_name,
                "column_name": column_name,
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "compact_count": compact_count,
            },
        )

    activity_type = "edicion_equipo" if table_name == "devices" else "edicion_registro"
    detail = f"{_activity_field_label(column_name)}: {_display_value(row.get('old_value'))} -> {_display_value(row.get('new_value'))}"
    if compact_count > 1:
        detail = f"{detail} ({compact_count} cambios compactados)"
    return _activity_make_row(
        source="change_log",
        raw_row=row,
        activity_type=activity_type,
        title=f"Editó {_activity_table_label(table_name).lower()}",
        detail=_truncate_text(detail),
        ingreso_context=ingreso_context,
        repuesto_context=repuesto_context_map.get(int(row["repuesto_id"])) if row.get("repuesto_id") is not None else None,
        meta={
            "table_name": table_name,
            "column_name": column_name,
            "old_value": row.get("old_value"),
            "new_value": row.get("new_value"),
            "compact_count": compact_count,
        },
    )


def _normalize_event_row(row, ingreso_context_map):
    ingreso_context = ingreso_context_map.get(int(row["ingreso_id"])) if row.get("ingreso_id") is not None else None
    state_to = _clean_text(row.get("a_estado")).lower()
    state_from = _clean_text(row.get("de_estado")).lower()
    comment = _clean_text(row.get("comentario"))
    comment_key = comment.lower()

    activity_type = "evento_estado"
    title = f"Registró evento de {_activity_state_label(state_to)}"
    if state_to == "derivado":
        activity_type = "derivacion"
        title = "Registró derivación externa"
    elif "devolucion de externo" in comment_key or ("devol" in comment_key and state_to == "ingresado"):
        activity_type = "devolucion_derivacion"
        title = "Registró devolución de derivación"
    elif state_to:
        title = f"Marcó {ingreso_context.get('os') if ingreso_context else 'la OS'} como {_activity_state_label(state_to)}"

    detail = comment or f"{_activity_state_label(state_from)} -> {_activity_state_label(state_to)}"
    return _activity_make_row(
        source="ingreso_event",
        raw_row=row,
        activity_type=activity_type,
        title=title,
        detail=_truncate_text(detail),
        ingreso_context=ingreso_context,
        repuesto_context=None,
        meta={
            "state_from": state_from or None,
            "state_to": state_to or None,
            "comment": comment or None,
        },
    )


def _normalize_repuesto_row(row, ingreso_context_map, repuesto_context_map):
    ingreso_context = ingreso_context_map.get(int(row["ingreso_id"])) if row.get("ingreso_id") is not None else None
    repuesto_context = repuesto_context_map.get(int(row["repuesto_id"])) if row.get("repuesto_id") is not None else None
    movement_type = _clean_text(row.get("tipo")).lower()
    movement_label = ACTIVITY_REPUESTO_MOVIMIENTO_LABELS.get(movement_type, _humanize_key(movement_type))
    qty = _activity_decimal_label(row.get("qty"))
    stock_prev = _activity_decimal_label(row.get("stock_prev"))
    stock_new = _activity_decimal_label(row.get("stock_new"))
    repuesto_ref = " - ".join([part for part in [_clean_text((repuesto_context or {}).get("codigo")), _clean_text((repuesto_context or {}).get("nombre"))] if part])
    detail_parts = [repuesto_ref or _clean_text(row.get("repuesto_nombre")) or "Repuesto", movement_label, f"Cantidad {qty}", f"Stock {stock_prev} -> {stock_new}"]
    if _clean_text(row.get("nota")):
        detail_parts.append(_truncate_text(row.get("nota"), 80))
    return _activity_make_row(
        source="repuestos_movimientos",
        raw_row=row,
        activity_type="movimiento_repuesto",
        title=f"Registró movimiento de repuesto{_ingreso_os_target_suffix(ingreso_context)}",
        detail=" | ".join([part for part in detail_parts if part]),
        ingreso_context=ingreso_context,
        repuesto_context=repuesto_context,
        meta={
            "movement_type": movement_type or None,
            "qty": row.get("qty"),
            "stock_prev": row.get("stock_prev"),
            "stock_new": row.get("stock_new"),
            "ref_tipo": row.get("ref_tipo"),
            "ref_id": row.get("ref_id"),
        },
    )


def _normalize_audit_log_row(row, ingreso_context_map):
    info = classify_read_path(row.get("path"))
    if not info:
        return None
    ingreso_id = info.get("ingreso_id")
    if ingreso_id is not None:
        row["ingreso_id"] = ingreso_id
    ingreso_context = ingreso_context_map.get(int(ingreso_id)) if ingreso_id is not None and int(ingreso_id) in ingreso_context_map else None
    detail = row.get("path")
    if ingreso_context and ingreso_context.get("equipo_label"):
        detail = f"{detail} | {ingreso_context.get('equipo_label')}"
    return _activity_make_row(
        source="audit_log",
        raw_row=row,
        activity_type=info["activity_type"],
        title=info["title"],
        detail=detail,
        ingreso_context=ingreso_context,
        repuesto_context=None,
        meta={
            "method": row.get("method"),
            "status_code": row.get("status_code"),
        },
    )


def _dedupe_activity_timeline(rows):
    if not rows:
        return rows

    kept = []
    for row in sorted(rows, key=lambda item: (_activity_ts_sort_value(item), _activity_id_sort_value(item)), reverse=True):
        if row.get("source") == "audit_log":
            duplicated_open = any(
                other.get("source") == "audit_log"
                and other.get("tecnico_id") == row.get("tecnico_id")
                and other.get("activity_type") == row.get("activity_type")
                and other.get("path") == row.get("path")
                and other.get("ingreso_id") == row.get("ingreso_id")
                and abs(_activity_ts_sort_value(other) - _activity_ts_sort_value(row)) <= 60
                for other in kept
            )
            if duplicated_open:
                continue

        if row.get("source") == "ingreso_event" and row.get("activity_type") in ACTIVITY_STATE_EVENT_TYPES:
            state_to = _clean_text((row.get("meta") or {}).get("state_to")).lower()
            duplicated_state = any(
                other.get("source") == "change_log"
                and other.get("tecnico_id") == row.get("tecnico_id")
                and other.get("ingreso_id") == row.get("ingreso_id")
                and _clean_text((other.get("meta") or {}).get("state_to")).lower() == state_to
                and abs(_activity_ts_sort_value(other) - _activity_ts_sort_value(row)) <= 120
                for other in kept
            )
            if duplicated_state:
                continue

        kept.append(row)

    kept.sort(key=lambda item: (_activity_ts_sort_value(item), _activity_id_sort_value(item)), reverse=True)
    return kept


def _build_activity_summary(rows):
    by_tecnico = {}
    by_type = defaultdict(int)
    for row in rows:
        tecnico_id = row.get("tecnico_id")
        tecnico_key = tecnico_id if tecnico_id is not None else f"anon:{row.get('tecnico_nombre')}"
        current = by_tecnico.setdefault(
            tecnico_key,
            {
                "tecnico_id": tecnico_id,
                "tecnico_nombre": row.get("tecnico_nombre"),
                "total": 0,
                "aperturas": 0,
                "movimientos_repuestos": 0,
            },
        )
        current["total"] += 1
        if str(row.get("activity_type") or "").startswith("apertura_"):
            current["aperturas"] += 1
        if row.get("activity_type") == "movimiento_repuesto":
            current["movimientos_repuestos"] += 1
        by_type[row.get("activity_type")] += 1

    by_tecnico_rows = sorted(
        by_tecnico.values(),
        key=lambda item: (-int(item.get("total") or 0), _clean_text(item.get("tecnico_nombre")).lower()),
    )
    by_type_rows = [
        {
            "activity_type": activity_type,
            "label": _activity_type_label(activity_type),
            "count": count,
        }
        for activity_type, count in sorted(by_type.items(), key=lambda item: (-item[1], _activity_type_label(item[0]).lower()))
    ]
    return {
        "total": len(rows),
        "unique_tecnicos": len(by_tecnico_rows),
        "by_tecnico": by_tecnico_rows,
        "by_activity_type": by_type_rows,
        "activity_types": by_type_rows,
    }


def _load_activity_history_transforms():
    try:
        from .ingresos_views import (
            _compact_history_rows,
            _drop_mirrored_device_history_rows,
            _resolve_assigned_history_values,
            _resolve_location_history_values,
        )
    except Exception:
        return {
            "compact": lambda rows: rows,
            "drop_mirrored": lambda rows: rows,
            "resolve_assigned": lambda rows: rows,
            "resolve_location": lambda rows: rows,
        }
    return {
        "compact": _compact_history_rows,
        "drop_mirrored": _drop_mirrored_device_history_rows,
        "resolve_assigned": _resolve_assigned_history_values,
        "resolve_location": _resolve_location_history_values,
    }


def _load_change_log_activity_rows(since, until, tecnico_id=None):
    if connection.vendor != "postgresql" or not _table_exists_local("change_log", "audit"):
        return []

    table_clause, table_params = _sql_in_clause(ACTIVITY_CHANGE_TABLES)
    role_clause, role_params = _sql_in_clause(ACTIVITY_ACTOR_ROLES)
    if not table_clause or not role_clause:
        return []

    joins = []
    ingreso_exprs = ["cl.ingreso_id"]
    repuesto_expr = (
        "CASE WHEN LOWER(cl.table_name) = 'catalogo_repuestos' THEN cl.record_id ELSE NULL END"
    )

    has_quotes = _table_exists_local("quotes")
    has_quote_items = _table_exists_local("quote_items")
    has_ingreso_acc = _table_exists_local("ingreso_accesorios")
    has_ingreso_alq_acc = _table_exists_local("ingreso_alquiler_accesorios")

    if has_quotes:
        joins.append(
            "LEFT JOIN quotes q ON LOWER(cl.table_name) = 'quotes' AND q.id = cl.record_id"
        )
        ingreso_exprs.append("q.ingreso_id")
    if has_quote_items:
        joins.append(
            "LEFT JOIN quote_items qi ON LOWER(cl.table_name) = 'quote_items' AND qi.id = cl.record_id"
        )
        if has_quotes:
            joins.append("LEFT JOIN quotes qq ON qq.id = qi.quote_id")
            ingreso_exprs.append("qq.ingreso_id")
        repuesto_expr = (
            "CASE "
            "WHEN LOWER(cl.table_name) = 'quote_items' THEN qi.repuesto_id "
            "WHEN LOWER(cl.table_name) = 'catalogo_repuestos' THEN cl.record_id "
            "ELSE NULL END"
        )
    if has_ingreso_acc:
        joins.append(
            "LEFT JOIN ingreso_accesorios ia ON LOWER(cl.table_name) = 'ingreso_accesorios' AND ia.id = cl.record_id"
        )
        ingreso_exprs.append("ia.ingreso_id")
    if has_ingreso_alq_acc:
        joins.append(
            "LEFT JOIN ingreso_alquiler_accesorios iala ON LOWER(cl.table_name) = 'ingreso_alquiler_accesorios' AND iala.id = cl.record_id"
        )
        ingreso_exprs.append("iala.ingreso_id")

    tecnico_where = ""
    params = [since, until, *role_params, *table_params]
    if tecnico_id is not None:
        tecnico_where = " AND cl.user_id = %s"
        params.append(tecnico_id)

    rows = q(
        f"""
        SELECT
          cl.id AS _id,
          cl.ts,
          cl.user_id,
          COALESCE(cl.user_role, u.rol, '') AS user_role,
          COALESCE(u.nombre, '') AS user_nombre,
          cl.table_name,
          cl.record_id,
          cl.column_name,
          cl.old_value,
          cl.new_value,
          COALESCE({", ".join(ingreso_exprs)}) AS ingreso_id,
          {repuesto_expr} AS repuesto_id
        FROM audit.change_log cl
        LEFT JOIN users u ON u.id = cl.user_id
        {" ".join(joins)}
        WHERE cl.ts BETWEEN %s AND %s
          AND LOWER(COALESCE(NULLIF(TRIM(cl.user_role), ''), COALESCE(u.rol, ''))) IN ({role_clause})
          AND LOWER(cl.table_name) IN ({table_clause})
          AND LOWER(cl.column_name) NOT IN ('created_at', 'updated_at')
          {tecnico_where}
        ORDER BY cl.ts DESC, cl.id DESC
        """,
        params,
    ) or []

    transforms = _load_activity_history_transforms()
    rows = transforms["compact"](rows)
    rows = transforms["drop_mirrored"](rows)
    rows = transforms["resolve_location"](rows)
    rows = transforms["resolve_assigned"](rows)
    return rows


def _load_ingreso_event_activity_rows(since, until, tecnico_id=None):
    if not _table_exists_local("ingreso_events"):
        return []

    role_clause, role_params = _sql_in_clause(ACTIVITY_ACTOR_ROLES)
    if not role_clause:
        return []

    tecnico_where = ""
    params = [since, until, *role_params]
    if tecnico_id is not None:
        tecnico_where = " AND e.usuario_id = %s"
        params.append(tecnico_id)

    return q(
        f"""
        SELECT
          e.id AS _id,
          e.ts,
          e.usuario_id AS user_id,
          COALESCE(u.rol, '') AS user_role,
          COALESCE(u.nombre, '') AS user_nombre,
          e.ingreso_id,
          e.de_estado,
          e.a_estado,
          COALESCE(e.comentario, '') AS comentario
        FROM ingreso_events e
        LEFT JOIN users u ON u.id = e.usuario_id
        WHERE e.ts BETWEEN %s AND %s
          AND LOWER(COALESCE(u.rol, '')) IN ({role_clause})
          {tecnico_where}
        ORDER BY e.ts DESC, e.id DESC
        """,
        params,
    ) or []


def _load_repuesto_activity_rows(since, until, tecnico_id=None):
    if not _table_exists_local("repuestos_movimientos"):
        return []

    role_clause, role_params = _sql_in_clause(ACTIVITY_ACTOR_ROLES)
    if not role_clause:
        return []

    joins = []
    ingreso_expr = "NULL"
    if _table_exists_local("quotes"):
        joins.append(
            "LEFT JOIN quotes q ON LOWER(COALESCE(rm.ref_tipo, '')) = 'quote' AND q.id = rm.ref_id"
        )
        ingreso_expr = (
            "CASE "
            "WHEN LOWER(COALESCE(rm.ref_tipo, '')) = 'ingreso' THEN rm.ref_id "
            "WHEN LOWER(COALESCE(rm.ref_tipo, '')) = 'quote' THEN q.ingreso_id "
            "ELSE NULL END"
        )

    tecnico_where = ""
    params = [since, until, *role_params]
    if tecnico_id is not None:
        tecnico_where = " AND rm.created_by = %s"
        params.append(tecnico_id)

    return q(
        f"""
        SELECT
          rm.id AS _id,
          rm.created_at AS ts,
          rm.created_by AS user_id,
          COALESCE(u.rol, '') AS user_role,
          COALESCE(u.nombre, '') AS user_nombre,
          rm.repuesto_id,
          rm.tipo,
          rm.qty,
          rm.stock_prev,
          rm.stock_new,
          rm.ref_tipo,
          rm.ref_id,
          COALESCE(rm.nota, '') AS nota,
          {ingreso_expr} AS ingreso_id
        FROM repuestos_movimientos rm
        LEFT JOIN users u ON u.id = rm.created_by
        {" ".join(joins)}
        WHERE rm.created_at BETWEEN %s AND %s
          AND LOWER(COALESCE(u.rol, '')) IN ({role_clause})
          {tecnico_where}
        ORDER BY rm.created_at DESC, rm.id DESC
        """,
        params,
    ) or []


def _load_audit_log_activity_rows(since, until, tecnico_id=None):
    if not _table_exists_local("audit_log"):
        return []

    role_clause, role_params = _sql_in_clause(ACTIVITY_ACTOR_ROLES)
    if not role_clause:
        return []

    tecnico_where = ""
    params = [since, until, *role_params]
    if tecnico_id is not None:
        tecnico_where = " AND al.user_id = %s"
        params.append(tecnico_id)

    return q(
        f"""
        SELECT
          al.id AS _id,
          al.ts,
          al.user_id,
          COALESCE(al.role, u.rol, '') AS user_role,
          COALESCE(u.nombre, '') AS user_nombre,
          al.method,
          al.path,
          al.status_code
        FROM audit_log al
        LEFT JOIN users u ON u.id = al.user_id
        WHERE al.ts BETWEEN %s AND %s
          AND UPPER(COALESCE(al.method, '')) = 'GET'
          AND COALESCE(al.status_code, 0) BETWEEN 200 AND 399
          AND LOWER(COALESCE(NULLIF(TRIM(al.role), ''), COALESCE(u.rol, ''))) IN ({role_clause})
          {tecnico_where}
        ORDER BY al.ts DESC, al.id DESC
        """,
        params,
    ) or []


def _filter_activity_rows(rows, activity_type=None):
    target = _clean_text(activity_type).lower()
    if not target:
        return list(rows or [])
    return [row for row in (rows or []) if _clean_text(row.get("activity_type")).lower() == target]


class MetricasConfigView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe"])  # solo Jefe
        work_start = int(request.GET.get("work_start") or WORKDAY_START_HOUR)
        work_end = int(request.GET.get("work_end") or WORKDAY_END_HOUR)
        workdays = list(WORKDAYS)
        cfg = {
            "holidays_country": "AR",
            "workday_start_hour": work_start,
            "workday_end_hour": work_end,
            "workdays": workdays,
            "sla_excluir_derivados_default": True,
            "source": "env+nager",
        }
        return Response(cfg)


class FeriadosView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe"])  # solo Jefe
        try:
            rows = q("SELECT fecha, nombre FROM feriados ORDER BY fecha ASC") or []
        except Exception:
            rows = []
        return Response(rows)
    def post(self, request):
        require_roles(request, ["jefe"])  # solo Jefe
        d = request.data or {}
        fecha = (d.get("fecha") or "").strip()
        nombre = (d.get("nombre") or "").strip() or "Feriado"
        if not fecha:
            return Response({"detail": "fecha requerida (YYYY-MM-DD)"}, status=400)
        try:
            if cache is not None:  # silence linter
                pass
            if hasattr(connection, 'vendor') and connection.vendor == 'postgresql':
                q("INSERT INTO feriados (fecha, nombre) VALUES (%s, %s) ON CONFLICT (fecha) DO UPDATE SET nombre=EXCLUDED.nombre", [fecha, nombre])
            else:
                q("INSERT INTO feriados (fecha, nombre) VALUES (%s, %s) ON DUPLICATE KEY UPDATE nombre=VALUES(nombre)", [fecha, nombre])
            cache.clear()
            return Response({"ok": True})
        except Exception as ex:
            return Response({"detail": str(ex)}, status=400)
    def delete(self, request):
        require_roles(request, ["jefe"])  # solo Jefe
        fecha = (request.GET.get("fecha") or "").strip()
        if not fecha:
            return Response({"detail": "fecha requerida (YYYY-MM-DD)"}, status=400)
        try:
            q("DELETE FROM feriados WHERE fecha=%s", [fecha])
            cache.clear()
        except Exception:
            pass
        return Response({"ok": True})


class MetricasSeriesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe"])  # solo Jefe
        since, until = _parse_range_params(request)
        join_dm, where_i, where_params = _filters_join_where(request)
        qt_join = _quote_total_join()
        qt_expr = _quote_total_expr()

        # buckets mensuales
        months = []
        y, m = since.year, since.month
        while y < until.year or (y == until.year and m <= until.month):
            months.append(f"{y:04d}-{m:02d}")
            if m == 12:
                y += 1; m = 1
            else:
                m += 1
        data = {k: {
            "entregados": 0,
            "_mttr_sum_min": 0,
            "_mttr_n": 0,
            "_mttr_vals": [],
            "sla_diag_total": 0,
            "sla_diag_dentro": 0,
            "aprob_emitidos": 0,
            "aprob_rechazados": 0,
            "aprob_aprobados": 0,
            "t_emitir_min_sum": 0,
            "t_emitir_n": 0,
            "t_emitir_hours_vals": [],
            "t_aprobar_min_sum": 0,
            "t_aprobar_n": 0,
            "t_aprobar_hours_vals": [],
            "tat_days_vals": [],
        } for k in months}

        # Entregados por mes
        ym_ent = _sql_ym('i.fecha_entrega')
        entreg_sql = (
            f"SELECT {ym_ent} AS ym, COUNT(*) AS c\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "WHERE i.fecha_entrega BETWEEN %s AND %s"
            f"{where_i}\n"
            "GROUP BY ym"
        )
        entreg_rows = q(entreg_sql, [since, until, *where_params]) or []
        for r in entreg_rows:
            k = r.get("ym")
            if k in data:
                data[k]["entregados"] = r.get("c", 0)

        # MTTR por mes (por reparado)
        ym_rep = _sql_ym('d.reparado_ts')
        rep_rows = q((
            f"SELECT {ym_rep} AS ym, r.reparar_ts, d.reparado_ts\n"
            "FROM (SELECT ingreso_id, MIN(ts) AS reparar_ts FROM ingreso_events WHERE a_estado='reparar' GROUP BY ingreso_id) r\n"
            "JOIN (SELECT ingreso_id, MIN(ts) AS reparado_ts FROM ingreso_events WHERE a_estado='reparado' GROUP BY ingreso_id) d\n"
            "  ON d.ingreso_id=r.ingreso_id\n"
            "JOIN ingresos i ON i.id=d.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE d.reparado_ts BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        for r in rep_rows:
            k = r.get("ym")
            if k in data and r.get("reparar_ts") and r.get("reparado_ts"):
                mins = max(0, int((r["reparado_ts"] - r["reparar_ts"]).total_seconds() // 60))
                data[k]["_mttr_sum_min"] += mins
                data[k]["_mttr_n"] += 1
                try:
                    data[k]["_mttr_vals"].append(mins / 60.0 / 24.0)
                except Exception:
                    pass

        # SLA diag por mes
        holi = _holidays_between((since - dt.timedelta(days=7)).date(), (until + dt.timedelta(days=7)).date())
        ym_ing = _sql_ym('i.fecha_ingreso')
        sla_rows = q((
            f"SELECT {ym_ing} AS ym, i.fecha_ingreso, COALESCE(di.ts, i.fecha_servicio, i.fecha_ingreso) AS diag_ts\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS ts FROM ingreso_events WHERE a_estado='diagnosticado' GROUP BY ingreso_id) di\n"
            "  ON di.ingreso_id=i.id\n"
            "WHERE i.fecha_ingreso BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        for r in sla_rows:
            k = r.get("ym")
            if k not in data:
                continue
            fi = r.get("fecha_ingreso"); dtg = r.get("diag_ts")
            if fi and dtg:
                data[k]["sla_diag_total"] += 1
                if business_minutes_between(fi, dtg, holidays=holi) <= 24 * 60:
                    data[k]["sla_diag_dentro"] += 1

        # Presupuestos por mes (emitidos/aprobados y tiempos)
        ym_emit = _sql_ym('q.fecha_emitido')
        emit_rows = q((
            f"SELECT {ym_emit} AS ym, q.fecha_emitido, q.fecha_aprobado, q.ingreso_id, q.estado\n"
            "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.fecha_emitido BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        for r in emit_rows:
            k = r.get("ym")
            if k not in data:
                continue
            data[k]["aprob_emitidos"] += 1
            fei = r.get("fecha_emitido"); fa = r.get("fecha_aprobado")
            # diag -> emitir: buscar diag_ts
            di = q("SELECT COALESCE(MIN(ts), NULL) AS ts FROM ingreso_events WHERE a_estado='diagnosticado' AND ingreso_id=%s", [r.get("ingreso_id")], one=True)
            diag_ts = di and di.get("ts")
            if diag_ts and fei:
                data[k]["t_emitir_min_sum"] += int(max(0, (fei - diag_ts).total_seconds() // 60))
                data[k]["t_emitir_n"] += 1
                try:
                    data[k]["t_emitir_hours_vals"].append(max(0.0, (fei - diag_ts).total_seconds() / 3600.0))
                except Exception:
                    pass
            estado = (r.get("estado") or "").strip().lower()
            if fa and estado == "aprobado":
                data[k]["aprob_aprobados"] += 1
                data[k]["t_aprobar_min_sum"] += int(max(0, (fa - fei).total_seconds() // 60))
                data[k]["t_aprobar_n"] += 1
            elif fa and estado == "rechazado":
                data[k]["aprob_rechazados"] += 1

        # Distribución mensual: emitir->aprobar (horas)
        mins_emit_aprob = _sql_mins('q.fecha_emitido','q.fecha_aprobado')
        aprobar_rows = q((
            f"SELECT {ym_emit} AS ym, {mins_emit_aprob} AS mins\n"
            "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.fecha_emitido BETWEEN %s AND %s "
            "AND q.fecha_aprobado IS NOT NULL "
            "AND q.estado='aprobado'"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        for r in aprobar_rows:
            k = r.get("ym")
            if k in data:
                v = r.get("mins") or 0
                try:
                    v = float(v)
                except Exception:
                    pass
                data[k]["t_aprobar_hours_vals"].append(max(0.0, v / 60.0))

        # TAT (Ingreso -> Entregado) por mes (días calendario)
        tat_rows = q((
            f"SELECT {ym_ent} AS ym, i.fecha_ingreso, i.fecha_entrega AS entregado_ts\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "WHERE i.fecha_entrega BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        for r in tat_rows:
            k = r.get("ym")
            if k not in data:
                continue
            fi = r.get("fecha_ingreso"); et = r.get("entregado_ts")
            if fi and et and et >= fi:
                data[k]["tat_days_vals"].append((et - fi).total_seconds() / 86400.0)

        series = []
        for k in months:
            series.append({
                "period": k,
                "entregados": data[k]["entregados"],
                "mttr_dias": (data[k]["_mttr_sum_min"] / 60 / 24 / data[k]["_mttr_n"]) if data[k]["_mttr_n"] else None,
                "mttr_percentiles": _pctiles(data[k]["_mttr_vals"], (25,50,75,90,95)) if data[k]["_mttr_vals"] else {"p25": None, "p50": None, "p75": None, "p90": None, "p95": None, "avg": None, "count": 0},
                "sla_diag_24h": {
                    "total": data[k]["sla_diag_total"],
                    "dentro": data[k]["sla_diag_dentro"],
                    "cumplimiento": (data[k]["sla_diag_dentro"] / data[k]["sla_diag_total"]) if data[k]["sla_diag_total"] else 0.0,
                },
                "aprob_presupuestos": {
                    "emitidos": data[k]["aprob_emitidos"],
                    "aprobados": data[k]["aprob_aprobados"],
                    "rechazados": data[k]["aprob_rechazados"],
                    "tasa": (
                        data[k]["aprob_aprobados"] /
                        (data[k]["aprob_aprobados"] + data[k]["aprob_rechazados"])
                    ) if (data[k]["aprob_aprobados"] + data[k]["aprob_rechazados"]) else 0.0,
                    "t_emitir_horas": (data[k]["t_emitir_min_sum"] / 60 / data[k]["t_emitir_n"]) if data[k]["t_emitir_n"] else None,
                    "t_aprobar_horas": (data[k]["t_aprobar_min_sum"] / 60 / data[k]["t_aprobar_n"]) if data[k]["t_aprobar_n"] else None,
                    "t_emitir_percentiles": _pctiles(data[k]["t_emitir_hours_vals"], (25,50,75,90,95)) if data[k]["t_emitir_hours_vals"] else {"p25": None, "p50": None, "p75": None, "p90": None, "p95": None, "avg": None, "count": 0},
                    "t_aprobar_percentiles": _pctiles(data[k]["t_aprobar_hours_vals"], (25,50,75,90,95)) if data[k]["t_aprobar_hours_vals"] else {"p25": None, "p50": None, "p75": None, "p90": None, "p95": None, "avg": None, "count": 0},
                },
                "tat_dias": (sum(data[k]["tat_days_vals"]) / len(data[k]["tat_days_vals"])) if data[k]["tat_days_vals"] else None,
                "tat_percentiles": _pctiles(data[k]["tat_days_vals"], (25,50,75,90,95)) if data[k]["tat_days_vals"] else {"p25": None, "p50": None, "p75": None, "p90": None, "p95": None, "avg": None, "count": 0},
            })

        # Yearly agregados
        yearly = {}
        for it in series:
            y = it["period"].split("-")[0]
            yy = yearly.setdefault(y, {
                "period": y,
                "entregados": 0,
                "mttr_sum": 0.0,
                "mttr_n": 0,
                "sla_total": 0,
                "sla_dentro": 0,
                "emitidos": 0,
                "aprobados": 0,
                "rechazados": 0,
                "t_emitir_sum": 0.0,
                "t_emitir_n": 0,
                "t_aprobar_sum": 0.0,
                "t_aprobar_n": 0,
            })
            yy["entregados"] += (it.get("entregados") or 0)
            if it.get("mttr_dias") is not None:
                yy["mttr_sum"] += it["mttr_dias"]
                yy["mttr_n"] += 1
            sla = it.get("sla_diag_24h", {})
            yy["sla_total"] += sla.get("total", 0)
            yy["sla_dentro"] += sla.get("dentro", 0)
            ap = it.get("aprob_presupuestos", {})
            yy["emitidos"] += ap.get("emitidos", 0)
            yy["aprobados"] += ap.get("aprobados", 0)
            yy["rechazados"] += ap.get("rechazados", 0)
            if ap.get("t_emitir_horas") is not None:
                yy["t_emitir_sum"] += ap["t_emitir_horas"]
                yy["t_emitir_n"] += 1
            if ap.get("t_aprobar_horas") is not None:
                yy["t_aprobar_sum"] += ap["t_aprobar_horas"]
                yy["t_aprobar_n"] += 1
        series_year = []
        for y, v in sorted(yearly.items()):
            series_year.append({
                "period": y,
                "entregados": v["entregados"],
                "mttr_dias": (v["mttr_sum"] / v["mttr_n"]) if v["mttr_n"] else None,
                "sla_diag_24h": {
                    "total": v["sla_total"],
                    "dentro": v["sla_dentro"],
                    "cumplimiento": (v["sla_dentro"] / v["sla_total"]) if v["sla_total"] else 0.0,
                },
                "aprob_presupuestos": {
                    "emitidos": v["emitidos"],
                    "aprobados": v["aprobados"],
                    "rechazados": v["rechazados"],
                    "tasa": (v["aprobados"] / (v["aprobados"] + v["rechazados"])) if (v["aprobados"] + v["rechazados"]) else 0.0,   
                    "t_emitir_horas": (v["t_emitir_sum"] / v["t_emitir_n"]) if v["t_emitir_n"] else None,
                    "t_aprobar_horas": (v["t_aprobar_sum"] / v["t_aprobar_n"]) if v["t_aprobar_n"] else None,
                },
            })

        resp = {"range": {"from": since, "to": until}, "monthly": series, "yearly": series_year}

        # Agrupaciones para export (técnico, marca, tipo, cliente)
        group = (request.GET.get("group") or "").strip().lower()
        if group == "tecnico":
            ym_ent = _sql_ym('i.fecha_entrega'); ym_qap = _sql_ym('q.fecha_aprobado')
            bytec = q((
                f"SELECT {ym_ent} AS period, i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, COUNT(*) AS entregados\n"
                "FROM ingresos i\n"
                f"{join_dm}\n"
                "LEFT JOIN users u ON u.id=i.asignado_a\n"
                "WHERE i.fecha_entrega BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, i.asignado_a, tecnico_nombre\n"
                "ORDER BY period ASC, entregados DESC\n"
            ), [since, until, *where_params]) or []
            fac_tec = q((
                f"SELECT {ym_qap} AS period, i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, ROUND(SUM({qt_expr}),2) AS facturacion\n"
                "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
                f"{qt_join}"
                f"{join_dm}\n"
                "LEFT JOIN users u ON u.id=i.asignado_a\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, i.asignado_a, tecnico_nombre\n"
                "ORDER BY period ASC, facturacion DESC\n"
            ), [since, until, *where_params]) or []
            mo_tec = q((
                f"SELECT {ym_qap} AS period, i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_mo\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                f"{join_dm}\n"
                "LEFT JOIN users u ON u.id=i.asignado_a\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='mano_obra'"
                f"{where_i}\n"
                "GROUP BY period, i.asignado_a, tecnico_nombre\n"
                "ORDER BY period ASC, monto_mo DESC\n"
            ), [since, until, *where_params]) or []
            rep_tec = q((
                f"SELECT {ym_qap} AS period, i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_repuestos\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                f"{join_dm}\n"
                "LEFT JOIN users u ON u.id=i.asignado_a\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='repuesto'"
                f"{where_i}\n"
                "GROUP BY period, i.asignado_a, tecnico_nombre\n"
                "ORDER BY period ASC, monto_repuestos DESC\n"
            ), [since, until, *where_params]) or []
            resp.update({
                "by_tecnico_monthly": bytec,
                "facturacion_tecnico_monthly": fac_tec,
                "mo_tecnico_monthly": mo_tec,
                "repuestos_tecnico_monthly": rep_tec,
            })
        elif group == "marca":
            ym_ent = _sql_ym('i.fecha_entrega'); ym_qap = _sql_ym('q.fecha_aprobado')
            bym = q((
                f"SELECT {ym_ent} AS period, d.marca_id AS marca_id, COALESCE(b.nombre,'') AS marca_nombre, COUNT(*) AS entregados\n"
                "FROM ingresos i\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN marcas b ON b.id=d.marca_id\n"
                f"{join_dm}\n"
                "WHERE i.fecha_entrega BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, d.marca_id, marca_nombre\n"
                "ORDER BY period ASC, entregados DESC\n"
            ), [since, until, *where_params]) or []
            facm = q((
                f"SELECT {ym_qap} AS period, d.marca_id AS marca_id, COALESCE(b.nombre,'') AS marca_nombre, ROUND(SUM({qt_expr}),2) AS facturacion\n"
                "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
                f"{qt_join}"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN marcas b ON b.id=d.marca_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, d.marca_id, marca_nombre\n"
                "ORDER BY period ASC, facturacion DESC\n"
            ), [since, until, *where_params]) or []
            mom = q((
                f"SELECT {ym_qap} AS period, d.marca_id AS marca_id, COALESCE(b.nombre,'') AS marca_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_mo\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN marcas b ON b.id=d.marca_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='mano_obra'"
                f"{where_i}\n"
                "GROUP BY period, d.marca_id, marca_nombre\n"
                "ORDER BY period ASC, monto_mo DESC\n"
            ), [since, until, *where_params]) or []
            repm = q((
                f"SELECT {ym_qap} AS period, d.marca_id AS marca_id, COALESCE(b.nombre,'') AS marca_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_repuestos\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN marcas b ON b.id=d.marca_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='repuesto'"
                f"{where_i}\n"
                "GROUP BY period, d.marca_id, marca_nombre\n"
                "ORDER BY period ASC, monto_repuestos DESC\n"
            ), [since, until, *where_params]) or []
            resp.update({
                "by_marca_monthly": bym,
                "facturacion_marca_monthly": facm,
                "mo_marca_monthly": mom,
                "repuestos_marca_monthly": repm,
            })
        elif group in ("tipo", "tipo_equipo"):
            ym_ent = _sql_ym('i.fecha_entrega'); ym_qap = _sql_ym('q.fecha_aprobado')
            byt = q((
                f"SELECT {ym_ent} AS period, COALESCE(m.tipo_equipo,'') AS tipo_equipo, COUNT(*) AS entregados\n"
                "FROM ingresos i\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN models m ON m.id=d.model_id\n"
                f"{join_dm}\n"
                "WHERE i.fecha_entrega BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, tipo_equipo\n"
                "ORDER BY period ASC, entregados DESC\n"
            ), [since, until, *where_params]) or []
            fact = q((
                f"SELECT {ym_qap} AS period, COALESCE(m.tipo_equipo,'') AS tipo_equipo, ROUND(SUM({qt_expr}),2) AS facturacion\n"
                "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
                f"{qt_join}"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN models m ON m.id=d.model_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, tipo_equipo\n"
                "ORDER BY period ASC, facturacion DESC\n"
            ), [since, until, *where_params]) or []
            mot = q((
                f"SELECT {ym_qap} AS period, COALESCE(m.tipo_equipo,'') AS tipo_equipo, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_mo\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN models m ON m.id=d.model_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='mano_obra'"
                f"{where_i}\n"
                "GROUP BY period, tipo_equipo\n"
                "ORDER BY period ASC, monto_mo DESC\n"
            ), [since, until, *where_params]) or []
            rept = q((
                f"SELECT {ym_qap} AS period, COALESCE(m.tipo_equipo,'') AS tipo_equipo, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_repuestos\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "LEFT JOIN models m ON m.id=d.model_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='repuesto'"
                f"{where_i}\n"
                "GROUP BY period, tipo_equipo\n"
                "ORDER BY period ASC, monto_repuestos DESC\n"
            ), [since, until, *where_params]) or []
            resp.update({
                "by_tipo_monthly": byt,
                "facturacion_tipo_monthly": fact,
                "mo_tipo_monthly": mot,
                "repuestos_tipo_monthly": rept,
            })
        elif group in ("cliente", "customer"):
            ym_ent = _sql_ym('i.fecha_entrega'); ym_qap = _sql_ym('q.fecha_aprobado')
            byc = q((
                f"SELECT {ym_ent} AS period, c.id AS cliente_id, COALESCE(c.razon_social,'') AS cliente_nombre, COUNT(*) AS entregados\n"
                "FROM ingresos i\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "JOIN customers c ON c.id=d.customer_id\n"
                f"{join_dm}\n"
                "WHERE i.fecha_entrega BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, c.id, cliente_nombre\n"
                "ORDER BY period ASC, entregados DESC\n"
            ), [since, until, *where_params]) or []
            facc = q((
                f"SELECT {ym_qap} AS period, c.id AS cliente_id, COALESCE(c.razon_social,'') AS cliente_nombre, ROUND(SUM({qt_expr}),2) AS facturacion\n"
                "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
                f"{qt_join}"
                "JOIN devices d ON d.id=i.device_id\n"
                "JOIN customers c ON c.id=d.customer_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s"
                f"{where_i}\n"
                "GROUP BY period, c.id, cliente_nombre\n"
                "ORDER BY period ASC, facturacion DESC\n"
            ), [since, until, *where_params]) or []
            moc = q((
                f"SELECT {ym_qap} AS period, c.id AS cliente_id, COALESCE(c.razon_social,'') AS cliente_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_mo\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "JOIN customers c ON c.id=d.customer_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='mano_obra'"
                f"{where_i}\n"
                "GROUP BY period, c.id, cliente_nombre\n"
                "ORDER BY period ASC, monto_mo DESC\n"
            ), [since, until, *where_params]) or []
            repc = q((
                f"SELECT {ym_qap} AS period, c.id AS cliente_id, COALESCE(c.razon_social,'') AS cliente_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS monto_repuestos\n"
                "FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id\n"
                "JOIN ingresos i ON i.id=q.ingreso_id\n"
                "JOIN devices d ON d.id=i.device_id\n"
                "JOIN customers c ON c.id=d.customer_id\n"
                f"{join_dm}\n"
                "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='repuesto'"
                f"{where_i}\n"
                "GROUP BY period, c.id, cliente_nombre\n"
                "ORDER BY period ASC, monto_repuestos DESC\n"
            ), [since, until, *where_params]) or []
            resp.update({
                "by_cliente_monthly": byc,
                "facturacion_cliente_monthly": facc,
                "mo_cliente_monthly": moc,
                "repuestos_cliente_monthly": repc,
            })

        return Response(resp)


class MetricasFinanzasView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe", "jefe_veedor"])
        since, until = _parse_range_params(request)
        join_dm, where_i, where_params = _filters_join_where(request)

        # buckets mensuales
        months = []
        y, m = since.year, since.month
        while y < until.year or (y == until.year and m <= until.month):
            months.append(f"{y:04d}-{m:02d}")
            if m == 12:
                y += 1; m = 1
            else:
                m += 1
        data = {k: {"cobro": 0, "liberados": 0, "mo": 0, "repuestos": 0, "costos_repuestos": 0} for k in months}

        ev_join = (
            "JOIN (SELECT ingreso_id, MAX(ts) AS ts "
            "FROM ingreso_events WHERE a_estado='liberado' GROUP BY ingreso_id) ev "
            "ON ev.ingreso_id=i.id\n"
        )
        financial_sql = _rejected_budget_financial_sql_fragments()
        quote_join = (
            "LEFT JOIN (SELECT ingreso_id, MAX(id) AS quote_id "
            "FROM quotes WHERE estado='aprobado' GROUP BY ingreso_id) qa "
            "ON qa.ingreso_id=i.id\n"
            "LEFT JOIN (SELECT ingreso_id, MAX(id) AS quote_id "
            "FROM quotes GROUP BY ingreso_id) ql "
            "ON ql.ingreso_id=i.id\n"
            f"LEFT JOIN quotes q ON q.id={financial_sql['selected_quote_expr']}\n"
        )
        items_join = (
            "LEFT JOIN ("
            "  SELECT quote_id,\n"
            "         ROUND(SUM(qi.qty*qi.precio_u),2) AS subtotal,\n"
            "         ROUND(SUM(CASE WHEN qi.tipo='mano_obra' THEN qi.qty*qi.precio_u ELSE 0 END),2) AS mano_obra,\n"
            "         ROUND(SUM(CASE WHEN qi.tipo='repuesto' THEN qi.qty*qi.precio_u ELSE 0 END),2) AS repuestos,\n"
            "         ROUND(SUM(CASE WHEN qi.tipo='repuesto' THEN qi.qty*COALESCE(qi.costo_u_neto, CASE WHEN cfg.mult > 0 THEN qi.precio_u / cfg.mult ELSE 0 END) ELSE 0 END),2) AS costo_repuestos\n"
            "  FROM quote_items qi\n"
            "  CROSS JOIN (SELECT COALESCE((SELECT multiplicador_general FROM repuestos_config ORDER BY id LIMIT 1), 1) AS mult) cfg\n"
            "  GROUP BY quote_id"
            ") qs ON qs.quote_id=q.id\n"
        )
        ym_ev = _sql_ym("ev.ts")

        # Ingresos sin IVA: subtotal del presupuesto (aprobado o ultimo) en equipos liberados
        total_rows = q((
            f"SELECT {ym_ev} AS ym,\n"
            "       COUNT(*) AS liberados,\n"
            f"       ROUND(SUM({financial_sql['cobro_expr']}),2) AS cobro,\n"
            f"       ROUND(SUM({financial_sql['mano_obra_expr']}),2) AS monto_mo,\n"
            f"       ROUND(SUM({financial_sql['repuestos_expr']}),2) AS monto_rep,\n"
            f"       ROUND(SUM({financial_sql['costos_expr']}),2) AS costos_repuestos\n"
            "FROM ingresos i\n"
            f"{quote_join}"
            f"{items_join}"
            f"{ev_join}"
            f"{join_dm}\n"
            "WHERE ev.ts BETWEEN %s AND %s\n"
            f"{where_i}\n"
            "GROUP BY ym"
        ), [since, until, *where_params]) or []
        for r in total_rows:
            k = r.get("ym")
            if k in data:
                data[k]["liberados"] = r.get("liberados", 0) or 0
                data[k]["cobro"] = r.get("cobro", 0) or 0
                data[k]["mo"] = r.get("monto_mo", 0) or 0
                data[k]["repuestos"] = r.get("monto_rep", 0) or 0
                data[k]["costos_repuestos"] = r.get("costos_repuestos", 0) or 0

        monthly = []
        for k in months:
            monthly.append({
                "period": k,
                "cobro": data[k]["cobro"],
                "liberados": data[k]["liberados"],
                "mo": data[k]["mo"],
                "repuestos": data[k]["repuestos"],
                "costos_repuestos": data[k]["costos_repuestos"],
                "margen": (data[k]["cobro"] or 0) - (data[k]["costos_repuestos"] or 0),
            })

        yearly = {}
        for it in monthly:
            y = it["period"].split("-")[0]
            yy = yearly.setdefault(y, {
                "period": y,
                "cobro": 0,
                "liberados": 0,
                "mo": 0,
                "repuestos": 0,
                "costos_repuestos": 0,
                "margen": 0,
            })
            yy["cobro"] += it.get("cobro") or 0
            yy["liberados"] += it.get("liberados") or 0
            yy["mo"] += it.get("mo") or 0
            yy["repuestos"] += it.get("repuestos") or 0
            yy["costos_repuestos"] += it.get("costos_repuestos") or 0
            yy["margen"] += it.get("margen") or 0

        yearly_list = [v for _, v in sorted(yearly.items())]
        total_cobro = sum((it.get("cobro") or 0) for it in monthly)
        total_lib = sum((it.get("liberados") or 0) for it in monthly)
        total_mo = sum((it.get("mo") or 0) for it in monthly)
        total_rep = sum((it.get("repuestos") or 0) for it in monthly)
        total_costos = sum((it.get("costos_repuestos") or 0) for it in monthly)
        total_margen = total_cobro - total_costos

        summary = {
            "cobro_total": total_cobro,
            "liberados_total": total_lib,
            "mo_total": total_mo,
            "repuestos_total": total_rep,
            "costos_repuestos_total": total_costos,
            "margen_total": total_margen,
            "ticket_promedio": (total_cobro / total_lib) if total_lib else None,
        }

        return Response({
            "range": {"from": since, "to": until},
            "summary": summary,
            "monthly": monthly,
            "yearly": yearly_list,
        })


class MetricasFinanzasLiberadosView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe", "jefe_veedor"])
        tz = timezone.get_current_timezone()
        month = (request.GET.get("month") or "").strip()
        if month:
            try:
                y_str, m_str = month.split("-", 1)
                y = int(y_str); m = int(m_str)
                if m < 1 or m > 12:
                    raise ValueError("month fuera de rango")
                since = timezone.make_aware(dt.datetime(y, m, 1), tz)
                if m == 12:
                    next_month = dt.datetime(y + 1, 1, 1)
                else:
                    next_month = dt.datetime(y, m + 1, 1)
                until = timezone.make_aware(next_month, tz) - dt.timedelta(microseconds=1)
            except Exception:
                return Response({"detail": "month inválido (YYYY-MM)"}, status=400)
        else:
            since, until = _parse_range_params(request)

        # Enforce global cutoff (inclusive)
        try:
            cutoff = _cutoff_ts()
            if since < cutoff:
                since = cutoff
        except Exception:
            pass

        wh = []
        params = []
        # Filtro global por ingreso (consistente con metricas)
        try:
            params.append(_cutoff_ts())
            wh.append(" i.fecha_ingreso >= %s ")
        except Exception:
            pass
        t_id = request.GET.get("tecnico_id")
        if t_id:
            try:
                params.append(int(t_id))
                wh.append(" i.asignado_a = %s ")
            except Exception:
                pass
        m_id = request.GET.get("marca_id")
        if m_id:
            try:
                params.append(int(m_id))
                wh.append(" d.marca_id = %s ")
            except Exception:
                pass
        tipo = (request.GET.get("tipo_equipo") or "").strip()
        if tipo:
            params.append(tipo)
            wh.append(" UPPER(TRIM(m.tipo_equipo)) = UPPER(TRIM(%s)) ")
        where_sql = (" AND " + " AND ".join(wh)) if wh else ""
        financial_sql = _rejected_budget_financial_sql_fragments()

        rows = q((
            "SELECT\n"
            "  i.id AS ingreso_id,\n"
            "  ev.ts AS fecha_liberado,\n"
            "  i.fecha_ingreso,\n"
            "  i.estado,\n"
            "  i.presupuesto_estado,\n"
            "  i.resolucion,\n"
            "  COALESCE(u.nombre,'') AS tecnico_nombre,\n"
            "  c.razon_social AS cliente,\n"
            "  COALESCE(b.nombre,'') AS marca,\n"
            "  COALESCE(m.nombre,'') AS modelo,\n"
            "  COALESCE(m.tipo_equipo,'') AS tipo_equipo,\n"
            "  COALESCE(d.numero_serie,'') AS numero_serie,\n"
            "  COALESCE(d.numero_interno,'') AS numero_interno,\n"
            "  q.id AS quote_id,\n"
            "  q.estado AS quote_estado,\n"
            f"  {financial_sql['cobro_expr']} AS ingresos_sin_iva,\n"
            f"  {financial_sql['mano_obra_expr']} AS mano_obra,\n"
            f"  {financial_sql['repuestos_expr']} AS repuestos,\n"
            f"  {financial_sql['costos_expr']} AS costo_repuestos,\n"
            f"  ROUND({financial_sql['cobro_expr']} - {financial_sql['costos_expr']}, 2) AS margen\n"
            "FROM ingresos i\n"
            "JOIN devices d ON d.id=i.device_id\n"
            "JOIN customers c ON c.id=d.customer_id\n"
            "LEFT JOIN marcas b ON b.id=d.marca_id\n"
            "LEFT JOIN models m ON m.id=d.model_id\n"
            "LEFT JOIN users u ON u.id=i.asignado_a\n"
            "JOIN (SELECT ingreso_id, MAX(ts) AS ts FROM ingreso_events WHERE a_estado='liberado' GROUP BY ingreso_id) ev\n"
            "  ON ev.ingreso_id=i.id\n"
            "LEFT JOIN (SELECT ingreso_id, MAX(id) AS quote_id FROM quotes WHERE estado='aprobado' GROUP BY ingreso_id) qa\n"
            "  ON qa.ingreso_id=i.id\n"
            "LEFT JOIN (SELECT ingreso_id, MAX(id) AS quote_id FROM quotes GROUP BY ingreso_id) ql\n"
            "  ON ql.ingreso_id=i.id\n"
            f"LEFT JOIN quotes q ON q.id={financial_sql['selected_quote_expr']}\n"
            "LEFT JOIN (\n"
            "  SELECT quote_id,\n"
            "         ROUND(SUM(qi.qty*qi.precio_u),2) AS subtotal,\n"
            "         ROUND(SUM(CASE WHEN qi.tipo='mano_obra' THEN qi.qty*qi.precio_u ELSE 0 END),2) AS mano_obra,\n"
            "         ROUND(SUM(CASE WHEN qi.tipo='repuesto' THEN qi.qty*qi.precio_u ELSE 0 END),2) AS repuestos,\n"
            "         ROUND(SUM(CASE WHEN qi.tipo='repuesto' THEN qi.qty*COALESCE(qi.costo_u_neto, CASE WHEN cfg.mult > 0 THEN qi.precio_u / cfg.mult ELSE 0 END) ELSE 0 END),2) AS costo_repuestos\n"
            "    FROM quote_items qi\n"
            "    CROSS JOIN (SELECT COALESCE((SELECT multiplicador_general FROM repuestos_config ORDER BY id LIMIT 1), 1) AS mult) cfg\n"
            "   GROUP BY quote_id\n"
            ") qs ON qs.quote_id=q.id\n"
            "WHERE ev.ts BETWEEN %s AND %s"
            f"{where_sql}\n"
            "ORDER BY ev.ts ASC, i.id ASC\n"
        ), [since, until, *params]) or []

        return Response(rows)


class MetricasActividadTecnicosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _require_metricas_activity_viewer(request)

        since, until = _parse_activity_range_params(request)
        if since > until:
            since, until = until, since

        tecnico_id = None
        tecnico_raw = _clean_text(request.GET.get("tecnico_id"))
        if tecnico_raw:
            try:
                tecnico_id = int(tecnico_raw)
            except Exception:
                tecnico_id = None

        activity_type = _clean_text(request.GET.get("tipo")).lower() or None

        change_rows = _load_change_log_activity_rows(since, until, tecnico_id=tecnico_id)
        event_rows = _load_ingreso_event_activity_rows(since, until, tecnico_id=tecnico_id)
        repuesto_rows = _load_repuesto_activity_rows(since, until, tecnico_id=tecnico_id)
        audit_rows = _load_audit_log_activity_rows(since, until, tecnico_id=tecnico_id)

        ingreso_ids = set()
        repuesto_ids = set()

        for row in change_rows:
            if row.get("ingreso_id") is not None:
                ingreso_ids.add(int(row["ingreso_id"]))
            if row.get("repuesto_id") is not None:
                repuesto_ids.add(int(row["repuesto_id"]))
        for row in event_rows:
            if row.get("ingreso_id") is not None:
                ingreso_ids.add(int(row["ingreso_id"]))
        for row in repuesto_rows:
            if row.get("ingreso_id") is not None:
                ingreso_ids.add(int(row["ingreso_id"]))
            if row.get("repuesto_id") is not None:
                repuesto_ids.add(int(row["repuesto_id"]))
        for row in audit_rows:
            info = classify_read_path(row.get("path"))
            if info and info.get("ingreso_id") is not None:
                ingreso_ids.add(int(info["ingreso_id"]))

        ingreso_context_map = _load_ingreso_context(ingreso_ids)
        repuesto_context_map = _load_repuesto_context(repuesto_ids)

        timeline = []
        for row in change_rows:
            timeline.append(_normalize_change_log_row(row, ingreso_context_map, repuesto_context_map))
        for row in event_rows:
            timeline.append(_normalize_event_row(row, ingreso_context_map))
        for row in repuesto_rows:
            timeline.append(_normalize_repuesto_row(row, ingreso_context_map, repuesto_context_map))
        for row in audit_rows:
            normalized = _normalize_audit_log_row(row, ingreso_context_map)
            if normalized:
                timeline.append(normalized)

        timeline = _dedupe_activity_timeline([row for row in timeline if row])
        available_activity_types = _build_activity_summary(timeline).get("activity_types", [])
        timeline = _filter_activity_rows(timeline, activity_type=activity_type)
        summary = _build_activity_summary(timeline)

        return Response(
            {
                "range": {
                    "from": since.date().isoformat(),
                    "to": until.date().isoformat(),
                    "preset": _clean_text(request.GET.get("preset")).lower() or None,
                },
                "available_activity_types": available_activity_types,
                "summary": summary,
                "timeline": timeline,
            }
        )


class MetricasCalibracionView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe"])  # solo Jefe
        since, until = _parse_range_params(request)
        join_dm, where_i, where_params = _filters_join_where(request)

        diag_rows = q((
            "SELECT i.fecha_ingreso, COALESCE(di.ts, i.fecha_servicio, i.fecha_ingreso) AS diag_ts\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS ts FROM ingreso_events WHERE a_estado='diagnosticado' GROUP BY ingreso_id) di\n"
            "  ON di.ingreso_id=i.id\n"
            "WHERE i.fecha_ingreso BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        holi = _holidays_between((since - dt.timedelta(days=7)).date(), (until + dt.timedelta(days=7)).date())
        diag_bmins = []
        for r in diag_rows:
            fi = r.get("fecha_ingreso"); dtg = r.get("diag_ts")
            if fi and dtg:
                diag_bmins.append(business_minutes_between(fi, dtg, holidays=holi))

        t_emit_rows = q((
            "SELECT COALESCE(di.ts, i.fecha_servicio, i.fecha_ingreso) AS diag_ts, q.fecha_emitido\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "JOIN quotes q ON q.ingreso_id=i.id\n"
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS ts FROM ingreso_events WHERE a_estado='diagnosticado' GROUP BY ingreso_id) di\n"
            "  ON di.ingreso_id=i.id\n"
            "WHERE q.fecha_emitido BETWEEN %s AND %s AND q.fecha_emitido IS NOT NULL"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        emit_hours = [max(0.0, (row["fecha_emitido"] - row["diag_ts"]).total_seconds() / 3600.0) for row in t_emit_rows if row.get("diag_ts") and row.get("fecha_emitido")]

        mins_emit_aprob = _sql_mins('q.fecha_emitido','q.fecha_aprobado')
        t_aprob_rows = q((
            f"SELECT {mins_emit_aprob} AS mins\n"
            "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.fecha_emitido BETWEEN %s AND %s AND q.fecha_aprobado IS NOT NULL"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        apro_hours = []
        for r in t_aprob_rows:
            v = r.get("mins") or 0
            try:
                v = float(v)
            except Exception:
                pass
            apro_hours.append(max(0.0, v / 60.0))

        t_rep_rows = q((
            "SELECT q.fecha_aprobado, r.reparado_ts\n"
            "FROM quotes q\n"
            "JOIN (SELECT ingreso_id, MIN(ts) AS reparado_ts FROM ingreso_events WHERE a_estado='reparado' GROUP BY ingreso_id) r\n"
            "  ON r.ingreso_id=q.ingreso_id\n"
            "JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.fecha_aprobado BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        apr_rep_days = [max(0.0, (row["reparado_ts"] - row["fecha_aprobado"]).total_seconds() / 86400.0) for row in t_rep_rows if row.get("fecha_aprobado") and row.get("reparado_ts") and row.get("reparado_ts") > row.get("fecha_aprobado")]

        rep_ent_rows = q((
            "SELECT r.reparado_ts, i.fecha_entrega AS entregado_ts\n"
            "FROM (SELECT ingreso_id, MIN(ts) AS reparado_ts FROM ingreso_events WHERE a_estado='reparado' GROUP BY ingreso_id) r\n"
            "JOIN ingresos i ON i.id=r.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE i.fecha_entrega BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        rep_ent_days = [max(0.0, (row["entregado_ts"] - row["reparado_ts"]).total_seconds() / 86400.0) for row in rep_ent_rows if row.get("reparado_ts") and row.get("entregado_ts") and row.get("entregado_ts") > row.get("reparado_ts")]

        ing_ent_rows = q((
            "SELECT i.fecha_ingreso, i.fecha_entrega AS entregado_ts\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "WHERE i.fecha_entrega BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        ing_ent_days = [max(0.0, (row["entregado_ts"] - row["fecha_ingreso"]).total_seconds() / 86400.0) for row in ing_ent_rows if row.get("fecha_ingreso") and row.get("entregado_ts") and row.get("entregado_ts") > row.get("fecha_ingreso")]

        return Response({
            "range": {"from": since, "to": until},
            "diag_business_minutes": _pctiles(diag_bmins),
            "diag_to_emit_hours": _pctiles(emit_hours),
            "emit_to_approve_hours": _pctiles(apro_hours),
            "approve_to_repair_days": _pctiles(apr_rep_days),
            "repair_to_deliver_days": _pctiles(rep_ent_days),
            "ingreso_to_deliver_days": _pctiles(ing_ent_days),
        })
class MetricasResumenView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe"])  # solo Jefe
        since, until = _parse_range_params(request)
        join_dm, where_i, where_params = _filters_join_where(request)
        qt_join = _quote_total_join()
        qt_expr = _quote_total_expr()

        # CERRADOS 7/30 días (entregado)
        seven = _sql_now_minus_days(7)
        cerrados_7d = q((
            "SELECT i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, COUNT(*) AS cerrados\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "LEFT JOIN users u ON u.id=i.asignado_a\n"
            f"WHERE i.fecha_entrega >= ({seven})"
            f"{where_i}\n"
            "GROUP BY i.asignado_a, tecnico_nombre\n"
            "ORDER BY cerrados DESC"
        ), where_params) or []
        thirty = _sql_now_minus_days(30)
        cerrados_30d = q((
            "SELECT i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, COUNT(*) AS cerrados\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "LEFT JOIN users u ON u.id=i.asignado_a\n"
            f"WHERE i.fecha_entrega >= ({thirty})"
            f"{where_i}\n"
            "GROUP BY i.asignado_a, tecnico_nombre\n"
            "ORDER BY cerrados DESC"
        ), where_params) or []

        # WIP por técnico
        wip = q((
            "SELECT i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, COUNT(*) AS wip\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "JOIN locations loc ON loc.id=i.ubicacion_id\n"
            "LEFT JOIN users u ON u.id=i.asignado_a\n"
            "WHERE i.estado NOT IN ('diagnosticado','presupuestado','reparado','controlado_sin_defecto','entregado','liberado','alquilado','baja','derivado','vendido_pendiente_entrega','vendido_entregado')\n"
            "  AND LOWER(loc.nombre) = LOWER(%s)"
            f"{(' AND ' + where_i[5:]) if where_i else ''}\n"
            "GROUP BY i.asignado_a, tecnico_nombre\n"
            "ORDER BY wip DESC"
        ), ["taller", *where_params]) or []

        # Aging WIP (días hábiles desde ingreso hasta ahora)
        now_ts = timezone.now()
        open_rows = q((
            "SELECT i.id, i.fecha_ingreso\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "JOIN locations loc ON loc.id=i.ubicacion_id\n"
            "WHERE i.estado NOT IN ('diagnosticado','presupuestado','reparado','controlado_sin_defecto','entregado','liberado','alquilado','baja','derivado','vendido_pendiente_entrega','vendido_entregado')\n"
            "  AND LOWER(loc.nombre) = LOWER(%s)"
            f"{where_i}\n"
        ), ["taller", *where_params]) or []
        holi = _holidays_between((since - dt.timedelta(days=7)).date(), (until + dt.timedelta(days=7)).date())
        buckets = {"0-2": 0, "3-5": 0, "6-10": 0, "11-15": 0, "16+": 0}
        for r in open_rows:
            fi = r.get("fecha_ingreso")
            if not fi:
                continue
            mins = business_minutes_between(fi, now_ts, holidays=holi)
            days = (mins or 0) / (60.0 * 24.0)
            if days <= 2:
                buckets["0-2"] += 1
            elif days <= 5:
                buckets["3-5"] += 1
            elif days <= 10:
                buckets["6-10"] += 1
            elif days <= 15:
                buckets["11-15"] += 1
            else:
                buckets["16+"] += 1

        # MTTR (reparar -> reparado) en rango (por reparado_ts)
        reparado_rows = q((
            "SELECT r.ingreso_id, r.reparar_ts, d.reparado_ts\n"
            "FROM (SELECT ingreso_id, MIN(ts) AS reparar_ts FROM ingreso_events WHERE a_estado='reparar' GROUP BY ingreso_id) r\n"
            "JOIN (SELECT ingreso_id, MIN(ts) AS reparado_ts FROM ingreso_events WHERE a_estado='reparado' GROUP BY ingreso_id) d\n"
            "  ON d.ingreso_id = r.ingreso_id\n"
            "JOIN ingresos i ON i.id=d.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE d.reparado_ts BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        mttr_minutes = [max(0, int((row["reparado_ts"] - row["reparar_ts"]).total_seconds() // 60)) for row in reparado_rows if row.get("reparar_ts") and row.get("reparado_ts")]
        mttr_dias = (sum(mttr_minutes) / 60 / 24 / len(mttr_minutes)) if mttr_minutes else None

        # SLA diagnóstico (ingreso -> diag) 24h hábiles, excluyendo derivados por defecto
        rows_der = q((
            "SELECT DISTINCT ed.ingreso_id AS id\n"
            "FROM equipos_derivados ed\n"
            "JOIN ingresos i ON i.id=ed.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE 1=1"
            f"{where_i}\n"
        ), where_params) or []
        derivados_ids = {r.get("id") for r in rows_der}
        ingresos_periodo = q((
            "SELECT i.id, i.fecha_ingreso, COALESCE(di.ts, i.fecha_servicio, i.fecha_ingreso) AS diag_ts\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS ts FROM ingreso_events WHERE a_estado='diagnosticado' GROUP BY ingreso_id) di\n"
            "  ON di.ingreso_id=i.id\n"
            "WHERE i.fecha_ingreso BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        holi = _holidays_between((since - dt.timedelta(days=7)).date(), (until + dt.timedelta(days=7)).date())
        sla_total = sla_dentro = 0
        for r in ingresos_periodo:
            rid = r.get("id")
            if rid in derivados_ids:
                continue
            fi = r.get("fecha_ingreso"); dtg = r.get("diag_ts")
            if fi and dtg:
                sla_total += 1
                if business_minutes_between(fi, dtg, holidays=holi) <= 24 * 60:
                    sla_dentro += 1
        sla = {"total": sla_total, "dentro": sla_dentro, "cumplimiento": (sla_dentro / sla_total) if sla_total else 0.0}

        # Presupuestos, tiempos y facturación/MO/repuestos por técnico
        presup_emitidos = q((
            "SELECT COUNT(*) AS c FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.fecha_emitido BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params], one=True) or {"c": 0}
        presup_aprobados = q((
            "SELECT COUNT(*) AS c FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params], one=True) or {"c": 0}

        presup_rechazados = q((
            "SELECT COUNT(*) AS c FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.estado='rechazado' AND q.fecha_rechazado BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params], one=True) or {"c": 0}

        den = (presup_aprobados.get("c", 0) or 0) + (presup_rechazados.get("c", 0) or 0)
        aprob_tasa = ((presup_aprobados.get("c", 0) or 0) / float(den)) if den else 0.0

        t_emit_rows = q((
            "SELECT COALESCE(di.ts, i.fecha_servicio, i.fecha_ingreso) AS diag_ts, q.fecha_emitido\n"
            "FROM ingresos i\n"
            f"{join_dm}\n"
            "JOIN quotes q ON q.ingreso_id=i.id\n"
            "LEFT JOIN (SELECT ingreso_id, MIN(ts) AS ts FROM ingreso_events WHERE a_estado='diagnosticado' GROUP BY ingreso_id) di\n"
            "  ON di.ingreso_id=i.id\n"
            "WHERE q.fecha_emitido BETWEEN %s AND %s AND q.fecha_emitido IS NOT NULL"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        t_emit_min = [max(0, int((row["fecha_emitido"] - row["diag_ts"]).total_seconds() // 60)) for row in t_emit_rows if row.get("diag_ts") and row.get("fecha_emitido")]
        t_emit_horas = (sum(t_emit_min) / 60 / len(t_emit_min)) if t_emit_min else None

        t_aprob_rows = q((
            f"SELECT {_sql_mins('q.fecha_emitido','q.fecha_aprobado')} AS mins\n"
            "FROM quotes q JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.fecha_emitido BETWEEN %s AND %s AND q.fecha_aprobado IS NOT NULL"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        t_aprob_min = [max(0, r.get("mins") or 0) for r in t_aprob_rows]
        t_aprob_horas = (sum(t_aprob_min) / 60 / len(t_aprob_min)) if t_aprob_min else None

        t_rep_rows = q((
            "SELECT q.fecha_aprobado, r.reparado_ts\n"
            "FROM quotes q\n"
            "JOIN (SELECT ingreso_id, MIN(ts) AS reparado_ts FROM ingreso_events WHERE a_estado='reparado' GROUP BY ingreso_id) r\n"
            "  ON r.ingreso_id=q.ingreso_id\n"
            "JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE q.fecha_aprobado BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params]) or []
        reparado_antes_aprob = 0
        t_rep_min = []
        for row in t_rep_rows:
            fa = row.get("fecha_aprobado")
            rr = row.get("reparado_ts")
            if not fa or not rr:
                continue
            if rr <= fa:
                reparado_antes_aprob += 1
            else:
                t_rep_min.append(int((rr - fa).total_seconds() // 60))
        t_rep_dias = (sum(t_rep_min) / 60 / 24 / len(t_rep_min)) if t_rep_min else None

        fact_rows = q((
            f"SELECT i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, ROUND(SUM({qt_expr}),2) AS facturacion\n"
            "FROM quotes q\n"
            "JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{qt_join}"
            f"{join_dm}\n"
            "LEFT JOIN users u ON u.id=i.asignado_a\n"
            "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s"
            f"{where_i}\n"
            "GROUP BY i.asignado_a, tecnico_nombre\n"
            "ORDER BY facturacion DESC\n"
        ), [since, until, *where_params]) or []
        util_mo_rows = q((
            "SELECT i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS utilidad_mo\n"
            "FROM quote_items qi\n"
            "JOIN quotes q ON q.id=qi.quote_id\n"
            "JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "LEFT JOIN users u ON u.id=i.asignado_a\n"
            "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='mano_obra'"
            f"{where_i}\n"
            "GROUP BY i.asignado_a, tecnico_nombre\n"
            "ORDER BY utilidad_mo DESC\n"
        ), [since, until, *where_params]) or []
        rep_rows = q((
            "SELECT i.asignado_a AS tecnico_id, COALESCE(u.nombre,'') AS tecnico_nombre, ROUND(SUM(qi.qty*qi.precio_u),2) AS ingreso_repuestos\n"
            "FROM quote_items qi\n"
            "JOIN quotes q ON q.id=qi.quote_id\n"
            "JOIN ingresos i ON i.id=q.ingreso_id\n"
            f"{join_dm}\n"
            "LEFT JOIN users u ON u.id=i.asignado_a\n"
            "WHERE q.estado='aprobado' AND q.fecha_aprobado BETWEEN %s AND %s AND qi.tipo='repuesto'"
            f"{where_i}\n"
            "GROUP BY i.asignado_a, tecnico_nombre\n"
            "ORDER BY ingreso_repuestos DESC\n"
        ), [since, until, *where_params]) or []

        # Derivaciones externas
        deriv_wip = q((
            "SELECT COUNT(*) AS c\n"
            "FROM equipos_derivados ed\n"
            "JOIN ingresos i ON i.id=ed.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE ed.estado IN ('derivado','en_servicio')"
            f"{where_i}\n"
        ), where_params, one=True) or {"c": 0}
        deriv_derivados = q((
            "SELECT COUNT(*) AS c\n"
            "FROM equipos_derivados ed\n"
            "JOIN ingresos i ON i.id=ed.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE ed.fecha_deriv BETWEEN %s AND %s"
            f"{where_i}\n"
        ), [since, until, *where_params], one=True) or {"c": 0}
        avg_dev_min = _sql_mins('ed.fecha_deriv','ed.fecha_entrega')
        deriv_devueltos = q((
            f"SELECT COUNT(*) AS c, AVG({avg_dev_min}) AS avg_min\n"
            "FROM equipos_derivados ed\n"
            "JOIN ingresos i ON i.id=ed.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE ed.fecha_entrega BETWEEN %s AND %s AND ed.fecha_entrega IS NOT NULL"
            f"{where_i}\n"
        ), [since, until, *where_params], one=True) or {"c": 0, "avg_min": None}
        deriv_t_deriv_a_dev_dias = ((deriv_devueltos.get("avg_min") or 0) / 60 / 24) if deriv_devueltos.get("avg_min") is not None else None
        avg_ent_min = _sql_mins('ed.fecha_entrega','i.fecha_entrega')
        deriv_dev_a_ent = q((
            f"SELECT AVG({avg_ent_min}) AS avg_min\n"
            "FROM equipos_derivados ed\n"
            "JOIN ingresos i ON i.id=ed.ingreso_id\n"
            f"{join_dm}\n"
            "WHERE ed.fecha_entrega BETWEEN %s AND %s AND ed.fecha_entrega IS NOT NULL AND i.fecha_entrega IS NOT NULL"
            f"{where_i}\n"
        ), [since, until, *where_params], one=True) or {"avg_min": None}
        deriv_t_dev_a_ent_dias = ((deriv_dev_a_ent.get("avg_min") or 0) / 60 / 24) if deriv_dev_a_ent.get("avg_min") is not None else None

        return Response({
            "range": {"from": since, "to": until},
            "mttr_dias": mttr_dias,
            "sla_diag_24h": sla,
            "aprob_presupuestos": {
                "emitidos": presup_emitidos.get("c", 0),
                "aprobados": presup_aprobados.get("c", 0),
                "rechazados": presup_rechazados.get("c", 0),
                "tasa": aprob_tasa,
                "t_emitir_horas": t_emit_horas,
                "t_aprobar_horas": t_aprob_horas,
            },
            "t_reparar_desde_aprob_dias": t_rep_dias,
            "reparado_antes_de_aprobar": reparado_antes_aprob,
            "cerrados_por_tecnico_7d": cerrados_7d,
            "cerrados_por_tecnico_30d": cerrados_30d,
            "wip_por_tecnico": wip,
            "wip_aging_buckets": buckets,
            "facturacion_por_tecnico": fact_rows,
            "utilidad_mo_por_tecnico": util_mo_rows,
            "repuestos_por_tecnico": rep_rows,
            "derivaciones": {
                "wip_externo": deriv_wip.get("c", 0),
                "derivados_periodo": deriv_derivados.get("c", 0),
                "devueltos_periodo": deriv_devueltos.get("c", 0),
                "t_deriv_a_devuelto_dias": deriv_t_deriv_a_dev_dias,
                "t_devuelto_a_entregado_dias": deriv_t_dev_a_ent_dias,
            },
        })
def _rejected_budget_financial_sql_fragments():
    if not has_rejected_budget_charge_schema():
        return {
            "selected_quote_expr": "COALESCE(qa.quote_id, ql.quote_id)",
            "cobro_expr": "COALESCE(qs.subtotal,0)",
            "mano_obra_expr": "COALESCE(qs.mano_obra,0)",
            "repuestos_expr": "CASE WHEN i.presupuesto_estado='no_aplica' THEN COALESCE(qs.costo_repuestos,0) ELSE COALESCE(qs.repuestos,0) END",
            "costos_expr": "COALESCE(qs.costo_repuestos,0)",
        }
    return {
        "selected_quote_expr": (
            "CASE "
            "WHEN COALESCE(i.resolucion,'')='presupuesto_rechazado' "
            "THEN COALESCE(i.presupuesto_rechazado_quote_id, ql.quote_id) "
            "ELSE COALESCE(qa.quote_id, ql.quote_id) "
            "END"
        ),
        "cobro_expr": (
            "CASE "
            "WHEN COALESCE(i.resolucion,'')='presupuesto_rechazado' "
            "THEN COALESCE(i.presupuesto_rechazado_cobro_neto,0) "
            "ELSE COALESCE(qs.subtotal,0) "
            "END"
        ),
        "mano_obra_expr": (
            "CASE "
            "WHEN COALESCE(i.resolucion,'')='presupuesto_rechazado' "
            "THEN COALESCE(i.presupuesto_rechazado_cobro_neto,0) "
            "ELSE COALESCE(qs.mano_obra,0) "
            "END"
        ),
        "repuestos_expr": (
            "CASE "
            "WHEN COALESCE(i.resolucion,'')='presupuesto_rechazado' THEN 0 "
            "WHEN i.presupuesto_estado='no_aplica' THEN COALESCE(qs.costo_repuestos,0) "
            "ELSE COALESCE(qs.repuestos,0) "
            "END"
        ),
        "costos_expr": (
            "CASE "
            "WHEN COALESCE(i.resolucion,'')='presupuesto_rechazado' THEN 0 "
            "ELSE COALESCE(qs.costo_repuestos,0) "
            "END"
        ),
    }
