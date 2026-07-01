from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .bejerman_companies import require_company
from .bejerman_documents import (
    REMITO_COMPROBANTE_TYPES,
    RIS_REGISTERED_NO_PDF_CODE,
    parse_bejerman_remito_number,
    registered_remito_no_pdf_detail,
)
from .bejerman_sdk import (
    BejermanPdfReference,
    BejermanPdfPendingError,
    BejermanSDKClient,
    BejermanSdkConfigError,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
    as_bool,
    as_string,
    bejerman_filter,
    build_customer_document_fields,
    build_service_ingress_comprobante,
    comprobante_id_of,
    fetch_comprobante_pdf,
    find_bejerman_client_record,
    first_record,
    first_value,
    format_remito_number,
    normalize_serial_for_lookup,
    parse_remito_response,
    records_from_response,
    resolve_customer_document_fields,
    resolve_equipment_partida,
    sdk_signed_article_quantity,
    sdk_uses_negative_article_quantity,
)
from .bejerman_sync import (
    BejermanBlockedError,
    BejermanConfigError,
    BejermanSDKClient as SyncBejermanSDKClient,
    BejermanTransientError,
    _article_code_from_record,
    _article_description_from_record,
    _deposit_from_record,
    _partida_from_record,
    _records_for_partida_any_quantity,
    normalize_article_variant,
    reopen_jobs_for_article_mapping,
    upsert_article_mapping,
    validate_bejerman_article_choice,
)
from .remito_pdf_notifications import notify_bejerman_remito_pdf_issued

logger = logging.getLogger(__name__)

RIS_DOCUMENT_MODE_EMIT = "emit"
RIS_DOCUMENT_MODE_REGISTER = "register"
RIS_REGISTER_POINT_OF_SALE = "00001"
RIS_REGISTER_NUMERA_FLEX = "N"
RIS_REGISTER_EMITE_REG = "R"
MG_CODE_RE = re.compile(r"^(MG|NM|NV|CE)\s*(\d{1,4})$", re.IGNORECASE)
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


class BejermanRisPreflightError(BejermanRisError):
    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("detail") or "La validación previa del RIS falló")
        self.payload = payload


class BejermanRisRegisteredNoPdfError(BejermanRisError):
    code = RIS_REGISTERED_NO_PDF_CODE

    def __init__(self, remito_number: Any = ""):
        super().__init__(registered_remito_no_pdf_detail(remito_number))
        self.remito_number = _clean(remito_number)


def _json_param(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


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


def _clean_upper(value: Any) -> str:
    return _clean(value).upper()


def _digits_only(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def parse_ris_document_mode(value: Any) -> str:
    text = _normalize_key(value)
    if text in {"register", "registered", "registrar", "registrado"}:
        return RIS_DOCUMENT_MODE_REGISTER
    return RIS_DOCUMENT_MODE_EMIT


def _register_point_of_sale() -> str:
    raw = _digits_only(getattr(settings, "BEJERMAN_RIS_REGISTER_POINT_OF_SALE", RIS_REGISTER_POINT_OF_SALE))
    return (raw or RIS_REGISTER_POINT_OF_SALE).zfill(5)


def _expected_register_point_of_sale(profile: dict[str, Any] | None = None) -> str:
    profile = profile if isinstance(profile, dict) else {}
    return (_digits_only(profile.get("pointOfSale")) or _register_point_of_sale()).zfill(5)


def _register_allowed_points_of_sale(profile: dict[str, Any] | None = None) -> list[str]:
    profile = profile if isinstance(profile, dict) else {}
    points: list[str] = []
    for raw in (_register_point_of_sale(), _digits_only(profile.get("pointOfSale"))):
        point = (_digits_only(raw) or "").zfill(5)
        if point and point != "00000" and point not in points:
            points.append(point)
    return points or [_register_point_of_sale()]


def _format_points_of_sale(points: list[str]) -> str:
    if len(points) <= 1:
        return points[0] if points else ""
    return ", ".join(points[:-1]) + f" o {points[-1]}"


def _validate_registered_point_of_sale(point: str, profile: dict[str, Any] | None = None) -> None:
    allowed = _register_allowed_points_of_sale(profile)
    if point not in allowed:
        raise BejermanRisError(f"El punto de venta del remito debe ser {_format_points_of_sale(allowed)}")


def _is_digital_registered_document(document: dict[str, str], payload: dict[str, Any]) -> bool:
    point = _clean(document.get("point")).zfill(5)
    if not point or point == _register_point_of_sale():
        return False
    profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    company_key = _clean(payload.get("companyKey")) or "SEPID"
    digital_points = {
        _digital_point_of_sale_for_company(company_key, profile.get("pointOfSale")),
        (_digits_only(profile.get("pointOfSale")) or "").zfill(5),
    }
    digital_points.discard("")
    digital_points.discard("00000")
    return point in digital_points


def _is_manual_registered_document(document: dict[str, str]) -> bool:
    return _clean(document.get("point")).zfill(5) == _register_point_of_sale()


def normalize_manual_ris_remito_number(
    value: Any,
    profile: dict[str, Any] | None = None,
    *,
    require_complete: bool = False,
) -> dict[str, str]:
    text = _clean(value)
    if not text:
        raise BejermanRisError("Debe cargar el número completo de remito")
    profile = profile if isinstance(profile, dict) else {}
    tipo = _clean(profile.get("type")) or _clean(getattr(settings, "BEJERMAN_RIS_TYPE", "RIS")) or "RIS"
    letter = _clean(profile.get("letter")) or _clean(getattr(settings, "BEJERMAN_RIS_LETTER", "R")) or "R"
    points = _register_allowed_points_of_sale(profile)
    examples = " o ".join(f"{tipo} {letter} {point}-00004566" for point in points)
    try:
        return parse_bejerman_remito_number(
            text,
            default_type=tipo,
            default_letter=letter,
            require_explicit_match=True,
            validate_point=lambda point: _validate_registered_point_of_sale(point, profile),
            allow_number_only=not require_complete,
            default_point=_expected_register_point_of_sale(profile),
            require_complete=require_complete,
            complete_error_message=f"Debe cargar el remito completo con punto de venta, por ejemplo {examples}",
            explicit_mismatch_message=f"El remito debe corresponder a {tipo} {letter} para este motivo de ingreso",
        )
    except ValueError as exc:
        raise BejermanRisError(str(exc)) from exc


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


def _setting_bool(name: str, default: bool = False) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return value
    return _normalize_key(value) in {"1", "s", "si", "true", "yes", "y", "on"}


def _digital_point_of_sale_for_company(company_key: Any, fallback: Any = "") -> str:
    company = _clean_upper(company_key) or "SEPID"
    if company == "MGBIO":
        return "00007"
    if company == "SEPID":
        return "00004"
    for name in (
        f"BEJERMAN_RIS_{company}_POINT_OF_SALE",
        f"BEJERMAN_REMITO_{company}_POINT_OF_SALE",
    ):
        value = _digits_only(getattr(settings, name, ""))
        if value:
            return value.zfill(5)
    return (_digits_only(fallback) or "00004").zfill(5)


def _document_profile_from_settings(
    *,
    key: str,
    label: str,
    reason: str,
    type_setting: str,
    point_setting: str,
    operation_setting: str,
    deposit_setting: str,
    update_stock_setting: str,
    type_default: str,
    point_default: str,
    operation_default: str,
    deposit_default: str,
    update_stock_default: bool,
    company_key: str = "SEPID",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "reason": reason,
        "type": _clean(getattr(settings, type_setting, type_default)) or type_default,
        "letter": _clean(getattr(settings, "BEJERMAN_RIS_LETTER", "R")) or "R",
        "pointOfSale": _digital_point_of_sale_for_company(company_key, getattr(settings, point_setting, point_default)),
        "operation": _clean(getattr(settings, operation_setting, operation_default)) or operation_default,
        "deposit": _clean(getattr(settings, deposit_setting, deposit_default)) or deposit_default,
        "updateStock": _setting_bool(update_stock_setting, update_stock_default),
    }


def _normal_ingress_profile(company_key: str = "SEPID") -> dict[str, Any]:
    return _document_profile_from_settings(
        key="ris",
        label="RIS",
        reason="Ingreso normal de servicio técnico.",
        type_setting="BEJERMAN_RIS_TYPE",
        point_setting="BEJERMAN_RIS_POINT_OF_SALE",
        operation_setting="BEJERMAN_RIS_SERVICE_OPERATION",
        deposit_setting="BEJERMAN_RIS_DEPOSIT",
        update_stock_setting="BEJERMAN_RIS_UPDATE_STOCK",
        type_default="RIS",
        point_default="00004",
        operation_default="REP",
        deposit_default="STR",
        update_stock_default=False,
        company_key=company_key,
    )


def _rental_return_profile(company_key: str = "SEPID") -> dict[str, Any]:
    return _document_profile_from_settings(
        key="rda",
        label="RDA",
        reason="Ingreso de equipo MG activo desde alquiler.",
        type_setting="BEJERMAN_RIS_RENTAL_RETURN_TYPE",
        point_setting="BEJERMAN_RIS_RENTAL_RETURN_POINT_OF_SALE",
        operation_setting="BEJERMAN_RIS_RENTAL_RETURN_OPERATION",
        deposit_setting="BEJERMAN_RIS_RENTAL_RETURN_DEPOSIT",
        update_stock_setting="BEJERMAN_RIS_RENTAL_RETURN_UPDATE_STOCK",
        type_default="RDA",
        point_default="00004",
        operation_default="ALQ",
        deposit_default="STR",
        update_stock_default=True,
        company_key=company_key,
    )


def _demo_return_profile(company_key: str = "SEPID") -> dict[str, Any]:
    return _document_profile_from_settings(
        key="rdn",
        label="RDN",
        reason="Devolución de demo.",
        type_setting="BEJERMAN_RIS_DEMO_RETURN_TYPE",
        point_setting="BEJERMAN_RIS_DEMO_RETURN_POINT_OF_SALE",
        operation_setting="BEJERMAN_RIS_DEMO_RETURN_OPERATION",
        deposit_setting="BEJERMAN_RIS_DEMO_RETURN_DEPOSIT",
        update_stock_setting="BEJERMAN_RIS_DEMO_RETURN_UPDATE_STOCK",
        type_default="RDN",
        point_default="00004",
        operation_default="DEMO",
        deposit_default="STR",
        update_stock_default=True,
        company_key=company_key,
    )


def _normalize_mg_code(value: Any) -> str | None:
    match = MG_CODE_RE.match(_clean(value).upper())
    if not match:
        return None
    return f"{match.group(1).upper()} {match.group(2).zfill(4)}"


def _is_mg_owner_code(*values: Any) -> bool:
    return any(MG_CODE_RE.match(_clean(value)) for value in values if _clean(value))


def _mg_owner_customer_id() -> int | None:
    row = q(
        "SELECT id FROM customers WHERE LOWER(razon_social) LIKE %s ORDER BY id ASC LIMIT 1",
        ["%mg%bio%"],
        one=True,
    )
    return row.get("id") if row else None


def _is_mg_owner_customer(customer_id: Any, mg_owner_id: Any = None) -> bool:
    try:
        if not customer_id:
            return False
        owner_id = mg_owner_id if mg_owner_id is not None else _mg_owner_customer_id()
        return bool(owner_id and int(customer_id) == int(owner_id))
    except Exception:
        return False


def _compact_identifier(value: Any) -> str:
    return _clean(value).upper().replace(" ", "")


def _identifier_candidates(value: Any, *, assume_mg_internal: bool = False) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    candidates = {text}
    normalized = _normalize_mg_code(text)
    if normalized:
        candidates.add(normalized)
    if assume_mg_internal and re.fullmatch(r"\d{1,4}", text):
        number = text.zfill(4)
        candidates.add(f"MG {number}")
        candidates.add(f"MG{text}")
    return sorted({_compact_identifier(item) for item in candidates if _clean(item)})


def _device_from_payload_equipment(equipment: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(equipment, dict) or not _table_exists("devices"):
        return None
    serial_candidates = _identifier_candidates(equipment.get("numero_serie") or equipment.get("serial"))
    internal_candidates = _identifier_candidates(
        equipment.get("numero_interno") or equipment.get("internalNumber"),
        assume_mg_internal=True,
    )
    control_candidates = _identifier_candidates(equipment.get("n_de_control") or equipment.get("numero_control"))
    conditions: list[str] = []
    params: list[Any] = []
    if serial_candidates:
        conditions.append(
            "UPPER(REPLACE(TRIM(COALESCE(d.numero_serie, '')), ' ', '')) IN ("
            + ", ".join(["%s"] * len(serial_candidates))
            + ")"
        )
        params.extend(serial_candidates)
    if internal_candidates:
        conditions.append(
            "UPPER(REPLACE(TRIM(COALESCE(d.numero_interno, '')), ' ', '')) IN ("
            + ", ".join(["%s"] * len(internal_candidates))
            + ")"
        )
        params.extend(internal_candidates)
    if control_candidates and _has_table_column("devices", "n_de_control"):
        conditions.append(
            "UPPER(REPLACE(TRIM(COALESCE(d.n_de_control, '')), ' ', '')) IN ("
            + ", ".join(["%s"] * len(control_candidates))
            + ")"
        )
        params.extend(control_candidates)
    if not conditions:
        return None
    n_de_control_sql = (
        "COALESCE(d.n_de_control, '') AS n_de_control,"
        if _has_table_column("devices", "n_de_control")
        else "'' AS n_de_control,"
    )
    mg_estado_sql = (
        "COALESCE(d.mg_estado, 'activo') AS mg_estado,"
        if _has_table_column("devices", "mg_estado")
        else "'activo' AS mg_estado,"
    )
    alquilado_sql = (
        "COALESCE(d.alquilado, false) AS alquilado,"
        if _has_table_column("devices", "alquilado")
        else "false AS alquilado,"
    )
    alquiler_a_sql = (
        "COALESCE(d.alquiler_a, '') AS alquiler_a,"
        if _has_table_column("devices", "alquiler_a")
        else "'' AS alquiler_a,"
    )
    return q(
        f"""
        SELECT d.id AS device_id,
               d.customer_id AS device_customer_id,
               COALESCE(d.numero_serie, '') AS numero_serie,
               COALESCE(d.numero_interno, '') AS numero_interno,
               {n_de_control_sql}
               {mg_estado_sql}
               {alquilado_sql}
               {alquiler_a_sql}
               COALESCE(d.variante, '') AS device_variante
          FROM devices d
         WHERE {" OR ".join(f"({condition})" for condition in conditions)}
         ORDER BY d.id DESC
         LIMIT 1
        """,
        params,
        one=True,
    )


def _context_mg_flags(context: dict[str, Any]) -> dict[str, Any]:
    mg_owner_id = _mg_owner_customer_id()
    customer_id = context.get("device_customer_id") or context.get("customer_id")
    explicit = _normalize_key(context.get("mg_estado")) or "activo"
    if explicit not in {"activo", "inactivo venta"}:
        explicit = "activo"
    mg_inactivo_venta = explicit == "inactivo venta"
    tiene_codigo_mg = _is_mg_owner_code(
        context.get("numero_interno"),
        context.get("numero_serie"),
        context.get("n_de_control"),
    )
    es_cliente_mg_owner = _is_mg_owner_customer(customer_id, mg_owner_id)
    es_propietario_mg = bool(es_cliente_mg_owner or (tiene_codigo_mg and not mg_inactivo_venta))
    return {
        "es_propietario_mg": es_propietario_mg,
        "es_cliente_mg_owner": es_cliente_mg_owner,
        "tiene_codigo_mg": tiene_codigo_mg,
        "mg_estado": "inactivo_venta" if mg_inactivo_venta else "activo",
        "mg_activo": not mg_inactivo_venta,
        "mg_inactivo_venta": mg_inactivo_venta,
    }


def _ingress_document_profile(context: dict[str, Any], company_key: str = "SEPID") -> dict[str, Any]:
    motivo = _normalize_key(context.get("motivo"))
    if motivo in {"baja alquiler", "reparacion alquiler"}:
        return _rental_return_profile(company_key)
    if motivo == "devolucion demo":
        return _demo_return_profile(company_key)
    return _normal_ingress_profile(company_key)


def _is_rental_return_profile(profile: dict[str, Any] | None, context: dict[str, Any] | None = None) -> bool:
    profile = profile if isinstance(profile, dict) else {}
    motivo = _normalize_key((context or {}).get("motivo"))
    return (
        _normalize_key(profile.get("key")) == "rda"
        or _clean_upper(profile.get("type")) == "RDA"
        or motivo in {"baja alquiler", "reparacion alquiler"}
    )


def _document_profile_key(profile: dict[str, Any] | None) -> tuple[Any, ...]:
    profile = profile if isinstance(profile, dict) else {}
    return (
        _clean_upper(profile.get("type")),
        _clean_upper(profile.get("letter")),
        _clean(profile.get("pointOfSale")).zfill(5),
        _clean_upper(profile.get("operation")),
        _clean_upper(profile.get("deposit")),
        bool(profile.get("updateStock")),
    )


def _document_profile_item_summary(payload: dict[str, Any], index: int) -> str:
    profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    equipment = payload.get("equipment") if isinstance(payload.get("equipment"), dict) else {}
    identifier = _clean(equipment.get("serial")) or _clean(equipment.get("internalNumber")) or f"#{index + 1}"
    motivo = _clean(equipment.get("repairReason")) or "-"
    return f"Equipo {index + 1} ({identifier}, motivo {motivo}) -> {profile.get('type')}/{profile.get('operation')}"


def _document_profile_mismatch_message(payloads: list[dict[str, Any]]) -> str:
    profile_keys = {_document_profile_key(payload.get("documentProfile")) for payload in payloads}
    if len(profile_keys) <= 1:
        return ""
    summaries = "; ".join(_document_profile_item_summary(payload, index) for index, payload in enumerate(payloads))
    return f"El lote mezcla comprobantes de ingreso incompatibles. {summaries}"


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


def _ingreso_ids_from_payload(payload: Any) -> list[int]:
    payload = _json_dict(payload)
    if not payload:
        return []
    raw_ids = payload.get("ingresoIds") or payload.get("ingreso_ids") or []
    if not isinstance(raw_ids, (list, tuple)):
        return []
    out: list[int] = []
    for raw in raw_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out


def serialize_ris_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "available": _has_ris_schema(),
            "status": "pending",
            "pdf_status": "pending",
            "document_mode": RIS_DOCUMENT_MODE_EMIT,
            "manual_remito_number": "",
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
        "document_mode": row.get("document_mode") or RIS_DOCUMENT_MODE_EMIT,
        "manual_remito_number": row.get("manual_remito_number") or "",
        "is_registered": (row.get("document_mode") or RIS_DOCUMENT_MODE_EMIT) == RIS_DOCUMENT_MODE_REGISTER
        and (row.get("status") or "") == "generated",
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
    return serialize_ris_row(_adopt_existing_ingress_remito_reference(ingreso_id))


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
                   pdf_status = 'pending',
                   document_mode = 'emit',
                   manual_remito_number = NULL,
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


def _lock_for_emit_many(ingreso_ids: list[int]) -> list[dict[str, Any]]:
    if not ingreso_ids:
        raise BejermanRisError("No hay ingresos para emitir RIS")
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_ingreso_remitos
                   SET status = 'running',
                       pdf_status = 'pending',
                       document_mode = 'emit',
                       manual_remito_number = NULL,
                       attempts = attempts + 1,
                       last_error = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE ingreso_id = ANY(%s)
                   AND (
                     status <> 'running'
                     OR updated_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
                   )
                RETURNING *
                """,
                [ingreso_ids],
            )
            rows = cur.fetchall()
            cols = [col[0] for col in cur.description]
            locked = [dict(zip(cols, row)) for row in rows]
            if len(locked) != len(ingreso_ids):
                raise BejermanRisBusyError("Ya hay una emisión RIS en curso")
            return locked


def _lock_for_register_many(ingreso_ids: list[int], document: dict[str, str], company_key: str) -> list[dict[str, Any]]:
    if not ingreso_ids:
        raise BejermanRisError("No hay ingresos para registrar remito")
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_ingreso_remitos
                   SET status = 'running',
                       pdf_status = 'not_applicable',
                       document_mode = 'register',
                       manual_remito_number = %s,
                       comprobante_tipo = %s,
                       comprobante_letra = %s,
                       comprobante_pto_venta = %s,
                       comprobante_numero = %s,
                       remito_number = %s,
                       company_key = NULLIF(%s, ''),
                       attempts = attempts + 1,
                       last_error = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE ingreso_id = ANY(%s)
                   AND (
                     status <> 'running'
                     OR updated_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
                   )
                RETURNING *
                """,
                [
                    document["remitoNumber"],
                    document["type"],
                    document["letter"],
                    document["point"],
                    document["number"],
                    document["remitoNumber"],
                    _clean(company_key),
                    ingreso_ids,
                ],
            )
            rows = cur.fetchall()
            cols = [col[0] for col in cur.description]
            locked = [dict(zip(cols, row)) for row in rows]
            if len(locked) != len(ingreso_ids):
                raise BejermanRisBusyError("Ya hay una registración RIS en curso")
            return locked


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


def _update_ris_failure_many(
    ingreso_ids: list[int],
    error: str,
    *,
    pdf_error: bool = False,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
) -> None:
    for ingreso_id in ingreso_ids:
        _update_ris_failure(
            ingreso_id,
            error,
            pdf_error=pdf_error,
            request_payload=request_payload,
            response_payload=response_payload,
        )


def _update_ris_generated(
    ingreso_id: int,
    payload: dict[str, Any],
    response: dict[str, Any],
    *,
    sync_ingreso_remito: bool = True,
) -> dict[str, Any]:
    summary = response.get("response") if isinstance(response.get("response"), dict) else {}
    profile = response.get("profile") if isinstance(response.get("profile"), dict) else {}
    document_mode = parse_ris_document_mode(response.get("documentMode") or payload.get("documentMode"))
    pdf_status = "not_applicable" if document_mode == RIS_DOCUMENT_MODE_REGISTER else "pending"
    manual_remito_number = _clean(response.get("manualRemitoNumber") or payload.get("manualRemitoNumber"))
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
                   pdf_status = %s,
                   document_mode = %s,
                   manual_remito_number = NULLIF(%s, ''),
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
                pdf_status,
                document_mode,
                manual_remito_number,
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
    if sync_ingreso_remito and remito_number:
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET remito_ingreso = %s WHERE id = %s", [remito_number, ingreso_id])
    return dict(zip(cols, row)) if row else (_row_for_ingreso(ingreso_id) or {})


def _update_ris_generated_many(ingreso_ids: list[int], payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    first_row: dict[str, Any] | None = None
    for ingreso_id in ingreso_ids:
        row = _update_ris_generated(ingreso_id, payload, response)
        if first_row is None:
            first_row = row
    return first_row or {}


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


def _parse_complete_existing_remito(raw_remito: str) -> dict[str, str]:
    if not re.match(r"^\s*[A-Za-z]{2,4}\s+[A-Za-z]\s+\d{1,5}\s*[-/ ]\s*\d{1,8}\s*$", raw_remito):
        raise BejermanRisError(
            "El remito guardado debe tener tipo, letra y punto de venta para asociarlo automáticamente"
        )
    try:
        return parse_bejerman_remito_number(
            raw_remito,
            default_type="RIS",
            default_letter="R",
            allowed_types=REMITO_COMPROBANTE_TYPES,
            allow_number_only=False,
            require_complete=True,
        )
    except ValueError as exc:
        raise BejermanRisError(str(exc)) from exc


def _existing_ingress_remito_reference(
    ingreso_id: int,
    current: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    context = _ingreso_context(ingreso_id)
    if not context:
        return None
    raw_remito = _clean((current or {}).get("remito_number")) or _clean(context.get("remito_ingreso"))
    if not raw_remito:
        return None
    try:
        company = require_company(context.get("empresa_bejerman") or context.get("empresa_facturar") or "SEPID")
    except ValueError as exc:
        raise BejermanRisError(str(exc)) from exc
    profile = _ingress_document_profile(context, company.key)
    document = _parse_complete_existing_remito(raw_remito)
    document_mode = RIS_DOCUMENT_MODE_REGISTER if _is_manual_registered_document(document) else RIS_DOCUMENT_MODE_EMIT
    issue_date = _date_iso(context.get("fecha_ingreso")) or timezone.localdate().isoformat()
    summary = {
        "comprobanteTipo": document["type"],
        "comprobanteLetra": document["letter"],
        "comprobantePtoVenta": document["point"],
        "comprobanteNumero": document["number"],
        "remitoNumber": document["remitoNumber"],
    }
    payload = {
        "requestId": f"reparaciones-ingreso-{ingreso_id}-remito-existente",
        "ingresoId": ingreso_id,
        "companyKey": company.key,
        "companyLabel": company.label,
        "bejermanCompany": company.bejerman_company,
        "documentProfile": profile,
        "documentMode": document_mode,
        "issueDate": issue_date,
        "customerCode": _clean(context.get("customer_code")),
        "customerName": _clean(context.get("customer_name")),
        "operation": "view_existing_ingress_remito",
        "existingBejermanRemito": True,
        "legacyRemitoIngreso": raw_remito,
    }
    if document_mode == RIS_DOCUMENT_MODE_REGISTER:
        payload["manualRemitoNumber"] = document["remitoNumber"]
    response = {
        "success": True,
        "requestId": payload["requestId"],
        "documentMode": document_mode,
        "companyKey": company.key,
        "companyLabel": company.label,
        "bejermanCompany": company.bejerman_company,
        "issueDate": issue_date,
        "remitoNumber": document["remitoNumber"],
        "response": summary,
        "profile": profile,
        "lineCount": 1,
        "raw": {"source": "ingresos.remito_ingreso", "value": raw_remito},
        "associatedExistingBejermanRemito": True,
    }
    if document_mode == RIS_DOCUMENT_MODE_REGISTER:
        response["manualRemitoNumber"] = document["remitoNumber"]
    return payload, response


def _adopt_existing_ingress_remito_reference(
    ingreso_id: int,
    user_id: int | None = None,
    current: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _has_ris_schema():
        return current
    row = current if current is not None else (_row_for_ingreso(ingreso_id) or {})
    if _clean(row.get("remito_number")) and row.get("status") == "generated":
        return row
    if _clean(row.get("remito_number")) and (row.get("document_mode") or RIS_DOCUMENT_MODE_EMIT) == RIS_DOCUMENT_MODE_REGISTER:
        return row
    if row.get("status") == "running":
        return row
    try:
        reference = _existing_ingress_remito_reference(ingreso_id, row)
    except BejermanRisError as exc:
        logger.info(
            "bejerman_ris_existing_remito_not_adopted",
            extra={"ingreso_id": ingreso_id, "error": str(exc)},
        )
        return row or None
    if not reference:
        return row or None
    payload, response = reference
    _ensure_ris_row(ingreso_id, user_id)
    return _update_ris_generated(ingreso_id, payload, response, sync_ingreso_remito=False)


def _accessories_text_for_ingreso(ingreso_id: int) -> str:
    try:
        rows = q(
            """
            SELECT
              COALESCE(ca.nombre, '') AS nombre,
              COALESCE(ia.referencia, '') AS referencia,
              COALESCE(ia.descripcion, '') AS descripcion
            FROM ingreso_accesorios ia
            LEFT JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id
            WHERE ia.ingreso_id = %s
            ORDER BY ia.id
            """,
            [ingreso_id],
        )
    except Exception:
        rows = []
    parts: list[str] = []
    for row in rows or []:
        label = _clean(row.get("nombre")) or _clean(row.get("descripcion"))
        ref = _clean(row.get("referencia"))
        if not label and not ref:
            continue
        parts.append(f"{label} (ref: {ref})" if ref else label)
    return ", ".join(parts)


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
    n_de_control_sql = (
        "COALESCE(d.n_de_control, '') AS n_de_control,"
        if _has_table_column("devices", "n_de_control")
        else "'' AS n_de_control,"
    )
    mg_estado_sql = (
        "COALESCE(d.mg_estado, 'activo') AS mg_estado,"
        if _has_table_column("devices", "mg_estado")
        else "'activo' AS mg_estado,"
    )
    alquilado_sql = (
        "COALESCE(d.alquilado, false) AS alquilado,"
        if _has_table_column("devices", "alquilado")
        else "false AS alquilado,"
    )
    alquiler_a_sql = (
        "COALESCE(d.alquiler_a, '') AS alquiler_a,"
        if _has_table_column("devices", "alquiler_a")
        else "'' AS alquiler_a,"
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
          d.customer_id AS device_customer_id,
          COALESCE(d.numero_serie, '') AS numero_serie,
          COALESCE(d.numero_interno, '') AS numero_interno,
          {n_de_control_sql}
          {mg_estado_sql}
          {alquilado_sql}
          {alquiler_a_sql}
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
    accessories_items = _accessories_text_for_ingreso(ingreso_id)
    if accessories_items:
        existing = _clean(row.get("accesorios"))
        row["accesorios"] = "; ".join(part for part in (existing, accessories_items) if part)
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


def _equipment_print_name(context: dict[str, Any], variante: str) -> str:
    model_text = " ".join(
        part
        for part in (
            _clean(context.get("modelo")),
            variante,
        )
        if part
    )
    return " ".join(
        part
        for part in (
            _clean(context.get("tipo_equipo")),
            _clean(context.get("marca")),
            model_text,
        )
        if part
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
    mapped_article_code = _clean((mapping or {}).get("article_code"))
    allow_generic_article = bool(getattr(settings, "BEJERMAN_RIS_ALLOW_GENERIC_ARTICLE", False))
    article_code = mapped_article_code or (
        _clean(getattr(settings, "BEJERMAN_RIS_GENERIC_ARTICLE_CODE", "SERVICIO")) if allow_generic_article else ""
    )
    mapped_article_name = _clean((mapping or {}).get("article_description")) or _clean(getattr(settings, "BEJERMAN_RIS_GENERIC_ARTICLE_NAME", "Equipo recibido para servicio técnico"))
    equipment_label = _equipment_print_name(context, variante)
    article_name = equipment_label or mapped_article_name
    document_profile = _ingress_document_profile(context, company.key)
    issue_date = _date_iso(context.get("fecha_ingreso")) or timezone.localdate().isoformat()
    serial_number = _clean(context.get("numero_serie"))
    serial = serial_number or _clean(context.get("numero_interno"))
    return {
        "requestId": f"reparaciones-ingreso-{context['id']}",
        "ingresoId": context["id"],
        "companyKey": company.key,
        "companyLabel": company.label,
        "bejermanCompany": company.bejerman_company,
        "documentProfile": document_profile,
        "issueDate": issue_date,
        "customerCode": customer_code,
        "customerName": _clean(context.get("customer_name")),
        "sellerCode": _clean(getattr(settings, "BEJERMAN_RIS_SELLER_CODE", "ADM")),
        "paymentTermCode": _clean(getattr(settings, "BEJERMAN_RIS_PAYMENT_TERM", "30")),
        "notes": f"OS {os_label(context['id'])}",
        "equipment": {
            "articleCode": article_code,
            "articleSource": "mapping" if mapped_article_code else ("generic" if allow_generic_article and article_code else ""),
            "articleMapped": bool(mapped_article_code),
            "mappingRequired": not bool(mapped_article_code),
            "articleName": article_name,
            "mappedArticleName": mapped_article_name,
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


def _build_batch_payload(ingreso_ids: list[int]) -> dict[str, Any]:
    contexts = [_ingreso_context(ingreso_id) for ingreso_id in ingreso_ids]
    if not contexts:
        raise BejermanRisError("No hay ingresos para emitir RIS")
    payloads = [_build_payload(context) for context in contexts]
    company_keys = {_clean(payload.get("companyKey")) for payload in payloads}
    customer_codes = {_clean(payload.get("customerCode")) for payload in payloads}
    if len(company_keys) != 1:
        raise BejermanRisError("Todos los ingresos del lote deben usar la misma empresa Bejerman")
    if len(customer_codes) != 1:
        raise BejermanRisError("Todos los ingresos del lote deben pertenecer al mismo cliente Bejerman")
    profile_mismatch = _document_profile_mismatch_message(payloads)
    if profile_mismatch:
        raise BejermanRisError(profile_mismatch)
    first = payloads[0]
    equipments: list[dict[str, Any]] = []
    for context, payload in zip(contexts, payloads):
        equipment = dict(payload.get("equipment") or {})
        equipment["ingresoId"] = context["id"]
        equipment["osLabel"] = os_label(context["id"])
        equipments.append(equipment)
    os_list = ", ".join(f"OS {os_label(context['id'])}" for context in contexts)
    return {
        **first,
        "requestId": f"reparaciones-ingreso-lote-{'-'.join(str(item) for item in ingreso_ids)}",
        "ingresoId": ingreso_ids[0],
        "ingresoIds": ingreso_ids,
        "notes": os_list,
        "equipment": equipments[0],
        "equipments": equipments,
    }


def _issue(
    code: str,
    message: str,
    *,
    scope: str = "ris",
    item_index: int | None = None,
    field: str = "",
    severity: str = "error",
    candidates: list[dict[str, Any]] | None = None,
    fix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "scope": scope,
        "field": field,
        "message": message,
        "candidates": candidates or [],
        "fix": fix or {},
    }
    if item_index is not None:
        payload["item_index"] = item_index
    return payload


def _payload_equipment_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    equipments = payload.get("equipments")
    if isinstance(equipments, list) and equipments:
        return [item for item in equipments if isinstance(item, dict)]
    equipment = payload.get("equipment")
    return [equipment] if isinstance(equipment, dict) else []


def _is_stock_ingress_payload(payload: dict[str, Any]) -> bool:
    profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    document_type = _clean_upper(profile.get("type"))
    return bool(profile.get("updateStock")) and sdk_uses_negative_article_quantity(document_type)


def _payload_equipment_identifiers(payload: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    for equipment in _payload_equipment_items(payload):
        for key in ("serial", "internalNumber"):
            value = _clean(equipment.get(key))
            if value and value not in identifiers:
                identifiers.append(value)
    return identifiers


def _payload_ingreso_ids(payload: dict[str, Any]) -> list[int]:
    ids = _ingreso_ids_from_payload(payload)
    raw = payload.get("ingresoId")
    try:
        ingreso_id = int(raw)
    except (TypeError, ValueError):
        ingreso_id = 0
    if ingreso_id > 0 and ingreso_id not in ids:
        ids.append(ingreso_id)
    return ids


def _existing_ingreso_ids(ingreso_ids: list[int]) -> list[int]:
    clean_ids: list[int] = []
    for raw in ingreso_ids or []:
        try:
            ingreso_id = int(raw)
        except (TypeError, ValueError):
            continue
        if ingreso_id > 0 and ingreso_id not in clean_ids:
            clean_ids.append(ingreso_id)
    if not clean_ids:
        return []
    rows = q("SELECT id FROM ingresos WHERE id = ANY(%s)", [clean_ids])
    existing = {int(row["id"]) for row in rows or []}
    return [ingreso_id for ingreso_id in clean_ids if ingreso_id in existing]


def _stored_register_payload(rows: list[dict[str, Any]], document: dict[str, str]) -> dict[str, Any]:
    for row in rows:
        payload = _json_dict(row.get("request_payload"))
        if not payload:
            continue
        manual = _clean(payload.get("manualRemitoNumber") or row.get("manual_remito_number") or row.get("remito_number"))
        if manual and manual != document["remitoNumber"]:
            continue
        if (payload.get("documentMode") or row.get("document_mode")) != RIS_DOCUMENT_MODE_REGISTER:
            continue
        return payload
    return {}


def _apply_stored_register_payload(batch_payload: dict[str, Any], stored_payload: dict[str, Any]) -> dict[str, Any]:
    if not stored_payload:
        return batch_payload
    out = dict(batch_payload)
    stored_profile = stored_payload.get("documentProfile")
    if isinstance(stored_profile, dict):
        out["documentProfile"] = {
            **(out.get("documentProfile") if isinstance(out.get("documentProfile"), dict) else {}),
            **stored_profile,
        }
        if "updateStock" in stored_profile:
            out["preserveRegisteredDocumentStockFlag"] = True
    for key in (
        "issueDate",
        "sellerCode",
        "paymentTermCode",
        "companyKey",
        "companyLabel",
        "bejermanCompany",
        "customerCode",
        "customerName",
    ):
        value = stored_payload.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def _ingress_duplicate_document_types(profile: dict[str, Any] | None) -> list[str]:
    raw = _clean(getattr(settings, "BEJERMAN_RIS_DUPLICATE_CHECK_TYPES", "RIS,RDA,RDN"))
    types = [_clean_upper(item) for item in raw.split(",") if _clean_upper(item)]
    profile_type = _clean_upper((profile or {}).get("type"))
    if profile_type and profile_type not in types:
        types.insert(0, profile_type)
    return types


def _local_ingress_equipment_remito_duplicate(payload: dict[str, Any], ingreso_ids: list[int]) -> dict[str, Any] | None:
    if not _has_ris_schema():
        return None
    identifiers = _payload_equipment_identifiers(payload)
    if not identifiers:
        return None
    profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    document_types = _ingress_duplicate_document_types(profile)
    issue_date = _date_iso(payload.get("issueDate")) or timezone.localdate().isoformat()
    excluded = [int(item) for item in ingreso_ids or [] if isinstance(item, int) or str(item).isdigit()]
    exclude_sql = "AND r.ingreso_id <> ALL(%s)" if excluded else ""
    params: list[Any] = [document_types, issue_date, identifiers, identifiers]
    if excluded:
        params.append(excluded)
    row = q(
        f"""
        SELECT r.ingreso_id,
               r.remito_number,
               r.comprobante_tipo,
               r.comprobante_letra,
               r.comprobante_pto_venta,
               r.comprobante_numero,
               COALESCE(d.numero_serie, '') AS numero_serie,
               COALESCE(d.numero_interno, '') AS numero_interno,
               COALESCE(r.issue_date, t.fecha_ingreso)::date AS issue_date
          FROM bejerman_ingreso_remitos r
          JOIN ingresos t ON t.id = r.ingreso_id
          JOIN devices d ON d.id = t.device_id
         WHERE COALESCE(r.comprobante_tipo, '') = ANY(%s)
           AND COALESCE(r.issue_date, t.fecha_ingreso)::date = %s::date
           AND (
             COALESCE(d.numero_serie, '') = ANY(%s)
             OR COALESCE(d.numero_interno, '') = ANY(%s)
           )
           AND r.status IN ('running', 'generated')
           {exclude_sql}
         ORDER BY r.generated_at DESC NULLS LAST, r.updated_at DESC NULLS LAST
         LIMIT 1
        """,
        params,
        one=True,
    )
    if not row:
        return None
    identifier = _clean(row.get("numero_serie")) or _clean(row.get("numero_interno"))
    return {
        "source": "local",
        "identifier": identifier,
        "issueDate": _date_iso(row.get("issue_date")) or issue_date,
        "remitoNumber": _clean(row.get("remito_number")),
        "ingresoId": row.get("ingreso_id"),
    }


def _detail_record_for_sales_header(client: BejermanSDKClient, header: dict[str, Any]) -> dict[str, Any]:
    if isinstance(header.get("Comprobante_Items"), list):
        return header
    comprobante_id = comprobante_id_of(header)
    if not comprobante_id:
        return header
    detail = first_record(client.detalle_comprobante_ventas(comprobante_id).get("DatosJSON")) or {}
    return {**header, **detail} if detail else header


def _record_document_label(record: dict[str, Any]) -> str:
    tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo")))
    letter = as_string(first_value(record, ("Comprobante_Letra", "Letra"))) or "R"
    point = as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
    number = as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante")))
    if tipo and point and number:
        return format_remito_number(tipo, letter, point, number)
    return as_string(first_value(record, ("Comprobante_NumeroCompleto", "NumeroCompleto", "Documento")))


def _record_matches_equipment_identifier(record: dict[str, Any], identifier: str) -> bool:
    normalized = normalize_serial_for_lookup(identifier)
    if len(normalized) < 4:
        return False
    raw_items = record.get("Comprobante_Items")
    items = raw_items if isinstance(raw_items, list) else [record]
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        partida = as_string(
            first_value(
                raw_item,
                (
                    "Item_Partida",
                    "Partida",
                    "Comprobante_ArtPartida",
                    "Art_Partida",
                    "ArtPartida",
                ),
            )
        )
        if partida and normalize_serial_for_lookup(partida) == normalized:
            return True
        text = " ".join(
            as_string(first_value(raw_item, (field,)))
            for field in (
                "Item_DescripArticulo",
                "Item_DescripcionArticulo",
                "Descripcion",
                "Detalle",
                "Observaciones",
            )
        )
        if text and normalized in normalize_serial_for_lookup(text):
            return True
    return False


def _remote_ingress_equipment_remito_duplicate(payload: dict[str, Any], client: BejermanSDKClient) -> dict[str, Any] | None:
    identifiers = _payload_equipment_identifiers(payload)
    if not identifiers:
        return None
    profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    issue_date = _date_iso(payload.get("issueDate")) or timezone.localdate().isoformat()
    for document_type in _ingress_duplicate_document_types(profile):
        filters = [
            bejerman_filter("Comprobante_Tipo", "IGUAL", document_type),
            bejerman_filter("Comprobante_FechaEmision", "MAYOR O IGUAL", issue_date),
            bejerman_filter("Comprobante_FechaEmision", "MENOR O IGUAL", issue_date),
        ]
        response = client.list_comprobantes_ventas(filters)
        for header in records_from_response(response):
            detail = _detail_record_for_sales_header(client, header)
            record_type = _clean_upper(first_value(detail, ("Comprobante_Tipo", "TipoComprobante", "Tipo")))
            if record_type and record_type != document_type:
                continue
            for identifier in identifiers:
                if not _record_matches_equipment_identifier(detail, identifier):
                    continue
                return {
                    "source": "remote",
                    "identifier": identifier,
                    "issueDate": issue_date,
                    "remitoNumber": _record_document_label(detail) or _record_document_label(header),
                    "raw": detail,
                }
    return None


def _equipment_remito_duplicate_message(duplicate: dict[str, Any]) -> str:
    remito = _clean(duplicate.get("remitoNumber")) or "una emisión en curso"
    identifier = _clean(duplicate.get("identifier")) or "seleccionado"
    issue_date = _clean(duplicate.get("issueDate")) or "-"
    return (
        f"Ya existe un remito de ingreso para el equipo {identifier} en la fecha {issue_date}: {remito}. "
        "No se emite otro para evitar duplicarlo."
    )


def _validate_equipment_remito_not_duplicated(
    payload: dict[str, Any],
    issues: list[dict[str, Any]],
    *,
    ingreso_ids: list[int] | None = None,
    actor_user_id: int | None = None,
) -> None:
    if not _payload_equipment_identifiers(payload):
        return
    local_duplicate = _local_ingress_equipment_remito_duplicate(payload, ingreso_ids or [])
    if local_duplicate:
        issues.append(
            _issue(
                "INGRESO_REMITO_EQUIPMENT_DUPLICATE_LOCAL",
                _equipment_remito_duplicate_message(local_duplicate),
                scope="remito",
                field="equipo.numero_serie",
            )
        )
        return
    company_key = _clean(payload.get("companyKey")) or "SEPID"
    try:
        remote_duplicate = _remote_ingress_equipment_remito_duplicate(
            payload,
            BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id),
        )
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, ValueError) as exc:
        issues.append(
            _issue(
                "BEJERMAN_UNAVAILABLE",
                f"No se pudo validar si el equipo ya tiene remito en Bejerman: {exc}",
                scope="bejerman",
            )
        )
        return
    if remote_duplicate:
        issues.append(
            _issue(
                "INGRESO_REMITO_EQUIPMENT_DUPLICATE_REMOTE",
                _equipment_remito_duplicate_message(remote_duplicate),
                scope="remito",
                field="equipo.numero_serie",
            )
        )


def _ensure_equipment_remito_not_duplicated(
    payload: dict[str, Any],
    ingreso_ids: list[int],
    client: BejermanSDKClient,
) -> None:
    local_duplicate = _local_ingress_equipment_remito_duplicate(payload, ingreso_ids)
    duplicate = local_duplicate or _remote_ingress_equipment_remito_duplicate(payload, client)
    if duplicate:
        raise BejermanSdkResponseError(_equipment_remito_duplicate_message(duplicate))


def _has_errors(issues: list[dict[str, Any]]) -> bool:
    return any((issue.get("severity") or "error") == "error" for issue in issues)


def _customer_code_from_record(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente")))


def _customer_name_from_record(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Nombre", "Cliente")))


def _customer_cuit_from_record(record: dict[str, Any]) -> str:
    return _digits_only(first_value(record, ("Cliente_NroDocumento", "Cliente_CUIT", "Cliente_Cuit", "CUIT", "Cuit", "TaxId")))


def _customer_iva_from_record(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Cliente_SitIVA", "Cliente_SituacionIVACodigo", "SituacionIVACodigo", "SituacionIVA")))


def _customer_province_from_record(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Cliente_Provincia", "Cliente_CodigoProvincia", "Provincia", "CodigoProvincia")))


def _name_score(left: Any, right: Any) -> float:
    left_norm = _normalize_key(left)
    right_norm = _normalize_key(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    if left_norm in right_norm or right_norm in left_norm:
        ratio = max(ratio, 0.86)
    return ratio


def _customer_candidate_payload(record: dict[str, Any], local: dict[str, Any] | None = None, query: str = "") -> dict[str, Any]:
    local = local or {}
    code = _customer_code_from_record(record)
    name = _customer_name_from_record(record)
    cuit = _customer_cuit_from_record(record)
    local_code = _clean_upper(local.get("customer_code") or local.get("cod_empresa"))
    local_name = _clean(local.get("customer_name") or local.get("razon_social"))
    local_cuit = _digits_only(local.get("cuit"))
    score = _name_score(local_name or query, name)
    reasons: list[str] = []
    if code and local_code and _clean_upper(code) == local_code:
        reasons.append("Código exacto")
        score = max(score, 1.0)
    if cuit and local_cuit and cuit == local_cuit:
        reasons.append("CUIT exacto")
        score = max(score, 0.98)
    if local_name and _normalize_key(local_name) == _normalize_key(name):
        reasons.append("Razón social exacta")
        score = max(score, 0.96)
    elif score >= 0.82:
        reasons.append("Razón social similar")
    query_norm = _normalize_key(query)
    query_digits = _digits_only(query)
    if not reasons and query_norm:
        flat = _normalize_key(f"{code} {name} {cuit}")
        if query_norm in flat or (query_digits and query_digits in _digits_only(cuit)):
            reasons.append("Coincide con la búsqueda")
            score = max(score, 0.7)
    return {
        "code": _clean(code),
        "name": _clean(name),
        "cuit": cuit,
        "iva": _clean(_customer_iva_from_record(record)),
        "province": _clean(_customer_province_from_record(record)),
        "score": round(score, 3),
        "reasons": reasons,
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, int]:
    score = float(candidate.get("score") or 0)
    complete = int(bool(candidate.get("iva")) and bool(candidate.get("cuit")) and _clean(candidate.get("province")) not in {"", "000"})
    if "Código exacto" in candidate.get("reasons", []):
        score += 0.2
    if "CUIT exacto" in candidate.get("reasons", []):
        score += 0.15
    return (score, complete)


def _customer_candidates(local: dict[str, Any], records: list[dict[str, Any]], *, limit: int = 5, query: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for record in records:
        candidate = _customer_candidate_payload(record, local=local, query=query)
        if candidate.get("reasons") or float(candidate.get("score") or 0) >= 0.82:
            out.append(candidate)
    out.sort(key=_candidate_sort_key, reverse=True)
    return out[:limit]


def _customer_payload_from_record(record: dict[str, Any]) -> dict[str, str]:
    return {
        "code": _clean(_customer_code_from_record(record)),
        "name": _clean(_customer_name_from_record(record)),
        "cuit": _customer_cuit_from_record(record),
        "iva": _clean(_customer_iva_from_record(record)),
        "province": _clean(_customer_province_from_record(record)),
    }


def _customer_sql_fields() -> str:
    cuit_sql = "COALESCE(c.cuit, '') AS cuit" if _has_table_column("customers", "cuit") else "'' AS cuit"
    return f"""
          c.id AS customer_id,
          COALESCE(c.cod_empresa, '') AS customer_code,
          COALESCE(c.razon_social, '') AS customer_name,
          {cuit_sql}
    """


def _customer_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    customer = payload.get("cliente") if isinstance(payload.get("cliente"), dict) else {}
    customer_id = customer.get("id") or payload.get("customer_id") or payload.get("cliente_id")
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return None
    return q(
        f"""
        SELECT {_customer_sql_fields()}
          FROM customers c
         WHERE c.id = %s
        """,
        [customer_id],
        one=True,
    )


def _model_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    equipment = payload.get("equipo") if isinstance(payload.get("equipo"), dict) else {}
    model_id = equipment.get("modelo_id") or equipment.get("model_id")
    try:
        model_id = int(model_id)
    except (TypeError, ValueError):
        return None
    return q(
        """
        SELECT
          m.id AS model_id,
          COALESCE(m.nombre, '') AS modelo,
          COALESCE(m.tipo_equipo, '') AS tipo_equipo,
          COALESCE(m.variante, '') AS modelo_variante,
          b.id AS marca_id,
          COALESCE(b.nombre, '') AS marca
        FROM models m
        LEFT JOIN marcas b ON b.id = m.marca_id
        WHERE m.id = %s
        """,
        [model_id],
        one=True,
    )


def _accessories_text_from_payload(payload: dict[str, Any]) -> str:
    items = payload.get("accesorios_items")
    if not isinstance(items, list) or not items:
        return _clean(payload.get("accesorios"))
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = _clean(item.get("descripcion") or item.get("accesorio_nombre"))
        ref = _clean(item.get("referencia"))
        acc_id = item.get("accesorio_id")
        if not label and acc_id:
            try:
                row = q("SELECT nombre FROM catalogo_accesorios WHERE id=%s", [int(acc_id)], one=True)
            except Exception:
                row = None
            label = _clean((row or {}).get("nombre"))
        if not label and not ref:
            continue
        parts.append(f"{label} (ref: {ref})" if ref else label)
    return "; ".join(parts)


def _context_from_payload(payload: dict[str, Any], *, item_index: int | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return None, [_issue("INVALID_PAYLOAD", "La carga del ingreso no es válida.", item_index=item_index)]
    customer = _customer_from_payload(payload)
    if not customer:
        issues.append(
            _issue(
                "CUSTOMER_REQUIRED",
                "Debe seleccionar un cliente válido antes de validar el RIS.",
                scope="cliente",
                field="cliente",
                item_index=item_index,
            )
        )
    model = _model_from_payload(payload)
    if not model:
        issues.append(
            _issue(
                "MODEL_REQUIRED",
                "Debe seleccionar un modelo válido antes de validar el RIS.",
                scope="equipo",
                field="equipo.modelo_id",
                item_index=item_index,
            )
        )
    equipment = payload.get("equipo") if isinstance(payload.get("equipo"), dict) else {}
    if not _clean(equipment.get("numero_serie")) and not _clean(equipment.get("numero_interno")):
        company_key = _clean(payload.get("empresa_bejerman") or payload.get("empresa_facturar") or "SEPID")
        document_profile = _ingress_document_profile({"motivo": _clean(payload.get("motivo"))}, company_key)
        document_type = _clean_upper(document_profile.get("type") or "RIS")
        check_payload = {"documentProfile": document_profile}
        if _is_stock_ingress_payload(check_payload):
            issues.append(
                _issue(
                    f"{document_type}_PARTIDA_REQUIRED",
                    f"Para {document_type} con actualización de stock, Bejerman requiere una partida/identificador del equipo. Cargá N° serie o número interno antes de emitir.",
                    scope="equipo",
                    field="equipo.numero_serie",
                    item_index=item_index,
                )
            )
        else:
            issues.append(
                _issue(
                    "EQUIPMENT_IDENTIFIER_REQUIRED",
                    "Debe completar número de serie o número interno para identificar el equipo en el RIS.",
                    scope="equipo",
                    field="equipo.numero_serie",
                    item_index=item_index,
                )
            )
    if not _clean(payload.get("motivo")):
        issues.append(
            _issue(
                "REPAIR_REASON_REQUIRED",
                "Debe seleccionar un motivo de ingreso para armar el RIS.",
                scope="ingreso",
                field="motivo",
                item_index=item_index,
            )
        )
    if issues:
        return None, issues
    assert customer is not None and model is not None
    device = _device_from_payload_equipment(equipment) or {}
    input_serial = _clean(equipment.get("numero_serie"))
    input_internal = _clean(equipment.get("numero_interno"))
    normalized_input_internal = _normalize_mg_code(input_internal)
    if not normalized_input_internal and re.fullmatch(r"\d{1,4}", input_internal):
        normalized_input_internal = f"MG {input_internal.zfill(4)}"
    context = {
        "id": f"preflight-{(item_index or 0) + 1}",
        "motivo": _clean(payload.get("motivo")),
        "fecha_ingreso": payload.get("fecha_ingreso") or timezone.localdate().isoformat(),
        "accesorios": _accessories_text_from_payload(payload),
        "comentarios": _clean(payload.get("comentarios")),
        "equipo_variante": _clean(payload.get("equipo_variante")),
        "remito_ingreso": _clean(payload.get("remito_ingreso")),
        "empresa_bejerman": _clean(payload.get("empresa_bejerman") or "SEPID"),
        "empresa_facturar": _clean(payload.get("empresa_facturar") or "SEPID"),
        "customer_id": customer.get("customer_id"),
        "customer_code": customer.get("customer_code") or "",
        "customer_name": customer.get("customer_name") or "",
        "cuit": customer.get("cuit") or "",
        "device_id": device.get("device_id"),
        "device_customer_id": device.get("device_customer_id") or customer.get("customer_id"),
        "numero_serie": input_serial or _clean(device.get("numero_serie")),
        "numero_interno": normalized_input_internal or input_internal or _clean(device.get("numero_interno")),
        "n_de_control": _clean(device.get("n_de_control")),
        "mg_estado": _clean(device.get("mg_estado")) or "activo",
        "alquilado": bool(device.get("alquilado")),
        "alquiler_a": _clean(device.get("alquiler_a")),
        "marca_id": model.get("marca_id"),
        "marca": model.get("marca") or "",
        "model_id": model.get("model_id"),
        "modelo": model.get("modelo") or "",
        "tipo_equipo": model.get("tipo_equipo") or "",
        "device_variante": _clean(device.get("device_variante")),
        "modelo_variante": model.get("modelo_variante") or "",
        "recibido_por_nombre": "",
    }
    return context, []


def _payload_items_from_request(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("items"), list):
        shared_keys = ("cliente", "propietario", "empresa_bejerman", "empresa_facturar", "fecha_ingreso", "ubicacion_id")
        items: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            payload = dict(item or {})
            for key in shared_keys:
                if key in data and key not in payload:
                    payload[key] = data.get(key)
            items.append(payload)
        return items
    return [data]


def _payloads_from_contexts(contexts: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, context in enumerate(contexts):
        try:
            payloads.append(_build_payload(context))
        except BejermanRisError as exc:
            issues.append(_issue("RIS_PAYLOAD_INVALID", str(exc), item_index=index if len(contexts) > 1 else None))
    return payloads


def _combined_payload(payloads: list[dict[str, Any]], issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not payloads:
        return None
    company_keys = {_clean(payload.get("companyKey")) for payload in payloads}
    customer_codes = {_clean_upper(payload.get("customerCode")) for payload in payloads}
    if len(company_keys) != 1:
        issues.append(_issue("RIS_BATCH_COMPANY_MISMATCH", "Todos los equipos del RIS deben usar la misma empresa Bejerman."))
    if len(customer_codes) != 1:
        issues.append(_issue("RIS_BATCH_CUSTOMER_MISMATCH", "Todos los equipos del RIS deben pertenecer al mismo cliente Bejerman."))
    profile_mismatch = _document_profile_mismatch_message(payloads)
    if profile_mismatch:
        issues.append(
            _issue(
                "INGRESO_DOCUMENT_PROFILE_MISMATCH",
                profile_mismatch,
                scope="remito",
                field="motivo",
            )
        )
    first = payloads[0]
    if len(payloads) == 1:
        return first
    equipments: list[dict[str, Any]] = []
    for index, payload in enumerate(payloads):
        equipment = dict(payload.get("equipment") or {})
        equipment["ingresoId"] = payload.get("ingresoId")
        equipment["osLabel"] = f"preflight-{index + 1}"
        equipments.append(equipment)
    return {
        **first,
        "requestId": "reparaciones-ingreso-preflight-lote",
        "ingresoIds": [payload.get("ingresoId") for payload in payloads],
        "notes": "Preflight RIS",
        "equipment": equipments[0],
        "equipments": equipments,
    }


def _stock_article_candidates(
    serial: str,
    *,
    limit: int = 5,
    actor_user_id: int | None = None,
) -> list[dict[str, Any]]:
    if not serial:
        return []
    client = SyncBejermanSDKClient(actor_user_id=actor_user_id)
    response = client.stock_by_deposit_partida("", serial)
    by_code: dict[str, dict[str, Any]] = {}
    for record in _records_for_partida_any_quantity(response, serial):
        code = _clean(_article_code_from_record(record))
        if not code or code in by_code:
            continue
        by_code[code] = {
            "article_code": code,
            "article_description": _clean(_article_description_from_record(record)),
            "deposit": _clean(_deposit_from_record(record)),
            "partida": _clean(_partida_from_record(record)),
            "source": "stock_partida",
        }
    return list(by_code.values())[:limit]


def _stock_article_candidates_for_payload(
    payload: dict[str, Any],
    context: dict[str, Any],
    *,
    actor_user_id: int | None = None,
) -> list[dict[str, Any]]:
    if _is_stock_ingress_payload(payload):
        return []
    equipment = payload.get("equipment") or {}
    return _stock_article_candidates(
        _clean(equipment.get("serial")) or _clean(equipment.get("internalNumber")),
        actor_user_id=actor_user_id,
    )


def _article_mapping_fix_from_candidates(
    candidates: list[dict[str, Any]],
    context: dict[str, Any],
    variante: str,
) -> dict[str, Any]:
    if not context.get("model_id"):
        return {}
    fix = {
        "type": "article_mapping",
        "model_id": context.get("model_id"),
        "variante": variante,
    }
    if len(candidates) == 1:
        fix.update(
            {
                "article_code": candidates[0].get("article_code") or "",
                "article_description": candidates[0].get("article_description") or "",
            }
        )
    return fix


def _validate_document_partida(
    payload: dict[str, Any],
    context: dict[str, Any],
    issues: list[dict[str, Any]],
    *,
    item_index: int | None = None,
) -> None:
    profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    document_type = _clean_upper(profile.get("type") or "RIS")
    if not _is_stock_ingress_payload(payload):
        return
    equipment = payload.get("equipment") or {}
    if resolve_equipment_partida(equipment):
        return
    issues.append(
        _issue(
            f"{document_type}_PARTIDA_REQUIRED",
            f"Para {document_type} con actualización de stock, Bejerman requiere una partida/identificador del equipo. Cargá N° serie o número interno antes de emitir.",
            scope="equipo",
            field="equipo.numero_serie",
            item_index=item_index,
        )
    )


def _validate_article(
    payload: dict[str, Any],
    context: dict[str, Any],
    issues: list[dict[str, Any]],
    *,
    item_index: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any] | None:
    equipment = payload.get("equipment") or {}
    article_code = _clean(equipment.get("articleCode"))
    variante = _clean(context.get("equipo_variante")) or _clean(context.get("device_variante")) or _clean(context.get("modelo_variante"))
    article_context = {
        "model_id": context.get("model_id"),
        "marca": context.get("marca") or "",
        "modelo": context.get("modelo") or "",
        "variante": variante,
    }
    mapping_required = bool(equipment.get("mappingRequired")) and not bool(
        getattr(settings, "BEJERMAN_RIS_ALLOW_GENERIC_ARTICLE", False)
    )
    if mapping_required:
        candidates: list[dict[str, Any]] = []
        try:
            candidates = _stock_article_candidates_for_payload(payload, context, actor_user_id=actor_user_id)
        except Exception:
            candidates = []
        message = "El modelo/variante no tiene artículo Bejerman mapeado. Resuelva el mapeo antes de emitir el comprobante."
        profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
        if _is_stock_ingress_payload(payload):
            document_type = _clean_upper(profile.get("type")) or "remito"
            deposit = _clean_upper(profile.get("deposit")) or "STR"
            message = (
                f"El modelo/variante no tiene artículo Bejerman mapeado. Para {document_type} con ingreso de stock "
                f"a {deposit} no se busca partida con stock positivo previo: la partida puede ser el número de serie "
                "o interno del equipo. Mapeá el artículo antes de emitir."
            )
        issues.append(
            _issue(
                "BEJERMAN_ARTICLE_MAPPING_REQUIRED",
                message,
                scope="articulo",
                field="equipo.modelo_id",
                item_index=item_index,
                candidates=candidates,
                fix=_article_mapping_fix_from_candidates(candidates, context, variante),
            )
        )
        return None
    if not article_code:
        issues.append(
            _issue(
                "BEJERMAN_ARTICLE_MISSING",
                "No se pudo determinar el artículo Bejerman para el equipo.",
                scope="articulo",
                field="equipo.modelo_id",
                item_index=item_index,
            )
        )
        return None
    try:
        return validate_bejerman_article_choice(
            article_code,
            client=SyncBejermanSDKClient(actor_user_id=actor_user_id),
            context=article_context,
        )
    except (BejermanBlockedError, BejermanConfigError, BejermanTransientError, ValueError) as exc:
        candidates: list[dict[str, Any]] = []
        try:
            candidates = _stock_article_candidates_for_payload(payload, context, actor_user_id=actor_user_id)
        except Exception:
            candidates = []
        fix = {}
        fix = _article_mapping_fix_from_candidates(candidates, context, variante)
        issues.append(
            _issue(
                "BEJERMAN_ARTICLE_INVALID",
                f"Bejerman no reconoce el artículo {article_code}: {exc}",
                scope="articulo",
                field="equipo.modelo_id",
                item_index=item_index,
                candidates=candidates,
                fix=fix,
            )
        )
        return None


def _validate_customer(
    payload: dict[str, Any],
    context: dict[str, Any],
    issues: list[dict[str, Any]],
    *,
    item_index: int | None = None,
    client_cache: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
    actor_user_id: int | None = None,
) -> dict[str, str] | None:
    customer_code = _clean(payload.get("customerCode"))
    local = {
        "customer_id": context.get("customer_id"),
        "customer_code": context.get("customer_code") or "",
        "customer_name": context.get("customer_name") or "",
        "cuit": context.get("cuit") or "",
    }
    if not customer_code:
        issues.append(
            _issue(
                "BEJERMAN_CUSTOMER_CODE_MISSING",
                "El cliente seleccionado no tiene código Bejerman.",
                scope="cliente",
                field="cliente.cod_empresa",
                item_index=item_index,
            )
        )
        return None
    try:
        company_key = _clean(payload.get("companyKey")) or "SEPID"
        if company_key not in client_cache:
            client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
            response = client.list_clientes()
            client_cache[company_key] = (response, records_from_response(response))
        response, records = client_cache[company_key]
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, ValueError) as exc:
        issues.append(
            _issue(
                "BEJERMAN_UNAVAILABLE",
                f"No se pudo consultar clientes en Bejerman: {exc}",
                scope="bejerman",
                item_index=item_index,
            )
        )
        return None
    record = find_bejerman_client_record(response, customer_code)
    if not record:
        candidates = _customer_candidates(local, records, query=" ".join([customer_code, local["customer_name"], local["cuit"]]))
        fix = {"type": "customer", "customer_id": local.get("customer_id"), "company_key": company_key} if candidates and local.get("customer_id") else {}
        issues.append(
            _issue(
                "BEJERMAN_CUSTOMER_NOT_FOUND",
                f"El código de cliente {customer_code} no existe en Bejerman.",
                scope="cliente",
                field="cliente.cod_empresa",
                item_index=item_index,
                candidates=candidates,
                fix=fix,
            )
        )
        return None
    fields = build_customer_document_fields(response, customer_code) or {}
    missing: list[str] = []
    if not _clean(fields.get("Cliente_SitIVA")):
        missing.append("situación de IVA")
    if not _digits_only(fields.get("Cliente_NroDocumento")):
        missing.append("CUIT")
    province = _clean(fields.get("Cliente_Provincia"))
    if not province or province == "000":
        missing.append("provincia")
    if missing:
        candidates = _customer_candidates(local, records, query=" ".join([local["customer_name"], local["cuit"]]))
        current = _customer_candidate_payload(record, local=local)
        if current and _clean_upper(current.get("code")) not in {_clean_upper(item.get("code")) for item in candidates}:
            candidates.insert(0, current)
        fix = {"type": "customer", "customer_id": local.get("customer_id"), "company_key": company_key} if candidates and local.get("customer_id") else {}
        issues.append(
            _issue(
                "BEJERMAN_CUSTOMER_FISCAL_INCOMPLETE",
                f"Al cliente Bejerman {customer_code} le falta {', '.join(missing)}.",
                scope="cliente",
                field="cliente.cod_empresa",
                item_index=item_index,
                candidates=candidates[:5],
                fix=fix,
            )
        )
        return None
    return fields


def _preview_payload(payload: dict[str, Any] | None, built: dict[str, Any] | None = None) -> dict[str, Any]:
    if not payload:
        return {"items": [], "lineCount": 0}
    equipments = payload.get("equipments") if isinstance(payload.get("equipments"), list) else [payload.get("equipment") or {}]
    items = []
    for equipment in equipments:
        profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
        update_stock = bool(profile.get("updateStock"))
        partida = resolve_equipment_partida(equipment)
        items.append(
            {
                "articleCode": _clean(equipment.get("articleCode")),
                "articleName": _clean(equipment.get("articleName")),
                "serial": _clean(equipment.get("serial")),
                "internalNumber": _clean(equipment.get("internalNumber")),
                "equipmentLabel": _clean(equipment.get("equipmentLabel")),
                "deposit": _clean(profile.get("deposit")) if update_stock else "",
                "partida": partida if update_stock else "",
                "partidaPolicy": "identifier" if update_stock else "",
            }
        )
    return {
        "companyKey": _clean(payload.get("companyKey")),
        "customerCode": _clean(payload.get("customerCode")),
        "customerName": _clean(payload.get("customerName")),
        "documentProfile": payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else None,
        "document_profile": payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else None,
        "lineCount": int((built or {}).get("lineCount") or len(items)),
        "items": items,
    }


def _preflight_from_contexts(
    contexts: list[dict[str, Any]],
    user_id: int | None = None,
    *,
    skip_equipment_duplicate_check: bool = False,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    payloads = _payloads_from_contexts(contexts, issues)
    combined = _combined_payload(payloads, issues)
    client_cache: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    customer_fields = None
    for index, (context, payload) in enumerate(zip(contexts, payloads)):
        item_index = index if len(contexts) > 1 else None
        fields = _validate_customer(
            payload,
            context,
            issues,
            item_index=item_index,
            client_cache=client_cache,
            actor_user_id=user_id,
        )
        if fields and customer_fields is None:
            customer_fields = fields
        _validate_document_partida(payload, context, issues, item_index=item_index)
        _validate_article(payload, context, issues, item_index=item_index, actor_user_id=user_id)
    if combined and not _has_errors(issues) and not skip_equipment_duplicate_check:
        _validate_equipment_remito_not_duplicated(
            combined,
            issues,
            ingreso_ids=_payload_ingreso_ids(combined),
            actor_user_id=user_id,
        )
    built = None
    document_profile = combined.get("documentProfile") if isinstance(combined, dict) else None
    document_type = _clean((document_profile or {}).get("type")) or "RIS"
    if combined and customer_fields and not _has_errors(issues):
        try:
            built = build_service_ingress_comprobante(combined, customer_fields)
        except Exception as exc:
            issues.append(_issue("BEJERMAN_COMPROBANTE_INVALID", f"No se pudo armar el comprobante {document_type}: {exc}"))
    return {
        "can_emit": not _has_errors(issues),
        "issues": issues,
        "preview": _preview_payload(combined, built),
        "document_profile": document_profile,
        "documentProfile": document_profile,
        "detail": f"{document_type} validado correctamente." if not _has_errors(issues) else "La validación previa del comprobante de ingreso encontró problemas.",
    }


def preflight_ris_for_ingreso(ingreso_id: int, user_id: int | None = None) -> dict[str, Any]:
    return _preflight_from_contexts([_ingreso_context(ingreso_id)], user_id=user_id)


def preflight_ris_for_ingresos(
    ingreso_ids: list[int],
    user_id: int | None = None,
    *,
    skip_equipment_duplicate_check: bool = False,
) -> dict[str, Any]:
    clean_ids: list[int] = []
    for raw in ingreso_ids or []:
        try:
            ingreso_id = int(raw)
        except (TypeError, ValueError):
            continue
        if ingreso_id > 0 and ingreso_id not in clean_ids:
            clean_ids.append(ingreso_id)
    if not clean_ids:
        return {
            "can_emit": False,
            "issues": [_issue("RIS_EMPTY_BATCH", "No hay ingresos para validar.")],
            "preview": {"items": [], "lineCount": 0},
            "detail": "No hay ingresos para validar.",
        }
    return _preflight_from_contexts(
        [_ingreso_context(ingreso_id) for ingreso_id in clean_ids],
        user_id=user_id,
        skip_equipment_duplicate_check=skip_equipment_duplicate_check,
    )


def preflight_ris_for_request_payload(data: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    document_mode = parse_ris_document_mode((data or {}).get("ris_mode") or (data or {}).get("document_mode"))
    manual_document: dict[str, str] | None = None
    if False and document_mode == RIS_DOCUMENT_MODE_REGISTER:
        try:
            manual_document = normalize_manual_ris_remito_number(
                (data or {}).get("manual_remito_number")
                or (data or {}).get("manualRemitoNumber")
                or (data or {}).get("remito_ingreso"),
                require_complete=True,
            )
        except BejermanRisError as exc:
            issues.append(
                _issue(
                    "MANUAL_REMITO_REQUIRED",
                    str(exc),
                    scope="remito",
                    field="manual_remito_number",
                )
            )
    items = _payload_items_from_request(data or {})
    if not items:
        issues.append(_issue("RIS_EMPTY_BATCH", "Debe agregar al menos un equipo al RIS."))
    for index, item in enumerate(items):
        context, item_issues = _context_from_payload(item, item_index=index if len(items) > 1 else None)
        issues.extend(item_issues)
        if context:
            contexts.append(context)
    if issues:
        return {
            "can_emit": False,
            "issues": issues,
            "preview": {"items": [], "lineCount": 0},
            "detail": "La validación previa del RIS encontró problemas.",
            "document_mode": document_mode,
        }
    result = _preflight_from_contexts(
        contexts,
        user_id=user_id,
        skip_equipment_duplicate_check=document_mode == RIS_DOCUMENT_MODE_REGISTER,
    )
    result["document_mode"] = document_mode
    if document_mode == RIS_DOCUMENT_MODE_REGISTER:
        raw_manual_remito_number = (
            (data or {}).get("manual_remito_number")
            or (data or {}).get("manualRemitoNumber")
            or (data or {}).get("remito_ingreso")
        )
        try:
            manual_document = normalize_manual_ris_remito_number(
                raw_manual_remito_number,
                result.get("document_profile") if isinstance(result.get("document_profile"), dict) else None,
                require_complete=True,
            )
        except BejermanRisError as exc:
            result["can_emit"] = False
            result.setdefault("issues", []).append(
                _issue(
                    "MANUAL_REMITO_REQUIRED" if not _clean(raw_manual_remito_number) else "MANUAL_REMITO_INVALID",
                    str(exc),
                    scope="remito",
                    field="manual_remito_number",
                )
            )
            result["detail"] = "La validación previa del remito encontró problemas."
    if manual_document:
        duplicate_issue: dict[str, Any] | None = None
        if result.get("can_emit") and not _is_manual_registered_document(manual_document):
            company_key = _clean((result.get("preview") or {}).get("companyKey")) or "SEPID"
            duplicate = _local_registered_ris_duplicate(manual_document, company_key, [])
            if duplicate:
                duplicate_issue = _issue(
                    "MANUAL_REMITO_ALREADY_ASSOCIATED_LOCAL",
                    f"NEXORA ya registra el remito {manual_document['remitoNumber']}; se asociará este ingreso al mismo remito.",
                    scope="remito",
                    field="manual_remito_number",
                    severity="warning",
                )
            elif not _is_digital_registered_document(
                manual_document,
                {
                    "companyKey": company_key,
                    "documentProfile": result.get("document_profile") if isinstance(result.get("document_profile"), dict) else {},
                },
            ):
                try:
                    duplicate_remote = _remote_ris_duplicate(
                        manual_document,
                        company_key,
                        BejermanSDKClient(company_key=company_key, actor_user_id=user_id),
                    )
                except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, ValueError) as exc:
                    duplicate_issue = _issue(
                        "BEJERMAN_UNAVAILABLE",
                        f"No se pudo validar el remito en Bejerman: {exc}",
                        scope="bejerman",
                    )
                else:
                    if duplicate_remote:
                        duplicate_issue = _issue(
                            "MANUAL_REMITO_DUPLICATE_REMOTE",
                            f"Bejerman ya registra el remito {manual_document['remitoNumber']}.",
                            scope="remito",
                            field="manual_remito_number",
                        )
        if duplicate_issue:
            if (duplicate_issue.get("severity") or "error") == "error":
                result["can_emit"] = False
            result.setdefault("issues", []).append(duplicate_issue)
            result["detail"] = "La validación previa del remito encontró problemas."
        result["manual_remito_number"] = manual_document["remitoNumber"]
        result.setdefault("preview", {})["remitoNumber"] = manual_document["remitoNumber"]
        if result.get("can_emit"):
            result["detail"] = "Remito validado correctamente para registrar."
    return result


def ensure_ris_preflight_ok(preflight: dict[str, Any]) -> None:
    if preflight.get("can_emit"):
        return
    raise BejermanRisPreflightError(preflight)


def apply_customer_fix_from_bejerman(
    customer_id: int,
    customer_code: str,
    company_key: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    code = _clean(customer_code)
    if not customer_id or not code:
        raise BejermanRisError("Cliente y código Bejerman son requeridos")
    client = BejermanSDKClient(company_key=_clean(company_key) or None, actor_user_id=user_id)
    response = client.list_clientes()
    record = find_bejerman_client_record(response, code)
    if not record:
        raise BejermanRisError(f"El cliente Bejerman {code} no existe")
    payload = _customer_payload_from_record(record)
    missing = []
    if not payload.get("iva"):
        missing.append("situación de IVA")
    if not payload.get("cuit"):
        missing.append("CUIT")
    if not payload.get("province") or payload.get("province") == "000":
        missing.append("provincia")
    if missing:
        raise BejermanRisError(f"El cliente Bejerman {code} no sirve para RIS: falta {', '.join(missing)}")
    set_parts = ["cod_empresa = %s", "razon_social = %s"]
    params: list[Any] = [payload["code"], payload["name"]]
    if _has_table_column("customers", "cuit"):
        set_parts.append("cuit = %s")
        params.append(payload["cuit"])
    params.append(customer_id)
    q(
        f"""
        UPDATE customers
           SET {", ".join(set_parts)}
         WHERE id = %s
        """,
        params,
    )
    row = q(
        f"""
        SELECT {_customer_sql_fields()}
          FROM customers c
         WHERE c.id = %s
        """,
        [customer_id],
        one=True,
    )
    return {"customer": row, "candidate": payload}


def apply_article_fix_from_bejerman(
    *,
    model_id: int,
    variante: str,
    article_code: str,
    article_description: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    if not model_id:
        raise BejermanRisError("Modelo requerido para mapear artículo Bejerman")
    code = _clean(article_code)
    if not code:
        raise BejermanRisError("Código de artículo Bejerman requerido")
    context = {
        "model_id": model_id,
        "variante": variante or "",
    }
    try:
        candidate = validate_bejerman_article_choice(
            code,
            client=SyncBejermanSDKClient(actor_user_id=user_id),
            context=context,
        )
    except (BejermanBlockedError, BejermanConfigError, BejermanTransientError) as exc:
        raise BejermanRisError(str(exc)) from exc
    mapping = upsert_article_mapping(
        model_id=int(model_id),
        variante=variante or "",
        article_code=code,
        article_description=article_description or candidate.get("article_description") or "",
        match_source="manual",
        source_payload={"source": "ris_preflight_fix", "candidate": candidate},
        confirmed_by=user_id,
    )
    reopened = reopen_jobs_for_article_mapping(int(model_id), variante or "", code)
    return {"mapping": mapping, "candidate": candidate, "reopened_jobs": reopened}


def _pdf_params(row: dict[str, Any]) -> dict[str, Any]:
    number = _clean(row.get("comprobante_numero")) or _number_from_remito(_clean(row.get("remito_number")))
    request_payload = _json_dict(row.get("request_payload"))
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


def _fetch_pdf(row: dict[str, Any], user_id: int | None = None) -> tuple[bytes, str]:
    params = _pdf_params(row)
    if not params["number"] or not params["pointOfSale"]:
        raise BejermanRisPdfError("No hay referencia de comprobante suficiente para pedir el PDF")
    try:
        client = BejermanSDKClient(company_key=params["companyKey"], actor_user_id=user_id)
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
    return fetch_comprobante_pdf(client, reference, interactive=True)


def _load_ingress_remito_pdf_attachment(ingreso_id: int, user_id: int | None = None) -> tuple[bytes, str, str]:
    row = _row_for_ingreso(ingreso_id) or {}
    if not row:
        raise BejermanRisPdfError("Remito de ingreso no inicializado")
    if row.get("status") != "generated" or not _clean(row.get("remito_number")):
        raise BejermanRisPdfPendingError("El remito todavía no fue emitido")
    pdf_bytes, content_type = _fetch_pdf(row, user_id=user_id)
    if (row.get("document_mode") or RIS_DOCUMENT_MODE_EMIT) != RIS_DOCUMENT_MODE_REGISTER:
        try:
            _update_ris_pdf_ready(ingreso_id)
        except Exception:
            logger.exception("No se pudo marcar el PDF del remito de ingreso como listo", extra={"ingreso_id": ingreso_id})
    filename = f"remito-{_clean(row.get('remito_number')) or os_label(ingreso_id)}.pdf".replace("/", "-")
    return pdf_bytes, content_type, filename


def _ingress_remito_pdf_email_details(ingreso_ids: list[int], payload: dict[str, Any], response: dict[str, Any]) -> list[str]:
    profile = response.get("profile") if isinstance(response.get("profile"), dict) else {}
    equipments = payload.get("equipments") if isinstance(payload.get("equipments"), list) else None
    if equipments is None:
        equipment = payload.get("equipment") if isinstance(payload.get("equipment"), dict) else {}
        equipments = [equipment] if equipment else []
    lines = [
        f"OS: {', '.join(os_label(item) for item in ingreso_ids)}",
        f"Punto de venta: {_clean(profile.get('pointOfSale')) or '-'}",
    ]
    if len(equipments) == 1:
        equipment = equipments[0] or {}
        equipment_label = " | ".join(
            value
            for value in [
                _clean(equipment.get("equipmentType")),
                _clean(equipment.get("brand")),
                _clean(equipment.get("model")),
            ]
            if value
        )
        if equipment_label:
            lines.append(f"Equipo: {equipment_label}")
        serial = _clean(equipment.get("serial"))
        internal_number = _clean(equipment.get("internalNumber"))
        if serial:
            lines.append(f"N/S: {serial}")
        if internal_number:
            lines.append(f"Número interno: {internal_number}")
    elif len(equipments) > 1:
        lines.append(f"Equipos: {len(equipments)}")
    return lines


def _notify_ingress_remito_pdf_email(
    row: dict[str, Any],
    ingreso_ids: list[int],
    payload: dict[str, Any],
    response: dict[str, Any],
    user_id: int | None,
) -> None:
    try:
        clean_ids = [int(item) for item in ingreso_ids if int(item) > 0]
        if not clean_ids:
            return
        summary = response.get("response") if isinstance(response.get("response"), dict) else {}
        profile = response.get("profile") if isinstance(response.get("profile"), dict) else {}
        remito_number = (
            _clean(row.get("remito_number"))
            or _clean(response.get("remitoNumber"))
            or _clean(summary.get("remitoNumber"))
        )
        document_type = (
            _clean(row.get("comprobante_tipo"))
            or _clean(summary.get("comprobanteTipo"))
            or _clean(profile.get("type"))
            or "RIS"
        )
        notify_bejerman_remito_pdf_issued(
            remito_number=remito_number,
            document_type=document_type,
            company_key=_clean(row.get("company_key")) or _clean(response.get("companyKey")) or _clean(payload.get("companyKey")),
            customer_name=_clean(row.get("customer_name")) or _clean(payload.get("customerName")),
            source="Ingreso",
            details=_ingress_remito_pdf_email_details(clean_ids, payload, response),
            actor_user_id=user_id,
            pdf_loader=lambda first_id=clean_ids[0], actor_id=user_id: _load_ingress_remito_pdf_attachment(first_id, actor_id),
        )
    except Exception:
        logger.exception("No se pudo programar el envío del PDF del remito de ingreso", extra={"ingreso_ids": ingreso_ids})


def _apply_registered_document(comprobante: dict[str, Any], payload: dict[str, Any], document: dict[str, str]) -> dict[str, Any]:
    out = dict(comprobante or {})
    profile = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    deposit = _clean(profile.get("deposit")) or _clean(getattr(settings, "BEJERMAN_RIS_DEPOSIT", "STR")) or "STR"
    preserve_stock_flag = as_bool(payload.get("preserveRegisteredDocumentStockFlag"))
    update_stock = as_bool(profile.get("updateStock")) if preserve_stock_flag and "updateStock" in profile else True
    out["Comprobante_Tipo"] = document["type"]
    out["Comprobante_Letra"] = document["letter"]
    out["Comprobante_PtoVenta"] = document["point"]
    out["Comprobante_Numero"] = document["number"]
    out["Comprobante_ActualizaStock"] = "S" if update_stock else "N"
    equipments = payload.get("equipments") if isinstance(payload.get("equipments"), list) else [payload.get("equipment") or {}]
    article_index = 0
    items: list[dict[str, Any]] = []
    for item in out.get("Comprobante_Items") or []:
        next_item = dict(item)
        next_item["Comprobante_Tipo"] = document["type"]
        next_item["Comprobante_Letra"] = document["letter"]
        next_item["Comprobante_PtoVenta"] = document["point"]
        next_item["Comprobante_Numero"] = document["number"]
        if next_item.get("Item_Tipo") == "A":
            equipment = equipments[min(article_index, max(len(equipments) - 1, 0))] if equipments else {}
            partida = resolve_equipment_partida(equipment) or " "
            quantity = _registered_document_article_quantity(document["type"])
            next_item["Item_CantidadUM1"] = quantity
            if update_stock:
                next_item["Item_Deposito"] = deposit
                next_item["Item_Partida"] = partida or " "
                next_item["Item_CantidadUM2"] = quantity
            else:
                next_item["Item_Deposito"] = next_item.get("Item_Deposito")
                next_item["Item_Partida"] = next_item.get("Item_Partida") or " "
                next_item["Item_CantidadUM2"] = next_item.get("Item_CantidadUM2") or 0
            article_index += 1
        items.append(next_item)
    out["Comprobante_Items"] = items
    return out


def _registered_document_article_quantity(document_type: Any) -> int | float:
    return sdk_signed_article_quantity(document_type, 1)


def _document_key_from_parts(parts: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        _clean_upper(parts.get("companyKey") or parts.get("company_key")),
        _clean_upper(parts.get("type") or parts.get("comprobanteTipo")),
        _clean_upper(parts.get("letter") or parts.get("comprobanteLetra")),
        _clean(parts.get("point") or parts.get("comprobantePtoVenta")).zfill(5),
        _clean(parts.get("number") or parts.get("comprobanteNumero")).zfill(8),
    )


def _remote_ris_duplicate(document: dict[str, str], company_key: str, client: BejermanSDKClient) -> dict[str, Any] | None:
    filters = [
        bejerman_filter("Comprobante_Tipo", "IGUAL", document["type"]),
        bejerman_filter("Comprobante_PtoVenta", "IGUAL", document["point"]),
        bejerman_filter("Comprobante_Numero", "IGUAL", document["number"]),
    ]
    response = client.list_comprobantes_ventas(filters)
    wanted = _document_key_from_parts({"companyKey": company_key, **document})
    for record in records_from_response(response):
        candidate = {
            "companyKey": company_key,
            "type": as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))),
            "letter": as_string(first_value(record, ("Comprobante_Letra", "Letra"))),
            "point": as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta"))),
            "number": as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante"))),
            "raw": record,
        }
        if _document_key_from_parts(candidate) == wanted:
            return candidate
    return None


def _registered_document_association_response(
    payload: dict[str, Any],
    document: dict[str, str],
    *,
    raw: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = {
        **(payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}),
        "pointOfSale": document["point"],
        "type": document["type"],
        "letter": document["letter"],
    }
    summary = {
        "comprobanteTipo": document["type"],
        "comprobanteLetra": document["letter"],
        "comprobantePtoVenta": document["point"],
        "comprobanteNumero": document["number"],
        "remitoNumber": document["remitoNumber"],
    }
    request_payload = {
        **payload,
        "documentMode": RIS_DOCUMENT_MODE_REGISTER,
        "manualRemitoNumber": document["remitoNumber"],
        "operation": "associate_existing_bejerman_remito",
        "existingBejermanRemito": True,
    }
    response = {
        "success": True,
        "requestId": request_payload["requestId"],
        "documentMode": RIS_DOCUMENT_MODE_REGISTER,
        "manualRemitoNumber": document["remitoNumber"],
        "companyKey": request_payload["companyKey"],
        "companyLabel": request_payload["companyLabel"],
        "bejermanCompany": request_payload["bejermanCompany"],
        "issueDate": request_payload["issueDate"],
        "remitoNumber": document["remitoNumber"],
        "response": summary,
        "profile": profile,
        "lineCount": max(len(_payload_equipment_items(payload)), 1),
        "raw": raw,
        "associatedExistingBejermanRemito": True,
    }
    return request_payload, response


def _local_registered_ris_duplicate(document: dict[str, str], company_key: str, ingreso_ids: list[int]) -> dict[str, Any] | None:
    exclude_sql = "AND NOT (ingreso_id = ANY(%s))" if ingreso_ids else ""
    params: list[Any] = [
        company_key,
        document["type"],
        document["letter"],
        document["point"],
        document["number"],
    ]
    if ingreso_ids:
        params.append(ingreso_ids)
    return q(
        f"""
        SELECT ingreso_id, remito_number
          FROM bejerman_ingreso_remitos
         WHERE COALESCE(company_key, '') = %s
           AND COALESCE(comprobante_tipo, '') = %s
           AND COALESCE(comprobante_letra, '') = %s
           AND COALESCE(comprobante_pto_venta, '') = %s
           AND COALESCE(comprobante_numero, '') = %s
           AND status IN ('running', 'generated')
           {exclude_sql}
         LIMIT 1
        """,
        params,
        one=True,
    )


def _register_payload_to_bejerman(
    payload: dict[str, Any],
    manual_remito_number: Any,
    ingreso_ids: list[int],
    user_id: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    operation = _clean(getattr(settings, "BEJERMAN_RIS_OPERATION", "IngresarComprobanteJSON")) or "IngresarComprobanteJSON"
    document = normalize_manual_ris_remito_number(
        manual_remito_number,
        payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else None,
        require_complete=True,
    )
    try:
        skip_duplicate_checks = _is_manual_registered_document(document)
        if not skip_duplicate_checks:
            duplicate = _local_registered_ris_duplicate(document, payload["companyKey"], ingreso_ids)
            if duplicate:
                return _registered_document_association_response(
                    payload,
                    document,
                    raw={"source": "local", "existing": duplicate},
                )
            if _is_digital_registered_document(document, payload):
                return _registered_document_association_response(payload, document)
        client = BejermanSDKClient(company_key=payload["companyKey"], actor_user_id=user_id)
        if not skip_duplicate_checks:
            duplicate_remote = _remote_ris_duplicate(document, payload["companyKey"], client)
            if duplicate_remote:
                raise BejermanSdkResponseError(f"Bejerman ya registra el remito {document['remitoNumber']}")
            _ensure_equipment_remito_not_duplicated(payload, ingreso_ids, client)
        customer_fields = resolve_customer_document_fields(client, payload["customerCode"])
        compact_payload = {**payload, "compactServiceIngressLegends": True}
        built = build_service_ingress_comprobante(compact_payload, customer_fields)
        comprobante = _apply_registered_document(built["comprobante"], payload, document)
        profile = {**(built.get("profile") or {}), "pointOfSale": document["point"], "type": document["type"]}
        request_payload = {
            **compact_payload,
            "documentMode": RIS_DOCUMENT_MODE_REGISTER,
            "manualRemitoNumber": document["remitoNumber"],
            "comprobante": comprobante,
            "operation": operation,
            "numeraFlex": RIS_REGISTER_NUMERA_FLEX,
            "emiteReg": RIS_REGISTER_EMITE_REG,
        }
        raw_response = client.ingresar_comprobante_ventas_json(
            comprobante,
            circuito="VENTAS",
            operacion=operation,
            numera_flex=RIS_REGISTER_NUMERA_FLEX,
            emite_reg=RIS_REGISTER_EMITE_REG,
        )
        summary = parse_remito_response(raw_response)
        if not _clean(summary.get("comprobanteNumero")):
            summary = {
                **summary,
                "comprobanteTipo": document["type"],
                "comprobanteLetra": document["letter"],
                "comprobantePtoVenta": document["point"],
                "comprobanteNumero": document["number"],
                "remitoNumber": document["remitoNumber"],
            }
        remito_number = _clean(summary.get("remitoNumber")) or document["remitoNumber"]
        response = {
            "success": True,
            "requestId": request_payload["requestId"],
            "documentMode": RIS_DOCUMENT_MODE_REGISTER,
            "manualRemitoNumber": document["remitoNumber"],
            "companyKey": request_payload["companyKey"],
            "companyLabel": request_payload["companyLabel"],
            "bejermanCompany": request_payload["bejermanCompany"],
            "issueDate": request_payload["issueDate"],
            "remitoNumber": remito_number,
            "response": summary,
            "profile": profile,
            "lineCount": built["lineCount"],
            "raw": raw_response,
        }
        return request_payload, response
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, ValueError) as exc:
        raise BejermanRisError(str(exc)) from exc


def _build_service_ingress_request(
    payload: dict[str, Any],
    customer_fields: dict[str, str],
    *,
    operation: str,
    numera_flex: str,
    emite_reg: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    built = build_service_ingress_comprobante(payload, customer_fields)
    request_payload = {
        **payload,
        "comprobante": built["comprobante"],
        "operation": operation,
        "numeraFlex": numera_flex,
        "emiteReg": emite_reg,
    }
    return request_payload, built


def _service_ingress_response(
    request_payload: dict[str, Any],
    built: dict[str, Any],
    raw_response: dict[str, Any],
) -> dict[str, Any]:
    summary = parse_remito_response(raw_response)
    remito_number = _clean(summary.get("remitoNumber"))
    if not remito_number:
        raise BejermanSdkResponseError("Bejerman no devolvió número de RIS")
    return {
        "success": True,
        "requestId": request_payload["requestId"],
        "companyKey": request_payload["companyKey"],
        "companyLabel": request_payload["companyLabel"],
        "bejermanCompany": request_payload["bejermanCompany"],
        "issueDate": request_payload["issueDate"],
        "remitoNumber": remito_number,
        "response": summary,
        "profile": built["profile"],
        "lineCount": built["lineCount"],
        "raw": raw_response,
    }


def _send_service_ingress_comprobante(
    client: BejermanSDKClient,
    payload: dict[str, Any],
    customer_fields: dict[str, str],
    *,
    operation: str,
    numera_flex: str,
    emite_reg: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_payload, built = _build_service_ingress_request(
        payload,
        customer_fields,
        operation=operation,
        numera_flex=numera_flex,
        emite_reg=emite_reg,
    )
    raw_response = client.ingresar_comprobante_ventas_json(
        built["comprobante"],
        circuito="VENTAS",
        operacion=operation,
        numera_flex=numera_flex,
        emite_reg=emite_reg,
    )
    return request_payload, _service_ingress_response(request_payload, built, raw_response)


def _emit_payload_to_bejerman(payload: dict[str, Any], user_id: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    operation = _clean(getattr(settings, "BEJERMAN_RIS_OPERATION", "IngresarComprobanteJSON")) or "IngresarComprobanteJSON"
    numera_flex = _clean(getattr(settings, "BEJERMAN_RIS_NUMERA_FLEX", "S")) or "S"
    emite_reg = _clean(getattr(settings, "BEJERMAN_RIS_EMITE_REG", "E")) or "E"
    try:
        client = BejermanSDKClient(company_key=payload["companyKey"], actor_user_id=user_id)
        _ensure_equipment_remito_not_duplicated(payload, _payload_ingreso_ids(payload), client)
        customer_fields = resolve_customer_document_fields(client, payload["customerCode"])
        return _send_service_ingress_comprobante(
            client,
            payload,
            customer_fields,
            operation=operation,
            numera_flex=numera_flex,
            emite_reg=emite_reg,
        )
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, ValueError) as exc:
        raise BejermanRisError(str(exc)) from exc


def emit_or_get_ris(ingreso_id: int, user_id: int | None = None) -> dict[str, Any]:
    started_at = time.monotonic()
    _ensure_ris_row(ingreso_id, user_id)
    current = _row_for_ingreso(ingreso_id) or {}
    manual_remito_number = _clean(current.get("manual_remito_number")) or _clean(current.get("remito_number"))
    if (
        current.get("status") == "failed"
        and (current.get("document_mode") or RIS_DOCUMENT_MODE_EMIT) == RIS_DOCUMENT_MODE_REGISTER
        and manual_remito_number
    ):
        batch_ids = _existing_ingreso_ids(_ingreso_ids_from_payload(current.get("request_payload"))) or [ingreso_id]
        if ingreso_id not in batch_ids:
            batch_ids.append(ingreso_id)
        return register_ris_batch(batch_ids, manual_remito_number, user_id=user_id)
    if _clean(current.get("remito_number")) and current.get("status") == "generated":
        logger.info(
            "bejerman_ris_emit_reused",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
        )
        return current
    existing = _adopt_existing_ingress_remito_reference(ingreso_id, user_id=user_id, current=current)
    if existing and _clean(existing.get("remito_number")) and existing.get("status") == "generated":
        logger.info(
            "bejerman_ris_existing_remito_reused",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
        )
        return existing
    batch_ids = _ingreso_ids_from_payload(current.get("request_payload"))
    if len(batch_ids) > 1:
        return emit_or_get_ris_batch(batch_ids, user_id=user_id)

    ensure_ris_preflight_ok(preflight_ris_for_ingreso(ingreso_id, user_id=user_id))
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
        client = BejermanSDKClient(company_key=payload["companyKey"], actor_user_id=user_id)
        _ensure_equipment_remito_not_duplicated(payload, [ingreso_id], client)
        customer_fields = resolve_customer_document_fields(client, payload["customerCode"])
        request_payload, response = _send_service_ingress_comprobante(
            client,
            payload,
            customer_fields,
            operation=operation,
            numera_flex=numera_flex,
            emite_reg=emite_reg,
        )
        logger.info(
            "bejerman_ris_emit_request_finished",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
        )
        payload = request_payload
        row = _update_ris_generated(ingreso_id, payload, response)
        _notify_ingress_remito_pdf_email(row, [ingreso_id], payload, response, user_id)
        logger.info(
            "bejerman_ris_emit_generated",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
        )
        return row
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable, ValueError) as exc:
        logger.warning(
            "bejerman_ris_emit_failed",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000), "error": str(exc)},
        )
        _update_ris_failure(ingreso_id, str(exc), pdf_error=False, request_payload=request_payload or payload or None)
        raise BejermanRisError(str(exc)) from exc

def emit_or_get_ris_batch(ingreso_ids: list[int], user_id: int | None = None) -> dict[str, Any]:
    clean_ids: list[int] = []
    for raw in ingreso_ids or []:
        try:
            ingreso_id = int(raw)
        except (TypeError, ValueError):
            continue
        if ingreso_id > 0 and ingreso_id not in clean_ids:
            clean_ids.append(ingreso_id)
    if not clean_ids:
        raise BejermanRisError("No hay ingresos para emitir RIS")
    for ingreso_id in clean_ids:
        _ensure_ris_row(ingreso_id, user_id)
    rows = [_row_for_ingreso(ingreso_id) or {} for ingreso_id in clean_ids]
    remito_numbers = [_clean(row.get("remito_number")) for row in rows]
    generated_numbers = {
        remito_number
        for row, remito_number in zip(rows, remito_numbers)
        if row.get("status") == "generated" and remito_number
    }
    if len(generated_numbers) == 1 and all(remito_number in generated_numbers for remito_number in remito_numbers):
        return rows[0]

    ensure_ris_preflight_ok(preflight_ris_for_ingresos(clean_ids, user_id=user_id))
    payload: dict[str, Any] = {
        "requestId": f"reparaciones-ingreso-lote-{'-'.join(str(item) for item in clean_ids)}",
        "ingresoId": clean_ids[0],
        "ingresoIds": clean_ids,
    }
    request_payload: dict[str, Any] | None = None
    _lock_for_emit_many(clean_ids)
    try:
        payload = _build_batch_payload(clean_ids)
        request_payload, response = _emit_payload_to_bejerman(payload, user_id=user_id)
    except BejermanRisError as exc:
        _update_ris_failure_many(clean_ids, str(exc), pdf_error=False, request_payload=request_payload or payload or None)
        raise
    row = _update_ris_generated_many(clean_ids, request_payload, response)
    return row


def register_ris_batch(ingreso_ids: list[int], manual_remito_number: Any, user_id: int | None = None) -> dict[str, Any]:
    clean_ids: list[int] = []
    for raw in ingreso_ids or []:
        try:
            ingreso_id = int(raw)
        except (TypeError, ValueError):
            continue
        if ingreso_id > 0 and ingreso_id not in clean_ids:
            clean_ids.append(ingreso_id)
    clean_ids = _existing_ingreso_ids(clean_ids)
    if not clean_ids:
        raise BejermanRisError("No hay ingresos para registrar remito")
    batch_payload = _build_batch_payload(clean_ids)
    document = normalize_manual_ris_remito_number(
        manual_remito_number,
        batch_payload.get("documentProfile") if isinstance(batch_payload.get("documentProfile"), dict) else None,
        require_complete=True,
    )
    for ingreso_id in clean_ids:
        _ensure_ris_row(ingreso_id, user_id)
    rows = [_row_for_ingreso(ingreso_id) or {} for ingreso_id in clean_ids]
    batch_payload = _apply_stored_register_payload(batch_payload, _stored_register_payload(rows, document))

    def is_same_registered_document(row: dict[str, Any]) -> bool:
        return (
            row.get("status") == "generated"
            and (row.get("document_mode") or RIS_DOCUMENT_MODE_EMIT) == RIS_DOCUMENT_MODE_REGISTER
            and _clean(row.get("remito_number")) == document["remitoNumber"]
        )

    if all(
        is_same_registered_document(row)
        for row in rows
    ):
        return rows[0]
    if any(
        row.get("status") == "generated"
        and _clean(row.get("remito_number"))
        and not is_same_registered_document(row)
        for row in rows
    ):
        raise BejermanRisError("El ingreso ya tiene un RIS/remito generado")

    ensure_ris_preflight_ok(
        preflight_ris_for_ingresos(clean_ids, user_id=user_id, skip_equipment_duplicate_check=True)
    )
    payload: dict[str, Any] = {
        **batch_payload,
        "documentMode": RIS_DOCUMENT_MODE_REGISTER,
        "manualRemitoNumber": document["remitoNumber"],
    }
    request_payload: dict[str, Any] | None = None
    _lock_for_register_many(clean_ids, document, payload.get("companyKey") or "")
    try:
        request_payload, response = _register_payload_to_bejerman(
            payload,
            document["remitoNumber"],
            clean_ids,
            user_id=user_id,
        )
    except BejermanRisError as exc:
        _update_ris_failure_many(clean_ids, str(exc), pdf_error=False, request_payload=request_payload or payload or None)
        raise
    row = _update_ris_generated_many(clean_ids, request_payload, response)
    return row


def fetch_ris_pdf(ingreso_id: int, user_id: int | None = None) -> tuple[bytes, str, dict[str, Any]]:
    started_at = time.monotonic()
    row = _row_for_ingreso(ingreso_id) or {}
    if row.get("status") != "generated" or not _clean(row.get("remito_number")):
        row = _adopt_existing_ingress_remito_reference(ingreso_id, user_id=user_id, current=row) or {}
    if not row:
        raise BejermanRisError("RIS no inicializado")
    if (row.get("document_mode") or RIS_DOCUMENT_MODE_EMIT) == RIS_DOCUMENT_MODE_REGISTER:
        raise BejermanRisRegisteredNoPdfError(row.get("remito_number") or row.get("manual_remito_number"))
    if row.get("status") != "generated" or not _clean(row.get("remito_number")):
        if row.get("status") == "failed":
            raise BejermanRisError(_clean(row.get("last_error")) or "No se pudo emitir el RIS")
        raise BejermanRisPdfPendingError("El RIS todavía no fue emitido")
    try:
        pdf_bytes, content_type = _fetch_pdf(row, user_id=user_id)
    except BejermanPdfPendingError as exc:
        logger.info(
            "bejerman_ris_pdf_pending",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
        )
        row = _update_ris_pdf_pending(ingreso_id)
        raise BejermanRisPdfPendingError(str(exc), retry_after_ms=getattr(exc, "retry_after_ms", 2500)) from exc
    except BejermanSdkUnavailable as exc:
        logger.info(
            "bejerman_ris_pdf_timeout_pending",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
        )
        row = _update_ris_pdf_pending(ingreso_id)
        raise BejermanRisPdfPendingError(
            "Bejerman no respondió a tiempo al pedir el PDF del RIS. El RIS ya está emitido; se reintentará la descarga.",
            retry_after_ms=5000,
        ) from exc
    except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanRisPdfError) as exc:
        logger.warning(
            "bejerman_ris_pdf_failed",
            extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000), "error": str(exc)},
        )
        _update_ris_failure(ingreso_id, str(exc), pdf_error=True)
        raise BejermanRisPdfError(str(exc)) from exc
    row = _update_ris_pdf_ready(ingreso_id)
    logger.info(
        "bejerman_ris_pdf_ready",
        extra={"ingreso_id": ingreso_id, "duration_ms": int((time.monotonic() - started_at) * 1000)},
    )
    return pdf_bytes, content_type, row


def emit_or_fetch_ris_pdf(ingreso_id: int, user_id: int | None = None) -> tuple[bytes, str, dict[str, Any]]:
    emit_or_get_ris(ingreso_id, user_id)
    return fetch_ris_pdf(ingreso_id, user_id=user_id)


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
