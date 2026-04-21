import calendar
import datetime as dt

from django.conf import settings
from django.db import transaction
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import _set_audit_user, exec_returning, exec_void, q, require_roles

_PERIOD_UNITS = {"dias", "meses", "anios"}
_ITEM_STATES = {"pendiente", "ok", "retirado", "no_controlado"}
_PLAN_ROLES = ["jefe", "jefe_veedor", "admin"]
_REVISION_ROLES = ["tecnico", "jefe", "jefe_veedor", "admin"]
_VIEW_ROLES = ["tecnico", "jefe", "jefe_veedor", "admin"]


def _uid(request):
    raw = getattr(request, "user_id", None)
    if raw is None:
        raw = getattr(getattr(request, "user", None), "id", None)
    try:
        return int(raw)
    except Exception:
        return None


def _parse_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "t"):
        return True
    if s in ("0", "false", "no", "n", "f"):
        return False
    return default


def _parse_int(value, field, required=False, default=None, min_value=None):
    if value is None or value == "":
        if required:
            raise ValidationError({field: "requerido"})
        return default
    try:
        parsed = int(value)
    except Exception:
        raise ValidationError({field: "debe ser entero"})
    if min_value is not None and parsed < min_value:
        raise ValidationError({field: f"debe ser >= {min_value}"})
    return parsed


def _parse_date(value, field, required=False):
    if value is None or value == "":
        if required:
            raise ValidationError({field: "requerido"})
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except Exception:
        raise ValidationError({field: "fecha inválida (YYYY-MM-DD)"})


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


def _preventivo_state(proxima, aviso_dias, has_plan=True, today=None):
    if not has_plan:
        return "sin_plan", None
    if today is None:
        today = dt.date.today()
    if not proxima:
        return "al_dia", None
    if today > proxima:
        return "vencido", (proxima - today).days
    lead = max(0, int(aviso_dias or 0))
    if today + dt.timedelta(days=lead) >= proxima:
        return "proximo", (proxima - today).days
    return "al_dia", (proxima - today).days


def _fetch_active_plan(scope_type, ref_id):
    if scope_type == "device":
        return q(
            """
            SELECT id, scope_type::text AS scope_type, device_id, customer_id,
                   periodicidad_valor, periodicidad_unidad::text AS periodicidad_unidad,
                   aviso_anticipacion_dias, ultima_revision_fecha, proxima_revision_fecha,
                   activa, COALESCE(observaciones,'') AS observaciones
              FROM preventivo_planes
             WHERE scope_type='device' AND device_id=%s AND activa=true
             ORDER BY id DESC
             LIMIT 1
            """,
            [ref_id],
            one=True,
        )
    return q(
        """
        SELECT id, scope_type::text AS scope_type, device_id, customer_id,
               periodicidad_valor, periodicidad_unidad::text AS periodicidad_unidad,
               aviso_anticipacion_dias, ultima_revision_fecha, proxima_revision_fecha,
               activa, COALESCE(observaciones,'') AS observaciones
          FROM preventivo_planes
         WHERE scope_type='customer' AND customer_id=%s AND activa=true
         ORDER BY id DESC
         LIMIT 1
        """,
        [ref_id],
        one=True,
    )


def _serialize_plan(row):
    if not row:
        return None
    effective = None
    if (row.get("scope_type") or "").strip().lower() == "device":
        effective = _resolve_effective_plan_dates(row)
    if effective:
        proxima = effective.get("proxima_revision_fecha")
        aviso = effective.get("aviso_anticipacion_dias")
        ultima = effective.get("ultima_revision_fecha")
    else:
        proxima = row.get("proxima_revision_fecha")
        aviso = row.get("aviso_anticipacion_dias")
        ultima = row.get("ultima_revision_fecha")
    state, days = _preventivo_state(
        proxima,
        aviso,
        has_plan=True,
    )
    out = {
        **row,
        "preventivo_estado": state,
        "preventivo_dias_restantes": days,
        "ultima_revision_fecha": ultima,
        "proxima_revision_fecha": proxima,
    }
    if effective:
        out["preventivo_source"] = effective.get("source")
        out["repuesto_proximo_id"] = effective.get("repuesto_proximo_id")
        out["repuesto_proximo_nombre"] = effective.get("repuesto_proximo_nombre") or ""
    return out


def _fetch_device_snapshot(device_id):
    return q(
        """
        SELECT d.id,
               d.customer_id,
               COALESCE(c.razon_social,'') AS customer_nombre,
               COALESCE(d.numero_serie,'') AS numero_serie,
               COALESCE(d.numero_interno,'') AS numero_interno,
               COALESCE(d.tipo_equipo,'') AS tipo_equipo,
               COALESCE(d.variante,'') AS variante,
               COALESCE(b.nombre,'') AS marca,
               COALESCE(m.nombre,'') AS modelo
          FROM devices d
          LEFT JOIN customers c ON c.id = d.customer_id
          LEFT JOIN marcas b ON b.id = d.marca_id
          LEFT JOIN models m ON m.id = d.model_id
         WHERE d.id=%s
        """,
        [device_id],
        one=True,
    )


def _device_label(row):
    tipo = (row.get("tipo_equipo") or "").strip()
    marca = (row.get("marca") or "").strip()
    modelo = (row.get("modelo") or "").strip()
    variante = (row.get("variante") or "").strip()
    model_txt = (f"{modelo} {variante}" if modelo and variante else (modelo or variante)).strip()
    parts = [p for p in [tipo, marca, model_txt] if p]
    return " | ".join(parts) if parts else "Equipo"


def _validate_period(data, default_lead):
    periodicidad_valor = _parse_int(data.get("periodicidad_valor"), "periodicidad_valor", required=True, min_value=1)
    periodicidad_unidad = (data.get("periodicidad_unidad") or "").strip().lower()
    if periodicidad_unidad not in _PERIOD_UNITS:
        raise ValidationError({"periodicidad_unidad": "debe ser dias|meses|anios"})
    aviso = _parse_int(
        data.get("aviso_anticipacion_dias"),
        "aviso_anticipacion_dias",
        required=False,
        default=int(default_lead or 30),
        min_value=0,
    )
    return periodicidad_valor, periodicidad_unidad, aviso


def _txt_norm(value):
    return " ".join(str(value or "").strip().split())


def _txt_norm_lower(value):
    return _txt_norm(value).lower()


def _repuesto_name_norm(value):
    out = _txt_norm_lower(value)
    if not out:
        raise ValidationError({"nombre_repuesto": "requerido"})
    return out


def _repuesto_key(catalogo_repuesto_id=None, nombre_repuesto=None):
    if catalogo_repuesto_id:
        return f"cat:{int(catalogo_repuesto_id)}"
    return f"txt:{_repuesto_name_norm(nombre_repuesto)}"


def _device_signature_from_snapshot(snapshot):
    tipo = _txt_norm_lower(snapshot.get("tipo_equipo"))
    marca = _txt_norm_lower(snapshot.get("marca"))
    modelo = _txt_norm_lower(snapshot.get("modelo"))
    variante = _txt_norm_lower(snapshot.get("variante"))
    signature_key = "|".join([tipo, marca, modelo, variante])
    return {
        "signature_key": signature_key,
        "signature_tipo_equipo": tipo or None,
        "signature_marca": marca or None,
        "signature_modelo": modelo or None,
        "signature_variante": variante or None,
    }


def _fetch_device_signature(device_id):
    row = _fetch_device_snapshot(device_id)
    if not row:
        return None
    return _device_signature_from_snapshot(row)


def _fetch_plan_device_signature(plan_id):
    row = q(
        """
        SELECT
          p.id AS plan_id,
          p.device_id,
          COALESCE(d.tipo_equipo,'') AS tipo_equipo,
          COALESCE(b.nombre,'') AS marca,
          COALESCE(m.nombre,'') AS modelo,
          COALESCE(d.variante,'') AS variante
        FROM preventivo_planes p
        JOIN devices d ON d.id = p.device_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        WHERE p.id=%s
          AND p.scope_type='device'
          AND p.activa=true
        """,
        [plan_id],
        one=True,
    )
    if not row:
        return None
    sig = _device_signature_from_snapshot(row)
    sig["plan_id"] = row.get("plan_id")
    sig["device_id"] = row.get("device_id")
    return sig


def _equivalent_active_plan_ids_by_signature(signature):
    if not signature:
        return []
    rows = q(
        """
        SELECT p.id
        FROM preventivo_planes p
        JOIN devices d ON d.id = p.device_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        WHERE p.scope_type='device'
          AND p.activa=true
          AND LOWER(TRIM(COALESCE(d.tipo_equipo,''))) = %s
          AND LOWER(TRIM(COALESCE(b.nombre,''))) = %s
          AND LOWER(TRIM(COALESCE(m.nombre,''))) = %s
          AND LOWER(TRIM(COALESCE(d.variante,''))) = %s
        ORDER BY p.id ASC
        """,
        [
            signature.get("signature_tipo_equipo") or "",
            signature.get("signature_marca") or "",
            signature.get("signature_modelo") or "",
            signature.get("signature_variante") or "",
        ],
    ) or []
    return [int(r.get("id")) for r in rows if r.get("id") is not None]


def _fetch_plan_repuestos_raw(plan_id):
    return q(
        """
        SELECT
          pr.id,
          pr.plan_id,
          pr.repuesto_key,
          pr.catalogo_repuesto_id,
          COALESCE(pr.nombre_repuesto,'') AS nombre_repuesto,
          pr.periodicidad_valor,
          pr.periodicidad_unidad::text AS periodicidad_unidad,
          pr.aviso_anticipacion_dias,
          pr.ultima_revision_fecha,
          pr.proxima_revision_fecha,
          pr.activa,
          pr.created_by,
          pr.updated_by,
          pr.created_at,
          pr.updated_at
        FROM preventivo_plan_repuestos pr
        WHERE pr.plan_id=%s
          AND pr.activa=true
        ORDER BY
          CASE WHEN pr.proxima_revision_fecha IS NULL THEN 1 ELSE 0 END,
          pr.proxima_revision_fecha ASC,
          pr.id ASC
        """,
        [plan_id],
    ) or []


def _serialize_plan_repuesto(row):
    if not row:
        return None
    state, days = _preventivo_state(
        row.get("proxima_revision_fecha"),
        row.get("aviso_anticipacion_dias"),
        has_plan=True,
    )
    return {
        **row,
        "preventivo_estado": state,
        "preventivo_dias_restantes": days,
    }


def _fetch_plan_repuestos(plan_id):
    return [_serialize_plan_repuesto(r) for r in _fetch_plan_repuestos_raw(plan_id)]


def _find_nearest_plan_repuesto(plan_id):
    row = q(
        """
        SELECT
          pr.id AS repuesto_id,
          COALESCE(pr.nombre_repuesto,'') AS repuesto_nombre,
          pr.aviso_anticipacion_dias AS repuesto_aviso_anticipacion_dias,
          pr.ultima_revision_fecha AS repuesto_ultima_revision_fecha,
          pr.proxima_revision_fecha AS repuesto_proxima_revision_fecha
        FROM preventivo_plan_repuestos pr
        WHERE pr.plan_id=%s
          AND pr.activa=true
        ORDER BY
          CASE WHEN pr.proxima_revision_fecha IS NULL THEN 1 ELSE 0 END,
          pr.proxima_revision_fecha ASC,
          pr.id ASC
        LIMIT 1
        """,
        [plan_id],
        one=True,
    )
    return row or None


def _resolve_effective_plan_dates(plan_row):
    if not plan_row:
        return None
    plan_id = plan_row.get("id")
    nearest = _find_nearest_plan_repuesto(plan_id) if plan_id else None
    if nearest:
        return {
            "source": "repuesto",
            "repuesto_proximo_id": nearest.get("repuesto_id"),
            "repuesto_proximo_nombre": nearest.get("repuesto_nombre") or "",
            "ultima_revision_fecha": nearest.get("repuesto_ultima_revision_fecha"),
            "proxima_revision_fecha": nearest.get("repuesto_proxima_revision_fecha"),
            "aviso_anticipacion_dias": nearest.get("repuesto_aviso_anticipacion_dias"),
        }
    return {
        "source": "plan",
        "repuesto_proximo_id": None,
        "repuesto_proximo_nombre": "",
        "ultima_revision_fecha": plan_row.get("ultima_revision_fecha"),
        "proxima_revision_fecha": plan_row.get("proxima_revision_fecha"),
        "aviso_anticipacion_dias": plan_row.get("aviso_anticipacion_dias"),
    }


def _parse_repuesto_payload(data, default_lead):
    out = {}
    cat_id = data.get("catalogo_repuesto_id")
    if cat_id not in (None, ""):
        cat_id = _parse_int(cat_id, "catalogo_repuesto_id", required=True, min_value=1)
        cat = q(
            "SELECT id, COALESCE(nombre,'') AS nombre FROM catalogo_repuestos WHERE id=%s",
            [cat_id],
            one=True,
        )
        if not cat:
            raise ValidationError({"catalogo_repuesto_id": "no existe"})
        out["catalogo_repuesto_id"] = int(cat.get("id"))
        default_name = _txt_norm(cat.get("nombre"))
    else:
        out["catalogo_repuesto_id"] = None
        default_name = ""

    name_raw = _txt_norm(data.get("nombre_repuesto"))
    if not name_raw:
        name_raw = default_name
    if not name_raw:
        raise ValidationError({"nombre_repuesto": "requerido si no hay catalogo_repuesto_id"})
    out["nombre_repuesto"] = name_raw
    out["repuesto_key"] = _repuesto_key(out["catalogo_repuesto_id"], out["nombre_repuesto"])

    out["periodicidad_valor"] = _parse_int(data.get("periodicidad_valor"), "periodicidad_valor", required=True, min_value=1)
    out["periodicidad_unidad"] = (data.get("periodicidad_unidad") or "").strip().lower()
    if out["periodicidad_unidad"] not in _PERIOD_UNITS:
        raise ValidationError({"periodicidad_unidad": "debe ser dias|meses|anios"})

    out["aviso_anticipacion_dias"] = _parse_int(
        data.get("aviso_anticipacion_dias"),
        "aviso_anticipacion_dias",
        required=False,
        default=int(default_lead or 30),
        min_value=0,
    )
    out["ultima_revision_fecha"] = _parse_date(data.get("ultima_revision_fecha"), "ultima_revision_fecha")
    out["proxima_revision_fecha"] = _parse_date(data.get("proxima_revision_fecha"), "proxima_revision_fecha")
    if not out["proxima_revision_fecha"] and out["ultima_revision_fecha"]:
        out["proxima_revision_fecha"] = _add_period(
            out["ultima_revision_fecha"],
            out["periodicidad_valor"],
            out["periodicidad_unidad"],
        )
    return out


def _upsert_template_item(signature, repuesto_payload, uid):
    if not signature:
        return
    existing = q(
        """
        SELECT id
        FROM preventivo_repuesto_plantillas
        WHERE signature_key=%s
          AND repuesto_key=%s
          AND activa=true
        LIMIT 1
        """,
        [signature.get("signature_key"), repuesto_payload.get("repuesto_key")],
        one=True,
    )
    if existing:
        exec_void(
            """
            UPDATE preventivo_repuesto_plantillas
               SET catalogo_repuesto_id=%s,
                   nombre_repuesto=%s,
                   periodicidad_valor=%s,
                   periodicidad_unidad=%s,
                   aviso_anticipacion_dias=%s,
                   updated_by=%s
             WHERE id=%s
            """,
            [
                repuesto_payload.get("catalogo_repuesto_id"),
                repuesto_payload.get("nombre_repuesto"),
                repuesto_payload.get("periodicidad_valor"),
                repuesto_payload.get("periodicidad_unidad"),
                repuesto_payload.get("aviso_anticipacion_dias"),
                uid,
                existing.get("id"),
            ],
        )
        return
    exec_void(
        """
        INSERT INTO preventivo_repuesto_plantillas(
          signature_key, signature_tipo_equipo, signature_marca, signature_modelo, signature_variante,
          repuesto_key, catalogo_repuesto_id, nombre_repuesto,
          periodicidad_valor, periodicidad_unidad, aviso_anticipacion_dias,
          activa, created_by, updated_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)
        """,
        [
            signature.get("signature_key"),
            signature.get("signature_tipo_equipo"),
            signature.get("signature_marca"),
            signature.get("signature_modelo"),
            signature.get("signature_variante"),
            repuesto_payload.get("repuesto_key"),
            repuesto_payload.get("catalogo_repuesto_id"),
            repuesto_payload.get("nombre_repuesto"),
            repuesto_payload.get("periodicidad_valor"),
            repuesto_payload.get("periodicidad_unidad"),
            repuesto_payload.get("aviso_anticipacion_dias"),
            uid,
            uid,
        ],
    )


def _delete_template_item(signature, repuesto_key):
    if not signature:
        return
    exec_void(
        """
        DELETE FROM preventivo_repuesto_plantillas
        WHERE signature_key=%s
          AND repuesto_key=%s
        """,
        [signature.get("signature_key"), repuesto_key],
    )


def _insert_plan_repuesto(plan_id, repuesto_payload, uid, copy_dates=True):
    exists = q(
        """
        SELECT id
        FROM preventivo_plan_repuestos
        WHERE plan_id=%s
          AND repuesto_key=%s
          AND activa=true
        LIMIT 1
        """,
        [plan_id, repuesto_payload.get("repuesto_key")],
        one=True,
    )
    if exists:
        return None
    ultima = repuesto_payload.get("ultima_revision_fecha") if copy_dates else None
    proxima = repuesto_payload.get("proxima_revision_fecha") if copy_dates else None
    item_id = exec_returning(
        """
        INSERT INTO preventivo_plan_repuestos(
          plan_id, repuesto_key, catalogo_repuesto_id, nombre_repuesto,
          periodicidad_valor, periodicidad_unidad, aviso_anticipacion_dias,
          ultima_revision_fecha, proxima_revision_fecha,
          activa, created_by, updated_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)
        RETURNING id
        """,
        [
            plan_id,
            repuesto_payload.get("repuesto_key"),
            repuesto_payload.get("catalogo_repuesto_id"),
            repuesto_payload.get("nombre_repuesto"),
            repuesto_payload.get("periodicidad_valor"),
            repuesto_payload.get("periodicidad_unidad"),
            repuesto_payload.get("aviso_anticipacion_dias"),
            ultima,
            proxima,
            uid,
            uid,
        ],
    )
    return int(item_id)


def _seed_plan_repuestos_from_template(plan_id, device_id, uid):
    signature = _fetch_device_signature(device_id)
    if not signature:
        return 0
    rows = q(
        """
        SELECT
          repuesto_key,
          catalogo_repuesto_id,
          COALESCE(nombre_repuesto,'') AS nombre_repuesto,
          periodicidad_valor,
          periodicidad_unidad::text AS periodicidad_unidad,
          aviso_anticipacion_dias
        FROM preventivo_repuesto_plantillas
        WHERE signature_key=%s
          AND activa=true
        ORDER BY id ASC
        """,
        [signature.get("signature_key")],
    ) or []
    seeded = 0
    for row in rows:
        inserted = _insert_plan_repuesto(
            plan_id=plan_id,
            repuesto_payload={
                "repuesto_key": row.get("repuesto_key"),
                "catalogo_repuesto_id": row.get("catalogo_repuesto_id"),
                "nombre_repuesto": row.get("nombre_repuesto"),
                "periodicidad_valor": row.get("periodicidad_valor"),
                "periodicidad_unidad": row.get("periodicidad_unidad"),
                "aviso_anticipacion_dias": row.get("aviso_anticipacion_dias"),
                "ultima_revision_fecha": None,
                "proxima_revision_fecha": None,
            },
            uid=uid,
            copy_dates=False,
        )
        if inserted:
            seeded += 1
    return seeded


def _fetch_plan_repuestos_by_ids(plan_id, ids):
    if not ids:
        return []
    ids_int = []
    for raw in ids:
        try:
            ids_int.append(int(raw))
        except Exception:
            continue
    if not ids_int:
        return []
    ph = ",".join(["%s"] * len(ids_int))
    rows = q(
        f"""
        SELECT
          pr.id,
          pr.plan_id,
          pr.repuesto_key,
          COALESCE(pr.nombre_repuesto,'') AS nombre_repuesto,
          pr.periodicidad_valor,
          pr.periodicidad_unidad::text AS periodicidad_unidad,
          pr.aviso_anticipacion_dias,
          pr.ultima_revision_fecha,
          pr.proxima_revision_fecha
        FROM preventivo_plan_repuestos pr
        WHERE pr.plan_id=%s
          AND pr.id IN ({ph})
          AND pr.activa=true
        ORDER BY pr.id ASC
        """,
        [plan_id] + ids_int,
    ) or []
    return rows


def _fetch_revision(revision_id):
    return q(
        """
        SELECT
          r.id,
          r.plan_id,
          r.estado::text AS estado,
          r.fecha_programada,
          r.fecha_realizada,
          r.realizada_por,
          COALESCE(r.resumen,'') AS resumen,
          r.created_by,
          r.updated_by,
          r.created_at,
          r.updated_at,
          p.scope_type::text AS scope_type,
          p.device_id AS plan_device_id,
          p.customer_id AS plan_customer_id,
          p.periodicidad_valor,
          p.periodicidad_unidad::text AS periodicidad_unidad,
          p.aviso_anticipacion_dias,
          p.ultima_revision_fecha,
          p.proxima_revision_fecha,
          p.activa,
          COALESCE(p.observaciones,'') AS plan_observaciones,
          COALESCE(cplan.razon_social, cdev.razon_social, '') AS customer_nombre,
          COALESCE(cplan.cod_empresa, cdev.cod_empresa, '') AS customer_cod_empresa,
          COALESCE(d.numero_serie,'') AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno,
          COALESCE(b.nombre,'') AS marca,
          COALESCE(m.nombre,'') AS modelo,
          COALESCE(d.tipo_equipo,'') AS tipo_equipo,
          COALESCE(d.variante,'') AS variante
        FROM preventivo_revisiones r
        JOIN preventivo_planes p ON p.id = r.plan_id
        LEFT JOIN devices d ON d.id = p.device_id
        LEFT JOIN customers cdev ON cdev.id = d.customer_id
        LEFT JOIN customers cplan ON cplan.id = p.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        WHERE r.id = %s
        """,
        [revision_id],
        one=True,
    )


def _fetch_revision_items(revision_id):
    return q(
        """
        SELECT
          i.id,
          i.revision_id,
          i.orden,
          i.device_id,
          COALESCE(i.equipo_snapshot,'') AS equipo_snapshot,
          COALESCE(i.serie_snapshot,'') AS serie_snapshot,
          COALESCE(i.interno_snapshot,'') AS interno_snapshot,
          i.estado_item::text AS estado_item,
          COALESCE(i.motivo_no_control,'') AS motivo_no_control,
          COALESCE(i.ubicacion_detalle,'') AS ubicacion_detalle,
          COALESCE(i.accesorios_cambiados,false) AS accesorios_cambiados,
          COALESCE(i.accesorios_detalle,'') AS accesorios_detalle,
          COALESCE(i.notas,'') AS notas,
          COALESCE(i.arrastrar_proxima,true) AS arrastrar_proxima,
          i.created_at,
          i.updated_at
        FROM preventivo_revision_items i
        WHERE i.revision_id = %s
        ORDER BY i.orden ASC, i.id ASC
        """,
        [revision_id],
    ) or []

def _serialize_revision(rev):
    if not rev:
        return None
    plan = {
        "id": rev.get("plan_id"),
        "scope_type": rev.get("scope_type"),
        "device_id": rev.get("plan_device_id"),
        "customer_id": rev.get("plan_customer_id"),
        "periodicidad_valor": rev.get("periodicidad_valor"),
        "periodicidad_unidad": rev.get("periodicidad_unidad"),
        "aviso_anticipacion_dias": rev.get("aviso_anticipacion_dias"),
        "ultima_revision_fecha": rev.get("ultima_revision_fecha"),
        "proxima_revision_fecha": rev.get("proxima_revision_fecha"),
        "activa": rev.get("activa"),
        "observaciones": rev.get("plan_observaciones") or "",
    }
    plan = _serialize_plan(plan)
    return {
        "id": rev.get("id"),
        "plan_id": rev.get("plan_id"),
        "estado": rev.get("estado"),
        "fecha_programada": rev.get("fecha_programada"),
        "fecha_realizada": rev.get("fecha_realizada"),
        "realizada_por": rev.get("realizada_por"),
        "resumen": rev.get("resumen") or "",
        "created_by": rev.get("created_by"),
        "updated_by": rev.get("updated_by"),
        "created_at": rev.get("created_at"),
        "updated_at": rev.get("updated_at"),
        "scope_type": rev.get("scope_type"),
        "customer_id": rev.get("plan_customer_id"),
        "customer_nombre": rev.get("customer_nombre") or "",
        "customer_cod_empresa": rev.get("customer_cod_empresa") or "",
        "device_id": rev.get("plan_device_id"),
        "numero_serie": rev.get("numero_serie") or "",
        "numero_interno": rev.get("numero_interno") or "",
        "marca": rev.get("marca") or "",
        "modelo": rev.get("modelo") or "",
        "tipo_equipo": rev.get("tipo_equipo") or "",
        "variante": rev.get("variante") or "",
        "plan": plan,
    }


def _agenda_plan_rows(scope=None, customer_id=None, q_text=""):
    params = []
    wh = ["p.activa = true"]

    if scope in ("device", "customer"):
        wh.append("p.scope_type::text = %s")
        params.append(scope)
    if customer_id is not None:
        wh.append("COALESCE(p.customer_id, d.customer_id) = %s")
        params.append(customer_id)
    if q_text:
        like = f"%{q_text}%"
        wh.append(
            "("
            "LOWER(COALESCE(cc.razon_social, cdev.razon_social, '')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(cc.cod_empresa, cdev.cod_empresa, '')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(b.nombre,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(m.nombre,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(d.numero_serie,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(d.numero_interno,'')) LIKE LOWER(%s)"
            ")"
        )
        params.extend([like, like, like, like, like, like])

    where_sql = " WHERE " + " AND ".join(wh)
    try:
        return q(
            f"""
            SELECT
              p.id AS plan_id,
              p.scope_type::text AS scope_type,
              p.device_id,
              p.customer_id,
              COALESCE(p.customer_id, d.customer_id) AS owner_customer_id,
              COALESCE(cc.razon_social, cdev.razon_social, '') AS customer_nombre,
              COALESCE(cc.cod_empresa, cdev.cod_empresa, '') AS customer_cod_empresa,
              p.periodicidad_valor,
              p.periodicidad_unidad::text AS periodicidad_unidad,
              p.aviso_anticipacion_dias,
              p.ultima_revision_fecha,
              p.proxima_revision_fecha,
              p.activa,
              COALESCE(p.observaciones,'') AS observaciones,
              COALESCE(d.numero_serie,'') AS numero_serie,
              COALESCE(d.numero_interno,'') AS numero_interno,
              COALESCE(d.tipo_equipo,'') AS tipo_equipo,
              COALESCE(d.variante,'') AS variante,
              COALESCE(b.nombre,'') AS marca,
              COALESCE(m.nombre,'') AS modelo,
              COALESCE(prc.total_repuestos,0) AS repuestos_total,
              prn.repuesto_id AS repuesto_proximo_id,
              COALESCE(prn.repuesto_nombre,'') AS repuesto_proximo_nombre,
              prn.repuesto_ultima_revision_fecha,
              prn.repuesto_proxima_revision_fecha,
              prn.repuesto_aviso_anticipacion_dias,
              dr.id AS borrador_revision_id
            FROM preventivo_planes p
            LEFT JOIN devices d ON d.id = p.device_id
            LEFT JOIN customers cdev ON cdev.id = d.customer_id
            LEFT JOIN customers cc ON cc.id = p.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN LATERAL (
              SELECT r.id
                FROM preventivo_revisiones r
               WHERE r.plan_id = p.id
                 AND r.estado = 'borrador'
               ORDER BY r.id DESC
               LIMIT 1
            ) dr ON TRUE
            LEFT JOIN LATERAL (
              SELECT COUNT(*) AS total_repuestos
              FROM preventivo_plan_repuestos prc
              WHERE prc.plan_id = p.id
                AND prc.activa=true
            ) prc ON TRUE
            LEFT JOIN LATERAL (
              SELECT
                pr.id AS repuesto_id,
                COALESCE(pr.nombre_repuesto,'') AS repuesto_nombre,
                pr.ultima_revision_fecha AS repuesto_ultima_revision_fecha,
                pr.proxima_revision_fecha AS repuesto_proxima_revision_fecha,
                pr.aviso_anticipacion_dias AS repuesto_aviso_anticipacion_dias
              FROM preventivo_plan_repuestos pr
              WHERE pr.plan_id = p.id
                AND pr.activa=true
              ORDER BY
                CASE WHEN pr.proxima_revision_fecha IS NULL THEN 1 ELSE 0 END,
                pr.proxima_revision_fecha ASC,
                pr.id ASC
              LIMIT 1
            ) prn ON TRUE
            {where_sql}
            """,
            params,
        ) or []
    except Exception:
        rows = q(
            f"""
            SELECT
              p.id AS plan_id,
              p.scope_type::text AS scope_type,
              p.device_id,
              p.customer_id,
              COALESCE(p.customer_id, d.customer_id) AS owner_customer_id,
              COALESCE(cc.razon_social, cdev.razon_social, '') AS customer_nombre,
              COALESCE(cc.cod_empresa, cdev.cod_empresa, '') AS customer_cod_empresa,
              p.periodicidad_valor,
              p.periodicidad_unidad::text AS periodicidad_unidad,
              p.aviso_anticipacion_dias,
              p.ultima_revision_fecha,
              p.proxima_revision_fecha,
              p.activa,
              COALESCE(p.observaciones,'') AS observaciones,
              COALESCE(d.numero_serie,'') AS numero_serie,
              COALESCE(d.numero_interno,'') AS numero_interno,
              COALESCE(d.tipo_equipo,'') AS tipo_equipo,
              COALESCE(d.variante,'') AS variante,
              COALESCE(b.nombre,'') AS marca,
              COALESCE(m.nombre,'') AS modelo,
              dr.id AS borrador_revision_id
            FROM preventivo_planes p
            LEFT JOIN devices d ON d.id = p.device_id
            LEFT JOIN customers cdev ON cdev.id = d.customer_id
            LEFT JOIN customers cc ON cc.id = p.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN LATERAL (
              SELECT r.id
                FROM preventivo_revisiones r
               WHERE r.plan_id = p.id
                 AND r.estado = 'borrador'
               ORDER BY r.id DESC
               LIMIT 1
            ) dr ON TRUE
            {where_sql}
            """,
            params,
        ) or []
        for row in rows:
            row["repuestos_total"] = 0
            row["repuesto_proximo_id"] = None
            row["repuesto_proximo_nombre"] = ""
            row["repuesto_ultima_revision_fecha"] = None
            row["repuesto_proxima_revision_fecha"] = None
            row["repuesto_aviso_anticipacion_dias"] = None
        return rows


def _agenda_plan_items(scope=None, customer_id=None, q_text="", estado=None):
    rows = _agenda_plan_rows(scope=scope, customer_id=customer_id, q_text=q_text)
    items = []
    for row in rows:
        use_repuesto = (row.get("scope_type") == "device") and int(row.get("repuestos_total") or 0) > 0
        if use_repuesto:
            base_ultima = row.get("repuesto_ultima_revision_fecha")
            base_proxima = row.get("repuesto_proxima_revision_fecha")
            base_aviso = row.get("repuesto_aviso_anticipacion_dias")
            source = "repuesto"
        else:
            base_ultima = row.get("ultima_revision_fecha")
            base_proxima = row.get("proxima_revision_fecha")
            base_aviso = row.get("aviso_anticipacion_dias")
            source = "plan"
        state, days = _preventivo_state(
            base_proxima,
            base_aviso,
            has_plan=True,
        )
        if estado and state != estado:
            continue
        label = ""
        if row.get("scope_type") == "device":
            label = _device_label(row)
        items.append(
            {
                "scope_type": row.get("scope_type"),
                "plan_id": row.get("plan_id"),
                "device_id": row.get("device_id"),
                "customer_id": row.get("owner_customer_id"),
                "customer_nombre": row.get("customer_nombre") or "",
                "customer_cod_empresa": row.get("customer_cod_empresa") or "",
                "equipo_label": label,
                "marca": row.get("marca") or "",
                "modelo": row.get("modelo") or "",
                "numero_serie": row.get("numero_serie") or "",
                "numero_interno": row.get("numero_interno") or "",
                "periodicidad_valor": row.get("periodicidad_valor"),
                "periodicidad_unidad": row.get("periodicidad_unidad"),
                "aviso_anticipacion_dias": row.get("aviso_anticipacion_dias"),
                "ultima_revision_fecha": base_ultima,
                "proxima_revision_fecha": base_proxima,
                "preventivo_estado": state,
                "preventivo_dias_restantes": days,
                "preventivo_source": source,
                "repuestos_total": int(row.get("repuestos_total") or 0),
                "repuesto_proximo_id": row.get("repuesto_proximo_id"),
                "repuesto_proximo_nombre": row.get("repuesto_proximo_nombre") or "",
                "borrador_revision_id": row.get("borrador_revision_id"),
            }
        )
    return items


def _agenda_sin_plan_device(scope=None, customer_id=None, q_text="", only_with_plan_history=False):
    if scope == "customer":
        return []
    params = []
    wh = ["pp.id IS NULL"]
    if customer_id is not None:
        wh.append("d.customer_id = %s")
        params.append(customer_id)
    if only_with_plan_history:
        wh.append(
            "EXISTS ("
            "SELECT 1 FROM preventivo_planes ph "
            "WHERE ph.scope_type='device' AND ph.device_id=d.id"
            ")"
        )
    if q_text:
        like = f"%{q_text}%"
        wh.append(
            "("
            "LOWER(COALESCE(c.razon_social,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(c.cod_empresa,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(b.nombre,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(m.nombre,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(d.numero_serie,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(d.numero_interno,'')) LIKE LOWER(%s)"
            ")"
        )
        params.extend([like, like, like, like, like, like])
    where_sql = " WHERE " + " AND ".join(wh)
    rows = q(
        f"""
        SELECT
          d.id AS device_id,
          d.customer_id,
          COALESCE(c.razon_social,'') AS customer_nombre,
          COALESCE(c.cod_empresa,'') AS customer_cod_empresa,
          COALESCE(d.numero_serie,'') AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno,
          COALESCE(d.tipo_equipo,'') AS tipo_equipo,
          COALESCE(d.variante,'') AS variante,
          COALESCE(b.nombre,'') AS marca,
          COALESCE(m.nombre,'') AS modelo
        FROM devices d
        LEFT JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        LEFT JOIN LATERAL (
          SELECT p.id
            FROM preventivo_planes p
           WHERE p.scope_type='device'
             AND p.device_id=d.id
             AND p.activa=true
           ORDER BY p.id DESC
           LIMIT 1
        ) pp ON TRUE
        {where_sql}
        """,
        params,
    ) or []
    out = []
    for row in rows:
        out.append(
            {
                "scope_type": "device",
                "plan_id": None,
                "device_id": row.get("device_id"),
                "customer_id": row.get("customer_id"),
                "customer_nombre": row.get("customer_nombre") or "",
                "customer_cod_empresa": row.get("customer_cod_empresa") or "",
                "equipo_label": _device_label(row),
                "marca": row.get("marca") or "",
                "modelo": row.get("modelo") or "",
                "numero_serie": row.get("numero_serie") or "",
                "numero_interno": row.get("numero_interno") or "",
                "periodicidad_valor": None,
                "periodicidad_unidad": None,
                "aviso_anticipacion_dias": None,
                "ultima_revision_fecha": None,
                "proxima_revision_fecha": None,
                "preventivo_estado": "sin_plan",
                "preventivo_dias_restantes": None,
                "preventivo_source": "plan",
                "repuestos_total": 0,
                "repuesto_proximo_id": None,
                "repuesto_proximo_nombre": "",
                "borrador_revision_id": None,
            }
        )
    return out


def _agenda_sin_plan_customer(scope=None, customer_id=None, q_text="", only_with_plan_history=False):
    if scope == "device":
        return []
    params = []
    wh = ["pp.id IS NULL"]
    if customer_id is not None:
        wh.append("c.id = %s")
        params.append(customer_id)
    if only_with_plan_history:
        wh.append(
            "EXISTS ("
            "SELECT 1 FROM preventivo_planes ph "
            "WHERE ph.scope_type='customer' AND ph.customer_id=c.id"
            ")"
        )
    if q_text:
        like = f"%{q_text}%"
        wh.append(
            "("
            "LOWER(COALESCE(c.razon_social,'')) LIKE LOWER(%s) OR "
            "LOWER(COALESCE(c.cod_empresa,'')) LIKE LOWER(%s)"
            ")"
        )
        params.extend([like, like])
    where_sql = " WHERE " + " AND ".join(wh)
    rows = q(
        f"""
        SELECT
          c.id AS customer_id,
          COALESCE(c.razon_social,'') AS customer_nombre,
          COALESCE(c.cod_empresa,'') AS customer_cod_empresa
        FROM customers c
        LEFT JOIN LATERAL (
          SELECT p.id
            FROM preventivo_planes p
           WHERE p.scope_type='customer'
             AND p.customer_id=c.id
             AND p.activa=true
           ORDER BY p.id DESC
           LIMIT 1
        ) pp ON TRUE
        {where_sql}
        """,
        params,
    ) or []
    return [
        {
            "scope_type": "customer",
            "plan_id": None,
            "device_id": None,
            "customer_id": r.get("customer_id"),
            "customer_nombre": r.get("customer_nombre") or "",
            "customer_cod_empresa": r.get("customer_cod_empresa") or "",
            "equipo_label": "",
            "marca": "",
            "modelo": "",
            "numero_serie": "",
            "numero_interno": "",
            "periodicidad_valor": None,
            "periodicidad_unidad": None,
            "aviso_anticipacion_dias": None,
            "ultima_revision_fecha": None,
            "proxima_revision_fecha": None,
            "preventivo_estado": "sin_plan",
            "preventivo_dias_restantes": None,
            "preventivo_source": "plan",
            "repuestos_total": 0,
            "repuesto_proximo_id": None,
            "repuesto_proximo_nombre": "",
            "borrador_revision_id": None,
        }
        for r in rows
    ]


def _collect_agenda(scope=None, customer_id=None, q_text="", only_with_plan=False):
    items = _agenda_plan_items(scope=scope, customer_id=customer_id, q_text=q_text, estado=None)
    if only_with_plan:
        items.extend(
            _agenda_sin_plan_device(
                scope=scope,
                customer_id=customer_id,
                q_text=q_text,
                only_with_plan_history=True,
            )
        )
        items.extend(
            _agenda_sin_plan_customer(
                scope=scope,
                customer_id=customer_id,
                q_text=q_text,
                only_with_plan_history=True,
            )
        )
    else:
        items.extend(_agenda_sin_plan_device(scope=scope, customer_id=customer_id, q_text=q_text))
        items.extend(_agenda_sin_plan_customer(scope=scope, customer_id=customer_id, q_text=q_text))
    return items


def _agenda_sort(items):
    prio = {"vencido": 0, "proximo": 1, "sin_plan": 2, "al_dia": 3}

    def _k(it):
        state = (it.get("preventivo_estado") or "al_dia").strip().lower()
        due = it.get("proxima_revision_fecha")
        due_key = due if due is not None else dt.date.max
        return (prio.get(state, 9), due_key, int(it.get("plan_id") or 0), int(it.get("device_id") or 0), int(it.get("customer_id") or 0))

    return sorted(items, key=_k)


def _agenda_counts(items):
    counts = {"vencido": 0, "proximo": 0, "sin_plan": 0, "al_dia": 0}
    for it in items:
        st = (it.get("preventivo_estado") or "").strip().lower()
        if st in counts:
            counts[st] += 1
    counts["total"] = sum(counts.values())
    return counts


def _next_item_order(revision_id):
    row = q(
        "SELECT COALESCE(MAX(orden),0) + 1 AS n FROM preventivo_revision_items WHERE revision_id=%s",
        [revision_id],
        one=True,
    ) or {}
    return int(row.get("n") or 1)


def _normalize_item_payload(data, device_id=None):
    estado_item = (data.get("estado_item") or "pendiente").strip().lower()
    if estado_item not in _ITEM_STATES:
        raise ValidationError({"estado_item": "debe ser pendiente|ok|retirado|no_controlado"})
    motivo = (data.get("motivo_no_control") or "").strip() or None
    if estado_item == "no_controlado" and not motivo:
        raise ValidationError({"motivo_no_control": "requerido para no_controlado"})
    arr = _parse_bool(data.get("arrastrar_proxima"), None)
    if arr is None:
        arr = False if estado_item == "retirado" else True

    snapshot = None
    if device_id:
        snapshot = _fetch_device_snapshot(device_id)
        if not snapshot:
            raise ValidationError({"device_id": "equipo inexistente"})

    equipo_snapshot = (data.get("equipo_snapshot") or "").strip()
    serie_snapshot = (data.get("serie_snapshot") or "").strip()
    interno_snapshot = (data.get("interno_snapshot") or "").strip()
    if snapshot:
        if not equipo_snapshot:
            equipo_snapshot = _device_label(snapshot)
        if not serie_snapshot:
            serie_snapshot = snapshot.get("numero_serie") or ""
        if not interno_snapshot:
            interno_snapshot = snapshot.get("numero_interno") or ""

    if not device_id and not equipo_snapshot:
        raise ValidationError({"equipo_snapshot": "requerido para item libre"})

    return {
        "device_id": device_id,
        "equipo_snapshot": equipo_snapshot or None,
        "serie_snapshot": serie_snapshot or None,
        "interno_snapshot": interno_snapshot or None,
        "estado_item": estado_item,
        "motivo_no_control": motivo,
        "ubicacion_detalle": (data.get("ubicacion_detalle") or "").strip() or None,
        "accesorios_cambiados": bool(_parse_bool(data.get("accesorios_cambiados"), False)),
        "accesorios_detalle": (data.get("accesorios_detalle") or "").strip() or None,
        "notas": (data.get("notas") or "").strip() or None,
        "arrastrar_proxima": bool(arr),
    }


def _seed_customer_revision_items(revision_id, customer_id, last_closed_revision_id=None):
    existing_device_ids = set()
    order = 1

    if last_closed_revision_id:
        previous_items = q(
            """
            SELECT
              device_id,
              COALESCE(equipo_snapshot,'') AS equipo_snapshot,
              COALESCE(serie_snapshot,'') AS serie_snapshot,
              COALESCE(interno_snapshot,'') AS interno_snapshot,
              estado_item::text AS estado_item,
              COALESCE(motivo_no_control,'') AS motivo_no_control,
              COALESCE(ubicacion_detalle,'') AS ubicacion_detalle,
              COALESCE(accesorios_cambiados,false) AS accesorios_cambiados,
              COALESCE(accesorios_detalle,'') AS accesorios_detalle,
              COALESCE(notas,'') AS notas,
              COALESCE(arrastrar_proxima,true) AS arrastrar_proxima
            FROM preventivo_revision_items
            WHERE revision_id = %s
              AND COALESCE(arrastrar_proxima,true) = true
            ORDER BY orden ASC, id ASC
            """,
            [last_closed_revision_id],
        ) or []
        for prev in previous_items:
            did = prev.get("device_id")
            if did:
                existing_device_ids.add(int(did))
            exec_void(
                """
                INSERT INTO preventivo_revision_items(
                  revision_id, orden, device_id, equipo_snapshot, serie_snapshot, interno_snapshot,
                  estado_item, motivo_no_control, ubicacion_detalle,
                  accesorios_cambiados, accesorios_detalle, notas, arrastrar_proxima
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    revision_id,
                    order,
                    did,
                    prev.get("equipo_snapshot") or None,
                    prev.get("serie_snapshot") or None,
                    prev.get("interno_snapshot") or None,
                    prev.get("estado_item") or "pendiente",
                    (prev.get("motivo_no_control") or "").strip() or None,
                    (prev.get("ubicacion_detalle") or "").strip() or None,
                    bool(prev.get("accesorios_cambiados")),
                    (prev.get("accesorios_detalle") or "").strip() or None,
                    (prev.get("notas") or "").strip() or None,
                    bool(prev.get("arrastrar_proxima")),
                ],
            )
            order += 1

    customer_devices = q(
        """
        SELECT d.id,
               COALESCE(d.numero_serie,'') AS numero_serie,
               COALESCE(d.numero_interno,'') AS numero_interno,
               COALESCE(d.tipo_equipo,'') AS tipo_equipo,
               COALESCE(d.variante,'') AS variante,
               COALESCE(b.nombre,'') AS marca,
               COALESCE(m.nombre,'') AS modelo
          FROM devices d
          LEFT JOIN marcas b ON b.id = d.marca_id
          LEFT JOIN models m ON m.id = d.model_id
         WHERE d.customer_id = %s
         ORDER BY d.id ASC
        """,
        [customer_id],
    ) or []

    for dev in customer_devices:
        did = int(dev.get("id"))
        if did in existing_device_ids:
            continue
        exec_void(
            """
            INSERT INTO preventivo_revision_items(
              revision_id, orden, device_id, equipo_snapshot, serie_snapshot, interno_snapshot,
              estado_item, motivo_no_control, ubicacion_detalle,
              accesorios_cambiados, accesorios_detalle, notas, arrastrar_proxima
            ) VALUES (%s, %s, %s, %s, %s, %s, 'pendiente', NULL, NULL, false, NULL, NULL, true)
            """,
            [
                revision_id,
                order,
                did,
                _device_label(dev),
                (dev.get("numero_serie") or "").strip() or None,
                (dev.get("numero_interno") or "").strip() or None,
            ],
        )
        order += 1


def _require_existing_customer(customer_id):
    if not q("SELECT id FROM customers WHERE id=%s", [customer_id], one=True):
        return False
    return True


def _require_existing_device(device_id):
    if not q("SELECT id FROM devices WHERE id=%s", [device_id], one=True):
        return False
    return True


def _fetch_plan_repuesto_item(plan_id, item_id):
    return q(
        """
        SELECT
          pr.id,
          pr.plan_id,
          pr.repuesto_key,
          pr.catalogo_repuesto_id,
          COALESCE(pr.nombre_repuesto,'') AS nombre_repuesto,
          pr.periodicidad_valor,
          pr.periodicidad_unidad::text AS periodicidad_unidad,
          pr.aviso_anticipacion_dias,
          pr.ultima_revision_fecha,
          pr.proxima_revision_fecha,
          pr.activa,
          pr.created_by,
          pr.updated_by,
          pr.created_at,
          pr.updated_at
        FROM preventivo_plan_repuestos pr
        WHERE pr.plan_id=%s
          AND pr.id=%s
          AND pr.activa=true
        """,
        [plan_id, item_id],
        one=True,
    )


def _fetch_plan_repuesto_item_by_key(plan_id, repuesto_key):
    return q(
        """
        SELECT
          pr.id,
          pr.plan_id,
          pr.repuesto_key,
          pr.catalogo_repuesto_id,
          COALESCE(pr.nombre_repuesto,'') AS nombre_repuesto,
          pr.periodicidad_valor,
          pr.periodicidad_unidad::text AS periodicidad_unidad,
          pr.aviso_anticipacion_dias,
          pr.ultima_revision_fecha,
          pr.proxima_revision_fecha,
          pr.activa
        FROM preventivo_plan_repuestos pr
        WHERE pr.plan_id=%s
          AND pr.repuesto_key=%s
          AND pr.activa=true
        LIMIT 1
        """,
        [plan_id, repuesto_key],
        one=True,
    )


def _equivalent_plan_ids_from_plan(plan_id):
    signature = _fetch_plan_device_signature(plan_id)
    if not signature:
        return [], None
    return _equivalent_active_plan_ids_by_signature(signature), signature


class DevicePreventivoPlanView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, device_id: int):
        require_roles(request, _PLAN_ROLES)
        _set_audit_user(request)
        if not _require_existing_device(device_id):
            return Response({"detail": "Equipo inexistente"}, status=404)
        if _fetch_active_plan("device", device_id):
            return Response({"detail": "Ya existe un plan activo"}, status=409)

        data = request.data or {}
        default_lead = int(getattr(settings, "PREVENTIVO_DEFAULT_LEAD_DAYS", 30) or 30)
        val, unit, aviso = _validate_period(data, default_lead)
        ultima = _parse_date(data.get("ultima_revision_fecha"), "ultima_revision_fecha")
        proxima = _parse_date(data.get("proxima_revision_fecha"), "proxima_revision_fecha")
        if not proxima and ultima:
            proxima = _add_period(ultima, val, unit)

        uid = _uid(request)
        plan_id = exec_returning(
            """
            INSERT INTO preventivo_planes(
              scope_type, device_id, periodicidad_valor, periodicidad_unidad,
              aviso_anticipacion_dias, ultima_revision_fecha, proxima_revision_fecha,
              activa, observaciones, created_by, updated_by
            ) VALUES ('device', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [
                device_id,
                val,
                unit,
                aviso,
                ultima,
                proxima,
                bool(_parse_bool(data.get("activa"), True)),
                (data.get("observaciones") or "").strip() or None,
                uid,
                uid,
            ],
        )
        seeded_repuestos = _seed_plan_repuestos_from_template(plan_id=int(plan_id), device_id=device_id, uid=uid)
        return Response(
            {
                "ok": True,
                "plan_id": plan_id,
                "seeded_repuestos": seeded_repuestos,
                "plan": _serialize_plan(_fetch_active_plan("device", device_id)),
            },
            status=201,
        )

    @transaction.atomic
    def patch(self, request, device_id: int):
        require_roles(request, _PLAN_ROLES)
        _set_audit_user(request)
        plan = _fetch_active_plan("device", device_id)
        if not plan:
            return Response({"detail": "Plan activo inexistente"}, status=404)

        d = request.data or {}
        merged = dict(plan)
        changed_period_or_last = False
        if "periodicidad_valor" in d:
            merged["periodicidad_valor"] = _parse_int(d.get("periodicidad_valor"), "periodicidad_valor", required=True, min_value=1)
            changed_period_or_last = True
        if "periodicidad_unidad" in d:
            merged["periodicidad_unidad"] = (d.get("periodicidad_unidad") or "").strip().lower()
            changed_period_or_last = True
        if int(merged.get("periodicidad_valor") or 0) <= 0:
            raise ValidationError({"periodicidad_valor": "debe ser mayor a 0"})
        if merged.get("periodicidad_unidad") not in _PERIOD_UNITS:
            raise ValidationError({"periodicidad_unidad": "debe ser dias|meses|anios"})

        if "aviso_anticipacion_dias" in d:
            merged["aviso_anticipacion_dias"] = _parse_int(d.get("aviso_anticipacion_dias"), "aviso_anticipacion_dias", required=True, min_value=0)
        if int(merged.get("aviso_anticipacion_dias") or 0) < 0:
            raise ValidationError({"aviso_anticipacion_dias": "no puede ser negativo"})

        if "ultima_revision_fecha" in d:
            merged["ultima_revision_fecha"] = _parse_date(d.get("ultima_revision_fecha"), "ultima_revision_fecha")
            changed_period_or_last = True
        if "proxima_revision_fecha" in d:
            merged["proxima_revision_fecha"] = _parse_date(d.get("proxima_revision_fecha"), "proxima_revision_fecha")
        elif changed_period_or_last and merged.get("ultima_revision_fecha"):
            merged["proxima_revision_fecha"] = _add_period(
                merged.get("ultima_revision_fecha"),
                int(merged.get("periodicidad_valor") or 0),
                merged.get("periodicidad_unidad"),
            )
        if "activa" in d:
            merged["activa"] = bool(_parse_bool(d.get("activa"), True))
        if "observaciones" in d:
            merged["observaciones"] = (d.get("observaciones") or "").strip() or None

        exec_void(
            """
            UPDATE preventivo_planes
               SET periodicidad_valor=%s,
                   periodicidad_unidad=%s,
                   aviso_anticipacion_dias=%s,
                   ultima_revision_fecha=%s,
                   proxima_revision_fecha=%s,
                   activa=%s,
                   observaciones=%s,
                   updated_by=%s
             WHERE id=%s
            """,
            [
                int(merged.get("periodicidad_valor") or 0),
                merged.get("periodicidad_unidad"),
                int(merged.get("aviso_anticipacion_dias") or 0),
                merged.get("ultima_revision_fecha"),
                merged.get("proxima_revision_fecha"),
                bool(merged.get("activa")),
                merged.get("observaciones"),
                _uid(request),
                plan.get("id"),
            ],
        )
        return Response({"ok": True, "plan": _serialize_plan(_fetch_active_plan("device", device_id) or merged)})


class DevicePreventivoRevisionCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, device_id: int):
        require_roles(request, _REVISION_ROLES)
        _set_audit_user(request)
        plan = _fetch_active_plan("device", device_id)
        if not plan:
            return Response({"detail": "El equipo no tiene plan activo"}, status=404)

        d = request.data or {}
        fecha_realizada = _parse_date(d.get("fecha_realizada"), "fecha_realizada") or dt.date.today()
        uid = _uid(request)
        repuesto_ids_raw = d.get("repuesto_ids")
        repuesto_ids = None
        if repuesto_ids_raw in (None, ""):
            repuesto_ids = None
        elif isinstance(repuesto_ids_raw, (list, tuple)):
            tmp = []
            for raw in repuesto_ids_raw:
                parsed = _parse_int(raw, "repuesto_ids", required=True, min_value=1)
                if parsed not in tmp:
                    tmp.append(parsed)
            if not tmp:
                raise ValidationError({"repuesto_ids": "debe incluir al menos un repuesto"})
            repuesto_ids = tmp
        else:
            raise ValidationError({"repuesto_ids": "debe ser lista de ids"})

        revision_id = exec_returning(
            """
            INSERT INTO preventivo_revisiones(
              plan_id, estado, fecha_programada, fecha_realizada,
              realizada_por, resumen, created_by, updated_by
            ) VALUES (%s, 'cerrada', %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [
                plan.get("id"),
                plan.get("proxima_revision_fecha"),
                fecha_realizada,
                uid,
                (d.get("resumen") or "").strip() or None,
                uid,
                uid,
            ],
        )
        if repuesto_ids:
            repuestos = _fetch_plan_repuestos_by_ids(plan.get("id"), repuesto_ids)
            by_id = {int(r.get("id")): r for r in repuestos if r.get("id") is not None}
            missing = [rid for rid in repuesto_ids if rid not in by_id]
            if missing:
                raise ValidationError({"repuesto_ids": f"ids inexistentes en el plan: {missing}"})
            for orden, rid in enumerate(repuesto_ids, start=1):
                rep = by_id.get(rid)
                rep_name = _txt_norm(rep.get("nombre_repuesto")) or f"Repuesto #{rid}"
                exec_void(
                    """
                    INSERT INTO preventivo_revision_items(
                      revision_id, orden, device_id, equipo_snapshot, serie_snapshot,
                      interno_snapshot, estado_item, motivo_no_control, ubicacion_detalle,
                      accesorios_cambiados, accesorios_detalle, notas, arrastrar_proxima
                    ) VALUES (%s, %s, NULL, %s, NULL, NULL, 'ok', NULL, NULL, false, NULL, NULL, true)
                    """,
                    [
                        revision_id,
                        orden,
                        f"Repuesto: {rep_name}",
                    ],
                )
                proxima_rep = _add_period(
                    fecha_realizada,
                    int(rep.get("periodicidad_valor") or 0),
                    rep.get("periodicidad_unidad"),
                )
                exec_void(
                    """
                    UPDATE preventivo_plan_repuestos
                       SET ultima_revision_fecha=%s,
                           proxima_revision_fecha=%s,
                           updated_by=%s
                     WHERE id=%s
                    """,
                    [fecha_realizada, proxima_rep, uid, rid],
                )
        else:
            normalized = _normalize_item_payload(d, device_id=device_id)
            exec_void(
                """
                INSERT INTO preventivo_revision_items(
                  revision_id, orden, device_id, equipo_snapshot, serie_snapshot,
                  interno_snapshot, estado_item, motivo_no_control, ubicacion_detalle,
                  accesorios_cambiados, accesorios_detalle, notas, arrastrar_proxima
                ) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    revision_id,
                    normalized.get("device_id"),
                    normalized.get("equipo_snapshot"),
                    normalized.get("serie_snapshot"),
                    normalized.get("interno_snapshot"),
                    normalized.get("estado_item"),
                    normalized.get("motivo_no_control"),
                    normalized.get("ubicacion_detalle"),
                    normalized.get("accesorios_cambiados"),
                    normalized.get("accesorios_detalle"),
                    normalized.get("notas"),
                    normalized.get("arrastrar_proxima"),
                ],
            )

        proxima = _add_period(
            fecha_realizada,
            int(plan.get("periodicidad_valor") or 0),
            plan.get("periodicidad_unidad"),
        )
        exec_void(
            """
            UPDATE preventivo_planes
               SET ultima_revision_fecha=%s,
                   proxima_revision_fecha=%s,
                   updated_by=%s
             WHERE id=%s
            """,
            [fecha_realizada, proxima, uid, plan.get("id")],
        )

        rev = _fetch_revision(revision_id)
        return Response(
            {
                "ok": True,
                "revision": _serialize_revision(rev),
                "items": _fetch_revision_items(revision_id),
                "plan": _serialize_plan(_fetch_active_plan("device", device_id)),
            },
            status=201,
        )


class DevicePreventivoRepuestosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, device_id: int):
        require_roles(request, _VIEW_ROLES)
        if not _require_existing_device(device_id):
            return Response({"detail": "Equipo inexistente"}, status=404)
        plan = _fetch_active_plan("device", device_id)
        if not plan:
            return Response({"plan": None, "items": [], "total": 0})
        items = _fetch_plan_repuestos(plan.get("id"))
        effective = _resolve_effective_plan_dates(plan) or {}
        return Response(
            {
                "plan": _serialize_plan(plan),
                "effective": effective,
                "items": items,
                "total": len(items),
            }
        )

    @transaction.atomic
    def post(self, request, device_id: int):
        require_roles(request, _PLAN_ROLES)
        _set_audit_user(request)
        if not _require_existing_device(device_id):
            return Response({"detail": "Equipo inexistente"}, status=404)
        plan = _fetch_active_plan("device", device_id)
        if not plan:
            return Response({"detail": "El equipo no tiene plan activo"}, status=404)

        default_lead = int(getattr(settings, "PREVENTIVO_DEFAULT_LEAD_DAYS", 30) or 30)
        payload = _parse_repuesto_payload(request.data or {}, default_lead)
        existing_origin = _fetch_plan_repuesto_item_by_key(plan.get("id"), payload.get("repuesto_key"))
        if existing_origin:
            return Response({"detail": "El repuesto ya existe en este plan"}, status=409)

        uid = _uid(request)
        plan_ids, signature = _equivalent_plan_ids_from_plan(plan.get("id"))
        if not plan_ids:
            plan_ids = [int(plan.get("id"))]

        origin_id = _insert_plan_repuesto(plan.get("id"), payload, uid, copy_dates=True)
        replicated = 0
        skipped = 0
        for pid in plan_ids:
            if int(pid) == int(plan.get("id")):
                continue
            inserted = _insert_plan_repuesto(int(pid), payload, uid, copy_dates=False)
            if inserted:
                replicated += 1
            else:
                skipped += 1
        _upsert_template_item(signature, payload, uid)
        created = _fetch_plan_repuesto_item(plan.get("id"), origin_id)
        return Response(
            {
                "ok": True,
                "item": _serialize_plan_repuesto(created),
                "replicacion": {
                    "planes_equivalentes": max(0, len(plan_ids) - 1),
                    "insertados": replicated,
                    "omitidos_existentes": skipped,
                },
            },
            status=201,
        )


class DevicePreventivoRepuestoDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, device_id: int, item_id: int):
        require_roles(request, _PLAN_ROLES)
        _set_audit_user(request)
        if not _require_existing_device(device_id):
            return Response({"detail": "Equipo inexistente"}, status=404)
        plan = _fetch_active_plan("device", device_id)
        if not plan:
            return Response({"detail": "El equipo no tiene plan activo"}, status=404)
        current = _fetch_plan_repuesto_item(plan.get("id"), item_id)
        if not current:
            return Response({"detail": "Repuesto preventivo inexistente"}, status=404)

        d = request.data or {}
        merged = dict(current)
        changed_period_or_last = False
        if "catalogo_repuesto_id" in d:
            raw_cat = d.get("catalogo_repuesto_id")
            if raw_cat in (None, ""):
                new_cat = None
            else:
                new_cat = _parse_int(raw_cat, "catalogo_repuesto_id", required=True, min_value=1)
            if int(new_cat or 0) != int(current.get("catalogo_repuesto_id") or 0):
                raise ValidationError({"catalogo_repuesto_id": "no se puede modificar en una edición global"})

        if "nombre_repuesto" in d:
            merged["nombre_repuesto"] = _txt_norm(d.get("nombre_repuesto"))
            if not merged["nombre_repuesto"]:
                raise ValidationError({"nombre_repuesto": "requerido"})
        if "periodicidad_valor" in d:
            merged["periodicidad_valor"] = _parse_int(d.get("periodicidad_valor"), "periodicidad_valor", required=True, min_value=1)
            changed_period_or_last = True
        if "periodicidad_unidad" in d:
            merged["periodicidad_unidad"] = (d.get("periodicidad_unidad") or "").strip().lower()
            changed_period_or_last = True
        if merged.get("periodicidad_unidad") not in _PERIOD_UNITS:
            raise ValidationError({"periodicidad_unidad": "debe ser dias|meses|anios"})
        if "aviso_anticipacion_dias" in d:
            merged["aviso_anticipacion_dias"] = _parse_int(d.get("aviso_anticipacion_dias"), "aviso_anticipacion_dias", required=True, min_value=0)
        if "ultima_revision_fecha" in d:
            merged["ultima_revision_fecha"] = _parse_date(d.get("ultima_revision_fecha"), "ultima_revision_fecha")
            changed_period_or_last = True
        if "proxima_revision_fecha" in d:
            merged["proxima_revision_fecha"] = _parse_date(d.get("proxima_revision_fecha"), "proxima_revision_fecha")
        elif changed_period_or_last and merged.get("ultima_revision_fecha"):
            merged["proxima_revision_fecha"] = _add_period(
                merged.get("ultima_revision_fecha"),
                int(merged.get("periodicidad_valor") or 0),
                merged.get("periodicidad_unidad"),
            )

        uid = _uid(request)
        repuesto_key = current.get("repuesto_key")
        plan_ids, signature = _equivalent_plan_ids_from_plan(plan.get("id"))
        if not plan_ids:
            plan_ids = [int(plan.get("id"))]

        replicated_updated = 0
        replicated_inserted = 0
        for pid in plan_ids:
            target = _fetch_plan_repuesto_item_by_key(pid, repuesto_key)
            if int(pid) == int(plan.get("id")):
                exec_void(
                    """
                    UPDATE preventivo_plan_repuestos
                       SET nombre_repuesto=%s,
                           periodicidad_valor=%s,
                           periodicidad_unidad=%s,
                           aviso_anticipacion_dias=%s,
                           ultima_revision_fecha=%s,
                           proxima_revision_fecha=%s,
                           updated_by=%s
                     WHERE id=%s
                    """,
                    [
                        merged.get("nombre_repuesto"),
                        merged.get("periodicidad_valor"),
                        merged.get("periodicidad_unidad"),
                        merged.get("aviso_anticipacion_dias"),
                        merged.get("ultima_revision_fecha"),
                        merged.get("proxima_revision_fecha"),
                        uid,
                        current.get("id"),
                    ],
                )
                continue

            if target:
                exec_void(
                    """
                    UPDATE preventivo_plan_repuestos
                       SET nombre_repuesto=%s,
                           periodicidad_valor=%s,
                           periodicidad_unidad=%s,
                           aviso_anticipacion_dias=%s,
                           updated_by=%s
                     WHERE id=%s
                    """,
                    [
                        merged.get("nombre_repuesto"),
                        merged.get("periodicidad_valor"),
                        merged.get("periodicidad_unidad"),
                        merged.get("aviso_anticipacion_dias"),
                        uid,
                        target.get("id"),
                    ],
                )
                replicated_updated += 1
            else:
                inserted = _insert_plan_repuesto(
                    plan_id=int(pid),
                    repuesto_payload={
                        "repuesto_key": repuesto_key,
                        "catalogo_repuesto_id": current.get("catalogo_repuesto_id"),
                        "nombre_repuesto": merged.get("nombre_repuesto"),
                        "periodicidad_valor": merged.get("periodicidad_valor"),
                        "periodicidad_unidad": merged.get("periodicidad_unidad"),
                        "aviso_anticipacion_dias": merged.get("aviso_anticipacion_dias"),
                        "ultima_revision_fecha": None,
                        "proxima_revision_fecha": None,
                    },
                    uid=uid,
                    copy_dates=False,
                )
                if inserted:
                    replicated_inserted += 1

        _upsert_template_item(
            signature,
            {
                "repuesto_key": repuesto_key,
                "catalogo_repuesto_id": current.get("catalogo_repuesto_id"),
                "nombre_repuesto": merged.get("nombre_repuesto"),
                "periodicidad_valor": merged.get("periodicidad_valor"),
                "periodicidad_unidad": merged.get("periodicidad_unidad"),
                "aviso_anticipacion_dias": merged.get("aviso_anticipacion_dias"),
            },
            uid,
        )
        updated = _fetch_plan_repuesto_item(plan.get("id"), item_id)
        return Response(
            {
                "ok": True,
                "item": _serialize_plan_repuesto(updated),
                "replicacion": {
                    "planes_equivalentes": max(0, len(plan_ids) - 1),
                    "actualizados": replicated_updated,
                    "insertados_faltantes": replicated_inserted,
                },
            }
        )

    @transaction.atomic
    def delete(self, request, device_id: int, item_id: int):
        require_roles(request, _PLAN_ROLES)
        _set_audit_user(request)
        if not _require_existing_device(device_id):
            return Response({"detail": "Equipo inexistente"}, status=404)
        plan = _fetch_active_plan("device", device_id)
        if not plan:
            return Response({"detail": "El equipo no tiene plan activo"}, status=404)
        current = _fetch_plan_repuesto_item(plan.get("id"), item_id)
        if not current:
            return Response({"detail": "Repuesto preventivo inexistente"}, status=404)

        repuesto_key = current.get("repuesto_key")
        plan_ids, signature = _equivalent_plan_ids_from_plan(plan.get("id"))
        if not plan_ids:
            plan_ids = [int(plan.get("id"))]

        deleted = 0
        for pid in plan_ids:
            row = q(
                """
                SELECT COUNT(*) AS n
                FROM preventivo_plan_repuestos
                WHERE plan_id=%s
                  AND repuesto_key=%s
                  AND activa=true
                """,
                [pid, repuesto_key],
                one=True,
            ) or {}
            deleted += int(row.get("n") or 0)
            exec_void(
                """
                DELETE FROM preventivo_plan_repuestos
                WHERE plan_id=%s
                  AND repuesto_key=%s
                  AND activa=true
                """,
                [pid, repuesto_key],
            )
        _delete_template_item(signature, repuesto_key)
        return Response(
            {
                "ok": True,
                "deleted": deleted,
                "planes_equivalentes": max(0, len(plan_ids) - 1),
            }
        )


class PreventivoAgendaView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, _VIEW_ROLES)
        scope = (request.GET.get("scope") or "").strip().lower() or None
        estado = (request.GET.get("estado") or "").strip().lower() or None
        q_text = (request.GET.get("q") or "").strip()
        customer_id = request.GET.get("customer_id")
        only_with_plan = bool(_parse_bool(request.GET.get("only_with_plan"), False))
        limit = _parse_int(request.GET.get("limit"), "limit", required=False, default=500, min_value=1)
        limit = min(limit, 2000)

        if scope and scope not in ("device", "customer"):
            raise ValidationError({"scope": "debe ser device|customer"})
        if estado and estado not in ("sin_plan", "al_dia", "proximo", "vencido"):
            raise ValidationError({"estado": "debe ser sin_plan|al_dia|próximo|vencido"})
        cid = None
        if customer_id not in (None, ""):
            cid = _parse_int(customer_id, "customer_id", required=True, min_value=1)

        all_items = _collect_agenda(
            scope=scope,
            customer_id=cid,
            q_text=q_text,
            only_with_plan=only_with_plan,
        )
        counts = _agenda_counts(all_items)
        items = all_items
        if estado:
            items = [it for it in items if (it.get("preventivo_estado") or "") == estado]
        items = _agenda_sort(items)
        if limit > 0:
            items = items[:limit]
        return Response({"items": items, "counts": counts, "total": len(items)})


class PreventivoClientesListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, _VIEW_ROLES)
        estado = (request.GET.get("preventivo_estado") or "").strip().lower() or None
        q_text = (request.GET.get("q") or "").strip()
        if estado and estado not in ("sin_plan", "al_dia", "proximo", "vencido"):
            raise ValidationError({"preventivo_estado": "debe ser sin_plan|al_dia|próximo|vencido"})

        params = []
        wh = ["1=1"]
        if q_text:
            like = f"%{q_text}%"
            wh.append(
                "("
                "LOWER(COALESCE(c.razon_social,'')) LIKE LOWER(%s) OR "
                "LOWER(COALESCE(c.cod_empresa,'')) LIKE LOWER(%s)"
                ")"
            )
            params.extend([like, like])
        where_sql = " WHERE " + " AND ".join(wh)
        rows = q(
            f"""
            SELECT
              c.id AS customer_id,
              COALESCE(c.razon_social,'') AS razon_social,
              COALESCE(c.cod_empresa,'') AS cod_empresa,
              COALESCE(c.telefono,'') AS telefono,
              COALESCE(c.telefono_2,'') AS telefono_2,
              COALESCE(c.email,'') AS email,
              p.id AS preventivo_plan_id,
              p.periodicidad_valor,
              p.periodicidad_unidad::text AS periodicidad_unidad,
              p.aviso_anticipacion_dias,
              p.ultima_revision_fecha,
              p.proxima_revision_fecha,
              p.activa,
              COALESCE(p.observaciones,'') AS observaciones,
              COALESCE(dc.total_equipos,0) AS total_equipos,
              COALESCE(rv.total_revisiones,0) AS total_revisiones,
              COALESCE(rv.revisiones_cerradas,0) AS revisiones_cerradas,
              rv.borrador_revision_id
            FROM customers c
            LEFT JOIN LATERAL (
              SELECT *
                FROM preventivo_planes p
               WHERE p.scope_type='customer'
                 AND p.customer_id=c.id
                 AND p.activa=true
               ORDER BY p.id DESC
               LIMIT 1
            ) p ON TRUE
            LEFT JOIN LATERAL (
              SELECT COUNT(*) AS total_equipos
                FROM devices d
               WHERE d.customer_id=c.id
            ) dc ON TRUE
            LEFT JOIN LATERAL (
              SELECT
                COUNT(*) AS total_revisiones,
                COUNT(*) FILTER (WHERE r.estado='cerrada') AS revisiones_cerradas,
                MAX(r.id) FILTER (WHERE r.estado='borrador') AS borrador_revision_id
              FROM preventivo_revisiones r
              WHERE r.plan_id = p.id
            ) rv ON TRUE
            {where_sql}
            ORDER BY c.razon_social ASC
            """,
            params,
        ) or []

        out = []
        for row in rows:
            has_plan = bool(row.get("preventivo_plan_id"))
            state, days = _preventivo_state(
                row.get("proxima_revision_fecha"),
                row.get("aviso_anticipacion_dias"),
                has_plan=has_plan,
            )
            if estado and state != estado:
                continue
            out.append(
                {
                    **row,
                    "preventivo_estado": state,
                    "preventivo_dias_restantes": days,
                    "plan": _serialize_plan(
                        {
                            "id": row.get("preventivo_plan_id"),
                            "scope_type": "customer",
                            "device_id": None,
                            "customer_id": row.get("customer_id"),
                            "periodicidad_valor": row.get("periodicidad_valor"),
                            "periodicidad_unidad": row.get("periodicidad_unidad"),
                            "aviso_anticipacion_dias": row.get("aviso_anticipacion_dias"),
                            "ultima_revision_fecha": row.get("ultima_revision_fecha"),
                            "proxima_revision_fecha": row.get("proxima_revision_fecha"),
                            "activa": bool(row.get("activa")) if row.get("preventivo_plan_id") else False,
                            "observaciones": row.get("observaciones") or "",
                        }
                    ) if has_plan else None,
                }
            )

        return Response({"items": out, "total": len(out)})


class CustomerPreventivoPlanView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, customer_id: int):
        require_roles(request, _PLAN_ROLES)
        _set_audit_user(request)
        if not _require_existing_customer(customer_id):
            return Response({"detail": "Institución inexistente"}, status=404)
        if _fetch_active_plan("customer", customer_id):
            return Response({"detail": "Ya existe un plan activo"}, status=409)

        data = request.data or {}
        default_lead = int(getattr(settings, "PREVENTIVO_DEFAULT_LEAD_DAYS", 30) or 30)
        val, unit, aviso = _validate_period(data, default_lead)
        ultima = _parse_date(data.get("ultima_revision_fecha"), "ultima_revision_fecha")
        proxima = _parse_date(data.get("proxima_revision_fecha"), "proxima_revision_fecha")
        if not proxima and ultima:
            proxima = _add_period(ultima, val, unit)

        uid = _uid(request)
        plan_id = exec_returning(
            """
            INSERT INTO preventivo_planes(
              scope_type, customer_id, periodicidad_valor, periodicidad_unidad,
              aviso_anticipacion_dias, ultima_revision_fecha, proxima_revision_fecha,
              activa, observaciones, created_by, updated_by
            ) VALUES ('customer', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [
                customer_id,
                val,
                unit,
                aviso,
                ultima,
                proxima,
                bool(_parse_bool(data.get("activa"), True)),
                (data.get("observaciones") or "").strip() or None,
                uid,
                uid,
            ],
        )
        return Response(
            {
                "ok": True,
                "plan_id": plan_id,
                "plan": _serialize_plan(_fetch_active_plan("customer", customer_id)),
            },
            status=201,
        )

    @transaction.atomic
    def patch(self, request, customer_id: int):
        require_roles(request, _PLAN_ROLES)
        _set_audit_user(request)
        plan = _fetch_active_plan("customer", customer_id)
        if not plan:
            return Response({"detail": "Plan activo inexistente"}, status=404)

        d = request.data or {}
        merged = dict(plan)
        changed_period_or_last = False
        if "periodicidad_valor" in d:
            merged["periodicidad_valor"] = _parse_int(d.get("periodicidad_valor"), "periodicidad_valor", required=True, min_value=1)
            changed_period_or_last = True
        if "periodicidad_unidad" in d:
            merged["periodicidad_unidad"] = (d.get("periodicidad_unidad") or "").strip().lower()
            changed_period_or_last = True
        if int(merged.get("periodicidad_valor") or 0) <= 0:
            raise ValidationError({"periodicidad_valor": "debe ser mayor a 0"})
        if merged.get("periodicidad_unidad") not in _PERIOD_UNITS:
            raise ValidationError({"periodicidad_unidad": "debe ser dias|meses|anios"})

        if "aviso_anticipacion_dias" in d:
            merged["aviso_anticipacion_dias"] = _parse_int(d.get("aviso_anticipacion_dias"), "aviso_anticipacion_dias", required=True, min_value=0)
        if int(merged.get("aviso_anticipacion_dias") or 0) < 0:
            raise ValidationError({"aviso_anticipacion_dias": "no puede ser negativo"})

        if "ultima_revision_fecha" in d:
            merged["ultima_revision_fecha"] = _parse_date(d.get("ultima_revision_fecha"), "ultima_revision_fecha")
            changed_period_or_last = True
        if "proxima_revision_fecha" in d:
            merged["proxima_revision_fecha"] = _parse_date(d.get("proxima_revision_fecha"), "proxima_revision_fecha")
        elif changed_period_or_last and merged.get("ultima_revision_fecha"):
            merged["proxima_revision_fecha"] = _add_period(
                merged.get("ultima_revision_fecha"),
                int(merged.get("periodicidad_valor") or 0),
                merged.get("periodicidad_unidad"),
            )
        if "activa" in d:
            merged["activa"] = bool(_parse_bool(d.get("activa"), True))
        if "observaciones" in d:
            merged["observaciones"] = (d.get("observaciones") or "").strip() or None

        exec_void(
            """
            UPDATE preventivo_planes
               SET periodicidad_valor=%s,
                   periodicidad_unidad=%s,
                   aviso_anticipacion_dias=%s,
                   ultima_revision_fecha=%s,
                   proxima_revision_fecha=%s,
                   activa=%s,
                   observaciones=%s,
                   updated_by=%s
             WHERE id=%s
            """,
            [
                int(merged.get("periodicidad_valor") or 0),
                merged.get("periodicidad_unidad"),
                int(merged.get("aviso_anticipacion_dias") or 0),
                merged.get("ultima_revision_fecha"),
                merged.get("proxima_revision_fecha"),
                bool(merged.get("activa")),
                merged.get("observaciones"),
                _uid(request),
                plan.get("id"),
            ],
        )
        return Response({"ok": True, "plan": _serialize_plan(_fetch_active_plan("customer", customer_id) or merged)})


class CustomerPreventivoRevisionesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, customer_id: int):
        require_roles(request, _VIEW_ROLES)
        if not _require_existing_customer(customer_id):
            return Response({"detail": "Institución inexistente"}, status=404)
        plan = _fetch_active_plan("customer", customer_id)
        if not plan:
            return Response({"plan": None, "items": []})

        rows = q(
            """
            SELECT
              r.id,
              r.plan_id,
              r.estado::text AS estado,
              r.fecha_programada,
              r.fecha_realizada,
              r.realizada_por,
              COALESCE(r.resumen,'') AS resumen,
              r.created_at,
              r.updated_at,
              COALESCE(cnt.total_items,0) AS total_items
            FROM preventivo_revisiones r
            LEFT JOIN LATERAL (
              SELECT COUNT(*) AS total_items
              FROM preventivo_revision_items i
              WHERE i.revision_id = r.id
            ) cnt ON TRUE
            WHERE r.plan_id = %s
            ORDER BY
              CASE WHEN r.estado='borrador' THEN 0 ELSE 1 END,
              COALESCE(r.fecha_programada, r.fecha_realizada, r.created_at::date) DESC,
              r.id DESC
            """,
            [plan.get("id")],
        ) or []
        return Response({"plan": _serialize_plan(plan), "items": rows})

    @transaction.atomic
    def post(self, request, customer_id: int):
        require_roles(request, _REVISION_ROLES)
        _set_audit_user(request)
        if not _require_existing_customer(customer_id):
            return Response({"detail": "Institución inexistente"}, status=404)

        plan = _fetch_active_plan("customer", customer_id)
        if not plan:
            return Response({"detail": "La institución no tiene plan activo"}, status=404)

        draft = q(
            """
            SELECT id
              FROM preventivo_revisiones
             WHERE plan_id=%s
               AND estado='borrador'
             ORDER BY id DESC
             LIMIT 1
            """,
            [plan.get("id")],
            one=True,
        )
        if draft:
            rev = _fetch_revision(draft.get("id"))
            return Response(
                {
                    "ok": True,
                    "reused": True,
                    "revision": _serialize_revision(rev),
                    "items": _fetch_revision_items(draft.get("id")),
                },
                status=200,
            )

        d = request.data or {}
        fecha_programada = _parse_date(d.get("fecha_programada"), "fecha_programada") or plan.get("proxima_revision_fecha") or dt.date.today()
        uid = _uid(request)
        revision_id = exec_returning(
            """
            INSERT INTO preventivo_revisiones(
              plan_id, estado, fecha_programada, fecha_realizada,
              realizada_por, resumen, created_by, updated_by
            ) VALUES (%s, 'borrador', %s, NULL, NULL, %s, %s, %s)
            RETURNING id
            """,
            [
                plan.get("id"),
                fecha_programada,
                (d.get("resumen") or "").strip() or None,
                uid,
                uid,
            ],
        )

        last_closed = q(
            """
            SELECT id
              FROM preventivo_revisiones
             WHERE plan_id=%s
               AND estado='cerrada'
             ORDER BY COALESCE(fecha_realizada, fecha_programada, created_at::date) DESC, id DESC
             LIMIT 1
            """,
            [plan.get("id")],
            one=True,
        )
        _seed_customer_revision_items(
            revision_id=revision_id,
            customer_id=customer_id,
            last_closed_revision_id=(last_closed.get("id") if last_closed else None),
        )
        rev = _fetch_revision(revision_id)
        return Response(
            {
                "ok": True,
                "reused": False,
                "revision": _serialize_revision(rev),
                "items": _fetch_revision_items(revision_id),
            },
            status=201,
        )


class PreventivoRevisionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, revision_id: int):
        require_roles(request, _VIEW_ROLES)
        rev = _fetch_revision(revision_id)
        if not rev:
            return Response({"detail": "Revisión inexistente"}, status=404)
        return Response({"revision": _serialize_revision(rev), "items": _fetch_revision_items(revision_id)})


class PreventivoRevisionItemsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, revision_id: int):
        require_roles(request, _REVISION_ROLES)
        _set_audit_user(request)
        rev = _fetch_revision(revision_id)
        if not rev:
            return Response({"detail": "Revisión inexistente"}, status=404)
        if rev.get("estado") != "borrador":
            return Response({"detail": "Solo se pueden editar items en borrador"}, status=409)

        d = request.data or {}
        device_id = d.get("device_id")
        if device_id not in (None, ""):
            device_id = _parse_int(device_id, "device_id", required=True, min_value=1)
        else:
            device_id = None
        normalized = _normalize_item_payload(d, device_id=device_id)
        order = _parse_int(d.get("orden"), "orden", required=False, default=_next_item_order(revision_id), min_value=1)

        item_id = exec_returning(
            """
            INSERT INTO preventivo_revision_items(
              revision_id, orden, device_id, equipo_snapshot, serie_snapshot, interno_snapshot,
              estado_item, motivo_no_control, ubicacion_detalle, accesorios_cambiados,
              accesorios_detalle, notas, arrastrar_proxima
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [
                revision_id,
                order,
                normalized.get("device_id"),
                normalized.get("equipo_snapshot"),
                normalized.get("serie_snapshot"),
                normalized.get("interno_snapshot"),
                normalized.get("estado_item"),
                normalized.get("motivo_no_control"),
                normalized.get("ubicacion_detalle"),
                normalized.get("accesorios_cambiados"),
                normalized.get("accesorios_detalle"),
                normalized.get("notas"),
                normalized.get("arrastrar_proxima"),
            ],
        )
        item = q(
            """
            SELECT
              id, revision_id, orden, device_id,
              COALESCE(equipo_snapshot,'') AS equipo_snapshot,
              COALESCE(serie_snapshot,'') AS serie_snapshot,
              COALESCE(interno_snapshot,'') AS interno_snapshot,
              estado_item::text AS estado_item,
              COALESCE(motivo_no_control,'') AS motivo_no_control,
              COALESCE(ubicacion_detalle,'') AS ubicacion_detalle,
              COALESCE(accesorios_cambiados,false) AS accesorios_cambiados,
              COALESCE(accesorios_detalle,'') AS accesorios_detalle,
              COALESCE(notas,'') AS notas,
              COALESCE(arrastrar_proxima,true) AS arrastrar_proxima,
              created_at, updated_at
            FROM preventivo_revision_items
            WHERE id=%s
            """,
            [item_id],
            one=True,
        )
        return Response({"ok": True, "item": item}, status=201)


class PreventivoRevisionItemDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, revision_id: int, item_id: int):
        require_roles(request, _REVISION_ROLES)
        _set_audit_user(request)
        row = q(
            """
            SELECT
              i.id,
              i.revision_id,
              i.orden,
              i.device_id,
              COALESCE(i.equipo_snapshot,'') AS equipo_snapshot,
              COALESCE(i.serie_snapshot,'') AS serie_snapshot,
              COALESCE(i.interno_snapshot,'') AS interno_snapshot,
              i.estado_item::text AS estado_item,
              COALESCE(i.motivo_no_control,'') AS motivo_no_control,
              COALESCE(i.ubicacion_detalle,'') AS ubicacion_detalle,
              COALESCE(i.accesorios_cambiados,false) AS accesorios_cambiados,
              COALESCE(i.accesorios_detalle,'') AS accesorios_detalle,
              COALESCE(i.notas,'') AS notas,
              COALESCE(i.arrastrar_proxima,true) AS arrastrar_proxima,
              r.estado::text AS revision_estado
            FROM preventivo_revision_items i
            JOIN preventivo_revisiones r ON r.id = i.revision_id
            WHERE i.revision_id=%s AND i.id=%s
            """,
            [revision_id, item_id],
            one=True,
        )
        if not row:
            return Response({"detail": "Item inexistente"}, status=404)
        if row.get("revision_estado") != "borrador":
            return Response({"detail": "Solo se pueden editar items en borrador"}, status=409)

        d = request.data or {}
        merged = dict(row)
        if "device_id" in d:
            raw = d.get("device_id")
            if raw in ("", None):
                merged["device_id"] = None
            else:
                merged["device_id"] = _parse_int(raw, "device_id", required=True, min_value=1)
        if "equipo_snapshot" in d:
            merged["equipo_snapshot"] = (d.get("equipo_snapshot") or "").strip()
        if "serie_snapshot" in d:
            merged["serie_snapshot"] = (d.get("serie_snapshot") or "").strip()
        if "interno_snapshot" in d:
            merged["interno_snapshot"] = (d.get("interno_snapshot") or "").strip()
        if "estado_item" in d:
            merged["estado_item"] = (d.get("estado_item") or "").strip().lower()
        if "motivo_no_control" in d:
            merged["motivo_no_control"] = (d.get("motivo_no_control") or "").strip()
        if "ubicacion_detalle" in d:
            merged["ubicacion_detalle"] = (d.get("ubicacion_detalle") or "").strip()
        if "accesorios_cambiados" in d:
            merged["accesorios_cambiados"] = bool(_parse_bool(d.get("accesorios_cambiados"), False))
        if "accesorios_detalle" in d:
            merged["accesorios_detalle"] = (d.get("accesorios_detalle") or "").strip()
        if "notas" in d:
            merged["notas"] = (d.get("notas") or "").strip()
        if "arrastrar_proxima" in d:
            merged["arrastrar_proxima"] = bool(_parse_bool(d.get("arrastrar_proxima"), True))
        if "orden" in d:
            merged["orden"] = _parse_int(d.get("orden"), "orden", required=True, min_value=1)

        normalized = _normalize_item_payload(
            {
                "estado_item": merged.get("estado_item"),
                "motivo_no_control": merged.get("motivo_no_control"),
                "arrastrar_proxima": merged.get("arrastrar_proxima"),
                "equipo_snapshot": merged.get("equipo_snapshot"),
                "serie_snapshot": merged.get("serie_snapshot"),
                "interno_snapshot": merged.get("interno_snapshot"),
                "ubicacion_detalle": merged.get("ubicacion_detalle"),
                "accesorios_cambiados": merged.get("accesorios_cambiados"),
                "accesorios_detalle": merged.get("accesorios_detalle"),
                "notas": merged.get("notas"),
            },
            device_id=merged.get("device_id"),
        )

        exec_void(
            """
            UPDATE preventivo_revision_items
               SET orden=%s,
                   device_id=%s,
                   equipo_snapshot=%s,
                   serie_snapshot=%s,
                   interno_snapshot=%s,
                   estado_item=%s,
                   motivo_no_control=%s,
                   ubicacion_detalle=%s,
                   accesorios_cambiados=%s,
                   accesorios_detalle=%s,
                   notas=%s,
                   arrastrar_proxima=%s
             WHERE id=%s
            """,
            [
                int(merged.get("orden") or 1),
                normalized.get("device_id"),
                normalized.get("equipo_snapshot"),
                normalized.get("serie_snapshot"),
                normalized.get("interno_snapshot"),
                normalized.get("estado_item"),
                normalized.get("motivo_no_control"),
                normalized.get("ubicacion_detalle"),
                normalized.get("accesorios_cambiados"),
                normalized.get("accesorios_detalle"),
                normalized.get("notas"),
                normalized.get("arrastrar_proxima"),
                item_id,
            ],
        )
        item = q(
            """
            SELECT
              id, revision_id, orden, device_id,
              COALESCE(equipo_snapshot,'') AS equipo_snapshot,
              COALESCE(serie_snapshot,'') AS serie_snapshot,
              COALESCE(interno_snapshot,'') AS interno_snapshot,
              estado_item::text AS estado_item,
              COALESCE(motivo_no_control,'') AS motivo_no_control,
              COALESCE(ubicacion_detalle,'') AS ubicacion_detalle,
              COALESCE(accesorios_cambiados,false) AS accesorios_cambiados,
              COALESCE(accesorios_detalle,'') AS accesorios_detalle,
              COALESCE(notas,'') AS notas,
              COALESCE(arrastrar_proxima,true) AS arrastrar_proxima,
              created_at, updated_at
            FROM preventivo_revision_items
            WHERE id=%s
            """,
            [item_id],
            one=True,
        )
        return Response({"ok": True, "item": item})


class PreventivoRevisionCerrarView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, revision_id: int):
        require_roles(request, _REVISION_ROLES)
        _set_audit_user(request)
        rev = _fetch_revision(revision_id)
        if not rev:
            return Response({"detail": "Revisión inexistente"}, status=404)
        if rev.get("estado") != "borrador":
            return Response({"detail": "La revisión no esta en borrador"}, status=409)

        d = request.data or {}
        fecha_realizada = _parse_date(d.get("fecha_realizada"), "fecha_realizada") or dt.date.today()
        resumen = (d.get("resumen") or rev.get("resumen") or "").strip() or None
        uid = _uid(request)
        exec_void(
            """
            UPDATE preventivo_revisiones
               SET estado='cerrada',
                   fecha_realizada=%s,
                   realizada_por=%s,
                   resumen=%s,
                   updated_by=%s
             WHERE id=%s
            """,
            [fecha_realizada, uid, resumen, uid, revision_id],
        )

        proxima = _add_period(
            fecha_realizada,
            int(rev.get("periodicidad_valor") or 0),
            rev.get("periodicidad_unidad"),
        )
        exec_void(
            """
            UPDATE preventivo_planes
               SET ultima_revision_fecha=%s,
                   proxima_revision_fecha=%s,
                   updated_by=%s
             WHERE id=%s
            """,
            [fecha_realizada, proxima, uid, rev.get("plan_id")],
        )

        out_rev = _fetch_revision(revision_id)
        return Response(
            {
                "ok": True,
                "revision": _serialize_revision(out_rev),
                "items": _fetch_revision_items(revision_id),
                "plan": _serialize_plan(
                    _fetch_active_plan(
                        out_rev.get("scope_type"),
                        out_rev.get("plan_device_id") or out_rev.get("plan_customer_id"),
                    )
                ),
            }
        )


__all__ = [
    "DevicePreventivoPlanView",
    "DevicePreventivoRevisionCreateView",
    "DevicePreventivoRepuestosView",
    "DevicePreventivoRepuestoDetailView",
    "PreventivoAgendaView",
    "PreventivoClientesListView",
    "CustomerPreventivoPlanView",
    "CustomerPreventivoRevisionesView",
    "PreventivoRevisionDetailView",
    "PreventivoRevisionItemsView",
    "PreventivoRevisionItemDetailView",
    "PreventivoRevisionCerrarView",
]
