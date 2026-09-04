from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.utils import timezone

from .bejerman_companies import company_for_key, require_company
from .bejerman_documents import is_transient_pdf_lookup_message
from .bejerman_remito_legends import (
    SERVICE_INGRESS_TITLE,
    SERVICE_RELEASE_RESOLUTION_LABELS,
    accessory_legend_lines,
    delivery_order_legend,
    service_ingress_equipment_legend_lines,
    service_ingress_main_legend,
    service_ingress_reason_legend,
    service_release_item_legend,
    service_release_legend,
    service_release_order_reference,
    service_release_resolution_label,
)
from .bejerman_pdf_remote import (
    is_remote_pdf_path,
    read_remote_pdf,
    read_remote_pdf_candidates,
    remote_pdf_uri,
    remove_remote_pdf_candidates_if_same,
    write_remote_pdf,
)
from .bejerman_pdf_settings import BejermanPdfOutputSettingsError, configured_pdf_output_dir
from .bejerman_user_credentials import (
    BEJERMAN_CREDENTIALS_REQUIRED,
    BejermanUserCredentialsError,
    get_user_bejerman_credentials,
    resolve_user_bejerman_workstation,
)


logger = logging.getLogger(__name__)

REGISTRO_ACTION = "http://localhost:57213/IEFlexSDK_Service/EFlexSDK_WSRegistro"
EJECUTAR_ACTION = "http://localhost:57213/IEFlexSDK_Service/EFlexSDK_WSEjecutar"

BILLING_COMPROBANTE_TYPES = {"FC", "NC", "ND"}
REMITO_COMPROBANTE_TYPES = {"RT", "RD", "RTA", "RTN", "RSS", "RIS", "RDA", "RDN"}
TIPO_OPERACION_LABELS = {
    "ALQ": "Alquiler",
    "BUSO": "Vta Bien de uso",
    "DEMO": "Demostración",
    "FAB": "Fabricación",
    "MC": "Mercadería",
    "REP": "Reparación",
}
TIPO_OPERACION_LABELS.update(
    {
        "DEMO": "Demostración",
        "FAB": "Fabricación",
        "MC": "Mercadería",
        "REP": "Reparación",
    }
)


class BejermanSdkError(RuntimeError):
    pass


class BejermanSdkConfigError(BejermanSdkError):
    pass


class BejermanSdkUnavailable(BejermanSdkError):
    pass


class BejermanSdkTransportUnavailable(BejermanSdkUnavailable):
    """El pedido no llegó a ejecutarse: el servicio no estaba en pie.

    Se distingue del resto de las fallas de red porque no deja ningún efecto en
    Bejerman (no se abrió la operación del SDK) y porque suele durar segundos,
    así que es la única que se puede repetir sola sin arriesgar un duplicado.
    """


# `requests.ConnectionError` tapa dos situaciones muy distintas: que la conexión no se
# haya podido abrir (servicio caído, DNS, puerto cerrado) y que se haya cortado con el
# pedido **ya en vuelo**. Sólo la primera permite afirmar que la operación no se ejecutó.
# La segunda puede dejar un comprobante emitido del otro lado, y tratarla como "no pasó
# nada" hace que se le diga al operador que reintente una emisión que quizá ya entró.
_IN_FLIGHT_CONNECTION_MARKERS = (
    "connection aborted",
    "connection reset",
    "remotedisconnected",
    "remote end closed connection",
    "response ended prematurely",
    "broken pipe",
)


def _connection_error_was_in_flight(exc: BaseException) -> bool:
    """¿El corte ocurrió con el pedido ya mandado?

    Se recorre toda la cadena: requests envuelve el error de urllib3 en `args[0]`, no
    siempre en `__cause__`. Ante la duda devuelve False, que es el comportamiento
    histórico (reintentable); lo que se busca acá es reconocer los cortes que sí
    pudieron dejar efecto.
    """
    seen: set[int] = set()
    pending: list[Any] = [exc]
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (ConnectionResetError, BrokenPipeError)):
            return True
        texto = str(current).lower()
        if any(marker in texto for marker in _IN_FLIGHT_CONNECTION_MARKERS):
            return True
        pending.append(current.__cause__)
        pending.append(current.__context__)
        pending.extend(getattr(current, "args", ()))
    return False


class BejermanSdkResponseError(BejermanSdkError):
    pass


class BejermanPdfPendingError(BejermanSdkResponseError):
    def __init__(self, message: str = "PDF pendiente de generación en Bejerman", *, retry_after_ms: int = 2500):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


@dataclass(frozen=True)
class BejermanPdfReference:
    type: str
    number: str
    letter: str
    point_of_sale: str
    issue_date: str
    customer_code: str = ""


def _setting(name: str, default: Any = "") -> Any:
    return getattr(settings, name, default)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_text(root: ET.Element, name: str) -> str:
    for elem in root.iter():
        if _local_name(elem.tag) == name:
            return elem.text or ""
    return ""


def parse_soap_response(xml_text: str) -> dict[str, str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise BejermanSdkUnavailable(f"Respuesta SOAP inválida: {exc}") from exc
    return {
        "Resultado": _xml_text(root, "Resultado"),
        "ErrorMsg": _xml_text(root, "ErrorMsg"),
        "Fault": _xml_text(root, "faultstring"),
        "DatosJSON": _xml_text(root, "DatosJSON"),
        "Token": _xml_text(root, "Token"),
        "EsLista": _xml_text(root, "EsLista"),
        "NombreTipoDatos": _xml_text(root, "NombreTipoDatos"),
    }


def is_ok_response(response: dict[str, Any]) -> bool:
    # `Resultado` manda: un ERROR con un texto que suena a éxito no alcanza para
    # dar el comprobante por emitido (lo fija
    # test_sdk_acepta_aviso_de_importacion_correcta_como_ok). Pero tampoco es un
    # rechazo limpio: ver `is_bejerman_imported_but_not_ok`, que es lo que manda
    # esos casos a revisión en vez de darlos por fallados.
    resultado = _clean(response.get("Resultado")).upper()
    error = _clean(response.get("ErrorMsg"))
    return resultado.startswith("OK") and (not error or is_bejerman_success_message(error))


# El SDK de Bejerman crea y borra vistas temporales (expCuotasV*, expSinCuotasV*)
# por cada consulta; dos llamadas concurrentes chocan entre sí y una falla con
# "Invalid object name ..." o "There is already an object named ...".
_SDK_RACE_MARKERS = (
    "expcuotas",
    "expsincuotas",
    "invalid object name",
    "already an object named",
)

# Operaciones de consulta sin efectos que es seguro reintentar cuando la falla
# es la carrera de vistas temporales. La generación de PDF se excluye a
# propósito: un timeout deja un resultado ambiguo y no debe duplicar la
# solicitud sobre Crystal.
_SDK_RETRYABLE_OPERATIONS = {
    "WSLISTARCOMPROBANTESJSON",
    "DETALLECOMPROBANTEJSON",
    "WS_CONSULTARCOMPROBANTEPDF",
    "OBTENERARTICULOS",
    "OBTENERCLIENTES",
    "OBTENERPROVEEDORES",
    "OBTENERSTOCKPARTIDA",
    "OBTENERSTOCKDEPOSITO",
    "OBTENERDEPOSITOS",
    "OBTENERPARTIDAS",
}


def is_bejerman_sdk_race_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _SDK_RACE_MARKERS)


# El SDK corre como servicio WCF bajo IIS. Cuando el pool se recicla, se queda
# sin memoria o el host no llega a activarse, IIS contesta un HTTP 500 con
# `System.ServiceModel.ServiceActivationException` (a veces un 502/503/504 del
# proxy) *antes* de instanciar el servicio: el pedido nunca se ejecutó. Son
# fallas de segundos y la única respuesta razonable es reintentar sola.
_TRANSPORT_TRANSIENT_MARKERS = (
    "serviceactivationexception",
    "servertoobusyexception",
    "serverbusyexception",
    "service unavailable",
    "servicio no disponible",
    "the service is unavailable",
    "bad gateway",
    "gateway timeout",
)

# 502/504 los devuelve un intermediario que nunca entregó el pedido; 503 es el
# propio IIS avisando que no puede atender. Ninguno llegó a la lógica del SDK.
_TRANSPORT_TRANSIENT_STATUSES = frozenset({502, 503, 504})


def is_bejerman_transport_transient_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _TRANSPORT_TRANSIENT_MARKERS)


def is_transient_http_failure(status_code: Any, body: Any = "") -> bool:
    """Si esa respuesta HTTP es un servicio caído y no un rechazo de Bejerman.

    Un 500 a secas puede ser un error de negocio devuelto como fault, así que
    sólo cuenta como transitorio cuando el cuerpo nombra una falla de
    activación o de saturación del host."""

    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return False
    if status in _TRANSPORT_TRANSIENT_STATUSES:
        return True
    return status >= 500 and is_bejerman_transport_transient_message(as_string(body)[:2000])


# La consulta interna de WSListarComprobantesJSON está rota en algunas
# instalaciones de Bejerman (columnas inexistentes / ORDER BY con UNION).
_SALES_QUERY_SCHEMA_MARKERS = ("IMPORTE_TOTAL", "ConvMonLocal", "CabVenta.", "ORDER BY items must appear")


def is_bejerman_sales_query_schema_message(value: Any) -> bool:
    text = as_string(value)
    return any(marker in text for marker in _SALES_QUERY_SCHEMA_MARKERS)


# Clasificadores compartidos de mensajes de error de Bejerman. Delivery los usa
# para diagnóstico y el worker de sincronización reutiliza los transitorios para
# decidir reintentos, backoff y reapertura automática.
_CUSTOMER_RESOLUTION_MARKERS = (
    "customer_not_found",
    "customer_vat_missing",
    "customer_document_type_missing",
    "customer_document_missing",
)


def is_bejerman_customer_resolution_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _CUSTOMER_RESOLUTION_MARKERS)


# Estos tres no son una demora de la consulta: son campos que faltan en la ficha del
# cliente en Bejerman. Distinguirlos importa porque el consejo es el opuesto —
# reintentar no los arregla nunca, hay que completar la ficha en el ERP.
_CUSTOMER_MASTER_DATA_FIELDS = (
    ("customer_vat_missing", "la situación de IVA"),
    ("customer_document_type_missing", "el tipo de documento"),
    ("customer_document_missing", "el número de documento (CUIT/DNI)"),
)


def bejerman_customer_missing_master_field(value: Any) -> str:
    """El campo que le falta a la ficha del cliente, o '' si el error es otro."""

    text = remove_accents(as_string(value)).lower()
    for marker, label in _CUSTOMER_MASTER_DATA_FIELDS:
        if marker in text:
            return label
    return ""


_PARTIDA_INEXISTENTE_MARKERS = ("partida inexistente",)


def is_bejerman_partida_inexistente_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _PARTIDA_INEXISTENTE_MARKERS)


_STOCK_INSUFICIENTE_MARKERS = ("no hay stock suficiente",)


def is_bejerman_stock_insuficiente_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _STOCK_INSUFICIENTE_MARKERS)


# Bejerman rechaza el comprobante porque ya lo tiene. Dicho de otro modo: el
# estado que queríamos alcanzar ya está. Tratarlo como fallo es lo que deja
# procesos reintentándose para siempre contra una pared que en realidad es la
# meta — y, peor, invita a emitir un duplicado "para destrabarlo".
#
# Aparece con dos textos distintos:
#   - "El comprobante no se importó debido a que ya existe en la empresa."
#     (comprobantes de venta con número manual: RIS/RDA en modo `register`)
#   - "El mismo ya existe en la empresa debido a una importación anterior."
#     (importador de stock: dedupe por `Comprobante_IdOrigen`)
_ALREADY_IMPORTED_MARKERS = (
    "ya existe en la empresa",
    "importacion anterior",
    "ya fue importado",
    "ya ha sido importado",
)


def is_bejerman_already_imported_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _ALREADY_IMPORTED_MARKERS)


# Rechazos que NEXORA levanta **armando** el comprobante, antes de que salga
# ningún pedido al SDK: falta la situación de IVA en la ficha del cliente, el
# código de vendedor no es válido, el artículo no está resuelto. Saber que son
# previos al envío importa para una sola cosa, pero central: con estos
# comprobantes Bejerman nunca se enteró de nada, así que el intento se puede
# cerrar sin miedo a estar tapando un remito emitido.
_LOCAL_PRECHECK_FAILURE_CODES = (
    "BEJERMAN_CUSTOMER_NOT_FOUND",
    "BEJERMAN_CUSTOMER_VAT_MISSING",
    "BEJERMAN_CUSTOMER_DOCUMENT_TYPE_MISSING",
    "BEJERMAN_CUSTOMER_DOCUMENT_MISSING",
    "BEJERMAN_OCCASIONAL_CUSTOMER_NAME_MISSING",
    "BEJERMAN_OCCASIONAL_CUSTOMER_DOCUMENT_MISSING",
    "INVALID_SELLER_CODE",
    "INVALID_SELLER_CODE_LENGTH",
    "SERVICE_INGRESS_ARTICLE_REQUIRED",
    "DELIVERY_ORDERS_REQUIRED",
    "INCOMPATIBLE_DELIVERY_REMITO_CURRENCY",
    "DELIVERY_REMITO_EXCHANGE_RATE_REQUIRED",
)


def is_bejerman_local_precheck_failure(value: Any) -> bool:
    text = as_string(value).upper()
    return any(code in text for code in _LOCAL_PRECHECK_FAILURE_CODES)


# Mismo defecto de bloqueo de tablas que la "carrera expCuotasV" (ver
# _SDK_RACE_MARKERS) pero con un texto de respuesta distinto: Bejerman
# devuelve esto cuando el importador no pudo tomar el lock de la tabla
# porque otra operación la estaba usando en simultáneo. Es transitorio.
_TABLE_UPDATE_TRANSIENT_MARKERS = (
    "no ha sido posible actualizar los datos en la tabla",
    "no se ha podido actualizar los datos en la tabla",
)


def is_bejerman_table_update_transient_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _TABLE_UPDATE_TRANSIENT_MARKERS)


_DUPLICATE_CANDIDATE_MARKERS = ("coincidentes para",)


def is_bejerman_duplicate_candidate_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    return any(marker in text for marker in _DUPLICATE_CANDIDATE_MARKERS)


def _sdk_race_retry_attempts() -> int:
    try:
        value = int(_setting("BEJERMAN_SDK_RACE_RETRY_ATTEMPTS", 2) or 2)
    except (TypeError, ValueError):
        return 2
    return value if value >= 0 else 2


def _sdk_race_retry_delay_ms() -> int:
    try:
        value = int(_setting("BEJERMAN_SDK_RACE_RETRY_DELAY_MS", 1200) or 1200)
    except (TypeError, ValueError):
        return 1200
    return value if value >= 0 else 1200


def _sdk_transport_retry_attempts() -> int:
    try:
        value = int(_setting("BEJERMAN_SDK_TRANSPORT_RETRY_ATTEMPTS", 2))
    except (TypeError, ValueError):
        return 2
    return value if value >= 0 else 2


def _sdk_transport_retry_delay_ms() -> int:
    try:
        value = int(_setting("BEJERMAN_SDK_TRANSPORT_RETRY_DELAY_MS", 700))
    except (TypeError, ValueError):
        return 700
    return value if value >= 0 else 700


def is_retryable_sdk_operation(operacion: Any) -> bool:
    return _clean(operacion).upper() in _SDK_RETRYABLE_OPERATIONS


def is_bejerman_success_message(value: Any) -> bool:
    text = remove_accents(as_string(value)).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return "comprobante se importo correctamente" in text


def is_bejerman_imported_but_not_ok(response: dict[str, Any]) -> bool:
    """Bejerman marcó la respuesta como error y en el mismo mensaje dice que el
    comprobante entró.

    Pasa con *"El comprobante se importó correctamente. La Fecha de
    Contabilización se modificó ya que debe ser posterior a la del último cierre
    del Libro IVA Ventas."*: la advertencia de la fecha ensucia el resultado
    aunque el alta se hizo. No alcanza para dar el comprobante por emitido —el
    número no viene— pero tratarlo como un fallo común es peor: el comprobante
    queda vivo en Bejerman, invisible para NEXORA, y el siguiente intento lo
    duplica. Es exactamente el caso que existe la revisión manual para atender
    (OS 28884, 04/08/2026)."""

    if is_ok_response(response):
        return False
    error = _clean(response.get("ErrorMsg")) or _clean(response.get("Resultado"))
    return bool(error) and is_bejerman_success_message(error)


def parse_json_maybe(raw: Any) -> Any:
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    text = _clean(raw)
    if not text:
        return None
    for _ in range(3):
        try:
            parsed = json.loads(text)
        except Exception:
            return text
        if isinstance(parsed, str):
            text = parsed.strip()
            continue
        return parsed
    return text


def as_record(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def records_from_value(value: Any) -> list[dict[str, Any]]:
    parsed = parse_json_maybe(value)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if not isinstance(parsed, dict):
        return []
    for key in ("DatosJSON", "items", "Items", "Comprobantes", "comprobantes", "Articulos", "articulos", "data"):
        records = records_from_value(parsed.get(key))
        if records:
            return records
    return [parsed]


def records_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    return records_from_value(response.get("DatosJSON"))


def first_record(value: Any) -> dict[str, Any] | None:
    parsed = parse_json_maybe(value)
    if isinstance(parsed, list):
        for item in parsed:
            found = first_record(item)
            if found:
                return found
        return None
    return parsed if isinstance(parsed, dict) else None


def first_value(record: dict[str, Any], keys: list[str] | tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    entries = list(record.items())
    for key in keys:
        lower = key.lower()
        for entry_key, value in entries:
            if entry_key.lower() == lower and value not in (None, ""):
                return value
    return None


def as_string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return as_string(value).lower() in {"1", "s", "si", "sí", "true", "yes", "y", "on"}


def as_nullable_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = as_string(value).strip().lower()
    if text in {"1", "s", "si", "sí", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "n", "no", "false", "f", "off"}:
        return False
    return None


def as_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def normalize_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = as_string(value)
    if not raw:
        return None
    match = re.match(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$", raw)
    if match:
        parsed = datetime.utcfromtimestamp(int(match.group(1)) / 1000)
        return parsed.date().isoformat()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.match(r"^(\d{4})(\d{2})(\d{2})$", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if match:
        return f"{match.group(3)}-{match.group(2).zfill(2)}-{match.group(1).zfill(2)}"
    return None


def format_pdf_generation_date(value: Any) -> str:
    raw = as_string(value)
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        return f"{match.group(3)}/{match.group(2)}/{match.group(1)}"
    return raw


def remove_accents(value: str) -> str:
    text = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_search(value: Any) -> str:
    return re.sub(r"\s+", " ", remove_accents(str(value or "").lower())).strip()


def flatten_text(value: Any, depth: int = 0) -> str:
    if value is None or depth > 5:
        return ""
    if not isinstance(value, (dict, list)):
        return as_string(value)
    if isinstance(value, list):
        return " ".join(flatten_text(item, depth + 1) for item in value)
    return " ".join(f"{key} {flatten_text(item, depth + 1)}" for key, item in value.items())


def normalize_seller_code(value: Any) -> str:
    return as_string(value).upper()


def document_number_from_parts(tipo: str, letter: str, point: str, number: str) -> str:
    parts = [tipo, letter, f"{point}-{number}" if point or number else ""]
    return " ".join(part for part in parts if part)


def format_remito_number(tipo: str, letter: str, point: str, number: str) -> str:
    if tipo and letter and point and number:
        return f"{tipo} {letter} {point}-{number}"
    if tipo and point and number:
        return f"{tipo} {point}-{number}"
    if tipo and number:
        return f"{tipo} {number}"
    return number or " ".join(part for part in (tipo, letter, point) if part) or "Remito Bejerman"


def _response_snippet(value: str, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


class BejermanSDKClient:
    def __init__(
        self,
        company_key: str | None = None,
        *,
        bejerman_company: str | None = None,
        actor_user_id: int | None = None,
        bejerman_username: str | None = None,
        bejerman_password: str | None = None,
        bejerman_workstation: str | None = None,
        allow_system_credentials: bool = False,
    ):
        self.wsdl_url = _clean(_setting("BEJERMAN_WSDL_URL"))
        self.endpoint_url = self.wsdl_url.split("?", 1)[0]
        self.timeout = int(_setting("BEJERMAN_REQUEST_TIMEOUT", 30) or 30)
        self.actor_user_id = actor_user_id
        self.bejerman_username = _clean(bejerman_username)
        self.bejerman_password = _clean(bejerman_password)
        self.bejerman_workstation = _clean(bejerman_workstation)
        self.allow_system_credentials = bool(allow_system_credentials)
        if bejerman_company:
            self.company_key = _clean(company_key)
            self.company = _clean(bejerman_company)
        elif company_key:
            company = require_company(company_key)
            self.company_key = company.key
            self.company = company.bejerman_company
        else:
            self.company_key = ""
            self.company = _clean(_setting("BEJERMAN_COMPANY"))
        self.token = ""
        self._http = requests.Session()

    def _credentials(self) -> tuple[str, str]:
        if self.bejerman_username and self.bejerman_password:
            return self.bejerman_username, self.bejerman_password
        if self.actor_user_id:
            try:
                return get_user_bejerman_credentials(int(self.actor_user_id))
            except BejermanUserCredentialsError as exc:
                if not self.allow_system_credentials:
                    raise BejermanSdkConfigError(str(exc)) from exc
        if self.allow_system_credentials:
            username = _clean(_setting("BEJERMAN_USER"))
            password = _clean(_setting("BEJERMAN_PASSWORD"))
            if username and password:
                return username, password
        raise BejermanSdkConfigError(BEJERMAN_CREDENTIALS_REQUIRED)

    def _workstation(self) -> str:
        if self.bejerman_workstation:
            return self.bejerman_workstation
        if self.actor_user_id:
            return resolve_user_bejerman_workstation(int(self.actor_user_id))
        return _clean(_setting("BEJERMAN_WORKSTATION")) or _clean(_setting("BEJERMAN_SERVICE_WORKSTATION"))

    def _post(self, action: str, body: str, *, retry_transport: bool = False) -> dict[str, str]:
        """Manda el sobre SOAP; con `retry_transport` repite si el servicio no estaba en pie.

        El reintento se pide explícitamente y sólo para operaciones sin efecto
        (ver `_SDK_RETRYABLE_OPERATIONS` y `register`): repetir una emisión a
        ciegas podría duplicar un comprobante.
        """

        if not self.endpoint_url:
            raise BejermanSdkConfigError("BEJERMAN_WSDL_URL requerido")
        envelope = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<s:Body>{body}</s:Body>"
            "</s:Envelope>"
        )
        retries = _sdk_transport_retry_attempts() if retry_transport else 0
        for attempt in range(retries + 1):
            try:
                return self._post_once(action, envelope)
            except BejermanSdkTransportUnavailable as exc:
                if attempt >= retries:
                    raise
                logger.info(
                    "bejerman_sdk_transport_retry",
                    extra={"bejerman_action": action, "attempt": attempt + 1, "error": str(exc)[:300]},
                )
                time.sleep(_sdk_transport_retry_delay_ms() * (attempt + 1) / 1000)
        raise BejermanSdkUnavailable("Bejerman no respondió al pedido SOAP")  # inalcanzable

    def _post_once(self, action: str, envelope: str) -> dict[str, str]:
        try:
            response = self._http.post(
                self.endpoint_url,
                data=envelope.encode("utf-8"),
                headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": action},
                timeout=self.timeout,
            )
        except requests.ConnectionError as exc:
            # No se llegó a mandar el cuerpo (host caído, conexión cortada al
            # abrir). Un timeout de lectura no entra acá a propósito: ahí el
            # pedido sí viajó y repetirlo alarga la espera del usuario.
            #
            # Un reset con el pedido en vuelo también llega como ConnectionError, y ese
            # NO se puede dar por no ejecutado: se degrada a la falla genérica para no
            # invitar a reintentar una emisión que quizá entró.
            if _connection_error_was_in_flight(exc):
                raise BejermanSdkUnavailable(f"Error HTTP Bejerman: {exc}") from exc
            raise BejermanSdkTransportUnavailable(f"Error HTTP Bejerman: {exc}") from exc
        except requests.RequestException as exc:
            raise BejermanSdkUnavailable(f"Error HTTP Bejerman: {exc}") from exc
        try:
            parsed = parse_soap_response(response.text)
        except BejermanSdkUnavailable as exc:
            if not response.ok:
                detail = _response_snippet(response.text) or response.reason or "sin detalle SOAP"
                message = f"Error HTTP Bejerman {response.status_code}: {detail}"
                if is_transient_http_failure(response.status_code, response.text):
                    raise BejermanSdkTransportUnavailable(message) from exc
                raise BejermanSdkUnavailable(message) from exc
            raise
        if not response.ok:
            error = (
                _clean(parsed.get("Fault"))
                or _clean(parsed.get("ErrorMsg"))
                or _clean(parsed.get("Resultado"))
                or response.reason
                or "sin detalle SOAP"
            )
            if is_transient_http_failure(response.status_code, f"{error} {response.text}"):
                raise BejermanSdkTransportUnavailable(f"Error HTTP Bejerman {response.status_code}: {error}")
            raise BejermanSdkResponseError(f"HTTP {response.status_code} Bejerman: {error}")
        if not is_ok_response(parsed):
            error = parsed.get("ErrorMsg") or parsed.get("Fault") or parsed.get("Resultado") or "Respuesta no OK de Bejerman"
            raise BejermanSdkResponseError(error)
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
            f"<xCodSucursal>{escape(_clean(_setting('BEJERMAN_BRANCH')))}</xCodSucursal>"
            "</EFlexSDK_WSRegistro>"
        )
        # El registro es la primera llamada de cada cliente y la que se come el
        # host recién reciclado. No toca datos —sólo pide un token—, así que
        # siempre se puede repetir.
        response = self._post(REGISTRO_ACTION, body, retry_transport=True)
        token = _clean(response.get("Token"))
        if not token:
            raise BejermanSdkResponseError("Bejerman no devolvió token")
        self.token = token
        return token

    def execute(self, circuito: str, operacion: str, *, params: list[Any] | None = None, params_json: Any = None) -> dict[str, str]:
        retries = _sdk_race_retry_attempts() if is_retryable_sdk_operation(operacion) else 0
        for attempt in range(retries + 1):
            try:
                return self._execute_once(circuito, operacion, params=params, params_json=params_json)
            except BejermanSdkResponseError as exc:
                if attempt >= retries or not is_bejerman_sdk_race_message(exc):
                    raise
                logger.info(
                    "bejerman_sdk_race_retry",
                    extra={"bejerman_operacion": operacion, "attempt": attempt + 1, "error": str(exc)[:300]},
                )
                time.sleep(_sdk_race_retry_delay_ms() / 1000)
        raise BejermanSdkUnavailable("Bejerman no respondió a la operación")  # inalcanzable

    def _execute_once(self, circuito: str, operacion: str, *, params: list[Any] | None = None, params_json: Any = None) -> dict[str, str]:
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
        return self._post(EJECUTAR_ACTION, body, retry_transport=is_retryable_sdk_operation(operacion))

    def list_comprobantes_ventas(self, filters: list[dict[str, Any]], en_mon_ext: str = "N") -> dict[str, str]:
        return self.execute("VENTAS", "WSListarComprobantesJSON", params_json=[_json_param(filters), en_mon_ext])

    def detalle_comprobante_ventas(self, comprobante_id: str | int) -> dict[str, str]:
        return self.execute("VENTAS", "DetalleComprobanteJSON", params_json=[str(comprobante_id)])

    def consultar_comprobante_ventas_pdf(self, reference: BejermanPdfReference) -> dict[str, str]:
        # Misma fecha dd/mm/aaaa que WS_GenerarComprobantePDF: las dos operaciones
        # filtran por el mismo comprobante y mandaban formatos distintos.
        return self.execute(
            "VENTAS",
            "WS_ConsultarComprobantePDF",
            params_json=[
                reference.type,
                reference.number,
                reference.letter,
                reference.point_of_sale,
                format_pdf_generation_date(reference.issue_date),
            ],
        )

    def generar_comprobante_ventas_pdf(self, reference: BejermanPdfReference) -> dict[str, str]:
        # No pasar por execute(): esa capa reintenta automáticamente algunas
        # operaciones ante carreras internas del SDK. Generar un PDF debe ser
        # una solicitud de a lo sumo una vez porque un timeout no confirma que
        # Bejerman haya cancelado el trabajo.
        return self._execute_once(
            "VENTAS",
            "WS_GenerarComprobantePDF",
            params_json=[
                reference.type,
                reference.number,
                reference.letter,
                reference.point_of_sale,
                format_pdf_generation_date(reference.issue_date),
            ],
        )

    def list_articulos(self, filters: list[dict[str, Any]] | None = None, limit: int = 20) -> dict[str, str]:
        # ObtenerArticulos ignores Bejerman-style filters in this installation.
        # Fetch the catalog and let build_articles_result apply code/detail search locally.
        return self.execute("TABLAS", "ObtenerArticulos", params_json=[])

    def list_clientes(self) -> dict[str, str]:
        return self.execute("TABLAS", "ObtenerClientes", params_json=[])

    def modificar_cliente(self, cliente: dict[str, Any]) -> dict[str, str]:
        """Modifica una ficha del maestro de clientes. Es read-modify-write: Bejerman espera la
        ficha completa (ver build_customer_toggle_payload), no un parche de los campos a cambiar."""
        return self.execute("TABLAS", "ModificarCliente", params_json=[_json_param(cliente)])

    def list_proveedores(self) -> dict[str, str]:
        return self.execute("TABLAS", "ObtenerProveedores", params_json=[])

    def list_comprobantes_compras(self, filters: list[dict[str, Any]], en_mon_ext: str = "N") -> dict[str, str]:
        return self.execute("COMPRAS", "WSListarComprobantesJSON", params_json=[_json_param(filters), en_mon_ext])

    def detalle_comprobante_compras(self, comprobante_id: str | int) -> dict[str, str]:
        return self.execute("COMPRAS", "DetalleComprobanteJSON", params_json=[str(comprobante_id)])

    def list_ordenes_compra(self, provider_code: str = "", en_mon_ext: str = "N") -> dict[str, str]:
        filters = [bejerman_filter("Comprobante_Tipo", "IGUAL", "OC")]
        if provider_code:
            filters.append(bejerman_filter("Proveedor_Codigo", "IGUAL", provider_code))
        return self.list_comprobantes_compras(filters, en_mon_ext)

    def _drop_cached_stock(self) -> None:
        """Marca como vencido el stock cacheado de la empresa.

        Se llama después de cualquier ingreso de comprobante, incluso si falló:
        un timeout no confirma que el movimiento no se haya registrado, así que
        lo seguro es asumir que el stock cambió. El import es diferido porque
        `bejerman_catalog_cache` importa de este módulo."""
        try:
            from . import bejerman_catalog_cache

            bejerman_catalog_cache.invalidate_stock(self.company_key or None)
        except Exception:  # nunca romper una emisión por el cache
            pass

    def ingresar_comprobante_ventas_json(
        self,
        comprobante: dict[str, Any],
        *,
        circuito: str = "VENTAS",
        operacion: str = "IngresarComprobanteJSON",
        numera_flex: str = "S",
        emite_reg: str = "E",
    ) -> dict[str, str]:
        try:
            return self.execute(circuito, operacion, params_json=[_json_param([comprobante]), numera_flex, emite_reg])
        finally:
            self._drop_cached_stock()

    def ingresar_lista_comprobantes_stock_json(
        self,
        comprobantes: list[dict[str, Any]],
        *,
        numera_flex: str = "N",
    ) -> dict[str, str]:
        try:
            return self.execute("STOCK", "IngresarListaComprobantesJSON", params_json=[_json_param(comprobantes), numera_flex])
        finally:
            self._drop_cached_stock()

    def ingresar_lista_comprobantes_compras_json(
        self,
        comprobantes: list[dict[str, Any]],
        *,
        numera_flex: str = "N",
        emite_reg: str = "R",
        maximo_valor_ajuste: float | None = None,
    ) -> dict[str, str]:
        params_json: list[Any] = [_json_param(comprobantes), numera_flex, emite_reg]
        if maximo_valor_ajuste is not None:
            params_json.append(maximo_valor_ajuste)
        try:
            return self.execute("COMPRAS", "IngresarListaComprobantesJSON", params_json=params_json)
        finally:
            self._drop_cached_stock()

    def obtener_stock_partida(self, partida: str) -> dict[str, str]:
        return self.execute("STOCK", "ObtenerStockPartida", params=[partida])

    def obtener_stock_deposito(self, deposit_code: str) -> dict[str, str]:
        return self.execute("STOCK", "ObtenerStockDeposito", params=[deposit_code])

    def obtener_depositos(self) -> dict[str, str]:
        return self.execute("STOCK", "ObtenerDepositos", params_json=[])

    def obtener_partidas(self) -> dict[str, str]:
        return self.execute("STOCK", "ObtenerPartidas", params_json=[])


def validate_sdk_config() -> None:
    missing = [
        name
        for name in ("BEJERMAN_WSDL_URL", "BEJERMAN_WORKSTATION")
        if not _clean(_setting(name))
    ]
    if missing:
        raise BejermanSdkConfigError("Faltan variables: " + ", ".join(missing))


def bejerman_filter(campo: str, accion: str, valor: str) -> dict[str, Any]:
    return {"Campo": campo, "Accion": accion, "Valor": valor, "Operacion": "Y", "Enlazada": False}


def build_sales_filters(
    customer_code: str | None,
    date_from: str | None = None,
    date_to: str | None = None,
    comprobante_type: str | None = None,
    operation_type: str | None = None,
) -> list[dict[str, Any]]:
    code = "" if customer_code is None else str(customer_code)
    filters = []
    if code.strip():
        filters.append(bejerman_filter("Cliente_Codigo", "IGUAL", code))
    tipo = _clean(comprobante_type).upper()
    if tipo:
        filters.append(bejerman_filter("Comprobante_Tipo", "IGUAL", tipo))
    operation = _clean(operation_type).upper()
    if operation:
        filters.append(bejerman_filter("Comprobante_TipoOperacion", "IGUAL", operation))
    if date_from:
        filters.append(bejerman_filter("Comprobante_FechaEmision", "MAYOR O IGUAL", date_from))
    if date_to:
        filters.append(bejerman_filter("Comprobante_FechaEmision", "MENOR O IGUAL", date_to))
    if not filters:
        raise ValueError("FILTROS_VENTAS_REQUERIDOS")
    return filters


def build_article_filters(search: str | None = None, field: str | None = None) -> list[dict[str, Any]]:
    normalized = _clean(search)
    if not normalized:
        return []
    code_like = re.match(r"^[A-Za-z0-9._/-]+$", normalized) is not None
    if field == "code":
        campo = "Articulo_Codigo"
    elif field == "description":
        campo = "Articulo_Descripcion"
    else:
        campo = "Articulo_Codigo" if code_like else "Articulo_Descripcion"
    return [bejerman_filter(campo, "CONTIENE", normalized)]


def build_article_code_from_tablas(record: dict[str, Any]) -> str:
    generic = as_string(first_value(record, ("Art_CodGenerico", "ArtCodGenerico", "CodigoGenerico")))
    if not generic:
        return ""
    elements = [
        as_string(first_value(record, ("Art_CodElemento1", "ArtCodElemento1", "CodigoElemento1"))),
        as_string(first_value(record, ("Art_CodElemento2", "ArtCodElemento2", "CodigoElemento2"))),
        as_string(first_value(record, ("Art_CodElemento3", "ArtCodElemento3", "CodigoElemento3"))),
    ]
    return "-".join([generic, *[item for item in elements if item]])


def map_article(record: dict[str, Any]) -> dict[str, Any] | None:
    code = as_string(first_value(record, ("Articulo_Codigo", "ArticuloCodigo", "CodigoArticulo", "Codigo", "Art_Codigo", "CodArticulo"))) or build_article_code_from_tablas(record)
    name = as_string(first_value(record, ("Articulo_Descripcion", "ArticuloDescripcion", "Descripcion", "Nombre", "Detalle", "Art_Descripcion", "Art_DescripcionGeneral", "ArtDescripcionGeneral")))
    description = as_string(first_value(record, ("Articulo_DescripcionAdicional", "DescripcionAdicional", "Observaciones", "Detalle", "Art_DescricpionAdicional", "Art_DescripcionAdicional"))) or name
    if not code and not name:
        return None
    public_description = " ".join(part for part in (description, f"Código {code}" if code and name else "") if part).strip()
    stock_by_partida = as_nullable_bool(first_value(record, ("Art_StockPorPartida", "stock_by_partida", "StockPorPartida")))
    sales_vat_type = as_string(first_value(record, ("Art_TipoTasaVentas", "TipoTasaVentas", "salesVatType", "vatType")))
    sales_vat_code = as_string(first_value(record, ("Art_CodTasaIVAVentas", "CodTasaIVAVentas", "salesVatCode", "vatCode")))
    sales_vat_rate = _delivery_vat_rate_for_code(sales_vat_code)
    return {
        "id": code or name,
        "code": code,
        "name": name or code,
        "description": public_description or name or code,
        "stockByPartida": stock_by_partida,
        "requiresPartida": stock_by_partida,
        "salesVatType": sales_vat_type,
        "salesVatCode": sales_vat_code,
        "salesVatRate": sales_vat_rate,
        "raw": record,
    }


def map_article_items(responses: list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    """Mapea y deduplica el volcado crudo de ObtenerArticulos.

    Separado de `build_articles_result` para poder cachear el catálogo mapeado
    (ver `bejerman_catalog_cache`) y aplicar después el filtro de búsqueda sobre
    esa lista, en vez de re-mapear el catálogo entero en cada tecla."""
    response_list = responses if isinstance(responses, list) else [responses]
    mapped = [article for response in response_list for article in (map_article(record) for record in records_from_response(response)) if article]
    unique: dict[str, dict[str, Any]] = {}
    for item in mapped:
        key = normalize_search(item.get("code") or item.get("name") or item.get("id"))
        unique.setdefault(key, item)
    return list(unique.values())


def article_search_blob(item: dict[str, Any]) -> str:
    return normalize_search(f"{item.get('code')} {item.get('name')} {item.get('description')}")


def filter_article_items(
    items: list[dict[str, Any]],
    query: dict[str, Any] | None = None,
    blobs: list[str] | None = None,
) -> dict[str, Any]:
    """Aplica búsqueda y límite sobre un catálogo ya mapeado.

    `blobs` es el texto normalizado de cada ítem precalculado en el mismo orden;
    si viene, evita normalizar el catálogo completo en cada búsqueda."""
    query = query or {}
    search = normalize_search(query.get("search"))
    if search:
        if blobs is None or len(blobs) != len(items):
            blobs = [article_search_blob(item) for item in items]
        items = [item for item, blob in zip(items, blobs) if search in blob]
    try:
        limit = max(1, min(50, int(query.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    return {"items": items[:limit], "pagination": create_pagination(len(items), 1, limit)}


def build_articles_result(responses: list[dict[str, Any]] | dict[str, Any], query: dict[str, Any] | None = None) -> dict[str, Any]:
    return filter_article_items(map_article_items(responses), query)


def normalize_article_code(value: Any) -> str:
    return re.sub(r"[\s-]+", "", as_string(value)).upper()


def _normalize_vat_code(value: Any) -> str:
    code = re.sub(r"[^A-Z0-9]", "", as_string(value).upper())
    if code.isdigit() and len(code) == 1:
        return code.zfill(2)
    return code


def _delivery_vat_rate_code_map() -> dict[str, float]:
    raw = as_string(_setting("BEJERMAN_REMITO_VAT_RATE_CODES", "01:21,03:10.5,04:0"))
    mapping: dict[str, float] = {}
    for chunk in re.split(r"[;,]", raw):
        if not chunk.strip() or not re.search(r"[:=]", chunk):
            continue
        code, value = re.split(r"[:=]", chunk, maxsplit=1)
        rate = as_number(value)
        normalized_code = _normalize_vat_code(code)
        if normalized_code and rate is not None:
            mapping[normalized_code] = rate
    return mapping


def _delivery_vat_rate_for_code(value: Any) -> float | None:
    code = _normalize_vat_code(value)
    if not code:
        return None
    return _delivery_vat_rate_code_map().get(code)


def _article_lookup_key(value: Any) -> str:
    return normalize_article_code(value) or normalize_search(value)


def build_article_sales_vat_map(
    responses: list[dict[str, Any]] | dict[str, Any],
    article_codes: list[str] | set[str] | tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    wanted = {_article_lookup_key(code) for code in article_codes if _article_lookup_key(code)}
    if not wanted:
        return {}
    response_list = responses if isinstance(responses, list) else [responses]
    result: dict[str, dict[str, Any]] = {}
    for response in response_list:
        for record in records_from_response(response):
            article = map_article(record)
            if not article:
                continue
            key = _article_lookup_key(article.get("code"))
            if key in wanted and key not in result:
                result[key] = {
                    "salesVatType": article.get("salesVatType"),
                    "salesVatCode": article.get("salesVatCode"),
                    "salesVatRate": article.get("salesVatRate"),
                    "raw": article.get("raw"),
                }
    return result


def build_article_description_map(
    responses: list[dict[str, Any]] | dict[str, Any],
    article_codes: list[str] | set[str] | tuple[str, ...],
) -> dict[str, str]:
    """Descripción que Bejerman tiene registrada para cada código de artículo.

    El nexo entre NEXORA y Bejerman es el **número de artículo**: la descripción que
    se imprime en el remito sale siempre del maestro de Bejerman, no del texto que
    quedó guardado en NEXORA cuando se armó el pedido. Ese texto es una foto vieja del
    catálogo y se desincroniza (un artículo renombrado, o —peor— el mismo código con
    otro artículo en la otra empresa: en MGBIO el 1206087 salió impreso como "Cánula
    nasal adulto BESMED" cuando en su maestro es una cánula de traqueotomía).

    Se usa `name` (`Articulo_Descripcion`) y no `description`, que es el texto de la
    interfaz y le agrega el código al final.
    """

    wanted = {_article_lookup_key(code) for code in article_codes if _article_lookup_key(code)}
    if not wanted:
        return {}
    response_list = responses if isinstance(responses, list) else [responses]
    result: dict[str, str] = {}
    for response in response_list:
        for record in records_from_response(response):
            article = map_article(record)
            if not article:
                continue
            key = _article_lookup_key(article.get("code"))
            description = as_string(article.get("name"))
            # `map_article` cae al código cuando el artículo no tiene descripción:
            # eso no es una descripción, así que se deja pasar el texto local.
            if key in wanted and description and key not in result and _article_lookup_key(description) != key:
                result[key] = description
    return result


def build_article_code_from_stock(record: dict[str, Any]) -> str:
    code = as_string(
        first_value(
            record,
            (
                "Articulo_Codigo",
                "ArticuloCodigo",
                "CodigoArticulo",
                "Codigo",
                "Art_Codigo",
                "CodArticulo",
                "article_code",
            ),
        )
    )
    if code:
        return code
    parts = [
        as_string(first_value(record, ("Art_CodGen", "ArtCodGen", "stkart_CodGen", "Comprobante_Art_CodGen"))),
        as_string(first_value(record, ("Art_CodEle1", "ArtCodEle1", "stkart_CodEle1"))),
        as_string(first_value(record, ("Art_CodEle2", "ArtCodEle2", "stkart_CodEle2"))),
        as_string(first_value(record, ("Art_CodEle3", "ArtCodEle3", "stkart_CodEle3"))),
    ]
    return "-".join(part for part in parts if part)


def _stock_quantity(record: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    return as_number(first_value(record, fields))


def map_stock_row(record: dict[str, Any]) -> dict[str, Any] | None:
    article_code = build_article_code_from_stock(record)
    deposit_code = as_string(
        first_value(
            record,
            (
                "Art_CodDeposito",
                "ArtCodDeposito",
                "Comprobante_ArtDeposito",
                "Deposito",
                "deposito",
                "stkdep_Cod",
                "Item_Deposito",
            ),
        )
    )
    partida = as_string(
        first_value(
            record,
            (
                "Art_Partida",
                "ArtPartida",
                "Comprobante_ArtPartida",
                "Partida",
                "partida",
                "stp_Partida",
                "Item_Partida",
            ),
        )
    )
    if not article_code and not deposit_code and not partida:
        return None
    return {
        "articleCode": article_code,
        "depositCode": deposit_code,
        "partida": partida,
        "realQuantity": _stock_quantity(record, ("Art_RealUM1", "ArtRealUM1", "RealUM1", "realQuantity", "Stock")),
        "committedQuantity": _stock_quantity(record, ("Art_CompUM1", "ArtCompUM1", "CompUM1", "committedQuantity")),
        "availableQuantity": _stock_quantity(record, ("Art_DispUM1", "ArtDispUM1", "DispUM1", "availableQuantity")),
        "raw": record,
    }


def build_stock_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in (map_stock_row(record) for record in records_from_response(response)) if row]


def map_partida_row(record: dict[str, Any]) -> dict[str, Any] | None:
    partida = as_string(
        first_value(
            record,
            (
                "Partida_Codigo",
                "PartidaCodigo",
                "Partida",
                "partida",
                "Art_Partida",
            ),
        )
    )
    deposit_code = as_string(
        first_value(
            record,
            (
                "Deposito_Codigo",
                "DepositoCodigo",
                "Deposito",
                "deposito",
                "Art_CodDeposito",
            ),
        )
    )
    if not partida and not deposit_code:
        return None
    return {
        "partida": partida,
        "depositCode": deposit_code,
        "expirationDate": normalize_date(
            first_value(
                record,
                (
                    "Partida_FechaVtoIngreso",
                    "PartidaFechaVtoIngreso",
                    "FechaVtoIngreso",
                    "FechaVencimientoIngreso",
                    "FechaVencimiento",
                    "FechaVto",
                    "Partida_FechaVencimiento",
                    "Partida_FechaVto",
                    "expirationDate",
                ),
            )
        ),
        "raw": record,
    }


def build_partida_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in (map_partida_row(record) for record in records_from_response(response)) if row]


def _partida_key(deposit_code: Any, partida: Any) -> str:
    return f"{as_string(deposit_code).upper()}|{as_string(partida).upper()}"


def _partida_only_key(partida: Any) -> str:
    return as_string(partida).upper()


def _sortable_expiration(value: Any) -> str:
    return normalize_date(value) or "9999-12-31"


class PartidaIndex:
    """Partidas indexadas por (depósito, partida) y por partida sola.

    `ObtenerPartidas` devuelve la tabla entera (~40k filas para SEPID) y armar
    estos dos dicts cuesta unos 35 ms. Hacerlo dentro de
    `build_article_stock_lots` lo repetía una vez por artículo consultado, así
    que un listado de equipos de alquiler pagaba el reindexado decenas de veces.
    Se arma una vez y se reusa para todos los artículos del mismo request.
    """

    __slots__ = ("by_deposit_partida", "by_partida")

    def __init__(self, partida_rows: list[dict[str, Any]]):
        by_deposit_partida: dict[str, dict[str, Any]] = {}
        by_partida: dict[str, dict[str, Any]] = {}
        for row in partida_rows:
            by_deposit_partida[_partida_key(row.get("depositCode"), row.get("partida"))] = row
            key = _partida_only_key(row.get("partida"))
            current = by_partida.get(key)
            if not current or (not current.get("expirationDate") and row.get("expirationDate")):
                by_partida[key] = row
        self.by_deposit_partida = by_deposit_partida
        self.by_partida = by_partida

    def lookup(self, deposit_code: Any, partida: Any) -> dict[str, Any] | None:
        """Partida del depósito pedido, o la misma partida en cualquier otro."""
        return self.by_deposit_partida.get(_partida_key(deposit_code, partida)) or self.by_partida.get(
            _partida_only_key(partida)
        )


def as_partida_index(partidas: list[dict[str, Any]] | PartidaIndex) -> PartidaIndex:
    """Acepta el volcado crudo o un índice ya armado."""
    return partidas if isinstance(partidas, PartidaIndex) else PartidaIndex(partidas)


def build_article_stock_lots(
    stock_rows: list[dict[str, Any]],
    partida_rows: list[dict[str, Any]] | PartidaIndex,
    article_code: Any,
    deposit_code: Any,
) -> list[dict[str, Any]]:
    normalized_article_code = normalize_article_code(article_code)
    normalized_deposit_code = as_string(deposit_code).upper()
    partidas = as_partida_index(partida_rows)

    lots: list[dict[str, Any]] = []
    for row in stock_rows:
        if normalize_article_code(row.get("articleCode")) != normalized_article_code:
            continue
        if as_string(row.get("depositCode")).upper() != normalized_deposit_code:
            continue
        partida = as_string(row.get("partida"))
        if not partida:
            continue
        available = row.get("availableQuantity")
        real = row.get("realQuantity")
        effective_stock = available if available is not None else real
        if effective_stock is None or effective_stock <= 0:
            continue
        partida_row = partidas.lookup(row.get("depositCode"), partida)
        lots.append(
            {
                "articleCode": row.get("articleCode") or as_string(article_code),
                "depositCode": row.get("depositCode") or as_string(deposit_code),
                "partida": partida,
                "expirationDate": (partida_row or {}).get("expirationDate"),
                "realQuantity": real,
                "committedQuantity": row.get("committedQuantity"),
                "availableQuantity": available,
            }
        )
    lots.sort(key=lambda item: (_sortable_expiration(item.get("expirationDate")), as_string(item.get("partida")).lower()))
    return lots


def build_document_id(payload: dict[str, Any]) -> str:
    raw = _json_param(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_document_id(document_id: str) -> dict[str, Any]:
    raw = _clean(document_id).replace("-", "+").replace("_", "/")
    raw = raw + "=" * ((4 - len(raw) % 4) % 4)
    parsed = parse_json_maybe(base64.b64decode(raw).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("DOCUMENT_ID_INVALIDO")
    return {
        "t": as_string(parsed.get("t")),
        "n": as_string(parsed.get("n")),
        "l": as_string(parsed.get("l")),
        "p": as_string(parsed.get("p")),
        "f": normalize_date(parsed.get("f")),
        "c": "" if parsed.get("c") is None else str(parsed.get("c")),
    }


def operation_code(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Comprobante_TipoOperacion", "TipoOperacion", "Tipo_Operacion"))).upper()


def operation_label(record: dict[str, Any]) -> str:
    code = operation_code(record)
    return TIPO_OPERACION_LABELS.get(code, "Sin tipo de operación")


def is_billing_record(record: dict[str, Any]) -> bool:
    tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))).upper()
    return tipo in BILLING_COMPROBANTE_TYPES


def is_remito_record(record: dict[str, Any]) -> bool:
    tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))).upper()
    return tipo in REMITO_COMPROBANTE_TYPES


def map_facturacion_item(record: dict[str, Any], fallback_customer_code: str) -> dict[str, Any]:
    tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo")))
    letter = as_string(first_value(record, ("Comprobante_Letra", "Letra")))
    point = as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
    number = as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante")))
    raw_customer_code = first_value(record, ("Cliente_Codigo", "ClienteCodigo", "CodCliente"))
    preserved_customer_code = "" if raw_customer_code is None else str(raw_customer_code)
    customer_code = preserved_customer_code if preserved_customer_code.strip() else fallback_customer_code
    display_customer_code = as_string(customer_code)
    issue_date = normalize_date(first_value(record, ("Comprobante_FechaEmision", "Comprobante_FEmision", "FechaEmision")))
    due_date = normalize_date(first_value(record, ("Comprobante_FechaVencimiento", "FechaVencimiento", "Vencimiento")))
    document_number = as_string(first_value(record, ("Comprobante_NumeroCompleto", "NumeroCompleto", "Documento"))) or document_number_from_parts(tipo, letter, point, number)
    document_id = build_document_id({"t": tipo, "n": number, "l": letter, "p": point, "f": issue_date, "c": customer_code})
    return {
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
        "dueDate": due_date,
        "customerCode": display_customer_code,
        "bejermanCustomerCode": customer_code,
        "customerName": as_string(first_value(record, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial"))),
        "origin": operation_label(record),
        "subtotal": as_number(first_value(record, ("Comprobante_ImporteNeto", "Comprobante_Subtotal", "Subtotal"))),
        "taxes": as_number(first_value(record, ("Comprobante_ImporteIVA", "Comprobante_Impuestos", "Impuestos", "IVA"))),
        "totalAmount": as_number(first_value(record, ("Comprobante_ImporteTotal", "Comprobante_Total", "ImporteTotal", "Total"))),
        "amount": as_number(first_value(record, ("Comprobante_ImporteTotal", "Comprobante_Total", "ImporteTotal", "Total"))),
        "balance": as_number(first_value(record, ("Comprobante_Saldo", "Saldo", "ImportePendiente"))),
        "raw": record,
    }


def map_remito_item(record: dict[str, Any], fallback_customer_code: str) -> dict[str, Any]:
    tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))).upper()
    letter = as_string(first_value(record, ("Comprobante_Letra", "Letra")))
    point = as_string(first_value(record, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
    number = as_string(first_value(record, ("Comprobante_Numero", "Numero", "NroComprobante")))
    raw_customer_code = first_value(record, ("Cliente_Codigo", "ClienteCodigo", "CodCliente"))
    preserved_customer_code = "" if raw_customer_code is None else str(raw_customer_code)
    customer_code = preserved_customer_code if preserved_customer_code.strip() else fallback_customer_code
    display_customer_code = as_string(customer_code)
    issue_date = normalize_date(first_value(record, ("Comprobante_FechaEmision", "Comprobante_FEmision", "FechaEmision")))
    due_date = normalize_date(first_value(record, ("Comprobante_FechaVencimiento", "FechaVencimiento", "Vencimiento")))
    document_number = (
        as_string(first_value(record, ("Comprobante_NumeroCompleto", "NumeroCompleto", "Documento")))
        or format_remito_number(tipo, letter, point, number)
    )
    document_id = build_document_id({"t": tipo, "n": number, "l": letter, "p": point, "f": issue_date, "c": customer_code})
    op_code = operation_code(record)
    op_label = operation_label(record)
    return {
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
        "dueDate": due_date,
        "customerCode": display_customer_code,
        "bejermanCustomerCode": customer_code,
        "customerName": as_string(first_value(record, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial"))),
        "operationCode": op_code,
        "tipoOperacion": op_code,
        "operationLabel": op_label,
        "origin": op_label,
        "subtotal": as_number(first_value(record, ("Comprobante_ImporteNeto", "Comprobante_Subtotal", "Subtotal"))),
        "taxes": as_number(first_value(record, ("Comprobante_ImporteIVA", "Comprobante_Impuestos", "Impuestos", "IVA"))),
        "totalAmount": as_number(first_value(record, ("Comprobante_ImporteTotal", "Comprobante_Total", "ImporteTotal", "Total"))),
        "amount": as_number(first_value(record, ("Comprobante_ImporteTotal", "Comprobante_Total", "ImporteTotal", "Total"))),
        "raw": record,
    }


def create_pagination(total: int, page: int, page_size: int) -> dict[str, Any]:
    total_pages = max(1, (total + page_size - 1) // page_size)
    safe_page = min(max(1, page), total_pages)
    return {
        "page": safe_page,
        "pageSize": page_size,
        "total": total,
        "totalItems": total,
        "totalPages": total_pages,
        "offset": (safe_page - 1) * page_size,
        "hasNextPage": safe_page < total_pages,
        "hasPreviousPage": safe_page > 1,
    }


def _document_natural_key(value: Any):
    text = as_string(value).strip().casefold()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", text)
        if part != ""
    )


def _document_sort_value(item: dict[str, Any], field: str):
    accessors = {
        "fecha": lambda row: row.get("issueDate") or row.get("date"),
        "cliente": lambda row: row.get("customerName") or row.get("cliente") or row.get("razonSocial"),
        "comprobante": lambda row: row.get("type") or row.get("comprobanteTipo"),
        "tipo": lambda row: row.get("type") or row.get("comprobanteTipo"),
        "numero": lambda row: row.get("documentNumber") or row.get("numero") or row.get("number"),
        "total": lambda row: row.get("totalAmount") if row.get("totalAmount") is not None else row.get("amount"),
        "codigo": lambda row: row.get("bejermanCustomerCode") or row.get("customerCode"),
        "operacion": lambda row: row.get("operationLabel") or row.get("operationCode") or row.get("origin"),
    }
    value = accessors[field](item)
    if field == "total":
        return as_number(value)
    return _document_natural_key(value)


def sort_document_items(
    items: list[dict[str, Any]],
    sort_raw: Any,
    allowed_fields: set[str],
) -> list[dict[str, Any]]:
    raw = as_string(sort_raw).strip()
    descending = raw.startswith("-")
    field = raw[1:] if descending else raw
    if field not in allowed_fields:
        return list(items)
    non_empty = []
    empty = []
    for item in items:
        raw_value = _document_sort_value(item, field)
        is_empty = raw_value is None or raw_value == () or raw_value == ""
        (empty if is_empty else non_empty).append(item)
    non_empty.sort(key=lambda row: _document_natural_key(row.get("documentId") or row.get("id")), reverse=descending)
    non_empty.sort(key=lambda row: _document_sort_value(row, field), reverse=descending)
    empty.sort(key=lambda row: _document_natural_key(row.get("documentId") or row.get("id")), reverse=descending)
    return non_empty + empty


def build_facturacion_result(
    response: dict[str, Any] | list[dict[str, Any]],
    customer_code: str,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = query or {}
    response_list = response if isinstance(response, list) else [response]
    mapped = [
        map_facturacion_item(record, customer_code)
        for item in response_list
        for record in records_from_response(item)
        if is_billing_record(record)
    ]
    unique: dict[str, dict[str, Any]] = {}
    for item in mapped:
        unique.setdefault(item["documentId"], item)
    items = list(unique.values())
    origin = as_string(query.get("origin"))
    search = normalize_search(query.get("search"))
    if origin:
        origin_type = origin.upper()
        if origin_type in BILLING_COMPROBANTE_TYPES:
            items = [item for item in items if as_string(item.get("type")).upper() == origin_type]
        else:
            items = [item for item in items if item.get("origin") == origin]
    if search:
        items = [item for item in items if search in normalize_search(flatten_text(item))]
    sort_raw = query.get("sort")
    if as_string(sort_raw).lstrip("-") in {"fecha", "cliente", "comprobante", "numero", "total"}:
        items = sort_document_items(
            items,
            sort_raw,
            {"fecha", "cliente", "comprobante", "numero", "total"},
        )
    else:
        items.sort(key=lambda item: (item.get("issueDate") or "", item.get("documentNumber") or ""), reverse=True)
    try:
        page = int(query.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(query.get("pageSize") or 25)
    except (TypeError, ValueError):
        page_size = 25
    page_size = max(1, min(100, page_size))
    pagination = create_pagination(len(items), page, page_size)
    offset = pagination["offset"]
    return {"items": items[offset : offset + pagination["pageSize"]], "pagination": pagination}


def build_remitos_result(
    response: dict[str, Any] | list[dict[str, Any]],
    customer_code: str,
    query: dict[str, Any] | None = None,
    extra_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    query = query or {}
    response_list = response if isinstance(response, list) else [response]
    mapped = [
        map_remito_item(record, customer_code)
        for item in response_list
        for record in records_from_response(item)
        if is_remito_record(record)
    ]
    unique: dict[str, dict[str, Any]] = {}
    for item in mapped:
        unique.setdefault(item["documentId"], item)
    for item in extra_items or []:
        document_id = item.get("documentId")
        if not document_id:
            continue
        if document_id not in unique:
            unique[document_id] = item
            continue
        for key in ("companyKey", "remitoGroupId", "pdfUrl", "printUrl", "source"):
            if item.get(key):
                unique[document_id][key] = item[key]
    items = list(unique.values())
    remito_type = as_string(query.get("remitoType") or query.get("type")).upper()
    operation_type = as_string(query.get("operationType") or query.get("operation")).upper()
    search = normalize_search(query.get("search"))
    if remito_type:
        items = [item for item in items if as_string(item.get("type")).upper() == remito_type]
    if operation_type:
        items = [item for item in items if as_string(item.get("operationCode")).upper() == operation_type]
    if search:
        items = [item for item in items if search in normalize_search(flatten_text(item))]
    sort_raw = query.get("sort")
    if as_string(sort_raw).lstrip("-") in {"fecha", "cliente", "codigo", "tipo", "operacion", "numero", "total"}:
        items = sort_document_items(
            items,
            sort_raw,
            {"fecha", "cliente", "codigo", "tipo", "operacion", "numero", "total"},
        )
    else:
        items.sort(key=lambda item: (item.get("issueDate") or "", item.get("documentNumber") or ""), reverse=True)
    try:
        page = int(query.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(query.get("pageSize") or 25)
    except (TypeError, ValueError):
        page_size = 25
    page_size = max(1, min(100, page_size))
    pagination = create_pagination(len(items), page, page_size)
    offset = pagination["offset"]
    return {"items": items[offset : offset + pagination["pageSize"]], "pagination": pagination}


def collect_pdf_candidates(value: Any, output: list[str] | None = None) -> list[str]:
    output = output or []
    if value is None:
        return output
    if isinstance(value, str):
        output.append(value)
        return output
    if isinstance(value, list):
        for item in value:
            collect_pdf_candidates(item, output)
        return output
    if isinstance(value, dict):
        likely = ("pdf", "archivo", "contenido", "base64", "datos", "data", "bytes")
        for key, item in value.items():
            if isinstance(item, str) and any(marker in key.lower() for marker in likely):
                output.insert(0, item)
            else:
                collect_pdf_candidates(item, output)
    return output


def _normalized_pdf_bytes(data: bytes) -> bytes | None:
    if not data:
        return None
    marker = b"%PDF-"
    if data.startswith(marker):
        return data
    offset = data.find(marker, 0, 16)
    if offset < 0:
        return None
    prefix = data[:offset]
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    if prefix.strip():
        return None
    return data[offset:]


def extract_pdf_bytes(response: dict[str, Any]) -> bytes | None:
    parsed = parse_json_maybe(response.get("DatosJSON"))
    candidates = collect_pdf_candidates(parsed)
    if isinstance(parsed, str):
        candidates.insert(0, parsed)
    for candidate in candidates:
        normalized = re.sub(r"\s+", "", re.sub(r"^data:application/pdf;base64,", "", candidate, flags=re.I))
        if not normalized:
            continue
        try:
            data = base64.b64decode(normalized)
        except Exception:
            continue
        pdf = _normalized_pdf_bytes(data)
        if pdf:
            return pdf
    return None


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(_setting(name, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@contextmanager
def _temporary_request_timeout(
    client: BejermanSDKClient,
    timeout_seconds: int | tuple[int, int] | None,
):
    original = getattr(client, "timeout", None)
    changed = False
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, tuple) and len(timeout_seconds) == 2:
            try:
                connect_timeout = int(timeout_seconds[0])
                read_timeout = int(timeout_seconds[1])
            except (TypeError, ValueError):
                connect_timeout = read_timeout = 0
            if connect_timeout > 0 and read_timeout > 0 and hasattr(client, "timeout"):
                client.timeout = (connect_timeout, read_timeout)
                changed = True
        else:
            try:
                timeout = int(timeout_seconds)
            except (TypeError, ValueError):
                timeout = 0
            if timeout > 0 and hasattr(client, "timeout"):
                client.timeout = timeout
                changed = True
    try:
        yield
    finally:
        if changed:
            client.timeout = original


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _pdf_folder_kind(reference: BejermanPdfReference) -> str:
    tipo = as_string(reference.type).upper()
    if tipo in REMITO_COMPROBANTE_TYPES:
        return "REMITOS"
    if tipo in BILLING_COMPROBANTE_TYPES:
        return "FACTURAS"
    return ""


def _pdf_company_key(client: BejermanSDKClient) -> str:
    marker = as_string(getattr(client, "company_key", "")) or as_string(getattr(client, "company", ""))
    company = company_for_key(marker, default="SEPID")
    if company:
        return company.key
    return marker.upper() or "SEPID"


def _configured_pdf_output_dir(client: BejermanSDKClient, reference: BejermanPdfReference) -> str:
    folder = _pdf_folder_kind(reference)
    if not folder:
        return ""
    company_key = _pdf_company_key(client)
    return configured_pdf_output_dir(company_key, folder, env_lookup=lambda name: _setting(name, ""))


def _safe_pdf_filename_part(value: Any, fallback: str = "") -> str:
    text = as_string(value).upper()
    if not text:
        return fallback
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip(". ")
    return text or fallback


def _numeric_pdf_filename_part(value: Any, width: int) -> str:
    text = _safe_pdf_filename_part(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(width) if digits else text


def _erp_customer_filename_part(value: Any) -> str:
    """Código de cliente como lo escribe el ERP: 6 caracteres, justificado a la derecha.

    Ej.: `NOV` -> `   NOV`, `BIOMED` -> `BIOMED`. Los espacios quedan en el medio del
    nombre (nunca al final), así que son válidos en Windows.
    """

    text = as_string(value).upper()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip(". ")
    return text[:6].rjust(6) if text else ""


def _comprobante_pdf_filename(reference: BejermanPdfReference) -> str:
    """Nombre con el que NEXORA guarda su copia.

    Usa la misma convención que el ERP para que no convivan dos formas distintas de
    escribir el mismo comprobante. La búsqueda sigue contemplando las tres
    convenciones (ver `_comprobante_pdf_filename_candidates`), así que los archivos
    guardados con el formato anterior se siguen encontrando.
    """

    tipo = _safe_pdf_filename_part(reference.type, "COMP")
    letter = _safe_pdf_filename_part(reference.letter)
    point = _numeric_pdf_filename_part(reference.point_of_sale, 5)
    number = _numeric_pdf_filename_part(reference.number, 8)
    customer = _erp_customer_filename_part(reference.customer_code)
    stem = f"{tipo}{letter}{point}-{number}" if point or number else tipo
    if customer:
        stem = f"{stem}-{customer}"
    return f"{stem}.pdf"


def _comprobante_pdf_filename_candidates(reference: BejermanPdfReference) -> list[str]:
    """Nombres con los que puede estar publicado el PDF.

    Conviven tres convenciones en las carpetas de Bejerman y hay que probarlas todas:

    * la de NEXORA (`_comprobante_pdf_filename`), que sanea y quita espacios:
      `RISR00004-00004252-NOV.pdf`;
    * la del ERP al imprimir, que **rellena el código de cliente a 6 con espacios a
      la izquierda**: `RISR00004-00004252-   NOV.pdf`. Buscar sólo el nombre propio
      hacía que un PDF que estaba en la carpeta se diera por ausente;
    * la del SDK (documentada en "10. Generación y Consulta de comprobantes
      emitidos"): `V-RT -R-00004-00004856-20072026-ARGAS .pdf`.
    """

    tipo = _safe_pdf_filename_part(reference.type, "COMP")
    letter = _safe_pdf_filename_part(reference.letter)
    point = _numeric_pdf_filename_part(reference.point_of_sale, 5)
    number = _numeric_pdf_filename_part(reference.number, 8)
    base = f"{tipo}{letter}{point}-{number}" if point or number else tipo
    candidates = [_comprobante_pdf_filename(reference)]  # ERP (lo que NEXORA escribe hoy)
    if base:
        candidates.append(f"{base}.pdf")  # sin código de cliente
        # Formato con el que NEXORA guardaba antes de unificar con el ERP: sin
        # relleno. Los archivos ya escritos así se tienen que seguir encontrando.
        legacy_customer = _safe_pdf_filename_part(reference.customer_code)
        if legacy_customer:
            candidates.append(f"{base}-{legacy_customer}.pdf")
        # Códigos ocasionales (@@@#@@) también aparecen saneados a alfanuméricos.
        alnum = "".join(ch for ch in as_string(reference.customer_code).upper() if ch.isalnum())
        if alnum:
            candidates.append(f"{base}-{alnum}.pdf")
            candidates.append(f"{base}-{alnum[:6].rjust(6)}.pdf")
    try:
        candidates.append(_samtronic_sdk_remito_facturas_filename(reference))
    except Exception:  # nombre opcional: nunca debe romper el sondeo
        pass
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _comprobante_pdf_filename_prefix(reference: BejermanPdfReference) -> str:
    tipo = _safe_pdf_filename_part(reference.type, "COMP")
    letter = _safe_pdf_filename_part(reference.letter)
    point = _numeric_pdf_filename_part(reference.point_of_sale, 5)
    number = _numeric_pdf_filename_part(reference.number, 8)
    return f"{tipo}{letter}{point}-{number}" if point or number else tipo


def _comprobante_pdf_filename_prefixes(reference: BejermanPdfReference) -> list[str]:
    """Prefijos para el barrido de carpeta, en las dos convenciones que existen."""

    prefixes = [_comprobante_pdf_filename_prefix(reference)]
    try:
        prefixes.append(_samtronic_sdk_remito_facturas_prefix(reference))
    except Exception:
        pass
    return [prefix for prefix in dict.fromkeys(prefixes) if prefix]


def _reference_issue_date_for_sdk_filename(reference: BejermanPdfReference) -> str:
    raw = as_string(reference.issue_date)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d%m%Y")
        except ValueError:
            pass
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 8:
        if raw[:4].isdigit():
            return digits[6:8] + digits[4:6] + digits[0:4]
        return digits
    return datetime.now().strftime("%d%m%Y")


def _sdk_customer_filename_part(value: Any) -> str:
    text = as_string(value).strip()
    if not text:
        return "".ljust(6)
    if text.isdigit():
        return str(int(text)).zfill(6)
    return text[:6].ljust(6)


def _sdk_pdf_output_dir() -> str:
    """Directorio `DirPDFSDK` de Bejerman (Parámetros Especiales, módulo GENPDF).

    Es donde el SDK deja los PDF que genera, con la convención `V-...`. No coincide
    necesariamente con la carpeta donde el ERP imprime cada tipo de comprobante.
    """

    try:
        return _samtronic_sdk_remito_facturas_dir_uri()
    except Exception:
        return ""


def _samtronic_sdk_remito_facturas_dir_uri() -> str:
    directory = as_string(getattr(settings, "BEJERMAN_PDF_SAMTRONIC_FACTURAS_DIR", "")) or r"C:\2. SEPID\PDF\FACTURAS"
    directory = directory.rstrip("\\/")
    if re.fullmatch(r"[A-Za-z]:", directory):
        directory += "\\"
    return remote_pdf_uri(directory)


def _samtronic_sdk_remito_facturas_filename(reference: BejermanPdfReference) -> str:
    tipo = as_string(reference.type).strip().upper()[:3].ljust(3)
    letter = as_string(reference.letter).strip().upper()[:1].ljust(1)
    point = _numeric_pdf_filename_part(reference.point_of_sale, 5)[:5].ljust(5)
    number = _numeric_pdf_filename_part(reference.number, 8)[-8:]
    issue_date = _reference_issue_date_for_sdk_filename(reference)
    customer = _sdk_customer_filename_part(reference.customer_code)
    return f"V-{tipo}-{letter}-{point}-{number}-{issue_date}-{customer}.pdf"


def _samtronic_sdk_remito_facturas_prefix(reference: BejermanPdfReference) -> str:
    tipo = as_string(reference.type).strip().upper()[:3].ljust(3)
    letter = as_string(reference.letter).strip().upper()[:1].ljust(1)
    point = _numeric_pdf_filename_part(reference.point_of_sale, 5)[:5].ljust(5)
    number = _numeric_pdf_filename_part(reference.number, 8)[-8:]
    return f"V-{tipo}-{letter}-{point}-{number}-"


def _samtronic_facturas_remito_cleanup_targets(reference: BejermanPdfReference) -> tuple[str, list[str], list[str]]:
    filenames = [_samtronic_sdk_remito_facturas_filename(reference)]
    filenames.extend(_comprobante_pdf_filename_candidates(reference))
    prefixes = [_samtronic_sdk_remito_facturas_prefix(reference), _comprobante_pdf_filename_prefix(reference)]
    return _samtronic_sdk_remito_facturas_dir_uri(), filenames, prefixes


def _cleanup_samtronic_facturas_remito_copy(reference: BejermanPdfReference, pdf: bytes) -> None:
    if _pdf_folder_kind(reference) != "REMITOS" or not pdf:
        return
    enabled = str(_setting("BEJERMAN_PDF_CLEAN_REMOTE_REMITOS_FROM_FACTURAS", "1")).strip().lower()
    if enabled in {"0", "false", "no", "n", "off"}:
        return
    try:
        directory_uri, filenames, prefixes = _samtronic_facturas_remito_cleanup_targets(reference)
        # Con el cliente persistido en la solicitud conocemos el nombre exacto
        # que usa Crystal. Probarlo primero evita listar toda FACTURAS y deja el
        # barrido por prefijo únicamente para documentos históricos.
        exact_names = filenames[:1]
        removed_paths = remove_remote_pdf_candidates_if_same(directory_uri, exact_names, [], pdf)
        if not removed_paths:
            removed_paths = remove_remote_pdf_candidates_if_same(directory_uri, filenames, prefixes, pdf)
        for path in removed_paths:
            logger.info(
                "bejerman_pdf_facturas_remito_copy_removed",
                extra={**_pdf_log_context(reference, interactive=False), "bejerman_pdf_path": path},
            )
    except Exception as exc:
        logger.warning(
            "bejerman_pdf_facturas_remito_copy_cleanup_failed",
            extra={**_pdf_log_context(reference, interactive=False), "error": str(exc)},
        )


def _unique_pdf_destination(folder: Path, filename: str, pdf: bytes) -> Path:
    destination = folder / filename
    if not destination.exists():
        return destination
    try:
        if destination.read_bytes() == pdf:
            return destination
    except OSError:
        pass
    for index in range(1, 100):
        candidate = folder / f"{destination.stem}_{index:02d}{destination.suffix}"
        if not candidate.exists():
            return candidate
        try:
            if candidate.read_bytes() == pdf:
                return candidate
        except OSError:
            pass
    return folder / f"{destination.stem}_{int(time.time())}{destination.suffix}"


def _store_comprobante_pdf_copy(client: BejermanSDKClient, reference: BejermanPdfReference, pdf: bytes) -> bool:
    try:
        output_dir = _configured_pdf_output_dir(client, reference)
    except BejermanPdfOutputSettingsError as exc:
        logger.warning(
            "bejerman_pdf_copy_invalid_output_dir",
            extra={**_pdf_log_context(reference, interactive=False), "error": str(exc)},
        )
        return False
    if not output_dir or not pdf:
        return False
    try:
        if is_remote_pdf_path(output_dir):
            result = write_remote_pdf(output_dir, _comprobante_pdf_filename(reference), pdf)
            _cleanup_samtronic_facturas_remito_copy(reference, pdf)
            logger.info(
                "bejerman_pdf_copy_saved",
                extra={**_pdf_log_context(reference, interactive=False), "bejerman_pdf_path": result.path},
            )
            return True
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        destination = _unique_pdf_destination(folder, _comprobante_pdf_filename(reference), pdf)
        if not destination.exists():
            destination.write_bytes(pdf)
        logger.info(
            "bejerman_pdf_copy_saved",
            extra={**_pdf_log_context(reference, interactive=False), "bejerman_pdf_path": str(destination)},
        )
        return True
    except Exception as exc:
        logger.warning(
            "bejerman_pdf_copy_failed",
            extra={**_pdf_log_context(reference, interactive=False), "bejerman_pdf_output_dir": output_dir, "error": str(exc)},
        )
        return False


def _probe_comprobante_pdf_copy(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    *,
    interactive: bool,
    allow_directory_scan: bool = True,
) -> tuple[bytes | None, str]:
    try:
        output_dir = _configured_pdf_output_dir(client, reference)
    except BejermanPdfOutputSettingsError as exc:
        logger.warning(
            "bejerman_pdf_copy_invalid_output_dir",
            extra={**_pdf_log_context(reference, interactive=interactive), "error": str(exc)},
        )
        return None, "error"
    if not output_dir:
        return None, "error"

    filenames = _comprobante_pdf_filename_candidates(reference)
    # El listado completo del directorio (fallback por prefijo) es caro contra la
    # carpeta remota de Bejerman/Samtronic (puede tardar minutos con miles de
    # archivos), así que se paga solo donde el llamador lo habilita. Los nombres
    # exactos cubren tanto el archivo que escribe NEXORA como el que publica
    # Bejerman; los prefijos son la red para las variantes que no adivinamos.
    prefixes = _comprobante_pdf_filename_prefixes(reference) if allow_directory_scan else []
    # El ERP imprime en la carpeta del tipo de comprobante (REMITOS / FACTURAS),
    # pero el SDK escribe todo junto en el directorio de `DirPDFSDK`, que puede ser
    # otro. Hay que mirar los dos: buscar sólo en el del tipo dejaba invisibles los
    # PDF que genera el SDK.
    search_dirs = [output_dir]
    sdk_dir = _sdk_pdf_output_dir()
    if sdk_dir and sdk_dir not in search_dirs:
        search_dirs.append(sdk_dir)

    raw = None
    path_label = ""
    for directory in search_dirs:
        try:
            if is_remote_pdf_path(directory):
                # Los chequeos interactivos y de última oportunidad van contra el
                # listado fresco; el ciclo normal del worker reutiliza el cacheado.
                raw, path_label = read_remote_pdf_candidates(
                    directory, filenames, prefixes or None, use_cache=not interactive,
                )
            else:
                folder = Path(directory)
                for filename in filenames:
                    path = folder / filename
                    if path.exists():
                        path_label = str(path)
                        raw = path.read_bytes()
                        break
                if raw is None and prefixes:
                    matches: list[Path] = []
                    for prefix in prefixes:
                        matches.extend(folder.glob(f"{prefix}*.pdf"))
                    for path in sorted(
                        dict.fromkeys(matches),
                        key=lambda item: item.stat().st_mtime if item.exists() else 0,
                        reverse=True,
                    ):
                        path_label = str(path)
                        raw = path.read_bytes()
                        if raw:
                            break
        except Exception as exc:
            logger.warning(
                "bejerman_pdf_copy_read_failed",
                extra={
                    **_pdf_log_context(reference, interactive=interactive),
                    "bejerman_pdf_path": directory,
                    "error": str(exc),
                },
            )
            return None, "error"
        if raw:
            break

    if not raw:
        return None, "missing"
    pdf = _normalized_pdf_bytes(raw)
    if not pdf:
        logger.warning(
            "bejerman_pdf_copy_invalid",
            extra={**_pdf_log_context(reference, interactive=interactive), "bejerman_pdf_path": path_label},
        )
        return None, "error"
    logger.info(
        "bejerman_pdf_cache_hit",
        extra={**_pdf_log_context(reference, interactive=interactive), "bejerman_pdf_path": path_label},
    )
    return pdf, "ready"


def _load_comprobante_pdf_copy(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    *,
    interactive: bool,
    allow_directory_scan: bool = True,
) -> bytes | None:
    pdf, _status = _probe_comprobante_pdf_copy(
        client, reference, interactive=interactive, allow_directory_scan=allow_directory_scan,
    )
    return pdf


def load_comprobante_pdf_copy(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    *,
    interactive: bool = True,
    allow_directory_scan: bool = True,
) -> bytes | None:
    """Lee y valida el PDF durable configurado para la empresa."""

    return _load_comprobante_pdf_copy(
        client, reference, interactive=interactive, allow_directory_scan=allow_directory_scan,
    )


def probe_comprobante_pdf_copy(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    *,
    interactive: bool = True,
    allow_directory_scan: bool = True,
) -> tuple[bytes | None, str]:
    """Distingue archivo listo, ausente y error de acceso/configuración.

    `allow_directory_scan` habilita el fallback caro de listar toda la carpeta
    remota por prefijo cuando los nombres exactos no coinciden (ver
    `_comprobante_pdf_filename_candidates`). Dejarlo en False en los caminos
    calientes (sondeo HTTP recurrente, ciclo normal del worker) y reservarlo
    para los chequeos de última oportunidad (recheck de estados terminales,
    vencimiento del deadline).
    """

    return _probe_comprobante_pdf_copy(
        client, reference, interactive=interactive, allow_directory_scan=allow_directory_scan,
    )


def store_comprobante_pdf_copy(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    pdf: bytes,
) -> bool:
    """Guarda bytes oficiales de Bejerman en el destino durable configurado."""

    normalized = _normalized_pdf_bytes(pdf)
    if not normalized:
        return False
    return _store_comprobante_pdf_copy(client, reference, normalized)


def normalize_pdf_bytes(data: bytes) -> bytes | None:
    """Normaliza un PDF admitiendo solamente BOM o espacios antes de `%PDF-`."""

    return _normalized_pdf_bytes(data)


def comprobante_pdf_output_path(client: BejermanSDKClient, reference: BejermanPdfReference) -> str:
    """Devuelve una etiqueta estable del destino esperado, sin acceder a la red."""

    try:
        output_dir = _configured_pdf_output_dir(client, reference)
    except BejermanPdfOutputSettingsError:
        return ""
    if not output_dir:
        return ""
    separator = "\\" if is_remote_pdf_path(output_dir) or "\\" in output_dir else os.sep
    clean_output_dir = output_dir.rstrip("\\/")
    return f"{clean_output_dir}{separator}{_comprobante_pdf_filename(reference)}"


def _pdf_log_context(reference: BejermanPdfReference, *, interactive: bool) -> dict[str, Any]:
    return {
        "bejerman_pdf_tipo": reference.type,
        "bejerman_pdf_numero": reference.number,
        "bejerman_pdf_letra": reference.letter,
        "bejerman_pdf_punto_venta": reference.point_of_sale,
        "bejerman_pdf_interactive": interactive,
    }


def _is_pdf_lookup_pending_error(exc: Exception) -> bool:
    return is_transient_pdf_lookup_message(exc)


def fetch_comprobante_pdf(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    *,
    retry_attempts: int | None = None,
    retry_delay_ms: int | None = None,
    interactive: bool = False,
    request_timeout: int | None = None,
) -> tuple[bytes, str]:
    if interactive:
        attempts = (
            retry_attempts
            if retry_attempts is not None
            else _positive_int_setting("BEJERMAN_PDF_INTERACTIVE_RETRY_ATTEMPTS", 1)
        )
        delay_ms = (
            retry_delay_ms
            if retry_delay_ms is not None
            else _positive_int_setting("BEJERMAN_PDF_INTERACTIVE_RETRY_DELAY_MS", 0)
        )
        retry_after_ms = _positive_int_setting("BEJERMAN_PDF_INTERACTIVE_RETRY_AFTER_MS", 1200)
        effective_timeout = (
            request_timeout
            if request_timeout is not None
            else _positive_int_setting("BEJERMAN_PDF_INTERACTIVE_REQUEST_TIMEOUT", 4)
        )
    else:
        attempts = retry_attempts if retry_attempts is not None else _positive_int_setting("BEJERMAN_PDF_RETRY_ATTEMPTS", 3)
        delay_ms = retry_delay_ms if retry_delay_ms is not None else _positive_int_setting("BEJERMAN_PDF_RETRY_DELAY_MS", 700)
        retry_after_ms = max(int(delay_ms or 0), 1000)
        effective_timeout = request_timeout
    attempts = max(1, int(attempts or 1))
    delay_ms = max(0, int(delay_ms or 0))
    total_started_at = time.monotonic()
    log_context = _pdf_log_context(reference, interactive=interactive)
    cached_pdf = _load_comprobante_pdf_copy(client, reference, interactive=interactive)
    if cached_pdf:
        logger.info("bejerman_pdf_ready", extra={**log_context, "duration_ms": _elapsed_ms(total_started_at), "source": "copy"})
        return cached_pdf, "application/pdf"
    with _temporary_request_timeout(client, effective_timeout):
        try:
            started_at = time.monotonic()
            response = client.consultar_comprobante_ventas_pdf(reference)
            pdf = extract_pdf_bytes(response)
            logger.info(
                "bejerman_pdf_initial_consult",
                extra={**log_context, "duration_ms": _elapsed_ms(started_at), "pdf_ready": bool(pdf)},
            )
            if pdf:
                _store_comprobante_pdf_copy(client, reference, pdf)
                logger.info("bejerman_pdf_ready", extra={**log_context, "duration_ms": _elapsed_ms(total_started_at)})
                return pdf, "application/pdf"
        except (BejermanSdkResponseError, BejermanSdkUnavailable):
            logger.info("bejerman_pdf_initial_consult_pending", extra=log_context)

        started_at = time.monotonic()
        try:
            client.generar_comprobante_ventas_pdf(reference)
            logger.info("bejerman_pdf_generate_requested", extra={**log_context, "duration_ms": _elapsed_ms(started_at)})
        except BejermanSdkUnavailable as exc:
            if interactive:
                logger.info(
                    "bejerman_pdf_generate_pending",
                    extra={**log_context, "duration_ms": _elapsed_ms(started_at), "retry_after_ms": retry_after_ms},
                )
                raise BejermanPdfPendingError(retry_after_ms=retry_after_ms) from exc
            raise
        except BejermanSdkResponseError as exc:
            if interactive and _is_pdf_lookup_pending_error(exc):
                logger.info(
                    "bejerman_pdf_generate_lookup_pending",
                    extra={**log_context, "duration_ms": _elapsed_ms(started_at), "retry_after_ms": retry_after_ms},
                )
                raise BejermanPdfPendingError(retry_after_ms=retry_after_ms) from exc
            raise

        for attempt in range(attempts):
            if attempt > 0 and delay_ms:
                time.sleep(delay_ms / 1000)
            try:
                started_at = time.monotonic()
                response = client.consultar_comprobante_ventas_pdf(reference)
            except (BejermanSdkResponseError, BejermanSdkUnavailable):
                logger.info("bejerman_pdf_retry_pending", extra={**log_context, "attempt": attempt + 1})
                continue
            pdf = extract_pdf_bytes(response)
            logger.info(
                "bejerman_pdf_retry_consult",
                extra={**log_context, "attempt": attempt + 1, "duration_ms": _elapsed_ms(started_at), "pdf_ready": bool(pdf)},
            )
            if pdf:
                _store_comprobante_pdf_copy(client, reference, pdf)
                logger.info("bejerman_pdf_ready", extra={**log_context, "duration_ms": _elapsed_ms(total_started_at)})
                return pdf, "application/pdf"
    pending_retry_ms = max(retry_after_ms, delay_ms, 1000)
    logger.info(
        "bejerman_pdf_pending",
        extra={**log_context, "duration_ms": _elapsed_ms(total_started_at), "retry_after_ms": pending_retry_ms},
    )
    raise BejermanPdfPendingError(retry_after_ms=pending_retry_ms)


def customer_codes_match(left: str | None, right: str | None) -> bool:
    return _clean(left) == _clean(right)


def find_bejerman_client_record(response: dict[str, Any], customer_code: str) -> dict[str, Any] | None:
    code = _clean(customer_code)
    if not code:
        return None
    for record in records_from_response(response):
        record_code = as_string(first_value(record, ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente")))
        if customer_codes_match(record_code, code):
            return record
    return None


CUSTOMER_DOCUMENT_RECORD_KEYS = ("Cliente_CUIT", "Cliente_NroDocumento", "Cliente_Cuit", "CUIT", "Cuit")


def find_bejerman_client_record_by_document(
    response: dict[str, Any], nro_documento: Any
) -> dict[str, Any] | None:
    """Hermano de find_bejerman_client_record, pero buscando por numero de documento.

    Es la misma busqueda que hace Bejerman cuando recibe el codigo ocasional '@@@#@@'. Compara solo
    digitos porque el maestro guarda el documento sin puntos ni guiones pero el dato local puede
    venir de cualquier forma. ObtenerClientes devuelve tambien las fichas deshabilitadas, asi que
    esta busqueda las encuentra igual (verificado contra el maestro real).
    """
    documento = _occasional_customer_digits(nro_documento)
    if not documento:
        return None
    for record in records_from_response(response):
        record_document = _occasional_customer_digits(first_value(record, CUSTOMER_DOCUMENT_RECORD_KEYS))
        if record_document and record_document == documento:
            return record
    return None


def build_customer_toggle_payload(record: dict[str, Any], *, habilitado: bool) -> dict[str, Any]:
    """Payload de ModificarCliente que cambia unicamente Cliente_Habilitado.

    ModificarCliente exige la ficha completa (razon social, provincia, situacion de IVA, vendedor,
    condicion de venta, etc. son obligatorios), asi que se reenvia tal cual vino de ObtenerClientes
    y se pisa un solo campo. Quien llame tiene que leer la ficha justo antes de escribir, para no
    revertir una edicion hecha desde el escritorio en el medio.
    """
    return {**record, "Cliente_Habilitado": bool(habilitado)}


def bejerman_client_record_is_enabled(record: dict[str, Any]) -> bool:
    return as_bool(first_value(record, ("Cliente_Habilitado", "ClienteHabilitado", "Habilitado")))


def build_customer_document_fields(response: dict[str, Any], customer_code: str) -> dict[str, str] | None:
    record = find_bejerman_client_record(response, customer_code)
    if not record:
        return None
    fields = {
        "Cliente_RazonSocial": as_string(first_value(record, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Nombre", "Cliente"))),
        "Cliente_TipoDocumento": as_string(
            first_value(
                record,
                (
                    "Cliente_TipoDocumento",
                    "Cliente_TipoDocumentoCodigo",
                    "TipoDocumento",
                    "TipoDocumentoCodigo",
                    "TipoDoc",
                ),
            )
        ),
        "Cliente_NroDocumento": as_string(first_value(record, ("Cliente_NroDocumento", "Cliente_CUIT", "Cliente_Cuit", "CUIT", "Cuit", "TaxId"))),
        "Cliente_Provincia": as_string(first_value(record, ("Cliente_Provincia", "Cliente_CodigoProvincia", "Provincia", "CodigoProvincia"))),
        "Cliente_SitIVA": as_string(first_value(record, ("Cliente_SitIVA", "Cliente_SituacionIVACodigo", "SituacionIVACodigo", "SituacionIVA"))),
        "Cliente_NumeroIIBB": as_string(first_value(record, ("Cliente_NumeroIIBB", "Cliente_NroIIBB", "NroIIBB", "IngresosBrutos"))),
    }
    return {key: value for key, value in fields.items() if value}


def resolve_customer_document_fields(client: BejermanSDKClient, customer_code: str) -> dict[str, str]:
    response = client.list_clientes()
    fields = build_customer_document_fields(response, customer_code)
    if not fields:
        raise BejermanSdkResponseError("BEJERMAN_CUSTOMER_NOT_FOUND")
    if not fields.get("Cliente_SitIVA"):
        raise BejermanSdkResponseError("BEJERMAN_CUSTOMER_VAT_MISSING")
    if not fields.get("Cliente_TipoDocumento"):
        raise BejermanSdkResponseError("BEJERMAN_CUSTOMER_DOCUMENT_TYPE_MISSING")
    if not fields.get("Cliente_NroDocumento"):
        raise BejermanSdkResponseError("BEJERMAN_CUSTOMER_DOCUMENT_MISSING")
    return fields


# Código especial documentado por Bejerman ("Estructuras de datos - Ventas", nota *3): en vez
# de buscar el cliente por Cliente_Codigo, el ERP lo busca (o da de alta automáticamente) por
# Cliente_NroDocumento. Es el equivalente API del checkbox "Cliente Vario" de su UI de escritorio.
BEJERMAN_OCCASIONAL_CUSTOMER_CODE = "@@@#@@"


def _occasional_customer_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


# Razones sociales locales que no son un cliente real de Bejerman sino el placeholder bajo el cual
# se cargan los particulares. Se comparan normalizadas (sin acentos, sin espacios, minúsculas).
OCCASIONAL_CUSTOMER_NAMES = frozenset({"particular", "particulares", "consumidorfinal", "clientevario"})


def _normalize_customer_name(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return "".join(ch for ch in text.lower() if ch.isalnum())


def is_occasional_customer_name(razon_social: Any) -> bool:
    """True si la razón social local marca un cliente ocasional (particular)."""
    return _normalize_customer_name(razon_social) in OCCASIONAL_CUSTOMER_NAMES


def bejerman_customer_code_for(razon_social: Any, cod_empresa: Any) -> str:
    """Código Bejerman a usar para un cliente local, o '' si es el placeholder de particulares.

    El maestro local puede tener cargado un código en la fila 'Particular' (históricamente 'PTL'),
    pero ese código no existe en Bejerman: los particulares se emiten como cliente ocasional. Devolver
    '' acá es lo que enciende el camino ocasional en todas las guardas `if not customer_code`.
    """
    if is_occasional_customer_name(razon_social):
        return ""
    return _clean(cod_empresa)


def build_occasional_customer_fields(razon_social: Any, nro_documento: Any) -> dict[str, str]:
    """Arma los campos Cliente_* para el código especial de cliente ocasional, sin consultar
    Bejerman. Se usa para el cliente placeholder 'Particular' (cod_empresa NULL) cuando el
    ingreso ya capturó nombre y documento del propietario ocasional."""
    nombre = _clean(razon_social)
    if not nombre:
        raise BejermanSdkResponseError("BEJERMAN_OCCASIONAL_CUSTOMER_NAME_MISSING")
    documento = _occasional_customer_digits(nro_documento)
    if not documento:
        raise BejermanSdkResponseError("BEJERMAN_OCCASIONAL_CUSTOMER_DOCUMENT_MISSING")
    tipo_documento = "1" if len(documento) == 11 else "5"  # C.U.I.T. (11 dígitos) vs D.N.I.
    return {
        "Cliente_RazonSocial": nombre,
        "Cliente_TipoDocumento": tipo_documento,
        "Cliente_NroDocumento": documento[:11],
        "Cliente_SitIVA": "3",  # Consumidor Final
        # Obligatorios segun "Estructuras de datos - Ventas": el cliente ocasional no los tiene, pero
        # la doc pide mandarlos vacios, no omitirlos.
        "Cliente_Provincia": "",
        "Cliente_NumeroIIBB": "",
    }


CUSTOMER_DOCUMENT_CACHE_REQUIRED_FIELDS = (
    "Cliente_RazonSocial",
    "Cliente_TipoDocumento",
    "Cliente_NroDocumento",
    "Cliente_Provincia",
    "Cliente_SitIVA",
)


def _customer_resolution_retry_delay_seconds(configured_value: Any = None) -> float:
    raw_value = (
        _setting("BEJERMAN_CUSTOMER_RESOLUTION_RETRY_DELAY_SECONDS", 2)
        if configured_value is None
        else configured_value
    )
    try:
        delay = float(raw_value)
    except (TypeError, ValueError):
        delay = 2.0
    return max(0.0, min(30.0, delay))


def resolve_customer_document_fields_cached(
    client: BejermanSDKClient,
    customer_code: str,
    local_lookup: Callable[[BejermanSDKClient, str], dict[str, str] | None],
    *,
    retry_delay_seconds: float | None = None,
) -> dict[str, str]:
    """Prioriza la caché local y reintenta una sola vez la resolución en vivo.

    El callback mantiene a esta capa desacoplada de la base de datos. Un error
    o un resultado incompleto de caché no bloquea la emisión: se continúa con
    el lookup normal de Bejerman. Solo se reintentan los errores específicos
    de resolución de cliente, nunca errores genéricos o de configuración.
    """

    try:
        cached_fields = local_lookup(client, customer_code)
    except Exception:
        logger.warning(
            "bejerman_customer_cache_lookup_failed",
            extra={
                "company_key": as_string(getattr(client, "company_key", "")),
                "customer_code": as_string(customer_code),
            },
            exc_info=True,
        )
        cached_fields = None

    if cached_fields and all(
        as_string(cached_fields.get(field))
        for field in CUSTOMER_DOCUMENT_CACHE_REQUIRED_FIELDS
    ):
        return dict(cached_fields)

    delay = _customer_resolution_retry_delay_seconds(retry_delay_seconds)
    for attempt in range(2):
        try:
            return resolve_customer_document_fields(client, customer_code)
        except BejermanSdkResponseError as exc:
            if attempt > 0 or not is_bejerman_customer_resolution_message(exc):
                raise
            logger.info(
                "bejerman_customer_resolution_retry",
                extra={
                    "company_key": as_string(getattr(client, "company_key", "")),
                    "customer_code": as_string(customer_code),
                    "delay_seconds": delay,
                    "error": str(exc)[:300],
                },
            )
            if delay:
                time.sleep(delay)

    raise BejermanSdkResponseError("BEJERMAN_CUSTOMER_NOT_FOUND")  # inalcanzable


def equipment_label(equipment: dict[str, Any]) -> str:
    explicit = as_string(equipment.get("equipmentLabel"))
    if explicit:
        return explicit
    model_text = " ".join(part for part in (as_string(equipment.get("model")), as_string(equipment.get("variant"))) if part)
    return " | ".join(part for part in (as_string(equipment.get("equipmentType")), as_string(equipment.get("brand")), model_text) if part) or as_string(equipment.get("articleName")) or "Equipo"


def equipment_article_description(equipment: dict[str, Any], fallback: str) -> str:
    # El código de artículo es la referencia maestra: si Bejerman ya tiene registrada una
    # descripción para ese código (mappedArticleName), esa es la que se manda, para que la
    # línea del comprobante coincida con lo que Bejerman resuelve por código en sus propias
    # queries. El rótulo armado localmente (equipmentLabel) solo se usa como respaldo cuando
    # todavía no se conoce la descripción real de Bejerman para ese artículo.
    mapped = as_string(equipment.get("mappedArticleName"))
    if mapped:
        return mapped
    explicit = as_string(equipment.get("equipmentLabel"))
    if explicit:
        return explicit
    model_text = " ".join(part for part in (as_string(equipment.get("model")), as_string(equipment.get("variant"))) if part)
    detail = " | ".join(part for part in (as_string(equipment.get("equipmentType")), as_string(equipment.get("brand")), model_text) if part)
    return detail or as_string(equipment.get("articleName")) or fallback


def service_release_equipment_line(value: Any, fallback: Any = "") -> str:
    text = as_string(value) or as_string(fallback) or "Equipo"
    text = re.sub(r"\s*-\s*OS[\s-]*\d+\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip(" -")
    return text or "Equipo"


def item_common(tipo: str, letter: str, point: str, issue_date: str, customer_code: str, sort_order: int) -> dict[str, Any]:
    return {
        "Comprobante_Tipo": tipo,
        "Comprobante_Letra": letter or "R",
        "Comprobante_PtoVenta": point,
        "Comprobante_Numero": "",
        "Comprobante_LoteHasta": "",
        "Comprobante_FechaEmision": issue_date,
        "Cliente_Codigo": customer_code,
        "Item_NumeroRenglon": sort_order,
    }


def legend_line(common: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        **common,
        "Item_Tipo": "L",
        "Item_CodigoArticulo": None,
        "Item_CantidadUM1": 0,
        "Item_CantidadUM2": 0,
        "Item_DescripArticulo": text[:250],
        "Item_PrecioUnitario": 0,
        "Item_TasaIVAInscrip": None,
        "Item_TasaIVANoInscrip": None,
        "Item_ImporteIVAInscrip": None,
        "Item_ImporteIVANoInscrip": None,
        "Item_ImporteTotal": None,
        "Item_ImporteDescComercial": None,
        "Item_ImporteDescFinanciero": None,
        "Item_ImporteDescGeneral": None,
        "Item_CodigoConceptoNoGravado": None,
        "Item_ImporteIVANoGravado": None,
        "Item_TipoIVA": None,
        "Item_CodigoDescPorLinea": None,
        "Item_ImporteDescPorLinea": None,
        "Item_Deposito": None,
        "Item_Partida": " ",
        "Item_TasaDescPorItem": 0,
        "Item_Importe": 0,
        "Item_FechaEntrega": common.get("Comprobante_FechaEmision"),
        "Item_Kit": None,
        "Item_RenglonKit": 0,
        "Item_EsPromocion": None,
        "Item_DatosAdicionales": None,
    }


def append_remito_legend_lines(
    items: list[dict[str, Any]],
    *,
    tipo: str,
    letter: str,
    point: str,
    issue_date: str,
    customer_code: str,
    sort_order: int,
    lines: Iterable[Any],
) -> int:
    """Serializa leyendas visibles de forma uniforme y devuelve el próximo orden.

    La composición de los textos vive en ``bejerman_remito_legends``; este
    adaptador concentra la forma técnica ``Item_Tipo='L'`` que exige el SDK.
    """

    for raw_line in lines:
        text = as_string(raw_line)
        if not text:
            continue
        items.append(
            legend_line(
                item_common(tipo, letter, point, issue_date, customer_code, sort_order),
                text,
            )
        )
        sort_order += 1
    return sort_order


# Bejerman's ventas SDK represents some stock-ingress remitos with negative
# item quantities. RIS is a service ingress and must add stock, so it stays
# positive; otherwise Bejerman treats it as an exit and asks for prior stock.
DEFAULT_SDK_NEGATIVE_QUANTITY_TYPES = "RD,RDN,RDA"
DEFAULT_EMISSION_NEGATIVE_QUANTITY_TYPES = DEFAULT_SDK_NEGATIVE_QUANTITY_TYPES


def _sdk_quantity_type_set(raw: Any) -> set[str]:
    return {part.strip().upper() for part in as_string(raw).split(",") if part.strip()}


def _emission_negative_quantity_types() -> set[str]:
    for setting_name in (
        "BEJERMAN_SDK_NEGATIVE_QUANTITY_TYPES",
        "BEJERMAN_EMISSION_NEGATIVE_QUANTITY_TYPES",
        "BEJERMAN_NEGATIVE_SDK_QUANTITY_TYPES",
        "BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES",
    ):
        default = DEFAULT_SDK_NEGATIVE_QUANTITY_TYPES if setting_name == "BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES" else ""
        raw = as_string(_setting(setting_name, default))
        if raw:
            return _sdk_quantity_type_set(raw)
    return _sdk_quantity_type_set(DEFAULT_SDK_NEGATIVE_QUANTITY_TYPES)


def _quantity_magnitude(value: Any) -> float:
    parsed = as_number(value)
    if parsed is None or parsed == 0:
        parsed = 1
    return abs(parsed)


def _quantity_value(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def sdk_uses_negative_article_quantity(document_type: Any) -> bool:
    return as_string(document_type).upper() in _emission_negative_quantity_types()


def sdk_signed_article_quantity(document_type: Any, quantity: Any = 1) -> int | float:
    magnitude = _quantity_magnitude(quantity)
    signed = -magnitude if sdk_uses_negative_article_quantity(document_type) else magnitude
    return _quantity_value(signed)


def sdk_emission_article_quantity(document_type: Any, quantity: Any = 1) -> int | float:
    return sdk_signed_article_quantity(document_type, quantity)


def resolve_equipment_partida(equipment: dict[str, Any] | None) -> str:
    equipment = equipment or {}
    explicit = as_string(equipment.get("partidaOverride")) or as_string(equipment.get("partida"))
    if explicit:
        return explicit
    return as_string(equipment.get("serial")) or as_string(equipment.get("internalNumber"))


def _service_ingress_partida_or_error(tipo: str, article_code: str, deposit: str, equipment: dict[str, Any]) -> str:
    partida = resolve_equipment_partida(equipment)
    if not partida:
        raise BejermanSdkResponseError(
            f"{tipo}_PARTIDA_REQUIRED: el artículo {article_code} requiere partida/identificador para ingresar stock en {deposit}"
        )
    return partida


def build_service_ingress_comprobante(payload: dict[str, Any], customer_fields: dict[str, str] | None = None) -> dict[str, Any]:
    customer_fields = customer_fields or {}
    equipment = payload.get("equipment") or {}
    seller = normalize_seller_code(payload.get("sellerCode"))
    if not seller:
        raise BejermanSdkResponseError("INVALID_SELLER_CODE")
    if len(seller) > 4:
        raise BejermanSdkResponseError("INVALID_SELLER_CODE_LENGTH")
    article_code = as_string(equipment.get("articleCode")) or as_string(_setting("BEJERMAN_RIS_GENERIC_ARTICLE_CODE", "SERVICIO"))
    if not article_code:
        raise BejermanSdkResponseError("SERVICE_INGRESS_ARTICLE_REQUIRED")
    issue_date = normalize_date(payload.get("issueDate")) or timezone.localdate().isoformat()
    profile_payload = payload.get("documentProfile") if isinstance(payload.get("documentProfile"), dict) else {}
    tipo = as_string(profile_payload.get("type") or _setting("BEJERMAN_RIS_TYPE", "RIS")) or "RIS"
    letter = as_string(profile_payload.get("letter") or _setting("BEJERMAN_RIS_LETTER", "R")) or "R"
    point = as_string(profile_payload.get("pointOfSale") or _setting("BEJERMAN_RIS_POINT_OF_SALE", "00004")) or "00004"
    operation = as_string(profile_payload.get("operation") or _setting("BEJERMAN_RIS_SERVICE_OPERATION", "REP")) or "REP"
    deposit = as_string(profile_payload.get("deposit") or _setting("BEJERMAN_RIS_DEPOSIT", "STR")) or "STR"
    update_stock = (
        as_bool(profile_payload.get("updateStock"))
        if "updateStock" in profile_payload
        else as_bool(_setting("BEJERMAN_RIS_UPDATE_STOCK", False))
    )
    raw_equipments = payload.get("equipments")
    equipments = [item for item in raw_equipments if isinstance(item, dict)] if isinstance(raw_equipments, list) else []
    partida = _service_ingress_partida_or_error(tipo, article_code, deposit, equipment) if update_stock and not equipments else ""
    serial_for_column = resolve_equipment_partida(equipment) if not partida else ""
    article_quantity = sdk_signed_article_quantity(tipo)
    # Mismo criterio que en el camino por lote: si el equipo arrastra un conflicto de ingreso
    # abierto, el comprobante sale sin número de OS.
    ingreso_id_text = "" if as_bool(equipment.get("suppressOsLabel")) else as_string(payload.get("ingresoId"))
    items: list[dict[str, Any]] = []
    sort_order = append_remito_legend_lines(
        items,
        tipo=tipo,
        letter=letter,
        point=point,
        issue_date=issue_date,
        customer_code=payload.get("customerCode"),
        sort_order=1,
        lines=[
            service_ingress_main_legend(ingreso_id_text),
            service_ingress_reason_legend(equipment, payload=payload, profile=profile_payload),
            *service_ingress_equipment_legend_lines(equipment, partida=partida),
        ],
    )
    items.append(
        {
            **item_common(tipo, letter, point, issue_date, payload.get("customerCode"), sort_order),
            "Item_Tipo": "A",
            "Item_CodigoArticulo": article_code,
            "Item_CantidadUM1": article_quantity,
            "Item_CantidadUM2": article_quantity if update_stock else 0,
            "Item_DescripArticulo": equipment_article_description(
                equipment,
                as_string(_setting("BEJERMAN_RIS_GENERIC_ARTICLE_NAME", "Equipo recibido para servicio técnico")),
            )[:250],
            "Item_PrecioUnitario": 0,
            "Item_TasaIVAInscrip": 0,
            "Item_TasaIVANoInscrip": 0,
            "Item_ImporteIVAInscrip": 0,
            "Item_ImporteIVANoInscrip": 0,
            "Item_ImporteTotal": 0,
            "Item_ImporteDescComercial": 0,
            "Item_ImporteDescFinanciero": 0,
            "Item_ImporteDescGeneral": 0,
            "Item_CodigoConceptoNoGravado": None,
            "Item_ImporteIVANoGravado": 0,
            "Item_TipoIVA": "1",
            "Item_CodigoDescPorLinea": None,
            "Item_ImporteDescPorLinea": 0,
            "Item_Deposito": deposit if update_stock else None,
            "Item_Partida": partida if update_stock else (serial_for_column or " "),
            "Item_TasaDescPorItem": 0,
            "Item_Importe": 0,
            "Item_FechaEntrega": issue_date,
            "Item_Kit": None,
            "Item_RenglonKit": 0,
            "Item_EsPromocion": None,
            "Item_DatosAdicionales": None,
        },
    )
    if equipments:
        generic_article_name = as_string(_setting("BEJERMAN_RIS_GENERIC_ARTICLE_NAME", "Equipo recibido para servicio técnico"))

        def batch_article_line(common: dict[str, Any], equipment_item: dict[str, Any]) -> dict[str, Any]:
            item_article_code = as_string(equipment_item.get("articleCode")) or article_code
            if not item_article_code:
                raise BejermanSdkResponseError("SERVICE_INGRESS_ARTICLE_REQUIRED")
            line_quantity = sdk_signed_article_quantity(tipo)
            item_partida = (
                _service_ingress_partida_or_error(tipo, item_article_code, deposit, equipment_item)
                if update_stock
                else ""
            )
            item_serial = resolve_equipment_partida(equipment_item) if not item_partida else ""
            return {
                **common,
                "Item_Tipo": "A",
                "Item_CodigoArticulo": item_article_code,
                "Item_CantidadUM1": line_quantity,
                "Item_CantidadUM2": line_quantity if update_stock else 0,
                "Item_DescripArticulo": equipment_article_description(equipment_item, generic_article_name)[:250],
                "Item_PrecioUnitario": 0,
                "Item_TasaIVAInscrip": 0,
                "Item_TasaIVANoInscrip": 0,
                "Item_ImporteIVAInscrip": 0,
                "Item_ImporteIVANoInscrip": 0,
                "Item_ImporteTotal": 0,
                "Item_ImporteDescComercial": 0,
                "Item_ImporteDescFinanciero": 0,
                "Item_ImporteDescGeneral": 0,
                "Item_CodigoConceptoNoGravado": None,
                "Item_ImporteIVANoGravado": 0,
                "Item_TipoIVA": "1",
                "Item_CodigoDescPorLinea": None,
                "Item_ImporteDescPorLinea": 0,
                "Item_Deposito": deposit if update_stock else None,
                "Item_Partida": item_partida if update_stock else (item_serial or " "),
                "Item_TasaDescPorItem": 0,
                "Item_Importe": 0,
                "Item_FechaEntrega": issue_date,
                "Item_Kit": None,
                "Item_RenglonKit": 0,
                "Item_EsPromocion": None,
                "Item_DatosAdicionales": None,
            }

        items = []
        sort_order = append_remito_legend_lines(
            items,
            tipo=tipo,
            letter=letter,
            point=point,
            issue_date=issue_date,
            customer_code=payload.get("customerCode"),
            sort_order=1,
            lines=[SERVICE_INGRESS_TITLE],
        )
        compact_legends = as_bool(payload.get("compactServiceIngressLegends"))
        for equipment_item in equipments:
            if not compact_legends:
                # `suppressOsLabel` marca los equipos cuya OS anterior quedó abierta: el renglón
                # sale sin número de OS hasta que se resuelva cuál de las dos órdenes vale.
                # Sin este corte el fallback a `ingresoId` volvería a imprimirla igual.
                os_text = (
                    ""
                    if as_bool(equipment_item.get("suppressOsLabel"))
                    else as_string(equipment_item.get("osLabel")) or as_string(equipment_item.get("ingresoId"))
                )
                sort_order = append_remito_legend_lines(
                    items,
                    tipo=tipo,
                    letter=letter,
                    point=point,
                    issue_date=issue_date,
                    customer_code=payload.get("customerCode"),
                    sort_order=sort_order,
                    lines=[
                        *service_ingress_equipment_legend_lines(equipment_item, os_text=os_text),
                        service_ingress_reason_legend(equipment_item, profile=profile_payload),
                        *accessory_legend_lines(equipment_item.get("accessories")),
                        as_string(equipment_item.get("comments")),
                    ],
                )
            items.append(batch_article_line(item_common(tipo, letter, point, issue_date, payload.get("customerCode"), sort_order), equipment_item))
            sort_order += 1

    notes = "\n".join(
        part
        for part in (
            as_string(payload.get("notes")),
            f"Accesorios: {as_string(equipment.get('accessories'))}" if as_string(equipment.get("accessories")) else "",
            as_string(equipment.get("comments")),
            f"Reparaciones {payload.get('requestId')}",
        )
        if part
    )
    return {
        "comprobante": {
            "Comprobante_Tipo": tipo,
            "Comprobante_Letra": letter,
            "Comprobante_PtoVenta": point,
            "Comprobante_Numero": "",
            "Comprobante_LoteHasta": "",
            "Comprobante_FechaEmision": issue_date,
            "Cliente_Codigo": payload.get("customerCode"),
            "Cliente_RazonSocial": customer_fields.get("Cliente_RazonSocial") or payload.get("customerName"),
            # `is not None` y no `value`: para el cliente ocasional hay campos obligatorios que
            # Bejerman espera presentes aunque vayan vacíos (Cliente_Provincia, Cliente_NumeroIIBB).
            # Los clientes resueltos contra Bejerman no se ven afectados porque
            # build_customer_document_fields ya descarta los vacíos antes de llegar acá.
            **{key: value for key, value in customer_fields.items() if key != "Cliente_RazonSocial" and value is not None},
            "Vendedor_Codigo": seller,
            "Comprobante_CondVenta": payload.get("paymentTermCode"),
            "Comprobante_FechaVencimiento": issue_date,
            "Comprobante_ImporteTotal": 0,
            "Comprobante_Mensaje": notes[:250] or " ",
            "Comprobante_ActualizaStock": "S" if update_stock else "N",
            "Comprobante_ListaPrecios": as_string(_setting("BEJERMAN_RIS_PRICE_LIST", "GN")) or "GN",
            "Comprobante_Moneda": as_string(_setting("BEJERMAN_RIS_CURRENCY", "")),
            "Comprobante_TipoCambio": as_string(_setting("BEJERMAN_RIS_EXCHANGE_TYPE", "")),
            "Comprobante_CotizacionCambio": float(_setting("BEJERMAN_RIS_EXCHANGE_RATE", 0) or 0),
            "Comprobante_FechaContabilizacion": issue_date,
            "Comprobante_FechaDDJJ": issue_date,
            "Comprobante_TipoOperacion": operation,
            "Comprobante_Items": items,
            "Comprobante_MediosPago": [],
            "Comprobante_RegEspeciales": [],
            "Comprobante_DatosAdicionales": None,
            "Comprobante_Cuotas": None,
        },
        "profile": {"type": tipo, "operation": operation, "deposit": deposit, "pointOfSale": point, "updateStock": update_stock},
        "lineCount": len(items),
    }


def parse_remito_response(response: dict[str, Any]) -> dict[str, Any]:
    record = first_record(response.get("DatosJSON")) or {}
    tipo = as_string(first_value(record, ("Comprobante_Tipo", "comprobanteTipo", "tipo")))
    letter = as_string(first_value(record, ("Comprobante_Letra", "comprobanteLetra", "letra")))
    point = as_string(first_value(record, ("Comprobante_PtoVenta", "comprobantePtoVenta", "puntoVenta")))
    number = as_string(first_value(record, ("Comprobante_Numero", "comprobanteNumero", "numero")))
    return {
        "comprobanteTipo": tipo,
        "comprobanteLetra": letter,
        "comprobantePtoVenta": point,
        "comprobanteNumero": number,
        "remitoNumber": format_remito_number(tipo, letter, point, number),
        "raw": record,
    }


def _positive_quantity(value: Any) -> int | float:
    return _quantity_value(_quantity_magnitude(value))


def _amount(value: Any) -> float:
    parsed = as_number(value)
    return parsed if parsed is not None else 0


def _percent(value: Any) -> float:
    parsed = as_number(value)
    if parsed is None:
        return 0
    return min(100, max(0, parsed))


def _delivery_price_currency(value: Any) -> str:
    text = as_string(value).upper().replace(" ", "")
    if not text:
        return "ARS"
    if text in {"$", "PESO", "PESOS"}:
        return "ARS"
    if text in {"U$S", "U$D", "US$", "DOLAR", "DOLARES"}:
        return "USD"
    return "USD" if text == "USD" else "ARS"


def _delivery_exchange_rate(value: Any, fallback: Any = 0) -> float:
    parsed = as_number(value)
    if parsed is not None and parsed > 0:
        return parsed
    fallback_parsed = as_number(fallback)
    return fallback_parsed if fallback_parsed is not None else 0


def _delivery_bejerman_currency_fields(
    price_currency: str,
    exchange_rate_input: Any,
    config: dict[str, Any],
) -> tuple[str, str, float]:
    if price_currency == "USD":
        exchange_rate = _delivery_exchange_rate(exchange_rate_input, config.get("exchangeRate"))
        if exchange_rate <= 0:
            raise BejermanSdkResponseError("DELIVERY_REMITO_EXCHANGE_RATE_REQUIRED")
        # Bejerman locks these header fields when billing a related remito.
        # Keep USD as Nexora's pricing context, but let billing convert to ARS.
        return "", "", 0
    return as_string(config.get("arsCurrencyCode")), as_string(config.get("arsExchangeTypeCode")), 0


def _delivery_item_price_currencies(request: dict[str, Any], orders: list[dict[str, Any]]) -> set[str]:
    request_currency = _delivery_price_currency(request.get("priceCurrency"))
    currencies: set[str] = set()
    for order in orders:
        order_currency = _delivery_price_currency(order.get("priceCurrency") or request_currency)
        for item in order.get("items") or []:
            if isinstance(item, dict):
                currencies.add(_delivery_price_currency(item.get("priceCurrency") or order_currency))
    return currencies or {request_currency}


def _delivery_decimal_label(value: Any, *, decimals: int = 2) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    label = f"{number:,.{decimals}f}"
    return label.replace(",", "_").replace(".", ",").replace("_", ".")


def _delivery_percent_label(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    if number.is_integer():
        return str(int(number))
    return _delivery_decimal_label(number, decimals=2).rstrip("0").rstrip(",")


def _delivery_money_label(value: Any, price_currency: str) -> str:
    symbol = "U$S" if price_currency == "USD" else "$"
    return f"{symbol} {_delivery_decimal_label(value)}"


def _delivery_discount_legend(discount_percent: Any, final_unit_price: Any, line_amount: Any, price_currency: str, quantity: Any) -> str:
    parts = [
        f"Descuento aplicado: {_delivery_percent_label(discount_percent)}%",
        f"Precio final con descuento: {_delivery_money_label(final_unit_price, price_currency)}",
    ]
    try:
        show_line_total = float(quantity or 0) != 1
    except (TypeError, ValueError):
        show_line_total = False
    if show_line_total:
        parts.append(f"Total neto: {_delivery_money_label(line_amount, price_currency)}")
    return " - ".join(parts)


def _profile_for_delivery_type(delivery_type: str, config: dict[str, Any]) -> dict[str, str]:
    if delivery_type == "rental":
        return {"type": config["rentalType"], "operation": config["rentalOperation"], "deposit": config["rentalDeposit"], "pointOfSale": config["rentalPointOfSale"]}
    if delivery_type == "demo":
        return {"type": config["demoType"], "operation": config["demoOperation"], "deposit": config["demoDeposit"], "pointOfSale": config["demoPointOfSale"]}
    if delivery_type == "service_release":
        return {"type": config["serviceType"], "operation": config["serviceOperation"], "deposit": config["serviceDeposit"], "pointOfSale": config["servicePointOfSale"]}
    return {"type": config["saleType"], "operation": config["saleOperation"], "deposit": config["saleDeposit"], "pointOfSale": config["salePointOfSale"]}


def delivery_remito_config(default_profile: dict[str, Any]) -> dict[str, Any]:
    point_of_sale = as_string(default_profile.get("pointOfSale", "00004")) or "00004"
    return {
        "letter": as_string(_setting("BEJERMAN_REMITO_LETTER", "R")) or "R",
        "priceListCode": as_string(_setting("BEJERMAN_REMITO_PRICE_LIST", "GN")) or "GN",
        "currencyCode": as_string(_setting("BEJERMAN_REMITO_CURRENCY", "")),
        "arsCurrencyCode": as_string(_setting("BEJERMAN_REMITO_CURRENCY_ARS", _setting("BEJERMAN_REMITO_CURRENCY", ""))),
        "usdCurrencyCode": as_string(_setting("BEJERMAN_REMITO_CURRENCY_USD", "2")) or "2",
        "exchangeTypeCode": as_string(_setting("BEJERMAN_REMITO_EXCHANGE_TYPE", "")),
        "arsExchangeTypeCode": as_string(_setting("BEJERMAN_REMITO_EXCHANGE_TYPE_ARS", _setting("BEJERMAN_REMITO_EXCHANGE_TYPE", ""))),
        "usdExchangeTypeCode": as_string(_setting("BEJERMAN_REMITO_EXCHANGE_TYPE_USD", _setting("BEJERMAN_REMITO_EXCHANGE_TYPE", "DVE"))) or "DVE",
        "exchangeRate": float(_setting("BEJERMAN_REMITO_EXCHANGE_RATE", 0) or 0),
        "saleType": as_string(_setting("BEJERMAN_REMITO_SALE_TYPE", default_profile.get("type", "RT"))),
        "saleOperation": as_string(_setting("BEJERMAN_REMITO_SALE_OPERATION", default_profile.get("operation", "MC"))),
        "saleDeposit": as_string(_setting("BEJERMAN_REMITO_SALE_DEPOSIT", default_profile.get("deposit", "VAL"))),
        "salePointOfSale": point_of_sale,
        "rentalType": as_string(_setting("BEJERMAN_REMITO_RENTAL_TYPE", default_profile.get("type", "RTA"))),
        "rentalOperation": as_string(_setting("BEJERMAN_REMITO_RENTAL_OPERATION", default_profile.get("operation", "ALQ"))),
        "rentalDeposit": as_string(_setting("BEJERMAN_REMITO_RENTAL_DEPOSIT", default_profile.get("deposit", "STL"))),
        "rentalPointOfSale": point_of_sale,
        "demoType": as_string(_setting("BEJERMAN_REMITO_DEMO_TYPE", default_profile.get("type", "RTN"))),
        "demoOperation": as_string(_setting("BEJERMAN_REMITO_DEMO_OPERATION", default_profile.get("operation", "DEMO"))),
        "demoDeposit": as_string(_setting("BEJERMAN_REMITO_DEMO_DEPOSIT", default_profile.get("deposit", "VAL"))),
        "demoPointOfSale": point_of_sale,
        "serviceType": as_string(_setting("BEJERMAN_REMITO_SERVICE_TYPE", default_profile.get("type", "RSS"))),
        "serviceOperation": as_string(_setting("BEJERMAN_REMITO_SERVICE_OPERATION", default_profile.get("operation", "REP"))),
        "serviceDeposit": as_string(_setting("BEJERMAN_REMITO_SERVICE_DEPOSIT", default_profile.get("deposit", "STC"))),
        "servicePointOfSale": point_of_sale,
    }


def article_description_for(article_code: Any, article_descriptions: dict[str, str] | None) -> str:
    """Busca en el mapa de `build_article_description_map` con las mismas variantes
    de código que usa el IVA de venta, que es donde se vio que los códigos llegan
    escritos de más de una forma."""

    if not article_descriptions:
        return ""
    for key in (_article_lookup_key(article_code), normalize_search(article_code), as_string(article_code)):
        if key and as_string(article_descriptions.get(key)):
            return as_string(article_descriptions[key])
    return ""


def _lookup_article_sales_vat(article_code: Any, article_tax_by_code: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    if not article_tax_by_code:
        return {}
    lookup_keys = [
        _article_lookup_key(article_code),
        normalize_search(article_code),
        as_string(article_code),
    ]
    for key in lookup_keys:
        if key and isinstance(article_tax_by_code.get(key), dict):
            return article_tax_by_code[key]
    return {}


def _first_sales_vat_value(sources: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = first_value(source, keys)
        if value not in (None, ""):
            return value
    return None


def _delivery_item_sales_vat(
    item: dict[str, Any],
    article_code: Any,
    article_tax_by_code: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    catalog_vat = _lookup_article_sales_vat(article_code, article_tax_by_code)
    sources = [catalog_vat, item]
    vat_type = as_string(
        _first_sales_vat_value(
            sources,
            (
                "salesVatType",
                "vatType",
                "ivaType",
                "tipoIva",
                "Art_TipoTasaVentas",
                "TipoTasaVentas",
                "Item_TipoIVA",
            ),
        )
    )
    vat_code = as_string(
        _first_sales_vat_value(
            sources,
            (
                "salesVatCode",
                "vatCode",
                "ivaCode",
                "codigoIva",
                "Art_CodTasaIVAVentas",
                "CodTasaIVAVentas",
            ),
        )
    )
    vat_rate = as_number(
        _first_sales_vat_value(
            sources,
            (
                "salesVatRate",
                "vatRate",
                "ivaRate",
                "taxRate",
                "Item_TasaIVAInscrip",
            ),
        )
    )
    if vat_rate is None:
        vat_rate = _delivery_vat_rate_for_code(vat_code)
    if vat_rate is not None and vat_rate > 0 and not vat_type:
        vat_type = "1"
    vat_type = vat_type or "1"
    if vat_type not in {"1", "5"}:
        vat_rate = 0
    if vat_rate is None or vat_rate < 0:
        vat_rate = 0
    return {"type": vat_type, "code": vat_code, "rate": vat_rate}


def _delivery_vat_amount(amount: float, vat_rate: float) -> float:
    if vat_rate <= 0:
        return 0
    return round(amount * vat_rate / 100, 2)


DELIVERY_MONEY_QUANT = Decimal("0.01")
DELIVERY_UNIT_PRICE_QUANT = Decimal("0.000001")


def _delivery_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _delivery_money(value: Any) -> Decimal:
    return _delivery_decimal(value).quantize(DELIVERY_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _delivery_money_number(value: Any) -> float:
    return float(_delivery_money(value))


def _delivery_unit_price_number(value: Any) -> float:
    return float(_delivery_decimal(value).quantize(DELIVERY_UNIT_PRICE_QUANT, rounding=ROUND_HALF_UP))


def _validate_delivery_comprobante_amounts(comprobante: dict[str, Any]) -> None:
    article_items = [
        item
        for item in comprobante.get("Comprobante_Items") or []
        if isinstance(item, dict) and as_string(item.get("Item_Tipo")).upper() == "A"
    ]
    calculated = sum(
        (
            _delivery_decimal(item.get("Item_ImporteTotal"))
            + _delivery_decimal(item.get("Item_ImporteIVAInscrip"))
            + _delivery_decimal(item.get("Item_ImporteIVANoInscrip"))
            + _delivery_decimal(item.get("Item_ImporteIVANoGravado"))
        )
        for item in article_items
    )
    calculated = _delivery_money(calculated)
    declared = _delivery_money(comprobante.get("Comprobante_ImporteTotal"))
    if calculated != declared:
        raise BejermanSdkResponseError(
            "DELIVERY_REMITO_AMOUNT_MISMATCH: "
            f"el total del encabezado ({declared}) no coincide con los renglones ({calculated})"
        )


def build_delivery_remito_comprobante(
    request: dict[str, Any],
    config: dict[str, Any],
    customer_fields: dict[str, str] | None = None,
    article_tax_by_code: dict[str, dict[str, Any]] | None = None,
    article_descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    customer_fields = customer_fields or {}
    orders = request.get("orders") or []
    if not orders:
        raise BejermanSdkResponseError("DELIVERY_ORDERS_REQUIRED")
    seller = normalize_seller_code(request.get("sellerCode"))
    if not seller:
        raise BejermanSdkResponseError("INVALID_SELLER_CODE")
    if len(seller) > 4:
        raise BejermanSdkResponseError("INVALID_SELLER_CODE_LENGTH")
    first_type = orders[0].get("deliveryType") or "sale"
    profile = _profile_for_delivery_type(first_type, config)
    issue_date = normalize_date(request.get("issueDate")) or timezone.localdate().isoformat()
    item_price_currencies = _delivery_item_price_currencies(request, orders)
    if len(item_price_currencies) != 1:
        raise BejermanSdkResponseError("INCOMPATIBLE_DELIVERY_REMITO_CURRENCY")
    price_currency = next(iter(item_price_currencies))
    exchange_rate_input = request.get("exchangeRate") or orders[0].get("commercialExchangeRate")
    currency_code, exchange_type_code, exchange_rate = _delivery_bejerman_currency_fields(
        price_currency,
        exchange_rate_input,
        config,
    )
    items: list[dict[str, Any]] = []
    sort_order = 1
    total = Decimal("0")
    tax_total = Decimal("0")
    for order in orders:
        is_service_release = order.get("deliveryType") == "service_release"
        is_sale = order.get("deliveryType") == "sale"
        if not is_service_release and not is_sale:
            sort_order = append_remito_legend_lines(
                items,
                tipo=profile["type"],
                letter=config["letter"],
                point=profile["pointOfSale"],
                issue_date=issue_date,
                customer_code=request.get("customerCode"),
                sort_order=sort_order,
                lines=[delivery_order_legend(order)],
            )
        for item in order.get("items") or []:
            article_code = as_string(item.get("articleCode"))
            quantity = _positive_quantity(item.get("quantity"))
            unit_price = _amount(item.get("unitPrice"))
            item_currency = _delivery_price_currency(item.get("priceCurrency") or price_currency)
            discount_percent = _percent(item.get("discountPercent"))
            explicit_partida = as_string(item.get("partida"))
            omit_partida = as_nullable_bool(item.get("articleRequiresPartida")) is False and not explicit_partida and not (item.get("partidas") or [])
            partida = "" if omit_partida else as_string(explicit_partida or item.get("equipmentSerial") or order.get("equipmentSerial"))
            item_deposit = as_string(item.get("stockDepositCode")) or profile["deposit"]
            # La descripción del maestro de Bejerman manda sobre el texto guardado en
            # NEXORA: el vínculo entre los dos sistemas es el código de artículo, y el
            # texto local es una foto del catálogo al momento de armar el pedido.
            article_name = (
                article_description_for(article_code, article_descriptions)
                or as_string(item.get("articleName"))
                or as_string(item.get("description"))
                or article_code
            )
            if is_service_release:
                article_name = service_release_equipment_line(article_name, item.get("description") or article_code)
            line_defs: list[tuple[float, str, str]] = []
            for partida_item in item.get("partidas") or []:
                if not isinstance(partida_item, dict):
                    continue
                partida_code = as_string(
                    partida_item.get("partida")
                    or partida_item.get("code")
                    or partida_item.get("numeroPartida")
                    or partida_item.get("serial")
                )
                if partida_code:
                    line_defs.append(
                        (
                            _positive_quantity(partida_item.get("assignedQuantity") or partida_item.get("quantity")),
                            partida_code,
                            as_string(partida_item.get("stockDepositCode")) or item_deposit,
                        )
                    )
            if not line_defs:
                line_defs = [(quantity, partida, item_deposit)]
            for line_quantity, line_partida, line_deposit in line_defs:
                if is_service_release:
                    sort_order = append_remito_legend_lines(
                        items,
                        tipo=profile["type"],
                        letter=config["letter"],
                        point=profile["pointOfSale"],
                        issue_date=issue_date,
                        customer_code=request.get("customerCode"),
                        sort_order=sort_order,
                        lines=[service_release_item_legend(order, item)],
                    )
                signed_line_quantity = sdk_signed_article_quantity(profile["type"], line_quantity)
                vat = _delivery_item_sales_vat(item, article_code, article_tax_by_code) if article_code else {"type": "1", "rate": 0}
                quantity_decimal = _delivery_decimal(line_quantity)
                unit_price_decimal = _delivery_decimal(unit_price)
                discount_decimal = _delivery_decimal(discount_percent)
                vat_rate_decimal = _delivery_decimal(vat["rate"])
                # El renglón siempre viaja con el neto y la alícuota del artículo discriminada,
                # cualquiera sea la situación de IVA del cliente. Entre el 14/07 y el 07/08/2026
                # los clientes que no eran responsables inscriptos salían con el precio
                # multiplicado por 1+tasa y la alícuota en 0: Bejerman guardaba el renglón con
                # código de IVA 04 (0%) y toda factura hecha desde ese remito —A, B o FCC—
                # nacía con IVA cero. Cómo se imprime el precio (neto o final) lo decide la
                # letra del comprobante en el ERP, no el remito.
                item_unit_price = unit_price_decimal
                gross_amount = quantity_decimal * item_unit_price
                discount_amount = _delivery_money(gross_amount * discount_decimal / Decimal("100"))
                amount = _delivery_money(gross_amount - discount_amount)
                final_unit_price = item_unit_price * (Decimal("1") - discount_decimal / Decimal("100"))
                item_vat_rate = vat_rate_decimal
                vat_amount = _delivery_money(amount * vat_rate_decimal / Decimal("100"))
                total += amount
                tax_total += vat_amount
                items.append(
                    {
                        **item_common(profile["type"], config["letter"], profile["pointOfSale"], issue_date, request.get("customerCode"), sort_order),
                        "Item_Tipo": "A" if article_code else "L",
                        "Item_CodigoArticulo": article_code or None,
                        "Item_CantidadUM1": signed_line_quantity if article_code else 0,
                        "Item_CantidadUM2": 0,
                        "Item_DescripArticulo": article_name[:250],
                        "Item_PrecioUnitario": _delivery_unit_price_number(item_unit_price),
                        "Item_TasaIVAInscrip": float(item_vat_rate),
                        "Item_TasaIVANoInscrip": 0,
                        "Item_ImporteIVAInscrip": _delivery_money_number(vat_amount),
                        "Item_ImporteIVANoInscrip": 0,
                        "Item_ImporteTotal": _delivery_money_number(amount),
                        "Item_ImporteDescComercial": 0,
                        "Item_ImporteDescFinanciero": 0,
                        "Item_ImporteDescGeneral": 0,
                        "Item_CodigoConceptoNoGravado": None,
                        "Item_ImporteIVANoGravado": 0,
                        "Item_TipoIVA": vat["type"],
                        "Item_CodigoDescPorLinea": None,
                        "Item_ImporteDescPorLinea": _delivery_money_number(discount_amount),
                        "Item_Deposito": (line_deposit or profile["deposit"]) if article_code else None,
                        "Item_Partida": line_partida or " ",
                        "Item_TasaDescPorItem": discount_percent,
                        "Item_Importe": _delivery_money_number(amount),
                        "Item_FechaEntrega": issue_date,
                        "Item_Kit": None,
                        "Item_RenglonKit": 0,
                        "Item_EsPromocion": None,
                        "Item_DatosAdicionales": None,
                    }
                )
                sort_order += 1
                if article_code and discount_percent > 0:
                    sort_order = append_remito_legend_lines(
                        items,
                        tipo=profile["type"],
                        letter=config["letter"],
                        point=profile["pointOfSale"],
                        issue_date=issue_date,
                        customer_code=request.get("customerCode"),
                        sort_order=sort_order,
                        lines=[
                            _delivery_discount_legend(
                                discount_percent,
                                float(final_unit_price),
                                float(amount),
                                item_currency,
                                line_quantity,
                            )
                        ],
                    )
    notes = "\n".join(part for part in (as_string(request.get("notes")), f"NEXORA {request.get('groupId')}", f"Órdenes {', '.join(as_string(order.get('orderNumber')) for order in orders)}") if part)
    comprobante = {
            "Comprobante_Tipo": profile["type"],
            "Comprobante_Letra": config["letter"],
            "Comprobante_PtoVenta": profile["pointOfSale"],
            "Comprobante_Numero": "",
            "Comprobante_LoteHasta": "",
            "Comprobante_FechaEmision": issue_date,
            "Cliente_Codigo": request.get("customerCode"),
            "Cliente_RazonSocial": customer_fields.get("Cliente_RazonSocial") or request.get("customerName"),
            # `is not None` y no `value`: para el cliente ocasional hay campos obligatorios que
            # Bejerman espera presentes aunque vayan vacíos (Cliente_Provincia, Cliente_NumeroIIBB).
            # Los clientes resueltos contra Bejerman no se ven afectados porque
            # build_customer_document_fields ya descarta los vacíos antes de llegar acá.
            **{key: value for key, value in customer_fields.items() if key != "Cliente_RazonSocial" and value is not None},
            "Vendedor_Codigo": seller,
            "Comprobante_CondVenta": request.get("paymentTermCode"),
            "Comprobante_FechaVencimiento": issue_date,
            "Comprobante_ImporteTotal": _delivery_money_number(total + tax_total),
            "Comprobante_Mensaje": notes[:250] or " ",
            "Comprobante_ActualizaStock": "S",
            "Comprobante_ListaPrecios": config["priceListCode"],
            "Comprobante_Moneda": currency_code,
            "Comprobante_TipoCambio": exchange_type_code,
            "Comprobante_CotizacionCambio": exchange_rate,
            "Comprobante_FechaContabilizacion": issue_date,
            "Comprobante_FechaDDJJ": issue_date,
            "Comprobante_TipoOperacion": profile["operation"],
            "Comprobante_Items": items,
            "Comprobante_MediosPago": [],
            "Comprobante_RegEspeciales": [],
            "Comprobante_DatosAdicionales": None,
            "Comprobante_Cuotas": None,
        }
    _validate_delivery_comprobante_amounts(comprobante)
    return {
        "comprobante": comprobante,
        "profile": profile,
        "lineCount": len(items),
        "amountMode": "tax_discriminated",
    }


def comprobante_id_of(record: dict[str, Any]) -> str:
    return as_string(first_value(record, ("Comprobante_ID", "ComprobanteId", "IDComprobante", "id")))


def normalize_serial_for_lookup(value: Any) -> str:
    return re.sub(r"[\s\-_./]+", "", as_string(value)).upper()


def build_serial_lookup_sales_filters(tipo: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
    return [
        bejerman_filter("Comprobante_Tipo", "IGUAL", tipo),
        bejerman_filter("Comprobante_FechaEmision", "MAYOR O IGUAL", date_from),
        bejerman_filter("Comprobante_FechaEmision", "MENOR O IGUAL", date_to),
    ]


def headers_from_sales_list(response: dict[str, Any], max_comprobantes: int) -> list[dict[str, Any]]:
    records = [record for record in records_from_response(response) if comprobante_id_of(record)]
    records.sort(key=lambda record: (normalize_date(first_value(record, ("Comprobante_FechaEmision", "FechaEmision"))) or "", as_string(first_value(record, ("Comprobante_Numero", "Numero")))), reverse=True)
    return records[: max(1, max_comprobantes)]


def find_serial_in_detail(serial: str, detail_response: dict[str, Any], checked: int) -> dict[str, Any] | None:
    normalized = normalize_serial_for_lookup(serial)
    detail = first_record(detail_response.get("DatosJSON"))
    if not detail:
        return None
    for raw_item in detail.get("Comprobante_Items") or []:
        if not isinstance(raw_item, dict):
            continue
        partida = as_string(first_value(raw_item, ("Item_Partida", "Partida")))
        if normalize_serial_for_lookup(partida) != normalized:
            continue
        doc_type = as_string(first_value(detail, ("Comprobante_Tipo", "TipoComprobante", "Tipo")))
        letter = as_string(first_value(detail, ("Comprobante_Letra", "Letra")))
        point = as_string(first_value(detail, ("Comprobante_PtoVenta", "PuntoVenta", "PtoVenta")))
        number = as_string(first_value(detail, ("Comprobante_Numero", "Numero", "NroComprobante")))
        customer_code = as_string(first_value(detail, ("Cliente_Codigo", "ClienteCodigo", "CodCliente")))
        issue_date = normalize_date(first_value(detail, ("Comprobante_FechaEmision", "FechaEmision")))
        return {
            "found": True,
            "serial": serial,
            "normalizedSerial": normalized,
            "articleCode": as_string(first_value(raw_item, ("Item_CodigoArticulo", "CodigoArticulo", "Articulo_Codigo"))),
            "articleDescription": as_string(first_value(raw_item, ("Item_DescripArticulo", "Item_DescripcionArticulo", "Descripcion"))),
            "customerCode": customer_code,
            "customerName": as_string(first_value(detail, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial"))),
            "issueDate": issue_date,
            "documentType": doc_type,
            "documentLetter": letter,
            "documentPointOfSale": point,
            "documentNumber": number,
            "documentId": build_document_id({"t": doc_type, "n": number, "l": letter, "p": point, "f": issue_date, "c": customer_code}) if doc_type and number and point else None,
            "documentLabel": document_number_from_parts(doc_type, letter, point, number),
            "itemPartida": partida,
            "itemQuantity": as_number(first_value(raw_item, ("Item_CantidadUM1", "CantidadUM1", "Cantidad"))),
            "comprobanteId": comprobante_id_of(detail),
            "checkedComprobantes": checked,
        }
    return None


def lookup_sale_by_serial(client: BejermanSDKClient, serial: str) -> dict[str, Any]:
    normalized = normalize_serial_for_lookup(serial)
    if not normalized:
        return {"found": False, "serial": serial, "normalizedSerial": normalized, "checkedComprobantes": 0}
    months = int(_setting("BEJERMAN_SERIAL_LOOKUP_MONTHS_BACK", 60) or 60)
    max_comprobantes = int(_setting("BEJERMAN_SERIAL_LOOKUP_MAX_COMPROBANTES", 120) or 120)
    types = [item.strip() for item in as_string(_setting("BEJERMAN_SERIAL_LOOKUP_TYPES", "FC")).split(",") if item.strip()] or ["FC"]
    today = timezone.localdate()
    date_from = (today - timedelta(days=max(1, months) * 31)).isoformat()
    checked = 0
    for tipo in types:
        headers = headers_from_sales_list(client.list_comprobantes_ventas(build_serial_lookup_sales_filters(tipo, date_from, today.isoformat())), max_comprobantes)
        for header in headers:
            checked += 1
            detail_id = comprobante_id_of(header)
            if not detail_id:
                continue
            found = find_serial_in_detail(serial, client.detalle_comprobante_ventas(detail_id), checked)
            if found:
                return found
    return {"found": False, "serial": serial, "normalizedSerial": normalized, "checkedComprobantes": checked}
