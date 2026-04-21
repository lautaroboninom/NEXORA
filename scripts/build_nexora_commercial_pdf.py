from __future__ import annotations

import argparse
import importlib.util
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTENT_PATH = REPO_ROOT / "docs" / "comercial" / "content.py"
DEFAULT_OUT = REPO_ROOT / "output" / "pdf" / "nexora_presentacion_comercial.pdf"
DEFAULT_RENDER_DIR = REPO_ROOT / "tmp" / "pdfs" / "nexora_presentacion"

PAGE_W = 960
PAGE_H = 540
PAGE_SIZE = (PAGE_W, PAGE_H)

FONT_DIR = REPO_ROOT / "docs" / "comercial" / "assets" / "fonts"
FONT_REGULAR = "SourceSans3"
FONT_SEMIBOLD = "SourceSans3-Semibold"
FONT_FILES = {
    FONT_REGULAR: FONT_DIR / "SourceSans3-Regular.ttf",
    FONT_SEMIBOLD: FONT_DIR / "SourceSans3-Semibold.ttf",
}

COLOR_BG = colors.HexColor("#F7FAFB")
COLOR_INK = colors.HexColor("#10263A")
COLOR_MUTED = colors.HexColor("#5F7385")
COLOR_ACCENT = colors.HexColor("#1597A8")
COLOR_ACCENT_DARK = colors.HexColor("#0D6F82")
COLOR_PANEL = colors.white
COLOR_PANEL_SOFT = colors.HexColor("#EAF6F8")
COLOR_BORDER = colors.HexColor("#D7E5EA")
COLOR_WARN = colors.HexColor("#FFF3E6")
COLOR_WARN_BORDER = colors.HexColor("#F4C58A")


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def top(self) -> float:
        return self.y + self.h


def register_fonts() -> None:
    for name, path in FONT_FILES.items():
        if not path.exists():
            raise FileNotFoundError(f"Falta la fuente requerida: {path}")
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))


def load_content():
    spec = importlib.util.spec_from_file_location("nexora_content", CONTENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo cargar {CONTENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def round_rect(c: canvas.Canvas, rect: Rect, fill, stroke=COLOR_BORDER, radius=18, line=1):
    c.setLineWidth(line)
    c.setStrokeColor(stroke)
    c.setFillColor(fill)
    c.roundRect(rect.x, rect.y, rect.w, rect.h, radius, fill=1, stroke=1)


def draw_background(c: canvas.Canvas, page_no: int):
    c.setFillColor(COLOR_BG)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#E6F7FA"))
    c.circle(PAGE_W - 110, PAGE_H - 55, 90, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#EFF6FF"))
    c.circle(80, PAGE_H - 20, 70, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#F2FBFD"))
    c.rect(0, 0, PAGE_W, 56, fill=1, stroke=0)
    c.setStrokeColor(colors.HexColor("#DDEBF0"))
    c.setLineWidth(1)
    c.line(36, PAGE_H - 64, PAGE_W - 36, PAGE_H - 64)


def draw_brand(c: canvas.Canvas, logo_path: Path):
    x = 44
    y = PAGE_H - 44
    c.setFillColor(COLOR_ACCENT)
    c.roundRect(x, y - 22, 18, 18, 5, fill=1, stroke=0)
    c.setFillColor(COLOR_ACCENT_DARK)
    c.roundRect(x + 10, y - 12, 18, 18, 5, fill=1, stroke=0)
    c.setFont(FONT_SEMIBOLD, 18)
    c.setFillColor(COLOR_INK)
    c.drawString(x + 38, y - 1, "NEXORA")
    c.setFont(FONT_REGULAR, 9.5)
    c.setFillColor(COLOR_MUTED)
    c.drawString(x + 38, y - 14, "Software para servicio técnico y mantenimiento")


def draw_footer(c: canvas.Canvas, page_no: int):
    c.setStrokeColor(COLOR_BORDER)
    c.line(42, 34, PAGE_W - 42, 34)
    c.setFont(FONT_REGULAR, 8.5)
    c.setFillColor(COLOR_MUTED)
    c.drawRightString(PAGE_W - 44, 18, f"Página {page_no:02d}")


def fit_text(text: str, font_name: str, font_size: float, max_width: float):
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def wrap_text(text: str, font_name: str, font_size: float, max_width: float):
    lines = []
    for raw in text.split("\n"):
        if not raw.strip():
            lines.append("")
            continue
        lines.extend(fit_text(raw, font_name, font_size, max_width))
    return lines or [""]


def text_height(text: str, font_name: str, font_size: float, max_width: float, leading: float) -> float:
    return len(wrap_text(text, font_name, font_size, max_width)) * leading


def choose_single_line_size(text: str, font_name: str, max_width: float, start: float, minimum: float, step: float = 0.4) -> float:
    size = start
    while size >= minimum:
        if pdfmetrics.stringWidth(text, font_name, size) <= max_width:
            return size
        size -= step
    return minimum


def choose_paragraph_style(text: str, width: float, max_height: float, start: float, minimum: float, font_name: str = FONT_REGULAR, ratio: float = 1.24):
    size = start
    while size >= minimum:
        leading = max(size + 2.0, round(size * ratio, 1))
        if text_height(text, font_name, size, width, leading) <= max_height:
            return size, leading
        size -= 0.4
    final_leading = max(minimum + 2.0, round(minimum * ratio, 1))
    return minimum, final_leading


def measure_bullets(items, width: float, size: float, leading: float, item_gap: float, font_name: str = FONT_REGULAR, bullet_indent: float = 16) -> float:
    total = 0.0
    for idx, item in enumerate(items):
        total += len(fit_text(item, font_name, size, width - bullet_indent)) * leading
        if idx < len(items) - 1:
            total += item_gap
    return total


def choose_bullet_style(items, width: float, max_height: float, start: float, minimum: float):
    size = start
    while size >= minimum:
        leading = max(size + 2.1, round(size * 1.23, 1))
        item_gap = 5 if size >= 10 else 4
        if measure_bullets(items, width, size, leading, item_gap) <= max_height:
            return size, leading, item_gap
        size -= 0.4
    final_leading = max(minimum + 2.0, round(minimum * 1.23, 1))
    return minimum, final_leading, 4


def measure_sections(sections, width: float, heading_size: float, heading_gap: float, bullet_size: float, bullet_leading: float, item_gap: float, section_gap: float) -> float:
    total = 0.0
    for idx, section in enumerate(sections):
        total += heading_size + heading_gap
        total += measure_bullets(section["points"], width, bullet_size, bullet_leading, item_gap)
        if idx < len(sections) - 1:
            total += section_gap
    return total


def choose_section_style(sections, width: float, max_height: float, start_bullet: float = 11.0, minimum_bullet: float = 8.6):
    bullet_size = start_bullet
    while bullet_size >= minimum_bullet:
        bullet_leading = max(bullet_size + 2.1, round(bullet_size * 1.23, 1))
        heading_size = max(11.0, round(bullet_size + 1.6, 1))
        heading_gap = max(14.0, round(bullet_size + 5.0, 1))
        item_gap = 5 if bullet_size >= 10 else 4
        section_gap = 8 if bullet_size >= 10 else 6
        used = measure_sections(sections, width, heading_size, heading_gap, bullet_size, bullet_leading, item_gap, section_gap)
        if used <= max_height:
            return {
                "heading_size": heading_size,
                "heading_gap": heading_gap,
                "bullet_size": bullet_size,
                "bullet_leading": bullet_leading,
                "item_gap": item_gap,
                "section_gap": section_gap,
            }
        bullet_size -= 0.4
    return {
        "heading_size": 10.8,
        "heading_gap": 14.0,
        "bullet_size": minimum_bullet,
        "bullet_leading": max(minimum_bullet + 2.0, round(minimum_bullet * 1.23, 1)),
        "item_gap": 4,
        "section_gap": 6,
    }


def measure_closing_blocks(blocks, width: float, heading_size: float, para_size: float, para_leading: float, block_gap: float) -> float:
    total = 0.0
    for idx, block in enumerate(blocks):
        total += heading_size + 8
        total += text_height(block["text"], FONT_REGULAR, para_size, width, para_leading)
        if idx < len(blocks) - 1:
            total += block_gap
    return total


def choose_closing_style(blocks, width: float, max_height: float, start_para: float = 10.4, minimum_para: float = 8.8):
    para_size = start_para
    while para_size >= minimum_para:
        para_leading = max(para_size + 2.0, round(para_size * 1.22, 1))
        heading_size = max(10.2, round(para_size + 0.8, 1))
        block_gap = 10 if para_size >= 10 else 8
        used = measure_closing_blocks(blocks, width, heading_size, para_size, para_leading, block_gap)
        if used <= max_height:
            return {
                "heading_size": heading_size,
                "para_size": para_size,
                "para_leading": para_leading,
                "block_gap": block_gap,
            }
        para_size -= 0.4
    return {
        "heading_size": 10.0,
        "para_size": minimum_para,
        "para_leading": max(minimum_para + 2.0, round(minimum_para * 1.22, 1)),
        "block_gap": 8,
    }


def draw_wrapped(c: canvas.Canvas, text: str, x: float, y: float, width: float, font: str = FONT_REGULAR, size: float = 11, leading: float = 15, color=COLOR_INK):
    c.setFont(font, size)
    c.setFillColor(color)
    cur_y = y
    for line in wrap_text(text, font, size, width):
        if line:
            c.drawString(x, cur_y, line)
        cur_y -= leading
    return cur_y


def draw_bullets(c: canvas.Canvas, items, x: float, y: float, width: float, size: float = 11, leading: float = 14, item_gap: float = 5, color=COLOR_INK, bullet_color=COLOR_ACCENT, font_name: str = FONT_REGULAR, bullet_indent: float = 16):
    cur_y = y
    for item in items:
        lines = fit_text(item, font_name, size, width - bullet_indent)
        c.setFillColor(bullet_color)
        c.circle(x + 4, cur_y - (size * 0.34), 2.3, fill=1, stroke=0)
        c.setFillColor(color)
        c.setFont(font_name, size)
        line_y = cur_y
        for line in lines:
            c.drawString(x + bullet_indent, line_y, line)
            line_y -= leading
        cur_y = line_y - item_gap
    return cur_y


def draw_section(c: canvas.Canvas, heading: str, points, x: float, y: float, width: float, style: dict, bullet_color=COLOR_ACCENT):
    c.setFont(FONT_SEMIBOLD, style["heading_size"])
    c.setFillColor(COLOR_INK)
    c.drawString(x, y, heading)
    return draw_bullets(
        c,
        points,
        x,
        y - style["heading_gap"],
        width,
        size=style["bullet_size"],
        leading=style["bullet_leading"],
        item_gap=style["item_gap"],
        bullet_color=bullet_color,
    )


def crop_image(path: Path, crop):
    img = Image.open(path).convert("RGB")
    if crop:
        left = int(img.width * crop[0])
        top = int(img.height * crop[1])
        right = int(img.width * crop[2])
        bottom = int(img.height * crop[3])
        img = img.crop((left, top, right, bottom))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def draw_image_card(c: canvas.Canvas, image_path: Path, crop, rect: Rect, caption: str):
    shadow = Rect(rect.x + 4, rect.y - 4, rect.w, rect.h)
    c.setFillColor(colors.HexColor("#DCE8ED"))
    c.roundRect(shadow.x, shadow.y, shadow.w, shadow.h, 20, fill=1, stroke=0)
    round_rect(c, rect, COLOR_PANEL, stroke=COLOR_BORDER, radius=20)

    inner = Rect(rect.x + 10, rect.y + 40, rect.w - 20, rect.h - 54)
    stream = crop_image(image_path, crop)
    image = ImageReader(stream)
    stream.seek(0)
    img = Image.open(stream)
    img_w, img_h = img.size
    img_ratio = img_w / img_h
    box_ratio = inner.w / inner.h
    if img_ratio > box_ratio:
        draw_w = inner.w
        draw_h = inner.w / img_ratio
    else:
        draw_h = inner.h
        draw_w = inner.h * img_ratio
    dx = inner.x + (inner.w - draw_w) / 2
    dy = inner.y + (inner.h - draw_h) / 2
    c.drawImage(image, dx, dy, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")

    caption_size, caption_leading = choose_paragraph_style(caption, rect.w - 24, 24, start=8.8, minimum=7.4)
    draw_wrapped(c, caption, rect.x + 12, rect.y + 22, rect.w - 24, font=FONT_REGULAR, size=caption_size, leading=caption_leading, color=COLOR_MUTED)


def draw_title(c: canvas.Canvas, title: str, subtitle: str):
    title_size = choose_single_line_size(title, FONT_SEMIBOLD, PAGE_W - 88, 24, 20)
    c.setFillColor(COLOR_INK)
    c.setFont(FONT_SEMIBOLD, title_size)
    c.drawString(44, PAGE_H - 96, title)
    subtitle_size, subtitle_leading = choose_paragraph_style(subtitle, PAGE_W - 88, 34, start=11.8, minimum=10.2)
    draw_wrapped(c, subtitle, 44, PAGE_H - 118, PAGE_W - 88, font=FONT_REGULAR, size=subtitle_size, leading=subtitle_leading, color=COLOR_MUTED)


def draw_cover(c: canvas.Canvas, page: dict, screenshots: dict, logo_path: Path):
    draw_background(c, 1)
    draw_brand(c, logo_path)
    c.setFillColor(COLOR_INK)
    c.setFont(FONT_SEMIBOLD, 32)
    c.drawString(44, PAGE_H - 112, page["title"])
    subtitle_size, subtitle_leading = choose_paragraph_style(page["subtitle"], 330, 48, start=12.8, minimum=11.0)
    draw_wrapped(c, page["subtitle"], 44, PAGE_H - 140, 330, font=FONT_REGULAR, size=subtitle_size, leading=subtitle_leading, color=COLOR_MUTED)

    summary_rect = Rect(44, 64, 340, 192)
    round_rect(c, summary_rect, COLOR_PANEL_SOFT, stroke=COLOR_BORDER, radius=22)
    c.setFont(FONT_SEMIBOLD, 12.5)
    c.setFillColor(COLOR_INK)
    c.drawString(64, 230, "Qué aporta NEXORA")
    bullet_size, bullet_leading, item_gap = choose_bullet_style(page["summary"], 296, 108, start=11.2, minimum=9.2)
    draw_bullets(c, page["summary"], 60, 208, 300, size=bullet_size, leading=bullet_leading, item_gap=item_gap)

    chip_x = 44
    chip_y = 58
    for item in page["highlights"]:
        w = pdfmetrics.stringWidth(item, FONT_SEMIBOLD, 9) + 24
        if chip_x + w > 384:
            chip_x = 44
            chip_y -= 26
        c.setFillColor(colors.white)
        c.setStrokeColor(COLOR_ACCENT)
        c.roundRect(chip_x, chip_y, w, 20, 10, fill=1, stroke=1)
        c.setFont(FONT_SEMIBOLD, 9)
        c.setFillColor(COLOR_ACCENT_DARK)
        c.drawString(chip_x + 12, chip_y + 6, item)
        chip_x += w + 10

    img_a = page["images"][0]
    spec_a = screenshots[img_a["key"]]
    draw_image_card(c, Path(spec_a["file"]), spec_a.get("crop"), Rect(410, 70, 510, 252), img_a["caption"])

    img_b = page["images"][1]
    spec_b = screenshots[img_b["key"]]
    draw_image_card(c, Path(spec_b["file"]), spec_b.get("crop"), Rect(590, 276, 320, 206), img_b["caption"])

    c.setFillColor(COLOR_ACCENT_DARK)
    c.setFont(FONT_SEMIBOLD, 11)
    c.drawRightString(PAGE_W - 44, 40, "Despliegue interno y adaptable")
    draw_footer(c, 1)


def draw_problem(c: canvas.Canvas, page: dict, screenshots: dict, logo_path: Path, page_no: int):
    draw_background(c, page_no)
    draw_brand(c, logo_path)
    draw_title(c, page["title"], page["subtitle"])

    rect_left = Rect(44, 92, 372, 300)
    round_rect(c, rect_left, COLOR_PANEL, radius=22)

    top_box = Rect(rect_left.x + 20, rect_left.y + 166, rect_left.w - 40, 120)
    bottom_box = Rect(rect_left.x + 20, rect_left.y + 38, rect_left.w - 40, 120)

    top = page["sections"][0]
    bottom = page["sections"][1]

    round_rect(c, top_box, COLOR_WARN, stroke=COLOR_WARN_BORDER, radius=16)
    round_rect(c, bottom_box, COLOR_PANEL_SOFT, stroke=COLOR_BORDER, radius=16)

    top_style = choose_section_style([top], top_box.w - 34, top_box.h - 34, start_bullet=10.2, minimum_bullet=8.8)
    bottom_style = choose_section_style([bottom], bottom_box.w - 34, bottom_box.h - 34, start_bullet=10.2, minimum_bullet=8.8)

    draw_section(c, top["heading"], top["points"], top_box.x + 16, top_box.top - 24, top_box.w - 32, top_style, bullet_color=colors.HexColor("#D67A00"))
    draw_section(c, bottom["heading"], bottom["points"], bottom_box.x + 16, bottom_box.top - 24, bottom_box.w - 32, bottom_style)

    spec = screenshots[page["image"]["key"]]
    draw_image_card(c, Path(spec["file"]), spec.get("crop"), Rect(438, 92, 478, 300), page["image"]["caption"])
    draw_footer(c, page_no)


def draw_flow(c: canvas.Canvas, page: dict, logo_path: Path, page_no: int):
    draw_background(c, page_no)
    draw_brand(c, logo_path)
    draw_title(c, page["title"], page["subtitle"])

    start_x = 56
    box_w = 132
    box_h = 80
    gap = 14
    top_row_y = 274
    bottom_row_y = 158

    for idx, (title, desc) in enumerate(page["flow_steps"]):
        x = start_x + (idx % 3) * (box_w + gap) + (0 if idx < 3 else 54)
        row_y = top_row_y if idx < 3 else bottom_row_y
        round_rect(c, Rect(x, row_y, box_w, box_h), COLOR_PANEL, radius=18)
        c.setFillColor(COLOR_ACCENT)
        c.circle(x + 18, row_y + box_h - 18, 10, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(FONT_SEMIBOLD, 10)
        c.drawCentredString(x + 18, row_y + box_h - 21, str(idx + 1))
        c.setFillColor(COLOR_INK)
        title_size = choose_single_line_size(title, FONT_SEMIBOLD, box_w - 48, 11.2, 9.2)
        c.setFont(FONT_SEMIBOLD, title_size)
        c.drawString(x + 34, row_y + box_h - 23, title)
        desc_size, desc_leading = choose_paragraph_style(desc, box_w - 28, 36, start=8.6, minimum=7.6)
        draw_wrapped(c, desc, x + 14, row_y + box_h - 41, box_w - 28, font=FONT_REGULAR, size=desc_size, leading=desc_leading, color=COLOR_MUTED)

        if idx < 2:
            ax = x + box_w + 4
            ay = row_y + box_h / 2
            c.setStrokeColor(COLOR_ACCENT)
            c.setLineWidth(2)
            c.line(ax, ay, ax + 8, ay)
            c.line(ax + 8, ay, ax + 4, ay + 4)
            c.line(ax + 8, ay, ax + 4, ay - 4)
        if idx == 2:
            bottom_target_x = start_x + 54 + box_w / 2
            elbow_y = 252
            c.setStrokeColor(COLOR_ACCENT)
            c.setLineWidth(2)
            c.line(x + box_w / 2, row_y - 6, x + box_w / 2, elbow_y)
            c.line(x + box_w / 2, elbow_y, bottom_target_x, elbow_y)
            c.line(bottom_target_x, elbow_y, bottom_target_x, bottom_row_y + box_h + 6)
            c.line(bottom_target_x, bottom_row_y + box_h + 6, bottom_target_x - 4, bottom_row_y + box_h + 10)
            c.line(bottom_target_x, bottom_row_y + box_h + 6, bottom_target_x + 4, bottom_row_y + box_h + 10)
        if 3 <= idx < 5:
            ax = x + box_w + 4
            ay = row_y + box_h / 2
            c.setStrokeColor(COLOR_ACCENT)
            c.setLineWidth(2)
            c.line(ax, ay, ax + 8, ay)
            c.line(ax + 8, ay, ax + 4, ay + 4)
            c.line(ax + 8, ay, ax + 4, ay - 4)

    callout_rect = Rect(634, 126, 280, 216)
    round_rect(c, callout_rect, COLOR_PANEL_SOFT, stroke=COLOR_BORDER, radius=24)
    c.setFont(FONT_SEMIBOLD, 13)
    c.setFillColor(COLOR_INK)
    c.drawString(callout_rect.x + 20, callout_rect.top - 32, "Lo que cambia en la gestión")

    bullet_width = callout_rect.w - 38
    bullet_size, bullet_leading, item_gap = choose_bullet_style(page["callouts"], bullet_width, 92, start=10.8, minimum=9.2)
    bullets_end = draw_bullets(
        c,
        page["callouts"],
        callout_rect.x + 16,
        callout_rect.top - 56,
        bullet_width,
        size=bullet_size,
        leading=bullet_leading,
        item_gap=item_gap,
    )

    result_title_y = bullets_end - 6
    c.setFont(FONT_SEMIBOLD, 10.5)
    c.setFillColor(COLOR_ACCENT_DARK)
    c.drawString(callout_rect.x + 20, result_title_y, page.get("result_title", "Resultado"))
    result_max_height = max(34, result_title_y - (callout_rect.y + 18) - 8)
    result_size, result_leading = choose_paragraph_style(page["result_text"], bullet_width, result_max_height, start=10.0, minimum=8.8)
    draw_wrapped(
        c,
        page["result_text"],
        callout_rect.x + 20,
        result_title_y - 16,
        bullet_width,
        font=FONT_REGULAR,
        size=result_size,
        leading=result_leading,
        color=COLOR_MUTED,
    )
    draw_footer(c, page_no)


def draw_text_image(c: canvas.Canvas, page: dict, screenshots: dict, logo_path: Path, page_no: int):
    draw_background(c, page_no)
    draw_brand(c, logo_path)
    draw_title(c, page["title"], page["subtitle"])

    text_rect = Rect(44, 62, 378, 324)
    image_rect = Rect(440, 62, 476, 324)

    round_rect(c, text_rect, COLOR_PANEL, radius=22)
    style = choose_section_style(page["sections"], text_rect.w - 40, text_rect.h - 48)
    cur_y = text_rect.top - 28
    for idx, section in enumerate(page["sections"]):
        cur_y = draw_section(c, section["heading"], section["points"], text_rect.x + 20, cur_y, text_rect.w - 40, style)
        if idx < len(page["sections"]) - 1:
            cur_y -= style["section_gap"]

    image = page["image"]
    spec = screenshots[image["key"]]
    draw_image_card(c, Path(spec["file"]), spec.get("crop"), image_rect, image["caption"])
    draw_footer(c, page_no)


def draw_two_images(c: canvas.Canvas, page: dict, screenshots: dict, logo_path: Path, page_no: int):
    draw_background(c, page_no)
    draw_brand(c, logo_path)
    draw_title(c, page["title"], page["subtitle"])

    text_rect = Rect(44, 62, 300, 324)
    round_rect(c, text_rect, COLOR_PANEL, radius=22)
    style = choose_section_style(page["sections"], text_rect.w - 40, text_rect.h - 48)
    cur_y = text_rect.top - 28
    for idx, section in enumerate(page["sections"]):
        cur_y = draw_section(c, section["heading"], section["points"], text_rect.x + 20, cur_y, text_rect.w - 40, style)
        if idx < len(page["sections"]) - 1:
            cur_y -= style["section_gap"]

    image_rects = [Rect(364, 218, 272, 168), Rect(644, 218, 272, 168)]
    for rect, image in zip(image_rects, page["images"]):
        spec = screenshots[image["key"]]
        draw_image_card(c, Path(spec["file"]), spec.get("crop"), rect, image["caption"])

    if "image_strip" in page:
        strip_rects = [Rect(364, 62, 272, 136), Rect(644, 62, 272, 136)]
        for rect, image in zip(strip_rects, page["image_strip"]):
            spec = screenshots[image["key"]]
            draw_image_card(c, Path(spec["file"]), spec.get("crop"), rect, image["caption"])
    else:
        band = Rect(364, 62, 552, 128)
        round_rect(c, band, COLOR_PANEL_SOFT, stroke=COLOR_BORDER, radius=18)
        band_text = "Las pantallas operativas de NEXORA priorizan lectura rápida, estado visible y decisiones inmediatas por rol."
        band_size, band_leading = choose_paragraph_style(band_text, band.w - 40, 64, start=11.8, minimum=10.0)
        draw_wrapped(c, band_text, band.x + 22, band.top - 34, band.w - 40, font=FONT_REGULAR, size=band_size, leading=band_leading, color=COLOR_ACCENT_DARK)

    draw_footer(c, page_no)


def draw_closing(c: canvas.Canvas, page: dict, screenshots: dict, logo_path: Path, page_no: int):
    draw_background(c, page_no)
    draw_brand(c, logo_path)
    draw_title(c, page["title"], page["subtitle"])

    left = Rect(44, 62, 408, 324)
    round_rect(c, left, COLOR_PANEL, radius=22)
    style = choose_section_style(page["sections"], left.w - 40, left.h - 48, start_bullet=10.8, minimum_bullet=8.8)
    cur_y = left.top - 28
    for idx, section in enumerate(page["sections"]):
        cur_y = draw_section(c, section["heading"], section["points"], left.x + 20, cur_y, left.w - 40, style)
        if idx < len(page["sections"]) - 1:
            cur_y -= style["section_gap"]

    top_rects = [Rect(468, 236, 212, 150), Rect(692, 236, 224, 150)]
    for rect, image in zip(top_rects, page["images"]):
        spec = screenshots[image["key"]]
        draw_image_card(c, Path(spec["file"]), spec.get("crop"), rect, image["caption"])

    closing_rect = Rect(468, 62, 448, 152)
    round_rect(c, closing_rect, COLOR_PANEL_SOFT, stroke=COLOR_BORDER, radius=22)
    c.setFont(FONT_SEMIBOLD, 12.5)
    c.setFillColor(COLOR_ACCENT_DARK)
    c.drawString(closing_rect.x + 20, closing_rect.top - 30, page.get("closing_title", "Cierre"))

    blocks = page.get("closing_blocks", [])
    closing_style = choose_closing_style(blocks, closing_rect.w - 40, closing_rect.h - 48)
    cur_y = closing_rect.top - 54
    for idx, block in enumerate(blocks):
        c.setFont(FONT_SEMIBOLD, closing_style["heading_size"])
        c.setFillColor(COLOR_INK)
        c.drawString(closing_rect.x + 20, cur_y, block["heading"])
        cur_y = draw_wrapped(
            c,
            block["text"],
            closing_rect.x + 20,
            cur_y - 14,
            closing_rect.w - 40,
            font=FONT_REGULAR,
            size=closing_style["para_size"],
            leading=closing_style["para_leading"],
            color=COLOR_INK,
        )
        if idx < len(blocks) - 1:
            cur_y -= closing_style["block_gap"]

    draw_footer(c, page_no)


def build_pdf(out_path: Path, skip_render: bool = False):
    register_fonts()
    content = load_content()
    screenshots = content.SCREENSHOTS
    pages = content.PAGES
    logo_path = Path(content.LOGO_PATH)

    missing = [str(Path(spec["file"])) for spec in screenshots.values() if not Path(spec["file"]).exists()]
    if missing:
        raise FileNotFoundError("Faltan capturas requeridas. Ejecuta 'python scripts/capture_nexora_screens.py' primero.\n" + "\n".join(missing))

    ensure_parent(out_path)
    c = canvas.Canvas(str(out_path), pagesize=PAGE_SIZE)
    c.setTitle("NEXORA - Presentación comercial")
    c.setAuthor("Codex")
    c.setSubject("Software de gestión para servicio técnico y mantenimiento")

    for idx, page in enumerate(pages, start=1):
        layout = page["layout"]
        if layout == "cover":
            draw_cover(c, page, screenshots, logo_path)
        elif layout == "problem":
            draw_problem(c, page, screenshots, logo_path, idx)
        elif layout == "flow":
            draw_flow(c, page, logo_path, idx)
        elif layout == "text_image":
            draw_text_image(c, page, screenshots, logo_path, idx)
        elif layout == "two_images":
            draw_two_images(c, page, screenshots, logo_path, idx)
        elif layout == "closing":
            draw_closing(c, page, screenshots, logo_path, idx)
        else:
            raise ValueError(f"Layout no soportado: {layout}")
        c.showPage()

    c.save()

    if not skip_render:
        render_dir = DEFAULT_RENDER_DIR
        render_dir.mkdir(parents=True, exist_ok=True)
        for png in render_dir.glob("page-*.png"):
            png.unlink()
        prefix = render_dir / "page"
        subprocess.run(["pdftoppm", "-png", str(out_path), str(prefix)], check=True, cwd=REPO_ROOT)


def main():
    parser = argparse.ArgumentParser(description="Genera el PDF comercial de NEXORA.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--skip-render", action="store_true")
    args = parser.parse_args()

    build_pdf(Path(args.out), skip_render=args.skip_render)
    print(f"[pdf] generado: {args.out}")
    if not args.skip_render:
        print(f"[pdf] renders: {DEFAULT_RENDER_DIR}")


if __name__ == "__main__":
    main()
