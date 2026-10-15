import json
import logging
import re
import time
import unicodedata
import zlib
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from .bejerman_companies import DEFAULT_INGRESS_COMPANY_KEY, require_company
from .bejerman_retry_advice import can_change_source, stock_diagnosis
from .mg_codes import MG_CODE_RE
from .bejerman_partida_grafias import stock_partida_sin_distinguir_mayusculas
from .bejerman_partida_observacion import observacion_partida_de_equipo, observacion_partida_de_ingreso
from .bejerman_sync_alerts import queue_stale_blocked_bejerman_job_alerts
from .bejerman_sdk import (
    is_bejerman_already_imported_message,
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
# Cambio manual entre ubicaciones físicas de NEXORA. A diferencia de los tipos
# anteriores, el origen y el destino salen del selector y no de una transición de
# estado de la OS.
SYNC_TYPE_STOCK_LOCATION_TRANSFER = "stock_location_transfer"
SYNC_TYPE_STOCK_EXIT_RTS = "stock_exit_rts"  # Nombre legacy; la salida física la gestiona Portal.
SYNC_TYPE_STOCK_TO_DESGUACE = "stock_to_desguace"
SYNC_TYPE_STOCK_FROM_DESGUACE = "stock_from_desguace"
# Alta de un equipo propio nuevo: la unidad llega al depósito central y hay que dejarla en la
# estantería de alquiler. Es el único tipo que no nace de una orden de servicio.
SYNC_TYPE_STOCK_VAL_TO_STL = "stock_val_to_stl"
ARTICLE_RECONCILE_SYNC_TYPES = (
    SYNC_TYPE_STOCK_STR_TO_STL,
    SYNC_TYPE_STOCK_STR_TO_STC,
    SYNC_TYPE_STOCK_STR_TO_VAL,
    SYNC_TYPE_STOCK_STR_TO_STCL,
    SYNC_TYPE_STOCK_LOCATION_TRANSFER,
    SYNC_TYPE_STOCK_TO_DESGUACE,
    SYNC_TYPE_STOCK_FROM_DESGUACE,
    SYNC_TYPE_STOCK_VAL_TO_STL,
)
TARGET_STOCK_RESTORE_SYNC_TYPES = (
    SYNC_TYPE_STOCK_STR_TO_STL,
    SYNC_TYPE_STOCK_STR_TO_STC,
    SYNC_TYPE_STOCK_STR_TO_VAL,
    SYNC_TYPE_STOCK_STR_TO_STCL,
    SYNC_TYPE_STOCK_LOCATION_TRANSFER,
    SYNC_TYPE_STOCK_VAL_TO_STL,
)

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_BLOCKED = "blocked"
JOB_STATUS_CANCELLED = "cancelled"

# Criterio único de número interno (`mg_codes`): hasta 6 dígitos y con ceros a la izquierda
# ("MG 001678", "MG 002562"). Con el límite en 4 esos equipos no se reconocían como propios y
# su stock terminaba en STC en vez de STL. Son 13 en producción.
INTERNAL_CODE_RE = MG_CODE_RE
RENTAL_FLEET_MOTIVOS = ("baja alquiler", "reparacion alquiler")
TRANSFER_PHASE_SAL_DONE = "sal_done"
TRANSFER_PHASE_ENT_PENDING = "ent_pending"
TRANSFER_PHASE_DONE = "done"
TRANSFER_PHASE_TARGET_ENTRY_DONE = "target_entry_done"
# Alta masiva de equipos propios: la transferencia VAL->STL de todos los equipos viaja en UNA
# SAL y UNA ENT (tabla bejerman_stock_lotes). Cada equipo conserva su job, que es lo que muestra
# el panel; el lote guarda la composición congelada y la fase, porque un reenvío tiene que
# repetir exactamente el mismo comprobante. `review` quiere decir que el lote quedó a medias en
# Bejerman y alguien tiene que mirarlo antes de seguir.
LOTE_PHASE_PENDING = "pending"
LOTE_PHASE_SAL_PENDING = "sal_pending"
LOTE_PHASE_SAL_DONE = "sal_done"
LOTE_PHASE_ENT_PENDING = "ent_pending"
LOTE_PHASE_DONE = "done"
LOTE_PHASE_REVIEW = "review"
# Fases en las que el lote ya pudo emitir: ahí no se descarta ni se restaura un equipo suelto,
# porque su renglón viaja en un comprobante compartido.
LOTE_EMITTING_PHASES = (LOTE_PHASE_SAL_PENDING, LOTE_PHASE_SAL_DONE, LOTE_PHASE_ENT_PENDING)
LOTE_ADVISORY_LOCK_NAMESPACE = "bejerman_stock_lote"
STOCK_LOTE_MAX_ITEMS = 100
NEXORA_STOCK_ENTRY_MESSAGE = (
    "El RIS/RDA de ingreso se emite en Bejerman antes de NEXORA; "
    "NEXORA no emite ENT/RIS de ingreso. Verificar remito emitido o stock en STR."
)
PORTAL_STOCK_EXIT_MESSAGE = "Salida física Bejerman gestionada por Portal; NEXORA no emite salida final"
LEGACY_BLOCKED_SYNC_TYPES = (SYNC_TYPE_STOCK_ENTRY_STR, SYNC_TYPE_STOCK_EXIT_RTS)
# Los unicos `sync_type` que el worker sabe ejecutar. Un job con cualquier otro
# valor -- porque lo creo un script de una sola vez, o porque el tipo se dejo de
# implementar -- esta muerto al nacer: falla, agota reintentos y se queda en el
# panel para siempre ofreciendo un "Reintentar" que no puede funcionar. Saberlo
# de antemano permite decirlo con todas las letras y ofrecer descartarlo.
SUPPORTED_SYNC_TYPES = (
    SYNC_TYPE_STOCK_ENTRY_STR,
    SYNC_TYPE_STOCK_EXIT_RTS,
    SYNC_TYPE_STOCK_TO_DESGUACE,
    SYNC_TYPE_STOCK_FROM_DESGUACE,
    SYNC_TYPE_STOCK_STR_TO_STL,
    SYNC_TYPE_STOCK_STR_TO_STC,
    SYNC_TYPE_STOCK_STR_TO_VAL,
    SYNC_TYPE_STOCK_STR_TO_STCL,
    SYNC_TYPE_STOCK_LOCATION_TRANSFER,
    SYNC_TYPE_STOCK_VAL_TO_STL,
)


def is_supported_sync_type(sync_type: Any) -> bool:
    return str(sync_type or "").strip() in SUPPORTED_SYNC_TYPES
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


def _is_already_imported_message(value: Any) -> bool:
    """Bejerman dice que el comprobante ya está. Ver
    `bejerman_sdk.is_bejerman_already_imported_message`."""

    return is_bejerman_already_imported_message(value)


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
        # Y compara la partida distinguiendo mayúsculas: ver bejerman_partida_grafias.
        return stock_partida_sin_distinguir_mayusculas(
            lambda grafia: self.execute("STOCK", "ObtenerStockPartida", params=[grafia]),
            serial,
        )

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


def _device_context(device_id: int, company_key: str | None = None) -> dict[str, Any]:
    """Mismo contexto que `_ingreso_context`, pero para un equipo sin orden de servicio.

    Un equipo propio nuevo se da de alta en Equipos con su MG y nunca pasa por un ingreso
    (ver `_rental_device_context_sql` en delivery_orders.py), asi que el movimiento de stock
    que lo deja en la estanteria de alquiler no tiene de donde sacar el contexto del equipo.
    Lo que la cadena de resolucion de articulo necesita -- model_id, variante, marca, modelo --
    esta todo en `devices`; la empresa, que en el otro camino sale de `ingresos`, se pasa.
    """
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
    resolved_company = str(company_key or "").strip() or DEFAULT_INGRESS_COMPANY_KEY
    row = q(
        f"""
        SELECT NULL::INTEGER AS ingreso_id,
               '' AS motivo,
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
               '' AS ingreso_variante,
               {device_variante_sql} AS device_variante,
               {model_variante_sql} AS model_variante,
               COALESCE(b.nombre, '') AS marca
          FROM devices d
          LEFT JOIN customers c ON c.id = d.customer_id
          LEFT JOIN models m ON m.id = d.model_id
          LEFT JOIN marcas b ON b.id = d.marca_id
         WHERE d.id = %s
        """,
        [device_id],
        one=True,
    )
    if not row:
        raise BejermanBlockedError(f"Equipo {device_id} no encontrado")
    row["empresa_bejerman"] = resolved_company
    row["empresa_facturar"] = resolved_company
    row["variante"] = (
        (row.get("device_variante") or "").strip()
        or (row.get("model_variante") or "").strip()
    )
    row["variante_norm"] = normalize_article_variant(row.get("variante"))
    return row


def _job_context(job: dict[str, Any]) -> dict[str, Any]:
    """Contexto del equipo del job, venga de una orden de servicio o de la ficha del equipo."""
    ingreso_id = job.get("ingreso_id")
    if ingreso_id:
        return _ingreso_context(int(ingreso_id))
    device_id = job.get("device_id")
    if not device_id:
        raise BejermanBlockedError("El job no tiene ni ingreso ni equipo asociado")
    return _device_context(int(device_id), job.get("company_key"))


def article_context_for_ingreso(ingreso_id: int) -> dict[str, Any]:
    return _ingreso_context(ingreso_id)


def article_context_for_job(job: dict[str, Any]) -> dict[str, Any]:
    """Contexto para mapear el artículo de un job, tenga o no orden de servicio."""
    return _job_context(job)


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


def ingreso_targets_rental_stock(ingreso_id: int) -> bool:
    """El equipo vuelve al parque de alquiler: su stock va a STL, no a STC.

    El numero interno solo no alcanza. La decision se toma en el instante en que
    se libera el equipo y depende de un campo que alguien puede estar editando
    justo entonces: el 11/05/2026 le vaciaron el `numero_interno` a MG 2562
    veinticuatro segundos antes de imprimir la orden de salida y la unidad se
    fue a STC, donde sigue. El motivo del ingreso no depende de eso, y un equipo
    de cliente nunca genera una baja de alquiler.

    La venta sigue mandando: un MG historico ya vendido es del cliente aunque
    haya vuelto de un alquiler.
    """
    if ingreso_is_internal_equipment(ingreso_id):
        return True
    row = _ingreso_context(ingreso_id)
    if _ingreso_has_mg_sale(row):
        return False
    return _text_key(row.get("motivo")) in RENTAL_FLEET_MOTIVOS


def device_targets_rental_stock(device_id: int) -> bool:
    """El equipo es nuestro y sigue siendo nuestro: su stock puede ir a STL.

    Es la mitad device-level de `ingreso_targets_rental_stock`, para el alta de un equipo
    propio que no tiene ingreso. Sin motivo de ingreso que consultar, lo que queda es el
    numero interno y la venta: un MG ya vendido paso a ser del cliente y no vuelve al parque
    de alquiler. La misma funcion se usa al encolar y al ejecutar el job: si las dos mitades
    divergen vuelve el bug de los equipos propios parados en STC.
    """
    row = _device_context(device_id)
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


def _reference_for_job(ingreso_id: Any, device_id: Any) -> str:
    """Identidad del trabajo en Bejerman: la OS si existe, si no el equipo.

    Da el prefijo de `Comprobante_IdOrigen`, que es la clave con la que Bejerman deduplica.
    Un job sin ingreso NO puede seguir usando `NEXORA-OS-`: saldría `NEXORA-OS-None-ENT-STL`
    para todos, `_is_own_id_origen` lo daría por propio y `_send_stock_comprobantes`
    absorbería como "ya importado" el choque entre dos equipos distintos.
    """
    if ingreso_id:
        return f"NEXORA-OS-{int(ingreso_id)}"
    if device_id:
        return f"NEXORA-DEV-{int(device_id)}"
    raise BejermanBlockedError("El job no tiene ni ingreso ni equipo asociado")


def _reference_for_lote(lote_id: Any) -> str:
    """Identidad en Bejerman de un lote de transferencias: la SAL y la ENT de todos sus equipos.

    Espacio de nombres propio, como `NEXORA-DEV-`: con el de un equipo, el choque "ya existe"
    entre un lote y un alta suelta se absorbería como si fuera el mismo movimiento.
    """
    if not lote_id:
        raise BejermanBlockedError("El lote de transferencias no tiene identificador")
    return f"NEXORA-LOTE-{int(lote_id)}"


def _enqueue_stock_job(
    *,
    ingreso_id: int | None = None,
    device_id: int | None = None,
    company_key: str | None = None,
    sync_type: str,
    source_deposit: str,
    target_deposit: str,
    trigger: str,
    ingreso_event_id: int | None = None,
    actor_user_id: int | None = None,
    article_code: str | None = None,
    requeue_succeeded: bool = False,
    request_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if ingreso_id:
        row = _ingreso_context(ingreso_id)
        company_key = _company_key_for_ingreso_row(row)
    elif device_id:
        row = _device_context(device_id, company_key)
        company_key = _company_key_for_ingreso_row(row)
    else:
        raise BejermanBlockedError("Falta el ingreso o el equipo para encolar el movimiento de stock")
    serial = (row.get("numero_serie") or "").strip()
    status = JOB_STATUS_PENDING if serial else JOB_STATUS_BLOCKED
    last_error = None if serial else "Número de serie requerido para sincronizar Bejerman"
    request_payload = {
        "source": "nexora",
        "trigger": trigger,
        "reference": _reference_for_job(ingreso_id, row.get("device_id")),
        "companyKey": company_key,
        "marca": row.get("marca") or "",
        "modelo": row.get("modelo") or "",
        "variante": row.get("variante") or "",
        **(request_details or {}),
    }
    # Los dos índices únicos son parciales, así que hay que nombrarles el predicado: sin el
    # WHERE, Postgres no infiere el índice y el ON CONFLICT falla.
    conflict_target = (
        "(sync_type, ingreso_id) WHERE ingreso_id IS NOT NULL"
        if ingreso_id
        else "(sync_type, device_id) WHERE ingreso_id IS NULL"
    )
    return q(
        f"""
        INSERT INTO bejerman_sync_jobs(
          sync_type, ingreso_id, device_id, ingreso_event_id, numero_serie,
          source_deposit, target_deposit, company_key, status, last_error, actor_user_id,
          article_code, request_payload
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULLIF(%s,''),%s::jsonb)
        ON CONFLICT {conflict_target} DO UPDATE
           SET ingreso_event_id = COALESCE(bejerman_sync_jobs.ingreso_event_id, EXCLUDED.ingreso_event_id),
               device_id = EXCLUDED.device_id,
               numero_serie = EXCLUDED.numero_serie,
               source_deposit = EXCLUDED.source_deposit,
               target_deposit = EXCLUDED.target_deposit,
               company_key = EXCLUDED.company_key,
               -- El artículo ya resuelto no se pisa con un NULL: lo que aprendió el job
               -- vale más que lo que trae un reencolado.
               article_code = COALESCE(bejerman_sync_jobs.article_code, EXCLUDED.article_code),
               actor_user_id = COALESCE(EXCLUDED.actor_user_id, bejerman_sync_jobs.actor_user_id),
               request_payload = bejerman_sync_jobs.request_payload || EXCLUDED.request_payload,
               status = CASE
                 WHEN %s THEN EXCLUDED.status
                 WHEN bejerman_sync_jobs.status = 'succeeded' THEN bejerman_sync_jobs.status
                 ELSE EXCLUDED.status
               END,
               last_error = CASE
                 WHEN %s THEN EXCLUDED.last_error
                 WHEN bejerman_sync_jobs.status = 'succeeded' THEN bejerman_sync_jobs.last_error
                 ELSE EXCLUDED.last_error
               END,
               attempts = CASE WHEN %s THEN 0 ELSE bejerman_sync_jobs.attempts END,
               next_attempt_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE bejerman_sync_jobs.next_attempt_at END,
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
            (article_code or "").strip(),
            _json_param(request_payload),
            bool(requeue_succeeded),
            bool(requeue_succeeded),
            bool(requeue_succeeded),
            bool(requeue_succeeded),
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
    *,
    source_deposit: str | None = None,
    requeue_succeeded: bool = False,
) -> dict[str, Any]:
    if not ingreso_targets_rental_stock(ingreso_id):
        raise BejermanBlockedError("Solo los equipos del parque de alquiler pueden transferirse de STR a STL")
    source_deposit = str(
        source_deposit or _setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR"
    ).strip() or "STR"
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
        requeue_succeeded=requeue_succeeded,
    )


def enqueue_location_transfer_for_ingreso(
    ingreso_id: int,
    *,
    source_deposit: str,
    target_deposit: str,
    source_location: str,
    target_location: str,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Encola el traslado explícitamente confirmado entre dos ubicaciones físicas.

    Nunca adivina el origen. Si Bejerman no confirma stock positivo en el depósito de
    origen, el worker bloquea el trabajo sin emitir una ENT: así un cambio de selector no
    puede inventar stock en el destino.
    """
    source = str(source_deposit or "").strip().upper()
    target = str(target_deposit or "").strip().upper()
    if not source or not target:
        raise BejermanBlockedError("La ubicación elegida no tiene un depósito Bejerman asociado")
    if source == target:
        raise BejermanBlockedError("El depósito de origen y el destino son el mismo")

    active = q(
        """
        SELECT id, status
          FROM bejerman_sync_jobs
         WHERE ingreso_id=%s
           AND sync_type=%s
           AND status IN ('pending', 'running')
         ORDER BY id DESC
         LIMIT 1
        """,
        [ingreso_id, SYNC_TYPE_STOCK_LOCATION_TRANSFER],
        one=True,
    )
    if active:
        raise BejermanBlockedError(
            "Ya hay un traslado de ubicación en curso en Bejerman. Esperá su resolución antes de moverlo otra vez."
        )

    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_LOCATION_TRANSFER,
        source_deposit=source,
        target_deposit=target,
        trigger="cambio_ubicacion_confirmado",
        actor_user_id=actor_user_id,
        requeue_succeeded=True,
        request_details={
            "strict_source_stock": True,
            "location_transfer": {
                "source_location": str(source_location or "").strip(),
                "target_location": str(target_location or "").strip(),
                "source_deposit": source,
                "target_deposit": target,
            },
        },
    )


def enqueue_stock_val_to_stl_for_device(
    device_id: int,
    *,
    company_key: str | None = None,
    source_deposit: str | None = None,
    article_code: str | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Deja en la estantería de alquiler un equipo propio recién dado de alta.

    La unidad nueva llega al depósito central (VAL) y "disponible para alquiler" es la
    intersección de la ubicación con stock positivo en STL, que además es de donde descuenta
    el RTA. Sin este movimiento el equipo queda invisible y, si igual se alquila, deja STL en
    negativo sin que nadie se entere.

    El origen es configurable porque la unidad puede haber quedado en otro depósito; el
    proceso además se autocorrige si la partida no está donde se declaró.

    El `article_code` viene del renglón de stock que eligió el operador. Sin él, un modelo
    todavía sin mapear bloquearía el job pidiendo que alguien confirme el artículo a mano,
    cuando el dato ya estaba a la vista en la pantalla del alta.
    """
    if not device_targets_rental_stock(device_id):
        raise BejermanBlockedError(
            "Solo un equipo propio activo puede transferirse a la estantería de alquiler"
        )
    source_deposit = str(source_deposit or _setting("BEJERMAN_DEMO_TARGET_DEPOSIT", "VAL") or "VAL").strip() or "VAL"
    target_deposit = str(_setting("BEJERMAN_TARGET_DEPOSIT", "STL") or "STL").strip() or "STL"
    if source_deposit.upper() == target_deposit.upper():
        raise BejermanBlockedError("El depósito de origen no puede ser el mismo que el destino")
    return _enqueue_stock_job(
        device_id=device_id,
        company_key=company_key,
        sync_type=SYNC_TYPE_STOCK_VAL_TO_STL,
        source_deposit=source_deposit,
        target_deposit=target_deposit,
        trigger="alta_equipo_propio_alquiler",
        article_code=article_code,
        actor_user_id=actor_user_id,
    )


def enqueue_stock_val_to_stl_lote(
    device_ids: Sequence[int],
    *,
    company_key: str | None = None,
    source_deposit: str | None = None,
    article_codes: dict[int, str] | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Encola en UN lote la transferencia a la estantería de alquiler de varios equipos propios.

    Cada equipo tiene su job, como en el alta individual —es lo que muestra el panel y lo que
    deduplica el índice por equipo—, pero todos cuelgan del mismo `bejerman_stock_lotes` y el
    worker los mueve juntos: una SAL y una ENT con un renglón por equipo, en vez de dos
    comprobantes por equipo. Tiene que correr dentro de la transacción del alta: si un equipo
    no califica, no se encola ninguno.
    """
    ids = [int(device_id) for device_id in dict.fromkeys(device_ids or [])]
    if not ids:
        raise BejermanBlockedError("El lote de transferencias no tiene equipos")
    if len(ids) > STOCK_LOTE_MAX_ITEMS:
        raise BejermanBlockedError(
            f"Un lote admite como máximo {STOCK_LOTE_MAX_ITEMS} equipos; dividí la carga."
        )
    source = str(source_deposit or _setting("BEJERMAN_DEMO_TARGET_DEPOSIT", "VAL") or "VAL").strip().upper() or "VAL"
    target = str(_setting("BEJERMAN_TARGET_DEPOSIT", "STL") or "STL").strip().upper() or "STL"
    if source == target:
        raise BejermanBlockedError("El depósito de origen no puede ser el mismo que el destino")
    # La misma guarda que el alta individual y que el worker al ejecutar: un equipo que no es
    # nuestro no puede terminar en STL.
    for device_id in ids:
        if not device_targets_rental_stock(device_id):
            raise BejermanBlockedError(
                f"El equipo {device_id} no es un equipo propio activo: no puede ir a la estantería de alquiler"
            )
    resolved_company = _company_key_for_ingreso_row({"empresa_bejerman": company_key})
    lote = q(
        """
        INSERT INTO bejerman_stock_lotes(
          sync_type, company_key, source_deposit, target_deposit, actor_user_id, request_payload
        ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING id
        """,
        [
            SYNC_TYPE_STOCK_VAL_TO_STL,
            resolved_company,
            source,
            target,
            actor_user_id,
            _json_param({"source": "nexora", "trigger": "alta_masiva_equipos_propios", "device_ids": ids}),
        ],
        one=True,
    )
    lote_id = int(lote["id"])
    reference = _reference_for_lote(lote_id)
    codes = {int(key): str(value or "").strip() for key, value in (article_codes or {}).items()}
    jobs = []
    for device_id in ids:
        job = _enqueue_stock_job(
            device_id=device_id,
            company_key=resolved_company,
            sync_type=SYNC_TYPE_STOCK_VAL_TO_STL,
            source_deposit=source,
            target_deposit=target,
            trigger="alta_masiva_equipos_propios",
            article_code=codes.get(device_id) or "",
            actor_user_id=actor_user_id,
        )
        q(
            """
            UPDATE bejerman_sync_jobs
               SET lote_id = %s,
                   request_payload = request_payload || %s::jsonb,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            [lote_id, _json_param({"lote_id": lote_id, "lote_reference": reference}), job["id"]],
        )
        jobs.append({**job, "device_id": device_id})
    return {
        "lote_id": lote_id,
        "reference": reference,
        "company_key": resolved_company,
        "source_deposit": source,
        "target_deposit": target,
        "jobs": jobs,
    }


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
    # AUTO igual que la baja: el origen lo decide el stock real, no la teoría. Fijarlo en DES
    # daba por hecho que la baja anterior había pasado por la integración, y en una corrección
    # histórica justamente no pasó: el equipo sigue en STR/STL/VAL y el alta se bloqueaba
    # con "se esperaba DES" sin corregir ningún depósito.
    target_deposit = _stock_alta_target_deposit()
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_FROM_DESGUACE,
        source_deposit="AUTO",
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


def cancel_sync_job(job_id: int, actor_user_id: int | None, reason: str) -> dict[str, Any]:
    """Cierra un job sin ejecutarlo, dejando escrito quién y por qué.

    La salida que le faltaba al panel: hay jobs que no pueden terminar bien nunca
    —el `sync_type` sin implementación, el movimiento que alguien ya hizo a mano
    en Bejerman— y hasta ahora la única acción ofrecida era reintentar, que
    vuelve a fallar. El motivo es obligatorio: un job descartado sin explicación
    es indistinguible de un movimiento de stock que se perdió.
    """

    reason = str(reason or "").strip()
    if not reason:
        raise BejermanBlockedError("Escriba el motivo por el que se descarta la operación.")
    row = q(
        "SELECT id, status, sync_type, response_payload, lote_id FROM bejerman_sync_jobs WHERE id=%s",
        [job_id],
        one=True,
    )
    if not row:
        raise BejermanBlockedError("Operación Bejerman no encontrada")
    status = str(row.get("status") or "").strip()
    if status in (JOB_STATUS_SUCCEEDED, JOB_STATUS_CANCELLED):
        raise BejermanBlockedError("La operación ya está cerrada.")
    if status == JOB_STATUS_RUNNING:
        raise BejermanBlockedError("La operación se está ejecutando; espere a que termine.")
    lote = _stock_lote_for_job(row)
    if lote and lote.get("phase") in LOTE_EMITTING_PHASES:
        # El renglón de este equipo viaja en una SAL/ENT compartida que ya pudo haber entrado:
        # descartarlo suelto dejaría al equipo fuera del origen sin llegar al destino.
        raise BejermanBlockedError(
            f"El equipo es parte del lote {_reference_for_lote(lote['id'])}, que ya emitió "
            "movimientos en Bejerman: reintentá la operación en vez de descartarla."
        )
    payload = {
        **_dict_payload(row.get("response_payload")),
        "cancellation": {
            "reason": reason,
            "actorUserId": actor_user_id,
            "at": timezone.now().isoformat(),
            "previousStatus": status,
        },
    }
    _finish_job(
        job_id,
        JOB_STATUS_CANCELLED,
        error=f"Descartada por un operador: {reason}",
        response_payload=payload,
    )
    return q(
        "SELECT id, status, attempts, last_error FROM bejerman_sync_jobs WHERE id=%s",
        [job_id],
        one=True,
    ) or {}


def partida_stock_locations(
    serial: str,
    *,
    company_key: str | None,
    actor_user_id: int | None,
    client: BejermanSDKClient | None = None,
) -> list[dict[str, Any]]:
    """Stock positivo de una partida en todos los depósitos de Bejerman.

    Sólo lee. `ObtenerStockPartida` trae todos los depósitos en una sola consulta: por eso el
    depósito que recibe `stock_by_deposit_partida` acá no importa.
    """

    serial = str(serial or "").strip()
    if not serial:
        return []
    own_client = client is None
    if own_client:
        validate_bejerman_config()
        client = BejermanSDKClient(
            company_key=company_key or DEFAULT_INGRESS_COMPANY_KEY,
            actor_user_id=actor_user_id,
        )
    try:
        response = client.stock_by_deposit_partida("", serial)
    finally:
        if own_client:
            client.close()
    return _stock_records_summary(_records_for_partida(response, serial), limit=20)


def _equipment_article_for_job(job: dict[str, Any]) -> str:
    """El artículo que este equipo debería tener en Bejerman, con la misma prioridad que el traslado."""

    confirmed_article, mapped_article = _mapping_article_codes_for_job(job)
    return next(
        (code for code in (confirmed_article, (job.get("article_code") or "").strip(), mapped_article) if code),
        "",
    )


def diagnose_job_stock(
    job_id: int,
    actor_user_id: int | None,
    *,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    """«Ver en Bejerman»: dónde tiene Bejerman el equipo de una operación y qué conviene hacer.

    Antes había que averiguarlo por SQL: el panel decía «no tiene stock positivo en STR», pero
    no dónde estaba el equipo, y sin eso no hay forma de saber si conviene reintentar, mover
    desde otro depósito o descartar. Sólo lee de Bejerman; el resultado se guarda en el job
    para que quien abra el panel después lo vea sin volver a consultar.
    """

    job = q("SELECT * FROM bejerman_sync_jobs WHERE id = %s", [job_id], one=True)
    if not job:
        raise BejermanBlockedError("Operación Bejerman no encontrada")
    job = {**job, "request_payload": _dict_payload(job.get("request_payload"))}
    serial = (job.get("numero_serie") or "").strip()
    equipment_article = _equipment_article_for_job(job)
    records = partida_stock_locations(
        serial,
        company_key=job.get("company_key"),
        actor_user_id=actor_user_id,
        client=client,
    )
    diagnosis = stock_diagnosis(
        serial=serial,
        source_deposit=job.get("source_deposit"),
        target_deposit=job.get("target_deposit"),
        records=records,
        equipment_article=equipment_article,
        movable=can_change_source(job),
    )
    diagnosis.update(
        {
            "equipmentArticle": equipment_article,
            "checkedAt": timezone.now().isoformat(),
            "checkedByUserId": actor_user_id,
        }
    )
    # Sin tocar `updated_at`: consultar no es un intento, y el panel muestra esa fecha como tal.
    q(
        """
        UPDATE bejerman_sync_jobs
           SET response_payload = COALESCE(response_payload, '{}'::jsonb)
                                  || jsonb_build_object('diagnosis', %s::jsonb)
         WHERE id = %s
        """,
        [_json_param(diagnosis), job_id],
    )
    return diagnosis


def change_job_source_deposit(
    job_id: int,
    deposit: str,
    actor_user_id: int | None,
    *,
    client: BejermanSDKClient | None = None,
) -> dict[str, Any]:
    """Cambia el depósito de origen de un traslado por el que Bejerman confirma que tiene el equipo.

    La salida de «Mover desde donde está». Un cambio manual de ubicación se bloquea a propósito
    cuando la partida no está en el origen declarado —nunca se crea stock por una selección de
    pantalla—, y hasta ahora la única salida era corregir Bejerman por SQL. Acá el operador elige
    el depósito y el cambio se aplica sólo si Bejerman confirma, en ese momento, que el equipo
    está ahí. Queda asentado quién lo cambió, desde dónde y cuándo.
    """

    deposit = str(deposit or "").strip().upper()
    if not deposit:
        raise BejermanBlockedError("Indique desde qué depósito hay que mover el equipo.")
    job = q("SELECT * FROM bejerman_sync_jobs WHERE id = %s", [job_id], one=True)
    if not job:
        raise BejermanBlockedError("Operación Bejerman no encontrada")
    request_payload = _dict_payload(job.get("request_payload"))
    job = {**job, "request_payload": request_payload}
    if not can_change_source(job):
        raise BejermanBlockedError(
            "A esta operación no se le puede cambiar el origen: sólo a un traslado fallido o bloqueado "
            "que todavía no emitió ningún movimiento y que no viaja en un lote."
        )
    previous = str(job.get("source_deposit") or "").strip().upper()
    target = str(job.get("target_deposit") or "").strip().upper()
    if deposit == target:
        raise BejermanBlockedError(f"{deposit} es el destino de la operación, no puede ser también el origen.")
    if deposit == previous:
        raise BejermanBlockedError(f"{deposit} ya es el origen de la operación: reintente directamente.")
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        raise BejermanBlockedError("Número de serie requerido para sincronizar Bejerman")
    equipment_article = _equipment_article_for_job(job)
    records = partida_stock_locations(
        serial,
        company_key=job.get("company_key"),
        actor_user_id=actor_user_id,
        client=client,
    )
    present = [
        record
        for record in records
        if _norm(record.get("deposito") or "") == _norm(deposit)
        and (not equipment_article or _norm(record.get("article_code") or "") in {"", _norm(equipment_article)})
    ]
    if not present:
        raise BejermanBlockedError(
            f"Bejerman no tiene la partida {serial} en {deposit}"
            + (f" con el artículo {equipment_article}" if equipment_article else "")
            + ": no se cambió nada."
        )
    override = {
        "previousSourceDeposit": previous,
        "sourceDeposit": deposit,
        "actorUserId": actor_user_id,
        "at": timezone.now().isoformat(),
        "stock": present,
    }
    updated = q(
        """
        UPDATE bejerman_sync_jobs
           SET source_deposit = %s,
               request_payload = COALESCE(request_payload, '{}'::jsonb)
                                 || jsonb_build_object('source_override', %s::jsonb)
         WHERE id = %s
           AND status IN ('failed', 'blocked')
        RETURNING id, source_deposit
        """,
        [deposit, _json_param(override), job_id],
        one=True,
    )
    if not updated:
        raise BejermanBlockedError("La operación cambió de estado mientras se verificaba: actualice el panel.")
    logger.info(
        "bejerman_job_source_changed",
        extra={"job_id": job_id, "from": previous, "to": deposit, "actor_user_id": actor_user_id},
    )
    return {**updated, "sourceOverride": override}


def _retry_next_attempt(attempts: int, error: str, max_attempts: int):
    """Cuándo reintentar un job que falló, o `None` si ya agotó los intentos.

    Separado de `_retry_job` para que el lote de transferencias aplique exactamente la misma
    política a todos sus equipos.
    """
    retryable_sync_error = _is_retryable_sync_message(error)
    effective_max_attempts = (
        _sync_transient_max_attempts(max_attempts) if retryable_sync_error else int(max_attempts)
    )
    if attempts >= effective_max_attempts:
        return None
    base_delay_seconds = (
        _positive_int_setting("BEJERMAN_SYNC_TRANSIENT_RETRY_BASE_SECONDS", 300)
        if retryable_sync_error
        else 60
    )
    delay_seconds = min(
        3600,
        max(base_delay_seconds, 2 ** max(0, attempts - 1) * base_delay_seconds),
    )
    return timezone.now() + timedelta(seconds=delay_seconds)


def _retry_job(job: dict[str, Any], error: str, max_attempts: int) -> str:
    next_attempt = _retry_next_attempt(int(job.get("attempts") or 0), error, max_attempts)
    if next_attempt is None:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=f"Máximos reintentos agotados: {error}")
        return JOB_STATUS_BLOCKED
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
        context = _job_context(job)
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
          JOIN devices d ON d.id = j.device_id
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
    context = _job_context(job)
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
        context = _job_context(job)
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
    reintentar de verdad.

    Sin ingreso el número sale del equipo, en el rango 9xxxxxxx que ya usan los otros
    movimientos que no nacen de una OS (correcciones de stock, force_stock_entry): así no
    puede pisar el número de un comprobante numerado con un `ingreso_id`."""
    attempts = int(job.get("attempts") or 0)
    if not job.get("ingreso_id"):
        # El corrimiento por reintento se hace DENTRO del rango: sumar y tomar módulo 1e8
        # podía dar la vuelta y aterrizar en el espacio de los comprobantes numerados por OS.
        seed = zlib.crc32(_reference_for_job(None, job.get("device_id")).encode("utf-8"))
        return f"{90000000 + ((seed + attempts * 100000) % 10000000):08d}"
    base = int(job["ingreso_id"]) % 100000000
    if attempts > 0:
        base = (base + attempts * 100000) % 100000000
    return f"{base:08d}"


def _stock_transfer_item(
    *,
    serial: str,
    article_code: str,
    comprobante: str,
    deposit: str,
    number: str,
    observacion: str,
) -> dict[str, Any]:
    """Un renglón de un comprobante de stock. El lote arma varios con el mismo número.

    `observacion` es el MG del equipo, no una referencia de NEXORA: ver
    `bejerman_partida_observacion`.
    """
    return {
        "Comprobante_FechaEmision": _stock_document_date(),
        "Comprobante_Tipo": comprobante,
        "Comprobante_Letra": "",
        "Comprobante_PtoVenta": "",
        "Comprobante_Numero": number,
        "Comprobante_Art_CodGen": article_code,
        "Comprobante_ArtPartida": serial,
        "Comprobante_DescArticulo": f"NEXORA {serial}"[:50],
        "Comprobante_ArtCodTipo": "1",
        "Comprobante_ArtDeposito": deposit,
        "Comprobante_CantidadUM1": 1,
        "Comprobante_CantidadUM2": 1,
        "Comprobante_PrecioTotalMonLocal": 0,
        "Partida_Observaciones": observacion,
    }


def _observacion_partida_for_job(job: dict[str, Any]) -> str:
    if job.get("device_id"):
        return observacion_partida_de_equipo(job["device_id"])
    return observacion_partida_de_ingreso(job.get("ingreso_id"))


def _base_stock_transfer_item(job: dict[str, Any], article_code: str, comprobante: str, deposit: str) -> dict[str, Any]:
    return _stock_transfer_item(
        serial=(job.get("numero_serie") or "").strip(),
        article_code=article_code,
        comprobante=comprobante,
        deposit=deposit,
        number=_stock_transfer_comprobante_number(job),
        observacion=_observacion_partida_for_job(job),
    )


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
    reference = _reference_for_job(job.get("ingreso_id"), job.get("device_id"))
    item["Comprobante_IdOrigen"] = f"{reference}-SAL-{source_deposit}"
    return [item]


def build_stock_transfer_in_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    target_deposit = job.get("target_deposit") or "STL"
    comprobante = str(_setting("BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE", "ENT") or "").strip()
    item = _base_stock_transfer_item(job, article_code, comprobante, target_deposit)
    reference = _reference_for_job(job.get("ingreso_id"), job.get("device_id"))
    item["Comprobante_IdOrigen"] = f"{reference}-ENT-{target_deposit}"
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
    reference = _reference_for_job(job.get("ingreso_id"), job.get("device_id"))
    item["Comprobante_IdOrigen"] = f"{reference}-SAL-{source_deposit}-{target_deposit}"
    item["Comprobante_CodigoCausaEmision"] = causa
    return [item]


def build_stock_baja_in_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    target_deposit = job.get("target_deposit") or _stock_baja_target_deposit()
    comprobante = str(_setting("BEJERMAN_STOCK_BAJA_IN_COMPROBANTE", "ENT") or "ENT").strip()
    item = _base_stock_transfer_item(job, article_code, comprobante, target_deposit)
    reference = _reference_for_job(job.get("ingreso_id"), job.get("device_id"))
    item["Comprobante_IdOrigen"] = f"{reference}-ENT-{target_deposit}"
    return [item]


def build_stock_alta_out_payload(
    job: dict[str, Any],
    article_code: str,
    source_deposit: str = "",
) -> list[dict[str, Any]]:
    # El origen llega resuelto contra el stock real (el job guarda 'AUTO'); sólo se cae al
    # default cuando alguien arma el payload a mano con un job viejo.
    source_deposit = str(source_deposit or "").strip()
    if not source_deposit or _norm(source_deposit) == "AUTO":
        source_deposit = str(job.get("source_deposit") or "").strip()
    if not source_deposit or _norm(source_deposit) == "AUTO":
        source_deposit = _stock_alta_source_deposit()
    target_deposit = job.get("target_deposit") or _stock_alta_target_deposit()
    comprobante = str(_setting("BEJERMAN_STOCK_ALTA_OUT_COMPROBANTE", "SAL") or "SAL").strip()
    causa = _stock_alta_causa_emision()
    item = _base_stock_transfer_item(job, article_code, comprobante, source_deposit)
    item["Comprobante_CantidadUM1"] = -1
    item["Comprobante_CantidadUM2"] = -1
    reference = _reference_for_job(job.get("ingreso_id"), job.get("device_id"))
    item["Comprobante_IdOrigen"] = f"{reference}-SAL-{source_deposit}-{target_deposit}"
    item["Comprobante_CodigoCausaEmision"] = causa
    return [item]


def build_stock_alta_in_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    target_deposit = job.get("target_deposit") or _stock_alta_target_deposit()
    comprobante = str(_setting("BEJERMAN_STOCK_ALTA_IN_COMPROBANTE", "ENT") or "ENT").strip()
    item = _base_stock_transfer_item(job, article_code, comprobante, target_deposit)
    reference = _reference_for_job(job.get("ingreso_id"), job.get("device_id"))
    item["Comprobante_IdOrigen"] = f"{reference}-ENT-{target_deposit}"
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
        sal_response = _send_stock_comprobantes(
            client,
            sal_payload,
            phase="baja_sal",
            job_id=job["id"],
            verify=lambda: not _stock_present_in_deposit(client, source_deposit, serial, article_code),
        )
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
    ent_response = _send_stock_comprobantes(
        client,
        ent_payload,
        phase="baja_ent",
        job_id=job["id"],
        verify=lambda: _stock_present_in_deposit(client, target, serial, article_code),
    )
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
                f"La partida {serial} no tiene stock positivo en ningún depósito de Bejerman; "
                f"no hay nada que dar de alta en {target}: hay que ingresarla desde Bejerman"
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
        # El único depósito con stock es el origen, venga de DES o de donde haya quedado el
        # equipo cuando la baja nunca se sincronizó. Exigir DES dejaba el depósito sin corregir.
        source_deposit = positive_deposits[0]
        source_records = [
            record
            for record in positive_records
            if _norm(_deposit_from_record(record) or "") == _norm(source_deposit)
        ]
    else:
        source_deposit = str(request_payload.get("source_stock_deposit") or job.get("source_deposit") or "").strip()
        if not source_deposit or _norm(source_deposit) == "AUTO":
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
        sal_payload = build_stock_alta_out_payload(job, article_code, source_deposit)
        sal_request_payload = {
            **next_request_payload,
            "alta_phase": "sal_pending",
            "sal_comprobantes": sal_payload,
        }
        _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload=sal_request_payload, article_code=article_code)
        sal_response = _send_stock_comprobantes(
            client,
            sal_payload,
            phase="alta_sal",
            job_id=job["id"],
            verify=lambda: not _stock_present_in_deposit(client, source_deposit, serial, article_code),
        )
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
    ent_response = _send_stock_comprobantes(
        client,
        ent_payload,
        phase="alta_ent",
        job_id=job["id"],
        verify=lambda: _stock_present_in_deposit(client, target, serial, article_code),
    )
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


def _stock_present_in_deposit(
    client: BejermanSDKClient, deposit: str, serial: str, article_code: str = ""
) -> bool:
    """¿Bejerman tiene la partida con cantidad en ese depósito?"""

    try:
        response = client.stock_by_deposit_partida(deposit, serial)
    except Exception:
        return False
    return bool(
        _records_for_equipment_article(_records_for_partida(response, serial, deposit), article_code)
    )


def _is_own_id_origen(comprobantes: list[dict[str, Any]]) -> bool:
    """Todos los renglones llevan un `Comprobante_IdOrigen` armado por NEXORA.

    `NEXORA-OS-` numera por orden de servicio, `NEXORA-DEV-` por equipo (los movimientos
    que no nacen de una OS, como el alta de un equipo propio) y `NEXORA-LOTE-` por lote de
    equipos (el alta masiva). Los tres identifican unívocamente el trabajo, que es lo que
    habilita tratar el "ya fue importado" como éxito.
    """

    items = [item for item in (comprobantes or []) if isinstance(item, dict)]
    if not items:
        return False
    return all(
        str(item.get("Comprobante_IdOrigen") or "").startswith(("NEXORA-OS-", "NEXORA-DEV-", "NEXORA-LOTE-"))
        for item in items
    )


def _send_stock_comprobantes(
    client: BejermanSDKClient,
    comprobantes: list[dict[str, Any]],
    *,
    phase: str,
    job_id: Any = None,
    verify: Any = None,
) -> dict[str, Any]:
    """Manda un lote de movimientos de stock tolerando el reenvío del mismo lote.

    Bejerman deduplica por `Comprobante_IdOrigen`, que NEXORA arma de forma
    determinística a partir de la OS y de la fase. Cuando un intento anterior
    llegó a entrar pero NEXORA no alcanzó a registrarlo (timeout, pestaña caída,
    reinicio del worker), el reintento vuelve con *"El mismo ya existe en la
    empresa debido a una importación anterior"*. Eso no es un rechazo: es la
    confirmación de que el movimiento ya está hecho. Tratarlo como fallo dejaba
    el job dando vueltas hasta agotar los reintentos y terminar en `blocked`,
    con el stock ya movido — el caso de la OS 29668 del 02/09/2026.

    Sólo se absorbe cuando el `IdOrigen` es nuestro: si el choque fuera contra un
    comprobante ajeno, callarlo escondería un problema real. Y nunca se absorbe a
    ciegas: en la transferencia la red la pone `_verify_stock_transfer_target`,
    que corre igual después del ENT; donde no hay una verificación posterior, el
    llamador pasa `verify` y sin esa confirmación el error se vuelve a levantar.
    """

    try:
        return client.ingresar_lista_comprobantes_json(comprobantes)
    except BejermanBlockedError as exc:
        if not _is_already_imported_message(str(exc)) or not _is_own_id_origen(comprobantes):
            raise
        if verify is not None:
            try:
                confirmed = bool(verify())
            except Exception:
                confirmed = False
            if not confirmed:
                # Bejerman dice que ya lo importó pero el stock no está donde
                # tendría que estar: el "duplicado" no es nuestro movimiento.
                raise
        logger.info(
            "bejerman_stock_comprobante_ya_importado",
            extra={
                "job_id": job_id,
                "phase": phase,
                "id_origen": [item.get("Comprobante_IdOrigen") for item in comprobantes],
                "error": str(exc),
            },
        )
        return {
            "Resultado": "OK",
            "alreadyImported": True,
            "alreadyImportedPhase": phase,
            "alreadyImportedMessage": str(exc),
        }


def _process_stock_transfer(job: dict[str, Any], client: BejermanSDKClient) -> str:
    validate_bejerman_config()
    sync_type = (job.get("sync_type") or "").strip()
    request_payload = _dict_payload(job.get("request_payload"))
    response_payload = _dict_payload(job.get("response_payload"))
    # La misma pregunta que se hizo al encolar, revalidada acá: entre las dos cosas el equipo
    # pudo venderse, y un MG vendido ya no vuelve al parque de alquiler. Las dos mitades tienen
    # que decidir igual; si divergen, el stock termina en el depósito equivocado.
    if sync_type == SYNC_TYPE_STOCK_STR_TO_STL:
        targets_rental = ingreso_targets_rental_stock(int(job["ingreso_id"]))
    elif sync_type == SYNC_TYPE_STOCK_VAL_TO_STL:
        targets_rental = device_targets_rental_stock(int(job["device_id"]))
    else:
        targets_rental = True
    if not targets_rental:
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
        idempotent_request_payload = {
            **request_payload,
            "idempotent": True,
            "numero_serie": serial,
            "target_deposit": target,
            "transfer_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
        }
        idempotent_response_payload = {**response_payload, "target": target_response}
        # Una unidad no puede estar en los dos depósitos. Si el origen todavía la tiene, al
        # destino la llevó otra cosa (el restore del 08/06 que emitía ENT sin SAL, un ENT a
        # mano) y la SAL de este traslado nunca se hizo. Darlo por hecho dejaba el stock
        # duplicado con el job en verde; cuando alguien borró el ENT sobrante, la unidad volvió
        # a quedar en el origen sin que nadie se enterara (OS 28054). El equipo va al destino:
        # lo que falta es la SAL del origen, con su propio IdOrigen.
        leftover_response = client.stock_by_deposit_partida(source, serial)
        leftover_records = _records_for_equipment_article(
            _records_for_partida(leftover_response, serial, source), article_code
        )
        if leftover_records and _norm(source) != _norm(target):
            validate_bejerman_config(comprobante="BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE")
            sal_payload = build_stock_transfer_out_payload(job, article_code, source_deposit=source)
            for item in sal_payload:
                item["Comprobante_IdOrigen"] = f"{item.get('Comprobante_IdOrigen', '')}-DUP"[:50]
            _finish_job(
                job["id"],
                JOB_STATUS_RUNNING,
                request_payload={**request_payload, "sal_duplicado_comprobantes": sal_payload},
                article_code=article_code,
            )
            idempotent_response_payload["source"] = leftover_response
            idempotent_response_payload["sal_duplicado"] = _send_stock_comprobantes(
                client,
                sal_payload,
                phase="transfer_sal_duplicado",
                job_id=job["id"],
                verify=lambda: not _stock_present_in_deposit(client, source, serial, article_code),
            )
            idempotent_request_payload.update(
                {
                    "sal_done": True,
                    "sal_duplicado_comprobantes": sal_payload,
                    "stock_duplicado_corregido": {
                        "source_deposit": source,
                        "records": _stock_records_summary(leftover_records),
                    },
                }
            )
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload=idempotent_request_payload,
            response_payload=idempotent_response_payload,
            article_code=article_code,
        )
        return JOB_STATUS_SUCCEEDED

    source_response = client.stock_by_deposit_partida(source, serial)
    source_known_records = _records_for_partida_any_quantity(source_response, serial, source)
    source_records = _records_for_partida(source_response, serial, source)

    # Este flujo nace de un cambio manual de ubicación: a diferencia de las
    # transiciones automáticas, no hay otro documento que pueda justificar una ENT
    # sin su SAL. Si Bejerman no tiene la partida en el origen declarado, se bloquea
    # para conciliar; nunca se crea stock por una selección de pantalla.
    if (
        sync_type == SYNC_TYPE_STOCK_LOCATION_TRANSFER
        and bool(request_payload.get("strict_source_stock"))
        and not sal_done
        and not source_records
    ):
        _finish_job(
            job["id"],
            JOB_STATUS_BLOCKED,
            request_payload={
                **request_payload,
                "strict_source_stock_failed": True,
                "source_stock_deposit": source,
            },
            response_payload={**response_payload, "source": source_response, "target": target_response},
            article_code=(job.get("article_code") or "").strip(),
        )
        raise BejermanBlockedError(
            f"La partida {serial} no tiene stock positivo en {source}; no se emitió ningún movimiento."
        )

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
        sal_response = _send_stock_comprobantes(
            client,
            sal_payload,
            phase="transfer_sal",
            job_id=job["id"],
            # La verificación de después del ENT mira sólo el destino: sin esto, un "ya existe"
            # con el origen todavía cargado pasaba por SAL hecha y el ENT duplicaba el stock.
            verify=lambda: not _stock_present_in_deposit(client, transfer_source, serial, article_code),
        )
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
    ent_response = _send_stock_comprobantes(client, ent_payload, phase="transfer_ent", job_id=job["id"])
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


def _config_error_message(exc: Exception) -> str:
    error = str(exc)
    if BEJERMAN_CREDENTIALS_REQUIRED in error:
        # Se reabre solo cuando la persona las carga (ver
        # reopen_jobs_waiting_for_credentials); decirlo acá evita que alguien
        # reintente a mano diez veces esperando que cambie algo.
        error = (
            f"{BEJERMAN_CREDENTIALS_REQUIRED} La operación se reanuda sola en cuanto el usuario "
            "que la originó cargue sus credenciales en su perfil."
        )
    return error


def _stock_lote_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    """El lote al que pertenece un job, o `None` si el equipo se mueve solo."""
    lote_id = (job or {}).get("lote_id")
    if not lote_id:
        return None
    return q(
        "SELECT id, phase, last_error FROM bejerman_stock_lotes WHERE id = %s",
        [int(lote_id)],
        one=True,
    )


def build_stock_lote_transfer_payloads(
    reference: str,
    number: str,
    items: Sequence[dict[str, Any]],
    *,
    source: str,
    target: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """La SAL y la ENT de un lote: un renglón por equipo, todos con el mismo número e IdOrigen.

    El IdOrigen es del lote y no del equipo: Bejerman deduplica por `Comprobante_IdOrigen`, así
    que reenviar el lote choca contra sí mismo, y ese choque es la confirmación de que ya entró.
    """
    sal_tipo = str(_setting("BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE", "SAL") or "").strip()
    ent_tipo = str(_setting("BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE", "ENT") or "").strip()
    sal: list[dict[str, Any]] = []
    ent: list[dict[str, Any]] = []
    for item in items:
        salida = _stock_transfer_item(
            serial=str(item["serial"]),
            article_code=str(item["article_code"]),
            comprobante=sal_tipo,
            deposit=source,
            number=number,
            observacion=str(item.get("observacion") or ""),
        )
        salida["Comprobante_CantidadUM1"] = -1
        salida["Comprobante_CantidadUM2"] = -1
        salida["Comprobante_IdOrigen"] = f"{reference}-SAL-{source}"
        sal.append(salida)
        entrada = _stock_transfer_item(
            serial=str(item["serial"]),
            article_code=str(item["article_code"]),
            comprobante=ent_tipo,
            deposit=target,
            number=number,
            observacion=str(item.get("observacion") or ""),
        )
        entrada["Comprobante_IdOrigen"] = f"{reference}-ENT-{target}"
        ent.append(entrada)
    return sal, ent


def _stock_lote_comprobante_number(reference: str, plan: int = 0) -> str:
    """Número de la SAL/ENT del lote, en el rango 9xxxxxxx de los movimientos sin OS.

    Se corre sólo cuando el lote se vuelve a planificar porque la SAL no entró —como el job
    individual lo corre por intento—; el reenvío de un lote congelado conserva el número.
    """
    seed = zlib.crc32(str(reference).encode("utf-8"))
    return f"{90000000 + ((seed + int(plan or 0) * 100000) % 10000000):08d}"


def _stock_lote_lock_key(lote_id: int) -> str:
    return f"{LOTE_ADVISORY_LOCK_NAMESPACE}:{int(lote_id)}"


def _try_lock_stock_lote(lote_id: int) -> bool:
    """Lock de sesión por lote: el worker y un "Reintentar" del panel no lo mueven a la vez."""
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [_stock_lote_lock_key(lote_id)])
        row = cur.fetchone()
    return bool(row and row[0])


def _unlock_stock_lote(lote_id: int) -> None:
    if connection.vendor != "postgresql":
        return
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [_stock_lote_lock_key(lote_id)])
    except Exception:
        # Si la conexión se cayó, el lock de sesión se fue con ella.
        logger.exception("No se pudo liberar el lock del lote Bejerman %s", lote_id)


def _requeue_claimed_lote_job(job: dict[str, Any], delay_seconds: int = 60) -> None:
    """Devuelve a la cola un job cuyo lote está moviendo otro proceso, sin gastarle un intento."""
    q(
        """
        UPDATE bejerman_sync_jobs
           SET status = 'pending',
               attempts = GREATEST(attempts - 1, 0),
               next_attempt_at = %s,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
           AND status = 'running'
        """,
        [timezone.now() + timedelta(seconds=delay_seconds), job["id"]],
    )


def _claim_stock_lote_members(lote_id: int, claimed_job: dict[str, Any]) -> list[dict[str, Any]]:
    """Toma todos los jobs vivos del lote, con el lock del lote ya tomado.

    El worker reclamó uno solo; los demás se marcan `running` acá para que nadie los agarre
    sueltos mientras viajan en el comprobante compartido. Quedan todos con los intentos del
    reclamado: la política de reintentos se aplica al lote entero.
    """
    rows = q(
        """
        UPDATE bejerman_sync_jobs
           SET status = 'running',
               attempts = CASE WHEN id = %s THEN attempts ELSE %s END,
               updated_at = CURRENT_TIMESTAMP
         WHERE lote_id = %s
           AND status IN ('pending','failed','blocked','running')
        RETURNING *
        """,
        [int(claimed_job["id"]), int(claimed_job.get("attempts") or 0), int(lote_id)],
    ) or []
    return sorted(rows, key=lambda row: int(row["id"]))


def _load_stock_lote(lote_id: int) -> dict[str, Any] | None:
    return q("SELECT * FROM bejerman_stock_lotes WHERE id = %s", [int(lote_id)], one=True)


def _update_stock_lote(
    lote_id: int,
    *,
    phase: str | None = None,
    request_payload: Any = None,
    response_payload: Any = None,
    error: str | None = None,
) -> None:
    """`error=None` conserva el último error; `error=""` lo limpia."""
    q(
        """
        UPDATE bejerman_stock_lotes
           SET phase = COALESCE(%s::text, phase),
               request_payload = COALESCE(%s::jsonb, request_payload),
               response_payload = COALESCE(%s::jsonb, response_payload),
               last_error = CASE WHEN %s::boolean THEN NULLIF(%s::text, '') ELSE last_error END,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [
            phase,
            _json_param(request_payload) if request_payload is not None else None,
            _json_param(response_payload) if response_payload is not None else None,
            error is not None,
            error or "",
            int(lote_id),
        ],
    )


def _set_running_lote_members(
    lote_id: int, status: str, *, error: str | None = None, next_attempt_at: Any = None
) -> None:
    q(
        """
        UPDATE bejerman_sync_jobs
           SET status = %s,
               last_error = %s,
               next_attempt_at = COALESCE(%s::timestamptz, next_attempt_at),
               updated_at = CURRENT_TIMESTAMP
         WHERE lote_id = %s
           AND status = 'running'
        """,
        [status, error, next_attempt_at, int(lote_id)],
    )


def _retry_stock_lote_members(lote_id: int, job: dict[str, Any], error: str, max_attempts: int) -> str:
    next_attempt = _retry_next_attempt(int(job.get("attempts") or 0), error, max_attempts)
    if next_attempt is None:
        _set_running_lote_members(lote_id, JOB_STATUS_BLOCKED, error=f"Máximos reintentos agotados: {error}")
        return JOB_STATUS_BLOCKED
    _set_running_lote_members(lote_id, JOB_STATUS_FAILED, error=error, next_attempt_at=next_attempt)
    return JOB_STATUS_FAILED


def _detach_stock_lote_member(job: dict[str, Any], reason: str) -> None:
    """Saca un equipo del lote ANTES de que el lote emita nada.

    Sigue por el camino individual (`_process_stock_transfer`), que ya sabe resolver una unidad
    que no está donde se esperaba: descuenta del depósito real o se bloquea con su propio
    mensaje. Una vez congelado el lote esto ya no se puede hacer.
    """
    payload = {
        key: value
        for key, value in _dict_payload(job.get("request_payload")).items()
        if key not in ("lote_id", "lote_reference")
    }
    payload["lote_detached"] = {
        "lote_id": job.get("lote_id"),
        "reason": reason,
        "at": timezone.now().isoformat(),
    }
    q(
        """
        UPDATE bejerman_sync_jobs
           SET lote_id = NULL,
               status = 'pending',
               last_error = NULL,
               next_attempt_at = CURRENT_TIMESTAMP,
               request_payload = %s::jsonb,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [_json_param(payload), job["id"]],
    )


def _release_stock_lote_leftovers(lote_id: int, items: Sequence[dict[str, Any]]) -> None:
    """Un job del lote que quedó `running` sin renglón congelado sigue por el camino individual.

    No debería pasar —cada equipo se resuelve o se desprende al planificar—, pero un job
    reabierto a mano después de terminado el lote quedaría `running` para siempre.
    """
    q(
        """
        UPDATE bejerman_sync_jobs
           SET lote_id = NULL,
               status = 'pending',
               next_attempt_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
         WHERE lote_id = %s
           AND status = 'running'
           AND NOT (id = ANY(%s::int[]))
        """,
        [int(lote_id), [int(item["job_id"]) for item in items]],
    )


def _stock_lote_item_in(client: BejermanSDKClient, deposit: str, item: dict[str, Any]) -> bool:
    """¿Bejerman tiene el artículo de este renglón con stock en el depósito?

    A diferencia de `_stock_present_in_deposit`, un error de Bejerman se propaga: acá la
    respuesta decide si se reenvía un comprobante, y "no pude mirar" no puede leerse como
    "no está".
    """
    serial = str(item.get("serial") or "").strip()
    response = client.stock_by_deposit_partida(deposit, serial)
    return bool(
        _records_for_equipment_article(
            _records_for_partida(response, serial, deposit),
            str(item.get("article_code") or ""),
        )
    )


def _stock_lote_series(items: Sequence[dict[str, Any]], flags: Sequence[bool]) -> str:
    series = [str(item.get("serial") or "") for item, flag in zip(items, flags) if flag]
    if len(series) > 10:
        return f"{', '.join(series[:10])} y {len(series) - 10} más"
    return ", ".join(series)


def _stock_lote_review(lote_id: int, message: str) -> None:
    """Deja el lote en revisión y corta: no se emite nada más hasta que alguien mire Bejerman."""
    _update_stock_lote(lote_id, phase=LOTE_PHASE_REVIEW, error=message)
    raise BejermanBlockedError(message)


def _stock_lote_partial_message(
    reference: str,
    comprobante: str,
    items: Sequence[dict[str, Any]],
    flags: Sequence[bool],
    *,
    presentes: str,
    ausentes: str,
) -> str:
    return (
        f"El lote {reference} quedó a medias con la {comprobante}: {_stock_lote_series(items, flags)} "
        f"{presentes} y {_stock_lote_series(items, [not flag for flag in flags])} {ausentes}. "
        "Un solo comprobante no deja el stock así: revisá Bejerman antes de reintentar. "
        "No se emitió nada más."
    )


def _plan_stock_lote_member(
    job: dict[str, Any], client: BejermanSDKClient, *, source: str, target: str
) -> dict[str, Any] | None:
    """Decide qué pasa con un equipo del lote antes de emitir nada.

    Devuelve el renglón a mover, o `None` si el equipo ya quedó resuelto: no corresponde
    moverlo, ya estaba en destino, o salió del lote para seguir por el camino individual. Son
    las mismas preguntas que `_process_stock_transfer` le hace a un equipo suelto, en el mismo
    orden, para que un equipo no se decida distinto por venir en un lote.
    """
    request_payload = _dict_payload(job.get("request_payload"))
    response_payload = _dict_payload(job.get("response_payload"))
    serial = (job.get("numero_serie") or "").strip()
    try:
        if not device_targets_rental_stock(int(job["device_id"])):
            _finish_job(
                job["id"],
                JOB_STATUS_SUCCEEDED,
                request_payload={
                    **request_payload,
                    "skipped_not_applicable": True,
                    "reason": "Equipo vendido o de cliente; no corresponde transferir a STL.",
                },
                response_payload=response_payload,
            )
            return None
        if not serial:
            _detach_stock_lote_member(job, "Número de serie requerido para sincronizar Bejerman")
            return None
        confirmed_article_code, mapped_article_code = _mapping_article_codes_for_job(job)
        expected_article_codes = [
            confirmed_article_code,
            (job.get("article_code") or "").strip(),
            str(request_payload.get("article_code") or "").strip(),
            mapped_article_code,
        ]
        equipment_article_code = next((code for code in expected_article_codes if code), "")
        target_response = client.stock_by_deposit_partida(target, serial)
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
            return None
        source_response = client.stock_by_deposit_partida(source, serial)
        source_records = _records_for_partida(source_response, serial, source)
        if not source_records:
            _detach_stock_lote_member(job, f"La partida {serial} no tiene stock en {source}")
            return None
        article_code = _article_code_for_partida_deposit(
            source_records,
            serial=serial,
            deposit=source,
            expected_article_codes=expected_article_codes,
        )
        _assert_source_article_matches_confirmed_mapping(
            job,
            serial=serial,
            deposit=source,
            source_article_code=article_code,
            confirmed_article_code=confirmed_article_code,
        )
    except BejermanBlockedError as exc:
        # Un bloqueo transitorio del SDK no es un problema del equipo: reintenta el lote entero.
        if _is_retryable_sync_message(str(exc)):
            raise
        _detach_stock_lote_member(job, str(exc))
        return None
    return {
        "job_id": int(job["id"]),
        "device_id": int(job["device_id"]),
        "serial": serial,
        "article_code": article_code,
        "observacion": observacion_partida_de_equipo(job["device_id"]),
    }


def _verify_stock_lote_target(
    client: BejermanSDKClient, items: Sequence[dict[str, Any]], target: str
) -> None:
    attempts = _positive_int_setting("BEJERMAN_SYNC_STOCK_VERIFY_ATTEMPTS", 3)
    try:
        delay_ms = int(_setting("BEJERMAN_SYNC_STOCK_VERIFY_DELAY_MS", 1500) or 0)
    except (TypeError, ValueError):
        delay_ms = 1500
    delay_ms = max(0, delay_ms)
    pendientes = list(items)
    for attempt in range(attempts):
        pendientes = [item for item in pendientes if not _stock_lote_item_in(client, target, item)]
        if not pendientes:
            return
        if attempt < attempts - 1 and delay_ms:
            time.sleep(delay_ms / 1000)
    raise BejermanTransientError(
        f"Bejerman aceptó la ENT del lote pero todavía no confirma stock en {target} para "
        f"{_stock_lote_series(pendientes, [True] * len(pendientes))}."
    )


def _send_stock_lote_ent(
    client: BejermanSDKClient,
    request_payload: dict[str, Any],
    items: Sequence[dict[str, Any]],
    target: str,
) -> dict[str, Any]:
    return _send_stock_comprobantes(
        client,
        list(request_payload.get("ent_comprobantes") or []),
        phase="lote_ent",
        job_id=items[0]["job_id"],
        verify=lambda: all(_stock_lote_item_in(client, target, item) for item in items),
    )


def _finish_stock_lote_items(
    lote_id: int,
    items: Sequence[dict[str, Any]],
    members_by_id: dict[int, dict[str, Any]],
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    *,
    source: str,
    target: str,
) -> None:
    reference = _reference_for_lote(lote_id)
    lote_info = {
        "id": lote_id,
        "reference": reference,
        "comprobante_numero": request_payload.get("comprobante_numero"),
        "sal": response_payload.get("sal"),
        "ent": response_payload.get("ent"),
    }
    for item in items:
        member = members_by_id.get(int(item["job_id"]))
        if member is None:
            continue
        _finish_job(
            int(item["job_id"]),
            JOB_STATUS_SUCCEEDED,
            request_payload={
                **_dict_payload(member.get("request_payload")),
                "lote_id": lote_id,
                "lote_reference": reference,
                "numero_serie": item["serial"],
                "article_code": item["article_code"],
                "source_stock_deposit": source,
                "target_deposit": target,
                "sal_done": True,
                "target_stock_entry_done": True,
                "transfer_phase": TRANSFER_PHASE_TARGET_ENTRY_DONE,
            },
            response_payload={**_dict_payload(member.get("response_payload")), "lote": lote_info},
            article_code=item["article_code"],
        )
    _release_stock_lote_leftovers(lote_id, items)


def _run_stock_lote(lote: dict[str, Any], members: list[dict[str, Any]], client: BejermanSDKClient) -> None:
    """Mueve un lote de equipos de origen a destino con UNA SAL y UNA ENT.

    Máquina de estados persistida en `bejerman_stock_lotes.phase`, porque cada paso puede
    cortarse a mitad de camino y el reintento tiene que saber qué ya entró:

    * `pending`: nada emitido. Se decide equipo por equipo (`_plan_stock_lote_member`), se
      congela la composición y recién después se manda la SAL.
    * `sal_pending`: la SAL se mandó pero no se sabe si entró. Lo dice el stock: si todas las
      unidades siguen en origen no entró y se vuelve a planificar; si no queda ninguna, entró.
    * `sal_done` / `ent_pending`: lo mismo con la ENT, mirando el destino.
    * `review`: el stock quedó como un solo comprobante no lo deja (unas unidades sí y otras
      no). No se emite nada más hasta que alguien mire Bejerman.

    Reenviar un comprobante congelado es seguro porque Bejerman deduplica por IdOrigen, y el
    choque sólo se absorbe si el stock confirma el movimiento (`_send_stock_comprobantes`).
    """
    validate_bejerman_config()
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_TRANSFER_OUT_COMPROBANTE")
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_TRANSFER_IN_COMPROBANTE")
    lote_id = int(lote["id"])
    reference = _reference_for_lote(lote_id)
    source = _norm(lote.get("source_deposit")) or "VAL"
    target = _norm(lote.get("target_deposit")) or "STL"
    request_payload = _dict_payload(lote.get("request_payload"))
    response_payload = _dict_payload(lote.get("response_payload"))
    loaded_phase = str(lote.get("phase") or "").strip() or LOTE_PHASE_PENDING
    phase = loaded_phase
    members_by_id = {int(member["id"]): member for member in members}
    items = [dict(item) for item in (request_payload.get("items") or []) if isinstance(item, dict)]

    if phase == LOTE_PHASE_DONE:
        _finish_stock_lote_items(
            lote_id, items, members_by_id, request_payload, response_payload, source=source, target=target
        )
        return
    if phase == LOTE_PHASE_REVIEW:
        raise BejermanBlockedError(
            str(lote.get("last_error") or "").strip()
            or f"El lote {reference} quedó a medias en Bejerman: revisalo antes de reintentar."
        )
    if phase in LOTE_EMITTING_PHASES and not items:
        _stock_lote_review(
            lote_id,
            f"El lote {reference} figura en curso ({phase}) pero no tiene renglones congelados: "
            "no se puede saber qué emitió.",
        )

    if phase == LOTE_PHASE_SAL_PENDING:
        en_origen = [_stock_lote_item_in(client, source, item) for item in items]
        if all(en_origen):
            phase = LOTE_PHASE_PENDING
        elif not any(en_origen):
            phase = LOTE_PHASE_SAL_DONE
            _update_stock_lote(lote_id, phase=phase)
        else:
            _stock_lote_review(
                lote_id,
                _stock_lote_partial_message(
                    reference,
                    "SAL",
                    items,
                    en_origen,
                    presentes=f"siguen en {source}",
                    ausentes=f"ya salieron de {source}",
                ),
            )

    ent_enviada = False
    if phase == LOTE_PHASE_PENDING:
        plan = int(request_payload.get("plan") or 0) + (1 if items else 0)
        items = [
            item
            for item in (
                _plan_stock_lote_member(member, client, source=source, target=target) for member in members
            )
            if item
        ]
        if not items:
            _update_stock_lote(
                lote_id,
                phase=LOTE_PHASE_DONE,
                request_payload={**request_payload, "plan": plan, "items": []},
                error="",
            )
            _release_stock_lote_leftovers(lote_id, items)
            return
        number = _stock_lote_comprobante_number(reference, plan)
        sal_payload, ent_payload = build_stock_lote_transfer_payloads(
            reference, number, items, source=source, target=target
        )
        request_payload = {
            **request_payload,
            "reference": reference,
            "plan": plan,
            "comprobante_numero": number,
            "items": items,
            "sal_comprobantes": sal_payload,
            "ent_comprobantes": ent_payload,
            "congelado_at": timezone.now().isoformat(),
        }
        # Se congela ANTES de mandar: si el envío se corta, el reintento tiene que repetir
        # exactamente este comprobante y no uno armado de nuevo.
        _update_stock_lote(lote_id, phase=LOTE_PHASE_SAL_PENDING, request_payload=request_payload, error="")
        for item in items:
            member = members_by_id.get(int(item["job_id"])) or {}
            _finish_job(
                int(item["job_id"]),
                JOB_STATUS_RUNNING,
                request_payload={
                    **_dict_payload(member.get("request_payload")),
                    "lote_id": lote_id,
                    "lote_reference": reference,
                    "article_code": item["article_code"],
                    "source_stock_deposit": source,
                    "transfer_phase": "sal_pending",
                },
                article_code=item["article_code"],
            )
        sal_response = _send_stock_comprobantes(
            client,
            sal_payload,
            phase="lote_sal",
            job_id=items[0]["job_id"],
            verify=lambda: not any(_stock_lote_item_in(client, source, item) for item in items),
        )
        response_payload = {**response_payload, "sal": sal_response}
        phase = LOTE_PHASE_SAL_DONE
        _update_stock_lote(lote_id, phase=phase, response_payload=response_payload)
    elif phase == LOTE_PHASE_SAL_DONE and loaded_phase == LOTE_PHASE_SAL_DONE:
        # Retomado después de una SAL confirmada. Si alguna unidad volvió al origen (un
        # movimiento a mano, una restauración), mandar la ENT duplicaría stock.
        vueltas = [_stock_lote_item_in(client, source, item) for item in items]
        if any(vueltas):
            _stock_lote_review(
                lote_id,
                f"El lote {reference} ya descontó de {source}, pero {_stock_lote_series(items, vueltas)} "
                f"volvieron a figurar ahí: mandar la ENT duplicaría stock. Revisá Bejerman antes de reintentar.",
            )

    if phase == LOTE_PHASE_SAL_DONE:
        _update_stock_lote(lote_id, phase=LOTE_PHASE_ENT_PENDING)
        ent_response = _send_stock_lote_ent(client, request_payload, items, target)
        response_payload = {**response_payload, "ent": ent_response}
        _update_stock_lote(lote_id, response_payload=response_payload)
        phase = LOTE_PHASE_ENT_PENDING
        ent_enviada = True

    if phase == LOTE_PHASE_ENT_PENDING and not ent_enviada:
        en_destino = [_stock_lote_item_in(client, target, item) for item in items]
        if not any(en_destino):
            ent_response = _send_stock_lote_ent(client, request_payload, items, target)
            response_payload = {**response_payload, "ent": ent_response}
            _update_stock_lote(lote_id, response_payload=response_payload)
        elif not all(en_destino):
            _stock_lote_review(
                lote_id,
                _stock_lote_partial_message(
                    reference,
                    "ENT",
                    items,
                    en_destino,
                    presentes=f"ya están en {target}",
                    ausentes=f"todavía no llegaron a {target}",
                ),
            )

    _verify_stock_lote_target(client, items, target)
    response_payload = {**response_payload, "destino_verificado_at": timezone.now().isoformat()}
    _update_stock_lote(lote_id, phase=LOTE_PHASE_DONE, response_payload=response_payload, error="")
    _finish_stock_lote_items(
        lote_id, items, members_by_id, request_payload, response_payload, source=source, target=target
    )


def _process_stock_lote_job(job: dict[str, Any], client: BejermanSDKClient, max_attempts: int) -> str:
    """Procesa el lote entero al que pertenece un job reclamado por el worker."""
    lote_id = int(job["lote_id"])
    if not _try_lock_stock_lote(lote_id):
        # Otro proceso (el worker o un "Reintentar" del panel) está moviendo este lote.
        _requeue_claimed_lote_job(job)
        return JOB_STATUS_PENDING
    try:
        try:
            members = _claim_stock_lote_members(lote_id, job)
            lote = _load_stock_lote(lote_id)
            if not lote:
                raise BejermanBlockedError(f"El lote de transferencias {lote_id} no existe.")
            _run_stock_lote(lote, members, client)
            return JOB_STATUS_SUCCEEDED
        except BejermanConfigError as exc:
            _set_running_lote_members(lote_id, JOB_STATUS_BLOCKED, error=_config_error_message(exc))
            return JOB_STATUS_BLOCKED
        except BejermanBlockedError as exc:
            if _is_retryable_sync_message(str(exc)):
                return _retry_stock_lote_members(lote_id, job, str(exc), max_attempts)
            _set_running_lote_members(lote_id, JOB_STATUS_BLOCKED, error=str(exc))
            return JOB_STATUS_BLOCKED
        except BejermanTransientError as exc:
            return _retry_stock_lote_members(lote_id, job, str(exc), max_attempts)
        except Exception as exc:
            logger.exception("Error sincronizando el lote Bejerman %s (job %s)", lote_id, job.get("id"))
            return _retry_stock_lote_members(lote_id, job, str(exc), max_attempts)
    finally:
        _unlock_stock_lote(lote_id)


def _process_claimed_job(job: dict[str, Any], client: BejermanSDKClient, max_attempts: int) -> str:
    if job.get("lote_id") and (job.get("sync_type") or "").strip() == SYNC_TYPE_STOCK_VAL_TO_STL:
        # Un equipo de un alta masiva se mueve junto con el resto de su lote. El lote maneja sus
        # propios errores: tiene que dejar a todos sus jobs en el mismo estado, no sólo a este.
        return _process_stock_lote_job(job, client, max_attempts)
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
            SYNC_TYPE_STOCK_LOCATION_TRANSFER,
            SYNC_TYPE_STOCK_VAL_TO_STL,
        ):
            return _process_stock_transfer(job, client)
        raise BejermanBlockedError(
            f"Tipo de sincronización no soportado: {sync_type}. "
            "Ninguna versión de NEXORA sabe ejecutar esta operación, así que reintentarla "
            "no puede funcionar: descártela desde el panel."
        )
    except BejermanConfigError as exc:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=_config_error_message(exc))
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
        context = _job_context(job)
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
    lote = _stock_lote_for_job(job)
    if lote and lote.get("phase") in (*LOTE_EMITTING_PHASES, LOTE_PHASE_REVIEW):
        # Restaurar un equipo suelto con su lote a mitad de camino emitiría un ENT por fuera del
        # comprobante compartido, y el lote después lo volvería a sumar: stock doble.
        return {"status": "skipped", "reason": "lote_en_curso", "job_id": job.get("id"), "lote_id": lote.get("id")}
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
        ent_response = _send_stock_comprobantes(
            client,
            ent_payload,
            phase="restore_ent",
            job_id=job["id"],
            verify=lambda: _stock_present_in_deposit(client, target, serial, article_code),
        )
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
        # El worker más importante del sistema no puede quedar vivo pero inútil.
        # Django recicla las conexiones en el ciclo de request; en un loop de consola
        # nadie lo hace, así que un reinicio de Postgres dejaba esta conexión rota
        # para siempre: el contenedor seguía en verde, el log repetía el mismo error
        # cada 30 segundos y ningún job de stock se movía hasta que alguien lo
        # reiniciaba a mano. Que es exactamente lo que no puede depender de alguien.
        close_old_connections()
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
            try:
                # Las emisiones de remito corren en un hilo del contenedor de la API,
                # no acá. Pero cuando ese contenedor se reinicia no queda nadie para
                # cerrar la fila, y este loop es el único proceso que ya está mirando
                # periódicamente qué quedó a mitad de camino. Import local: sólo para
                # no atar el módulo de sincronización al de entregas.
                from .delivery_remito_jobs import sweep_stale_remito_jobs

                barridos = sweep_stale_remito_jobs()
                if barridos:
                    logger.info("delivery_remito_jobs_stale_cerrados count=%s", barridos)
            except Exception:
                logger.exception("delivery_remito_jobs_sweep falló")
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
