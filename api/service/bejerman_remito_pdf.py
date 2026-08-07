"""Genera el PDF de un remito replicando el que emite Bejerman.

Por qué existe: Bejerman es el maestro de la numeración de remitos, así que el
comprobante **tiene** que emitirse allá. Pero recuperar el PDF depende de servicios
del SDK que en esta instalación no responden (ver `RegenPDFSDK`). Como del PDF sólo
nos importa que refleje lo que Bejerman registró, NEXORA lo reproduce a partir de la
respuesta de emisión.

El PDF de Bejerman es un **overlay sobre papel preimpreso**: no lleva logo, ni
líneas, ni el número de remito (todo eso viene impreso en el formulario). Es sólo
texto monoespaciado en posiciones fijas, lo que lo hace reproducible exactamente.

## De dónde salen estos números

Medidos con PyMuPDF sobre 28 remitos reales de `C:\\2. SEPID\\PDF\\REMITOS` y
`C:\\3. MGBIO\\PDF\\REMITOS`, usando la línea base (`origin`) y no el bbox, que varía
según los ascendentes del texto.

Conviven dos *productores* de PDF, que se distinguen por los metadatos:

* `Crystal Reports` / `Powered By Crystal` — el que genera el SDK
  (`WS_GenerarComprobantePDF`), o sea **el camino que NEXORA reemplaza**. Cuerpos
  9.65 / 9.35 / 7.5 y es el único que imprime el pie `VALOR DECLARADO:`.
* `e-Flexware` / `novaPDF SDK` — la impresión desde el cliente de escritorio de
  Bejerman. Cuerpos 11.03 / 9.95 / 8.03 y sin pie.

**Las columnas caen en la misma x en los dos** (42.35 vs 42.36, 144.40 vs 144.48,
331.50 vs 331.56...), así que el calce sobre el papel no depende del productor: la
diferencia es sólo el cuerpo de la tipografía. Reproducimos el de Crystal, que es el
que NEXORA venía entregando y el único que trae el valor declarado.

Lo que sí cambia de verdad es la **empresa**: el bloque de renglones de MGBIO está
22.64 pt a la izquierda del de SEPID (el encabezado, en cambio, es idéntico). Son dos
formularios preimpresos distintos, por eso hay una plantilla por empresa.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from io import BytesIO
from typing import Any

from django.conf import settings
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


logger = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = letter  # 612 x 792 pt, igual que los de Bejerman

# Courier New y la Courier de ReportLab comparten el avance de 0.6 em, así que cada
# carácter cae exactamente en la misma posición. Si se apunta
# BEJERMAN_REMITO_PDF_FONT_DIR a un directorio con cour.ttf/courbd.ttf se embeben las
# tipografías reales y coinciden también los glifos.
FONT_REGULAR = "Courier"
FONT_BOLD = "Courier-Bold"
CHAR_WIDTH_EM = 0.6
_EMBEDDED_FONTS_READY: bool | None = None


def _resolve_fonts() -> tuple[str, str]:
    global _EMBEDDED_FONTS_READY
    if _EMBEDDED_FONTS_READY is False:
        return FONT_REGULAR, FONT_BOLD
    if _EMBEDDED_FONTS_READY is True:
        return "CourierNew", "CourierNew-Bold"

    directory = str(getattr(settings, "BEJERMAN_REMITO_PDF_FONT_DIR", "") or "").strip()
    if not directory:
        _EMBEDDED_FONTS_READY = False
        return FONT_REGULAR, FONT_BOLD
    try:
        pdfmetrics.registerFont(TTFont("CourierNew", os.path.join(directory, "cour.ttf")))
        pdfmetrics.registerFont(TTFont("CourierNew-Bold", os.path.join(directory, "courbd.ttf")))
        _EMBEDDED_FONTS_READY = True
        return "CourierNew", "CourierNew-Bold"
    except Exception:
        logger.warning("bejerman_remito_pdf_font_fallback", extra={"font_dir": directory})
        _EMBEDDED_FONTS_READY = False
        return FONT_REGULAR, FONT_BOLD


@dataclass(frozen=True)
class RemitoPdfTemplate:
    """Coordenadas en puntos, con origen arriba a la izquierda (como PyMuPDF).

    `render_remito_pdf` las convierte al sistema de ReportLab (origen abajo).
    """

    # Fecha, arriba a la derecha, en negrita.
    date_x: float = 460.90
    date_baseline: float = 43.20
    date_size: float = 9.65

    # Bloque de cabecera del cliente. Es idéntico en las dos empresas.
    header_label_x: float = 48.00
    header_value_x: float = 116.05
    header_localidad_x: float = 147.25
    header_cuit_label_x: float = 388.20
    header_cuit_value_x: float = 422.20
    header_first_baseline: float = 104.85
    header_pitch: float = 14.17
    header_size: float = 9.35

    # Renglones. `items_row_pitch` es el salto entre renglones del formulario y
    # `items_wrap_pitch` el de una descripción que sigue abajo dentro del mismo
    # renglón. Ojo: no es exactamente la mitad (17.00 contra 8.52), así que se
    # miden por separado en vez de derivar uno del otro.
    items_first_baseline: float = 202.00
    items_row_pitch: float = 17.00
    items_wrap_pitch: float = 8.60
    items_size: float = 7.50

    # Columnas del formulario preimpreso:
    #   CÓDIGO | CANT. | DESCRIPCIÓN | PM | LOTE/Nº SERIE | VTO. | OBSERVACIONES
    # CÓDIGO, DESCRIPCIÓN y PM van a la izquierda; CANT., LOTE/Nº SERIE y VTO.
    # van CENTRADAS. Verificado con valores de distinto largo en el mismo remito:
    # los seriales de 6 a 13 caracteres centran todos en 412.30, y la cantidad
    # '-1,00' centra en el mismo punto que ' 1,00'.
    col_codigo_x: float = 42.35
    col_cantidad_center_x: float = 118.90
    col_descripcion_x: float = 144.40
    col_pm_x: float = 331.50
    col_serie_center_x: float = 412.30
    col_vto_center_x: float = 484.60

    # Bejerman corta la descripción a lo ancho de la columna, permitiendo cortar
    # después de un guion (por eso "...Konsung KSOC-" / "10").
    descripcion_max_chars: int = 37

    # Renglones útiles del formulario. El armado de remitos ya parte los envíos
    # largos antes de llegar acá; esto es la red de contención.
    max_rows: int = 22

    # Pie: "VALOR DECLARADO:" con el importe alineado a la derecha.
    footer_label_x: float = 363.75
    footer_label_baseline: float = 623.10
    footer_amount_right_x: float = 569.40
    footer_amount_baseline: float = 621.45
    footer_size: float = 9.35


# El encabezado es igual en las dos empresas; lo que se corre es el bloque de
# renglones. Medido columna por columna: 22.67 / 22.63 / 22.72 / 22.62 / 22.57 /
# 22.63 -> se aplica el desplazamiento medido en cada una, no un promedio.
TEMPLATE_SEPID = RemitoPdfTemplate()
TEMPLATE_MGBIO = replace(
    TEMPLATE_SEPID,
    col_codigo_x=19.68,
    col_cantidad_center_x=96.27,
    col_descripcion_x=121.68,
    col_pm_x=308.88,
    col_serie_center_x=389.73,
    col_vto_center_x=461.97,
)

TEMPLATES: dict[str, RemitoPdfTemplate] = {
    "SEPID": TEMPLATE_SEPID,
    "MGBIO": TEMPLATE_MGBIO,
}
DEFAULT_TEMPLATE = TEMPLATE_SEPID

# Cuando no viene la empresa explícita, el punto de venta la delata.
POINT_OF_SALE_COMPANIES = {"00004": "SEPID", "4": "SEPID", "00007": "MGBIO", "7": "MGBIO"}

SIT_IVA_LABELS = {
    "1": "Responsable Inscripto",
    "2": "Responsable No Inscripto",
    "3": "Responsable Inscripto",
    "4": "Exento",
    "5": "Consumidor Final",
    "6": "Monotributo",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def company_for(comprobante: dict[str, Any], company_key: Any = None) -> str:
    """Empresa dueña del talonario: explícita, o deducida del punto de venta."""

    explicit = _clean(company_key).upper()
    if explicit in TEMPLATES:
        return explicit
    for field in ("Comprobante_Empresa", "Empresa", "company_key", "companyKey"):
        candidate = _clean(comprobante.get(field)).upper()
        if candidate in TEMPLATES:
            return candidate
    punto = _clean(comprobante.get("Comprobante_PtoVenta"))
    return POINT_OF_SALE_COMPANIES.get(punto, POINT_OF_SALE_COMPANIES.get(punto.lstrip("0"), "SEPID"))


def template_for(company_key: Any = None, comprobante: dict[str, Any] | None = None) -> RemitoPdfTemplate:
    return TEMPLATES.get(company_for(comprobante or {}, company_key), DEFAULT_TEMPLATE)


def _first_value(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and _clean(record.get(key)):
            return record.get(key)
    return None


def _parse_bejerman_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    raw = _clean(value)
    if not raw:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", raw)
    if match:
        try:
            return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
    return None


def _format_quantity(value: Any) -> str:
    """Cantidad como la escribe Bejerman: dos decimales con coma, sin separador de miles."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    texto = f"{number:.2f}".replace(".", ",")
    # Bejerman reserva la posición del signo: los positivos llevan un espacio
    # adelante, donde los negativos llevan el "-". Por eso ' 1,00' y '-1,00'
    # centran exactamente en el mismo punto.
    return texto if texto.startswith("-") else f" {texto}"


def _format_amount(value: Any) -> str:
    """Importe con separador de miles, como el pie de los remitos de Bejerman."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    texto = f"{number:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return texto if texto.startswith("-") else f" {texto}"


def _format_expiration(value: Any) -> str:
    """VTO. en dd/mm/aaaa, que es como lo imprime Bejerman ('01/03/2029')."""

    parsed = _parse_bejerman_date(value)
    if parsed is not None:
        return parsed.strftime("%d/%m/%Y")
    # Si ya viene formateada desde Bejerman se respeta tal cual.
    return _clean(value)


def _sit_iva_label(comprobante: dict[str, Any]) -> str:
    explicit = _clean(comprobante.get("Cliente_DescripSitIVA"))
    if explicit:
        return explicit
    return SIT_IVA_LABELS.get(_clean(comprobante.get("Cliente_SitIVA")), "Consumidor Final")


# Claves con las que puede llegar cada dato. PM y VTO no vienen en la respuesta de
# emisión: los completa `bejerman_remito_pdf_data` antes de renderizar.
PM_KEYS = ("Item_PM", "Item_DescripReducida", "Item_ArticuloDescRedu", "Articulo_DescRedu", "art_DescRedu")
VTO_KEYS = (
    "Item_Vencimiento",
    "Item_FechaVencimiento",
    "Item_PartidaVencimiento",
    "Partida_FechaVencimiento",
    "partida_expiration_date",
)
VALOR_DECLARADO_KEYS = (
    "Comprobante_ValorDeclarado",
    "Comprobante_ImporteTotal",
    "Comprobante_Total",
    "ValorDeclarado",
)


@dataclass
class _Line:
    renglon: int
    codigo: str = ""
    cantidad: str = ""
    descripcion: str = ""
    pm: str = ""
    partida: str = ""
    vto: str = ""


def _lines_from_items(items: list[dict[str, Any]]) -> list[_Line]:
    """Un item de Bejerman = un renglón = una línea del PDF.

    `Item_NumeroRenglon` es el que manda: los huecos entre renglones son los que
    producen los saltos que se ven en los PDF reales.
    """

    lines: list[_Line] = []
    for index, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        try:
            renglon = int(item.get("Item_NumeroRenglon") or index)
        except (TypeError, ValueError):
            renglon = index
        descripcion = _clean(item.get("Item_DescripArticulo"))
        if _clean(item.get("Item_Tipo")).upper() == "L":
            # Renglón de leyenda: sólo texto, sin código ni cantidad.
            lines.append(_Line(renglon=renglon, descripcion=descripcion))
            continue
        lines.append(
            _Line(
                renglon=renglon,
                codigo=_clean(item.get("Item_CodigoArticulo")),
                cantidad=_format_quantity(item.get("Item_CantidadUM1")),
                descripcion=descripcion,
                pm=_clean(_first_value(item, PM_KEYS)),
                partida=_clean(item.get("Item_Partida")),
                vto=_format_expiration(_first_value(item, VTO_KEYS)),
            )
        )
    return lines


def _wrap_parrafo(texto: str, ancho: int) -> list[str]:
    """Corta un párrafo como Bejerman: por palabra, y también después de un guion.

    El corte tras el guion es el que produce "Concentrador de oxígeno Konsung KSOC-"
    seguido de "10" en los remitos reales; sin él, un word-wrap común dejaría
    "KSOC-10" entero en el renglón siguiente.
    """

    if len(texto) <= ancho:
        return [texto] if texto.strip() else []

    # Trozos indivisibles: cada uno termina en un espacio o en un guion, que son los
    # dos únicos lugares donde Bejerman parte.
    trozos = [t for t in re.split(r"(?<=[ -])", texto) if t]
    lineas: list[str] = []
    actual = ""
    for trozo in trozos:
        candidata = actual + trozo
        if len(candidata.rstrip()) <= ancho:
            actual = candidata
            continue
        if actual:
            lineas.append(actual)
            actual = ""
        while len(trozo) > ancho:  # una palabra sola más larga que la columna
            lineas.append(trozo[:ancho])
            trozo = trozo[ancho:]
        actual = trozo
    if actual.strip():
        lineas.append(actual)
    return lineas


def _wrap_descripcion(texto: str, ancho: int) -> list[str]:
    """Corta la descripción respetando primero los saltos de línea explícitos.

    Las descripciones de las órdenes de entrega las escriben los usuarios y a veces
    traen un salto propio; Bejerman lo respeta y corta ahí aunque todavía entre en
    la columna (se ve en el remito 0004-00004986 contra el 0007-00000728, que llevan
    el mismo artículo partido en lugares distintos).
    """

    limpio = (texto or "").rstrip()
    if not limpio:
        return []
    lineas: list[str] = []
    for parrafo in re.split(r"\r\n|\r|\n", limpio):
        lineas.extend(_wrap_parrafo(parrafo.rstrip(), ancho))
    return lineas


def render_remito_pdf(
    comprobante: dict[str, Any],
    *,
    company_key: Any = None,
    template: RemitoPdfTemplate | None = None,
) -> bytes:
    """Devuelve los bytes del PDF del remito a partir del comprobante de Bejerman.

    `comprobante` es el `DatosJSON` que devuelve `IngresarComprobanteJSON`, con PM y
    VTO ya completados (ver `bejerman_remito_pdf_data.enrich_comprobante_for_pdf`).
    """

    if not isinstance(comprobante, dict):
        raise ValueError("El comprobante de Bejerman debe ser un diccionario.")

    tpl = template or template_for(company_key, comprobante)
    regular, bold = _resolve_fonts()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.setTitle(
        f"{_clean(comprobante.get('Comprobante_Tipo'))} "
        f"{_clean(comprobante.get('Comprobante_Letra'))} "
        f"{_clean(comprobante.get('Comprobante_PtoVenta'))}-{_clean(comprobante.get('Comprobante_Numero'))}".strip()
    )

    def draw(x: float, baseline_from_top: float, text: str, size: float, font: str = "") -> None:
        if not text:
            return
        pdf.setFont(font or regular, size)
        pdf.drawString(x, PAGE_HEIGHT - baseline_from_top, text)

    def draw_centered(center_x: float, baseline_from_top: float, text: str, size: float) -> None:
        if not text:
            return
        pdf.setFont(regular, size)
        # No usamos drawCentredString: mide con las métricas de la fuente y acá el
        # centro tiene que salir del ancho monoespaciado, que es lo que Bejerman usa
        # (y lo que hace que ' 1,00' y '-1,00' caigan en el mismo lugar).
        pdf.drawString(center_x - len(text) * size * CHAR_WIDTH_EM / 2, PAGE_HEIGHT - baseline_from_top, text)

    def draw_right(right_x: float, baseline_from_top: float, text: str, size: float) -> None:
        if not text:
            return
        pdf.setFont(regular, size)
        pdf.drawString(right_x - len(text) * size * CHAR_WIDTH_EM, PAGE_HEIGHT - baseline_from_top, text)

    # Fecha (arriba a la derecha, negrita, dd/mm/aa)
    emitted = _parse_bejerman_date(comprobante.get("Comprobante_FechaEmision"))
    if emitted:
        draw(tpl.date_x, tpl.date_baseline, emitted.strftime("%d/%m/%y"), tpl.date_size, bold)

    # Cabecera del cliente
    y = tpl.header_first_baseline
    draw(tpl.header_label_x, y, "Señor/es:", tpl.header_size)
    draw(tpl.header_value_x, y, _clean(comprobante.get("Cliente_RazonSocial")), tpl.header_size)

    y += tpl.header_pitch
    draw(tpl.header_label_x, y, "Domicilio:", tpl.header_size)
    draw(
        tpl.header_value_x,
        y,
        _clean(comprobante.get("Cliente_Direccion") or comprobante.get("Cliente_Domicilio")),
        tpl.header_size,
    )

    y += tpl.header_pitch
    draw(tpl.header_label_x, y, "C.P./Localidad:", tpl.header_size)
    codigo_postal = _clean(comprobante.get("Cliente_CodigoPostal"))
    localidad = _clean(comprobante.get("Cliente_Localidad"))
    ubicacion = " - ".join(part for part in (codigo_postal, localidad) if part)
    draw(tpl.header_localidad_x, y, ubicacion, tpl.header_size)

    y += tpl.header_pitch
    draw(tpl.header_label_x, y, f"IVA : {_sit_iva_label(comprobante)}", tpl.header_size)
    documento = _clean(comprobante.get("Cliente_NroDocumento"))
    if documento:
        draw(tpl.header_cuit_label_x, y, "CUIT:", tpl.header_size)
        draw(tpl.header_cuit_value_x, y, documento, tpl.header_size)

    # Renglones
    for line in _lines_from_items(comprobante.get("Comprobante_Items") or []):
        if line.renglon < 1 or line.renglon > tpl.max_rows:
            continue
        baseline = tpl.items_first_baseline + (line.renglon - 1) * tpl.items_row_pitch
        draw(tpl.col_codigo_x, baseline, line.codigo, tpl.items_size)
        draw_centered(tpl.col_cantidad_center_x, baseline, line.cantidad, tpl.items_size)
        draw(tpl.col_pm_x, baseline, line.pm, tpl.items_size)
        draw_centered(tpl.col_serie_center_x, baseline, line.partida, tpl.items_size)
        draw_centered(tpl.col_vto_center_x, baseline, line.vto, tpl.items_size)
        for offset, trozo in enumerate(_wrap_descripcion(line.descripcion, tpl.descripcion_max_chars)):
            draw(tpl.col_descripcion_x, baseline + offset * tpl.items_wrap_pitch, trozo, tpl.items_size)

    # Pie: valor declarado
    valor = _format_amount(_first_value(comprobante, VALOR_DECLARADO_KEYS))
    if valor:
        draw(tpl.footer_label_x, tpl.footer_label_baseline, "VALOR DECLARADO:", tpl.footer_size)
        draw_right(tpl.footer_amount_right_x, tpl.footer_amount_baseline, valor, tpl.footer_size)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def render_alignment_sheet(company_key: str = "SEPID") -> bytes:
    """Hoja de calibración para imprimir sobre el formulario preimpreso.

    Sólo hace falta si se cambia de imprenta o de talonario: las posiciones vigentes
    salieron de medir los remitos reales, no de estimarlas. Dibuja una marca por
    campo en la posición exacta de la plantilla, más una regla de renglones
    numerados; puesta sobre el papel se ve al toque si hay que correr algo.
    """

    tpl = template_for(company_key)
    regular, bold = _resolve_fonts()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.setTitle(f"Calibración remito {company_key}")

    def draw(x: float, top: float, text: str, size: float, font: str = "") -> None:
        pdf.setFont(font or regular, size)
        pdf.drawString(x, PAGE_HEIGHT - top, text)

    def draw_centered(center_x: float, top: float, text: str, size: float) -> None:
        draw(center_x - len(text) * size * CHAR_WIDTH_EM / 2, top, text, size)

    draw(tpl.date_x, tpl.date_baseline, "99/99/99", tpl.date_size, bold)

    for indice, etiqueta in enumerate(("Señor/es:", "Domicilio:", "C.P./Localidad:", "IVA : ")):
        y = tpl.header_first_baseline + indice * tpl.header_pitch
        draw(tpl.header_label_x, y, etiqueta, tpl.header_size)
        draw(tpl.header_localidad_x if indice == 2 else tpl.header_value_x, y, "X" * 30, tpl.header_size)
    y_cuit = tpl.header_first_baseline + 3 * tpl.header_pitch
    draw(tpl.header_cuit_label_x, y_cuit, "CUIT:", tpl.header_size)
    draw(tpl.header_cuit_value_x, y_cuit, "99999999999", tpl.header_size)

    for indice in range(tpl.max_rows):
        y = tpl.items_first_baseline + indice * tpl.items_row_pitch
        draw(tpl.col_codigo_x, y, f"{indice + 1:02d}99999", tpl.items_size)
        draw_centered(tpl.col_cantidad_center_x, y, " 999,00", tpl.items_size)
        draw(tpl.col_descripcion_x, y, "X" * tpl.descripcion_max_chars, tpl.items_size)
        draw(tpl.col_pm_x, y, "9999-99", tpl.items_size)
        draw_centered(tpl.col_serie_center_x, y, "SERIE999999", tpl.items_size)
        draw_centered(tpl.col_vto_center_x, y, "99/99/9999", tpl.items_size)

    draw(tpl.footer_label_x, tpl.footer_label_baseline, "VALOR DECLARADO:", tpl.footer_size)
    monto = " 9.999.999,00"
    draw(tpl.footer_amount_right_x - len(monto) * tpl.footer_size * CHAR_WIDTH_EM,
         tpl.footer_amount_baseline, monto, tpl.footer_size)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
