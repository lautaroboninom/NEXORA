from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .bejerman_sdk import (
    BejermanSDKClient,
    as_string,
    bejerman_filter,
    build_article_filters,
    build_articles_result,
    first_value,
    format_remito_number,
    normalize_date,
    parse_remito_response,
    records_from_response,
)


COMPANY_KEY = "SEPID"
COMPROBANTE_TIPO = "RT"
OPERATION_CODE = "MC"
DEFAULT_DEPOSIT = "VAL"
DEFAULT_LETTER = "R"
MUTABLE_STATUSES = {"draft", "validated", "failed"}
FINAL_STATUSES = {"running", "generated", "cancelled"}
QTY_QUANT = Decimal("0.0001")
MONEY_QUANT = Decimal("0.01")


class BejermanPurchaseError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_value(value: Any, default: Any):
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _raw_text(value: Any) -> str:
    return "" if value is None else str(value)


def _get(payload: dict[str, Any] | None, *keys: str) -> Any:
    payload = payload or {}
    for key in keys:
        if key in payload:
            return payload.get(key)
    return None


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return default


def _positive_decimal(value: Any, default: Decimal | None = None) -> Decimal:
    parsed = _decimal(value, default)
    if parsed is None or parsed <= 0:
        raise BejermanPurchaseError("INVALID_QUANTITY", "La cantidad debe ser mayor a cero")
    return parsed.quantize(QTY_QUANT)


def _nonnegative_money(value: Any, default: Decimal | None = None) -> Decimal:
    parsed = _decimal(value, default)
    if parsed is None:
        raise BejermanPurchaseError("VALUE_REQUIRED", "Hay que cargar el valor unitario")
    if parsed < 0:
        raise BejermanPurchaseError("INVALID_VALUE", "El valor unitario no puede ser negativo")
    return parsed.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _money(value: Any) -> Decimal:
    parsed = _decimal(value, Decimal("0")) or Decimal("0")
    return parsed.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _qty(value: Any) -> Decimal:
    parsed = _decimal(value, Decimal("0")) or Decimal("0")
    return parsed.quantize(QTY_QUANT)


def _public_decimal(value: Any, places: str = "0.01") -> float:
    parsed = _decimal(value, Decimal("0")) or Decimal("0")
    return float(parsed.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _barcode_norm(value: Any) -> str:
    return re.sub(r"\s+", " ", _raw_text(value)).strip().upper()


def _today() -> str:
    return timezone.localdate().isoformat()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _fetchone(sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        row = cur.fetchone()
        if not row:
            return None
        cols = [col[0] for col in cur.description]
        return dict(zip(cols, row))


def _fetchall(sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        cols = [col[0] for col in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _execute(sql: str, params: list[Any] | tuple[Any, ...] | None = None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])


def _uuid(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def _require_entry(entry_id: str) -> dict[str, Any]:
    row = _fetchone("SELECT * FROM bejerman_purchase_entries WHERE id = %s", [entry_id])
    if not row:
        raise BejermanPurchaseError("PURCHASE_ENTRY_NOT_FOUND", "Ingreso de mercadería no encontrado", status_code=404)
    return row


def _require_mutable(entry_id: str) -> dict[str, Any]:
    row = _require_entry(entry_id)
    if row.get("status") in FINAL_STATUSES:
        raise BejermanPurchaseError("PURCHASE_ENTRY_LOCKED", "El lote ya no se puede editar", status_code=409)
    return row


def _event(entry_id: str, actor_user_id: int | None, event_type: str, *, note: str = "", metadata: dict[str, Any] | None = None):
    _execute(
        """
        INSERT INTO bejerman_purchase_entry_events (entry_id, actor_user_id, event_type, note, metadata)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        [entry_id, actor_user_id, event_type, note or "", _json_param(metadata or {})],
    )


def _mark_draft_after_change(entry_id: str, actor_user_id: int | None):
    _execute(
        """
        UPDATE bejerman_purchase_entries
           SET status = CASE WHEN status IN ('validated', 'failed') THEN 'draft' ELSE status END,
               updated_by_user_id = %s,
               last_error = NULL
         WHERE id = %s AND status IN ('draft', 'validated', 'failed')
        """,
        [actor_user_id, entry_id],
    )


def _recalculate_entry_totals(entry_id: str):
    _execute(
        """
        UPDATE bejerman_purchase_entries e
           SET total_quantity = COALESCE(t.total_quantity, 0),
               total_value = COALESCE(t.total_value, 0)
          FROM (
            SELECT entry_id,
                   SUM(conversion_factor) AS total_quantity,
                   SUM(total_value) AS total_value
              FROM bejerman_purchase_entry_scans
             WHERE entry_id = %s
             GROUP BY entry_id
          ) t
         WHERE e.id = %s AND e.id = t.entry_id
        """,
        [entry_id, entry_id],
    )
    _execute(
        """
        UPDATE bejerman_purchase_entries
           SET total_quantity = 0,
               total_value = 0
         WHERE id = %s
           AND NOT EXISTS (SELECT 1 FROM bejerman_purchase_entry_scans WHERE entry_id = %s)
        """,
        [entry_id, entry_id],
    )


def _line_rows(entry_id: str) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT *
          FROM bejerman_purchase_entry_lines
         WHERE entry_id = %s
         ORDER BY sort_order ASC, created_at ASC, id ASC
        """,
        [entry_id],
    )


def _scan_rows(entry_id: str) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT *
          FROM bejerman_purchase_entry_scans
         WHERE entry_id = %s
         ORDER BY sort_order ASC, created_at ASC, id ASC
        """,
        [entry_id],
    )


def _event_rows(entry_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return _fetchall(
        """
        SELECT id, actor_user_id, event_type, note, metadata, created_at
          FROM bejerman_purchase_entry_events
         WHERE entry_id = %s
         ORDER BY created_at DESC, id DESC
         LIMIT %s
        """,
        [entry_id, limit],
    )


def _serialize_scan(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "entryId": row.get("entry_id"),
        "lineId": row.get("line_id"),
        "barcode": row.get("barcode") or "",
        "barcodeNorm": row.get("barcode_norm") or "",
        "articleCode": row.get("article_code") or "",
        "articleDescription": row.get("article_description") or "",
        "conversionFactor": _public_decimal(row.get("conversion_factor"), "0.0001"),
        "unitValue": _public_decimal(row.get("unit_value")),
        "totalValue": _public_decimal(row.get("total_value")),
        "isManualQuantity": bool(row.get("is_manual_quantity")),
        "note": row.get("note") or "",
        "sortOrder": int(row.get("sort_order") or 0),
        "scannedByUserId": row.get("scanned_by_user_id"),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


def _serialize_line(row: dict[str, Any], scans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "entryId": row.get("entry_id"),
        "articleCode": row.get("article_code") or "",
        "articleDescription": row.get("article_description") or "",
        "defaultConversionFactor": _public_decimal(row.get("default_conversion_factor"), "0.0001"),
        "defaultUnitValue": _public_decimal(row.get("default_unit_value")),
        "depositCode": row.get("deposit_code") or DEFAULT_DEPOSIT,
        "sortOrder": int(row.get("sort_order") or 0),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
        "scans": scans,
        "totalQuantity": _public_decimal(sum((_decimal(scan.get("conversionFactor"), Decimal("0")) or Decimal("0")) for scan in scans), "0.0001"),
        "totalValue": _public_decimal(sum((_decimal(scan.get("totalValue"), Decimal("0")) or Decimal("0")) for scan in scans)),
    }


def _serialize_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "actorUserId": row.get("actor_user_id"),
        "eventType": row.get("event_type") or "",
        "note": row.get("note") or "",
        "metadata": _json_value(row.get("metadata"), {}),
        "createdAt": _iso(row.get("created_at")),
    }


def _serialize_entry(
    row: dict[str, Any],
    *,
    include_lines: bool = True,
    include_events: bool = False,
    include_payloads: bool = True,
) -> dict[str, Any]:
    scans_by_line: dict[str, list[dict[str, Any]]] = {}
    lines = []
    if include_lines:
        for scan in (_serialize_scan(item) for item in _scan_rows(row["id"])):
            scans_by_line.setdefault(scan["lineId"], []).append(scan)
        lines = [_serialize_line(line, scans_by_line.get(line["id"], [])) for line in _line_rows(row["id"])]
    payload = {
        "id": row.get("id"),
        "status": row.get("status") or "draft",
        "companyKey": row.get("company_key") or COMPANY_KEY,
        "comprobanteTipo": row.get("comprobante_tipo") or COMPROBANTE_TIPO,
        "comprobanteLetra": row.get("comprobante_letra") or DEFAULT_LETTER,
        "comprobantePtoVenta": row.get("comprobante_pto_venta") or "",
        "comprobanteNumero": row.get("comprobante_numero") or "",
        "remitoNumber": row.get("remito_number") or "",
        "supplierCode": row.get("supplier_code") or "",
        "supplierCodeRaw": row.get("supplier_code_raw") or row.get("supplier_code") or "",
        "supplierName": row.get("supplier_name") or "",
        "supplierTaxId": row.get("supplier_tax_id") or "",
        "supplierSnapshot": _json_value(row.get("supplier_snapshot"), {}),
        "paymentTermCode": row.get("payment_term_code") or "",
        "issueDate": _iso(row.get("issue_date")),
        "accountingDate": _iso(row.get("accounting_date")),
        "ddjjDate": _iso(row.get("ddjj_date")),
        "operationCode": row.get("operation_code") or OPERATION_CODE,
        "depositCode": row.get("deposit_code") or DEFAULT_DEPOSIT,
        "notes": row.get("notes") or "",
        "totalQuantity": _public_decimal(row.get("total_quantity"), "0.0001"),
        "totalValue": _public_decimal(row.get("total_value")),
        "lastError": row.get("last_error") or "",
        "createdByUserId": row.get("created_by_user_id"),
        "updatedByUserId": row.get("updated_by_user_id"),
        "generatedByUserId": row.get("generated_by_user_id"),
        "generatedAt": _iso(row.get("generated_at")),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
        "lines": lines,
    }
    if include_payloads:
        payload["requestPayload"] = _json_value(row.get("request_payload"), {})
        payload["responsePayload"] = _json_value(row.get("response_payload"), {})
    if include_events:
        payload["events"] = [_serialize_event(item) for item in _event_rows(row["id"])]
    return payload


def _first_raw(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    value = first_value(record, keys)
    if value not in (None, ""):
        return value
    return None


def _map_provider(record: dict[str, Any]) -> dict[str, Any] | None:
    raw_code_value = _first_raw(record, ("Proveedor_Codigo", "ProveedorCodigo", "CodigoProveedor", "Codigo", "CodProveedor"))
    raw_code = _raw_text(raw_code_value)
    code = raw_code.strip()
    name = as_string(
        first_value(
            record,
            (
                "Proveedor_RazonSocial",
                "ProveedorRazonSocial",
                "RazonSocial",
                "Nombre",
                "Proveedor_Nombre",
                "Descripcion",
            ),
        )
    )
    cuit = as_string(first_value(record, ("Proveedor_NroDocumento", "Proveedor_CUIT", "CUIT", "Cuit", "NroDocumento")))
    if not code and not name:
        return None
    return {
        "id": code or name,
        "code": code,
        "rawCode": raw_code or code,
        "name": name or code,
        "cuit": cuit,
        "paymentTermCode": as_string(first_value(record, ("Comprobante_CondicionPago", "Proveedor_CondicionPago", "CondicionPago"))),
        "tipoDocumento": first_value(record, ("Proveedor_TipoDocumento", "TipoDocumento")),
        "provincia": as_string(first_value(record, ("Proveedor_Provincia", "Provincia"))),
        "sitIva": as_string(first_value(record, ("Proveedor_SitIVA", "SitIVA"))),
        "ingresosBrutos": as_string(first_value(record, ("Proveedor_NroIngresosBrutos", "Proveedor_NumeroIIBB", "IngresosBrutos"))),
        "raw": record,
    }


def _supplier_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    provider = _get(payload, "provider", "supplier")
    provider = provider if isinstance(provider, dict) else {}
    raw_code = _raw_text(_get(payload, "supplierCodeRaw", "providerCodeRaw") or provider.get("rawCode") or provider.get("Proveedor_Codigo"))
    code = _text(_get(payload, "supplierCode", "providerCode") or provider.get("code") or raw_code)
    name = _text(_get(payload, "supplierName", "providerName") or provider.get("name") or provider.get("Proveedor_RazonSocial"))
    cuit = _text(_get(payload, "supplierTaxId", "providerTaxId") or provider.get("cuit") or provider.get("Proveedor_NroDocumento"))
    snapshot = provider.get("raw") if isinstance(provider.get("raw"), dict) else provider
    return {
        "code": code,
        "rawCode": raw_code or code,
        "name": name,
        "taxId": cuit,
        "snapshot": snapshot or {},
        "paymentTermCode": _text(
            _get(payload, "paymentTermCode")
            or provider.get("paymentTermCode")
            or provider.get("Comprobante_CondicionPago")
        ),
    }


def list_purchase_providers(
    params: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    params = params or {}
    search = _text(_get(params, "q", "search")).lower()
    limit = max(1, min(100, int(_text(params.get("limit")) or 30)))
    response = (client or BejermanSDKClient(company_key=COMPANY_KEY, actor_user_id=actor_user_id)).list_proveedores()
    items = [item for item in (_map_provider(record) for record in records_from_response(response)) if item]
    if search:
        items = [
            item
            for item in items
            if search in f"{item.get('code')} {item.get('rawCode')} {item.get('name')} {item.get('cuit')}".lower()
        ]
    return {"items": items[:limit], "companyKey": COMPANY_KEY}


def list_purchase_articles(
    params: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    params = params or {}
    filters = build_article_filters(_get(params, "q", "search"), _get(params, "field"))
    response = (client or BejermanSDKClient(company_key=COMPANY_KEY, actor_user_id=actor_user_id)).list_articulos(filters, int(_text(params.get("limit")) or 20))
    result = build_articles_result(response, params)
    result["companyKey"] = COMPANY_KEY
    return result


def list_purchase_entries(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    where = ["company_key = %s"]
    sql_params: list[Any] = [COMPANY_KEY]
    status = _text(params.get("status"))
    if status:
        where.append("status = %s")
        sql_params.append(status)
    else:
        where.append("status <> 'cancelled'")
    search = _text(_get(params, "q", "search"))
    if search:
        like = f"%{search}%"
        where.append(
            """
            (
              supplier_code ILIKE %s OR supplier_name ILIKE %s OR comprobante_numero ILIKE %s
              OR comprobante_pto_venta ILIKE %s OR remito_number ILIKE %s OR last_error ILIKE %s
            )
            """
        )
        sql_params.extend([like, like, like, like, like, like])
    limit = max(1, min(250, int(_text(params.get("limit")) or 100)))
    sql_params.append(limit)
    rows = _fetchall(
        f"""
        SELECT *
          FROM bejerman_purchase_entries
         WHERE {" AND ".join(where)}
         ORDER BY created_at DESC, id DESC
         LIMIT %s
        """,
        sql_params,
    )
    return {"items": [_serialize_entry(row, include_lines=False, include_payloads=False) for row in rows]}


@transaction.atomic
def create_purchase_entry(payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    payload = payload or {}
    supplier = _supplier_from_payload(payload)
    issue_date = normalize_date(_get(payload, "issueDate", "fecha")) or _today()
    entry_id = _uuid("bpe")
    _execute(
        """
        INSERT INTO bejerman_purchase_entries (
          id, company_key, status, comprobante_tipo, comprobante_letra,
          comprobante_pto_venta, comprobante_numero, supplier_code, supplier_code_raw,
          supplier_name, supplier_tax_id, supplier_snapshot, payment_term_code,
          issue_date, accounting_date, ddjj_date, operation_code, deposit_code,
          notes, created_by_user_id, updated_by_user_id
        )
        VALUES (
          %s, %s, 'draft', %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s::jsonb, %s,
          %s, %s, %s, %s, %s,
          %s, %s, %s
        )
        """,
        [
            entry_id,
            COMPANY_KEY,
            COMPROBANTE_TIPO,
            _text(_get(payload, "comprobanteLetra", "letter")) or DEFAULT_LETTER,
            _text(_get(payload, "comprobantePtoVenta", "pointOfSale")),
            _text(_get(payload, "comprobanteNumero", "number")),
            supplier["code"],
            supplier["rawCode"],
            supplier["name"],
            supplier["taxId"],
            _json_param(supplier["snapshot"]),
            _text(_get(payload, "paymentTermCode")) or supplier["paymentTermCode"],
            issue_date,
            normalize_date(_get(payload, "accountingDate")) or issue_date,
            normalize_date(_get(payload, "ddjjDate")) or issue_date,
            OPERATION_CODE,
            _text(_get(payload, "depositCode")) or DEFAULT_DEPOSIT,
            _text(payload.get("notes")),
            actor_user_id,
            actor_user_id,
        ],
    )
    _event(entry_id, actor_user_id, "created", metadata={"source": "nexora"})
    return get_purchase_entry(entry_id)


@transaction.atomic
def update_purchase_entry(entry_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    _require_mutable(entry_id)
    payload = payload or {}
    current = _require_entry(entry_id)
    supplier = _supplier_from_payload(payload)
    use_supplier = bool(supplier["code"] or supplier["name"] or _get(payload, "supplier", "provider"))
    issue_date = normalize_date(_get(payload, "issueDate")) if "issueDate" in payload else current.get("issue_date")
    accounting_date = normalize_date(_get(payload, "accountingDate")) if "accountingDate" in payload else current.get("accounting_date")
    ddjj_date = normalize_date(_get(payload, "ddjjDate")) if "ddjjDate" in payload else current.get("ddjj_date")
    _execute(
        """
        UPDATE bejerman_purchase_entries
           SET comprobante_letra = %s,
               comprobante_pto_venta = %s,
               comprobante_numero = %s,
               supplier_code = %s,
               supplier_code_raw = %s,
               supplier_name = %s,
               supplier_tax_id = %s,
               supplier_snapshot = %s::jsonb,
               payment_term_code = %s,
               issue_date = %s,
               accounting_date = %s,
               ddjj_date = %s,
               deposit_code = %s,
               notes = %s,
               updated_by_user_id = %s
         WHERE id = %s
        """,
        [
            _text(_get(payload, "comprobanteLetra")) or current.get("comprobante_letra") or DEFAULT_LETTER,
            _text(_get(payload, "comprobantePtoVenta")) if "comprobantePtoVenta" in payload else current.get("comprobante_pto_venta"),
            _text(_get(payload, "comprobanteNumero")) if "comprobanteNumero" in payload else current.get("comprobante_numero"),
            supplier["code"] if use_supplier else current.get("supplier_code"),
            supplier["rawCode"] if use_supplier else current.get("supplier_code_raw"),
            supplier["name"] if use_supplier else current.get("supplier_name"),
            supplier["taxId"] if use_supplier else current.get("supplier_tax_id"),
            _json_param(supplier["snapshot"] if use_supplier else _json_value(current.get("supplier_snapshot"), {})),
            _text(_get(payload, "paymentTermCode")) if "paymentTermCode" in payload else current.get("payment_term_code"),
            issue_date,
            accounting_date,
            ddjj_date,
            _text(_get(payload, "depositCode")) if "depositCode" in payload else current.get("deposit_code"),
            _text(payload.get("notes")) if "notes" in payload else current.get("notes"),
            actor_user_id,
            entry_id,
        ],
    )
    _mark_draft_after_change(entry_id, actor_user_id)
    _event(entry_id, actor_user_id, "updated")
    return get_purchase_entry(entry_id)


def get_purchase_entry(entry_id: str, *, include_events: bool = False) -> dict[str, Any]:
    row = _require_entry(entry_id)
    return _serialize_entry(row, include_events=include_events)


@transaction.atomic
def discard_purchase_entry(entry_id: str, actor_user_id: int | None) -> dict[str, Any]:
    row = _require_entry(entry_id)
    if row.get("status") in {"running", "generated"}:
        raise BejermanPurchaseError("PURCHASE_ENTRY_LOCKED", "No se puede descartar un lote emitido o en emisión", status_code=409)
    if row.get("status") != "cancelled":
        _execute(
            """
            UPDATE bejerman_purchase_entries
               SET status = 'cancelled',
                   last_error = NULL,
                   updated_by_user_id = %s
             WHERE id = %s
            """,
            [actor_user_id, entry_id],
        )
        _event(entry_id, actor_user_id, "discarded")
    return get_purchase_entry(entry_id, include_events=True)


@transaction.atomic
def add_purchase_line(entry_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    entry = _require_mutable(entry_id)
    payload = payload or {}
    article_code = _text(_get(payload, "articleCode", "article_code"))
    article_description = _text(_get(payload, "articleDescription", "article_description", "description"))
    if not article_code:
        raise BejermanPurchaseError("ARTICLE_REQUIRED", "Hay que seleccionar un artículo Bejerman")
    default_factor = _positive_decimal(_get(payload, "defaultConversionFactor", "conversionFactor"), Decimal("1"))
    default_unit_value = _nonnegative_money(_get(payload, "defaultUnitValue", "unitValue"), Decimal("0"))
    row = _fetchone(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM bejerman_purchase_entry_lines WHERE entry_id = %s",
        [entry_id],
    )
    line_id = _uuid("bpl")
    _execute(
        """
        INSERT INTO bejerman_purchase_entry_lines (
          id, entry_id, article_code, article_description, default_conversion_factor,
          default_unit_value, deposit_code, sort_order
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            line_id,
            entry_id,
            article_code,
            article_description,
            default_factor,
            default_unit_value,
            _text(_get(payload, "depositCode")) or entry.get("deposit_code") or DEFAULT_DEPOSIT,
            int(row.get("next_order") or 1),
        ],
    )
    _mark_draft_after_change(entry_id, actor_user_id)
    _event(entry_id, actor_user_id, "line_added", metadata={"lineId": line_id, "articleCode": article_code})
    return get_purchase_entry(entry_id)


@transaction.atomic
def update_purchase_line(entry_id: str, line_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    _require_mutable(entry_id)
    line = _fetchone("SELECT * FROM bejerman_purchase_entry_lines WHERE id = %s AND entry_id = %s", [line_id, entry_id])
    if not line:
        raise BejermanPurchaseError("PURCHASE_LINE_NOT_FOUND", "Línea no encontrada", status_code=404)
    payload = payload or {}
    article_code = _text(_get(payload, "articleCode", "article_code")) if "articleCode" in payload or "article_code" in payload else line.get("article_code")
    article_description = (
        _text(_get(payload, "articleDescription", "article_description", "description"))
        if any(key in payload for key in ("articleDescription", "article_description", "description"))
        else line.get("article_description")
    )
    default_factor = (
        _positive_decimal(_get(payload, "defaultConversionFactor", "conversionFactor"), Decimal("1"))
        if "defaultConversionFactor" in payload or "conversionFactor" in payload
        else line.get("default_conversion_factor")
    )
    default_unit_value = (
        _nonnegative_money(_get(payload, "defaultUnitValue", "unitValue"), Decimal("0"))
        if "defaultUnitValue" in payload or "unitValue" in payload
        else line.get("default_unit_value")
    )
    deposit_code = _text(_get(payload, "depositCode")) if "depositCode" in payload else line.get("deposit_code")
    _execute(
        """
        UPDATE bejerman_purchase_entry_lines
           SET article_code = %s,
               article_description = %s,
               default_conversion_factor = %s,
               default_unit_value = %s,
               deposit_code = %s
         WHERE id = %s AND entry_id = %s
        """,
        [article_code, article_description, default_factor, default_unit_value, deposit_code, line_id, entry_id],
    )
    if article_code != line.get("article_code") or article_description != line.get("article_description"):
        _execute(
            """
            UPDATE bejerman_purchase_entry_scans
               SET article_code = %s,
                   article_description = %s
             WHERE entry_id = %s AND line_id = %s
            """,
            [article_code, article_description, entry_id, line_id],
        )
    _recalculate_entry_totals(entry_id)
    _mark_draft_after_change(entry_id, actor_user_id)
    _event(entry_id, actor_user_id, "line_updated", metadata={"lineId": line_id, "articleCode": article_code})
    return get_purchase_entry(entry_id)


@transaction.atomic
def delete_purchase_line(entry_id: str, line_id: str, actor_user_id: int | None) -> dict[str, Any]:
    _require_mutable(entry_id)
    line = _fetchone("SELECT id FROM bejerman_purchase_entry_lines WHERE id = %s AND entry_id = %s", [line_id, entry_id])
    if not line:
        raise BejermanPurchaseError("PURCHASE_LINE_NOT_FOUND", "Línea no encontrada", status_code=404)
    _execute("DELETE FROM bejerman_purchase_entry_lines WHERE id = %s AND entry_id = %s", [line_id, entry_id])
    _recalculate_entry_totals(entry_id)
    _mark_draft_after_change(entry_id, actor_user_id)
    _event(entry_id, actor_user_id, "line_deleted", metadata={"lineId": line_id})
    return get_purchase_entry(entry_id)


def _split_barcodes(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("barcodes")
    if isinstance(raw, list):
        items = raw
    elif raw:
        items = re.split(r"[\r\n\t,;]+", str(raw))
    else:
        barcode = payload.get("barcode")
        items = [barcode] if barcode not in (None, "") else []
    return [_text(item) for item in items if _text(item)]


def _insert_scan(
    entry_id: str,
    line: dict[str, Any],
    payload: dict[str, Any],
    actor_user_id: int | None,
    *,
    barcode: str = "",
    seen: set[str] | None = None,
) -> str:
    norm = _barcode_norm(barcode)
    if seen is not None and norm and norm in seen:
        raise BejermanPurchaseError("DUPLICATE_BARCODE", f"El código {barcode} está repetido en el lote", status_code=409)
    if norm:
        existing = _fetchone(
            """
            SELECT id FROM bejerman_purchase_entry_scans
             WHERE entry_id = %s AND barcode_norm = %s
             LIMIT 1
            """,
            [entry_id, norm],
        )
        if existing:
            raise BejermanPurchaseError("DUPLICATE_BARCODE", f"El código {barcode} ya está cargado en este lote", status_code=409)
        if seen is not None:
            seen.add(norm)
    default_factor = line.get("default_conversion_factor") or Decimal("1")
    default_value = line.get("default_unit_value") or Decimal("0")
    is_manual_quantity = not bool(barcode)
    if is_manual_quantity:
        conversion_factor = _positive_decimal(
            _get(payload, "quantity", "cantidad", "conversionFactor"),
            default_factor,
        )
    else:
        conversion_factor = _positive_decimal(_get(payload, "conversionFactor"), default_factor)
    unit_value = _nonnegative_money(_get(payload, "unitValue", "valorUnitario"), default_value)
    total_value = (conversion_factor * unit_value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    order_row = _fetchone(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM bejerman_purchase_entry_scans WHERE entry_id = %s",
        [entry_id],
    )
    scan_id = _uuid("bps")
    _execute(
        """
        INSERT INTO bejerman_purchase_entry_scans (
          id, entry_id, line_id, barcode, barcode_norm, article_code, article_description,
          conversion_factor, unit_value, total_value, is_manual_quantity, note, sort_order,
          scanned_by_user_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            scan_id,
            entry_id,
            line["id"],
            barcode,
            norm,
            line.get("article_code") or "",
            line.get("article_description") or "",
            conversion_factor,
            unit_value,
            total_value,
            is_manual_quantity,
            _text(payload.get("note")),
            int(order_row.get("next_order") or 1),
            actor_user_id,
        ],
    )
    return scan_id


@transaction.atomic
def add_purchase_scan(entry_id: str, line_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    _require_mutable(entry_id)
    line = _fetchone("SELECT * FROM bejerman_purchase_entry_lines WHERE id = %s AND entry_id = %s", [line_id, entry_id])
    if not line:
        raise BejermanPurchaseError("PURCHASE_LINE_NOT_FOUND", "Línea no encontrada", status_code=404)
    payload = payload or {}
    barcodes = _split_barcodes(payload)
    seen: set[str] = set()
    scan_ids = []
    if barcodes:
        for barcode in barcodes:
            scan_ids.append(_insert_scan(entry_id, line, payload, actor_user_id, barcode=barcode, seen=seen))
    else:
        scan_ids.append(_insert_scan(entry_id, line, payload, actor_user_id, barcode="", seen=seen))
    _recalculate_entry_totals(entry_id)
    _mark_draft_after_change(entry_id, actor_user_id)
    _event(entry_id, actor_user_id, "scan_added", metadata={"lineId": line_id, "scanIds": scan_ids})
    return get_purchase_entry(entry_id)


@transaction.atomic
def update_purchase_scan(entry_id: str, scan_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    _require_mutable(entry_id)
    scan = _fetchone("SELECT * FROM bejerman_purchase_entry_scans WHERE id = %s AND entry_id = %s", [scan_id, entry_id])
    if not scan:
        raise BejermanPurchaseError("PURCHASE_SCAN_NOT_FOUND", "Bulto no encontrado", status_code=404)
    payload = payload or {}
    barcode = _text(payload.get("barcode")) if "barcode" in payload else scan.get("barcode")
    norm = _barcode_norm(barcode)
    if norm and norm != scan.get("barcode_norm"):
        existing = _fetchone(
            "SELECT id FROM bejerman_purchase_entry_scans WHERE entry_id = %s AND barcode_norm = %s AND id <> %s",
            [entry_id, norm, scan_id],
        )
        if existing:
            raise BejermanPurchaseError("DUPLICATE_BARCODE", f"El código {barcode} ya está cargado en este lote", status_code=409)
    conversion_factor = (
        _positive_decimal(_get(payload, "conversionFactor", "quantity"), None)
        if "conversionFactor" in payload or "quantity" in payload
        else _qty(scan.get("conversion_factor"))
    )
    unit_value = (
        _nonnegative_money(_get(payload, "unitValue"), None)
        if "unitValue" in payload
        else _money(scan.get("unit_value"))
    )
    total_value = (conversion_factor * unit_value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    _execute(
        """
        UPDATE bejerman_purchase_entry_scans
           SET barcode = %s,
               barcode_norm = %s,
               conversion_factor = %s,
               unit_value = %s,
               total_value = %s,
               is_manual_quantity = %s,
               note = %s
         WHERE id = %s AND entry_id = %s
        """,
        [
            barcode or "",
            norm,
            conversion_factor,
            unit_value,
            total_value,
            not bool(barcode),
            _text(payload.get("note")) if "note" in payload else scan.get("note"),
            scan_id,
            entry_id,
        ],
    )
    _recalculate_entry_totals(entry_id)
    _mark_draft_after_change(entry_id, actor_user_id)
    _event(entry_id, actor_user_id, "scan_updated", metadata={"scanId": scan_id})
    return get_purchase_entry(entry_id)


@transaction.atomic
def delete_purchase_scan(entry_id: str, scan_id: str, actor_user_id: int | None) -> dict[str, Any]:
    _require_mutable(entry_id)
    scan = _fetchone("SELECT id FROM bejerman_purchase_entry_scans WHERE id = %s AND entry_id = %s", [scan_id, entry_id])
    if not scan:
        raise BejermanPurchaseError("PURCHASE_SCAN_NOT_FOUND", "Bulto no encontrado", status_code=404)
    _execute("DELETE FROM bejerman_purchase_entry_scans WHERE id = %s AND entry_id = %s", [scan_id, entry_id])
    _recalculate_entry_totals(entry_id)
    _mark_draft_after_change(entry_id, actor_user_id)
    _event(entry_id, actor_user_id, "scan_deleted", metadata={"scanId": scan_id})
    return get_purchase_entry(entry_id)


def _document_key(entry: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        _text(entry.get("supplierCode")).upper(),
        _text(entry.get("comprobanteTipo")).upper(),
        _text(entry.get("comprobanteLetra")).upper(),
        _text(entry.get("comprobantePtoVenta")).upper(),
        _text(entry.get("comprobanteNumero")).upper(),
    )


def _remote_purchase_duplicate(entry: dict[str, Any], client: BejermanSDKClient) -> dict[str, Any] | None:
    supplier_value = entry.get("supplierCodeRaw") or entry.get("supplierCode")
    filters = [
        bejerman_filter("Comprobante_Tipo", "IGUAL", entry.get("comprobanteTipo") or COMPROBANTE_TIPO),
        bejerman_filter("Proveedor_Codigo", "IGUAL", supplier_value),
        bejerman_filter("Comprobante_Numero", "IGUAL", entry.get("comprobanteNumero") or ""),
    ]
    response = client.list_comprobantes_compras(filters)
    wanted = _document_key(entry)
    for record in records_from_response(response):
        candidate = {
            "supplierCode": as_string(first_value(record, ("Proveedor_Codigo", "ProveedorCodigo", "CodProveedor"))),
            "comprobanteTipo": as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))),
            "comprobanteLetra": as_string(first_value(record, ("Comprobante_Letra", "Letra"))),
            "comprobantePtoVenta": as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta"))),
            "comprobanteNumero": as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante"))),
            "raw": record,
        }
        if _document_key(candidate) == wanted:
            return candidate
    return None


def validate_purchase_entry(
    entry_id: str,
    *,
    check_remote: bool = False,
    client: BejermanSDKClient | None = None,
    mark_validated: bool = False,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    entry = get_purchase_entry(entry_id)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def add_error(code: str, message: str, field: str = ""):
        errors.append({"code": code, "message": message, "field": field})

    if entry["companyKey"] != COMPANY_KEY:
        add_error("INVALID_COMPANY", "Este flujo solo admite SEPID", "companyKey")
    if entry["comprobanteTipo"] != COMPROBANTE_TIPO:
        add_error("INVALID_COMPROBANTE_TYPE", "Solo se permite RT en esta versión", "comprobanteTipo")
    if entry["operationCode"] != OPERATION_CODE:
        add_error("INVALID_OPERATION", "Solo se permite operación MC en esta versión", "operationCode")
    if not entry["supplierCode"]:
        add_error("SUPPLIER_REQUIRED", "Hay que seleccionar proveedor Bejerman", "supplierCode")
    if not entry["comprobanteLetra"]:
        add_error("LETTER_REQUIRED", "Hay que cargar la letra del RT", "comprobanteLetra")
    if not entry["comprobantePtoVenta"]:
        add_error("POINT_OF_SALE_REQUIRED", "Hay que cargar el punto de venta del RT", "comprobantePtoVenta")
    if not entry["comprobanteNumero"]:
        add_error("NUMBER_REQUIRED", "Hay que cargar el número del RT", "comprobanteNumero")
    if not entry["issueDate"]:
        add_error("ISSUE_DATE_REQUIRED", "Hay que cargar la fecha del RT", "issueDate")
    if not entry["lines"]:
        add_error("LINES_REQUIRED", "Hay que cargar al menos un artículo", "lines")

    seen_barcodes: set[str] = set()
    scan_count = 0
    for line in entry["lines"]:
        if not line["articleCode"]:
            add_error("ARTICLE_REQUIRED", "Cada línea debe tener artículo Bejerman", "articleCode")
        for scan in line["scans"]:
            scan_count += 1
            if not scan["articleCode"]:
                add_error("ARTICLE_REQUIRED", "Cada bulto debe tener artículo Bejerman", "articleCode")
            if _decimal(scan["conversionFactor"], Decimal("0")) <= 0:
                add_error("INVALID_QUANTITY", "La cantidad/factor debe ser mayor a cero", "conversionFactor")
            if _decimal(scan["unitValue"], Decimal("0")) <= 0:
                add_error("VALUE_REQUIRED", "Hay que cargar un valor unitario mayor a cero", "unitValue")
            norm = _barcode_norm(scan.get("barcode"))
            if norm:
                if norm in seen_barcodes:
                    add_error("DUPLICATE_BARCODE", f"El código {scan.get('barcode')} está repetido", "barcode")
                seen_barcodes.add(norm)
    if not scan_count:
        add_error("SCANS_REQUIRED", "Hay que escanear bultos o cargar una cantidad manual", "scans")

    if entry["supplierCode"] and entry["comprobanteNumero"]:
        duplicate = _fetchone(
            """
            SELECT id, remito_number
              FROM bejerman_purchase_entries
             WHERE company_key = %s
               AND supplier_code = %s
               AND comprobante_tipo = %s
               AND comprobante_letra = %s
               AND comprobante_pto_venta = %s
               AND comprobante_numero = %s
               AND status IN ('running', 'generated')
               AND id <> %s
             LIMIT 1
            """,
            [
                COMPANY_KEY,
                entry["supplierCode"],
                entry["comprobanteTipo"],
                entry["comprobanteLetra"],
                entry["comprobantePtoVenta"],
                entry["comprobanteNumero"],
                entry_id,
            ],
        )
        if duplicate:
            add_error("LOCAL_DUPLICATE", "Ya existe un RT generado localmente con ese proveedor y número", "comprobanteNumero")

    if check_remote and not errors:
        try:
            duplicate = _remote_purchase_duplicate(
                entry,
                client or BejermanSDKClient(company_key=COMPANY_KEY, actor_user_id=actor_user_id),
            )
        except Exception as exc:
            raise BejermanPurchaseError("REMOTE_DUPLICATE_CHECK_FAILED", str(exc), status_code=502) from exc
        if duplicate:
            add_error("REMOTE_DUPLICATE", "Bejerman ya registra un RT con ese proveedor y número", "comprobanteNumero")

    ok = not errors
    if ok and mark_validated and entry["status"] in MUTABLE_STATUSES:
        _execute(
            """
            UPDATE bejerman_purchase_entries
               SET status = 'validated',
                   updated_by_user_id = %s
             WHERE id = %s AND status IN ('draft', 'failed', 'validated')
            """,
            [actor_user_id, entry_id],
        )
        _event(entry_id, actor_user_id, "validated")
        entry = get_purchase_entry(entry_id)
    return {"ok": ok, "errors": errors, "warnings": warnings, "totals": {"quantity": entry["totalQuantity"], "value": entry["totalValue"]}, "entry": entry}


def _provider_fields(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry.get("supplierSnapshot") if isinstance(entry.get("supplierSnapshot"), dict) else {}
    return {
        "Proveedor_TipoDocumento": first_value(raw, ("Proveedor_TipoDocumento", "tipoDocumento", "TipoDocumento")),
        "Proveedor_Provincia": first_value(raw, ("Proveedor_Provincia", "provincia", "Provincia")),
        "Proveedor_SitIVA": first_value(raw, ("Proveedor_SitIVA", "sitIva", "SitIVA")),
        "Proveedor_NroDocumento": first_value(raw, ("Proveedor_NroDocumento", "cuit", "CUIT", "Proveedor_CUIT")),
        "Proveedor_NroIngresosBrutos": first_value(raw, ("Proveedor_NroIngresosBrutos", "ingresosBrutos", "IngresosBrutos")),
    }


def _item_common(entry: dict[str, Any], sort_order: int) -> dict[str, Any]:
    return {
        "Comprobante_Tipo": entry["comprobanteTipo"],
        "Comprobante_Letra": entry["comprobanteLetra"],
        "Comprobante_PtoVenta": entry["comprobantePtoVenta"],
        "Comprobante_Numero": entry["comprobanteNumero"],
        "Comprobante_LoteHasta": "",
        "Comprobante_FechaEmision": entry["issueDate"],
        "Proveedor_Codigo": entry["supplierCodeRaw"] or entry["supplierCode"],
        "Item_NumeroRenglon": f"{sort_order:>3}",
    }


def build_purchase_entry_comprobante(entry: dict[str, Any]) -> dict[str, Any]:
    issue_date = normalize_date(entry.get("issueDate")) or _today()
    accounting_date = normalize_date(entry.get("accountingDate")) or issue_date
    ddjj_date = normalize_date(entry.get("ddjjDate")) or issue_date
    items: list[dict[str, Any]] = []
    total = Decimal("0")
    sort_order = 1
    for line in entry.get("lines") or []:
        for scan in line.get("scans") or []:
            qty = _qty(scan.get("conversionFactor"))
            unit_value = _money(scan.get("unitValue"))
            item_total = (qty * unit_value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            total += item_total
            article_code = _text(scan.get("articleCode") or line.get("articleCode"))
            article_description = _text(scan.get("articleDescription") or line.get("articleDescription") or article_code)
            items.append(
                {
                    **_item_common({**entry, "issueDate": issue_date}, sort_order),
                    "Item_Tipo": "A",
                    "Item_CodigoArticulo": article_code,
                    "Item_CantidadUM1": float(qty),
                    "Item_CantidadUM2": 0,
                    "Item_DescripArticulo": article_description[:250],
                    "Item_PrecioUnitario": float(unit_value),
                    "Item_TasaIVAInscrip": 0,
                    "Item_TasaIVANoInscrip": 0,
                    "Item_ImporteIVAInscrip": 0,
                    "Item_ImporteIVANoInscrip": 0,
                    "Item_ImporteTotal": float(item_total),
                    "Item_ImporteDescComercial": 0,
                    "Item_ImporteDescFinanciero": 0,
                    "Item_ImporteDescGeneral": 0,
                    "Item_CodigoConceptoNoGravado": None,
                    "Item_ImporteIVANoGravado": 0,
                    "Item_TipoIVA": "1",
                    "Item_CodigoDescPorLinea": None,
                    "Item_ImporteDescPorLinea": 0,
                    "Item_Deposito": line.get("depositCode") or entry.get("depositCode") or DEFAULT_DEPOSIT,
                    "Item_Partida": scan.get("barcode") or " ",
                    "Item_TasaDescPorItem": 0,
                    "Item_Importe": float(item_total),
                    "Item_ImporteItem": float(item_total),
                    "Item_ImputacionCreditoFiscal": "1",
                    "Item_RubroCreditoFiscal": "0",
                    "Item_FechaEntrega": issue_date,
                    "Item_Kit": None,
                    "Item_RenglonKit": 0,
                    "Item_EsPromocion": None,
                    "Item_DatosAdicionales": None,
                }
            )
            sort_order += 1
    provider_fields = {key: value for key, value in _provider_fields(entry).items() if value not in (None, "")}
    notes = "\n".join(part for part in (_text(entry.get("notes")), f"NEXORA {entry.get('id')}") if part)
    comprobante = {
        "Comprobante_Tipo": entry["comprobanteTipo"],
        "Comprobante_Letra": entry["comprobanteLetra"],
        "Comprobante_PtoVenta": entry["comprobantePtoVenta"],
        "Comprobante_Numero": entry["comprobanteNumero"],
        "Comprobante_LoteHasta": "",
        "Comprobante_FechaEmision": issue_date,
        "Proveedor_Codigo": entry["supplierCodeRaw"] or entry["supplierCode"],
        "Proveedor_RazonSocial": entry.get("supplierName") or entry["supplierCode"],
        **provider_fields,
        "Comprobante_CondicionPago": entry.get("paymentTermCode"),
        "Comprobante_CodigoCausaEmision": None,
        "Comprobante_FechaVencimiento": issue_date,
        "Comprobante_ImporteTotal": float(total.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)),
        "Comprobante_Mensaje": notes[:250] or " ",
        "Comprobante_ActualizaStock": "S",
        "Comprobante_Moneda": as_string(getattr(settings, "BEJERMAN_PURCHASE_CURRENCY", "")),
        "Comprobante_TipoCambio": as_string(getattr(settings, "BEJERMAN_PURCHASE_EXCHANGE_TYPE", "")),
        "Comprobante_CotizacionCambio": float(getattr(settings, "BEJERMAN_PURCHASE_EXCHANGE_RATE", 0) or 0),
        "Comprobante_FechaContabilizacion": accounting_date,
        "Comprobante_FechaDDJJ": ddjj_date,
        "Comprobante_TipoOperacion": entry.get("operationCode") or OPERATION_CODE,
        "Comprobante_Items": items,
        "Comprobante_MediosPago": [],
        "Comprobante_RegEspeciales": [],
        "Comprobante_DatosAdicionales": None,
        "Comprobante_Cuotas": None,
    }
    return {
        "comprobante": comprobante,
        "profile": {
            "companyKey": entry.get("companyKey") or COMPANY_KEY,
            "type": entry.get("comprobanteTipo") or COMPROBANTE_TIPO,
            "operation": entry.get("operationCode") or OPERATION_CODE,
            "deposit": entry.get("depositCode") or DEFAULT_DEPOSIT,
        },
        "lineCount": len(items),
        "totalValue": float(total.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)),
    }


def _purchase_request_payload(comprobante: dict[str, Any], *, numera_flex: str, emite_reg: str) -> dict[str, Any]:
    return {
        "Circuito": "COMPRAS",
        "Operacion": "IngresarListaComprobantesJSON",
        "Parametros": None,
        "ParametrosJson": [_json_param([comprobante]), numera_flex, emite_reg],
        "Comprobante": comprobante,
    }


def emit_purchase_entry(entry_id: str, actor_user_id: int | None, client: BejermanSDKClient | None = None) -> dict[str, Any]:
    client = client or BejermanSDKClient(company_key=COMPANY_KEY, actor_user_id=actor_user_id)
    validation = validate_purchase_entry(entry_id, check_remote=True, client=client, actor_user_id=actor_user_id)
    if not validation["ok"]:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_INVALID",
            validation["errors"][0]["message"] if validation["errors"] else "El lote no es válido",
            status_code=409,
        )
    entry = validation["entry"]
    if entry.get("status") in FINAL_STATUSES:
        raise BejermanPurchaseError("PURCHASE_ENTRY_LOCKED", "El lote ya no se puede emitir", status_code=409)
    built = build_purchase_entry_comprobante(entry)
    numera_flex = as_string(getattr(settings, "BEJERMAN_PURCHASE_NUMERA_FLEX", "N")) or "N"
    emite_reg = as_string(getattr(settings, "BEJERMAN_PURCHASE_EMITE_REG", "R")) or "R"
    request_payload = _purchase_request_payload(built["comprobante"], numera_flex=numera_flex, emite_reg=emite_reg)
    try:
        with transaction.atomic():
            _execute(
                """
                UPDATE bejerman_purchase_entries
                   SET status = 'running',
                       request_payload = %s::jsonb,
                       response_payload = '{}'::jsonb,
                       last_error = NULL,
                       updated_by_user_id = %s
                 WHERE id = %s AND status IN ('draft', 'validated', 'failed')
                """,
                [_json_param(request_payload), actor_user_id, entry_id],
            )
            _event(entry_id, actor_user_id, "emission_started", metadata={"lineCount": built["lineCount"]})
    except IntegrityError as exc:
        raise BejermanPurchaseError("LOCAL_DUPLICATE", "Ya hay un RT local en emisión o generado con ese proveedor y número", status_code=409) from exc

    try:
        raw_response = client.ingresar_lista_comprobantes_compras_json(
            [built["comprobante"]],
            numera_flex=numera_flex,
            emite_reg=emite_reg,
        )
        parsed = parse_remito_response(raw_response)
        final_type = parsed.get("comprobanteTipo") or entry["comprobanteTipo"]
        final_letter = parsed.get("comprobanteLetra") or entry["comprobanteLetra"]
        final_point = parsed.get("comprobantePtoVenta") or entry["comprobantePtoVenta"]
        final_number = parsed.get("comprobanteNumero") or entry["comprobanteNumero"]
        remito_number = parsed.get("remitoNumber") or format_remito_number(final_type, final_letter, final_point, final_number)
        response_payload = {"parsed": parsed, "raw": raw_response, "profile": built["profile"], "lineCount": built["lineCount"]}
        with transaction.atomic():
            _execute(
                """
                UPDATE bejerman_purchase_entries
                   SET status = 'generated',
                       comprobante_tipo = %s,
                       comprobante_letra = %s,
                       comprobante_pto_venta = %s,
                       comprobante_numero = %s,
                       remito_number = %s,
                       response_payload = %s::jsonb,
                       generated_by_user_id = %s,
                       generated_at = CURRENT_TIMESTAMP,
                       updated_by_user_id = %s,
                       last_error = NULL
                 WHERE id = %s
                """,
                [
                    final_type,
                    final_letter,
                    final_point,
                    final_number,
                    remito_number,
                    _json_param(response_payload),
                    actor_user_id,
                    actor_user_id,
                    entry_id,
                ],
            )
            _event(entry_id, actor_user_id, "emission_generated", metadata={"remitoNumber": remito_number})
    except Exception as exc:
        with transaction.atomic():
            _execute(
                """
                UPDATE bejerman_purchase_entries
                   SET status = 'failed',
                       request_payload = %s::jsonb,
                       response_payload = %s::jsonb,
                       last_error = %s,
                       updated_by_user_id = %s
                 WHERE id = %s
                """,
                [_json_param(request_payload), _json_param({"error": str(exc)}), str(exc), actor_user_id, entry_id],
            )
            _event(entry_id, actor_user_id, "emission_failed", note=str(exc))
        raise BejermanPurchaseError("BEJERMAN_PURCHASE_EMIT_FAILED", str(exc), status_code=502) from exc
    return get_purchase_entry(entry_id, include_events=True)


def list_purchase_history(
    params: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    params = params or {}
    filters = [bejerman_filter("Comprobante_Tipo", "IGUAL", _text(params.get("tipo")) or COMPROBANTE_TIPO)]
    if _text(_get(params, "supplierCode", "proveedor")):
        filters.append(bejerman_filter("Proveedor_Codigo", "IGUAL", _text(_get(params, "supplierCode", "proveedor"))))
    if _text(_get(params, "dateFrom", "desde")):
        filters.append(bejerman_filter("Comprobante_FechaEmision", "MAYOR O IGUAL", _text(_get(params, "dateFrom", "desde"))))
    if _text(_get(params, "dateTo", "hasta")):
        filters.append(bejerman_filter("Comprobante_FechaEmision", "MENOR O IGUAL", _text(_get(params, "dateTo", "hasta"))))
    response = (client or BejermanSDKClient(company_key=COMPANY_KEY, actor_user_id=actor_user_id)).list_comprobantes_compras(filters)
    limit = max(1, min(200, int(_text(params.get("limit")) or 100)))
    items = []
    for record in records_from_response(response):
        operation = as_string(first_value(record, ("Comprobante_TipoOperacion", "TipoOperacion", "Tipo_Operacion")))
        if operation and operation.upper() != OPERATION_CODE:
            continue
        tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo")))
        letter = as_string(first_value(record, ("Comprobante_Letra", "Letra")))
        point = as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
        number = as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante")))
        items.append(
            {
                "comprobanteTipo": tipo,
                "comprobanteLetra": letter,
                "comprobantePtoVenta": point,
                "comprobanteNumero": number,
                "remitoNumber": format_remito_number(tipo, letter, point, number),
                "issueDate": normalize_date(first_value(record, ("Comprobante_FechaEmision", "FechaEmision"))),
                "supplierCode": as_string(first_value(record, ("Proveedor_Codigo", "ProveedorCodigo", "CodProveedor"))),
                "supplierName": as_string(first_value(record, ("Proveedor_RazonSocial", "RazonSocial", "Nombre"))),
                "operationCode": operation,
                "totalValue": first_value(record, ("Comprobante_ImporteTotal", "ImporteTotal", "Total")),
                "raw": record,
            }
        )
    return {"items": items[:limit], "companyKey": COMPANY_KEY}
