import hashlib
import hmac
import ipaddress
import json

from django.conf import settings
from django.db import connection
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from service.ip_utils import get_client_ip

from .helpers import _fetchall_dicts, os_label


TALLER_LOCATION = "taller"
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50


def _setting_list(name):
    value = getattr(settings, name, [])
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _configured_token_hashes():
    hashes = []
    primary = (getattr(settings, "PORTAL_INTEGRATION_TOKEN_SHA256", "") or "").strip()
    if primary:
        hashes.append(primary.lower())
    hashes.extend(value.lower() for value in _setting_list("PORTAL_INTEGRATION_TOKEN_SHA256_FALLBACKS"))
    return [value for value in hashes if len(value) == 64]


def _bearer_token(request):
    header = request.META.get("HTTP_AUTHORIZATION", "") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _token_is_valid(request):
    expected_hashes = _configured_token_hashes()
    if not expected_hashes:
        return False

    token = _bearer_token(request)
    if not token:
        return False

    provided = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return any(hmac.compare_digest(provided, expected) for expected in expected_hashes)


def _ip_is_allowed(request):
    allowed = _setting_list("PORTAL_INTEGRATION_ALLOWED_IPS")
    if not allowed:
        return True

    client_ip = get_client_ip(request.META)
    try:
        parsed_ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    for entry in allowed:
        try:
            if "/" in entry:
                if parsed_ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif parsed_ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _unauthorized():
    return Response({"detail": "Unauthorized"}, status=401)


def _audit_portal_call(request, event_name, metadata=None, status_code=200):
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (ts, user_id, role, method, path, ip, user_agent, status_code, body)
                VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                [
                    None,
                    "portal_integration",
                    request.method,
                    request.path,
                    get_client_ip(request.META),
                    (request.META.get("HTTP_USER_AGENT", "") or "")[:512],
                    status_code,
                    json.dumps({"event": event_name, **(metadata or {})}),
                ],
            )
    except Exception:
        pass


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _clean(value):
    return "" if value is None else str(value).strip()


def _label(value):
    raw = _clean(value)
    if not raw:
        return ""
    key = raw.strip().lower().replace("_", " ")
    labels = {
        "ingresado": "Ingresado",
        "en diagnostico": "En Diagnostico",
        "diagnostico": "En Diagnostico",
        "diagnóstico": "En Diagnostico",
        "esperando presupuesto": "Esperando Presupuesto",
        "pendiente presupuesto": "Esperando Presupuesto",
        "pendiente": "Pendiente",
        "presupuestado": "Presupuestado",
        "en reparacion": "En Reparacion",
        "en reparación": "En Reparacion",
        "para reparar": "En Reparacion",
        "reparacion": "En Reparacion",
        "reparación": "En Reparacion",
        "detenido repuesto": "Detenido - Repuesto",
        "detenido - repuesto": "Detenido - Repuesto",
        "reparado": "Listo para Entrega",
        "controlado sin defecto": "Listo para Entrega",
        "listo para entrega": "Listo para Entrega",
        "liberado": "Listo para Entrega",
        "emitido": "Emitido",
        "enviado": "Emitido",
        "aprobado": "Aprobado",
        "rechazado": "Rechazado",
        "vencido": "Vencido",
        "no aplica": "No aplica",
    }
    return labels.get(key, raw[:1].upper() + raw[1:])


def _equipment_label(row):
    parts = [
        _clean(row.get("tipo_equipo")),
        _clean(row.get("marca")),
        _clean(row.get("modelo")),
        _clean(row.get("equipo_variante")),
    ]
    return " ".join(part for part in parts if part) or "Equipo sin modelo"


def _base_ingreso_payload(row):
    ingreso_id = row.get("id")
    received_at = row.get("fecha_ingreso") or row.get("fecha_creacion")
    updated_at = (
        row.get("updated_at")
        or row.get("presupuesto_fecha_emision")
        or row.get("fecha_servicio")
        or row.get("fecha_ingreso")
        or row.get("fecha_creacion")
    )
    budget_status = row.get("presupuesto_estado") or row.get("quote_estado")

    return {
        "id": str(ingreso_id),
        "nexoraIngresoId": ingreso_id,
        "ingresoNumber": os_label(ingreso_id),
        "companyId": str(row.get("customer_id") or ""),
        "companyName": _clean(row.get("razon_social")),
        "status": _label(row.get("estado")),
        "presupuestoStatus": _label(budget_status) if budget_status else "",
        "receivedAt": _iso(received_at),
        "updatedAt": _iso(updated_at),
        "estimatedDeliveryAt": None,
        "equipmentLabel": _equipment_label(row),
        "equipoId": str(row.get("device_id") or ""),
        "equipoModel": _equipment_label(row),
        "equipoSerial": _clean(row.get("numero_serie")),
        "equipoInternalNumber": _clean(row.get("numero_interno")),
        "locationName": _clean(row.get("ubicacion_nombre")),
        "budgetIssuedAt": _iso(row.get("presupuesto_fecha_emision")),
        "nexoraUrlPath": f"/ingresos/{ingreso_id}" if ingreso_id else "",
    }


def _priority_for_row(row):
    days = int(row.get("dias_espera") or 0)
    motivo = _clean(row.get("motivo")).lower()
    if row.get("derivado_devuelto") or "urgente" in motivo or days >= 16:
        return "Alta"
    if days >= 7:
        return "Media"
    return "Baja"


def _sla_for_row(row):
    days = int(row.get("dias_espera") or 0)
    if days >= 16:
        return "Vencido"
    if days >= 10:
        return "En riesgo"
    return "En plazo"


def _work_queue_payload(row):
    base = _base_ingreso_payload(row)
    assigned_user_id = row.get("asignado_a")
    return {
        "ingresoId": base["id"],
        "nexoraIngresoId": base["nexoraIngresoId"],
        "ingresoNumber": base["ingresoNumber"],
        "companyId": base["companyId"],
        "companyName": base["companyName"],
        "equipoId": base["equipoId"],
        "equipoModel": base["equipoModel"],
        "equipoSerial": base["equipoSerial"],
        "ingresoStatus": base["status"],
        "receivedAt": base["receivedAt"],
        "updatedAt": base["updatedAt"],
        "assignedUserId": str(assigned_user_id) if assigned_user_id is not None else None,
        "assignedUserName": _clean(row.get("asignado_a_nombre") or row.get("tecnico_nombre") or row.get("tecnico")) or None,
        "priority": _priority_for_row(row),
        "slaTargetAt": None,
        "slaStatus": _sla_for_row(row),
        "checklistTotal": 0,
        "checklistDone": 0,
        "notesCount": 0,
        "nexoraUrlPath": base["nexoraUrlPath"],
    }


def _positive_int(raw, fallback):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _pagination(request):
    page = _positive_int(request.GET.get("page"), 1)
    page_size = min(max(_positive_int(request.GET.get("pageSize") or request.GET.get("page_size"), DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE)
    return page, page_size, (page - 1) * page_size


def _state_candidates(value):
    raw = _clean(value)
    if not raw:
        return []
    key = raw.lower().replace("_", " ")
    candidates = {
        "ingresado": ["ingresado"],
        "en diagnostico": ["diagnostico", "en diagnostico", "diagnóstico", "en diagnóstico"],
        "en diagnóstico": ["diagnostico", "en diagnostico", "diagnóstico", "en diagnóstico"],
        "esperando presupuesto": ["pendiente presupuesto", "esperando presupuesto", "pendiente"],
        "presupuestado": ["presupuestado"],
        "en reparacion": ["para reparar", "reparacion", "reparación", "en reparacion", "en reparación"],
        "en reparación": ["para reparar", "reparacion", "reparación", "en reparacion", "en reparación"],
        "detenido - repuesto": ["detenido repuesto", "detenido - repuesto"],
        "listo para entrega": ["reparado", "controlado sin defecto", "liberado", "listo para entrega"],
    }
    return candidates.get(key, [key])


class PortalIntegrationBaseView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def authorize(self, request):
        if not _token_is_valid(request) or not _ip_is_allowed(request):
            return _unauthorized()
        return None


def _client_general_rows(customer_id, ingreso_id=None):
    filters = [customer_id, TALLER_LOCATION]
    ingreso_clause = ""
    if ingreso_id is not None:
        filters.insert(1, ingreso_id)
        ingreso_clause = "AND t.id = %s"

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              t.id,
              t.device_id,
              t.estado,
              t.presupuesto_estado,
              t.fecha_ingreso,
              t.fecha_creacion,
              t.fecha_servicio,
              GREATEST(
                COALESCE(t.fecha_servicio, t.fecha_ingreso, t.fecha_creacion),
                COALESCE(q.fecha_emitido, t.fecha_ingreso, t.fecha_creacion)
              ) AS updated_at,
              q.fecha_emitido AS presupuesto_fecha_emision,
              q.estado AS quote_estado,
              COALESCE(loc.nombre,'') AS ubicacion_nombre,
              c.id AS customer_id,
              c.razon_social,
              d.numero_serie,
              COALESCE(d.numero_interno,'') AS numero_interno,
              COALESCE(b.nombre,'') AS marca,
              COALESCE(m.nombre,'') AS modelo,
              COALESCE(m.tipo_equipo,'') AS tipo_equipo,
              COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN quotes q ON q.id = (
              SELECT q2.id FROM quotes q2
              WHERE q2.ingreso_id = t.id
              ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
              LIMIT 1
            )
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            WHERE c.id = %s
              {ingreso_clause}
              AND LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
              AND t.estado NOT IN ('entregado','alquilado','baja')
            ORDER BY t.fecha_ingreso DESC, t.id DESC
            """,
            filters,
        )
        return _fetchall_dicts(cur)


class PortalClienteGeneralView(PortalIntegrationBaseView):
    def get(self, request, customer_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        rows = _client_general_rows(customer_id)
        payload = {
            "generatedAt": _iso(timezone.now()),
            "customer": {
                "id": customer_id,
                "razonSocial": _clean(rows[0].get("razon_social")) if rows else "",
            },
            "items": [_base_ingreso_payload(row) for row in rows],
        }
        _audit_portal_call(request, "portal_client_general", {"customer_id": customer_id})
        return Response(payload)


class PortalClienteIngresoSummaryView(PortalIntegrationBaseView):
    def get(self, request, customer_id, ingreso_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        rows = _client_general_rows(customer_id, ingreso_id=ingreso_id)
        if not rows:
            _audit_portal_call(
                request,
                "portal_client_ingreso_summary_not_found",
                {"customer_id": customer_id, "ingreso_id": ingreso_id},
                status_code=404,
            )
            return Response({"detail": "Not found"}, status=404)

        payload = {
            "generatedAt": _iso(timezone.now()),
            "item": _base_ingreso_payload(rows[0]),
        }
        _audit_portal_call(request, "portal_client_ingreso_summary", {"customer_id": customer_id, "ingreso_id": ingreso_id})
        return Response(payload)


def _work_where(request):
    clauses = [
        "LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)",
        "t.estado NOT IN ('liberado','entregado','alquilado','baja')",
    ]
    params = [TALLER_LOCATION]

    customer_id = _clean(request.GET.get("customer_id") or request.GET.get("companyId"))
    if customer_id.isdigit():
        clauses.append("c.id = %s")
        params.append(int(customer_id))

    assigned = _clean(request.GET.get("assigned_user_id") or request.GET.get("assignedUserId"))
    if assigned:
        if assigned.lower() in {"sin_asignar", "unassigned", "none", "null"}:
            clauses.append("t.asignado_a IS NULL")
        elif assigned.isdigit():
            clauses.append("t.asignado_a = %s")
            params.append(int(assigned))

    status_candidates = _state_candidates(request.GET.get("status"))
    if status_candidates:
        clauses.append("LOWER(t.estado::text) = ANY(%s)")
        params.append(status_candidates)

    search = _clean(request.GET.get("search")).lower()
    if search:
        params.append(f"%{search}%")
        clauses.append(
            """(
              LOWER(c.razon_social) LIKE %s
              OR LOWER(COALESCE(d.numero_serie,'')) LIKE %s
              OR LOWER(COALESCE(d.numero_interno,'')) LIKE %s
              OR LOWER(COALESCE(b.nombre,'')) LIKE %s
              OR LOWER(COALESCE(m.nombre,'')) LIKE %s
              OR CAST(t.id AS TEXT) LIKE %s
            )"""
        )
        wildcard = params.pop()
        params.extend([wildcard, wildcard, wildcard, wildcard, wildcard, wildcard])

    return " AND ".join(clauses), params


def _work_queue_select(where_sql, params, limit=None, offset=None):
    pagination_sql = ""
    pagination_params = []
    if limit is not None and offset is not None:
        pagination_sql = "LIMIT %s OFFSET %s"
        pagination_params = [limit, offset]

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              t.id,
              t.device_id,
              t.estado,
              t.presupuesto_estado,
              t.motivo,
              t.fecha_ingreso,
              t.fecha_creacion,
              t.fecha_servicio,
              GREATEST(
                COALESCE(t.fecha_servicio, t.fecha_ingreso, t.fecha_creacion),
                COALESCE(q.fecha_emitido, t.fecha_ingreso, t.fecha_creacion)
              ) AS updated_at,
              q.fecha_emitido AS presupuesto_fecha_emision,
              q.estado AS quote_estado,
              c.id AS customer_id,
              c.razon_social,
              d.numero_serie,
              COALESCE(d.numero_interno,'') AS numero_interno,
              COALESCE(b.nombre,'') AS marca,
              COALESCE(m.nombre,'') AS modelo,
              COALESCE(m.tipo_equipo,'') AS tipo_equipo,
              COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
              t.asignado_a,
              COALESCE(u.nombre,'') AS asignado_a_nombre,
              COALESCE(u.nombre,'') AS tecnico_nombre,
              COALESCE(loc.nombre,'') AS ubicacion_nombre,
              GREATEST(0, (CURRENT_DATE - CAST(COALESCE(t.fecha_ingreso, t.fecha_creacion) AS DATE)))::int AS dias_espera,
              CASE WHEN ed.estado = 'devuelto' THEN true ELSE false END AS derivado_devuelto
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN users u ON u.id = t.asignado_a
            LEFT JOIN quotes q ON q.id = (
              SELECT q2.id FROM quotes q2
              WHERE q2.ingreso_id = t.id
              ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
              LIMIT 1
            )
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            LEFT JOIN (
              SELECT e.*, ROW_NUMBER() OVER (
                PARTITION BY e.ingreso_id ORDER BY e.fecha_deriv DESC, e.id DESC
              ) AS rn
              FROM equipos_derivados e
            ) ed ON ed.ingreso_id = t.id AND ed.rn = 1
            WHERE {where_sql}
            ORDER BY
              (CASE WHEN ed.estado = 'devuelto' THEN 1 ELSE 0 END) DESC,
              (t.motivo = 'urgente control') DESC,
              t.fecha_ingreso ASC,
              t.id ASC
            {pagination_sql}
            """,
            [*params, *pagination_params],
        )
        return _fetchall_dicts(cur)


def _work_count(where_sql, params):
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)::int AS total
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN users u ON u.id = t.asignado_a
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            WHERE {where_sql}
            """,
            params,
        )
        row = cur.fetchone()
    return int(row[0] if row else 0)


def _work_options():
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT c.id::text AS id, c.razon_social AS name
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
              AND t.estado NOT IN ('liberado','entregado','alquilado','baja')
            ORDER BY c.razon_social ASC
            """,
            [TALLER_LOCATION],
        )
        companies = _fetchall_dicts(cur)

        cur.execute(
            """
            SELECT DISTINCT u.id::text AS id, u.nombre AS name, COALESCE(u.rol, '') AS role
            FROM ingresos t
            JOIN users u ON u.id = t.asignado_a
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            WHERE LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
              AND t.estado NOT IN ('liberado','entregado','alquilado','baja')
              AND t.asignado_a IS NOT NULL
            ORDER BY u.nombre ASC
            """,
            [TALLER_LOCATION],
        )
        users = _fetchall_dicts(cur)

    return {"companies": companies, "users": users}


class PortalInternalWorkQueueView(PortalIntegrationBaseView):
    def get(self, request):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        page, page_size, offset = _pagination(request)
        where_sql, params = _work_where(request)
        total = _work_count(where_sql, params)
        rows = _work_queue_select(where_sql, params, page_size, offset)
        items = [_work_queue_payload(row) for row in rows]
        payload = {
            "generatedAt": _iso(timezone.now()),
            "items": items,
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "totalItems": total,
                "totalPages": max((total + page_size - 1) // page_size, 1),
            },
            **_work_options(),
        }
        _audit_portal_call(request, "portal_internal_work_queue", {"items": len(items), "total": total})
        return Response(payload)


class PortalInternalWorkSummaryView(PortalIntegrationBaseView):
    def get(self, request):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        where_sql, params = _work_where(request)
        rows = _work_queue_select(where_sql, params)
        items = [_work_queue_payload(row) for row in rows[:16]]
        stats = {
            "totalIngresos": len(rows),
            "highPriority": sum(1 for row in rows if _priority_for_row(row) == "Alta"),
            "unassigned": sum(1 for row in rows if row.get("asignado_a") is None),
            "slaAtRisk": sum(1 for row in rows if _sla_for_row(row) == "En riesgo"),
            "slaOverdue": sum(1 for row in rows if _sla_for_row(row) == "Vencido"),
        }
        payload = {
            "generatedAt": _iso(timezone.now()),
            "stats": stats,
            "prioridades": items,
        }
        _audit_portal_call(request, "portal_internal_work_summary", stats)
        return Response(payload)
