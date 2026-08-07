from __future__ import annotations

import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Any

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

from .bejerman_sdk import BejermanSDKClient, as_string, first_value, normalize_search, records_from_response
from .bejerman_companies import company_for_key, list_ingress_companies


logger = logging.getLogger(__name__)


BEJERMAN_COMPANY_CONTEXT_KEY = "__nexora_bejerman_company_key"

# El catálogo completo de clientes se trae por SOAP (sin filtro server-side),
# así que se cachea unos minutos: evita repetir el fetch completo en cada
# carga de la página de clientes y en cada búsqueda de candidatos.
BEJERMAN_CUSTOMERS_CACHE_SECONDS = 300
BEJERMAN_CUSTOMERS_CACHE_KEY_PREFIX = "bejerman_customer_records"


def _bejerman_customers_cache_key(company_key: str) -> str:
    return f"{BEJERMAN_CUSTOMERS_CACHE_KEY_PREFIX}:{company_key}"

BEJERMAN_CUSTOMER_TEXT_COLUMNS = (
    "bejerman_cod_empresa",
    "bejerman_company_key",
    "bejerman_nombre_fantasia",
    "bejerman_tipo_documento",
    "bejerman_domicilio",
    "bejerman_localidad",
    "bejerman_provincia",
    "bejerman_codigo_postal",
    "bejerman_pais",
    "bejerman_condicion_iva",
    "bejerman_numero_iibb",
    "bejerman_condicion_venta",
    "bejerman_vendedor",
    "bejerman_lista_precio",
    "bejerman_contacto",
    "bejerman_telefono",
    "bejerman_telefono_2",
    "bejerman_email",
)

BEJERMAN_CUSTOMER_COLUMNS = (
    *BEJERMAN_CUSTOMER_TEXT_COLUMNS,
    "bejerman_synced_at",
    "bejerman_raw",
)

BEJERMAN_CUSTOMER_FIELD_ALIASES = {
    "cod_empresa": ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente"),
    "bejerman_cod_empresa": ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente"),
    "bejerman_company_key": ("CompanyKey", "EmpresaKey", "Empresa_Bejerman_Key"),
    "razon_social": ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Nombre", "Cliente"),
    "cuit": ("Cliente_NroDocumento", "Cliente_CUIT", "Cliente_Cuit", "CUIT", "Cuit", "TaxId"),
    "bejerman_nombre_fantasia": ("Cliente_NombreFantasia", "NombreFantasia", "Fantasia", "NombreComercial"),
    "bejerman_tipo_documento": (
        "Cliente_TipoDocumento",
        "Cliente_TipoDocumentoCodigo",
        "Cliente_TipoDoc",
        "TipoDocumento",
        "TipoDocumentoCodigo",
        "TipoDoc",
    ),
    "bejerman_domicilio": ("Cliente_Domicilio", "Cliente_Direccion", "Domicilio", "Direccion", "Dirección"),
    "bejerman_localidad": ("Cliente_Localidad", "Localidad", "Ciudad"),
    "bejerman_provincia": ("Cliente_Provincia", "Cliente_CodigoProvincia", "Provincia", "CodigoProvincia"),
    "bejerman_codigo_postal": ("Cliente_CodigoPostal", "Cliente_CP", "CodigoPostal", "CodPostal", "CP"),
    "bejerman_pais": ("Cliente_Pais", "Cliente_PaisCodigo", "País", "Pais", "CodigoPais"),
    "bejerman_condicion_iva": (
        "Cliente_SitIVA",
        "Cliente_SituacionIVA",
        "Cliente_SituacionIVACodigo",
        "SituacionIVA",
        "SituacionIVACodigo",
        "SitIVA",
        "CondicionIVA",
        "CondiciónIVA",
    ),
    "bejerman_numero_iibb": (
        "Cliente_NumeroIIBB",
        "Cliente_NroIIBB",
        "Cliente_IngresosBrutos",
        "NroIIBB",
        "NumeroIIBB",
        "IngresosBrutos",
    ),
    "bejerman_condicion_venta": (
        "Cliente_CondVenta",
        "Cliente_CondicionVentaCodigo",
        "Cliente_CondicionVenta",
        "CondVenta",
        "CondicionVenta",
        "CodigoCondicionVenta",
        "Comprobante_CondVenta",
    ),
    "bejerman_vendedor": ("Cliente_Vendedor", "Cliente_VendedorCodigo", "Vendedor", "Vendedor_Codigo"),
    "bejerman_lista_precio": (
        "Cliente_ListaPrecio",
        "Cliente_ListaPreciosCodigo",
        "Cliente_ListaPrecios",
        "ListaPrecio",
        "ListaPrecios",
    ),
    "bejerman_contacto": ("Cliente_Contacto", "Contacto", "NombreContacto"),
    "bejerman_telefono": ("Cliente_Telefono", "Cliente_Teléfono", "Telefono", "Teléfono", "Telefono1", "Tel"),
    "bejerman_telefono_2": ("Cliente_Telefono2", "Cliente_Celular", "Telefono2", "Celular"),
    "bejerman_email": ("Cliente_Email", "Cliente_EMail", "Cliente_Mail", "Email", "E-Mail", "Mail"),
}

CUSTOMER_NAME_MATCH_THRESHOLD = 0.82


def clean(value: Any) -> str:
    return str(value or "").strip()


def clean_upper(value: Any) -> str:
    return clean(value).upper()


def digits_only(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _name_score(left: Any, right: Any) -> float:
    left_norm = normalize_search(left)
    right_norm = normalize_search(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    if left_norm in right_norm or right_norm in left_norm:
        ratio = max(ratio, 0.86)
    return ratio


def _identity_mismatch(local: dict[str, Any], details: dict[str, str]) -> bool:
    local_name = clean(local.get("razon_social"))
    remote_name = clean(details.get("razon_social"))
    if local_name and remote_name and _name_score(local_name, remote_name) < CUSTOMER_NAME_MATCH_THRESHOLD:
        return True

    local_cuit = digits_only(local.get("cuit"))
    remote_cuit = digits_only(details.get("cuit"))
    if local_cuit and remote_cuit and local_cuit != remote_cuit:
        return True

    return False


def table_columns(table_name: str) -> set[str]:
    try:
        with connection.cursor() as cur:
            return {column.name for column in connection.introspection.get_table_description(cur, table_name)}
    except Exception:
        return set()


def _client_company_key(client: BejermanSDKClient) -> str:
    marker = clean(getattr(client, "company_key", "")) or clean(getattr(client, "company", ""))
    company = company_for_key(marker, default=None)
    if company:
        return company.key
    return clean_upper(marker)


LOCAL_CUSTOMER_DOCUMENT_FIELD_MAP = {
    "Cliente_RazonSocial": "razon_social",
    "Cliente_TipoDocumento": "bejerman_tipo_documento",
    "Cliente_NroDocumento": "cuit",
    "Cliente_Provincia": "bejerman_provincia",
    "Cliente_SitIVA": "bejerman_condicion_iva",
    "Cliente_NumeroIIBB": "bejerman_numero_iibb",
}
LOCAL_CUSTOMER_DOCUMENT_REQUIRED_FIELDS = (
    "Cliente_RazonSocial",
    "Cliente_TipoDocumento",
    "Cliente_NroDocumento",
    "Cliente_Provincia",
    "Cliente_SitIVA",
)


def lookup_local_customer_document_fields(
    client: BejermanSDKClient,
    customer_code: str,
) -> dict[str, str] | None:
    """Resuelve los datos fiscales cacheados sin cruzar empresas Bejerman.

    `bejerman_cod_empresa` es la identidad canónica. `cod_empresa` solo se usa
    como compatibilidad para filas antiguas que todavía no tienen el código
    Bejerman persistido. Ante duplicados, datos incompletos o un problema de
    esquema se fuerza el fallback en vivo devolviendo `None`.
    """

    code = clean_upper(customer_code)
    company_key = _client_company_key(client)
    if not code or not company_key:
        return None

    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT razon_social,
                       bejerman_tipo_documento,
                       cuit,
                       bejerman_provincia,
                       bejerman_condicion_iva,
                       bejerman_numero_iibb
                  FROM customers
                 WHERE UPPER(TRIM(COALESCE(bejerman_company_key, ''))) = %s
                   AND (
                        UPPER(TRIM(COALESCE(bejerman_cod_empresa, ''))) = %s
                        OR (
                            TRIM(COALESCE(bejerman_cod_empresa, '')) = ''
                            AND UPPER(TRIM(COALESCE(cod_empresa, ''))) = %s
                        )
                   )
                 ORDER BY CASE
                            WHEN UPPER(TRIM(COALESCE(bejerman_cod_empresa, ''))) = %s THEN 0
                            ELSE 1
                          END,
                          id ASC
                 LIMIT 2
                """,
                [company_key, code, code, code],
            )
            names = [column[0] for column in cur.description]
            rows = [dict(zip(names, row)) for row in cur.fetchall()]
    except Exception:
        logger.warning(
            "bejerman_customer_local_lookup_failed",
            extra={"company_key": company_key, "customer_code": code},
            exc_info=True,
        )
        return None

    if len(rows) != 1:
        if len(rows) > 1:
            logger.warning(
                "bejerman_customer_local_lookup_ambiguous",
                extra={"company_key": company_key, "customer_code": code},
            )
        return None

    fields = {
        sdk_field: clean(rows[0].get(local_column))
        for sdk_field, local_column in LOCAL_CUSTOMER_DOCUMENT_FIELD_MAP.items()
    }
    fields = {key: value for key, value in fields.items() if value}
    if any(not fields.get(field) for field in LOCAL_CUSTOMER_DOCUMENT_REQUIRED_FIELDS):
        return None
    return fields


def _with_company_context(record: dict[str, Any], company_key: str) -> dict[str, Any]:
    if not company_key:
        return dict(record)
    return {**record, BEJERMAN_COMPANY_CONTEXT_KEY: company_key}


def _raw_bejerman_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in (record or {}).items() if not str(key).startswith("__nexora_")}


def load_bejerman_customer_records(
    *,
    user_id: int | None = None,
    allow_system_credentials: bool = False,
    company_key: str | None = None,
) -> list[dict[str, Any]]:
    client = BejermanSDKClient(
        company_key=company_key,
        actor_user_id=user_id,
        allow_system_credentials=allow_system_credentials,
    )
    resolved_company_key = _client_company_key(client)
    return [_with_company_context(record, resolved_company_key) for record in records_from_response(client.list_clientes())]


def load_bejerman_customer_records_for_companies(
    *,
    user_id: int | None = None,
    allow_system_credentials: bool = False,
    company_keys: list[str] | None = None,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Carga clientes de todas las empresas Bejerman configuradas (excluye la de prueba).

    El catálogo por empresa se cachea BEJERMAN_CUSTOMERS_CACHE_SECONDS ya que el SDK
    no soporta filtrar server-side (hay que traer todo y filtrar en Python en cada
    carga de clientes / búsqueda de candidatos). Usar force_refresh=True para forzar
    un fetch en vivo (p.ej. al sincronizar manualmente).

    Devuelve (records, errors); solo lanza si ninguna empresa pudo consultarse.
    """
    if company_keys:
        keys = [clean_upper(key) for key in company_keys if clean(key)]
    else:
        keys = [company.key for company in list_ingress_companies() if not company.is_test]

    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    pending_keys: list[str] = []
    for key in keys:
        cache_key = _bejerman_customers_cache_key(key)
        cached = None if force_refresh else cache.get(cache_key)
        if cached is not None:
            records.extend(cached)
        else:
            pending_keys.append(key)

    if pending_keys:
        # Cada empresa pendiente implica un fetch SOAP en vivo (sin filtro server-side);
        # se disparan en paralelo para no pagar la latencia de forma secuencial en el
        # request que carga la página de clientes.
        def _fetch(key: str):
            try:
                return key, load_bejerman_customer_records(
                    user_id=user_id,
                    allow_system_credentials=allow_system_credentials,
                    company_key=key,
                ), None
            except Exception as exc:  # noqa: BLE001 - se agrega a errors, no se relanza acá
                return key, None, exc
            finally:
                # Cada thread abre su propia conexión de DB (credenciales del usuario);
                # cerrarla evita dejar conexiones colgadas fuera del ciclo request/response.
                connection.close()

        with ThreadPoolExecutor(max_workers=len(pending_keys)) as executor:
            futures = [executor.submit(_fetch, key) for key in pending_keys]
            for future in as_completed(futures):
                key, company_records, exc = future.result()
                if exc is not None:
                    errors.append({"company": key, "error": clean(str(exc)) or exc.__class__.__name__})
                    continue
                cache.set(_bejerman_customers_cache_key(key), company_records, BEJERMAN_CUSTOMERS_CACHE_SECONDS)
                records.extend(company_records)

    if keys and errors and not records:
        raise RuntimeError("; ".join(f"{item['company']}: {item['error']}" for item in errors))
    return records, errors


def bejerman_customer_details_from_record(record: dict[str, Any]) -> dict[str, str]:
    details = {
        key: as_string(first_value(record, aliases))
        for key, aliases in BEJERMAN_CUSTOMER_FIELD_ALIASES.items()
    }
    details["cod_empresa"] = clean(details.get("cod_empresa"))
    details["bejerman_cod_empresa"] = clean(details.get("bejerman_cod_empresa") or details.get("cod_empresa"))
    details["bejerman_company_key"] = clean_upper(
        record.get(BEJERMAN_COMPANY_CONTEXT_KEY) or details.get("bejerman_company_key")
    )
    details["razon_social"] = clean(details.get("razon_social"))
    details["cuit"] = digits_only(details.get("cuit"))
    return details


def customer_bejerman_detail_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    raw = row.get("bejerman_raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return {
        "codigoEmpresa": row.get("bejerman_cod_empresa") or "",
        "companyKey": row.get("bejerman_company_key") or "",
        "nombreFantasia": row.get("bejerman_nombre_fantasia") or "",
        "tipoDocumento": row.get("bejerman_tipo_documento") or "",
        "domicilio": row.get("bejerman_domicilio") or "",
        "localidad": row.get("bejerman_localidad") or "",
        "provincia": row.get("bejerman_provincia") or "",
        "codigoPostal": row.get("bejerman_codigo_postal") or "",
        "pais": row.get("bejerman_pais") or "",
        "condicionIva": row.get("bejerman_condicion_iva") or "",
        "numeroIibb": row.get("bejerman_numero_iibb") or "",
        "condicionVenta": row.get("bejerman_condicion_venta") or "",
        "vendedor": row.get("bejerman_vendedor") or "",
        "listaPrecio": row.get("bejerman_lista_precio") or "",
        "contacto": row.get("bejerman_contacto") or "",
        "telefono": row.get("bejerman_telefono") or "",
        "telefono2": row.get("bejerman_telefono_2") or "",
        "email": row.get("bejerman_email") or "",
        "syncedAt": row.get("bejerman_synced_at"),
        "raw": raw,
    }


def bejerman_customer_detail_payload_from_record(record: dict[str, Any]) -> dict[str, Any]:
    details = bejerman_customer_details_from_record(record)
    return customer_bejerman_detail_payload({**details, "bejerman_raw": record})


def _select_expr(columns: set[str], name: str) -> str:
    if name in columns:
        return f"{name}"
    return f"NULL AS {name}"


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
        *(_select_expr(columns, column) for column in BEJERMAN_CUSTOMER_COLUMNS),
    ]
    with connection.cursor() as cur:
        cur.execute(f"SELECT {', '.join(select_columns)} FROM customers")
        names = [col[0] for col in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def _row_company_key(row: dict[str, Any]) -> str:
    return clean_upper(row.get("bejerman_company_key"))


def _company_compatible(row: dict[str, Any], company_key: str) -> bool:
    row_company = _row_company_key(row)
    return not company_key or not row_company or row_company == company_key


def _register_index(
    row: dict[str, Any],
    by_code: dict[str, list[dict[str, Any]]],
    by_cuit: dict[str, list[dict[str, Any]]],
    by_name: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    code = clean_upper(row.get("cod_empresa"))
    cuit = digits_only(row.get("cuit"))
    name = normalize_search(row.get("razon_social"))
    if code:
        by_code[code].append(row)
    if cuit:
        by_cuit[cuit].append(row)
    if by_name is not None and name:
        by_name[name].append(row)


def _json_db_value(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _sync_values(details: dict[str, str], record: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    existing = existing or {}
    values: dict[str, Any] = {
        "cod_empresa": details.get("cod_empresa") or existing.get("cod_empresa"),
        "razon_social": details.get("razon_social") or existing.get("razon_social") or details.get("cod_empresa"),
        "cuit": details.get("cuit") or existing.get("cuit"),
    }
    fill_only_when_blank = {
        "contacto": "bejerman_contacto",
        "telefono": "bejerman_telefono",
        "telefono_2": "bejerman_telefono_2",
        "email": "bejerman_email",
    }
    for local_column, bejerman_column in fill_only_when_blank.items():
        values[local_column] = existing.get(local_column)
        if not clean(existing.get(local_column)) and clean(details.get(bejerman_column)):
            values[local_column] = details.get(bejerman_column)
    for column in BEJERMAN_CUSTOMER_TEXT_COLUMNS:
        values[column] = details.get(column) or None
    values["bejerman_synced_at"] = timezone.now()
    values["bejerman_raw"] = _raw_bejerman_record(record)
    return values


def _execute_write(table: str, values: dict[str, Any], columns: set[str], *, customer_id: int | None = None) -> int | None:
    writable = {key: value for key, value in values.items() if key == "razon_social" or key in columns}
    if not writable.get("razon_social"):
        return None
    params: list[Any] = []
    if customer_id is not None:
        sets = []
        for column, value in writable.items():
            if column == "bejerman_raw" and connection.vendor == "postgresql":
                sets.append(f"{column} = %s::jsonb")
                params.append(_json_db_value(value))
            else:
                sets.append(f"{column} = %s")
                params.append(_json_db_value(value) if column == "bejerman_raw" else value)
        params.append(customer_id)
        with connection.cursor() as cur:
            cur.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s", params)
        return customer_id

    names = list(writable.keys())
    placeholders = []
    for column in names:
        if column == "bejerman_raw" and connection.vendor == "postgresql":
            placeholders.append("%s::jsonb")
            params.append(_json_db_value(writable[column]))
        else:
            placeholders.append("%s")
            params.append(_json_db_value(writable[column]) if column == "bejerman_raw" else writable[column])
    with connection.cursor() as cur:
        cur.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(placeholders)}) RETURNING id",
            params,
        )
        return int(cur.fetchone()[0])


def sync_customers_from_bejerman_records(
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    columns = table_columns("customers")
    if "razon_social" not in columns:
        raise RuntimeError("Falta la columna customers.razon_social")

    local_rows = _load_local_customers(columns)
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_cuit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in local_rows:
        _register_index(row, by_code, by_cuit, by_name)

    summary = {
        "ok": True,
        "total_bejerman": len(records),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "identity_mismatch": 0,
        "linked_other_company": 0,
        "errors": [],
    }
    seen_codes: set[str] = set()

    with transaction.atomic():
        for record in records:
            details = bejerman_customer_details_from_record(record)
            record_company = clean_upper(details.get("bejerman_company_key"))
            code_key = clean_upper(details.get("cod_empresa"))
            cuit_key = digits_only(details.get("cuit"))
            label = details.get("cod_empresa") or details.get("razon_social") or cuit_key or "cliente sin identificar"

            if not details.get("cod_empresa") and not details.get("razon_social"):
                summary["skipped"] += 1
                summary["errors"].append({"customer": label, "reason": "missing_identity"})
                continue
            seen_key = f"{record_company}|{code_key}"
            if code_key and seen_key in seen_codes:
                summary["skipped"] += 1
                summary["errors"].append({"customer": label, "reason": "duplicate_bejerman_code"})
                continue
            if code_key:
                seen_codes.add(seen_key)

            target = None
            match_reason = ""
            if code_key:
                matches = [row for row in by_code.get(code_key, []) if _company_compatible(row, record_company)]
                if len(matches) == 1:
                    target = matches[0]
                    match_reason = "code"
                elif len(matches) > 1:
                    summary["skipped"] += 1
                    summary["errors"].append({"customer": label, "reason": "duplicate_local_code"})
                    continue
            if target is None and cuit_key:
                matches = [row for row in by_cuit.get(cuit_key, []) if _company_compatible(row, record_company)]
                if len(matches) == 1:
                    target = matches[0]
                    match_reason = "cuit"
                elif len(matches) > 1:
                    summary["skipped"] += 1
                    summary["errors"].append({"customer": label, "reason": "duplicate_local_cuit"})
                    continue
            name_key = normalize_search(details.get("razon_social"))
            if target is None:
                matches = [row for row in by_name.get(name_key, []) if _company_compatible(row, record_company)] if name_key else []
                if len(matches) == 1:
                    target = matches[0]
                    match_reason = "name"
                elif len(matches) > 1:
                    summary["skipped"] += 1
                    summary["errors"].append({"customer": label, "reason": "duplicate_local_name"})
                    continue

            if target is None and record_company:
                # El mismo cliente ya existe vinculado a otra empresa Bejerman:
                # no se duplica ni se pisa esa vinculación.
                bound_elsewhere = None
                if cuit_key:
                    bound_elsewhere = next(
                        (row for row in by_cuit.get(cuit_key, []) if not _company_compatible(row, record_company)),
                        None,
                    )
                if bound_elsewhere is None and name_key:
                    bound_elsewhere = next(
                        (row for row in by_name.get(name_key, []) if not _company_compatible(row, record_company)),
                        None,
                    )
                if bound_elsewhere is not None:
                    summary["skipped"] += 1
                    summary["linked_other_company"] += 1
                    continue

            if target and _identity_mismatch(target, details):
                summary["skipped"] += 1
                summary["identity_mismatch"] += 1
                summary["errors"].append(
                    {
                        "customer": label,
                        "reason": "identity_mismatch",
                        "match": match_reason,
                        "local_id": target.get("id"),
                        "local_name": target.get("razon_social") or "",
                        "bejerman_name": details.get("razon_social") or "",
                        "local_cuit": digits_only(target.get("cuit")),
                        "bejerman_cuit": cuit_key,
                    }
                )
                continue

            values = _sync_values(details, record, target)
            if dry_run:
                summary["updated" if target else "created"] += 1
                continue

            if target:
                customer_id = int(target["id"])
                _execute_write("customers", values, columns, customer_id=customer_id)
                target.update(values)
                summary["updated"] += 1
            else:
                customer_id = _execute_write("customers", values, columns)
                if customer_id is None:
                    summary["skipped"] += 1
                    summary["errors"].append({"customer": label, "reason": "missing_razon_social"})
                    continue
                new_row = {"id": customer_id, **values}
                _register_index(new_row, by_code, by_cuit, by_name)
                summary["created"] += 1

    summary["errors"] = summary["errors"][:50]
    return summary
