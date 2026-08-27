from __future__ import annotations

import os
from datetime import date, datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas


_ITEM_RESULT_LABELS = {
    "ok": "OK",
    "observado": "Observado",
    "no_ok": "No OK",
    "na": "N/A",
}

_GLOBAL_RESULT_LABELS = {
    "pendiente": "Pendiente",
    "apto": "Apto",
    "apto_condicional": "Apto condicional",
    "no_apto": "No apto",
}

# Paleta pensada para impresión en blanco y negro: sólo negro, grises claros
# y filetes finos.  Nada de color que se pierda en una fotocopia.
_INK = colors.HexColor("#111418")
_MUTED = colors.HexColor("#5f6874")
_RULE = colors.HexColor("#b9c0ca")
_HAIRLINE = colors.HexColor("#dde1e7")
_SOFT = colors.HexColor("#eef1f5")
_ZEBRA = colors.HexColor("#f7f8fa")
_CHIP = colors.HexColor("#dfe3e9")

_LEGEND = (
    "Los resultados de este informe corresponden exclusivamente al equipo identificado y a las condiciones "
    "verificadas en la fecha de ejecución indicada. Las mediciones se contrastaron contra los instrumentos "
    "declarados y las normas de referencia listadas."
)


def _safe_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_local(value):
    """Pasa la fecha a la zona horaria del sistema (la base guarda en UTC)."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        try:
            from django.utils import timezone

            return timezone.localtime(value)
        except Exception:
            return value
    return value


def _fmt_dt(value) -> str:
    """Fecha en formato local (dd/mm/aaaa hh:mm)."""
    value = _to_local(value)
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    s = _safe_text(value)
    if not s:
        return ""
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return parsed.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return s.replace("T", " ")[:16]


def _split_long_token(token: str, font: str, size: float, max_width: float) -> list[str]:
    if not token:
        return [""]
    out: list[str] = []
    cur = ""
    for ch in token:
        trial = f"{cur}{ch}"
        if pdfmetrics.stringWidth(trial, font, size) <= max_width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out or [token]


def _wrap_text(text: str, font: str, size: float, max_width: float) -> list[str]:
    s = _safe_text(text)
    if not s:
        return [""]
    lines: list[str] = []
    for raw_par in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        words = raw_par.split()
        if not words:
            lines.append("")
            continue
        cur = ""
        for word in words:
            trial = word if not cur else f"{cur} {word}"
            if pdfmetrics.stringWidth(trial, font, size) <= max_width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if pdfmetrics.stringWidth(word, font, size) <= max_width:
                cur = word
            else:
                chunks = _split_long_token(word, font, size, max_width)
                lines.extend(chunks[:-1])
                cur = chunks[-1]
        if cur:
            lines.append(cur)
    return lines or [""]


def _fit_text(text: str, font: str, size: float, max_width: float, min_size: float = 6.2) -> tuple[str, float]:
    """Achica el cuerpo hasta ``min_size`` y, si aún no entra, recorta con puntos suspensivos."""
    s = _safe_text(text) or "-"
    cur = size
    while cur > min_size and pdfmetrics.stringWidth(s, font, cur) > max_width:
        cur -= 0.25
    if pdfmetrics.stringWidth(s, font, cur) <= max_width:
        return s, cur
    ell = "..."
    out = s
    while out and pdfmetrics.stringWidth(out + ell, font, cur) > max_width:
        out = out[:-1]
    return (out + ell) if out else s[:1], cur


def _resolve_logo_path(empresa: str) -> str | None:
    """Logo de la empresa reutilizando la resolución del resto de los PDF."""
    try:
        from .pdf import _logo_path_for_company

        path = _logo_path_for_company(_safe_text(empresa).upper())
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    for cand in (os.environ.get("LOGO_PATH"), "/app/staticfiles/branding/logo-app.png"):
        try:
            if cand and os.path.exists(cand):
                return cand
        except Exception:
            continue
    return None


def _count_results(report: dict) -> dict[str, int]:
    counts = {"total": 0, "ok": 0, "observado": 0, "no_ok": 0, "na": 0, "sin_dato": 0}
    for section in report.get("sections") or []:
        for item in section.get("items") or []:
            counts["total"] += 1
            key = _safe_text((item.get("value") or {}).get("result")).lower()
            if key in counts:
                counts[key] += 1
            else:
                counts["sin_dato"] += 1
    return counts


def _counts_caption(counts: dict[str, int]) -> str:
    parts = []
    if counts["ok"]:
        parts.append(f"{counts['ok']} OK")
    if counts["observado"]:
        parts.append(f"{counts['observado']} obs.")
    if counts["no_ok"]:
        parts.append(f"{counts['no_ok']} no OK")
    if counts["na"]:
        parts.append(f"{counts['na']} N/A")
    if counts["sin_dato"]:
        parts.append(f"{counts['sin_dato']} sin dato")
    return " · ".join(parts)


def _section_columns(section: dict) -> list[dict]:
    """Definición de columnas según el modo de carga de la sección (ancho útil: 184 mm)."""
    entry_mode = _safe_text(section.get("entry_mode")).lower()
    if entry_mode == "result_only":
        return [
            {"key": "label", "title": "Parámetro", "w": 54 * mm, "align": "l"},
            {"key": "target", "title": "Objetivo / Tolerancia", "w": 96 * mm, "align": "l"},
            {"key": "result", "title": "Resultado", "w": 20 * mm, "align": "c"},
            {"key": "refs", "title": "Ref.", "w": 14 * mm, "align": "c"},
        ]
    if entry_mode == "measured_only":
        return [
            {"key": "label", "title": "Parámetro", "w": 46 * mm, "align": "l"},
            {"key": "target", "title": "Objetivo / Tolerancia", "w": 76 * mm, "align": "l"},
            {"key": "measured", "title": "Medido", "w": 28 * mm, "align": "c"},
            {"key": "result", "title": "Resultado", "w": 20 * mm, "align": "c"},
            {"key": "refs", "title": "Ref.", "w": 14 * mm, "align": "c"},
        ]
    return [
        {"key": "label", "title": "Parámetro", "w": 42 * mm, "align": "l"},
        {"key": "target", "title": "Objetivo / Tolerancia", "w": 62 * mm, "align": "l"},
        {"key": "valor_a_medir", "title": "Valor a medir", "w": 24 * mm, "align": "c"},
        {"key": "measured", "title": "Medido", "w": 22 * mm, "align": "c"},
        {"key": "result", "title": "Resultado", "w": 20 * mm, "align": "c"},
        {"key": "refs", "title": "Ref.", "w": 14 * mm, "align": "c"},
    ]


def _cell_font(column_key: str) -> str:
    return "Helvetica-Bold" if column_key in ("label", "measured") else "Helvetica"


def _cell_size(column_key: str, base: float) -> float:
    return 6.8 if column_key == "refs" else base


def _cell_value(column_key: str, item: dict) -> str:
    value = item.get("value") or {}
    unit = _safe_text(item.get("unit"))
    if column_key == "label":
        return _safe_text(item.get("label"))
    if column_key == "target":
        return _safe_text(item.get("target"))
    if column_key == "valor_a_medir":
        return _safe_text(value.get("valor_a_medir"))
    if column_key == "measured":
        measured = _safe_text(value.get("measured"))
        if measured and unit:
            return f"{measured} {unit}"
        return measured
    if column_key == "refs":
        return ", ".join(item.get("ref_ids") or [])
    if column_key == "result":
        key = _safe_text(value.get("result")).lower()
        return _ITEM_RESULT_LABELS.get(key, _safe_text(value.get("result")))
    return ""


def _render(report: dict, printed_by: str, total_pages: int | None) -> tuple[bytes, int]:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    ML = 13 * mm            # margen lateral
    MT = 12 * mm            # margen superior
    MB = 11 * mm            # margen inferior
    CW = W - 2 * ML         # ancho útil (184 mm)
    FOOTER_H = 8 * mm
    BOTTOM = MB + FOOTER_H
    LH = 3.5 * mm           # interlínea base

    os_txt = _safe_text(report.get("os") or report.get("ingreso_id")) or "-"
    cliente = _safe_text(report.get("cliente")) or "-"
    equipo = _safe_text(report.get("tipo_equipo")) or "-"
    marca_modelo = " ".join(
        p for p in (_safe_text(report.get("marca")), _safe_text(report.get("modelo"))) if p
    ) or "-"
    serie = _safe_text(report.get("numero_serie")) or "-"
    interno = _safe_text(report.get("numero_interno")) or "-"
    protocolo = _safe_text(report.get("template_key")) or "-"
    version = _safe_text(report.get("template_version"))
    fecha = _fmt_dt(report.get("fecha_ejecucion")) or "-"
    result_key = _safe_text(report.get("resultado_global")).lower()
    result_label = _GLOBAL_RESULT_LABELS.get(result_key, _safe_text(report.get("resultado_global")) or "Pendiente")
    counts = _count_results(report)
    logo_path = _resolve_logo_path(report.get("empresa") or "")

    c.setTitle(f"Informe de test OS {os_txt} - {cliente}")
    c.setSubject(f"Informe técnico de test - protocolo {protocolo}")

    y = H - MT
    page_no = 0

    # ---------- primitivas ----------

    def put(x, yy, text, font="Helvetica", size=8.0, color=_INK, align="l", char_space=0.0):
        if char_space:
            # El espaciado entre letras sólo existe en el objeto de texto,
            # así que la alineación se resuelve a mano.
            width = pdfmetrics.stringWidth(text, font, size) + char_space * max(0, len(text) - 1)
            x0 = x - width if align == "r" else (x - width / 2.0 if align == "c" else x)
            obj = c.beginText(x0, yy)
            obj.setFont(font, size)
            obj.setFillColor(color)
            obj.setCharSpace(char_space)
            obj.textOut(text)
            c.drawText(obj)
            # Tc es estado de texto del PDF y sobrevive al bloque: si no se
            # vuelve a cero, todo lo que se dibuje después sale más ancho de
            # lo que miden las funciones de wrap y se pisa con la columna vecina.
            reset = c.beginText(0, 0)
            reset.setCharSpace(0)
            c.drawText(reset)
            return
        c.setFillColor(color)
        c.setFont(font, size)
        if align == "r":
            c.drawRightString(x, yy, text)
        elif align == "c":
            c.drawCentredString(x, yy, text)
        else:
            c.drawString(x, yy, text)

    def rule(yy, width=0.6, color=_HAIRLINE, x0=None, x1=None):
        c.setStrokeColor(color)
        c.setLineWidth(width)
        c.line(x0 if x0 is not None else ML, yy, x1 if x1 is not None else W - ML, yy)
        c.setLineWidth(1)

    def caption(x, yy, text, align="l"):
        put(x, yy, _safe_text(text).upper(), "Helvetica-Bold", 5.8, _MUTED, align=align, char_space=0.55)

    def field(x, top, w, label, value, size=9.0, font="Helvetica-Bold"):
        """Campo del cabezal: etiqueta chica arriba y valor destacado abajo.

        El valor entra en una línea siempre que se pueda; si no, pasa a dos
        líneas más chicas antes de recortar (los modelos y las razones
        sociales largas se leen enteros).
        """
        caption(x, top - 2.3 * mm, label)
        avail = w - 2.0 * mm
        text = _safe_text(value) or "-"
        if pdfmetrics.stringWidth(text, font, size) <= avail:
            put(x, top - 7.1 * mm, text, font, size, _INK)
            return
        small = 7.4
        lines = _wrap_text(text, font, small, avail)
        if len(lines) <= 2:
            put(x, top - 6.4 * mm, lines[0], font, small, _INK)
            if len(lines) == 2:
                put(x, top - 9.6 * mm, lines[1], font, small, _INK)
            return
        head = lines[0]
        tail, tail_size = _fit_text(" ".join(lines[1:]), font, small, avail, 6.4)
        put(x, top - 6.4 * mm, head, font, small, _INK)
        put(x, top - 9.6 * mm, tail, font, tail_size, _INK)

    # ---------- encabezado ----------

    def draw_masthead(first: bool) -> None:
        nonlocal y
        top = H - MT
        if first:
            drawn_logo = False
            if logo_path:
                try:
                    img = ImageReader(logo_path)
                    iw, ih = img.getSize()
                    logo_h = 10.5 * mm
                    logo_w = min(46 * mm, logo_h * (iw / float(ih or 1)))
                    c.drawImage(
                        img,
                        ML,
                        top - logo_h + 1.5 * mm,
                        width=logo_w,
                        height=logo_h,
                        preserveAspectRatio=True,
                        anchor="sw",
                        mask="auto",
                    )
                    drawn_logo = True
                except Exception:
                    drawn_logo = False
            if not drawn_logo:
                put(ML, top - 5 * mm, "SERVICIO TÉCNICO", "Helvetica-Bold", 11, _INK, char_space=0.8)

            put(W - ML, top - 3.4 * mm, "INFORME TÉCNICO DE TEST", "Helvetica-Bold", 12.5, _INK, align="r")
            if protocolo and protocolo != "-":
                proto_txt = f"Protocolo {protocolo}" + (f" · versión {version}" if version else "")
                put(W - ML, top - 7.9 * mm, proto_txt, "Helvetica", 7.4, _MUTED, align="r")

            y = top - 13.5 * mm
            rule(y, width=1.3, color=_INK)
            y -= 4.2 * mm
            draw_id_band()
        else:
            head = f"INFORME TÉCNICO DE TEST · OS {os_txt}"
            put(ML, top - 3.2 * mm, head, "Helvetica-Bold", 9, _INK)
            sub = f"{equipo} · {marca_modelo} · Serie {serie}"
            txt, size = _fit_text(sub, "Helvetica", 7.4, CW * 0.55, 6.2)
            put(W - ML, top - 3.2 * mm, txt, "Helvetica", size, _MUTED, align="r")
            y = top - 5.6 * mm
            rule(y, width=0.9, color=_INK)
            y -= 5 * mm

    def draw_verdict_badge(x, top, w) -> None:
        """Sello del resultado global, legible aun fotocopiado."""
        if result_key == "apto":
            fill, stroke, ink, line_w = colors.black, colors.black, colors.white, 1.2
        elif result_key == "no_apto":
            fill, stroke, ink, line_w = colors.white, colors.black, colors.black, 2.0
        elif result_key == "apto_condicional":
            fill, stroke, ink, line_w = _CHIP, colors.black, colors.black, 1.2
        else:
            fill, stroke, ink, line_w = colors.white, _RULE, _MUTED, 0.9

        badge_h = 10.2 * mm
        c.setLineWidth(line_w)
        c.setStrokeColor(stroke)
        c.setFillColor(fill)
        c.roundRect(x, top - badge_h, w, badge_h, 1.8 * mm, stroke=1, fill=1)
        c.setLineWidth(1)
        label = result_label.upper()
        txt, size = _fit_text(label, "Helvetica-Bold", 15, w - 4 * mm, 9)
        put(x + w / 2.0, top - badge_h + 3.3 * mm, txt, "Helvetica-Bold", size, ink, align="c")

    def draw_id_band() -> None:
        nonlocal y
        band_h = 26.5 * mm
        verdict_w = 52 * mm
        left_w = CW - verdict_w
        top = y

        c.setStrokeColor(_RULE)
        c.setLineWidth(0.8)
        c.setFillColor(colors.white)
        c.rect(ML, top - band_h, CW, band_h, stroke=1, fill=1)
        # Celda del veredicto con fondo suave para separarla de los datos.
        c.setFillColor(_SOFT)
        c.setStrokeColor(_RULE)
        c.rect(ML + left_w, top - band_h, verdict_w, band_h, stroke=1, fill=1)
        c.setLineWidth(1)

        pad = 3.2 * mm
        row1 = top - 1.2 * mm
        row2 = top - 13.6 * mm
        col_x = ML + pad
        # Fila 1: identificación del equipo (lo que más se busca de un vistazo).
        widths_1 = [left_w * 0.40, left_w * 0.36, left_w * 0.24]
        field(col_x, row1, widths_1[0], "Equipo", equipo)
        field(col_x + widths_1[0], row1, widths_1[1], "Marca / Modelo", marca_modelo)
        field(col_x + widths_1[0] + widths_1[1], row1, widths_1[2], "N° de serie", serie)
        # Fila 2: contexto administrativo.
        widths_2 = [left_w * 0.48, left_w * 0.28, left_w * 0.24]
        field(col_x, row2, widths_2[0], "Cliente", cliente)
        field(col_x + widths_2[0], row2, widths_2[1], "Fecha de ejecución", fecha)
        field(col_x + widths_2[0] + widths_2[1], row2, widths_2[2], "N° interno", interno)

        # Celda derecha: OS + veredicto + resumen de parámetros.
        vx = ML + left_w + 3 * mm
        vw = verdict_w - 6 * mm
        caption(vx, top - 3.2 * mm, "Orden de servicio")
        put(vx, top - 9.0 * mm, f"OS {os_txt}", "Helvetica-Bold", 14, _INK)
        draw_verdict_badge(vx, top - 11.4 * mm, vw)
        detalle = _counts_caption(counts) or "sin resultados cargados"
        txt, size = _fit_text(detalle, "Helvetica", 6.6, vw, 5.2)
        put(vx + vw / 2.0, top - band_h + 2.1 * mm, txt, "Helvetica", size, _MUTED, align="c")

        y = top - band_h - 5.5 * mm

    def draw_footer() -> None:
        rule(MB + 5.2 * mm, width=0.6, color=_HAIRLINE)
        partes = []
        if printed_by:
            partes.append(f"Impreso por: {printed_by}")
        if protocolo and protocolo != "-":
            partes.append(f"Plantilla {protocolo}" + (f" v{version}" if version else ""))
        if fecha and fecha != "-":
            partes.append(f"fuentes vigentes al {fecha}")
        left = " · ".join(partes)
        txt, size = _fit_text(left or " ", "Helvetica", 6.6, CW - 28 * mm, 5.6)
        put(ML, MB + 1.9 * mm, txt, "Helvetica", size, _MUTED)
        pag = f"Página {page_no} de {total_pages}" if total_pages else f"Página {page_no}"
        put(W - ML, MB + 1.9 * mm, pag, "Helvetica", 6.8, _MUTED, align="r")

    def start_page(first: bool = False) -> None:
        nonlocal page_no
        page_no += 1
        draw_masthead(first)

    def break_page() -> None:
        draw_footer()
        c.showPage()
        start_page(False)

    def ensure(required_h: float) -> bool:
        if y - required_h < BOTTOM:
            break_page()
            return True
        return False

    # ---------- secciones de medición ----------

    def draw_section_title(section: dict, index: int, cont: bool = False) -> None:
        nonlocal y
        title = _safe_text(section.get("title")) or "Sección"
        if cont:
            title = f"{title} (continúa)"
        put(ML, y - 3.4 * mm, f"{index}.", "Helvetica-Bold", 9.5, _MUTED)
        put(ML + 6 * mm, y - 3.4 * mm, title, "Helvetica-Bold", 9.5, _INK)
        sec_counts = {"total": 0, "ok": 0, "observado": 0, "no_ok": 0, "na": 0, "sin_dato": 0}
        for item in section.get("items") or []:
            sec_counts["total"] += 1
            key = _safe_text((item.get("value") or {}).get("result")).lower()
            sec_counts[key if key in sec_counts else "sin_dato"] += 1
        detalle = _counts_caption(sec_counts)
        if detalle:
            put(W - ML, y - 3.4 * mm, detalle, "Helvetica", 7, _MUTED, align="r")
        y -= 5.2 * mm
        rule(y, width=0.9, color=_INK)
        y -= 0.6 * mm

    def draw_table_header(cols: list[dict]) -> None:
        nonlocal y
        header_h = 5.8 * mm
        c.setFillColor(_SOFT)
        c.setStrokeColor(_SOFT)
        c.rect(ML, y - header_h, CW, header_h, stroke=0, fill=1)
        x = ML
        for col in cols:
            if col["align"] == "c":
                caption(x + col["w"] / 2.0, y - header_h + 2.0 * mm, col["title"], align="c")
            else:
                caption(x + 2.0 * mm, y - header_h + 2.0 * mm, col["title"])
            x += col["w"]
        y -= header_h
        rule(y, width=0.7, color=_RULE)

    def draw_sections() -> None:
        nonlocal y
        pad = 2.0 * mm
        row_font = 8.0

        sections = [s for s in (report.get("sections") or []) if s.get("items")]
        for index, section in enumerate(sections, start=1):
            cols = _section_columns(section)
            ensure(24 * mm)
            draw_section_title(section, index)
            draw_table_header(cols)

            zebra = False
            for item in section.get("items") or []:
                value = item.get("value") or {}
                result_item = _safe_text(value.get("result")).lower()
                obs = _safe_text(value.get("observaciones"))

                wrapped = []
                max_lines = 1
                for col in cols:
                    font = _cell_font(col["key"])
                    size = _cell_size(col["key"], row_font)
                    lines = _wrap_text(_cell_value(col["key"], item), font, size, col["w"] - 2 * pad)
                    wrapped.append(lines)
                    max_lines = max(max_lines, len(lines))

                obs_lines: list[str] = []
                obs_width = CW - cols[-1]["w"] - cols[-2]["w"] - 2 * pad
                if obs:
                    obs_lines = _wrap_text(f"Obs.: {obs}", "Helvetica-Oblique", 7.0, obs_width)

                main_h = max(6.2 * mm, max_lines * LH + 2.6 * mm)
                row_h = main_h + (len(obs_lines) * 3.2 * mm + 1.0 * mm if obs_lines else 0)

                if y - row_h < BOTTOM:
                    break_page()
                    draw_section_title(section, index, cont=True)
                    draw_table_header(cols)
                    zebra = False

                if zebra:
                    c.setFillColor(_ZEBRA)
                    c.setStrokeColor(_ZEBRA)
                    c.rect(ML, y - row_h, CW, row_h, stroke=0, fill=1)
                zebra = not zebra

                x = ML
                first_baseline = y - pad - 2.5 * mm
                center_y = y - main_h / 2.0
                for idx, col in enumerate(cols):
                    lines = wrapped[idx]
                    if col["key"] == "result":
                        draw_result_cell(x, center_y, col["w"], lines[0] if lines else "", result_item)
                        x += col["w"]
                        continue
                    font = _cell_font(col["key"])
                    size = _cell_size(col["key"], row_font)
                    color = _MUTED if col["key"] == "refs" else _INK
                    if col["align"] == "c":
                        # Las columnas numéricas quedan centradas también en
                        # vertical: si no, bailan respecto del chip de resultado.
                        yy = center_y + (len(lines) - 1) * LH / 2.0 - 1.2 * mm
                    else:
                        yy = first_baseline
                    for line in lines:
                        if col["align"] == "c":
                            put(x + col["w"] / 2.0, yy, line, font, size, color, align="c")
                        else:
                            put(x + pad, yy, line, font, size, color)
                        yy -= LH
                    x += col["w"]

                if obs_lines:
                    yy = y - main_h - 2.4 * mm
                    for line in obs_lines:
                        put(ML + pad + 2 * mm, yy, line, "Helvetica-Oblique", 7.0, _MUTED)
                        yy -= 3.2 * mm

                y -= row_h
                rule(y, width=0.5, color=_HAIRLINE)

            y -= 4.6 * mm

    def draw_result_cell(x: float, cy: float, w: float, label: str, key: str) -> None:
        """Resultado del renglón: chip sólo cuando hay algo que mirar dos veces."""
        if not label:
            return
        if key == "no_ok":
            chip_w, chip_h = min(w - 3 * mm, 17 * mm), 4.6 * mm
            c.setFillColor(colors.black)
            c.setStrokeColor(colors.black)
            c.roundRect(x + (w - chip_w) / 2.0, cy - chip_h / 2.0, chip_w, chip_h, 1.1 * mm, stroke=0, fill=1)
            put(x + w / 2.0, cy - 1.3 * mm, label.upper(), "Helvetica-Bold", 7.2, colors.white, align="c")
            return
        if key == "observado":
            chip_w, chip_h = min(w - 3 * mm, 17 * mm), 4.6 * mm
            c.setFillColor(_CHIP)
            c.setStrokeColor(_CHIP)
            c.roundRect(x + (w - chip_w) / 2.0, cy - chip_h / 2.0, chip_w, chip_h, 1.1 * mm, stroke=0, fill=1)
            put(x + w / 2.0, cy - 1.3 * mm, label, "Helvetica-Bold", 7.2, _INK, align="c")
            return
        color = _INK if key == "ok" else _MUTED
        put(x + w / 2.0, cy - 1.3 * mm, label, "Helvetica-Bold", 8.0, color, align="c")

    # ---------- cierre ----------

    def draw_block_title(title: str) -> None:
        nonlocal y
        put(ML, y - 3.4 * mm, title, "Helvetica-Bold", 9.5, _INK)
        y -= 5.2 * mm
        rule(y, width=0.9, color=_INK)
        y -= 1.2 * mm

    def draw_labeled_paragraph(label: str, value: str, size: float = 8.2) -> None:
        nonlocal y
        lines = _wrap_text(value or "-", "Helvetica", size, CW)
        ensure(6 * mm + LH)
        caption(ML, y - 2.4 * mm, label)
        y -= 5.4 * mm
        for line in lines:
            ensure(LH + 1 * mm)
            put(ML, y, line, "Helvetica", size, _INK)
            y -= LH
        y -= 1.2 * mm

    def draw_references() -> None:
        nonlocal y
        refs = report.get("references") or []
        ensure(14 * mm)
        draw_block_title("Normas técnicas de referencia aplicadas")
        y -= 2.4 * mm
        if not refs:
            put(ML, y, "Sin referencias técnicas declaradas.", "Helvetica", 8, _MUTED)
            y -= LH + 3 * mm
            return
        for ref in refs:
            ref_id = _safe_text(ref.get("ref_id")) or "-"
            titulo = _safe_text(ref.get("titulo"))
            edicion = _safe_text(ref.get("edicion")) or _safe_text(ref.get("anio"))
            organismo = _safe_text(ref.get("organismo_o_fabricante"))
            tipo = _safe_text(ref.get("tipo"))
            aplica = _safe_text(ref.get("aplica_a"))
            url = _safe_text(ref.get("url"))

            head_parts = [p for p in (titulo, f"ed. {edicion}" if edicion else "", organismo) if p]
            head_txt = " · ".join(head_parts) or "-"
            sub_parts = [p for p in (tipo.replace("_", " ").capitalize() if tipo else "", aplica, url) if p]
            sub_txt = " · ".join(sub_parts)

            id_w = 17 * mm
            head_lines = _wrap_text(head_txt, "Helvetica-Bold", 8.0, CW - id_w)
            sub_lines = _wrap_text(sub_txt, "Helvetica", 6.8, CW - id_w) if sub_txt else []
            block_h = (len(head_lines) + len(sub_lines)) * 3.3 * mm + 2.2 * mm
            ensure(block_h)

            put(ML, y, ref_id, "Helvetica-Bold", 8.0, _INK)
            yy = y
            for line in head_lines:
                put(ML + id_w, yy, line, "Helvetica-Bold", 8.0, _INK)
                yy -= 3.3 * mm
            for line in sub_lines:
                put(ML + id_w, yy, line, "Helvetica", 6.8, _MUTED)
                yy -= 3.3 * mm
            y = yy - 1.4 * mm
        y -= 2.5 * mm

    def closing_block_height() -> float:
        """Alto del cierre + firma, para no dejarlos partidos en dos páginas."""
        h = 6.4 * mm + 1.4 * mm + 11.4 * mm
        for value in (_safe_text(report.get("conclusion")), _safe_text(report.get("instrumentos"))):
            h += 5.4 * mm + len(_wrap_text(value or "-", "Helvetica", 8.2, CW)) * LH + 1.2 * mm
        h += 2 * mm + 22 * mm + 4.5 * mm
        h += len(_wrap_text(_LEGEND, "Helvetica", 6.6, CW)) * 3.0 * mm
        return h

    def draw_closing() -> None:
        nonlocal y
        # Si entero entra en una página, se lo empuja completo a la siguiente
        # antes que dejar la hoja de la firma casi vacía.
        block_h = closing_block_height()
        page_h = H - MT - BOTTOM - 12 * mm
        ensure(block_h if block_h <= page_h else 34 * mm)
        draw_block_title("Cierre técnico")
        y -= 1.4 * mm
        caption(ML, y - 2.6 * mm, "Resultado global")
        detalle = _counts_caption(counts)
        if detalle:
            plural = "parámetro evaluado" if counts["total"] == 1 else "parámetros evaluados"
            put(W - ML, y - 2.6 * mm, f"{counts['total']} {plural} · {detalle}", "Helvetica", 7.4, _MUTED, align="r")
        put(ML, y - 8.6 * mm, result_label.upper(), "Helvetica-Bold", 12, _INK)
        y -= 11.4 * mm
        draw_labeled_paragraph("Observaciones", _safe_text(report.get("conclusion")))
        draw_labeled_paragraph("Instrumentos utilizados", _safe_text(report.get("instrumentos")))

    def draw_signature() -> None:
        nonlocal y
        box_h = 22 * mm
        legend_h = len(_wrap_text(_LEGEND, "Helvetica", 6.6, CW)) * 3.0 * mm
        needed = 2 * mm + box_h + 4.5 * mm + legend_h
        ensure(needed)
        # Si sobra hoja, el bloque de firma baja (hasta un límite) para que el
        # informe no termine con un hueco largo debajo del texto.
        y = max(BOTTOM + needed, y - 32 * mm)
        y -= 2 * mm
        top = y
        c.setStrokeColor(_RULE)
        c.setLineWidth(0.8)
        c.setFillColor(colors.white)
        box_w = 92 * mm
        box_x = W - ML - box_w
        c.rect(box_x, top - box_h, box_w, box_h, stroke=1, fill=1)
        c.setLineWidth(1)

        caption(box_x + 4 * mm, top - 4.2 * mm, "Firma del responsable técnico")
        line_w = box_w - 12 * mm
        line_x = box_x + 6 * mm
        rule(top - box_h + 7.5 * mm, width=0.7, color=_RULE, x0=line_x, x1=line_x + line_w)
        # El nombre va debajo de la línea: arriba queda el espacio para firmar.
        firmado = _safe_text(report.get("firmado_por"))
        if firmado:
            txt, size = _fit_text(firmado, "Helvetica", 8, line_w - 6 * mm, 6.5)
            put(line_x + line_w / 2.0, top - box_h + 3.6 * mm, txt, "Helvetica", size, _MUTED, align="c")
        y = top - box_h - 4.5 * mm

        for line in _wrap_text(_LEGEND, "Helvetica", 6.6, CW):
            ensure(3 * mm)
            put(ML, y, line, "Helvetica", 6.6, _MUTED)
            y -= 3.0 * mm

    start_page(first=True)
    draw_sections()
    draw_references()
    draw_closing()
    draw_signature()
    draw_footer()

    c.save()
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes, page_no


def render_ingreso_test_pdf(report: dict, printed_by: str = "") -> tuple[bytes, str]:
    """
    Render test report PDF with reference traceability.

    report expected keys:
      ingreso_id, os, fecha_ejecucion, cliente, tipo_equipo, marca, modelo,
      numero_serie, numero_interno, template_key, template_version,
      resultado_global, conclusion, instrumentos, firmado_por,
      references(list), sections(list -> items with value payload).
      empresa (opcional: SEPID / MGBIO / TEST, sólo para elegir el logo).
    """
    # Dos pasadas: la primera cuenta páginas, la segunda imprime "Página X de N".
    _, pages = _render(report, printed_by, total_pages=None)
    pdf_bytes, _ = _render(report, printed_by, total_pages=pages)
    ingreso_id = _safe_text(report.get("ingreso_id")) or "0"
    return pdf_bytes, f"InformeTest_{ingreso_id}.pdf"
