from __future__ import annotations

import argparse
import html
import math
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A2, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "output" / "pdf" / "flujo_servicio_tecnico_sepid.pdf"
DEFAULT_RENDER_DIR = REPO_ROOT / "tmp" / "pdfs" / "flujo_servicio_tecnico_sepid"
TMP_DIR = REPO_ROOT / "tmp" / "graphviz_flujo_servicio_tecnico_sepid"

BASE_PAGE_W, PAGE_H = landscape(A2)
PAGE_W = BASE_PAGE_W + 760
PAGE_SIZE = (PAGE_W, PAGE_H)

FONT_DIR = REPO_ROOT / "docs" / "comercial" / "assets" / "fonts"
FONT_REGULAR = "SourceSans3"
FONT_SEMIBOLD = "SourceSans3-Semibold"
FONT_FILES = {
    FONT_REGULAR: FONT_DIR / "SourceSans3-Regular.ttf",
    FONT_SEMIBOLD: FONT_DIR / "SourceSans3-Semibold.ttf",
}

COLOR_BG = colors.HexColor("#F5F7FA")
COLOR_INK = colors.HexColor("#15283B")
COLOR_MUTED = colors.HexColor("#5E6C80")
COLOR_LINE = colors.HexColor("#355C7D")
COLOR_BORDER = colors.HexColor("#D7DEE6")
COLOR_PANEL = colors.white
COLOR_PANEL_SOFT = colors.HexColor("#FBFCFD")
COLOR_ACCENT = colors.HexColor("#0A84A6")
COLOR_LANE_TEXT = colors.HexColor("#5A6677")

COLOR_PROCESS = colors.HexColor("#EAF3FF")
COLOR_PROCESS_BORDER = colors.HexColor("#8CB2E1")
COLOR_DECISION = colors.HexColor("#FFF2D4")
COLOR_DECISION_BORDER = colors.HexColor("#D8B365")
COLOR_DOC = colors.HexColor("#EAF8F8")
COLOR_DOC_BORDER = colors.HexColor("#82C2C4")
COLOR_EXTERNAL = colors.HexColor("#EEF7EE")
COLOR_EXTERNAL_BORDER = colors.HexColor("#9AC39D")
COLOR_DANGER = colors.HexColor("#FDECEC")
COLOR_DANGER_BORDER = colors.HexColor("#D98F8F")
COLOR_NOTE = colors.HexColor("#FFF8E5")
COLOR_NOTE_BORDER = colors.HexColor("#E0C273")
COLOR_CONNECTOR = colors.HexColor("#F2F5FA")
COLOR_CONNECTOR_BORDER = colors.HexColor("#A6B8C9")
COLOR_DASH = colors.HexColor("#7A8DA3")

MARGIN = 36
HEADER_TOP = PAGE_H - 40
LANE_TOP = PAGE_H - 122
LANE_BOTTOM = 246
PAGE2_LANE_BOTTOM = 210
FOOTER_Y = 28
LANE_LABEL_W = 132


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def top(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


@dataclass(frozen=True)
class Lane:
    name: str
    subtitle: str
    fill: colors.Color
    head_fill: colors.Color


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    rect: Rect
    title: str
    style: str
    detail: str = ""
    state: str = ""
    title_size: float = 15.2


@dataclass(frozen=True)
class EdgeSpec:
    tail: str
    head: str
    tailport: str = "e"
    headport: str = "w"
    label: str = ""
    dashed: bool = False


@dataclass(frozen=True)
class RoutedEdge:
    points: list[tuple[float, float]]
    label: str = ""
    label_pos: tuple[float, float] | None = None
    dashed: bool = False


LANES = [
    Lane("Cliente", "Aprobación y decisiones", colors.HexColor("#F3FAF1"), colors.HexColor("#DBF0D7")),
    Lane("Recepción", "Ingreso y entrega", colors.HexColor("#F3F6FC"), colors.HexColor("#DFE8F7")),
    Lane("Logística", "Movimientos físicos", colors.HexColor("#F6F8FB"), colors.HexColor("#E8EEF6")),
    Lane("Administración", "Alta y documentación", colors.HexColor("#F3FBFB"), colors.HexColor("#DDF1F1")),
    Lane("Taller/Jefatura", "Diagnóstico y gestión", colors.HexColor("#FBFCFE"), colors.HexColor("#EBF0F7")),
    Lane("Proveedor", "Disponibilidad y entrega", colors.HexColor("#FFF9EE"), colors.HexColor("#F7E7BE")),
]


def register_fonts() -> None:
    for name, path in FONT_FILES.items():
        if not path.exists():
            raise FileNotFoundError(f"Falta la fuente requerida: {path}")
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def fit_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
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


def wrap_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw.strip():
            lines.append("")
            continue
        lines.extend(fit_text(raw, font_name, font_size, max_width))
    return lines or [""]


def lane_height(lane_top: float = LANE_TOP, lane_bottom: float = LANE_BOTTOM) -> float:
    return (lane_top - lane_bottom) / len(LANES)


def lane_rect(index: int, lane_top: float = LANE_TOP, lane_bottom: float = LANE_BOTTOM) -> Rect:
    current_lane_h = lane_height(lane_top, lane_bottom)
    y = lane_top - (index + 1) * current_lane_h
    return Rect(MARGIN, y, PAGE_W - (2 * MARGIN), current_lane_h)


def place_in_lane(
    index: int,
    cx: float,
    w: float,
    h: float,
    y_offset: float = 0,
    lane_top: float = LANE_TOP,
    lane_bottom: float = LANE_BOTTOM,
) -> Rect:
    lane = lane_rect(index, lane_top=lane_top, lane_bottom=lane_bottom)
    cy = lane.cy + y_offset
    return Rect(cx - w / 2.0, cy - h / 2.0, w, h)


def graphviz_shape(style: str) -> str:
    return "diamond" if style == "decision" else "box"


def graphviz_neato() -> str:
    candidates = [
        shutil.which("neato"),
        r"C:\Program Files\Graphviz\bin\neato.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise FileNotFoundError("No se encontro Graphviz neato.exe. Instala Graphviz para generar este PDF.")


def build_graphviz_routes(page_key: str, nodes: list[NodeSpec], edges: list[EdgeSpec]) -> list[RoutedEdge]:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    dot_path = TMP_DIR / f"{page_key}.dot"

    lines = [
        "digraph G {",
        '  graph [splines=ortho, overlap=false, outputorder=nodesfirst, esep="+18"];',
        '  node [label="", fixedsize=true, pin=true, margin=0];',
        '  edge [arrowhead=normal, arrowsize=0.72];',
    ]
    for node in nodes:
        shape = graphviz_shape(node.style)
        lines.append(
            '  "{node_id}" [shape={shape}, width={width:.4f}, height={height:.4f}, pos="{x:.2f},{y:.2f}!"];'.format(
                node_id=node.node_id,
                shape=shape,
                width=node.rect.w / 72.0,
                height=node.rect.h / 72.0,
                x=node.rect.cx,
                y=node.rect.cy,
            )
        )
    for edge in edges:
        attrs: list[str] = []
        if edge.label:
            attrs.append(f'label="{edge.label}"')
        if edge.dashed:
            attrs.append("style=dashed")
        attr_str = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f'  "{edge.tail}":{edge.tailport} -> "{edge.head}":{edge.headport}{attr_str};')
    lines.append("}")
    dot_path.write_text("\n".join(lines), encoding="utf-8")

    result = subprocess.run(
        [graphviz_neato(), "-n2", "-Tplain", str(dot_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    parsed_nodes: dict[str, tuple[float, float]] = {}
    raw_edges: list[tuple[list[tuple[float, float]], str, tuple[float, float] | None, bool]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = shlex.split(line)
        if not parts:
            continue
        kind = parts[0]
        if kind == "node":
            parsed_nodes[parts[1]] = (float(parts[2]) * 72.0, float(parts[3]) * 72.0)
            continue
        if kind != "edge":
            continue

        point_count = int(parts[3])
        coord_tokens = parts[4 : 4 + (point_count * 2)]
        points = []
        for idx in range(0, len(coord_tokens), 2):
            points.append((float(coord_tokens[idx]) * 72.0, float(coord_tokens[idx + 1]) * 72.0))

        remainder = parts[4 + (point_count * 2) :]
        label = ""
        label_pos = None
        dashed = False
        if len(remainder) >= 5:
            label = remainder[0]
            label_pos = (float(remainder[1]) * 72.0, float(remainder[2]) * 72.0)
            dashed = remainder[3] == "dashed"
        elif len(remainder) >= 2:
            dashed = remainder[0] == "dashed"

        raw_edges.append((points, label, label_pos, dashed))

    node_lookup = {node.node_id: node for node in nodes}
    shift_x = sum(node_lookup[node_id].rect.cx - pt[0] for node_id, pt in parsed_nodes.items()) / len(parsed_nodes)
    shift_y = sum(node_lookup[node_id].rect.cy - pt[1] for node_id, pt in parsed_nodes.items()) / len(parsed_nodes)

    routed: list[RoutedEdge] = []
    for edge, raw in zip(edges, raw_edges, strict=True):
        points, label, label_pos, dashed = raw
        routed.append(
            RoutedEdge(
                points=[(x + shift_x, y + shift_y) for x, y in points],
                label=label or edge.label,
                label_pos=((label_pos[0] + shift_x, label_pos[1] + shift_y) if label_pos else None),
                dashed=edge.dashed or dashed,
            )
        )
    return routed


def draw_background(c: canvas.Canvas) -> None:
    c.setFillColor(COLOR_BG)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#EAF2F8"))
    c.circle(PAGE_W - 120, PAGE_H - 64, 104, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#EEF8F1"))
    c.circle(112, PAGE_H - 42, 78, fill=1, stroke=0)
    c.setStrokeColor(colors.HexColor("#D8E1E8"))
    c.setLineWidth(1)
    c.line(MARGIN, LANE_TOP + 18, PAGE_W - MARGIN, LANE_TOP + 18)


def draw_round_rect(c: canvas.Canvas, rect: Rect, fill, stroke=COLOR_BORDER, radius: float = 14, line: float = 1) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(line)
    c.roundRect(rect.x, rect.y, rect.w, rect.h, radius, fill=1, stroke=1)


def draw_diamond(c: canvas.Canvas, rect: Rect, fill, stroke) -> None:
    pts = [
        (rect.cx, rect.top),
        (rect.x + rect.w, rect.cy),
        (rect.cx, rect.y),
        (rect.x, rect.cy),
    ]
    path = c.beginPath()
    path.moveTo(*pts[0])
    for px, py in pts[1:]:
        path.lineTo(px, py)
    path.close()
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(1.2)
    c.drawPath(path, fill=1, stroke=1)


def paragraph_markup(text: str) -> str:
    return html.escape(text).replace("\n", "<br/>")


def draw_paragraph_fit(
    c: canvas.Canvas,
    text: str,
    rect: Rect,
    font_name: str,
    start_size: float,
    min_size: float,
    color,
    leading_factor: float = 1.12,
) -> None:
    size = start_size
    while size >= min_size:
        style = ParagraphStyle(
            name=f"fit-{font_name}-{size}",
            fontName=font_name,
            fontSize=size,
            leading=size * leading_factor,
            textColor=color,
            alignment=TA_CENTER,
            spaceAfter=0,
            spaceBefore=0,
        )
        para = Paragraph(paragraph_markup(text), style)
        _, height = para.wrap(rect.w, rect.h)
        if height <= rect.h:
            para.drawOn(c, rect.x, rect.y + ((rect.h - height) / 2.0))
            return
        size -= 0.4

    fallback_style = ParagraphStyle(
        name="fit-fallback",
        fontName=font_name,
        fontSize=min_size,
        leading=min_size * leading_factor,
        textColor=color,
        alignment=TA_CENTER,
        spaceAfter=0,
        spaceBefore=0,
    )
    para = Paragraph(paragraph_markup(text), fallback_style)
    _, height = para.wrap(rect.w, rect.h)
    para.drawOn(c, rect.x, rect.y + max(0.0, (rect.h - height) / 2.0))


def draw_pill(c: canvas.Canvas, x: float, y: float, text: str, fill, stroke, text_color) -> None:
    width = pdfmetrics.stringWidth(text, FONT_SEMIBOLD, 9.6) + 22
    rect = Rect(x, y, width, 18)
    draw_round_rect(c, rect, fill, stroke=stroke, radius=7, line=0.8)
    c.setFillColor(text_color)
    c.setFont(FONT_SEMIBOLD, 9.6)
    c.drawCentredString(rect.cx, rect.y + 6.3, text)


def draw_node(c: canvas.Canvas, node: NodeSpec) -> None:
    if node.style == "process":
        fill, stroke = COLOR_PROCESS, COLOR_PROCESS_BORDER
    elif node.style == "decision":
        fill, stroke = COLOR_DECISION, COLOR_DECISION_BORDER
    elif node.style == "doc":
        fill, stroke = COLOR_DOC, COLOR_DOC_BORDER
    elif node.style == "external":
        fill, stroke = COLOR_EXTERNAL, COLOR_EXTERNAL_BORDER
    elif node.style == "danger":
        fill, stroke = COLOR_DANGER, COLOR_DANGER_BORDER
    elif node.style == "note":
        fill, stroke = COLOR_NOTE, COLOR_NOTE_BORDER
    else:
        fill, stroke = COLOR_CONNECTOR, COLOR_CONNECTOR_BORDER

    if node.style == "decision":
        draw_diamond(c, node.rect, fill, stroke)
        text_rect = Rect(node.rect.x + 16, node.rect.y + 16, node.rect.w - 32, node.rect.h - 32)
        draw_paragraph_fit(c, node.title, text_rect, FONT_SEMIBOLD, 14.8, 11.0, COLOR_INK)
        return

    if node.style == "note":
        c.setDash(4, 3)
        draw_round_rect(c, node.rect, fill, stroke=stroke, radius=12, line=1)
        c.setDash()
    else:
        draw_round_rect(c, node.rect, fill, stroke=stroke, radius=12, line=1)

    state_h = 18 if node.state else 0
    detail_h = 16 if node.detail else 0
    state_y = node.rect.y + 6
    detail_y = state_y + state_h + (4 if node.state and node.detail else 0)
    title_bottom = detail_y + detail_h + (6 if node.detail else 0)

    title_rect = Rect(
        node.rect.x + 10,
        title_bottom,
        node.rect.w - 20,
        node.rect.top - title_bottom - 10,
    )
    draw_paragraph_fit(c, node.title, title_rect, FONT_SEMIBOLD, node.title_size, 11.0, COLOR_INK)

    if node.detail:
        detail_rect = Rect(node.rect.x + 10, detail_y, node.rect.w - 20, detail_h)
        draw_paragraph_fit(c, node.detail, detail_rect, FONT_REGULAR, 10.4, 9.0, COLOR_MUTED, leading_factor=1.0)
    if node.state:
        draw_pill(c, node.rect.x + 10, state_y, node.state, colors.white, stroke, COLOR_INK)


def draw_header(c: canvas.Canvas, title: str, subtitle: str, page_no: int, page_count: int) -> None:
    c.setFillColor(COLOR_INK)
    c.setFont(FONT_SEMIBOLD, 24)
    c.drawString(MARGIN, HEADER_TOP, title)
    c.setFont(FONT_REGULAR, 12)
    c.setFillColor(COLOR_MUTED)
    c.drawString(MARGIN, HEADER_TOP - 20, subtitle)

    tag = Rect(PAGE_W - 240, HEADER_TOP - 20, 212, 38)
    draw_round_rect(c, tag, COLOR_PANEL, stroke=COLOR_BORDER, radius=12)
    c.setFillColor(COLOR_MUTED)
    c.setFont(FONT_REGULAR, 10.0)
    c.drawString(tag.x + 12, tag.y + 22, "Swimlane interno para directivos.")
    c.drawString(tag.x + 12, tag.y + 9, f"Pagina {page_no} de {page_count}.")


def draw_lanes(c: canvas.Canvas, lane_top: float = LANE_TOP, lane_bottom: float = LANE_BOTTOM) -> None:
    for idx, lane in enumerate(LANES):
        rect = lane_rect(idx, lane_top=lane_top, lane_bottom=lane_bottom)
        label = Rect(rect.x, rect.y, LANE_LABEL_W, rect.h)
        body = Rect(rect.x + LANE_LABEL_W, rect.y, rect.w - LANE_LABEL_W, rect.h)
        draw_round_rect(c, label, lane.head_fill, stroke=lane.head_fill, radius=12)
        draw_round_rect(c, body, lane.fill, stroke=COLOR_BORDER, radius=12)
        c.setFillColor(COLOR_INK)
        c.setFont(FONT_SEMIBOLD, 12.8)
        c.drawString(label.x + 12, label.cy + 10, lane.name)
        c.setFont(FONT_REGULAR, 9.0)
        c.setFillColor(COLOR_LANE_TEXT)
        c.drawString(label.x + 12, label.cy - 5, lane.subtitle)


def draw_footer(c: canvas.Canvas) -> None:
    c.setStrokeColor(COLOR_BORDER)
    c.line(MARGIN, FOOTER_Y, PAGE_W - MARGIN, FOOTER_Y)
    c.setFont(FONT_REGULAR, 8.6)
    c.setFillColor(COLOR_MUTED)
    c.drawString(MARGIN, 11, "Documento interno - SEPID")
    c.drawRightString(PAGE_W - MARGIN, 11, "Generado desde scripts/build_flujo_servicio_tecnico_sepid_pdf.py")


def draw_arrow(c: canvas.Canvas, points: list[tuple[float, float]], color=COLOR_LINE, width: float = 1.9, dashed: bool = False) -> None:
    if len(points) < 2:
        return
    c.setStrokeColor(color)
    c.setLineWidth(width)
    c.setDash(4, 3 if dashed else 0)
    if not dashed:
        c.setDash()
    for idx in range(len(points) - 1):
        x1, y1 = points[idx]
        x2, y2 = points[idx + 1]
        c.line(x1, y1, x2, y2)
    c.setDash()

    x1, y1 = points[-2]
    x2, y2 = points[-1]
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy) or 1.0
    ux = dx / length
    uy = dy / length
    px = -uy
    py = ux
    arrow_len = 9.5
    arrow_half = 3.6
    p1 = (x2, y2)
    p2 = (x2 - arrow_len * ux + arrow_half * px, y2 - arrow_len * uy + arrow_half * py)
    p3 = (x2 - arrow_len * ux - arrow_half * px, y2 - arrow_len * uy - arrow_half * py)
    path = c.beginPath()
    path.moveTo(*p1)
    path.lineTo(*p2)
    path.lineTo(*p3)
    path.close()
    c.setFillColor(color)
    c.setStrokeColor(color)
    c.drawPath(path, fill=1, stroke=0)


def draw_edge_label(c: canvas.Canvas, x: float, y: float, text: str) -> None:
    width = pdfmetrics.stringWidth(text, FONT_SEMIBOLD, 9.2) + 14
    rect = Rect(x - width / 2.0, y - 6.0, width, 15)
    draw_round_rect(c, rect, colors.white, stroke=COLOR_BORDER, radius=6, line=0.7)
    c.setFillColor(COLOR_INK)
    c.setFont(FONT_SEMIBOLD, 9.2)
    c.drawCentredString(rect.cx, rect.y + 4.9, text)


def anchor_point(node: NodeSpec, port: str) -> tuple[float, float]:
    if port == "n":
        return (node.rect.cx, node.rect.top)
    if port == "s":
        return (node.rect.cx, node.rect.y)
    if port == "e":
        return (node.rect.x + node.rect.w, node.rect.cy)
    if port == "w":
        return (node.rect.x, node.rect.cy)
    raise ValueError(f"Puerto no soportado: {port}")


def label_mid(a: tuple[float, float], b: tuple[float, float], dx: float = 0, dy: float = 0) -> tuple[float, float]:
    return (((a[0] + b[0]) / 2.0) + dx, ((a[1] + b[1]) / 2.0) + dy)


def route(points: list[tuple[float, float]], label: str = "", label_pos: tuple[float, float] | None = None, dashed: bool = False) -> RoutedEdge:
    return RoutedEdge(points=points, label=label, label_pos=label_pos, dashed=dashed)


def build_page_one_routes(nodes: list[NodeSpec]) -> list[RoutedEdge]:
    node = {item.node_id: item for item in nodes}
    merge_x = node["rec_rem"].rect.x + node["rec_rem"].rect.w + 74
    admin_turn_x = node["adm_os"].rect.x - 62
    diag_turn_x = node["diag"].rect.x - 78
    na_repair_y = node["repair"].rect.y - 22
    approve_entry_y = node["approve"].rect.top + 28
    approve_no_x = node["approve"].rect.x - 86
    release_left_corridor_x = node["release"].rect.x - 20
    release_right_corridor_x = node["release"].rect.x + node["release"].rect.w + 22

    return [
        route([anchor_point(node["rec_in"], "e"), anchor_point(node["rec_rem"], "w")]),
        route([anchor_point(node["log_in"], "e"), anchor_point(node["log_rem"], "w")]),
        route(
            [
                anchor_point(node["rec_rem"], "e"),
                (merge_x, node["rec_rem"].rect.cy),
                (merge_x, node["adm_os"].rect.cy),
                (admin_turn_x, node["adm_os"].rect.cy),
                anchor_point(node["adm_os"], "w"),
            ]
        ),
        route(
            [
                anchor_point(node["log_rem"], "e"),
                (merge_x, node["log_rem"].rect.cy),
                (merge_x, node["adm_os"].rect.cy),
                (admin_turn_x, node["adm_os"].rect.cy),
                anchor_point(node["adm_os"], "w"),
            ]
        ),
        route(
            [
                anchor_point(node["adm_os"], "e"),
                (diag_turn_x, node["adm_os"].rect.cy),
                (diag_turn_x, node["diag"].rect.cy),
                anchor_point(node["diag"], "w"),
            ]
        ),
        route([anchor_point(node["diag"], "e"), anchor_point(node["na"], "w")]),
        route(
            [anchor_point(node["na"], "e"), anchor_point(node["stock"], "w")],
            label="No",
            label_pos=label_mid(anchor_point(node["na"], "e"), anchor_point(node["stock"], "w"), 0, 18),
        ),
        route(
            [
                anchor_point(node["na"], "s"),
                (node["na"].rect.cx, na_repair_y),
                (node["repair"].rect.cx, na_repair_y),
                anchor_point(node["repair"], "s"),
            ],
            label="Si",
            label_pos=(node["na"].rect.cx + 72, na_repair_y + 12),
        ),
        route(
            [anchor_point(node["stock"], "e"), anchor_point(node["emit"], "w")],
            label="Si",
            label_pos=label_mid(anchor_point(node["stock"], "e"), anchor_point(node["emit"], "w"), 0, 18),
        ),
        route(
            [
                anchor_point(node["stock"], "s"),
                (node["stock"].rect.cx, node["page2"].rect.cy),
                anchor_point(node["page2"], "w"),
            ],
            label="No",
            label_pos=(node["stock"].rect.cx + 28, node["page2"].rect.cy + 18),
        ),
        route(
            [
                anchor_point(node["emit"], "e"),
                (node["emit"].rect.x + node["emit"].rect.w, approve_entry_y),
                (node["approve"].rect.cx, approve_entry_y),
                anchor_point(node["approve"], "n"),
            ]
        ),
        route(
            [
                anchor_point(node["approve"], "s"),
                (node["approve"].rect.cx, node["repair"].rect.top + 24),
                (node["repair"].rect.cx, node["repair"].rect.top + 24),
                anchor_point(node["repair"], "n"),
            ],
            label="Si",
            label_pos=(node["repair"].rect.cx + 42, node["repair"].rect.top + 32),
        ),
        route(
            [
                anchor_point(node["approve"], "w"),
                (approve_no_x, node["approve"].rect.cy),
                (approve_no_x, node["charge"].rect.cy),
                anchor_point(node["charge"], "e"),
            ],
            label="No",
            label_pos=(approve_no_x - 18, (node["approve"].rect.cy + node["charge"].rect.cy) / 2.0),
        ),
        route(
            [
                anchor_point(node["charge"], "e"),
                (release_left_corridor_x, node["charge"].rect.cy),
                (release_left_corridor_x, node["out"].rect.cy),
                anchor_point(node["out"], "w"),
            ],
            label="Sin reparar",
            label_pos=(release_left_corridor_x - 4, node["out"].rect.cy - 18),
        ),
        route(
            [
                anchor_point(node["repair"], "e"),
                (release_left_corridor_x, node["repair"].rect.cy),
                (release_left_corridor_x, node["release"].rect.cy),
                anchor_point(node["release"], "w"),
            ]
        ),
        route(
            [
                anchor_point(node["release"], "e"),
                (release_right_corridor_x, node["release"].rect.cy),
                (release_right_corridor_x, node["out"].rect.cy),
                anchor_point(node["out"], "w"),
            ]
        ),
    ]


def build_page_two_routes(nodes: list[NodeSpec]) -> list[RoutedEdge]:
    node = {item.node_id: item for item in nodes}
    ask_turn_x = node["avail"].rect.x - 42
    avail_emit_x = node["wait"].rect.x + node["wait"].rect.w + 24
    wait_requery_x = node["avail"].rect.x - 86
    avail_emit_y = node["wait"].rect.y - 18
    wait_drop_x = node["charge"].rect.x - 44
    wait_requery_y = node["avail"].rect.y - 18
    retire_turn_x = node["emit"].rect.x + node["emit"].rect.w + 40
    client_to_charge_x = node["charge"].rect.x + node["charge"].rect.w + 44
    client_to_dispatch_x = node["out"].rect.x - 4
    dispatch_bypass_x = node["release"].rect.x - 10
    repair_to_release_x = node["release"].rect.x - 16
    release_to_out_x = node["release"].rect.x + node["release"].rect.w + 6
    reject_corridor_y = node["release"].rect.y - 38
    no_repair_corridor_y = node["repair"].rect.y - 26

    return [
        route([anchor_point(node["from_p1"], "e"), anchor_point(node["ask"], "w")]),
        route(
            [
                anchor_point(node["ask"], "e"),
                (ask_turn_x, node["ask"].rect.cy),
                (ask_turn_x, node["avail"].rect.cy),
                anchor_point(node["avail"], "w"),
            ]
        ),
        route(
            [
                anchor_point(node["avail"], "e"),
                (node["avail"].rect.x + node["avail"].rect.w + 14, node["avail"].rect.cy),
                (node["avail"].rect.x + node["avail"].rect.w + 14, avail_emit_y),
                (avail_emit_x, avail_emit_y),
                (avail_emit_x, node["emit"].rect.cy),
                anchor_point(node["emit"], "w"),
            ],
            label="Si",
            label_pos=(avail_emit_x + 16, node["emit"].rect.cy + 18),
        ),
        route(
            [
                anchor_point(node["avail"], "n"),
                (node["avail"].rect.cx, node["wait_or_retire"].rect.cy),
                anchor_point(node["wait_or_retire"], "w"),
            ],
            label="No",
            label_pos=(node["avail"].rect.cx - 18, node["wait_or_retire"].rect.cy - 16),
        ),
        route(
            [
                anchor_point(node["wait_or_retire"], "s"),
                (node["wait_or_retire"].rect.cx, node["wait_or_retire"].rect.y - 46),
                (wait_drop_x, node["wait_or_retire"].rect.y - 46),
                (wait_drop_x, node["wait"].rect.top + 24),
                (node["wait"].rect.cx, node["wait"].rect.top + 24),
                anchor_point(node["wait"], "n"),
            ],
            label="Esperar",
            label_pos=(wait_drop_x + 20, node["wait"].rect.top + 36),
        ),
        route(
            [
                anchor_point(node["wait"], "s"),
                (node["wait"].rect.cx, wait_requery_y),
                (wait_requery_x, wait_requery_y),
                (wait_requery_x, node["ask"].rect.cy),
                anchor_point(node["ask"], "e"),
            ],
            label="Reconsulta",
            label_pos=(wait_requery_x + 12, (wait_requery_y + node["ask"].rect.cy) / 2.0),
            dashed=True,
        ),
        route(
            [
                anchor_point(node["wait_or_retire"], "s"),
                (node["wait_or_retire"].rect.cx, node["wait_or_retire"].rect.y - 48),
                (retire_turn_x, node["wait_or_retire"].rect.y - 48),
                (retire_turn_x, no_repair_corridor_y),
                (node["no_repair"].rect.cx, no_repair_corridor_y),
                anchor_point(node["no_repair"], "s"),
            ],
            label="Retirar",
            label_pos=(retire_turn_x + 12, no_repair_corridor_y + 18),
        ),
        route(
            [
                anchor_point(node["emit"], "e"),
                (node["client_ok"].rect.cx, node["emit"].rect.cy),
                (node["client_ok"].rect.cx, node["client_ok"].rect.top + 28),
                anchor_point(node["client_ok"], "n"),
            ]
        ),
        route(
            [
                anchor_point(node["client_ok"], "e"),
                (client_to_dispatch_x, node["client_ok"].rect.cy),
                (client_to_dispatch_x, node["dispatch"].rect.cy),
                anchor_point(node["dispatch"], "e"),
            ],
            label="Compra / pedido",
            label_pos=(client_to_dispatch_x - 12, node["dispatch"].rect.cy + 18),
        ),
        route(
            [
                anchor_point(node["client_ok"], "w"),
                (client_to_charge_x, node["client_ok"].rect.cy),
                (client_to_charge_x, node["charge"].rect.cy),
                anchor_point(node["charge"], "e"),
            ],
            label="No",
            label_pos=(client_to_charge_x - 18, (node["client_ok"].rect.cy + node["charge"].rect.cy) / 2.0),
        ),
        route(
            [
                anchor_point(node["dispatch"], "e"),
                (dispatch_bypass_x, node["dispatch"].rect.cy),
                (dispatch_bypass_x, node["log_recv"].rect.cy),
                anchor_point(node["log_recv"], "e"),
            ]
        ),
        route([anchor_point(node["log_recv"], "s"), anchor_point(node["repair"], "n")]),
        route(
            [
                anchor_point(node["repair"], "e"),
                (repair_to_release_x, node["repair"].rect.cy),
                (repair_to_release_x, node["release"].rect.cy),
                anchor_point(node["release"], "w"),
            ]
        ),
        route(
            [
                anchor_point(node["release"], "e"),
                (release_to_out_x, node["release"].rect.cy),
                (release_to_out_x, node["out"].rect.cy),
                anchor_point(node["out"], "w"),
            ]
        ),
        route(
            [
                anchor_point(node["charge"], "e"),
                (client_to_charge_x, node["charge"].rect.cy),
                (client_to_charge_x, reject_corridor_y),
                (release_to_out_x, reject_corridor_y),
                (release_to_out_x, node["out"].rect.cy),
                anchor_point(node["out"], "w"),
            ],
            label="Sin reparar",
            label_pos=(release_to_out_x - 6, reject_corridor_y + 18),
        ),
        route(
            [
                anchor_point(node["no_repair"], "e"),
                (anchor_point(node["no_repair"], "e")[0], no_repair_corridor_y),
                (release_to_out_x, no_repair_corridor_y),
                (release_to_out_x, node["out"].rect.cy),
                anchor_point(node["out"], "w"),
            ],
            label="Sin cobro diag.",
            label_pos=(release_to_out_x - 10, no_repair_corridor_y + 18),
        ),
    ]


def draw_note_card(c: canvas.Canvas, rect: Rect, title: str, lines: list[str]) -> None:
    draw_round_rect(c, rect, COLOR_PANEL_SOFT, stroke=COLOR_BORDER, radius=12)
    c.setFillColor(COLOR_INK)
    c.setFont(FONT_SEMIBOLD, 10.2)
    c.drawString(rect.x + 10, rect.top - 18, title)
    y = rect.top - 34
    for item in lines:
        wrapped = wrap_text(item, FONT_REGULAR, 8.0, rect.w - 24)
        c.setFillColor(COLOR_ACCENT)
        c.circle(rect.x + 10, y - 2, 1.8, fill=1, stroke=0)
        c.setFillColor(COLOR_INK)
        for line in wrapped:
            c.setFont(FONT_REGULAR, 8.0)
            c.drawString(rect.x + 18, y, line)
            y -= 9.0
        y -= 3.5


def page_one_nodes() -> list[NodeSpec]:
    intake_col = 272
    receipt_col = 470
    admin_col = 820
    diag_col = 1080
    na_col = 1290
    stock_col = 1470
    emit_col = 1630
    charge_col = 1590
    approve_col = 1850
    repair_col = 1820
    release_col = 2040
    out_col = 2260

    return [
        NodeSpec("rec_in", place_in_lane(1, intake_col, 122, 46), "Ingreso por\nRecepción", "external"),
        NodeSpec("rec_rem", place_in_lane(1, receipt_col, 146, 48), "Remito de\ningreso", "doc"),
        NodeSpec("log_in", place_in_lane(2, intake_col, 122, 46), "Ingreso por\nLogística", "external"),
        NodeSpec("log_rem", place_in_lane(2, receipt_col, 146, 48), "Remito de\ningreso", "doc"),
        NodeSpec("adm_os", place_in_lane(3, admin_col, 164, 64), "Alta OS /\ningreso en\nsistema", "doc", state="[ingresado]", title_size=15.4),
        NodeSpec("diag", place_in_lane(4, diag_col, 188, 66), "Diagnóstico y\ndefinición\ntécnica", "process", state="[diagnosticado]", title_size=15.4),
        NodeSpec("na", place_in_lane(4, na_col, 114, 114), "Presupuesto\nno aplica?", "decision"),
        NodeSpec("stock", place_in_lane(4, stock_col, 114, 114), "Hay repuesto\npropio?", "decision"),
        NodeSpec("emit", place_in_lane(4, emit_col, 184, 76), "Emitir\npresupuesto", "process", "Precio cliente", "[presupuestado]", 15.8),
        NodeSpec("approve", place_in_lane(0, approve_col, 124, 124), "Aprueba\npresupuesto?", "decision"),
        NodeSpec("repair", place_in_lane(4, repair_col, 174, 66, y_offset=-10), "Reparación y\ntest técnico", "process", state="[reparar -> reparado]", title_size=15.4),
        NodeSpec("charge", place_in_lane(3, charge_col, 150, 54), "Cobro de\ndiagnóstico", "danger"),
        NodeSpec("release", place_in_lane(3, release_col, 162, 64), "Liberación /\nremito /\nfactura", "doc", state="[liberado]", title_size=14.8),
        NodeSpec("out", place_in_lane(1, out_col, 154, 56), "Entrega al\ncliente", "external", "Retiro por mostrador", title_size=14.8),
        NodeSpec("page2", place_in_lane(5, emit_col, 178, 50), "Ver página 2", "connector", "Subflujo repuesto / proveedor", title_size=14.0),
    ]


def page_one_edges() -> list[EdgeSpec]:
    return [
        EdgeSpec("rec_in", "rec_rem", "e", "w"),
        EdgeSpec("log_in", "log_rem", "e", "w"),
        EdgeSpec("rec_rem", "adm_os", "e", "w"),
        EdgeSpec("log_rem", "adm_os", "e", "w"),
        EdgeSpec("adm_os", "diag", "e", "w"),
        EdgeSpec("diag", "na", "e", "w"),
        EdgeSpec("na", "stock", "e", "w", "No"),
        EdgeSpec("na", "repair", "s", "w", "Si"),
        EdgeSpec("stock", "emit", "e", "w", "Si"),
        EdgeSpec("stock", "page2", "s", "w", "No"),
        EdgeSpec("emit", "approve", "n", "w"),
        EdgeSpec("approve", "repair", "e", "e", "Si"),
        EdgeSpec("approve", "charge", "s", "e", "No"),
        EdgeSpec("charge", "out", "e", "w", "Sin reparar"),
        EdgeSpec("repair", "release", "e", "w"),
        EdgeSpec("release", "out", "n", "s"),
    ]


def page_two_nodes() -> list[NodeSpec]:
    lane_bottom = PAGE2_LANE_BOTTOM
    intake_col = 272
    ask_col = 660
    avail_col = 940
    wait_note_col = 1160
    wait_decision_col = 1310
    emit_col = 1490
    client_col = 1600
    charge_col = 1360
    no_repair_col = 1770
    supplier_col = 1940
    release_col = 2115
    out_col = 2268

    return [
        NodeSpec("from_p1", place_in_lane(4, intake_col, 164, 56, y_offset=14, lane_bottom=lane_bottom), "Desde página 1", "connector", "Sin stock propio", title_size=14.4),
        NodeSpec("ask", place_in_lane(4, ask_col, 186, 62, y_offset=14, lane_bottom=lane_bottom), "Consultar\nproveedor", "process", "Disponibilidad y precio", title_size=15.0),
        NodeSpec("avail", place_in_lane(5, avail_col, 122, 122, y_offset=8, lane_bottom=lane_bottom), "Hay stock y\nprecio viable?", "decision"),
        NodeSpec("wait_or_retire", place_in_lane(0, wait_decision_col, 124, 124, y_offset=-8, lane_bottom=lane_bottom), "Esperar\nrepuesto\no retirar?", "decision"),
        NodeSpec("wait", place_in_lane(5, wait_note_col, 204, 56, y_offset=-16, lane_bottom=lane_bottom), "En espera de repuesto", "note", "Estado operativo manual", title_size=14.4),
        NodeSpec("emit", place_in_lane(4, emit_col, 204, 80, y_offset=14, lane_bottom=lane_bottom), "Trasladar costo /\nemitir presupuesto", "process", "Precio cliente", "[presupuestado]", 15.4),
        NodeSpec("client_ok", place_in_lane(0, client_col, 124, 124, y_offset=-8, lane_bottom=lane_bottom), "Aprueba\npresupuesto?", "decision"),
        NodeSpec("charge", place_in_lane(3, charge_col, 156, 56, lane_bottom=lane_bottom), "Cobro de\ndiagnóstico", "danger"),
        NodeSpec("no_repair", place_in_lane(4, no_repair_col, 166, 60, y_offset=-8, lane_bottom=lane_bottom), "No reparado", "danger", title_size=15.0),
        NodeSpec("dispatch", place_in_lane(5, supplier_col, 166, 56, y_offset=-18, lane_bottom=lane_bottom), "Proveedor\ndespacha\nrepuesto", "external", title_size=14.6),
        NodeSpec("log_recv", place_in_lane(2, supplier_col, 184, 60, lane_bottom=lane_bottom), "Logística recibe\ny entrega a\ntaller", "process", title_size=14.4),
        NodeSpec("repair", place_in_lane(4, supplier_col, 166, 66, y_offset=-20, lane_bottom=lane_bottom), "Reparación y\ntest técnico", "process", state="[reparar -> reparado]", title_size=15.2),
        NodeSpec("release", place_in_lane(3, release_col, 160, 64, lane_bottom=lane_bottom), "Liberación /\nremito /\nfactura", "doc", state="[liberado]", title_size=14.8),
        NodeSpec("out", place_in_lane(1, out_col, 142, 56, lane_bottom=lane_bottom), "Entrega al\ncliente", "external", "Retiro por mostrador", title_size=14.4),
    ]


def page_two_edges() -> list[EdgeSpec]:
    return [
        EdgeSpec("from_p1", "ask", "e", "w"),
        EdgeSpec("ask", "avail", "e", "w"),
        EdgeSpec("avail", "emit", "e", "w", "Si"),
        EdgeSpec("avail", "wait_or_retire", "n", "w", "No"),
        EdgeSpec("wait_or_retire", "wait", "w", "w", "Esperar"),
        EdgeSpec("wait", "ask", "w", "e", "Reconsulta", dashed=True),
        EdgeSpec("wait_or_retire", "no_repair", "e", "w", "Retirar"),
        EdgeSpec("emit", "client_ok", "e", "w"),
        EdgeSpec("client_ok", "dispatch", "e", "w", "Compra / pedido"),
        EdgeSpec("client_ok", "charge", "s", "e", "No"),
        EdgeSpec("dispatch", "log_recv", "n", "s"),
        EdgeSpec("log_recv", "repair", "s", "n"),
        EdgeSpec("repair", "release", "e", "w"),
        EdgeSpec("release", "out", "n", "s"),
        EdgeSpec("charge", "out", "e", "w", "Sin reparar"),
        EdgeSpec("no_repair", "out", "e", "w", "Sin cobro diag."),
    ]


def draw_page_one(c: canvas.Canvas) -> None:
    draw_background(c)
    draw_header(
        c,
        "Flujo 1 - Circuito principal",
        "Ingreso, alta administrativa, diagnóstico, presupuesto directo, aprobación, reparación y entrega.",
        1,
        2,
    )
    draw_lanes(c)

    nodes = page_one_nodes()
    routes = build_page_one_routes(nodes)
    for route in routes:
        draw_arrow(c, route.points, dashed=route.dashed)
        if route.label and route.label_pos:
            draw_edge_label(c, route.label_pos[0], route.label_pos[1], route.label)
    for node in nodes:
        draw_node(c, node)

    panel = Rect(MARGIN, 58, PAGE_W - (2 * MARGIN), 116)
    draw_round_rect(c, panel, COLOR_PANEL, stroke=COLOR_BORDER, radius=14)
    c.setFillColor(COLOR_INK)
    c.setFont(FONT_SEMIBOLD, 10.2)
    c.drawString(panel.x + 12, panel.top - 18, "Por qué esta página se simplificó")
    draw_note_card(
        c,
        Rect(panel.x + 10, panel.y + 12, panel.w - 20, 74),
        "",
        [
            "El subflujo de proveedor y espera se mueve a la página 2 para evitar cruces y líneas sobre nodos.",
            "La página 1 muestra solo el circuito principal y la aprobación comercial directa.",
            "La salida de rechazo mantiene cobro de diagnóstico y entrega sin reparar.",
        ],
    )
    draw_footer(c)


def draw_page_two(c: canvas.Canvas) -> None:
    draw_background(c)
    draw_header(
        c,
        "Flujo 2 - Repuesto, proveedor y excepciones",
        "Consulta al proveedor, espera, retiro sin reparar, compra aprobada y ramas laterales compactas.",
        2,
        2,
    )
    draw_lanes(c, lane_bottom=PAGE2_LANE_BOTTOM)

    nodes = page_two_nodes()
    routes = build_page_two_routes(nodes)
    for route in routes:
        draw_arrow(c, route.points, dashed=route.dashed)
        if route.label and route.label_pos:
            draw_edge_label(c, route.label_pos[0], route.label_pos[1], route.label)
    for node in nodes:
        draw_node(c, node)

    card_w = (PAGE_W - (2 * MARGIN) - 24) / 3.0
    card_y = 52
    card_h = 96
    cards = [
        Rect(MARGIN, card_y, card_w, card_h),
        Rect(MARGIN + card_w + 12, card_y, card_w, card_h),
        Rect(MARGIN + (card_w + 12) * 2, card_y, card_w, card_h),
    ]
    draw_note_card(c, cards[0], "Derivación externa", ["Se deriva a tercero, se trabaja afuera y luego retorna a taller para continuar o cerrar."])
    draw_note_card(c, cards[1], "Baja", ["Si se define baja, sale del circuito normal y queda cerrado como baja."])
    draw_note_card(c, cards[2], "Alquilado / entrega especial", ["Compacta salidas por logística o circuitos especiales que no pasan por retiro clásico."])
    draw_footer(c)


def build_pdf(out_path: Path, skip_render: bool = False) -> None:
    register_fonts()
    ensure_parent(out_path)

    c = canvas.Canvas(str(out_path), pagesize=PAGE_SIZE)
    c.setTitle("Diagrama integral del servicio técnico SEPID")
    c.setAuthor("Codex")
    c.setSubject("Flujo interno completo del servicio técnico")

    draw_page_one(c)
    c.showPage()
    draw_page_two(c)
    c.showPage()
    c.save()

    if skip_render:
        return

    DEFAULT_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    for png in DEFAULT_RENDER_DIR.glob("page-*.png"):
        png.unlink()
    prefix = DEFAULT_RENDER_DIR / "page"
    subprocess.run(["pdftoppm", "-png", str(out_path), str(prefix)], check=True, cwd=REPO_ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera el PDF interno del flujo integral del servicio técnico SEPID.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--skip-render", action="store_true")
    args = parser.parse_args()

    build_pdf(Path(args.out), skip_render=args.skip_render)
    if not args.skip_render:
        reader = PdfReader(str(args.out))
        print(f"[pdf] generado: {args.out} ({len(reader.pages)} páginas)")
        print(f"[pdf] renders: {DEFAULT_RENDER_DIR}")
    else:
        print(f"[pdf] generado: {args.out}")


if __name__ == "__main__":
    main()
