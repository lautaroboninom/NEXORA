import json
import logging
import re
import time
import unicodedata
from datetime import timedelta
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

SYNC_TYPE_STOCK_ENTRY_STR = "stock_entry_str"
SYNC_TYPE_STOCK_STR_TO_STL = "stock_str_to_stl"
SYNC_TYPE_STOCK_STR_TO_STC = "stock_str_to_stc"
SYNC_TYPE_STOCK_STR_TO_STCL = "stock_str_to_stcl"  # Alias legacy previo a STC.
SYNC_TYPE_STOCK_EXIT_RTS = "stock_exit_rts"  # Nombre legacy; emite comprobante RSS.

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_BLOCKED = "blocked"

INTERNAL_CODE_RE = re.compile(r"^MG\s*\d{1,4}$", re.IGNORECASE)


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
    return resultado.startswith("OK") and not error


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


def _is_internal_equipment(numero_interno: str, numero_serie: str = "") -> bool:
    return bool(INTERNAL_CODE_RE.match((numero_interno or "").strip())) or bool(
        INTERNAL_CODE_RE.match((numero_serie or "").strip())
    )


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


def _extract_article_code(records: list[dict[str, Any]]) -> str:
    for record in records:
        value = _article_code_from_record(record)
        if value:
            return value
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


def _records_for_partida(response: dict[str, Any], serial: str, deposit: str | None = None) -> list[dict[str, Any]]:
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
        qty = _quantity_from_record(record)
        if qty is not None and qty <= 0:
            continue
        records.append(record)
    return records


def _other_positive_records(response: dict[str, Any], serial: str, expected_deposit: str) -> list[dict[str, Any]]:
    expected = _norm(expected_deposit)
    out = []
    for record in _records_for_partida(response, serial):
        deposito = _deposit_from_record(record)
        if deposito and _norm(deposito) == expected:
            continue
        out.append(record)
    return out


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
    def __init__(self):
        self.wsdl_url = str(_setting("BEJERMAN_WSDL_URL", "") or "").strip()
        self.endpoint_url = self.wsdl_url.split("?", 1)[0]
        self.timeout = int(_setting("BEJERMAN_REQUEST_TIMEOUT", 30) or 30)
        self.token = ""

    def _post(self, action: str, body: str) -> dict[str, str]:
        if not self.endpoint_url:
            raise BejermanConfigError("BEJERMAN_WSDL_URL requerido")
        envelope = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<s:Body>{body}</s:Body>"
            "</s:Envelope>"
        )
        try:
            response = requests.post(
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
            raise BejermanBlockedError(error)
        return parsed

    def register(self) -> str:
        body = (
            '<EFlexSDK_WSRegistro xmlns="http://localhost:57213/">'
            f"<xUsuario>{escape(str(_setting('BEJERMAN_USER', '') or ''))}</xUsuario>"
            f"<xClave>{escape(str(_setting('BEJERMAN_PASSWORD', '') or ''))}</xClave>"
            f"<xCodEmpresa>{escape(str(_setting('BEJERMAN_COMPANY', '') or ''))}</xCodEmpresa>"
            f"<xPtoTrabajo>{escape(str(_setting('BEJERMAN_WORKSTATION', '') or ''))}</xPtoTrabajo>"
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
        "BEJERMAN_USER",
        "BEJERMAN_PASSWORD",
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
    device_variante_sql = "COALESCE(d.variante, '')" if _column_exists("devices", "variante") else "''"
    model_variante_sql = "COALESCE(m.variante, '')" if _column_exists("models", "variante") else "''"
    model_tipo_sql = "COALESCE(m.tipo_equipo, '')" if _column_exists("models", "tipo_equipo") else "''"
    row = q(
        f"""
        SELECT t.id AS ingreso_id,
               d.id AS device_id,
               d.model_id,
               d.marca_id,
               COALESCE(d.numero_serie, '') AS numero_serie,
               COALESCE(d.numero_interno, '') AS numero_interno,
               COALESCE(m.nombre, '') AS modelo,
               {model_tipo_sql} AS tipo_equipo,
               {ingreso_variante_sql} AS ingreso_variante,
               {device_variante_sql} AS device_variante,
               {model_variante_sql} AS model_variante,
               COALESCE(b.nombre, '') AS marca
          FROM ingresos t
          JOIN devices d ON d.id = t.device_id
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


def ingreso_is_internal_equipment(ingreso_id: int) -> bool:
    row = _ingreso_context(ingreso_id)
    return _is_internal_equipment(row.get("numero_interno") or "", row.get("numero_serie") or "")


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


def _enqueue_stock_job(
    *,
    ingreso_id: int,
    sync_type: str,
    source_deposit: str,
    target_deposit: str,
    trigger: str,
    ingreso_event_id: int | None = None,
) -> dict[str, Any]:
    row = _ingreso_context(ingreso_id)
    serial = (row.get("numero_serie") or "").strip()
    status = JOB_STATUS_PENDING if serial else JOB_STATUS_BLOCKED
    last_error = None if serial else "Número de serie requerido para sincronizar Bejerman"
    request_payload = {
        "source": "nexora",
        "trigger": trigger,
        "reference": f"NEXORA-OS-{ingreso_id}",
        "marca": row.get("marca") or "",
        "modelo": row.get("modelo") or "",
        "variante": row.get("variante") or "",
    }
    return q(
        """
        INSERT INTO bejerman_sync_jobs(
          sync_type, ingreso_id, device_id, ingreso_event_id, numero_serie,
          source_deposit, target_deposit, status, last_error, request_payload
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (sync_type, ingreso_id) DO UPDATE
           SET ingreso_event_id = COALESCE(bejerman_sync_jobs.ingreso_event_id, EXCLUDED.ingreso_event_id),
               device_id = EXCLUDED.device_id,
               numero_serie = EXCLUDED.numero_serie,
               source_deposit = EXCLUDED.source_deposit,
               target_deposit = EXCLUDED.target_deposit,
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
            status,
            last_error,
            _json_param(request_payload),
        ],
        one=True,
    )


def enqueue_stock_entry_for_ingreso(ingreso_id: int, ingreso_event_id: int | None = None) -> dict[str, Any]:
    target_deposit = str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR").strip() or "STR"
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_ENTRY_STR,
        source_deposit="NEXORA",
        target_deposit=target_deposit,
        trigger="ingreso_creado",
        ingreso_event_id=ingreso_event_id,
    )


def enqueue_stock_transfer_for_ingreso(ingreso_id: int, ingreso_event_id: int | None = None) -> dict[str, Any]:
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
    )


def enqueue_client_ready_transfer_for_ingreso(ingreso_id: int, ingreso_event_id: int | None = None) -> dict[str, Any]:
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
    )


def enqueue_stock_exit_for_ingreso(
    ingreso_id: int,
    ingreso_event_id: int | None = None,
    source_deposit: str | None = None,
) -> dict[str, Any]:
    if source_deposit is None:
        if ingreso_is_internal_equipment(ingreso_id):
            source_deposit = str(_setting("BEJERMAN_TARGET_DEPOSIT", "STL") or "STL").strip() or "STL"
        else:
            source_deposit = str(_setting("BEJERMAN_CLIENT_TARGET_DEPOSIT", "STC") or "STC").strip() or "STC"
    if ingreso_event_id is None:
        ingreso_event_id = _last_liberado_event_id(ingreso_id)
    return _enqueue_stock_job(
        ingreso_id=ingreso_id,
        sync_type=SYNC_TYPE_STOCK_EXIT_RTS,
        source_deposit=source_deposit,
        target_deposit="SALIDA",
        trigger="remito_salida_rss",
        ingreso_event_id=ingreso_event_id,
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


def _retry_job(job: dict[str, Any], error: str, max_attempts: int):
    attempts = int(job.get("attempts") or 0)
    if attempts >= max_attempts:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=f"Máximos reintentos agotados: {error}")
        return
    delay_seconds = min(3600, max(60, 2 ** max(0, attempts - 1) * 60))
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


def _mapping_for_context(context: dict[str, Any]) -> dict[str, Any] | None:
    model_id = context.get("model_id")
    if not model_id:
        return None
    return q(
        """
        SELECT *
          FROM bejerman_article_mappings
         WHERE model_id = %s
           AND variante_norm = %s
         LIMIT 1
        """,
        [model_id, normalize_article_variant(context.get("variante"))],
        one=True,
    )


def upsert_article_mapping(
    *,
    model_id: int,
    variante: str = "",
    article_code: str,
    article_description: str = "",
    match_source: str = "manual",
    source_payload: Any = None,
    confirmed_by: int | None = None,
) -> dict[str, Any]:
    code = (article_code or "").strip()
    if not code:
        raise BejermanBlockedError("Código de artículo Bejerman requerido")
    variant = (variante or "").strip()
    variant_norm = normalize_article_variant(variant)
    now_confirmed_sql = "CURRENT_TIMESTAMP" if match_source == "manual" else "NULL"
    row = q(
        f"""
        INSERT INTO bejerman_article_mappings(
          model_id, variante, variante_norm, article_code, article_description,
          match_source, source_payload, confirmed_by, confirmed_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,{now_confirmed_sql})
        ON CONFLICT (model_id, variante_norm) DO UPDATE
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
        ],
        one=True,
    )
    return row or {}


def reopen_jobs_for_article_mapping(model_id: int, variante: str, article_code: str) -> int:
    variant_norm = normalize_article_variant(variante)
    rows = q(
        """
        SELECT j.id, j.ingreso_id
          FROM bejerman_sync_jobs j
          JOIN ingresos t ON t.id = j.ingreso_id
          JOIN devices d ON d.id = t.device_id
         WHERE d.model_id = %s
           AND j.status = 'blocked'
           AND COALESCE(j.article_code, '') = ''
           AND (
             COALESCE(j.last_error, '') ILIKE '%%artículo%%'
             OR COALESCE(j.last_error, '') ILIKE '%%articulo%%'
             OR j.response_payload ? 'candidates'
           )
        """,
        [model_id],
    ) or []
    ids: list[int] = []
    for row in rows:
        try:
            context = _ingreso_context(int(row["ingreso_id"]))
        except Exception:
            continue
        if normalize_article_variant(context.get("variante")) == variant_norm:
            ids.append(int(row["id"]))
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


def _article_candidates(context: dict[str, Any], response: dict[str, Any]) -> list[dict[str, Any]]:
    model_tokens = _tokens(context.get("modelo"))
    variant_tokens = _tokens(context.get("variante"))
    brand_tokens = _tokens(context.get("marca"))
    required = model_tokens | variant_tokens
    candidates: list[dict[str, Any]] = []
    for record in _iter_dicts(response.get("DatosJSON")):
        code = _article_code_from_record(record)
        description = _article_description_from_record(record)
        haystack = " ".join([code, description, _json_param(record)])
        hay_tokens = _tokens(haystack)
        if not code:
            continue
        if required and not required.issubset(hay_tokens):
            continue
        brand_score = len(brand_tokens & hay_tokens)
        candidates.append(
            {
                "article_code": code,
                "article_description": description,
                "brand_score": brand_score,
                "raw": record,
            }
        )
    candidates.sort(key=lambda item: (item["brand_score"], item["article_code"]), reverse=True)
    return candidates[:20]


def _resolve_article_for_job(job: dict[str, Any], client: BejermanSDKClient) -> str:
    existing = (job.get("article_code") or "").strip()
    if existing:
        return existing
    context = _ingreso_context(int(job["ingreso_id"]))
    mapping = _mapping_for_context(context)
    if mapping and (mapping.get("article_code") or "").strip():
        code = (mapping.get("article_code") or "").strip()
        _finish_job(job["id"], JOB_STATUS_RUNNING, article_code=code)
        return code

    if not bool(_setting("BEJERMAN_ARTICLE_AUTO_MATCH", True)):
        raise BejermanBlockedError("No hay mapeo local de artículo Bejerman para este modelo/variante")

    response = client.obtener_articulos()
    candidates = _article_candidates(context, response)
    if len(candidates) == 1:
        candidate = candidates[0]
        code = candidate["article_code"]
        upsert_article_mapping(
            model_id=int(context["model_id"]),
            variante=context.get("variante") or "",
            article_code=code,
            article_description=candidate.get("article_description") or "",
            match_source="auto",
            source_payload=candidate.get("raw") or {},
        )
        _finish_job(job["id"], JOB_STATUS_RUNNING, response_payload={"article_match": candidate}, article_code=code)
        return code

    payload = {
        "marca": context.get("marca") or "",
        "modelo": context.get("modelo") or "",
        "variante": context.get("variante") or "",
        "candidates": candidates,
    }
    _finish_job(job["id"], JOB_STATUS_BLOCKED, response_payload=payload)
    if not candidates:
        raise BejermanBlockedError("No se encontró un artículo Bejerman único para este modelo/variante")
    raise BejermanBlockedError("Hay más de un artículo Bejerman posible para este modelo/variante")


def build_stock_transfer_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    document_date = _stock_document_date()
    reference = f"NEXORA-OS-{job['ingreso_id']}"
    serial = (job.get("numero_serie") or "").strip()
    tipo_operacion = str(_setting("BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION", "") or "").strip()
    common = {
        "Comprobante_FechaEmision": document_date,
        "Comprobante_Tipo": str(_setting("BEJERMAN_STOCK_TRANSFER_COMPROBANTE", "TRA") or "").strip(),
        "Comprobante_Letra": "",
        "Comprobante_PtoVenta": "",
        "Comprobante_Numero": f"{int(job['ingreso_id']) % 100000000:08d}",
        "Comprobante_Art_CodGen": article_code,
        "Comprobante_ArtPartida": serial,
        "Comprobante_DescArticulo": f"NEXORA {serial}"[:50],
        "Comprobante_ArtCodTipo": "1",
        "Comprobante_PrecioTotalMonLocal": 0,
        "Partida_Observaciones": reference[:20],
    }
    if tipo_operacion:
        common["Comprobante_TipoOperacion"] = tipo_operacion
    source_quantity = -1
    target_quantity = 1
    source = {
        **common,
        "Comprobante_ArtDeposito": job.get("source_deposit") or "STR",
        "Comprobante_CantidadUM1": source_quantity,
        "Comprobante_CantidadUM2": source_quantity,
        "Comprobante_IdOrigen": f"{reference}-{job.get('source_deposit') or 'STR'}",
    }
    target = {
        **common,
        "Comprobante_ArtDeposito": job.get("target_deposit") or "STL",
        "Comprobante_CantidadUM1": target_quantity,
        "Comprobante_CantidadUM2": target_quantity,
        "Comprobante_IdOrigen": f"{reference}-{job.get('target_deposit') or 'STL'}",
    }
    return [source, target]


def build_stock_entry_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    document_date = _stock_document_date()
    reference = f"NEXORA-OS-{job['ingreso_id']}"
    serial = (job.get("numero_serie") or "").strip()
    comprobante = str(_setting("BEJERMAN_STOCK_ENTRY_COMPROBANTE", "") or "").strip()
    return [
        {
            "Comprobante_FechaEmision": document_date,
            "Comprobante_Tipo": comprobante,
            "Comprobante_Letra": "",
            "Comprobante_PtoVenta": "",
            "Comprobante_Numero": f"{int(job['ingreso_id']) % 100000000:08d}",
            "Comprobante_Art_CodGen": article_code,
            "Comprobante_ArtPartida": serial,
            "Comprobante_DescArticulo": f"NEXORA {serial}"[:50],
            "Comprobante_ArtCodTipo": "1",
            "Comprobante_ArtDeposito": job.get("target_deposit") or "STR",
            "Comprobante_CantidadUM1": 1,
            "Comprobante_CantidadUM2": 1,
            "Comprobante_PrecioTotalMonLocal": 0,
            "Comprobante_IdOrigen": f"{reference}-RIS-{job.get('target_deposit') or 'STR'}",
            "Partida_Observaciones": reference[:20],
        }
    ]


def build_stock_exit_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    document_date = _stock_document_date()
    reference = f"NEXORA-OS-{job['ingreso_id']}"
    serial = (job.get("numero_serie") or "").strip()
    source_deposit = job.get("source_deposit") or "STC"
    return [
        {
            "Comprobante_FechaEmision": document_date,
            "Comprobante_Tipo": str(_setting("BEJERMAN_STOCK_EXIT_COMPROBANTE", "RSS") or "").strip(),
            "Comprobante_Letra": "",
            "Comprobante_PtoVenta": "",
            "Comprobante_Numero": f"{int(job['ingreso_id']) % 100000000:08d}",
            "Comprobante_Art_CodGen": article_code,
            "Comprobante_ArtPartida": serial,
            "Comprobante_DescArticulo": f"NEXORA {serial}"[:50],
            "Comprobante_ArtCodTipo": "1",
            "Comprobante_ArtDeposito": source_deposit,
            "Comprobante_CantidadUM1": -1,
            "Comprobante_CantidadUM2": -1,
            "Comprobante_PrecioTotalMonLocal": 0,
            "Comprobante_IdOrigen": f"{reference}-RSS-{source_deposit}",
            "Partida_Observaciones": reference[:20],
        }
    ]


def _process_stock_entry(job: dict[str, Any], client: BejermanSDKClient) -> str:
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_ENTRY_COMPROBANTE")
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        raise BejermanBlockedError("Número de serie requerido para sincronizar Bejerman")

    target = job.get("target_deposit") or str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR")
    stock_response = client.stock_by_deposit_partida(target, serial)
    target_records = _records_for_partida(stock_response, serial, target)
    if target_records:
        article_code = _extract_article_code(target_records) or (job.get("article_code") or "")
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={"idempotent": True, "numero_serie": serial, "target_deposit": target},
            response_payload={"target": stock_response},
            article_code=article_code,
        )
        return JOB_STATUS_SUCCEEDED

    other_records = _other_positive_records(stock_response, serial, target)
    if other_records:
        deposits = sorted({_deposit_from_record(record) or "-" for record in other_records})
        _finish_job(job["id"], JOB_STATUS_BLOCKED, response_payload={"stock": stock_response})
        raise BejermanBlockedError(
            f"Partida {serial} encontrada en otro depósito ({', '.join(deposits)}); requiere conciliación"
        )

    article_code = _resolve_article_for_job(job, client)
    comprobantes = build_stock_entry_payload(job, article_code)
    _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload={"comprobantes": comprobantes}, article_code=article_code)
    response = client.ingresar_lista_comprobantes_json(comprobantes)
    _finish_job(
        job["id"],
        JOB_STATUS_SUCCEEDED,
        request_payload={"comprobantes": comprobantes},
        response_payload=response,
        article_code=article_code,
    )
    return JOB_STATUS_SUCCEEDED


def _process_stock_exit(job: dict[str, Any], client: BejermanSDKClient) -> str:
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_EXIT_COMPROBANTE")
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        raise BejermanBlockedError("Número de serie requerido para sincronizar Bejerman")

    source = job.get("source_deposit") or "STC"
    stock_response = client.stock_by_deposit_partida(source, serial)
    source_records = _records_for_partida(stock_response, serial, source)
    if not source_records:
        other_records = _other_positive_records(stock_response, serial, source)
        if other_records:
            deposits = sorted({_deposit_from_record(record) or "-" for record in other_records})
            _finish_job(job["id"], JOB_STATUS_BLOCKED, response_payload={"stock": stock_response})
            raise BejermanBlockedError(
                f"Partida {serial} encontrada en otro depósito ({', '.join(deposits)}); falta transferencia a {source}"
            )
        raise BejermanBlockedError(f"Partida {serial} no encontrada en depósito {source} para emitir RSS")

    article_code = (job.get("article_code") or "").strip() or _extract_article_code(source_records)
    if not article_code:
        article_code = _resolve_article_for_job(job, client)

    comprobantes = build_stock_exit_payload(job, article_code)
    _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload={"comprobantes": comprobantes}, article_code=article_code)
    response = client.ingresar_lista_comprobantes_json(comprobantes)
    _finish_job(
        job["id"],
        JOB_STATUS_SUCCEEDED,
        request_payload={"comprobantes": comprobantes},
        response_payload=response,
        article_code=article_code,
    )
    return JOB_STATUS_SUCCEEDED


def _process_stock_transfer(job: dict[str, Any], client: BejermanSDKClient) -> str:
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_TRANSFER_COMPROBANTE")
    serial = (job.get("numero_serie") or "").strip()
    if not serial:
        raise BejermanBlockedError("Número de serie requerido para sincronizar Bejerman")

    source_response = client.stock_by_deposit_partida(job.get("source_deposit") or "STR", serial)
    target_response = client.stock_by_deposit_partida(job.get("target_deposit") or "STL", serial)
    source_records = _records_for_partida(source_response, serial, job.get("source_deposit") or "STR")
    target_records = _records_for_partida(target_response, serial, job.get("target_deposit") or "STL")

    if target_records and not source_records:
        article_code = _extract_article_code(target_records) or (job.get("article_code") or "")
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={"idempotent": True, "numero_serie": serial},
            response_payload={"target": target_response},
            article_code=article_code,
        )
        return JOB_STATUS_SUCCEEDED
    if target_records and source_records:
        raise BejermanBlockedError(f"Partida {serial} encontrada en ambos depósitos")
    if not source_records:
        raise BejermanBlockedError(f"Partida {serial} no encontrada en depósito {job.get('source_deposit')}")

    article_code = (job.get("article_code") or "").strip() or _extract_article_code(source_records)
    if not article_code:
        article_code = _resolve_article_for_job(job, client)

    comprobantes = build_stock_transfer_payload(job, article_code)
    _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload={"comprobantes": comprobantes}, article_code=article_code)
    response = client.ingresar_lista_comprobantes_json(comprobantes)
    _finish_job(
        job["id"],
        JOB_STATUS_SUCCEEDED,
        request_payload={"comprobantes": comprobantes},
        response_payload=response,
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
        if sync_type in (SYNC_TYPE_STOCK_STR_TO_STL, SYNC_TYPE_STOCK_STR_TO_STC, SYNC_TYPE_STOCK_STR_TO_STCL):
            return _process_stock_transfer(job, client)
        raise BejermanBlockedError(f"Tipo de sincronización no soportado: {sync_type}")
    except BejermanConfigError as exc:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=str(exc))
        return JOB_STATUS_BLOCKED
    except BejermanBlockedError as exc:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=str(exc))
        return JOB_STATUS_BLOCKED
    except BejermanTransientError as exc:
        _retry_job(job, str(exc), max_attempts)
        return JOB_STATUS_FAILED
    except Exception as exc:
        logger.exception("Error sincronizando job Bejerman %s", job.get("id"))
        _retry_job(job, str(exc), max_attempts)
        return JOB_STATUS_FAILED


def process_bejerman_jobs(limit: int = 10, job_id: int | None = None, client: BejermanSDKClient | None = None) -> dict[str, int]:
    if not bool(_setting("BEJERMAN_SYNC_ENABLED", False)):
        return {"processed": 0, "disabled": 1}
    max_attempts = int(_setting("BEJERMAN_MAX_ATTEMPTS", 8) or 8)
    stats = {"processed": 0, JOB_STATUS_SUCCEEDED: 0, JOB_STATUS_FAILED: 0, JOB_STATUS_BLOCKED: 0}
    client = client or BejermanSDKClient()
    for _ in range(max(1, int(limit or 1))):
        with transaction.atomic():
            job = _claim_job(max_attempts=max_attempts, job_id=job_id)
        if not job:
            break
        status = _process_claimed_job(job, client, max_attempts)
        stats["processed"] += 1
        stats[status] = stats.get(status, 0) + 1
        if job_id is not None:
            break
    return stats


def run_bejerman_sync_loop(interval_seconds: int = 30, limit: int = 10):
    while True:
        try:
            stats = process_bejerman_jobs(limit=limit)
            logger.info("bejerman_sync_loop stats=%s", stats)
        except Exception:
            logger.exception("bejerman_sync_loop falló")
        time.sleep(max(1, int(interval_seconds or 30)))
