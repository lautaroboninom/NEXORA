from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable
from urllib.parse import quote, urlencode


REGISTERED_REMITO_NO_PDF_CODE = "BEJERMAN_REMITO_REGISTERED_NO_PDF"
RIS_REGISTERED_NO_PDF_CODE = "RIS_REGISTERED_NO_PDF"
REGISTERED_NO_PDF_CODES = frozenset({REGISTERED_REMITO_NO_PDF_CODE, RIS_REGISTERED_NO_PDF_CODE})

REMITO_COMPROBANTE_TYPES = ("RT", "RD", "RTA", "RTN", "RSS", "RIS", "RDA", "RDN")


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def clean_upper(value: Any) -> str:
    return clean_text(value).upper()


def digits_only(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def format_bejerman_document_number(tipo: str, letter: str, point: str, number: str) -> str:
    return f"{clean_upper(tipo)} {clean_upper(letter)} {digits_only(point).zfill(5)}-{digits_only(number).zfill(8)}"


def registered_document_mode(value: Any) -> bool:
    return clean_text(value).lower() in {"register", "registered", "registrar", "registrado"}


def registered_remito_no_pdf_detail(remito_number: Any = "") -> str:
    number = clean_text(remito_number)
    if number:
        return (
            f"El remito {number} fue registrado manualmente en NEXORA, no emitido. "
            "Los remitos registrados no generan PDF de Bejerman para abrir o imprimir desde NEXORA."
        )
    return (
        "Este remito fue registrado manualmente en NEXORA, no emitido. "
        "Los remitos registrados no generan PDF de Bejerman para abrir o imprimir desde NEXORA."
    )


def is_registered_remito_summary(summary: Any) -> bool:
    if not isinstance(summary, dict) or not summary:
        return False
    return (
        registered_document_mode(summary.get("documentMode"))
        or registered_document_mode(summary.get("document_mode"))
        or registered_document_mode(summary.get("mode"))
        or bool(summary.get("manualRemitoNumber") and summary.get("existingBejermanRemito"))
        or bool(summary.get("manual_remito_number") and summary.get("existing_bejerman_remito"))
    )


def local_remito_pdf_metadata(group_id: str, company_key: str | None = None) -> dict[str, Any]:
    return {
        "companyKey": company_key or "",
        "remitoGroupId": group_id,
        "pdfUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/",
        "printUrl": f"/api/ordenes-entrega/remito-bejerman/{group_id}/print/",
        "source": "nexora",
    }


def cobranzas_remito_pdf_metadata(
    document_id: str,
    *,
    customer_code: str | None = None,
    company_key: str | None = None,
) -> dict[str, Any]:
    doc = clean_text(document_id)
    if not doc:
        return {}
    params = {
        key: value
        for key, value in {
            "customerCode": clean_text(customer_code),
            "companyKey": clean_text(company_key),
        }.items()
        if value
    }
    suffix = f"?{urlencode(params)}" if params else ""
    encoded = quote(doc, safe="")
    return {
        "pdfUrl": f"/api/cobranzas/remitos/{encoded}/pdf/{suffix}",
        "printUrl": f"/api/cobranzas/remitos/{encoded}/print/{suffix}",
    }


def retry_after_header_value(retry_after_ms: Any) -> str:
    try:
        ms = int(retry_after_ms or 0)
    except (TypeError, ValueError):
        ms = 0
    return str(max(1, (ms + 999) // 1000))


def normalize_lookup_text(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def is_transient_pdf_lookup_message(value: Any) -> bool:
    text = normalize_lookup_text(value)
    return (
        "remito_document_not_found" in text
        or "no se encontro el comprobante asociado" in text
        or "no se encontro el pdf del comprobante" in text
        or "no se encontro el archivo pdf" in text
        or "no se encontro en bejerman un remito" in text
    )


def parse_bejerman_remito_number(
    value: Any,
    *,
    default_type: str,
    default_letter: str = "R",
    allowed_types: tuple[str, ...] | set[str] | None = None,
    require_explicit_match: bool = False,
    validate_point: Callable[[str], None] | None = None,
    allow_number_only: bool = False,
    default_point: str = "",
    require_complete: bool = False,
    complete_error_message: str = "",
    explicit_mismatch_message: str = "",
) -> dict[str, str]:
    text = clean_text(value)
    if not text:
        raise ValueError("Debe cargar el número completo de remito")

    tipo = clean_upper(default_type) or "RIS"
    letter = clean_upper(default_letter) or "R"
    allowed = {clean_upper(item) for item in (allowed_types or []) if clean_text(item)}

    explicit = re.search(r"^\s*([A-Za-z]{2,4})(?:\s+([A-Za-z]))?\s+(\d{1,5})\s*[-/ ]\s*(\d{1,8})\s*$", text)
    if explicit:
        parsed_type = explicit.group(1).upper()
        parsed_letter = (explicit.group(2) or letter).upper()
        if require_explicit_match and (parsed_type != tipo or parsed_letter != letter):
            raise ValueError(explicit_mismatch_message or f"El remito debe corresponder a {tipo} {letter}")
        if allowed and parsed_type not in allowed:
            raise ValueError(f"Tipo de remito no válido: {parsed_type}")
        point = explicit.group(3).zfill(5)
        if validate_point:
            validate_point(point)
        number = explicit.group(4).zfill(8)
        return {
            "type": parsed_type if not require_explicit_match else tipo,
            "letter": parsed_letter if not require_explicit_match else letter,
            "point": point,
            "number": number,
            "remitoNumber": format_bejerman_document_number(
                parsed_type if not require_explicit_match else tipo,
                parsed_letter if not require_explicit_match else letter,
                point,
                number,
            ),
        }

    point_and_number = re.search(r"^\s*(\d{1,5})\s*[-/ ]\s*(\d{1,8})\s*$", text)
    if point_and_number:
        point = point_and_number.group(1).zfill(5)
        if validate_point:
            validate_point(point)
        number = point_and_number.group(2).zfill(8)
        return {
            "type": tipo,
            "letter": letter,
            "point": point,
            "number": number,
            "remitoNumber": format_bejerman_document_number(tipo, letter, point, number),
        }

    if require_complete or not allow_number_only:
        raise ValueError(
            complete_error_message
            or f"Cargue el remito completo, por ejemplo {tipo} {letter} 00004-00004715."
        )

    match = re.search(r"(\d+)\s*$", text)
    if not match:
        raise ValueError("Número de remito inválido")
    number = match.group(1)
    if len(number) > 8:
        raise ValueError("El número de remito debe tener hasta 8 dígitos")
    point = (digits_only(default_point) or "0").zfill(5)
    if validate_point:
        validate_point(point)
    return {
        "type": tipo,
        "letter": letter,
        "point": point,
        "number": number.zfill(8),
        "remitoNumber": format_bejerman_document_number(tipo, letter, point, number),
    }
