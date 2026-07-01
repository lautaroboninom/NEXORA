from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .bejerman_companies import company_for_key
from .bejerman_ris import equipment_suggestion_from_bejerman_article, find_customer_suggestion
from .bejerman_sdk import (
    BejermanSDKClient,
    as_number,
    as_string,
    build_document_id,
    build_serial_lookup_sales_filters,
    comprobante_id_of,
    document_number_from_parts,
    first_record,
    first_value,
    headers_from_sales_list,
    normalize_date,
    normalize_search,
    normalize_serial_for_lookup,
)
from .customer_bejerman_sync import (
    BEJERMAN_COMPANY_CONTEXT_KEY,
    BEJERMAN_CUSTOMER_COLUMNS,
    _execute_write,
    _sync_values,
    bejerman_customer_details_from_record,
    clean,
    clean_upper,
    digits_only,
    table_columns,
)
from .warranty import compute_warranty_from_sale_date


SALE_ITEM_TABLE = "bejerman_sale_items"

CUSTOMER_CUIT_ALIASES = (
    "Cliente_NroDocumento",
    "Cliente_CUIT",
    "Cliente_Cuit",
    "Cliente_NumeroDocumento",
    "Cliente_NroDoc",
    "CUIT",
    "Cuit",
    "TaxId",
)


def _setting(name: str, default: Any = "") -> Any:
    return getattr(settings, name, default)


def _json_db_value(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _parse_json_db(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _date_to_iso(value: Any) -> str | None:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return normalize_date(value)


def _parse_iso_date(value: Any, fallback: date) -> date:
    parsed = normalize_date(value)
    if not parsed:
        return fallback
    try:
        return date.fromisoformat(parsed)
    except ValueError:
        return fallback


def _date_windows(date_from: str, date_to: str, chunk_days: int) -> list[tuple[str, str]]:
    end = _parse_iso_date(date_to, timezone.localdate())
    start = _parse_iso_date(date_from, end)
    if start > end:
        start, end = end, start
    chunk = max(1, int(chunk_days or 31))
    windows: list[tuple[str, str]] = []
    current_end = end
    while current_end >= start:
        current_start = max(start, current_end - timedelta(days=chunk - 1))
        windows.append((current_start.isoformat(), current_end.isoformat()))
        current_end = current_start - timedelta(days=1)
    return windows


def _decimal_to_float(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


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
                return bool(cur.fetchone())
            tables = connection.introspection.table_names(cur)
        return table_name in tables
    except Exception:
        return False


def _company_key_from_client(client: BejermanSDKClient) -> str:
    marker = as_string(getattr(client, "company_key", "")) or as_string(getattr(client, "company", ""))
    company = company_for_key(marker, default=None)
    if company:
        return company.key
    return marker.upper() or "SEPID"


def _sale_item_key(item: dict[str, Any]) -> str:
    parts = [
        item.get("company_key") or "",
        item.get("comprobante_id") or "",
        item.get("document_type") or "",
        item.get("document_letter") or "",
        item.get("document_point_of_sale") or "",
        item.get("document_number") or "",
        item.get("issue_date") or "",
        item.get("article_code") or "",
        item.get("item_partida") or "",
        str(item.get("line_index") if item.get("line_index") is not None else ""),
    ]
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sale_item_from_detail(
    detail: dict[str, Any],
    raw_item: dict[str, Any],
    *,
    company_key: str,
    line_index: int,
    header: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    header = header or {}
    document = {**header, **(detail or {})}
    partida = as_string(first_value(raw_item, ("Item_Partida", "Partida", "NroPartida", "Lote")))
    normalized_serial = normalize_serial_for_lookup(partida)
    if not normalized_serial:
        return None

    doc_type = as_string(first_value(document, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))).upper()
    letter = as_string(first_value(document, ("Comprobante_Letra", "Letra"))).upper()
    point = as_string(first_value(document, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
    number = as_string(first_value(document, ("Comprobante_Numero", "Numero", "NroComprobante")))
    issue_date = normalize_date(first_value(document, ("Comprobante_FechaEmision", "FechaEmision")))
    customer_code = as_string(first_value(document, ("Cliente_Codigo", "ClienteCodigo", "CodCliente")))
    customer_cuit = digits_only(first_value(document, CUSTOMER_CUIT_ALIASES))
    article_code = as_string(first_value(raw_item, ("Item_CodigoArticulo", "CodigoArticulo", "Articulo_Codigo")))
    document_id = (
        build_document_id({"t": doc_type, "n": number, "l": letter, "p": point, "f": issue_date, "c": customer_code})
        if doc_type and number and point
        else None
    )

    item = {
        "company_key": clean_upper(company_key) or "SEPID",
        "comprobante_id": comprobante_id_of(document),
        "document_id": document_id,
        "document_type": doc_type,
        "document_letter": letter,
        "document_point_of_sale": point,
        "document_number": number,
        "document_label": document_number_from_parts(doc_type, letter, point, number),
        "issue_date": issue_date,
        "customer_code": customer_code,
        "customer_name": as_string(first_value(document, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial"))),
        "customer_cuit": customer_cuit,
        "article_code": article_code,
        "article_description": as_string(
            first_value(raw_item, ("Item_DescripArticulo", "Item_DescripcionArticulo", "Descripcion"))
        ),
        "item_partida": partida,
        "normalized_serial": normalized_serial,
        "quantity": as_number(first_value(raw_item, ("Item_CantidadUM1", "CantidadUM1", "Cantidad"))),
        "unit_price": as_number(first_value(raw_item, ("Item_PrecioUnitario", "PrecioUnitario", "Precio"))),
        "total_amount": as_number(first_value(raw_item, ("Item_ImporteTotal", "ImporteTotal", "Total"))),
        "currency": as_string(first_value(document, ("Comprobante_Moneda", "Moneda"))),
        "line_index": int(line_index),
        "raw_document": document,
        "raw_item": raw_item,
    }
    item["sale_item_key"] = _sale_item_key(item)
    return item


def sale_items_from_detail_response(
    detail_response: dict[str, Any],
    *,
    company_key: str,
    header: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    detail = first_record((detail_response or {}).get("DatosJSON"))
    if not isinstance(detail, dict):
        return []
    items = detail.get("Comprobante_Items") or detail.get("Items") or []
    if not isinstance(items, list):
        return []
    parsed: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            continue
        item = sale_item_from_detail(detail, raw_item, company_key=company_key, line_index=index, header=header)
        if item:
            parsed.append(item)
    return parsed


def upsert_sale_items(items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    if not _table_exists(SALE_ITEM_TABLE):
        raise RuntimeError("Falta la tabla bejerman_sale_items. Ejecuta apply_bejerman_sale_items_schema.")

    columns = [
        "sale_item_key",
        "company_key",
        "comprobante_id",
        "document_id",
        "document_type",
        "document_letter",
        "document_point_of_sale",
        "document_number",
        "document_label",
        "issue_date",
        "customer_code",
        "customer_name",
        "customer_cuit",
        "article_code",
        "article_description",
        "item_partida",
        "normalized_serial",
        "quantity",
        "unit_price",
        "total_amount",
        "currency",
        "line_index",
        "raw_document",
        "raw_item",
    ]
    updated = 0
    with transaction.atomic():
        with connection.cursor() as cur:
            for item in items:
                params = []
                placeholders = []
                for column in columns:
                    value = item.get(column)
                    if column in {"raw_document", "raw_item"}:
                        params.append(_json_db_value(value))
                        placeholders.append("%s::jsonb" if connection.vendor == "postgresql" else "%s")
                    else:
                        params.append(value)
                        placeholders.append("%s")
                if connection.vendor == "postgresql":
                    update_sets = [
                        f"{column}=EXCLUDED.{column}"
                        for column in columns
                        if column not in {"sale_item_key"}
                    ]
                    update_sets.append("synced_at=CURRENT_TIMESTAMP")
                    cur.execute(
                        f"""
                        INSERT INTO {SALE_ITEM_TABLE} ({', '.join(columns)})
                        VALUES ({', '.join(placeholders)})
                        ON CONFLICT (sale_item_key) DO UPDATE
                           SET {', '.join(update_sets)}
                        """,
                        params,
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT OR REPLACE INTO {SALE_ITEM_TABLE} ({', '.join(columns)})
                        VALUES ({', '.join(placeholders)})
                        """,
                        params,
                    )
                updated += 1
    return updated


def sale_payload_from_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"found": False, "source": "bejerman_sale_cache"}
    issue_date = _date_to_iso(row.get("issue_date"))
    synced_at = _date_to_iso(row.get("synced_at"))
    raw_document = _parse_json_db(row.get("raw_document"))
    raw_item = _parse_json_db(row.get("raw_item"))
    return {
        "found": True,
        "source": "bejerman_sale_cache",
        "cacheSaleItemId": row.get("sale_item_key") or "",
        "companyKey": row.get("company_key") or "",
        "serial": row.get("item_partida") or "",
        "normalizedSerial": row.get("normalized_serial") or "",
        "articleCode": row.get("article_code") or "",
        "articleDescription": row.get("article_description") or "",
        "customerCode": row.get("customer_code") or "",
        "customerName": row.get("customer_name") or "",
        "customerCuit": row.get("customer_cuit") or "",
        "issueDate": issue_date,
        "documentType": row.get("document_type") or "",
        "documentLetter": row.get("document_letter") or "",
        "documentPointOfSale": row.get("document_point_of_sale") or "",
        "documentNumber": row.get("document_number") or "",
        "documentId": row.get("document_id") or "",
        "documentLabel": row.get("document_label") or "",
        "itemPartida": row.get("item_partida") or "",
        "itemQuantity": _decimal_to_float(row.get("quantity")),
        "unitPrice": _decimal_to_float(row.get("unit_price")),
        "totalAmount": _decimal_to_float(row.get("total_amount")),
        "currency": row.get("currency") or "",
        "comprobanteId": row.get("comprobante_id") or "",
        "syncedAt": synced_at,
        "rawDocument": raw_document,
        "rawItem": raw_item,
    }


def find_latest_sale_by_serial(serial: str, *, company_key: str | None = None) -> dict[str, Any]:
    normalized = normalize_serial_for_lookup(serial)
    if not normalized:
        return {"found": False, "source": "bejerman_sale_cache", "serial": serial, "normalizedSerial": normalized}
    if not _table_exists(SALE_ITEM_TABLE):
        return {"found": False, "source": "bejerman_sale_cache", "serial": serial, "normalizedSerial": normalized}

    where = ["normalized_serial = %s"]
    params: list[Any] = [normalized]
    if clean(company_key):
        where.append("UPPER(company_key) = UPPER(%s)")
        params.append(clean(company_key))
    order_sql = (
        "issue_date DESC NULLS LAST, synced_at DESC NULLS LAST, created_at DESC NULLS LAST"
        if connection.vendor == "postgresql"
        else "issue_date DESC, synced_at DESC, created_at DESC"
    )
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
              FROM {SALE_ITEM_TABLE}
             WHERE {' AND '.join(where)}
             ORDER BY {order_sql}
             LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            return {
                "found": False,
                "source": "bejerman_sale_cache",
                "serial": serial,
                "normalizedSerial": normalized,
            }
        names = [col[0] for col in cur.description]
    return sale_payload_from_row(dict(zip(names, row)))


def lookup_and_cache_sale_by_serial(client: BejermanSDKClient, serial: str) -> dict[str, Any]:
    normalized = normalize_serial_for_lookup(serial)
    if not normalized:
        return {"found": False, "source": "bejerman_sale_live", "serial": serial, "normalizedSerial": normalized}

    months = int(_setting("BEJERMAN_SERIAL_LOOKUP_MONTHS_BACK", 60) or 60)
    max_comprobantes = int(
        _setting(
            "BEJERMAN_SERIAL_LOOKUP_FALLBACK_MAX_COMPROBANTES",
            _setting("BEJERMAN_SERIAL_LOOKUP_MAX_COMPROBANTES", 120),
        )
        or 120
    )
    types = [
        item.strip().upper()
        for item in as_string(_setting("BEJERMAN_SERIAL_LOOKUP_TYPES", "FC")).split(",")
        if item.strip()
    ] or ["FC"]
    today = timezone.localdate()
    date_from = (today - timedelta(days=max(1, months) * 31)).isoformat()
    company_key = _company_key_from_client(client)
    chunk_days = int(_setting("BEJERMAN_SERIAL_LOOKUP_CHUNK_DAYS", 31) or 31)
    fallback_timeout = int(_setting("BEJERMAN_SERIAL_LOOKUP_FALLBACK_TIMEOUT", 12) or 12)
    max_seconds = int(_setting("BEJERMAN_SERIAL_LOOKUP_FALLBACK_MAX_SECONDS", 20) or 20)
    checked = 0
    started_at = time.monotonic()
    original_timeout = getattr(client, "timeout", None)
    if fallback_timeout > 0 and hasattr(client, "timeout"):
        client.timeout = min(int(original_timeout or fallback_timeout), fallback_timeout)

    try:
        for tipo in types:
            for win_from, win_to in _date_windows(date_from, today.isoformat(), chunk_days):
                headers = headers_from_sales_list(
                    client.list_comprobantes_ventas(build_serial_lookup_sales_filters(tipo, win_from, win_to)),
                    max_comprobantes,
                )
                for header in headers:
                    if max_seconds > 0 and time.monotonic() - started_at >= max_seconds:
                        return {
                            "found": False,
                            "source": "bejerman_sale_live",
                            "serial": serial,
                            "normalizedSerial": normalized,
                            "checkedComprobantes": checked,
                            "lookup_error": "La consulta Bejerman superó el tiempo máximo interactivo; se requiere sincronizar la caché.",
                        }
                    if checked >= max_comprobantes:
                        return {
                            "found": False,
                            "source": "bejerman_sale_live",
                            "serial": serial,
                            "normalizedSerial": normalized,
                            "checkedComprobantes": checked,
                            "lookup_error": "La venta no apareció en los comprobantes revisados; se requiere sincronizar la caché completa.",
                        }
                    checked += 1
                    detail_id = comprobante_id_of(header)
                    if not detail_id:
                        continue
                    detail_response = client.detalle_comprobante_ventas(detail_id)
                    items = sale_items_from_detail_response(detail_response, company_key=company_key, header=header)
                    match = next((item for item in items if item.get("normalized_serial") == normalized), None)
                    if not match:
                        continue
                    if _table_exists(SALE_ITEM_TABLE):
                        upsert_sale_items(items)
                        payload = find_latest_sale_by_serial(serial, company_key=company_key)
                    else:
                        payload = sale_payload_from_row(match)
                    payload["source"] = "bejerman_sale_live"
                    payload["checkedComprobantes"] = checked
                    return payload
    finally:
        if original_timeout is not None and hasattr(client, "timeout"):
            client.timeout = original_timeout

    return {
        "found": False,
        "source": "bejerman_sale_live",
        "serial": serial,
        "normalizedSerial": normalized,
        "checkedComprobantes": checked,
    }


def _select_expr(columns: set[str], name: str) -> str:
    return name if name in columns else f"NULL AS {name}"


def _load_local_customers(columns: set[str]) -> list[dict[str, Any]]:
    select_columns = [
        "id",
        _select_expr(columns, "cod_empresa"),
        "razon_social",
        _select_expr(columns, "cuit"),
        _select_expr(columns, "contacto"),
        _select_expr(columns, "telefono"),
        _select_expr(columns, "telefono_2"),
        _select_expr(columns, "email"),
        _select_expr(columns, "alias_interno"),
        *(_select_expr(columns, column) for column in BEJERMAN_CUSTOMER_COLUMNS),
    ]
    with connection.cursor() as cur:
        cur.execute(f"SELECT {', '.join(select_columns)} FROM customers")
        names = [col[0] for col in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def _customer_record_from_sale(sale_payload: dict[str, Any]) -> dict[str, Any]:
    raw_document = sale_payload.get("rawDocument")
    record = dict(raw_document) if isinstance(raw_document, dict) else {}
    record.update(
        {
            "Cliente_Codigo": sale_payload.get("customerCode") or "",
            "Cliente_RazonSocial": sale_payload.get("customerName") or "",
            "Cliente_NroDocumento": sale_payload.get("customerCuit") or "",
            BEJERMAN_COMPANY_CONTEXT_KEY: sale_payload.get("companyKey") or "SEPID",
        }
    )
    return record


def _customer_response(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row.get("id"),
        "razon_social": row.get("razon_social") or "",
        "cod_empresa": row.get("cod_empresa") or row.get("bejerman_cod_empresa") or "",
        "telefono": row.get("telefono") or row.get("bejerman_telefono") or "",
        "cuit": row.get("cuit") or "",
    }


def resolve_customer_for_sale(sale_payload: dict[str, Any], *, create: bool = True) -> dict[str, Any]:
    record = _customer_record_from_sale(sale_payload)
    details = bejerman_customer_details_from_record(record)
    code_key = clean_upper(details.get("cod_empresa"))
    cuit_key = digits_only(details.get("cuit"))
    name_key = normalize_search(details.get("razon_social"))
    if not (code_key or cuit_key or name_key):
        return {"status": "missing", "reason": "missing_identity", "local_customer": None}

    columns = table_columns("customers")
    if "razon_social" not in columns:
        return {"status": "conflict", "reason": "missing_customers_schema", "local_customer": None}

    local_rows = _load_local_customers(columns)
    target = None
    match_reason = ""

    if cuit_key:
        matches = [row for row in local_rows if digits_only(row.get("cuit")) == cuit_key]
        if len(matches) > 1:
            return {"status": "conflict", "reason": "duplicate_local_cuit", "local_customer": None}
        if len(matches) == 1:
            target = matches[0]
            match_reason = "cuit"

    if target is None and code_key:
        matches = [
            row
            for row in local_rows
            if clean_upper(row.get("cod_empresa")) == code_key
            or clean_upper(row.get("bejerman_cod_empresa")) == code_key
        ]
        if len(matches) > 1:
            return {"status": "conflict", "reason": "duplicate_local_code", "local_customer": None}
        if len(matches) == 1:
            target = matches[0]
            match_reason = "code"

    if target is None and name_key:
        matches = [
            row
            for row in local_rows
            if normalize_search(row.get("razon_social")) == name_key
            or normalize_search(row.get("alias_interno")) == name_key
        ]
        if len(matches) > 1:
            return {"status": "conflict", "reason": "duplicate_local_name", "local_customer": None}
        if len(matches) == 1:
            target = matches[0]
            match_reason = "name"

    if target is not None:
        local_cuit = digits_only(target.get("cuit"))
        if cuit_key and local_cuit and local_cuit != cuit_key:
            return {
                "status": "conflict",
                "reason": "cuit_mismatch",
                "local_customer": _customer_response(target),
            }
        if not cuit_key and match_reason == "code":
            local_name = normalize_search(target.get("razon_social"))
            if name_key and local_name and local_name != name_key:
                return {
                    "status": "conflict",
                    "reason": "name_mismatch_without_cuit",
                    "local_customer": _customer_response(target),
                }
        values = _sync_values(details, record, target)
        if create:
            with transaction.atomic():
                _execute_write("customers", values, columns, customer_id=int(target["id"]))
            target.update(values)
        return {
            "status": "matched",
            "reason": match_reason,
            "local_customer": _customer_response(target),
        }

    if not create:
        return {"status": "missing", "reason": "not_found", "local_customer": None}

    values = _sync_values(details, record, None)
    with transaction.atomic():
        customer_id = _execute_write("customers", values, columns)
    if not customer_id:
        return {"status": "conflict", "reason": "missing_razon_social", "local_customer": None}
    row = {"id": customer_id, **values}
    return {
        "status": "created",
        "reason": "bejerman_sale",
        "local_customer": _customer_response(row),
    }


def bejerman_sale_response(
    code: str,
    ns_key: str | None,
    sale_payload: dict[str, Any],
    *,
    create_customer: bool,
) -> dict[str, Any] | None:
    if not sale_payload or not sale_payload.get("found"):
        return None
    article_code = sale_payload.get("articleCode") or ""
    article_description = sale_payload.get("articleDescription") or ""
    equipment = equipment_suggestion_from_bejerman_article(article_code, article_description)
    customer_resolution = resolve_customer_for_sale(sale_payload, create=create_customer)
    fallback_customer = find_customer_suggestion(
        sale_payload.get("customerCode") or "",
        sale_payload.get("customerName") or "",
    )
    local_customer = customer_resolution.get("local_customer") or fallback_customer
    warranty = compute_warranty_from_sale_date(
        sale_payload.get("issueDate"),
        numero_serie=sale_payload.get("serial") or code,
        brand_id=equipment.get("marca_id") if equipment.get("confidence") == "high" else None,
        model_id=equipment.get("modelo_id") if equipment.get("confidence") == "high" else None,
        source="bejerman_sale",
    )
    meta = warranty.get("meta") or {}
    meta.update(
        {
            "source": "bejerman_sale",
            "documentLabel": sale_payload.get("documentLabel") or "",
            "customerCode": sale_payload.get("customerCode") or "",
            "customerName": sale_payload.get("customerName") or "",
            "customerCuit": sale_payload.get("customerCuit") or "",
            "articleCode": article_code,
            "cacheSaleItemId": sale_payload.get("cacheSaleItemId") or "",
            "cacheSyncedAt": sale_payload.get("syncedAt"),
        }
    )
    return {
        "kind": "bejerman_sale",
        "source": "bejerman_sale",
        "raw": code,
        "normalized": code,
        "normalized_key": ns_key,
        "suggestion": {
            "source": sale_payload.get("source") or "bejerman_sale_cache",
            "cacheSaleItemId": sale_payload.get("cacheSaleItemId") or "",
            "companyKey": sale_payload.get("companyKey") or "",
            "serial": sale_payload.get("serial") or code,
            "normalizedSerial": sale_payload.get("normalizedSerial") or ns_key,
            "article": {
                "code": article_code,
                "description": article_description,
                "itemPartida": sale_payload.get("itemPartida") or "",
                "itemQuantity": sale_payload.get("itemQuantity"),
                "unitPrice": sale_payload.get("unitPrice"),
                "totalAmount": sale_payload.get("totalAmount"),
                "currency": sale_payload.get("currency") or "",
            },
            "document": {
                "type": sale_payload.get("documentType") or "",
                "letter": sale_payload.get("documentLetter") or "",
                "pointOfSale": sale_payload.get("documentPointOfSale") or "",
                "number": sale_payload.get("documentNumber") or "",
                "label": sale_payload.get("documentLabel") or "",
                "documentId": sale_payload.get("documentId"),
                "comprobanteId": sale_payload.get("comprobanteId") or "",
                "issueDate": sale_payload.get("issueDate"),
            },
            "customer": {
                "code": sale_payload.get("customerCode") or "",
                "name": sale_payload.get("customerName") or "",
                "cuit": sale_payload.get("customerCuit") or "",
                "local_customer": local_customer,
                "resolution": customer_resolution,
            },
            "equipment": equipment,
            "warranty": {
                "garantia": warranty.get("garantia"),
                "vence_el": warranty.get("vence_el").isoformat() if warranty.get("vence_el") else None,
                "fecha_venta": warranty.get("fecha_venta").isoformat() if warranty.get("fecha_venta") else None,
                "days": warranty.get("days"),
                "meta": meta,
            },
            "syncedAt": sale_payload.get("syncedAt"),
        },
    }


def sync_bejerman_sale_items(
    client: BejermanSDKClient,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    days: int | None = None,
    types: list[str] | None = None,
    max_comprobantes: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    today = timezone.localdate()
    if not date_to:
        date_to = today.isoformat()
    if not date_from:
        window_days = int(days or _setting("BEJERMAN_SALE_ITEMS_SYNC_DAYS", 365) or 365)
        date_from = (today - timedelta(days=max(1, window_days))).isoformat()
    if not types:
        types = [
            item.strip().upper()
            for item in as_string(_setting("BEJERMAN_SERIAL_LOOKUP_TYPES", "FC")).split(",")
            if item.strip()
        ] or ["FC"]
    max_headers = int(max_comprobantes or _setting("BEJERMAN_SALE_ITEMS_SYNC_MAX_COMPROBANTES", 500) or 500)
    company_key = _company_key_from_client(client)

    summary = {
        "ok": True,
        "companyKey": company_key,
        "dateFrom": date_from,
        "dateTo": date_to,
        "types": types,
        "headers": 0,
        "details": 0,
        "items": 0,
        "upserted": 0,
        "errors": [],
        "dryRun": bool(dry_run),
    }
    chunk_days = int(_setting("BEJERMAN_SALE_ITEMS_SYNC_CHUNK_DAYS", 31) or 31)
    summary["windows"] = 0
    for tipo in types:
        for win_from, win_to in _date_windows(date_from, date_to, chunk_days):
            summary["windows"] += 1
            try:
                filters = build_serial_lookup_sales_filters(tipo, win_from, win_to)
                headers = headers_from_sales_list(client.list_comprobantes_ventas(filters), max_headers)
            except Exception as exc:
                summary["errors"].append({"window": f"{win_from}/{win_to}", "type": tipo, "error": str(exc)})
                if len(summary["errors"]) >= 20:
                    break
                continue
            summary["headers"] += len(headers)
            for header in headers:
                detail_id = comprobante_id_of(header)
                if not detail_id:
                    continue
                try:
                    detail_response = client.detalle_comprobante_ventas(detail_id)
                    summary["details"] += 1
                    items = sale_items_from_detail_response(detail_response, company_key=company_key, header=header)
                    summary["items"] += len(items)
                    if not dry_run:
                        summary["upserted"] += upsert_sale_items(items)
                except Exception as exc:
                    summary["errors"].append({"comprobanteId": detail_id, "error": str(exc)})
                    if len(summary["errors"]) >= 20:
                        break
            if len(summary["errors"]) >= 20:
                break
        if len(summary["errors"]) >= 20:
            break
    return summary
