import re

from django.db import connection
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import q, require_roles, _set_audit_user
from .mg_state import resolve_mg_flags
from ..bejerman_ris import equipment_suggestion_from_bejerman_article, find_customer_suggestion
from ..bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkConfigError,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
    lookup_sale_by_serial,
)
from ..warranty import compute_warranty_from_sale_date


INGRESO_ESTADOS_NO_BLOQUEAN_ALTA = {
    "entregado",
    "alquilado",
    "baja",
    "vendido_pendiente_entrega",
    "vendido_entregado",
}


def _ingreso_en_curso(row: dict | None) -> bool:
    estado = str((row or {}).get("estado") or "").strip().lower()
    return bool(estado) and estado not in INGRESO_ESTADOS_NO_BLOQUEAN_ALTA


def _normalize_ingreso_summary(row: dict | None, ingreso_en_curso: bool | None = None):
    if not row:
        return row
    if row.get("fecha_ingreso") is None and row.get("fecha_creacion") is not None:
        row["fecha_ingreso"] = row.get("fecha_creacion")
    row["ingreso_en_curso"] = _ingreso_en_curso(row) if ingreso_en_curso is None else bool(ingreso_en_curso)
    row.pop("fecha_creacion", None)
    return row


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
    return _normalize_ingreso_summary(q(
        """
        SELECT
          t.id,
          t.estado,
          t.presupuesto_estado,
          t.resolucion,
          COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
          t.fecha_creacion,
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
    ))


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


def _fetch_ingreso_en_curso(device_id: int):
    return _normalize_ingreso_summary(q(
        """
        SELECT
          t.id,
          t.estado,
          t.presupuesto_estado,
          COALESCE(t.alquilado, false) AS alquilado,
          COALESCE(t.alquiler_a, '') AS alquiler_a,
          COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
          t.fecha_creacion,
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
          AND t.estado NOT IN ('entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
        ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
        LIMIT 1
        """,
        [device_id],
        one=True,
    ), ingreso_en_curso=True)


def _fetch_last_ingreso(device_id: int):
    return _normalize_ingreso_summary(q(
        """
        SELECT
          t.id,
          t.estado,
          t.presupuesto_estado,
          COALESCE(t.alquilado, false) AS alquilado,
          COALESCE(t.alquiler_a, '') AS alquiler_a,
          COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
          t.fecha_creacion,
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
    ))


def _lookup_bejerman_sale_by_serial(serial: str):
    try:
        return lookup_sale_by_serial(BejermanSDKClient(), serial)
    except BejermanSdkConfigError as exc:
        return {"found": False, "lookup_error": str(exc)}
    except (BejermanSdkResponseError, BejermanSdkUnavailable) as exc:
        return {"found": False, "lookup_error": str(exc)}
    except Exception as exc:
        return {"found": False, "lookup_error": f"No se pudo consultar Bejerman: {exc}"}


def _bejerman_sale_response(code: str, ns_key: str | None, sale_payload: dict):
    if not sale_payload or not sale_payload.get("found"):
        return None
    article_code = sale_payload.get("articleCode") or ""
    article_description = sale_payload.get("articleDescription") or ""
    equipment = equipment_suggestion_from_bejerman_article(article_code, article_description)
    customer = find_customer_suggestion(
        sale_payload.get("customerCode") or "",
        sale_payload.get("customerName") or "",
    )
    warranty = compute_warranty_from_sale_date(
        sale_payload.get("issueDate"),
        numero_serie=sale_payload.get("serial") or code,
        brand_id=equipment.get("marca_id"),
        model_id=equipment.get("modelo_id"),
        source="bejerman_sale",
    )
    return {
        "kind": "bejerman_sale",
        "source": "bejerman_sale",
        "raw": code,
        "normalized": code,
        "normalized_key": ns_key,
        "suggestion": {
            "serial": sale_payload.get("serial") or code,
            "normalizedSerial": sale_payload.get("normalizedSerial") or ns_key,
            "article": {
                "code": article_code,
                "description": article_description,
                "itemPartida": sale_payload.get("itemPartida") or "",
                "itemQuantity": sale_payload.get("itemQuantity"),
            },
            "document": {
                "type": sale_payload.get("documentType") or "",
                "letter": sale_payload.get("documentLetter") or "",
                "pointOfSale": sale_payload.get("documentPointOfSale") or "",
                "number": sale_payload.get("documentNumber") or "",
                "label": sale_payload.get("documentLabel") or "",
                "documentId": sale_payload.get("documentId"),
                "issueDate": sale_payload.get("issueDate"),
            },
            "customer": {
                "code": sale_payload.get("customerCode") or "",
                "name": sale_payload.get("customerName") or "",
                "local_customer": customer,
            },
            "equipment": equipment,
            "warranty": {
                "garantia": warranty.get("garantia"),
                "vence_el": warranty.get("vence_el").isoformat() if warranty.get("vence_el") else None,
                "fecha_venta": warranty.get("fecha_venta").isoformat() if warranty.get("fecha_venta") else None,
                "days": warranty.get("days"),
                "meta": warranty.get("meta") or {},
            },
            "checkedComprobantes": sale_payload.get("checkedComprobantes"),
        },
    }


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
            sale_payload = _lookup_bejerman_sale_by_serial(code)
            sale_response = _bejerman_sale_response(code, ns_key, sale_payload or {})
            if sale_response:
                return Response(sale_response)
            return Response({
                "kind": "none",
                "source": source or "serial",
                "raw": code,
                "normalized": code,
                "normalized_key": ns_key,
                "bejerman_lookup_error": (sale_payload or {}).get("lookup_error") if isinstance(sale_payload, dict) else None,
            })

        ingreso_en_curso = _fetch_ingreso_en_curso(device["id"])
        last_ingreso = ingreso_en_curso or _fetch_last_ingreso(device["id"])

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
