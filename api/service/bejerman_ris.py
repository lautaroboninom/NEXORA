from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .bejerman_companies import require_company
from .bejerman_sdk import (
    BejermanPdfReference,
    BejermanPdfPendingError,
    BejermanSDKClient,
    BejermanSdkConfigError,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
    build_service_ingress_comprobante,
    fetch_comprobante_pdf,
    parse_remito_response,
    resolve_customer_document_fields,
)
from .bejerman_sync import normalize_article_variant

logger = logging.getLogger(__name__)


class BejermanRisError(RuntimeError):
    pass


class BejermanRisBusyError(BejermanRisError):
    pass


class BejermanRisPdfError(BejermanRisError):
    pass


class BejermanRisPdfPendingError(BejermanRisPdfError):
    def __init__(self, message: str = "El PDF del RIS todavía no está listo", *, retry_after_ms: int = 2500):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


def _json_param(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def q(sql: str, params=None, one: bool = False):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        if not cur.description:
            return None
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if one:
            return rows[0] if rows else None
        return rows


def os_label(value: Any) -> str:
    try:
        return str(int(value)).zfill(5)
    except Exception:
        return str(value or "")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _date_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _clean(value)
    return text[:10] if text else None


def _normalize_key(value: Any) -> str:
    text = _clean(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: Any) -> set[str]:
    return {part for part in _normalize_key(value).split(" ") if len(part) >= 2}


def _table_exists(table_name: str) -> bool:
    try:
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
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
            else:
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


def _has_table_column(table_name: str, column_name: str) -> bool:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = %s
                   AND column_name = %s
                 LIMIT 1
                """,
                [table_name, column_name],
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _has_ris_schema() -> bool:
    return _table_exists("bejerman_ingreso_remitos")


def _row_for_ingreso(ingreso_id: int) -> dict[str, Any] | None:
    if not _has_ris_schema():
        return None
    return q(
        """
        SELECT *
          FROM bejerman_ingreso_remitos
         WHERE ingreso_id = %s
         LIMIT 1
        """,
        [ingreso_id],
        one=True,
    )


def serialize_ris_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "available": _has_ris_schema(),
            "status": "pending",
            "pdf_status": "pending",
            "remito_number": "",
            "company_key": "",
            "company_label": "",
            "bejerman_company": "",
            "last_error": "",
        }
    return {
        "available": True,
        "id": row.get("id"),
        "ingreso_id": row.get("ingreso_id"),
        "status": row.get("status") or "pending",
        "pdf_status": row.get("pdf_status") or "pending",
        "attempts": row.get("attempts") or 0,
        "last_error": row.get("last_error") or "",
        "remito_number": row.get("remito_number") or "",
        "comprobante_tipo": row.get("comprobante_tipo") or "",
        "comprobante_letra": row.get("comprobante_letra") or "",
        "comprobante_pto_venta": row.get("comprobante_pto_venta") or "",
        "comprobante_numero": row.get("comprobante_numero") or "",
        "customer_code": row.get("customer_code") or "",
        "customer_name": row.get("customer_name") or "",
        "company_key": row.get("company_key") or "",
        "company_label": row.get("company_label") or "",
        "bejerman_company": row.get("bejerman_company") or "",
        "issue_date": _date_iso(row.get("issue_date")),
        "generated_at": row.get("generated_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def get_ris_status_for_ingreso(ingreso_id: int) -> dict[str, Any]:
    return serialize_ris_row(_row_for_ingreso(ingreso_id))


def _ensure_ris_row(ingreso_id: int, user_id: int | None) -> dict[str, Any]:
    if not _has_ris_schema():
        raise BejermanRisError("Falta aplicar el esquema de RIS de ingreso")
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_ingreso_remitos(ingreso_id, created_by)
            VALUES (%s, %s)
            ON CONFLICT (ingreso_id) DO NOTHING
            """,
            [ingreso_id, user_id],
        )
    row = _row_for_ingreso(ingreso_id)
    if not row:
        raise BejermanRisError("No se pudo inicializar el estado RIS")
    return row


def _lock_for_emit(ingreso_id: int) -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_ingreso_remitos
               SET status = 'running',
                   attempts = attempts + 1,
                   last_error = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE ingreso_id = %s
               AND (
                 status <> 'running'
                 OR updated_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
               )
            RETURNING *
            """,
            [ingreso_id],
        )
        row = cur.fetchone()
        if not row:
            raise BejermanRisBusyError("Ya hay una emisión RIS en curso")
        cols = [col[0] for col in cur.description]
        return dict(zip(cols, row))


def _update_ris_failure(
    ingreso_id: int,
    error: str,
    *,
    pdf_error: bool = False,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
) -> None:
    status_sql = "status" if not pdf_error else "pdf_status"
    failed_value = "failed"
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE bejerman_ingreso_remitos
               SET {status_sql} = %s,
                   last_error = %s,
                   request_payload = COALESCE(%s::jsonb, request_payload),
                   response_payload = COALESCE(%s::jsonb, response_payload),
                   updated_at = CURRENT_TIMESTAMP
             WHERE ingreso_id = %s
            """,
            [
                failed_value,
                error[:2000],
                _json_param(request_payload) if request_payload is not None else None,
                _json_param(response_payload) if response_payload is not None else None,
                ingreso_id,
            ],
        )


def _update_ris_generated(ingreso_id: int, payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    summary = response.get("response") if isinstance(response.get("response"), dict) else {}
    profile = response.get("profile") if isinstance(response.get("profile"), dict) else {}
    remito_number = _clean(response.get("remitoNumber")) or _clean(summary.get("remitoNumber"))
    comprobante_tipo = _clean(summary.get("comprobanteTipo")) or _clean(profile.get("type")) or "RIS"
    comprobante_letra = _clean(summary.get("comprobanteLetra")) or "R"
    comprobante_pto = _clean(summary.get("comprobantePtoVenta")) or _clean(profile.get("pointOfSale"))
    comprobante_numero = _clean(summary.get("comprobanteNumero")) or _number_from_remito(remito_number)
    issue_date = _date_iso(response.get("issueDate") or payload.get("issueDate")) or timezone.localdate().isoformat()
    customer_code = _clean(payload.get("customerCode"))
    customer_name = _clean(payload.get("customerName"))
    company_key = _clean(response.get("companyKey")) or _clean(payload.get("companyKey"))
    company_label = _clean(response.get("companyLabel")) or _clean(payload.get("companyLabel"))
    bejerman_company = _clean(response.get("bejermanCompany")) or _clean(payload.get("bejermanCompany"))

    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_ingreso_remitos
               SET status = 'generated',
                   pdf_status = 'pending',
                   last_error = NULL,
                   request_payload = %s::jsonb,
                   response_payload = %s::jsonb,
                   comprobante_tipo = NULLIF(%s, ''),
                   comprobante_letra = NULLIF(%s, ''),
                   comprobante_pto_venta = NULLIF(%s, ''),
                   comprobante_numero = NULLIF(%s, ''),
                   remito_number = NULLIF(%s, ''),
                   customer_code = NULLIF(%s, ''),
                   customer_name = NULLIF(%s, ''),
                   company_key = NULLIF(%s, ''),
                   company_label = NULLIF(%s, ''),
                   bejerman_company = NULLIF(%s, ''),
                   issue_date = %s,
                   generated_at = COALESCE(generated_at, CURRENT_TIMESTAMP),
                   updated_at = CURRENT_TIMESTAMP
             WHERE ingreso_id = %s
            RETURNING *
            """,
            [
                _json_param(payload),
                _json_param(response),
                comprobante_tipo,
                comprobante_letra,
                comprobante_pto,
                comprobante_numero,
                remito_number,
                customer_code,
                customer_name,
                company_key,
                company_label,
                bejerman_company,
                issue_date,
                ingreso_id,
            ],
        )
        row = cur.fetchone()
        cols = [col[0] for col in cur.description]
    if remito_number:
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET remito_ingreso = %s WHERE id = %s", [remito_number, ingreso_id])
    return dict(zip(cols, row)) if row else (_row_for_ingreso(ingreso_id) or {})


def _update_ris_pdf_ready(ingreso_id: int) -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_ingreso_remitos
               SET pdf_status = 'ready',
                   last_error = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE ingreso_id = %s
            RETURNING *
            """,
            [ingreso_id],
        )
        row = cur.fetchone()
        cols = [col[0] for col in cur.description]
    return dict(zip(cols, row)) if row else (_row_for_ingreso(ingreso_id) or {})


def _update_ris_pdf_pending(ingreso_id: int) -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_ingreso_remitos
               SET pdf_status = 'pending',
                   last_error = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE ingreso_id = %s
            RETURNING *
            """,
            [ingreso_id],
        )
        row = cur.fetchone()
        cols = [col[0] for col in cur.description]
    return dict(zip(cols, row)) if row else (_row_for_ingreso(ingreso_id) or {})


def _number_from_remito(remito_number: str) -> str:
    text = _clean(remito_number)
    if not text:
        return ""
    match = re.search(r"-(\d+)\s*$", text)
    if match:
        return match.group(1)
    match = re.search(r"(\d+)\s*$", text)
    return match.group(1) if match else ""


def _ingreso_context(ingreso_id: int) -> dict[str, Any]:
    empresa_bejerman_sql = (
        "COALESCE(t.empresa_bejerman, 'SEPID') AS empresa_bejerman,"
        if _has_table_column("ingresos", "empresa_bejerman")
        else "'SEPID' AS empresa_bejerman,"
    )
    empresa_facturar_sql = (
        "COALESCE(t.empresa_facturar, 'SEPID') AS empresa_facturar,"
        if _has_table_column("ingresos", "empresa_facturar")
        else "'SEPID' AS empresa_facturar,"
    )
    row = q(
        f"""
        SELECT
          t.id,
          t.motivo,
          t.fecha_ingreso,
          COALESCE(t.accesorios, '') AS accesorios,
          COALESCE(t.comentarios, '') AS comentarios,
          COALESCE(t.equipo_variante, '') AS equipo_variante,
          COALESCE(t.remito_ingreso, '') AS remito_ingreso,
          {empresa_bejerman_sql}
          {empresa_facturar_sql}
          c.id AS customer_id,
          COALESCE(c.cod_empresa, '') AS customer_code,
          COALESCE(c.razon_social, '') AS customer_name,
          d.id AS device_id,
          COALESCE(d.numero_serie, '') AS numero_serie,
          COALESCE(d.numero_interno, '') AS numero_interno,
          d.marca_id,
          COALESCE(b.nombre, '') AS marca,
          d.model_id,
          COALESCE(m.nombre, '') AS modelo,
          COALESCE(m.tipo_equipo, '') AS tipo_equipo,
          COALESCE(d.variante, '') AS device_variante,
          COALESCE(m.variante, '') AS modelo_variante,
          COALESCE(u.nombre, '') AS recibido_por_nombre
        FROM ingresos t
        JOIN devices d ON d.id = t.device_id
        JOIN customers c ON c.id = d.customer_id
        LEFT JOIN marcas b ON b.id = d.marca_id
        LEFT JOIN models m ON m.id = d.model_id
        LEFT JOIN users u ON u.id = t.recibido_por
        WHERE t.id = %s
        """,
        [ingreso_id],
        one=True,
    )
    if not row:
        raise BejermanRisError("Ingreso no encontrado")
    return row


def _article_mapping_for_context(model_id: Any, variante: str) -> dict[str, Any] | None:
    if not model_id or not _table_exists("bejerman_article_mappings"):
        return None
    return q(
        """
        SELECT *
          FROM bejerman_article_mappings
         WHERE model_id = %s
           AND variante_norm = %s
         ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END,
                  confirmed_at DESC NULLS LAST,
                  updated_at DESC
         LIMIT 1
        """,
        [model_id, normalize_article_variant(variante)],
        one=True,
    )


def _build_payload(context: dict[str, Any]) -> dict[str, Any]:
    customer_code = _clean(context.get("customer_code"))
    try:
        company = require_company(context.get("empresa_bejerman") or context.get("empresa_facturar") or "SEPID")
    except ValueError as exc:
        raise BejermanRisError(str(exc)) from exc
    if not customer_code:
        raise BejermanRisError("El cliente seleccionado no tiene código Bejerman")
    variante = _clean(context.get("equipo_variante")) or _clean(context.get("device_variante")) or _clean(context.get("modelo_variante"))
    mapping = _article_mapping_for_context(context.get("model_id"), variante)
    article_code = _clean((mapping or {}).get("article_code")) or _clean(getattr(settings, "BEJERMAN_RIS_GENERIC_ARTICLE_CODE", "SERVICIO"))
    article_name = _clean((mapping or {}).get("article_description")) or _clean(getattr(settings, "BEJERMAN_RIS_GENERIC_ARTICLE_NAME", "Equipo recibido para servicio técnico"))
    issue_date = _date_iso(context.get("fecha_ingreso")) or timezone.localdate().isoformat()
    serial = _clean(context.get("numero_serie")) or _clean(context.get("numero_interno"))
    pieces = [
        _clean(context.get("tipo_equipo")),
        _clean(context.get("marca")),
        _clean(context.get("modelo")),
        variante,
    ]
    equipment_label = " ".join(part for part in pieces if part)
    return {
        "requestId": f"reparaciones-ingreso-{context['id']}",
        "ingresoId": context["id"],
        "companyKey": company.key,
        "companyLabel": company.label,
        "bejermanCompany": company.bejerman_company,
        "issueDate": issue_date,
        "customerCode": customer_code,
        "customerName": _clean(context.get("customer_name")),
        "sellerCode": _clean(getattr(settings, "BEJERMAN_RIS_SELLER_CODE", "ADM")),
        "paymentTermCode": _clean(getattr(settings, "BEJERMAN_RIS_PAYMENT_TERM", "30")),
        "notes": f"OS {os_label(context['id'])}",
        "equipment": {
            "articleCode": article_code,
            "articleName": article_name,
            "serial": serial,
            "internalNumber": _clean(context.get("numero_interno")),
            "equipmentType": _clean(context.get("tipo_equipo")),
            "brand": _clean(context.get("marca")),
            "model": _clean(context.get("modelo")),
            "variant": variante,
            "repairReason": _clean(context.get("motivo")),
            "accessories": _clean(context.get("accesorios")),
            "comments": _clean(context.get("comentarios")),
            "equipmentLabel": equipment_label,
        },
    }


def _pdf_params(row: dict[str, Any]) -> dict[str, Any]:
    number = _clean(row.get("comprobante_numero")) or _number_from_remito(_clean(row.get("remito_number")))
    request_payload = row.get("request_payload") if isinstance(row.get("request_payload"), dict) else {}
    company_key = _clean(row.get("company_key")) or _clean(request_payload.get("companyKey")) or "SEPID"
    return {
        "type": _clean(row.get("comprobante_tipo")) or "RIS",
        "letter": _clean(row.get("comprobante_letra")) or "R",
        "pointOfSale": _clean(row.get("comprobante_pto_venta")),
        "number": number,
        "companyKey": company_key,
        "issueDate": _date_iso(row.get("issue_date")) or timezone.localdate().isoformat(),
        "customerCode": _clean(row.get("customer_code")),
    }


def _fetch_pdf(row: dict[str, Any]) -> tuple[bytes, str]:
    params = _pdf_params(row)
    if not params["number"] or not params["pointOfSale"]:
        raise BejermanRisPdfError("No hay referencia de comprobante suficiente para pedir el PDF")
    try:
        client = BejermanSDKClient(company_key=params["companyKey"])
    except ValueError as exc:
        raise BejermanRisPdfError(str(exc)) from exc
    reference = BejermanPdfReference(
        type=params["type"],
        number=params["number"],
        letter=params["letter"],
        point_of_sale=params["pointOfSale"],
        issue_date=params["issueDate"],
        customer_code=params["customerCode"],
    )
    return fetch_comprobante_pdf(client, reference)


def emit_or_get_ris(ingreso_id: int, user_id: int | None = None) -> dict[str, Any]:
    _ensure_ris_row(ingreso_id, user_id)
    current = _row_for_ingreso(ingreso_id) or {}
    if _clean(current.get("remito_number")) and current.get("status") == "generated":
        return current

    _lock_for_emit(ingreso_id)
    payload: dict[str, Any] = {}
    request_payload: dict[str, Any] | None = None
    operation = _clean(getattr(settings, "BEJERMAN_RIS_OPERATION", "IngresarComprobanteJSON")) or "IngresarComprobanteJSON"
    numera_flex = _clean(getattr(settings, "BEJERMAN_RIS_NUMERA_FLEX", "S")) or "S"
    emite_reg = _clean(getattr(settings, "BEJERMAN_RIS_EMITE_REG", "E")) or "E"
    try:
        context = _ingreso_context(ingreso_id)
        payload = _build_payload(context)
    except BejermanRisError as exc:
        _update_ris_failure(ingreso_id, str(exc), pdf_error=False, request_payload=payload or None)
        raise
    try:
        client = BejermanSDKClient(company_key=payload["companyKey"])
        customer_fields = resolve_customer_document_fields(client, payload["customerCode"])
        built = build_service_ingress_comprobante(payload, customer_fields)
        request_payload = {
            **payload,
            "comprobante": built["comprobante"],
            "operation": operation,
            "numeraFlex": numera_flex,
            "emiteReg": emite_reg,
        }
        raw_response = client.ingresar_comprobante_ventas_json(
            built["comprobante"],
            circuito="VENTAS",
            operacion=operation,
            numera_flex=numera_flex,
            emite_reg=emite_reg,
        )
        summary = parse_remito_response(raw_response)
        remito_number = _clean(summary.get("remitoNumber"))
        if not remito_number:
            raise BejermanSdkResponseError("Bejerman no devolvió número de RIS")
        payload = request_payload
        response = {
            "success": True,
            "requestId": payload["requestId"],
            "companyKey": payload["companyKey"],
            "companyLabel": payload["companyLabel"],
            "bejermanCompany": payload["bejermanCompany"],
            "issueDate": payload["issueDate"],
            "remitoNumber": remito_number,
            "response": summary,
            "profile": built["profile"],
            "lineCount": built["lineCount"],
            "raw": raw_response,
        }
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, ValueError) as exc:
        _update_ris_failure(ingreso_id, str(exc), pdf_error=False, request_payload=request_payload or payload or None)
        raise BejermanRisError(str(exc)) from exc

    return _update_ris_generated(ingreso_id, payload, response)


def fetch_ris_pdf(ingreso_id: int) -> tuple[bytes, str, dict[str, Any]]:
    row = _row_for_ingreso(ingreso_id) or {}
    if not row:
        raise BejermanRisError("RIS no inicializado")
    if row.get("status") != "generated" or not _clean(row.get("remito_number")):
        if row.get("status") == "failed":
            raise BejermanRisError(_clean(row.get("last_error")) or "No se pudo emitir el RIS")
        raise BejermanRisPdfPendingError("El RIS todavía no fue emitido")
    try:
        pdf_bytes, content_type = _fetch_pdf(row)
    except BejermanPdfPendingError as exc:
        row = _update_ris_pdf_pending(ingreso_id)
        raise BejermanRisPdfPendingError(str(exc), retry_after_ms=getattr(exc, "retry_after_ms", 2500)) from exc
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, BejermanRisPdfError) as exc:
        _update_ris_failure(ingreso_id, str(exc), pdf_error=True)
        raise BejermanRisPdfError(str(exc)) from exc
    row = _update_ris_pdf_ready(ingreso_id)
    return pdf_bytes, content_type, row


def emit_or_fetch_ris_pdf(ingreso_id: int, user_id: int | None = None) -> tuple[bytes, str, dict[str, Any]]:
    emit_or_get_ris(ingreso_id, user_id)
    return fetch_ris_pdf(ingreso_id)


def find_customer_suggestion(customer_code: str, customer_name: str) -> dict[str, Any] | None:
    code = _clean(customer_code)
    name = _clean(customer_name)
    row = None
    if code:
        row = q(
            """
            SELECT id, cod_empresa, razon_social, telefono
              FROM customers
             WHERE cod_empresa = %s
             LIMIT 1
            """,
            [code],
            one=True,
        )
    if not row and name:
        row = q(
            """
            SELECT id, cod_empresa, razon_social, telefono
              FROM customers
             WHERE LOWER(TRIM(razon_social)) = LOWER(TRIM(%s))
             LIMIT 1
            """,
            [name],
            one=True,
        )
    return row


def equipment_suggestion_from_bejerman_article(article_code: str, description: str) -> dict[str, Any]:
    mapping = None
    code = _clean(article_code)
    if code and _table_exists("bejerman_article_mappings"):
        mapping = q(
            """
            SELECT
              bam.article_code,
              bam.article_description,
              bam.match_source,
              m.id AS modelo_id,
              COALESCE(m.nombre, '') AS modelo,
              COALESCE(m.tipo_equipo, '') AS tipo_equipo,
              COALESCE(m.variante, '') AS variante,
              b.id AS marca_id,
              COALESCE(b.nombre, '') AS marca
            FROM bejerman_article_mappings bam
            JOIN models m ON m.id = bam.model_id
            LEFT JOIN marcas b ON b.id = m.marca_id
            WHERE UPPER(TRIM(bam.article_code)) = UPPER(TRIM(%s))
            ORDER BY CASE WHEN bam.match_source = 'manual' THEN 0 ELSE 1 END,
                     bam.confirmed_at DESC NULLS LAST,
                     bam.updated_at DESC
            LIMIT 1
            """,
            [code],
            one=True,
        )
    if mapping:
        return {
            "source": "article_mapping",
            "confidence": "high",
            "marca_id": mapping.get("marca_id"),
            "marca": mapping.get("marca") or "",
            "modelo_id": mapping.get("modelo_id"),
            "modelo": mapping.get("modelo") or "",
            "tipo_equipo": mapping.get("tipo_equipo") or "",
            "variante": mapping.get("variante") or "",
        }

    desc_tokens = _tokens(description)
    if not desc_tokens:
        return {"source": "description", "confidence": "none"}
    rows = q(
        """
        SELECT
          m.id AS modelo_id,
          COALESCE(m.nombre, '') AS modelo,
          COALESCE(m.tipo_equipo, '') AS tipo_equipo,
          COALESCE(m.variante, '') AS variante,
          b.id AS marca_id,
          COALESCE(b.nombre, '') AS marca
        FROM models m
        LEFT JOIN marcas b ON b.id = m.marca_id
        ORDER BY m.id DESC
        LIMIT 3000
        """
    ) or []
    best = None
    best_score = 0
    for row in rows:
        brand = _tokens(row.get("marca"))
        model = _tokens(row.get("modelo"))
        variant = _tokens(row.get("variante"))
        kind = _tokens(row.get("tipo_equipo"))
        score = len(desc_tokens & brand) * 35 + len(desc_tokens & model) * 50 + len(desc_tokens & variant) * 20 + len(desc_tokens & kind) * 12
        if score > best_score:
            best = row
            best_score = score
    if best and best_score >= 50:
        return {
            "source": "description",
            "confidence": "medium" if best_score < 90 else "high",
            "score": best_score,
            "marca_id": best.get("marca_id"),
            "marca": best.get("marca") or "",
            "modelo_id": best.get("modelo_id"),
            "modelo": best.get("modelo") or "",
            "tipo_equipo": best.get("tipo_equipo") or "",
            "variante": best.get("variante") or "",
        }
    return {"source": "description", "confidence": "none", "score": best_score}
