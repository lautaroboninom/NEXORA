import re

from django.db import connection
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import q, require_roles, _set_audit_user
from .mg_state import resolve_mg_flags


def _parse_ingreso_id(raw: str):
    value = (raw or "").strip()
    if not value:
        return None, None
    match = re.search(r"(?:^|[/-])ingresos(?:[/-])(\d+)", value, re.IGNORECASE)
    if match:
        return int(match.group(1)), "url"
    match = re.search(r"\bOS\s*#?\s*(\d{1,9})\b", value, re.IGNORECASE)
    if match:
        return int(match.group(1)), "os"
    match = re.search(r"(?:ingreso_id|ingreso)=(\d+)", value, re.IGNORECASE)
    if match:
        return int(match.group(1)), "query"
    if value.isdigit():
        return int(value), "numeric"
    return None, None


def _fetch_ingreso_summary(ingreso_id: int):
    return q(
        """
        SELECT
          t.id,
          t.estado,
          t.presupuesto_estado,
          t.resolucion,
          t.fecha_ingreso,
          t.fecha_entrega,
          COALESCE(t.equipo_variante,'') AS equipo_variante,
          t.device_id,
          c.id AS customer_id,
          COALESCE(c.razon_social,'') AS razon_social,
          COALESCE(c.cod_empresa,'') AS cod_empresa,
          COALESCE(c.telefono,'') AS telefono,
          COALESCE(d.numero_serie,'') AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno,
          d.marca_id,
          COALESCE(b.nombre,'') AS marca,
          d.model_id,
          COALESCE(m.nombre,'') AS modelo,
          COALESCE(m.tipo_equipo,'') AS tipo_equipo
        FROM ingresos t
        JOIN devices d ON d.id = t.device_id
        JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        WHERE t.id = %s
        """,
        [ingreso_id],
        one=True,
    )


def _has_mg_schema() -> bool:
    try:
        if connection.vendor == "postgresql":
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name='devices'
                   AND column_name='mg_estado'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                one=True,
            )
            return bool(row)
        row = q(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name='devices'
               AND column_name='mg_estado'
             LIMIT 1
            """,
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _fetch_device_by_code(raw: str, mg_select_sql: str):
    raw = (raw or "").strip()
    if not raw:
        return None, None
    ns_key = re.sub(r"[\s-]+", "", raw).upper()
    mg_match = re.match(r"^(MG|NM|NV|CE)\s*(\d{1,4})$", raw, re.IGNORECASE)
    mg_no_space = None
    if mg_match:
        mg_no_space = f"{mg_match.group(1).upper()}{mg_match.group(2).zfill(4)}"

    wh = []
    params = []
    if ns_key:
        wh.append("REPLACE(REPLACE(UPPER(COALESCE(d.numero_serie,'')),' ',''),'-','') = %s")
        params.append(ns_key)
    if mg_no_space:
        wh.append("REPLACE(UPPER(COALESCE(d.numero_interno,'')),' ','') = %s")
        params.append(mg_no_space)
        wh.append("REPLACE(UPPER(COALESCE(d.numero_serie,'')),' ','') = %s")
        params.append(mg_no_space)
    if not wh:
        return None, ns_key

    where_sql = " OR ".join(wh)
    sql = f"""
        SELECT
          d.id,
          d.customer_id,
          COALESCE(c.razon_social,'') AS customer_nombre,
          COALESCE(c.cod_empresa,'') AS customer_cod,
          COALESCE(c.telefono,'') AS customer_telefono,
          d.marca_id,
          COALESCE(b.nombre,'') AS marca,
          d.model_id,
          COALESCE(m.nombre,'') AS modelo,
          COALESCE(m.tipo_equipo,'') AS tipo_equipo,
          COALESCE(d.numero_serie,'') AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno,
          COALESCE(d.variante,'') AS variante,
          COALESCE(d.propietario_nombre, d.propietario, '') AS propietario_nombre,
          COALESCE(d.propietario_contacto, '') AS propietario_contacto,
          COALESCE(d.propietario_doc, '') AS propietario_doc,
          COALESCE(d.alquilado, false) AS alquilado,
          COALESCE(d.alquiler_a, '') AS alquiler_a,
          {mg_select_sql}
        FROM devices d
        LEFT JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        WHERE ({where_sql})
        LIMIT 1
    """
    dev = q(sql, params, one=True)
    return dev, ns_key


def _fetch_last_ingreso(device_id: int):
    return q(
        """
        SELECT
          t.id,
          t.estado,
          t.presupuesto_estado,
          COALESCE(t.alquilado, false) AS alquilado,
          COALESCE(t.alquiler_a, '') AS alquiler_a,
          t.fecha_ingreso,
          t.fecha_entrega,
          COALESCE(t.equipo_variante,'') AS equipo_variante,
          COALESCE(c.razon_social,'') AS razon_social,
          COALESCE(d.numero_serie,'') AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno,
          COALESCE(b.nombre,'') AS marca,
          COALESCE(m.nombre,'') AS modelo,
          COALESCE(m.tipo_equipo,'') AS tipo_equipo
        FROM ingresos t
        JOIN devices d ON d.id = t.device_id
        JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        WHERE t.device_id = %s
        ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
        LIMIT 1
        """,
        [device_id],
        one=True,
    )


class ScanLookupView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor", "admin", "recepcion"])
        _set_audit_user(request)
        code = (request.GET.get("code") or "").strip()
        if not code:
            return Response({"detail": "code requerido"}, status=400)

        ingreso_id, source = _parse_ingreso_id(code)
        if ingreso_id:
            ingreso = _fetch_ingreso_summary(ingreso_id)
            if ingreso:
                return Response({
                    "kind": "ingreso",
                    "source": source,
                    "raw": code,
                    "ingreso": ingreso,
                })
            if source != "numeric":
                return Response({
                    "kind": "none",
                    "source": source,
                    "raw": code,
                    "detail": "Ingreso no encontrado",
                })

        has_mg_schema = _has_mg_schema()
        if has_mg_schema:
            mg_select_sql = """
              COALESCE(d.mg_estado, 'activo') AS mg_estado,
              d.mg_inactivo_desde AS mg_inactivo_desde,
              d.mg_venta_fecha AS mg_venta_fecha,
              COALESCE(d.mg_venta_factura_numero,'') AS mg_venta_factura_numero,
              COALESCE(d.mg_venta_remito_numero,'') AS mg_venta_remito_numero,
              COALESCE(d.mg_venta_observaciones,'') AS mg_venta_observaciones,
              d.mg_venta_usuario_id AS mg_venta_usuario_id
            """
        else:
            mg_select_sql = """
              'activo' AS mg_estado,
              NULL AS mg_inactivo_desde,
              NULL AS mg_venta_fecha,
              '' AS mg_venta_factura_numero,
              '' AS mg_venta_remito_numero,
              '' AS mg_venta_observaciones,
              NULL AS mg_venta_usuario_id
            """

        device, ns_key = _fetch_device_by_code(code, mg_select_sql)
        if not device:
            return Response({
                "kind": "none",
                "source": source or "serial",
                "raw": code,
                "normalized": code,
                "normalized_key": ns_key,
            })

        last_ingreso = _fetch_last_ingreso(device["id"])

        mg_owner = q(
            "SELECT id FROM customers WHERE LOWER(razon_social) LIKE %s ORDER BY id ASC LIMIT 1",
            ["%mg%bio%"],
            one=True,
        )
        mg_owner_id = mg_owner["id"] if mg_owner else None
        flags = resolve_mg_flags(device, mg_owner_id)

        return Response({
            "kind": "device",
            "source": source or "serial",
            "raw": code,
            "normalized": code,
            "normalized_key": ns_key,
            "device": device,
            "ingreso": last_ingreso,
            "flags": flags,
        })


__all__ = ["ScanLookupView"]
