from datetime import datetime

from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import q, exec_void, exec_returning, require_roles, _fetchall_dicts, _set_audit_user
from .mg_state import normalize_mg, resolve_mg_flags


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


def _has_mg_sale_extended_schema() -> bool:
    return _has_table_column("devices", "mg_venta_customer_id") and _has_table_column(
        "devices", "mg_venta_numero_alternativo"
    )


def _has_mg_events_extended_schema() -> bool:
    return _has_table_column("device_mg_events", "venta_customer_id") and _has_table_column(
        "device_mg_events", "venta_numero_alternativo"
    )


def _mg_owner_customer_id():
    row = q(
        "SELECT id FROM customers WHERE LOWER(razon_social) LIKE %s ORDER BY id ASC LIMIT 1",
        ["%mg%bio%"],
        one=True,
    )
    return row.get("id") if row else None


def _parse_sale_datetime(raw):
    if raw is None or str(raw).strip() == "":
        return timezone.now()
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        s_norm = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = parse_datetime(s_norm) or parse_datetime(s)
        if not dt:
            d = parse_date(s)
            if d:
                dt = datetime(d.year, d.month, d.day, 0, 0, 0)
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _normalize_mg(numero_interno: str) -> str:
    value = (numero_interno or "").strip()
    if value and not value.upper().startswith(("MG", "NM", "NV", "CE")):
        value = "MG " + value
    return value


def _fetch_device_editable(device_id: int):
    return q(
        """
        SELECT
          d.id,
          d.customer_id,
          COALESCE(c.razon_social,'') AS customer_nombre,
          COALESCE(c.cod_empresa,'') AS cod_empresa,
          d.marca_id,
          d.model_id,
          COALESCE(b.nombre,'') AS marca,
          COALESCE(m.nombre,'') AS modelo,
          COALESCE(NULLIF(d.tipo_equipo,''), NULLIF(m.tipo_equipo,''), '') AS tipo_equipo,
          COALESCE(NULLIF(d.variante,''), NULLIF(m.variante,''), '') AS variante,
          COALESCE(d.numero_serie,'') AS numero_serie,
          COALESCE(d.numero_interno,'') AS numero_interno,
          d.ubicacion_id,
          COALESCE(loc.nombre,'') AS ubicacion_nombre,
          COALESCE(d.alquilado,false) AS alquilado,
          COALESCE(d.alquiler_a,'') AS alquiler_a
        FROM devices d
        LEFT JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        LEFT JOIN locations loc ON loc.id = d.ubicacion_id
        WHERE d.id=%s
        """,
        [device_id],
        one=True,
    )


class DeviceIdentificadoresView(APIView):
    """
    Obtiene y corrige datos clave de un equipo existente.
    Mantiene compatibilidad con PATCH parcial de numero_serie/numero_interno.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, device_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "tecnico"])
        dev = _fetch_device_editable(device_id)
        if not dev:
            return Response({"detail": "Device inexistente"}, status=404)
        return Response({"ok": True, "device": dev})

    @transaction.atomic
    def patch(self, request, device_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "tecnico"])
        _set_audit_user(request)
        data = request.data or {}
        if not isinstance(data, dict):
            return Response({"detail": "Payload invalido"}, status=400)

        dev = _fetch_device_editable(device_id)
        if not dev:
            return Response({"detail": "Device inexistente"}, status=404)

        editable_keys = {
            "customer_id",
            "tipo_equipo",
            "marca_id",
            "model_id",
            "variante",
            "numero_serie",
            "numero_interno",
            "ubicacion_id",
            "alquilado",
            "alquiler_a",
        }
        if not any(key in data for key in editable_keys):
            return Response({"detail": "No se enviaron cambios"}, status=400)

        next_customer_id = int(dev.get("customer_id") or 0)
        next_marca_id = _parse_int_or_none(dev.get("marca_id"))
        next_model_id = _parse_int_or_none(dev.get("model_id"))
        next_ubicacion_id = _parse_int_or_none(dev.get("ubicacion_id"))
        next_tipo_equipo = (dev.get("tipo_equipo") or "").strip()
        next_variante = (dev.get("variante") or "").strip()
        next_numero_serie = (dev.get("numero_serie") or "").strip()
        next_numero_interno = (dev.get("numero_interno") or "").strip()
        next_alquilado = bool(dev.get("alquilado"))
        next_alquiler_a = (dev.get("alquiler_a") or "").strip()

        if "customer_id" in data:
            parsed_customer_id = _parse_int_or_none(data.get("customer_id"))
            if not parsed_customer_id:
                return Response({"detail": "customer_id requerido"}, status=400)
            next_customer_id = parsed_customer_id

        if "marca_id" in data:
            next_marca_id = _parse_int_or_none(data.get("marca_id"))
        if "model_id" in data:
            next_model_id = _parse_int_or_none(data.get("model_id"))
        if "ubicacion_id" in data:
            next_ubicacion_id = _parse_int_or_none(data.get("ubicacion_id"))
        if "tipo_equipo" in data:
            next_tipo_equipo = (data.get("tipo_equipo") or "").strip()
        if "variante" in data:
            next_variante = (data.get("variante") or "").strip()
        if "numero_serie" in data:
            next_numero_serie = (data.get("numero_serie") or "").strip()
        if "numero_interno" in data:
            next_numero_interno = _normalize_mg(data.get("numero_interno") or "")
        if "alquilado" in data:
            next_alquilado = bool(_parse_bool_or_default(data.get("alquilado"), next_alquilado))
        if "alquiler_a" in data:
            next_alquiler_a = (data.get("alquiler_a") or "").strip()
        if not next_alquilado:
            next_alquiler_a = ""

        customer = q("SELECT id FROM customers WHERE id=%s", [next_customer_id], one=True)
        if not customer:
            return Response({"detail": "Institucion/cliente inexistente"}, status=404)

        model_marca_id = None
        if next_model_id:
            model = q("SELECT id, marca_id FROM models WHERE id=%s", [next_model_id], one=True)
            if not model:
                return Response({"detail": "model_id inexistente"}, status=400)
            model_marca_id = int(model.get("marca_id") or 0) if model.get("marca_id") is not None else None

        if next_marca_id:
            marca = q("SELECT id FROM marcas WHERE id=%s", [next_marca_id], one=True)
            if not marca:
                return Response({"detail": "marca_id inexistente"}, status=400)

        if next_model_id and model_marca_id:
            if next_marca_id and int(next_marca_id) != int(model_marca_id):
                return Response({"detail": "model_id no pertenece a marca_id"}, status=400)
            if not next_marca_id:
                next_marca_id = int(model_marca_id)

        if next_ubicacion_id:
            loc = q("SELECT id FROM locations WHERE id=%s", [next_ubicacion_id], one=True)
            if not loc:
                return Response({"detail": "ubicacion_id inexistente"}, status=400)

        if "numero_serie" in data and next_numero_serie:
            ns_key = next_numero_serie.replace(" ", "").replace("-", "").upper()
            other = q(
                """
                SELECT id
                  FROM devices
                 WHERE REPLACE(REPLACE(UPPER(numero_serie),' ',''),'-','') = %s
                   AND id <> %s
                 LIMIT 1
                """,
                [ns_key, device_id],
                one=True,
            )
            if other:
                return Response(
                    {
                        "detail": "El número de serie ya está asignado a otro equipo.",
                        "conflict_type": "NS_DUPLICATE",
                        "conflict_device_id": other["id"],
                        "current_device_id": device_id,
                        "numero_serie_input": next_numero_serie,
                    },
                    status=400,
                )

        if "numero_interno" in data and next_numero_interno:
            if connection.vendor == "postgresql":
                conflict = q(
                    """
                    SELECT id
                      FROM devices
                     WHERE id <> %s
                       AND numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$'
                       AND UPPER(REGEXP_REPLACE(numero_interno,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0'))) =
                           UPPER(REGEXP_REPLACE(%s,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))
                     LIMIT 1
                    """,
                    [device_id, next_numero_interno],
                    one=True,
                )
            else:
                conflict = q(
                    "SELECT id FROM devices WHERE id <> %s AND numero_interno = %s LIMIT 1",
                    [device_id, next_numero_interno],
                    one=True,
                )
            if conflict:
                return Response(
                    {
                        "detail": "El número interno ya está asignado a otro equipo.",
                        "conflict_type": "MG_DUPLICATE",
                        "conflict_device_id": conflict["id"],
                        "current_device_id": device_id,
                        "numero_interno_input": next_numero_interno,
                    },
                    status=400,
                )

        updates = []
        params = []

        if int(dev.get("customer_id") or 0) != int(next_customer_id):
            updates.append("customer_id = %s")
            params.append(next_customer_id)

        cur_marca_id = _parse_int_or_none(dev.get("marca_id"))
        if cur_marca_id != next_marca_id:
            updates.append("marca_id = %s")
            params.append(next_marca_id)

        cur_model_id = _parse_int_or_none(dev.get("model_id"))
        if cur_model_id != next_model_id:
            updates.append("model_id = %s")
            params.append(next_model_id)

        cur_ubicacion_id = _parse_int_or_none(dev.get("ubicacion_id"))
        if cur_ubicacion_id != next_ubicacion_id:
            updates.append("ubicacion_id = %s")
            params.append(next_ubicacion_id)

        if (dev.get("tipo_equipo") or "").strip() != next_tipo_equipo:
            updates.append("tipo_equipo = NULLIF(%s,'')")
            params.append(next_tipo_equipo)

        if (dev.get("variante") or "").strip() != next_variante:
            updates.append("variante = NULLIF(%s,'')")
            params.append(next_variante)

        if (dev.get("numero_serie") or "").strip() != next_numero_serie:
            updates.append("numero_serie = NULLIF(%s,'')")
            params.append(next_numero_serie)

        if (dev.get("numero_interno") or "").strip() != next_numero_interno:
            updates.append("numero_interno = NULLIF(%s,'')")
            params.append(next_numero_interno)

        if bool(dev.get("alquilado")) != bool(next_alquilado):
            updates.append("alquilado = %s")
            params.append(bool(next_alquilado))

        if (dev.get("alquiler_a") or "").strip() != next_alquiler_a:
            updates.append("alquiler_a = NULLIF(%s,'')")
            params.append(next_alquiler_a)

        if not updates:
            return Response({"detail": "No se enviaron cambios"}, status=400)

        params.append(device_id)
        sql = "UPDATE devices SET " + ", ".join(updates) + " WHERE id=%s"
        exec_void(sql, params)
        return Response({"ok": True})


class DevicesListView(APIView):
    """
    Listado de devices (equipos) con info de propiedad (MG/propio), último cliente
    y datos básicos de identificación. Solo visible para roles de sistema.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "tecnico"])
        q_raw = (request.GET.get("q") or "").strip()
        propio_raw = (request.GET.get("propio") or "").strip().lower()
        alquilado_raw = (request.GET.get("alquilado") or "").strip().lower()
        preventivo_estado_raw = (request.GET.get("preventivo_estado") or "").strip().lower()
        con_plan_raw = (request.GET.get("con_plan") or "").strip().lower()
        sort_raw = (request.GET.get("sort") or "").strip()
        page_raw = (request.GET.get("page") or "").strip()
        page_size_raw = (request.GET.get("page_size") or "").strip()

        page = int(page_raw) if page_raw.isdigit() and int(page_raw) > 0 else 1
        try:
            page_size = int(page_size_raw) if page_size_raw else 0
        except Exception:
            page_size = 0
        if page_size < 0:
            page_size = 0
        if page_size > 0:
            page_size = min(page_size, 500)

        has_preventivos = bool(
            q(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema='public'
                   AND table_name='preventivo_planes'
                """,
                one=True,
            )
        )
        has_mg_schema = _has_mg_schema()
        has_mg_sale_extended = _has_mg_sale_extended_schema()
        mg_sale_customer_join = (
            "LEFT JOIN customers c_mg_sale ON c_mg_sale.id = d.mg_venta_customer_id"
            if has_mg_sale_extended
            else "LEFT JOIN customers c_mg_sale ON 1=0"
        )

        if preventivo_estado_raw and preventivo_estado_raw not in ("sin_plan", "al_dia", "proximo", "vencido"):
            return Response({"detail": "preventivo_estado inválido"}, status=400)

        con_plan_val = None
        if con_plan_raw in ("1", "true", "yes", "y", "t"):
            con_plan_val = True
        elif con_plan_raw in ("0", "false", "no", "n", "f"):
            con_plan_val = False

        if has_preventivos:
            from_sql = f"""
                FROM devices d
                LEFT JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN locations loc ON loc.id = d.ubicacion_id
                {mg_sale_customer_join}
                LEFT JOIN LATERAL (
                  SELECT i.id AS ingreso_id,
                         COALESCE(i.fecha_ingreso, i.fecha_creacion) AS fecha_ingreso
                    FROM ingresos i
                   WHERE i.device_id = d.id
                   ORDER BY COALESCE(i.fecha_ingreso, i.fecha_creacion) DESC, i.id DESC
                   LIMIT 1
                ) lasti ON TRUE
                LEFT JOIN LATERAL (
                  SELECT
                    p.id,
                    p.periodicidad_valor,
                    p.periodicidad_unidad::text AS periodicidad_unidad,
                    p.aviso_anticipacion_dias,
                    p.ultima_revision_fecha,
                    p.proxima_revision_fecha
                  FROM preventivo_planes p
                  WHERE p.scope_type='device'
                    AND p.device_id=d.id
                    AND p.activa=true
                  ORDER BY p.id DESC
                  LIMIT 1
                ) pp ON TRUE
            """
        else:
            from_sql = f"""
                FROM devices d
                LEFT JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN locations loc ON loc.id = d.ubicacion_id
                {mg_sale_customer_join}
                LEFT JOIN LATERAL (
                  SELECT i.id AS ingreso_id,
                         COALESCE(i.fecha_ingreso, i.fecha_creacion) AS fecha_ingreso
                    FROM ingresos i
                   WHERE i.device_id = d.id
                   ORDER BY COALESCE(i.fecha_ingreso, i.fecha_creacion) DESC, i.id DESC
                   LIMIT 1
                ) lasti ON TRUE
            """

        with connection.cursor() as cur:
            # Identificar id de cliente propio (MG BIO) si existe
            cur.execute(
                "SELECT id FROM customers WHERE LOWER(razon_social) LIKE %s ORDER BY id ASC LIMIT 1",
                ["%mg%bio%"],
            )
            row_mg_owner = cur.fetchone()
            mg_owner_id = row_mg_owner[0] if row_mg_owner else None

            wh, params = [], []
            if q_raw:
                like = f"%{q_raw}%"
                wh.append(
                    "("
                    "LOWER(COALESCE(d.numero_serie,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(d.numero_interno,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(c.razon_social,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(b.nombre,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(m.nombre,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(NULLIF(d.tipo_equipo,''), NULLIF(m.tipo_equipo,''), '')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(NULLIF(d.variante,''), NULLIF(m.variante,''), '')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(d.propietario_nombre,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(d.propietario_contacto,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(d.propietario_doc,'')) LIKE LOWER(%s) OR "
                    "LOWER(COALESCE(loc.nombre,'')) LIKE LOWER(%s)"
                    ")"
                )
                params.extend([like, like, like, like, like, like, like, like, like, like, like])

            if propio_raw in ("1", "true", "yes", "y", "t"):
                wh.append(
                    "("
                    "d.numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$' OR "
                    "d.numero_serie ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$'"
                    ")"
                )
            if alquilado_raw in ("1", "true", "yes", "y", "t"):
                wh.append("COALESCE(d.alquilado,false) = true")
            elif alquilado_raw in ("0", "false", "no", "n"):
                wh.append("COALESCE(d.alquilado,false) = false")

            if has_preventivos:
                if con_plan_val is True:
                    wh.append("pp.id IS NOT NULL")
                elif con_plan_val is False:
                    wh.append("pp.id IS NULL")

                if preventivo_estado_raw == "sin_plan":
                    wh.append("pp.id IS NULL")
                elif preventivo_estado_raw == "vencido":
                    wh.append("pp.id IS NOT NULL AND pp.proxima_revision_fecha IS NOT NULL AND CURRENT_DATE > pp.proxima_revision_fecha")
                elif preventivo_estado_raw == "proximo":
                    wh.append(
                        "pp.id IS NOT NULL "
                        "AND pp.proxima_revision_fecha IS NOT NULL "
                        "AND CURRENT_DATE <= pp.proxima_revision_fecha "
                        "AND (CURRENT_DATE + (COALESCE(pp.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= pp.proxima_revision_fecha"
                    )
                elif preventivo_estado_raw == "al_dia":
                    wh.append(
                        "pp.id IS NOT NULL AND ("
                        "pp.proxima_revision_fecha IS NULL OR ("
                        "CURRENT_DATE <= pp.proxima_revision_fecha AND "
                        "(CURRENT_DATE + (COALESCE(pp.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date < pp.proxima_revision_fecha"
                        "))"
                    )
            else:
                # Sin esquema preventivo aplicado: todo se considera sin plan.
                if con_plan_val is True:
                    wh.append("1=0")
                if preventivo_estado_raw in ("vencido", "proximo", "al_dia"):
                    wh.append("1=0")

            where_sql = (" WHERE " + " AND ".join(wh)) if wh else ""

            sort_map = {
                "id": "d.id",
                "-id": "d.id DESC",
                "ns": "d.numero_serie",
                "-ns": "d.numero_serie DESC",
                "mg": "d.numero_interno",
                "-mg": "d.numero_interno DESC",
                "marca": "b.nombre",
                "-marca": "b.nombre DESC",
                "modelo": "m.nombre",
                "-modelo": "m.nombre DESC",
                "cliente": "c.razon_social",
                "-cliente": "c.razon_social DESC",
                "ubicacion": "loc.nombre",
                "-ubicacion": "loc.nombre DESC",
            }
            if has_preventivos:
                sort_map.update(
                    {
                        "preventivo_ultima": "pp.ultima_revision_fecha",
                        "-preventivo_ultima": "pp.ultima_revision_fecha DESC",
                        "preventivo_proxima": "pp.proxima_revision_fecha",
                        "-preventivo_proxima": "pp.proxima_revision_fecha DESC",
                        "preventivo_estado": (
                            "CASE "
                            "WHEN pp.id IS NULL THEN 0 "
                            "WHEN pp.proxima_revision_fecha IS NOT NULL AND CURRENT_DATE > pp.proxima_revision_fecha THEN 1 "
                            "WHEN pp.proxima_revision_fecha IS NOT NULL AND (CURRENT_DATE + (COALESCE(pp.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= pp.proxima_revision_fecha THEN 2 "
                            "ELSE 3 END"
                        ),
                        "-preventivo_estado": (
                            "CASE "
                            "WHEN pp.id IS NULL THEN 0 "
                            "WHEN pp.proxima_revision_fecha IS NOT NULL AND CURRENT_DATE > pp.proxima_revision_fecha THEN 1 "
                            "WHEN pp.proxima_revision_fecha IS NOT NULL AND (CURRENT_DATE + (COALESCE(pp.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= pp.proxima_revision_fecha THEN 2 "
                            "ELSE 3 END DESC"
                        ),
                    }
                )
            else:
                sort_map.update(
                    {
                        "preventivo_ultima": "d.id",
                        "-preventivo_ultima": "d.id DESC",
                        "preventivo_proxima": "d.id",
                        "-preventivo_proxima": "d.id DESC",
                        "preventivo_estado": "0",
                        "-preventivo_estado": "0 DESC",
                    }
                )
            order_sql = sort_map.get(sort_raw or "", "d.id DESC")

            limit_sql = ""
            limit_params = []
            overfetch = 0
            if page_size > 0:
                overfetch = 1
                limit_sql = " LIMIT %s OFFSET %s"
                limit_params.extend([page_size + overfetch, max(0, (page - 1) * page_size)])

            if has_preventivos:
                preventivo_select_sql = """
                  pp.id AS preventivo_plan_id,
                  pp.periodicidad_valor AS preventivo_periodicidad_valor,
                  pp.periodicidad_unidad AS preventivo_periodicidad_unidad,
                  pp.ultima_revision_fecha AS preventivo_ultima_revision,
                  pp.proxima_revision_fecha AS preventivo_proxima_revision,
                  pp.aviso_anticipacion_dias AS preventivo_aviso_dias,
                  (CASE
                    WHEN pp.id IS NULL THEN 'sin_plan'
                    WHEN pp.proxima_revision_fecha IS NOT NULL AND CURRENT_DATE > pp.proxima_revision_fecha THEN 'vencido'
                    WHEN pp.proxima_revision_fecha IS NOT NULL
                         AND (CURRENT_DATE + (COALESCE(pp.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= pp.proxima_revision_fecha
                         THEN 'proximo'
                    ELSE 'al_dia'
                  END) AS preventivo_estado,
                  (CASE
                    WHEN pp.proxima_revision_fecha IS NULL THEN NULL
                    ELSE (pp.proxima_revision_fecha - CURRENT_DATE)
                  END) AS preventivo_dias_restantes
                """
            else:
                preventivo_select_sql = """
                  NULL::integer AS preventivo_plan_id,
                  NULL::integer AS preventivo_periodicidad_valor,
                  NULL::text AS preventivo_periodicidad_unidad,
                  NULL::date AS preventivo_ultima_revision,
                  NULL::date AS preventivo_proxima_revision,
                  NULL::integer AS preventivo_aviso_dias,
                  'sin_plan'::text AS preventivo_estado,
                  NULL::integer AS preventivo_dias_restantes
                """

            if has_mg_schema and has_mg_sale_extended:
                mg_select_sql = """
                  COALESCE(d.mg_estado, 'activo') AS mg_estado,
                  d.mg_inactivo_desde AS mg_inactivo_desde,
                  d.mg_venta_fecha AS mg_venta_fecha,
                  COALESCE(d.mg_venta_factura_numero,'') AS mg_venta_factura_numero,
                  COALESCE(d.mg_venta_remito_numero,'') AS mg_venta_remito_numero,
                  COALESCE(d.mg_venta_observaciones,'') AS mg_venta_observaciones,
                  d.mg_venta_usuario_id AS mg_venta_usuario_id,
                  d.mg_venta_customer_id AS mg_venta_customer_id,
                  COALESCE(c_mg_sale.razon_social,'') AS mg_venta_customer_nombre,
                  COALESCE(d.mg_venta_numero_alternativo,'') AS mg_venta_numero_alternativo,
                """
            else:
                mg_select_sql = """
                  'activo' AS mg_estado,
                  NULL AS mg_inactivo_desde,
                  NULL AS mg_venta_fecha,
                  '' AS mg_venta_factura_numero,
                  '' AS mg_venta_remito_numero,
                  '' AS mg_venta_observaciones,
                  NULL AS mg_venta_usuario_id,
                  NULL AS mg_venta_customer_id,
                  '' AS mg_venta_customer_nombre,
                  '' AS mg_venta_numero_alternativo,
                """

            sql = f"""
                SELECT
                  d.id,
                  d.customer_id,
                  COALESCE(c.razon_social,'') AS customer_nombre,
                  d.marca_id,
                  d.model_id,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(d.numero_serie,'') AS numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(NULLIF(d.tipo_equipo,''), NULLIF(m.tipo_equipo,''), '') AS tipo_equipo,
                  COALESCE(NULLIF(d.variante,''), NULLIF(m.variante,''), '') AS variante,
                  d.garantia_vence,
                  COALESCE(d.alquilado,false) AS alquilado,
                  COALESCE(d.alquiler_a,'') AS alquiler_a,
                  d.ubicacion_id,
                  COALESCE(loc.nombre,'') AS ubicacion_nombre,
                  d.propietario,
                  d.propietario_nombre,
                  d.propietario_contacto,
                  d.propietario_doc,
                  {mg_select_sql}
                  lasti.ingreso_id AS last_ingreso_id,
                  NULL::integer AS last_customer_id,
                  ''::text AS last_customer_nombre,
                  lasti.fecha_ingreso AS last_fecha_ingreso,
                  {preventivo_select_sql}
                {from_sql}
                {where_sql}
                ORDER BY {order_sql}
                {limit_sql}
            """
            cur.execute(sql, params + limit_params)
            rows = _fetchall_dicts(cur)
            for row in rows:
                row.update(resolve_mg_flags(row, mg_owner_id))

        with connection.cursor() as cur2:
            cur2.execute(
                f"""
                SELECT COUNT(*)
                {from_sql}
                {where_sql}
                """,
                params,
            )
            total_count = int(cur2.fetchone()[0] or 0)

        if page_size == 0:
            return Response({"items": rows, "total_count": total_count})
        has_next = False
        if len(rows) > page_size:
            has_next = True
            rows = rows[:page_size]
        return Response({
            "items": rows,
            "page": page,
            "page_size": page_size,
            "has_next": bool(has_next),
            "total_count": total_count,
        })

def _parse_int_or_none(raw):
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _parse_bool_or_default(raw, default=False):
    if raw is None:
        return bool(default)
    if isinstance(raw, bool):
        return raw
    txt = str(raw).strip().lower()
    if txt in ("1", "true", "yes", "y", "t", "si", "s"):
        return True
    if txt in ("0", "false", "no", "n", "f"):
        return False
    return bool(default)


class DeviceDirectCreateView(APIView):
    """
    Alta directa de equipo en tabla devices sin generar un ingreso.
    Pensado para equipos bajo tutela del servicio tecnico instalados en instituciones.
    """

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        require_roles(request, ["jefe", "jefe_veedor", "admin"])
        _set_audit_user(request)
        data = request.data or {}

        customer_id = _parse_int_or_none(data.get("customer_id"))
        if not customer_id:
            return Response({"detail": "customer_id requerido"}, status=400)
        customer = q(
            "SELECT id FROM customers WHERE id=%s",
            [customer_id],
            one=True,
        )
        if not customer:
            return Response({"detail": "Institución/cliente inexistente"}, status=404)

        marca_id = _parse_int_or_none(data.get("marca_id"))
        model_id = _parse_int_or_none(data.get("model_id"))
        ubicacion_id = _parse_int_or_none(data.get("ubicacion_id"))

        if marca_id:
            marca = q("SELECT id FROM marcas WHERE id=%s", [marca_id], one=True)
            if not marca:
                return Response({"detail": "marca_id inexistente"}, status=400)

        if model_id:
            model = q("SELECT id, marca_id FROM models WHERE id=%s", [model_id], one=True)
            if not model:
                return Response({"detail": "model_id inexistente"}, status=400)
            model_marca_id = int(model.get("marca_id") or 0) if model.get("marca_id") is not None else None
            if marca_id and model_marca_id and int(marca_id) != model_marca_id:
                return Response({"detail": "model_id no pertenece a marca_id"}, status=400)
            if not marca_id:
                marca_id = model_marca_id

        if ubicacion_id:
            loc = q("SELECT id FROM locations WHERE id=%s", [ubicacion_id], one=True)
            if not loc:
                return Response({"detail": "ubicacion_id inexistente"}, status=400)

        numero_serie = (data.get("numero_serie") or "").strip()
        numero_interno = (data.get("numero_interno") or "").strip()
        if numero_interno and not numero_interno.upper().startswith(("MG", "NM", "NV", "CE")):
            numero_interno = "MG " + numero_interno

        tipo_equipo = (data.get("tipo_equipo") or "").strip()
        variante = (data.get("variante") or "").strip()
        alquilado = bool(_parse_bool_or_default(data.get("alquilado"), False))
        alquiler_a = (data.get("alquiler_a") or "").strip()
        if not alquilado:
            alquiler_a = ""

        if not (numero_serie or numero_interno or tipo_equipo or variante or model_id):
            return Response(
                {"detail": "Completa al menos N/S, MG, tipo_equipo, variante o modelo."},
                status=400,
            )

        if numero_serie:
            ns_key = numero_serie.replace(" ", "").replace("-", "").upper()
            other_ns = q(
                """
                SELECT id
                  FROM devices
                 WHERE REPLACE(REPLACE(UPPER(numero_serie),' ',''),'-','') = %s
                 LIMIT 1
                """,
                [ns_key],
                one=True,
            )
            if other_ns:
                return Response(
                    {
                        "detail": "El número de serie ya esta asignado a otro equipo.",
                        "conflict_type": "NS_DUPLICATE",
                        "conflict_device_id": other_ns["id"],
                    },
                    status=400,
                )

        if numero_interno:
            if connection.vendor == "postgresql":
                other_mg = q(
                    """
                    SELECT id
                      FROM devices
                     WHERE numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$'
                       AND UPPER(REGEXP_REPLACE(numero_interno,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0'))) =
                           UPPER(REGEXP_REPLACE(%s,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))
                     LIMIT 1
                    """,
                    [numero_interno],
                    one=True,
                )
            else:
                other_mg = q(
                    "SELECT id FROM devices WHERE numero_interno = %s LIMIT 1",
                    [numero_interno],
                    one=True,
                )
            if other_mg:
                return Response(
                    {
                        "detail": "El número interno ya esta asignado a otro equipo.",
                        "conflict_type": "MG_DUPLICATE",
                        "conflict_device_id": other_mg["id"],
                    },
                    status=400,
                )

        device_id = exec_returning(
            """
            INSERT INTO devices(
              customer_id, marca_id, model_id, numero_serie, numero_interno,
              tipo_equipo, variante, ubicacion_id, alquilado, alquiler_a
            ) VALUES (%s, %s, %s, NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,''), %s, %s, NULLIF(%s,''))
            RETURNING id
            """,
            [
                customer_id,
                marca_id,
                model_id,
                numero_serie,
                numero_interno,
                tipo_equipo,
                variante,
                ubicacion_id,
                alquilado,
                alquiler_a,
            ],
        )

        row = q(
            """
            SELECT
              d.id,
              d.customer_id,
              COALESCE(c.razon_social,'') AS customer_nombre,
              d.marca_id,
              d.model_id,
              COALESCE(b.nombre,'') AS marca,
              COALESCE(m.nombre,'') AS modelo,
              COALESCE(d.numero_serie,'') AS numero_serie,
              COALESCE(d.numero_interno,'') AS numero_interno,
              COALESCE(d.tipo_equipo,'') AS tipo_equipo,
              COALESCE(d.variante,'') AS variante,
              d.ubicacion_id,
              COALESCE(loc.nombre,'') AS ubicacion_nombre,
              COALESCE(d.alquilado,false) AS alquilado,
              COALESCE(d.alquiler_a,'') AS alquiler_a
            FROM devices d
            LEFT JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN locations loc ON loc.id = d.ubicacion_id
            WHERE d.id=%s
            """,
            [device_id],
            one=True,
        )
        return Response({"ok": True, "device": row}, status=201)


def _norm_mg(value: str):
    return normalize_mg(value)


class DevicesMergeView(APIView):
    """
    Unificar dos devices en uno solo, moviendo sus ingresos al destino.
    - Se mantiene el device destino (target_id) y se elimina el source_id.
    - Se puede fijar un nuevo numero_serie para el destino.
    - El numero_interno se mantiene del destino por defecto.
    - Si se envía numero_interno, se aplica (debe coincidir con MG del target o source).
    - Si no se envía numero_interno y ambos MG existen y difieren, devuelve error.
    """

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        require_roles(request, ["jefe", "jefe_veedor", "admin"])
        _set_audit_user(request)
        data = request.data or {}
        try:
            target_id = int(data.get("target_id"))
            source_id = int(data.get("source_id"))
        except Exception:
            return Response({"detail": "target_id y source_id requeridos"}, status=400)
        if target_id == source_id:
            return Response({"detail": "target_id y source_id deben ser distintos"}, status=400)

        new_ns = (data.get("numero_serie") or "").strip()
        copy_mg_if_missing = bool(data.get("copy_mg_if_missing"))
        has_mg_override = "numero_interno" in data
        desired_mg_raw = (data.get("numero_interno") or "").strip() if has_mg_override else None

        target = q(
            "SELECT id, numero_serie, numero_interno FROM devices WHERE id=%s",
            [target_id],
            one=True,
        )
        source = q(
            "SELECT id, numero_serie, numero_interno FROM devices WHERE id=%s",
            [source_id],
            one=True,
        )
        if not target or not source:
            return Response({"detail": "Device destino o fuente inexistente"}, status=404)

        mg_target = _norm_mg(target.get("numero_interno") or "")
        mg_source = _norm_mg(source.get("numero_interno") or "")

        desired_mg = None
        if has_mg_override:
            if desired_mg_raw:
                desired_mg = _norm_mg(desired_mg_raw)
                if not desired_mg:
                    return Response(
                        {
                            "detail": "numero_interno inválido para unificar.",
                            "conflict_type": "MG_INVALID",
                        },
                        status=400,
                    )
            if desired_mg not in (None, mg_target, mg_source):
                return Response(
                    {
                        "detail": "numero_interno inválido para unificar.",
                        "conflict_type": "MG_INVALID",
                    },
                    status=400,
                )

        # MG conflict check
        if (not has_mg_override) and mg_target and mg_source and mg_target != mg_source:
            return Response(
                {
                    "detail": "Los equipos a unificar tienen numeros internos distintos.",
                    "conflict_type": "MG_MISMATCH",
                    "mg_target": mg_target,
                    "mg_source": mg_source,
                },
                status=400,
            )

        # Determinar MG final
        mg_to_apply = mg_target
        if has_mg_override:
            mg_to_apply = desired_mg
        elif not mg_target and mg_source and copy_mg_if_missing:
            mg_to_apply = mg_source

        if not new_ns and not mg_to_apply:
            return Response({"detail": "Se requiere N/S o número interno para unificar."}, status=400)

        if new_ns:
            ns_key = new_ns.replace(" ", "").replace("-", "").upper()
            ns_conflict = q(
                """
                SELECT id FROM devices
                 WHERE REPLACE(REPLACE(UPPER(numero_serie),' ',''),'-','') = %s
                   AND id NOT IN (%s, %s)
                 LIMIT 1
                """,
                [ns_key, target_id, source_id],
                one=True,
            )
            if ns_conflict:
                return Response(
                    {
                        "detail": "El número de serie ya está asignado a otro equipo.",
                        "conflict_type": "NS_DUPLICATE",
                        "conflict_device_id": ns_conflict["id"],
                    },
                    status=400,
                )

        # Si vamos a aplicar MG (nuevo o distinto), validar que no choque con otros
        if mg_to_apply and mg_to_apply != mg_target:
            mg_conflict = q(
                """
                SELECT id
                  FROM devices
                 WHERE id NOT IN (%s,%s)
                   AND numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$'
                   AND UPPER(REGEXP_REPLACE(numero_interno,
                       '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0'))) =
                       UPPER(REGEXP_REPLACE(%s,
                       '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))
                 LIMIT 1
                """,
                [target_id, source_id, mg_to_apply],
                one=True,
            )
            if mg_conflict:
                return Response(
                    {
                        "detail": "El número interno ya está asignado a otro equipo.",
                        "conflict_type": "MG_DUPLICATE",
                        "conflict_device_id": mg_conflict["id"],
                    },
                    status=400,
                )

        # 1) Limpiar N/S del source para evitar choque de índice al setear en target
        if new_ns:
            exec_void("UPDATE devices SET numero_serie = NULL WHERE id=%s", [source_id])
            exec_void("UPDATE devices SET numero_serie = NULLIF(%s,'') WHERE id=%s", [new_ns, target_id])
        # 3) Aplicar MG al target si corresponde (liberar source si necesitamos moverlo)
        if mg_to_apply != mg_target:
            if mg_to_apply:
                try:
                    exec_void("UPDATE devices SET numero_interno = NULL WHERE id=%s", [source_id])
                    exec_void("UPDATE devices SET numero_interno = %s WHERE id=%s", [mg_to_apply, target_id])
                except Exception as e:
                    return Response(
                        {
                            "detail": "No se pudo asignar el número interno al destino (posible duplicado).",
                            "conflict_type": "MG_UNIQUE_CONSTRAINT",
                            "numero_interno_input": mg_to_apply,
                            "error": str(e),
                        },
                        status=400,
                    )
            else:
                exec_void("UPDATE devices SET numero_interno = NULL WHERE id=%s", [target_id])
        # 4) Mover ingresos al target
        exec_void("UPDATE ingresos SET device_id=%s WHERE device_id=%s", [target_id, source_id])
        # 5) Eliminar el source
        exec_void("DELETE FROM devices WHERE id=%s", [source_id])

        return Response({
            "ok": True,
            "target_id": target_id,
            "source_id": source_id,
            "applied_numero_serie": new_ns,
            "applied_numero_interno": mg_to_apply,
        })


class DeviceMgVentaView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, device_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "tecnico"])
        _set_audit_user(request)

        if not _has_mg_schema() or not _has_mg_sale_extended_schema() or not _has_mg_events_extended_schema():
            return Response(
                {"detail": "Schema MG venta extendido no aplicado. Ejecuta apply_historical_corrections_schema."},
                status=500,
            )

        data = request.data or {}
        factura_numero = (data.get("factura_numero") or "").strip()
        remito_numero = (data.get("remito_numero") or "").strip()
        if not factura_numero and not remito_numero:
            return Response(
                {
                    "detail": "Debe informar factura o remito para marcar venta.",
                    "conflict_type": "MG_VENTA_COMPROBANTE_REQUERIDO",
                },
                status=400,
            )

        source = (data.get("source") or "equipos").strip().lower()
        if source not in ("equipos", "service_sheet"):
            return Response({"detail": "source inválido"}, status=400)

        fecha_venta = _parse_sale_datetime(data.get("fecha_venta"))
        if not fecha_venta:
            return Response({"detail": "fecha_venta inválida"}, status=400)

        observaciones = (data.get("observaciones") or "").strip()
        venta_numero_alternativo = (data.get("venta_numero_alternativo") or "").strip()
        venta_customer_id_raw = data.get("venta_customer_id")
        if venta_customer_id_raw in (None, ""):
            return Response(
                {
                    "detail": "Debe informar comprador de venta.",
                    "conflict_type": "MG_VENTA_COMPRADOR_REQUERIDO",
                },
                status=400,
            )
        try:
            venta_customer_id = int(venta_customer_id_raw)
        except Exception:
            return Response({"detail": "venta_customer_id inválido"}, status=400)
        if venta_customer_id <= 0:
            return Response({"detail": "venta_customer_id inválido"}, status=400)
        venta_customer = q("SELECT id FROM customers WHERE id=%s", [venta_customer_id], one=True)
        if not venta_customer:
            return Response({"detail": "Comprador inexistente"}, status=404)
        ingreso_id_raw = data.get("ingreso_id")
        ingreso_id = None
        if ingreso_id_raw not in (None, ""):
            try:
                ingreso_id = int(ingreso_id_raw)
            except Exception:
                return Response({"detail": "ingreso_id inválido"}, status=400)

        uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)

        device = q(
            """
            SELECT
              id, customer_id,
              COALESCE(numero_interno,'') AS numero_interno,
              COALESCE(numero_serie,'') AS numero_serie,
              COALESCE(alquilado,false) AS alquilado,
              COALESCE(mg_estado,'activo') AS mg_estado
            FROM devices
            WHERE id=%s
            """,
            [device_id],
            one=True,
        )
        if not device:
            return Response({"detail": "Equipo inexistente"}, status=404)

        mg_owner_id = _mg_owner_customer_id()
        mg_norm = _norm_mg(device.get("numero_interno") or "")
        if not mg_norm:
            return Response(
                {
                    "detail": "El equipo no tiene un número interno MG válido.",
                    "conflict_type": "MG_NUMERO_INTERNO_REQUERIDO",
                },
                status=400,
            )

        mg_flags = resolve_mg_flags(device, mg_owner_id)
        if mg_flags.get("mg_inactivo_venta"):
            return Response(
                {
                    "detail": "El MG ya está inactivo por venta.",
                    "conflict_type": "MG_YA_INACTIVO_VENTA",
                },
                status=400,
            )

        if ingreso_id is not None:
            ing = q("SELECT id, device_id FROM ingresos WHERE id=%s", [ingreso_id], one=True)
            if not ing:
                return Response({"detail": "ingreso_id inexistente"}, status=404)
            if int(ing.get("device_id") or 0) != int(device_id):
                return Response({"detail": "ingreso_id no corresponde al equipo"}, status=400)

        device_sets = [
            "mg_estado = 'inactivo_venta'",
            "mg_inactivo_desde = %s",
            "mg_venta_fecha = %s",
            "mg_venta_factura_numero = NULLIF(%s,'')",
            "mg_venta_remito_numero = NULLIF(%s,'')",
            "mg_venta_observaciones = NULLIF(%s,'')",
            "mg_venta_usuario_id = %s",
            "mg_venta_customer_id = %s",
            "mg_venta_numero_alternativo = NULLIF(%s,'')",
        ]
        device_params = [
            fecha_venta,
            fecha_venta,
            factura_numero,
            remito_numero,
            observaciones,
            uid,
            venta_customer_id,
            venta_numero_alternativo,
        ]
        if _has_table_column("devices", "alquilado"):
            device_sets.append("alquilado = FALSE")
        if _has_table_column("devices", "alquiler_a"):
            device_sets.append("alquiler_a = NULL")
        if _has_table_column("devices", "ubicacion_id"):
            dash_row = q("SELECT id FROM locations WHERE nombre='-' LIMIT 1", one=True)
            dash_id = dash_row.get("id") if dash_row else None
            if dash_id:
                device_sets.append("ubicacion_id = %s")
                device_params.append(dash_id)
        device_params.append(device_id)
        exec_void(
            f"""
            UPDATE devices
               SET {', '.join(device_sets)}
             WHERE id = %s
            """,
            device_params,
        )
        exec_void(
            """
            INSERT INTO device_mg_events(
              device_id, accion, numero_interno_snapshot, fecha_evento,
              factura_numero, remito_numero, observaciones, usuario_id, ingreso_id,
              venta_customer_id, venta_numero_alternativo, source
            )
            VALUES (%s, 'venta', %s, %s, NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,''), %s, %s, %s, NULLIF(%s,''), %s)
            """,
            [
                device_id,
                mg_norm,
                fecha_venta,
                factura_numero,
                remito_numero,
                observaciones,
                uid,
                ingreso_id,
                venta_customer_id,
                venta_numero_alternativo,
                source,
            ],
        )

        if source == "service_sheet" and ingreso_id is not None:
            has_ingreso_alquilado = _has_table_column("ingresos", "alquilado")
            alquilado_sql = "COALESCE(alquilado,false) AS alquilado" if has_ingreso_alquilado else "FALSE AS alquilado"
            ingreso_row = q(
                f"""
                SELECT id, estado, {alquilado_sql}
                  FROM ingresos
                 WHERE id=%s
                """,
                [ingreso_id],
                one=True,
            ) or {}
            estado_anterior = (ingreso_row.get("estado") or "").strip().lower()
            estaba_alquilado = bool(ingreso_row.get("alquilado")) or estado_anterior == "alquilado"
            nuevo_estado = "vendido_entregado" if estaba_alquilado else "vendido_pendiente_entrega"
            ingreso_sets = ["estado=%s"]
            ingreso_params = [nuevo_estado]

            if _has_table_column("ingresos", "factura_numero"):
                ingreso_sets.append("factura_numero = COALESCE(NULLIF(%s,''), factura_numero)")
                ingreso_params.append(factura_numero)
            if estaba_alquilado and _has_table_column("ingresos", "fecha_entrega"):
                ingreso_sets.append("fecha_entrega = COALESCE(fecha_entrega, %s)")
                ingreso_params.append(fecha_venta)
            if estaba_alquilado and _has_table_column("ingresos", "remito_salida"):
                ingreso_sets.append("remito_salida = COALESCE(NULLIF(%s,''), remito_salida)")
                ingreso_params.append(remito_numero)
            if has_ingreso_alquilado:
                ingreso_sets.append("alquilado = FALSE")
            if _has_table_column("ingresos", "alquiler_a"):
                ingreso_sets.append("alquiler_a = NULL")
            if _has_table_column("ingresos", "alquiler_remito"):
                ingreso_sets.append("alquiler_remito = NULL")
            if _has_table_column("ingresos", "alquiler_fecha"):
                ingreso_sets.append("alquiler_fecha = NULL")
            if _has_table_column("ingresos", "ubicacion_id"):
                dash_row = q("SELECT id FROM locations WHERE nombre='-' LIMIT 1", one=True)
                dash_id = dash_row.get("id") if dash_row else None
                if dash_id:
                    ingreso_sets.append("ubicacion_id = %s")
                    ingreso_params.append(dash_id)

            ingreso_params.append(ingreso_id)
            exec_void(
                f"UPDATE ingresos SET {', '.join(ingreso_sets)} WHERE id=%s",
                ingreso_params,
            )
            try:
                with transaction.atomic():
                    exec_void(
                        """
                        INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
                        VALUES (%s, NULLIF(%s,'')::ticket_state, %s::ticket_state, %s, %s, %s)
                        """,
                        [
                            ingreso_id,
                            estado_anterior,
                            nuevo_estado,
                            uid,
                            fecha_venta,
                            (
                                "Venta registrada y entrega confirmada"
                                if nuevo_estado == "vendido_entregado"
                                else "Venta registrada; entrega pendiente"
                            ),
                        ],
                    )
            except Exception:
                try:
                    with transaction.atomic():
                        exec_void(
                            """
                            INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, ts, comentario)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            [
                                ingreso_id,
                                nuevo_estado,
                                uid,
                                fecha_venta,
                                (
                                    "Venta registrada y entrega confirmada"
                                    if nuevo_estado == "vendido_entregado"
                                    else "Venta registrada; entrega pendiente"
                                ),
                            ],
                        )
                except Exception:
                    pass

        row = q(
            """
            SELECT
              d.id,
              COALESCE(d.numero_interno,'') AS numero_interno,
              COALESCE(d.numero_serie,'') AS numero_serie,
              COALESCE(d.mg_estado,'activo') AS mg_estado,
              d.mg_inactivo_desde,
              d.mg_venta_fecha,
              COALESCE(d.mg_venta_factura_numero,'') AS mg_venta_factura_numero,
              COALESCE(d.mg_venta_remito_numero,'') AS mg_venta_remito_numero,
              COALESCE(d.mg_venta_observaciones,'') AS mg_venta_observaciones,
              d.mg_venta_usuario_id,
              d.mg_venta_customer_id AS mg_venta_customer_id,
              COALESCE(c.razon_social,'') AS mg_venta_customer_nombre,
              COALESCE(d.mg_venta_numero_alternativo,'') AS mg_venta_numero_alternativo
            FROM devices d
            LEFT JOIN customers c ON c.id = d.mg_venta_customer_id
            WHERE d.id=%s
            """,
            [device_id],
            one=True,
        ) or {}
        row.update(resolve_mg_flags(row, mg_owner_id))
        return Response({"ok": True, "device": row})


class DeviceMgReactivarView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, device_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "tecnico"])
        _set_audit_user(request)

        if not _has_mg_schema() or not _has_mg_sale_extended_schema() or not _has_mg_events_extended_schema():
            return Response(
                {"detail": "Schema MG venta extendido no aplicado. Ejecuta apply_historical_corrections_schema."},
                status=500,
            )

        data = request.data or {}
        source = (data.get("source") or "equipos").strip().lower()
        if source not in ("equipos", "service_sheet"):
            return Response({"detail": "source inválido"}, status=400)
        observaciones = (data.get("observaciones") or "").strip()
        ingreso_id_raw = data.get("ingreso_id")
        ingreso_id = None
        if ingreso_id_raw not in (None, ""):
            try:
                ingreso_id = int(ingreso_id_raw)
            except Exception:
                return Response({"detail": "ingreso_id inválido"}, status=400)

        uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)

        device = q(
            """
            SELECT
              id, customer_id,
              COALESCE(numero_interno,'') AS numero_interno,
              COALESCE(numero_serie,'') AS numero_serie,
              COALESCE(alquilado,false) AS alquilado,
              COALESCE(mg_estado,'activo') AS mg_estado
            FROM devices
            WHERE id=%s
            """,
            [device_id],
            one=True,
        )
        if not device:
            return Response({"detail": "Equipo inexistente"}, status=404)

        mg_owner_id = _mg_owner_customer_id()
        mg_norm = _norm_mg(device.get("numero_interno") or "")
        if not mg_norm:
            return Response(
                {
                    "detail": "El equipo no tiene un número interno MG válido.",
                    "conflict_type": "MG_NUMERO_INTERNO_REQUERIDO",
                },
                status=400,
            )

        mg_flags = resolve_mg_flags(device, mg_owner_id)
        if not mg_flags.get("mg_inactivo_venta"):
            return Response(
                {
                    "detail": "El MG ya está activo.",
                    "conflict_type": "MG_YA_ACTIVO",
                },
                status=400,
            )

        if ingreso_id is not None:
            ing = q("SELECT id, device_id FROM ingresos WHERE id=%s", [ingreso_id], one=True)
            if not ing:
                return Response({"detail": "ingreso_id inexistente"}, status=404)
            if int(ing.get("device_id") or 0) != int(device_id):
                return Response({"detail": "ingreso_id no corresponde al equipo"}, status=400)

        now_ts = timezone.now()
        exec_void(
            """
            UPDATE devices
               SET mg_estado = 'activo',
                   mg_inactivo_desde = NULL,
                   mg_venta_fecha = NULL,
                   mg_venta_factura_numero = NULL,
                   mg_venta_remito_numero = NULL,
                   mg_venta_observaciones = NULL,
                   mg_venta_usuario_id = NULL,
                   mg_venta_customer_id = NULL,
                   mg_venta_numero_alternativo = NULL
             WHERE id = %s
            """,
            [device_id],
        )
        exec_void(
            """
            INSERT INTO device_mg_events(
              device_id, accion, numero_interno_snapshot, fecha_evento,
              factura_numero, remito_numero, observaciones, usuario_id, ingreso_id,
              venta_customer_id, venta_numero_alternativo, source
            )
            VALUES (%s, 'reactivacion', %s, %s, NULL, NULL, NULLIF(%s,''), %s, %s, NULL, NULL, %s)
            """,
            [
                device_id,
                mg_norm,
                now_ts,
                observaciones,
                uid,
                ingreso_id,
                source,
            ],
        )

        row = q(
            """
            SELECT
              d.id,
              COALESCE(d.numero_interno,'') AS numero_interno,
              COALESCE(d.numero_serie,'') AS numero_serie,
              COALESCE(d.mg_estado,'activo') AS mg_estado,
              d.mg_inactivo_desde,
              d.mg_venta_fecha,
              COALESCE(d.mg_venta_factura_numero,'') AS mg_venta_factura_numero,
              COALESCE(d.mg_venta_remito_numero,'') AS mg_venta_remito_numero,
              COALESCE(d.mg_venta_observaciones,'') AS mg_venta_observaciones,
              d.mg_venta_usuario_id,
              d.mg_venta_customer_id AS mg_venta_customer_id,
              COALESCE(c.razon_social,'') AS mg_venta_customer_nombre,
              COALESCE(d.mg_venta_numero_alternativo,'') AS mg_venta_numero_alternativo
            FROM devices d
            LEFT JOIN customers c ON c.id = d.mg_venta_customer_id
            WHERE d.id=%s
            """,
            [device_id],
            one=True,
        ) or {}
        row.update(resolve_mg_flags(row, mg_owner_id))
        return Response({"ok": True, "device": row})


__all__ = [
    "DeviceDirectCreateView",
    "DeviceIdentificadoresView",
    "DevicesListView",
    "DevicesMergeView",
    "DeviceMgVentaView",
    "DeviceMgReactivarView",
]
