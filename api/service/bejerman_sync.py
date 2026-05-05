import json
import logging
import time
from datetime import timedelta
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone


logger = logging.getLogger(__name__)

SYNC_TYPE_STOCK_STR_TO_STL = "stock_str_to_stl"
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_BLOCKED = "blocked"


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
        raise BejermanTransientError(f"Respuesta SOAP invalida: {exc}") from exc
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


def _first_value(record: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    lower = {str(k).lower(): v for k, v in record.items()}
    for name in candidates:
        if name in record:
            return record.get(name)
        key = name.lower()
        if key in lower:
            return lower[key]
    return None


def _extract_article_code(records: list[dict[str, Any]]) -> str:
    fields = (
        "Comprobante_Art_CodGen",
        "Art_CodGen",
        "ART_CODGEN",
        "Articulo_Codigo",
        "CodigoArticulo",
        "Codigo_Articulo",
        "Item_CodigoArticulo",
        "Codigo",
        "codigo",
    )
    for record in records:
        value = _first_value(record, fields)
        if value:
            return str(value).strip()
    return ""


def _records_for_partida(response: dict[str, Any], serial: str, deposit: str) -> list[dict[str, Any]]:
    serial_norm = _norm(serial)
    deposit_norm = _norm(deposit)
    records: list[dict[str, Any]] = []
    for record in _iter_dicts(response.get("DatosJSON")):
        partida = _first_value(
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
        deposito = _first_value(
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
        if partida and _norm(partida) != serial_norm:
            continue
        if deposito and _norm(deposito) != deposit_norm:
            continue
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
        if qty not in (None, ""):
            try:
                if float(qty) <= 0:
                    continue
            except Exception:
                pass
        records.append(record)
    return records


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
            raise BejermanBlockedError("Bejerman no devolvio token")
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
        # En Bejerman SEP, ObtenerStockDepositoPartida responde OK pero vacio para partidas reales.
        # ObtenerStockPartida devuelve todos los depositos; filtramos por deposito localmente.
        return self.execute("STOCK", "ObtenerStockPartida", params=[serial])

    def ingresar_lista_comprobantes_json(self, comprobantes: list[dict[str, Any]]) -> dict[str, str]:
        datos_comprobantes = _json_param(comprobantes)
        return self.execute(
            "STOCK",
            "IngresarListaComprobantesJSON",
            params_json=[datos_comprobantes, str(_setting("BEJERMAN_NUMERA_FLEX", "S") or "S")],
        )


def validate_bejerman_config() -> None:
    required = [
        "BEJERMAN_WSDL_URL",
        "BEJERMAN_USER",
        "BEJERMAN_PASSWORD",
        "BEJERMAN_COMPANY",
        "BEJERMAN_WORKSTATION",
        "BEJERMAN_STOCK_TRANSFER_COMPROBANTE",
    ]
    missing = [name for name in required if not str(_setting(name, "") or "").strip()]
    if missing:
        raise BejermanConfigError("Faltan variables: " + ", ".join(missing))


def enqueue_stock_transfer_for_ingreso(ingreso_id: int, ingreso_event_id: int | None = None) -> dict[str, Any]:
    source_deposit = str(_setting("BEJERMAN_SOURCE_DEPOSIT", "STR") or "STR").strip()
    target_deposit = str(_setting("BEJERMAN_TARGET_DEPOSIT", "STL") or "STL").strip()
    row = q(
        """
        SELECT t.id AS ingreso_id,
               d.id AS device_id,
               COALESCE(d.numero_serie, '') AS numero_serie
          FROM ingresos t
          JOIN devices d ON d.id = t.device_id
         WHERE t.id = %s
        """,
        [ingreso_id],
        one=True,
    )
    if not row:
        raise BejermanBlockedError(f"Ingreso {ingreso_id} no encontrado")
    if ingreso_event_id is None:
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
        ingreso_event_id = event and event.get("id")
    serial = (row.get("numero_serie") or "").strip()
    status = JOB_STATUS_PENDING if serial else JOB_STATUS_BLOCKED
    last_error = None if serial else "numero_serie requerido para sincronizar Bejerman"
    request_payload = {
        "source": "nexora",
        "trigger": "ingreso_liberado",
        "reference": f"NEXORA-OS-{ingreso_id}",
    }
    return q(
        """
        INSERT INTO bejerman_sync_jobs(
          sync_type, ingreso_id, device_id, ingreso_event_id, numero_serie,
          source_deposit, target_deposit, status, last_error, request_payload
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (sync_type, ingreso_id) DO UPDATE
           SET ingreso_event_id = COALESCE(bejerman_sync_jobs.ingreso_event_id, EXCLUDED.ingreso_event_id),
               updated_at = CURRENT_TIMESTAMP
        RETURNING id, status, attempts
        """,
        [
            SYNC_TYPE_STOCK_STR_TO_STL,
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


def _finish_job(job_id: int, status: str, *, error: str | None = None, request_payload=None, response_payload=None):
    q(
        """
        UPDATE bejerman_sync_jobs
           SET status = %s,
               last_error = %s,
               request_payload = COALESCE(%s::jsonb, request_payload),
               response_payload = COALESCE(%s::jsonb, response_payload),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        [
            status,
            error,
            _json_param(request_payload) if request_payload is not None else None,
            _json_param(response_payload) if response_payload is not None else None,
            job_id,
        ],
    )


def _retry_job(job: dict[str, Any], error: str, max_attempts: int):
    attempts = int(job.get("attempts") or 0)
    if attempts >= max_attempts:
        _finish_job(job["id"], JOB_STATUS_BLOCKED, error=f"Maximos reintentos agotados: {error}")
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


def build_stock_transfer_payload(job: dict[str, Any], article_code: str) -> list[dict[str, Any]]:
    now = timezone.localtime().replace(microsecond=0).isoformat()
    reference = f"NEXORA-OS-{job['ingreso_id']}"
    serial = (job.get("numero_serie") or "").strip()
    tipo_operacion = str(_setting("BEJERMAN_STOCK_TRANSFER_TIPO_OPERACION", "") or "").strip()
    common = {
        "Comprobante_FechaEmision": now,
        "Comprobante_Tipo": str(_setting("BEJERMAN_STOCK_TRANSFER_COMPROBANTE", "TRA") or "").strip(),
        "Comprobante_Letra": "",
        "Comprobante_PtoVenta": "",
        "Comprobante_Numero": f"{int(job['ingreso_id']) % 100000000:08d}",
        "Comprobante_Art_CodGen": article_code,
        "Comprobante_ArtPartida": serial,
        "Comprobante_DescArticulo": f"NEXORA {serial}"[:50],
        "Comprobante_ArtCodTipo": "1",
        "Partida_Observaciones": reference[:20],
    }
    if tipo_operacion:
        common["Comprobante_TipoOperacion"] = tipo_operacion
    source = {
        **common,
        "Comprobante_ArtDeposito": job.get("source_deposit") or "STR",
        "Comprobante_CantidadUM1": -1,
        "Comprobante_CantidadUM2": -1,
        "Comprobante_IdOrigen": f"{reference}-{job.get('source_deposit') or 'STR'}",
    }
    target = {
        **common,
        "Comprobante_ArtDeposito": job.get("target_deposit") or "STL",
        "Comprobante_CantidadUM1": 1,
        "Comprobante_CantidadUM2": 1,
        "Comprobante_IdOrigen": f"{reference}-{job.get('target_deposit') or 'STL'}",
    }
    return [source, target]


def _process_claimed_job(job: dict[str, Any], client: BejermanSDKClient, max_attempts: int) -> str:
    try:
        validate_bejerman_config()
        serial = (job.get("numero_serie") or "").strip()
        if not serial:
            raise BejermanBlockedError("numero_serie requerido para sincronizar Bejerman")

        source_response = client.stock_by_deposit_partida(job.get("source_deposit") or "STR", serial)
        target_response = client.stock_by_deposit_partida(job.get("target_deposit") or "STL", serial)
        source_records = _records_for_partida(source_response, serial, job.get("source_deposit") or "STR")
        target_records = _records_for_partida(target_response, serial, job.get("target_deposit") or "STL")

        if target_records and not source_records:
            _finish_job(
                job["id"],
                JOB_STATUS_SUCCEEDED,
                request_payload={"idempotent": True, "numero_serie": serial},
                response_payload={"target": target_response},
            )
            return JOB_STATUS_SUCCEEDED
        if target_records and source_records:
            raise BejermanBlockedError(f"Partida {serial} encontrada en ambos depositos")
        if not source_records:
            raise BejermanBlockedError(f"Partida {serial} no encontrada en deposito {job.get('source_deposit')}")

        article_code = _extract_article_code(source_records)
        if not article_code:
            raise BejermanBlockedError(f"No se pudo resolver articulo para partida {serial}")

        comprobantes = build_stock_transfer_payload(job, article_code)
        _finish_job(job["id"], JOB_STATUS_RUNNING, request_payload={"comprobantes": comprobantes})
        response = client.ingresar_lista_comprobantes_json(comprobantes)
        _finish_job(
            job["id"],
            JOB_STATUS_SUCCEEDED,
            request_payload={"comprobantes": comprobantes},
            response_payload=response,
        )
        return JOB_STATUS_SUCCEEDED
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
            logger.exception("bejerman_sync_loop fallo")
        time.sleep(max(1, int(interval_seconds or 30)))
