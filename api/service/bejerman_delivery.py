from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .bejerman_sdk import (
    BejermanPdfReference,
    BejermanPdfPendingError,
    BejermanSDKClient,
    BejermanSdkConfigError,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
    as_number,
    as_nullable_bool,
    as_string,
    build_article_filters,
    build_article_sales_vat_map,
    build_article_stock_lots,
    build_articles_result,
    build_partida_rows,
    build_delivery_remito_comprobante,
    build_facturacion_result,
    build_remitos_result,
    build_sales_filters,
    build_stock_rows,
    decode_document_id,
    delivery_remito_config,
    fetch_comprobante_pdf,
    first_value,
    normalize_search,
    parse_remito_response,
    records_from_response,
    resolve_customer_document_fields,
)
from .bejerman_companies import company_for_key
from .bejerman_documents import (
    REGISTERED_REMITO_NO_PDF_CODE,
    REMITO_COMPROBANTE_TYPES,
    cobranzas_remito_pdf_metadata,
    is_registered_remito_summary,
    is_transient_pdf_lookup_message,
    local_remito_pdf_metadata,
    parse_bejerman_remito_number,
    registered_remito_no_pdf_detail,
)
from .delivery_orders import (
    DeliveryOrderError,
    RENTAL_STOCK_DEPOSIT,
    _article_mapping_for_equipment,
    _json_param,
    _optional_text,
    create_event,
    delivery_source_reference,
    ensure_service_release_order_for_ingreso,
    get_delivery_order,
    get_user_seller_code,
    list_delivery_orders,
    notify_delivery_order_remito_ready,
    normalize_delivery_type,
    refresh_delivery_order_article_partida_flags,
    rental_item_from_context,
    remito_profile_for_type,
    remito_type_requires_billing,
    serialize_order,
    service_release_ingreso_ids_from_order,
    validate_rental_delivery_items_ready,
)
from .remito_pdf_notifications import notify_bejerman_remito_pdf_issued


logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400, retry_after_ms: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_ms = retry_after_ms


def registered_remito_no_pdf_error(remito_number: Any = "") -> BillingError:
    return BillingError(REGISTERED_REMITO_NO_PDF_CODE, registered_remito_no_pdf_detail(remito_number), status_code=409)


FACTURACION_COMPROBANTE_TYPES = ("FC", "NC", "ND")
REMITO_OPERATION_TYPES = ("MC", "REP", "ALQ", "DEMO", "BUSO", "FAB")
DELIVERY_ORDER_STOCK_DEPOSIT = "VAL"
DELIVERY_ORDER_FALLBACK_DEPOSITS = ("VAL", "STL")
DELIVERY_MANUAL_REGISTER_POINT_OF_SALE = "00001"
DELIVERY_EXISTING_BEJERMAN_POINTS_OF_SALE = {"00004", "00007"}


def _is_bejerman_sales_query_schema_error(message: str) -> bool:
    text = str(message or "")
    markers = ("IMPORTE_TOTAL", "ConvMonLocal", "CabVenta.", "ORDER BY items must appear")
    return any(marker in text for marker in markers)


def _sdk_error(exc: Exception) -> BillingError | DeliveryOrderError:
    message = str(exc)
    if isinstance(exc, BejermanPdfPendingError):
        return BillingError(
            "BEJERMAN_PDF_PENDING",
            str(exc),
            status_code=202,
            retry_after_ms=getattr(exc, "retry_after_ms", 2500),
        )
    if isinstance(exc, BejermanSdkConfigError):
        return BillingError("BEJERMAN_SDK_NOT_CONFIGURED", message, status_code=503)
    if isinstance(exc, BejermanSdkUnavailable):
        return BillingError("BEJERMAN_SDK_UNAVAILABLE", message, status_code=503)
    if isinstance(exc, BejermanSdkResponseError):
        if "expSinCuotasV1" in message:
            return BillingError(
                "BEJERMAN_BILLING_QUERY_BUSY",
                "Bejerman no pudo completar la consulta de facturación porque dejó bloqueado el objeto interno expSinCuotasV1. Reintente la consulta; si persiste, hay que revisar el servicio Bejerman.",
                status_code=502,
            )
        if _is_bejerman_sales_query_schema_error(message):
            return BillingError(
                "BEJERMAN_SDK_INTERNAL_QUERY_ERROR",
                "Bejerman devolvió un error interno al consultar comprobantes. No es un bloqueo por partidas ni por datos de NEXORA; verifique si el comprobante quedó emitido antes de reintentar.",
                status_code=502,
            )
        return BillingError("BEJERMAN_SDK_ERROR", message, status_code=502)
    return BillingError("BEJERMAN_ERROR", message, status_code=502)


def list_facturacion_company_options() -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id,
                   razon_social,
                   TRIM(COALESCE(cod_empresa, '')) AS cod_empresa
            FROM customers
            WHERE TRIM(COALESCE(cod_empresa, '')) <> ''
            ORDER BY razon_social ASC
            """
        )
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return [
        {
            "id": row["id"],
            "name": row.get("razon_social") or "",
            "bejermanCustomerCode": row.get("cod_empresa") or "",
        }
        for row in rows
    ]


def _article_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _optional_text(item.get(key))
        if value:
            return value
    return ""


def _bridge_article(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    code = _article_text(item, "code", "codigo", "Articulo_Codigo")
    name = _article_text(item, "name", "nombre", "Articulo_Descripcion")
    description = _article_text(item, "description") or name
    if not code and not name:
        return None
    return {
        "id": _article_text(item, "id") or code or name,
        "code": code,
        "name": name or code,
        "description": description,
        "raw": item,
    }


def list_bejerman_articles(
    search: str | None = None,
    limit: Any = 20,
    actor_user_id: int | None = None,
    company_key: str | None = None,
) -> dict[str, Any]:
    try:
        clean_limit = max(1, min(50, int(limit or 20)))
    except (TypeError, ValueError):
        clean_limit = 20
    q = _optional_text(search)
    normalized_company = _normalize_delivery_company_key(company_key)
    try:
        client = BejermanSDKClient(
            company_key=normalized_company,
            actor_user_id=actor_user_id,
            allow_system_credentials=True,
        )
        filters = build_article_filters(q) if q else []
        data = build_articles_result(client.list_articulos(filters, clean_limit), {"search": q, "limit": clean_limit})
    except Exception as exc:
        raise _sdk_error(exc) from exc
    return {
        "items": data.get("items") if isinstance(data, dict) else [],
        "companyKey": normalized_company,
        "unavailable": False,
    }


def _normalize_delivery_company_key(value: Any) -> str:
    return _company_key_for_marker(value) or "SEPID"


def _normalize_price_currency(value: Any, default: str = "ARS") -> str:
    value = _optional_text(value) or default
    value = (value or "ARS").upper().replace(" ", "")
    if value in {"$", "PESO", "PESOS"}:
        return "ARS"
    if value in {"U$S", "U$D", "US$", "DOLAR", "DOLARES"}:
        return "USD"
    if value not in {"ARS", "USD"}:
        raise DeliveryOrderError("INVALID_PRICE_CURRENCY", "La moneda debe ser ARS o USD")
    return value


def _order_price_currency(order: dict[str, Any]) -> str:
    return _normalize_price_currency(order.get("priceCurrency") or order.get("price_currency"))


def _order_item_price_currencies(order: dict[str, Any]) -> set[str]:
    default_currency = _order_price_currency(order)
    currencies: set[str] = set()
    for item in order.get("items") or []:
        if isinstance(item, dict):
            currencies.add(_normalize_price_currency(item.get("priceCurrency") or item.get("price_currency"), default_currency))
    return currencies or {default_currency}


def _order_exchange_rate(order: dict[str, Any]) -> str:
    return _optional_text(order.get("commercialExchangeRate") or order.get("commercial_exchange_rate")) or ""


def _parsed_order_exchange_rate(value: Any) -> float | None:
    parsed = as_number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _normalize_deposit_code(value: Any, default: str = DELIVERY_ORDER_STOCK_DEPOSIT) -> str:
    return (_optional_text(value) or default).upper()


def _fallback_deposit_items(selected_code: str | None = None) -> list[dict[str, Any]]:
    codes = list(DELIVERY_ORDER_FALLBACK_DEPOSITS)
    selected = _normalize_deposit_code(selected_code) if selected_code else ""
    if selected and selected not in codes:
        codes.insert(0, selected)
    return [{"code": code, "name": code, "label": code} for code in codes]


def _deposit_item(record: dict[str, Any]) -> dict[str, Any] | None:
    code = _normalize_deposit_code(
        first_value(
            record,
            (
                "Deposito_Codigo",
                "DepositoCodigo",
                "Deposito",
                "Codigo",
                "dep_Cod",
                "Dep_Cod",
                "code",
            ),
        ),
        "",
    )
    name = _optional_text(
        first_value(
            record,
            (
                "Deposito_Descripcion",
                "DepositoDescripcion",
                "Deposito_Nombre",
                "DepositoNombre",
                "Descripcion",
                "Nombre",
                "dep_Desc",
                "Dep_Desc",
                "name",
            ),
        )
    )
    if not code:
        return None
    return {"code": code, "name": name or code, "label": code if not name else f"{code} - {name}"}


def list_bejerman_depositos(company_key: str | None = None, actor_user_id: int | None = None) -> dict[str, Any]:
    normalized_company = _normalize_delivery_company_key(company_key)
    try:
        client = BejermanSDKClient(
            company_key=normalized_company,
            actor_user_id=actor_user_id,
            allow_system_credentials=True,
        )
        deposits: dict[str, dict[str, Any]] = {}
        for record in records_from_response(client.obtener_depositos()):
            item = _deposit_item(record)
            if item:
                deposits.setdefault(item["code"], item)
        items = list(deposits.values())
        if not items:
            raise BillingError("BEJERMAN_DEPOSITS_EMPTY", "Bejerman no devolvió depósitos.", status_code=502)
        return {
            "items": items,
            "companyKey": normalized_company,
            "unavailable": False,
            "warning": "",
        }
    except Exception:
        return {
            "items": _fallback_deposit_items(),
            "companyKey": normalized_company,
            "unavailable": True,
            "warning": "No fue posible consultar depósitos Bejerman. Se muestran depósitos estándar.",
        }


def list_bejerman_article_stock(
    article_code: str | None = None,
    limit: Any = 100,
    actor_user_id: int | None = None,
    company_key: str | None = None,
    deposit_code: str | None = None,
    delivery_type: str | None = None,
) -> dict[str, Any]:
    code = _optional_text(article_code)
    normalized_company = _normalize_delivery_company_key(company_key)
    profile_deposit = _profile_for_order(delivery_type or "sale", normalized_company)["deposit"]
    normalized_deposit = _normalize_deposit_code(deposit_code, profile_deposit)
    try:
        clean_limit = max(1, min(200, int(limit or 100)))
    except (TypeError, ValueError):
        clean_limit = 100
    try:
        client = BejermanSDKClient(
            company_key=normalized_company,
            actor_user_id=actor_user_id,
            allow_system_credentials=True,
        )
        stock_response = client.obtener_stock_deposito(normalized_deposit)
        partidas_response = client.obtener_partidas()
        lots = build_article_stock_lots(
            build_stock_rows(stock_response),
            build_partida_rows(partidas_response),
            code,
            normalized_deposit,
        )
    except Exception as exc:
        raise _sdk_error(exc) from exc
    checked_at = timezone.now().isoformat()
    items = [
        {
            **lot,
            "stockDepositCode": lot.get("depositCode") or normalized_deposit,
            "partidaExpirationDate": lot.get("expirationDate"),
            "stockAvailableQuantity": lot.get("availableQuantity"),
            "stockCheckedAt": checked_at,
        }
        for lot in lots[:clean_limit]
    ]
    return {
        "items": items,
        "depositCode": normalized_deposit,
        "companyKey": normalized_company,
        "unavailable": False,
        "warning": None if items else f"Sin stock positivo en depósito {normalized_deposit} para este artículo.",
    }


def _available_quantity(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _effective_available_quantity(record: dict[str, Any]) -> float:
    available = record.get("availableQuantity")
    if available is not None:
        return _available_quantity(available)
    return _available_quantity(record.get("realQuantity"))


def _matching_lot_for_partida(lots: list[dict[str, Any]], partida: str) -> dict[str, Any] | None:
    wanted = (_optional_text(partida) or "").casefold()
    if not wanted:
        return None
    for lot in lots:
        if (_optional_text(lot.get("partida")) or "").casefold() != wanted:
            continue
        if _effective_available_quantity(lot) > 0:
            return lot
    return None


def _partida_expiration(partida_rows: list[dict[str, Any]], deposit_code: str, partida: str) -> str:
    wanted_deposit = (_optional_text(deposit_code) or "").upper()
    wanted_partida = (_optional_text(partida) or "").casefold()
    fallback = ""
    if not wanted_partida:
        return ""
    for row in partida_rows:
        if (_optional_text(row.get("partida")) or "").casefold() != wanted_partida:
            continue
        expiration = _optional_text(row.get("expirationDate")) or ""
        if (_optional_text(row.get("depositCode")) or "").upper() == wanted_deposit:
            return expiration
        if not fallback:
            fallback = expiration
    return fallback


def _stock_lot_for_partida(
    stock_rows: list[dict[str, Any]],
    partida_rows: list[dict[str, Any]],
    partida: str,
    deposit_code: str,
) -> dict[str, Any] | None:
    wanted_partida = (_optional_text(partida) or "").casefold()
    wanted_deposit = (_optional_text(deposit_code) or "").upper()
    if not wanted_partida or not wanted_deposit:
        return None
    for row in stock_rows:
        if (_optional_text(row.get("depositCode")) or "").upper() != wanted_deposit:
            continue
        if (_optional_text(row.get("partida")) or "").casefold() != wanted_partida:
            continue
        if _effective_available_quantity(row) <= 0:
            continue
        return {
            "articleCode": row.get("articleCode") or "",
            "depositCode": row.get("depositCode") or deposit_code,
            "partida": row.get("partida") or partida,
            "expirationDate": _partida_expiration(partida_rows, row.get("depositCode") or deposit_code, row.get("partida") or partida),
            "realQuantity": row.get("realQuantity"),
            "committedQuantity": row.get("committedQuantity"),
            "availableQuantity": row.get("availableQuantity"),
        }
    return None


def list_rental_available_equipment(
    search: str | None = None,
    limit: Any = 80,
    actor_user_id: int | None = None,
    company_key: str | None = None,
) -> dict[str, Any]:
    normalized_company = _normalize_delivery_company_key(company_key)
    try:
        clean_limit = max(1, min(200, int(limit or 80)))
    except (TypeError, ValueError):
        clean_limit = 80
    q = normalize_search(search)
    fetch_limit = 2000 if q else min(300, max(clean_limit * 4, 80))
    ingreso_columns = _table_columns("ingresos", {"empresa_bejerman", "alquilado", "ubicacion_id"})
    company_expr = "COALESCE(t.empresa_bejerman, 'SEPID')" if "empresa_bejerman" in ingreso_columns else "'SEPID'"
    alquilado_expr = "COALESCE(t.alquilado, FALSE)" if "alquilado" in ingreso_columns else "FALSE"
    location_select = "t.ubicacion_id, COALESCE(loc.nombre, '') AS ubicacion_nombre" if "ubicacion_id" in ingreso_columns else "NULL AS ubicacion_id, '' AS ubicacion_nombre"
    location_join = "LEFT JOIN locations loc ON loc.id = t.ubicacion_id" if "ubicacion_id" in ingreso_columns else ""
    location_filter = (
        "AND LOWER(COALESCE(loc.nombre, '')) LIKE %s AND LOWER(COALESCE(loc.nombre, '')) LIKE %s"
        if "ubicacion_id" in ingreso_columns
        else "AND FALSE"
    )
    location_params = ["%estanter%", "%alquiler%"] if "ubicacion_id" in ingreso_columns else []
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id AS ingreso_id,
                   t.device_id,
                   COALESCE(t.estado::text, '') AS estado,
                   {alquilado_expr} AS alquilado,
                   {company_expr} AS company_key,
                   {location_select},
                   COALESCE(t.equipo_variante, '') AS equipo_variante,
                   c.id AS owner_customer_id,
                   COALESCE(c.razon_social, '') AS owner_customer_name,
                   COALESCE(d.numero_serie, '') AS equipment_serial,
                   COALESCE(d.numero_interno, '') AS equipment_internal_number,
                   d.model_id,
                   COALESCE(d.variante, '') AS device_variante,
                   COALESCE(m.nombre, '') AS modelo,
                   COALESCE(m.tipo_equipo, '') AS tipo_equipo,
                   COALESCE(m.variante, '') AS modelo_variante,
                   COALESCE(b.nombre, '') AS marca
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              {location_join}
             WHERE {alquilado_expr} = FALSE
               AND LOWER(COALESCE(t.estado::text, '')) NOT IN ('entregado', 'alquilado', 'baja', 'vendido_pendiente_entrega', 'vendido_entregado')
               {location_filter}
             ORDER BY t.id DESC
             LIMIT %s
            """,
            [*location_params, fetch_limit],
        )
        cols = [col[0] for col in cur.description]
        local_rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    checked_at = timezone.now().isoformat()
    try:
        client = BejermanSDKClient(company_key=normalized_company, actor_user_id=actor_user_id)
        stock_rows = build_stock_rows(client.obtener_stock_deposito(RENTAL_STOCK_DEPOSIT))
        partida_rows = build_partida_rows(client.obtener_partidas())
    except Exception as exc:
        raise _sdk_error(exc) from exc

    lots_by_article: dict[str, list[dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []
    for row in local_rows:
        serial = _optional_text(row.get("equipment_serial"))
        if not serial:
            continue
        mapping = _article_mapping_for_equipment(row) or {}
        article_code = _optional_text((mapping or {}).get("article_code"))
        lot = None
        if not article_code:
            lot = _stock_lot_for_partida(stock_rows, partida_rows, serial, RENTAL_STOCK_DEPOSIT)
            article_code = _optional_text((lot or {}).get("articleCode"))
        if article_code and not lot and article_code not in lots_by_article:
            lots_by_article[article_code] = build_article_stock_lots(
                stock_rows,
                partida_rows,
                article_code,
                RENTAL_STOCK_DEPOSIT,
            )
        if article_code and not lot:
            lot = _matching_lot_for_partida(lots_by_article[article_code], serial)
        if not lot:
            lot = _stock_lot_for_partida(stock_rows, partida_rows, serial, RENTAL_STOCK_DEPOSIT)
            article_code = _optional_text((lot or {}).get("articleCode")) or article_code
        if not lot or not article_code:
            continue
        item_mapping = {
            **mapping,
            "article_code": article_code,
            "article_description": mapping.get("article_description") or "",
        }
        item = rental_item_from_context(
            row,
            item_mapping,
            stock={
                "stockAvailableQuantity": lot.get("availableQuantity"),
                "partidaExpirationDate": lot.get("expirationDate"),
                "stockCheckedAt": checked_at,
            },
        )
        if q:
            haystack = normalize_search(
                " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "sourceReference",
                        "equipmentModel",
                        "equipmentDetail",
                        "equipmentSerial",
                        "equipmentInternalNumber",
                        "articleCode",
                        "articleName",
                        "ownerCustomerName",
                    )
                )
            )
            if q not in haystack:
                continue
        items.append(item)
        if len(items) >= clean_limit:
            break

    return {
        "items": items,
        "depositCode": RENTAL_STOCK_DEPOSIT,
        "companyKey": normalized_company,
        "unavailable": False,
        "warning": None if items else "Sin equipos disponibles con stock positivo en STL.",
    }


def _digits_only(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _bejerman_customer_code(record: dict[str, Any]) -> str:
    value = first_value(record, ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente"))
    text = "" if value is None else str(value)
    return text if text.strip() else ""


def _bejerman_customer_name(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Nombre", "Cliente")))


def _bejerman_customer_cuit(record: dict[str, Any]) -> str:
    return _digits_only(first_value(record, ("Cliente_NroDocumento", "Cliente_CUIT", "Cliente_Cuit", "CUIT", "Cuit", "TaxId")))


def _local_customer_for_code(customer_code: str) -> dict[str, str] | None:
    code = _optional_text(customer_code)
    if not code:
        return None
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT razon_social,
                   COALESCE(cuit, '') AS cuit,
                   COALESCE(cod_empresa, '') AS cod_empresa
            FROM customers
            WHERE UPPER(TRIM(COALESCE(cod_empresa, ''))) = UPPER(TRIM(%s))
            ORDER BY id ASC
            LIMIT 1
            """,
            [code],
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"name": row[0] or "", "cuit": row[1] or "", "code": row[2] or ""}


def _resolve_facturacion_customer_code(client: BejermanSDKClient, requested_code: str) -> str:
    requested = "" if requested_code is None else str(requested_code)
    requested_norm = requested.strip().upper()
    if not requested_norm:
        return ""
    try:
        records = records_from_response(client.list_clientes())
    except Exception:
        return requested

    for record in records:
        code = _bejerman_customer_code(record)
        if code and code.strip().upper() == requested_norm:
            return code

    local = _local_customer_for_code(requested)
    if not local:
        return requested

    local_cuit = _digits_only(local.get("cuit"))
    if local_cuit:
        for record in records:
            code = _bejerman_customer_code(record)
            if code and _bejerman_customer_cuit(record) == local_cuit:
                return code

    local_name = normalize_search(local.get("name"))
    if local_name:
        for record in records:
            code = _bejerman_customer_code(record)
            if code and normalize_search(_bejerman_customer_name(record)) == local_name:
                return code

    return requested


def _default_facturacion_dates(params: dict[str, Any]) -> None:
    if params.get("dateFrom") or params.get("dateTo"):
        return
    today = timezone.localdate()
    params["dateFrom"] = f"{today.year}-01-01"
    params["dateTo"] = today.isoformat()


def _facturacion_comprobante_types(params: dict[str, Any]) -> tuple[str, ...]:
    origin_type = (_optional_text(params.get("origin")) or "").upper()
    if origin_type in FACTURACION_COMPROBANTE_TYPES:
        return (origin_type,)
    return FACTURACION_COMPROBANTE_TYPES


def _facturacion_query_comprobante_types(params: dict[str, Any]) -> tuple[str | None, ...]:
    requested_types = _facturacion_comprobante_types(params)
    if requested_types == FACTURACION_COMPROBANTE_TYPES:
        return (None,)
    return requested_types


def _normalize_remito_company_key(value: Any) -> str:
    company = company_for_key(value, default="SEPID")
    if not company:
        raise BillingError("INVALID_COMPANY_KEY", "Empresa Bejerman no válida", status_code=400)
    return company.key


def _normalize_remito_filter(value: Any, allowed: tuple[str, ...], code: str, label: str) -> str:
    text = (_optional_text(value) or "").upper()
    if not text:
        return ""
    if text not in allowed:
        raise BillingError(code, f"{label} no válido", status_code=400)
    return text


def _remitos_params(raw_filters: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for source_keys, target_key in (
        (("dateFrom", "desde"), "dateFrom"),
        (("dateTo", "hasta"), "dateTo"),
        (("remitoType", "tipoRemito", "comprobanteTipo", "tipo"), "remitoType"),
        (("operationType", "tipoOperacion", "operacion", "operation"), "operationType"),
        (("page",), "page"),
        (("pageSize",), "pageSize"),
        (("search", "q"), "search"),
    ):
        for source_key in source_keys:
            value = raw_filters.get(source_key)
            if value not in (None, ""):
                params[target_key] = value
                break
    params["remitoType"] = _normalize_remito_filter(
        params.get("remitoType"),
        REMITO_COMPROBANTE_TYPES,
        "INVALID_REMITO_TYPE",
        "Tipo de remito",
    )
    params["operationType"] = _normalize_remito_filter(
        params.get("operationType"),
        REMITO_OPERATION_TYPES,
        "INVALID_OPERATION_TYPE",
        "Tipo de operación",
    )
    return params


def _remito_comprobante_types(params: dict[str, Any]) -> tuple[str, ...]:
    remito_type = _optional_text(params.get("remitoType"))
    if remito_type:
        return (remito_type.upper(),)
    return REMITO_COMPROBANTE_TYPES


def _remito_partial_error(comprobante_type: str, exc: Exception) -> dict[str, str]:
    normalized = _sdk_error(exc)
    return {
        "remitoType": comprobante_type,
        "code": getattr(normalized, "code", "BEJERMAN_ERROR"),
        "message": str(normalized),
    }


def _pdf_sdk_error(exc: Exception) -> BillingError | DeliveryOrderError:
    message = str(exc)
    if "expCuotasV" in message:
        return BillingError(
            "BEJERMAN_PDF_GENERATION_ERROR",
            "Bejerman no pudo generar el PDF del comprobante porque su SDK informa que falta el objeto interno expCuotasV.",
            status_code=502,
        )
    if _is_bejerman_sales_query_schema_error(message):
        return BillingError(
            "BEJERMAN_PDF_GENERATION_ERROR",
            "El remito está emitido, pero Bejerman no pudo consultar o generar el PDF por un error interno de su SDK. Reintente la impresión desde el historial; si persiste, hay que revisar Bejerman.",
            status_code=502,
        )
    if is_transient_pdf_lookup_message(message):
        return BillingError(
            "BEJERMAN_PDF_NOT_AVAILABLE",
            "Bejerman no devolvió el PDF del comprobante. El comprobante existe en la consulta, pero el PDF no está disponible desde el SDK.",
            status_code=502,
        )
    return _sdk_error(exc)


def _date_only(value: Any) -> str:
    if hasattr(value, "date"):
        return value.date().isoformat()
    text = _optional_text(value)
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else text


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _local_remito_issue_date(row: dict[str, Any], raw: dict[str, Any]) -> str:
    raw_date = first_value(raw, ("Comprobante_FechaEmision", "Comprobante_FEmision", "FechaEmision"))
    return _date_only(raw_date) or _date_only(row.get("generated_at")) or _date_only(row.get("created_at"))


def _local_remito_document_number(tipo: str, letter: str, point: str, number: str, fallback: Any) -> str:
    remito_number = _optional_text(fallback)
    if remito_number:
        return remito_number
    if tipo and letter and point and number:
        return f"{tipo} {letter} {point}-{number}"
    return "RSS"


def resolve_remito_group_for_document_id(document_id: str, company_key: str | None = None) -> dict[str, Any] | None:
    doc = _optional_text(document_id)
    if not doc:
        return None
    try:
        decoded = decode_document_id(doc)
    except Exception:
        return None
    tipo = (_optional_text(decoded.get("t") or decoded.get("type")) or "").upper()
    letter = _optional_text(decoded.get("l") or decoded.get("letter"))
    point = _optional_text(decoded.get("p") or decoded.get("pointOfSale"))
    number = _optional_text(decoded.get("n") or decoded.get("number"))
    customer_code = _optional_text(decoded.get("c") or decoded.get("customerCode"))
    if not all((tipo, letter, point, number)):
        return None
    normalized_company = _company_key_for_marker(company_key) if company_key else None

    where = [
        "status = 'generated'",
        "UPPER(COALESCE(comprobante_tipo, '')) = %s",
        "COALESCE(comprobante_letra, '') = %s",
        "COALESCE(comprobante_pto_venta, '') = %s",
        "COALESCE(comprobante_numero, '') = %s",
    ]
    params: list[Any] = [tipo, letter, point, number]
    if customer_code:
        where.append("UPPER(TRIM(COALESCE(customer_code, ''))) = UPPER(TRIM(%s))")
        params.append(customer_code)
    if normalized_company:
        where.append("(company_key IS NULL OR company_key = %s)")
        params.append(normalized_company)

    try:
        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                       comprobante_numero, remito_number, customer_code, customer_name, operation_code,
                       created_at, generated_at, response_summary
                  FROM bejerman_remito_groups
                 WHERE {' AND '.join(where)}
                 ORDER BY
                       CASE WHEN company_key = %s THEN 0 ELSE 1 END,
                       generated_at DESC NULLS LAST,
                       created_at DESC NULLS LAST
                 LIMIT 5
                """,
                [*params, normalized_company or ""],
            )
            cols = [col[0] for col in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        logger.warning("bejerman_remito_group_lookup_failed", exc_info=True)
        return None

    for row in rows:
        summary = row.get("response_summary") if isinstance(row.get("response_summary"), dict) else {}
        resolved_company = _company_key_for_remito_group(row.get("id"), summary, row.get("company_key"))
        if normalized_company and resolved_company != normalized_company:
            continue
        return {**row, "resolvedCompanyKey": resolved_company}
    return None


def resolve_registered_ingreso_remito_for_document_id(document_id: str, company_key: str | None = None) -> dict[str, Any] | None:
    doc = _optional_text(document_id)
    if not doc or not _table_exists("bejerman_ingreso_remitos"):
        return None
    try:
        decoded = decode_document_id(doc)
    except Exception:
        return None
    tipo = (_optional_text(decoded.get("t") or decoded.get("type")) or "").upper()
    letter = _optional_text(decoded.get("l") or decoded.get("letter"))
    point = _optional_text(decoded.get("p") or decoded.get("pointOfSale"))
    number = _optional_text(decoded.get("n") or decoded.get("number"))
    customer_code = _optional_text(decoded.get("c") or decoded.get("customerCode"))
    if not all((tipo, letter, point, number)):
        return None
    normalized_company = _company_key_for_marker(company_key) if company_key else None

    where = [
        "status = 'generated'",
        "document_mode = 'register'",
        "UPPER(COALESCE(comprobante_tipo, '')) = %s",
        "COALESCE(comprobante_letra, '') = %s",
        "COALESCE(comprobante_pto_venta, '') = %s",
        "COALESCE(comprobante_numero, '') = %s",
    ]
    params: list[Any] = [tipo, letter, point, number]
    if customer_code:
        where.append("UPPER(TRIM(COALESCE(customer_code, ''))) = UPPER(TRIM(%s))")
        params.append(customer_code)
    if normalized_company:
        where.append("(company_key IS NULL OR company_key = %s)")
        params.append(normalized_company)

    try:
        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, ingreso_id, company_key, comprobante_tipo, comprobante_letra,
                       comprobante_pto_venta, comprobante_numero, remito_number,
                       manual_remito_number, customer_code, customer_name, issue_date,
                       generated_at
                  FROM bejerman_ingreso_remitos
                 WHERE {' AND '.join(where)}
                 ORDER BY
                       CASE WHEN company_key = %s THEN 0 ELSE 1 END,
                       generated_at DESC NULLS LAST,
                       id DESC
                 LIMIT 1
                """,
                [*params, normalized_company or ""],
            )
            cols = [col[0] for col in cur.description]
            row_raw = cur.fetchone()
            return dict(zip(cols, row_raw)) if row_raw else None
    except Exception:
        logger.warning("bejerman_ingreso_registered_remito_lookup_failed", exc_info=True)
        return None


def _enrich_remito_items_with_local_groups(items: list[dict[str, Any]], company_key: str) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for item in items:
        document_id = _optional_text(item.get("documentId") or item.get("id"))
        if not document_id:
            enriched.append(item)
            continue
        had_pdf_metadata = bool(item.get("pdfUrl") and item.get("printUrl"))
        metadata = cobranzas_remito_pdf_metadata(
            document_id,
            customer_code=item.get("bejermanCustomerCode") or item.get("customerCode"),
            company_key=item.get("companyKey") or company_key,
        )
        item = {**metadata, **item}
        if had_pdf_metadata:
            enriched.append(item)
            continue
        group = resolve_remito_group_for_document_id(document_id, company_key=company_key)
        if not group:
            enriched.append(item)
            continue
        enriched.append({**item, **local_remito_pdf_metadata(group["id"], group.get("resolvedCompanyKey") or company_key)})
    return enriched


def _local_generated_rss_remito_items(
    params: dict[str, Any],
    *,
    company_key: str,
    customer_code: str,
) -> list[dict[str, Any]]:
    requested_type = (_optional_text(params.get("remitoType")) or "").upper()
    if requested_type and requested_type != "RSS":
        return []

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                   comprobante_numero, remito_number, customer_code, customer_name, operation_code,
                   created_at, generated_at, response_summary
              FROM bejerman_remito_groups
             WHERE status = 'generated'
               AND UPPER(COALESCE(comprobante_tipo, '')) = 'RSS'
               AND (company_key IS NULL OR company_key = %s)
            """,
            [company_key],
        )
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    date_from = _date_only(params.get("dateFrom"))
    date_to = _date_only(params.get("dateTo"))
    items: list[dict[str, Any]] = []
    for row in rows:
        summary = row.get("response_summary") if isinstance(row.get("response_summary"), dict) else {}
        resolved_company = _company_key_for_remito_group(row.get("id"), summary, row.get("company_key"))
        if resolved_company != company_key:
            continue
        row_customer_code = _optional_text(row.get("customer_code"))
        if customer_code and row_customer_code != customer_code:
            continue

        raw = summary.get("raw") if isinstance(summary.get("raw"), dict) else {}
        issue_date = _local_remito_issue_date(row, raw)
        if date_from and issue_date and issue_date < date_from:
            continue
        if date_to and issue_date and issue_date > date_to:
            continue

        tipo = (_optional_text(row.get("comprobante_tipo") or summary.get("comprobanteTipo")) or "").upper() or "RSS"
        letter = _optional_text(row.get("comprobante_letra") or summary.get("comprobanteLetra") or "R") or "R"
        point = _optional_text(row.get("comprobante_pto_venta") or summary.get("comprobantePtoVenta"))
        number = _optional_text(row.get("comprobante_numero") or summary.get("comprobanteNumero"))
        if not point or not number:
            continue
        operation_code = (_optional_text(row.get("operation_code")) or "").upper()
        document_number = _local_remito_document_number(tipo, letter, point, number, row.get("remito_number"))
        document_id = _document_id({"t": tipo, "n": number, "l": letter, "p": point, "f": issue_date, "c": row_customer_code})
        total = as_number(first_value(raw, ("Comprobante_ImporteTotal", "Comprobante_Total", "ImporteTotal", "Total")))
        items.append(
            {
                "id": document_id,
                "documentId": document_id,
                "type": tipo,
                "comprobanteTipo": tipo,
                "letter": letter,
                "pointOfSale": point,
                "number": number,
                "comprobanteNumero": number,
                "documentNumber": document_number,
                "numero": document_number,
                "issueDate": issue_date,
                "date": issue_date,
                "customerCode": row_customer_code,
                "bejermanCustomerCode": row_customer_code,
                "customerName": _optional_text(row.get("customer_name")),
                "operationCode": operation_code,
                "tipoOperacion": operation_code,
                "operationLabel": operation_code,
                "origin": operation_code,
                "subtotal": as_number(first_value(raw, ("Comprobante_ImporteNeto", "Comprobante_Subtotal", "Subtotal"))),
                "taxes": as_number(first_value(raw, ("Comprobante_ImporteIVA", "Comprobante_Impuestos", "Impuestos", "IVA"))),
                "totalAmount": total,
                "amount": total,
                **local_remito_pdf_metadata(row.get("id"), resolved_company),
                "raw": raw,
            }
        )
    return items


def list_remitos_from_bejerman(
    customer_code: str | None,
    filters: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    company_key: str | None = None,
) -> dict[str, Any]:
    code = _optional_text(customer_code)
    raw_filters = filters or {}
    params = _remitos_params(raw_filters)
    normalized_company = _normalize_remito_company_key(
        company_key or raw_filters.get("companyKey") or raw_filters.get("company_key") or raw_filters.get("empresa")
    )
    try:
        client = BejermanSDKClient(company_key=normalized_company, actor_user_id=actor_user_id)
        effective_code = _resolve_facturacion_customer_code(client, code) if code else ""
        _default_facturacion_dates(params)
        requested_types = _remito_comprobante_types(params)
        responses = []
        partial_errors = []
        strict_type = bool(_optional_text(params.get("remitoType")))
        for comprobante_type in requested_types:
            try:
                responses.append(
                    client.list_comprobantes_ventas(
                        build_sales_filters(
                            effective_code,
                            params.get("dateFrom"),
                            params.get("dateTo"),
                            comprobante_type,
                            params.get("operationType"),
                        )
                    )
                )
            except Exception as exc:
                if strict_type and comprobante_type != "RSS":
                    raise
                partial_errors.append(_remito_partial_error(comprobante_type, exc))
                logger.warning("bejerman_remito_type_query_failed", extra={"remito_type": comprobante_type})
        failed_type_set = {error["remitoType"] for error in partial_errors}
        local_rss_items = (
            _local_generated_rss_remito_items(params, company_key=normalized_company, customer_code=effective_code)
            if "RSS" in failed_type_set
            else []
        )
        if partial_errors and not responses and not local_rss_items and failed_type_set != {"RSS"}:
            first_error = partial_errors[0]
            raise BillingError(first_error["code"], first_error["message"], status_code=502)
        data = build_remitos_result(responses, effective_code, params, extra_items=local_rss_items)
        data["items"] = _enrich_remito_items_with_local_groups(data.get("items") or [], normalized_company)
        if partial_errors:
            data["partialErrors"] = partial_errors
            if "RSS" in failed_type_set:
                data["warning"] = (
                    "No se pudo consultar RSS desde Bejerman. "
                    "Se muestran los RSS generados por NEXORA y los demás remitos disponibles."
                )
            else:
                failed_types = ", ".join(error["remitoType"] for error in partial_errors)
                data["warning"] = f"No se pudieron consultar estos tipos de remito: {failed_types}. Se muestran los demás resultados."
        data["requestedCustomerCode"] = code
        data["effectiveCustomerCode"] = as_string(effective_code)
        data["bejermanCustomerCode"] = effective_code
        data["companyKey"] = normalized_company
        data["scope"] = "customer" if code else "company"
    except BillingError:
        raise
    except Exception as exc:
        raise _sdk_error(exc) from exc
    return data or {"items": [], "pagination": {"page": 1, "pageSize": 25, "total": 0}}


def list_facturacion_from_bejerman(
    customer_code: str | None,
    filters: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    company_key: str | None = None,
) -> dict[str, Any]:
    code = _optional_text(customer_code)
    raw_filters = filters or {}
    params = {}
    for source_keys, target_key in (
        (("dateFrom", "desde"), "dateFrom"),
        (("dateTo", "hasta"), "dateTo"),
        (("origin", "origen", "tipo"), "origin"),
        (("page",), "page"),
        (("pageSize",), "pageSize"),
        (("search",), "search"),
    ):
        for source_key in source_keys:
            value = raw_filters.get(source_key)
            if value not in (None, ""):
                params[target_key] = value
                break
    try:
        client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
        effective_code = _resolve_facturacion_customer_code(client, code) if code else ""
        _default_facturacion_dates(params)
        responses = [
            client.list_comprobantes_ventas(
                build_sales_filters(effective_code, params.get("dateFrom"), params.get("dateTo"), comprobante_type)
            )
            for comprobante_type in _facturacion_query_comprobante_types(params)
        ]
        data = build_facturacion_result(
            responses,
            effective_code,
            params,
        )
        data["requestedCustomerCode"] = code
        data["effectiveCustomerCode"] = as_string(effective_code)
        data["bejermanCustomerCode"] = effective_code
        data["scope"] = "customer" if code else "company"
    except Exception as exc:
        raise _sdk_error(exc) from exc
    return data or {"items": [], "pagination": {"page": 1, "pageSize": 25, "total": 0}}


def _invoice_text_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", as_string(value).upper())


def _invoice_digits_key(value: Any) -> str:
    digits = _digits_only(value)
    return digits.lstrip("0") or ("0" if digits else "")


def _invoice_year_from_order(order: dict[str, Any]) -> int:
    today_year = timezone.localdate().year
    for field in ("invoicedAt", "invoiceDate", "orderDate", "createdAt", "updatedAt"):
        match = re.search(r"(20\d{2}|19\d{2})", as_string(order.get(field)))
        if match:
            return min(int(match.group(1)), today_year)
    return today_year


def _invoice_search_windows(order: dict[str, Any]) -> list[tuple[str, str]]:
    today = timezone.localdate()
    base_year = _invoice_year_from_order(order)
    windows = [(f"{base_year}-01-01", today.isoformat())]
    previous_year = base_year - 1
    if previous_year >= 2000:
        windows.append((f"{previous_year}-01-01", today.isoformat()))
    return windows


def _invoice_document_candidates(item: dict[str, Any]) -> list[str]:
    tipo = as_string(item.get("type") or item.get("comprobanteTipo"))
    letter = as_string(item.get("letter") or item.get("comprobanteLetra"))
    point = as_string(item.get("pointOfSale") or item.get("comprobantePtoVenta"))
    number = as_string(item.get("number") or item.get("comprobanteNumero"))
    candidates = [
        item.get("documentNumber"),
        item.get("numero"),
        item.get("number"),
        item.get("comprobanteNumero"),
        f"{tipo} {letter} {point}-{number}".strip(),
        f"{point}-{number}".strip("-"),
    ]
    return [as_string(value) for value in candidates if as_string(value)]


def _invoice_matches_item(invoice_number: str, item: dict[str, Any]) -> bool:
    target_text = _invoice_text_key(invoice_number)
    target_digits = _invoice_digits_key(invoice_number)
    if not target_text and not target_digits:
        return False

    for candidate in _invoice_document_candidates(item):
        if target_text and target_text == _invoice_text_key(candidate):
            return True
        if target_digits and target_digits == _invoice_digits_key(candidate):
            return True

    point_digits = _digits_only(item.get("pointOfSale") or item.get("comprobantePtoVenta"))
    number_digits = _digits_only(item.get("number") or item.get("comprobanteNumero"))
    if target_digits and number_digits:
        if target_digits == (number_digits.lstrip("0") or "0"):
            return True
        point_number = f"{point_digits}{number_digits}"
        if point_number and target_digits == (point_number.lstrip("0") or "0"):
            return True
    return False


def resolve_delivery_order_invoice_document(order: dict[str, Any], actor_user_id: int | None = None) -> dict[str, Any]:
    invoice_number = _optional_text(order.get("invoiceNumber") or order.get("invoice_number"))
    if not invoice_number:
        raise BillingError("INVOICE_REQUIRED", "La orden no tiene número de factura cargado.", status_code=409)
    customer_code = _optional_text(order.get("bejermanCustomerCode") or order.get("bejerman_customer_code"))
    if not customer_code:
        raise BillingError("CUSTOMER_CODE_REQUIRED", "La orden no tiene código Bejerman de cliente.", status_code=409)

    company_key = _company_key_for_order(order)
    matches_by_id: dict[str, dict[str, Any]] = {}
    last_payload: dict[str, Any] | None = None
    for date_from, date_to in _invoice_search_windows(order):
        page = 1
        while True:
            payload = list_facturacion_from_bejerman(
                customer_code,
                {
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "origin": "FC",
                    "page": page,
                    "pageSize": 100,
                },
                actor_user_id=actor_user_id,
                company_key=company_key,
            )
            last_payload = payload
            for item in payload.get("items") or []:
                document_id = _optional_text(item.get("documentId") or item.get("id"))
                if document_id and _invoice_matches_item(invoice_number, item):
                    matches_by_id.setdefault(document_id, item)
            pagination = payload.get("pagination") if isinstance(payload, dict) else {}
            total_pages = int((pagination or {}).get("totalPages") or page)
            if not (pagination or {}).get("hasNextPage") or page >= total_pages:
                break
            page += 1
        if matches_by_id:
            break

    if not matches_by_id:
        raise BillingError(
            "INVOICE_DOCUMENT_NOT_FOUND",
            f"No se encontró en Bejerman una factura que coincida con {invoice_number}.",
            status_code=404,
        )
    if len(matches_by_id) > 1:
        raise BillingError(
            "INVOICE_DOCUMENT_AMBIGUOUS",
            f"Bejerman devolvió más de una factura compatible con {invoice_number}.",
            status_code=409,
        )
    document = next(iter(matches_by_id.values()))
    document["effectiveCustomerCode"] = (last_payload or {}).get("effectiveCustomerCode") or customer_code
    document["companyKey"] = company_key
    return document


def get_delivery_order_invoice_pdf(order_id: str, actor_user_id: int | None = None) -> tuple[bytes, str, str]:
    order = get_delivery_order(order_id, include_events=False)
    document = resolve_delivery_order_invoice_document(order, actor_user_id=actor_user_id)
    document_id = document.get("documentId") or document.get("id")
    customer_code = document.get("bejermanCustomerCode") or document.get("customerCode") or order.get("bejermanCustomerCode")
    company_key = document.get("companyKey") or _company_key_for_order(order)
    bytes_, content_type = get_facturacion_pdf(
        customer_code,
        document_id,
        interactive=True,
        actor_user_id=actor_user_id,
        company_key=company_key,
    )
    invoice_label = re.sub(r"[^A-Za-z0-9._-]+", "-", as_string(order.get("invoiceNumber"))).strip("-") or as_string(order_id)
    return bytes_, content_type, f"factura-{invoice_label}.pdf"


def get_facturacion_pdf(
    customer_code: str,
    document_id: str,
    *,
    interactive: bool = False,
    actor_user_id: int | None = None,
    company_key: str | None = None,
) -> tuple[bytes, str]:
    code = _optional_text(customer_code)
    doc = _optional_text(document_id)
    if not doc:
        raise BillingError("DOCUMENT_REQUIRED", "Documento requerido")
    started_at = time.monotonic()
    try:
        decoded = decode_document_id(doc)
        if not decoded.get("type") and decoded.get("t"):
            decoded["type"] = decoded["t"]
        if not all(decoded.get(key) for key in ("t", "n", "l", "p", "f")):
            raise BillingError("DOCUMENT_ID_INVALID", "Referencia de comprobante inválida", status_code=400)
        bejerman_customer_code = decoded.get("c") or code
        if not _optional_text(bejerman_customer_code):
            raise BillingError("CUSTOMER_CODE_REQUIRED", "Código Bejerman requerido", status_code=400)
        return fetch_comprobante_pdf(
            BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id),
            BejermanPdfReference(
                type=decoded["t"],
                number=decoded["n"],
                letter=decoded["l"],
                point_of_sale=decoded["p"],
                issue_date=decoded["f"],
                customer_code=bejerman_customer_code,
            ),
            interactive=interactive,
        )
    except BejermanSdkUnavailable as exc:
        if interactive:
            logger.info(
                "bejerman_remito_pdf_timeout_pending",
                extra={"document_id": doc, "duration_ms": int((time.monotonic() - started_at) * 1000)},
            )
            raise BillingError(
                "BEJERMAN_PDF_PENDING",
                "Bejerman no respondió a tiempo al pedir el PDF. El remito ya está emitido; se reintentará la descarga.",
                status_code=202,
                retry_after_ms=5000,
            ) from exc
        raise _sdk_error(exc) from exc
    except Exception as exc:
        if isinstance(exc, BillingError):
            raise
        raise _pdf_sdk_error(exc) from exc


def _env_text(name: str, default: str) -> str:
    value = getattr(settings, name, None)
    if value is None:
        value = os.getenv(name, default)
    text = str(value or "").strip()
    return text or default


def _remito_requires_billing(profile: dict[str, Any]) -> bool:
    comprobante_tipo = (_optional_text(profile.get("type") or profile.get("comprobanteTipo")) or "").upper()
    return remito_type_requires_billing(comprobante_tipo)


def _delivery_article_codes_requiring_vat(bridge_request: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    if not _remito_requires_billing(profile):
        return []
    codes: set[str] = set()
    for order in bridge_request.get("orders") or []:
        if not isinstance(order, dict):
            continue
        for item in order.get("items") or []:
            if not isinstance(item, dict):
                continue
            code = _optional_text(item.get("articleCode"))
            unit_price = as_number(item.get("unitPrice")) or 0
            if code and unit_price > 0:
                codes.add(code)
    return sorted(codes)


def _delivery_article_sales_vat_map(
    client: BejermanSDKClient,
    bridge_request: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    article_codes = _delivery_article_codes_requiring_vat(bridge_request, profile)
    if not article_codes:
        return {}
    catalog = client.list_articulos([], max(50, len(article_codes)))
    return build_article_sales_vat_map(catalog, article_codes)


def _delivery_status_for_remito_profile(profile: dict[str, Any]) -> str:
    return "entregado_pendiente_facturacion" if _remito_requires_billing(profile) else "entregado_no_facturable"


def _env_override(name: str) -> str | None:
    value = getattr(settings, name, None)
    if value is None:
        value = os.getenv(name)
    text = str(value or "").strip()
    return text or None


def _delivery_profile_prefix(delivery_type: str) -> str:
    return {
        "rental": "RENTAL",
        "demo": "DEMO",
        "service_release": "SERVICE",
    }.get(normalize_delivery_type(delivery_type), "SALE")


def _clean_company_marker(value: Any) -> str:
    text = str(value or "").upper()
    return "".join(char for char in text if char.isalnum())


def _company_key_for_marker(value: Any) -> str | None:
    marker = _clean_company_marker(value)
    if not marker:
        return None
    if marker in {"MG", "MGB", "MGBI", "MGBIO", "MGBIOSA", "PORMG"}:
        return "MGBIO"
    if "MGBIO" in marker or marker.startswith("MGBI"):
        return "MGBIO"
    if marker in {
        "SEPID",
        "SEP",
        "SEPIDSA",
        "CEPIL",
        "ALTAALQUILER",
        "ALQUILERES",
        "REPARACION",
        "MARKETING",
        "DEMO",
        "RESPIFLOW",
        "NOVAMED",
    }:
        return "SEPID"
    return None


def _company_key_for_order(order: dict[str, Any]) -> str:
    for value in (
        order.get("companyKey"),
        order.get("company_key"),
        order.get("operationCompanyLabel"),
        order.get("sourceCompanyId"),
    ):
        company_key = _company_key_for_marker(value)
        if company_key:
            return company_key
    return "SEPID"


def _parse_delivery_order_remito_number(order: dict[str, Any]) -> dict[str, str]:
    remito_number = _optional_text(order.get("remitoNumber") or order.get("remito_number"))
    if not remito_number:
        raise BillingError("REMITO_REQUIRED", "La orden no tiene número de remito cargado.", status_code=409)

    profile = remito_profile_for_type(order.get("deliveryType") or order.get("delivery_type") or "sale")
    try:
        return parse_bejerman_remito_number(
            remito_number,
            default_type=profile.comprobante_tipo,
            default_letter="R",
            allowed_types=REMITO_COMPROBANTE_TYPES,
            complete_error_message="Cargue el remito completo, por ejemplo RSS R 00004-00004715.",
        )
    except ValueError as exc:
        message = str(exc)
        code = "INVALID_REMITO_TYPE" if message.startswith("Tipo de remito no válido") else "REMITO_NUMBER_INVALID"
        raise BillingError(code, message, status_code=400) from exc


def _remito_item_matches_document(document: dict[str, str], item: dict[str, Any]) -> bool:
    item_type = (_optional_text(item.get("type") or item.get("comprobanteTipo")) or "").upper()
    item_letter = (_optional_text(item.get("letter") or item.get("comprobanteLetra")) or "").upper()
    item_point = _digits_only(item.get("pointOfSale") or item.get("comprobantePtoVenta")).zfill(5)
    item_number = _digits_only(item.get("number") or item.get("comprobanteNumero")).zfill(8)
    return (
        item_type == document["type"]
        and (not item_letter or item_letter == document["letter"])
        and item_point == document["point"]
        and item_number == document["number"]
    )


def _delivery_order_remito_lookup_attempts(customer_code: str, document: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    base = {"remitoType": document["type"], "pageSize": 100}
    search_terms = [document["number"], document["remitoNumber"]]
    customer_scopes = [customer_code, ""]
    attempts: list[tuple[str, dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()

    for search_term in search_terms:
        for scope_code in customer_scopes:
            params = {**base, "search": search_term}
            key = (scope_code, params["remitoType"], params.get("search") or "")
            if key not in seen:
                seen.add(key)
                attempts.append((scope_code, params))

    for scope_code in customer_scopes:
        key = (scope_code, base["remitoType"], "")
        if key not in seen:
            seen.add(key)
            attempts.append((scope_code, dict(base)))

    return attempts


def resolve_delivery_order_remito_document(order: dict[str, Any], actor_user_id: int | None = None) -> dict[str, Any]:
    customer_code = _optional_text(order.get("bejermanCustomerCode") or order.get("bejerman_customer_code"))
    document = _parse_delivery_order_remito_number(order)
    company_key = _company_key_for_order(order)
    matches_by_id: dict[str, dict[str, Any]] = {}
    matched_payload: dict[str, Any] = {}
    first_error: BillingError | None = None
    for scope_customer_code, params in _delivery_order_remito_lookup_attempts(customer_code, document):
        try:
            payload = list_remitos_from_bejerman(
                scope_customer_code,
                params,
                actor_user_id=actor_user_id,
                company_key=company_key,
            )
        except BillingError as exc:
            if first_error is None:
                first_error = exc
            if scope_customer_code:
                continue
            raise
        for item in payload.get("items") or []:
            document_id = _optional_text(item.get("documentId") or item.get("id"))
            if document_id and _remito_item_matches_document(document, item):
                matches_by_id.setdefault(document_id, item)
        if matches_by_id:
            matched_payload = payload
            break

    if not matches_by_id:
        if first_error is not None:
            raise first_error
        raise BillingError(
            "REMITO_DOCUMENT_NOT_FOUND",
            f"No se encontró en Bejerman un remito que coincida con {document['remitoNumber']}.",
            status_code=404,
        )
    if len(matches_by_id) > 1:
        raise BillingError(
            "REMITO_DOCUMENT_AMBIGUOUS",
            f"Bejerman devolvió más de un remito compatible con {document['remitoNumber']}.",
            status_code=409,
        )
    resolved = next(iter(matches_by_id.values()))
    resolved["effectiveCustomerCode"] = (
        (matched_payload or {}).get("effectiveCustomerCode")
        or resolved.get("bejermanCustomerCode")
        or resolved.get("customerCode")
        or customer_code
    )
    resolved["companyKey"] = company_key
    return resolved


def get_delivery_order_remito_pdf(order_id: str, actor_user_id: int | None = None) -> tuple[bytes, str, str]:
    order = get_delivery_order(order_id, include_events=False)
    document = resolve_delivery_order_remito_document(order, actor_user_id=actor_user_id)
    document_id = document.get("documentId") or document.get("id")
    customer_code = document.get("bejermanCustomerCode") or document.get("customerCode") or order.get("bejermanCustomerCode")
    company_key = document.get("companyKey") or _company_key_for_order(order)
    bytes_, content_type = get_facturacion_pdf(
        customer_code,
        document_id,
        interactive=True,
        actor_user_id=actor_user_id,
        company_key=company_key,
    )
    remito_label = re.sub(r"[^A-Za-z0-9._-]+", "-", as_string(order.get("remitoNumber"))).strip("-") or as_string(order_id)
    return bytes_, content_type, f"remito-{remito_label}.pdf"


def _profile_value(delivery_type: str, company_key: str, field: str, default: str) -> str:
    prefix = _delivery_profile_prefix(delivery_type)
    company = (company_key or "SEPID").strip().upper() or "SEPID"
    if field == "POINT_OF_SALE":
        if company == "MGBIO":
            return "00007"
        if company == "SEPID":
            return "00004"
        names = [
            f"BEJERMAN_REMITO_{prefix}_{company}_{field}",
            f"BEJERMAN_REMITO_{company}_{field}",
        ]
        for name in names:
            value = _env_override(name)
            if value:
                return value
        value = _env_override(f"BEJERMAN_REMITO_{prefix}_{field}")
        return value or default
    names = [f"BEJERMAN_REMITO_{prefix}_{company}_{field}", f"BEJERMAN_REMITO_{prefix}_{field}"]
    for name in names:
        value = _env_override(name)
        if value:
            return value
    return default


def _profile_for_order(delivery_type: str, company_key: str = "SEPID"):
    delivery_type = normalize_delivery_type(delivery_type)
    profile = remito_profile_for_type(delivery_type)
    return {
        "type": _profile_value(delivery_type, company_key, "TYPE", profile.comprobante_tipo),
        "pointOfSale": _profile_value(delivery_type, company_key, "POINT_OF_SALE", profile.point_of_sale),
        "operation": _profile_value(delivery_type, company_key, "OPERATION", profile.operation_code),
        "deposit": _profile_value(delivery_type, company_key, "DEPOSIT", profile.deposit_code),
    }


def _format_points_of_sale(points: list[str]) -> str:
    if len(points) <= 1:
        return points[0] if points else ""
    return ", ".join(points[:-1]) + f" o {points[-1]}"


def _manual_delivery_allowed_points(profile: dict[str, Any]) -> list[str]:
    points: list[str] = []
    for raw in (
        DELIVERY_MANUAL_REGISTER_POINT_OF_SALE,
        profile.get("pointOfSale"),
        *sorted(DELIVERY_EXISTING_BEJERMAN_POINTS_OF_SALE),
    ):
        point = _digits_only(raw).zfill(5)
        if point and point != "00000" and point not in points:
            points.append(point)
    return points


def _validate_manual_delivery_point_of_sale(point: str, profile: dict[str, Any]) -> None:
    allowed = _manual_delivery_allowed_points(profile)
    if point not in allowed:
        raise ValueError(f"El punto de venta del remito debe ser {_format_points_of_sale(allowed)}")


def _company_key_for_manual_remito_point(point: str, fallback: str) -> str:
    normalized = _digits_only(point).zfill(5)
    if normalized == "00007":
        return "MGBIO"
    if normalized == "00004":
        return "SEPID"
    return fallback


def normalize_delivery_order_manual_remito_number(order: dict[str, Any], value: Any) -> dict[str, Any]:
    delivery_type = normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type") or "sale")
    order_company_key = _company_key_for_order(order)
    profile = _profile_for_order(delivery_type, order_company_key)
    try:
        document = parse_bejerman_remito_number(
            value,
            default_type=profile["type"],
            default_letter="R",
            allowed_types=(profile["type"],),
            require_explicit_match=True,
            validate_point=lambda point: _validate_manual_delivery_point_of_sale(point, profile),
            allow_number_only=False,
            require_complete=True,
            complete_error_message=(
                f"Debe cargar el remito con punto de venta, por ejemplo "
                f"{profile['type']} R {profile['pointOfSale']}-00004715."
            ),
            explicit_mismatch_message=f"El remito debe corresponder a {profile['type']} R para esta orden de entrega.",
        )
    except ValueError as exc:
        raise DeliveryOrderError("MANUAL_REMITO_INVALID", str(exc), status_code=409) from exc

    point = document["point"]
    document_mode = "register" if point == DELIVERY_MANUAL_REGISTER_POINT_OF_SALE else "existing"
    company_key = _company_key_for_manual_remito_point(point, order_company_key)
    return {
        **document,
        "documentMode": document_mode,
        "stockMovementGenerated": document_mode == "register",
        "companyKey": company_key,
        "profile": {**profile, "pointOfSale": point},
    }


def _manual_delivery_seller_code(order: dict[str, Any], actor_user_id: int | None, delivery_type: str) -> str:
    type_default_seller = {
        "service_release": _env_text("BEJERMAN_REMITO_SERVICE_SELLER", "ADM"),
        "rental": _env_text("BEJERMAN_REMITO_RENTAL_SELLER", "ADM"),
        "demo": _env_text("BEJERMAN_REMITO_DEMO_SELLER", "ADM"),
    }.get(delivery_type)
    return (
        _optional_text(order.get("sellerCode") or order.get("seller_code"))
        or type_default_seller
        or get_user_seller_code(actor_user_id)
        or _env_text("BEJERMAN_REMITO_SELLER", "ADM")
    )


def _insert_manual_delivery_remito_group(
    order: dict[str, Any],
    actor_user_id: int | None,
    document: dict[str, Any],
    *,
    group_id: str | None = None,
    seller_code: str,
    payment_term_code: str,
    response_summary: dict[str, Any],
) -> str:
    group_id = group_id or f"brg-{uuid4()}"
    order_id = order["id"]
    profile = document["profile"]
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_remito_groups (
              id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
              comprobante_numero, remito_number, customer_code, customer_name,
              seller_code, payment_term_code, operation_code, deposit_code,
              status, order_ids, response_summary, created_by_user_id, generated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'generated', %s::jsonb, %s::jsonb, %s, CURRENT_TIMESTAMP)
            """,
            [
                group_id,
                document["companyKey"],
                document["type"],
                document["letter"],
                document["point"],
                document["number"],
                document["remitoNumber"],
                _optional_text(order.get("bejermanCustomerCode") or order.get("bejerman_customer_code")) or "",
                _optional_text(order.get("customerName") or order.get("customer_name")) or "",
                seller_code,
                payment_term_code,
                profile.get("operation") or "",
                profile.get("deposit") or "",
                _json_param([order_id]),
                _json_param(response_summary),
                actor_user_id,
            ],
        )
        cur.execute(
            """
            UPDATE delivery_orders
               SET bejerman_remito_group_id = %s
             WHERE id = %s
            """,
            [group_id, order_id],
        )
    return group_id


def _manual_delivery_bridge_request(
    order: dict[str, Any],
    group_id: str,
    compatibility: dict[str, Any],
    seller_code: str,
    payment_term_code: str,
) -> dict[str, Any]:
    return {
        "groupId": group_id,
        "companyKey": compatibility["companyKey"],
        "issueDate": timezone.localdate().isoformat(),
        "customerCode": compatibility["customerCode"],
        "customerName": compatibility["customerName"],
        "priceCurrency": compatibility["priceCurrency"],
        "exchangeRate": compatibility["exchangeRate"],
        "sellerCode": seller_code,
        "paymentTermCode": payment_term_code,
        "notes": "Remito cargado manualmente en NEXORA",
        "orders": [_bridge_order(order)],
    }


def _register_manual_delivery_order_stock_movement(
    order: dict[str, Any],
    actor_user_id: int | None,
    document: dict[str, Any],
) -> dict[str, Any]:
    compatibility = _validate_remito_orders([order])
    seller_code = _manual_delivery_seller_code(order, actor_user_id, compatibility["deliveryType"])
    payment_term_code = _env_text("BEJERMAN_REMITO_PAYMENT_TERM", _env_text("BEJERMAN_RIS_PAYMENT_TERM", "30"))
    group_id = f"brg-{uuid4()}"
    profile = {
        **_profile_for_order(compatibility["deliveryType"], compatibility["companyKey"]),
        "pointOfSale": document["point"],
    }
    bridge_request = _manual_delivery_bridge_request(order, group_id, compatibility, seller_code, payment_term_code)
    bridge_request["companyKey"] = document["companyKey"]

    try:
        client = BejermanSDKClient(company_key=document["companyKey"], actor_user_id=actor_user_id)
        customer_fields = resolve_customer_document_fields(client, compatibility["customerCode"])
        article_tax_by_code = _delivery_article_sales_vat_map(client, bridge_request, profile)
        built = build_delivery_remito_comprobante(
            bridge_request,
            delivery_remito_config(profile),
            customer_fields,
            article_tax_by_code,
        )
        comprobante = built["comprobante"]
        comprobante["Comprobante_PtoVenta"] = document["point"]
        comprobante["Comprobante_Numero"] = document["number"]
        raw_response = client.ingresar_comprobante_ventas_json(
            comprobante,
            circuito=_env_text("BEJERMAN_REMITO_CIRCUIT", "VENTAS"),
            operacion=_env_text("BEJERMAN_REMITO_OPERATION", "IngresarComprobanteJSON"),
            numera_flex="N",
            emite_reg="R",
        )
    except Exception as exc:
        converted = _sdk_error(exc)
        raise DeliveryOrderError(converted.code, str(converted), status_code=converted.status_code) from exc

    response = parse_remito_response(raw_response)
    response_summary = {
        **response,
        "comprobanteTipo": response.get("comprobanteTipo") or document["type"],
        "comprobanteLetra": response.get("comprobanteLetra") or document["letter"],
        "comprobantePtoVenta": response.get("comprobantePtoVenta") or document["point"],
        "comprobanteNumero": response.get("comprobanteNumero") or document["number"],
        "remitoNumber": response.get("remitoNumber") or document["remitoNumber"],
        "companyKey": document["companyKey"],
        "profile": profile,
        "documentMode": "register",
        "manualRemitoNumber": document["remitoNumber"],
        "stockMovementGenerated": True,
        "stockMovementSource": "nexora_manual_pv1",
        "billingRequired": _remito_requires_billing(profile),
        "lineCount": built["lineCount"],
        "raw": raw_response,
    }
    group_id = _insert_manual_delivery_remito_group(
        order,
        actor_user_id,
        document,
        group_id=group_id,
        seller_code=seller_code,
        payment_term_code=payment_term_code,
        response_summary=response_summary,
    )
    create_event(
        order["id"],
        actor_user_id,
        "bejerman_remito_registered",
        metadata={
            "groupId": group_id,
            "remitoNumber": document["remitoNumber"],
            "documentMode": "register",
            "stockMovementGenerated": True,
        },
    )
    return {
        "groupId": group_id,
        "remitoNumber": document["remitoNumber"],
        "documentMode": "register",
        "stockMovementGenerated": True,
    }


def _associate_existing_delivery_order_remito(
    order: dict[str, Any],
    actor_user_id: int | None,
    document: dict[str, Any],
) -> dict[str, Any]:
    delivery_type = normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type") or "sale")
    seller_code = _manual_delivery_seller_code(order, actor_user_id, delivery_type)
    payment_term_code = _env_text("BEJERMAN_REMITO_PAYMENT_TERM", _env_text("BEJERMAN_RIS_PAYMENT_TERM", "30"))
    profile = document["profile"]
    response_summary = {
        "comprobanteTipo": document["type"],
        "comprobanteLetra": document["letter"],
        "comprobantePtoVenta": document["point"],
        "comprobanteNumero": document["number"],
        "remitoNumber": document["remitoNumber"],
        "companyKey": document["companyKey"],
        "profile": profile,
        "documentMode": "existing",
        "existingBejermanRemito": True,
        "stockMovementGenerated": False,
        "stockMovementSource": "bejerman_direct",
        "billingRequired": _remito_requires_billing(profile),
    }
    group_id = _insert_manual_delivery_remito_group(
        order,
        actor_user_id,
        document,
        seller_code=seller_code,
        payment_term_code=payment_term_code,
        response_summary=response_summary,
    )
    create_event(
        order["id"],
        actor_user_id,
        "bejerman_remito_associated",
        metadata={
            "groupId": group_id,
            "remitoNumber": document["remitoNumber"],
            "documentMode": "existing",
            "stockMovementGenerated": False,
        },
    )
    return {
        "groupId": group_id,
        "remitoNumber": document["remitoNumber"],
        "documentMode": "existing",
        "stockMovementGenerated": False,
    }


def register_delivery_order_manual_remito(
    order: dict[str, Any],
    actor_user_id: int | None,
    remito_number: Any,
) -> dict[str, Any]:
    document = normalize_delivery_order_manual_remito_number(order, remito_number)
    if document["documentMode"] == "register":
        return _register_manual_delivery_order_stock_movement(order, actor_user_id, document)
    return _associate_existing_delivery_order_remito(order, actor_user_id, document)


def _validate_remito_orders(orders: list[dict[str, Any]]) -> dict[str, Any]:
    if not orders:
        raise DeliveryOrderError("DELIVERY_ORDER_NOT_FOUND", "No se encontraron órdenes", status_code=404)
    customer_codes = {(_optional_text(order.get("bejermanCustomerCode")) or "") for order in orders}
    if len(customer_codes) != 1 or not next(iter(customer_codes)):
        raise DeliveryOrderError("CUSTOMER_MAPPING_REQUIRED", "Las órdenes necesitan el mismo código Bejerman", status_code=409)
    delivery_types = {normalize_delivery_type(order.get("deliveryType")) for order in orders}
    if len(delivery_types) != 1:
        raise DeliveryOrderError(
            "INCOMPATIBLE_DELIVERY_REMITO_PROFILE",
            "Las órdenes requieren distinto tipo u operación de remito",
            status_code=409,
        )
    company_keys = {_company_key_for_order(order) for order in orders}
    if len(company_keys) != 1:
        labels = sorted(
            {
                _optional_text(order.get("companyKey"))
                or _optional_text(order.get("operationCompanyLabel"))
                or _optional_text(order.get("sourceCompanyId"))
                or _company_key_for_order(order)
                for order in orders
            }
        )
        raise DeliveryOrderError(
            "INCOMPATIBLE_DELIVERY_REMITO_COMPANY",
            f"Las órdenes corresponden a empresas Bejerman distintas: {', '.join(labels)}",
            status_code=409,
        )
    price_currencies: set[str] = set()
    for order in orders:
        price_currencies.update(_order_item_price_currencies(order))
    if len(price_currencies) != 1:
        raise DeliveryOrderError(
            "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY",
            "Las órdenes seleccionadas tienen monedas de precio distintas",
            status_code=409,
        )
    price_currency = next(iter(price_currencies))
    exchange_rates = {_order_exchange_rate(order) for order in orders if _order_exchange_rate(order)}
    exchange_rate = next(iter(exchange_rates)) if exchange_rates else ""
    if price_currency == "USD":
        parsed_exchange_rates = [_parsed_order_exchange_rate(rate) for rate in exchange_rates]
        if not exchange_rates or any(rate is None for rate in parsed_exchange_rates):
            raise DeliveryOrderError(
                "DELIVERY_REMITO_EXCHANGE_RATE_REQUIRED",
                "Las órdenes en dólares requieren tipo de cambio antes de emitir el remito",
                status_code=409,
            )
        unique_exchange_rates = set(parsed_exchange_rates)
        if len(unique_exchange_rates) > 1:
            raise DeliveryOrderError(
                "INCOMPATIBLE_DELIVERY_REMITO_EXCHANGE_RATE",
                "Las órdenes en dólares tienen tipos de cambio distintos",
                status_code=409,
            )
        if len(exchange_rates) > 1:
            exchange_rate = str(next(iter(unique_exchange_rates)))
    for order in orders:
        if order.get("status") == "pendiente_stock":
            raise DeliveryOrderError(
                "DELIVERY_ORDER_STOCK_PENDING",
                "Una de las órdenes está pendiente de stock",
                status_code=409,
            )
        if order.get("status") in {"facturado", "cancelado"}:
            raise DeliveryOrderError("DELIVERY_ORDER_CLOSED", "Una de las órdenes ya está cerrada", status_code=409)
        if _optional_text(order.get("remitoNumber")):
            raise DeliveryOrderError("SALES_ORDER_ALREADY_HAS_REMITO", "Una de las órdenes ya tiene remito cargado", status_code=409)
        if _optional_text(order.get("bejermanRemitoGroupId")):
            raise DeliveryOrderError("SALES_ORDER_REMITO_GROUP_IN_PROGRESS", "Una de las órdenes ya tiene una emisión Bejerman en curso", status_code=409)
        if not _order_has_article_line(order):
            raise DeliveryOrderError("DELIVERY_REMITO_ARTICLE_REQUIRED", "Cada orden necesita al menos un artículo Bejerman asociado", status_code=409)
        _assert_partidas_ready(order)
    return {
        "customerCode": next(iter(customer_codes)),
        "customerName": orders[0].get("customerName") or "",
        "deliveryType": next(iter(delivery_types)),
        "companyKey": next(iter(company_keys)),
        "priceCurrency": price_currency,
        "exchangeRate": exchange_rate,
    }


def _order_has_article_line(order: dict[str, Any]) -> bool:
    return any(_optional_text(item.get("articleCode")) for item in (order.get("items") or []))


def _item_quantity(item: dict[str, Any]) -> float:
    try:
        return float(item.get("quantity") or 1)
    except (TypeError, ValueError):
        return 1


def _effective_item_partida(order: dict[str, Any], item: dict[str, Any]) -> str | None:
    explicit = _optional_text(item.get("partida"))
    if explicit:
        return explicit
    if item.get("partidas"):
        return None
    if _item_quantity(item) > 1:
        return None
    if order.get("deliveryType") == "sale":
        return None
    return _optional_text(order.get("equipmentSerial"))


def _item_article_requires_partida(item: dict[str, Any]) -> bool | None:
    if "articleRequiresPartida" in item:
        return as_nullable_bool(item.get("articleRequiresPartida"))
    return as_nullable_bool(item.get("article_requires_partida"))


def _item_can_omit_catalog_partida(order: dict[str, Any], item: dict[str, Any]) -> bool:
    return normalize_delivery_type(order.get("deliveryType") or order.get("delivery_type")) in {"sale", "demo"} and _item_article_requires_partida(item) is False


def _partida_policy_unknown_error(item: dict[str, Any], action: str) -> DeliveryOrderError:
    article_code = _optional_text(item.get("articleCode") or item.get("article_code")) or "el artículo"
    return DeliveryOrderError(
        "DELIVERY_ORDER_ARTICLE_PARTIDA_POLICY_UNKNOWN",
        f"No se pudo verificar si el artículo {article_code} requiere partida. Reintente la verificación Bejerman o cargue partidas antes de {action}.",
        status_code=409,
    )


def _assert_partidas_ready(order: dict[str, Any]) -> None:
    for item in order.get("items") or []:
        if not _optional_text(item.get("articleCode")):
            continue
        quantity = _item_quantity(item)
        item_partidas = item.get("partidas") or []
        explicit_partida = _optional_text(item.get("partida"))
        if order.get("deliveryType") == "rental":
            if not explicit_partida and not item_partidas:
                raise DeliveryOrderError(
                    "DELIVERY_REMITO_PARTIDAS_INCOMPLETE",
                    "Faltan NS para emitir el RTA",
                    status_code=409,
                )
            if item_partidas:
                assigned = 0.0
                for partida in item_partidas:
                    try:
                        assigned += float(partida.get("assignedQuantity") or 0)
                    except (TypeError, ValueError):
                        pass
                    if (_optional_text(partida.get("stockDepositCode")) or "").upper() != RENTAL_STOCK_DEPOSIT:
                        raise DeliveryOrderError(
                            "RENTAL_STL_REQUIRED",
                            "Los alquileres deben salir del depósito STL",
                            status_code=409,
                        )
                if assigned <= 0 or abs(assigned - quantity) > 0.0001:
                    raise DeliveryOrderError(
                        "DELIVERY_REMITO_PARTIDAS_INCOMPLETE",
                        "Faltan NS para emitir el RTA",
                        status_code=409,
                    )
                continue
            if (_optional_text(item.get("stockDepositCode")) or "").upper() != RENTAL_STOCK_DEPOSIT:
                raise DeliveryOrderError(
                    "RENTAL_STL_REQUIRED",
                    "Los alquileres deben salir del depósito STL",
                    status_code=409,
                )
            continue
        if normalize_delivery_type(order.get("deliveryType")) in {"sale", "demo"} and not explicit_partida and not item_partidas:
            if _item_can_omit_catalog_partida(order, item):
                continue
            if _item_article_requires_partida(item) is None:
                raise _partida_policy_unknown_error(item, "emitir el remito")
            raise DeliveryOrderError("DELIVERY_REMITO_PARTIDAS_INCOMPLETE", "Complete las partidas antes de emitir el remito", status_code=409)
        assigned = 0.0
        for partida in item_partidas:
            try:
                assigned += float(partida.get("assignedQuantity") or 0)
            except (TypeError, ValueError):
                pass
        equipment_serial_fallback = (
            not explicit_partida
            and not item_partidas
            and bool(_effective_item_partida(order, item))
        )
        if equipment_serial_fallback or (quantity <= 1 and not item_partidas):
            continue
        if assigned <= 0 or abs(assigned - quantity) > 0.0001:
            raise DeliveryOrderError("DELIVERY_REMITO_PARTIDAS_INCOMPLETE", "Complete las partidas antes de emitir el remito", status_code=409)


def _bridge_order(order: dict[str, Any]) -> dict[str, Any]:
    order_currency = _order_price_currency(order)
    return {
        "id": order.get("id"),
        "orderNumber": order.get("orderNumber"),
        "deliveryType": order.get("deliveryType"),
        "companyKey": order.get("companyKey"),
        "priceCurrency": next(iter(_order_item_price_currencies(order))),
        "commercialExchangeRate": _order_exchange_rate(order),
        "sourceReference": delivery_source_reference(order.get("sourceReference"), order.get("deliveryType"), order.get("ingresoId")),
        "sourceCompanyId": order.get("sourceCompanyId"),
        "serviceResolution": order.get("rawPedido") if order.get("deliveryType") == "service_release" else None,
        "customerName": order.get("customerName"),
        "operationCompanyLabel": order.get("operationCompanyLabel"),
        "equipmentModel": order.get("equipmentModel"),
        "equipmentSerial": order.get("equipmentSerial"),
        "equipmentInternalNumber": order.get("equipmentInternalNumber"),
        "rawPedido": order.get("rawPedido"),
        "items": [
            {
                "id": item.get("id"),
                "ingresoId": item.get("ingresoId"),
                "deviceId": item.get("deviceId"),
                "articleCode": item.get("articleCode") or None,
                "articleName": item.get("articleName") or None,
                "articleRequiresPartida": _item_article_requires_partida(item),
                "description": item.get("description") or item.get("sourceText") or "",
                "quantity": item.get("quantity") or 1,
                "unitPrice": item.get("unitPrice"),
                "priceCurrency": _normalize_price_currency(item.get("priceCurrency") or item.get("price_currency"), order_currency),
                "discountPercent": item.get("discountPercent"),
                "sourceText": item.get("sourceText") or "",
                "partida": _effective_item_partida(order, item),
                "partidaExpirationDate": item.get("partidaExpirationDate"),
                "stockDepositCode": item.get("stockDepositCode"),
                "partidas": item.get("partidas") or [],
                "equipmentSerial": item.get("partida") or order.get("equipmentSerial"),
                "equipmentInternalNumber": order.get("equipmentInternalNumber"),
            }
            for item in (order.get("items") or [])
        ],
    }


def _refresh_service_release_orders_for_remito(orders: list[dict[str, Any]], actor_user_id: int | None) -> list[dict[str, Any]]:
    refreshed_orders: list[dict[str, Any]] = []
    for order in orders:
        if order.get("deliveryType") != "service_release":
            refreshed_orders.append(order)
            continue
        ingreso_ids = service_release_ingreso_ids_from_order(order)
        if not ingreso_ids:
            refreshed_orders.append(order)
            continue
        for ingreso_id in ingreso_ids:
            try:
                ensure_service_release_order_for_ingreso(int(ingreso_id), actor_user_id)
            except (TypeError, ValueError):
                continue
        try:
            refreshed = get_delivery_order(order["id"], include_events=False, actor_user_id=actor_user_id)
        except Exception:
            refreshed = None
        refreshed_orders.append(refreshed or order)
    return refreshed_orders


@transaction.atomic
def reserve_remito_group(
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
    *,
    seller_code: str,
    payment_term_code: str,
) -> tuple[str, dict[str, Any]]:
    compatibility = _validate_remito_orders(orders)
    profile = _profile_for_order(compatibility["deliveryType"], compatibility["companyKey"])
    group_id = f"brg-{uuid4()}"
    order_ids = [order["id"] for order in orders]
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_remito_groups (
              id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
              customer_code, customer_name, seller_code, payment_term_code,
              operation_code, deposit_code, status, order_ids, response_summary, created_by_user_id
            )
            VALUES (%s, %s, %s, 'R', %s, %s, %s, %s, %s, %s, %s, 'pending', %s::jsonb, %s::jsonb, %s)
            """,
            [
                group_id,
                compatibility["companyKey"],
                profile["type"],
                profile["pointOfSale"],
                compatibility["customerCode"],
                compatibility["customerName"],
                seller_code,
                payment_term_code,
                profile["operation"],
                profile["deposit"],
                _json_param(order_ids),
                _json_param({"companyKey": compatibility["companyKey"], "profile": profile}),
                actor_user_id,
            ],
        )
        cur.execute(
            """
            UPDATE delivery_orders
            SET bejerman_remito_group_id = %s
            WHERE id = ANY(%s)
            """,
            [group_id, order_ids],
        )
    return group_id, {**compatibility, "profile": profile}


def _mark_group_failed(group_id: str, order_ids: list[str], actor_user_id: int | None, exc: Exception):
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                SET status = 'failed',
                    response_summary = %s::jsonb
                WHERE id = %s
                """,
                [_json_param({"error": str(exc)}), group_id],
            )
            cur.execute(
                """
                UPDATE delivery_orders
                SET bejerman_remito_group_id = NULL
                WHERE id = ANY(%s) AND bejerman_remito_group_id = %s
                """,
                [order_ids, group_id],
            )
        for order_id in order_ids:
            create_event(order_id, actor_user_id, "bejerman_remito_failed", metadata={"groupId": group_id, "error": str(exc)})


def _table_exists(table_name: str) -> bool:
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


def _table_columns(table_name: str, column_names: set[str]) -> set[str]:
    if not column_names:
        return set()
    try:
        with connection.cursor() as cur:
            placeholders = ",".join(["%s"] * len(column_names))
            cur.execute(
                f"""
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = %s
                   AND column_name IN ({placeholders})
                """,
                [table_name, *sorted(column_names)],
            )
            return {str(row[0]) for row in cur.fetchall()}
    except Exception:
        return set()


def _insert_service_release_delivery_events(ingreso_ids: list[int], actor_user_id: int | None) -> None:
    if not ingreso_ids or not _table_exists("ingreso_events"):
        return
    columns = _table_columns("ingreso_events", {"ticket_id", "a_estado", "usuario_id", "comentario"})
    if not {"ticket_id", "a_estado", "usuario_id", "comentario"}.issubset(columns):
        return
    for ingreso_id in ingreso_ids:
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, comentario)
                        SELECT %s,
                               CASE WHEN estado = 'vendido_entregado' THEN 'vendido_entregado' ELSE 'entregado' END,
                               %s,
                               'Entrega registrada por RSS Bejerman'
                          FROM ingresos
                         WHERE id = %s
                           AND NOT EXISTS (
                             SELECT 1
                               FROM ingreso_events
                              WHERE ticket_id = %s
                                AND a_estado IN ('entregado', 'vendido_entregado')
                           )
                        """,
                        [ingreso_id, actor_user_id, ingreso_id, ingreso_id],
                    )
        except Exception:
            pass


def _sync_service_release_ingresos_after_remito(
    order_ids: list[str],
    group_id: str,
    actor_user_id: int | None,
    remito_number: str,
) -> list[int]:
    remito = _optional_text(remito_number)
    if not remito or not order_ids:
        return []
    columns = _table_columns(
        "ingresos",
        {
            "id",
            "estado",
            "remito_salida",
            "fecha_entrega",
            "alquilado",
            "alquiler_a",
            "alquiler_remito",
            "alquiler_fecha",
        },
    )
    if not {"id", "estado", "remito_salida", "fecha_entrega"}.issubset(columns):
        return []

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ingreso_id
              FROM (
                    SELECT o.ingreso_id
                      FROM delivery_orders o
                     WHERE o.id = ANY(%s)
                       AND o.bejerman_remito_group_id = %s
                       AND o.delivery_type = 'service_release'
                       AND o.ingreso_id IS NOT NULL
                    UNION
                    SELECT doi.ingreso_id
                      FROM delivery_order_items doi
                      JOIN delivery_orders o ON o.id = doi.order_id
                     WHERE o.id = ANY(%s)
                       AND o.bejerman_remito_group_id = %s
                       AND o.delivery_type = 'service_release'
                       AND doi.ingreso_id IS NOT NULL
                    UNION
                    SELECT doip.ingreso_id
                      FROM delivery_order_item_partidas doip
                      JOIN delivery_order_items doi ON doi.id = doip.order_item_id
                      JOIN delivery_orders o ON o.id = doi.order_id
                     WHERE o.id = ANY(%s)
                       AND o.bejerman_remito_group_id = %s
                       AND o.delivery_type = 'service_release'
                       AND doip.ingreso_id IS NOT NULL
                   ) linked_ingresos
            """,
            [order_ids, group_id, order_ids, group_id, order_ids, group_id],
        )
        ingreso_ids = [int(row[0]) for row in cur.fetchall() if row[0] is not None]

    if not ingreso_ids:
        return []

    sets = [
        """
        estado = CASE
          WHEN estado = 'vendido_pendiente_entrega' THEN 'vendido_entregado'
          WHEN estado IN ('entregado', 'vendido_entregado', 'alquilado', 'baja') THEN estado
          ELSE 'entregado'
        END
        """,
        "remito_salida = %s",
        "fecha_entrega = COALESCE(fecha_entrega, CURRENT_TIMESTAMP)",
    ]
    params: list[Any] = [remito]
    if {"alquilado", "alquiler_a", "alquiler_remito", "alquiler_fecha"}.issubset(columns):
        sets.extend(
            [
                "alquilado = CASE WHEN estado = 'vendido_pendiente_entrega' THEN FALSE ELSE alquilado END",
                "alquiler_a = CASE WHEN estado = 'vendido_pendiente_entrega' THEN NULL ELSE alquiler_a END",
                "alquiler_remito = CASE WHEN estado = 'vendido_pendiente_entrega' THEN NULL ELSE alquiler_remito END",
                "alquiler_fecha = CASE WHEN estado = 'vendido_pendiente_entrega' THEN NULL ELSE alquiler_fecha END",
            ]
        )
    params.append(ingreso_ids)

    with connection.cursor() as cur:
        cur.execute(f"UPDATE ingresos SET {', '.join(sets)} WHERE id = ANY(%s)", params)

    _insert_service_release_delivery_events(ingreso_ids, actor_user_id)
    return ingreso_ids


def _assert_rental_orders_ready_for_remito(
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
    company_key: str,
) -> None:
    rental_orders = [order for order in orders if order.get("deliveryType") == "rental"]
    if not rental_orders:
        return
    for order in rental_orders:
        try:
            validate_rental_delivery_items_ready(
                order.get("items") or [],
                order_id=order.get("id"),
                company_key=company_key,
                actor_user_id=actor_user_id,
            )
        except Exception as exc:
            if isinstance(exc, DeliveryOrderError):
                raise
            raise _sdk_error(exc) from exc


def _dash_location_id() -> int | None:
    if not _table_exists("locations"):
        return None
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id
              FROM locations
             WHERE TRIM(COALESCE(nombre, '')) = '-'
             ORDER BY id ASC
             LIMIT 1
            """
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _insert_rental_delivery_event(ingreso_id: int, actor_user_id: int | None) -> None:
    if not _table_exists("ingreso_events"):
        return
    columns = _table_columns("ingreso_events", {"ticket_id", "a_estado", "usuario_id", "comentario"})
    if not {"ticket_id", "a_estado", "usuario_id", "comentario"}.issubset(columns):
        return
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, comentario)
                VALUES (%s, 'alquilado', %s, 'Alquiler registrado por RTA Bejerman')
                """,
                [ingreso_id, actor_user_id],
            )
    except Exception:
        pass


def _sync_rental_ingresos_after_remito(
    order_ids: list[str],
    group_id: str,
    actor_user_id: int | None,
    remito_number: str,
    issue_date: str | None,
) -> list[int]:
    remito = _optional_text(remito_number)
    if not remito or not order_ids:
        return []
    columns = _table_columns(
        "ingresos",
        {"id", "estado", "alquilado", "alquiler_a", "alquiler_remito", "alquiler_fecha", "ubicacion_id"},
    )
    if not {"id", "estado", "alquilado", "alquiler_a", "alquiler_remito", "alquiler_fecha"}.issubset(columns):
        return []
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ingreso_id, customer_name
              FROM (
                    SELECT doi.ingreso_id,
                           COALESCE(NULLIF(o.customer_name, ''), '-') AS customer_name
                      FROM delivery_order_items doi
                      JOIN delivery_orders o ON o.id = doi.order_id
                     WHERE o.id = ANY(%s)
                       AND o.bejerman_remito_group_id = %s
                       AND o.delivery_type = 'rental'
                       AND doi.ingreso_id IS NOT NULL
                    UNION
                    SELECT doip.ingreso_id,
                           COALESCE(NULLIF(o.customer_name, ''), '-') AS customer_name
                      FROM delivery_order_item_partidas doip
                      JOIN delivery_order_items doi ON doi.id = doip.order_item_id
                      JOIN delivery_orders o ON o.id = doi.order_id
                     WHERE o.id = ANY(%s)
                       AND o.bejerman_remito_group_id = %s
                       AND o.delivery_type = 'rental'
                       AND doip.ingreso_id IS NOT NULL
                   ) rental_ingresos
             ORDER BY ingreso_id
            """,
            [order_ids, group_id, order_ids, group_id],
        )
        rows = [{"ingreso_id": int(row[0]), "customer_name": row[1] or "-"} for row in cur.fetchall()]
    if not rows:
        return []

    dash_id = _dash_location_id() if "ubicacion_id" in columns else None
    synced: list[int] = []
    with connection.cursor() as cur:
        for row in rows:
            sets = [
                "estado = 'alquilado'",
                "alquilado = TRUE",
                "alquiler_a = %s",
                "alquiler_remito = %s",
                "alquiler_fecha = %s",
            ]
            params: list[Any] = [
                row["customer_name"],
                remito,
                issue_date or timezone.localdate().isoformat(),
            ]
            if dash_id is not None:
                sets.append("ubicacion_id = %s")
                params.append(dash_id)
            params.append(row["ingreso_id"])
            cur.execute(f"UPDATE ingresos SET {', '.join(sets)} WHERE id = %s", params)
            if cur.rowcount:
                synced.append(row["ingreso_id"])
    for ingreso_id in synced:
        _insert_rental_delivery_event(ingreso_id, actor_user_id)
    return synced


def _delivery_remito_pdf_email_details(
    orders: list[dict[str, Any]],
    profile: dict[str, Any],
    billing_required: bool,
) -> list[str]:
    order_numbers = [
        _optional_text(order.get("orderNumber")) or _optional_text(order.get("id")) or "-"
        for order in orders
    ]
    delivery_types = sorted({_optional_text(order.get("deliveryType")) or "-" for order in orders})
    lines = [
        f"Órdenes: {', '.join(order_numbers)}",
        f"Tipo de entrega: {', '.join(delivery_types)}",
        f"Punto de venta: {_optional_text(profile.get('pointOfSale')) or '-'}",
        f"Facturable: {'Sí' if billing_required else 'No'}",
    ]
    if len(orders) == 1:
        order = orders[0]
        source = _optional_text(order.get("sourceReference"))
        serial = _optional_text(order.get("equipmentSerial"))
        internal_number = _optional_text(order.get("equipmentInternalNumber"))
        if source:
            lines.append(f"Referencia: {source}")
        if serial:
            lines.append(f"N/S: {serial}")
        if internal_number:
            lines.append(f"Número interno: {internal_number}")
    return lines


def generate_bejerman_remito(order_ids: list[str], actor_user_id: int | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    started_at = time.monotonic()
    ids = [str(item).strip() for item in (order_ids or []) if str(item).strip()]
    if not ids:
        raise DeliveryOrderError("ORDER_IDS_REQUIRED", "Hay que seleccionar órdenes")
    orders = [
        get_delivery_order(
            order_id,
            include_events=False,
            actor_user_id=actor_user_id,
            refresh_article_flags=True,
        )
        for order_id in ids
    ]
    orders = _refresh_service_release_orders_for_remito(orders, actor_user_id)
    orders = [
        refresh_delivery_order_article_partida_flags(order, actor_user_id, strict=True, action="emitir el remito")
        for order in orders
    ]
    compatibility = _validate_remito_orders(orders)
    order_seller_codes = {_optional_text(order.get("sellerCode")) for order in orders}
    order_seller_code = next(iter(order_seller_codes)) if len(order_seller_codes) == 1 else None
    type_default_seller = {
        "service_release": _env_text("BEJERMAN_REMITO_SERVICE_SELLER", "ADM"),
        "rental": _env_text("BEJERMAN_REMITO_RENTAL_SELLER", "ADM"),
        "demo": _env_text("BEJERMAN_REMITO_DEMO_SELLER", "ADM"),
    }.get(compatibility["deliveryType"])
    if compatibility["deliveryType"] == "rental":
        seller_code = "ADM"
    else:
        seller_code = (
            _optional_text((payload or {}).get("sellerCode"))
            or order_seller_code
            or type_default_seller
            or get_user_seller_code(actor_user_id)
            or _env_text("BEJERMAN_REMITO_SELLER", "ADM")
        )
    payment_term_code = (
        _optional_text((payload or {}).get("paymentTermCode"))
        or _env_text("BEJERMAN_REMITO_PAYMENT_TERM", _env_text("BEJERMAN_RIS_PAYMENT_TERM", "30"))
    )
    _assert_rental_orders_ready_for_remito(orders, actor_user_id, compatibility["companyKey"])

    group_id, compatibility = reserve_remito_group(
        orders,
        actor_user_id,
        seller_code=seller_code,
        payment_term_code=payment_term_code,
    )
    bridge_request = {
        "groupId": group_id,
        "companyKey": compatibility["companyKey"],
        "issueDate": (payload or {}).get("issueDate"),
        "customerCode": compatibility["customerCode"],
        "customerName": compatibility["customerName"],
        "priceCurrency": compatibility["priceCurrency"],
        "exchangeRate": compatibility["exchangeRate"],
        "sellerCode": seller_code,
        "paymentTermCode": payment_term_code,
        "notes": _optional_text((payload or {}).get("notes")),
        "orders": [_bridge_order(order) for order in orders],
    }
    if not bridge_request["issueDate"]:
        bridge_request["issueDate"] = timezone.localdate().isoformat()

    try:
        client = BejermanSDKClient(company_key=compatibility["companyKey"], actor_user_id=actor_user_id)
        customer_fields = resolve_customer_document_fields(client, compatibility["customerCode"])
        article_tax_by_code = _delivery_article_sales_vat_map(client, bridge_request, compatibility["profile"])
        built = build_delivery_remito_comprobante(
            bridge_request,
            delivery_remito_config(compatibility["profile"]),
            customer_fields,
            article_tax_by_code,
        )
        bejerman_started_at = time.monotonic()
        raw_response = client.ingresar_comprobante_ventas_json(
            built["comprobante"],
            circuito=_env_text("BEJERMAN_REMITO_CIRCUIT", "VENTAS"),
            operacion=_env_text("BEJERMAN_REMITO_OPERATION", "IngresarComprobanteJSON"),
            numera_flex=_env_text("BEJERMAN_REMITO_NUMERA_FLEX", "S"),
            emite_reg=_env_text("BEJERMAN_REMITO_EMITE_REG", "E"),
        )
        logger.info(
            "bejerman_remito_emit_request_finished",
            extra={
                "group_id": group_id,
                "company_key": compatibility["companyKey"],
                "point_of_sale": compatibility["profile"].get("pointOfSale"),
                "duration_ms": int((time.monotonic() - bejerman_started_at) * 1000),
            },
        )
        response = parse_remito_response(raw_response)
        bridge_data = {
            "remitoNumber": response.get("remitoNumber"),
            "response": response,
            "profile": built["profile"],
            "lineCount": built["lineCount"],
            "stockWarnings": [],
            "raw": raw_response,
        }
    except Exception as exc:
        logger.warning(
            "bejerman_remito_emit_failed",
            extra={"group_id": group_id, "duration_ms": int((time.monotonic() - started_at) * 1000), "error": str(exc)},
        )
        _mark_group_failed(group_id, ids, actor_user_id, exc)
        converted = _sdk_error(exc)
        raise DeliveryOrderError(converted.code, str(converted), status_code=converted.status_code) from exc

    remito_number = _optional_text(bridge_data.get("remitoNumber"))
    response = bridge_data.get("response") or {}
    profile = bridge_data.get("profile") or compatibility["profile"]
    billing_profile = {**profile, "type": response.get("comprobanteTipo") or profile.get("type")}
    billing_required = _remito_requires_billing(billing_profile)
    if not remito_number:
        exc = DeliveryOrderError("BEJERMAN_REMITO_RESPONSE_INCOMPLETE", "Bejerman no devolvió número de remito", status_code=502)
        _mark_group_failed(group_id, ids, actor_user_id, exc)
        raise exc

    response_summary = {
        **response,
        "companyKey": compatibility["companyKey"],
        "profile": profile,
        "billingRequired": billing_required,
        "lineCount": bridge_data.get("lineCount"),
        "stockWarnings": bridge_data.get("stockWarnings") or [],
    }
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                SET status = 'generated',
                    company_key = %s,
                    comprobante_tipo = %s,
                    comprobante_letra = %s,
                    comprobante_pto_venta = %s,
                    comprobante_numero = %s,
                    remito_number = %s,
                    operation_code = %s,
                    deposit_code = %s,
                    response_summary = %s::jsonb,
                    generated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                [
                    compatibility["companyKey"],
                    response.get("comprobanteTipo") or profile.get("type") or compatibility["profile"]["type"],
                    response.get("comprobanteLetra") or "R",
                    response.get("comprobantePtoVenta") or profile.get("pointOfSale"),
                    response.get("comprobanteNumero"),
                    remito_number,
                    profile.get("operation") or compatibility["profile"]["operation"],
                    profile.get("deposit") or compatibility["profile"]["deposit"],
                    _json_param(response_summary),
                    group_id,
                ],
            )
            cur.execute(
                """
                UPDATE delivery_orders
                SET status = 'armado_pendiente_entrega',
                    remito_number = %s,
                    remito_location = COALESCE(remito_location, 'recepcion'),
                    remito_location_updated_by = COALESCE(remito_location_updated_by, %s),
                    remito_location_updated_at = COALESCE(remito_location_updated_at, CURRENT_TIMESTAMP),
                    prepared_by_user_id = COALESCE(prepared_by_user_id, %s),
                    prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP)
                WHERE id = ANY(%s) AND bejerman_remito_group_id = %s
                """,
                [remito_number, actor_user_id, actor_user_id, ids, group_id],
            )
        _sync_service_release_ingresos_after_remito(ids, group_id, actor_user_id, remito_number)
        _sync_rental_ingresos_after_remito(ids, group_id, actor_user_id, remito_number, bridge_request["issueDate"])
        for order_id in ids:
            create_event(
                order_id,
                actor_user_id,
                "bejerman_remito_generated",
                metadata={
                    "groupId": group_id,
                    "remitoNumber": remito_number,
                    "companyKey": compatibility["companyKey"],
                    "sellerCode": seller_code,
                    "paymentTermCode": payment_term_code,
                    "profile": profile,
                    "billingRequired": billing_required,
                    "stockWarnings": bridge_data.get("stockWarnings") or [],
                },
            )
            if billing_required:
                notify_delivery_order_remito_ready(order_id, actor_user_id)
        notify_bejerman_remito_pdf_issued(
            remito_number=remito_number,
            document_type=response.get("comprobanteTipo") or profile.get("type") or compatibility["profile"]["type"],
            company_key=compatibility["companyKey"],
            customer_name=compatibility["customerName"],
            source="Orden de entrega",
            details=_delivery_remito_pdf_email_details(orders, profile, billing_required),
            actor_user_id=actor_user_id,
            pdf_loader=lambda remito_group_id=group_id, actor_id=actor_user_id: get_remito_group_pdf(
                remito_group_id,
                actor_user_id=actor_id,
            ),
        )

    updated = list_delivery_orders({"limit": len(ids)})["items"]
    updated_by_id = {order["id"]: order for order in updated if order["id"] in ids}
    result = {
        "success": True,
        "groupId": group_id,
        "remitoNumber": remito_number,
        "companyKey": compatibility["companyKey"],
        "profile": profile,
        "billingRequired": billing_required,
        "orders": [updated_by_id.get(order_id) or get_delivery_order(order_id, include_events=False) for order_id in ids],
        "stockWarnings": bridge_data.get("stockWarnings") or [],
        "pdfUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/",
        "printUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/print/",
    }
    logger.info(
        "bejerman_remito_emit_generated",
        extra={
            "group_id": group_id,
            "company_key": compatibility["companyKey"],
            "point_of_sale": profile.get("pointOfSale"),
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        },
    )
    return result


def _document_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _company_key_from_remito_summary(summary: Any) -> str | None:
    if not isinstance(summary, dict):
        return None
    company_key = _company_key_for_marker(summary.get("companyKey") or summary.get("company_key"))
    if company_key:
        return company_key
    profile = summary.get("profile")
    if isinstance(profile, dict):
        return _company_key_for_marker(profile.get("companyKey") or profile.get("company_key"))
    return None


def _company_key_for_remito_group(group_id: str, summary: Any = None, company_key: Any = None) -> str:
    explicit_company_key = _company_key_for_marker(company_key)
    if explicit_company_key:
        return explicit_company_key
    company_key = _company_key_from_remito_summary(summary)
    if company_key:
        return company_key
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT company_key, operation_company_label, source_company_id
              FROM delivery_orders
             WHERE bejerman_remito_group_id = %s
            """,
            [group_id],
        )
        keys = {
            _company_key_for_order({"companyKey": row[0], "operationCompanyLabel": row[1], "sourceCompanyId": row[2]})
            for row in cur.fetchall()
        }
    if len(keys) == 1:
        return next(iter(keys))
    return "SEPID"


def get_remito_group_pdf(group_id: str, actor_user_id: int | None = None) -> tuple[bytes, str, str]:
    started_at = time.monotonic()
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                   customer_code, remito_number, status, created_at, generated_at, response_summary, company_key
            FROM bejerman_remito_groups
            WHERE id = %s
            """,
            [group_id],
        )
        cols = [col[0] for col in cur.description]
        row_raw = cur.fetchone()
        row = dict(zip(cols, row_raw)) if row_raw else None
    if not row:
        raise BillingError("REMITO_NOT_FOUND", "Remito Bejerman no encontrado", status_code=404)
    if row.get("status") == "failed":
        summary = row.get("response_summary") if isinstance(row.get("response_summary"), dict) else {}
        raise BillingError("REMITO_FAILED", _optional_text(summary.get("error")) or "No se pudo emitir el remito Bejerman", status_code=409)
    summary = _json_object(row.get("response_summary"))
    if is_registered_remito_summary(summary):
        raise registered_remito_no_pdf_error(
            row.get("remito_number") or summary.get("manualRemitoNumber") or summary.get("manual_remito_number")
        )
    if row.get("status") != "generated" or not row.get("comprobante_numero") or not row.get("comprobante_pto_venta"):
        raise BillingError(
            "BEJERMAN_PDF_PENDING",
            "El remito ya fue solicitado. Bejerman todavía no publicó el PDF.",
            status_code=202,
            retry_after_ms=2500,
        )
    company_key = _company_key_for_remito_group(group_id, row.get("response_summary"), row.get("company_key"))
    doc_id = _document_id(
        {
            "t": row.get("comprobante_tipo"),
            "n": row.get("comprobante_numero"),
            "l": row.get("comprobante_letra") or "R",
            "p": row.get("comprobante_pto_venta"),
            "f": (row.get("generated_at") or row.get("created_at")).date().isoformat()
            if hasattr(row.get("generated_at") or row.get("created_at"), "date")
            else None,
            "c": row.get("customer_code"),
        }
    )
    try:
        bytes_, content_type = get_facturacion_pdf(
            row["customer_code"],
            doc_id,
            interactive=True,
            actor_user_id=actor_user_id,
            company_key=company_key,
        )
    except BillingError as exc:
        if exc.status_code == 202:
            logger.info(
                "bejerman_remito_pdf_pending",
                extra={"group_id": group_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
            )
        raise
    filename = f"remito-{row.get('remito_number') or group_id}.pdf"
    logger.info(
        "bejerman_remito_pdf_ready",
        extra={"group_id": group_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
    )
    return bytes_, content_type, filename
