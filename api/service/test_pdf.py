from __future__ import annotations

from datetime import datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
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


def _safe_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _fmt_dt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    try:
        s = _safe_text(value)
        if not s:
            return ""
        # Keep only a short human-readable format.
        return s.replace("T", " ")[:16]
    except Exception:
        return ""


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


def render_ingreso_test_pdf(report: dict, printed_by: str = "") -> tuple[bytes, str]:
    """
    Render test report PDF with reference traceability.

    report expected keys:
      ingreso_id, os, fecha_ejecucion, cliente, tipo_equipo, marca, modelo,
      numero_serie, numero_interno, template_key, template_version,
      resultado_global, conclusion, instrumentos, firmado_por,
      references(list), sections(list -> items with value payload).
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    margin = 12 * mm
    footer_h = 12 * mm
    line_h = 3.7 * mm
    section_gap = 2.4 * mm
    page_note = (
        f"Fuentes vigentes al {_fmt_dt(report.get('fecha_ejecucion') or datetime.now())} | "
        f"Plantilla: {_safe_text(report.get('template_key'))} v{_safe_text(report.get('template_version'))}"
    ).strip()
    y = H - margin
    # Small vertical offsets to improve visual centering in table cells and summary block.
    table_header_text_drop = 0.8 * mm
    table_cell_text_drop = 0.9 * mm
    summary_block_drop = 1.2 * mm

    def draw_footer(extra_note: str = "") -> None:
        pageno = c.getPageNumber()
        c.setStrokeColor(colors.lightgrey)
        c.line(margin, margin + 3 * mm, W - margin, margin + 3 * mm)
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 7)
        c.drawRightString(W - margin, margin + 1 * mm, f"Página {pageno}")
        if printed_by:
            c.drawString(margin, margin + 1 * mm, f"Impreso por: {printed_by}")
        if extra_note:
            c.setFont("Helvetica", 6.7)
            c.drawString(margin, margin + 5.2 * mm, extra_note[:250])

    def draw_header() -> None:
        nonlocal y
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, H - margin + 0.5 * mm, "INFORME TÉCNICO DE TEST")

        c.setFont("Helvetica", 8)
        c.drawRightString(
            W - margin,
            H - margin + 0.5 * mm,
            f"OS: {_safe_text(report.get('os') or report.get('ingreso_id'))}",
        )

        y0 = H - margin - 5 * mm
        lines = [
            f"Fecha de ejecución: {_fmt_dt(report.get('fecha_ejecucion') or datetime.now())}",
            f"Cliente: {_safe_text(report.get('cliente')) or '-'}",
            (
                "Equipo: "
                f"{_safe_text(report.get('tipo_equipo')) or '-'} | "
                f"{_safe_text(report.get('marca')) or '-'} | "
                f"{_safe_text(report.get('modelo')) or '-'}"
            ),
            (
                "Serie / Interno: "
                f"{_safe_text(report.get('numero_serie')) or '-'} / "
                f"{_safe_text(report.get('numero_interno')) or '-'}"
            ),
            (
                "Protocolo: "
                f"{_safe_text(report.get('template_key')) or '-'} "
                f"v{_safe_text(report.get('template_version')) or '-'}"
            ),
        ]
        for idx, line in enumerate(lines):
            c.drawString(margin, y0 - idx * line_h, line[:220])
        y = y0 - len(lines) * line_h - 2 * mm

    def ensure_space(required_h: float) -> None:
        nonlocal y
        if y - required_h < margin + footer_h:
            draw_footer()
            c.showPage()
            draw_header()

    def draw_reference_block() -> None:
        nonlocal y
        refs = report.get("references") or []
        ensure_space(8 * mm)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, y, "Normas técnicas de referencia aplicadas")
        y -= 4.6 * mm

        if not refs:
            ensure_space(6 * mm)
            c.setFont("Helvetica", 8)
            c.drawString(margin, y, "Sin referencias técnicas declaradas.")
            y -= 5 * mm
            return

        for ref in refs:
            ref_txt = (
                f"{_safe_text(ref.get('ref_id'))} "
                f"[{_safe_text(ref.get('tipo'))}] "
                f"{_safe_text(ref.get('titulo'))} "
                f"({(_safe_text(ref.get('edicion')) or _safe_text(ref.get('anio')) or '-')}) - "
                f"{_safe_text(ref.get('organismo_o_fabricante'))}. "
                f"URL: {_safe_text(ref.get('url'))}"
            ).strip()
            wrapped = _wrap_text(ref_txt, "Helvetica", 7.5, W - 2 * margin)
            block_h = max(5.0, len(wrapped) * line_h + 1.2 * mm)
            ensure_space(block_h)
            c.setFont("Helvetica", 7.5)
            for line in wrapped:
                c.drawString(margin, y, line)
                y -= line_h
            y -= 1.1 * mm
        y -= section_gap

    def draw_sections() -> None:
        nonlocal y
        table_x = margin
        header_h = 6.5 * mm
        row_font = 7.4
        row_inner_pad = 1.2 * mm
        post_performance_drop = 2.2 * mm
        dropped_after_performance = False

        for section in report.get("sections") or []:
            items = section.get("items") or []
            if not items:
                continue
            section_id = _safe_text(section.get("id")).lower()
            section_title = _safe_text(section.get("title")).lower()
            is_performance_section = (section_id == "performance") or (section_title == "rendimiento")
            if is_performance_section and not dropped_after_performance:
                ensure_space(post_performance_drop + 10 * mm)
                y -= post_performance_drop
                dropped_after_performance = True

            entry_mode = _safe_text(section.get("entry_mode")).lower()
            is_result_only = entry_mode == "result_only"
            is_measured_only = entry_mode == "measured_only"
            if is_result_only:
                columns_mm = [44, 72, 22, 48]
                col_titles = ["Parámetro", "Objetivo / Tolerancia", "Resultado", "Ref."]
            elif is_measured_only:
                columns_mm = [32, 64, 20, 12, 20, 38]
                col_titles = ["Parámetro", "Objetivo / Tolerancia", "Medido", "Unidad", "Resultado", "Ref."]
            else:
                columns_mm = [30, 50, 24, 18, 12, 18, 34]
                col_titles = ["Parámetro", "Objetivo / Tolerancia", "Valor a medir", "Medido", "Unidad", "Resultado", "Ref."]
            col_widths = [w * mm for w in columns_mm]

            ensure_space(10 * mm)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(margin, y, _safe_text(section.get("title")) or "Sección")
            y -= 4.5 * mm

            ensure_space(header_h + 2 * mm)
            x = table_x
            c.setFillColor(colors.HexColor("#f3f4f6"))
            c.rect(table_x, y - header_h, sum(col_widths), header_h, stroke=1, fill=1)
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 7.2)
            for idx, title in enumerate(col_titles):
                c.drawString(x + row_inner_pad, y - header_h + 2.1 * mm - table_header_text_drop, title)
                x += col_widths[idx]
            y -= header_h

            for item in items:
                value = item.get("value") or {}
                result_label = _ITEM_RESULT_LABELS.get(_safe_text(value.get("result")).lower(), _safe_text(value.get("result")))
                if is_result_only:
                    cells = [
                        _safe_text(item.get("label")),
                        _safe_text(item.get("target")),
                        result_label,
                        ", ".join(item.get("ref_ids") or []),
                    ]
                elif is_measured_only:
                    cells = [
                        _safe_text(item.get("label")),
                        _safe_text(item.get("target")),
                        _safe_text(value.get("measured")),
                        _safe_text(item.get("unit")),
                        result_label,
                        ", ".join(item.get("ref_ids") or []),
                    ]
                else:
                    cells = [
                        _safe_text(item.get("label")),
                        _safe_text(item.get("target")),
                        _safe_text(value.get("valor_a_medir")),
                        _safe_text(value.get("measured")),
                        _safe_text(item.get("unit")),
                        result_label,
                        ", ".join(item.get("ref_ids") or []),
                    ]

                wrapped_cells = []
                max_lines = 1
                for idx, cell in enumerate(cells):
                    lines = _wrap_text(cell, "Helvetica", row_font, col_widths[idx] - 2 * row_inner_pad)
                    wrapped_cells.append(lines)
                    max_lines = max(max_lines, len(lines))
                row_h = max(5.2 * mm, max_lines * line_h + 1.1 * mm)
                ensure_space(row_h)

                x = table_x
                c.setFont("Helvetica", row_font)
                for idx, lines in enumerate(wrapped_cells):
                    c.rect(x, y - row_h, col_widths[idx], row_h, stroke=1, fill=0)
                    yy = y - row_inner_pad - 2.1 - table_cell_text_drop
                    for line in lines:
                        c.drawString(x + row_inner_pad, yy, line)
                        yy -= line_h
                    x += col_widths[idx]
                y -= row_h
            y -= section_gap

    def draw_summary() -> None:
        nonlocal y
        block_h = 65 * mm
        ensure_space(block_h + 2 * mm)
        y -= summary_block_drop

        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, y, "Resultado y cierre técnico")
        y -= 4.5 * mm

        result_key = _safe_text(report.get("resultado_global")).lower()
        result_global = _GLOBAL_RESULT_LABELS.get(
            result_key,
            _safe_text(report.get("resultado_global")) or "Pendiente",
        )
        # Visual pensado para impresión en blanco y negro:
        # APTO queda con alto contraste (fondo negro + texto blanco).
        if result_key == "apto":
            text_color, fill_color, stroke_color = (colors.white, colors.black, colors.black)
            badge_line_w = 1.4
        elif result_key == "no_apto":
            text_color, fill_color, stroke_color = (colors.black, colors.white, colors.black)
            badge_line_w = 1.4
        elif result_key == "apto_condicional":
            text_color, fill_color, stroke_color = (colors.black, colors.HexColor("#e5e7eb"), colors.black)
            badge_line_w = 1.2
        else:
            text_color, fill_color, stroke_color = (colors.black, colors.white, colors.black)
            badge_line_w = 1.0

        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 8)
        badge_w = 62 * mm
        badge_h = 10.5 * mm
        result_x = W - margin - badge_w
        c.drawString(margin, y, "Resultado global:")
        y -= 1.8 * mm

        badge_y = y - badge_h
        c.setLineWidth(badge_line_w)
        c.setStrokeColor(stroke_color)
        c.setFillColor(fill_color)
        c.roundRect(result_x, badge_y, badge_w, badge_h, 2.2 * mm, stroke=1, fill=1)
        c.setFillColor(text_color)
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(result_x + (badge_w / 2.0), badge_y + 2.9 * mm, result_global.upper())
        c.setLineWidth(1.0)
        c.setFillColor(colors.black)
        y = badge_y - 4.6 * mm

        def draw_labeled_text(label: str, value: str) -> None:
            nonlocal y
            label_txt = f"{label}: "
            c.setFont("Helvetica-Bold", 8)
            label_w = pdfmetrics.stringWidth(label_txt, "Helvetica-Bold", 8)
            max_w = W - 2 * margin
            value_w = max(12 * mm, max_w - label_w)
            wrapped_value = _wrap_text(value or "-", "Helvetica", 8, value_w)

            ensure_space(line_h + 1.5 * mm)
            c.setFont("Helvetica-Bold", 8)
            c.drawString(margin, y, label_txt)
            c.setFont("Helvetica", 8)
            c.drawString(margin + label_w, y, wrapped_value[0] if wrapped_value else "-")
            y -= line_h

            for cont in wrapped_value[1:]:
                ensure_space(line_h + 1.5 * mm)
                c.drawString(margin + label_w, y, cont)
                y -= line_h
            y -= 0.9 * mm

        draw_labeled_text("Observaciones", _safe_text(report.get("conclusion")) or "-")
        draw_labeled_text("Instrumentos", _safe_text(report.get("instrumentos")) or "-")

        # Campo intencionalmente en blanco para firma manual.
        # Lo ubicamos más abajo y a la derecha para aprovechar mejor el espacio de página.
        sig_w = 58 * mm
        sig_x = W - margin - sig_w
        sig_y = margin + footer_h + 16.0 * mm
        c.line(sig_x, sig_y, sig_x + sig_w, sig_y)
        c.setFont("Helvetica", 8)
        c.drawString(sig_x, sig_y - 4.2 * mm, "Firma responsable")
        y = sig_y - 7 * mm

    draw_header()
    draw_reference_block()
    draw_sections()
    draw_summary()
    draw_footer(extra_note=page_note)

    c.save()
    pdf_bytes = buf.getvalue()
    buf.close()
    ingreso_id = _safe_text(report.get("ingreso_id")) or "0"
    return pdf_bytes, f"InformeTest_{ingreso_id}.pdf"

