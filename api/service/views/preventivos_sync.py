import calendar
import datetime as dt
import logging

from django.utils import timezone

from .helpers import exec_void, q

logger = logging.getLogger(__name__)


def _add_period(base_date, value, unit):
    if not base_date:
        return None
    if unit == "dias":
        return base_date + dt.timedelta(days=value)
    if unit == "meses":
        month0 = base_date.month - 1 + value
        year = base_date.year + month0 // 12
        month = month0 % 12 + 1
        day = min(base_date.day, calendar.monthrange(year, month)[1])
        return dt.date(year, month, day)
    if unit == "anios":
        year = base_date.year + value
        day = min(base_date.day, calendar.monthrange(year, base_date.month)[1])
        return dt.date(year, base_date.month, day)
    return base_date


def _as_local_date(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        dt_value = value
        if timezone.is_naive(dt_value):
            dt_value = timezone.make_aware(dt_value, timezone.get_current_timezone())
        return timezone.localtime(dt_value, timezone.get_current_timezone()).date()
    if isinstance(value, dt.date):
        return value
    return None


def _log_sync_result(result):
    status = result.get("status") or "unknown"
    level = logging.WARNING if status == "error" else logging.INFO
    logger.log(
        level,
        "preventivo sync from ingreso fecha_servicio result: status=%s ingreso_id=%s device_id=%s plan_id=%s",
        status,
        result.get("ingreso_id"),
        result.get("device_id"),
        result.get("plan_id"),
    )


def sync_plan_from_ingreso_fecha_servicio(ingreso_id: int, actor_user_id: int | None):
    out = {
        "status": "noop_no_fecha",
        "ingreso_id": int(ingreso_id),
        "device_id": None,
        "plan_id": None,
        "fecha_servicio": None,
        "proxima_revision_fecha": None,
    }
    try:
        ingreso = q(
            """
            SELECT id, device_id, fecha_servicio
              FROM ingresos
             WHERE id=%s
            """,
            [ingreso_id],
            one=True,
        )
        if not ingreso:
            out["status"] = "noop_no_ingreso"
            _log_sync_result(out)
            return out

        device_id = ingreso.get("device_id")
        out["device_id"] = device_id

        fecha_servicio_date = _as_local_date(ingreso.get("fecha_servicio"))
        out["fecha_servicio"] = fecha_servicio_date
        if not fecha_servicio_date:
            out["status"] = "noop_no_fecha"
            _log_sync_result(out)
            return out

        plan = q(
            """
            SELECT
              id,
              periodicidad_valor,
              periodicidad_unidad::text AS periodicidad_unidad,
              ultima_revision_fecha,
              proxima_revision_fecha
            FROM preventivo_planes
            WHERE scope_type='device'
              AND device_id=%s
              AND activa=true
            ORDER BY id DESC
            LIMIT 1
            """,
            [device_id],
            one=True,
        )
        if not plan:
            out["status"] = "noop_no_plan"
            _log_sync_result(out)
            return out

        out["plan_id"] = plan.get("id")
        ultima = plan.get("ultima_revision_fecha")
        if ultima and fecha_servicio_date < ultima:
            out["status"] = "noop_older_than_last"
            _log_sync_result(out)
            return out

        periodicidad_valor = int(plan.get("periodicidad_valor") or 0)
        periodicidad_unidad = plan.get("periodicidad_unidad")
        proxima = _add_period(fecha_servicio_date, periodicidad_valor, periodicidad_unidad)
        out["proxima_revision_fecha"] = proxima

        if actor_user_id is None:
            exec_void(
                """
                UPDATE preventivo_planes
                   SET ultima_revision_fecha=%s,
                       proxima_revision_fecha=%s
                 WHERE id=%s
                """,
                [fecha_servicio_date, proxima, plan.get("id")],
            )
        else:
            exec_void(
                """
                UPDATE preventivo_planes
                   SET ultima_revision_fecha=%s,
                       proxima_revision_fecha=%s,
                       updated_by=%s
                 WHERE id=%s
                """,
                [fecha_servicio_date, proxima, int(actor_user_id), plan.get("id")],
            )

        out["status"] = "updated"
        _log_sync_result(out)
        return out
    except Exception as exc:
        out["status"] = "error"
        out["error"] = str(exc)
        logger.exception(
            "preventivo sync from ingreso fecha_servicio failed: ingreso_id=%s device_id=%s",
            out.get("ingreso_id"),
            out.get("device_id"),
        )
        _log_sync_result(out)
        return out


__all__ = ["sync_plan_from_ingreso_fecha_servicio"]
