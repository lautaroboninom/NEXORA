import json
import logging
import re
import time
import unicodedata
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .bejerman_companies import DEFAULT_INGRESS_COMPANY_KEY, require_company
from .bejerman_sync_alerts import queue_stale_blocked_bejerman_job_alerts
from .bejerman_sdk import (
    is_bejerman_sdk_race_message,
    is_bejerman_table_update_transient_message,
)
from .bejerman_user_credentials import (
    BEJERMAN_CREDENTIALS_REQUIRED,
    BejermanUserCredentialsError,
    get_user_bejerman_credentials,
    resolve_user_bejerman_workstation,
)

logger = logging.getLogger(__name__)

SYNC_TYPE_STOCK_ENTRY_STR = "stock_entry_str"
SYNC_TYPE_STOCK_STR_TO_STL = "stock_str_to_stl"
SYNC_TYPE_STOCK_STR_TO_STC = "stock_str_to_stc"
SYNC_TYPE_STOCK_STR_TO_VAL = "stock_str_to_val"
SYNC_TYPE_STOCK_STR_TO_STCL = "stock_str_to_stcl"  # Alias legacy previo a STC.
SYNC_TYPE_STOCK_EXIT_RTS = "stock_exit_rts"  # Nombre legacy; la salida física la gestiona Portal.
SYNC_TYPE_STOCK_TO_DESGUACE = "stock_to_desguace"
SYNC_TYPE_STOCK_FROM_DESGUACE = "stock_from_desguace"
ARTICLE_RECONCILE_SYNC_TYPES = (
    SYNC_TYPE_STOCK_STR_TO_STL,
    SYNC_TYPE_STOCK_STR_TO_STC,
    SYNC_TYPE_STOCK_STR_TO_VAL,
    SYNC_TYPE_STOCK_STR_TO_STCL,
    SYNC_TYPE_STOCK_TO_DESGUACE,
    SYNC_TYPE_STOCK_FROM_DESGUACE,
)
TARGET_STOCK_RESTORE_SYNC_TYPES = (
    SYNC_TYPE_STOCK_STR_TO_STL,
    SYNC_TYPE_STOCK_STR_TO_STC,
    SYNC_TYPE_STOCK_STR_TO_VAL,
    SYNC_TYPE_STOCK_STR_TO_STCL,
)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_BLOCKED = "blocked"

INTERNAL_CODE_RE = re.compile(r"^(MG|NM|NV|CE)\s*\d{1,4}$", re.IGNORECASE)
TRANSFER_PHASE_SAL_DONE = "sal_done"
TRANSFER_PHASE_ENT_PENDING = "ent_pending"
TRANSFER_PHASE_DONE = "done"
TRANSFER_PHASE_TARGET_ENTRY_DONE = "target_entry_done"
NEXORA_STOCK_ENTRY_MESSAGE = (
    "El RIS/RDA de ingreso se emite en Bejerman antes de NEXORA; "
    "NEXORA no emite ENT/RIS de ingreso. Verificar remito emitido o stock en STR."
)
PORTAL_STOCK_EXIT_MESSAGE = "Salida física Bejerman gestionada por Portal; NEXORA no emite salida final"
LEGACY_BLOCKED_SYNC_TYPES = (SYNC_TYPE_STOCK_ENTRY_STR, SYNC_TYPE_STOCK_EXIT_RTS)
RESTORE_TARGET_STOCK_ERROR_PREFIX = "No se pudo restaurar stock en destino"
TRANSIENT_REOPEN_COUNT_KEY = "transient_reopen_count"


class BejermanSyncError(Exception):
    pass


class BejermanConfigError(BejermanSyncError):
    pass


class BejermanBlockedError(BejermanSyncError):
    pass


class BejermanTransientError(BejermanSyncError):
    pass


def _setting(name: str, default: Any = ""):
    return getattr(settings, name, default)


def _positive_int_setting(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        raw_value = _setting(name, default)
        value = int(default if raw_value in (None, "") else raw_value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _is_retryable_sync_message(value: Any) -> bool:
    return is_bejerman_table_update_transient_message(value) or is_bejerman_sdk_race_message(
        value
    )


def _sync_transient_max_attempts(general_max_attempts: int) -> int:
    # Los bloqueos temporales del SDK/importador necesitan una ventana mayor
    # que los errores comunes, sin alterar el máximo general de los demás jobs.
    configured = _positive_int_setting("BEJERMAN_SYNC_TRANSIENT_MAX_ATTEMPTS", 12)
    return max(int(general_max_attempts), configured)


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


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _stock_numera_flex() -> str:
    value = str(_setting("BEJERMAN_STOCK_NUMERA_FLEX", "N") or "N").strip().upper()
    if value not in {"N", "S"}:
        raise BejermanConfigError("BEJERMAN_STOCK_NUMERA_FLEX debe ser N o S")
    return value


def _stock_document_date() -> str:
    return timezone.localdate().isoformat()


def _stock_baja_target_deposit() -> str:
    return str(_setting("BEJERMAN_STOCK_BAJA_TARGET_DEPOSIT", "DES") or "DES").strip() or "DES"


def _stock_baja_causa_emision() -> str:
    return str(_setting("BEJERMAN_STOCK_BAJA_CAUSA_EMISION", "DES") or "DES").strip() or "DES"


def _stock_venta_target_deposit() -> str:
    """Depósito al que va un equipo que pasó a ser nuestro y se destina a venta.

    VAL es el depósito de ventas: es de donde sale un equipo vendido (el mismo del que
    salen los equipos del cambio, ver `_swap_source_deposits`).
    """
    return str(_setting("BEJERMAN_STOCK_VENTA_TARGET_DEPOSIT", "VAL") or "VAL").strip() or "VAL"


def _stock_alta_source_deposit() -> str:
    return str(_setting("BEJERMAN_STOCK_ALTA_SOURCE_DEPOSIT", "DES") or "DES").strip() or "DES"


def _stock_alta_target_deposit() -> str:
    return str(_setting("BEJERMAN_STOCK_ALTA_TARGET_DEPOSIT", "STR") or "STR").strip() or "STR"


def _stock_alta_causa_emision() -> str:
    return str(_setting("BEJERMAN_STOCK_ALTA_CAUSA_EMISION", "ALQ") or "ALQ").strip() or "ALQ"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_text(root: ET.Element, name: str) -> str:
    for elem in root.iter():
        if _local_name(elem.tag) == name:
            return elem.text or ""
    return ""


def _response_dict(xml_text: str) -> dict[str, str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise BejermanTransientError(f"Respuesta SOAP inválida: {exc}") from exc
    return {
        "Resultado": _xml_text(root, "Resultado"),
        "ErrorMsg": _xml_text(root, "ErrorMsg"),
        "DatosJSON": _xml_text(root, "DatosJSON"),
        "Token": _xml_text(root, "Token"),
        "EsLista": _xml_text(root, "EsLista"),
        "NombreTipoDatos": _xml_text(root, "NombreTipoDatos"),
    }


def _is_ok_response(response: dict[str, Any]) -> bool:
    resultado = str(response.get("Resultado") or "").strip().upper()
    error = str(response.get("ErrorMsg") or "").strip()
    return resultado.startswith("OK") and (not error or _is_bejerman_success_message(error))


def _is_bejerman_success_message(value: Any) -> bool:
    text = _text_key(value)
    return "comprobante se importo correctamente" in text


def _parse_json_maybe(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    for _ in range(2):
        try:
            parsed = json.loads(text)
        except Exception:
            return text
        if isinstance(parsed, str):
            text = parsed.strip()
            continue
        return parsed
    return text


def _iter_dicts(value: Any):
    value = _parse_json_maybe(value)
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def _dict_payload(value: Any) -> dict[str, Any]:
    parsed = _parse_json_maybe(value)
    return parsed if isinstance(parsed, dict) else {}


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _text_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_article_variant(value: Any) -> str:
    return _text_key(value)


def _is_internal_equipment(numero_interno: str, numero_serie: str = "", n_de_control: str = "") -> bool:
    return any(
        bool(INTERNAL_CODE_RE.match((value or "").strip()))
        for value in (numero_interno, numero_serie, n_de_control)
    )


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
        if not owner_id:
            return False
        return int(customer_id) == int(owner_id)
    except Exception:
        return False


def _tokens(value: Any) -> set[str]:
    return {part for part in _text_key(value).split(" ") if len(part) >= 2}


def _first_value(record: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    lower = {str(k).lower(): v for k, v in record.items()}
    for name in candidates:
        if name in record:
            return record.get(name)
        key = name.lower()
        if key in lower:
            return lower[key]
    return None


ARTICLE_CODE_FIELDS = (
    "article_code",
    "Comprobante_Art_CodGen",
    "Art_CodGenerico",
    "Art_CodGen",
    "ART_CODGEN",
    "Art_CodReducido",
    "Articulo_Codigo",
    "CodigoArticulo",
    "Codigo_Articulo",
    "Item_CodigoArticulo",
    "Codigo",
    "codigo",
)

ARTICLE_DESCRIPTION_FIELDS = (
    "article_description",
    "Art_DescripcionGeneral",
    "Art_DescripcionReducida",
    "Art_DescricpionAdicional",
    "Art_DescripcionAdicional",
    "Art_DescripcionElemento1",
    "Art_DescripcionElemento2",
    "Art_DescripcionElemento3",
    "Descripcion",
    "descripcion",
    "Nombre",
)


def _article_code_from_record(record: dict[str, Any]) -> str:
    value = _first_value(record, ARTICLE_CODE_FIELDS)
    return str(value or "").strip()


def _article_description_from_record(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ARTICLE_DESCRIPTION_FIELDS:
        value = _first_value(record, (field,))
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts).strip()


def _bool_from_record(record: dict[str, Any], fields: tuple[str, ...]) -> bool | None:
    value = _first_value(record, fields)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "s", "si", "sí", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "n", "no", "false", "f"}:
        return False
    return None


def _article_type_from_record(record: dict[str, Any]) -> str:
    return str(_first_value(record, ("Art_Tipo", "Tipo", "article_type")) or "").strip()


def _article_deposit_from_record(record: dict[str, Any]) -> str:
    return _deposit_from_record(record) or str(_first_value(record, ("Art_CodDeposito", "article_deposit")) or "").strip()


def _article_flags_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_by_partida": _bool_from_record(record, ("Art_StockPorPartida", "stock_by_partida")),
        "participates_stock": _bool_from_record(record, ("Art_ParticipaCircuitoStock", "participates_stock")),
        "participates_sales": _bool_from_record(record, ("Art_ParticipaCircuitoVentas", "participates_sales")),
        "includes_price_list": _bool_from_record(record, ("Art_IncluirEnListaPrecios", "includes_price_list")),
        "controls_stock": _bool_from_record(record, ("Art_ControlaStock", "controls_stock")),
        "article_type": _article_type_from_record(record),
        "deposit": _article_deposit_from_record(record),
    }


def _looks_like_spare_or_accessory(description: str) -> bool:
    tokens = _tokens(description)
    spare_tokens = {
        "accesorio",
        "bateria",
        "bolso",
        "cable",
        "filtro",
        "fuente",
        "globo",
        "kit",
        "placa",
        "repuesto",
        "set",
        "soporte",
        "transformer",
        "turbina",
        "valvula",
    }
    return bool(tokens & spare_tokens)


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, str]:
    return (int(candidate.get("score") or 0), str(candidate.get("article_code") or ""))


def normalize_bejerman_article_candidate(
    record: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    query: str = "",
) -> dict[str, Any]:
    context = context or {}
    code = _article_code_from_record(record)
    description = _article_description_from_record(record)
    haystack = " ".join([code, description, _json_param(record)])
    hay_tokens = _tokens(haystack)
    brand_tokens = _tokens(context.get("marca"))
    model_tokens = _tokens(context.get("modelo"))
    variant_tokens = _tokens(context.get("variante"))
    query_tokens = _tokens(query)

    brand_matches = sorted(brand_tokens & hay_tokens)
    model_matches = sorted(model_tokens & hay_tokens)
    variant_matches = sorted(variant_tokens & hay_tokens)
    query_matches = sorted(query_tokens & hay_tokens)
    flags = _article_flags_from_record(record)
    reasons: list[str] = []
    warnings: list[str] = []

    if brand_matches:
        reasons.append("Coincide con la marca")
    if model_matches:
        reasons.append("Coincide con el modelo")
    if variant_matches:
        reasons.append("Coincide con la variante")
    if query_matches:
        reasons.append("Coincide con la búsqueda")
    if query and _norm(code) == _norm(query):
        reasons.append("Código exacto")
    if flags.get("stock_by_partida") is True:
        reasons.append("Usa partida/número de serie")

    if flags.get("stock_by_partida") is False:
        warnings.append("No informa stock por partida")
    if flags.get("participates_stock") is False:
        warnings.append("No participa en stock")
    if flags.get("article_type") and flags.get("article_type") != "1":
        warnings.append("Tipo de artículo distinto de equipo")
    if _looks_like_spare_or_accessory(description):
        warnings.append("Parece repuesto o accesorio")

    score = 0
    score += len(model_matches) * 35
    score += len(variant_matches) * 25
    score += len(brand_matches) * 20
    score += len(query_matches) * 10
    if query and _norm(code) == _norm(query):
        score += 50
    if flags.get("stock_by_partida") is True:
        score += 10
    if flags.get("participates_stock") is True:
        score += 5
    score -= len(warnings) * 5

    return {
        "article_code": code,
        "article_description": description,
        "score": score,
        "brand_score": len(brand_matches),
        "model_score": len(model_matches),
        "variant_score": len(variant_matches),
        "query_score": len(query_matches),
        "reasons": reasons,
        "warnings": warnings,
        "flags": flags,
        "raw": record,
    }


def _extract_article_code(records: list[dict[str, Any]]) -> str:
    for record in records:
        value = _article_code_from_record(record)
        if value:
            return value
    return ""


def _article_code_for_partida_deposit(
    records: list[dict[str, Any]],
    *,
    serial: str,
    deposit: str,
    expected_article_codes: Sequence[str] = (),
) -> str:
    """Resuelve el artículo de una partida dentro de un único depósito.

    El artículo elegido para un movimiento de stock no puede venir del mapeo
    general del modelo: Bejerman valida la terna depósito + artículo + partida.

    Que un depósito devuelva más de un artículo para la misma partida es
    normal y no es un error de datos: el CPAP y su humidificador comparten el
    número de serie y son artículos distintos. En ese caso desempata el
    artículo que el equipo ya tiene resuelto; sólo se bloquea si ninguno de los
    artículos con stock es el del equipo, o si el equipo todavía no tiene
    artículo definido.
    """
    article_codes = list(
        dict.fromkeys(
            _article_code_from_record(record)
            for record in records
            if _article_code_from_record(record)
        )
    )
    if len(article_codes) > 1:
        expected = list(
            dict.fromkeys(
                str(code or "").strip()
                for code in expected_article_codes
                if str(code or "").strip()
            )
        )
        for candidate in expected:
            for code in article_codes:
                if _norm(code) == _norm(candidate):
                    return code
        if expected:
            raise BejermanBlockedError(
                f"La partida {serial} tiene stock en el depósito {deposit} para los artículos "
                f"{', '.join(article_codes)}, y ninguno es el artículo del equipo "
                f"({', '.join(expected)}); revise el mapeo del modelo antes de moverla."
            )
        raise BejermanBlockedError(
            f"La partida {serial} tiene más de un artículo con stock en el depósito {deposit} "
            f"({', '.join(article_codes)}) y el equipo no tiene artículo confirmado; "
            "confirme el artículo del modelo para saber cuál mover."
        )
    if article_codes:
        return article_codes[0]
    if records:
        raise BejermanBlockedError(
            f"La partida {serial} existe en el depósito {deposit}, pero Bejerman no informó su artículo."
        )
    return ""


def _quantity_from_record(record: dict[str, Any]) -> float | None:
    qty = _first_value(
        record,
        (
            "Stock",
            "Cantidad",
            "CantidadUM1",
            "Comprobante_CantidadUM1",
            "Art_RealUM1",
            "Art_DispUM1",
        ),
    )
    if qty in (None, ""):
        return None
    try:
        return float(qty)
    except Exception:
        return None


def _partida_from_record(record: dict[str, Any]) -> str:
    value = _first_value(
        record,
        (
            "Comprobante_ArtPartida",
            "Art_Partida",
            "ArtPartida",
            "Partida",
            "partida",
            "Item_Partida",
            "Serie",
            "NumeroSerie",
            "numero_serie",
        ),
    )
    return str(value or "").strip()


def _deposit_from_record(record: dict[str, Any]) -> str:
    value = _first_value(
        record,
        (
            "Comprobante_ArtDeposito",
            "Art_CodDeposito",
            "ArtDeposito",
            "Deposito",
            "deposito",
            "Item_Deposito",
        ),
    )
    return str(value or "").strip()


def _records_for_partida_any_quantity(
    response: dict[str, Any], serial: str, deposit: str | None = None
) -> list[dict[str, Any]]:
    serial_norm = _norm(serial)
    deposit_norm = _norm(deposit) if deposit else ""
    records: list[dict[str, Any]] = []
    for record in _iter_dicts(response.get("DatosJSON")):
        partida = _partida_from_record(record)
        deposito = _deposit_from_record(record)
        if partida and _norm(partida) != serial_norm:
            continue
        if deposit_norm and deposito and _norm(deposito) != deposit_norm:
            continue
        records.append(record)
    return records


def _records_for_partida(response: dict[str, Any], serial: str, deposit: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in _records_for_partida_any_quantity(response, serial, deposit):
        qty = _quantity_from_record(record)
        if qty is not None and qty <= 0:
            continue
        records.append(record)
    return records


def _records_for_equipment_article(
    records: list[dict[str, Any]], article_code: str
) -> list[dict[str, Any]]:
    """Deja sólo los renglones de stock del artículo de este equipo.

    Ver la partida en un depósito no alcanza para saber si el equipo está ahí:
    el CPAP y su humidificador comparten el número de serie, así que la misma
    partida aparece bajo dos artículos. Sin artículo conocido se devuelve todo,
    que es el comportamiento de siempre.
    """
    code = str(article_code or "").strip()
    if not code:
        return records
    wanted = _norm(code)
    return [
        record
        for record in records
        if _norm(_article_code_from_record(record)) == wanted
    ]


def _other_positive_records(response: dict[str, Any], serial: str, expected_deposit: str) -> list[dict[str, Any]]:
    expected = _norm(expected_deposit)
    out = []
    for record in _records_for_partida(response, serial):
        deposito = _deposit_from_record(record)
        if deposito and _norm(deposito) == expected:
            continue
        out.append(record)
    return out


def _stock_record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "deposito": _deposit_from_record(record),
        "partida": _partida_from_record(record),
        "article_code": _article_code_from_record(record),
        "article_description": _article_description_from_record(record),
        "quantity": _quantity_from_record(record),
    }


def _stock_records_summary(records: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    return [_stock_record_summary(record) for record in records[:limit]]


def _job_response_payload(job_id: int) -> dict[str, Any]:
    row = q("SELECT response_payload FROM bejerman_sync_jobs WHERE id = %s", [job_id], one=True)
    return _dict_payload((row or {}).get("response_payload"))


def _restore_stock_diagnostic_payload(
    *,
    serial: str,
    source: str,
    target: str,
    source_records: list[dict[str, Any]],
    target_records: list[dict[str, Any]],
    error: str,
) -> dict[str, Any]:
    return {
        "serial": serial,
        "source_deposit": source,
        "target_deposit": target,
        "source_record_count": len(source_records),
        "target_record_count": len(target_records),
        "source_records": _stock_records_summary(source_records),
        "target_records": _stock_records_summary(target_records),
        "article_resolution_error": error,
    }


def _restore_stock_error_message(
    *,
    serial: str,
    source: str,
    target: str,
    source_records: list[dict[str, Any]],
    target_records: list[dict[str, Any]],
    error: str,
) -> str:
    records = [*source_records, *target_records]
    deposits = sorted(
        {
            _deposit_from_record(record) or "-"
            for record in records
            if _deposit_from_record(record) or _article_code_from_record(record) or _partida_from_record(record)
        }
    )
    if deposits:
        stock_detail = f"Bejerman devolvió la partida en depósito(s) {', '.join(deposits)}."
    else:
        stock_detail = f"Se consultó la partida en {source} y {target}, pero Bejerman no devolvió stock con artículo."
    return (
        f"{RESTORE_TARGET_STOCK_ERROR_PREFIX}: falta definir el artículo Bejerman para la partida {serial}. "
        f"{stock_detail} Resolución de artículo: {error}"
    )


def _column_exists(table: str, column: str) -> bool:
    row = q(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = ANY(current_schemas(true))
           AND table_name = %s
           AND column_name = %s
         LIMIT 1
        """,
        [table, column],
        one=True,
    )
    return bool(row)


class BejermanSDKClient:
    def __init__(
        self,
        *,
        company_key: str | None = None,
        actor_user_id: int | None = None,
        bejerman_username: str | None = None,
        bejerman_password: str | None = None,
        bejerman_workstation: str | None = None,
        allow_system_credentials: bool = False,
    ):
        self.wsdl_url = str(_setting("BEJERMAN_WSDL_URL", "") or "").strip()
        self.endpoint_url = self.wsdl_url.split("?", 1)[0]
        self.timeout = int(_setting("BEJERMAN_REQUEST_TIMEOUT", 30) or 30)
        if company_key:
            company = require_company(company_key)
            self.company_key = company.key
            self.company = company.bejerman_company
        else:
            self.company_key = ""
            self.company = str(_setting("BEJERMAN_COMPANY", "") or "").strip()
        self.actor_user_id = actor_user_id
        self.bejerman_username = str(bejerman_username or "").strip()
        self.bejerman_password = str(bejerman_password or "").strip()
        self.bejerman_workstation = str(bejerman_workstation or "").strip()
        self.allow_system_credentials = bool(allow_system_credentials)
        self.token = ""
        self._http = requests.Session()

    def _credentials(self) -> tuple[str, str]:
        if self.bejerman_username and self.bejerman_password:
            return self.bejerman_username, self.bejerman_password
        if self.actor_user_id:
            try:
                return get_user_bejerman_credentials(int(self.actor_user_id))
            except BejermanUserCredentialsError as exc:
                raise BejermanConfigError(str(exc)) from exc
        if self.allow_system_credentials:
            username = str(_setting("BEJERMAN_USER", "") or "").strip()
            password = str(_setting("BEJERMAN_PASSWORD", "") or "").strip()
            if username and password:
                return username, password
        raise BejermanConfigError(BEJERMAN_CREDENTIALS_REQUIRED)

    def _workstation(self) -> str:
        if self.bejerman_workstation:
            return self.bejerman_workstation
        if self.actor_user_id:
            return resolve_user_bejerman_workstation(int(self.actor_user_id))
        return str(_setting("BEJERMAN_WORKSTATION", "") or _setting("BEJERMAN_SERVICE_WORKSTATION", "") or "").strip()

    def _post(self, action: str, body: str) -> dict[str, str]:
        if not self.endpoint_url:
            raise BejermanConfigError("BEJERMAN_WSDL_URL requerido")
        envelope = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<s:Body>{body}</s:Body>"
            "</s:Envelope>"
        )
        try:
            response = self._http.post(
                self.endpoint_url,
                data=envelope.encode("utf-8"),
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": action,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise BejermanTransientError(f"Error HTTP Bejerman: {exc}") from exc
        parsed = _response_dict(response.text)
        if not _is_ok_response(parsed):
            error = parsed.get("ErrorMsg") or parsed.get("Resultado") or "Respuesta no OK de Bejerman"
            if _is_retryable_sync_message(error):
                raise BejermanTransientError(error)
            raise BejermanBlockedError(error)
        return parsed

    def close(self) -> None:
        self._http.close()

    def register(self) -> str:
        username, password = self._credentials()
        body = (
            '<EFlexSDK_WSRegistro xmlns="http://localhost:57213/">'
            f"<xUsuario>{escape(username)}</xUsuario>"
            f"<xClave>{escape(password)}</xClave>"
            f"<xCodEmpresa>{escape(self.company)}</xCodEmpresa>"
            f"<xPtoTrabajo>{escape(self._workstation())}</xPtoTrabajo>"
            f"<xCodSucursal>{escape(str(_setting('BEJERMAN_BRANCH', '') or ''))}</xCodSucursal>"
            "</EFlexSDK_WSRegistro>"
        )
        response = self._post("http://localhost:57213/IEFlexSDK_Service/EFlexSDK_WSRegistro", body)
        token = str(response.get("Token") or "").strip()
        if not token:
            raise BejermanBlockedError("Bejerman no devolvió token")
        self.token = token
        return token

    def execute(self, circuito: str, operacion: str, *, params=None, params_json: Any = None) -> dict[str, str]:
        if not self.token:
            self.register()
        params_xml = '<a:Parametros i:nil="true" />'
        if params is not None:
            items = "".join(
                '<b:anyType i:type="c:string" xmlns:c="http://www.w3.org/2001/XMLSchema">'
                f"{escape(str(item))}"
                "</b:anyType>"
                for item in params
            )
            params_xml = (
                '<a:Parametros xmlns:b="http://schemas.microsoft.com/2003/10/Serialization/Arrays">'
                f"{items}"
                "</a:Parametros>"
            )
        params_json_xml = '<a:ParametrosJson i:nil="true" />'
        if params_json is not None:
            params_json_xml = f"<a:ParametrosJson>{escape(_json_param(params_json))}</a:ParametrosJson>"
        body = (
            '<EFlexSDK_WSEjecutar xmlns="http://localhost:57213/">'
            '<xRequest xmlns:a="http://schemas.datacontract.org/2004/07/SB.NET.eFlex.SDKWS" '
            'xmlns:i="http://www.w3.org/2001/XMLSchema-instance">'
            f"<a:Circuito>{escape(circuito)}</a:Circuito>"
            f"<a:Operacion>{escape(operacion)}</a:Operacion>"
            f"{params_xml}"
            f"{params_json_xml}"
            f"<a:Token>{escape(self.token)}</a:Token>"
            "</xRequest>"
            "</EFlexSDK_WSEjecutar>"
        )
        return self._post("http://localhost:57213/IEFlexSDK_Service/EFlexSDK_WSEjecutar", body)

    def stock_by_deposit_partida(self, deposit: str, serial: str) -> dict[str, str]:
        # En Bejerman SEP, ObtenerStockDepositoPartida responde OK pero vacío para partidas reales.
        # ObtenerStockPartida devuelve todos los depósitos; filtramos por depósito localmente.
        return self.execute("STOCK", "ObtenerStockPartida", params=[serial])

    def obtener_articulos(self, article_code: str = "") -> dict[str, str]:
        params = [article_code] if (article_code or "").strip() else []
        return self.execute("TABLAS", "ObtenerArticulos", params=params)

    def ingresar_lista_comprobantes_json(self, comprobantes: list[dict[str, Any]]) -> dict[str, str]:
        datos_comprobantes = _json_param(comprobantes)
        return self.execute(
            "STOCK",
            "IngresarListaComprobantesJSON",
            params_json=[datos_comprobantes, _stock_numera_flex()],
        )


def validate_bejerman_config(*, comprobante: str | None = None) -> None:
    required = [
        "BEJERMAN_WSDL_URL",
        "BEJERMAN_COMPANY",
        "BEJERMAN_WORKSTATION",
    ]
    if comprobante:
        required.append(comprobante)
    missing = [name for name in required if not str(_setting(name, "") or "").strip()]
    if missing:
        raise BejermanConfigError("Faltan variables: " + ", ".join(missing))


def _ingreso_context(ingreso_id: int) -> dict[str, Any]:
    ingreso_variante_sql = "COALESCE(t.equipo_variante, '')" if _column_exists("ingresos", "equipo_variante") else "''"
    empresa_bejerman_sql = (
        "COALESCE(t.empresa_bejerman, 'SEPID')" if _column_exists("ingresos", "empresa_bejerman") else "'SEPID'"
    )
    empresa_facturar_sql = (
        "COALESCE(t.empresa_facturar, 'SEPID')" if _column_exists("ingresos", "empresa_facturar") else "'SEPID'"
    )
    n_de_control_sql = "COALESCE(d.n_de_control, '')" if _column_exists("devices", "n_de_control") else "''"
    device_variante_sql = "COALESCE(d.variante, '')" if _column_exists("devices", "variante") else "''"
    model_variante_sql = "COALESCE(m.variante, '')" if _column_exists("models", "variante") else "''"
    model_tipo_sql = "COALESCE(m.tipo_equipo, '')" if _column_exists("models", "tipo_equipo") else "''"
    mg_estado_sql = "COALESCE(d.mg_estado, 'activo')" if _column_exists("devices", "mg_estado") else "'activo'"
    mg_venta_fecha_sql = "d.mg_venta_fecha" if _column_exists("devices", "mg_venta_fecha") else "NULL"
    mg_venta_factura_sql = (
        "COALESCE(d.mg_venta_factura_numero, '')"
        if _column_exists("devices", "mg_venta_factura_numero")
        else "''"
    )
    mg_venta_remito_sql = (
        "COALESCE(d.mg_venta_remito_numero, '')"
        if _column_exists("devices", "mg_venta_remito_numero")
        else "''"
    )
    mg_venta_customer_sql = (
        "d.mg_venta_customer_id" if _column_exists("devices", "mg_venta_customer_id") else "NULL"
    )
    row = q(
        f"""
        SELECT t.id AS ingreso_id,
               COALESCE(t.motivo::TEXT, '') AS motivo,
               {empresa_bejerman_sql} AS empresa_bejerman,
               {empresa_facturar_sql} AS empresa_facturar,
               d.id AS device_id,
               d.customer_id,
               d.model_id,
               d.marca_id,
               COALESCE(c.cod_empresa, '') AS customer_code,
               COALESCE(c.razon_social, '') AS customer_name,
               COALESCE(d.numero_serie, '') AS numero_serie,
               COALESCE(d.numero_interno, '') AS numero_interno,
               {n_de_control_sql} AS n_de_control,
               {mg_estado_sql} AS mg_estado,
               {mg_venta_fecha_sql} AS mg_venta_fecha,
               {mg_venta_factura_sql} AS mg_venta_factura_numero,
               {mg_venta_remito_sql} AS mg_venta_remito_numero,
               {mg_venta_customer_sql} AS mg_venta_customer_id,
               COALESCE(m.nombre, '') AS modelo,
               {model_tipo_sql} AS tipo_equipo,
               {ingreso_variante_sql} AS ingreso_variante,
               {device_variante_sql} AS device_variante,
               {model_variante_sql} AS model_variante,
               COALESCE(b.nombre, '') AS marca
          FROM ingresos t
          JOIN devices d ON d.id = t.device_id
          LEFT JOIN customers c ON c.id = d.customer_id
          LEFT JOIN models m ON m.id = d.model_id
          LEFT JOIN marcas b ON b.id = d.marca_id
         WHERE t.id = %s
        """,
        [ingreso_id],
        one=True,
    )
    if not row:
        raise BejermanBlockedError(f"Ingreso {ingreso_id} no encontrado")
    row["variante"] = (
        (row.get("ingreso_variante") or "").strip()
        or (row.get("device_variante") or "").strip()
        or (row.get("model_variante") or "").strip()
    )
    row["variante_norm"] = normalize_article_variant(row.get("variante"))
    return row


def article_context_for_ingreso(ingreso_id: int) -> dict[str, Any]:
    return _ingreso_context(ingreso_id)


def _ingreso_has_mg_sale(row: dict[str, Any]) -> bool:
    if str(row.get("mg_estado") or "").strip().lower() == "inactivo_venta":
        return True
    return bool(
        row.get("mg_venta_fecha")
        or row.get("mg_venta_factura_numero")
        or row.get("mg_venta_remito_numero")
        or row.get("mg_venta_customer_id")
    )


def ingreso_is_internal_equipment(ingreso_id: int) -> bool:
    row = _ingreso_context(ingreso_id)
    if _ingreso_has_mg_sale(row):
        return False
    return bool(
        _is_mg_owner_customer(row.get("customer_id"))
        or _is_internal_equipment(row.get("numero_interno"), row.get("numero_serie"), row.get("n_de_control"))
    )


def ingreso_is_demo_return(ingreso_id: int) -> bool:
    row = _ingreso_context(ingreso_id)
    return _text_key(row.get("motivo")) == "devolucion demo"


def _last_liberado_event_id(ingreso_id: int) -> int | None:
    event = q(
        """
        SELECT id
          FROM ingreso_events
         WHERE ingreso_id = %s
           AND a_estado = 'liberado'
         ORDER BY ts DESC, id DESC
         LIMIT 1
        """,
        [ingreso_id],
        one=True,
    )
    return event and event.get("id")


def _company_key_for_ingreso_row(row: dict[str, Any]) -> str:
    marker = row.get("empresa_bejerman") or row.get("empresa_facturar") or DEFAULT_INGRESS_COMPANY_KEY
    try:
        return require_company(marker).key
    except ValueError as exc:
        raise BejermanBlockedError(str(exc)) from exc


def _enqueue_stock_job(
    *,
    ingreso_id: int,
    sync_type: str,
    source_deposit: str,
    target_deposit: str,
    trigger: str,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    row = _ingreso_context(ingreso_id)
    serial = (row.get("numero_serie") or "").strip()
    company_key = _company_key_for_ingreso_row(row)
    status = JOB_STATUS_PENDING if serial else JOB_STATUS_BLOCKED
    last_error = None if serial else "Número de serie requerido para sincronizar Bejerman"
    request_payload = {
        "source": "nexora",
        "trigger": trigger,
        "reference": f"NEXORA-OS-{ingreso_id}",
        "companyKey": company_key,
        "marca": row.get("marca") or "",
        "modelo": row.get("modelo") or "",
        "variante": row.get("variante") or "",
    }
    return q(
        """
        INSERT INTO bejerman_sync_jobs(
          sync_type, ingreso_id, device_id, ingreso_event_id, numero_serie,
          source_deposit, target_deposit, company_key, status, last_error, actor_user_id, request_payload
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (sync_type, ingreso_id) DO UPDATE
           SET ingreso_event_id = COALESCE(bejerman_sync_jobs.ingreso_event_id, EXCLUDED.ingreso_event_id),
               device_id = EXCLUDED.device_id,
               numero_serie = EXCLUDED.numero_serie,
               source_deposit = EXCLUDED.source_deposit,
               target_deposit = EXCLUDED.target_deposit,
               company_key = EXCLUDED.company_key,
               actor_user_id = COALESCE(EXCLUDED.actor_user_id, bejerman_sync_jobs.actor_user_id),
               request_payload = bejerman_sync_jobs.request_payload || EXCLUDED.request_payload,
               status = CASE
                 WHEN bejerman_sync_jobs.status = 'succeeded' THEN bejerman_sync_jobs.status
                 ELSE EXCLUDED.status
               END,
               last_error = CASE
                 WHEN bejerman_sync_jobs.status = 'succeeded' THEN bejerman_sync_jobs.last_error
                 ELSE EXCLUDED.last_error
               END,
               updated_at = CURRENT_TIMESTAMP
        RETURNING id, status, attempts
        """,
        [
            sync_type,
            ingreso_id,
            row.get("device_id"),
            ingreso_event_id,
            serial,
            source_deposit,
            target_deposit,
            company_key,
            status,
            last_error,
            actor_user_id,
            _json_param(request_payload),
        ],
        one=True,
    )


def enqueue_stock_entry_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    target_deposit = str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR").strip() or "STR"
    job = _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_ENTRY_STR,
        source_deposit="NEXORA",
        target_deposit=target_deposit,
        trigger="ingreso_creado",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )
    if job.get("status") == JOB_STATUS_SUCCEEDED:
        return job
    return q(
        """
        UPDATE bejerman_sync_jobs
           SET status = %s,
               last_error = %s,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        RETURNING id, status, attempts
        """,
        [JOB_STATUS_BLOCKED, NEXORA_STOCK_ENTRY_MESSAGE, job["id"]],
        one=True,
    )


def enqueue_stock_transfer_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    if not ingreso_is_internal_equipment(ingreso_id):
        raise BejermanBlockedError("Solo los equipos MG pueden transferirse de STR a STL")
    source_deposit = str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR").strip() or "STR"
    target_deposit = str(_setting("BEJERMAN_TARGET_DEPOSIT", "STL") or "STL").strip() or "STL"
    if ingreso_event_id is None:
        ingreso_event_id = _last_liberado_event_id(ingreso_id)
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_STR_TO_STL,
        source_deposit=source_deposit,
        target_deposit=target_deposit,
        trigger="equipo_propio_listo_alquiler",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )


def enqueue_client_ready_transfer_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    source_deposit = str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR").strip() or "STR"
    target_deposit = str(_setting("BEJERMAN_CLIENT_TARGET_DEPOSIT", "STC") or "STC").strip() or "STC"
    if ingreso_event_id is None:
        ingreso_event_id = _last_liberado_event_id(ingreso_id)
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_STR_TO_STC,
        source_deposit=source_deposit,
        target_deposit=target_deposit,
        trigger="orden_salida_impresa",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )


def enqueue_demo_ready_transfer_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    source_deposit = str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR").strip() or "STR"
    target_deposit = str(_setting("BEJERMAN_DEMO_TARGET_DEPOSIT", "VAL") or "VAL").strip() or "VAL"
    if ingreso_event_id is None:
        ingreso_event_id = _last_liberado_event_id(ingreso_id)
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_STR_TO_VAL,
        source_deposit=source_deposit,
        target_deposit=target_deposit,
        trigger="demo_lista_val",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )


def enqueue_stock_venta_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Mueve a VAL el equipo que quedó para SEPID y se destina a venta.

    Es el destino 'venta' del cambio de equipo / parte de pago: el equipo ya pasó a nuestro
    patrimonio y tiene que quedar en el depósito de ventas para poder venderlo.

    Reusa el tipo de transferencia STR->VAL (el mismo de la demo lista para vender): el
    origen declarado es STR porque el equipo entró como reparación, pero si Bejerman lo
    tiene anotado en otro depósito (STC, típicamente, cuando la orden de salida ya se
    imprimió) `_process_stock_transfer` descuenta del depósito real en vez de frenar.
    """
    source_deposit = str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR").strip() or "STR"
    target_deposit = _stock_venta_target_deposit()
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_STR_TO_VAL,
        source_deposit=source_deposit,
        target_deposit=target_deposit,
        trigger="parte_de_pago_venta",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )


def enqueue_stock_exit_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    source_deposit: str | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    if source_deposit is None:
        if ingreso_is_internal_equipment(ingreso_id):
            source_deposit = str(_setting("BEJERMAN_TARGET_DEPOSIT", "STL") or "STL").strip() or "STL"
        else:
            source_deposit = str(_setting("BEJERMAN_CLIENT_TARGET_DEPOSIT", "STC") or "STC").strip() or "STC"
    if ingreso_event_id is None:
        ingreso_event_id = _last_liberado_event_id(ingreso_id)
    job = _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_EXIT_RTS,
        source_deposit=source_deposit,
        target_deposit="SALIDA",
        trigger="remito_salida_rss",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )
    return q(
        """
        UPDATE bejerman_sync_jobs
           SET status = %s,
               last_error = %s,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        RETURNING id, status, attempts
        """,
        [JOB_STATUS_BLOCKED, PORTAL_STOCK_EXIT_MESSAGE, job["id"]],
        one=True,
    )


def enqueue_stock_baja_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    target_deposit = _stock_baja_target_deposit()
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_TO_DESGUACE,
        source_deposit="AUTO",
        target_deposit=target_deposit,
        trigger="baja_desguace",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )


def enqueue_stock_alta_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    source_deposit = _stock_alta_source_deposit()
    target_deposit = _stock_alta_target_deposit()
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_FROM_DESGUACE,
        source_deposit=source_deposit,
        target_deposit=target_deposit,
        trigger="alta_desguace",
        ingreso_event_id=ingreso_event_id,
        actor_user_id=actor_user_id,
    )


def _claim_job(max_attempts: int, job_id: int | None = None) -> dict[str, Any] | None:
    params: list[Any] = [max_attempts]
    job_filter = ""
    if job_id is not None:
        job_filter = "AND id = %s"
        params.append(job_id)
    return q(
        f"""
        WITH candidate AS (
          SELECT id
            FROM bejerman_sync_jobs
           WHERE status IN ('pending','failed')
             AND next_attempt_at <= CURRENT_TIMESTAMP
             AND attempts < %s
             {job_filter}
           ORDER BY next_attempt_at ASC, id ASC
           FOR UPDATE SKIP LOCKED
           LIMIT 1
        )
        UPDATE bejerman_sync_jobs j
           SET status = 'running',
               attempts = attempts + 1,
               updated_at = CURRENT_TIMESTAMP
          FROM candidate
         WHERE j.id = candidate.id
        RETURNING j.*
        """,
        params,
        one=True,
    )


def _finish_job(
    job_id: int,
    status: str,
    *,
    error: str | None = None,
    request_payload=None,
    response_payload=None,
    article_code: str | None = None,
):
    q(
        """
        UPDATE bejerman_sync_jobs
           SET status = %s,
               last_error = %s,
               article_code = COALESCE(NULLIF(%s, ''), article_code),
               request_payload = COALESCE(%s::jsonb, request_payload),
               response_payload = COALESCE(%s::jsonb, response_payload),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [
            status,
            error,
            article_code or "",
            _json_param(request_payload) if request_payload is not None else None,
            _json_param(response_payload) if response_payload is not None else None,
            job_id,
        ],
    )


def _retry_job(job: dict[str, Any], error: str, max_attempts: int) -> str:
    attempts = int(job.get("attempts") or 0)
    retryable_sync_error = _is_retryable_sync_message(error)
    effective_max_attempts = (
        _sync_transient_max_attempts(max_attempts) if retryable_sync_error else int(max_attempts)
    )
    if attempts >= effective_max_attempts:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=f"Máximos reintentos agotados: {error}")
        return JOB_STATUS_BLOCKED
    base_delay_seconds = (
        _positive_int_setting("BEJERMAN_SYNC_TRANSIENT_RETRY_BASE_SECONDS", 300)
        if retryable_sync_error
        else 60
    )
    delay_seconds = min(
        3600,
        max(base_delay_seconds, 2 ** max(0, attempts - 1) * base_delay_seconds),
    )
    next_attempt = timezone.now() + timedelta(seconds=delay_seconds)
    q(
        """
        UPDATE bejerman_sync_jobs
           SET status = 'failed',
               last_error = %s,
               next_attempt_at = %s,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [error, next_attempt, job["id"]],
    )
    return JOB_STATUS_FAILED


def reopen_transient_blocked_jobs(
    *,
    cooldown_seconds: int | None = None,
    max_cycles: int | None = None,
    limit: int = 100,
) -> int:
    if cooldown_seconds is None:
        cooldown_seconds = _positive_int_setting(
            "BEJERMAN_SYNC_TRANSIENT_REOPEN_COOLDOWN_SECONDS",
            1800,
            minimum=0,
        )
    else:
        cooldown_seconds = max(0, int(cooldown_seconds))
    if max_cycles is None:
        max_cycles = _positive_int_setting("BEJERMAN_SYNC_TRANSIENT_REOPEN_MAX_CYCLES", 2)
    else:
        max_cycles = max(1, int(max_cycles))
    limit = max(1, int(limit or 1))

    reopened = 0
    with transaction.atomic():
        rows = q(
            """
            SELECT id, last_error, response_payload
              FROM bejerman_sync_jobs
             WHERE status = 'blocked'
               AND actor_user_id IS NOT NULL
               AND updated_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
             ORDER BY updated_at ASC, id ASC
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            [cooldown_seconds, limit],
        ) or []
        for row in rows:
            last_error = str(row.get("last_error") or "")
            if not _is_retryable_sync_message(last_error):
                continue
            response_payload = _dict_payload(row.get("response_payload"))
            try:
                reopen_count = int(response_payload.get(TRANSIENT_REOPEN_COUNT_KEY) or 0)
            except (TypeError, ValueError):
                reopen_count = 0
            if reopen_count >= max_cycles:
                continue
            reopen_metadata = {
                TRANSIENT_REOPEN_COUNT_KEY: reopen_count + 1,
                "transient_reopened_at": timezone.now().isoformat(),
                "transient_last_error": last_error,
            }
            updated = q(
                """
                UPDATE bejerman_sync_jobs
                   SET status = 'pending',
                       attempts = 0,
                       last_error = NULL,
                       next_attempt_at = CURRENT_TIMESTAMP,
                       response_payload = COALESCE(response_payload, '{}'::jsonb) || %s::jsonb,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                   AND status = 'blocked'
                RETURNING id
                """,
                [_json_param(reopen_metadata), row["id"]],
                one=True,
            )
            if updated:
                reopened += 1
    return reopened


def recover_stuck_sync_jobs(*, running_timeout_seconds: int | None = None) -> dict[str, int]:
    """Devuelve a la cola —o al panel— los jobs de los que nadie los saca.

    `_claim_job` sólo toma `pending`/`failed` con `attempts < max`. Eso deja dos
    agujeros por los que un job desaparece sin que nadie se entere:

    - **`running` huérfano**: si el proceso que lo tomó se murió (deploy, reinicio,
      caída del contenedor), el job queda `running` para siempre. Nadie lo reclama y
      en el panel parece que está trabajando.
    - **`pending` con los intentos agotados**: deja de ser elegible, pero el estado
      sigue diciendo `pending`, que se lee como "en camino". Es el peor de los dos,
      porque se ve sano.

    Los primeros vuelven a `pending` (la operación es idempotente: se reconsulta el
    stock antes de mover nada). Los segundos pasan a `blocked`, que es lo que en
    realidad son, para que aparezcan donde se los mira.
    """

    if running_timeout_seconds is None:
        running_timeout_seconds = _positive_int_setting("BEJERMAN_SYNC_RUNNING_TIMEOUT_SECONDS", 1800)
    max_attempts = _positive_int_setting("BEJERMAN_MAX_ATTEMPTS", 8)
    claim_max_attempts = _sync_transient_max_attempts(max_attempts)

    requeued = q(
        """
        UPDATE bejerman_sync_jobs
           SET status = 'pending',
               next_attempt_at = CURRENT_TIMESTAMP,
               last_error = 'Se reanudó: el proceso que lo estaba ejecutando no terminó (reinicio o caída).',
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'running'
           AND updated_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
           AND attempts < %s
        RETURNING id
        """,
        [running_timeout_seconds, claim_max_attempts],
    ) or []

    abandoned = q(
        """
        UPDATE bejerman_sync_jobs
           SET status = 'blocked',
               last_error = COALESCE(
                 NULLIF(last_error, ''),
                 'Máximos reintentos agotados sin un error registrado.'
               ),
               updated_at = CURRENT_TIMESTAMP
         WHERE status IN ('pending', 'running')
           AND attempts >= %s
           AND next_attempt_at <= CURRENT_TIMESTAMP
        RETURNING id
        """,
        [claim_max_attempts],
    ) or []

    return {"requeued_running": len(requeued), "blocked_exhausted": len(abandoned)}


def reopen_jobs_waiting_for_credentials(*, limit: int = 100) -> int:
    """Reabre los jobs que se bloquearon porque su usuario no tenía credenciales.

    Faltar las credenciales personales de Bejerman no es un problema del job: es una
    condición del entorno que se resuelve sola en cuanto la persona las carga. Pero
    el job se bloqueaba al primer intento y ahí se quedaba, así que cargarlas no
    servía de nada y el equipo nunca se movía de depósito.
    """

    rows = q(
        """
        SELECT id, actor_user_id
          FROM bejerman_sync_jobs
         WHERE status = 'blocked'
           AND actor_user_id IS NOT NULL
           AND last_error ILIKE %s
         ORDER BY updated_at ASC, id ASC
         LIMIT %s
        """,
        [f"%{BEJERMAN_CREDENTIALS_REQUIRED[:40]}%", max(1, int(limit or 1))],
    ) or []
    reopened = 0
    for row in rows:
        try:
            get_user_bejerman_credentials(int(row["actor_user_id"]))
        except BejermanUserCredentialsError:
            continue
        except Exception:
            logger.exception("bejerman_sync_credential_recheck falló job=%s", row.get("id"))
            continue
        if _reopen_blocked_job_inline(int(row["id"])):
            reopened += 1
    return reopened


def _reopen_blocked_job_inline(job_id: int, actor_user_id: int | None = None) -> bool:
    updated = q(
        """
        UPDATE bejerman_sync_jobs
           SET status = 'pending',
               attempts = 0,
               last_error = NULL,
               next_attempt_at = CURRENT_TIMESTAMP,
               actor_user_id = COALESCE(%s, actor_user_id),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
           AND status = 'blocked'
        RETURNING id
        """,
        [actor_user_id, job_id],
        one=True,
    )
    return bool(updated)


def _company_key_for_mapping_lookup(context: dict[str, Any], company_key: str | None = None) -> str:
    if company_key:
        try:
            return require_company(company_key).key
        except ValueError:
            return "SEPID"
    try:
        return _company_key_for_ingreso_row(context)
    except BejermanBlockedError:
        return "SEPID"


def _mapping_for_context(context: dict[str, Any], company_key: str | None = None) -> dict[str, Any] | None:
    model_id = context.get("model_id")
    if not model_id:
        return None
    resolved_company = _company_key_for_mapping_lookup(context, company_key)
    return q(
        """
        SELECT *
          FROM bejerman_article_mappings
         WHERE model_id = %s
           AND variante_norm = %s
           AND company_key = %s
         LIMIT 1
        """,
        [model_id, normalize_article_variant(context.get("variante")), resolved_company],
        one=True,
    )


def _record_payload_for_article_match(job: dict[str, Any], record: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "source": source,
        "sync_job_id": job.get("id"),
        "ingreso_id": job.get("ingreso_id"),
        "numero_serie": job.get("numero_serie") or "",
        "deposito": _deposit_from_record(record),
        "partida": _partida_from_record(record),
        "record": record,
    }


def upsert_article_mapping(
    *,
    model_id: int,
    variante: str = "",
    article_code: str,
    article_description: str = "",
    match_source: str = "manual",
    source_payload: Any = None,
    confirmed_by: int | None = None,
    company_key: str = "SEPID",
) -> dict[str, Any]:
    code = (article_code or "").strip()
    if not code:
        raise BejermanBlockedError("Código de artículo Bejerman requerido")
    variant = (variante or "").strip()
    variant_norm = normalize_article_variant(variant)
    try:
        resolved_company = require_company(company_key or "SEPID").key
    except ValueError:
        resolved_company = "SEPID"
    now_confirmed_sql = "CURRENT_TIMESTAMP" if match_source == "manual" else "NULL"
    row = q(
        f"""
        INSERT INTO bejerman_article_mappings(
          model_id, variante, variante_norm, article_code, article_description,
          match_source, source_payload, confirmed_by, confirmed_at, company_key
        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,{now_confirmed_sql},%s)
        ON CONFLICT (model_id, variante_norm, company_key) DO UPDATE
           SET variante = EXCLUDED.variante,
               article_code = EXCLUDED.article_code,
               article_description = EXCLUDED.article_description,
               match_source = EXCLUDED.match_source,
               source_payload = EXCLUDED.source_payload,
               confirmed_by = COALESCE(EXCLUDED.confirmed_by, bejerman_article_mappings.confirmed_by),
               confirmed_at = CASE
                 WHEN EXCLUDED.match_source = 'manual' THEN CURRENT_TIMESTAMP
                 ELSE bejerman_article_mappings.confirmed_at
               END,
               updated_at = CURRENT_TIMESTAMP
        RETURNING *
        """,
        [
            model_id,
            variant,
            variant_norm,
            code,
            (article_description or "").strip(),
            match_source,
            _json_param(source_payload or {}),
            confirmed_by,
            resolved_company,
        ],
        one=True,
    )
    return row or {}


def article_mapping_summary_for_context(
    model_id: int | None, variante: str = "", company_key: str | None = None
) -> dict[str, Any] | None:
    if not model_id:
        return None
    mapping = _mapping_for_context({"model_id": model_id, "variante": variante}, company_key)
    if not mapping:
        return None
    return {
        "id": mapping.get("id"),
        "model_id": mapping.get("model_id"),
        "variante": mapping.get("variante") or "",
        "variante_norm": mapping.get("variante_norm") or "",
        "article_code": mapping.get("article_code") or "",
        "article_description": mapping.get("article_description") or "",
        "match_source": mapping.get("match_source") or "",
        "company_key": mapping.get("company_key") or "",
        "confirmed_at": mapping.get("confirmed_at"),
        "updated_at": mapping.get("updated_at"),
    }


def article_context_for_model(model_id: int, variante: str = "") -> dict[str, Any]:
    row = q(
        """
        SELECT m.id AS model_id,
               COALESCE(m.nombre, '') AS modelo,
               COALESCE(m.tipo_equipo, '') AS tipo_equipo,
               COALESCE(b.nombre, '') AS marca
          FROM models m
          LEFT JOIN marcas b ON b.id = m.marca_id
         WHERE m.id = %s
        """,
        [model_id],
        one=True,
    )
    if not row:
        raise BejermanBlockedError("Modelo no encontrado")
    return {
        "ingreso_id": None,
        "model_id": row.get("model_id"),
        "marca": row.get("marca") or "",
        "modelo": row.get("modelo") or "",
        "tipo_equipo": row.get("tipo_equipo") or "",
        "variante": (variante or "").strip(),
        "variante_norm": normalize_article_variant(variante),
    }


def remember_article_mapping_from_stock_record(
    job: dict[str, Any],
    record: dict[str, Any] | None,
    *,
    source: str,
) -> str:
    if not record:
        return ""
    code = _article_code_from_record(record)
    if not code:
        return ""
    try:
        context = _ingreso_context(int(job["ingreso_id"]))
        company_key = job.get("company_key") or _company_key_for_mapping_lookup(context)
        mapping = _mapping_for_context(context, company_key)
        existing_code = (mapping or {}).get("article_code") or ""
        if mapping and (mapping.get("match_source") or "") == "manual" and existing_code.strip() != code:
            return code
        upsert_article_mapping(
            model_id=int(context["model_id"]),
            variante=context.get("variante") or "",
            article_code=code,
            article_description=_article_description_from_record(record),
            match_source="auto",
            source_payload=_record_payload_for_article_match(job, record, source),
            company_key=company_key,
        )
    except Exception:
        logger.exception("No se pudo asentar artículo Bejerman por serie para job %s", job.get("id"))
    return code


def remember_article_mapping_from_stock_records(
    job: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    source: str,
) -> str:
    for record in records:
        code = remember_article_mapping_from_stock_record(job, record, source=source)
        if code:
            return code
    return ""


def _blocked_article_job_ids_for_context(model_id: int, variante: str, company_key: str | None = None) -> list[int]:
    variant_norm = normalize_article_variant(variante)
    params: list[Any] = [model_id]
    company_clause = ""
    if company_key:
        try:
            company_clause = "AND j.company_key = %s"
            params.append(require_company(company_key).key)
        except ValueError:
            company_clause = ""
            params.pop()
    rows = q(
        f"""
        SELECT j.id, j.ingreso_id
          FROM bejerman_sync_jobs j
          JOIN ingresos t ON t.id = j.ingreso_id
          JOIN devices d ON d.id = t.device_id
         WHERE d.model_id = %s
           {company_clause}
           AND j.status = 'blocked'
           AND COALESCE(j.article_code, '') = ''
           AND (
             COALESCE(j.last_error, '') ILIKE '%%art%%culo%%'
             OR COALESCE(j.last_error, '') ILIKE '%%articulo%%'
             OR j.response_payload ? 'candidates'
             OR j.response_payload ? 'stock_restore_diagnostic'
           )
        """,
        params,
    ) or []
    ids: list[int] = []
    for row in rows:
        try:
            context = _ingreso_context(int(row["ingreso_id"]))
        except Exception:
            continue
        if normalize_article_variant(context.get("variante")) == variant_norm:
            ids.append(int(row["id"]))
    return ids


def count_blocked_article_jobs_for_context(model_id: int, variante: str, company_key: str | None = None) -> int:
    return len(_blocked_article_job_ids_for_context(model_id, variante, company_key))


def reopen_jobs_for_article_mapping(
    model_id: int, variante: str, article_code: str, company_key: str | None = None
) -> int:
    ids = _blocked_article_job_ids_for_context(model_id, variante, company_key)
    if not ids:
        return 0
    q(
        """
        UPDATE bejerman_sync_jobs j
           SET status = 'pending',
               article_code = %s,
               last_error = NULL,
               next_attempt_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
         WHERE j.id = ANY(%s)
        """,
        [article_code, ids],
    )
    return len(ids)


def apply_article_mapping_to_job(job_id: int | None, article_code: str) -> bool:
    if not job_id:
        return False
    code = (article_code or "").strip()
    if not code:
        raise BejermanBlockedError("Código de artículo Bejerman requerido")
    row = q(
        "SELECT id, status, sync_type FROM bejerman_sync_jobs WHERE id=%s",
        [job_id],
        one=True,
    )
    if not row:
        raise BejermanBlockedError("Operación Bejerman no encontrada")
    sync_type = (row.get("sync_type") or "").strip()
    if sync_type in LEGACY_BLOCKED_SYNC_TYPES:
        detail = NEXORA_STOCK_ENTRY_MESSAGE if sync_type == SYNC_TYPE_STOCK_ENTRY_STR else PORTAL_STOCK_EXIT_MESSAGE
        raise BejermanBlockedError(detail)
    status = (row.get("status") or "").strip()
    if status not in (JOB_STATUS_PENDING, JOB_STATUS_FAILED, JOB_STATUS_BLOCKED):
        return False
    q(
        """
        UPDATE bejerman_sync_jobs
           SET status = CASE
                 WHEN status IN ('failed', 'blocked') THEN 'pending'
                 ELSE status
               END,
               attempts = CASE
                 WHEN status IN ('failed', 'blocked') THEN 0
                 ELSE attempts
               END,
               article_code = %s,
               last_error = CASE
                 WHEN status IN ('failed', 'blocked') THEN NULL
                 ELSE last_error
               END,
               next_attempt_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [code, job_id],
    )
    return True


def _article_candidates(context: dict[str, Any], response: dict[str, Any]) -> list[dict[str, Any]]:
    model_tokens = _tokens(context.get("modelo"))
    variant_tokens = _tokens(context.get("variante"))
    required = model_tokens | variant_tokens
    candidates: list[dict[str, Any]] = []
    for record in _iter_dicts(response.get("DatosJSON")):
        candidate = normalize_bejerman_article_candidate(record, context=context)
        code = candidate.get("article_code") or ""
        haystack = " ".join([code, candidate.get("article_description") or "", _json_param(record)])
        hay_tokens = _tokens(haystack)
        if not code:
            continue
        if required and not required.issubset(hay_tokens):
            continue
        candidates.append(candidate)
    candidates.sort(key=_candidate_sort_key, reverse=True)
    return candidates[:20]


def _article_search_matches(candidate: dict[str, Any], query: str) -> bool:
    query = (query or "").strip()
    if not query:
        return True
    code = str(candidate.get("article_code") or "")
    description = str(candidate.get("article_description") or "")
    hay_tokens = _tokens(" ".join([code, description, _json_param(candidate.get("raw") or {})]))
    query_tokens = _tokens(query)
    if _norm(query) in _norm(code):
        return True
    if not query_tokens:
        return True
    return query_tokens.issubset(hay_tokens)


def _candidate_has_required_context(candidate: dict[str, Any], context: dict[str, Any]) -> bool:
    required = _tokens(context.get("modelo")) | _tokens(context.get("variante"))
    if not required:
        return True
    hay_tokens = _tokens(
        " ".join(
            [
                candidate.get("article_code") or "",
                candidate.get("article_description") or "",
                _json_param(candidate.get("raw") or {}),
            ]
        )
    )
    return required.issubset(hay_tokens)


def search_bejerman_articles_for_job(
    *,
    job_id: int,
    query: str = "",
    client: BejermanSDKClient | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    row = q("SELECT ingreso_id FROM bejerman_sync_jobs WHERE id=%s", [job_id], one=True)
    if not row:
        raise BejermanBlockedError("Operación Bejerman no encontrada")
    context = _ingreso_context(int(row["ingreso_id"]))
    payload = search_bejerman_articles_for_context(
        context=context,
        query=query,
        client=client,
        limit=limit,
    )
    payload["context"] = {
        **(payload.get("context") or {}),
        "job_id": job_id,
        "ingreso_id": context.get("ingreso_id"),
    }
    return payload


def search_bejerman_articles_for_context(
    *,
    context: dict[str, Any],
    query: str = "",
    client: BejermanSDKClient | None = None,
    limit: int = 20,
    company_key: str | None = None,
) -> dict[str, Any]:
    model_id = context.get("model_id")
    if not model_id:
        raise BejermanBlockedError("Modelo requerido para buscar artículos Bejerman")
    resolved_company = company_key or _company_key_for_mapping_lookup(context)
    client = client or BejermanSDKClient(company_key=resolved_company)
    query = (query or "").strip()
    response = client.obtener_articulos(query)
    candidates: list[dict[str, Any]] = []
    for record in _iter_dicts(response.get("DatosJSON")):
        candidate = normalize_bejerman_article_candidate(record, context=context, query=query)
        if not candidate.get("article_code"):
            continue
        if not _article_search_matches(candidate, query):
            continue
        if not (query or "").strip() and not _candidate_has_required_context(candidate, context):
            continue
        candidates.append(candidate)
    if query and not candidates:
        response = client.obtener_articulos()
        for record in _iter_dicts(response.get("DatosJSON")):
            candidate = normalize_bejerman_article_candidate(record, context=context, query=query)
            if not candidate.get("article_code"):
                continue
            if not _article_search_matches(candidate, query):
                continue
            candidates.append(candidate)
    candidates.sort(key=_candidate_sort_key, reverse=True)
    return {
        "context": {
            "model_id": context.get("model_id"),
            "marca": context.get("marca") or "",
            "modelo": context.get("modelo") or "",
            "variante": context.get("variante") or "",
            "scope": "modelo_variante",
        },
        "mapping": article_mapping_summary_for_context(int(model_id), context.get("variante") or "", resolved_company),
        "related_blocked_jobs": count_blocked_article_jobs_for_context(
            int(model_id), context.get("variante") or "", resolved_company
        ),
        "items": candidates[: max(1, int(limit or 1))],
    }


def validate_bejerman_article_choice(
    article_code: str,
    *,
    client: BejermanSDKClient | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = (article_code or "").strip()
    if not code:
        raise BejermanBlockedError("Código de artículo Bejerman requerido")
    client = client or BejermanSDKClient()
    response = client.obtener_articulos(code)
    candidates = [
        normalize_bejerman_article_candidate(record, context=context or {}, query=code)
        for record in _iter_dicts(response.get("DatosJSON"))
    ]
    exact = [candidate for candidate in candidates if _norm(candidate.get("article_code")) == _norm(code)]
    if not exact:
        raise BejermanBlockedError(f"No se encontró el artículo Bejerman {code}")
    exact.sort(key=_candidate_sort_key, reverse=True)
    return exact[0]


def article_resolution_for_job_row(row: dict[str, Any]) -> dict[str, Any]:
    response_payload = _dict_payload(row.get("response_payload"))
    context = {
        "marca": row.get("marca") or "",
        "modelo": row.get("modelo") or "",
        "variante": row.get("variante") or "",
    }
    raw_candidates = response_payload.get("candidates") if isinstance(response_payload, dict) else None
    candidates: list[dict[str, Any]] = []
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            record = item.get("raw") if isinstance(item.get("raw"), dict) else item
            candidates.append(normalize_bejerman_article_candidate(record, context=context))
    candidates.sort(key=_candidate_sort_key, reverse=True)
    related = 0
    mapping = None
    model_id = row.get("model_id")
    company_key = row.get("company_key") or None
    if model_id:
        try:
            related = count_blocked_article_jobs_for_context(int(model_id), row.get("variante") or "", company_key)
            mapping = article_mapping_summary_for_context(int(model_id), row.get("variante") or "", company_key)
        except Exception:
            related = 0
            mapping = None
    return {
        "scope": "modelo_variante",
        "scope_label": "marca + modelo + variante",
        "context": context,
        "related_blocked_jobs": related,
        "mapping": mapping,
        "candidates": candidates,
    }


def _resolve_article_for_job(job: dict[str, Any], client: BejermanSDKClient) -> str:
    existing = (job.get("article_code") or "").strip()
    if existing:
        return existing
    context = _ingreso_context(int(job["ingreso_id"]))
    mapping = _mapping_for_context(context, job.get("company_key"))
    if mapping and (mapping.get("article_code") or "").strip():
        code = (mapping.get("article_code") or "").strip()
        _finish_job(job["id"], JOB_STATUS_RUNNING, article_code=code)
        return code

    if not bool(_setting("BEJERMAN_ARTICLE_AUTO_MATCH", True)):
        raise BejermanBlockedError("No hay mapeo local de artículo Bejerman para este modelo/variante")

    response = client.obtener_articulos()
    candidates = _article_candidates(context, response)

    payload = {
        "marca": context.get("marca") or "",
        "modelo": context.get("modelo") or "",
        "variante": context.get("variante") or "",
        "candidates": candidates,
    }
    _finish_job(job["id"], JOB_STATUS_BLOCKED, response_payload=payload)
    if not candidates:
        raise BejermanBlockedError("No se encontró un artículo Bejerman único para este modelo/variante")
    if len(candidates) == 1:
        raise BejermanBlockedError(
            "Hay un artículo Bejerman sugerido para este modelo/variante. Confirmalo manualmente antes de transferir stock."
        )
    raise BejermanBlockedError("Hay más de un artículo Bejerman posible para este modelo/variante")


def _mapping_article_codes_for_job(job: dict[str, Any]) -> tuple[str, str]:
    """Artículo del mapeo del equipo: (confirmado a mano, mapeado sea cual sea el origen).

    El confirmado a mano es el único que puede vetar un movimiento; el mapeado
    sirve para desempatar cuando la partida tiene stock en más de un artículo,
    porque ahí sólo se elige entre códigos que Bejerman ya informó.
    """
    try:
        context = _ingreso_context(int(job["ingreso_id"]))
        mapping = _mapping_for_context(context, job.get("company_key")) or {}
    except Exception:
        logger.exception("No se pudo consultar el mapeo confirmado para job Bejerman %s", job.get("id"))
        return "", ""
    code = (mapping.get("article_code") or "").strip()
    is_manual = (mapping.get("match_source") or "").strip().lower() == "manual"
    return (code if is_manual else ""), code


def _confirmed_article_code_for_job(job: dict[str, Any]) -> str:
    """Devuelve únicamente un artículo confirmado manualmente para el equipo.

    Una partida puede repetirse en artículos distintos (por ejemplo, una máscara
    y un concentrador). El dato de stock define el artículo a mover, pero no se
    debe mover si contradice una confirmación humana del modelo.
    """
    return _mapping_article_codes_for_job(job)[0]


def _assert_source_article_matches_confirmed_mapping(
    job: dict[str, Any],
    *,
    serial: str,
    deposit: str,
    source_article_code: str,
    confirmed_article_code: str | None = None,
) -> str:
    if confirmed_article_code is None:
        confirmed_article_code = _confirmed_article_code_for_job(job)
    if (
        confirmed_article_code
        and _norm(confirmed_article_code) != _norm(source_article_code)
    ):
        raise BejermanBlockedError(
            f"La partida {serial} figura como artículo {source_article_code} en {deposit}, "
            f"pero el equipo tiene confirmado el artículo {confirmed_article_code}. "
            "No se emitió ningún movimiento: revise la partida o el mapeo del equipo."
        )
    return confirmed_article_code


def _stock_transfer_comprobante_number(job: dict[str, Any]) -> str:
    """El número base es determinístico (ingreso_id) para mantener idempotencia
    en el primer intento. Pero si Bejerman ya "aceptó fantasma" ese número en un
    intento previo (acepta el comprobante y rechaza el movimiento de stock en
    silencio, ver force_stock_entry_for_ingreso), todo reintento posterior con el
    mismo número choca para siempre con 'el mismo ya existe... importación
    anterior'. A partir del segundo intento se corre el número para poder
    reintentar de verdad."""
    base = int(job["ingreso_id"]) % 100000000
    attempts = int(job.get("attempts") or 0)
    if attempts > 0:
        base = (base + attempts * 100000) % 100000000
    return f"{base:08d}"


def _base_stock_transfer_item(job: dict[str, Any], article_code: str, comprobante: str, deposit: str) -> dict[str, Any]:
    document_date = _stock_document_date()
    reference = f"NEXORA-OS-{job['ingreso_id']}"
    serial = (job.get("numero_serie") or "").strip()
    return {
        "Comprobante_FechaEmision": document_date,
        "Comprobante_Tipo": comprobante,
        "Comprobante_Letra": "",
        "Comprobante_PtoVenta": "",
        "Comprobante_Numero": _stock_transfer_comprobante_number(job),
        "Comprobante_Art_CodGen": article_code,
        "Comprobante_ArtPartida": serial,
        "Comprobante_DescArticulo": f"NEXORA {serial}"[:50],
        "Comprobante_ArtCodTipo": "1",
        "Comprobante_ArtDeposito": deposit,
        "Comprobante_CantidadUM1": 1,
        "Comprobante_CantidadUM2": 1,
        "Comprobante_PrecioTotalMonLocal": 0,
        "Partida_Observaciones": reference[:20],
    }


def build_stock_transfer_out_payload(
    job: dict[str, Any],
    article_code: str,
    source_deposit: str | None = None,
) -> list[dict[str, Any]]:
    # `source_deposit` explícito para cuando la partida no está en el depósito
    # esperado: se descuenta de donde Bejerman la tiene, no de donde debería estar.
    source_deposit = str(source_deposit or "").strip() or job.get("source_deposit") or "STR"
    comprobante = str(_setting("BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE", "SAL") or "").strip()
    item = _base_stock_transfer_item(job, article_code, comprobante, source_deposit)
    item["Comprobante_CantidadUM1"] = -1
    item["Comprobante_CantidadUM2"] = -1
    item["Comprobante_IdOrigen"] = f"NEXORA-OS-{job['ingreso_id']}-SAL-{source_deposit}"
    return [item]


def build_stock_transfer_in_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    target_deposit = job.get("target_deposit") or "STL"
    comprobante = str(_setting("BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE", "ENT") or "").strip()
    item = _base_stock_transfer_item(job, article_code, comprobante, target_deposit)
    item["Comprobante_IdOrigen"] = f"NEXORA-OS-{job['ingreso_id']}-ENT-{target_deposit}"
    return [item]


def build_stock_transfer_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    return [
        *build_stock_transfer_out_payload(job, article_code),
        *build_stock_transfer_in_payload(job, article_code),
    ]


def build_stock_baja_out_payload(
    job: dict[str, Any],
    article_code: str,
    source_deposit: str,
) -> list[dict[str, Any]]:
    comprobante = str(_setting("BEJERMAN_STOCK_BAJA_OUT_COMPROBANTE", "SAL") or "SAL").strip()
    target_deposit = job.get("target_deposit") or _stock_baja_target_deposit()
    causa = _stock_baja_causa_emision()
    item = _base_stock_transfer_item(job, article_code, comprobante, source_deposit)
    item["Comprobante_CantidadUM1"] = -1
    item["Comprobante_CantidadUM2"] = -1
    item["Comprobante_IdOrigen"] = f"NEXORA-OS-{job['ingreso_id']}-SAL-{source_deposit}-{target_deposit}"
    item["Comprobante_CodigoCausaEmision"] = causa
    return [item]


def build_stock_baja_in_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    target_deposit = job.get("target_deposit") or _stock_baja_target_deposit()
    comprobante = str(_setting("BEJERMAN_STOCK_BAJA_IN_COMPROBANTE", "ENT") or "ENT").strip()
    item = _base_stock_transfer_item(job, article_code, comprobante, target_deposit)
    item["Comprobante_IdOrigen"] = f"NEXORA-OS-{job['ingreso_id']}-ENT-{target_deposit}"
    return [item]


def build_stock_alta_out_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    source_deposit = job.get("source_deposit") or _stock_alta_source_deposit()
    target_deposit = job.get("target_deposit") or _stock_alta_target_deposit()
    comprobante = str(_setting("BEJERMAN_STOCK_ALTA_OUT_COMPROBANTE", "SAL") or "SAL").strip()
    causa = _stock_alta_causa_emision()
    item = _base_stock_transfer_item(job, article_code, comprobante, source_deposit)
    item["Comprobante_CantidadUM1"] = -1
    item["Comprobante_CantidadUM2"] = -1
    item["Comprobante_IdOrigen"] = f"NEXORA-OS-{job['ingreso_id']}-SAL-{source_deposit}-{target_deposit}"
    item["Comprobante_CodigoCausaEmision"] = causa
    return [item]


def build_stock_alta_in_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    target_deposit = job.get("target_deposit") or _stock_alta_target_deposit()
    comprobante = str(_setting("BEJERMAN_STOCK_ALTA_IN_COMPROBANTE", "ENT") or "ENT").strip()
    item = _base_stock_transfer_item(job, article_code, comprobante, target_deposit)
    item["Comprobante_IdOrigen"] = f"NEXORA-OS-{job['ingreso_id']}-ENT-{target_deposit}"
    return [item]


def _process_stock_entry(job: dict[str, Any], client: BejermanSDKClient) -> str:
    raise BejermanBlockedError(NEXORA_STOCK_ENTRY_MESSAGE)


def _process_stock_exit(job: dict[str, Any], client: BejermanSDKClient) -> str:
    raise BejermanBlockedError(PORTAL_STOCK_EXIT_MESSAGE)


def _process_stock_to_desguace(job: dict[str, Any], client: BejermanSDKClient) -> str:
    validate_bejerman_config()
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_BAJA_OUT_COMPROBANTE")
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_BAJA_IN_COMPROBANTE")
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        raise BejermanBlockedError("Número de serie requerido para sincronizar Bejerman")

    target = str(job.get("target_deposit") or _stock_baja_target_deposit()).strip() or _stock_baja_target_deposit()
    target_norm = _norm(target)
    request_payload = _dict_payload(job.get("request_payload"))
    response_payload = _dict_payload(job.get("response_payload"))
    sal_done = bool(request_payload.get("sal_done")) or request_payload.get("baja_phase") in {
        TRANSFER_PHASE_SAL_DONE,
        TRANSFER_PHASE_ENT_PENDING,
        TRANSFER_PHASE_DONE,
        TRANSFER_PHASE_TARGET_ENTRY_DONE,
    }

    stock_response = client.stock_by_deposit_partida("", serial)
    positive_records = _records_for_partida(stock_response, serial)
    target_records = [
        record
        for record in positive_records
        if _norm(_deposit_from_record(record) or "") == target_norm
    ]
    stock_article_code = remember_article_mapping_from_stock_records(
        job,
        positive_records,
        source="stock_baja_partida",
    )
    if target_records:
        article_code = _extract_article_code(target_records) or stock_article_code or (job.get("article_code") or "")
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={
                **request_payload,
                "idempotent": True,
                "baja_to_desguace": True,
                "numero_serie": serial,
                "target_deposit": target,
                "baja_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
            },
            response_payload={**response_payload, "stock": stock_response},
            article_code=article_code,
        )
        return JOB_STATUS_SUCCEEDED

    source_records = [
        record
        for record in positive_records
        if _norm(_deposit_from_record(record) or "") != target_norm
    ]
    source_deposits = sorted(
        {
            (_deposit_from_record(record) or "").strip()
            for record in source_records
            if (_deposit_from_record(record) or "").strip()
        }
    )
    if not sal_done:
        if not source_records:
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "stock": stock_response},
                article_code=stock_article_code,
            )
            raise BejermanBlockedError(
                f"No se encontró stock positivo para la partida {serial}; no se puede mover a {target}"
            )
        if len(source_deposits) != 1:
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "stock": stock_response},
                article_code=stock_article_code,
            )
            deposits_label = ", ".join(source_deposits) if source_deposits else "-"
            raise BejermanBlockedError(
                f"Partida {serial} encontrada con stock positivo en múltiples depósitos ({deposits_label}); requiere conciliación"
            )
        source_deposit = source_deposits[0]
    else:
        source_deposit = str(request_payload.get("source_stock_deposit") or job.get("source_deposit") or "").strip()
        if not source_deposit or _norm(source_deposit) == "AUTO":
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "stock": stock_response},
                article_code=stock_article_code,
            )
            raise BejermanBlockedError("No se pudo continuar la baja: falta el depósito origen ya descontado")

    article_code = (
        (job.get("article_code") or "").strip()
        or str(request_payload.get("article_code") or "").strip()
        or _extract_article_code(source_records)
        or stock_article_code
    )
    if not article_code:
        article_code = _resolve_article_for_job(job, client)

    next_request_payload = {
        **request_payload,
        "article_code": article_code,
        "baja_to_desguace": True,
        "numero_serie": serial,
        "source_stock_deposit": source_deposit,
        "target_deposit": target,
        "causa_emision": _stock_baja_causa_emision(),
    }
    next_response_payload = {**response_payload, "stock": stock_response}
    if not sal_done:
        sal_payload = build_stock_baja_out_payload(job, article_code, source_deposit)
        sal_request_payload = {
            **next_request_payload,
            "baja_phase": "sal_pending",
            "sal_comprobantes": sal_payload,
        }
        _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload=sal_request_payload, article_code=article_code)
        sal_response = client.ingresar_lista_comprobantes_json(sal_payload)
        next_request_payload = {
            **sal_request_payload,
            "sal_done": True,
            "baja_phase": TRANSFER_PHASE_SAL_DONE,
        }
        next_response_payload = {**next_response_payload, "sal": sal_response}
        _finish_job(
            job["id"],
            JOB_STATUS_RUNNING,
            request_payload=next_request_payload,
            response_payload=next_response_payload,
            article_code=article_code,
        )

    ent_payload = build_stock_baja_in_payload(job, article_code)
    ent_request_payload = {
        **next_request_payload,
        "baja_phase": TRANSFER_PHASE_ENT_PENDING,
        "ent_comprobantes": ent_payload,
        "target_stock_entry_deposit": target,
    }
    _finish_job(
        job["id"],
        JOB_STATUS_RUNNING,
        request_payload=ent_request_payload,
        response_payload=next_response_payload,
        article_code=article_code,
    )
    ent_response = client.ingresar_lista_comprobantes_json(ent_payload)
    _finish_job(
        job["id"],
        JOB_STATUS_SUCCEEDED,
        request_payload={
            **ent_request_payload,
            "target_stock_entry_done": True,
            "baja_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
        },
        response_payload={**next_response_payload, "target_stock_entry": ent_response},
        article_code=article_code,
    )
    return JOB_STATUS_SUCCEEDED


def _process_stock_from_desguace(job: dict[str, Any], client: BejermanSDKClient) -> str:
    validate_bejerman_config()
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_ALTA_OUT_COMPROBANTE")
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_ALTA_IN_COMPROBANTE")
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        raise BejermanBlockedError("Número de serie requerido para sincronizar Bejerman")

    source = str(job.get("source_deposit") or _stock_alta_source_deposit()).strip() or _stock_alta_source_deposit()
    target = str(job.get("target_deposit") or _stock_alta_target_deposit()).strip() or _stock_alta_target_deposit()
    source_norm = _norm(source)
    target_norm = _norm(target)
    request_payload = _dict_payload(job.get("request_payload"))
    response_payload = _dict_payload(job.get("response_payload"))
    sal_done = bool(request_payload.get("sal_done")) or request_payload.get("alta_phase") in {
        TRANSFER_PHASE_SAL_DONE,
        TRANSFER_PHASE_ENT_PENDING,
        TRANSFER_PHASE_DONE,
        TRANSFER_PHASE_TARGET_ENTRY_DONE,
    }

    stock_response = client.stock_by_deposit_partida("", serial)
    positive_records = _records_for_partida(stock_response, serial)
    stock_article_code = remember_article_mapping_from_stock_records(
        job,
        positive_records,
        source="stock_alta_partida",
    )
    source_records = [
        record
        for record in positive_records
        if _norm(_deposit_from_record(record) or "") == source_norm
    ]
    target_records = [
        record
        for record in positive_records
        if _norm(_deposit_from_record(record) or "") == target_norm
    ]
    other_records = [
        record
        for record in positive_records
        if _norm(_deposit_from_record(record) or "") not in {source_norm, target_norm}
    ]
    positive_deposits = sorted(
        {
            (_deposit_from_record(record) or "").strip()
            for record in positive_records
            if (_deposit_from_record(record) or "").strip()
        }
    )

    if target_records and not source_records and not other_records:
        article_code = _extract_article_code(target_records) or stock_article_code or (job.get("article_code") or "")
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={
                **request_payload,
                "idempotent": True,
                "alta_from_desguace": True,
                "numero_serie": serial,
                "source_deposit": source,
                "target_deposit": target,
                "alta_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
            },
            response_payload={**response_payload, "stock": stock_response},
            article_code=article_code,
        )
        return JOB_STATUS_SUCCEEDED

    if not sal_done:
        if not positive_records:
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "stock": stock_response},
                article_code=stock_article_code,
            )
            raise BejermanBlockedError(
                f"No se encontró stock positivo para la partida {serial}; no se puede mover a {target}"
            )
        if len(positive_deposits) != 1:
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "stock": stock_response},
                article_code=stock_article_code,
            )
            deposits_label = ", ".join(positive_deposits) if positive_deposits else "-"
            raise BejermanBlockedError(
                f"Partida {serial} encontrada con stock positivo en múltiples depósitos ({deposits_label}); requiere conciliación"
            )
        if not source_records:
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "stock": stock_response},
                article_code=stock_article_code,
            )
            deposit = positive_deposits[0] if positive_deposits else "-"
            raise BejermanBlockedError(
                f"Partida {serial} encontrada en depósito {deposit}; se esperaba {source} para alta a {target}"
            )
        source_deposit = source
    else:
        source_deposit = str(request_payload.get("source_stock_deposit") or job.get("source_deposit") or "").strip()
        if not source_deposit:
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "stock": stock_response},
                article_code=stock_article_code,
            )
            raise BejermanBlockedError("No se pudo continuar el alta: falta el depósito origen ya descontado")

    article_code = (
        (job.get("article_code") or "").strip()
        or str(request_payload.get("article_code") or "").strip()
        or _extract_article_code(source_records)
        or stock_article_code
    )
    if not article_code:
        article_code = _resolve_article_for_job(job, client)

    next_request_payload = {
        **request_payload,
        "article_code": article_code,
        "alta_from_desguace": True,
        "numero_serie": serial,
        "source_stock_deposit": source_deposit,
        "target_deposit": target,
        "causa_emision": _stock_alta_causa_emision(),
    }
    next_response_payload = {**response_payload, "stock": stock_response}
    if not sal_done:
        sal_payload = build_stock_alta_out_payload(job, article_code)
        sal_request_payload = {
            **next_request_payload,
            "alta_phase": "sal_pending",
            "sal_comprobantes": sal_payload,
        }
        _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload=sal_request_payload, article_code=article_code)
        sal_response = client.ingresar_lista_comprobantes_json(sal_payload)
        next_request_payload = {
            **sal_request_payload,
            "sal_done": True,
            "alta_phase": TRANSFER_PHASE_SAL_DONE,
        }
        next_response_payload = {**next_response_payload, "sal": sal_response}
        _finish_job(
            job["id"],
            JOB_STATUS_RUNNING,
            request_payload=next_request_payload,
            response_payload=next_response_payload,
            article_code=article_code,
        )

    ent_payload = build_stock_alta_in_payload(job, article_code)
    ent_request_payload = {
        **next_request_payload,
        "alta_phase": TRANSFER_PHASE_ENT_PENDING,
        "ent_comprobantes": ent_payload,
        "target_stock_entry_deposit": target,
    }
    _finish_job(
        job["id"],
        JOB_STATUS_RUNNING,
        request_payload=ent_request_payload,
        response_payload=next_response_payload,
        article_code=article_code,
    )
    ent_response = client.ingresar_lista_comprobantes_json(ent_payload)
    _finish_job(
        job["id"],
        JOB_STATUS_SUCCEEDED,
        request_payload={
            **ent_request_payload,
            "target_stock_entry_done": True,
            "alta_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
        },
        response_payload={**next_response_payload, "target_stock_entry": ent_response},
        article_code=article_code,
    )
    return JOB_STATUS_SUCCEEDED


def _verify_stock_transfer_target(
    client: BejermanSDKClient,
    *,
    target: str,
    serial: str,
    article_code: str = "",
) -> dict[str, Any]:
    attempts = _positive_int_setting("BEJERMAN_SYNC_STOCK_VERIFY_ATTEMPTS", 3)
    try:
        delay_ms = int(_setting("BEJERMAN_SYNC_STOCK_VERIFY_DELAY_MS", 1500) or 0)
    except (TypeError, ValueError):
        delay_ms = 1500
    delay_ms = max(0, delay_ms)
    last_response: dict[str, Any] = {}
    for attempt in range(attempts):
        last_response = client.stock_by_deposit_partida(target, serial)
        if _records_for_equipment_article(
            _records_for_partida(last_response, serial, target), article_code
        ):
            return last_response
        if attempt < attempts - 1 and delay_ms:
            time.sleep(delay_ms / 1000)
    raise BejermanTransientError(
        f"Bejerman aceptó el ENT pero todavía no se confirmó stock positivo para la partida "
        f"{serial} en el depósito {target}."
    )


def _process_stock_transfer(job: dict[str, Any], client: BejermanSDKClient) -> str:
    validate_bejerman_config()
    sync_type = (job.get("sync_type") or "").strip()
    request_payload = _dict_payload(job.get("request_payload"))
    response_payload = _dict_payload(job.get("response_payload"))
    if sync_type == SYNC_TYPE_STOCK_STR_TO_STL and not ingreso_is_internal_equipment(int(job["ingreso_id"])):
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={
                **request_payload,
                "skipped_not_applicable": True,
                "reason": "Equipo vendido o de cliente; no corresponde transferir a STL.",
                "expected_target_deposit": str(_setting("BEJERMAN_CLIENT_TARGET_DEPOSIT", "STC") or "STC").strip()
                or "STC",
            },
            response_payload=response_payload,
        )
        return JOB_STATUS_SUCCEEDED

    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        raise BejermanBlockedError("Número de serie requerido para sincronizar Bejerman")

    source = job.get("source_deposit") or "STR"
    target = job.get("target_deposit") or "STL"
    # Una misma partida puede tener stock en varios artículos del mismo
    # depósito: el CPAP y su humidificador comparten el número de serie. El
    # artículo que el equipo ya tiene resuelto desempata cuál hay que mover.
    equipment_confirmed_article_code, equipment_mapped_article_code = _mapping_article_codes_for_job(job)
    expected_article_codes = [
        equipment_confirmed_article_code,
        (job.get("article_code") or "").strip(),
        str(request_payload.get("article_code") or "").strip(),
        equipment_mapped_article_code,
    ]
    equipment_article_code = next((code for code in expected_article_codes if code), "")
    sal_done = bool(request_payload.get("sal_done")) or request_payload.get("transfer_phase") in {
        TRANSFER_PHASE_SAL_DONE,
        TRANSFER_PHASE_ENT_PENDING,
        TRANSFER_PHASE_DONE,
        TRANSFER_PHASE_TARGET_ENTRY_DONE,
    }

    target_response = client.stock_by_deposit_partida(target, serial)
    # La idempotencia se mide por artículo y no por partida: si el humidificador
    # ya se movió, su gemelo el CPAP —misma partida, otro artículo— todavía no.
    target_records = _records_for_equipment_article(
        _records_for_partida(target_response, serial, target),
        equipment_article_code,
    )

    if target_records:
        article_code = _article_code_for_partida_deposit(
            target_records,
            serial=serial,
            deposit=target,
            expected_article_codes=expected_article_codes,
        )
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={
                **request_payload,
                "idempotent": True,
                "numero_serie": serial,
                "target_deposit": target,
                "transfer_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
            },
            response_payload={**response_payload, "target": target_response},
            article_code=article_code,
        )
        return JOB_STATUS_SUCCEEDED

    source_response = client.stock_by_deposit_partida(source, serial)
    source_known_records = _records_for_partida_any_quantity(source_response, serial, source)
    source_records = _records_for_partida(source_response, serial, source)

    other_records = [
        record
        for record in _records_for_equipment_article(
            _other_positive_records(target_response, serial, target),
            equipment_article_code,
        )
        if _norm(_deposit_from_record(record) or "") != _norm(source)
    ]
    other_deposits = sorted(
        {
            (_deposit_from_record(record) or "").strip()
            for record in other_records
            if (_deposit_from_record(record) or "").strip()
        }
    )
    # El equipo está en el taller y hay que entregarlo: que Bejerman lo tenga
    # anotado en otro depósito es una diferencia para conciliar después, no un
    # motivo para frenar el remito. Si el origen tiene stock se descuenta de ahí;
    # si no, se descuenta del depósito donde Bejerman realmente lo tiene, así el
    # stock se mueve en vez de duplicarse. La diferencia queda registrada en el
    # job para poder conciliarla más tarde.
    conciliation: dict[str, Any] | None = None
    if other_records:
        conciliation = {
            "serial": serial,
            "expected_source_deposit": source,
            "found_in": other_deposits,
            "records": _stock_records_summary(other_records),
        }

    transfer_source = (
        str(request_payload.get("source_stock_deposit") or source).strip() or source
        if sal_done
        else source
    )
    sal_records = source_records
    if not sal_done and not source_records and other_records:
        if len(other_deposits) != 1:
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                response_payload={**response_payload, "source": source_response, "target": target_response},
                article_code=(job.get("article_code") or "").strip(),
            )
            raise BejermanBlockedError(
                f"Partida {serial} con stock positivo en varios depósitos "
                f"({', '.join(other_deposits) or '-'}); no se puede saber de cuál descontar"
            )
        transfer_source = other_deposits[0]
        sal_records = other_records

    article_resolution_source = "job_or_mapping"
    if sal_done:
        # La SAL ya fue confirmada: el reintento debe conservar exactamente el
        # artículo con el que se descontó, aunque el stock posterior cambie.
        article_code = (
            str(request_payload.get("article_code") or "").strip()
            or (job.get("article_code") or "").strip()
        )
        article_resolution_source = "confirmed_source_exit"
    elif sal_records:
        # La terna artículo + partida + depósito se valida en Bejerman. El
        # artículo del stock real prevalece sobre cualquier mapeo del modelo.
        article_code = _article_code_for_partida_deposit(
            sal_records,
            serial=serial,
            deposit=transfer_source,
            expected_article_codes=expected_article_codes,
        )
        article_resolution_source = "positive_source_stock"
    elif source_known_records:
        article_code = _article_code_for_partida_deposit(
            source_known_records,
            serial=serial,
            deposit=source,
            expected_article_codes=expected_article_codes,
        )
        article_resolution_source = "source_stock_without_positive_quantity"
    else:
        article_code = (
            str(request_payload.get("article_code") or "").strip()
            or (job.get("article_code") or "").strip()
        )
    if not article_code:
        article_code = _resolve_article_for_job(job, client)
        article_resolution_source = "resolved_mapping"

    # La SAL puede haber quedado deshecha: cuando el ENT falla, el botón de
    # recuperación devuelve la unidad al origen con un ENT de reposición, pero
    # el payload sigue diciendo `sal_done`. Reintentar así saltea la SAL y el
    # ENT del destino DUPLICA el stock (pasó el 27/08/2026 con 18 equipos). Si
    # el origen todavía tiene el artículo, la salida hay que volver a emitirla.
    repos_detected = False
    if sal_done and _records_for_equipment_article(sal_records, article_code):
        sal_done = False
        repos_detected = True
        article_resolution_source = "source_stock_after_repos"
        logger.warning(
            "Job Bejerman %s decía sal_done pero la partida %s sigue en %s con el artículo %s: "
            "se vuelve a emitir la SAL para no duplicar stock",
            job.get("id"),
            serial,
            transfer_source,
            article_code,
        )

    confirmed_article_code = ""
    if not sal_done and article_resolution_source in {
        "positive_source_stock",
        "source_stock_without_positive_quantity",
    }:
        confirmed_article_code = _assert_source_article_matches_confirmed_mapping(
            job,
            serial=serial,
            deposit=transfer_source,
            source_article_code=article_code,
            confirmed_article_code=equipment_confirmed_article_code,
        )

    next_request_payload = {
        **request_payload,
        "article_code": article_code,
        "source_stock_deposit": transfer_source,
        "stock_article_resolution": {
            "source": article_resolution_source,
            "source_deposit": transfer_source,
            "article_code": article_code,
            "confirmed_article_code": confirmed_article_code,
        },
        **({"stock_conciliation": conciliation} if conciliation else {}),
        **({"sal_reemitida_tras_repos": True} if repos_detected else {}),
    }
    next_response_payload = {
        **response_payload,
        "source": source_response,
        "target": target_response,
    }
    if sal_records and not sal_done:
        validate_bejerman_config(comprobante="BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE")
        sal_payload = build_stock_transfer_out_payload(job, article_code, source_deposit=transfer_source)
        if repos_detected:
            # Bejerman deduplica por IdOrigen: la SAL original ya usó el de la
            # OS, así que la reemisión necesita el suyo o rebota con
            # "el mismo ya existe... importación anterior".
            for item in sal_payload:
                item["Comprobante_IdOrigen"] = f"{item.get('Comprobante_IdOrigen', '')}-REPOS"[:50]
        sal_request_payload = {
            **next_request_payload,
            "transfer_phase": "sal_pending",
            "sal_comprobantes": sal_payload,
        }
        _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload=sal_request_payload, article_code=article_code)
        sal_response = client.ingresar_lista_comprobantes_json(sal_payload)
        next_request_payload = {
            **sal_request_payload,
            "sal_done": True,
            "transfer_phase": TRANSFER_PHASE_SAL_DONE,
        }
        next_response_payload = {**next_response_payload, "sal": sal_response}
        _finish_job(
            job["id"],
            JOB_STATUS_RUNNING,
            request_payload=next_request_payload,
            response_payload=next_response_payload,
            article_code=article_code,
        )

    ent_payload = build_stock_transfer_in_payload(job, article_code)
    ent_request_payload = {
        **next_request_payload,
        "transfer_phase": TRANSFER_PHASE_ENT_PENDING,
        "ent_comprobantes": ent_payload,
        "target_stock_entry_deposit": target,
    }
    _finish_job(
        job["id"],
        JOB_STATUS_RUNNING,
        request_payload=ent_request_payload,
        response_payload=next_response_payload,
        article_code=article_code,
    )
    ent_response = client.ingresar_lista_comprobantes_json(ent_payload)
    ent_response_payload = {**next_response_payload, "target_stock_entry": ent_response}
    _finish_job(
        job["id"],
        JOB_STATUS_RUNNING,
        request_payload=ent_request_payload,
        response_payload=ent_response_payload,
        article_code=article_code,
    )
    target_verification = _verify_stock_transfer_target(
        client,
        target=target,
        serial=serial,
        article_code=article_code,
    )
    _finish_job(
        job["id"],
        JOB_STATUS_SUCCEEDED,
        request_payload={
            **ent_request_payload,
            "target_stock_entry_done": True,
            "transfer_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
        },
        response_payload={
            **ent_response_payload,
            "target_stock_verification": target_verification,
        },
        article_code=article_code,
    )
    return JOB_STATUS_SUCCEEDED


def _process_claimed_job(job: dict[str, Any], client: BejermanSDKClient, max_attempts: int) -> str:
    try:
        sync_type = (job.get("sync_type") or "").strip()
        if sync_type == SYNC_TYPE_STOCK_ENTRY_STR:
            return _process_stock_entry(job, client)
        if sync_type == SYNC_TYPE_STOCK_EXIT_RTS:
            return _process_stock_exit(job, client)
        if sync_type == SYNC_TYPE_STOCK_TO_DESGUACE:
            return _process_stock_to_desguace(job, client)
        if sync_type == SYNC_TYPE_STOCK_FROM_DESGUACE:
            return _process_stock_from_desguace(job, client)
        if sync_type in (
            SYNC_TYPE_STOCK_STR_TO_STL,
            SYNC_TYPE_STOCK_STR_TO_STC,
            SYNC_TYPE_STOCK_STR_TO_VAL,
            SYNC_TYPE_STOCK_STR_TO_STCL,
        ):
            return _process_stock_transfer(job, client)
        raise BejermanBlockedError(f"Tipo de sincronización no soportado: {sync_type}")
    except BejermanConfigError as exc:
        error = str(exc)
        if BEJERMAN_CREDENTIALS_REQUIRED in error:
            # Se reabre solo cuando la persona las carga (ver
            # reopen_jobs_waiting_for_credentials); decirlo acá evita que alguien
            # reintente a mano diez veces esperando que cambie algo.
            error = (
                f"{BEJERMAN_CREDENTIALS_REQUIRED} La operación se reanuda sola en cuanto el usuario "
                "que la originó cargue sus credenciales en su perfil."
            )
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=error)
        return JOB_STATUS_BLOCKED
    except BejermanBlockedError as exc:
        if _is_retryable_sync_message(str(exc)):
            return _retry_job(job, str(exc), max_attempts)
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=str(exc))
        return JOB_STATUS_BLOCKED
    except BejermanTransientError as exc:
        return _retry_job(job, str(exc), max_attempts)
    except Exception as exc:
        logger.exception("Error sincronizando job Bejerman %s", job.get("id"))
        return _retry_job(job, str(exc), max_attempts)


def process_bejerman_jobs(
    limit: int = 10,
    job_id: int | None = None,
    client: BejermanSDKClient | None = None,
    *,
    actor_user_id: int | None = None,
    reopen_blocked: bool = False,
) -> dict[str, int]:
    if not bool(_setting("BEJERMAN_SYNC_ENABLED", False)):
        return {"processed": 0, "disabled": 1}
    if reopen_blocked and job_id is None:
        raise ValueError("reopen_blocked requiere indicar job_id")
    max_attempts = _positive_int_setting("BEJERMAN_MAX_ATTEMPTS", 8)
    claim_max_attempts = _sync_transient_max_attempts(max_attempts)
    stats = {"processed": 0, JOB_STATUS_SUCCEEDED: 0, JOB_STATUS_FAILED: 0, JOB_STATUS_BLOCKED: 0}
    for _ in range(max(1, int(limit or 1))):
        with transaction.atomic():
            if reopen_blocked and job_id is not None:
                _reopen_blocked_job_inline(job_id, actor_user_id)
            job = _claim_job(max_attempts=claim_max_attempts, job_id=job_id)
        if not job:
            break
        effective_actor_user_id = (
            actor_user_id if actor_user_id is not None else job.get("actor_user_id")
        )
        previous_error = str(job.get("last_error") or "")
        if int(job.get("attempts") or 0) > max_attempts and not _is_retryable_sync_message(
            previous_error
        ):
            status = JOB_STATUS_BLOCKED
            _finish_job(
                job["id"],
                status,
                error=f"Máximos reintentos agotados: {previous_error or 'error de sincronización'}",
            )
        elif client is None and not effective_actor_user_id:
            status = JOB_STATUS_BLOCKED
            _finish_job(
                job["id"],
                status,
                error="Requiere ejecutar con sesión de usuario y credenciales Bejerman personales.",
            )
        else:
            job_client = client or BejermanSDKClient(
                company_key=job.get("company_key") or DEFAULT_INGRESS_COMPANY_KEY,
                actor_user_id=effective_actor_user_id,
            )
            try:
                status = _process_claimed_job(job, job_client, max_attempts)
            finally:
                if client is None:
                    close = getattr(job_client, "close", None)
                    if callable(close):
                        close()
        stats["processed"] += 1
        stats[status] = stats.get(status, 0) + 1
        if job_id is not None:
            break
    return stats


def _job_deposits_for_article_reconcile(job: dict[str, Any]) -> list[str]:
    deposits: list[str] = []
    for key in ("source_deposit", "target_deposit"):
        deposit = str(job.get(key) or "").strip()
        if not deposit or _norm(deposit) in {"NEXORA", "SALIDA", "AUTO"}:
            continue
        if deposit not in deposits:
            deposits.append(deposit)
    return deposits


def _update_job_article_code_if_empty(job_id: int, article_code: str) -> None:
    q(
        """
        UPDATE bejerman_sync_jobs
           SET article_code = COALESCE(NULLIF(article_code, ''), %s),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [article_code, job_id],
    )


def reconcile_article_mapping_for_job(job: dict[str, Any], client: BejermanSDKClient) -> dict[str, Any]:
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        return {"status": "skipped", "reason": "sin_serie", "job_id": job.get("id")}

    records: list[dict[str, Any]] = []
    checked_deposits = _job_deposits_for_article_reconcile(job)
    for deposit in checked_deposits:
        response = client.stock_by_deposit_partida(deposit, serial)
        # Algunos puentes devuelven registros de todos los depósitos aunque se
        # consulte uno. Filtrar acá evita que VAL/STL cambie el mapeo de una
        # transferencia cuya partida está realmente en STR.
        records.extend(_records_for_partida_any_quantity(response, serial, deposit))

    article_code = remember_article_mapping_from_stock_records(
        job,
        records,
        source="stock_partida_reconcile",
    )
    if article_code:
        _update_job_article_code_if_empty(int(job["id"]), article_code)
        return {
            "status": "mapped_from_stock",
            "job_id": job.get("id"),
            "article_code": article_code,
            "deposits": checked_deposits,
        }

    try:
        context = _ingreso_context(int(job["ingreso_id"]))
        mapping = _mapping_for_context(context, job.get("company_key"))
    except Exception:
        mapping = None
    existing_code = (mapping or {}).get("article_code") or ""
    if existing_code:
        _update_job_article_code_if_empty(int(job["id"]), existing_code)
        return {
            "status": "mapped_from_local",
            "job_id": job.get("id"),
            "article_code": existing_code,
            "deposits": checked_deposits,
        }

    return {"status": "not_found", "job_id": job.get("id"), "deposits": checked_deposits}


def reconcile_article_mappings_from_stock(
    *,
    limit: int = 250,
    job_id: int | None = None,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    validate_bejerman_config()
    client = client or BejermanSDKClient()
    params: list[Any] = list(ARTICLE_RECONCILE_SYNC_TYPES)
    job_filter = ""
    if job_id is not None:
        job_filter = "AND id = %s"
        params.append(job_id)
    params.append(max(1, int(limit or 1)))
    jobs = q(
        f"""
        SELECT *
          FROM bejerman_sync_jobs
         WHERE sync_type IN ({",".join(["%s"] * len(ARTICLE_RECONCILE_SYNC_TYPES))})
           AND NULLIF(TRIM(numero_serie), '') IS NOT NULL
           AND (
             COALESCE(article_code, '') = ''
             OR status IN ('blocked','failed','pending')
           )
           {job_filter}
         ORDER BY updated_at DESC, id DESC
         LIMIT %s
        """,
        params,
    ) or []

    stats: dict[str, Any] = {
        "checked": 0,
        "mapped_from_stock": 0,
        "mapped_from_local": 0,
        "not_found": 0,
        "skipped": 0,
        "errors": 0,
        "items": [],
    }
    for job in jobs:
        stats["checked"] += 1
        try:
            result = reconcile_article_mapping_for_job(job, client)
        except Exception as exc:
            logger.exception("No se pudo conciliar artículo Bejerman para job %s", job.get("id"))
            result = {"status": "error", "job_id": job.get("id"), "error": str(exc)}
        status = result.get("status") or "error"
        if status in stats:
            stats[status] += 1
        else:
            stats["errors"] += 1
        stats["items"].append(result)
    return stats


def restore_target_stock_for_job(job: dict[str, Any], client: BejermanSDKClient) -> dict[str, Any]:
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        return {"status": "skipped", "reason": "sin_serie", "job_id": job.get("id")}

    target = str(job.get("target_deposit") or "").strip()
    if not target or _norm(target) in {"NEXORA", "SALIDA"}:
        return {"status": "skipped", "reason": "sin_deposito_destino", "job_id": job.get("id")}

    request_payload = _dict_payload(job.get("request_payload"))
    response_payload = _dict_payload(job.get("response_payload"))
    target_response = client.stock_by_deposit_partida(target, serial)
    target_records = _records_for_partida(target_response, serial, target)
    target_all_records = _records_for_partida_any_quantity(target_response, serial)
    stock_article_code = remember_article_mapping_from_stock_records(
        job, target_all_records, source="target_stock_restore_target_partida"
    )

    if target_records:
        article_code = _extract_article_code(target_records) or stock_article_code or (job.get("article_code") or "")
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={
                **request_payload,
                "restore_target_stock": True,
                "restore_idempotent": True,
                "numero_serie": serial,
                "target_deposit": target,
                "transfer_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
            },
            response_payload={**response_payload, "target": target_response},
            article_code=article_code,
        )
        return {
            "status": "already_present",
            "job_id": job.get("id"),
            "serial": serial,
            "target_deposit": target,
            "article_code": article_code,
        }

    source = str(job.get("source_deposit") or "STR").strip() or "STR"
    source_response = client.stock_by_deposit_partida(source, serial)
    source_all_records = _records_for_partida_any_quantity(source_response, serial)
    stock_article_code = stock_article_code or remember_article_mapping_from_stock_records(
        job,
        [*source_all_records, *target_all_records],
        source="target_stock_restore_partida",
    )
    positive_elsewhere = []
    seen_positive_elsewhere: set[tuple[str, str, str]] = set()
    for record in [
        *_records_for_partida(target_response, serial),
        *_records_for_partida(source_response, serial),
    ]:
        deposit = _deposit_from_record(record)
        if deposit and _norm(deposit) == _norm(target):
            continue
        key = (_norm(deposit), _norm(_article_code_from_record(record)), _norm(_partida_from_record(record)))
        if key in seen_positive_elsewhere:
            continue
        seen_positive_elsewhere.add(key)
        positive_elsewhere.append(record)
    if positive_elsewhere:
        article_code = _extract_article_code(positive_elsewhere) or stock_article_code or (job.get("article_code") or "")
        deposits = sorted({_deposit_from_record(record) or "-" for record in positive_elsewhere})
        error = (
            f"No se restaura stock en {target}: la partida {serial} ya tiene stock positivo "
            f"en otro depósito ({', '.join(deposits)}); requiere conciliación."
        )
        _finish_job(
            job["id"],
            JOB_STATUS_BLOCKED,
            error=error,
            response_payload={
                **response_payload,
                "source": source_response,
                "target": target_response,
                "stock_restore_duplicate_guard": {
                    "serial": serial,
                    "target_deposit": target,
                    "positive_elsewhere": _stock_records_summary(positive_elsewhere),
                },
            },
            article_code=article_code,
        )
        return {
            "status": "blocked",
            "job_id": job.get("id"),
            "serial": serial,
            "target_deposit": target,
            "article_code": article_code,
            "error": error,
        }
    article_code = (
        (job.get("article_code") or "").strip()
        or str(request_payload.get("article_code") or "").strip()
        or _extract_article_code(source_all_records)
        or stock_article_code
    )
    if not article_code:
        try:
            article_code = _resolve_article_for_job(job, client)
        except BejermanBlockedError as exc:
            error = str(exc)
            diagnostic = _restore_stock_diagnostic_payload(
                serial=serial,
                source=source,
                target=target,
                source_records=source_all_records,
                target_records=target_all_records,
                error=error,
            )
            response_after_article_lookup = _job_response_payload(int(job["id"]))
            _finish_job(
                job["id"],
                JOB_STATUS_BLOCKED,
                error=_restore_stock_error_message(
                    serial=serial,
                    source=source,
                    target=target,
                    source_records=source_all_records,
                    target_records=target_all_records,
                    error=error,
                ),
                response_payload={
                    **response_after_article_lookup,
                    "stock_restore_diagnostic": diagnostic,
                },
            )
            return {
                "status": "not_found",
                "job_id": job.get("id"),
                "serial": serial,
                "target_deposit": target,
                "error": error,
            }

    ent_payload = build_stock_transfer_in_payload(job, article_code)
    next_request_payload = {
        **request_payload,
        "restore_target_stock": True,
        "article_code": article_code,
        "ent_comprobantes": ent_payload,
        "target_stock_entry_deposit": target,
    }
    next_response_payload = {
        **response_payload,
        "source": source_response,
        "target": target_response,
    }
    _finish_job(
        job["id"],
        JOB_STATUS_RUNNING,
        request_payload=next_request_payload,
        response_payload=next_response_payload,
        article_code=article_code,
    )
    try:
        ent_response = client.ingresar_lista_comprobantes_json(ent_payload)
    except BejermanBlockedError as exc:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=str(exc), article_code=article_code)
        return {
            "status": "blocked",
            "job_id": job.get("id"),
            "serial": serial,
            "target_deposit": target,
            "article_code": article_code,
            "error": str(exc),
        }

    _finish_job(
        job["id"],
        JOB_STATUS_SUCCEEDED,
        request_payload={
            **next_request_payload,
            "target_stock_entry_done": True,
            "transfer_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
        },
        response_payload={**next_response_payload, "target_stock_entry": ent_response},
        article_code=article_code,
    )
    return {
        "status": "restored",
        "job_id": job.get("id"),
        "serial": serial,
        "target_deposit": target,
        "article_code": article_code,
    }


def restore_target_stock_from_jobs(
    *,
    limit: int = 250,
    job_id: int | None = None,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    validate_bejerman_config()
    client = client or BejermanSDKClient()
    params: list[Any] = list(TARGET_STOCK_RESTORE_SYNC_TYPES)
    job_filter = ""
    if job_id is not None:
        job_filter = "AND id = %s"
        params.append(job_id)
    params.append(max(1, int(limit or 1)))
    jobs = q(
        f"""
        SELECT DISTINCT ON (numero_serie, target_deposit) *
          FROM bejerman_sync_jobs
         WHERE sync_type IN ({",".join(["%s"] * len(TARGET_STOCK_RESTORE_SYNC_TYPES))})
           AND NULLIF(TRIM(numero_serie), '') IS NOT NULL
           AND NULLIF(TRIM(target_deposit), '') IS NOT NULL
           AND target_deposit NOT IN ('NEXORA', 'SALIDA')
           {job_filter}
         ORDER BY numero_serie, target_deposit, updated_at DESC, id DESC
         LIMIT %s
        """,
        params,
    ) or []

    stats: dict[str, Any] = {
        "checked": 0,
        "restored": 0,
        "already_present": 0,
        "not_found": 0,
        "skipped": 0,
        "blocked": 0,
        "errors": 0,
        "items": [],
    }
    for job in jobs:
        stats["checked"] += 1
        try:
            result = restore_target_stock_for_job(job, client)
        except Exception as exc:
            logger.exception("No se pudo restaurar stock Bejerman para job %s", job.get("id"))
            result = {"status": "error", "job_id": job.get("id"), "error": str(exc)}
        status = result.get("status") or "error"
        if status in stats:
            stats[status] += 1
        else:
            stats["errors"] += 1
        stats["items"].append(result)
    return stats


def run_bejerman_sync_loop(
    interval_seconds: int = 30,
    limit: int = 10,
    sale_items_interval_seconds: int | None = None,
):
    transient_reopen_interval = _positive_int_setting(
        "BEJERMAN_SYNC_TRANSIENT_REOPEN_INTERVAL_SECONDS",
        300,
        minimum=0,
    )
    blocked_alert_interval = _positive_int_setting(
        "BEJERMAN_SYNC_BLOCKED_ALERT_INTERVAL_SECONDS",
        900,
        minimum=0,
    )
    sale_interval = (
        int(sale_items_interval_seconds)
        if sale_items_interval_seconds is not None
        else int(getattr(settings, "BEJERMAN_SALE_ITEMS_LOOP_INTERVAL_SECONDS", 21600) or 0)
    )
    sale_days = int(getattr(settings, "BEJERMAN_SALE_ITEMS_SYNC_DAYS", 2) or 2)
    sale_max = int(getattr(settings, "BEJERMAN_SALE_ITEMS_SYNC_MAX_COMPROBANTES", 500) or 500)
    sale_companies = [
        str(key).strip().upper()
        for key in (getattr(settings, "BEJERMAN_SALE_ITEMS_SYNC_COMPANY_KEYS", None) or [])
        if str(key).strip()
    ] or [getattr(settings, "BEJERMAN_SALE_ITEMS_SYNC_COMPANY_KEY", "SEPID") or "SEPID"]
    sale_startup_delay = max(
        0,
        int(getattr(settings, "BEJERMAN_SALE_ITEMS_STARTUP_DELAY_SECONDS", 300) or 0),
    )
    next_transient_reopen_at = time.monotonic()
    next_blocked_alert_at = time.monotonic()
    next_sale_sync_at = time.monotonic() + sale_startup_delay
    logger.info(
        "bejerman_sale_items_scheduler interval_seconds=%s startup_delay_seconds=%s days=%s max_comprobantes=%s",
        sale_interval,
        sale_startup_delay,
        sale_days,
        sale_max,
    )
    while True:
        now = time.monotonic()
        if transient_reopen_interval > 0 and now >= next_transient_reopen_at:
            try:
                reopened = reopen_transient_blocked_jobs()
                if reopened:
                    logger.info(
                        "bejerman_sync_transient_jobs_reopened count=%s",
                        reopened,
                    )
            except Exception:
                logger.exception("bejerman_sync_transient_reopen falló")
            try:
                stuck = recover_stuck_sync_jobs()
                if stuck.get("requeued_running") or stuck.get("blocked_exhausted"):
                    logger.info("bejerman_sync_stuck_jobs_recovered stats=%s", stuck)
            except Exception:
                logger.exception("bejerman_sync_stuck_recovery falló")
            try:
                with_credentials = reopen_jobs_waiting_for_credentials()
                if with_credentials:
                    logger.info(
                        "bejerman_sync_credential_jobs_reopened count=%s",
                        with_credentials,
                    )
            except Exception:
                logger.exception("bejerman_sync_credential_reopen falló")
            finally:
                next_transient_reopen_at = time.monotonic() + transient_reopen_interval
        try:
            stats = process_bejerman_jobs(limit=limit)
            logger.info("bejerman_sync_loop stats=%s", stats)
        except Exception:
            logger.exception("bejerman_sync_loop falló")
        if blocked_alert_interval > 0 and time.monotonic() >= next_blocked_alert_at:
            try:
                alert_stats = queue_stale_blocked_bejerman_job_alerts()
                if alert_stats.get("matched") or alert_stats.get("errors"):
                    logger.info(
                        "bejerman_sync_blocked_alerts stats=%s",
                        alert_stats,
                    )
            except Exception:
                logger.exception("bejerman_sync_blocked_alerts falló")
            finally:
                next_blocked_alert_at = time.monotonic() + blocked_alert_interval
        if sale_interval > 0 and time.monotonic() >= next_sale_sync_at:
            try:
                from .bejerman_sales import sync_bejerman_sale_items
                from .bejerman_sdk import BejermanSDKClient as SalesBejermanSDKClient

                # Una empresa que falla no puede dejar a la otra sin sincronizar: si MG BIO
                # se cae, SEPID igual tiene que actualizar su caché de ventas (y viceversa).
                for sale_company in sale_companies:
                    sale_started_at = time.monotonic()
                    try:
                        client = SalesBejermanSDKClient(
                            company_key=sale_company,
                            allow_system_credentials=True,
                        )
                        sale_stats = sync_bejerman_sale_items(
                            client,
                            days=sale_days,
                            max_comprobantes=sale_max,
                        )
                        logger.info(
                            "bejerman_sale_items_sync company_key=%s duration_ms=%s stats=%s",
                            sale_company,
                            int((time.monotonic() - sale_started_at) * 1000),
                            sale_stats,
                        )
                    except Exception:
                        logger.exception(
                            "bejerman_sale_items_sync falló para company_key=%s",
                            sale_company,
                        )
            except Exception:
                logger.exception("bejerman_sale_items_sync falló")
            finally:
                next_sale_sync_at = time.monotonic() + sale_interval
        time.sleep(max(1, int(interval_seconds or 30)))
