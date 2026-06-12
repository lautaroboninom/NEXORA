from __future__ import annotations

import base64
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from django.conf import settings
from django.utils import timezone

from .bejerman_companies import require_company


REGISTRO_ACTION = "http://localhost:57213/IEFlexSDK_Service/EFlexSDK_WSRegistro"
EJECUTAR_ACTION = "http://localhost:57213/IEFlexSDK_Service/EFlexSDK_WSEjecutar"

BILLING_COMPROBANTE_TYPES = {"FC", "NC", "ND"}
TIPO_OPERACION_LABELS = {
    "ALQ": "Alquiler",
    "BUSO": "Vta Bien de uso",
    "DEMO": "Demostración",
    "FAB": "Fabricación",
    "MC": "Mercadería",
    "REP": "Reparación",
}


class BejermanSdkError(RuntimeError):
    pass


class BejermanSdkConfigError(BejermanSdkError):
    pass


class BejermanSdkUnavailable(BejermanSdkError):
    pass


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
    return json.dumps(value, ensure_ascii=False, default=str)


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
    resultado = _clean(response.get("Resultado")).upper()
    error = _clean(response.get("ErrorMsg"))
    return resultado.startswith("OK") and not error


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
    match = re.match(r"^/Date\((\d+)\)/$", raw)
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
    def __init__(self, company_key: str | None = None, *, bejerman_company: str | None = None):
        self.wsdl_url = _clean(_setting("BEJERMAN_WSDL_URL"))
        self.endpoint_url = self.wsdl_url.split("?", 1)[0]
        self.timeout = int(_setting("BEJERMAN_REQUEST_TIMEOUT", 30) or 30)
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

    def _post(self, action: str, body: str) -> dict[str, str]:
        if not self.endpoint_url:
            raise BejermanSdkConfigError("BEJERMAN_WSDL_URL requerido")
        envelope = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<s:Body>{body}</s:Body>"
            "</s:Envelope>"
        )
        try:
            response = requests.post(
                self.endpoint_url,
                data=envelope.encode("utf-8"),
                headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": action},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BejermanSdkUnavailable(f"Error HTTP Bejerman: {exc}") from exc
        try:
            parsed = parse_soap_response(response.text)
        except BejermanSdkUnavailable as exc:
            if not response.ok:
                detail = _response_snippet(response.text) or response.reason or "sin detalle SOAP"
                raise BejermanSdkUnavailable(f"Error HTTP Bejerman {response.status_code}: {detail}") from exc
            raise
        if not response.ok:
            error = (
                _clean(parsed.get("Fault"))
                or _clean(parsed.get("ErrorMsg"))
                or _clean(parsed.get("Resultado"))
                or response.reason
                or "sin detalle SOAP"
            )
            raise BejermanSdkResponseError(f"HTTP {response.status_code} Bejerman: {error}")
        if not is_ok_response(parsed):
            error = parsed.get("ErrorMsg") or parsed.get("Fault") or parsed.get("Resultado") or "Respuesta no OK de Bejerman"
            raise BejermanSdkResponseError(error)
        return parsed

    def register(self) -> str:
        body = (
            '<EFlexSDK_WSRegistro xmlns="http://localhost:57213/">'
            f"<xUsuario>{escape(_clean(_setting('BEJERMAN_USER')))}</xUsuario>"
            f"<xClave>{escape(_clean(_setting('BEJERMAN_PASSWORD')))}</xClave>"
            f"<xCodEmpresa>{escape(self.company)}</xCodEmpresa>"
            f"<xPtoTrabajo>{escape(_clean(_setting('BEJERMAN_WORKSTATION')))}</xPtoTrabajo>"
            f"<xCodSucursal>{escape(_clean(_setting('BEJERMAN_BRANCH')))}</xCodSucursal>"
            "</EFlexSDK_WSRegistro>"
        )
        response = self._post(REGISTRO_ACTION, body)
        token = _clean(response.get("Token"))
        if not token:
            raise BejermanSdkResponseError("Bejerman no devolvió token")
        self.token = token
        return token

    def execute(self, circuito: str, operacion: str, *, params: list[Any] | None = None, params_json: Any = None) -> dict[str, str]:
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
        return self._post(EJECUTAR_ACTION, body)

    def list_comprobantes_ventas(self, filters: list[dict[str, Any]], en_mon_ext: str = "N") -> dict[str, str]:
        return self.execute("VENTAS", "WSListarComprobantesJSON", params_json=[_json_param(filters), en_mon_ext])

    def detalle_comprobante_ventas(self, comprobante_id: str | int) -> dict[str, str]:
        return self.execute("VENTAS", "DetalleComprobanteJSON", params_json=[str(comprobante_id)])

    def consultar_comprobante_ventas_pdf(self, reference: BejermanPdfReference) -> dict[str, str]:
        return self.execute(
            "VENTAS",
            "WS_ConsultarComprobantePDF",
            params_json=[
                reference.type,
                reference.number,
                reference.letter,
                reference.point_of_sale,
                reference.issue_date,
            ],
        )

    def generar_comprobante_ventas_pdf(self, reference: BejermanPdfReference) -> dict[str, str]:
        return self.execute(
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
        query = ""
        for item in filters or []:
            if isinstance(item, dict) and as_string(item.get("Valor")):
                query = as_string(item.get("Valor"))
                break
        return self.execute("TABLAS", "ObtenerArticulos", params=[query] if query else [])

    def list_clientes(self) -> dict[str, str]:
        return self.execute("TABLAS", "ObtenerClientes", params_json=[])

    def list_proveedores(self) -> dict[str, str]:
        return self.execute("TABLAS", "ObtenerProveedores", params_json=[])

    def list_comprobantes_compras(self, filters: list[dict[str, Any]], en_mon_ext: str = "N") -> dict[str, str]:
        return self.execute("COMPRAS", "WSListarComprobantesJSON", params_json=[_json_param(filters), en_mon_ext])

    def detalle_comprobante_compras(self, comprobante_id: str | int) -> dict[str, str]:
        return self.execute("COMPRAS", "DetalleComprobanteJSON", params_json=[str(comprobante_id)])

    def ingresar_comprobante_ventas_json(
        self,
        comprobante: dict[str, Any],
        *,
        circuito: str = "VENTAS",
        operacion: str = "IngresarComprobanteJSON",
        numera_flex: str = "S",
        emite_reg: str = "E",
    ) -> dict[str, str]:
        return self.execute(circuito, operacion, params_json=[_json_param([comprobante]), numera_flex, emite_reg])

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
        return self.execute("COMPRAS", "IngresarListaComprobantesJSON", params_json=params_json)

    def obtener_stock_partida(self, partida: str) -> dict[str, str]:
        return self.execute("STOCK", "ObtenerStockPartida", params=[partida])


def validate_sdk_config() -> None:
    missing = [
        name
        for name in ("BEJERMAN_WSDL_URL", "BEJERMAN_USER", "BEJERMAN_PASSWORD", "BEJERMAN_WORKSTATION")
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
) -> list[dict[str, Any]]:
    code = "" if customer_code is None else str(customer_code)
    filters = []
    if code.strip():
        filters.append(bejerman_filter("Cliente_Codigo", "IGUAL", code))
    tipo = _clean(comprobante_type).upper()
    if tipo:
        filters.append(bejerman_filter("Comprobante_Tipo", "IGUAL", tipo))
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
    return {"id": code or name, "code": code, "name": name or code, "description": public_description or name or code, "raw": record}


def build_articles_result(responses: list[dict[str, Any]] | dict[str, Any], query: dict[str, Any] | None = None) -> dict[str, Any]:
    query = query or {}
    response_list = responses if isinstance(responses, list) else [responses]
    mapped = [article for response in response_list for article in (map_article(record) for record in records_from_response(response)) if article]
    unique: dict[str, dict[str, Any]] = {}
    for item in mapped:
        key = normalize_search(item.get("code") or item.get("name") or item.get("id"))
        unique.setdefault(key, item)
    search = normalize_search(query.get("search"))
    items = list(unique.values())
    if search:
        items = [item for item in items if search in normalize_search(f"{item.get('code')} {item.get('name')} {item.get('description')}")]
    try:
        limit = max(1, min(50, int(query.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    return {"items": items[:limit], "pagination": create_pagination(len(items), 1, limit)}


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


def operation_label(record: dict[str, Any]) -> str:
    code = as_string(first_value(record, ("Comprobante_TipoOperacion", "TipoOperacion", "Tipo_Operacion"))).upper()
    return TIPO_OPERACION_LABELS.get(code, "Sin tipo de operación")


def is_billing_record(record: dict[str, Any]) -> bool:
    tipo = as_string(first_value(record, ("Comprobante_Tipo", "TipoComprobante", "Tipo"))).upper()
    return tipo in BILLING_COMPROBANTE_TYPES


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
        if data:
            return data
    return None


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(_setting(name, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def fetch_comprobante_pdf(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    *,
    retry_attempts: int | None = None,
    retry_delay_ms: int | None = None,
) -> tuple[bytes, str]:
    attempts = retry_attempts if retry_attempts is not None else _positive_int_setting("BEJERMAN_PDF_RETRY_ATTEMPTS", 3)
    attempts = max(1, int(attempts or 1))
    delay_ms = retry_delay_ms if retry_delay_ms is not None else _positive_int_setting("BEJERMAN_PDF_RETRY_DELAY_MS", 700)
    delay_ms = max(0, int(delay_ms or 0))
    try:
        response = client.consultar_comprobante_ventas_pdf(reference)
        pdf = extract_pdf_bytes(response)
        if pdf:
            return pdf, "application/pdf"
    except BejermanSdkResponseError:
        pass
    client.generar_comprobante_ventas_pdf(reference)
    for attempt in range(attempts):
        if attempt > 0 and delay_ms:
            time.sleep(delay_ms / 1000)
        try:
            response = client.consultar_comprobante_ventas_pdf(reference)
        except BejermanSdkResponseError:
            continue
        pdf = extract_pdf_bytes(response)
        if pdf:
            return pdf, "application/pdf"
    raise BejermanPdfPendingError(retry_after_ms=max(delay_ms, 1000))


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


def build_customer_document_fields(response: dict[str, Any], customer_code: str) -> dict[str, str] | None:
    record = find_bejerman_client_record(response, customer_code)
    if not record:
        return None
    fields = {
        "Cliente_RazonSocial": as_string(first_value(record, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Nombre", "Cliente"))),
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
    return fields


def equipment_label(equipment: dict[str, Any]) -> str:
    model_text = " ".join(part for part in (as_string(equipment.get("model")), as_string(equipment.get("variant"))) if part)
    return " | ".join(part for part in (as_string(equipment.get("equipmentType")), as_string(equipment.get("brand")), model_text) if part) or as_string(equipment.get("articleName")) or "Equipo"


SERVICE_RELEASE_RESOLUTION_LABELS = {
    "controlado sin defecto": "controlado sin defecto",
    "controlado_sin_defecto": "controlado sin defecto",
    "no reparado": "sin reparar",
    "no_reparado": "sin reparar",
    "sin reparar": "sin reparar",
    "sin_reparar": "sin reparar",
    "reparado": "reparado",
}


def service_release_resolution_label(value: Any) -> str:
    raw = as_string(value)
    normalized = normalize_search(raw).replace("-", " ").replace("_", " ")
    compact = normalized.replace(" ", "_")
    return SERVICE_RELEASE_RESOLUTION_LABELS.get(compact) or SERVICE_RELEASE_RESOLUTION_LABELS.get(normalized) or "reparado"


def service_release_legend(order: dict[str, Any]) -> str:
    resolution = (
        order.get("serviceResolution")
        or order.get("rawPedido")
        or order.get("resolution")
        or order.get("resolucion")
    )
    return f"Se entrega {service_release_resolution_label(resolution)}"


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
    tipo = as_string(_setting("BEJERMAN_RIS_TYPE", "RIS")) or "RIS"
    letter = as_string(_setting("BEJERMAN_RIS_LETTER", "R")) or "R"
    point = as_string(_setting("BEJERMAN_RIS_POINT_OF_SALE", "00004")) or "00004"
    operation = as_string(_setting("BEJERMAN_RIS_SERVICE_OPERATION", "REP")) or "REP"
    deposit = as_string(_setting("BEJERMAN_RIS_DEPOSIT", "STR")) or "STR"
    update_stock = as_bool(_setting("BEJERMAN_RIS_UPDATE_STOCK", False))
    serial = as_string(equipment.get("serial"))
    legend = " - ".join(
        part
        for part in (
            "Ingreso a servicio técnico",
            f"OS {payload.get('ingresoId')}" if payload.get("ingresoId") else "",
            equipment_label(equipment),
            f"Serie {serial}" if serial else "",
            f"Interno {as_string(equipment.get('internalNumber'))}" if as_string(equipment.get("internalNumber")) else "",
            f"Motivo {as_string(equipment.get('repairReason'))}" if as_string(equipment.get("repairReason")) else "",
        )
        if part
    )
    items = [
        legend_line(item_common(tipo, letter, point, issue_date, payload.get("customerCode"), 1), legend),
        {
            **item_common(tipo, letter, point, issue_date, payload.get("customerCode"), 2),
            "Item_Tipo": "A",
            "Item_CodigoArticulo": article_code,
            "Item_CantidadUM1": 1,
            "Item_CantidadUM2": 1 if update_stock and serial else 0,
            "Item_DescripArticulo": (as_string(equipment.get("articleName")) or as_string(_setting("BEJERMAN_RIS_GENERIC_ARTICLE_NAME", "Equipo recibido para servicio técnico")))[:250],
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
            "Item_Partida": serial if update_stock and serial else " ",
            "Item_TasaDescPorItem": 0,
            "Item_Importe": 0,
            "Item_FechaEntrega": issue_date,
            "Item_Kit": None,
            "Item_RenglonKit": 0,
            "Item_EsPromocion": None,
            "Item_DatosAdicionales": None,
        },
    ]
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
            **{key: value for key, value in customer_fields.items() if key != "Cliente_RazonSocial" and value},
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
        "profile": {"type": tipo, "operation": operation, "deposit": deposit, "pointOfSale": point},
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


def _positive_quantity(value: Any) -> float:
    parsed = as_number(value)
    return parsed if parsed and parsed > 0 else 1


def _amount(value: Any) -> float:
    parsed = as_number(value)
    return parsed if parsed is not None else 0


def _profile_for_delivery_type(delivery_type: str, config: dict[str, Any]) -> dict[str, str]:
    if delivery_type == "rental":
        return {"type": config["rentalType"], "operation": config["rentalOperation"], "deposit": config["rentalDeposit"], "pointOfSale": config["rentalPointOfSale"]}
    if delivery_type == "service_release":
        return {"type": config["serviceType"], "operation": config["serviceOperation"], "deposit": config["serviceDeposit"], "pointOfSale": config["servicePointOfSale"]}
    return {"type": config["saleType"], "operation": config["saleOperation"], "deposit": config["saleDeposit"], "pointOfSale": config["salePointOfSale"]}


def delivery_remito_config(default_profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "letter": as_string(_setting("BEJERMAN_REMITO_LETTER", "R")) or "R",
        "priceListCode": as_string(_setting("BEJERMAN_REMITO_PRICE_LIST", "GN")) or "GN",
        "currencyCode": as_string(_setting("BEJERMAN_REMITO_CURRENCY", "")),
        "exchangeTypeCode": as_string(_setting("BEJERMAN_REMITO_EXCHANGE_TYPE", "")),
        "exchangeRate": float(_setting("BEJERMAN_REMITO_EXCHANGE_RATE", 0) or 0),
        "saleType": as_string(_setting("BEJERMAN_REMITO_SALE_TYPE", default_profile.get("type", "RT"))),
        "saleOperation": as_string(_setting("BEJERMAN_REMITO_SALE_OPERATION", default_profile.get("operation", "MC"))),
        "saleDeposit": as_string(_setting("BEJERMAN_REMITO_SALE_DEPOSIT", default_profile.get("deposit", "VAL"))),
        "salePointOfSale": as_string(_setting("BEJERMAN_REMITO_SALE_POINT_OF_SALE", default_profile.get("pointOfSale", "00002"))),
        "rentalType": as_string(_setting("BEJERMAN_REMITO_RENTAL_TYPE", "RTA")),
        "rentalOperation": as_string(_setting("BEJERMAN_REMITO_RENTAL_OPERATION", "ALQ")),
        "rentalDeposit": as_string(_setting("BEJERMAN_REMITO_RENTAL_DEPOSIT", "STL")),
        "rentalPointOfSale": as_string(_setting("BEJERMAN_REMITO_RENTAL_POINT_OF_SALE", "00001")),
        "serviceType": as_string(_setting("BEJERMAN_REMITO_SERVICE_TYPE", "RSS")),
        "serviceOperation": as_string(_setting("BEJERMAN_REMITO_SERVICE_OPERATION", "REP")),
        "serviceDeposit": as_string(_setting("BEJERMAN_REMITO_SERVICE_DEPOSIT", "STC")),
        "servicePointOfSale": as_string(_setting("BEJERMAN_REMITO_SERVICE_POINT_OF_SALE", "00004")),
    }


def build_delivery_remito_comprobante(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
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
    items: list[dict[str, Any]] = []
    sort_order = 1
    total = 0.0
    for order in orders:
        if order.get("deliveryType") == "service_release":
            legend = service_release_legend(order)
        else:
            legend = " - ".join(
                part
                for part in (
                    f"Orden {order.get('orderNumber')}",
                    f"Ref {order.get('sourceReference')}" if order.get("sourceReference") else "",
                    f"Equipo {order.get('equipmentModel')}" if order.get("equipmentModel") else "",
                    f"Serie {order.get('equipmentSerial')}" if order.get("equipmentSerial") else "",
                )
                if part
            )
        items.append(legend_line(item_common(profile["type"], config["letter"], profile["pointOfSale"], issue_date, request.get("customerCode"), sort_order), legend))
        sort_order += 1
        for item in order.get("items") or []:
            article_code = as_string(item.get("articleCode"))
            quantity = _positive_quantity(item.get("quantity"))
            unit_price = _amount(item.get("unitPrice"))
            partida = as_string(item.get("partida") or item.get("equipmentSerial") or order.get("equipmentSerial"))
            article_name = as_string(item.get("articleName")) or as_string(item.get("description")) or article_code
            line_defs: list[tuple[float, str]] = []
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
                        )
                    )
            if not line_defs:
                line_defs = [(quantity, partida)]
            for line_quantity, line_partida in line_defs:
                amount = line_quantity * unit_price
                total += amount
                items.append(
                    {
                        **item_common(profile["type"], config["letter"], profile["pointOfSale"], issue_date, request.get("customerCode"), sort_order),
                        "Item_Tipo": "A" if article_code else "L",
                        "Item_CodigoArticulo": article_code or None,
                        "Item_CantidadUM1": line_quantity if article_code else 0,
                        "Item_CantidadUM2": line_quantity if article_code and line_partida else 0,
                        "Item_DescripArticulo": article_name[:250],
                        "Item_PrecioUnitario": unit_price,
                        "Item_TasaIVAInscrip": 0,
                        "Item_TasaIVANoInscrip": 0,
                        "Item_ImporteIVAInscrip": 0,
                        "Item_ImporteIVANoInscrip": 0,
                        "Item_ImporteTotal": amount,
                        "Item_ImporteDescComercial": 0,
                        "Item_ImporteDescFinanciero": 0,
                        "Item_ImporteDescGeneral": 0,
                        "Item_CodigoConceptoNoGravado": None,
                        "Item_ImporteIVANoGravado": 0,
                        "Item_TipoIVA": "1",
                        "Item_CodigoDescPorLinea": None,
                        "Item_ImporteDescPorLinea": 0,
                        "Item_Deposito": profile["deposit"] if article_code else None,
                        "Item_Partida": line_partida or " ",
                        "Item_TasaDescPorItem": 0,
                        "Item_Importe": amount,
                        "Item_FechaEntrega": issue_date,
                        "Item_Kit": None,
                        "Item_RenglonKit": 0,
                        "Item_EsPromocion": None,
                        "Item_DatosAdicionales": None,
                    }
                )
                sort_order += 1
    notes = "\n".join(part for part in (as_string(request.get("notes")), f"NEXORA {request.get('groupId')}", f"Órdenes {', '.join(as_string(order.get('orderNumber')) for order in orders)}") if part)
    return {
        "comprobante": {
            "Comprobante_Tipo": profile["type"],
            "Comprobante_Letra": config["letter"],
            "Comprobante_PtoVenta": profile["pointOfSale"],
            "Comprobante_Numero": "",
            "Comprobante_LoteHasta": "",
            "Comprobante_FechaEmision": issue_date,
            "Cliente_Codigo": request.get("customerCode"),
            "Cliente_RazonSocial": request.get("customerName"),
            "Vendedor_Codigo": seller,
            "Comprobante_CondVenta": request.get("paymentTermCode"),
            "Comprobante_FechaVencimiento": issue_date,
            "Comprobante_ImporteTotal": total,
            "Comprobante_Mensaje": notes[:250] or " ",
            "Comprobante_ActualizaStock": "S",
            "Comprobante_ListaPrecios": config["priceListCode"],
            "Comprobante_Moneda": config["currencyCode"],
            "Comprobante_TipoCambio": config["exchangeTypeCode"],
            "Comprobante_CotizacionCambio": config["exchangeRate"],
            "Comprobante_FechaContabilizacion": issue_date,
            "Comprobante_FechaDDJJ": issue_date,
            "Comprobante_TipoOperacion": profile["operation"],
            "Comprobante_Items": items,
            "Comprobante_MediosPago": [],
            "Comprobante_RegEspeciales": [],
            "Comprobante_DatosAdicionales": None,
            "Comprobante_Cuotas": None,
        },
        "profile": profile,
        "lineCount": len(items),
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
        return {"found": False, "serial": serial, "normalizedSeria