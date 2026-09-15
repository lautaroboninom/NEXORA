from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .bejerman_companies import require_company
from .bejerman_particular_customers import ensure_particular_customer_enabled
from .bejerman_documents import parse_bejerman_remito_number
from .bejerman_partida_observacion import observacion_partida
from .bejerman_sdk import (
    BEJERMAN_OCCASIONAL_CUSTOMER_CODE,
    BejermanPdfReference,
    BejermanSDKClient as SalesSDKClient,
    BejermanSdkResponseError,
    as_number,
    as_string,
    bejerman_customer_code_for,
    bejerman_filter,
    build_delivery_remito_comprobante,
    build_occasional_customer_fields,
    delivery_remito_config,
    first_value,
    parse_remito_response,
    probe_comprobante_pdf_copy,
    records_from_response,
    resolve_customer_document_fields,
)
from .bejerman_sync import BejermanSDKClient as StockSDKClient


RENTAL_REMITO_TYPE = "RTA"
RENTAL_REMITO_LETTER = "R"
RENTAL_OPERATION = "ALQ"
RENTAL_STOCK_DEPOSIT = "STL"
RENTAL_ALLOWED_POINTS = {"00001", "00004", "00007"}
SYNC_TYPE_RENTAL_REGISTER_RTA = "rental_remito_rta_register"
SYNC_TYPE_RENTAL_STL_EXIT = "rental_remito_stl_exit"
REMITO_REQUIRED_MESSAGE = (
    "Debe cargar el remito completo, por ejemplo RTA R 00004-00004587 o 00004-00004587."
)


class RentalRemitoError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class RentalRemitoDocument:
    type: str
    letter: str
    point: str
    number: str
    remito_number: str
    company_key: str
    document_mode: str


def _rows(cur) -> list[dict[str, Any]]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _one(cur) -> dict[str, Any] | None:
    columns = [col[0] for col in cur.description]
    row = cur.fetchone()
    return dict(zip(columns, row)) if row else None


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_upper(value: Any) -> str:
    return _clean_text(value).upper()


def _has_table_column(table_name: str, column_name: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name = %s
               AND column_name = %s
               AND table_schema = ANY(current_schemas(true))
             LIMIT 1
            """,
            [table_name, column_name],
        )
        return cur.fetchone() is not None


def _digits(value: Any) -> str:
    return re.sub(r"\D+", "", _clean_text(value))


def _norm(value: Any) -> str:
    return re.sub(r"[\s\-_./]+", "", _clean_upper(value))


def _amount(value: Any) -> Decimal:
    parsed = as_number(value)
    if parsed is None:
        return Decimal("0")
    return Decimal(str(parsed))


def _setting(name: str, default: Any = "") -> Any:
    return getattr(settings, name, default)


def _validate_rental_point(point: str) -> None:
    if point not in RENTAL_ALLOWED_POINTS:
        raise ValueError("El punto de venta debe ser 00001, 00004 o 00007.")


def normalize_rental_remito_number(value: Any, *, fallback_company_key: str = "SEPID") -> RentalRemitoDocument:
    try:
        parsed = parse_bejerman_remito_number(
            value,
            default_type=RENTAL_REMITO_TYPE,
            default_letter=RENTAL_REMITO_LETTER,
            allowed_types=(RENTAL_REMITO_TYPE,),
            require_explicit_match=True,
            validate_point=_validate_rental_point,
            allow_number_only=False,
            require_complete=True,
            complete_error_message=REMITO_REQUIRED_MESSAGE,
            explicit_mismatch_message="El remito de alquiler debe corresponder a RTA R.",
        )
    except ValueError as exc:
        raise RentalRemitoError("RENTAL_REMITO_INVALID", str(exc), status_code=400) from exc
    company_key = company_key_for_rental_point(parsed["point"], fallback_company_key)
    return RentalRemitoDocument(
        type=parsed["type"],
        letter=parsed["letter"],
        point=parsed["point"],
        number=parsed["number"],
        remito_number=parsed["remitoNumber"],
        company_key=company_key,
        document_mode="register" if parsed["point"] == "00001" else "existing",
    )


def company_key_for_rental_point(point: str, fallback_company_key: str = "SEPID") -> str:
    if point == "00004":
        return "SEPID"
    if point == "00007":
        return "MGBIO"
    try:
        return require_company(fallback_company_key or "SEPID").key
    except ValueError:
        return "SEPID"


def _ingreso_context(ingreso_id: int) -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.device_id, t.estado, t.resolucion, t.motivo, t.alquilado,
                   t.alquiler_a, t.alquiler_remito, t.alquiler_fecha,
                   t.empresa_bejerman, t.empresa_facturar,
                   COALESCE(t.propietario_nombre, '') AS propietario_nombre,
                   COALESCE(t.propietario_contacto, '') AS propietario_contacto,
                   COALESCE(t.propietario_doc, '') AS propietario_doc,
                   d.numero_serie, d.numero_interno, d.n_de_control, d.mg_estado,
                   d.marca_id, d.model_id, COALESCE(NULLIF(t.equipo_variante, ''), d.variante, '') AS variante,
                   c.id AS customer_id, c.cod_empresa AS owner_customer_code,
                   c.razon_social AS owner_customer_name,
                   m.nombre AS marca, mo.nombre AS modelo, mo.tipo_equipo
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              LEFT JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas m ON m.id = d.marca_id
              LEFT JOIN models mo ON mo.id = d.model_id
             WHERE t.id = %s
            """,
            [ingreso_id],
        )
        row = _one(cur)
    if not row:
        raise RentalRemitoError("INGRESO_NOT_FOUND", "Ingreso no encontrado.", status_code=404)
    return row


def _company_key_for_context(row: dict[str, Any]) -> str:
    marker = row.get("empresa_bejerman") or row.get("empresa_facturar") or "SEPID"
    try:
        return require_company(marker).key
    except ValueError:
        return "SEPID"


def _resolve_customer_from_text(text: Any) -> dict[str, Any]:
    value = _clean_text(text)
    if not value:
        return {"name": "", "code": "", "matched": False}
    has_alias = _has_table_column("customers", "alias_interno")
    alias_select = "alias_interno" if has_alias else "NULL::text AS alias_interno"
    alias_filter = "OR LOWER(TRIM(COALESCE(alias_interno, ''))) = LOWER(TRIM(%s))" if has_alias else ""
    params = [value, value]
    if has_alias:
        params.append(value)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, cod_empresa, razon_social, {alias_select}
              FROM customers
             WHERE LOWER(TRIM(COALESCE(razon_social, ''))) = LOWER(TRIM(%s))
                {alias_filter}
                OR LOWER(TRIM(COALESCE(cod_empresa, ''))) = LOWER(TRIM(%s))
             ORDER BY id
             LIMIT 1
            """,
            params,
        )
        row = _one(cur)
    if not row:
        return {"name": value, "code": "", "matched": False}
    return {
        "id": row.get("id"),
        # Mismo criterio que el resto del circuito: el placeholder local de particulares no lleva
        # codigo de cliente a Bejerman, se emite como cliente ocasional con los datos del propietario.
        "code": bejerman_customer_code_for(row.get("razon_social"), row.get("cod_empresa")),
        "name": _clean_text(row.get("razon_social")) or value,
        "alias": _clean_text(row.get("alias_interno")),
        "matched": True,
    }


def _mapping_article_for_context(row: dict[str, Any], company_key: str = "SEPID") -> dict[str, Any]:
    model_id = row.get("model_id")
    if not model_id:
        return {}
    try:
        resolved_company = require_company(company_key or "SEPID").key
    except ValueError:
        resolved_company = "SEPID"
    variant = _clean_text(row.get("variante"))
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT article_code, article_description, match_source
              FROM bejerman_article_mappings
             WHERE model_id = %s
               AND COALESCE(variante_norm, '') = LOWER(TRIM(%s))
               AND company_key = %s
             ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END, updated_at DESC, id DESC
             LIMIT 1
            """,
            [model_id, variant, resolved_company],
        )
        exact = _one(cur)
        if exact:
            return exact
        cur.execute(
            """
            SELECT article_code, article_description, match_source
              FROM bejerman_article_mappings
             WHERE model_id = %s
               AND company_key = %s
             ORDER BY CASE WHEN match_source = 'manual' THEN 0 ELSE 1 END, updated_at DESC, id DESC
             LIMIT 1
            """,
            [model_id, resolved_company],
        )
        return _one(cur) or {}


def _record_partida(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Item_Partida", "Partida", "Comprobante_ArtPartida", "ArtPartida", "NroPartida")))


def _record_article_code(record: dict[str, Any]) -> str:
    return as_string(
        first_value(
            record,
            (
                "Item_CodigoArticulo",
                "CodigoArticulo",
                "Articulo_Codigo",
                "Comprobante_Art_CodGen",
                "Art_CodGen",
                "ArtCodGen",
            ),
        )
    )


def _record_article_description(record: dict[str, Any]) -> str:
    return as_string(
        first_value(
            record,
            (
                "Item_DescripArticulo",
                "Item_DescripcionArticulo",
                "Articulo_Descripcion",
                "Comprobante_DescArticulo",
                "Descripcion",
            ),
        )
    )


def _record_deposit(record: dict[str, Any]) -> str:
    return as_string(
        first_value(record, ("Item_Deposito", "Deposito", "Comprobante_ArtDeposito", "ArtDeposito", "Deposito_Codigo"))
    )


def _record_quantity(record: dict[str, Any]) -> Decimal:
    return _amount(
        first_value(
            record,
            ("Item_CantidadUM1", "CantidadUM1", "Comprobante_CantidadUM1", "Cantidad", "Stock_Real", "Real", "Disponible"),
        )
    )


def _doc_header_value(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    return as_string(first_value(record, keys))


def _record_matches_document(record: dict[str, Any], document: RentalRemitoDocument) -> bool:
    tipo = _doc_header_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))
    letter = _doc_header_value(record, ("Comprobante_Letra", "Letra"))
    point = _digits(_doc_header_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta"))).zfill(5)
    number = _digits(_doc_header_value(record, ("Comprobante_Numero", "Numero", "NroComprobante"))).zfill(8)
    operation = _clean_upper(_doc_header_value(record, ("Comprobante_TipoOperacion", "TipoOperacion", "Operacion")))
    return (
        _clean_upper(tipo) == document.type
        and (not letter or _clean_upper(letter) == document.letter)
        and point == document.point
        and number == document.number
        and (not operation or operation == RENTAL_OPERATION)
    )


def _items_from_sales_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = record.get("Comprobante_Items") or record.get("Items") or record.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _serial_matches(value: Any, serial: str) -> bool:
    return bool(serial) and _norm(value) == _norm(serial)


def _article_matches(value: Any, article_code: str) -> bool:
    return bool(article_code) and _clean_upper(value) == _clean_upper(article_code)


def _find_matching_item(record: dict[str, Any], serial: str, article_code: str) -> dict[str, Any] | None:
    if _serial_matches(_record_partida(record), serial) and (
        not article_code or _article_matches(_record_article_code(record), article_code)
    ):
        return record
    for item in _items_from_sales_record(record):
        if not _serial_matches(_record_partida(item), serial):
            continue
        if article_code and not _article_matches(_record_article_code(item), article_code):
            continue
        return item
    return None


def _sales_lookup_filters(document: RentalRemitoDocument) -> list[dict[str, Any]]:
    return [
        bejerman_filter("Comprobante_Tipo", "IGUAL", document.type),
        bejerman_filter("Comprobante_PtoVenta", "IGUAL", document.point),
        bejerman_filter("Comprobante_Numero", "IGUAL", document.number),
    ]


def _lookup_rta_document(
    client: SalesSDKClient,
    document: RentalRemitoDocument,
    *,
    serial: str,
    article_code: str,
) -> dict[str, Any]:
    response = client.list_comprobantes_ventas(_sales_lookup_filters(document))
    records = [record for record in records_from_response(response) if _record_matches_document(record, document)]
    if not records:
        return {"exists": False, "records": [], "raw": response}
    matching = []
    for record in records:
        item = _find_matching_item(record, serial, article_code)
        if item:
            matching.append((record, item))
    first_record_match, first_item_match = matching[0] if matching else (records[0], None)
    customer_code = as_string(first_value(first_record_match, ("Cliente_Codigo", "ClienteCodigo", "CodCliente")))
    customer_name = as_string(
        first_value(first_record_match, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Cliente"))
    )
    issue_date = as_string(first_value(first_record_match, ("Comprobante_FechaEmision", "FechaEmision")))
    return {
        "exists": True,
        "matchesItem": bool(first_item_match),
        "records": records,
        "record": first_record_match,
        "item": first_item_match,
        "raw": response,
        "customerCode": customer_code,
        "customerName": customer_name,
        "issueDate": issue_date[:10] if issue_date else "",
        "articleCode": _record_article_code(first_item_match or first_record_match),
        "articleDescription": _record_article_description(first_item_match or first_record_match),
    }


def _stock_records(response: dict[str, Any], serial: str) -> list[dict[str, Any]]:
    return [record for record in records_from_response(response) if _serial_matches(_record_partida(record), serial)]


def _stock_summary(response: dict[str, Any], serial: str) -> dict[str, Any]:
    records = _stock_records(response, serial)
    rows = []
    stl_qty = Decimal("0")
    stl_available = Decimal("0")
    article_code = ""
    article_description = ""
    for record in records:
        deposit = _record_deposit(record)
        qty = _record_quantity(record)
        available_value = _amount(first_value(record, ("Stock_Disponible", "Disponible", "Disp", "StockDisp")))
        code = _record_article_code(record)
        description = _record_article_description(record)
        if code and not article_code:
            article_code = code
        if description and not article_description:
            article_description = description
        if _clean_upper(deposit) == RENTAL_STOCK_DEPOSIT:
            stl_qty += qty
            stl_available += available_value if available_value != 0 else qty
        rows.append(
            {
                "deposit": deposit,
                "partida": _record_partida(record),
                "articleCode": code,
                "articleDescription": description,
                "quantity": float(qty),
                "available": float(available_value if available_value != 0 else qty),
            }
        )
    return {
        "stlQuantity": float(stl_qty),
        "stlAvailable": float(stl_available),
        "hasStlStock": stl_qty > 0,
        "articleCode": article_code,
        "articleDescription": article_description,
        "records": rows,
        "raw": response,
    }


def _company_label(company_key: str) -> str:
    try:
        return require_company(company_key).label
    except ValueError:
        return company_key


def _base_response(
    row: dict[str, Any],
    document: RentalRemitoDocument,
    *,
    customer: dict[str, Any],
    article_code: str,
    article_description: str,
    stock: dict[str, Any],
    lookup: dict[str, Any],
) -> dict[str, Any]:
    remito_customer_name = _clean_text(lookup.get("customerName"))
    current_customer_name = _clean_text(customer.get("name") or row.get("alquiler_a"))
    mismatch = bool(remito_customer_name and current_customer_name and _norm(remito_customer_name) != _norm(current_customer_name))
    return {
        "ingresoId": row.get("id"),
        "status": "blocked",
        "code": "",
        "message": "",
        "remitoNumber": document.remito_number,
        "document": {
            "type": document.type,
            "letter": document.letter,
            "point": document.point,
            "number": document.number,
            "documentMode": document.document_mode,
        },
        "company": {"key": document.company_key, "label": _company_label(document.company_key)},
        "customer": {
            "inputName": current_customer_name,
            "code": _clean_text(customer.get("code")),
            "name": current_customer_name,
            "matched": bool(customer.get("matched")),
        },
        "remitoCustomer": {
            "code": _clean_text(lookup.get("customerCode")),
            "name": remito_customer_name,
        },
        "customerMismatch": mismatch,
        "customerSuggestion": remito_customer_name,
        "article": {
            "code": article_code,
            "description": article_description,
            "source": "stock" if stock.get("articleCode") == article_code and article_code else "mapping",
        },
        "partida": _clean_text(row.get("numero_serie")),
        "stock": {
            "deposit": RENTAL_STOCK_DEPOSIT,
            "stlQuantity": stock.get("stlQuantity", 0),
            "stlAvailable": stock.get("stlAvailable", 0),
            "hasStlStock": bool(stock.get("hasStlStock")),
            "records": stock.get("records") or [],
        },
        "remitoExists": bool(lookup.get("exists")),
        "remitoMatchesItem": bool(lookup.get("matchesItem")),
        "issueDate": _clean_text(lookup.get("issueDate")),
        "action": None,
        "blockedReason": "",
    }


def get_rental_remito_state(
    ingreso_id: int,
    remito_number: Any | None = None,
    *,
    alquiler_a_override: Any | None = None,
    actor_user_id: int | None = None,
    sales_client: SalesSDKClient | None = None,
    stock_client: StockSDKClient | None = None,
) -> dict[str, Any]:
    row = _ingreso_context(int(ingreso_id))
    raw_number = remito_number if remito_number is not None else row.get("alquiler_remito")
    if not _clean_text(raw_number):
        return {"ingresoId": ingreso_id, "status": "empty", "message": "No hay remito de alquiler cargado."}
    fallback_company = _company_key_for_context(row)
    document = normalize_rental_remito_number(raw_number, fallback_company_key=fallback_company)
    serial = _clean_text(row.get("numero_serie"))
    if not serial:
        raise RentalRemitoError("RENTAL_SERIAL_REQUIRED", "El ingreso no tiene número de serie para validar Bejerman.", status_code=409)
    customer_text = row.get("alquiler_a") if alquiler_a_override is None else alquiler_a_override
    customer = _resolve_customer_from_text(customer_text)
    mapping = _mapping_article_for_context(row, document.company_key)
    sales = sales_client or SalesSDKClient(company_key=document.company_key, actor_user_id=actor_user_id)
    stock = stock_client or StockSDKClient(company_key=document.company_key, actor_user_id=actor_user_id)
    stock_response = stock.stock_by_deposit_partida("", serial)
    stock_state = _stock_summary(stock_response, serial)
    article_code = _clean_text(mapping.get("article_code")) or _clean_text(stock_state.get("articleCode"))
    article_description = _clean_text(mapping.get("article_description")) or _clean_text(stock_state.get("articleDescription"))
    lookup = _lookup_rta_document(sales, document, serial=serial, article_code=article_code)
    response = _base_response(
        {**row, "alquiler_a": customer_text},
        document,
        customer=customer,
        article_code=article_code,
        article_description=article_description,
        stock=stock_state,
        lookup=lookup,
    )

    if document.document_mode == "existing":
        if not lookup.get("exists"):
            response.update(
                {
                    "status": "blocked",
                    "code": "RENTAL_REMITO_NOT_FOUND",
                    "message": f"No se encontró en Bejerman el {document.remito_number}.",
                    "blockedReason": "El punto 00004/00007 debe existir como RTA R en Bejerman.",
                }
            )
            return response
        if not lookup.get("matchesItem"):
            response.update(
                {
                    "status": "blocked",
                    "code": "RENTAL_REMITO_ITEM_MISMATCH",
                    "message": "El remito existe, pero no contiene la partida/artículo de este equipo.",
                    "blockedReason": "Revisá que el remito corresponda al número de serie del ingreso.",
                }
            )
            return response
        if stock_state.get("hasStlStock"):
            response.update(
                {
                    "status": "stock_inconsistent",
                    "code": "RENTAL_STL_STOCK_PENDING",
                    "message": "El RTA existe, pero la partida sigue con stock positivo en STL.",
                    "action": {"type": "correct_stl_exit", "label": "Corregir salida de stock"},
                }
            )
            return response
        response.update({"status": "verified", "code": "RENTAL_REMITO_VERIFIED", "message": "Remito y stock STL verificados."})
        return response

    if lookup.get("exists"):
        if stock_state.get("hasStlStock"):
            response.update(
                {
                    "status": "stock_inconsistent",
                    "code": "RENTAL_STL_STOCK_PENDING",
                    "message": "El RTA ya existe, pero la partida sigue con stock positivo en STL.",
                    "action": {"type": "correct_stl_exit", "label": "Corregir salida de stock"},
                }
            )
            return response
        response.update({"status": "verified", "code": "RENTAL_RTA_ALREADY_REGISTERED", "message": "El RTA ya existe en Bejerman."})
        return response

    if not stock_state.get("hasStlStock"):
        response.update(
            {
                "status": "blocked",
                "code": "RENTAL_STL_STOCK_REQUIRED",
                "message": "No se puede generar el RTA: la partida no tiene stock positivo en STL.",
                "blockedReason": "El movimiento de alquiler necesita stock disponible en Estantería de Alquiler.",
            }
        )
        return response
    if not article_code:
        response.update(
            {
                "status": "blocked",
                "code": "RENTAL_ARTICLE_REQUIRED",
                "message": "No se puede generar el RTA: falta resolver el artículo Bejerman del equipo.",
                "blockedReason": "Definí el mapeo de artículo o conciliá la partida desde stock Bejerman.",
            }
        )
        return response
    occasional_customer_available = bool(
        _clean_text(row.get("propietario_nombre")) and _clean_text(row.get("propietario_doc"))
    )
    if not customer.get("code") and not occasional_customer_available:
        response.update(
            {
                "status": "blocked",
                "code": "RENTAL_CUSTOMER_REQUIRED",
                "message": "No se puede generar el RTA: el cliente de alquiler no tiene código Bejerman.",
                "blockedReason": "Seleccioná un cliente del catálogo con código Bejerman.",
            }
        )
        return response
    response.update(
        {
            "status": "needs_stock_action",
            "code": "RENTAL_RTA_CAN_REGISTER",
            "message": "El remito 00001 puede registrarse en Bejerman con salida real desde STL.",
            "action": {"type": "register_rta_pv1", "label": "Generar movimiento Bejerman"},
        }
    )
    return response


def _insert_ingreso_event(ingreso_id: int, actor_user_id: int | None, estado: str, comment: str) -> int | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingreso_events(ticket_id, a_estado, usuario_id, comentario)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            [ingreso_id, estado, actor_user_id, comment],
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def _upsert_trace_job(
    *,
    ingreso_id: int,
    row: dict[str, Any],
    sync_type: str,
    status: str,
    actor_user_id: int | None,
    event_id: int | None,
    document: RentalRemitoDocument,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    last_error: str | None = None,
    article_code: str = "",
) -> dict[str, Any]:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_sync_jobs(
              sync_type, ingreso_id, device_id, ingreso_event_id, numero_serie,
              source_deposit, target_deposit, company_key, status, last_error,
              article_code, actor_user_id, request_payload, response_payload
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            ON CONFLICT (sync_type, ingreso_id) WHERE ingreso_id IS NOT NULL DO UPDATE
               SET ingreso_event_id = COALESCE(EXCLUDED.ingreso_event_id, bejerman_sync_jobs.ingreso_event_id),
                   numero_serie = EXCLUDED.numero_serie,
                   source_deposit = EXCLUDED.source_deposit,
                   target_deposit = EXCLUDED.target_deposit,
                   company_key = EXCLUDED.company_key,
                   status = EXCLUDED.status,
                   last_error = EXCLUDED.last_error,
                   article_code = COALESCE(NULLIF(EXCLUDED.article_code, ''), bejerman_sync_jobs.article_code),
                   actor_user_id = COALESCE(EXCLUDED.actor_user_id, bejerman_sync_jobs.actor_user_id),
                   request_payload = bejerman_sync_jobs.request_payload || EXCLUDED.request_payload,
                   response_payload = bejerman_sync_jobs.response_payload || EXCLUDED.response_payload,
                   updated_at = CURRENT_TIMESTAMP
            RETURNING id, status
            """,
            [
                sync_type,
                ingreso_id,
                row.get("device_id"),
                event_id,
                _clean_text(row.get("numero_serie")),
                RENTAL_STOCK_DEPOSIT,
                "SALIDA",
                document.company_key,
                status,
                last_error,
                article_code,
                actor_user_id,
                _json_param(request_payload),
                _json_param(response_payload),
            ],
        )
        traced = _one(cur)
    return traced or {}


def save_rental_remito(
    ingreso_id: int,
    remito_number: Any,
    *,
    alquiler_a: Any = None,
    actor_user_id: int | None = None,
    sales_client: SalesSDKClient | None = None,
    stock_client: StockSDKClient | None = None,
) -> dict[str, Any]:
    row = _ingreso_context(int(ingreso_id))
    customer_text = alquiler_a if alquiler_a is not None else row.get("alquiler_a")
    state = get_rental_remito_state(
        ingreso_id,
        remito_number,
        alquiler_a_override=customer_text,
        actor_user_id=actor_user_id,
        sales_client=sales_client,
        stock_client=stock_client,
    )
    if state.get("status") == "blocked":
        return state
    document_number = _clean_text(state.get("remitoNumber"))
    issue_date = _clean_text(state.get("issueDate"))
    with connection.cursor() as cur:
        if issue_date:
            cur.execute(
                """
                UPDATE ingresos
                   SET alquiler_remito = %s,
                       remito_salida = COALESCE(NULLIF(remito_salida, ''), %s),
                       alquiler_fecha = COALESCE(alquiler_fecha, %s),
                       alquiler_a = CASE WHEN %s IS NULL THEN alquiler_a ELSE NULLIF(%s, '') END
                 WHERE id = %s
                """,
                [document_number, document_number, issue_date, alquiler_a, _clean_text(customer_text), ingreso_id],
            )
        else:
            cur.execute(
                """
                UPDATE ingresos
                   SET alquiler_remito = %s,
                       remito_salida = COALESCE(NULLIF(remito_salida, ''), %s),
                       alquiler_a = CASE WHEN %s IS NULL THEN alquiler_a ELSE NULLIF(%s, '') END
                 WHERE id = %s
                """,
                [document_number, document_number, alquiler_a, _clean_text(customer_text), ingreso_id],
            )
    return state


def fetch_rental_remito_pdf(
    ingreso_id: int,
    *,
    actor_user_id: int | None = None,
    sales_client: SalesSDKClient | None = None,
) -> tuple[bytes, str, str]:
    """Abre únicamente la copia ya publicada del RTA, sin emitir ni regenerar nada."""
    row = _ingreso_context(int(ingreso_id))
    remito = _clean_text(row.get("alquiler_remito"))
    if not remito:
        raise RentalRemitoError("RENTAL_REMITO_REQUIRED", "El equipo no tiene un RTA asociado.", status_code=409)
    document = normalize_rental_remito_number(remito, fallback_company_key=_company_key_for_context(row))
    customer = _resolve_customer_from_text(row.get("alquiler_a"))
    reference = BejermanPdfReference(
        type=document.type,
        number=document.number,
        letter=document.letter,
        point_of_sale=document.point,
        issue_date=_clean_text(row.get("alquiler_fecha"))[:10],
        customer_code=_clean_text(customer.get("code")),
    )
    client = sales_client or SalesSDKClient(company_key=document.company_key, actor_user_id=actor_user_id)
    try:
        pdf, status = probe_comprobante_pdf_copy(
            client, reference, interactive=True, allow_directory_scan=True,
        )
    except Exception as exc:
        raise RentalRemitoError("RENTAL_REMITO_PDF_LOOKUP_FAILED", str(exc), status_code=502) from exc
    if not pdf:
        if status == "error":
            raise RentalRemitoError(
                "RENTAL_REMITO_PDF_LOOKUP_FAILED",
                f"No se pudo leer la carpeta de PDFs de Bejerman para buscar el {document.remito_number}.",
                status_code=502,
            )
        raise RentalRemitoError(
            "RENTAL_REMITO_PDF_NOT_FOUND",
            f"No se encontró el PDF ya impreso del {document.remito_number} en Bejerman.",
            status_code=404,
        )
    return pdf, "application/pdf", f"remito-{document.remito_number}.pdf".replace("/", "-")


def _delivery_request_for_rta(row: dict[str, Any], state: dict[str, Any], document: RentalRemitoDocument) -> dict[str, Any]:
    issue_date = timezone.localdate().isoformat()
    customer_code = state["customer"].get("code")
    customer_name = state["customer"].get("name")
    article = state.get("article") or {}
    serial = _clean_text(row.get("numero_serie"))
    equipment_name = " ".join(
        part for part in (_clean_text(row.get("marca")), _clean_text(row.get("modelo")), _clean_text(row.get("variante"))) if part
    )
    return {
        "groupId": f"rental-{uuid4()}",
        "companyKey": document.company_key,
        "issueDate": issue_date,
        "customerCode": customer_code,
        "customerName": customer_name,
        "priceCurrency": "ARS",
        "exchangeRate": 1,
        "sellerCode": _clean_text(_setting("BEJERMAN_REMITO_RENTAL_SELLER", "ADM")) or "ADM",
        "paymentTermCode": _clean_text(_setting("BEJERMAN_REMITO_PAYMENT_TERM", _setting("BEJERMAN_RIS_PAYMENT_TERM", "30"))) or "30",
        "notes": f"Alquiler OS {row.get('id')} - {document.remito_number}",
        "orders": [
            {
                "id": f"ingreso-{row.get('id')}",
                "orderNumber": f"OS {row.get('id')}",
                "deliveryType": "rental",
                "sourceReference": f"OS {row.get('id')}",
                "equipmentModel": equipment_name,
                "equipmentSerial": serial,
                "items": [
                    {
                        "articleCode": article.get("code"),
                        "articleName": article.get("description") or equipment_name or article.get("code"),
                        "description": article.get("description") or equipment_name or article.get("code"),
                        "quantity": 1,
                        "unitPrice": 0,
                        "priceCurrency": "ARS",
                        "discountPercent": 0,
                        "partida": serial,
                        "equipmentSerial": serial,
                        "stockDepositCode": RENTAL_STOCK_DEPOSIT,
                    }
                ],
            }
        ],
    }


def _rental_profile(document: RentalRemitoDocument) -> dict[str, Any]:
    return {
        "type": RENTAL_REMITO_TYPE,
        "operation": RENTAL_OPERATION,
        "deposit": RENTAL_STOCK_DEPOSIT,
        "pointOfSale": document.point,
    }


def _register_rta(
    ingreso_id: int,
    state: dict[str, Any],
    document: RentalRemitoDocument,
    *,
    actor_user_id: int | None,
    sales_client: SalesSDKClient | None,
) -> dict[str, Any]:
    row = _ingreso_context(int(ingreso_id))
    client = sales_client or SalesSDKClient(company_key=document.company_key, actor_user_id=actor_user_id)
    bridge_request = _delivery_request_for_rta(row, state, document)
    try:
        if bridge_request["customerCode"]:
            customer_fields = resolve_customer_document_fields(client, bridge_request["customerCode"])
        else:
            propietario_nombre = _clean_text(row.get("propietario_nombre"))
            propietario_doc = _clean_text(row.get("propietario_doc"))
            if not (propietario_nombre and propietario_doc):
                raise BejermanSdkResponseError("BEJERMAN_CUSTOMER_NOT_FOUND")
            # Ver el mismo paso en bejerman_ris._resolve_or_build_customer_fields.
            ensure_particular_customer_enabled(client, document.company_key, propietario_doc)
            customer_fields = build_occasional_customer_fields(propietario_nombre, propietario_doc)
            bridge_request["customerCode"] = BEJERMAN_OCCASIONAL_CUSTOMER_CODE
        built = build_delivery_remito_comprobante(
            bridge_request,
            delivery_remito_config(_rental_profile(document)),
            customer_fields,
            {state["article"]["code"]: {"type": "1", "code": "", "rate": 0}},
        )
        comprobante = built["comprobante"]
        comprobante["Comprobante_PtoVenta"] = document.point
        comprobante["Comprobante_Numero"] = document.number
        raw_response = client.ingresar_comprobante_ventas_json(
            comprobante,
            circuito=_clean_text(_setting("BEJERMAN_REMITO_CIRCUIT", "VENTAS")) or "VENTAS",
            operacion=_clean_text(_setting("BEJERMAN_REMITO_OPERATION", "IngresarComprobanteJSON")) or "IngresarComprobanteJSON",
            numera_flex="N",
            emite_reg="R",
        )
    except Exception as exc:
        raise RentalRemitoError("RENTAL_RTA_REGISTER_FAILED", str(exc), status_code=502) from exc
    response = parse_remito_response(raw_response)
    response_summary = {
        **response,
        "remitoNumber": response.get("remitoNumber") or document.remito_number,
        "companyKey": document.company_key,
        "documentMode": "register",
        "lineCount": built["lineCount"],
        "raw": raw_response,
    }
    event_id = _insert_ingreso_event(
        ingreso_id,
        actor_user_id,
        "alquilado",
        f"Alquiler registrado en Bejerman con {document.remito_number}.",
    )
    _upsert_trace_job(
        ingreso_id=ingreso_id,
        row=row,
        sync_type=SYNC_TYPE_RENTAL_REGISTER_RTA,
        status="succeeded",
        actor_user_id=actor_user_id,
        event_id=event_id,
        document=document,
        request_payload={
            "action": "register_rta_pv1",
            "remitoNumber": document.remito_number,
            "stockBefore": state.get("stock"),
            "bridgeRequest": bridge_request,
        },
        response_payload={"sdk": response_summary},
        article_code=state["article"].get("code") or "",
    )
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE ingresos
               SET estado = 'alquilado',
                   alquilado = TRUE,
                   alquiler_remito = %s,
                   remito_salida = COALESCE(NULLIF(remito_salida, ''), %s),
                   alquiler_fecha = COALESCE(alquiler_fecha, %s),
                   alquiler_a = COALESCE(NULLIF(alquiler_a, ''), %s)
             WHERE id = %s
            """,
            [document.remito_number, document.remito_number, timezone.localdate().isoformat(), bridge_request["customerName"], ingreso_id],
        )
    return {"status": "succeeded", "idempotent": False, "response": response_summary}


def _stock_exit_payload(row: dict[str, Any], state: dict[str, Any], document: RentalRemitoDocument) -> list[dict[str, Any]]:
    serial = _clean_text(row.get("numero_serie"))
    article_code = state["article"].get("code") or ""
    document_date = timezone.localdate().isoformat()
    return [
        {
            "Comprobante_FechaEmision": document_date,
            "Comprobante_Tipo": _clean_text(_setting("BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE", "SAL")) or "SAL",
            "Comprobante_Letra": "",
            "Comprobante_PtoVenta": "",
            "Comprobante_Numero": f"{90000000 + (int(row['id']) % 100000):08d}",
            "Comprobante_Art_CodGen": article_code,
            "Comprobante_ArtPartida": serial,
            "Comprobante_DescArticulo": (state["article"].get("description") or f"NEXORA {serial}")[:50],
            "Comprobante_ArtCodTipo": "1",
            "Comprobante_ArtDeposito": RENTAL_STOCK_DEPOSIT,
            "Comprobante_CantidadUM1": -1,
            "Comprobante_CantidadUM2": -1,
            "Comprobante_PrecioTotalMonLocal": 0,
            "Partida_Observaciones": observacion_partida(
                row.get("numero_interno"), row.get("n_de_control"), mg_estado=row.get("mg_estado")
            ),
            "Comprobante_IdOrigen": f"NEXORA-OS-{row['id']}-SAL-STL-{document.point}-{document.number}",
        }
    ]


def _correct_stl_exit(
    ingreso_id: int,
    state: dict[str, Any],
    document: RentalRemitoDocument,
    *,
    actor_user_id: int | None,
    stock_client: StockSDKClient | None,
) -> dict[str, Any]:
    row = _ingreso_context(int(ingreso_id))
    if not state.get("stock", {}).get("hasStlStock"):
        return {"status": "succeeded", "idempotent": True, "message": "La partida ya no tiene stock positivo en STL."}
    if not state.get("article", {}).get("code"):
        raise RentalRemitoError("RENTAL_ARTICLE_REQUIRED", "Falta resolver el artículo Bejerman para corregir STL.", status_code=409)
    payload = _stock_exit_payload(row, state, document)
    client = stock_client or StockSDKClient(company_key=document.company_key, actor_user_id=actor_user_id)
    try:
        raw_response = client.ingresar_lista_comprobantes_json(payload)
    except Exception as exc:
        raise RentalRemitoError("RENTAL_STL_EXIT_FAILED", str(exc), status_code=502) from exc
    event_id = _insert_ingreso_event(
        ingreso_id,
        actor_user_id,
        "alquilado",
        f"Stock STL corregido por alquiler {document.remito_number}.",
    )
    _upsert_trace_job(
        ingreso_id=ingreso_id,
        row=row,
        sync_type=SYNC_TYPE_RENTAL_STL_EXIT,
        status="succeeded",
        actor_user_id=actor_user_id,
        event_id=event_id,
        document=document,
        request_payload={"action": "correct_stl_exit", "remitoNumber": document.remito_number, "stockBefore": state.get("stock"), "payload": payload},
        response_payload={"sdk": raw_response},
        article_code=state["article"].get("code") or "",
    )
    return {"status": "succeeded", "idempotent": False, "response": raw_response}


def execute_rental_remito_action(
    ingreso_id: int,
    action: str,
    *,
    remito_number: Any | None = None,
    actor_user_id: int | None = None,
    sales_client: SalesSDKClient | None = None,
    stock_client: StockSDKClient | None = None,
) -> dict[str, Any]:
    row = _ingreso_context(int(ingreso_id))
    raw_number = remito_number if remito_number is not None else row.get("alquiler_remito")
    document = normalize_rental_remito_number(raw_number, fallback_company_key=_company_key_for_context(row))
    action = _clean_text(action)
    if action not in {"register_rta_pv1", "correct_stl_exit"}:
        raise RentalRemitoError("RENTAL_ACTION_INVALID", "Acción de alquiler inválida.", status_code=400)
    with transaction.atomic():
        state = get_rental_remito_state(
            ingreso_id,
            document.remito_number,
            actor_user_id=actor_user_id,
            sales_client=sales_client,
            stock_client=stock_client,
        )
        available_action = (state.get("action") or {}).get("type")
        if state.get("status") == "verified" and action == "register_rta_pv1":
            return {"status": "succeeded", "idempotent": True, "state": state}
        if available_action != action:
            if action == "correct_stl_exit" and not state.get("stock", {}).get("hasStlStock"):
                return {"status": "succeeded", "idempotent": True, "state": state}
            raise RentalRemitoError(
                "RENTAL_ACTION_NOT_AVAILABLE",
                state.get("blockedReason") or state.get("message") or "La acción no está disponible para este remito.",
                status_code=409,
            )
        if action == "register_rta_pv1":
            result = _register_rta(ingreso_id, state, document, actor_user_id=actor_user_id, sales_client=sales_client)
        else:
            result = _correct_stl_exit(ingreso_id, state, document, actor_user_id=actor_user_id, stock_client=stock_client)
    fresh_state = get_rental_remito_state(
        ingreso_id,
        document.remito_number,
        actor_user_id=actor_user_id,
        sales_client=sales_client,
        stock_client=stock_client,
    )
    return {**result, "state": fresh_state}
