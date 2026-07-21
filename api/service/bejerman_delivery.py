from __future__ import annotations

import base64
import csv
import hashlib
import json
import logging
import os
import re
import time
from io import StringIO
from datetime import timedelta
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
    comprobante_id_of,
    decode_document_id,
    delivery_remito_config,
    first_value,
    is_bejerman_sdk_race_message,
    is_bejerman_sales_query_schema_message,
    normalize_article_code,
    normalize_date,
    normalize_search,
    parse_remito_response,
    records_from_response,
    REMITO_ITEM_SEPARATOR,
    resolve_customer_document_fields,
    service_release_equipment_line,
    service_release_item_legend,
)
from .bejerman_pdf_requests import (
    BejermanPdfFolderUnavailableError,
    BejermanPdfNotPublishedError,
    fetch_or_enqueue_comprobante_pdf,
    pdf_error_metadata,
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
    service_order_reference,
    service_release_max_items_per_order,
    validate_rental_delivery_items_ready,
)
from .remito_pdf_notifications import notify_bejerman_remito_pdf_issued


logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retry_after_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_ms = retry_after_ms
        self.payload = payload or {}


def registered_remito_no_pdf_error(remito_number: Any = "") -> BillingError:
    return BillingError(REGISTERED_REMITO_NO_PDF_CODE, registered_remito_no_pdf_detail(remito_number), status_code=409)


FACTURACION_COMPROBANTE_TYPES = ("FC", "NC", "ND")
REMITO_OPERATION_TYPES = ("MC", "REP", "ALQ", "DEMO", "BUSO", "FAB")
DELIVERY_ORDER_STOCK_DEPOSIT = "VAL"
DELIVERY_ORDER_FALLBACK_DEPOSITS = ("VAL", "STL")
DELIVERY_MANUAL_REGISTER_POINT_OF_SALE = "00001"
DELIVERY_EXISTING_BEJERMAN_POINTS_OF_SALE = {"00004", "00007"}
REMITO_GROUP_FINAL_STATUSES = {"generated", "reconciled"}
REMITO_GROUP_UNCERTAIN_STATUSES = {"sent_unknown", "failed_review"}
REMITO_GROUP_PDF_REFERENCE_STATUSES = REMITO_GROUP_FINAL_STATUSES | {"failed_review"}
REMITO_REVIEW_REQUIRED_CODE = "BEJERMAN_REMITO_REQUIRES_REVIEW"
REMITO_REVIEW_LOOKUP_UNAVAILABLE_CODE = "BEJERMAN_REMITO_LOOKUP_UNAVAILABLE"
REMITO_REVIEW_LOOKUP_AMBIGUOUS_CODE = "BEJERMAN_REMITO_LOOKUP_AMBIGUOUS"
REMITO_REVIEW_PAYLOAD_UNCHANGED_CODE = "BEJERMAN_REMITO_PAYLOAD_UNCHANGED"
REMITO_REVIEW_STATE_REQUIRES_VERIFICATION = "requires_verification"
REMITO_REVIEW_STATE_READY_FOR_CORRECTION = "ready_for_correction"
REMITO_REVIEW_STATE_READY_TO_REISSUE = "ready_to_reissue"
REMITO_REVIEW_STATE_BLOCKED = "blocked"
REMITO_REVIEW_STATE_SUPERSEDED = "superseded"
REMITO_FAILURE_CONFIRMED_NOT_IMPORTED = "confirmed_not_imported"
REMITO_FAILURE_UNKNOWN = "unknown"
BEJERMAN_REMOTE_RECOVERY_LIMIT = 100
REMITO_QUERY_COMPANY_KEYS = ("SEPID", "MGBIO")
REMITO_ALL_COMPANY_MARKERS = {"ALL", "TODAS", "TODOS", "*"}
BEJERMAN_SQL_DATABASE_DEFAULTS = {
    "SEPID": "SBDASEP",
    "MGBIO": "SBDAMGBI",
}
ARTICLE_SALES_VAT_REQUIRED_PREFIX = "BEJERMAN_ARTICLE_SALES_VAT_REQUIRED:"


def _is_bejerman_sales_query_schema_error(message: str) -> bool:
    return is_bejerman_sales_query_schema_message(message)


def _sdk_error(exc: Exception) -> BillingError | DeliveryOrderError:
    message = str(exc)
    if isinstance(exc, BejermanPdfFolderUnavailableError):
        return BillingError(
            exc.code,
            message,
            status_code=exc.status_code,
            payload=pdf_error_metadata(exc),
        )
    if isinstance(exc, BejermanPdfNotPublishedError):
        return BillingError(
            exc.code,
            message,
            status_code=exc.status_code,
            payload=pdf_error_metadata(exc),
        )
    if isinstance(exc, BejermanPdfPendingError):
        return BillingError(
            "BEJERMAN_PDF_PENDING",
            str(exc),
            status_code=202,
            retry_after_ms=getattr(exc, "retry_after_ms", 2500),
            payload=pdf_error_metadata(exc),
        )
    if isinstance(exc, BejermanSdkConfigError):
        return BillingError("BEJERMAN_SDK_NOT_CONFIGURED", message, status_code=503)
    if isinstance(exc, BejermanSdkUnavailable):
        return BillingError("BEJERMAN_SDK_UNAVAILABLE", message, status_code=503)
    if isinstance(exc, BejermanSdkResponseError):
        if message.startswith(ARTICLE_SALES_VAT_REQUIRED_PREFIX):
            detail = message.split(":", 1)[1].strip() or "No se pudo resolver el IVA de venta de los artículos en Bejerman."
            return BillingError(
                "BEJERMAN_ARTICLE_SALES_VAT_REQUIRED",
                detail,
                status_code=409,
            )
        if "expSinCuotasV1" in message:
            return BillingError(
                "BEJERMAN_BILLING_QUERY_BUSY",
                "Bejerman no pudo completar la consulta de facturación porque dejó bloqueado el objeto interno expSinCuotasV1. Reintente la consulta; si persiste, hay que revisar el servicio Bejerman.",
                status_code=502,
            )
        if is_bejerman_sdk_race_message(message):
            return BillingError(
                "BEJERMAN_SDK_BUSY",
                "Bejerman estaba procesando otra operación al mismo tiempo y la consulta falló "
                "(colisión interna de vistas temporales del SDK). No es un problema de datos de NEXORA; reintente en unos segundos.",
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


def _is_all_remito_company_key(value: Any) -> bool:
    return (_optional_text(value) or "").strip().upper() in REMITO_ALL_COMPANY_MARKERS


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


def _remitos_page_params(params: dict[str, Any]) -> tuple[int, int]:
    try:
        page = int(params.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(params.get("pageSize") or 25)
    except (TypeError, ValueError):
        page_size = 25
    return max(1, page), max(1, min(100, page_size))


def _remitos_pagination(total: int, page: int, page_size: int) -> dict[str, Any]:
    total = max(0, int(total or 0))
    total_pages = max(1, (total + page_size - 1) // page_size)
    safe_page = max(1, min(page, total_pages))
    offset = (safe_page - 1) * page_size
    return {
        "page": safe_page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
        "offset": offset,
        "hasNextPage": safe_page < total_pages,
        "hasPreviousPage": safe_page > 1,
    }


def _plain_filters(raw_filters: dict[str, Any]) -> dict[str, Any]:
    if hasattr(raw_filters, "items"):
        return {str(key): value for key, value in raw_filters.items()}
    return dict(raw_filters or {})


def _remito_item_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _optional_text(item.get("issueDate") or item.get("date")),
        _optional_text(item.get("documentNumber") or item.get("numero")),
        _optional_text(item.get("companyKey")),
    )


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
    if isinstance(exc, (BejermanPdfPendingError, BejermanPdfNotPublishedError)):
        return _sdk_error(exc)
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
        "status IN ('generated','reconciled')",
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
        "status IN ('generated','reconciled')",
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


def _local_generated_remito_items(
    params: dict[str, Any],
    *,
    company_key: str,
    customer_code: str,
    remito_types: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    requested_type = (_optional_text(params.get("remitoType")) or "").upper()
    allowed_types = {(_optional_text(value) or "").upper() for value in (remito_types or REMITO_COMPROBANTE_TYPES)}
    allowed_types = {value for value in allowed_types if value}
    if requested_type:
        if allowed_types and requested_type not in allowed_types:
            return []
        allowed_types = {requested_type}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                   comprobante_numero, remito_number, customer_code, customer_name, operation_code,
                   created_at, generated_at, response_summary
              FROM bejerman_remito_groups
             WHERE status IN ('generated','reconciled')
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
        tipo = (_optional_text(row.get("comprobante_tipo") or summary.get("comprobanteTipo")) or "").upper()
        if allowed_types and tipo not in allowed_types:
            continue
        if date_from and issue_date and issue_date < date_from:
            continue
        if date_to and issue_date and issue_date > date_to:
            continue

        tipo = tipo or "RSS"
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


def _local_generated_rss_remito_items(
    params: dict[str, Any],
    *,
    company_key: str,
    customer_code: str,
) -> list[dict[str, Any]]:
    return _local_generated_remito_items(
        params,
        company_key=company_key,
        customer_code=customer_code,
        remito_types={"RSS"},
    )


def _list_remitos_all_companies(
    customer_code: str | None,
    raw_filters: dict[str, Any],
    *,
    actor_user_id: int | None,
) -> dict[str, Any]:
    base_filters = _plain_filters(raw_filters)
    base_params = _remitos_params(base_filters)
    page, page_size = _remitos_page_params(base_params)
    required_per_company = page * page_size
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    partial_errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    total = 0
    effective_codes: dict[str, str] = {}

    for company in REMITO_QUERY_COMPANY_KEYS:
        company_items: list[dict[str, Any]] = []
        company_total = 0
        company_page = 1
        while len(company_items) < required_per_company:
            filters = {
                **base_filters,
                "companyKey": company,
                "page": company_page,
                "pageSize": min(100, max(page_size, required_per_company - len(company_items))),
            }
            result = list_remitos_from_bejerman(
                customer_code,
                filters,
                actor_user_id=actor_user_id,
                company_key=company,
            )
            company_total = int((result.get("pagination") or {}).get("total") or 0)
            effective_codes[company] = _optional_text(result.get("effectiveCustomerCode") or result.get("bejermanCustomerCode"))
            for item in result.get("items") or []:
                if not isinstance(item, dict):
                    continue
                item = {**item, "companyKey": item.get("companyKey") or company}
                key = (item["companyKey"], _optional_text(item.get("documentId") or item.get("id") or item.get("documentNumber")))
                if key[1]:
                    merged.setdefault(key, item)
                company_items.append(item)
            for error in result.get("partialErrors") or []:
                if isinstance(error, dict):
                    partial_errors.append({**error, "companyKey": company})
            if result.get("warning"):
                warnings.append(f"{company}: {result['warning']}")
            pagination = result.get("pagination") or {}
            if not pagination.get("hasNextPage"):
                break
            if len(company_items) >= company_total:
                break
            company_page += 1
        total += company_total

    items = list(merged.values())
    items.sort(key=_remito_item_sort_key, reverse=True)
    pagination = _remitos_pagination(total, page, page_size)
    offset = pagination["offset"]
    data = {
        "items": items[offset : offset + page_size],
        "pagination": pagination,
        "requestedCustomerCode": _optional_text(customer_code),
        "effectiveCustomerCode": next((code for code in effective_codes.values() if code), ""),
        "bejermanCustomerCode": effective_codes,
        "companyKey": "ALL",
        "companies": list(REMITO_QUERY_COMPANY_KEYS),
        "scope": "customer" if _optional_text(customer_code) else "company",
    }
    if partial_errors:
        data["partialErrors"] = partial_errors
    if warnings:
        data["warning"] = " ".join(warnings)
    return data


def list_remitos_from_bejerman(
    customer_code: str | None,
    filters: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    company_key: str | None = None,
) -> dict[str, Any]:
    code = _optional_text(customer_code)
    raw_filters = filters or {}
    params = _remitos_params(raw_filters)
    company_filter = company_key or raw_filters.get("companyKey") or raw_filters.get("company_key") or raw_filters.get("empresa")
    if _is_all_remito_company_key(company_filter):
        return _list_remitos_all_companies(
            customer_code,
            raw_filters,
            actor_user_id=actor_user_id,
        )
    normalized_company = _normalize_remito_company_key(
        company_filter
    )
    try:
        client = BejermanSDKClient(company_key=normalized_company, actor_user_id=actor_user_id)
        effective_code = _resolve_facturacion_customer_code(client, code) if code else ""
        _default_facturacion_dates(params)
        requested_types = _remito_comprobante_types(params)
        responses = []
        partial_errors = []
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
                partial_errors.append(_remito_partial_error(comprobante_type, exc))
                logger.warning("bejerman_remito_type_query_failed", extra={"remito_type": comprobante_type})
        failed_type_set = {error["remitoType"] for error in partial_errors}
        local_remito_items = (
            _local_generated_remito_items(
                params,
                company_key=normalized_company,
                customer_code=effective_code,
                remito_types=failed_type_set,
            )
            if failed_type_set
            else []
        )
        data = build_remitos_result(responses, effective_code, params, extra_items=local_remito_items)
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
    remito_group_id: str | None = None,
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
        client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
        return fetch_or_enqueue_comprobante_pdf(
            client,
            BejermanPdfReference(
                type=decoded["t"],
                number=decoded["n"],
                letter=decoded["l"],
                point_of_sale=decoded["p"],
                issue_date=decoded["f"],
                customer_code=bejerman_customer_code,
            ),
            actor_user_id=actor_user_id,
            remito_group_id=remito_group_id,
        )
    except BejermanPdfFolderUnavailableError as exc:
        raise _sdk_error(exc) from exc
    except BejermanSdkUnavailable as exc:
        if interactive:
            logger.info(
                "bejerman_remito_pdf_timeout_pending",
                extra={"document_id": doc, "duration_ms": int((time.monotonic() - started_at) * 1000)},
            )
            raise BillingError(
                "BEJERMAN_PDF_PENDING",
                "Bejerman no respondió a tiempo al pedir el PDF. El remito ya está emitido; se volverá a comprobar únicamente la carpeta.",
                status_code=202,
                retry_after_ms=5000,
                payload={"pdf_state": "waiting_folder", "generation_requested": True},
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


def _env_positive_int(name: str, default: int) -> int:
    value = getattr(settings, name, None)
    if value is None:
        value = os.getenv(name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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
            if code:
                codes.add(code)
    return sorted(codes)


def _article_sales_vat_lookup_keys(article_code: Any) -> tuple[str, ...]:
    keys = [
        normalize_article_code(article_code),
        normalize_search(article_code),
        as_string(article_code),
    ]
    return tuple(key for key in keys if key)


def _article_sales_vat_entry(
    article_tax_by_code: dict[str, dict[str, Any]],
    article_code: Any,
) -> dict[str, Any]:
    for key in _article_sales_vat_lookup_keys(article_code):
        entry = article_tax_by_code.get(key)
        if isinstance(entry, dict):
            return entry
    return {}


def _article_sales_vat_resolved(entry: dict[str, Any]) -> bool:
    if not entry:
        return False
    return as_number(entry.get("salesVatRate")) is not None


def _missing_article_sales_vat_codes(
    article_codes: list[str],
    article_tax_by_code: dict[str, dict[str, Any]],
) -> list[str]:
    return [
        code
        for code in article_codes
        if not _article_sales_vat_resolved(_article_sales_vat_entry(article_tax_by_code, code))
    ]


def _delivery_article_sales_vat_map(
    client: BejermanSDKClient,
    bridge_request: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    article_codes = _delivery_article_codes_requiring_vat(bridge_request, profile)
    if not article_codes:
        return {}
    catalog = client.list_articulos([], max(50, len(article_codes)))
    article_tax_by_code = build_article_sales_vat_map(catalog, article_codes)
    missing_codes = _missing_article_sales_vat_codes(article_codes, article_tax_by_code)
    if missing_codes:
        responses = [client.list_articulos(build_article_filters(code, field="code"), 20) for code in missing_codes]
        article_tax_by_code.update(build_article_sales_vat_map(responses, missing_codes))
        missing_codes = _missing_article_sales_vat_codes(article_codes, article_tax_by_code)
    if missing_codes:
        labels = ", ".join(missing_codes)
        raise BejermanSdkResponseError(
            f"{ARTICLE_SALES_VAT_REQUIRED_PREFIX} No se pudo resolver el IVA de venta en Bejerman para: {labels}. "
            "Revise que esos artículos tengan código de IVA de venta configurado antes de emitir el comprobante."
        )
    return article_tax_by_code


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
    group_id = _optional_text(order.get("bejermanRemitoGroupId"))
    if group_id:
        return get_remito_group_pdf(group_id, actor_user_id=actor_user_id)
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
    document_mode = _optional_text(response_summary.get("documentMode")).lower()
    group_status = "reconciled" if document_mode == "existing" else "generated"
    response_summary = {
        **response_summary,
        "orchestrationStatus": group_status,
        "correlationKey": f"NEXORA {group_id}",
    }
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_remito_groups (
              id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
              comprobante_numero, remito_number, customer_code, customer_name,
              seller_code, payment_term_code, operation_code, deposit_code,
              status, order_ids, response_summary, correlation_key, created_by_user_id, generated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s::jsonb, %s::jsonb, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
              company_key = EXCLUDED.company_key,
              comprobante_tipo = EXCLUDED.comprobante_tipo,
              comprobante_letra = EXCLUDED.comprobante_letra,
              comprobante_pto_venta = EXCLUDED.comprobante_pto_venta,
              comprobante_numero = EXCLUDED.comprobante_numero,
              remito_number = EXCLUDED.remito_number,
              customer_code = EXCLUDED.customer_code,
              customer_name = EXCLUDED.customer_name,
              seller_code = EXCLUDED.seller_code,
              payment_term_code = EXCLUDED.payment_term_code,
              operation_code = EXCLUDED.operation_code,
              deposit_code = EXCLUDED.deposit_code,
              status = EXCLUDED.status,
              order_ids = EXCLUDED.order_ids,
              response_summary = COALESCE(bejerman_remito_groups.response_summary, '{}'::jsonb) || EXCLUDED.response_summary,
              correlation_key = EXCLUDED.correlation_key,
              last_error = NULL,
              next_retry_at = NULL,
              generated_at = CURRENT_TIMESTAMP
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
                group_status,
                _json_param([order_id]),
                _json_param(response_summary),
                f"NEXORA {group_id}",
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
    _record_bejerman_integration_attempt(
        group_id,
        [order_id],
        actor_user_id,
        "manual_associate" if document_mode == "existing" else "manual_register",
        status=group_status,
        response_payload=response_summary,
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
    *,
    group_id: str | None = None,
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
        group_id=group_id,
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
    *,
    document: dict[str, Any] | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    document = document or normalize_delivery_order_manual_remito_number(order, remito_number)
    if document["documentMode"] == "register":
        if group_id:
            raise DeliveryOrderError(
                REMITO_REVIEW_REQUIRED_CODE,
                "La orden tiene una emisión Bejerman incierta. No se puede registrar otro movimiento de stock con punto de venta 0001 sin revisar Bejerman.",
                status_code=409,
            )
        return _register_manual_delivery_order_stock_movement(order, actor_user_id, document)
    return _associate_existing_delivery_order_remito(order, actor_user_id, document, group_id=group_id)


def _validate_remito_orders(orders: list[dict[str, Any]], *, allow_existing_group_id: str | None = None) -> dict[str, Any]:
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
        existing_group_id = _optional_text(order.get("bejermanRemitoGroupId"))
        if existing_group_id and existing_group_id != _optional_text(allow_existing_group_id):
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


def _service_release_stock_quantity(row: dict[str, Any]) -> float:
    value = row.get("availableQuantity")
    if value is None:
        value = row.get("realQuantity")
    parsed = as_number(value)
    return parsed if parsed is not None else 0


def _service_release_stock_article_name(row: dict[str, Any]) -> str:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    return as_string(
        first_value(
            raw,
            (
                "Articulo_Descripcion",
                "ArticuloDescripcion",
                "Art_Descripcion",
                "ArtDescripcion",
                "Descripcion",
            ),
        )
    )


def _service_release_stock_rows_for_partida(
    client: BejermanSDKClient,
    partida: str,
    deposit: str,
    cache: dict[tuple[str, str], list[dict[str, Any]] | None],
) -> list[dict[str, Any]]:
    key = (deposit.upper(), partida.casefold())
    if key in cache:
        return cache[key] or []
    if not hasattr(client, "obtener_stock_partida"):
        cache[key] = None
        return []
    try:
        rows = build_stock_rows(client.obtener_stock_partida(partida))
    except Exception:
        logger.warning(
            "service_release_stock_article_lookup_failed",
            extra={"deposit": deposit, "partida": partida},
            exc_info=True,
        )
        cache[key] = None
        return []
    matches = [
        row
        for row in rows
        if as_string(row.get("depositCode")).upper() == deposit.upper()
        and as_string(row.get("partida")).casefold() == partida.casefold()
        and _service_release_stock_quantity(row) > 0
        and as_string(row.get("articleCode"))
    ]
    cache[key] = matches
    return matches


def _align_service_release_items_with_stock_article(
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
    compatibility: dict[str, Any],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    if normalize_delivery_type(compatibility.get("deliveryType")) != "service_release":
        return orders
    deposit = as_string(profile.get("deposit") or "STC").upper()
    try:
        client = BejermanSDKClient(company_key=compatibility["companyKey"], actor_user_id=actor_user_id)
    except Exception:
        logger.warning("service_release_stock_article_client_unavailable", exc_info=True)
        return orders

    cache: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
    changed_order_ids: set[str] = set()
    updates: list[dict[str, Any]] = []
    aligned_orders: list[dict[str, Any]] = []
    checked_at = timezone.now().isoformat()
    for order in orders:
        if normalize_delivery_type(order.get("deliveryType")) != "service_release":
            aligned_orders.append(order)
            continue
        aligned_items: list[dict[str, Any]] = []
        for item in order.get("items") or []:
            aligned_item = {**item}
            partida = _effective_item_partida(order, aligned_item)
            article_code = as_string(aligned_item.get("articleCode"))
            if partida and article_code:
                stock_rows = _service_release_stock_rows_for_partida(client, partida, deposit, cache)
                stock_codes = {
                    normalize_article_code(row.get("articleCode")): as_string(row.get("articleCode"))
                    for row in stock_rows
                    if normalize_article_code(row.get("articleCode"))
                }
                current_key = normalize_article_code(article_code)
                if len(stock_codes) == 1 and current_key not in stock_codes:
                    stock_key, stock_article_code = next(iter(stock_codes.items()))
                    stock_row = next(
                        row for row in stock_rows if normalize_article_code(row.get("articleCode")) == stock_key
                    )
                    stock_article_name = _service_release_stock_article_name(stock_row)
                    previous_article_code = article_code
                    aligned_item["articleCode"] = stock_article_code
                    if stock_article_name:
                        aligned_item["articleName"] = stock_article_name
                    aligned_item["articleRequiresPartida"] = True
                    aligned_item["stockDepositCode"] = as_string(stock_row.get("depositCode")) or deposit
                    aligned_item["stockAvailableQuantity"] = _service_release_stock_quantity(stock_row)
                    aligned_item["stockCheckedAt"] = checked_at
                    changed_order_ids.add(as_string(order.get("id")))
                    updates.append(
                        {
                            "orderId": order.get("id"),
                            "itemId": item.get("id"),
                            "partida": partida,
                            "previousArticleCode": previous_article_code,
                            "stockArticleCode": stock_article_code,
                            "stockArticleName": stock_article_name,
                            "stockDepositCode": aligned_item["stockDepositCode"],
                            "stockAvailableQuantity": aligned_item["stockAvailableQuantity"],
                        }
                    )
            aligned_items.append(aligned_item)
        aligned_orders.append({**order, "items": aligned_items})

    if updates:
        with transaction.atomic():
            with connection.cursor() as cur:
                for update in updates:
                    if not update.get("itemId"):
                        continue
                    cur.execute(
                        """
                        UPDATE delivery_order_items
                           SET article_code = %s,
                               article_name = COALESCE(NULLIF(%s, ''), article_name),
                               article_requires_partida = TRUE,
                               stock_deposit_code = %s,
                               stock_available_quantity = %s,
                               stock_checked_at = %s,
                               updated_at = CURRENT_TIMESTAMP
                         WHERE id = %s
                        """,
                        [
                            update["stockArticleCode"],
                            update["stockArticleName"],
                            update["stockDepositCode"],
                            update["stockAvailableQuantity"],
                            checked_at,
                            update["itemId"],
                        ],
                    )
            for order_id in changed_order_ids:
                create_event(
                    order_id,
                    actor_user_id,
                    "service_release_article_aligned_from_bejerman_stock",
                    metadata={
                        "deposit": deposit,
                        "updates": [update for update in updates if as_string(update.get("orderId")) == order_id],
                    },
                )
    return aligned_orders


def _refresh_service_release_orders_for_remito(orders: list[dict[str, Any]], actor_user_id: int | None) -> list[dict[str, Any]]:
    refreshed_orders: list[dict[str, Any]] = []
    for order in orders:
        if order.get("deliveryType") != "service_release":
            refreshed_orders.append(order)
            continue
        if _optional_text(order.get("sourceSystem") or order.get("source_system")) == "nexora-rss-split":
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


# El remito impreso de Bejerman es de una sola hoja: si el comprobante supera el
# límite de líneas hay que emitir varios remitos. Aplica a todos los remitos de
# salida de órdenes de entrega: liberaciones de servicio (RSS), ventas (RT),
# alquileres (RTA) y demos.
REMITO_PRINT_SPLIT_DELIVERY_TYPES = {"service_release", "sale", "rental", "demo"}


def _remito_print_line_limit(delivery_type: str) -> int:
    if delivery_type == "service_release":
        setting_name, default = "BEJERMAN_RSS_MAX_PRINT_LINES", 22
    else:
        setting_name, default = "BEJERMAN_REMITO_MAX_PRINT_LINES", 21
    try:
        max_lines = int(getattr(settings, setting_name, default))
    except (TypeError, ValueError):
        max_lines = default
    return max(4, min(22, max_lines))


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _print_lines_per_partida(delivery_type: str) -> int:
    if delivery_type != "service_release":
        return 1
    try:
        item_print_lines = int(getattr(settings, "BEJERMAN_RSS_PRINT_LINES_PER_ITEM", 3))
    except (TypeError, ValueError):
        item_print_lines = 3
    return max(1, item_print_lines)


def _order_print_header_line_count(delivery_type: str) -> int:
    # build_delivery_remito_comprobante antepone separador + leyenda de orden
    # para los tipos que no son service_release.
    return 0 if delivery_type == "service_release" else 2


def _item_print_line_count(item: dict[str, Any], lines_per_partida: int) -> int:
    partida_lines = len([partida for partida in (item.get("partidas") or []) if isinstance(partida, dict)]) or 1
    discount = as_number(item.get("discountPercent")) or 0
    return partida_lines * (max(1, lines_per_partida) + (1 if discount > 0 else 0))


def _order_print_line_count(order: dict[str, Any]) -> int:
    delivery_type = normalize_delivery_type(order.get("deliveryType"))
    if delivery_type not in REMITO_PRINT_SPLIT_DELIVERY_TYPES:
        return 0
    lines_per_partida = _print_lines_per_partida(delivery_type)
    return _order_print_header_line_count(delivery_type) + sum(
        _item_print_line_count(item, lines_per_partida)
        for item in (order.get("items") or [])
        if isinstance(item, dict)
    )


def _remito_print_item_chunks(order: dict[str, Any]) -> list[list[dict[str, Any]]]:
    items = [item for item in (order.get("items") or []) if isinstance(item, dict)]
    if not items:
        return []
    delivery_type = normalize_delivery_type(order.get("deliveryType"))
    if delivery_type not in REMITO_PRINT_SPLIT_DELIVERY_TYPES:
        return [items]
    lines_per_partida = _print_lines_per_partida(delivery_type)
    limit = max(1, _remito_print_line_limit(delivery_type) - _order_print_header_line_count(delivery_type))
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_lines = 0
    for item in items:
        item_lines = _item_print_line_count(item, lines_per_partida)
        if current and current_lines + item_lines > limit:
            chunks.append(current)
            current = []
            current_lines = 0
        current.append(item)
        current_lines += item_lines
    if current:
        chunks.append(current)
    return chunks


def _first_chunk_item_context(chunk: list[dict[str, Any]]) -> dict[str, Any]:
    first = chunk[0] if chunk else {}
    ingreso_id = _to_int(first.get("ingresoId") or first.get("ingreso_id"))
    device_id = _to_int(first.get("deviceId") or first.get("device_id"))
    source_reference = service_order_reference(ingreso_id) if ingreso_id else None
    return {
        "ingreso_id": ingreso_id,
        "device_id": device_id,
        "source_external_id": str(ingreso_id) if ingreso_id else None,
        "source_reference": source_reference,
        "equipment_serial": _optional_text(first.get("partida") or first.get("equipmentSerial")),
        "equipment_internal_number": _optional_text(first.get("equipmentInternalNumber")),
    }


def _split_order_number(base: str, index: int) -> str:
    clean_base = _optional_text(base) or "OS"
    suffix = f"P{index + 1}-{uuid4().hex[:6].upper()}"
    return f"{clean_base}-{suffix}"


def _materialize_remito_order_split(
    order: dict[str, Any],
    chunks: list[list[dict[str, Any]]],
    actor_user_id: int | None,
) -> list[dict[str, Any]]:
    if len(chunks) <= 1:
        return [order]
    order_id = _optional_text(order.get("id"))
    if not order_id:
        return [order]
    item_ids = [
        _optional_text(item.get("id"))
        for chunk in chunks
        for item in chunk
        if _optional_text(item.get("id"))
    ]
    if not item_ids:
        return [order]

    target_ids = [order_id] + [f"do-{uuid4()}" for _ in chunks[1:]]
    base_order_number = _optional_text(order.get("orderNumber")) or order_id
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, remito_number, bejerman_remito_group_id
                  FROM delivery_orders
                 WHERE id = %s
                 FOR UPDATE
                """,
                [order_id],
            )
            locked = cur.fetchone()
            if not locked or _optional_text(locked[1]) or _optional_text(locked[2]):
                return [get_delivery_order(order_id, include_events=False)]
            cur.execute(
                """
                SELECT id
                  FROM delivery_order_items
                 WHERE order_id = %s
                   AND id = ANY(%s)
                """,
                [order_id, item_ids],
            )
            existing_item_ids = {_optional_text(row[0]) for row in cur.fetchall()}
            if set(item_ids) != existing_item_ids:
                return [get_delivery_order(order_id, include_events=False)]

            first_context = _first_chunk_item_context(chunks[0])
            cur.execute(
                """
                UPDATE delivery_orders
                   SET source_external_id = COALESCE(%s, source_external_id),
                       source_reference = COALESCE(%s, source_reference),
                       ingreso_id = COALESCE(%s, ingreso_id),
                       device_id = COALESCE(%s, device_id),
                       equipment_serial = COALESCE(%s, equipment_serial),
                       equipment_internal_number = COALESCE(%s, equipment_internal_number),
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [
                    first_context["source_external_id"],
                    first_context["source_reference"],
                    first_context["ingreso_id"],
                    first_context["device_id"],
                    first_context["equipment_serial"],
                    first_context["equipment_internal_number"],
                    order_id,
                ],
            )
            for index, chunk in enumerate(chunks[1:], start=1):
                context = _first_chunk_item_context(chunk)
                new_order_id = target_ids[index]
                split_source_external_id = f"{order_id}:rss-split:{index}:{context['source_external_id'] or new_order_id}"
                cur.execute(
                    """
                    INSERT INTO delivery_orders (
                      id, order_number, customer_id, bejerman_customer_code, customer_name,
                      delivery_type, company_key, source_system, source_external_id, source_reference, source_company_id,
                      source_sheet, source_row, source_color,
                      ingreso_id, device_id, equipment_model, equipment_serial, equipment_internal_number,
                      seller_name, seller_code, order_date, operation_company_label, raw_pedido,
                      commercial_terms, commercial_price, commercial_exchange_rate, price_currency, commercial_condition, commercial_deadline,
                      status, priority, remito_number, bejerman_remito_group_id, remito_location,
                      remito_location_updated_by, remito_location_updated_at, invoice_number, imported_delivered_flag,
                      created_by_user_id, prepared_by_user_id, delivered_by_user_id, invoiced_by_user_id, cancelled_by_user_id,
                      source_created_by_user_id, source_prepared_by_user_id, source_delivered_by_user_id,
                      source_invoiced_by_user_id, source_cancelled_by_user_id,
                      created_at, updated_at, prepared_at, delivered_at, invoiced_at, cancelled_at
                    )
                    SELECT
                      %s, %s, customer_id, bejerman_customer_code, customer_name,
                      delivery_type, company_key, %s, %s, %s, source_company_id,
                      source_sheet, source_row, source_color,
                      %s, %s, equipment_model, %s, %s,
                      seller_name, seller_code, order_date, operation_company_label, raw_pedido,
                      commercial_terms, commercial_price, commercial_exchange_rate, price_currency, commercial_condition, commercial_deadline,
                      status, priority, NULL, NULL, NULL,
                      NULL, NULL, invoice_number, imported_delivered_flag,
                      created_by_user_id, prepared_by_user_id, delivered_by_user_id, invoiced_by_user_id, cancelled_by_user_id,
                      source_created_by_user_id, source_prepared_by_user_id, source_delivered_by_user_id,
                      source_invoiced_by_user_id, source_cancelled_by_user_id,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, prepared_at, delivered_at, invoiced_at, cancelled_at
                    FROM delivery_orders
                    WHERE id = %s
                    """,
                    [
                        new_order_id,
                        _split_order_number(base_order_number, index),
                        "nexora-rss-split",
                        split_source_external_id,
                        context["source_reference"],
                        context["ingreso_id"],
                        context["device_id"],
                        context["equipment_serial"],
                        context["equipment_internal_number"],
                        order_id,
                    ],
                )
            for chunk_index, chunk in enumerate(chunks):
                target_order_id = target_ids[chunk_index]
                for sort_order, item in enumerate(chunk):
                    item_id = _optional_text(item.get("id"))
                    if not item_id:
                        continue
                    cur.execute(
                        """
                        UPDATE delivery_order_items
                           SET order_id = %s,
                               sort_order = %s
                         WHERE id = %s
                        """,
                        [target_order_id, sort_order, item_id],
                    )

    delivery_type = normalize_delivery_type(order.get("deliveryType"))
    event_name = (
        "service_release_order_split_for_rss_print"
        if delivery_type == "service_release"
        else "delivery_order_split_for_remito_print"
    )
    for target_order_id in target_ids:
        create_event(
            target_order_id,
            actor_user_id,
            event_name,
            metadata={
                "sourceOrderId": order_id,
                "maxPrintLines": _remito_print_line_limit(delivery_type),
                "splitOrderIds": target_ids,
            },
        )
    return [get_delivery_order(target_id, include_events=False) for target_id in target_ids]


def _split_remito_orders_for_printing(
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
) -> list[dict[str, Any]]:
    split_orders: list[dict[str, Any]] = []
    changed = False
    for order in orders:
        if normalize_delivery_type(order.get("deliveryType")) not in REMITO_PRINT_SPLIT_DELIVERY_TYPES:
            split_orders.append(order)
            continue
        if _optional_text(order.get("bejermanRemitoGroupId")):
            split_orders.append(order)
            continue
        chunks = _remito_print_item_chunks(order)
        if len(chunks) <= 1:
            split_orders.append(order)
            continue
        split_orders.extend(_materialize_remito_order_split(order, chunks, actor_user_id))
        changed = True
    return split_orders if changed else orders


def _partition_remito_orders_for_printing(orders: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    delivery_types = {normalize_delivery_type(order.get("deliveryType")) for order in orders}
    if not orders or len(delivery_types) != 1 or next(iter(delivery_types)) not in REMITO_PRINT_SPLIT_DELIVERY_TYPES:
        return [orders]
    limit = _remito_print_line_limit(next(iter(delivery_types)))
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_lines = 0
    for order in orders:
        order_lines = max(1, _order_print_line_count(order))
        if current and current_lines + order_lines > limit:
            batches.append(current)
            current = []
            current_lines = 0
        current.append(order)
        current_lines += order_lines
    if current:
        batches.append(current)
    return batches or [orders]


def _remito_orders_need_print_split(orders: list[dict[str, Any]]) -> bool:
    delivery_types = {normalize_delivery_type(order.get("deliveryType")) for order in orders}
    if not orders or len(delivery_types) != 1 or next(iter(delivery_types)) not in REMITO_PRINT_SPLIT_DELIVERY_TYPES:
        return False
    return any(len(_remito_print_item_chunks(order)) > 1 for order in orders) or len(_partition_remito_orders_for_printing(orders)) > 1


def _supersede_uncertain_remito_group_for_print_split(
    group: dict[str, Any],
    order_ids: list[str],
    actor_user_id: int | None,
    exc: Exception,
) -> None:
    group_id = _optional_text(group.get("id"))
    if not group_id or not order_ids:
        return
    summary_patch = {
        "orchestrationStatus": "failed_review",
        "reviewRequired": True,
        "supersededForPrintSplit": True,
        "printSplitRestartedAt": timezone.now().isoformat(),
        "error": str(exc),
    }
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_orders
                   SET bejerman_remito_group_id = NULL
                 WHERE id = ANY(%s)
                   AND bejerman_remito_group_id = %s
                   AND NULLIF(BTRIM(COALESCE(remito_number, '')), '') IS NULL
                """,
                [order_ids, group_id],
            )
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb,
                       last_error = COALESCE(last_error, %s)
                 WHERE id = %s
                """,
                [_json_param(summary_patch), str(exc), group_id],
            )
        _record_bejerman_integration_attempt(
            group_id,
            order_ids,
            actor_user_id,
            "print_split_restart",
            status="previous_group_superseded",
            response_payload=summary_patch,
            error_message=str(exc),
        )
        for order_id in order_ids:
            create_event(
                order_id,
                actor_user_id,
                "bejerman_remito_print_split_restart",
                metadata={"groupId": group_id, "error": str(exc)},
            )


def _remito_group_confirms_not_imported(group: dict[str, Any]) -> bool:
    summary = _json_object(group.get("response_summary"))
    if _optional_text(summary.get("failureCertainty")) == REMITO_FAILURE_CONFIRMED_NOT_IMPORTED:
        return True
    detail = " ".join(
        _optional_text(value)
        for value in (group.get("last_error"), summary.get("error"), summary.get("detail"), summary.get("message"))
        if _optional_text(value)
    ).casefold()
    return any(
        marker in detail
        for marker in (
            "no se importó",
            "no se importo",
            "no fue importado",
            "no ha sido importado",
        )
    )


def _remito_failure_certainty(exc: Any) -> str:
    detail = _optional_text(str(exc)).casefold()
    if any(
        marker in detail
        for marker in (
            "no se importó",
            "no se importo",
            "no fue importado",
            "no ha sido importado",
        )
    ):
        return REMITO_FAILURE_CONFIRMED_NOT_IMPORTED
    return REMITO_FAILURE_UNKNOWN


def _signature_number(value: Any) -> Any:
    number = as_number(value)
    return number if number is not None else _optional_text(value)


def _comprobante_signature_payload(comprobante: dict[str, Any]) -> dict[str, Any]:
    items = []
    for item in comprobante.get("Comprobante_Items") or []:
        if not isinstance(item, dict) or (_optional_text(item.get("Item_Tipo")) or "").upper() != "A":
            continue
        items.append(
            {
                "type": (_optional_text(item.get("Item_Tipo")) or "").upper(),
                "article": (_optional_text(item.get("Item_CodigoArticulo")) or "").upper(),
                "description": _optional_text(item.get("Item_DescripArticulo")),
                "quantity1": _signature_number(item.get("Item_CantidadUM1")),
                "quantity2": _signature_number(item.get("Item_CantidadUM2")),
                "unitPrice": _signature_number(item.get("Item_PrecioUnitario")),
                "vatRate": _signature_number(item.get("Item_TasaIVAInscrip")),
                "vatAmount": _signature_number(item.get("Item_ImporteIVAInscrip")),
                "lineTotal": _signature_number(item.get("Item_ImporteTotal")),
                "lineAmount": _signature_number(item.get("Item_Importe")),
                "discountRate": _signature_number(item.get("Item_TasaDescPorItem")),
                "discountAmount": _signature_number(item.get("Item_ImporteDescPorLinea")),
                "deposit": (_optional_text(item.get("Item_Deposito")) or "").upper(),
                "partida": _optional_text(item.get("Item_Partida")),
            }
        )
    return {
        "type": (_optional_text(comprobante.get("Comprobante_Tipo")) or "").upper(),
        "letter": (_optional_text(comprobante.get("Comprobante_Letra")) or "").upper(),
        "point": _normalized_digits(comprobante.get("Comprobante_PtoVenta")).zfill(5),
        "customer": (_optional_text(comprobante.get("Cliente_Codigo")) or "").upper(),
        "customerVat": (_optional_text(comprobante.get("Cliente_SitIVA")) or "").upper(),
        "seller": (_optional_text(comprobante.get("Vendedor_Codigo")) or "").upper(),
        "paymentTerm": (_optional_text(comprobante.get("Comprobante_CondVenta")) or "").upper(),
        "operation": (_optional_text(comprobante.get("Comprobante_TipoOperacion")) or "").upper(),
        "currency": (_optional_text(comprobante.get("Comprobante_Moneda")) or "").upper(),
        "exchangeType": (_optional_text(comprobante.get("Comprobante_TipoCambio")) or "").upper(),
        "exchangeRate": _signature_number(comprobante.get("Comprobante_CotizacionCambio")),
        "headerTotal": _signature_number(comprobante.get("Comprobante_ImporteTotal")),
        "items": items,
    }


def _comprobante_signature(comprobante: dict[str, Any] | None) -> str:
    if not isinstance(comprobante, dict) or not comprobante:
        return ""
    raw = json.dumps(
        _comprobante_signature_payload(comprobante),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _attempted_comprobante_signature(group: dict[str, Any]) -> str:
    summary = _json_object(group.get("response_summary"))
    saved = _optional_text(summary.get("attemptedPayloadSignature"))
    if saved:
        return saved
    group_id = _optional_text(group.get("id"))
    if not group_id:
        return ""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT request_payload
              FROM bejerman_integration_attempts
             WHERE group_id = %s
               AND phase = 'sending'
             ORDER BY id DESC
             LIMIT 1
            """,
            [group_id],
        )
        row = cur.fetchone()
    request_payload = _json_object(row[0]) if row else {}
    return _comprobante_signature(request_payload.get("comprobante"))


def _patch_remito_review_summary(group_id: str, patch: dict[str, Any]) -> None:
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_remito_groups
               SET response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb,
                   next_retry_at = NULL
             WHERE id = %s
            """,
            [_json_param(patch), group_id],
        )


def _prepare_remito_group_for_correction(
    group: dict[str, Any],
    order_ids: list[str],
    actor_user_id: int | None,
) -> dict[str, Any]:
    group_id = _optional_text(group.get("id"))
    if not group_id or not order_ids:
        raise DeliveryOrderError(REMITO_REVIEW_REQUIRED_CODE, "El proceso no conserva órdenes para corregir.", status_code=409)
    attempted_signature = _attempted_comprobante_signature(group)
    summary_patch = {
        "orchestrationStatus": "failed_review",
        "reviewRequired": False,
        "reconciliationRequired": True,
        "failureCertainty": REMITO_FAILURE_CONFIRMED_NOT_IMPORTED,
        "reviewState": REMITO_REVIEW_STATE_READY_FOR_CORRECTION,
        "readyForCorrectionAt": timezone.now().isoformat(),
        "attemptedPayloadSignature": attempted_signature,
        "operatorCorrectionRecorded": False,
    }
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, bejerman_remito_group_id, remito_number
                  FROM delivery_orders
                 WHERE id = ANY(%s)
                 FOR UPDATE
                """,
                [order_ids],
            )
            rows = cur.fetchall()
            if len(rows) != len(order_ids) or any(
                _optional_text(row[1]) != group_id or _optional_text(row[2]) for row in rows
            ):
                raise DeliveryOrderError(
                    REMITO_REVIEW_REQUIRED_CODE,
                    "Las órdenes cambiaron mientras se verificaba el remito. Actualice el proceso antes de continuar.",
                    status_code=409,
                )
            cur.execute(
                """
                UPDATE delivery_orders
                   SET bejerman_remito_group_id = NULL
                 WHERE id = ANY(%s)
                   AND bejerman_remito_group_id = %s
                   AND NULLIF(BTRIM(COALESCE(remito_number, '')), '') IS NULL
                """,
                [order_ids, group_id],
            )
            if cur.rowcount != len(order_ids):
                raise DeliveryOrderError(
                    REMITO_REVIEW_REQUIRED_CODE,
                    "No se pudieron habilitar todas las órdenes para corrección.",
                    status_code=409,
                )
            _patch_remito_review_summary(group_id, summary_patch)
        _record_bejerman_integration_attempt(
            group_id,
            order_ids,
            actor_user_id,
            "review_ready_for_correction",
            status="orders_unlocked",
            response_payload=summary_patch,
            error_message=group.get("last_error"),
        )
        for order_id in order_ids:
            create_event(
                order_id,
                actor_user_id,
                "bejerman_remito_ready_for_correction",
                metadata={"groupId": group_id, "attemptedPayloadSignature": attempted_signature},
            )
    return {
        "success": True,
        "groupId": group_id,
        "reviewState": REMITO_REVIEW_STATE_READY_FOR_CORRECTION,
        "failureCertainty": REMITO_FAILURE_CONFIRMED_NOT_IMPORTED,
        "orderIds": order_ids,
    }


def _mark_review_group_superseded(
    group: dict[str, Any],
    order_ids: list[str],
    actor_user_id: int | None,
    result: dict[str, Any],
    current_signature: str,
) -> None:
    group_id = _optional_text(group.get("id"))
    replacement_group_id = _optional_text(result.get("groupId"))
    patch = {
        "orchestrationStatus": "failed_review",
        "reviewRequired": False,
        "reconciliationRequired": False,
        "reviewState": REMITO_REVIEW_STATE_SUPERSEDED,
        "reissueInProgress": False,
        "supersededAt": timezone.now().isoformat(),
        "replacementGroupId": replacement_group_id,
        "replacementRemitoNumber": _optional_text(result.get("remitoNumber")),
        "replacementPayloadSignature": current_signature,
    }
    with transaction.atomic():
        _patch_remito_review_summary(group_id, patch)
        _record_bejerman_integration_attempt(
            group_id,
            order_ids,
            actor_user_id,
            "review_reissue",
            status="generated",
            response_payload=patch,
            error_message=group.get("last_error"),
        )
        for order_id in order_ids:
            create_event(
                order_id,
                actor_user_id,
                "bejerman_remito_reissued_after_review",
                metadata={"previousGroupId": group_id, "replacementGroupId": replacement_group_id},
            )


def _combine_delivery_remito_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid_results = [result for result in results if result]
    if len(valid_results) == 1:
        return valid_results[0]
    remito_numbers = [
        remito
        for remito in (_optional_text(result.get("remitoNumber")) for result in valid_results)
        if remito
    ]
    orders: list[dict[str, Any]] = []
    stock_warnings: list[Any] = []
    for result in valid_results:
        orders.extend(result.get("orders") or [])
        stock_warnings.extend(result.get("stockWarnings") or [])
    first = valid_results[0] if valid_results else {}
    return {
        "success": True,
        "multiple": True,
        "splitRemito": True,
        "remitoNumber": " / ".join(remito_numbers),
        "remitoNumbers": remito_numbers,
        "groupIds": [result.get("groupId") for result in valid_results if result.get("groupId")],
        "remitos": valid_results,
        "companyKey": first.get("companyKey"),
        "profile": first.get("profile"),
        "billingRequired": any(bool(result.get("billingRequired")) for result in valid_results),
        "orders": orders,
        "stockWarnings": stock_warnings,
    }


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
    correlation_key = f"NEXORA {group_id}"
    order_ids = [order["id"] for order in orders]
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_remito_groups (
              id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
              customer_code, customer_name, seller_code, payment_term_code,
              operation_code, deposit_code, status, order_ids, response_summary, correlation_key, created_by_user_id
            )
            VALUES (%s, %s, %s, 'R', %s, %s, %s, %s, %s, %s, %s, 'reserved', %s::jsonb, %s::jsonb, %s, %s)
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
                _json_param(
                    {
                        "companyKey": compatibility["companyKey"],
                        "profile": profile,
                        "orchestrationStatus": "reserved",
                        "correlationKey": correlation_key,
                    }
                ),
                correlation_key,
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
    _record_bejerman_integration_attempt(
        group_id,
        order_ids,
        actor_user_id,
        "reserve",
        status="reserved",
        request_payload={
            "correlationKey": correlation_key,
            "companyKey": compatibility["companyKey"],
            "customerCode": compatibility["customerCode"],
            "customerName": compatibility["customerName"],
            "profile": profile,
            "orderIds": order_ids,
        },
    )
    return group_id, {**compatibility, "profile": profile}


def _mark_group_failed(group_id: str, order_ids: list[str], actor_user_id: int | None, exc: Exception):
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                SET status = 'failed',
                    last_error = %s,
                    response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
                WHERE id = %s
                """,
                [str(exc), _json_param({"error": str(exc), "orchestrationStatus": "failed"}), group_id],
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
        _record_bejerman_integration_attempt(
            group_id,
            order_ids,
            actor_user_id,
            "failed",
            status="local_failure",
            error_message=str(exc),
        )


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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _record_bejerman_integration_attempt(
    group_id: str,
    order_ids: list[str] | None,
    actor_user_id: int | None,
    phase: str,
    *,
    status: str = "recorded",
    attempt_number: int | None = None,
    request_payload: Any = None,
    response_payload: Any = None,
    error_message: Any = None,
    recovery_payload: Any = None,
) -> None:
    if not group_id:
        return
    try:
        if not _table_exists("bejerman_integration_attempts"):
            return
        with connection.cursor() as cur:
            if attempt_number is None:
                cur.execute("SELECT COALESCE(attempt_count, 0) FROM bejerman_remito_groups WHERE id = %s", [group_id])
                row = cur.fetchone()
                attempt_number = int(row[0] or 1) if row else 1
            cur.execute(
                """
                INSERT INTO bejerman_integration_attempts (
                  group_id, order_ids, actor_user_id, phase, status, attempt_number,
                  request_payload, response_payload, error_message, recovery_payload
                )
                VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
                """,
                [
                    group_id,
                    _json_param(order_ids or []),
                    actor_user_id,
                    phase,
                    status,
                    attempt_number or 1,
                    _json_param(request_payload or {}),
                    _json_param(response_payload or {}),
                    _optional_text(error_message),
                    _json_param(recovery_payload or {}),
                ],
            )
    except Exception:
        logger.warning("bejerman_integration_attempt_record_failed", extra={"group_id": group_id, "phase": phase}, exc_info=True)


def _next_retry_at_for_attempt(attempt_count: int | None) -> Any:
    count = max(1, int(attempt_count or 1))
    minutes = min(30, 2 ** min(count, 5))
    return timezone.now() + timedelta(minutes=minutes)


def _load_remito_group_row(group_id: str) -> dict[str, Any] | None:
    if not group_id:
        return None
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, company_key, comprobante_tipo, comprobante_letra, comprobante_pto_venta,
                       comprobante_numero, remito_number, customer_code, customer_name,
                       seller_code, payment_term_code, operation_code, deposit_code,
                       status, order_ids, response_summary, correlation_key,
                       attempt_count, last_attempt_at, next_retry_at, last_error,
                       created_at, generated_at
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                [group_id],
            )
            row = cur.fetchone()
            if not row:
                return None
            columns = [column[0] for column in cur.description]
            return dict(zip(columns, row))
    except Exception:
        logger.warning("bejerman_remito_group_load_failed", extra={"group_id": group_id}, exc_info=True)
        return None


def _same_uncertain_remito_group_for_orders(orders: list[dict[str, Any]]) -> dict[str, Any] | None:
    group_ids = {_optional_text(order.get("bejermanRemitoGroupId")) for order in orders}
    group_ids.discard("")
    if len(group_ids) != 1:
        return None
    group = _load_remito_group_row(next(iter(group_ids)))
    if not group or _optional_text(group.get("status")) not in REMITO_GROUP_UNCERTAIN_STATUSES:
        return None
    return group


def _mark_group_sending(
    group_id: str,
    order_ids: list[str],
    actor_user_id: int | None,
    *,
    bridge_request: dict[str, Any],
    comprobante: dict[str, Any],
) -> int:
    summary_patch = {
        "orchestrationStatus": "sending",
        "correlationKey": f"NEXORA {group_id}",
        "bridgeRequest": bridge_request,
    }
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_remito_groups
               SET status = 'sending',
                   attempt_count = COALESCE(attempt_count, 0) + 1,
                   last_attempt_at = CURRENT_TIMESTAMP,
                   correlation_key = %s,
                   last_error = NULL,
                   next_retry_at = NULL,
                   response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
             WHERE id = %s
             RETURNING attempt_count
            """,
            [summary_patch["correlationKey"], _json_param(summary_patch), group_id],
        )
        row = cur.fetchone()
    attempt_number = int(row[0] or 1) if row else 1
    _record_bejerman_integration_attempt(
        group_id,
        order_ids,
        actor_user_id,
        "sending",
        status="sdk_request",
        attempt_number=attempt_number,
        request_payload={"bridgeRequest": bridge_request, "comprobante": comprobante},
    )
    return attempt_number


def _mark_group_sent_unknown(
    group_id: str,
    order_ids: list[str],
    actor_user_id: int | None,
    exc: Exception,
    *,
    request_payload: Any = None,
) -> None:
    summary_patch = {
        "orchestrationStatus": "sent_unknown",
        "error": str(exc),
        "reviewRequired": False,
        "reconciliationRequired": True,
    }
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_remito_groups
               SET status = 'sent_unknown',
                   last_error = %s,
                   next_retry_at = %s,
                   response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
             WHERE id = %s
            """,
            [str(exc), _next_retry_at_for_attempt(1), _json_param(summary_patch), group_id],
        )
    _record_bejerman_integration_attempt(
        group_id,
        order_ids,
        actor_user_id,
        "sdk_error",
        status="sent_unknown",
        request_payload=request_payload or {},
        error_message=str(exc),
    )


def _mark_group_review_required(
    group_id: str,
    order_ids: list[str],
    actor_user_id: int | None,
    exc: Exception,
    *,
    request_payload: Any = None,
    recovery_payload: Any = None,
    reason: str = "bejerman_no_confirmo_el_remito",
) -> None:
    request_object = _json_object(request_payload)
    attempted_signature = _comprobante_signature(request_object.get("comprobante"))
    failure_certainty = _remito_failure_certainty(exc)
    with transaction.atomic():
        attempt_count = 1
        with connection.cursor() as cur:
            cur.execute("SELECT COALESCE(attempt_count, 0) FROM bejerman_remito_groups WHERE id = %s", [group_id])
            row = cur.fetchone()
            attempt_count = int(row[0] or 1) if row else 1
            summary_patch = {
                "orchestrationStatus": "failed_review",
                "reviewRequired": True,
                "reconciliationRequired": True,
                "reviewReason": reason,
                "error": str(exc),
                "failureCertainty": failure_certainty,
                "reviewState": REMITO_REVIEW_STATE_REQUIRES_VERIFICATION,
                "attemptedPayloadSignature": attempted_signature,
                "operatorCorrectionRecorded": False,
            }
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET status = 'failed_review',
                       last_error = %s,
                       next_retry_at = %s,
                       response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
                 WHERE id = %s
                """,
                [str(exc), _next_retry_at_for_attempt(attempt_count), _json_param(summary_patch), group_id],
            )
        _record_bejerman_integration_attempt(
            group_id,
            order_ids,
            actor_user_id,
            "failed_review",
            status="requires_review",
            attempt_number=attempt_count,
            request_payload=request_payload or {},
            response_payload={"reason": reason},
            error_message=str(exc),
            recovery_payload=recovery_payload or {},
        )
        for order_id in order_ids:
            create_event(
                order_id,
                actor_user_id,
                "bejerman_remito_review_required",
                metadata={"groupId": group_id, "error": str(exc), "reason": reason},
            )


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


def _normalized_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _same_point_of_sale(value: Any, expected: Any) -> bool:
    value_digits = _normalized_digits(value)
    expected_digits = _normalized_digits(expected)
    if not value_digits or not expected_digits:
        return False
    return value_digits.zfill(5) == expected_digits.zfill(5)


def _recovery_record_text(record: dict[str, Any]) -> str:
    try:
        raw = json.dumps(record, ensure_ascii=False, default=str)
    except Exception:
        raw = str(record)
    return normalize_search(raw)


def _recovery_expected_article_codes(bridge_request: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for order in bridge_request.get("orders") or []:
        if not isinstance(order, dict):
            continue
        for item in order.get("items") or []:
            if not isinstance(item, dict):
                continue
            code = _optional_text(item.get("articleCode"))
            if code:
                codes.add(normalize_search(code))
    return codes


def _recovery_expected_partidas(bridge_request: dict[str, Any]) -> set[str]:
    partidas: set[str] = set()
    for order in bridge_request.get("orders") or []:
        if not isinstance(order, dict):
            continue
        for item in order.get("items") or []:
            if not isinstance(item, dict):
                continue
            partida = _optional_text(item.get("partida"))
            if partida:
                partidas.add(normalize_search(partida))
            for row in item.get("partidas") or []:
                if not isinstance(row, dict):
                    continue
                row_partida = _optional_text(
                    row.get("partida")
                    or row.get("code")
                    or row.get("numeroPartida")
                    or row.get("serial")
                )
                if row_partida:
                    partidas.add(normalize_search(row_partida))
    return partidas


def _recovery_record_matches_delivery_request(
    record: dict[str, Any],
    bridge_request: dict[str, Any],
    group_id: str,
) -> bool:
    text = _recovery_record_text(record)
    normalized_group_id = normalize_search(group_id)
    if normalized_group_id in text or normalize_search(f"NEXORA {group_id}") in text:
        return True
    for prefix_length in (40, 36, 32, 28, 24):
        if len(normalized_group_id) >= prefix_length and normalized_group_id[:prefix_length] in text:
            return True

    order_numbers = [
        normalize_search(order.get("orderNumber"))
        for order in bridge_request.get("orders") or []
        if isinstance(order, dict) and _optional_text(order.get("orderNumber"))
    ]
    if not order_numbers or not all(order_number in text for order_number in order_numbers):
        return False

    article_codes = _recovery_expected_article_codes(bridge_request)
    if article_codes and not any(code in text for code in article_codes):
        return False

    partidas = _recovery_expected_partidas(bridge_request)
    if partidas and not all(partida in text for partida in partidas):
        return False

    return True


def _sales_recovery_record_detail(client: BejermanSDKClient, header: dict[str, Any]) -> dict[str, Any]:
    detail_id = comprobante_id_of(header)
    if not detail_id:
        return header
    try:
        detail_records = records_from_response(client.detalle_comprobante_ventas(detail_id))
    except Exception:
        logger.warning("bejerman_remito_recovery_detail_lookup_failed", extra={"comprobante_id": detail_id}, exc_info=True)
        return header
    detail = detail_records[0] if detail_records else {}
    return {**header, **detail} if isinstance(detail, dict) else header


def _recovery_candidate_document_key(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        as_string(first_value(record, ("Comprobante_ID", "ID", "IdComprobante"))),
        as_string(first_value(record, ("Comprobante_Tipo", "Tipo"))).upper(),
        as_string(first_value(record, ("Comprobante_Letra", "Letra"))).upper(),
        _normalized_digits(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta"))).zfill(5),
        _normalized_digits(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante"))).zfill(8),
    )


def _collapse_recovery_candidate_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str, str, str]] = []
    for record in candidates:
        if not isinstance(record, dict):
            continue
        key = _recovery_candidate_document_key(record)
        target = grouped.get(key)
        if target is None:
            target = {**record, "Comprobante_Items": []}
            grouped[key] = target
            order.append(key)
        target["Comprobante_Items"].append(record)
        text_parts = [
            target.get("Recovery_Text"),
            record.get("Recovery_Text"),
            record.get("Item_DescripArticulo"),
            record.get("Item_CodigoArticulo"),
            record.get("Item_Partida"),
        ]
        target["Recovery_Text"] = " ".join(_optional_text(part) for part in text_parts if _optional_text(part))
    return [grouped[key] for key in order]


def _sql_literal(value: Any) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _sql_like_literal(value: Any) -> str:
    text = str(value or "").replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return _sql_literal(f"%{text}%")


def _sql_in(values: list[str]) -> str:
    clean_values = [_optional_text(value) for value in values]
    clean_values = [value for value in clean_values if value]
    return ", ".join(_sql_literal(value) for value in clean_values) or "''"


def _bejerman_remote_recovery_enabled() -> bool:
    explicit = (_optional_text(os.getenv("BEJERMAN_SQL_RECOVERY_ENABLED")) or "").lower()
    if explicit in {"0", "false", "no", "n"}:
        return False
    return bool(_optional_text(os.getenv("BEJERMAN_SSH_PASSWORD") or getattr(settings, "BEJERMAN_SSH_PASSWORD", "")))


def _bejerman_sql_database_for_company(company_key: Any = None) -> str:
    company = _company_key_for_marker(company_key) or "SEPID"
    for name in (f"BEJERMAN_SQL_DATABASE_{company}", f"BEJERMAN_{company}_SQL_DATABASE"):
        value = _optional_text(os.getenv(name) or getattr(settings, name, ""))
        if value:
            return value
    default = BEJERMAN_SQL_DATABASE_DEFAULTS.get(company)
    if default:
        return default
    return _optional_text(os.getenv("BEJERMAN_SQL_DATABASE") or getattr(settings, "BEJERMAN_SQL_DATABASE", "")) or "SBDASEP"


def _remote_bejerman_sql_csv(
    sql: str,
    *,
    timeout_seconds: int = 35,
    company_key: Any = None,
    outcome: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not _bejerman_remote_recovery_enabled():
        if outcome is not None:
            outcome.update({"status": "disabled"})
        return []
    try:
        import paramiko  # type: ignore
    except Exception:
        logger.warning("bejerman_remito_remote_recovery_paramiko_missing")
        if outcome is not None:
            outcome.update({"status": "dependency_missing"})
        return []

    host = _optional_text(os.getenv("BEJERMAN_SSH_HOST") or getattr(settings, "BEJERMAN_SSH_HOST", "")) or "45.173.2.155"
    user = _optional_text(os.getenv("BEJERMAN_SSH_USER") or getattr(settings, "BEJERMAN_SSH_USER", "")) or "administrator"
    password = _optional_text(os.getenv("BEJERMAN_SSH_PASSWORD") or getattr(settings, "BEJERMAN_SSH_PASSWORD", ""))
    sql_server = _optional_text(os.getenv("BEJERMAN_SQL_SERVER")) or "10.0.200.155,1433"
    database = _bejerman_sql_database_for_company(company_key)
    if not password:
        if outcome is not None:
            outcome.update({"status": "credentials_missing"})
        return []

    ps_script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$sql = @'
{sql}
'@
$cs = 'Server={sql_server};Database={database};Integrated Security=SSPI;TrustServerCertificate=True;Connection Timeout=15'
$conn = New-Object System.Data.SqlClient.SqlConnection $cs
$conn.Open()
try {{
  $cmd = $conn.CreateCommand()
  $cmd.CommandTimeout = 90
  $cmd.CommandText = $sql
  $adapter = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
  $dt = New-Object System.Data.DataTable
  [void]$adapter.Fill($dt)
  $rows = @()
  foreach ($row in $dt.Rows) {{
    $obj = [ordered]@{{}}
    foreach ($col in $dt.Columns) {{
      $obj[$col.ColumnName] = $row[$col.ColumnName]
    }}
    $rows += [pscustomobject]$obj
  }}
  $csv = $rows | ConvertTo-Csv -NoTypeInformation
  foreach ($line in $csv) {{
    [Console]::Out.WriteLine($line)
  }}
}} finally {{
  $conn.Close()
}}
""".strip()
    remote_path = f"C:/Windows/Temp/nexora_remito_recovery_{uuid4().hex}.ps1"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=timeout_seconds,
            banner_timeout=timeout_seconds,
            auth_timeout=timeout_seconds,
        )
        sftp = ssh.open_sftp()
        try:
            with sftp.file(remote_path, "w") as handle:
                handle.write(ps_script)
        finally:
            sftp.close()
        _, stdout, stderr = ssh.exec_command(
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{remote_path}"',
            timeout=timeout_seconds + 20,
        )
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            logger.warning("bejerman_remito_remote_recovery_sql_failed", extra={"code": code, "error": err[:1000]})
            if outcome is not None:
                outcome.update({"status": "sql_failed", "exitCode": code})
            return []
    except Exception as exc:
        logger.warning("bejerman_remito_remote_recovery_sql_error", exc_info=True)
        if outcome is not None:
            outcome.update({"status": "connection_failed", "detail": str(exc)})
        return []
    finally:
        try:
            ssh.exec_command(
                f'powershell -NoProfile -Command "Remove-Item -LiteralPath \'{remote_path}\' -Force -ErrorAction SilentlyContinue"'
            )
        except Exception:
            pass
        ssh.close()

    rows = [dict(row) for row in csv.DictReader(StringIO(out))] if out.strip() else []
    if outcome is not None:
        outcome.update({"status": "success", "rowCount": len(rows)})
    return rows


def _remote_sales_recovery_records(
    bridge_request: dict[str, Any],
    compatibility: dict[str, Any],
    profile: dict[str, Any],
    group_id: str,
    issue_date: str,
    lookup_outcome: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    customer_code = _optional_text(compatibility.get("customerCode"))
    document_type = _optional_text(profile.get("type") or compatibility["profile"].get("type"))
    operation = _optional_text(profile.get("operation") or compatibility["profile"].get("operation"))
    point = _normalized_digits(profile.get("pointOfSale") or compatibility["profile"].get("pointOfSale")).zfill(5)
    company_key = compatibility.get("companyKey") or profile.get("companyKey")
    if not customer_code or not document_type or not point:
        return []

    order_numbers = [
        _optional_text(order.get("orderNumber"))
        for order in bridge_request.get("orders") or []
        if isinstance(order, dict)
    ]
    article_codes = sorted(_recovery_expected_article_codes(bridge_request))
    partidas = sorted(_recovery_expected_partidas(bridge_request))
    like_terms = [group_id, f"NEXORA {group_id}", *order_numbers, *partidas]
    like_conditions = [
        f"(c.scv_Mens LIKE {_sql_like_literal(term)} OR d.sdv_Desc LIKE {_sql_like_literal(term)})"
        for term in like_terms
        if _optional_text(term)
    ]
    filters = [
        f"c.scv_FEmision >= CONVERT(datetime, {_sql_literal(issue_date)}, 120)",
        f"c.scv_FEmision < DATEADD(day, 1, CONVERT(datetime, {_sql_literal(issue_date)}, 120))",
        f"LTRIM(RTRIM(t.spvtco_Cod)) = {_sql_literal(document_type)}",
        f"LTRIM(RTRIM(t.spv_CodPvt)) = {_sql_literal(point)}",
        f"LTRIM(RTRIM(c.scvcli_Cod)) = {_sql_literal(customer_code)}",
    ]
    if operation:
        filters.append(f"LTRIM(RTRIM(c.scvtop_Cod)) = {_sql_literal(operation)}")
    broad_match = []
    if partidas:
        broad_match.append(f"LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) IN ({_sql_in(partidas)})")
    if article_codes:
        broad_match.append(
            "LTRIM(RTRIM(COALESCE(d.sdvart_CodGen, '') + COALESCE(d.sdvart_CodEle1, '') + "
            f"COALESCE(d.sdvart_CodEle2, '') + COALESCE(d.sdvart_CodEle3, ''))) IN ({_sql_in(article_codes)})"
        )
    broad_match.extend(like_conditions)
    if broad_match:
        filters.append("(" + " OR ".join(broad_match) + ")")

    sql = f"""
SET NOCOUNT ON;

SELECT TOP {BEJERMAN_REMOTE_RECOVERY_LIMIT}
  CONVERT(varchar(20), c.scv_ID) AS Comprobante_ID,
  LTRIM(RTRIM(t.spvtco_Cod)) AS Comprobante_Tipo,
  LTRIM(RTRIM(t.spv_Letra)) AS Comprobante_Letra,
  LTRIM(RTRIM(t.spv_CodPvt)) AS Comprobante_PtoVenta,
  LTRIM(RTRIM(t.spv_Nro)) AS Comprobante_Numero,
  CONVERT(varchar(10), c.scv_FEmision, 23) AS Comprobante_FechaEmision,
  LTRIM(RTRIM(c.scvcli_Cod)) AS Cliente_Codigo,
  LTRIM(RTRIM(c.scvtop_Cod)) AS Comprobante_TipoOperacion,
  LTRIM(RTRIM(COALESCE(c.scv_Mens, ''))) AS Comprobante_Mensaje,
  LTRIM(RTRIM(COALESCE(d.sdvart_CodGen, '') + COALESCE(d.sdvart_CodEle1, '') + COALESCE(d.sdvart_CodEle2, '') + COALESCE(d.sdvart_CodEle3, ''))) AS Item_CodigoArticulo,
  LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) AS Item_Partida,
  LTRIM(RTRIM(COALESCE(d.sdvdep_Cod, ''))) AS Item_Deposito,
  CAST(COALESCE(d.sdv_CantUM1, 0) AS varchar(40)) AS Item_CantidadUM1,
  LTRIM(RTRIM(COALESCE(d.sdv_Desc, ''))) AS Item_DescripArticulo,
  CONCAT(
    LTRIM(RTRIM(COALESCE(c.scv_Mens, ''))), ' ',
    LTRIM(RTRIM(COALESCE(d.sdv_Desc, ''))), ' ',
    LTRIM(RTRIM(COALESCE(d.sdvart_CodGen, '') + COALESCE(d.sdvart_CodEle1, '') + COALESCE(d.sdvart_CodEle2, '') + COALESCE(d.sdvart_CodEle3, ''))), ' ',
    LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, '')))
  ) AS Recovery_Text
FROM dbo.SegCabV c
JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
LEFT JOIN dbo.SegDetV d ON d.sdvscv_ID = c.scv_ID
WHERE {" AND ".join(filters)}
ORDER BY c.scv_FEmision DESC, t.spv_Nro DESC, c.scv_ID DESC, d.sdv_NReng DESC;
""".strip()
    return _remote_bejerman_sql_csv(sql, company_key=company_key, outcome=lookup_outcome)


def _recover_delivery_remito_from_matches(
    matches: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    group_id: str,
    order_ids: list[str],
    actor_user_id: int | None,
    bridge_request: dict[str, Any],
    original_error: Exception,
    profile: dict[str, Any],
    line_count: Any,
    source: str,
) -> dict[str, Any] | None:
    if not matches:
        return None
    if len(matches) > 1:
        _record_bejerman_integration_attempt(
            group_id,
            order_ids,
            actor_user_id,
            "recovery_lookup",
            status="ambiguous",
            request_payload={"bridgeRequest": bridge_request},
            response_payload={"matchCount": len(matches), "matches": [match[1] for match in matches]},
            error_message=str(original_error),
            recovery_payload={"records": [match[0] for match in matches], "source": source},
        )
        logger.warning(
            "bejerman_remito_recovery_ambiguous",
            extra={"group_id": group_id, "match_count": len(matches), "original_error": str(original_error)},
        )
        return None

    record, recovered = matches[0]
    logger.warning(
        "bejerman_remito_recovered_after_emit_error",
        extra={
            "group_id": group_id,
            "remito_number": recovered.get("remitoNumber"),
            "original_error": str(original_error),
            "source": source,
        },
    )
    _record_bejerman_integration_attempt(
        group_id,
        order_ids,
        actor_user_id,
        "recovered",
        status="matched",
        request_payload={"bridgeRequest": bridge_request},
        response_payload=recovered,
        error_message=str(original_error),
        recovery_payload={"record": record, "source": source},
    )
    return {
        "remitoNumber": recovered.get("remitoNumber"),
        "response": {
            **recovered,
            "recoveredAfterError": True,
            "recoverySource": source,
            "duplicateNexoraMatches": len(matches) if len(matches) > 1 else 0,
            "originalEmitError": str(original_error),
        },
        "profile": profile,
        "lineCount": line_count,
        "stockWarnings": [],
        "raw": {"recoveredRecord": record, "recoverySource": source},
        "recoveredAfterError": True,
    }


def _recover_delivery_remito_after_emit_error(
    client: BejermanSDKClient,
    bridge_request: dict[str, Any],
    compatibility: dict[str, Any],
    group_id: str,
    built: dict[str, Any],
    original_error: Exception,
    *,
    actor_user_id: int | None = None,
    order_ids: list[str] | None = None,
    lookup_outcome: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    profile = built.get("profile") or compatibility["profile"]
    issue_date = normalize_date(bridge_request.get("issueDate")) or timezone.localdate().isoformat()
    candidates: list[dict[str, Any]] = []
    source = "bejerman_lookup_after_emit_error"
    try:
        response = client.list_comprobantes_ventas(
            build_sales_filters(
                compatibility["customerCode"],
                issue_date,
                issue_date,
                profile.get("type") or compatibility["profile"]["type"],
                profile.get("operation") or compatibility["profile"]["operation"],
            )
        )
        candidates = [
            record
            for record in records_from_response(response)
            if _same_point_of_sale(
                first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")),
                profile.get("pointOfSale") or compatibility["profile"]["pointOfSale"],
            )
        ]
    except Exception as lookup_exc:
        logger.warning(
            "bejerman_remito_recovery_lookup_failed",
            extra={"group_id": group_id, "error": str(original_error), "lookup_error": str(lookup_exc)},
            exc_info=True,
        )
        remote_lookup_outcome: dict[str, Any] = {}
        candidates = _remote_sales_recovery_records(
            bridge_request,
            compatibility,
            profile,
            group_id,
            issue_date,
            lookup_outcome=remote_lookup_outcome,
        )
        source = "bejerman_sql_recovery_after_sdk_lookup_error"
        if not candidates and remote_lookup_outcome.get("status") != "success":
            if lookup_outcome is not None:
                lookup_outcome.update(
                    {
                        "status": "lookup_failed",
                        "source": source,
                        "detail": str(lookup_exc),
                        "remoteLookupStatus": remote_lookup_outcome.get("status"),
                    }
                )
            _record_bejerman_integration_attempt(
                group_id,
                order_ids or [],
                actor_user_id,
                "recovery_lookup",
                status="lookup_failed",
                request_payload={"bridgeRequest": bridge_request},
                response_payload={
                    "fallback": "sql_remote",
                    "fallbackCandidateCount": 0,
                    "remoteLookupStatus": remote_lookup_outcome.get("status"),
                },
                error_message=f"{original_error} | lookup: {lookup_exc}",
            )
            return None

    candidates.sort(
        key=lambda record: (
            normalize_date(first_value(record, ("Comprobante_FechaEmision", "Comprobante_FEmision", "FechaEmision"))) or "",
            as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante"))),
        ),
        reverse=True,
    )
    if source.startswith("bejerman_sql_"):
        candidates = _collapse_recovery_candidate_rows(candidates)
    _record_bejerman_integration_attempt(
        group_id,
        order_ids or [],
        actor_user_id,
        "recovery_lookup",
        status="candidates",
        request_payload={"bridgeRequest": bridge_request},
        response_payload={"candidateCount": len(candidates), "candidates": candidates},
        error_message=str(original_error),
    )
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for header in candidates:
        record = header if source.startswith("bejerman_sql_") else _sales_recovery_record_detail(client, header)
        if not _recovery_record_matches_delivery_request(record, bridge_request, group_id):
            continue
        recovered = parse_remito_response({"DatosJSON": record})
        if not _optional_text(recovered.get("remitoNumber")):
            continue
        matches.append((record, recovered))

    if lookup_outcome is not None:
        lookup_outcome.update(
            {
                "status": "matched" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "no_match",
                "source": source,
                "candidateCount": len(candidates),
                "matchCount": len(matches),
            }
        )

    recovered_data = _recover_delivery_remito_from_matches(
        matches,
        group_id=group_id,
        order_ids=order_ids or [],
        actor_user_id=actor_user_id,
        bridge_request=bridge_request,
        original_error=original_error,
        profile=profile,
        line_count=built.get("lineCount"),
        source=source,
    )
    if recovered_data:
        return recovered_data
    _record_bejerman_integration_attempt(
        group_id,
        order_ids or [],
        actor_user_id,
        "recovery_lookup",
        status="no_match",
        request_payload={"bridgeRequest": bridge_request},
        response_payload={"candidateCount": len(candidates)},
        error_message=str(original_error),
    )
    return None


def _finalize_delivery_remito_group(
    group_id: str,
    ids: list[str],
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
    compatibility: dict[str, Any],
    bridge_request: dict[str, Any],
    bridge_data: dict[str, Any],
    seller_code: str,
    payment_term_code: str,
) -> dict[str, Any]:
    remito_number = _optional_text(bridge_data.get("remitoNumber"))
    if not remito_number:
        raise DeliveryOrderError("BEJERMAN_REMITO_RESPONSE_INCOMPLETE", "Bejerman no devolvió número de remito", status_code=502)

    response = bridge_data.get("response") or {}
    profile = bridge_data.get("profile") or compatibility["profile"]
    billing_profile = {**profile, "type": response.get("comprobanteTipo") or profile.get("type")}
    billing_required = _remito_requires_billing(billing_profile)
    group_status = "reconciled" if bridge_data.get("recoveredAfterError") else "generated"
    response_summary = {
        **response,
        "companyKey": compatibility["companyKey"],
        "profile": profile,
        "billingRequired": billing_required,
        "lineCount": bridge_data.get("lineCount"),
        "stockWarnings": bridge_data.get("stockWarnings") or [],
        "orchestrationStatus": group_status,
        "correlationKey": f"NEXORA {group_id}",
        "bridgeRequest": bridge_request,
    }
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                SET status = %s,
                    company_key = %s,
                    comprobante_tipo = %s,
                    comprobante_letra = %s,
                    comprobante_pto_venta = %s,
                    comprobante_numero = %s,
                    remito_number = %s,
                    operation_code = %s,
                    deposit_code = %s,
                    correlation_key = %s,
                    last_error = NULL,
                    next_retry_at = NULL,
                    response_summary = %s::jsonb,
                    generated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                [
                    group_status,
                    compatibility["companyKey"],
                    response.get("comprobanteTipo") or profile.get("type") or compatibility["profile"]["type"],
                    response.get("comprobanteLetra") or "R",
                    response.get("comprobantePtoVenta") or profile.get("pointOfSale"),
                    response.get("comprobanteNumero"),
                    remito_number,
                    profile.get("operation") or compatibility["profile"]["operation"],
                    profile.get("deposit") or compatibility["profile"]["deposit"],
                    f"NEXORA {group_id}",
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
        _record_bejerman_integration_attempt(
            group_id,
            ids,
            actor_user_id,
            group_status,
            status="finalized",
            response_payload=response_summary,
        )
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
                    "recoveredAfterError": bool(bridge_data.get("recoveredAfterError")),
                    "orchestrationStatus": group_status,
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
    return {
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


def _reattach_review_orders_for_reconciliation(group_id: str, order_ids: list[str]) -> None:
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, bejerman_remito_group_id, remito_number
                  FROM delivery_orders
                 WHERE id = ANY(%s)
                 FOR UPDATE
                """,
                [order_ids],
            )
            rows = cur.fetchall()
            if len(rows) != len(order_ids) or any(
                (_optional_text(row[1]) not in {"", group_id}) or _optional_text(row[2]) for row in rows
            ):
                raise DeliveryOrderError(
                    REMITO_REVIEW_REQUIRED_CODE,
                    "Las órdenes cambiaron mientras se reconciliaba el remito encontrado.",
                    status_code=409,
                )
            cur.execute(
                """
                UPDATE delivery_orders
                   SET bejerman_remito_group_id = %s
                 WHERE id = ANY(%s)
                   AND NULLIF(BTRIM(COALESCE(remito_number, '')), '') IS NULL
                """,
                [group_id, order_ids],
            )
            if cur.rowcount != len(order_ids):
                raise DeliveryOrderError(
                    REMITO_REVIEW_REQUIRED_CODE,
                    "No se pudieron asociar todas las órdenes al remito encontrado.",
                    status_code=409,
                )


def _reconcile_existing_uncertain_remito_group(
    group: dict[str, Any],
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
    payload: dict[str, Any] | None,
    *,
    force_reviewed_reissue: bool = False,
    review_action: str = "",
) -> dict[str, Any] | None:
    group_id = str(group["id"])
    ids = [order["id"] for order in orders]
    compatibility = _validate_remito_orders(orders, allow_existing_group_id=group_id)
    profile = _profile_for_order(compatibility["deliveryType"], compatibility["companyKey"])
    orders = _align_service_release_items_with_stock_article(orders, actor_user_id, compatibility, profile)
    seller_code = _optional_text(group.get("seller_code")) or _env_text("BEJERMAN_REMITO_SELLER", "ADM")
    payment_term_code = _optional_text(group.get("payment_term_code")) or _env_text(
        "BEJERMAN_REMITO_PAYMENT_TERM",
        _env_text("BEJERMAN_RIS_PAYMENT_TERM", "30"),
    )
    summary = _json_object(group.get("response_summary"))
    saved_bridge_request = summary.get("bridgeRequest") if isinstance(summary.get("bridgeRequest"), dict) else {}
    review_state = _optional_text(summary.get("reviewState"))
    review_lookup = review_action in {"verify", "pre_reissue"}
    bridge_request = {
        "groupId": group_id,
        "companyKey": compatibility["companyKey"],
        "issueDate": (_optional_text(saved_bridge_request.get("issueDate")) if review_lookup else "")
        or _optional_text((payload or {}).get("issueDate"))
        or _optional_text(saved_bridge_request.get("issueDate"))
        or timezone.localdate().isoformat(),
        "customerCode": compatibility["customerCode"],
        "customerName": compatibility["customerName"],
        "priceCurrency": compatibility["priceCurrency"],
        "exchangeRate": compatibility["exchangeRate"],
        "sellerCode": seller_code,
        "paymentTermCode": payment_term_code,
        "notes": _optional_text((payload or {}).get("notes")) or _optional_text(saved_bridge_request.get("notes")),
        "orders": [_bridge_order(order) for order in orders],
    }
    current_attempt_count = int(group.get("attempt_count") or 0)
    if current_attempt_count >= 5 and not force_reviewed_reissue and not review_lookup:
        raise DeliveryOrderError(
            REMITO_REVIEW_REQUIRED_CODE,
            "La emisión ya alcanzó el límite de verificaciones contra Bejerman. Verifique en Bejerman si el remito existe; si no existe, reinicie la emisión desde NEXORA.",
            status_code=409,
        )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_remito_groups
               SET attempt_count = COALESCE(attempt_count, 0) + 1,
                   last_attempt_at = CURRENT_TIMESTAMP,
                   next_retry_at = NULL
             WHERE id = %s
             RETURNING attempt_count
            """,
            [group_id],
        )
        row = cur.fetchone()
    recovery_attempt_number = int(row[0] or (current_attempt_count + 1)) if row else current_attempt_count + 1
    client = BejermanSDKClient(company_key=compatibility["companyKey"], actor_user_id=actor_user_id)
    lookup_error = DeliveryOrderError(
        REMITO_REVIEW_REQUIRED_CODE,
        "La emisión anterior quedó incierta y se reintentó primero la reconciliación con Bejerman.",
        status_code=409,
    )
    _record_bejerman_integration_attempt(
        group_id,
        ids,
        actor_user_id,
        "recovery_retry",
        status="lookup_before_reissue",
        attempt_number=recovery_attempt_number,
        request_payload={"bridgeRequest": bridge_request},
        error_message=group.get("last_error") or summary.get("error") or str(lookup_error),
    )
    lookup_outcome: dict[str, Any] = {}
    recovered_bridge_data = _recover_delivery_remito_after_emit_error(
        client,
        bridge_request,
        {**compatibility, "profile": profile},
        group_id,
        {"profile": profile, "lineCount": summary.get("lineCount")},
        lookup_error,
        actor_user_id=actor_user_id,
        order_ids=ids,
        lookup_outcome=lookup_outcome,
    )
    if recovered_bridge_data:
        if review_lookup and any(not _optional_text(order.get("bejermanRemitoGroupId")) for order in orders):
            _reattach_review_orders_for_reconciliation(group_id, ids)
        return _finalize_delivery_remito_group(
            group_id,
            ids,
            orders,
            actor_user_id,
            {**compatibility, "profile": profile},
            bridge_request,
            recovered_bridge_data,
            seller_code,
            payment_term_code,
        )
    lookup_status = _optional_text(lookup_outcome.get("status"))
    if lookup_status == "lookup_failed":
        patch = {
            "reviewRequired": True,
            "reconciliationRequired": True,
            "failureCertainty": REMITO_FAILURE_UNKNOWN,
            "reviewState": REMITO_REVIEW_STATE_BLOCKED,
            "blockedReason": "No fue posible consultar Bejerman ni confirmar que el comprobante no exista.",
            "diagnosticCode": REMITO_REVIEW_LOOKUP_UNAVAILABLE_CODE,
        }
        _patch_remito_review_summary(group_id, patch)
        raise DeliveryOrderError(
            REMITO_REVIEW_LOOKUP_UNAVAILABLE_CODE,
            patch["blockedReason"],
            status_code=409,
        )
    if lookup_status == "ambiguous":
        patch = {
            "reviewRequired": True,
            "reconciliationRequired": True,
            "failureCertainty": REMITO_FAILURE_UNKNOWN,
            "reviewState": REMITO_REVIEW_STATE_BLOCKED,
            "blockedReason": "Bejerman devolvió más de un candidato y NEXORA no puede decidir cuál adoptar.",
            "diagnosticCode": REMITO_REVIEW_LOOKUP_AMBIGUOUS_CODE,
            "candidateCount": lookup_outcome.get("candidateCount"),
            "matchCount": lookup_outcome.get("matchCount"),
        }
        _patch_remito_review_summary(group_id, patch)
        raise DeliveryOrderError(
            REMITO_REVIEW_LOOKUP_AMBIGUOUS_CODE,
            patch["blockedReason"],
            status_code=409,
        )
    if lookup_status == "no_match" and _remito_group_confirms_not_imported(group):
        if review_action == "verify":
            if review_state in {REMITO_REVIEW_STATE_READY_FOR_CORRECTION, REMITO_REVIEW_STATE_READY_TO_REISSUE}:
                return {
                    "success": True,
                    "groupId": group_id,
                    "reviewState": review_state,
                    "failureCertainty": REMITO_FAILURE_CONFIRMED_NOT_IMPORTED,
                    "orderIds": ids,
                }
            return _prepare_remito_group_for_correction(group, ids, actor_user_id)
        if review_action == "pre_reissue" and review_state in {
            REMITO_REVIEW_STATE_READY_FOR_CORRECTION,
            REMITO_REVIEW_STATE_READY_TO_REISSUE,
        }:
            return {
                "success": True,
                "groupId": group_id,
                "reviewState": "confirmed_not_imported",
                "failureCertainty": REMITO_FAILURE_CONFIRMED_NOT_IMPORTED,
                "orderIds": ids,
            }
    if force_reviewed_reissue:
        blocked_error = DeliveryOrderError(
            REMITO_REVIEW_REQUIRED_CODE,
            "NEXORA no va a reemitir automáticamente una orden incierta. Verifique el comprobante en Bejerman y cargue el remito existente, o escale el caso para revisión manual.",
            status_code=409,
        )
        _record_bejerman_integration_attempt(
            group_id,
            ids,
            actor_user_id,
            "review_reissue_blocked",
            status="manual_review_required",
            attempt_number=recovery_attempt_number,
            request_payload={"bridgeRequest": bridge_request},
            error_message=group.get("last_error") or summary.get("error") or str(blocked_error),
        )
        _mark_group_review_required(
            group_id,
            ids,
            actor_user_id,
            blocked_error,
            request_payload={"bridgeRequest": bridge_request},
            reason="reintento_automatico_bloqueado",
        )
        raise blocked_error
    _mark_group_review_required(
        group_id,
        ids,
        actor_user_id,
        lookup_error,
        request_payload={"bridgeRequest": bridge_request},
        reason="reintento_sin_confirmacion_bejerman",
    )
    raise DeliveryOrderError(
        REMITO_REVIEW_REQUIRED_CODE,
        "La emisión sigue sin confirmación clara en Bejerman. La orden queda en revisión para evitar duplicar stock.",
        status_code=409,
    )


def _orders_for_review_group(group: dict[str, Any], actor_user_id: int | None) -> list[dict[str, Any]]:
    raw_order_ids = group.get("order_ids")
    if isinstance(raw_order_ids, str):
        try:
            raw_order_ids = json.loads(raw_order_ids)
        except Exception:
            raw_order_ids = []
    order_ids = [str(value).strip() for value in (raw_order_ids or []) if str(value).strip()]
    if not order_ids:
        raise DeliveryOrderError(
            REMITO_REVIEW_REQUIRED_CODE,
            "El proceso no conserva las órdenes necesarias para la revisión.",
            status_code=409,
        )
    return [
        get_delivery_order(
            order_id,
            include_events=False,
            actor_user_id=actor_user_id,
            refresh_article_flags=True,
        )
        for order_id in order_ids
    ]


def _current_review_comprobante(
    group: dict[str, Any],
    orders: list[dict[str, Any]],
    actor_user_id: int | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    group_id = _optional_text(group.get("id"))
    compatibility = _validate_remito_orders(orders, allow_existing_group_id=group_id)
    profile = _profile_for_order(compatibility["deliveryType"], compatibility["companyKey"])
    orders = _align_service_release_items_with_stock_article(orders, actor_user_id, compatibility, profile)
    summary = _json_object(group.get("response_summary"))
    saved_bridge_request = summary.get("bridgeRequest") if isinstance(summary.get("bridgeRequest"), dict) else {}
    seller_code = _optional_text(group.get("seller_code")) or _env_text("BEJERMAN_REMITO_SELLER", "ADM")
    payment_term_code = _optional_text(group.get("payment_term_code")) or _env_text(
        "BEJERMAN_REMITO_PAYMENT_TERM",
        _env_text("BEJERMAN_RIS_PAYMENT_TERM", "30"),
    )
    bridge_request = {
        "groupId": group_id,
        "companyKey": compatibility["companyKey"],
        "issueDate": _optional_text((payload or {}).get("issueDate")) or timezone.localdate().isoformat(),
        "customerCode": compatibility["customerCode"],
        "customerName": compatibility["customerName"],
        "priceCurrency": compatibility["priceCurrency"],
        "exchangeRate": compatibility["exchangeRate"],
        "sellerCode": seller_code,
        "paymentTermCode": payment_term_code,
        "notes": _optional_text((payload or {}).get("notes")) or _optional_text(saved_bridge_request.get("notes")),
        "orders": [_bridge_order(order) for order in orders],
    }
    client = BejermanSDKClient(company_key=compatibility["companyKey"], actor_user_id=actor_user_id)
    customer_fields = resolve_customer_document_fields(client, compatibility["customerCode"])
    article_tax_by_code = _delivery_article_sales_vat_map(client, bridge_request, profile)
    built = build_delivery_remito_comprobante(
        bridge_request,
        delivery_remito_config(profile),
        customer_fields,
        article_tax_by_code,
    )
    return {
        "signature": _comprobante_signature(built.get("comprobante")),
        "comprobante": built.get("comprobante"),
        "bridgeRequest": bridge_request,
    }


def verify_bejerman_remito_review(group_id: str, actor_user_id: int | None) -> dict[str, Any]:
    group = _load_remito_group_row(_optional_text(group_id))
    if not group or _optional_text(group.get("status")) != "failed_review":
        raise DeliveryOrderError("BEJERMAN_REMITO_REVIEW_NOT_FOUND", "No se encontró un proceso RT pendiente de revisión.", status_code=404)
    summary = _json_object(group.get("response_summary"))
    if _optional_text(summary.get("reviewState")) == REMITO_REVIEW_STATE_SUPERSEDED:
        raise DeliveryOrderError(REMITO_REVIEW_REQUIRED_CODE, "Este proceso ya fue reemplazado por otro remito.", status_code=409)
    orders = _orders_for_review_group(group, actor_user_id)
    result = _reconcile_existing_uncertain_remito_group(
        group,
        orders,
        actor_user_id,
        None,
        review_action="verify",
    )
    return result or {"success": True, "groupId": group_id, "reviewState": REMITO_REVIEW_STATE_BLOCKED}


def _claim_review_reissue(group_id: str) -> None:
    patch = {"reissueInProgress": True, "reissueClaimedAt": timezone.now().isoformat()}
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE bejerman_remito_groups
               SET response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
             WHERE id = %s
               AND status = 'failed_review'
               AND COALESCE(response_summary->>'reviewState', '') IN ('ready_for_correction', 'ready_to_reissue')
               AND (
                    COALESCE(response_summary->>'reissueInProgress', 'false') <> 'true'
                    OR NULLIF(response_summary->>'reissueClaimedAt', '') IS NULL
                    OR (response_summary->>'reissueClaimedAt')::timestamptz
                       < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
               )
            RETURNING id
            """,
            [_json_param(patch), group_id],
        )
        if not cur.fetchone():
            raise DeliveryOrderError(
                "BEJERMAN_REMITO_REISSUE_CONFLICT",
                "El proceso no está listo para reemitir o ya hay otra reemisión en curso.",
                status_code=409,
            )


def _release_review_reissue_claim(group_id: str) -> None:
    _patch_remito_review_summary(group_id, {"reissueInProgress": False})


def reissue_corrected_bejerman_remito_review(
    group_id: str,
    actor_user_id: int | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (payload or {}).get("confirm") is not True:
        raise DeliveryOrderError("BEJERMAN_REMITO_REISSUE_CONFIRMATION_REQUIRED", "Debe confirmar la reemisión corregida.", status_code=400)
    group = _load_remito_group_row(_optional_text(group_id))
    if not group or _optional_text(group.get("status")) != "failed_review":
        raise DeliveryOrderError("BEJERMAN_REMITO_REVIEW_NOT_FOUND", "No se encontró un proceso RT pendiente de revisión.", status_code=404)
    _claim_review_reissue(_optional_text(group_id))
    completed = False
    try:
        group = _load_remito_group_row(_optional_text(group_id)) or group
        orders = _orders_for_review_group(group, actor_user_id)
        reconciled = _reconcile_existing_uncertain_remito_group(
            group,
            orders,
            actor_user_id,
            None,
            review_action="pre_reissue",
        )
        if reconciled and _optional_text(reconciled.get("reviewState")) != "confirmed_not_imported":
            completed = True
            return reconciled
        current = _current_review_comprobante(group, orders, actor_user_id, payload)
        previous_signature = _attempted_comprobante_signature(group)
        if not previous_signature or current["signature"] == previous_signature:
            _patch_remito_review_summary(
                _optional_text(group_id),
                {
                    "reviewState": REMITO_REVIEW_STATE_READY_FOR_CORRECTION,
                    "hasPayloadChanges": False,
                    "blockedReason": "El comprobante corregido no presenta cambios respecto del intento anterior.",
                    "diagnosticCode": REMITO_REVIEW_PAYLOAD_UNCHANGED_CODE,
                },
            )
            raise DeliveryOrderError(
                REMITO_REVIEW_PAYLOAD_UNCHANGED_CODE,
                "El comprobante no cambió. Corrija la orden o el cálculo fiscal antes de reemitir.",
                status_code=409,
            )
        _patch_remito_review_summary(
            _optional_text(group_id),
            {
                "reviewState": REMITO_REVIEW_STATE_READY_TO_REISSUE,
                "hasPayloadChanges": True,
                "currentPayloadSignature": current["signature"],
            },
        )
        next_payload = {
            key: value
            for key, value in (payload or {}).items()
            if key not in {"action", "confirm", "retryConfirmedNotImported", "forceReviewedReissue"}
        }
        result = generate_bejerman_remito([order["id"] for order in orders], actor_user_id, next_payload)
        _mark_review_group_superseded(group, [order["id"] for order in orders], actor_user_id, result, current["signature"])
        completed = True
        return {**result, "previousGroupId": group_id}
    finally:
        if not completed:
            _release_review_reissue_claim(_optional_text(group_id))


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
    existing_uncertain_group = _same_uncertain_remito_group_for_orders(orders)
    if existing_uncertain_group:
        force_print_split_reissue = bool((payload or {}).get("forcePrintSplitReissue"))
        print_split_required = force_print_split_reissue and _remito_orders_need_print_split(orders)
        try:
            reconciled_result = _reconcile_existing_uncertain_remito_group(
                existing_uncertain_group,
                orders,
                actor_user_id,
                payload,
                force_reviewed_reissue=bool((payload or {}).get("forceReviewedReissue")) or print_split_required,
            )
        except DeliveryOrderError as exc:
            if not (print_split_required and exc.code == REMITO_REVIEW_REQUIRED_CODE):
                raise
            _supersede_uncertain_remito_group_for_print_split(existing_uncertain_group, ids, actor_user_id, exc)
            reconciled_result = None
        if reconciled_result:
            return reconciled_result
        orders = [
            get_delivery_order(
                order_id,
                include_events=False,
                actor_user_id=actor_user_id,
                refresh_article_flags=True,
            )
            for order_id in ids
        ]
        orders = [
            refresh_delivery_order_article_partida_flags(order, actor_user_id, strict=True, action="emitir el remito")
            for order in orders
        ]
    if not bool((payload or {}).get("_skipPrintSplit")):
        orders = _split_remito_orders_for_printing(orders, actor_user_id)
        ids = [order["id"] for order in orders]
        batches = _partition_remito_orders_for_printing(orders)
        if len(batches) > 1:
            next_payload = {**(payload or {}), "_skipPrintSplit": True}
            results = [
                generate_bejerman_remito([order["id"] for order in batch], actor_user_id, next_payload)
                for batch in batches
            ]
            return _combine_delivery_remito_results(results)
    compatibility = _validate_remito_orders(orders)
    profile = _profile_for_order(compatibility["deliveryType"], compatibility["companyKey"])
    orders = _align_service_release_items_with_stock_article(orders, actor_user_id, compatibility, profile)
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

    client: BejermanSDKClient | None = None
    built: dict[str, Any] | None = None
    emission_attempted = False
    attempt_number = 1
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
        attempt_number = _mark_group_sending(
            group_id,
            ids,
            actor_user_id,
            bridge_request=bridge_request,
            comprobante=built["comprobante"],
        )
        bejerman_started_at = time.monotonic()
        emission_attempted = True
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
        _record_bejerman_integration_attempt(
            group_id,
            ids,
            actor_user_id,
            "sdk_response",
            status="received",
            attempt_number=attempt_number,
            request_payload={"bridgeRequest": bridge_request},
            response_payload=raw_response,
        )
        bridge_data = {
            "remitoNumber": response.get("remitoNumber"),
            "response": response,
            "profile": built["profile"],
            "lineCount": built["lineCount"],
            "stockWarnings": [],
            "raw": raw_response,
        }
    except Exception as exc:
        if emission_attempted and client is not None and built is not None:
            _mark_group_sent_unknown(
                group_id,
                ids,
                actor_user_id,
                exc,
                request_payload={"bridgeRequest": bridge_request, "comprobante": built.get("comprobante")},
            )
            recovered_bridge_data = _recover_delivery_remito_after_emit_error(
                client,
                bridge_request,
                compatibility,
                group_id,
                built,
                exc,
                actor_user_id=actor_user_id,
                order_ids=ids,
            )
            if recovered_bridge_data:
                bridge_data = recovered_bridge_data
            else:
                logger.warning(
                    "bejerman_remito_emit_failed",
                    extra={"group_id": group_id, "duration_ms": int((time.monotonic() - started_at) * 1000), "error": str(exc)},
                )
                _mark_group_review_required(
                    group_id,
                    ids,
                    actor_user_id,
                    exc,
                    request_payload={"bridgeRequest": bridge_request, "comprobante": built.get("comprobante")},
                    reason="error_post_envio_sin_confirmacion",
                )
                detail = _optional_text(str(exc))
                message = (
                    "Bejerman devolvió un error después del envío. "
                    "NEXORA dejó la orden en revisión y no va a reemitir hasta reconciliar el comprobante."
                )
                if detail:
                    message = f"{message} Detalle Bejerman: {detail}"
                raise DeliveryOrderError(
                    REMITO_REVIEW_REQUIRED_CODE,
                    message,
                    status_code=409,
                ) from exc
        else:
            logger.warning(
                "bejerman_remito_emit_failed",
                extra={"group_id": group_id, "duration_ms": int((time.monotonic() - started_at) * 1000), "error": str(exc)},
            )
            _mark_group_failed(group_id, ids, actor_user_id, exc)
            converted = _sdk_error(exc)
            raise DeliveryOrderError(converted.code, str(converted), status_code=converted.status_code) from exc

    remito_number = _optional_text(bridge_data.get("remitoNumber"))
    if not remito_number and emission_attempted and client is not None and built is not None:
        incomplete_error = DeliveryOrderError(
            "BEJERMAN_REMITO_RESPONSE_INCOMPLETE",
            "Bejerman no devolvió número de remito",
            status_code=502,
        )
        _mark_group_sent_unknown(
            group_id,
            ids,
            actor_user_id,
            incomplete_error,
            request_payload={"bridgeRequest": bridge_request, "comprobante": built.get("comprobante")},
        )
        recovered_bridge_data = _recover_delivery_remito_after_emit_error(
            client,
            bridge_request,
            compatibility,
            group_id,
            built,
            incomplete_error,
            actor_user_id=actor_user_id,
            order_ids=ids,
        )
        if recovered_bridge_data:
            bridge_data = recovered_bridge_data
            remito_number = _optional_text(bridge_data.get("remitoNumber"))

    if not remito_number:
        exc = DeliveryOrderError("BEJERMAN_REMITO_RESPONSE_INCOMPLETE", "Bejerman no devolvió número de remito", status_code=502)
        _mark_group_review_required(
            group_id,
            ids,
            actor_user_id,
            exc,
            request_payload={"bridgeRequest": bridge_request, "comprobante": built.get("comprobante") if built else {}},
            reason="respuesta_sin_numero_sin_confirmacion",
        )
        raise DeliveryOrderError(
            REMITO_REVIEW_REQUIRED_CODE,
            "Bejerman no devolvió número de remito y la reconciliación no encontró una coincidencia clara. La orden queda en revisión.",
            status_code=409,
        ) from exc

    result = _finalize_delivery_remito_group(
        group_id,
        ids,
        orders,
        actor_user_id,
        compatibility,
        bridge_request,
        bridge_data,
        seller_code,
        payment_term_code,
    )
    profile = result["profile"]
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


def _remito_group_fallback_lines(row: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    raw = summary.get("raw") if isinstance(summary.get("raw"), dict) else {}
    raw_items = raw.get("Comprobante_Items") if isinstance(raw.get("Comprobante_Items"), list) else []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if _optional_text(item.get("Item_Tipo")).upper() != "A":
            continue
        code = _optional_text(item.get("Item_CodigoArticulo"))
        detail = _optional_text(item.get("Item_DescripArticulo"))
        quantity = _optional_text(item.get("Item_CantidadUM1"))
        partida = _optional_text(item.get("Item_Partida"))
        deposit = _optional_text(item.get("Item_Deposito"))
        if not code and not detail:
            continue
        lines.append(
            {
                "order": "",
                "code": code,
                "detail": detail,
                "quantity": quantity,
                "partida": partida,
                "deposit": deposit,
            }
        )

    bridge_request = summary.get("bridgeRequest") if isinstance(summary.get("bridgeRequest"), dict) else {}
    for order in bridge_request.get("orders") if isinstance(bridge_request.get("orders"), list) else []:
        if not isinstance(order, dict):
            continue
        order_number = _optional_text(order.get("orderNumber") or order.get("sourceReference"))
        for item in order.get("items") if isinstance(order.get("items"), list) else []:
            if not isinstance(item, dict):
                continue
            partidas = item.get("partidas") if isinstance(item.get("partidas"), list) else []
            item_lines = partidas or [item]
            for partida in item_lines:
                lines.append(
                    {
                        "order": order_number,
                        "code": _optional_text(item.get("articleCode")),
                        "detail": _optional_text(item.get("articleName") or item.get("description")),
                        "quantity": _optional_text(partida.get("assignedQuantity") or item.get("quantity")),
                        "partida": _optional_text(partida.get("partida") or item.get("partida") or item.get("equipmentSerial")),
                        "deposit": _optional_text(partida.get("stockDepositCode") or item.get("stockDepositCode")),
                    }
                )

    if lines:
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        for line in lines:
            key = (
                line.get("order") or "",
                line.get("code") or "",
                line.get("detail") or "",
                line.get("quantity") or "",
                line.get("partida") or "",
                line.get("deposit") or "",
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(line)
        return unique

    raw_order_ids = row.get("order_ids")
    if isinstance(raw_order_ids, str):
        try:
            raw_order_ids = json.loads(raw_order_ids)
        except Exception:
            raw_order_ids = []
    order_ids = [
        _optional_text(order_id)
        for order_id in (raw_order_ids if isinstance(raw_order_ids, list) else [])
        if _optional_text(order_id)
    ]
    for order_id in order_ids:
        try:
            order = get_delivery_order(order_id, include_events=False)
        except Exception:
            continue
        for item in order.get("items") or []:
            if not isinstance(item, dict):
                continue
            lines.append(
                {
                    "order": _optional_text(order.get("orderNumber") or order.get("sourceReference")),
                    "code": _optional_text(item.get("articleCode")),
                    "detail": _optional_text(item.get("articleName") or item.get("description")),
                    "quantity": _optional_text(item.get("quantity")),
                    "partida": _optional_text(item.get("partida") or item.get("equipmentSerial")),
                    "deposit": _optional_text(item.get("stockDepositCode")),
                }
            )
    return lines


def _remito_group_service_release_print_lines(row: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    profile = summary.get("profile") if isinstance(summary.get("profile"), dict) else {}
    document_type = _optional_text(profile.get("type") or row.get("comprobante_tipo")).upper()
    operation = _optional_text(profile.get("operation") or row.get("operation_code")).upper()
    if document_type != "RSS" or operation != "REP":
        return []

    raw_order_ids = row.get("order_ids")
    if isinstance(raw_order_ids, str):
        try:
            raw_order_ids = json.loads(raw_order_ids)
        except Exception:
            raw_order_ids = []
    order_ids = [
        _optional_text(order_id)
        for order_id in (raw_order_ids if isinstance(raw_order_ids, list) else [])
        if _optional_text(order_id)
    ]

    item_by_ingreso: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    ordered_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for order_id in order_ids:
        try:
            order = get_delivery_order(order_id, include_events=False)
        except Exception:
            continue
        if normalize_delivery_type(order.get("deliveryType")) != "service_release":
            continue
        for item in order.get("items") or []:
            if not isinstance(item, dict):
                continue
            pair = (order, item)
            ordered_items.append(pair)
            try:
                ingreso_id = int(item.get("ingresoId") or 0)
            except (TypeError, ValueError):
                ingreso_id = 0
            if ingreso_id > 0:
                item_by_ingreso[ingreso_id] = pair

    raw_os_ids = summary.get("osIds") if isinstance(summary.get("osIds"), list) else []
    ordered_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for raw_os_id in raw_os_ids:
        try:
            ingreso_id = int(raw_os_id)
        except (TypeError, ValueError):
            continue
        pair = item_by_ingreso.get(ingreso_id)
        if pair:
            ordered_pairs.append(pair)
    if not ordered_pairs:
        ordered_pairs = ordered_items

    lines: list[str] = []
    seen: set[int] = set()
    for order, item in ordered_pairs:
        try:
            ingreso_id = int(item.get("ingresoId") or 0)
        except (TypeError, ValueError):
            ingreso_id = 0
        if ingreso_id and ingreso_id in seen:
            continue
        if ingreso_id:
            seen.add(ingreso_id)
        legend = service_release_item_legend(order, item)
        equipment = service_release_equipment_line(
            item.get("articleName") or item.get("description"),
            item.get("sourceText") or item.get("articleCode"),
        )
        lines.extend([legend, equipment, REMITO_ITEM_SEPARATOR])
    return [line for line in lines if _optional_text(line)]


def get_remito_group_pdf(group_id: str, actor_user_id: int | None = None) -> tuple[bytes, str, str]:
    started_at = time.monotonic()
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                   customer_code, remito_number, status, created_at, generated_at, response_summary, company_key,
                   customer_name, operation_code, deposit_code, order_ids
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
    has_document_reference = bool(row.get("comprobante_numero") and row.get("comprobante_pto_venta"))
    if row.get("status") not in REMITO_GROUP_PDF_REFERENCE_STATUSES or not has_document_reference:
        raise BillingError(
            "BEJERMAN_PDF_PENDING",
            "El remito ya fue solicitado. Bejerman todavía no publicó el PDF.",
            status_code=202,
            retry_after_ms=2500,
            payload={"pdf_state": "pending_document", "generation_requested": False},
        )
    company_key = _company_key_for_remito_group(group_id, row.get("response_summary"), row.get("company_key"))
    raw_summary = summary.get("raw") if isinstance(summary.get("raw"), dict) else {}
    doc_id = _document_id(
        {
            "t": row.get("comprobante_tipo"),
            "n": row.get("comprobante_numero"),
            "l": row.get("comprobante_letra") or "R",
            "p": row.get("comprobante_pto_venta"),
            "f": _local_remito_issue_date(row, raw_summary),
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
            remito_group_id=group_id,
        )
    except BillingError as exc:
        if exc.status_code == 202:
            logger.info(
                "bejerman_remito_pdf_pending",
                extra={"group_id": group_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
            )
        else:
            logger.warning(
                "bejerman_remito_pdf_unavailable",
                extra={
                    "group_id": group_id,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "error": str(exc),
                },
            )
        raise
    filename = f"remito-{row.get('remito_number') or group_id}.pdf"
    logger.info(
        "bejerman_remito_pdf_ready",
        extra={"group_id": group_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
    )
    return bytes_, content_type, filename
