import json

from django.db import connection
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_delivery import generate_bejerman_remito
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
    search_bejerman_articles_for_job,
    search_bejerman_articles_for_context,
    reopen_jobs_for_article_mapping,
    upsert_article_mapping,
    validate_bejerman_article_choice,
)
from ..bejerman_companies import DEFAULT_INGRESS_COMPANY_KEY, company_for_key, list_ingress_companies
from ..bejerman_pdf_settings import (
    BejermanPdfOutputSettingsError,
    serialize_pdf_output_settings,
    update_pdf_output_settings,
)
from ..delivery_orders import DeliveryOrderError
from ..permissions import require_any_permission, require_permission
from .helpers import q


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)


def _as_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _build_job_filters(params, *, include_status=True):
    where = []
    sql_params = []

    if str(params.get("include_legacy") or "").strip() not in {"1", "true", "True"}:
        placeholders = ",".join(["%s"] * len(LEGACY_BLOCKED_SYNC_TYPES))
        where.append(f"j.sync_type NOT IN ({placeholders})")
        sql_params.extend(LEGACY_BLOCKED_SYNC_TYPES)

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


def _filter_remito_processes(rows, params, *, include_status=True):
    source = _clean_filter(params.get("source") or params.get("origen")).lower()
    status = _clean_filter(params.get("status")).lower()
    company_key = _clean_filter(params.get("company_key") or params.get("company")).upper()
    os_value = _as_int(params.get("os") or params.get("ingreso_id"))
    cliente = _clean_filter(params.get("cliente")).lower()
    search = _clean_filter(params.get("q")).lower()

    filtered = []
    for row in rows:
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


def _remito_error_message(payload, fallback=""):
    payload = _json_value(payload, {})
    if not isinstance(payload, dict):
        payload = {}
    fallback = _clean_filter(fallback)
    for key in ("error", "bridgeError", "detail", "message"):
        value = _clean_filter(payload.get(key))
        if value and value != "BEJERMAN_BRIDGE_RESPONSE_ERROR":
            return value
    message = _clean_filter(payload.get("message"))
    if message == "BEJERMAN_BRIDGE_RESPONSE_ERROR":
        stage = _clean_filter(payload.get("stage"))
        suffix = f" ({stage})" if stage else ""
        return f"Bejerman devolvió un error de generación sin detalle adicional{suffix}."
    return fallback or message


def _sort_remito_processes(rows):
    status_priority = {"failed": 1, "running": 2, "pending": 3, "generated": 4}

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


def _delivery_remito_processes():
    if not _table_exists("bejerman_remito_groups"):
        return []
    rows = q(
        """
        SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
               comprobante_numero, remito_number, customer_code, customer_name,
               seller_code, payment_term_code, operation_code, deposit_code,
               status, order_ids, response_summary, created_at, generated_at
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
                       equipment_model, equipment_serial, raw_pedido, ingreso_id
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
            }
            for order in order_rows
        ]
        first_order = orders[0] if orders else {}
        company = _company_payload(row.get("company_key"))
        last_error = _remito_error_message(response_summary)
        generated = row.get("status") == "generated"
        attempted_at = row.get("generated_at") or row.get("created_at")
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
                "attempts": 1,
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
                "retryable": row.get("status") == "failed" and bool(order_ids),
                "pdf_url": f"/api/ordenes-entrega/remito-bejerman/{row.get('id')}/pdf/" if generated else None,
                "print_url": f"/api/ordenes-entrega/remito-bejerman/{row.get('id')}/print/" if generated else None,
            }
        )
    return out


def _list_remito_processes(params, *, include_status=True):
    source = _clean_filter(params.get("source") or params.get("origen")).lower()
    rows = []
    if source in {"", "ingreso"}:
        rows.extend(_ingreso_remito_processes())
    if source in {"", "orden_entrega"}:
        rows.extend(_delivery_remito_processes())
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
            if status not in ("failed", "blocked"):
                return Response({"detail": "Solo se pueden reintentar operaciones fallidas o bloqueadas"}, status=409)
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
        return Response(updated)


class BejermanRemitoProcessesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_permission(request, "page.bejerman_sync")
        rows = _list_remito_processes(request.GET, include_status=True)[:250]
        counter_rows = _list_remito_processes(request.GET, include_status=False)
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
            if row.get("status") != "failed":
                return Response({"detail": "Solo se pueden reintentar remitos fallidos"}, status=409)
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
                SELECT id, status, order_ids
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                [process_id],
                one=True,
            )
            if not row:
                return Response({"detail": "Proceso de remito no encontrado"}, status=404)
            if row.get("status") != "failed":
                return Response({"detail": "Solo se pueden reintentar remitos fallidos"}, status=409)
            order_ids = _json_value(row.get("order_ids"), [])
            if not isinstance(order_ids, list) or not order_ids:
                return Response({"detail": "El proceso fallido no conserva órdenes para reintentar"}, status=409)
            try:
                result = generate_bejerman_remito(order_ids, actor_id, request.data or {})
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
                   match_source, confirmed_at, updated_at
              FROM bejerman_article_mappings
             WHERE model_id = %s
             ORDER BY variante_norm, updated_at DESC
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
        context = None
        company_key = DEFAULT_INGRESS_COMPANY_KEY
        if job_id:
            row = q(
                "SELECT ingreso_id, COALESCE(NULLIF(company_key, ''), %s) AS company_key FROM bejerman_sync_jobs WHERE id=%s",
                [DEFAULT_INGRESS_COMPANY_KEY, job_id],
                one=True,
            )
            if not row:
                return Response({"detail": "Operación Bejerman no encontrada"}, status=404)
            company_key = row.get("company_key") or DEFAULT_INGRESS_COMPANY_KEY
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
                client=BejermanSDKClient(company_key=company_key if job_id else None, actor_user_id=user_id),
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
        )
        reopened = reopen_jobs_for_article_mapping(model_id, variante, article_code)
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
                    raise BejermanBlockedError("OperaciÃ³n Bejerman no encontrada")
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
                client = BejermanSDKClient(actor_user_id=_actor_id(request))
                context = article_context_for_model(int(model_id), variante)
                payload = search_bejerman_articles_for_context(
                    context=context,
                    query=query,
                    client=client,
                    limit=min(max(limit, 1), 50),
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
