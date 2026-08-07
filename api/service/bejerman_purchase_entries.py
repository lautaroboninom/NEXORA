from __future__ import annotations

import json
import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .bejerman_companies import require_company
from .bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkConfigError,
    BejermanSdkError,
    as_nullable_bool,
    as_number,
    as_string,
    bejerman_filter,
    build_article_filters,
    build_articles_result,
    build_stock_rows,
    comprobante_id_of,
    first_value,
    format_remito_number,
    map_article,
    normalize_article_code,
    normalize_date,
    normalize_search,
    parse_remito_response,
    records_from_response,
)


COMPANY_KEY = "SEPID"
COMPROBANTE_TIPO = "RT"
OPERATION_CODE = "MC"
DEFAULT_DEPOSIT = "VAL"
DEFAULT_LETTER = "R"
MUTABLE_STATUSES = {"draft", "validated", "failed"}
FINAL_STATUSES = {"running", "verifying", "generated", "failed_review", "cancelled"}
QTY_QUANT = Decimal("0.0001")
MONEY_QUANT = Decimal("0.01")
POINT_OF_SALE_WIDTH = 5
DOCUMENT_NUMBER_WIDTH = 8
ACTIVE_DOCUMENT_STATUSES = {"running", "verifying", "generated", "failed_review"}
DEFAULT_RUNNING_GRACE_SECONDS = 120
DEFAULT_MAX_BULK_SCANS = 5000


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


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(_text(value) or default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _cache_seconds(setting_name: str, default: int) -> int:
    return _bounded_int(
        getattr(settings, setting_name, default),
        default=default,
        minimum=1,
        maximum=600,
    )


def _purchase_environment() -> str:
    wsdl_url = _text(getattr(settings, "BEJERMAN_WSDL_URL", "")).upper()
    if not wsdl_url:
        return "NO_CONFIGURADO"
    return "TEST" if "TEST" in wsdl_url else "PRODUCCION"


def _resolve_purchase_company(params: dict[str, Any]):
    return require_company(_get(params, "companyKey", "company_key"), default=COMPANY_KEY)


def _purchase_emission_supported_companies() -> set[str]:
    # El depósito de compras (DEFAULT_DEPOSIT) sólo está confirmado para SEPID.
    # Buscar/citar/autocompletar OC funciona para cualquier empresa; emitir el
    # RT queda bloqueado acá hasta confirmar el depósito real de cada una.
    configured = {
        code.strip().upper()
        for code in as_string(
            getattr(settings, "BEJERMAN_PURCHASE_EMISSION_COMPANIES", "")
        ).split(",")
        if code.strip()
    }
    return configured or {COMPANY_KEY}


def _purchase_emission_enabled() -> bool:
    return as_nullable_bool(
        getattr(settings, "BEJERMAN_PURCHASE_EMISSION_ENABLED", False)
    ) is True


def _purchase_quantity_mismatch_threshold() -> Decimal:
    raw = getattr(settings, "BEJERMAN_PURCHASE_QUANTITY_MISMATCH_THRESHOLD", "0.2")
    try:
        threshold = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        threshold = Decimal("0.2")
    if threshold < 0:
        threshold = Decimal("0.2")
    return threshold


def _purchase_order_quantity_mismatches(entry: dict[str, Any]) -> list[dict[str, Any]]:
    # expectedQuantity sólo se completa al autocompletar renglones desde una
    # OC citada (populate_purchase_entry_lines_from_order); en carga manual
    # queda None y esta línea no participa del chequeo.
    threshold = _purchase_quantity_mismatch_threshold()
    mismatches: list[dict[str, Any]] = []
    for line in entry.get("lines") or []:
        expected = line.get("expectedQuantity")
        if expected is None:
            continue
        expected_decimal = _decimal(expected, Decimal("0")) or Decimal("0")
        if expected_decimal <= 0:
            continue
        total_decimal = _decimal(line.get("totalQuantity"), Decimal("0")) or Decimal("0")
        relative_diff = abs(total_decimal - expected_decimal) / expected_decimal
        if relative_diff <= threshold:
            continue
        article_code = line.get("articleCode") or "-"
        mismatches.append(
            {
                "code": "PURCHASE_ORDER_QUANTITY_MISMATCH",
                "field": "lines",
                "articleCode": article_code,
                "expectedQuantity": _public_decimal(expected_decimal, "0.0001"),
                "totalQuantity": _public_decimal(total_decimal, "0.0001"),
                "message": (
                    f"El artículo {article_code} tiene {_public_decimal(total_decimal, '0.0001')} escaneado "
                    f"contra {_public_decimal(expected_decimal, '0.0001')} pedido en la OC citada"
                ),
            }
        )
    return mismatches


def _purchase_deposit_code(value: Any = None) -> str:
    deposit_code = _text(value or DEFAULT_DEPOSIT).upper()
    if deposit_code != DEFAULT_DEPOSIT:
        raise BejermanPurchaseError(
            "PURCHASE_DEPOSIT_NOT_ALLOWED",
            f"Este circuito sólo admite el depósito {DEFAULT_DEPOSIT}",
            status_code=409,
        )
    return DEFAULT_DEPOSIT


def _catalog_cache_key(kind: str, actor_user_id: int | None) -> str:
    actor_key = str(int(actor_user_id)) if actor_user_id else "anonymous"
    endpoint_scope = "|".join(
        (
            _text(getattr(settings, "BEJERMAN_WSDL_URL", "")).lower(),
            _text(getattr(settings, "BEJERMAN_COMPANY_SEPID", "")).upper(),
        )
    )
    endpoint_hash = hashlib.sha256(
        endpoint_scope.encode("utf-8")
    ).hexdigest()[:16]
    return (
        f"nexora:bejerman:purchase:{COMPANY_KEY}:"
        f"{endpoint_hash}:{kind}:{actor_key}"
    )


def _cached_sdk_response(
    kind: str,
    actor_user_id: int | None,
    loader,
    *,
    setting_name: str,
    default_seconds: int,
) -> dict[str, Any]:
    cache_key = _catalog_cache_key(kind, actor_user_id)
    response = cache.get(cache_key)
    if response is None:
        response = loader()
        cache.set(
            cache_key,
            response,
            _cache_seconds(setting_name, default_seconds),
        )
    return response


def _normalize_letter(value: Any, *, default: str = "") -> str:
    text = _text(value).upper() or default
    if text and not re.fullmatch(r"[A-Z]", text):
        raise BejermanPurchaseError("INVALID_DOCUMENT_LETTER", "La letra del RT debe tener un solo carácter")
    return text


def _normalize_document_digits(value: Any, *, width: int, field: str, label: str) -> str:
    text = _text(value)
    if not text:
        return ""
    if not text.isdigit() or len(text) > width:
        raise BejermanPurchaseError(
            f"INVALID_{field.upper()}",
            f"{label} debe contener hasta {width} dígitos",
        )
    return text.zfill(width)


def _canonical_document_digits(value: Any, width: int) -> str:
    text = _text(value)
    return text.zfill(width) if text.isdigit() and len(text) <= width else text.upper()


def _request_fingerprint(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _article_query_cache_kind(
    filters: list[dict[str, Any]] | None,
    limit: int,
) -> str:
    fingerprint = _request_fingerprint(
        {
            "filters": filters or [],
            "limit": int(limit),
        }
    )
    return f"articles:{fingerprint}"


def _normalized_error_text(value: Any) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", _text(value))
        if not unicodedata.combining(char)
    ).lower()


def _confirms_not_imported(exc: Exception) -> bool:
    text = _normalized_error_text(exc)
    return any(
        marker in text
        for marker in (
            "no se importo",
            "no fue importado",
            "no ha sido importado",
        )
    )


def _ambiguous_emit_error(exc: Exception) -> bool:
    return not isinstance(exc, BejermanSdkConfigError) and not _confirms_not_imported(exc)


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


def _positive_money(value: Any, default: Decimal | None = None) -> Decimal:
    parsed = _nonnegative_money(value, default)
    if parsed <= 0:
        raise BejermanPurchaseError(
            "VALUE_REQUIRED",
            "Hay que cargar un valor unitario mayor a cero antes de ingresar mercadería",
        )
    return parsed


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


def _require_entry(entry_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock_sql = " FOR UPDATE" if for_update else ""
    row = _fetchone(f"SELECT * FROM bejerman_purchase_entries WHERE id = %s{lock_sql}", [entry_id])
    if not row:
        raise BejermanPurchaseError("PURCHASE_ENTRY_NOT_FOUND", "Ingreso de mercadería no encontrado", status_code=404)
    return row


def _require_mutable(entry_id: str) -> dict[str, Any]:
    row = _require_entry(entry_id, for_update=True)
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
        "partidaExpirationDate": _iso(row.get("partida_expiration_date")),
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
        "expectedQuantity": (
            _public_decimal(row.get("expected_quantity"), "0.0001")
            if row.get("expected_quantity") is not None
            else None
        ),
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
        "comprobanteTipoOrigen": row.get("comprobante_tipo_origen") or "",
        "comprobanteLetraOrigen": row.get("comprobante_letra_origen") or "",
        "comprobantePtoVentaOrigen": row.get("comprobante_pto_venta_origen") or "",
        "comprobanteNumeroOrigen": row.get("comprobante_numero_origen") or "",
        "comprobanteOrigenLabel": (
            format_remito_number(
                row.get("comprobante_tipo_origen"),
                row.get("comprobante_letra_origen"),
                row.get("comprobante_pto_venta_origen"),
                row.get("comprobante_numero_origen"),
            )
            if row.get("comprobante_numero_origen")
            else ""
        ),
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
        "attemptCount": int(row.get("attempt_count") or 0),
        "lastAttemptAt": _iso(row.get("last_attempt_at")),
        "requestFingerprint": row.get("request_fingerprint") or "",
        "bejermanEnvironment": _purchase_environment(),
        "emissionEnabled": _purchase_emission_enabled(),
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
    cuit = as_string(
        first_value(
            record,
            (
                "Proveedor_NroDocumento",
                "Proveedor_NumeroDocumento",
                "Proveedor_CUIT",
                "CUIT",
                "Cuit",
                "NroDocumento",
            ),
        )
    )
    if not code and not name:
        return None
    return {
        "id": code or name,
        "code": code,
        "rawCode": raw_code or code,
        "name": name or code,
        "cuit": cuit,
        "paymentTermCode": as_string(
            first_value(
                record,
                (
                    "Proveedor_CondicionPagoCodigo",
                    "Comprobante_CondicionPago",
                    "Proveedor_CondicionPago",
                    "CondicionPago",
                ),
            )
        ),
        "tipoDocumento": first_value(
            record,
            ("Proveedor_TipoDocumentoCodigo", "Proveedor_TipoDocumento", "TipoDocumento"),
        ),
        "provincia": as_string(
            first_value(
                record,
                ("Proveedor_ProvinciaCodigo", "Proveedor_Provincia", "Provincia"),
            )
        ),
        "sitIva": as_string(
            first_value(
                record,
                ("Proveedor_SituacionIVACodigo", "Proveedor_SitIVA", "SitIVA"),
            )
        ),
        "ingresosBrutos": as_string(
            first_value(
                record,
                (
                    "Proveedor_NumeroIIBB",
                    "Proveedor_NroIngresosBrutos",
                    "Proveedor_NumeroIngresosBrutos",
                    "IngresosBrutos",
                ),
            )
        ),
        "enabled": as_nullable_bool(first_value(record, ("Proveedor_Habilitado", "Habilitado", "enabled"))),
        "raw": record,
    }


def _supplier_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    provider = _get(payload, "provider", "supplier")
    provider = provider if isinstance(provider, dict) else {}
    raw_code = _raw_text(_get(payload, "supplierCodeRaw", "providerCodeRaw") or provider.get("rawCode") or provider.get("Proveedor_Codigo"))
    code = _text(_get(payload, "supplierCode", "providerCode") or provider.get("code") or raw_code)
    name = _text(_get(payload, "supplierName", "providerName") or provider.get("name") or provider.get("Proveedor_RazonSocial"))
    cuit = _text(
        _get(payload, "supplierTaxId", "providerTaxId")
        or provider.get("cuit")
        or provider.get("Proveedor_NroDocumento")
        or provider.get("Proveedor_NumeroDocumento")
    )
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
            or provider.get("Proveedor_CondicionPagoCodigo")
            or provider.get("Comprobante_CondicionPago")
        ),
    }


def _purchase_article_fields(article: dict[str, Any]) -> dict[str, Any]:
    raw = article.get("raw") if isinstance(article.get("raw"), dict) else {}
    return {
        **article,
        "purchaseEnabled": as_nullable_bool(
            first_value(raw, ("Art_ParticipaCircuitoCompras", "purchaseEnabled"))
        ),
        "controlsStock": as_nullable_bool(
            first_value(raw, ("Art_ControlaStock", "controlsStock"))
        ),
        "stockByPartida": as_nullable_bool(
            first_value(raw, ("Art_StockPorPartida", "stockByPartida"))
        ),
        "stockEnabled": as_nullable_bool(
            first_value(raw, ("Art_ParticipaCircuitoStock", "stockEnabled"))
        ),
        "requiresPartida": as_nullable_bool(
            first_value(raw, ("Art_StockPorPartida", "requiresPartida", "stockByPartida"))
        ),
        "partidaMask": as_string(
            first_value(raw, ("Art_MascaraPartida", "partidaMask"))
        ),
        "purchaseVatType": as_string(
            first_value(raw, ("Art_TipoTasaCompras", "purchaseVatType"))
        ),
        "purchaseVatCode": as_string(
            first_value(raw, ("Art_CodTasaIVACompras", "purchaseVatCode"))
        ),
        "taxCreditAllocation": as_string(
            first_value(raw, ("Art_ImpCreditoFiscal", "taxCreditAllocation"))
        ),
        "taxCreditCategory": as_string(
            first_value(raw, ("Art_CodRubroCreditoFiscal", "taxCreditCategory"))
        ),
        "defaultDepositCode": as_string(
            first_value(raw, ("Art_CodDeposito", "defaultDepositCode"))
        ),
    }


def _purchase_articles_from_response(response: dict[str, Any], *, search: str = "", limit: int = 50) -> list[dict[str, Any]]:
    result = build_articles_result(response, {"search": search, "limit": limit})
    return [_purchase_article_fields(item) for item in result.get("items") or []]


def list_purchase_providers(
    params: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    params = params or {}
    company = _resolve_purchase_company(params)
    search = _text(_get(params, "q", "search")).lower()
    limit = _bounded_int(params.get("limit"), default=30, minimum=1, maximum=100)
    sdk_client = client or BejermanSDKClient(company_key=company.key, actor_user_id=actor_user_id)
    response = (
        sdk_client.list_proveedores()
        if client is not None
        else _cached_sdk_response(
            f"providers:{company.key}",
            actor_user_id,
            sdk_client.list_proveedores,
            setting_name="BEJERMAN_PURCHASE_PROVIDER_CACHE_SECONDS",
            default_seconds=60,
        )
    )
    items = [item for item in (_map_provider(record) for record in records_from_response(response)) if item]
    if search:
        items = [
            item
            for item in items
            if search in f"{item.get('code')} {item.get('rawCode')} {item.get('name')} {item.get('cuit')}".lower()
        ]
    return {
        "items": items[:limit],
        "companyKey": company.key,
        "bejermanEnvironment": _purchase_environment(),
        "emissionEnabled": _purchase_emission_enabled(),
    }


def list_purchase_articles(
    params: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    params = params or {}
    company = _resolve_purchase_company(params)
    search = _text(_get(params, "q", "search"))
    limit = _bounded_int(params.get("limit"), default=20, minimum=1, maximum=50)
    if search and len(search) < 2:
        result = build_articles_result({"DatosJSON": []}, {"search": search, "limit": limit})
        result["companyKey"] = company.key
        result["bejermanEnvironment"] = _purchase_environment()
        result["emissionEnabled"] = _purchase_emission_enabled()
        return result
    filters = build_article_filters(search, _get(params, "field"))
    sdk_client = client or BejermanSDKClient(company_key=company.key, actor_user_id=actor_user_id)
    response = (
        sdk_client.list_articulos(filters, limit)
        if client is not None
        else _cached_sdk_response(
            f"{_article_query_cache_kind(filters, limit)}:{company.key}",
            actor_user_id,
            lambda: sdk_client.list_articulos(filters, limit),
            setting_name="BEJERMAN_PURCHASE_CATALOG_CACHE_SECONDS",
            default_seconds=30,
        )
    )
    result = build_articles_result(response, {"search": search, "limit": limit})
    result["items"] = [_purchase_article_fields(item) for item in result.get("items") or []]
    result["companyKey"] = company.key
    result["bejermanEnvironment"] = _purchase_environment()
    result["emissionEnabled"] = _purchase_emission_enabled()
    return result


def _purchase_order_matches_query(item: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = normalize_search(f"{item.get('label')} {item.get('supplierName')}")
    needle = normalize_search(query)
    if needle and needle in haystack:
        return True
    query_digits = re.sub(r"\D", "", query).lstrip("0")
    number_digits = re.sub(r"\D", "", str(item.get("comprobanteNumero") or "")).lstrip("0")
    return bool(query_digits) and query_digits == number_digits


def list_purchase_orders(
    params: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    params = params or {}
    company = _resolve_purchase_company(params)
    # OJO: no usar _text() acá — Bejerman guarda Proveedor_Codigo con padding de
    # espacios (ej. "   DCD") y el filtro Proveedor_Codigo IGUAL es exacto, así
    # que hay que mandar el código tal cual vino (supplierCodeRaw del proveedor
    # elegido), sin recortar los espacios.
    supplier_code = _raw_text(_get(params, "supplierCode", "proveedor"))
    if not supplier_code.strip():
        raise BejermanPurchaseError(
            "PURCHASE_ORDER_SUPPLIER_REQUIRED",
            "Elegí un proveedor antes de buscar sus órdenes de compra",
        )
    query = _text(_get(params, "q", "search"))
    limit = _bounded_int(params.get("limit"), default=30, minimum=1, maximum=100)
    sdk_client = client or BejermanSDKClient(company_key=company.key, actor_user_id=actor_user_id)
    # Esta instalación de Bejerman no siempre respeta filtros de texto del lado
    # servidor (ver list_articulos): traemos todas las OC del proveedor (cacheadas)
    # y filtramos/ordenamos acá, igual que se hace con el catálogo de artículos.
    response = (
        sdk_client.list_ordenes_compra(supplier_code)
        if client is not None
        else _cached_sdk_response(
            f"orders:{company.key}:{supplier_code}",
            actor_user_id,
            lambda: sdk_client.list_ordenes_compra(supplier_code),
            setting_name="BEJERMAN_PURCHASE_ORDER_CACHE_SECONDS",
            default_seconds=60,
        )
    )
    items = []
    for record in records_from_response(response):
        tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo")))
        if tipo.upper() != "OC":
            continue
        letter = as_string(first_value(record, ("Comprobante_Letra", "Letra")))
        point = as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
        number = as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante")))
        items.append(
            {
                "comprobanteId": comprobante_id_of(record),
                "comprobanteTipo": tipo,
                "comprobanteLetra": letter,
                "comprobantePtoVenta": point,
                "comprobanteNumero": number,
                "label": format_remito_number(tipo, letter, point, number),
                "issueDate": normalize_date(first_value(record, ("Comprobante_FechaEmision", "FechaEmision"))),
                "supplierCode": as_string(first_value(record, ("Proveedor_Codigo", "ProveedorCodigo", "CodProveedor"))),
                "supplierName": as_string(first_value(record, ("Proveedor_RazonSocial", "RazonSocial", "Nombre"))),
                "totalValue": first_value(record, ("Comprobante_ImporteTotal", "ImporteTotal", "Total")),
            }
        )
    if query:
        items = [item for item in items if _purchase_order_matches_query(item, query)]
    items.sort(key=lambda item: item.get("issueDate") or "", reverse=True)
    return {
        "items": items[:limit],
        "companyKey": company.key,
        "bejermanEnvironment": _purchase_environment(),
        "emissionEnabled": _purchase_emission_enabled(),
    }


def list_purchase_entries(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    company = _resolve_purchase_company(params)
    where = ["company_key = %s"]
    sql_params: list[Any] = [company.key]
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
    return {
        "items": [
            _serialize_entry(row, include_lines=False, include_payloads=False)
            for row in rows
        ],
        "bejermanEnvironment": _purchase_environment(),
        "emissionEnabled": _purchase_emission_enabled(),
    }


def _purchase_origin_fields(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    numero = _normalize_document_digits(
        _get(payload, "comprobanteNumeroOrigen"),
        width=DOCUMENT_NUMBER_WIDTH,
        field="origin_document_number",
        label="El número de la orden de compra citada",
    )
    if not numero:
        return "", "", "", ""
    tipo = _text(_get(payload, "comprobanteTipoOrigen")).upper() or "OC"
    if tipo != "OC":
        raise BejermanPurchaseError(
            "INVALID_PURCHASE_ORDER_TYPE",
            "Sólo se admite citar órdenes de compra (OC)",
        )
    letra = _normalize_letter(_get(payload, "comprobanteLetraOrigen"))
    pto_venta = _normalize_document_digits(
        _get(payload, "comprobantePtoVentaOrigen"),
        width=POINT_OF_SALE_WIDTH,
        field="origin_point_of_sale",
        label="El punto de venta de la orden de compra citada",
    )
    return tipo, letra, pto_venta, numero


@transaction.atomic
def create_purchase_entry(payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    payload = payload or {}
    company = _resolve_purchase_company(payload)
    supplier = _supplier_from_payload(payload)
    issue_date = normalize_date(_get(payload, "issueDate", "fecha")) or _today()
    origin_tipo, origin_letra, origin_pto_venta, origin_numero = _purchase_origin_fields(payload)
    entry_id = _uuid("bpe")
    _execute(
        """
        INSERT INTO bejerman_purchase_entries (
          id, company_key, status, comprobante_tipo, comprobante_letra,
          comprobante_pto_venta, comprobante_numero,
          comprobante_tipo_origen, comprobante_letra_origen,
          comprobante_pto_venta_origen, comprobante_numero_origen,
          supplier_code, supplier_code_raw,
          supplier_name, supplier_tax_id, supplier_snapshot, payment_term_code,
          issue_date, accounting_date, ddjj_date, operation_code, deposit_code,
          notes, created_by_user_id, updated_by_user_id
        )
        VALUES (
          %s, %s, 'draft', %s, %s,
          %s, %s,
          %s, %s,
          %s, %s,
          %s, %s,
          %s, %s, %s::jsonb, %s,
          %s, %s, %s, %s, %s,
          %s, %s, %s
        )
        """,
        [
            entry_id,
            company.key,
            COMPROBANTE_TIPO,
            _normalize_letter(_get(payload, "comprobanteLetra", "letter"), default=DEFAULT_LETTER),
            _normalize_document_digits(
                _get(payload, "comprobantePtoVenta", "pointOfSale"),
                width=POINT_OF_SALE_WIDTH,
                field="point_of_sale",
                label="El punto de venta",
            ),
            _normalize_document_digits(
                _get(payload, "comprobanteNumero", "number"),
                width=DOCUMENT_NUMBER_WIDTH,
                field="document_number",
                label="El número del RT",
            ),
            origin_tipo,
            origin_letra,
            origin_pto_venta,
            origin_numero,
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
            _purchase_deposit_code(_get(payload, "depositCode")),
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
    supplier_object = _get(payload, "supplier", "provider")
    supplier_snapshot = (
        supplier["snapshot"]
        if isinstance(supplier_object, dict)
        else _json_value(current.get("supplier_snapshot"), {})
    )
    issue_date = normalize_date(_get(payload, "issueDate")) if "issueDate" in payload else current.get("issue_date")
    accounting_date = normalize_date(_get(payload, "accountingDate")) if "accountingDate" in payload else current.get("accounting_date")
    ddjj_date = normalize_date(_get(payload, "ddjjDate")) if "ddjjDate" in payload else current.get("ddjj_date")
    origin_keys = (
        "comprobanteTipoOrigen",
        "comprobanteLetraOrigen",
        "comprobantePtoVentaOrigen",
        "comprobanteNumeroOrigen",
    )
    if any(key in payload for key in origin_keys):
        origin_tipo, origin_letra, origin_pto_venta, origin_numero = _purchase_origin_fields(payload)
    else:
        origin_tipo = current.get("comprobante_tipo_origen") or ""
        origin_letra = current.get("comprobante_letra_origen") or ""
        origin_pto_venta = current.get("comprobante_pto_venta_origen") or ""
        origin_numero = current.get("comprobante_numero_origen") or ""
    _execute(
        """
        UPDATE bejerman_purchase_entries
           SET comprobante_letra = %s,
               comprobante_pto_venta = %s,
               comprobante_numero = %s,
               comprobante_tipo_origen = %s,
               comprobante_letra_origen = %s,
               comprobante_pto_venta_origen = %s,
               comprobante_numero_origen = %s,
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
            (
                _normalize_letter(_get(payload, "comprobanteLetra"), default=DEFAULT_LETTER)
                if "comprobanteLetra" in payload
                else current.get("comprobante_letra") or DEFAULT_LETTER
            ),
            (
                _normalize_document_digits(
                    _get(payload, "comprobantePtoVenta"),
                    width=POINT_OF_SALE_WIDTH,
                    field="point_of_sale",
                    label="El punto de venta",
                )
                if "comprobantePtoVenta" in payload
                else current.get("comprobante_pto_venta")
            ),
            (
                _normalize_document_digits(
                    _get(payload, "comprobanteNumero"),
                    width=DOCUMENT_NUMBER_WIDTH,
                    field="document_number",
                    label="El número del RT",
                )
                if "comprobanteNumero" in payload
                else current.get("comprobante_numero")
            ),
            origin_tipo,
            origin_letra,
            origin_pto_venta,
            origin_numero,
            supplier["code"] if use_supplier else current.get("supplier_code"),
            supplier["rawCode"] if use_supplier else current.get("supplier_code_raw"),
            supplier["name"] if use_supplier else current.get("supplier_name"),
            supplier["taxId"] if use_supplier else current.get("supplier_tax_id"),
            _json_param(supplier_snapshot if use_supplier else _json_value(current.get("supplier_snapshot"), {})),
            _text(_get(payload, "paymentTermCode")) if "paymentTermCode" in payload else current.get("payment_term_code"),
            issue_date,
            accounting_date,
            ddjj_date,
            (
                _purchase_deposit_code(_get(payload, "depositCode"))
                if "depositCode" in payload
                else _purchase_deposit_code(current.get("deposit_code"))
            ),
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
    row = _require_entry(entry_id, for_update=True)
    if row.get("status") in {"running", "verifying", "generated", "failed_review"}:
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


def _insert_purchase_entry_line(
    entry_id: str,
    *,
    article_code: str,
    article_description: str,
    conversion_factor: Decimal,
    unit_value: Decimal,
    deposit_code: str,
    expected_quantity: Decimal | None,
    sort_order: int,
) -> str:
    line_id = _uuid("bpl")
    _execute(
        """
        INSERT INTO bejerman_purchase_entry_lines (
          id, entry_id, article_code, article_description, default_conversion_factor,
          default_unit_value, deposit_code, expected_quantity, sort_order
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            line_id,
            entry_id,
            article_code,
            article_description,
            conversion_factor,
            unit_value,
            deposit_code,
            expected_quantity,
            sort_order,
        ],
    )
    return line_id


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
    line_id = _insert_purchase_entry_line(
        entry_id,
        article_code=article_code,
        article_description=article_description,
        conversion_factor=default_factor,
        unit_value=default_unit_value,
        deposit_code=_purchase_deposit_code(_get(payload, "depositCode") or entry.get("deposit_code")),
        expected_quantity=None,
        sort_order=int(row.get("next_order") or 1),
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
    deposit_code = _purchase_deposit_code(
        _get(payload, "depositCode")
        if "depositCode" in payload
        else line.get("deposit_code")
    )
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


def _purchase_order_matching_record(
    sdk_client: BejermanSDKClient,
    actor_user_id: int | None,
    *,
    company_key: str,
    supplier_code: str,
    tipo: str,
    letra: str,
    pto_venta: str,
    numero: str,
) -> dict[str, Any] | None:
    response = _cached_sdk_response(
        f"orders:{company_key}:{supplier_code}",
        actor_user_id,
        lambda: sdk_client.list_ordenes_compra(supplier_code),
        setting_name="BEJERMAN_PURCHASE_ORDER_CACHE_SECONDS",
        default_seconds=60,
    )
    for record in records_from_response(response):
        record_tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))).upper()
        if record_tipo != tipo.upper():
            continue
        record_letra = as_string(first_value(record, ("Comprobante_Letra", "Letra")))
        record_pto = as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
        record_numero = as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante")))
        if record_letra == letra and record_pto == pto_venta and record_numero == numero:
            return record
    return None


def _purchase_order_detail_items(detail_response: dict[str, Any]) -> list[dict[str, Any]]:
    raw = detail_response.get("DatosJSON") if isinstance(detail_response, dict) else detail_response
    items = _purchase_item_list(raw)
    article_keys = ("Item_CodigoArticulo", "Articulo_Codigo", "ArticuloCodigo", "CodigoArticulo")
    if items and any(first_value(item, article_keys) for item in items):
        return items
    # Bejerman puede envolver el comprobante en una lista de un elemento en vez
    # de devolver los ítems directo; en ese caso hay que bajar un nivel más
    # hasta Comprobante_Items (mismo patrón que _purchase_document_evidence).
    wrapped = detail_response if isinstance(detail_response, dict) else {"DatosJSON": raw}
    for record in records_from_response(wrapped):
        nested = _purchase_item_list(first_value(record, ("Comprobante_Items", "Items", "ComprobanteItems")))
        if nested:
            return nested
    return items


def populate_purchase_entry_lines_from_order(
    entry_id: str,
    actor_user_id: int | None,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    # Wrapper sin @transaction.atomic a propósito: si Bejerman falla al traer
    # el detalle de la OC, necesitamos que el evento de auditoría de abajo
    # sobreviva aunque _populate_purchase_entry_lines_from_order_atomic haga
    # rollback de su propio savepoint (si el _event fuera parte de ese mismo
    # bloque atómico, el rollback también se lo llevaría puesto).
    try:
        return _populate_purchase_entry_lines_from_order_atomic(entry_id, actor_user_id, client)
    except BejermanPurchaseError as exc:
        if exc.code == "PURCHASE_ORDER_DETAIL_UNAVAILABLE":
            _event(
                entry_id,
                actor_user_id,
                "order_detail_unavailable",
                note=str(exc.__cause__ or exc),
                metadata={
                    "comprobanteId": getattr(exc, "comprobante_id", ""),
                    "comprobanteNumeroOrigen": getattr(exc, "comprobante_numero_origen", ""),
                },
            )
        raise


@transaction.atomic
def _populate_purchase_entry_lines_from_order_atomic(
    entry_id: str,
    actor_user_id: int | None,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    entry = _require_mutable(entry_id)
    origin_numero = _text(entry.get("comprobante_numero_origen"))
    if not origin_numero:
        raise BejermanPurchaseError(
            "PURCHASE_ORDER_NOT_CITED",
            "Primero citá una orden de compra para poder autocompletar sus artículos",
        )
    # Igual que en list_purchase_orders: mandar el código de proveedor tal cual
    # está guardado (con su padding de espacios), no la versión recortada.
    supplier_code = _raw_text(entry.get("supplier_code_raw") or entry.get("supplier_code"))
    if not supplier_code.strip():
        raise BejermanPurchaseError(
            "PURCHASE_ORDER_SUPPLIER_REQUIRED",
            "El lote no tiene proveedor asignado",
        )
    company_key = entry.get("company_key") or COMPANY_KEY
    sdk_client = client or BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)

    order_record = _purchase_order_matching_record(
        sdk_client,
        actor_user_id,
        company_key=company_key,
        supplier_code=supplier_code,
        tipo=_text(entry.get("comprobante_tipo_origen")) or "OC",
        letra=_text(entry.get("comprobante_letra_origen")),
        pto_venta=_text(entry.get("comprobante_pto_venta_origen")),
        numero=origin_numero,
    )
    if not order_record:
        raise BejermanPurchaseError(
            "PURCHASE_ORDER_NOT_FOUND",
            "No se encontró la orden de compra citada en Bejerman para traer sus artículos",
            status_code=409,
        )
    comprobante_id = comprobante_id_of(order_record)

    # WSListarComprobantesJSON (list_ordenes_compra) ya trae los renglones
    # embebidos en Comprobante_Items de cada OC. Usarlos directo evita depender
    # de DetalleComprobanteJSON, que en la práctica puede tirar una excepción
    # cruda del lado servidor de Bejerman para alguna OC puntual (dato
    # corrupto en su base) aunque la propia OC liste bien. Sólo si el listado
    # no trae items embebidos (por si alguna instalación/circuito no los
    # incluye) se recurre al endpoint de detalle como respaldo.
    raw_items = _purchase_item_list(order_record.get("Comprobante_Items"))
    if not raw_items:
        try:
            detail_response = sdk_client.detalle_comprobante_compras(comprobante_id)
        except BejermanSdkError as exc:
            error = BejermanPurchaseError(
                "PURCHASE_ORDER_DETAIL_UNAVAILABLE",
                f"Bejerman no pudo devolver el detalle de esta OC ({exc}); probá con otra OC o avisá a soporte Bejerman",
                status_code=502,
            )
            error.comprobante_id = comprobante_id
            error.comprobante_numero_origen = origin_numero
            raise error from exc
        raw_items = _purchase_order_detail_items(detail_response)
    if not raw_items:
        raise BejermanPurchaseError(
            "PURCHASE_ORDER_WITHOUT_ITEMS",
            "La orden de compra citada no tiene artículos para autocompletar",
            status_code=409,
        )

    descriptions: dict[str, str] = {}
    unit_values: dict[str, Decimal] = {}
    for item in raw_items:
        article_code = as_string(
            first_value(item, ("Item_CodigoArticulo", "Articulo_Codigo", "ArticuloCodigo", "CodigoArticulo"))
        )
        key = normalize_article_code(article_code)
        if not key:
            continue
        description = as_string(
            first_value(item, ("Item_DescripArticulo", "Item_Descripcion", "Articulo_Descripcion", "Descripcion"))
        )
        if description:
            descriptions.setdefault(key, description)
        direct_unit_value = _decimal(
            first_value(item, ("Item_PrecioUnitario", "Item_ValorUnitario", "PrecioUnitario"))
        )
        if direct_unit_value is not None and key not in unit_values:
            unit_values[key] = direct_unit_value

    groups = _purchase_item_groups(raw_items)
    row = _fetchone(
        "SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM bejerman_purchase_entry_lines WHERE entry_id = %s",
        [entry_id],
    )
    next_order = int(row.get("max_order") or 0)
    deposit_code = _purchase_deposit_code(entry.get("deposit_code"))
    created_line_ids: list[str] = []
    for group in sorted(groups.values(), key=lambda item: item["articleCode"]):
        quantity = group["quantity"] or Decimal("0")
        if quantity <= 0:
            continue
        article_code = group["articleCode"]
        article_key = normalize_article_code(article_code)
        unit_value = unit_values.get(article_key)
        if unit_value is None:
            unit_value = (group["netValue"] / quantity) if quantity else Decimal("0")
        next_order += 1
        line_id = _insert_purchase_entry_line(
            entry_id,
            article_code=article_code,
            article_description=descriptions.get(article_key, ""),
            conversion_factor=Decimal("1"),
            unit_value=_nonnegative_money(unit_value, Decimal("0")),
            deposit_code=deposit_code,
            expected_quantity=quantity.quantize(QTY_QUANT),
            sort_order=next_order,
        )
        created_line_ids.append(line_id)

    if not created_line_ids:
        raise BejermanPurchaseError(
            "PURCHASE_ORDER_WITHOUT_ITEMS",
            "La orden de compra citada no tiene artículos para autocompletar",
            status_code=409,
        )

    _mark_draft_after_change(entry_id, actor_user_id)
    _event(
        entry_id,
        actor_user_id,
        "lines_populated_from_order",
        metadata={"lineIds": created_line_ids, "comprobanteNumeroOrigen": origin_numero},
    )
    result = get_purchase_entry(entry_id, include_events=True)
    result["populatedLineCount"] = len(created_line_ids)
    return result


def _purchase_max_bulk_scans() -> int:
    return _bounded_int(
        getattr(
            settings,
            "BEJERMAN_PURCHASE_MAX_BULK_SCANS",
            DEFAULT_MAX_BULK_SCANS,
        ),
        default=DEFAULT_MAX_BULK_SCANS,
        minimum=1,
        maximum=DEFAULT_MAX_BULK_SCANS,
    )


def _split_barcodes(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("barcodes")
    if isinstance(raw, list):
        items = raw
    elif raw:
        items = re.split(r"[\r\n\t,;]+", str(raw))
    else:
        barcode = payload.get("barcode")
        items = [barcode] if barcode not in (None, "") else []
    barcodes = [_text(item) for item in items if _text(item)]
    maximum = _purchase_max_bulk_scans()
    if len(barcodes) > maximum:
        raise BejermanPurchaseError(
            "PURCHASE_SCAN_LIMIT_EXCEEDED",
            f"Cada carga admite hasta {maximum} códigos; dividí el ingreso en bloques",
            status_code=413,
        )
    return barcodes


def _scan_insert_values(
    entry_id: str,
    line: dict[str, Any],
    payload: dict[str, Any],
    actor_user_id: int | None,
    *,
    barcode: str,
    sort_order: int,
) -> tuple[str, list[Any]]:
    norm = _barcode_norm(barcode)
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
    unit_value = _positive_money(
        _get(payload, "unitValue", "valorUnitario"),
        default_value,
    )
    total_value = (conversion_factor * unit_value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    scan_id = _uuid("bps")
    return (
        scan_id,
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
            sort_order,
            actor_user_id,
        ],
    )


@transaction.atomic
def add_purchase_scan(entry_id: str, line_id: str, payload: dict[str, Any], actor_user_id: int | None) -> dict[str, Any]:
    _require_mutable(entry_id)
    line = _fetchone("SELECT * FROM bejerman_purchase_entry_lines WHERE id = %s AND entry_id = %s", [line_id, entry_id])
    if not line:
        raise BejermanPurchaseError("PURCHASE_LINE_NOT_FOUND", "Línea no encontrada", status_code=404)
    payload = payload or {}
    barcodes = _split_barcodes(payload)
    existing_count_row = _fetchone(
        """
        SELECT count(*) AS scan_count
          FROM bejerman_purchase_entry_scans
         WHERE entry_id = %s
        """,
        [entry_id],
    )
    existing_count = int(existing_count_row.get("scan_count") or 0)
    incoming_count = len(barcodes) if barcodes else 1
    maximum = _purchase_max_bulk_scans()
    if existing_count + incoming_count > maximum:
        raise BejermanPurchaseError(
            "PURCHASE_SCAN_LIMIT_EXCEEDED",
            (
                f"El lote admite hasta {maximum} códigos o cantidades; "
                f"ya contiene {existing_count} y esta carga agrega {incoming_count}"
            ),
            status_code=413,
        )
    normalized = [_barcode_norm(barcode) for barcode in barcodes if _barcode_norm(barcode)]
    if len(normalized) != len(set(normalized)):
        raise BejermanPurchaseError("DUPLICATE_BARCODE", "Hay códigos repetidos en la carga", status_code=409)
    if normalized:
        existing = _fetchall(
            """
            SELECT barcode, barcode_norm
              FROM bejerman_purchase_entry_scans
             WHERE entry_id = %s
               AND barcode_norm = ANY(%s)
            """,
            [entry_id, normalized],
        )
        if existing:
            duplicate = existing[0].get("barcode") or existing[0].get("barcode_norm")
            raise BejermanPurchaseError(
                "DUPLICATE_BARCODE",
                f"El código {duplicate} ya está cargado en este lote",
                status_code=409,
            )
    order_row = _fetchone(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM bejerman_purchase_entry_scans WHERE entry_id = %s",
        [entry_id],
    )
    next_order = int(order_row.get("next_order") or 1)
    scan_ids: list[str] = []
    insert_values: list[list[Any]] = []
    for offset, barcode in enumerate(barcodes or [""]):
        scan_id, values = _scan_insert_values(
            entry_id,
            line,
            payload,
            actor_user_id,
            barcode=barcode,
            sort_order=next_order + offset,
        )
        scan_ids.append(scan_id)
        insert_values.append(values)
    with connection.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO bejerman_purchase_entry_scans (
              id, entry_id, line_id, barcode, barcode_norm, article_code, article_description,
              conversion_factor, unit_value, total_value, is_manual_quantity, note, sort_order,
              scanned_by_user_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            insert_values,
        )
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
        _positive_money(_get(payload, "unitValue"), None)
        if "unitValue" in payload
        else _money(scan.get("unit_value"))
    )
    total_value = (conversion_factor * unit_value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    partida_expiration_date = (
        normalize_date(payload.get("partidaExpirationDate"))
        if "partidaExpirationDate" in payload
        else scan.get("partida_expiration_date")
    )
    _execute(
        """
        UPDATE bejerman_purchase_entry_scans
           SET barcode = %s,
               barcode_norm = %s,
               conversion_factor = %s,
               unit_value = %s,
               total_value = %s,
               is_manual_quantity = %s,
               note = %s,
               partida_expiration_date = %s
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
            partida_expiration_date,
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
        _canonical_document_digits(entry.get("comprobantePtoVenta"), POINT_OF_SALE_WIDTH),
        _canonical_document_digits(entry.get("comprobanteNumero"), DOCUMENT_NUMBER_WIDTH),
    )


def _active_barcode_conflicts(entry_id: str, barcode_norms: list[str]) -> list[dict[str, Any]]:
    if not barcode_norms:
        return []
    return _fetchall(
        """
        SELECT s.barcode, s.barcode_norm, s.entry_id, e.remito_number, e.status
          FROM bejerman_purchase_entry_scans s
          JOIN bejerman_purchase_entries e ON e.id = s.entry_id
         WHERE s.entry_id <> %s
           AND s.barcode_norm = ANY(%s)
            AND e.status IN ('running', 'verifying', 'generated', 'failed_review')
         ORDER BY s.created_at ASC, s.id ASC
        """,
        [entry_id, barcode_norms],
    )


def _lock_purchase_claims(company_key: str = COMPANY_KEY):
    """Serializa la validación final y el claim sin consumir un lock por bulto."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            [f"bejerman-purchase-claim:{company_key}"],
        )


def _non_partida_article_keys(
    entry: dict[str, Any],
    reference_data: dict[str, Any],
) -> set[str]:
    article_by_code = reference_data.get("articles") or {}
    result: set[str] = set()
    for line in entry.get("lines") or []:
        article_key = normalize_article_code(line.get("articleCode"))
        article = article_by_code.get(article_key) or {}
        if article_key and article.get("requiresPartida") is not True:
            result.add(article_key)
    return result


def _active_non_partida_article_conflicts(
    entry_id: str,
    article_keys: set[str],
) -> list[dict[str, Any]]:
    if not article_keys:
        return []
    rows = _fetchall(
        """
        SELECT e.id, e.status, e.remito_number, l.article_code
          FROM bejerman_purchase_entries e
          JOIN bejerman_purchase_entry_lines l ON l.entry_id = e.id
         WHERE e.id <> %s
           AND e.status IN ('running', 'verifying', 'failed_review')
           AND UPPER(l.deposit_code) = %s
           AND UPPER(
                 REGEXP_REPLACE(l.article_code, '[[:space:]-]+', '', 'g')
               ) = ANY(%s)
         ORDER BY e.created_at ASC, e.id ASC, l.sort_order ASC
        """,
        [entry_id, DEFAULT_DEPOSIT, sorted(article_keys)],
    )
    return [
        row
        for row in rows
        if normalize_article_code(row.get("article_code")) in article_keys
    ]


def _remote_purchase_matches(entry: dict[str, Any], client: BejermanSDKClient) -> list[dict[str, Any]]:
    supplier_value = entry.get("supplierCodeRaw") or entry.get("supplierCode")
    filters = [
        bejerman_filter("Comprobante_Tipo", "IGUAL", entry.get("comprobanteTipo") or COMPROBANTE_TIPO),
        bejerman_filter("Proveedor_Codigo", "IGUAL", supplier_value),
        bejerman_filter("Comprobante_Numero", "IGUAL", entry.get("comprobanteNumero") or ""),
    ]
    response = client.list_comprobantes_compras(filters)
    wanted = _document_key(entry)
    matches = []
    for record in records_from_response(response):
        candidate = {
            "supplierCode": as_string(first_value(record, ("Proveedor_Codigo", "ProveedorCodigo", "CodProveedor"))),
            "comprobanteTipo": as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))),
            "comprobanteLetra": as_string(first_value(record, ("Comprobante_Letra", "Letra"))),
            "comprobantePtoVenta": as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta"))),
            "comprobanteNumero": as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante"))),
            "operationCode": as_string(
                first_value(
                    record,
                    (
                        "Comprobante_TipoOperacion",
                        "TipoOperacion",
                        "Tipo_Operacion",
                    ),
                )
            ),
            "raw": record,
        }
        if _document_key(candidate) == wanted:
            matches.append(candidate)
    return matches


def _remote_purchase_duplicate(entry: dict[str, Any], client: BejermanSDKClient) -> dict[str, Any] | None:
    matches = _remote_purchase_matches(entry, client)
    return matches[0] if matches else None


def _map_purchase_deposit(record: dict[str, Any]) -> dict[str, Any] | None:
    code = as_string(
        first_value(
            record,
            (
                "Deposito_Codigo",
                "DepositoCodigo",
                "CodigoDeposito",
                "Codigo",
                "dep_Cod",
            ),
        )
    )
    if not code:
        return None
    return {
        "code": code,
        "enabled": as_nullable_bool(
            first_value(
                record,
                (
                    "Deposito_Habilitacion",
                    "Deposito_Habilitado",
                    "Habilitado",
                    "enabled",
                ),
            )
        ),
        "raw": record,
    }


def _map_purchase_partida(record: dict[str, Any]) -> dict[str, Any] | None:
    partida = as_string(
        first_value(
            record,
            (
                "Partida_Codigo",
                "PartidaCodigo",
                "Partida",
                "partida",
                "Art_Partida",
            ),
        )
    )
    if not partida:
        return None
    return {
        "partida": partida,
        "partidaNorm": _barcode_norm(partida),
        "articleCode": as_string(
            first_value(
                record,
                (
                    "Articulo_Codigo",
                    "ArticuloCodigo",
                    "CodigoArticulo",
                    "Art_Codigo",
                    "Art_CodArticulo",
                    "Partida_ArticuloCodigo",
                ),
            )
        ),
        "depositCode": as_string(
            first_value(
                record,
                (
                    "Deposito_Codigo",
                    "DepositoCodigo",
                    "Deposito",
                    "Art_CodDeposito",
                ),
            )
        ),
        "raw": record,
    }


def _assert_purchase_provider_reference(provider: dict[str, Any]) -> None:
    provider_code = _text(provider.get("code")) or "-"
    if provider.get("enabled") is not True:
        code = (
            "PURCHASE_PROVIDER_DISABLED"
            if provider.get("enabled") is False
            else "PURCHASE_PROVIDER_STATUS_UNKNOWN"
        )
        message = (
            f"El proveedor {provider_code} está deshabilitado en Bejerman"
            if provider.get("enabled") is False
            else f"Bejerman no informó si el proveedor {provider_code} está habilitado"
        )
        raise BejermanPurchaseError(code, message, status_code=409)


def _assert_purchase_article_reference(
    article: dict[str, Any],
    requested_code: str,
) -> None:
    article_code = _text(article.get("code")) or requested_code or "-"
    if article.get("purchaseEnabled") is not True:
        code = (
            "PURCHASE_ARTICLE_DISABLED"
            if article.get("purchaseEnabled") is False
            else "PURCHASE_ARTICLE_PURCHASE_STATUS_UNKNOWN"
        )
        message = (
            f"El artículo {article_code} no participa del circuito de compras"
            if article.get("purchaseEnabled") is False
            else f"Bejerman no informó si el artículo {article_code} participa de Compras"
        )
        raise BejermanPurchaseError(code, message, status_code=409)
    if article.get("controlsStock") is not True or article.get("stockEnabled") is not True:
        raise BejermanPurchaseError(
            "PURCHASE_ARTICLE_STOCK_DISABLED",
            f"El artículo {article_code} no está habilitado para actualizar stock",
            status_code=409,
        )
    if article.get("requiresPartida") not in {True, False}:
        raise BejermanPurchaseError(
            "PURCHASE_ARTICLE_PARTIDA_STATUS_UNKNOWN",
            f"Bejerman no informó el control de partida del artículo {article_code}",
            status_code=409,
        )
    vat_code = _text(article.get("purchaseVatCode"))
    if not vat_code or _purchase_vat_rate(vat_code) is None:
        raise BejermanPurchaseError(
            "PURCHASE_ARTICLE_VAT_UNMAPPED",
            f"El artículo {article_code} no tiene una tasa de IVA de compras configurada",
            status_code=409,
        )
    if not _text(article.get("taxCreditAllocation")):
        raise BejermanPurchaseError(
            "PURCHASE_ARTICLE_TAX_ALLOCATION_MISSING",
            f"El artículo {article_code} no tiene imputación de crédito fiscal",
            status_code=409,
        )
    if not _text(article.get("taxCreditCategory")):
        raise BejermanPurchaseError(
            "PURCHASE_ARTICLE_TAX_CATEGORY_MISSING",
            f"El artículo {article_code} no tiene rubro de crédito fiscal",
            status_code=409,
        )


def _assert_purchase_deposit_reference(deposit: dict[str, Any], deposit_code: str) -> None:
    _purchase_deposit_code(deposit_code)
    if deposit.get("enabled") is not True:
        code = (
            "PURCHASE_DEPOSIT_DISABLED"
            if deposit.get("enabled") is False
            else "PURCHASE_DEPOSIT_STATUS_UNKNOWN"
        )
        message = (
            f"El depósito {deposit_code} está deshabilitado en Bejerman"
            if deposit.get("enabled") is False
            else f"Bejerman no informó si el depósito {deposit_code} está habilitado"
        )
        raise BejermanPurchaseError(code, message, status_code=409)


def _assert_reference_data_matches_entry(
    entry: dict[str, Any],
    reference_data: dict[str, Any],
) -> list[dict[str, Any]]:
    provider = reference_data.get("provider") or {}
    if _text(provider.get("code")).upper() != _text(entry.get("supplierCode")).upper():
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_CHANGED",
            "El proveedor cambió durante la validación; volvé a validar el lote",
            status_code=409,
        )
    _assert_purchase_provider_reference(provider)

    articles = reference_data.get("articles") or {}
    deposits = reference_data.get("deposits") or {}
    partida_index = reference_data.get("partidas") or {}
    for line in entry.get("lines") or []:
        article_code = _text(line.get("articleCode"))
        article_key = normalize_article_code(article_code)
        article = articles.get(article_key)
        if not article:
            raise BejermanPurchaseError(
                "PURCHASE_ENTRY_CHANGED",
                f"El artículo {article_code or '-'} cambió durante la validación; volvé a validar el lote",
                status_code=409,
            )
        _assert_purchase_article_reference(article, article_code)

        deposit_code = _purchase_deposit_code(
            line.get("depositCode") or entry.get("depositCode") or DEFAULT_DEPOSIT
        )
        deposit = deposits.get(deposit_code)
        if not deposit:
            raise BejermanPurchaseError(
                "PURCHASE_ENTRY_CHANGED",
                f"El depósito {deposit_code or '-'} cambió durante la validación; volvé a validar el lote",
                status_code=409,
            )
        _assert_purchase_deposit_reference(deposit, deposit_code)

        if article.get("requiresPartida") is not True:
            continue
        for scan in line.get("scans") or []:
            partida_norm = _barcode_norm(scan.get("barcode"))
            if not partida_norm:
                raise BejermanPurchaseError(
                    "PURCHASE_PARTIDA_REQUIRED",
                    f"El artículo {article_code} requiere partida o número de serie",
                    status_code=409,
                )
            remote_match = partida_index.get(partida_norm)
            if remote_match:
                raise BejermanPurchaseError(
                    "PURCHASE_PARTIDA_ALREADY_EXISTS",
                    (
                        f"La partida o serie {scan.get('barcode')} ya existe en Bejerman "
                        f"(depósito {remote_match.get('depositCode') or '-'})"
                    ),
                    status_code=409,
                )
    return []


def _stock_totals_by_article(
    stock_rows: list[dict[str, Any]],
    deposit_code: str,
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    wanted_deposit = _text(deposit_code).upper()
    for row in stock_rows:
        if _text(row.get("depositCode")).upper() != wanted_deposit:
            continue
        article_key = normalize_article_code(row.get("articleCode"))
        if not article_key:
            continue
        totals[article_key] = totals.get(article_key, Decimal("0")) + (
            _decimal(row.get("realQuantity"), Decimal("0")) or Decimal("0")
        )
    return totals


def _stock_totals_by_article_partida(
    stock_rows: list[dict[str, Any]],
    deposit_code: str,
) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = {}
    wanted_deposit = _text(deposit_code).upper()
    for row in stock_rows:
        if _text(row.get("depositCode")).upper() != wanted_deposit:
            continue
        article_key = normalize_article_code(row.get("articleCode"))
        partida_key = _barcode_norm(row.get("partida"))
        if not article_key or not partida_key:
            continue
        key = (article_key, partida_key)
        totals[key] = totals.get(key, Decimal("0")) + (
            _decimal(row.get("realQuantity"), Decimal("0")) or Decimal("0")
        )
    return totals


def _purchase_verification_baseline(
    entry: dict[str, Any],
    article_by_code: dict[str, dict[str, Any]],
    stock_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    stock_totals = _stock_totals_by_article(stock_rows, DEFAULT_DEPOSIT)
    stock_by_partida = _stock_totals_by_article_partida(
        stock_rows,
        DEFAULT_DEPOSIT,
    )
    articles: dict[str, dict[str, Any]] = {}
    for line in entry.get("lines") or []:
        requested_code = _text(line.get("articleCode"))
        requested_key = normalize_article_code(requested_code)
        article = article_by_code.get(requested_key) or {}
        canonical_code = _text(article.get("code")) or requested_code
        article_key = normalize_article_code(canonical_code)
        if not article_key:
            continue
        item = articles.setdefault(
            article_key,
            {
                "articleCode": canonical_code,
                "requiresPartida": article.get("requiresPartida") is True,
                "trackingMode": (
                    "partida"
                    if article.get("requiresPartida") is True
                    else "none"
                ),
                "baselineRealQuantity": _public_decimal(
                    stock_totals.get(article_key, Decimal("0")),
                    "0.0001",
                ),
                "expectedIncrement": 0.0,
                "partidas": {},
            },
        )
        expected_increment = _decimal(
            item.get("expectedIncrement"),
            Decimal("0"),
        ) or Decimal("0")
        for scan in line.get("scans") or []:
            expected_increment += (
                _decimal(scan.get("conversionFactor"), Decimal("0"))
                or Decimal("0")
            )
            partida = _text(scan.get("barcode"))
            if item["requiresPartida"] and partida:
                partida_key = _barcode_norm(partida)
                partida_item = item["partidas"].setdefault(
                    partida_key,
                    {
                        "partida": partida,
                        "baselineRealQuantity": _public_decimal(
                            stock_by_partida.get(
                                (article_key, partida_key),
                                Decimal("0"),
                            ),
                            "0.0001",
                        ),
                        "expectedIncrement": 0.0,
                    },
                )
                partida_increment = (
                    _decimal(
                        partida_item.get("expectedIncrement"),
                        Decimal("0"),
                    )
                    or Decimal("0")
                )
                partida_item["expectedIncrement"] = _public_decimal(
                    partida_increment
                    + (
                        _decimal(
                            scan.get("conversionFactor"),
                            Decimal("0"),
                        )
                        or Decimal("0")
                    ),
                    "0.0001",
                )
        item["expectedIncrement"] = _public_decimal(
            expected_increment,
            "0.0001",
        )
    for item in articles.values():
        item["partidas"] = sorted(
            item["partidas"].values(),
            key=lambda value: _barcode_norm(value.get("partida")),
        )
    return {
        "version": 2,
        "depositCode": DEFAULT_DEPOSIT,
        "articles": [
            articles[key]
            for key in sorted(articles)
        ],
    }


def _resolve_purchase_reference_data(
    entry: dict[str, Any],
    client: BejermanSDKClient,
    actor_user_id: int | None,
    *,
    capture_stock_baseline: bool = True,
) -> dict[str, Any]:
    company_key = getattr(client, "company_key", "") or entry.get("companyKey") or COMPANY_KEY
    provider_response = _cached_sdk_response(
        f"providers:{company_key}",
        actor_user_id,
        client.list_proveedores,
        setting_name="BEJERMAN_PURCHASE_PROVIDER_CACHE_SECONDS",
        default_seconds=60,
    )
    provider_code = _text(entry.get("supplierCode")).upper()
    providers = [
        provider
        for provider in (_map_provider(record) for record in records_from_response(provider_response))
        if provider and _text(provider.get("code")).upper() == provider_code
    ]
    if not providers:
        raise BejermanPurchaseError(
            "PURCHASE_PROVIDER_NOT_FOUND",
            f"El proveedor {entry.get('supplierCode') or '-'} no existe en Bejerman {company_key}",
            status_code=409,
        )
    provider = providers[0]
    _assert_purchase_provider_reference(provider)

    requested_articles: dict[str, str] = {}
    for line in entry.get("lines") or []:
        requested_code = _text(line.get("articleCode"))
        key = normalize_article_code(requested_code)
        if key:
            requested_articles.setdefault(key, requested_code)

    article_by_code: dict[str, dict[str, Any]] = {}
    if requested_articles:
        article_response = _cached_sdk_response(
            f"articles-reference-full:{company_key}",
            actor_user_id,
            lambda: client.list_articulos([], 5000),
            setting_name="BEJERMAN_PURCHASE_CATALOG_CACHE_SECONDS",
            default_seconds=30,
        )
        candidates: dict[str, dict[str, Any]] = {}
        for record in records_from_response(article_response):
            mapped = map_article(record)
            if not mapped:
                continue
            candidate_key = normalize_article_code(mapped.get("code"))
            if candidate_key:
                candidates.setdefault(
                    candidate_key,
                    _purchase_article_fields(mapped),
                )
    else:
        candidates = {}

    for key, requested_code in requested_articles.items():
        article = candidates.get(key)
        if not article:
            raise BejermanPurchaseError(
                "PURCHASE_ARTICLE_NOT_FOUND",
                f"El artículo {requested_code or '-'} no existe en Bejerman {company_key}",
                status_code=409,
            )
        _assert_purchase_article_reference(article, requested_code)
        article_by_code[key] = article

    deposit_response = _cached_sdk_response(
        f"deposits:{company_key}",
        actor_user_id,
        client.obtener_depositos,
        setting_name="BEJERMAN_PURCHASE_DEPOSIT_CACHE_SECONDS",
        default_seconds=60,
    )
    deposits = {
        _text(deposit.get("code")).upper(): deposit
        for deposit in (_map_purchase_deposit(record) for record in records_from_response(deposit_response))
        if deposit
    }
    requested_deposits = {
        _purchase_deposit_code(
            line.get("depositCode") or entry.get("depositCode") or DEFAULT_DEPOSIT
        )
        for line in entry.get("lines") or []
    } or {_purchase_deposit_code(entry.get("depositCode") or DEFAULT_DEPOSIT)}
    for deposit_code in requested_deposits:
        deposit = deposits.get(deposit_code)
        if not deposit:
            raise BejermanPurchaseError(
                "PURCHASE_DEPOSIT_NOT_FOUND",
                f"El depósito {deposit_code or '-'} no existe en Bejerman {company_key}",
                status_code=409,
            )
        _assert_purchase_deposit_reference(deposit, deposit_code)

    requires_partida_catalog = any(
        article_by_code.get(normalize_article_code(line.get("articleCode")), {}).get("requiresPartida")
        is True
        for line in entry.get("lines") or []
    )
    partida_index: dict[str, dict[str, Any]] = {}
    if requires_partida_catalog:
        partida_catalog = _cached_sdk_response(
            f"partida-index:{company_key}",
            actor_user_id,
            lambda: {
                "items": {
                    partida["partidaNorm"]: {
                        "partida": partida["partida"],
                        "depositCode": partida["depositCode"],
                    }
                    for partida in (
                        _map_purchase_partida(record)
                        for record in records_from_response(client.obtener_partidas())
                    )
                    if partida and partida.get("partidaNorm")
                }
            },
            setting_name="BEJERMAN_PURCHASE_PARTIDA_CACHE_SECONDS",
            default_seconds=60,
        )
        partida_index = partida_catalog.get("items") or {}

    verification_baseline: dict[str, Any] = {}
    if capture_stock_baseline:
        # La línea base debe pertenecer a esta prevalidación concreta.
        stock_response = client.obtener_stock_deposito(DEFAULT_DEPOSIT)
        verification_baseline = _purchase_verification_baseline(
            entry,
            article_by_code,
            build_stock_rows(stock_response),
        )
    reference_data = {
        "provider": provider,
        "articles": article_by_code,
        "deposits": deposits,
        "partidas": partida_index,
        "verificationBaseline": verification_baseline,
    }
    reference_data["warnings"] = _assert_reference_data_matches_entry(entry, reference_data)
    return reference_data


def _entry_with_purchase_reference_data(
    entry: dict[str, Any],
    reference_data: dict[str, Any],
) -> dict[str, Any]:
    provider = reference_data.get("provider") or {}
    provider_snapshot = (
        provider.get("raw")
        if isinstance(provider.get("raw"), dict)
        else entry.get("supplierSnapshot") or {}
    )
    return {
        **entry,
        "supplierCode": provider.get("code") or entry.get("supplierCode"),
        "supplierCodeRaw": provider.get("rawCode") or entry.get("supplierCodeRaw"),
        "supplierName": provider.get("name") or entry.get("supplierName"),
        "supplierTaxId": provider.get("cuit") or entry.get("supplierTaxId"),
        "supplierSnapshot": provider_snapshot,
        "paymentTermCode": (
            entry.get("paymentTermCode")
            or provider.get("paymentTermCode")
            or ""
        ),
    }


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
    document_totals: dict[str, float] | None = None
    preflight_fingerprint = ""

    def add_error(code: str, message: str, field: str = ""):
        errors.append({"code": code, "message": message, "field": field})

    if entry["companyKey"] not in _purchase_emission_supported_companies():
        add_error(
            "COMPANY_EMISSION_NOT_CONFIGURED",
            (
                f"La emisión del RT de compra todavía no está habilitada para {entry['companyKey']} "
                "(falta configurar su depósito en Bejerman); se puede buscar, citar y autocompletar la OC igual"
            ),
            "companyKey",
        )
    if entry["comprobanteTipo"] != COMPROBANTE_TIPO:
        add_error("INVALID_COMPROBANTE_TYPE", "Solo se permite RT en esta versión", "comprobanteTipo")
    if entry["operationCode"] != OPERATION_CODE:
        add_error("INVALID_OPERATION", "Solo se permite operación MC en esta versión", "operationCode")
    if _text(entry.get("depositCode")).upper() != DEFAULT_DEPOSIT:
        add_error(
            "PURCHASE_DEPOSIT_NOT_ALLOWED",
            f"Este circuito sólo admite el depósito {DEFAULT_DEPOSIT}",
            "depositCode",
        )
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
        if _text(line.get("depositCode")).upper() != DEFAULT_DEPOSIT:
            add_error(
                "PURCHASE_DEPOSIT_NOT_ALLOWED",
                f"Cada línea debe ingresar al depósito {DEFAULT_DEPOSIT}",
                "depositCode",
            )
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
    maximum_scans = _purchase_max_bulk_scans()
    if scan_count > maximum_scans:
        add_error(
            "PURCHASE_SCAN_LIMIT_EXCEEDED",
            f"El lote supera el máximo de {maximum_scans} códigos o cantidades",
            "scans",
        )
    if not scan_count:
        add_error("SCANS_REQUIRED", "Hay que escanear bultos o cargar una cantidad manual", "scans")

    warnings.extend(_purchase_order_quantity_mismatches(entry))

    barcode_conflicts = _active_barcode_conflicts(entry_id, sorted(seen_barcodes))
    if barcode_conflicts:
        conflict = barcode_conflicts[0]
        document = conflict.get("remito_number") or conflict.get("entry_id")
        add_error(
            "PURCHASE_BARCODE_ALREADY_USED",
            f"El código {conflict.get('barcode') or conflict.get('barcode_norm')} ya pertenece al lote activo {document}",
            "barcode",
        )

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
               AND status IN ('running', 'verifying', 'generated', 'failed_review')
               AND id <> %s
             LIMIT 1
            """,
            [
                entry["companyKey"],
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
        sdk_client = client or BejermanSDKClient(
            company_key=entry["companyKey"],
            actor_user_id=actor_user_id,
        )
        try:
            duplicate = _remote_purchase_duplicate(entry, sdk_client)
        except Exception as exc:
            raise BejermanPurchaseError("REMOTE_DUPLICATE_CHECK_FAILED", str(exc), status_code=502) from exc
        if duplicate:
            add_error("REMOTE_DUPLICATE", "Bejerman ya registra un RT con ese proveedor y número", "comprobanteNumero")
        if not errors:
            try:
                reference_data = _resolve_purchase_reference_data(
                    entry,
                    sdk_client,
                    actor_user_id,
                )
            except BejermanPurchaseError as exc:
                add_error(exc.code, str(exc))
            except Exception as exc:
                raise BejermanPurchaseError(
                    "PURCHASE_REFERENCE_CHECK_FAILED",
                    str(exc),
                    status_code=502,
                ) from exc
            else:
                warnings.extend(reference_data.get("warnings") or [])
                canonical_entry = _entry_with_purchase_reference_data(
                    entry,
                    reference_data,
                )
                built = build_purchase_entry_comprobante(
                    canonical_entry,
                    reference_data,
                )
                numera_flex, emite_reg = _purchase_emission_options()
                request_payload = _purchase_request_payload(
                    built["comprobante"],
                    numera_flex=numera_flex,
                    emite_reg=emite_reg,
                )
                request_payload["VerificationBaseline"] = (
                    reference_data.get("verificationBaseline") or {}
                )
                if warnings:
                    request_payload["ValidationWarnings"] = warnings
                preflight_fingerprint = _request_fingerprint(request_payload)
                document_totals = {
                    "netValue": _public_decimal(entry.get("totalValue")),
                    "taxValue": _public_decimal(built.get("taxValue")),
                    "totalValue": _public_decimal(built.get("totalValue")),
                }

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
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "totals": {
            "quantity": entry["totalQuantity"],
            "value": entry["totalValue"],
        },
        "documentTotals": document_totals,
        "preflightFingerprint": preflight_fingerprint,
        "entry": entry,
    }


def _provider_fields(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry.get("supplierSnapshot") if isinstance(entry.get("supplierSnapshot"), dict) else {}
    return {
        "Proveedor_TipoDocumento": first_value(
            raw,
            (
                "Proveedor_TipoDocumentoCodigo",
                "Proveedor_TipoDocumento",
                "tipoDocumento",
                "TipoDocumento",
            ),
        ),
        "Proveedor_Provincia": first_value(
            raw,
            (
                "Proveedor_ProvinciaCodigo",
                "Proveedor_Provincia",
                "provincia",
                "Provincia",
            ),
        ),
        "Proveedor_SitIVA": first_value(
            raw,
            (
                "Proveedor_SituacionIVACodigo",
                "Proveedor_SitIVA",
                "sitIva",
                "SitIVA",
            ),
        ),
        "Proveedor_NroDocumento": first_value(
            raw,
            (
                "Proveedor_NumeroDocumento",
                "Proveedor_NroDocumento",
                "cuit",
                "CUIT",
                "Proveedor_CUIT",
            ),
        ),
        "Proveedor_NroIngresosBrutos": first_value(
            raw,
            (
                "Proveedor_NumeroIIBB",
                "Proveedor_NroIngresosBrutos",
                "ingresosBrutos",
                "IngresosBrutos",
            ),
        ),
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
        "Item_NumeroRenglon": sort_order,
    }


def _purchase_vat_rate(code: Any) -> Decimal | None:
    normalized_code = re.sub(r"[^A-Z0-9]", "", _text(code).upper())
    if normalized_code.isdigit() and len(normalized_code) == 1:
        normalized_code = normalized_code.zfill(2)
    raw_mapping = _text(
        getattr(
            settings,
            "BEJERMAN_PURCHASE_VAT_RATE_CODES",
            getattr(settings, "BEJERMAN_REMITO_VAT_RATE_CODES", "01:21,03:10.5,04:0"),
        )
    )
    for chunk in re.split(r"[;,]", raw_mapping):
        if not re.search(r"[:=]", chunk):
            continue
        raw_code, raw_rate = re.split(r"[:=]", chunk, maxsplit=1)
        candidate = re.sub(r"[^A-Z0-9]", "", raw_code.strip().upper())
        if candidate.isdigit() and len(candidate) == 1:
            candidate = candidate.zfill(2)
        if candidate == normalized_code:
            return _decimal(as_number(raw_rate), Decimal("0")) or Decimal("0")
    return None


def _purchase_currency_options() -> tuple[str, str, Decimal]:
    currency = _text(
        getattr(settings, "BEJERMAN_PURCHASE_CURRENCY", "")
    ).upper()
    exchange_type = _text(
        getattr(settings, "BEJERMAN_PURCHASE_EXCHANGE_TYPE", "")
    ).upper()
    exchange_rate = _decimal(
        getattr(settings, "BEJERMAN_PURCHASE_EXCHANGE_RATE", 1),
        Decimal("0"),
    ) or Decimal("0")
    if not currency or not exchange_type or exchange_rate <= 0:
        raise BejermanPurchaseError(
            "PURCHASE_CURRENCY_CONFIG_INVALID",
            (
                "El circuito COMPRAS requiere moneda, tipo de cambio "
                "y cotización positiva antes de registrar en Bejerman"
            ),
            status_code=503,
        )
    return currency, exchange_type, exchange_rate


def _validate_purchase_comprobante_amounts(
    comprobante: dict[str, Any],
) -> None:
    calculated = Decimal("0")
    for item in comprobante.get("Comprobante_Items") or []:
        if not isinstance(item, dict) or _text(item.get("Item_Tipo")).upper() != "A":
            continue
        calculated += (
            _decimal(item.get("Item_ImporteTotal"), Decimal("0"))
            or Decimal("0")
        )
        calculated += (
            _decimal(item.get("Item_ImporteIVAInscrip"), Decimal("0"))
            or Decimal("0")
        )
        calculated += (
            _decimal(item.get("Item_ImporteIVANoInscrip"), Decimal("0"))
            or Decimal("0")
        )
        calculated += (
            _decimal(item.get("Item_ImporteConceptoNoGravado"), Decimal("0"))
            or Decimal("0")
        )
    calculated = calculated.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    declared = _money(comprobante.get("Comprobante_ImporteTotal"))
    if calculated != declared:
        raise BejermanPurchaseError(
            "PURCHASE_AMOUNT_MISMATCH",
            (
                f"El total del RT ({declared}) no coincide con "
                f"la suma de sus ítems e IVA ({calculated})"
            ),
            status_code=409,
        )


def build_purchase_entry_comprobante(
    entry: dict[str, Any],
    reference_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_date = normalize_date(entry.get("issueDate")) or _today()
    accounting_date = normalize_date(entry.get("accountingDate")) or issue_date
    ddjj_date = normalize_date(entry.get("ddjjDate")) or issue_date
    article_by_code = (reference_data or {}).get("articles") or {}
    items: list[dict[str, Any]] = []
    total = Decimal("0")
    tax_total = Decimal("0")
    sort_order = 1
    currency, exchange_type, exchange_rate = _purchase_currency_options()
    for line in entry.get("lines") or []:
        requested_article_code = _text(line.get("articleCode"))
        article = article_by_code.get(normalize_article_code(requested_article_code))
        if not article:
            raise BejermanPurchaseError(
                "PURCHASE_ARTICLE_REFERENCE_REQUIRED",
                f"Falta el maestro Bejerman del artículo {requested_article_code or '-'}",
                status_code=409,
            )
        _assert_purchase_article_reference(article, requested_article_code)
        vat_rate = _purchase_vat_rate(article.get("purchaseVatCode"))
        if vat_rate is None:
            raise BejermanPurchaseError(
                "PURCHASE_ARTICLE_VAT_UNMAPPED",
                f"El artículo {requested_article_code or '-'} no tiene una tasa de IVA de compras configurada",
                status_code=409,
            )
        tax_credit_allocation = _text(article.get("taxCreditAllocation"))
        tax_credit_category = _text(article.get("taxCreditCategory"))
        vat_type = _text(article.get("purchaseVatType")) or "1"
        requires_partida = article.get("requiresPartida")
        for scan in line.get("scans") or []:
            qty = _qty(scan.get("conversionFactor"))
            unit_value = _money(scan.get("unitValue"))
            item_total = (qty * unit_value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            item_tax = (item_total * vat_rate / Decimal("100")).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
            total += item_total
            tax_total += item_tax
            article_code = _text(article.get("code"))
            article_description = _text(
                article.get("name")
                or article.get("description")
                or line.get("articleDescription")
                or article_code
            )
            items.append(
                {
                    **_item_common({**entry, "issueDate": issue_date}, sort_order),
                    "Item_Tipo": "A",
                    "Item_CodigoArticulo": article_code,
                    "Item_CantidadUM1": float(qty),
                    "Item_CantidadUM2": 0,
                    "Item_DescripArticulo": article_description[:250],
                    "Item_PrecioUnitario": float(unit_value),
                    "Item_TasaIVAInscrip": float(vat_rate),
                    "Item_TasaIVANoInscrip": 0,
                    "Item_ImporteIVAInscrip": float(item_tax),
                    "Item_ImporteIVANoInscrip": 0,
                    "Item_ImporteTotal": float(item_total),
                    "Item_ImporteDescComercial": 0,
                    "Item_ImporteDescFinanciero": 0,
                    "Item_CodigoConceptoNoGravado": None,
                    "Item_ImporteConceptoNoGravado": 0,
                    "Item_TipoIVA": vat_type,
                    "Item_ImporteDescPorItem": 0,
                    "Item_Deposito": _purchase_deposit_code(
                        line.get("depositCode")
                        or entry.get("depositCode")
                        or DEFAULT_DEPOSIT
                    ),
                    "Item_Partida": (scan.get("barcode") or " ") if requires_partida is not False else " ",
                    "Item_TasaDescPorItem": 0,
                    "Item_ImporteItem": float(item_total),
                    "Item_ImputacionCreditoFiscal": tax_credit_allocation,
                    "Item_RubroCreditoFiscal": tax_credit_category,
                    "Item_DatosAdicionales": None,
                }
            )
            sort_order += 1
    provider_fields = {key: value for key, value in _provider_fields(entry).items() if value not in (None, "")}
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
        "Comprobante_ImporteTotal": float((total + tax_total).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)),
        "Comprobante_ActualizaStock": "S",
        "Comprobante_Moneda": currency,
        "Comprobante_TipoCambio": exchange_type,
        "Comprobante_CotizacionCambio": float(exchange_rate),
        "Comprobante_FechaContabilizacion": accounting_date,
        "Comprobante_FechaDDJJ": ddjj_date,
        "Comprobante_TipoOperacion": entry.get("operationCode") or OPERATION_CODE,
        "Comprobante_Items": items,
        "Comprobante_MediosPago": [],
        "Comprobante_RegEspeciales": [],
        "Comprobante_DatosAdicionales": None,
        "Comprobante_Cuotas": None,
    }
    if entry.get("comprobanteNumeroOrigen"):
        comprobante = {
            "Comprobante_Tipo_Origen": entry.get("comprobanteTipoOrigen") or "OC",
            "Comprobante_Letra_Origen": entry.get("comprobanteLetraOrigen") or "",
            "Comprobante_PtoVenta_Origen": entry.get("comprobantePtoVentaOrigen") or "",
            "Comprobante_Numero_Origen": entry["comprobanteNumeroOrigen"],
            **comprobante,
        }
    _validate_purchase_comprobante_amounts(comprobante)
    return {
        "comprobante": comprobante,
        "profile": {
            "companyKey": entry.get("companyKey") or COMPANY_KEY,
            "type": entry.get("comprobanteTipo") or COMPROBANTE_TIPO,
            "operation": entry.get("operationCode") or OPERATION_CODE,
            "deposit": entry.get("depositCode") or DEFAULT_DEPOSIT,
        },
        "lineCount": len(items),
        "totalValue": float((total + tax_total).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)),
        "taxValue": float(tax_total.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)),
    }


def _purchase_request_payload(comprobante: dict[str, Any], *, numera_flex: str, emite_reg: str) -> dict[str, Any]:
    return {
        "Circuito": "COMPRAS",
        "Operacion": "IngresarListaComprobantesJSON",
        "Parametros": None,
        "ParametrosJson": [_json_param([comprobante]), numera_flex, emite_reg],
        "Comprobante": comprobante,
    }


def _purchase_emission_options() -> tuple[str, str]:
    numera_flex = as_string(
        getattr(settings, "BEJERMAN_PURCHASE_NUMERA_FLEX", "N")
    ).upper() or "N"
    emite_reg = as_string(
        getattr(settings, "BEJERMAN_PURCHASE_EMITE_REG", "R")
    ).upper() or "R"
    if numera_flex != "N" or emite_reg != "R":
        raise BejermanPurchaseError(
            "PURCHASE_EMISSION_CONFIG_INVALID",
            (
                "El circuito de compras sólo admite numeración Bejerman N y registración R; "
                f"configuración recibida: {numera_flex}/{emite_reg}"
            ),
            status_code=503,
        )
    return numera_flex, emite_reg


def _raise_for_locked_emission_status(entry: dict[str, Any]):
    status_value = entry.get("status")
    if status_value == "running":
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_EMISSION_RUNNING",
            "El lote ya tiene una registración en curso; verificá su resultado antes de continuar",
            status_code=409,
        )
    if status_value == "verifying":
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_VERIFICATION_PENDING",
            "Bejerman aceptó el RT; verificá cabecera, ítems, stock y partidas antes de continuar",
            status_code=409,
        )
    if status_value == "failed_review":
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_REVIEW_REQUIRED",
            "El resultado anterior es incierto; verificá Bejerman y no vuelvas a registrar el lote",
            status_code=409,
        )
    if status_value == "cancelled":
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_CANCELLED",
            "El lote está descartado y no se puede registrar",
            status_code=409,
        )


@transaction.atomic
def _claim_purchase_entry(
    entry_id: str,
    actor_user_id: int | None,
    reference_data: dict[str, Any],
    client: BejermanSDKClient,
    *,
    numera_flex: str,
    emite_reg: str,
    expected_preflight_fingerprint: str,
) -> dict[str, Any]:
    row = _require_entry(entry_id, for_update=True)
    current = _serialize_entry(row)
    if current.get("status") == "generated":
        return {
            "alreadyGenerated": True,
            "entry": _serialize_entry(row, include_events=True),
        }
    _raise_for_locked_emission_status(current)
    if current.get("status") not in MUTABLE_STATUSES:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_LOCKED",
            "El lote no está en un estado registrable",
            status_code=409,
        )

    # Un único lock por circuito (por empresa) mantiene acotado el consumo de
    # locks aun con 5.000 bultos y vuelve atómicos los controles de
    # duplicados/solapamientos.
    _lock_purchase_claims(current.get("companyKey") or COMPANY_KEY)
    validation = validate_purchase_entry(entry_id)
    if not validation["ok"]:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_INVALID",
            validation["errors"][0]["message"]
            if validation["errors"]
            else "El lote no es válido",
            status_code=409,
        )
    current = validation["entry"]
    # Tiene que juntar las mismas advertencias que arma la prevalidación
    # (validate_purchase_entry con check_remote=True) para que el fingerprint
    # de acá coincida con el que ya confirmó el usuario — si no, cualquier
    # lote con aviso de cantidad OC vs RT siempre daría PURCHASE_PREFLIGHT_STALE.
    warnings = list(validation.get("warnings") or []) + _assert_reference_data_matches_entry(
        current, reference_data
    )
    non_partida_keys = _non_partida_article_keys(current, reference_data)
    article_conflicts = _active_non_partida_article_conflicts(
        entry_id,
        non_partida_keys,
    )
    if article_conflicts:
        conflict = article_conflicts[0]
        document = conflict.get("remito_number") or conflict.get("id")
        raise BejermanPurchaseError(
            "PURCHASE_ARTICLE_EMISSION_PENDING",
            (
                f"El artículo {conflict.get('article_code') or '-'} ya participa "
                f"en el lote activo {document}. Verificá ese RT antes de "
                "registrar otro ingreso del mismo artículo sin partida"
            ),
            status_code=409,
        )

    # Se vuelve a leer VAL dentro del lock de claim. Así, si otro RT terminó
    # entre la prevalidación y el clic de emisión, cambia el fingerprint y se
    # obliga a prevalidar nuevamente en vez de atribuirle stock ajeno.
    try:
        stock_response = client.obtener_stock_deposito(DEFAULT_DEPOSIT)
    except Exception as exc:
        raise BejermanPurchaseError(
            "PURCHASE_STOCK_BASELINE_FAILED",
            f"No se pudo obtener la línea base fresca de stock en {DEFAULT_DEPOSIT}: {exc}",
            status_code=502,
        ) from exc
    reference_data = {
        **reference_data,
        "verificationBaseline": _purchase_verification_baseline(
            current,
            reference_data.get("articles") or {},
            build_stock_rows(stock_response),
        ),
    }
    entry = _entry_with_purchase_reference_data(current, reference_data)
    built = build_purchase_entry_comprobante(entry, reference_data)
    request_payload = _purchase_request_payload(built["comprobante"], numera_flex=numera_flex, emite_reg=emite_reg)
    request_payload["VerificationBaseline"] = (
        reference_data.get("verificationBaseline") or {}
    )
    if warnings:
        request_payload["ValidationWarnings"] = warnings
    fingerprint = _request_fingerprint(request_payload)
    if not expected_preflight_fingerprint or fingerprint != expected_preflight_fingerprint:
        raise BejermanPurchaseError(
            "PURCHASE_PREFLIGHT_STALE",
            "El lote o sus datos maestros cambiaron después de la prevalidación; volvé a validar antes de registrar",
            status_code=409,
        )
    try:
        claimed = _fetchone(
            """
            UPDATE bejerman_purchase_entries
               SET status = 'running',
                   supplier_code = %s,
                   supplier_code_raw = %s,
                   supplier_name = %s,
                   supplier_tax_id = %s,
                   supplier_snapshot = %s::jsonb,
                   payment_term_code = %s,
                   request_payload = %s::jsonb,
                   response_payload = '{}'::jsonb,
                   last_error = NULL,
                   attempt_count = COALESCE(attempt_count, 0) + 1,
                   last_attempt_at = CURRENT_TIMESTAMP,
                   request_fingerprint = %s,
                   updated_by_user_id = %s
             WHERE id = %s
               AND status IN ('draft', 'validated', 'failed')
            RETURNING *
            """,
            [
                entry["supplierCode"],
                entry["supplierCodeRaw"],
                entry["supplierName"],
                entry["supplierTaxId"],
                _json_param(entry["supplierSnapshot"]),
                entry["paymentTermCode"],
                _json_param(request_payload),
                fingerprint,
                actor_user_id,
                entry_id,
            ],
        )
    except IntegrityError as exc:
        raise BejermanPurchaseError(
            "LOCAL_DUPLICATE",
            "Ya hay un RT local activo con ese proveedor y número",
            status_code=409,
        ) from exc
    if not claimed:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_CLAIM_CONFLICT",
            "Otro proceso cambió el lote antes de iniciar la registración",
            status_code=409,
        )
    _event(
        entry_id,
        actor_user_id,
        "emission_started",
        metadata={
            "lineCount": built["lineCount"],
            "attemptCount": int(claimed.get("attempt_count") or 0),
            "requestFingerprint": fingerprint,
            "warningCount": len(warnings),
        },
    )
    return {
        "alreadyGenerated": False,
        "entry": entry,
        "built": built,
        "requestPayload": request_payload,
        "requestFingerprint": fingerprint,
    }


@transaction.atomic
def _finalize_purchase_emission(
    entry_id: str,
    actor_user_id: int | None,
    claimed: dict[str, Any],
    raw_response: dict[str, Any],
) -> dict[str, Any]:
    entry = claimed["entry"]
    built = claimed["built"]
    parsed = parse_remito_response(raw_response)
    required_response_fields = (
        "comprobanteTipo",
        "comprobanteLetra",
        "comprobantePtoVenta",
        "comprobanteNumero",
    )
    if any(not _text(parsed.get(field)) for field in required_response_fields):
        raise BejermanPurchaseError(
            "BEJERMAN_PURCHASE_RESPONSE_INCOMPLETE",
            "Bejerman respondió sin la identificación completa del RT registrado",
            status_code=502,
        )
    final_type = _text(parsed.get("comprobanteTipo")).upper()
    final_letter = _normalize_letter(
        parsed.get("comprobanteLetra"),
        default=DEFAULT_LETTER,
    )
    final_point = _normalize_document_digits(
        parsed.get("comprobantePtoVenta"),
        width=POINT_OF_SALE_WIDTH,
        field="point_of_sale",
        label="El punto de venta devuelto por Bejerman",
    )
    final_number = _normalize_document_digits(
        parsed.get("comprobanteNumero"),
        width=DOCUMENT_NUMBER_WIDTH,
        field="document_number",
        label="El número del RT devuelto por Bejerman",
    )
    requested_identity = (
        _text(entry.get("comprobanteTipo")).upper(),
        _normalize_letter(entry.get("comprobanteLetra"), default=DEFAULT_LETTER),
        _normalize_document_digits(
            entry.get("comprobantePtoVenta"),
            width=POINT_OF_SALE_WIDTH,
            field="point_of_sale",
            label="El punto de venta solicitado",
        ),
        _normalize_document_digits(
            entry.get("comprobanteNumero"),
            width=DOCUMENT_NUMBER_WIDTH,
            field="document_number",
            label="El número del RT solicitado",
        ),
    )
    response_identity = (final_type, final_letter, final_point, final_number)
    if response_identity != requested_identity:
        raise BejermanPurchaseError(
            "BEJERMAN_PURCHASE_RESPONSE_MISMATCH",
            (
                "Bejerman devolvió un documento distinto del RT externo solicitado; "
                "el lote requiere revisión"
            ),
            status_code=502,
        )
    remito_number = format_remito_number(
        final_type,
        final_letter,
        final_point,
        final_number,
    )
    response_payload = {
        "parsed": parsed,
        "raw": raw_response,
        "profile": built["profile"],
        "lineCount": built["lineCount"],
        "requestFingerprint": claimed["requestFingerprint"],
        "postconditionVerification": {
            "status": "pending",
            "overallStatus": "pending",
            "confirmed": False,
            "checkedAt": None,
            "document": {
                "status": "pending",
                "confirmed": False,
                "responseIdentityAccepted": True,
            },
            "stock": {
                "status": "pending",
                "confirmed": False,
                "depositCode": DEFAULT_DEPOSIT,
            },
            "partidas": {
                "status": "pending",
                "confirmed": False,
                "required": sum(
                    len(article.get("partidas") or [])
                    for article in (
                        claimed["requestPayload"]
                        .get("VerificationBaseline", {})
                        .get("articles", [])
                    )
                    if isinstance(article, dict)
                ),
                "confirmedCount": 0,
            },
        },
    }
    updated = _fetchone(
        """
        UPDATE bejerman_purchase_entries
           SET status = 'verifying',
               comprobante_tipo = %s,
               comprobante_letra = %s,
               comprobante_pto_venta = %s,
               comprobante_numero = %s,
               remito_number = %s,
               response_payload = %s::jsonb,
               updated_by_user_id = %s,
               last_error = NULL
         WHERE id = %s
           AND status = 'running'
           AND request_fingerprint = %s
        RETURNING id
        """,
        [
            final_type,
            final_letter,
            final_point,
            final_number,
            remito_number,
            _json_param(response_payload),
            actor_user_id,
            entry_id,
            claimed["requestFingerprint"],
        ],
    )
    if not updated:
        current = get_purchase_entry(entry_id, include_events=True)
        if current.get("status") in {"verifying", "generated"}:
            return current
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_FINALIZE_CONFLICT",
            "El lote cambió mientras Bejerman procesaba la registración; requiere revisión",
            status_code=409,
        )
    _event(
        entry_id,
        actor_user_id,
        "emission_accepted",
        metadata={
            "remitoNumber": remito_number,
            "requestFingerprint": claimed["requestFingerprint"],
            "postconditions": "pending",
        },
    )
    return get_purchase_entry(entry_id, include_events=True)


@transaction.atomic
def _record_purchase_emission_error(
    entry_id: str,
    actor_user_id: int | None,
    *,
    request_fingerprint: str,
    exc: Exception,
    raw_response: dict[str, Any] | None = None,
) -> str:
    requires_review = _ambiguous_emit_error(exc)
    next_status = "failed_review" if requires_review else "failed"
    response_payload: dict[str, Any] = {
        "error": str(exc),
        "requiresReview": requires_review,
        "requestFingerprint": request_fingerprint,
        "recordedAt": timezone.now().isoformat(),
    }
    if raw_response is not None:
        response_payload["raw"] = raw_response
    updated = _fetchone(
        """
        UPDATE bejerman_purchase_entries
           SET status = %s,
               response_payload = %s::jsonb,
               last_error = %s,
               updated_by_user_id = %s
         WHERE id = %s
           AND status = 'running'
           AND request_fingerprint = %s
        RETURNING id
        """,
        [
            next_status,
            _json_param(response_payload),
            str(exc),
            actor_user_id,
            entry_id,
            request_fingerprint,
        ],
    )
    if updated:
        _event(
            entry_id,
            actor_user_id,
            "emission_review_required" if requires_review else "emission_failed",
            note=str(exc),
            metadata={
                "requestFingerprint": request_fingerprint,
                "requiresReview": requires_review,
            },
        )
    return next_status


def emit_purchase_entry(
    entry_id: str,
    actor_user_id: int | None,
    payload: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    initial = get_purchase_entry(entry_id, include_events=True)
    if initial.get("status") == "generated":
        return initial
    _raise_for_locked_emission_status(initial)
    if not _purchase_emission_enabled():
        raise BejermanPurchaseError(
            "PURCHASE_EMISSION_DISABLED",
            "La registración de compras está deshabilitada en este entorno",
            status_code=503,
        )
    payload = payload or {}
    if as_nullable_bool(payload.get("confirmed")) is not True:
        raise BejermanPurchaseError(
            "PURCHASE_EMISSION_CONFIRMATION_REQUIRED",
            "Confirmá expresamente la registración del RT de compra",
            status_code=409,
        )
    expected_preflight_fingerprint = _text(
        payload.get("preflightFingerprint")
    ).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_preflight_fingerprint):
        raise BejermanPurchaseError(
            "PURCHASE_PREFLIGHT_REQUIRED",
            "Prevalidá nuevamente el lote antes de registrarlo",
            status_code=409,
        )
    numera_flex, emite_reg = _purchase_emission_options()
    sdk_client = client or BejermanSDKClient(
        company_key=initial.get("companyKey") or COMPANY_KEY,
        actor_user_id=actor_user_id,
    )

    validation = validate_purchase_entry(entry_id)
    if not validation["ok"]:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_INVALID",
            validation["errors"][0]["message"]
            if validation["errors"]
            else "El lote no es válido",
            status_code=409,
        )
    entry = validation["entry"]
    quantity_mismatches = [
        warning
        for warning in validation["warnings"]
        if warning.get("code") == "PURCHASE_ORDER_QUANTITY_MISMATCH"
    ]
    if quantity_mismatches and as_nullable_bool(payload.get("confirmedQuantityMismatch")) is not True:
        raise BejermanPurchaseError(
            "PURCHASE_QUANTITY_MISMATCH_CONFIRMATION_REQUIRED",
            quantity_mismatches[0]["message"] + "; confirmá expresamente para registrar igual",
            status_code=409,
        )
    duplicate = _remote_purchase_duplicate(entry, sdk_client)
    if duplicate:
        current = get_purchase_entry(entry_id, include_events=True)
        if current.get("status") == "generated":
            return current
        _raise_for_locked_emission_status(current)
        raise BejermanPurchaseError(
            "REMOTE_DUPLICATE",
            "Bejerman ya registra un RT con ese proveedor y número",
            status_code=409,
        )
    reference_data = _resolve_purchase_reference_data(
        entry,
        sdk_client,
        actor_user_id,
        capture_stock_baseline=False,
    )
    try:
        claimed = _claim_purchase_entry(
            entry_id,
            actor_user_id,
            reference_data,
            sdk_client,
            numera_flex=numera_flex,
            emite_reg=emite_reg,
            expected_preflight_fingerprint=expected_preflight_fingerprint,
        )
    except IntegrityError as exc:
        raise BejermanPurchaseError(
            "LOCAL_DUPLICATE",
            "Ya hay un RT local activo con ese proveedor y número",
            status_code=409,
        ) from exc
    if claimed["alreadyGenerated"]:
        return claimed["entry"]

    raw_response: dict[str, Any] | None = None
    try:
        raw_response = sdk_client.ingresar_lista_comprobantes_compras_json(
            [claimed["built"]["comprobante"]],
            numera_flex=numera_flex,
            emite_reg=emite_reg,
        )
        return _finalize_purchase_emission(
            entry_id,
            actor_user_id,
            claimed,
            raw_response,
        )
    except Exception as exc:
        next_status = _record_purchase_emission_error(
            entry_id,
            actor_user_id,
            request_fingerprint=claimed["requestFingerprint"],
            exc=exc,
            raw_response=raw_response,
        )
        code = (
            "BEJERMAN_PURCHASE_EMIT_REVIEW_REQUIRED"
            if next_status == "failed_review"
            else "BEJERMAN_PURCHASE_EMIT_FAILED"
        )
        message = str(exc)
        if next_status == "failed_review":
            message = (
                f"{message}. El resultado es incierto: verificá Bejerman antes de reabrir o reintentar"
            )
        raise BejermanPurchaseError(code, message, status_code=502) from exc


def _running_attempt_is_recent(row: dict[str, Any]) -> bool:
    last_attempt_at = row.get("last_attempt_at")
    if not last_attempt_at or not hasattr(last_attempt_at, "tzinfo"):
        return False
    grace_seconds = _bounded_int(
        getattr(
            settings,
            "BEJERMAN_PURCHASE_RUNNING_GRACE_SECONDS",
            DEFAULT_RUNNING_GRACE_SECONDS,
        ),
        default=DEFAULT_RUNNING_GRACE_SECONDS,
        minimum=0,
        maximum=3600,
    )
    return (timezone.now() - last_attempt_at).total_seconds() < grace_seconds


@transaction.atomic
def _store_purchase_verification(
    entry_id: str,
    actor_user_id: int | None,
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    row = _require_entry(entry_id, for_update=True)
    if row.get("status") == "generated":
        return {
            "outcome": "already_generated",
            "canReopen": False,
            "entry": _serialize_entry(row, include_events=True),
        }
    if row.get("status") not in {"running", "verifying", "failed_review"}:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_NOT_REVIEWABLE",
            "Sólo se puede verificar un lote en curso, verificando o pendiente de revisión",
            status_code=409,
        )

    checked_at = timezone.now().isoformat()
    existing_response = _json_value(row.get("response_payload"), {})
    existing_response = (
        dict(existing_response)
        if isinstance(existing_response, dict)
        else {}
    )
    confirmed_matches = [
        match
        for match in matches
        if _text(match.get("operationCode")).upper() == OPERATION_CODE
    ]
    verification = {
        "checkedAt": checked_at,
        "matchCount": len(matches),
        "confirmedMatchCount": len(confirmed_matches),
        "requestFingerprint": row.get("request_fingerprint") or "",
    }
    if len(matches) == 1 and len(confirmed_matches) == 1:
        match = confirmed_matches[0]
        final_type = _text(match.get("comprobanteTipo") or COMPROBANTE_TIPO).upper()
        final_letter = _normalize_letter(
            match.get("comprobanteLetra"),
            default=DEFAULT_LETTER,
        )
        final_point = _normalize_document_digits(
            match.get("comprobantePtoVenta"),
            width=POINT_OF_SALE_WIDTH,
            field="point_of_sale",
            label="El punto de venta verificado en Bejerman",
        )
        final_number = _normalize_document_digits(
            match.get("comprobanteNumero"),
            width=DOCUMENT_NUMBER_WIDTH,
            field="document_number",
            label="El número del RT verificado en Bejerman",
        )
        remito_number = format_remito_number(
            final_type,
            final_letter,
            final_point,
            final_number,
        )
        verification["outcome"] = "matched"
        response_payload = {
            **existing_response,
            "verification": verification,
            "reconciled": {
                "raw": match.get("raw") or {},
                "remitoNumber": remito_number,
            },
        }
        _execute(
            """
            UPDATE bejerman_purchase_entries
               SET status = 'verifying',
                   comprobante_tipo = %s,
                   comprobante_letra = %s,
                   comprobante_pto_venta = %s,
                   comprobante_numero = %s,
                   remito_number = %s,
                   response_payload = %s::jsonb,
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
                entry_id,
            ],
        )
        _event(
            entry_id,
            actor_user_id,
            "emission_reconciled",
            metadata={
                "remitoNumber": remito_number,
                "requestFingerprint": row.get("request_fingerprint") or "",
            },
        )
        return {
            "outcome": "matched",
            "canReopen": False,
            "entry": get_purchase_entry(entry_id, include_events=True),
        }

    outcome = "not_found" if not matches else "ambiguous"
    verification["outcome"] = outcome
    response_payload = {
        **existing_response,
        "verification": verification,
    }
    message = (
        "La verificación no encontró el RT en Bejerman"
        if outcome == "not_found"
        else "La verificación encontró más de un RT coincidente en Bejerman"
    )
    next_status = (
        "verifying"
        if row.get("status") == "verifying" and outcome == "not_found"
        else "failed_review"
    )
    _execute(
        """
        UPDATE bejerman_purchase_entries
           SET status = %s,
               response_payload = %s::jsonb,
               last_error = %s,
               updated_by_user_id = %s
         WHERE id = %s
        """,
        [
            next_status,
            _json_param(response_payload),
            message,
            actor_user_id,
            entry_id,
        ],
    )
    _event(
        entry_id,
        actor_user_id,
        "emission_not_found" if outcome == "not_found" else "emission_ambiguous",
        note=message,
        metadata={
            "matchCount": len(matches),
            "requestFingerprint": row.get("request_fingerprint") or "",
        },
    )
    return {
        "outcome": outcome,
        "canReopen": False,
        "entry": get_purchase_entry(entry_id, include_events=True),
    }


def _purchase_item_list(value: Any) -> list[dict[str, Any]]:
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            return []
    if isinstance(parsed, dict):
        for key in ("items", "Items", "Comprobante_Items", "Datos"):
            candidate = parsed.get(key)
            if isinstance(candidate, (list, str, dict)):
                return _purchase_item_list(candidate)
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _purchase_item_groups(items: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        article_code = as_string(
            first_value(
                item,
                (
                    "Item_CodigoArticulo",
                    "Articulo_Codigo",
                    "ArticuloCodigo",
                    "CodigoArticulo",
                ),
            )
        )
        deposit_code = as_string(
            first_value(
                item,
                ("Item_Deposito", "Deposito_Codigo", "DepositoCodigo"),
            )
        ).upper()
        partida = as_string(
            first_value(
                item,
                ("Item_Partida", "Partida_Codigo", "PartidaCodigo"),
            )
        )
        article_key = normalize_article_code(article_code)
        if not article_key:
            continue
        key = (article_key, deposit_code, _barcode_norm(partida))
        current = groups.setdefault(
            key,
            {
                "articleCode": article_code,
                "depositCode": deposit_code,
                "partida": partida,
                "quantity": Decimal("0"),
                "netValue": Decimal("0"),
                "lineCount": 0,
            },
        )
        current["quantity"] += (
            _decimal(
                first_value(
                    item,
                    ("Item_CantidadUM1", "CantidadUM1", "Cantidad"),
                ),
                Decimal("0"),
            )
            or Decimal("0")
        )
        current["netValue"] += (
            _decimal(
                first_value(
                    item,
                    (
                        "Item_ImporteItem",
                        "Item_ImporteTotal",
                        "ImporteItem",
                    ),
                ),
                Decimal("0"),
            )
            or Decimal("0")
        )
        current["lineCount"] += 1
    return groups


def _public_purchase_item_group(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "articleCode": item.get("articleCode") or "",
        "depositCode": item.get("depositCode") or "",
        "partida": item.get("partida") or "",
        "quantity": _public_decimal(item.get("quantity"), "0.0001"),
        "netValue": _public_decimal(item.get("netValue"), "0.01"),
        "lineCount": int(item.get("lineCount") or 0),
    }


def _purchase_document_evidence(
    entry: dict[str, Any],
    remote_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    request_payload = (
        entry.get("requestPayload")
        if isinstance(entry.get("requestPayload"), dict)
        else {}
    )
    expected_document = (
        request_payload.get("Comprobante")
        if isinstance(request_payload.get("Comprobante"), dict)
        else {}
    )
    expected_items = _purchase_item_list(
        expected_document.get("Comprobante_Items")
    )
    mc_matches = [
        match
        for match in remote_matches
        if _text(match.get("operationCode")).upper() == OPERATION_CODE
    ]
    identity_confirmed = len(remote_matches) == 1 and len(mc_matches) == 1
    evidence: dict[str, Any] = {
        "status": "pending",
        "confirmed": False,
        "identityConfirmed": identity_confirmed,
        "matchCount": len(remote_matches),
        "mcMatchCount": len(mc_matches),
        "expected": {
            "supplierCode": entry.get("supplierCode") or "",
            "type": entry.get("comprobanteTipo") or COMPROBANTE_TIPO,
            "letter": entry.get("comprobanteLetra") or DEFAULT_LETTER,
            "pointOfSale": entry.get("comprobantePtoVenta") or "",
            "number": entry.get("comprobanteNumero") or "",
            "operation": OPERATION_CODE,
            "itemCount": len(expected_items),
        },
        "items": {
            "status": "pending",
            "confirmed": False,
            "expectedLineCount": len(expected_items),
            "observedLineCount": 0,
            "expectedGroupCount": 0,
            "observedGroupCount": 0,
            "mismatches": [],
        },
    }
    if len(remote_matches) > 1:
        evidence["status"] = "ambiguous"
        return evidence
    if not remote_matches:
        return evidence
    if not identity_confirmed:
        evidence["status"] = "mismatch"
        return evidence

    raw_match = (
        remote_matches[0].get("raw")
        if isinstance(remote_matches[0].get("raw"), dict)
        else {}
    )
    observed_items = _purchase_item_list(
        first_value(
            raw_match,
            ("Comprobante_Items", "Items", "ComprobanteItems"),
        )
    )
    if not observed_items:
        evidence["observed"] = {
            "operation": remote_matches[0].get("operationCode") or "",
            "itemCount": 0,
        }
        evidence["items"] = {
            **evidence["items"],
            "status": "pending",
            "observedLineCount": 0,
        }
        return evidence
    expected_groups = _purchase_item_groups(expected_items)
    observed_groups = _purchase_item_groups(observed_items)
    expected_mapped_count = sum(
        int(group.get("lineCount") or 0)
        for group in expected_groups.values()
    )
    observed_mapped_count = sum(
        int(group.get("lineCount") or 0)
        for group in observed_groups.values()
    )
    expected_public = [
        _public_purchase_item_group(expected_groups[key])
        for key in sorted(expected_groups)
    ]
    observed_public = [
        _public_purchase_item_group(observed_groups[key])
        for key in sorted(observed_groups)
    ]
    mismatches: list[dict[str, Any]] = []
    if expected_mapped_count != len(expected_items):
        mismatches.append(
            {
                "reason": "expected_unmapped",
                "expected": {
                    "lineCount": len(expected_items),
                    "mappedLineCount": expected_mapped_count,
                },
                "observed": None,
            }
        )
    if observed_mapped_count != len(observed_items):
        mismatches.append(
            {
                "reason": "unexpected_unmapped",
                "expected": None,
                "observed": {
                    "lineCount": len(observed_items),
                    "mappedLineCount": observed_mapped_count,
                },
            }
        )
    for key in sorted(set(expected_groups) | set(observed_groups)):
        expected = expected_groups.get(key)
        observed = observed_groups.get(key)
        mismatch = ""
        if expected is None:
            mismatch = "unexpected"
        elif observed is None:
            mismatch = "missing"
        else:
            quantity_delta = observed["quantity"] - expected["quantity"]
            value_delta = observed["netValue"] - expected["netValue"]
            if abs(quantity_delta) > QTY_QUANT:
                mismatch = "quantity"
            elif abs(value_delta) > MONEY_QUANT:
                mismatch = "value"
        if mismatch:
            mismatches.append(
                {
                    "reason": mismatch,
                    "expected": (
                        _public_purchase_item_group(expected)
                        if expected is not None
                        else None
                    ),
                    "observed": (
                        _public_purchase_item_group(observed)
                        if observed is not None
                        else None
                    ),
                }
            )

    items_confirmed = bool(
        expected_items
        and observed_items
        and expected_mapped_count == len(expected_items)
        and observed_mapped_count == len(observed_items)
        and not mismatches
    )
    evidence["items"] = {
        "status": "confirmed" if items_confirmed else "mismatch",
        "confirmed": items_confirmed,
        "expectedLineCount": len(expected_items),
        "observedLineCount": len(observed_items),
        "expectedGroupCount": len(expected_groups),
        "observedGroupCount": len(observed_groups),
        "expectedFingerprint": _request_fingerprint(expected_public),
        "observedFingerprint": _request_fingerprint(observed_public),
        "mismatches": mismatches[:200],
        "mismatchCount": len(mismatches),
    }
    evidence["observed"] = {
        "operation": remote_matches[0].get("operationCode") or "",
        "itemCount": len(observed_items),
    }
    expected_total = _decimal(
        expected_document.get("Comprobante_ImporteTotal"),
        None,
    )
    observed_total = _decimal(
        first_value(
            raw_match,
            ("Comprobante_ImporteTotal", "ImporteTotal", "Total"),
        ),
        None,
    )
    if expected_total is None or observed_total is None:
        total_status = "pending"
    elif abs(observed_total - expected_total) <= MONEY_QUANT:
        total_status = "confirmed"
    else:
        total_status = "mismatch"
    total_confirmed = total_status == "confirmed"
    expected_date = normalize_date(
        expected_document.get("Comprobante_FechaEmision")
        or entry.get("issueDate")
    )
    observed_date = normalize_date(
        first_value(
            raw_match,
            ("Comprobante_FechaEmision", "FechaEmision"),
        )
    )
    if not expected_date or not observed_date:
        date_status = "pending"
    elif observed_date == expected_date:
        date_status = "confirmed"
    else:
        date_status = "mismatch"
    date_confirmed = date_status == "confirmed"
    evidence["expected"]["totalValue"] = (
        _public_decimal(expected_total)
        if expected_total is not None
        else None
    )
    evidence["expected"]["issueDate"] = expected_date
    evidence["observed"].update(
        {
            "totalValue": (
                _public_decimal(observed_total)
                if observed_total is not None
                else None
            ),
            "issueDate": observed_date,
        }
    )
    evidence["totalConfirmed"] = total_confirmed
    evidence["dateConfirmed"] = date_confirmed
    evidence["totalStatus"] = total_status
    evidence["dateStatus"] = date_status
    evidence["confirmed"] = (
        identity_confirmed
        and items_confirmed
        and total_confirmed
        and date_confirmed
    )
    if evidence["confirmed"]:
        evidence["status"] = "confirmed"
    elif (
        evidence["items"].get("status") == "mismatch"
        or total_status == "mismatch"
        or date_status == "mismatch"
    ):
        evidence["status"] = "mismatch"
    else:
        evidence["status"] = "pending"
    return evidence


def _quantity_evidence_status(
    observed_delta: Decimal,
    expected_delta: Decimal,
) -> str:
    difference = observed_delta - expected_delta
    if abs(difference) <= QTY_QUANT:
        return "confirmed"
    if difference < 0:
        return "pending"
    return "ambiguous"


def _purchase_postcondition_snapshot(
    entry: dict[str, Any],
    client: BejermanSDKClient,
    *,
    matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checked_at = timezone.now().isoformat()
    remote_matches = (
        matches
        if matches is not None
        else _remote_purchase_matches(entry, client)
    )
    document = _purchase_document_evidence(entry, remote_matches)

    request_payload = (
        entry.get("requestPayload")
        if isinstance(entry.get("requestPayload"), dict)
        else {}
    )
    baseline = (
        request_payload.get("VerificationBaseline")
        if isinstance(request_payload.get("VerificationBaseline"), dict)
        else {}
    )
    baseline_articles = (
        baseline.get("articles")
        if isinstance(baseline.get("articles"), list)
        else []
    )
    deposit_code = _purchase_deposit_code(
        baseline.get("depositCode")
        or entry.get("depositCode")
        or DEFAULT_DEPOSIT
    )

    stock_rows = build_stock_rows(
        client.obtener_stock_deposito(deposit_code)
    )
    current_totals = _stock_totals_by_article(stock_rows, deposit_code)
    stock_by_partida = _stock_totals_by_article_partida(
        stock_rows,
        deposit_code,
    )

    expected_partidas: list[dict[str, Any]] = []
    for article in baseline_articles:
        if not isinstance(article, dict) or article.get("requiresPartida") is not True:
            continue
        for raw_partida in article.get("partidas") or []:
            if isinstance(raw_partida, dict):
                partida_item = dict(raw_partida)
            else:
                partida_item = {
                    "partida": _text(raw_partida),
                    "baselineRealQuantity": None,
                    "expectedIncrement": None,
                }
            if _text(partida_item.get("partida")):
                expected_partidas.append(
                    {
                        **partida_item,
                        "articleCode": article.get("articleCode") or "",
                    }
                )

    partida_catalog: set[tuple[str, str]] = set()
    if expected_partidas:
        for record in records_from_response(client.obtener_partidas()):
            partida = _map_purchase_partida(record)
            if not partida:
                continue
            partida_catalog.add(
                (
                    _text(partida.get("depositCode")).upper(),
                    _barcode_norm(partida.get("partida")),
                )
            )

    article_results: list[dict[str, Any]] = []
    partida_discrepancies: list[dict[str, Any]] = []
    confirmed_partidas = 0
    ambiguous_inventory = False
    pending_inventory = False
    for article in baseline_articles:
        if not isinstance(article, dict):
            continue
        article_code = _text(article.get("articleCode"))
        article_key = normalize_article_code(article_code)
        baseline_quantity = _decimal(
            article.get("baselineRealQuantity"),
            None,
        )
        expected_increment = (
            _decimal(article.get("expectedIncrement"), Decimal("0"))
            or Decimal("0")
        )
        current_quantity = current_totals.get(article_key, Decimal("0"))
        observed_increment = (
            current_quantity - baseline_quantity
            if baseline_quantity is not None
            else None
        )
        requires_partida = article.get("requiresPartida") is True
        partida_items: list[dict[str, Any]] = []

        if requires_partida:
            for raw_partida in article.get("partidas") or []:
                if isinstance(raw_partida, dict):
                    partida_text = _text(raw_partida.get("partida"))
                    partida_baseline = _decimal(
                        raw_partida.get("baselineRealQuantity"),
                        None,
                    )
                    partida_expected = _decimal(
                        raw_partida.get("expectedIncrement"),
                        None,
                    )
                else:
                    partida_text = _text(raw_partida)
                    partida_baseline = None
                    partida_expected = None
                partida_norm = _barcode_norm(partida_text)
                current_partida = stock_by_partida.get(
                    (article_key, partida_norm),
                    Decimal("0"),
                )
                catalog_confirmed = (
                    deposit_code,
                    partida_norm,
                ) in partida_catalog
                if partida_baseline is None or partida_expected is None:
                    quantity_status = (
                        "confirmed"
                        if current_partida > 0
                        else "pending"
                    )
                    partida_delta = None
                else:
                    partida_delta = current_partida - partida_baseline
                    quantity_status = _quantity_evidence_status(
                        partida_delta,
                        partida_expected,
                    )
                confirmed = (
                    quantity_status == "confirmed"
                    and catalog_confirmed
                )
                if confirmed:
                    confirmed_partidas += 1
                else:
                    if quantity_status == "ambiguous":
                        ambiguous_inventory = True
                    else:
                        pending_inventory = True
                    partida_discrepancies.append(
                        {
                            "articleCode": article_code,
                            "partida": partida_text,
                            "catalogConfirmed": catalog_confirmed,
                            "quantityStatus": quantity_status,
                            "baselineRealQuantity": (
                                _public_decimal(partida_baseline, "0.0001")
                                if partida_baseline is not None
                                else None
                            ),
                            "expectedIncrement": (
                                _public_decimal(partida_expected, "0.0001")
                                if partida_expected is not None
                                else None
                            ),
                            "currentRealQuantity": _public_decimal(
                                current_partida,
                                "0.0001",
                            ),
                            "observedIncrement": (
                                _public_decimal(partida_delta, "0.0001")
                                if partida_delta is not None
                                else None
                            ),
                        }
                    )
                partida_items.append(
                    {
                        "partida": partida_text,
                        "confirmed": confirmed,
                        "quantityStatus": quantity_status,
                        "catalogConfirmed": catalog_confirmed,
                    }
                )
            article_status = (
                "confirmed"
                if partida_items
                and all(item.get("confirmed") is True for item in partida_items)
                else (
                    "ambiguous"
                    if any(
                        item.get("quantityStatus") == "ambiguous"
                        for item in partida_items
                    )
                    else "pending"
                )
            )
        elif baseline_quantity is None:
            article_status = "unavailable"
            pending_inventory = True
        else:
            article_status = _quantity_evidence_status(
                observed_increment or Decimal("0"),
                expected_increment,
            )
            if article_status == "ambiguous":
                ambiguous_inventory = True
            elif article_status != "confirmed":
                pending_inventory = True

        article_results.append(
            {
                "articleCode": article_code,
                "trackingMode": (
                    article.get("trackingMode")
                    or ("partida" if requires_partida else "none")
                ),
                "requiresPartida": requires_partida,
                "status": article_status,
                "confirmed": article_status == "confirmed",
                "baselineRealQuantity": (
                    _public_decimal(baseline_quantity, "0.0001")
                    if baseline_quantity is not None
                    else None
                ),
                "expectedIncrement": _public_decimal(
                    expected_increment,
                    "0.0001",
                ),
                "currentRealQuantity": _public_decimal(
                    current_quantity,
                    "0.0001",
                ),
                "observedIncrement": (
                    _public_decimal(observed_increment, "0.0001")
                    if observed_increment is not None
                    else None
                ),
                "requiredPartidas": len(partida_items),
                "confirmedPartidas": sum(
                    1
                    for item in partida_items
                    if item.get("confirmed") is True
                ),
            }
        )

    baseline_available = bool(baseline_articles)
    stock_confirmed = bool(
        baseline_available
        and article_results
        and all(item.get("confirmed") is True for item in article_results)
    )
    if stock_confirmed:
        stock_status = "confirmed"
    elif not baseline_available:
        stock_status = "unavailable"
    elif ambiguous_inventory:
        stock_status = "ambiguous"
    else:
        stock_status = "pending"

    required_partidas = len(expected_partidas)
    partidas_confirmed = (
        required_partidas == 0
        or confirmed_partidas == required_partidas
    )
    if required_partidas == 0:
        partidas_status = "not_applicable"
    elif partidas_confirmed:
        partidas_status = "confirmed"
    elif ambiguous_inventory:
        partidas_status = "ambiguous"
    else:
        partidas_status = "pending"
    partidas = {
        "status": partidas_status,
        "confirmed": partidas_confirmed,
        "required": required_partidas,
        "confirmedCount": confirmed_partidas,
        "missing": partida_discrepancies[:500],
        "discrepancyCount": len(partida_discrepancies),
        "expectedFingerprint": _request_fingerprint(
            sorted(
                (
                    normalize_article_code(item.get("articleCode")),
                    _barcode_norm(item.get("partida")),
                )
                for item in expected_partidas
            )
        ),
    }
    stock = {
        "status": stock_status,
        "confirmed": stock_confirmed,
        "baselineAvailable": baseline_available,
        "depositCode": deposit_code,
        "articles": article_results,
    }
    confirmed = bool(
        document.get("confirmed") is True
        and stock_confirmed
        and partidas_confirmed
    )
    if confirmed:
        overall_status = "confirmed"
    elif (
        document.get("status") in {"ambiguous", "mismatch"}
        or (
            document.get("status") == "pending"
            and stock_confirmed
            and document.get("identityConfirmed") is not True
        )
        or stock_status == "ambiguous"
        or partidas_status == "ambiguous"
    ):
        overall_status = "ambiguous"
    elif stock_status == "unavailable":
        overall_status = "unavailable"
    else:
        overall_status = "pending"
    return {
        "status": overall_status,
        "overallStatus": overall_status,
        "confirmed": confirmed,
        "checkedAt": checked_at,
        "document": document,
        "header": document,
        "stock": stock,
        "partidas": partidas,
    }


@transaction.atomic
def _store_purchase_postcondition_verification(
    entry_id: str,
    actor_user_id: int | None,
    snapshot: dict[str, Any],
    *,
    outcome: str,
) -> dict[str, Any]:
    row = _require_entry(entry_id, for_update=True)
    if row.get("status") not in {
        "running",
        "verifying",
        "generated",
        "failed_review",
    }:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_NOT_REVIEWABLE",
            "El lote no está en un estado verificable",
            status_code=409,
        )
    response_payload = _json_value(row.get("response_payload"), {})
    response_payload = (
        dict(response_payload)
        if isinstance(response_payload, dict)
        else {}
    )
    previous_snapshot = (
        response_payload.get("postconditionVerification")
        if isinstance(
            response_payload.get("postconditionVerification"),
            dict,
        )
        else {}
    )
    if (
        row.get("status") == "generated"
        and previous_snapshot.get("confirmed") is True
    ):
        return {
            "outcome": "already_generated",
            "canReopen": False,
            "postconditions": previous_snapshot,
            "entry": _serialize_entry(row, include_events=True),
        }
    snapshot = {
        **snapshot,
        "attempt": int(previous_snapshot.get("attempt") or 0) + 1,
        "nextCheckAt": None,
        "exhausted": False,
    }
    response_payload["postconditionVerification"] = snapshot
    snapshot_status = _text(
        snapshot.get("overallStatus") or snapshot.get("status")
    ).lower() or "pending"
    confirmed = snapshot.get("confirmed") is True
    if confirmed:
        next_status = "generated"
        last_error = None
        _execute(
            """
            UPDATE bejerman_purchase_entries
               SET status = 'generated',
                   response_payload = %s::jsonb,
                   generated_by_user_id = COALESCE(generated_by_user_id, %s),
                   generated_at = COALESCE(generated_at, CURRENT_TIMESTAMP),
                   updated_by_user_id = %s,
                   last_error = NULL
             WHERE id = %s
            """,
            [
                _json_param(response_payload),
                actor_user_id,
                actor_user_id,
                entry_id,
            ],
        )
    else:
        if row.get("status") == "generated":
            next_status = "generated"
        elif snapshot_status == "ambiguous":
            next_status = "failed_review"
        elif row.get("status") == "verifying":
            next_status = "verifying"
        else:
            next_status = "failed_review"
        last_error = {
            "ambiguous": (
                "La verificación encontró evidencia contradictoria; "
                "el lote requiere revisión manual"
            ),
            "unavailable": (
                "No se pudo completar la verificación en Bejerman; "
                "el lote permanece bloqueado"
            ),
        }.get(
            snapshot_status,
            "Aún faltan postcondiciones de Bejerman; volvé a verificar el lote",
        )
        _execute(
            """
            UPDATE bejerman_purchase_entries
               SET status = %s,
                   response_payload = %s::jsonb,
                   updated_by_user_id = %s,
                   last_error = %s
             WHERE id = %s
            """,
            [
                next_status,
                _json_param(response_payload),
                actor_user_id,
                last_error,
                entry_id,
            ],
        )
    event_type = {
        "confirmed": "postconditions_verified",
        "ambiguous": "postconditions_ambiguous",
        "unavailable": "postconditions_unavailable",
    }.get(snapshot_status, "postconditions_pending")
    _event(
        entry_id,
        actor_user_id,
        event_type,
        note=last_error or "",
        metadata={
            "status": snapshot_status,
            "nextEntryStatus": next_status,
            "headerConfirmed": (
                snapshot.get("document") or snapshot.get("header") or {}
            ).get("confirmed"),
            "stockConfirmed": (
                snapshot.get("stock") or {}
            ).get("confirmed"),
            "requiredPartidas": (
                snapshot.get("partidas") or {}
            ).get("required"),
            "confirmedPartidas": (
                snapshot.get("partidas") or {}
            ).get("confirmedCount"),
            "attempt": snapshot.get("attempt"),
        },
    )
    return {
        "outcome": "verified" if confirmed else snapshot_status,
        "canReopen": False,
        "postconditions": snapshot,
        "entry": get_purchase_entry(entry_id, include_events=True),
    }


def _purchase_unavailable_snapshot(
    entry: dict[str, Any],
    *,
    matches: list[dict[str, Any]] | None,
    scope: str,
    exc: Exception,
) -> dict[str, Any]:
    checked_at = timezone.now().isoformat()
    request_payload = (
        entry.get("requestPayload")
        if isinstance(entry.get("requestPayload"), dict)
        else {}
    )
    baseline = (
        request_payload.get("VerificationBaseline")
        if isinstance(request_payload.get("VerificationBaseline"), dict)
        else {}
    )
    document = (
        _purchase_document_evidence(entry, matches)
        if matches is not None
        else {
            "status": "unavailable",
            "confirmed": False,
            "matchCount": None,
            "mcMatchCount": None,
        }
    )
    return {
        "status": "unavailable",
        "overallStatus": "unavailable",
        "confirmed": False,
        "checkedAt": checked_at,
        "unavailableScope": scope,
        "error": str(exc),
        "document": document,
        "header": document,
        "stock": {
            "status": "unavailable",
            "confirmed": False,
            "baselineAvailable": bool(baseline.get("articles")),
            "depositCode": entry.get("depositCode") or DEFAULT_DEPOSIT,
            "articles": [],
        },
        "partidas": {
            "status": "unavailable",
            "confirmed": False,
            "required": 0,
            "confirmedCount": 0,
            "missing": [],
        },
    }


def verify_purchase_entry(
    entry_id: str,
    actor_user_id: int | None,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    row = _require_entry(entry_id)
    if row.get("status") not in {
        "running",
        "verifying",
        "failed_review",
        "generated",
    }:
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_NOT_REVIEWABLE",
            (
                "Sólo se puede verificar un lote en curso, verificando, "
                "pendiente de revisión o registrado"
            ),
            status_code=409,
        )
    if row.get("status") == "running" and _running_attempt_is_recent(row):
        raise BejermanPurchaseError(
            "PURCHASE_ENTRY_EMISSION_RUNNING",
            "La registración todavía puede estar en curso; esperá antes de verificarla",
            status_code=409,
        )
    entry = _serialize_entry(row)
    existing_response = (
        entry.get("responsePayload")
        if isinstance(entry.get("responsePayload"), dict)
        else {}
    )
    existing_snapshot = (
        existing_response.get("postconditionVerification")
        if isinstance(
            existing_response.get("postconditionVerification"),
            dict,
        )
        else {}
    )
    if (
        row.get("status") == "generated"
        and existing_snapshot.get("confirmed") is True
    ):
        return {
            "outcome": "already_generated",
            "canReopen": False,
            "postconditions": existing_snapshot,
            "entry": get_purchase_entry(entry_id, include_events=True),
        }
    sdk_client = client or BejermanSDKClient(
        company_key=entry.get("companyKey") or COMPANY_KEY,
        actor_user_id=actor_user_id,
    )
    try:
        matches = _remote_purchase_matches(entry, sdk_client)
    except Exception as exc:
        snapshot = _purchase_unavailable_snapshot(
            entry,
            matches=None,
            scope="document",
            exc=exc,
        )
        return _store_purchase_postcondition_verification(
            entry_id,
            actor_user_id,
            snapshot,
            outcome="unavailable",
        )
    try:
        snapshot = _purchase_postcondition_snapshot(
            entry,
            sdk_client,
            matches=matches,
        )
    except Exception as exc:
        snapshot = _purchase_unavailable_snapshot(
            entry,
            matches=matches,
            scope="inventory",
            exc=exc,
        )
    if row.get("status") != "generated":
        _store_purchase_verification(
            entry_id,
            actor_user_id,
            matches,
        )
    return _store_purchase_postcondition_verification(
        entry_id,
        actor_user_id,
        snapshot,
        outcome=(
            "verified"
            if snapshot.get("confirmed") is True
            else _text(
                snapshot.get("overallStatus")
                or snapshot.get("status")
            ).lower()
            or "pending"
        ),
    )


@transaction.atomic
def reopen_purchase_entry(
    entry_id: str,
    actor_user_id: int | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_entry(entry_id, for_update=True)
    raise BejermanPurchaseError(
        "PURCHASE_REOPEN_MANUAL_REVIEW_REQUIRED",
        (
            "La reapertura automática está deshabilitada: un resultado incierto "
            "de COMPRAS debe auditarse manualmente para evitar duplicar el RT"
        ),
        status_code=409,
    )


def list_purchase_history(
    params: dict[str, Any] | None = None,
    client: BejermanSDKClient | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    params = params or {}
    company = _resolve_purchase_company(params)
    filters = [bejerman_filter("Comprobante_Tipo", "IGUAL", _text(params.get("tipo")) or COMPROBANTE_TIPO)]
    # Igual que en list_purchase_orders: Proveedor_Codigo es un filtro exacto y
    # Bejerman guarda el código con padding de espacios, no recortarlo acá.
    raw_history_supplier_code = _raw_text(_get(params, "supplierCode", "proveedor"))
    if raw_history_supplier_code.strip():
        filters.append(bejerman_filter("Proveedor_Codigo", "IGUAL", raw_history_supplier_code))
    if _text(_get(params, "dateFrom", "desde")):
        filters.append(bejerman_filter("Comprobante_FechaEmision", "MAYOR O IGUAL", _text(_get(params, "dateFrom", "desde"))))
    if _text(_get(params, "dateTo", "hasta")):
        filters.append(bejerman_filter("Comprobante_FechaEmision", "MENOR O IGUAL", _text(_get(params, "dateTo", "hasta"))))
    response = (client or BejermanSDKClient(company_key=company.key, actor_user_id=actor_user_id)).list_comprobantes_compras(filters)
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
    return {
        "items": items[:limit],
        "companyKey": company.key,
        "bejermanEnvironment": _purchase_environment(),
        "emissionEnabled": _purchase_emission_enabled(),
    }
