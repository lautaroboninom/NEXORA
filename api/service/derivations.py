"""Reglas compartidas para la condición de derivado de una OS."""

from django.db import connection


ACTIVE_DERIVATION_STATES = ("derivado", "en_servicio")
ACTIVE_DERIVATION_SQL = "fecha_entrega IS NULL AND estado IN ('derivado','en_servicio')"

CLOSED_INGRESO_STATES = {
    "liberado",
    "entregado",
    "alquilado",
    "baja",
    "vendido_pendiente_entrega",
    "vendido_entregado",
}

ACTIVE_DERIVATION_BLOCK_DETAIL = (
    "El equipo sigue derivado. Marcá la derivación como devuelta antes de liberarlo."
)


def _fetchall_dicts(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _empty_derivation_payload():
    return {
        "derivacion_activa": False,
        "derivacion_id": None,
        "derivacion_estado": None,
        "derivacion_proveedor_id": None,
        "derivacion_proveedor": "",
        "derivacion_fecha": None,
    }


def active_derivation_payload(row):
    if not row:
        return _empty_derivation_payload()
    return {
        "derivacion_activa": True,
        "derivacion_id": row.get("derivacion_id") or row.get("id"),
        "derivacion_estado": row.get("derivacion_estado") or row.get("estado"),
        "derivacion_proveedor_id": row.get("derivacion_proveedor_id") or row.get("proveedor_id"),
        "derivacion_proveedor": row.get("derivacion_proveedor") or row.get("proveedor") or "",
        "derivacion_fecha": row.get("derivacion_fecha") or row.get("fecha_deriv"),
    }


def get_active_derivation(ingreso_id, *, for_update=False):
    lock_sql = " FOR UPDATE OF ed" if for_update and connection.vendor == "postgresql" else ""
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT ed.id AS derivacion_id,
                   ed.ingreso_id,
                   ed.estado::text AS derivacion_estado,
                   ed.proveedor_id AS derivacion_proveedor_id,
                   COALESCE(pe.nombre, '') AS derivacion_proveedor,
                   ed.fecha_deriv AS derivacion_fecha,
                   ed.fecha_entrega
              FROM equipos_derivados ed
              LEFT JOIN proveedores_externos pe ON pe.id = ed.proveedor_id
             WHERE ed.ingreso_id = %s
               AND ed.fecha_entrega IS NULL
               AND ed.estado IN ('derivado','en_servicio')
             ORDER BY ed.fecha_deriv DESC, ed.id DESC
             LIMIT 1{lock_sql}
            """,
            [ingreso_id],
        )
        columns = [column[0] for column in cursor.description]
        row = cursor.fetchone()
    return dict(zip(columns, row)) if row else None


def attach_active_derivations(rows, *, ingreso_key="id"):
    """Agrega el contrato plano de derivación activa a filas de lista o detalle."""
    if isinstance(rows, dict):
        target_rows = [rows]
    else:
        target_rows = list(rows or [])

    ingreso_ids = []
    for row in target_rows:
        row.update(_empty_derivation_payload())
        try:
            ingreso_id = int(row.get(ingreso_key) or 0)
        except (TypeError, ValueError):
            ingreso_id = 0
        if ingreso_id > 0:
            ingreso_ids.append(ingreso_id)

    if not ingreso_ids:
        return rows

    placeholders = ",".join(["%s"] * len(set(ingreso_ids)))
    params = sorted(set(ingreso_ids))
    distinct_sql = "DISTINCT ON (ed.ingreso_id)" if connection.vendor == "postgresql" else ""
    order_sql = "ed.ingreso_id, ed.fecha_deriv DESC, ed.id DESC" if connection.vendor == "postgresql" else "ed.fecha_deriv DESC, ed.id DESC"
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {distinct_sql}
                   ed.ingreso_id,
                   ed.id AS derivacion_id,
                   ed.estado AS derivacion_estado,
                   ed.proveedor_id AS derivacion_proveedor_id,
                   COALESCE(pe.nombre, '') AS derivacion_proveedor,
                   ed.fecha_deriv AS derivacion_fecha
              FROM equipos_derivados ed
              LEFT JOIN proveedores_externos pe ON pe.id = ed.proveedor_id
             WHERE ed.ingreso_id IN ({placeholders})
               AND ed.fecha_entrega IS NULL
               AND ed.estado IN ('derivado','en_servicio')
             ORDER BY {order_sql}
            """,
            params,
        )
        active_rows = _fetchall_dicts(cursor)

    active_by_ingreso = {}
    for active in active_rows:
        active_by_ingreso.setdefault(int(active["ingreso_id"]), active_derivation_payload(active))
    for row in target_rows:
        try:
            ingreso_id = int(row.get(ingreso_key) or 0)
        except (TypeError, ValueError):
            continue
        if ingreso_id in active_by_ingreso:
            row.update(active_by_ingreso[ingreso_id])
    return rows


def active_derivation_block_payload(derivation):
    payload = {
        "detail": ACTIVE_DERIVATION_BLOCK_DETAIL,
        "code": "INGRESO_DERIVACION_ACTIVA",
    }
    payload.update(active_derivation_payload(derivation))
    return payload
