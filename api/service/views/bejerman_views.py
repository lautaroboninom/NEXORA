import json
import logging
from datetime import timedelta

from django.db import connection
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_delivery import (
    generate_bejerman_remito,
    reissue_corrected_bejerman_remito_review,
    resolve_bejerman_remito_review,
    review_candidates_for_group,
    verify_bejerman_remito_review,
)
from ..bejerman_ris import (
    BejermanRisBusyError,
    BejermanRisError,
    BejermanRisPreflightError,
    emit_or_get_ris,
    register_ris_batch,
)
from ..bejerman_sync import (
    BejermanBlockedError,
    BejermanConfigError,
    BejermanSDKClient,
    BejermanTransientError,
    LEGACY_BLOCKED_SYNC_TYPES,
    NEXORA_STOCK_ENTRY_MESSAGE,
    PORTAL_STOCK_EXIT_MESSAGE,
    SYNC_TYPE_STOCK_ENTRY_STR,
    SYNC_TYPE_STOCK_EXIT_RTS,
    apply_article_mapping_to_job,
    article_resolution_for_job_row,
    article_context_for_ingreso,
    article_context_for_model,
    process_bejerman_jobs,
    search_bejerman_articles_for_job,
    search_bejerman_articles_for_context,
    reopen_jobs_for_article_mapping,
    upsert_article_mapping,
    validate_bejerman_article_choice,
)
from ..bejerman_companies import DEFAULT_INGRESS_COMPANY_KEY, company_for_key, list_ingress_companies
from ..bejerman_pdf_requests import pdf_queue_status, requeue_pdf_request
from ..bejerman_pdf_settings import (
    BejermanPdfOutputSettingsError,
    serialize_pdf_output_settings,
    update_pdf_output_settings,
)
from ..delivery_orders import DeliveryOrderError
from ..permissions import require_any_permission, require_permission, user_has_permission
from .helpers import os_label, q


logger = logging.getLogger(__name__)


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)


def _as_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _wants_bejerman_history(params):
    return str(params.get("history") or "").strip() in {"1", "true", "True"}


def _build_job_filters(params, *, include_status=True):
    where = []
    sql_params = []

    if str(params.get("include_legacy") or "").strip() not in {"1", "true", "True"}:
        placeholders = ",".join(["%s"] * len(LEGACY_BLOCKED_SYNC_TYPES))
        where.append(f"j.sync_type NOT IN ({placeholders})")
        sql_params.extend(LEGACY_BLOCKED_SYNC_TYPES)

    if not _wants_bejerman_history(params):
        where.append(
            """
            NOT (
              (j.status = 'succeeded' AND j.updated_at < NOW() - INTERVAL '7 days')
              OR (
                j.status IN ('failed', 'blocked')
                AND (
                  t.estado IN ('vendido_entregado', 'baja')
                  OR t.remito_salida IS NOT NULL
                )
              )
            )
            """
        )

    if include_status:
        status = (params.get("status") or "").strip()
        if status:
            where.append("j.status = %s")
            sql_params.append(status)

    sync_type = (params.get("sync_type") or params.get("type") or "").strip()
    if sync_type:
        where.append("j.sync_type = %s")
        sql_params.append(sync_type)

    company_param = (params.get("company_key") or params.get("company") or "").strip()
    company = company_for_key(company_param, default=None) if company_param else None
    if company:
        where.append("UPPER(COALESCE(NULLIF(j.company_key, ''), NULLIF(t.empresa_bejerman, ''), %s)) = %s")
        sql_params.extend([DEFAULT_INGRESS_COMPANY_KEY, company.key])

    os_value = _as_int(params.get("os") or params.get("ingreso_id"))
    if os_value:
        where.append("j.ingreso_id = %s")
        sql_params.append(os_value)

    serial = (params.get("serie") or params.get("serial") or "").strip()
    if serial:
        where.append("j.numero_serie ILIKE %s")
        sql_params.append(f"%{serial}%")

    cliente = (params.get("cliente") or "").strip()
    if cliente:
        where.append("c.razon_social ILIKE %s")
        sql_params.append(f"%{cliente}%")

    article = (params.get("articulo") or params.get("article") or "").strip()
    if article:
        where.append("j.article_code ILIKE %s")
        sql_params.append(f"%{article}%")

    search = (params.get("q") or "").strip()
    if search:
        where.append(
            """
            (
              CAST(j.ingreso_id AS TEXT) ILIKE %s
              OR j.numero_serie ILIKE %s
              OR COALESCE(d.numero_interno, '') ILIKE %s
              OR COALESCE(c.razon_social, '') ILIKE %s
              OR COALESCE(j.article_code, '') ILIKE %s
              OR COALESCE(j.last_error, '') ILIKE %s
            )
            """
        )
        like = f"%{search}%"
        sql_params.extend([like, like, like, like, like, like])

    return where, sql_params


def _json_value(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _table_exists(table_name):
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = %s
                 LIMIT 1
                """,
                [table_name],
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _clean_filter(value):
    return str(value or "").strip()


def _matches_text(row, search):
    if not search:
        return True
    needle = search.lower()
    haystack = " ".join(
        str(value or "")
        for value in (
            row.get("id"),
            row.get("group_id"),
            row.get("ingreso_id"),
            row.get("remito_number"),
            row.get("comprobante_tipo"),
            row.get("comprobante_pto_venta"),
            row.get("comprobante_numero"),
            row.get("customer_code"),
            row.get("customer_name"),
            row.get("cliente"),
            row.get("numero_serie"),
            row.get("numero_interno"),
            row.get("equipment_label"),
            row.get("last_error"),
        )
    ).lower()
    if needle in haystack:
        return True
    return any(
        needle in " ".join(str(value or "") for value in order.values()).lower()
        for order in (row.get("orders") or [])
        if isinstance(order, dict)
    )


REMITO_PROCESS_TERMINAL_ORDER_STATUSES = {"cancelado", "facturado", "entregado_no_facturable"}
REMITO_PROCESS_TERMINAL_INGRESO_STATUSES = {"vendido_entregado", "baja"}
REMITO_PROCESS_SUCCESS_RETENTION_DAYS = 7


def _remito_process_is_archivable(row):
    status = str(row.get("status") or "").lower()
    if status in {"generated", "reconciled"}:
        reference = row.get("generated_at") or row.get("updated_at") or row.get("created_at")
        if not reference:
            return False
        cutoff = timezone.now() - timedelta(days=REMITO_PROCESS_SUCCESS_RETENTION_DAYS)
        return reference < cutoff
    if status in {"failed", "failed_review"}:
        if row.get("source") == "ingreso":
            return str(row.get("ingreso_estado") or "").lower() in REMITO_PROCESS_TERMINAL_INGRESO_STATUSES
        orders = row.get("orders") or []
        return any(
            str(order.get("status") or "").lower() in REMITO_PROCESS_TERMINAL_ORDER_STATUSES
            for order in orders
            if isinstance(order, dict)
        )
    return False


def _filter_remito_processes(rows, params, *, include_status=True):
    source = _clean_filter(params.get("source") or params.get("origen")).lower()
    status = _clean_filter(params.get("status")).lower()
    company_key = _clean_filter(params.get("company_key") or params.get("company")).upper()
    os_value = _as_int(params.get("os") or params.get("ingreso_id"))
    cliente = _clean_filter(params.get("cliente")).lower()
    search = _clean_filter(params.get("q")).lower()
    show_history = _wants_bejerman_history(params)

    filtered = []
    for row in rows:
        if not show_history and _remito_process_is_archivable(row):
            continue
        if include_status and status and str(row.get("status") or "").lower() != status:
            continue
        if source and str(row.get("source") or "").lower() != source:
            continue
        if company_key and str(row.get("company_key") or "").upper() != company_key:
            continue
        if os_value and int(row.get("ingreso_id") or 0) != os_value:
            continue
        if cliente:
            values = [
                row.get("customer_name"),
                row.get("cliente"),
                *(order.get("customerName") for order in (row.get("orders") or []) if isinstance(order, dict)),
            ]
            if cliente not in " ".join(str(value or "") for value in values).lower():
                continue
        if not _matches_text(row, search):
            continue
        filtered.append(row)
    return filtered


def _company_payload(company_key, company_label="", bejerman_company=""):
    company = company_for_key(company_key, default=None) if company_key else None
    key = (company.key if company else company_key) or DEFAULT_INGRESS_COMPANY_KEY
    return {
        "company_key": key,
        "company_label": company_label or (company.label if company else key),
        "bejerman_company": bejerman_company or (company.bejerman_company if company else ""),
    }


def _is_bare_error_code(value):
    """Un token tipo BEJERMAN_CUSTOMER_VAT_MISSING, sin nada legible alrededor."""

    text = _clean_filter(value)
    return bool(text) and " " not in text and text == text.upper() and "_" in text


def _remito_error_message(payload, fallback=""):
    payload = _json_value(payload, {})
    if not isinstance(payload, dict):
        payload = {}
    fallback = _clean_filter(fallback)
    for key in ("error", "bridgeError", "detail", "message"):
        value = _clean_filter(payload.get(key))
        if value and value != "BEJERMAN_BRIDGE_RESPONSE_ERROR":
            # Cuando Bejerman devuelve un código pelado, mostrarlo tal cual no le
            # dice nada a nadie. La traducción ya está guardada al lado.
            if _is_bare_error_code(value):
                explained = _clean_filter(payload.get("errorCategoryMessage"))
                if explained and not _is_bare_error_code(explained):
                    return explained
            return value
    message = _clean_filter(payload.get("message"))
    if message == "BEJERMAN_BRIDGE_RESPONSE_ERROR":
        stage = _clean_filter(payload.get("stage"))
        suffix = f" ({stage})" if stage else ""
        return f"Bejerman devolvió un error de generación sin detalle adicional{suffix}."
    return fallback or message


def _sort_remito_processes(rows):
    status_priority = {"failed_review": 1, "failed": 2, "running": 3, "pending": 4, "generated": 5}

    def timestamp(row):
        value = row.get("updated_at") or row.get("generated_at") or row.get("created_at")
        try:
            return value.timestamp()
        except Exception:
            return 0

    return sorted(
        rows,
        key=lambda row: (
            status_priority.get(str(row.get("status") or ""), 9),
            -timestamp(row),
            str(row.get("id") or ""),
        ),
    )


def _ingreso_remito_processes():
    if not _table_exists("bejerman_ingreso_remitos"):
        return []
    rows = q(
        """
        SELECT r.id,
               r.ingreso_id,
               i.estado AS ingreso_estado,
               COALESCE(NULLIF(r.company_key, ''), NULLIF(i.empresa_bejerman, ''), %s) AS company_key,
               COALESCE(r.company_label, '') AS company_label,
               COALESCE(r.bejerman_company, '') AS bejerman_company,
               r.status,
               r.pdf_status,
               r.document_mode,
               r.manual_remito_number,
               r.remito_number,
               r.comprobante_tipo,
               r.comprobante_letra,
               r.comprobante_pto_venta,
               r.comprobante_numero,
               r.customer_code,
               COALESCE(NULLIF(r.customer_name, ''), c.razon_social, '') AS customer_name,
               r.issue_date,
               r.attempts,
               r.last_error,
               r.request_payload,
               r.response_payload,
               r.created_at,
               r.updated_at,
               r.generated_at,
               d.id AS device_id,
               COALESCE(d.numero_serie, '') AS numero_serie,
               COALESCE(d.numero_interno, '') AS numero_interno,
               COALESCE(c.razon_social, '') AS cliente,
               COALESCE(b.nombre, '') AS marca,
               COALESCE(m.nombre, '') AS modelo,
               COALESCE(NULLIF(i.equipo_variante, ''), NULLIF(d.variante, ''), NULLIF(m.variante, ''), '') AS variante
          FROM bejerman_ingreso_remitos r
          JOIN ingresos i ON i.id = r.ingreso_id
          JOIN devices d ON d.id = i.device_id
          LEFT JOIN customers c ON c.id = d.customer_id
          LEFT JOIN marcas b ON b.id = d.marca_id
          LEFT JOIN models m ON m.id = d.model_id
        """,
        [DEFAULT_INGRESS_COMPANY_KEY],
    ) or []
    out = []
    for row in rows:
        company = _company_payload(row.get("company_key"), row.get("company_label"), row.get("bejerman_company"))
        equipment_label = " ".join(
            value for value in (row.get("marca"), row.get("modelo"), row.get("variante")) if value
        )
        document_mode = row.get("document_mode") or "emit"
        generated = row.get("status") == "generated"
        response_payload = _json_value(row.get("response_payload"), {})
        attempted_at = row.get("updated_at") or row.get("generated_at") or row.get("created_at")
        out.append(
            {
                **row,
                **company,
                "source": "ingreso",
                "source_label": "Ingreso de servicio",
                "process_id": f"ingreso:{row.get('id')}",
                "display_id": f"OS-{row.get('ingreso_id')}",
                "equipment_label": equipment_label,
                "title": row.get("remito_number") or row.get("manual_remito_number") or "Remito de ingreso",
                "operation_label": "Registrar remito" if document_mode == "register" else "Emitir remito",
                "retryable": row.get("status") == "failed",
                "last_error": _remito_error_message(response_payload, row.get("last_error")),
                "attempted_at": attempted_at,
                "pdf_url": f"/api/ingresos/{row.get('ingreso_id')}/ris/pdf/" if generated and document_mode == "emit" else None,
                "print_url": f"/api/ingresos/{row.get('ingreso_id')}/ris/print/" if generated and document_mode == "emit" else None,
            }
        )
    return out


def _delivery_review_diagnostic(row, response_summary):
    error = _remito_error_message(response_summary, row.get("last_error"))
    code = _clean_filter(response_summary.get("diagnosticCode"))
    normalized_error = error.lower()
    if not code and ("máximo para ajustar diferencias" in normalized_error or "maximo para ajustar diferencias" in normalized_error):
        code = "BEJERMAN_REMITO_AMOUNT_MISMATCH"
    if code == "BEJERMAN_REMITO_AMOUNT_MISMATCH":
        return {
            "code": code,
            "explanation": "Bejerman rechazó el RT porque la suma de los renglones no coincidió con el encabezado.",
            "technicalDetail": error,
            "solutions": [
                "Verificar que el comprobante no se haya importado.",
                "Abrir la orden, revisar sus artículos y guardar la corrección.",
                "Reemitir con el cálculo fiscal corregido y confirmación humana.",
            ],
        }
    candidates = review_candidates_for_group(row)
    candidate_labels = [
        " ".join(
            part
            for part in (
                _clean_filter(candidate.get("comprobanteTipo")),
                _clean_filter(candidate.get("comprobanteLetra")),
                "-".join(
                    part
                    for part in (
                        _clean_filter(candidate.get("comprobantePtoVenta")),
                        _clean_filter(candidate.get("comprobanteNumero")),
                    )
                    if part
                ),
                f"({_clean_filter(candidate.get('fechaEmision'))})" if candidate.get("fechaEmision") else "",
            )
            if part
        )
        for candidate in candidates
    ]
    if code == "BEJERMAN_REMITO_LOOKUP_AMBIGUOUS":
        return {
            "code": code,
            "explanation": (
                "Bejerman tiene más de un comprobante que podría ser el de esta entrega, casi siempre "
                "porque un intento anterior llegó a emitir aunque devolviera error. NEXORA no adopta "
                "ninguno por su cuenta: si eligiera mal, duplicaría el stock y la facturación."
            ),
            "technicalDetail": error,
            "candidates": candidates,
            "solutions": [
                "Abrir Bejerman y mirar los comprobantes listados acá: "
                + (", ".join(candidate_labels) if candidate_labels else "(la lista quedó vacía, use la verificación)"),
                "Anular en Bejerman los que estén de más; queda uno solo válido para esta entrega.",
                "Volver acá y usar «Resolver revisión» → «El remito existe» con ese número.",
                "Si ninguno corresponde a esta entrega, usar «No se emitió» para habilitar la corrección y reemitir.",
            ],
        }
    if code == "BEJERMAN_REMITO_LOOKUP_UNAVAILABLE":
        return {
            "code": code,
            "explanation": (
                "NEXORA no pudo consultar Bejerman, así que no sabe si el remito llegó a emitirse. "
                "Reemitir a ciegas puede duplicarlo."
            ),
            "technicalDetail": error,
            "candidates": candidates,
            "solutions": [
                "Reintentar la verificación: si Bejerman ya responde, el proceso se destraba solo.",
                "Si sigue sin responder, buscar el comprobante en Bejerman por cliente y fecha.",
                "Si está emitido, usar «Resolver revisión» → «El remito existe» con ese número.",
                "Si no está, usar «No se emitió» para habilitar la corrección y reemitir.",
            ],
        }
    explanations = {
        "BEJERMAN_REMITO_PAYLOAD_UNCHANGED": "El comprobante actual no presenta cambios frente al intento rechazado.",
    }
    return {
        "code": code or "BEJERMAN_REMITO_REQUIRES_REVIEW",
        "explanation": explanations.get(code) or (
            "La emisión quedó sin confirmación de Bejerman. Antes de reemitir hay que asegurarse de que "
            "el remito no haya salido igual, para no duplicarlo."
        ),
        "technicalDetail": error,
        "candidates": candidates,
        "solutions": [
            "Ejecutar la verificación contra Bejerman: resuelve sola la mayoría de los casos.",
            "Si la verificación no puede decidir, resolver a mano indicando si el remito existe o no.",
        ],
    }


def _delivery_remito_processes(action_permissions=None):
    if not _table_exists("bejerman_remito_groups"):
        return []
    rows = q(
        """
        SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
               comprobante_numero, remito_number, customer_code, customer_name,
               seller_code, payment_term_code, operation_code, deposit_code,
               status, order_ids, response_summary, attempt_count, last_attempt_at,
               next_retry_at, last_error, created_at, generated_at
          FROM bejerman_remito_groups
        """,
    ) or []
    out = []
    for row in rows:
        order_ids = _json_value(row.get("order_ids"), [])
        if not isinstance(order_ids, list):
            order_ids = []
        response_summary = _json_value(row.get("response_summary"), {})
        if not isinstance(response_summary, dict):
            response_summary = {}
        order_rows = []
        if order_ids:
            order_rows = q(
                """
                SELECT id, order_number, delivery_type, source_reference, customer_name,
                       equipment_model, equipment_serial, raw_pedido, ingreso_id, status
                  FROM delivery_orders
                 WHERE id = ANY(%s)
                """,
                [order_ids],
            ) or []
        orders = [
            {
                "id": order.get("id"),
                "orderNumber": order.get("order_number") or "",
                "deliveryType": order.get("delivery_type") or "",
                "sourceReference": order.get("source_reference") or "",
                "customerName": order.get("customer_name") or "",
                "equipmentModel": order.get("equipment_model") or "",
                "equipmentSerial": order.get("equipment_serial") or "",
                "rawPedido": order.get("raw_pedido") or "",
                "ingresoId": order.get("ingreso_id"),
                "status": order.get("status") or "",
            }
            for order in order_rows
        ]
        first_order = orders[0] if orders else {}
        company = _company_payload(row.get("company_key"))
        last_error = _remito_error_message(response_summary, row.get("last_error"))
        generated = row.get("status") in {"generated", "reconciled"}
        status = row.get("status") or ""
        review_state = _clean_filter(response_summary.get("reviewState"))
        if status == "failed_review" and not review_state:
            review_state = "requires_verification"
        failure_certainty = _clean_filter(response_summary.get("failureCertainty")) or "unknown"
        superseded = review_state == "superseded"
        permissions_for_actions = action_permissions or {}
        can_manage = bool(permissions_for_actions.get("manage", True))
        can_edit = bool(permissions_for_actions.get("edit", True))
        can_generate = bool(permissions_for_actions.get("generate", True))
        blocked_reason = _clean_filter(response_summary.get("blockedReason"))
        if review_state == "blocked" and not blocked_reason:
            blocked_reason = "La verificación no produjo un resultado seguro."
        closed_order = next(
            (order for order in orders if order.get("status") in {"cancelado", "facturado"}),
            None,
        )
        closed_order_reason = ""
        if closed_order:
            order_label = closed_order.get("orderNumber") or closed_order.get("id") or "La orden"
            status_label = "cancelada" if closed_order.get("status") == "cancelado" else "facturada"
            closed_order_reason = (
                f"La orden {order_label} está {status_label}. "
                "Este proceso queda sólo como registro y no puede verificarse ni reemitirse."
            )
            blocked_reason = closed_order_reason
        actions = {
            "verify": {
                "visible": status == "failed_review" and not superseded,
                "allowed": status == "failed_review" and not superseded and can_manage and not closed_order,
                "blockedReason": (
                    closed_order_reason
                    or ("No tiene permiso para verificar comprobantes en Bejerman." if not can_manage else "")
                ),
            },
            "edit": {
                "visible": status == "failed_review" and not superseded,
                "allowed": review_state in {"ready_for_correction", "ready_to_reissue"} and can_edit and not closed_order,
                "blockedReason": (
                    closed_order_reason
                    or (
                        # Decirle "verifique primero" a alguien cuya verificación ya
                        # falló es mandarlo contra la misma pared: cuando el proceso
                        # está trabado, el camino es resolverlo a mano.
                        "La verificación no pudo decidir sola. Use «Resolver revisión» indicando si el remito existe en Bejerman."
                        if review_state == "blocked"
                        else "Primero debe verificarse que el comprobante no se importó."
                        if review_state not in {"ready_for_correction", "ready_to_reissue"}
                        else "No tiene permiso para modificar los artículos de la orden."
                        if not can_edit
                        else ""
                    )
                ),
            },
            # La verificación automática no siempre puede decidir (Bejerman devuelve
            # varios candidatos o no responde). Sin esta acción esos procesos quedaban
            # sin ningún camino disponible: el operador veía "necesita revisión", los
            # otros dos botones deshabilitados, y el de verificar fallando siempre.
            "resolve": {
                "visible": status == "failed_review" and not superseded,
                "allowed": status == "failed_review" and not superseded and can_manage and not closed_order,
                "blockedReason": (
                    closed_order_reason
                    or ("No tiene permiso para resolver revisiones de Bejerman." if not can_manage else "")
                ),
                "candidates": review_candidates_for_group(row),
            },
            "reissue": {
                "visible": status == "failed_review" and not superseded,
                "allowed": review_state == "ready_to_reissue" and can_manage and can_generate and not closed_order,
                "blockedReason": (
                    closed_order_reason
                    or (
                        "La verificación no pudo decidir sola. Use «Resolver revisión» indicando si el remito existe en Bejerman."
                        if review_state == "blocked"
                        else "Debe abrir, revisar y guardar la orden antes de reemitir."
                        if review_state != "ready_to_reissue"
                        else "No tiene todos los permisos necesarios para reemitir el RT."
                        if not (can_manage and can_generate)
                        else ""
                    )
                ),
            },
        }
        attempted_at = row.get("last_attempt_at") or row.get("generated_at") or row.get("created_at")
        out.append(
            {
                **row,
                **company,
                "source": "orden_entrega",
                "source_label": "Orden de entrega",
                "process_id": f"orden_entrega:{row.get('id')}",
                "group_id": row.get("id"),
                "ingreso_id": first_order.get("ingresoId"),
                "document_mode": "emit",
                "pdf_status": "pending" if generated else "",
                "attempts": int(row.get("attempt_count") or 0),
                "last_error": last_error,
                "request_payload": {"orderIds": order_ids},
                "response_payload": response_summary,
                "attempted_at": attempted_at,
                "updated_at": attempted_at,
                "orders": orders,
                "order_count": len(order_ids) or len(orders),
                "display_id": ", ".join(order.get("orderNumber") or order.get("id") or "" for order in orders[:3]) or row.get("id"),
                "equipment_label": first_order.get("equipmentModel") or "",
                "numero_serie": first_order.get("equipmentSerial") or "",
                "numero_interno": "",
                "cliente": row.get("customer_name") or "",
                "title": row.get("remito_number") or "Remito de entrega",
                "operation_label": "Emitir remito",
                "retryable": (
                    row.get("status") in {"failed", "failed_review"}
                    and bool(order_ids)
                    and not superseded
                    and not closed_order
                ),
                "retry_mode": "reconcile" if row.get("status") == "failed_review" else "retry",
                "reviewState": review_state,
                "failureCertainty": failure_certainty,
                "hasPayloadChanges": response_summary.get("hasPayloadChanges"),
                "diagnostic": _delivery_review_diagnostic(row, response_summary) if status == "failed_review" else None,
                "actions": actions,
                "blockedReason": blocked_reason,
                "pdf_url": f"/api/ordenes-entrega/remito-bejerman/{row.get('id')}/pdf/" if generated else None,
                "print_url": f"/api/ordenes-entrega/remito-bejerman/{row.get('id')}/print/" if generated else None,
            }
        )
    return out


def _ingreso_conflict_processes(action_permissions=None):
    """Equipos que entraron con su orden anterior todavía abierta.

    No es un proceso de Bejerman fallado, pero es lo mismo desde el lado de quien mira este
    panel: algo quedó a medio resolver y hay que decidirlo. Entra como "Revisión requerida"
    para que aparezca arriba de todo junto con el resto de lo trabado.
    """

    try:
        from ..bejerman_ingreso_conflicts import list_pending_conflicts
    except Exception:
        return []
    try:
        conflicts = list_pending_conflicts()
    except Exception:
        logger.warning("ingreso_open_conflicts_list_failed", exc_info=True)
        return []

    permissions_for_actions = action_permissions or {}
    can_resolve = bool(permissions_for_actions.get("resolve_conflict", True))
    can_force = bool(permissions_for_actions.get("force_close", False))
    out = []
    for row in conflicts:
        company = _company_payload(row.get("empresa_bejerman"))
        equipment_label = " ".join(
            value for value in (row.get("tipo_equipo"), row.get("marca"), row.get("modelo")) if value
        )
        os_actual = os_label(row.get("ingreso_id"))
        os_previa = os_label(row.get("conflicting_ingreso_id"))
        fecha_previa = row.get("conflicting_fecha_ingreso")
        out.append(
            {
                **company,
                "id": row.get("id"),
                "conflict_id": row.get("id"),
                "source": "ingreso_conflicto",
                "source_label": "Conflicto de ingreso",
                "process_id": f"ingreso_conflicto:{row.get('id')}",
                "status": "failed_review",
                "pdf_status": "",
                "document_mode": "emit",
                "ingreso_id": row.get("ingreso_id"),
                "conflicting_ingreso_id": row.get("conflicting_ingreso_id"),
                "conflicting_os": os_previa,
                "conflicting_fecha_ingreso": fecha_previa,
                "conflicting_estado": row.get("conflicting_estado") or "",
                "display_id": f"OS-{row.get('ingreso_id')}",
                "numero_serie": row.get("numero_serie") or "",
                "numero_interno": row.get("numero_interno") or "",
                "tipo_equipo": row.get("tipo_equipo") or "",
                "equipment_label": equipment_label,
                "cliente": row.get("cliente") or "",
                "customer_name": row.get("cliente") or "",
                "remito_number": row.get("remito_number") or "",
                "title": row.get("remito_number") or "Remito de ingreso",
                "operation_label": "Resolver orden anterior",
                "retryable": False,
                "attempts": 0,
                "last_error": (
                    f"El equipo ya tenía la OS {os_previa} abierta al emitirse este remito. "
                    f"El renglón de {os_actual} salió sin número de OS hasta que se decida "
                    "si la orden anterior se cierra o si corresponde unificarlas."
                ),
                "attempted_at": row.get("updated_at") or row.get("created_at"),
                "updated_at": row.get("updated_at") or row.get("created_at"),
                "created_at": row.get("created_at"),
                "request_payload": {},
                "response_payload": {},
                "evidence": row.get("evidence") or {},
                "actions": {
                    "resolve_conflict": {
                        "visible": True,
                        "allowed": bool(can_resolve),
                        "blockedReason": (
                            "" if can_resolve else "No tiene permiso para resolver ingresos con orden anterior abierta."
                        ),
                    },
                    # Cerrar sin ningún comprobante que lo respalde es la decisión, no el
                    # trámite: la toma jefatura y por eso va con su propio permiso.
                    "force_close": {
                        "visible": bool(can_force),
                        "allowed": bool(can_force),
                        "blockedReason": (
                            "" if can_force else "Sólo jefatura puede cerrar la orden sin comprobante de salida."
                        ),
                    },
                },
                "pdf_url": None,
                "print_url": None,
            }
        )
    return out


def _list_remito_processes(params, *, include_status=True, action_permissions=None):
    source = _clean_filter(params.get("source") or params.get("origen")).lower()
    rows = []
    if source in {"", "ingreso"}:
        rows.extend(_ingreso_remito_processes())
    if source in {"", "orden_entrega"}:
        rows.extend(_delivery_remito_processes(action_permissions=action_permissions))
    if source in {"", "ingreso_conflicto"}:
        rows.extend(_ingreso_conflict_processes(action_permissions=action_permissions))
    return _sort_remito_processes(_filter_remito_processes(rows, params, include_status=include_status))


def _ingreso_ids_from_process_payload(payload, fallback_id):
    payload = _json_value(payload, {})
    raw_ids = payload.get("ingresoIds") or payload.get("ingreso_ids") or []
    if not isinstance(raw_ids, (list, tuple)):
        raw_ids = []
    out = []
    for raw in [*raw_ids, fallback_id]:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out


class BejermanJobsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_permission(request, "page.bejerman_sync")
        where, sql_params = _build_job_filters(request.GET, include_status=True)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        rows = q(
            f"""
            SELECT j.id,
                   j.sync_type,
                   j.ingreso_id,
                   j.device_id,
                   j.ingreso_event_id,
                   j.numero_serie,
                   j.source_deposit,
                   j.target_deposit,
                   COALESCE(NULLIF(j.company_key, ''), NULLIF(t.empresa_bejerman, ''), %s) AS company_key,
                   j.article_code,
                   j.status,
                   j.attempts,
                   j.next_attempt_at,
                   j.last_error,
                   j.request_payload,
                   j.response_payload,
                   j.created_at,
                   j.updated_at,
                   d.model_id,
                   COALESCE(d.numero_interno, '') AS numero_interno,
                   COALESCE(c.razon_social, '') AS cliente,
                   COALESCE(b.nombre, '') AS marca,
                   COALESCE(m.nombre, '') AS modelo,
                   COALESCE(NULLIF(t.equipo_variante, ''), NULLIF(d.variante, ''), NULLIF(m.variante, ''), '') AS variante
              FROM bejerman_sync_jobs j
              JOIN ingresos t ON t.id = j.ingreso_id
              JOIN devices d ON d.id = j.device_id
              LEFT JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              {where_sql}
             ORDER BY
               CASE j.status
                 WHEN 'blocked' THEN 1
                 WHEN 'failed' THEN 2
                 WHEN 'pending' THEN 3
                 WHEN 'running' THEN 4
                 ELSE 5
               END,
               j.updated_at DESC,
               j.id DESC
             LIMIT 250
            """,
            [DEFAULT_INGRESS_COMPANY_KEY, *sql_params],
        ) or []
        companies_by_key = {company.key: company for company in list_ingress_companies()}
        for row in rows:
            company = companies_by_key.get(row.get("company_key") or DEFAULT_INGRESS_COMPANY_KEY)
            row["company_label"] = company.label if company else row.get("company_key") or DEFAULT_INGRESS_COMPANY_KEY
            resolution = article_resolution_for_job_row(row)
            row["article_resolution"] = resolution
            row["article_mapping"] = resolution.get("mapping")

        counter_where, counter_params = _build_job_filters(request.GET, include_status=False)
        counter_sql = "WHERE " + " AND ".join(counter_where) if counter_where else ""
        counters = q(
            f"""
            SELECT j.status, COUNT(*) AS count
              FROM bejerman_sync_jobs j
              JOIN ingresos t ON t.id = j.ingreso_id
              JOIN devices d ON d.id = j.device_id
              LEFT JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              {counter_sql}
             GROUP BY j.status
            """,
            counter_params,
        ) or []
        return Response(
            {
                "items": rows,
                "counters": {row["status"]: int(row["count"]) for row in counters},
            }
        )


class BejermanIngressCompaniesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_any_permission(request, ["action.ingreso.create", "page.new_ingreso"])
        return Response(
            {
                "items": [company.as_public_dict() for company in list_ingress_companies()],
                "defaultKey": DEFAULT_INGRESS_COMPANY_KEY,
            }
        )


class BejermanPdfOutputSettingsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_permission(request, "page.bejerman_sync")
        return Response(serialize_pdf_output_settings())

    def put(self, request):
        require_permission(request, "action.bejerman_sync.manage")
        try:
            payload = update_pdf_output_settings(
                (request.data or {}).get("items"),
                actor_user_id=_actor_id(request),
            )
        except BejermanPdfOutputSettingsError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(payload)


class BejermanPdfQueueStatusView(APIView):
    """Diagnóstico de la cola de PDF sin tener que entrar al servidor."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_permission(request, "page.bejerman_sync")
        try:
            limit = int(request.query_params.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        stuck_only = str(request.query_params.get("stuckOnly") or "").strip().lower() in {"1", "true", "si", "sí"}
        return Response(pdf_queue_status(limit=max(1, min(limit, 200)), stuck_only=stuck_only))


class BejermanPdfQueueRetryView(APIView):
    """Abre un ciclo de generación nuevo para un PDF que no llegó."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, document_key: str):
        require_permission(request, "action.bejerman_sync.manage")
        force = bool((request.data or {}).get("force")) if isinstance(request.data, dict) else False
        try:
            result = requeue_pdf_request(
                document_key,
                reason="manual_retry",
                actor_user_id=_actor_id(request),
                force=force,
            )
        except (BejermanConfigError, BejermanPdfOutputSettingsError) as exc:
            return Response({"detail": str(exc)}, status=503)
        outcome = result.get("outcome")
        if outcome == "missing":
            return Response({"detail": "No hay una solicitud de PDF para ese comprobante."}, status=404)
        if outcome == "exhausted":
            return Response(
                {
                    "detail": (
                        "Ya se agotaron los reintentos automáticos de generación para este comprobante. "
                        "Se puede forzar uno nuevo, pero conviene revisar antes por qué Bejerman no publica el archivo."
                    ),
                    "code": "BEJERMAN_PDF_REGENERATION_EXHAUSTED",
                    **result,
                },
                status=409,
            )
        detail = (
            "El PDF ya estaba publicado en la carpeta de Bejerman y quedó disponible."
            if outcome == "ready"
            else "Se pidió una generación nueva del PDF a Bejerman."
        )
        return Response({"detail": detail, **result})


class BejermanJobRetryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, job_id: int):
        require_permission(request, "action.bejerman_sync.manage")
        with connection.cursor() as cur:
            cur.execute("SELECT id, status, sync_type FROM bejerman_sync_jobs WHERE id=%s", [job_id])
            row = cur.fetchone()
            if not row:
                return Response({"detail": "Operación Bejerman no encontrada"}, status=404)
            status = (row[1] or "").strip()
            sync_type = (row[2] or "").strip()
            if sync_type in LEGACY_BLOCKED_SYNC_TYPES:
                detail = NEXORA_STOCK_ENTRY_MESSAGE if sync_type == SYNC_TYPE_STOCK_ENTRY_STR else PORTAL_STOCK_EXIT_MESSAGE
                return Response({"detail": detail}, status=409)
            # 'pending' entra a proposito: si el worker de sincronizacion no esta corriendo, el job
            # queda encolado para siempre y este es el unico modo de forzarlo desde el panel.
            if status not in ("failed", "blocked", "pending"):
                return Response(
                    {"detail": "Solo se pueden reintentar operaciones pendientes, fallidas o bloqueadas"},
                    status=409,
                )
            cur.execute(
                """
                UPDATE bejerman_sync_jobs
                   SET status = 'pending',
                       attempts = 0,
                       last_error = NULL,
                       next_attempt_at = CURRENT_TIMESTAMP,
                       actor_user_id = %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                RETURNING id, status, attempts
                """,
                [_actor_id(request), job_id],
            )
            cols = [col[0] for col in cur.description]
            updated = dict(zip(cols, cur.fetchone()))

        # Se corre en el request en vez de dejarlo para el worker: el operador necesita saber ahora
        # si el movimiento entro o no. Si falla, process_bejerman_jobs lo vuelve a dejar encolado.
        stats = process_bejerman_jobs(job_id=job_id, actor_user_id=_actor_id(request))
        if stats.get("disabled"):
            return Response(
                {"detail": "La sincronización con Bejerman está desactivada (BEJERMAN_SYNC_ENABLED)."},
                status=409,
            )
        with connection.cursor() as cur:
            cur.execute("SELECT id, status, attempts, last_error FROM bejerman_sync_jobs WHERE id=%s", [job_id])
            cols = [col[0] for col in cur.description]
            updated = dict(zip(cols, cur.fetchone()))
        return Response(updated)


class BejermanRemitoProcessesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_permission(request, "page.bejerman_sync")
        action_permissions = {
            "manage": user_has_permission(request, "action.bejerman_sync.manage"),
            "edit": user_has_permission(request, "action.delivery_order.assign_articles"),
            "generate": user_has_permission(request, "action.delivery_order.generate_bejerman_remito"),
            "resolve_conflict": user_has_permission(request, "action.ingreso.resolve_open_conflict"),
            "force_close": user_has_permission(request, "action.ingreso.force_historical"),
        }
        rows = _list_remito_processes(request.GET, include_status=True, action_permissions=action_permissions)[:250]
        counter_rows = _list_remito_processes(request.GET, include_status=False, action_permissions=action_permissions)
        counters = {}
        for row in counter_rows:
            status = row.get("status") or "pending"
            counters[status] = counters.get(status, 0) + 1
        return Response({"items": rows, "counters": counters})


class BejermanRemitoProcessRetryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, source: str, process_id: str):
        require_permission(request, "action.bejerman_sync.manage")
        source = _clean_filter(source).lower()
        actor_id = _actor_id(request)
        if source == "ingreso":
            row = q(
                """
                SELECT id, ingreso_id, status, document_mode, manual_remito_number,
                       remito_number, request_payload
                  FROM bejerman_ingreso_remitos
                 WHERE id = %s
                """,
                [process_id],
                one=True,
            )
            if not row:
                return Response({"detail": "Proceso de remito no encontrado"}, status=404)
            if row.get("status") not in {"failed", "failed_review"}:
                return Response({"detail": "Solo se pueden reintentar remitos fallidos o pendientes de revisión"}, status=409)
            try:
                if (row.get("document_mode") or "emit") == "register":
                    manual = row.get("manual_remito_number") or row.get("remito_number")
                    if not manual:
                        return Response({"detail": "El registro fallido no tiene número de remito manual"}, status=409)
                    ingreso_ids = _ingreso_ids_from_process_payload(row.get("request_payload"), row.get("ingreso_id"))
                    result = register_ris_batch(ingreso_ids, manual, user_id=actor_id)
                else:
                    result = emit_or_get_ris(int(row["ingreso_id"]), user_id=actor_id)
            except BejermanRisPreflightError as exc:
                return Response({"detail": str(exc), **exc.payload}, status=409)
            except BejermanRisBusyError as exc:
                return Response({"detail": str(exc)}, status=409)
            except BejermanRisError as exc:
                return Response({"detail": str(exc)}, status=502)
            return Response({"ok": True, "source": source, "result": result})

        if source == "orden_entrega":
            row = q(
                """
                SELECT id, status, order_ids, response_summary
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                [process_id],
                one=True,
            )
            if not row:
                return Response({"detail": "Proceso de remito no encontrado"}, status=404)
            order_ids = _json_value(row.get("order_ids"), [])
            if not isinstance(order_ids, list) or not order_ids:
                return Response({"detail": "El proceso fallido no conserva órdenes para reintentar"}, status=409)
            try:
                retry_payload = dict(request.data or {})
                action = _clean_filter(retry_payload.get("action")).lower()
                if row.get("status") == "failed_review":
                    if action == "verify":
                        result = verify_bejerman_remito_review(process_id, actor_id)
                    elif action == "reissue_corrected":
                        require_permission(request, "action.delivery_order.generate_bejerman_remito")
                        if retry_payload.get("confirm") is not True:
                            return Response(
                                {"detail": "Debe confirmar expresamente la reemisión corregida.", "code": "BEJERMAN_REMITO_REISSUE_CONFIRMATION_REQUIRED"},
                                status=400,
                            )
                        result = reissue_corrected_bejerman_remito_review(process_id, actor_id, retry_payload)
                    elif action == "resolve":
                        # La salida manual del callejón: la usa el operador que ya
                        # miró Bejerman y sabe algo que NEXORA no puede averiguar.
                        result = resolve_bejerman_remito_review(process_id, actor_id, retry_payload)
                    else:
                        return Response(
                            {"detail": "Indique action=verify, action=resolve o action=reissue_corrected para un proceso en revisión."},
                            status=400,
                        )
                elif row.get("status") == "failed":
                    if action not in {"", "retry"}:
                        return Response({"detail": "Acción inválida para un proceso fallido."}, status=400)
                    for key in ("forceReviewedReissue", "forcePrintSplitReissue", "retryConfirmedNotImported", "action", "confirm"):
                        retry_payload.pop(key, None)
                    result = generate_bejerman_remito(order_ids, actor_id, retry_payload)
                else:
                    return Response({"detail": "Solo se pueden operar remitos fallidos o pendientes de revisión."}, status=409)
            except DeliveryOrderError as exc:
                return Response({"detail": str(exc), "code": exc.code}, status=getattr(exc, "status_code", 400) or 400)
            return Response({"ok": True, "source": source, "result": result})

        return Response({"detail": "Origen de remito inválido"}, status=400)


class BejermanArticleMappingsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_any_permission(request, ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"])
        model_id = _as_int(request.GET.get("model_id"))
        if not model_id:
            return Response({"detail": "Modelo requerido"}, status=400)
        rows = q(
            """
            SELECT id, model_id, variante, variante_norm, article_code, article_description,
                   match_source, company_key, confirmed_at, updated_at
              FROM bejerman_article_mappings
             WHERE model_id = %s
             ORDER BY variante_norm, company_key, updated_at DESC
            """,
            [model_id],
        ) or []
        return Response({"items": rows})

    def post(self, request):
        require_any_permission(request, ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"])
        data = request.data or {}
        article_code = (data.get("article_code") or data.get("codigo") or "").strip()
        article_description = (data.get("article_description") or data.get("descripcion") or "").strip()
        if not article_code:
            return Response({"detail": "Código de artículo Bejerman requerido"}, status=400)

        model_id = _as_int(data.get("model_id"))
        variante = (data.get("variante") or "").strip()
        job_id = _as_int(data.get("job_id"))
        requested_company_key = (data.get("company_key") or data.get("companyKey") or "").strip()
        context = None
        company_key = requested_company_key or DEFAULT_INGRESS_COMPANY_KEY
        if job_id:
            row = q(
                "SELECT ingreso_id, COALESCE(NULLIF(company_key, ''), %s) AS company_key FROM bejerman_sync_jobs WHERE id=%s",
                [DEFAULT_INGRESS_COMPANY_KEY, job_id],
                one=True,
            )
            if not row:
                return Response({"detail": "Operación Bejerman no encontrada"}, status=404)
            company_key = requested_company_key or row.get("company_key") or DEFAULT_INGRESS_COMPANY_KEY
            context = article_context_for_ingreso(int(row["ingreso_id"]))
            model_id = int(context["model_id"])
            variante = context.get("variante") or ""

        if not model_id:
            return Response({"detail": "Modelo requerido para mapear el artículo"}, status=400)
        if context is None:
            try:
                context = article_context_for_model(int(model_id), variante)
            except BejermanBlockedError as exc:
                return Response({"detail": str(exc)}, status=404)

        user_id = _actor_id(request)
        try:
            candidate = validate_bejerman_article_choice(
                article_code,
                client=BejermanSDKClient(company_key=company_key if (job_id or requested_company_key) else None, actor_user_id=user_id),
                context=context or {},
            )
        except BejermanBlockedError as exc:
            return Response({"detail": str(exc)}, status=400)
        except (BejermanConfigError, BejermanTransientError) as exc:
            return Response({"detail": str(exc)}, status=503)

        mapping = upsert_article_mapping(
            model_id=model_id,
            variante=variante,
            article_code=article_code,
            article_description=article_description or candidate.get("article_description") or "",
            match_source="manual",
            source_payload={"source": "manual_resolution", "candidate": candidate},
            confirmed_by=user_id,
            company_key=company_key,
        )
        reopened = reopen_jobs_for_article_mapping(model_id, variante, article_code, company_key)
        try:
            updated_job = apply_article_mapping_to_job(job_id, article_code) if job_id else False
        except BejermanBlockedError as exc:
            return Response({"detail": str(exc)}, status=409)
        return Response(
            {
                "mapping": mapping,
                "candidate": candidate,
                "reopened_jobs": reopened,
                "updated_job": updated_job,
            }
        )


class BejermanArticlesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_any_permission(request, ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"])
        job_id = _as_int(request.GET.get("job_id"))
        model_id = _as_int(request.GET.get("model_id") or request.GET.get("modelId"))
        variante = (request.GET.get("variante") or request.GET.get("variant") or "").strip()
        if not job_id and not model_id:
            return Response({"detail": "Job Bejerman o modelo requerido"}, status=400)
        query = (request.GET.get("q") or request.GET.get("query") or "").strip()
        limit = _as_int(request.GET.get("limit")) or 20
        try:
            if job_id:
                job = q(
                    "SELECT COALESCE(NULLIF(company_key, ''), %s) AS company_key FROM bejerman_sync_jobs WHERE id=%s",
                    [DEFAULT_INGRESS_COMPANY_KEY, job_id],
                    one=True,
                )
                if not job:
                    raise BejermanBlockedError("Operación Bejerman no encontrada")
                client = BejermanSDKClient(
                    company_key=job.get("company_key") or DEFAULT_INGRESS_COMPANY_KEY,
                    actor_user_id=_actor_id(request),
                )
                payload = search_bejerman_articles_for_job(
                    job_id=job_id,
                    query=query,
                    client=client,
                    limit=min(max(limit, 1), 50),
                )
            else:
                company_key = (request.GET.get("company_key") or request.GET.get("companyKey") or "").strip()
                client = BejermanSDKClient(company_key=company_key or None, actor_user_id=_actor_id(request))
                context = article_context_for_model(int(model_id), variante)
                payload = search_bejerman_articles_for_context(
                    context=context,
                    query=query,
                    client=client,
                    limit=min(max(limit, 1), 50),
                    company_key=company_key or None,
                )
        except BejermanBlockedError as exc:
            return Response({"detail": str(exc)}, status=404)
        except (BejermanConfigError, BejermanTransientError) as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response(payload)


__all__ = [
    "BejermanIngressCompaniesView",
    "BejermanPdfOutputSettingsView",
    "BejermanJobsView",
    "BejermanJobRetryView",
    "BejermanRemitoProcessesView",
    "BejermanRemitoProcessRetryView",
    "BejermanArticleMappingsView",
    "BejermanArticlesView",
]
