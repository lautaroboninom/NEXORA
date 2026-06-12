import hashlib
import hmac
import ipaddress
import json
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from rest_framework import permissions
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from service.notifications import emit_notification
from service.pdf import render_quote_pdf
from service.ip_utils import get_client_ip

from .helpers import _fetchall_dicts, _motivo_is_cotizacion_equipo, exec_void, money, os_label, q
from .ingreso_tests_views import (
    _default_instrumentos_for_protocol,
    _extract_schema_snapshot,
    _extract_values_from_payload,
    _has_ingreso_tests_table,
    _load_ingreso_context,
    _load_test_row,
    _merge_values,
    _protocol_from_schema_snapshot,
    _protocol_sections_with_values,
    _resolve_protocol_for_row,
    _safe_json,
    _trim_text,
)
from .quotes_views import _ingresos_has_permite_reparacion_col, _send_stock_min_alerts


TALLER_LOCATION = "taller"
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
BUDGET_STATES = ("presupuestado", "aprobado", "rechazado")
PORTAL_ACTOR_HEADER_PREFIX = "HTTP_X_PORTAL_"


class PortalPdfRenderer(BaseRenderer):
    media_type = "application/pdf"
    format = "pdf"
    charset = None

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


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
        with transaction.atomic():
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
        try:
            transaction.set_rollback(False)
        except Exception:
            pass
        pass


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _clean(value):
    return "" if value is None else str(value).strip()


def _clean_upper(value):
    return _clean(value).upper()


def _digits_only(value):
    return re.sub(r"\D+", "", _clean(value))


def _normalize_customer_name(value):
    text = unicodedata.normalize("NFD", _clean(value).upper())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"^\d+\s+", "", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\b(SOCIEDAD ANONIMA|S A S|S R L|S A|SRL|SAS|SA)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _customer_name_score(left, right):
    left_norm = _normalize_customer_name(left)
    right_norm = _normalize_customer_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _customer_names_compatible(left, right):
    return _customer_name_score(left, right) >= 0.72


def _label(value):
    raw = _clean(value)
    if not raw:
        return ""
    key = raw.strip().lower().replace("_", " ")
    labels = {
        "ingresado": "Ingresado",
        "en diagnostico": "En Diagnóstico",
        "diagnostico": "En Diagnóstico",
        "diagnóstico": "En Diagnóstico",
        "esperando presupuesto": "Esperando Presupuesto",
        "pendiente presupuesto": "Esperando Presupuesto",
        "pendiente": "Pendiente",
        "presupuestado": "Presupuestado",
        "en reparacion": "En Reparación",
        "en reparación": "En Reparación",
        "para reparar": "En Reparación",
        "reparacion": "En Reparación",
        "reparación": "En Reparación",
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


def _table_exists(table_name):
    try:
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_name = %s
                       AND table_schema = ANY(current_schemas(true))
                     LIMIT 1
                    """,
                    [table_name],
                )
                return cur.fetchone() is not None
            if connection.vendor == "sqlite":
                cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s LIMIT 1", [table_name])
                return cur.fetchone() is not None
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_name = %s
                 LIMIT 1
                """,
                [table_name],
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _article_variant_norm_expr():
    raw = "COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,''), '')"
    if connection.vendor == "postgresql":
        return f"LOWER(TRIM(REGEXP_REPLACE({raw}, '[^[:alnum:]]+', ' ', 'g')))"
    return f"LOWER(TRIM({raw}))"


def _bejerman_article_mapping_sql():
    if not _table_exists("bejerman_article_mappings"):
        return (
            "'' AS bejerman_article_code,\n              '' AS bejerman_article_description",
            "",
        )
    return (
        "COALESCE(bam.article_code,'') AS bejerman_article_code,\n"
        "              COALESCE(bam.article_description,'') AS bejerman_article_description",
        f"""
            LEFT JOIN bejerman_article_mappings bam
              ON bam.model_id = m.id
             AND bam.variante_norm = {_article_variant_norm_expr()}
        """,
    )


def _test_references(row, schema_snapshot):
    references_snapshot = _safe_json((row or {}).get("references_snapshot"), [])
    if not isinstance(references_snapshot, list):
        references_snapshot = []
    if not references_snapshot and schema_snapshot and isinstance(schema_snapshot.get("references"), list):
        references_snapshot = schema_snapshot.get("references") or []
    return references_snapshot


def _load_test_summary(ingreso_id):
    if not ingreso_id or not _has_ingreso_tests_table():
        return None

    row = _load_test_row(ingreso_id)
    if not row:
        return None

    ingreso = _load_ingreso_context(ingreso_id) or {}
    protocol_live = _resolve_protocol_for_row(ingreso, row)
    schema_snapshot = _extract_schema_snapshot(row)
    protocol = _protocol_from_schema_snapshot(schema_snapshot, protocol_live)
    references_snapshot = _test_references(row, schema_snapshot)

    return {
        "ingresoId": str(ingreso_id),
        "result": row.get("resultado_global") or "pendiente",
        "templateKey": row.get("template_key") or schema_snapshot.get("template_key") or (protocol or {}).get("template_key") or "",
        "templateVersion": row.get("template_version") or schema_snapshot.get("template_version") or (protocol or {}).get("template_version") or "",
        "protocolLabel": (protocol or {}).get("display_name") or row.get("tipo_equipo_snapshot") or "",
        "equipmentType": row.get("tipo_equipo_snapshot") or ingreso.get("tipo_equipo") or "",
        "executedAt": _iso(row.get("fecha_ejecucion")),
        "updatedAt": _iso(row.get("updated_at")),
        "conclusion": row.get("conclusion") or "",
        "instruments": row.get("instrumentos") or _default_instrumentos_for_protocol(protocol or {}),
        "signedBy": row.get("firmado_por") or row.get("tecnico_nombre") or "",
        "pdfAvailable": bool(protocol and references_snapshot),
    }


def _load_media_items(ingreso_id):
    if not ingreso_id or not _table_exists("ingreso_media"):
        return []

    rows = q(
        """
        SELECT
          id,
          ingreso_id,
          comentario,
          mime_type,
          size_bytes,
          width,
          height,
          original_name,
          created_at,
          updated_at
        FROM ingreso_media
        WHERE ingreso_id = %s
        ORDER BY created_at DESC, id DESC
        """,
        [ingreso_id],
    ) or []
    return [
        {
            "id": str(row.get("id")),
            "ingresoId": str(row.get("ingreso_id") or ingreso_id),
            "comment": row.get("comentario") or "",
            "mimeType": row.get("mime_type") or "application/octet-stream",
            "sizeBytes": row.get("size_bytes") or 0,
            "width": row.get("width"),
            "height": row.get("height"),
            "originalName": row.get("original_name") or f"adjunto-{row.get('id')}",
            "createdAt": _iso(row.get("created_at")),
            "updatedAt": _iso(row.get("updated_at")),
        }
        for row in rows
    ]


def _number(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _portal_actor(request):
    return {
        "user_id": _clean(request.META.get(f"{PORTAL_ACTOR_HEADER_PREFIX}USER_ID")),
        "email": _clean(request.META.get(f"{PORTAL_ACTOR_HEADER_PREFIX}USER_EMAIL")),
        "name": _clean(request.META.get(f"{PORTAL_ACTOR_HEADER_PREFIX}USER_NAME")),
        "session_id": _clean(request.META.get(f"{PORTAL_ACTOR_HEADER_PREFIX}SESSION_ID")),
        "client_ip": _clean(request.META.get(f"{PORTAL_ACTOR_HEADER_PREFIX}CLIENT_IP")),
        "user_agent": _clean(request.META.get(f"{PORTAL_ACTOR_HEADER_PREFIX}USER_AGENT"))[:512],
    }


def _budget_base_payload(row):
    ingreso_id = row.get("id")
    quote_id = row.get("quote_id")
    status = _label(row.get("quote_estado"))
    total = _number(row.get("quote_total"))
    return {
        "id": str(quote_id or ingreso_id),
        "quoteId": quote_id,
        "quoteNumber": f"P-{int(quote_id):06d}" if quote_id else f"P-{ingreso_id}",
        "ingresoId": str(ingreso_id),
        "nexoraIngresoId": ingreso_id,
        "ingresoNumber": os_label(ingreso_id),
        "companyId": str(row.get("customer_id") or ""),
        "companyName": _clean(row.get("razon_social")),
        "status": status,
        "actionable": status == "Presupuestado",
        "issueDate": _iso(row.get("fecha_emitido")),
        "decisionDate": _iso(row.get("fecha_rechazado") or row.get("fecha_aprobado")),
        "currency": _clean(row.get("moneda")) or "ARS",
        "subtotal": _number(row.get("quote_subtotal")),
        "iva21": _number(row.get("quote_iva_21")),
        "totalAmount": total,
        "equipmentLabel": _equipment_label(row),
        "equipoSerial": _clean(row.get("numero_serie")),
        "equipoInternalNumber": _clean(row.get("numero_interno")),
        "ingresoStatus": _label(row.get("estado")),
        "nexoraUrlPath": f"/ingresos/{ingreso_id}" if ingreso_id else "",
    }


def _quote_item_payload(row):
    return {
        "id": str(row.get("id") or ""),
        "type": _clean(row.get("tipo")),
        "description": _clean(row.get("descripcion")),
        "quantity": _number(row.get("qty")),
    }


def _budget_summary_payload(row):
    payload = _budget_base_payload(row)
    ingreso_id = row.get("id")
    items = q(
        """
        SELECT
          qi.id,
          qi.tipo,
          qi.descripcion,
          qi.qty
        FROM quote_items qi
        JOIN quotes qx ON qx.id = qi.quote_id
        WHERE qx.ingreso_id = %s
          AND qx.id = %s
        ORDER BY qi.id ASC
        """,
        [ingreso_id, row.get("quote_id")],
    ) or []
    payload.update(
        {
            "authorizedBy": _clean(row.get("autorizado_por")),
            "paymentTerms": _clean(row.get("forma_pago")),
            "deliveryTerms": _clean(row.get("plazo_entrega_txt")),
            "warrantyTerms": _clean(row.get("garantia_txt")),
            "offerValidity": _clean(row.get("mant_oferta_txt")),
            "diagnosis": _clean(row.get("descripcion_problema")) or _clean(row.get("informe_preliminar")),
            "workToDo": _clean(row.get("trabajos_realizados")),
            "items": [_quote_item_payload(item) for item in items],
        }
    )
    return payload


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
        "serviceResolution": _clean(row.get("resolucion")),
        "resolucion": _clean(row.get("resolucion")),
        "technicalReport": _load_test_summary(ingreso_id),
        "mediaItems": _load_media_items(ingreso_id),
        "bejermanArticleCode": _clean(row.get("bejerman_article_code")),
        "bejermanArticleName": _clean(row.get("bejerman_article_description")),
        "bejermanArticleDescription": _clean(row.get("bejerman_article_description")),
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
    estado = _clean(row.get("estado")).lower()
    released_at = row.get("fecha_liberacion")
    return {
        "ingresoId": base["id"],
        "nexoraIngresoId": base["nexoraIngresoId"],
        "ingresoNumber": base["ingresoNumber"],
        "companyId": base["companyId"],
        "companyName": base["companyName"],
        "equipoId": base["equipoId"],
        "equipoModel": base["equipoModel"],
        "equipoSerial": base["equipoSerial"],
        "equipoInternalNumber": base["equipoInternalNumber"],
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
        "serviceResolution": base["serviceResolution"],
        "resolucion": base["resolucion"],
        "releasedForDelivery": estado in {"liberado", "vendido_pendiente_entrega"} or released_at is not None,
        "releasedAt": _iso(released_at),
        "bejermanArticleCode": base["bejermanArticleCode"],
        "bejermanArticleName": base["bejermanArticleName"],
        "bejermanArticleDescription": base["bejermanArticleDescription"],
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
    article_select_sql, article_join_sql = _bejerman_article_mapping_sql()

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              t.id,
              t.device_id,
              t.estado,
              t.resolucion,
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
              {article_select_sql},
              COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            {article_join_sql}
            LEFT JOIN quotes q ON q.id = (
              SELECT q2.id FROM quotes q2
              WHERE q2.ingreso_id = t.id
              ORDER BY COALESCE(q2.version_num, 1) DESC, q2.id DESC
              LIMIT 1
            )
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            WHERE c.id = %s
              {ingreso_clause}
              AND LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)
              AND t.estado NOT IN ('entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
            ORDER BY t.fecha_ingreso DESC, t.id DESC
            """,
            filters,
        )
        return _fetchall_dicts(cur)


def _ingreso_summary_row(ingreso_id, customer_id=None):
    params = [ingreso_id]
    customer_clause = ""
    if customer_id is not None:
        params.append(customer_id)
        customer_clause = "AND c.id = %s"
    article_select_sql, article_join_sql = _bejerman_article_mapping_sql()

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              t.id,
              t.device_id,
              t.estado,
              t.resolucion,
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
              {article_select_sql},
              COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            {article_join_sql}
            LEFT JOIN quotes q ON q.id = (
              SELECT q2.id FROM quotes q2
              WHERE q2.ingreso_id = t.id
              ORDER BY COALESCE(q2.version_num, 1) DESC, q2.id DESC
              LIMIT 1
            )
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            WHERE t.id = %s
              {customer_clause}
            """,
            params,
        )
        rows = _fetchall_dicts(cur)
    return rows[0] if rows else None


def _load_media_row(ingreso_id, media_id, customer_id=None):
    if not _table_exists("ingreso_media"):
        return None

    params = [ingreso_id, media_id]
    customer_clause = ""
    if customer_id is not None:
        params.append(customer_id)
        customer_clause = "AND c.id = %s"

    return q(
        f"""
        SELECT
          im.id,
          im.ingreso_id,
          im.storage_path,
          im.thumbnail_path,
          im.original_name,
          im.mime_type,
          im.size_bytes
        FROM ingreso_media im
        JOIN ingresos t ON t.id = im.ingreso_id
        JOIN devices d ON d.id = t.device_id
        JOIN customers c ON c.id = d.customer_id
        WHERE im.ingreso_id = %s
          AND im.id = %s
          {customer_clause}
        """,
        params,
        one=True,
    )


def _render_test_pdf_response(ingreso_id, customer_id=None):
    summary_row = _ingreso_summary_row(ingreso_id, customer_id=customer_id)
    if not summary_row:
        return Response({"detail": "Not found"}, status=404)
    if not _has_ingreso_tests_table():
        return Response({"detail": "Tabla ingreso_tests inexistente"}, status=503)

    row = _load_test_row(ingreso_id)
    if not row:
        return Response({"detail": "No existe test guardado para este ingreso"}, status=404)

    ingreso = _load_ingreso_context(ingreso_id) or {}
    protocol_live = _resolve_protocol_for_row(ingreso, row)
    schema_snapshot = _extract_schema_snapshot(row)
    protocol = _protocol_from_schema_snapshot(schema_snapshot, protocol_live)
    if protocol is None:
        return Response({"detail": "No se pudo resolver protocolo de test"}, status=409)

    references_snapshot = _test_references(row, schema_snapshot)
    if not references_snapshot:
        return Response({"detail": "No hay referencias congeladas para este test"}, status=409)

    values = _merge_values(_extract_values_from_payload(row.get("payload")), None, protocol)
    resultado_global = _trim_text(row.get("resultado_global"), 50).lower()
    report = {
        "ingreso_id": ingreso_id,
        "os": ingreso_id,
        "fecha_ejecucion": row.get("fecha_ejecucion") or timezone.now(),
        "cliente": ingreso.get("cliente") or summary_row.get("razon_social") or "",
        "tipo_equipo": ingreso.get("tipo_equipo") or summary_row.get("tipo_equipo") or "",
        "marca": ingreso.get("marca") or summary_row.get("marca") or "",
        "modelo": ingreso.get("modelo") or summary_row.get("modelo") or "",
        "numero_serie": ingreso.get("numero_serie") or summary_row.get("numero_serie") or "",
        "numero_interno": ingreso.get("numero_interno") or summary_row.get("numero_interno") or "",
        "template_key": row.get("template_key") or schema_snapshot.get("template_key") or protocol.get("template_key") or "",
        "template_version": row.get("template_version") or schema_snapshot.get("template_version") or protocol.get("template_version") or "",
        "resultado_global": resultado_global or "pendiente",
        "conclusion": row.get("conclusion") or "",
        "instrumentos": row.get("instrumentos") or _default_instrumentos_for_protocol(protocol),
        "firmado_por": row.get("firmado_por") or "",
        "references": references_snapshot,
        "sections": _protocol_sections_with_values(protocol, values),
    }
    from ..test_pdf import render_ingreso_test_pdf

    pdf_bytes, fname = render_ingreso_test_pdf(report, printed_by="Portal SEPID")
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{fname}"'
    return response


def _media_file_response(ingreso_id, media_id, kind, customer_id=None):
    row = _load_media_row(ingreso_id, media_id, customer_id=customer_id)
    if not row:
        return Response({"detail": "Adjunto no encontrado"}, status=404)

    storage_path = row.get("thumbnail_path") if kind == "miniatura" else row.get("storage_path")
    if not storage_path:
        return Response({"detail": "Archivo no encontrado"}, status=404)

    try:
        file_obj = default_storage.open(storage_path, "rb")
    except FileNotFoundError:
        return Response({"detail": "Archivo no disponible"}, status=410)
    except Exception:
        return Response({"detail": "No se pudo abrir el archivo"}, status=500)

    content_type = "image/jpeg" if kind == "miniatura" else (row.get("mime_type") or "application/octet-stream")
    response = FileResponse(file_obj, content_type=content_type)
    size_bytes = row.get("size_bytes")
    if size_bytes and kind != "miniatura":
        response["Content-Length"] = str(size_bytes)
    filename = row.get("original_name") or f"ingreso-{ingreso_id}-adjunto-{media_id}"
    response["Content-Disposition"] = f"{'inline' if kind == 'miniatura' else 'inline'}; filename*=UTF-8''{quote(str(filename))}"
    response["Cache-Control"] = "private, max-age=86400"
    return response


def _budget_rows(customer_id, ingreso_id=None):
    state_placeholders = ", ".join(["%s"] * len(BUDGET_STATES))
    params = [customer_id, *BUDGET_STATES]
    ingreso_clause = ""
    if ingreso_id is not None:
        params.insert(1, ingreso_id)
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
              COALESCE(t.informe_preliminar, '') AS informe_preliminar,
              COALESCE(t.descripcion_problema, '') AS descripcion_problema,
              COALESCE(t.trabajos_realizados, '') AS trabajos_realizados,
              q.id AS quote_id,
              q.estado AS quote_estado,
              COALESCE(q.moneda, 'ARS') AS moneda,
              COALESCE(q.subtotal, 0) AS quote_subtotal,
              COALESCE(q.iva_21, 0) AS quote_iva_21,
              COALESCE(q.total, 0) AS quote_total,
              q.fecha_emitido,
              q.fecha_aprobado,
              q.fecha_rechazado,
              COALESCE(q.autorizado_por, '') AS autorizado_por,
              COALESCE(q.forma_pago, '') AS forma_pago,
              COALESCE(q.plazo_entrega_txt, '') AS plazo_entrega_txt,
              COALESCE(q.garantia_txt, '') AS garantia_txt,
              COALESCE(q.mant_oferta_txt, '') AS mant_oferta_txt,
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
            JOIN quotes q ON q.id = (
              SELECT q2.id FROM quotes q2
              WHERE q2.ingreso_id = t.id
              ORDER BY COALESCE(q2.version_num, 1) DESC, q2.id DESC
              LIMIT 1
            )
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            WHERE c.id = %s
              {ingreso_clause}
              AND q.estado IN ({state_placeholders})
            ORDER BY (q.fecha_emitido IS NULL) ASC, q.fecha_emitido DESC, t.id DESC
            """,
            params,
        )
        return _fetchall_dicts(cur)


def _budget_row(customer_id, ingreso_id):
    rows = _budget_rows(customer_id, ingreso_id=ingreso_id)
    return rows[0] if rows else None


def _all_budget_rows():
    state_placeholders = ", ".join(["%s"] * len(BUDGET_STATES))
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
              COALESCE(t.informe_preliminar, '') AS informe_preliminar,
              COALESCE(t.descripcion_problema, '') AS descripcion_problema,
              COALESCE(t.trabajos_realizados, '') AS trabajos_realizados,
              q.id AS quote_id,
              q.estado AS quote_estado,
              COALESCE(q.moneda, 'ARS') AS moneda,
              COALESCE(q.subtotal, 0) AS quote_subtotal,
              COALESCE(q.iva_21, 0) AS quote_iva_21,
              COALESCE(q.total, 0) AS quote_total,
              q.fecha_emitido,
              q.fecha_aprobado,
              q.fecha_rechazado,
              COALESCE(q.autorizado_por, '') AS autorizado_por,
              COALESCE(q.forma_pago, '') AS forma_pago,
              COALESCE(q.plazo_entrega_txt, '') AS plazo_entrega_txt,
              COALESCE(q.garantia_txt, '') AS garantia_txt,
              COALESCE(q.mant_oferta_txt, '') AS mant_oferta_txt,
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
            JOIN quotes q ON q.id = (
              SELECT q2.id FROM quotes q2
              WHERE q2.ingreso_id = t.id
              ORDER BY COALESCE(q2.version_num, 1) DESC, q2.id DESC
              LIMIT 1
            )
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            WHERE q.estado IN ({state_placeholders})
            ORDER BY (q.fecha_emitido IS NULL) ASC, q.fecha_emitido DESC, t.id DESC
            """,
            list(BUDGET_STATES),
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


class PortalInternalIngresoSummaryView(PortalIntegrationBaseView):
    def get(self, request, ingreso_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        row = _ingreso_summary_row(ingreso_id)
        if not row:
            _audit_portal_call(
                request,
                "portal_internal_ingreso_summary_not_found",
                {"ingreso_id": ingreso_id},
                status_code=404,
            )
            return Response({"detail": "Not found"}, status=404)

        payload = {
            "generatedAt": _iso(timezone.now()),
            "item": _base_ingreso_payload(row),
        }
        _audit_portal_call(request, "portal_internal_ingreso_summary", {"ingreso_id": ingreso_id})
        return Response(payload)


class PortalClienteIngresoTestPdfView(PortalIntegrationBaseView):
    renderer_classes = [PortalPdfRenderer, JSONRenderer]

    def get(self, request, customer_id, ingreso_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized
        return _render_test_pdf_response(ingreso_id, customer_id=customer_id)


class PortalInternalIngresoTestPdfView(PortalIntegrationBaseView):
    renderer_classes = [PortalPdfRenderer, JSONRenderer]

    def get(self, request, ingreso_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized
        return _render_test_pdf_response(ingreso_id)


class PortalClienteIngresoMediaFileView(PortalIntegrationBaseView):
    def get(self, request, customer_id, ingreso_id, media_id, kind):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized
        if kind not in {"archivo", "miniatura"}:
            return Response({"detail": "Tipo de archivo invalido"}, status=400)
        return _media_file_response(ingreso_id, media_id, kind, customer_id=customer_id)


class PortalInternalIngresoMediaFileView(PortalIntegrationBaseView):
    def get(self, request, ingreso_id, media_id, kind):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized
        if kind not in {"archivo", "miniatura"}:
            return Response({"detail": "Tipo de archivo invalido"}, status=400)
        return _media_file_response(ingreso_id, media_id, kind)


def _jefe_user_ids():
    rows = q(
        """
        SELECT id
        FROM users
        WHERE activo
          AND LOWER(COALESCE(rol, '')) = 'jefe'
        """,
        [],
    ) or []
    return [row.get("id") for row in rows if row.get("id") is not None]


def _notify_budget_decision(row, decision, actor, reason=""):
    user_ids = _jefe_user_ids()
    if not user_ids:
        return 0

    ingreso_id = row.get("id")
    action = "aprobo" if decision == "approve" else "rechazo"
    actor_label = actor.get("name") or actor.get("email") or actor.get("user_id") or "Cliente Portal"
    reason_line = f"\nMotivo: {reason}" if reason else ""
    body = "\n".join(
        [
            f"{actor_label} {action} el presupuesto desde Portal.",
            f"Cliente: {_clean(row.get('razon_social')) or '-'}",
            f"Equipo: {_equipment_label(row)}",
            f"Serie/MG: {_clean(row.get('numero_interno')) or _clean(row.get('numero_serie')) or '-'}",
            f"Monto: {_clean(row.get('moneda')) or 'ARS'} {_number(row.get('quote_total')):,.2f}",
            reason_line,
        ]
    ).strip()
    return emit_notification(
        "presupuesto_decision_portal",
        user_ids=user_ids,
        title=f"Decision de presupuesto Portal - {os_label(ingreso_id)}",
        body=body,
        href=f"/ingresos/{ingreso_id}?tab=presupuesto",
        severity="warning" if decision == "reject" else "info",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:portal_budget_decision:{decision}:{timezone.now().isoformat()}",
        payload={"ingreso_id": ingreso_id, "decision": decision, "actor": actor, "reason": reason},
    )


def _save_budget_pdf_copy(ingreso_id, quote_id=None):
    try:
        with transaction.atomic():
            fname, pdf = render_quote_pdf(ingreso_id, quote_id=quote_id)
            save_dir = getattr(settings, "QUOTES_SAVE_DIR", None)
            if save_dir and pdf:
                import os

                os.makedirs(save_dir, exist_ok=True)
                with open(os.path.join(save_dir, fname), "wb") as fh:
                    fh.write(pdf)
    except Exception:
        try:
            transaction.set_rollback(False)
        except Exception:
            pass
        pass


def _approve_budget_from_portal(row):
    ingreso_id = row.get("id")
    qid = row.get("quote_id")
    alert_items = []
    with transaction.atomic():
        locked = q("SELECT estado FROM quotes WHERE id=%s FOR UPDATE", [qid], one=True) or {}
        if (locked.get("estado") or "").strip() != "presupuestado":
            return False, "conflict"

        exec_void(
            """
            UPDATE quotes
               SET estado='aprobado',
                   fecha_aprobado=now()
             WHERE id=%s
            """,
            [qid],
        )

        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        permite_reparacion_sql = (
            "COALESCE(permite_reparacion, TRUE) AS permite_reparacion"
            if has_permite_reparacion
            else "TRUE AS permite_reparacion"
        )
        ingreso_row = q(
            f"""
            SELECT estado, motivo, {permite_reparacion_sql}
              FROM ingresos
             WHERE id=%s
             FOR UPDATE
            """,
            [ingreso_id],
            one=True,
        ) or {}
        bloqueada_por_cotizacion = _motivo_is_cotizacion_equipo(ingreso_row.get("motivo")) and not bool(
            ingreso_row.get("permite_reparacion")
        )
        if bloqueada_por_cotizacion:
            exec_void("UPDATE ingresos SET presupuesto_estado='aprobado' WHERE id=%s", [ingreso_id])
        else:
            exec_void(
                """
                UPDATE ingresos
                   SET presupuesto_estado='aprobado',
                       estado = CASE
                                  WHEN estado IN ('ingresado','diagnosticado','presupuestado')
                                  THEN 'reparar'
                                  ELSE estado
                                END
                 WHERE id=%s
                """,
                [ingreso_id],
            )

        items = q(
            """
            SELECT repuesto_id, SUM(qty) AS qty
            FROM quote_items
            WHERE quote_id=%s
              AND tipo='repuesto'
              AND repuesto_id IS NOT NULL
            GROUP BY repuesto_id
            """,
            [qid],
        ) or []
        for item in items:
            rep_id = item.get("repuesto_id")
            if not rep_id:
                continue
            qty = money(item.get("qty") or 0)
            if qty == 0:
                continue
            rep_row = q(
                """
                SELECT id, codigo, nombre, stock_on_hand, stock_min, ubicacion_deposito
                FROM catalogo_repuestos
                WHERE id=%s
                FOR UPDATE
                """,
                [rep_id],
                one=True,
            )
            if not rep_row:
                continue
            stock_prev = money(rep_row.get("stock_on_hand") or 0)
            stock_min = money(rep_row.get("stock_min") or 0)
            delta = money(-qty)
            stock_new = money(stock_prev + delta)
            exec_void(
                "UPDATE catalogo_repuestos SET stock_on_hand=%s, updated_at=NOW() WHERE id=%s",
                [stock_new, rep_id],
            )
            exec_void(
                """
                INSERT INTO repuestos_movimientos
                  (repuesto_id, tipo, qty, stock_prev, stock_new, ref_tipo, ref_id, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [rep_id, "egreso_aprobado", delta, stock_prev, stock_new, "quote", qid, None],
            )
            if stock_prev > stock_min and stock_new <= stock_min:
                alert_items.append(
                    {
                        "id": rep_row.get("id"),
                        "codigo": rep_row.get("codigo"),
                        "nombre": rep_row.get("nombre"),
                        "stock_on_hand": stock_new,
                        "stock_min": stock_min,
                        "ubicacion_deposito": rep_row.get("ubicacion_deposito"),
                    }
                )

    if alert_items:
        try:
            _send_stock_min_alerts(alert_items)
        except Exception:
            try:
                transaction.set_rollback(False)
            except Exception:
                pass
            pass
    _save_budget_pdf_copy(ingreso_id, quote_id=qid)
    return True, ""


def _reject_budget_from_portal(row, reason=""):
    ingreso_id = row.get("id")
    qid = row.get("quote_id")
    with transaction.atomic():
        locked = q("SELECT estado FROM quotes WHERE id=%s FOR UPDATE", [qid], one=True) or {}
        if (locked.get("estado") or "").strip() != "presupuestado":
            return False
        exec_void(
            """
            UPDATE quotes
               SET estado='rechazado',
                   fecha_rechazado=now(),
                   rechazo_comentario=%s
             WHERE id=%s
            """,
            [reason or None, qid],
        )
        exec_void("UPDATE ingresos SET presupuesto_estado='rechazado' WHERE id=%s", [ingreso_id])
    return True


class PortalClientePresupuestosView(PortalIntegrationBaseView):
    def get(self, request, customer_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        rows = _budget_rows(customer_id)
        status_filter = _clean(request.GET.get("status") or request.GET.get("estado"))
        if status_filter:
            rows = [row for row in rows if _label(row.get("quote_estado")) == status_filter]

        payload = {
            "generatedAt": _iso(timezone.now()),
            "customer": {
                "id": customer_id,
                "razonSocial": _clean(rows[0].get("razon_social")) if rows else "",
            },
            "items": [_budget_base_payload(row) for row in rows],
        }
        _audit_portal_call(request, "portal_client_budgets", {"customer_id": customer_id, "items": len(rows)})
        return Response(payload)


class PortalClientePresupuestoSummaryView(PortalIntegrationBaseView):
    def get(self, request, customer_id, ingreso_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        row = _budget_row(customer_id, ingreso_id)
        if not row:
            _audit_portal_call(
                request,
                "portal_client_budget_summary_not_found",
                {"customer_id": customer_id, "ingreso_id": ingreso_id},
                status_code=404,
            )
            return Response({"detail": "Not found"}, status=404)

        _audit_portal_call(request, "portal_client_budget_summary", {"customer_id": customer_id, "ingreso_id": ingreso_id})
        return Response({"generatedAt": _iso(timezone.now()), "item": _budget_summary_payload(row)})


class PortalClientePresupuestoPdfView(PortalIntegrationBaseView):
    renderer_classes = [JSONRenderer, PortalPdfRenderer]

    def get(self, request, customer_id, ingreso_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        row = _budget_row(customer_id, ingreso_id)
        if not row:
            _audit_portal_call(
                request,
                "portal_client_budget_pdf_not_found",
                {"customer_id": customer_id, "ingreso_id": ingreso_id},
                status_code=404,
            )
            return Response({"detail": "Not found"}, status=404)

        fname, pdf = render_quote_pdf(ingreso_id, quote_id=row.get("quote_id"))
        if not pdf:
            return Response({"detail": "Not found"}, status=404)
        _audit_portal_call(request, "portal_client_budget_pdf", {"customer_id": customer_id, "ingreso_id": ingreso_id})
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{fname}"'
        return response


class PortalClientePresupuestoDecisionView(PortalIntegrationBaseView):
    def post(self, request, customer_id, ingreso_id):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        row = _budget_row(customer_id, ingreso_id)
        if not row:
            _audit_portal_call(
                request,
                "portal_client_budget_decision_not_found",
                {"customer_id": customer_id, "ingreso_id": ingreso_id},
                status_code=404,
            )
            return Response({"detail": "Not found"}, status=404)

        if (row.get("quote_estado") or "").strip() != "presupuestado":
            return Response({"detail": "Budget already decided"}, status=409)

        payload = request.data if isinstance(request.data, dict) else {}
        decision = _clean(payload.get("decision"))
        reason = _clean(payload.get("reason"))
        if decision == "approve":
            if set(payload.keys()) != {"decision"}:
                return Response({"detail": "Invalid payload"}, status=400)
            ok, code = _approve_budget_from_portal(row)
            if not ok and code == "conflict":
                return Response({"detail": "Budget already decided"}, status=409)
        elif decision == "reject":
            if set(payload.keys()) != {"decision", "reason"} or not reason:
                return Response({"detail": "Reason required"}, status=400)
            if len(reason) > 1000:
                return Response({"detail": "Reason too long"}, status=400)
            if not _reject_budget_from_portal(row, reason=reason):
                return Response({"detail": "Budget already decided"}, status=409)
        else:
            return Response({"detail": "Invalid decision"}, status=400)

        updated = _budget_row(customer_id, ingreso_id)
        actor = _portal_actor(request)
        _notify_budget_decision(updated or row, decision, actor, reason)
        _audit_portal_call(
            request,
            "portal_client_budget_decision",
            {
                "customer_id": customer_id,
                "ingreso_id": ingreso_id,
                "decision": decision,
                "reason": reason,
                "actor": actor,
            },
        )
        return Response({"generatedAt": _iso(timezone.now()), "item": _budget_summary_payload(updated or row)})


class PortalInternalPresupuestosView(PortalIntegrationBaseView):
    def get(self, request):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        page, page_size, offset = _pagination(request)
        rows = _all_budget_rows()

        status_filter = _clean(request.GET.get("status") or request.GET.get("estado"))
        if status_filter:
            rows = [row for row in rows if _label(row.get("quote_estado")) == status_filter]

        search = _clean(request.GET.get("search")).lower()
        if search:
            rows = [
                row
                for row in rows
                if any(
                    search in _clean(value).lower()
                    for value in (
                        row.get("id"),
                        row.get("quote_id"),
                        row.get("razon_social"),
                        row.get("numero_serie"),
                        row.get("numero_interno"),
                        row.get("marca"),
                        row.get("modelo"),
                    )
                )
            ]

        total = len(rows)
        items = rows[offset: offset + page_size]
        payload = {
            "generatedAt": _iso(timezone.now()),
            "items": [_budget_base_payload(row) for row in items],
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "totalItems": total,
                "totalPages": max((total + page_size - 1) // page_size, 1),
            },
        }
        _audit_portal_call(request, "portal_internal_budgets", {"items": len(items), "total": total})
        return Response(payload)


def _bejerman_client_payload(data):
    payload = data or {}
    code = _clean(payload.get("bejermanCustomerCode") or payload.get("customerCode") or payload.get("code"))
    name = _clean(payload.get("name") or payload.get("razonSocial") or payload.get("razon_social"))
    return {
        "code": code,
        "name": name,
        "tax_id": _clean(payload.get("taxId") or payload.get("cuit")),
        "email": _clean(payload.get("email")),
        "phone": _clean(payload.get("phone") or payload.get("telefono")),
    }


def _load_customer_rows_for_bejerman():
    return q(
        """
        SELECT id,
               razon_social,
               COALESCE(cod_empresa, '') AS cod_empresa,
               COALESCE(cuit, '') AS cuit,
               COALESCE(telefono, '') AS telefono,
               COALESCE(email, '') AS email
          FROM customers
         ORDER BY id ASC
        """
    ) or []


def _customer_suggestion(row, score=None):
    return {
        "id": row.get("id"),
        "name": _clean(row.get("razon_social")),
        "taxId": _clean(row.get("cuit")) or None,
        "score": score,
    }


def _customer_upsert_response(outcome, customer_id=None, suggestions=None):
    return {
        "outcome": outcome,
        "nexoraCustomerId": customer_id,
        "customerId": customer_id,
        "suggestions": suggestions or [],
    }


def _update_customer_from_bejerman(customer_id, payload, *, overwrite_code):
    code = _clean_upper(payload["code"])
    tax_id = _clean(payload["tax_id"])
    phone = _clean(payload["phone"])
    email = _clean(payload["email"])
    if overwrite_code:
        code_sql = "cod_empresa = %s"
    else:
        code_sql = "cod_empresa = COALESCE(NULLIF(TRIM(cod_empresa), ''), %s)"

    exec_void(
        f"""
        UPDATE customers
           SET {code_sql},
               cuit = CASE
                 WHEN NULLIF(TRIM(COALESCE(cuit, '')), '') IS NULL AND %s <> '' THEN %s
                 ELSE cuit
               END,
               telefono = CASE
                 WHEN NULLIF(TRIM(COALESCE(telefono, '')), '') IS NULL AND %s <> '' THEN %s
                 ELSE telefono
               END,
               email = CASE
                 WHEN NULLIF(TRIM(COALESCE(email, '')), '') IS NULL AND %s <> '' THEN %s
                 ELSE email
               END
         WHERE id = %s
        """,
        [code, tax_id, tax_id, phone, phone, email, email, customer_id],
    )


def _create_customer_from_bejerman(payload):
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO customers(cod_empresa, razon_social, cuit, telefono, email)
            VALUES (%s, %s, NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''))
            RETURNING id
            """,
            [
                _clean_upper(payload["code"]),
                payload["name"],
                _clean(payload["tax_id"]),
                _clean(payload["phone"]),
                _clean(payload["email"]),
            ],
        )
        row = cur.fetchone()
    return row[0]


def _rows_by_bejerman_code(rows, code):
    clean_code = _clean_upper(code)
    return [row for row in rows if _clean_upper(row.get("cod_empresa")) == clean_code]


def _rows_by_tax_id(rows, tax_id):
    clean_tax_id = _digits_only(tax_id)
    if not clean_tax_id:
        return []
    return [row for row in rows if _digits_only(row.get("cuit")) == clean_tax_id]


def _rows_by_exact_name(rows, name):
    normalized_name = _normalize_customer_name(name)
    if not normalized_name:
        return []
    return [row for row in rows if _normalize_customer_name(row.get("razon_social")) == normalized_name]


def _review_response(rows, input_name, *, event_name, request):
    suggestions = [
        _customer_suggestion(row, _customer_name_score(row.get("razon_social"), input_name))
        for row in rows[:5]
    ]
    _audit_portal_call(request, event_name, {"outcome": "needs_review", "suggestions": len(suggestions)}, status_code=200)
    return Response(_customer_upsert_response("needs_review", suggestions=suggestions))


class PortalInternalBejermanClientUpsertView(PortalIntegrationBaseView):
    def post(self, request):
        unauthorized = self.authorize(request)
        if unauthorized:
            return unauthorized

        payload = _bejerman_client_payload(request.data)
        if not payload["code"] or not payload["name"]:
            return Response({"detail": "bejermanCustomerCode y name son requeridos"}, status=400)

        payload["code"] = _clean_upper(payload["code"])
        rows = _load_customer_rows_for_bejerman()
        event_name = "portal_internal_bejerman_client_upsert"

        with transaction.atomic():
            code_matches = _rows_by_bejerman_code(rows, payload["code"])
            if len(code_matches) > 1:
                return _review_response(code_matches, payload["name"], event_name=event_name, request=request)
            if len(code_matches) == 1:
                row = code_matches[0]
                if not _customer_names_compatible(row.get("razon_social"), payload["name"]):
                    return _review_response([row], payload["name"], event_name=event_name, request=request)
                _update_customer_from_bejerman(row["id"], payload, overwrite_code=False)
                _audit_portal_call(request, event_name, {"outcome": "matched", "customer_id": row["id"]})
                return Response(_customer_upsert_response("matched", row["id"]))

            tax_matches = _rows_by_tax_id(rows, payload["tax_id"])
            if len(tax_matches) > 1:
                return _review_response(tax_matches, payload["name"], event_name=event_name, request=request)
            if len(tax_matches) == 1:
                row = tax_matches[0]
                _update_customer_from_bejerman(row["id"], payload, overwrite_code=True)
                _audit_portal_call(request, event_name, {"outcome": "matched", "customer_id": row["id"], "match": "tax_id"})
                return Response(_customer_upsert_response("matched", row["id"]))

            name_matches = _rows_by_exact_name(rows, payload["name"])
            if len(name_matches) > 1:
                return _review_response(name_matches, payload["name"], event_name=event_name, request=request)
            if len(name_matches) == 1:
                row = name_matches[0]
                _update_customer_from_bejerman(row["id"], payload, overwrite_code=True)
                _audit_portal_call(request, event_name, {"outcome": "matched", "customer_id": row["id"], "match": "name"})
                return Response(_customer_upsert_response("matched", row["id"]))

            fuzzy_matches = [
                (row, _customer_name_score(row.get("razon_social"), payload["name"]))
                for row in rows
            ]
            fuzzy_matches = [(row, score) for row, score in fuzzy_matches if score >= 0.82]
            fuzzy_matches.sort(key=lambda item: item[1], reverse=True)
            if fuzzy_matches:
                suggestions = [_customer_suggestion(row, score) for row, score in fuzzy_matches[:5]]
                _audit_portal_call(request, event_name, {"outcome": "needs_review", "suggestions": len(suggestions)})
                return Response(_customer_upsert_response("needs_review", suggestions=suggestions))

            customer_id = _create_customer_from_bejerman(payload)
            _audit_portal_call(request, event_name, {"outcome": "created", "customer_id": customer_id})
            return Response(_customer_upsert_response("created", customer_id))


def _work_where(request):
    status_candidates = _state_candidates(request.GET.get("status"))
    includes_released = any(
        candidate in {"liberado", "vendido_pendiente_entrega"}
        for candidate in status_candidates
    )
    clauses = [
        "LOWER(COALESCE(loc.nombre,'')) = LOWER(%s)",
    ]
    if includes_released:
        clauses.append("t.estado NOT IN ('entregado','alquilado','baja','vendido_entregado')")
    else:
        clauses.append("t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')")
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
    article_select_sql, article_join_sql = _bejerman_article_mapping_sql()

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              t.id,
              t.device_id,
              t.estado,
              t.resolucion,
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
              {article_select_sql},
              COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
              t.asignado_a,
              COALESCE(u.nombre,'') AS asignado_a_nombre,
              COALESCE(u.nombre,'') AS tecnico_nombre,
              COALESCE(loc.nombre,'') AS ubicacion_nombre,
              ev.fecha_liberacion,
              GREATEST(0, (CURRENT_DATE - CAST(COALESCE(t.fecha_ingreso, t.fecha_creacion) AS DATE)))::int AS dias_espera,
              CASE WHEN ed.estado = 'devuelto' THEN true ELSE false END AS derivado_devuelto
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            {article_join_sql}
            LEFT JOIN users u ON u.id = t.asignado_a
            LEFT JOIN quotes q ON q.id = (
              SELECT q2.id FROM quotes q2
              WHERE q2.ingreso_id = t.id
              ORDER BY COALESCE(q2.version_num, 1) DESC, q2.id DESC
              LIMIT 1
            )
            LEFT JOIN locations loc ON loc.id = t.ubicacion_id
            LEFT JOIN (
              SELECT ingreso_id, MAX(ts) AS fecha_liberacion
              FROM ingreso_events
              WHERE a_estado = 'liberado'
              GROUP BY ingreso_id
            ) ev ON ev.ingreso_id = t.id
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
              AND t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
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
              AND t.estado NOT IN ('liberado','entregado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
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
        rows = _work_queue_select(where_sql, params)
        items = [_work_queue_payload(row) for row in rows]
        priority = _clean(request.GET.get("priority"))
        if priority:
            items = [item for item in items if item["priority"] == priority]
        sla_status = _clean(request.GET.get("slaStatus") or request.GET.get("sla_status"))
        if sla_status:
            items = [item for item in items if item["slaStatus"] == sla_status]
        total = len(items)
        items = items[offset: offset + page_size]
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
