import os
import unicodedata
from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.http import HttpResponse

from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from ..pdf import render_quote_pdf
from ..notifications import emit_notification, notify_presupuesto_aprobado
from ..permissions import user_has_permission
from ..rejected_budget import has_rejected_budget_charge_schema
from ..serializers import (
    QuoteDetailSerializer,
)
from .helpers import (
    _motivo_is_cotizacion_equipo,
    q,
    exec_void,
    exec_returning,
    money,
    require_roles,
    require_roles_strict,
    _set_audit_user,
    _frontend_url,
    _email_append_footer_text,
)
from ..repuestos import get_repuestos_config, calc_costo_ars, calc_precio_venta


def _can_view_costs(user) -> bool:
    return user_has_permission(user, "action.presupuesto.view_costs")


def _mask_costs(payload: dict, allow_costs: bool) -> dict:
    if allow_costs:
        return payload
    for it in payload.get("items") or []:
        it["costo_u_neto"] = None
        it["costo_total_neto"] = None
    return payload


DEFAULT_AUTORIZADO_POR = "Cliente"
DEFAULT_FORMA_PAGO = "30 F.F."
DEFAULT_PLAZO_ENTREGA_TXT = "< 5 D\u00cdAS H\u00c1BILES"
DEFAULT_GARANTIA_TXT = "90 D\u00cdAS"
DEFAULT_MANT_OFERTA_TXT = "7 D\u00cdAS"
EMITTED_QUOTE_STATES = {"presupuestado", "emitido"}


def _quote_estado_str(value) -> str:
    return str(value or "").strip()


def _is_emitted_quote_state(value) -> bool:
    return _quote_estado_str(value) in EMITTED_QUOTE_STATES


def _quote_estado_for_response(value) -> str:
    estado = _quote_estado_str(value)
    return "presupuestado" if estado == "emitido" else estado


def _clear_transaction_rollback():
    try:
        transaction.set_rollback(False)
    except Exception:
        pass


def _clean_text_or_default(value, default: str) -> str:
    cleaned = (value or "").strip()
    return cleaned or default


def _parse_rejected_budget_charge_or_raise(raw_value, *, required: bool) -> object:
    if raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == ""):
        if required:
            raise ValidationError("presupuesto_rechazado_cobro_neto requerido")
        return None
    try:
        cobro_neto = money(raw_value)
    except (TypeError, ValueError):
        raise ValidationError("presupuesto_rechazado_cobro_neto debe ser numérico")
    if cobro_neto < 0:
        raise ValidationError("presupuesto_rechazado_cobro_neto no puede ser negativo")
    return cobro_neto


def _get_stock_alert_recipients():
    rows = q(
        """
        SELECT DISTINCT LOWER(email) AS email
        FROM users
        WHERE activo
          AND LOWER(rol) IN ('jefe', 'jefe_veedor')
          AND COALESCE(email, '') <> ''
        """,
        [],
    ) or []
    return [r.get("email") for r in rows if r.get("email")]


def _send_stock_min_alerts(items: list[dict]):
    if not items:
        return
    recipients = _get_stock_alert_recipients()
    try:
        title = f"Stock mínimo - {len(items)} repuesto(s)"
        body_lines = ["Se alcanzó el stock mínimo en:", ""]
        for it in items:
            body_lines.append(f"- {it.get('codigo') or '-'} | {it.get('nombre') or '-'}")
            body_lines.append(f"  Stock: {it.get('stock_on_hand')} | Mínimo: {it.get('stock_min')}")
        emit_notification(
            "stock_minimo",
            emails=recipients,
            title=title,
            body="\n".join(body_lines).strip(),
            href="/catalogo/repuestos",
            severity="warning",
            entity_type="repuesto",
            entity_id=str(items[0].get("id") or items[0].get("codigo") or "stock"),
            dedupe_key=f"stock_minimo:{','.join(str(it.get('id') or it.get('codigo') or it.get('nombre') or '-') for it in items)}:{len(items)}",
            payload={"items": items},
        )
    except Exception:
        try:
            transaction.set_rollback(False)
        except Exception:
            pass
        pass
    if not recipients:
        return
    subject = f"Alerta stock minimo - {len(items)} repuesto(s)"
    lines = ["Se alcanzo el stock minimo en:", ""]
    for it in items:
        lines.append(f"- {it.get('codigo') or '-'} | {it.get('nombre') or '-'}")
        lines.append(f"  Stock: {it.get('stock_on_hand')} | Min: {it.get('stock_min')}")
        if it.get("ubicacion_deposito"):
            lines.append(f"  Ubicación: {it.get('ubicacion_deposito')}")
        lines.append("")
    body = _email_append_footer_text("\n".join(lines).rstrip() + "\n")
    send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", None), recipients, fail_silently=True)


def _has_catalogo_repuestos_estado_column() -> bool:
    try:
        if connection.vendor == "sqlite":
            with connection.cursor() as cur:
                cur.execute("PRAGMA table_info('catalogo_repuestos')")
                cols = {str(row[1]).lower() for row in cur.fetchall()}
            return "estado" in cols
        if connection.vendor == "postgresql":
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name='catalogo_repuestos'
                   AND column_name='estado'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                one=True,
            )
        else:
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_schema = DATABASE()
                   AND table_name='catalogo_repuestos'
                   AND column_name='estado'
                 LIMIT 1
                """,
                one=True,
            )
        return bool(row)
    except Exception:
        return False


def _ingresos_has_permite_reparacion_col() -> bool:
    try:
        if connection.vendor == "postgresql":
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name='ingresos'
                   AND column_name='permite_reparacion'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                one=True,
            )
            return bool(row)
        row = q(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name='ingresos'
               AND column_name='permite_reparacion'
             LIMIT 1
            """,
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _normalize_assoc_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.split())


def _build_ingreso_equipo_assoc_label(ingreso_id: int) -> str | None:
    try:
        row = q(
            """
            SELECT
              COALESCE(b.nombre,'') AS marca,
              COALESCE(m.nombre,'') AS modelo,
              COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS variante
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            WHERE t.id=%s
            """,
            [ingreso_id],
            one=True,
        ) or {}
    except Exception:
        return None

    marca = (row.get("marca") or "").strip()
    modelo = (row.get("modelo") or "").strip()
    variante = (row.get("variante") or "").strip()
    if not marca or not modelo:
        return None
    if variante:
        return f"{marca} | {modelo} | {variante}"
    return f"{marca} | {modelo}"


def _get_quote_repuesto_ids_for_assoc(quote_id: int) -> list[int]:
    rows = q(
        """
        SELECT DISTINCT
          COALESCE(
            qi.repuesto_id,
            (
              SELECT cr.id
              FROM catalogo_repuestos cr
              WHERE UPPER(cr.codigo)=UPPER(qi.repuesto_codigo)
                AND cr.activo
              ORDER BY cr.id
              LIMIT 1
            )
          ) AS repuesto_id
        FROM quote_items qi
        WHERE qi.quote_id=%s
          AND qi.tipo='repuesto'
        """,
        [quote_id],
    ) or []
    out: list[int] = []
    seen: set[int] = set()
    for row in rows:
        rep_id = row.get("repuesto_id")
        try:
            rep_id_i = int(rep_id)
        except (TypeError, ValueError):
            continue
        if rep_id_i <= 0 or rep_id_i in seen:
            continue
        seen.add(rep_id_i)
        out.append(rep_id_i)
    return out


def _append_equipo_assoc_to_repuesto(repuesto_id: int, equipo_label: str) -> bool:
    if not _has_catalogo_repuestos_estado_column():
        return False
    target = (equipo_label or "").strip()
    if not target:
        return False

    lock_clause = "" if connection.vendor == "sqlite" else "FOR UPDATE"
    row = q(
        f"""
        SELECT id, COALESCE(estado, '') AS estado
        FROM catalogo_repuestos
        WHERE id=%s
        {lock_clause}
        """,
        [repuesto_id],
        one=True,
    )
    if not row:
        return False

    lines = [str(line).strip() for line in str(row.get("estado") or "").splitlines() if str(line).strip()]
    target_key = _normalize_assoc_key(target)
    existing_keys = {_normalize_assoc_key(line) for line in lines}
    if target_key in existing_keys:
        return False

    lines.append(target)
    exec_void(
        "UPDATE catalogo_repuestos SET estado=%s WHERE id=%s",
        ["\n".join(lines), repuesto_id],
    )
    return True


def _sync_quote_repuestos_equipo_assoc(ingreso_id: int, quote_id: int):
    equipo_label = _build_ingreso_equipo_assoc_label(ingreso_id)
    if not equipo_label:
        return
    for repuesto_id in _get_quote_repuesto_ids_for_assoc(quote_id):
        _append_equipo_assoc_to_repuesto(repuesto_id, equipo_label)


def _resolve_repuesto_data(repuesto_id, repuesto_codigo):
    rep_id = None
    rep_codigo = None
    rep_nombre = None
    rep_costo = None
    rep_precio = None
    rep_multiplicador = None

    row = None
    if repuesto_id:
        row = q(
            """
            SELECT id, codigo, nombre, costo_neto, costo_usd, costo_moneda, multiplicador, precio_venta
            FROM catalogo_repuestos
            WHERE id=%s AND activo
            """,
            [repuesto_id],
            one=True,
        )
    if not row and repuesto_codigo:
        row = q(
            """
            SELECT id, codigo, nombre, costo_neto, costo_usd, costo_moneda, multiplicador, precio_venta
            FROM catalogo_repuestos
            WHERE UPPER(codigo)=UPPER(%s) AND activo
            """,
            [repuesto_codigo],
            one=True,
        )

    if row:
        rep_id = row.get("id")
        rep_codigo = row.get("codigo")
        rep_nombre = row.get("nombre")
        rep_multiplicador = row.get("multiplicador")
        cfg = get_repuestos_config()
        rep_costo = calc_costo_ars(
            row.get("costo_usd"),
            cfg.get("dolar_ars"),
            costo_moneda=row.get("costo_moneda"),
            costo_neto=row.get("costo_neto"),
        )
        rep_precio = calc_precio_venta(
            row.get("costo_usd"),
            cfg.get("dolar_ars"),
            cfg.get("multiplicador_general"),
            row.get("multiplicador"),
            costo_moneda=row.get("costo_moneda"),
            costo_neto=row.get("costo_neto"),
            costo_ars=rep_costo,
        )
        if rep_precio is None:
            rep_precio = row.get("precio_venta")
    else:
        rep_codigo = (repuesto_codigo or "").strip().upper() or None

    return rep_id, rep_codigo, rep_nombre, rep_costo, rep_precio, rep_multiplicador


_QUOTE_COST_MISSING = object()


def _parse_quote_item_cost_input(data, user):
    if "costo_u_neto" not in data:
        return _QUOTE_COST_MISSING
    if not _can_view_costs(user):
        raise PermissionDenied("No autorizado para editar costo")

    raw = data.get("costo_u_neto")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    try:
        costo = money(raw)
    except (TypeError, ValueError):
        raise ValidationError("costo_u_neto debe ser numérico")
    if costo < 0:
        raise ValidationError("costo_u_neto no puede ser negativo")
    return costo


def _calc_quote_item_price_from_cost(costo_u_neto, *, repuesto_id=None, repuesto_codigo=None, repuesto_multiplicador=None):
    if costo_u_neto is None:
        return None
    cfg = get_repuestos_config()
    multiplicador = repuesto_multiplicador
    if multiplicador is None and (repuesto_id or repuesto_codigo):
        row = None
        if repuesto_id:
            row = q(
                """
                SELECT multiplicador
                FROM catalogo_repuestos
                WHERE id=%s AND activo
                """,
                [repuesto_id],
                one=True,
            )
        elif repuesto_codigo:
            row = q(
                """
                SELECT multiplicador
                FROM catalogo_repuestos
                WHERE UPPER(codigo)=UPPER(%s) AND activo
                """,
                [repuesto_codigo],
                one=True,
            )
        if row:
            multiplicador = row.get("multiplicador")
    return calc_precio_venta(
        None,
        None,
        cfg.get("multiplicador_general"),
        multiplicador,
        costo_ars=costo_u_neto,
    )


def _quote_order_sql(alias: str = "q") -> str:
    return f"COALESCE({alias}.version_num, 1) DESC, {alias}.id DESC"


def _quote_lock_clause(for_update: bool = False) -> str:
    return " FOR UPDATE" if for_update and connection.vendor != "sqlite" else ""


def _parse_quote_id(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("quote_id inválido")


def _refresh_quote_totals(quote_id: int):
    row = q(
        """
        SELECT COALESCE(ROUND(SUM(qi.qty * qi.precio_u), 2), 0) AS subtotal
        FROM quote_items qi
        WHERE qi.quote_id=%s
        """,
        [quote_id],
        one=True,
    ) or {"subtotal": 0}
    subtotal = money(row.get("subtotal") or 0)
    iva_21 = money(subtotal * money("0.21"))
    total = money(subtotal + iva_21)
    values_by_column = {
        "subtotal": subtotal,
        "iva_21": iva_21,
        "total": total,
    }
    update_columns = _get_quote_total_update_columns()
    assignments = ",\n                   ".join(f"{col}=%s" for col in update_columns)
    params = [values_by_column[col] for col in update_columns] + [quote_id]
    exec_void(
        f"""
        UPDATE quotes
           SET {assignments}
         WHERE id=%s
        """,
        params,
    )
    return subtotal, iva_21, total


def _get_quote_total_update_columns():
    columns = ["subtotal", "iva_21", "total"]
    if connection.vendor != "postgresql":
        return columns

    try:
        rows = q(
            """
            SELECT column_name, COALESCE(is_generated, 'NEVER') AS is_generated
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'quotes'
              AND column_name IN ('subtotal', 'iva_21', 'total')
            """,
        )
    except Exception:
        return columns

    if not rows:
        return columns

    generated_by_column = {
        str(row.get("column_name") or ""): str(row.get("is_generated") or "NEVER").upper()
        for row in rows
    }
    return [
        column
        for column in columns
        if column == "subtotal" or generated_by_column.get(column, "NEVER") != "ALWAYS"
    ]


def _get_current_quote_row(ingreso_id: int, *, for_update: bool = False):
    return q(
        f"""
        SELECT
          q.id,
          COALESCE(q.version_num, 1) AS version_num,
          q.origen_quote_id,
          q.estado,
          q.moneda,
          q.fecha_emitido,
          q.fecha_aprobado,
          q.fecha_rechazado,
          q.rechazo_comentario,
          q.pdf_url
        FROM quotes q
        WHERE q.ingreso_id=%s
        ORDER BY {_quote_order_sql('q')}
        LIMIT 1{_quote_lock_clause(for_update)}
        """,
        [ingreso_id],
        one=True,
    )


def _get_quote_row(ingreso_id: int, quote_id: int, *, for_update: bool = False):
    return q(
        f"""
        SELECT
          q.id,
          COALESCE(q.version_num, 1) AS version_num,
          q.origen_quote_id,
          q.estado,
          q.moneda,
          q.fecha_emitido,
          q.fecha_aprobado,
          q.fecha_rechazado,
          q.rechazo_comentario,
          q.pdf_url
        FROM quotes q
        WHERE q.ingreso_id=%s
          AND q.id=%s
        LIMIT 1{_quote_lock_clause(for_update)}
        """,
        [ingreso_id, quote_id],
        one=True,
    )


def _ensure_current_quote(ingreso_id: int):
    row = _get_current_quote_row(ingreso_id)
    if row:
        return row["id"]

    try:
        with transaction.atomic():
            row = _get_current_quote_row(ingreso_id, for_update=True)
            if row:
                return row["id"]
            return exec_returning(
                "INSERT INTO quotes(ingreso_id, version_num) VALUES (%s, %s) RETURNING id",
                [ingreso_id, 1],
            )
    except Exception:
        row = _get_current_quote_row(ingreso_id)
        if row:
            return row["id"]
        if connection.vendor == "postgresql":
            try:
                with transaction.atomic():
                    with connection.cursor() as cur:
                        cur.execute(
                            "SELECT setval(pg_get_serial_sequence('quotes','id'), COALESCE((SELECT MAX(id) FROM quotes), 1))"
                        )
                    row = _get_current_quote_row(ingreso_id, for_update=True)
                    if row:
                        return row["id"]
                    return exec_returning(
                        "INSERT INTO quotes(ingreso_id, version_num) VALUES (%s, %s) RETURNING id",
                        [ingreso_id, 1],
                    )
            except Exception:
                pass
        row = _get_current_quote_row(ingreso_id)
        if row:
            return row["id"]
        raise


def _ensure_current_quote_editable(ingreso_id: int):
    _ensure_current_quote(ingreso_id)
    current = _get_current_quote_row(ingreso_id)
    if not current:
        raise ValidationError("Ingreso no encontrado o sin presupuesto")
    if (current.get("estado") or "").strip() in {"aprobado", "rechazado", "no_aplica"}:
        raise ValidationError("La versión vigente del presupuesto no es editable.")
    return current


def _build_quote_versions_summary(ingreso_id: int, current_quote_id: int):
    return q(
        """
        SELECT
          q.id AS quote_id,
          COALESCE(q.version_num, 1) AS version_num,
          q.estado,
          q.fecha_emitido,
          q.fecha_aprobado,
          q.fecha_rechazado,
          q.rechazo_comentario,
          COALESCE(q.pdf_url, '') AS pdf_url,
          COALESCE(ROUND(SUM(qi.qty * qi.precio_u), 2), 0) AS subtotal,
          ROUND(COALESCE(SUM(qi.qty * qi.precio_u), 0) * 0.21, 2) AS iva_21,
          ROUND(COALESCE(SUM(qi.qty * qi.precio_u), 0) * 1.21, 2) AS total
        FROM quotes q
        LEFT JOIN quote_items qi ON qi.quote_id = q.id
        WHERE q.ingreso_id=%s
        GROUP BY q.id, q.version_num, q.estado, q.fecha_emitido, q.fecha_aprobado, q.fecha_rechazado, q.rechazo_comentario, q.pdf_url
        ORDER BY COALESCE(q.version_num, 1) DESC, q.id DESC
        """,
        [ingreso_id],
    ) or []


def _load_quote_payload(ingreso_id: int, quote_id: int | None = None):
    if quote_id is None:
        _ensure_current_quote(ingreso_id)
    current = _get_current_quote_row(ingreso_id)
    if not current:
        raise ValidationError("Ingreso no encontrado o sin presupuesto")

    selected = current if quote_id is None else _get_quote_row(ingreso_id, quote_id)
    if not selected:
        raise ValidationError("Versión de presupuesto no encontrada")

    head = q(
        """
        SELECT
          q.id AS quote_id,
          COALESCE(q.version_num, 1) AS version_num,
          q.origen_quote_id,
          q.estado,
          q.moneda,
          q.subtotal,
          q.iva_21,
          q.total,
          q.fecha_emitido,
          q.fecha_aprobado,
          q.fecha_rechazado,
          q.rechazo_comentario,
          COALESCE(q.pdf_url, '') AS pdf_url,
          COALESCE(NULLIF(q.autorizado_por, ''), %s) AS autorizado_por,
          COALESCE(NULLIF(q.forma_pago, ''), %s) AS forma_pago,
          COALESCE(NULLIF(q.plazo_entrega_txt, ''), %s) AS plazo_entrega_txt,
          COALESCE(NULLIF(q.garantia_txt, ''), %s) AS garantia_txt,
          COALESCE(NULLIF(q.mant_oferta_txt, ''), %s) AS mant_oferta_txt
        FROM quotes q
        WHERE q.id=%s
        """,
        [
            DEFAULT_AUTORIZADO_POR,
            DEFAULT_FORMA_PAGO,
            DEFAULT_PLAZO_ENTREGA_TXT,
            DEFAULT_GARANTIA_TXT,
            DEFAULT_MANT_OFERTA_TXT,
            selected["id"],
        ],
        one=True,
    )

    items = q(
        """
        SELECT
          qi.id, qi.tipo, qi.repuesto_id, qi.repuesto_codigo, qi.descripcion, qi.qty, qi.precio_u,
          ROUND(qi.qty * qi.precio_u, 2) AS subtotal,
          qi.costo_u_neto,
          ROUND(qi.qty * COALESCE(qi.costo_u_neto,0), 2) AS costo_total_neto
        FROM quote_items qi
        WHERE qi.quote_id=%s
        ORDER BY qi.id ASC
        """,
        [selected["id"]],
    ) or []

    tot_rep = (
        q(
            """
            SELECT COALESCE(SUM(qi.qty*qi.precio_u),0) AS x
            FROM quote_items qi
            WHERE qi.quote_id=%s
              AND qi.tipo='repuesto'
            """,
            [selected["id"]],
            one=True,
        )
        or {"x": 0}
    )["x"]

    mano_obra = (
        q(
            """
            SELECT COALESCE(SUM(qi.qty*qi.precio_u),0) AS x
            FROM quote_items qi
            WHERE qi.quote_id=%s
              AND qi.tipo='mano_obra'
            """,
            [selected["id"]],
            one=True,
        )
        or {"x": 0}
    )["x"]

    subtotal_calc = money(sum((it.get("subtotal") or money(0)) for it in items))
    iva21_calc = money(subtotal_calc * money("0.21"))
    total_calc = money(subtotal_calc + iva21_calc)
    versions = _build_quote_versions_summary(ingreso_id, current["id"])
    for version in versions:
        version["is_current"] = int(version.get("quote_id") or 0) == int(current["id"])
        version["estado"] = _quote_estado_for_response(version.get("estado"))

    is_current = int(selected["id"]) == int(current["id"])
    selected_estado = _quote_estado_str(selected.get("estado"))
    head_estado = _quote_estado_str(head.get("estado"))
    is_editable = is_current and _quote_estado_for_response(selected_estado) in {"pendiente", "presupuestado"}

    return {
        "ingreso_id": ingreso_id,
        "quote_id": head["quote_id"],
        "current_quote_id": current["id"],
        "version_num": head["version_num"],
        "current_version_num": current["version_num"],
        "origen_quote_id": head.get("origen_quote_id"),
        "estado": _quote_estado_for_response(head_estado),
        "moneda": head["moneda"],
        "autorizado_por": head["autorizado_por"],
        "forma_pago": head["forma_pago"],
        "plazo_entrega_txt": head["plazo_entrega_txt"],
        "garantia_txt": head["garantia_txt"],
        "mant_oferta_txt": head["mant_oferta_txt"],
        "fecha_emitido": head.get("fecha_emitido"),
        "fecha_aprobado": head.get("fecha_aprobado"),
        "fecha_rechazado": head.get("fecha_rechazado"),
        "rechazo_comentario": head.get("rechazo_comentario"),
        "is_current": is_current,
        "is_editable": is_editable,
        "can_reject": is_current and _is_emitted_quote_state(head_estado),
        "can_create_new_version": is_current and (head.get("estado") or "").strip() == "rechazado",
        "items": items,
        "versions": versions,
        "pdf_url": head.get("pdf_url") or "",
        "tot_repuestos": tot_rep,
        "mano_obra": mano_obra,
        "subtotal": subtotal_calc,
        "iva_21": iva21_calc,
        "total": total_calc,
    }


def _clone_current_quote_version_from_rejected(ingreso_id: int) -> int:
    with transaction.atomic():
        current = _get_current_quote_row(ingreso_id, for_update=True)
        if not current:
            raise ValidationError("Ingreso no encontrado o sin presupuesto")
        if (current.get("estado") or "").strip() != "rechazado":
            raise ValidationError("Solo se puede crear una nueva versión cuando la vigente está rechazada.")

        new_id = exec_returning(
            """
            INSERT INTO quotes(
              ingreso_id,
              version_num,
              origen_quote_id,
              estado,
              moneda,
              subtotal,
              autorizado_por,
              forma_pago,
              plazo_entrega_txt,
              garantia_txt,
              mant_oferta_txt,
              fecha_emitido,
              fecha_aprobado,
              fecha_rechazado,
              rechazo_comentario,
              pdf_url
            )
            SELECT
              ingreso_id,
              COALESCE(version_num, 1) + 1,
              id,
              'pendiente',
              COALESCE(moneda, 'ARS'),
              0,
              autorizado_por,
              forma_pago,
              plazo_entrega_txt,
              garantia_txt,
              mant_oferta_txt,
              NULL,
              NULL,
              NULL,
              NULL,
              NULL
            FROM quotes
            WHERE id=%s
            RETURNING id
            """,
            [current["id"]],
        )
        exec_void(
            """
            INSERT INTO quote_items(
              quote_id,
              tipo,
              descripcion,
              qty,
              precio_u,
              repuesto_id,
              repuesto_codigo,
              costo_u_neto
            )
            SELECT
              %s,
              tipo,
              descripcion,
              qty,
              precio_u,
              repuesto_id,
              repuesto_codigo,
              costo_u_neto
            FROM quote_items
            WHERE quote_id=%s
            ORDER BY id
            """,
            [new_id, current["id"]],
        )
        _refresh_quote_totals(new_id)
        exec_void("UPDATE ingresos SET presupuesto_estado='pendiente' WHERE id=%s", [ingreso_id])
        return new_id


def _quote_response(request, ingreso_id: int, *, quote_id: int | None = None, status_code: int = 200):
    data = _load_quote_payload(ingreso_id, quote_id=quote_id)
    data = _mask_costs(data, _can_view_costs(request.user))
    return Response(QuoteDetailSerializer(data).data, status=status_code)


def _clean_optional_note(value, *, max_len: int = 1000):
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > max_len:
        raise ValidationError(f"La nota no puede superar los {max_len} caracteres.")
    return text


class QuoteDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico", "recepcion"])
        quote_id = _parse_quote_id(request.query_params.get("quote_id"))
        _ensure_current_quote(ingreso_id)
        return _quote_response(request, ingreso_id, quote_id=quote_id)


class QuoteItemsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        d = request.data or {}
        tipo = (d.get("tipo") or "").strip()
        if tipo not in ("repuesto", "mano_obra", "servicio"):
            raise ValidationError("tipo inválido")
        desc = (d.get("descripcion") or "").strip()

        try:
            qty = money(d.get("qty"))
        except (TypeError, ValueError):
            raise ValidationError("qty y precio_u deben ser numéricos")
        #if qty < 0 or precio < 0:
            #raise ValidationError("qty y precio_u no pueden ser negativos")
        repuesto_id = None
        if d.get("repuesto_id") not in (None, ""):
            try:
                repuesto_id = int(d.get("repuesto_id"))
            except (TypeError, ValueError):
                repuesto_id = None
        repuesto_codigo = (d.get("repuesto_codigo") or "").strip()
        custom_cost = _parse_quote_item_cost_input(d, request.user)

        rep_id = rep_codigo = rep_nombre = rep_costo = rep_precio = rep_multiplicador = None
        if tipo == "repuesto":
            rep_id, rep_codigo, rep_nombre, rep_costo, rep_precio, rep_multiplicador = _resolve_repuesto_data(
                repuesto_id,
                repuesto_codigo,
            )
            if rep_nombre:
                desc = rep_nombre
        else:
            repuesto_id = None
            repuesto_codigo = ""

        costo_to_save = rep_costo
        precio_auto = rep_precio
        if custom_cost is not _QUOTE_COST_MISSING:
            if tipo == "repuesto" and custom_cost is None:
                costo_to_save = rep_costo
                precio_auto = rep_precio
            else:
                costo_to_save = custom_cost
                if tipo == "repuesto" and custom_cost is not None:
                    precio_auto = _calc_quote_item_price_from_cost(
                        custom_cost,
                        repuesto_id=rep_id,
                        repuesto_codigo=rep_codigo or repuesto_codigo,
                        repuesto_multiplicador=rep_multiplicador,
                    )
                else:
                    precio_auto = None

        precio_raw = d.get("precio_u")
        try:
            if precio_raw is None or (isinstance(precio_raw, str) and precio_raw.strip() == ""):
                precio = money(precio_auto) if precio_auto is not None else money(precio_raw)
            else:
                precio = money(precio_raw)
        except (TypeError, ValueError):
            raise ValidationError("qty y precio_u deben ser numéricos")

        if not desc:
            raise ValidationError("descripcion requerida")

        current = _ensure_current_quote_editable(ingreso_id)
        qid = current["id"]
        _set_audit_user(request)
        exec_void(
            """
            INSERT INTO quote_items(quote_id, tipo, descripcion, qty, precio_u, repuesto_id, repuesto_codigo, costo_u_neto)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [qid, tipo, desc, qty, precio, rep_id, rep_codigo, costo_to_save],
        )
        _refresh_quote_totals(qid)
        return _quote_response(request, ingreso_id, status_code=201)


class QuoteItemDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, ingreso_id: int, item_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        current = _ensure_current_quote_editable(ingreso_id)
        current_quote_id = current["id"]
        d = request.data or {}
        sets, params = [], []
        desc_from_rep = None
        auto_precio = _QUOTE_COST_MISSING
        current_item = q(
            """
            SELECT qi.tipo, qi.repuesto_id, qi.repuesto_codigo
            FROM quote_items qi
            WHERE qi.quote_id=%s AND qi.id=%s
            """,
            [current_quote_id, item_id],
            one=True,
        )
        if not current_item:
            raise ValidationError("\u00cdtem no encontrado")

        final_tipo = (current_item.get("tipo") or "").strip()
        target_repuesto_id = current_item.get("repuesto_id")
        target_repuesto_codigo = current_item.get("repuesto_codigo")
        custom_cost = _parse_quote_item_cost_input(d, request.user)
        if "tipo" in d:
            t = (d.get("tipo") or "").strip()
            if t not in ("repuesto", "mano_obra", "servicio"):
                raise ValidationError("tipo inválido")
            final_tipo = t
            sets.append("tipo=%s"); params.append(t)
            if t != "repuesto":
                target_repuesto_id = None
                target_repuesto_codigo = None
                sets.append("repuesto_id=%s"); params.append(None)
                sets.append("repuesto_codigo=%s"); params.append(None)
                sets.append("costo_u_neto=%s"); params.append(None)
        if final_tipo == "repuesto" and ("repuesto_id" in d or "repuesto_codigo" in d):
            repuesto_id = None
            if d.get("repuesto_id") not in (None, ""):
                try:
                    repuesto_id = int(d.get("repuesto_id"))
                except (TypeError, ValueError):
                    repuesto_id = None
            repuesto_codigo = (d.get("repuesto_codigo") or "").strip() if "repuesto_codigo" in d else None

            rep_id, rep_code, rep_nombre, rep_costo, rep_precio, _ = _resolve_repuesto_data(repuesto_id, repuesto_codigo)
            if rep_nombre:
                target_repuesto_id = rep_id
                target_repuesto_codigo = rep_code
                sets.append("repuesto_id=%s"); params.append(rep_id)
                sets.append("repuesto_codigo=%s"); params.append(rep_code)
                if custom_cost is _QUOTE_COST_MISSING:
                    sets.append("costo_u_neto=%s"); params.append(rep_costo)
                    auto_precio = rep_precio
                desc_from_rep = rep_nombre
            else:
                if repuesto_id is None and (repuesto_codigo is None or repuesto_codigo == ""):
                    target_repuesto_id = None
                    target_repuesto_codigo = None
                    sets.append("repuesto_id=%s"); params.append(None)
                    sets.append("repuesto_codigo=%s"); params.append(None)
                    if custom_cost is _QUOTE_COST_MISSING:
                        sets.append("costo_u_neto=%s"); params.append(None)
                else:
                    target_repuesto_id = None
                    sets.append("repuesto_id=%s"); params.append(None)
                    if repuesto_codigo is not None:
                        target_repuesto_codigo = repuesto_codigo.strip().upper() or None
                        sets.append("repuesto_codigo=%s"); params.append(target_repuesto_codigo)
                    if custom_cost is _QUOTE_COST_MISSING:
                        sets.append("costo_u_neto=%s"); params.append(None)
        if custom_cost is not _QUOTE_COST_MISSING:
            resolved_rep_id, resolved_rep_code, _, resolved_rep_costo, resolved_rep_precio, resolved_rep_mult = _resolve_repuesto_data(
                target_repuesto_id,
                target_repuesto_codigo,
            )
            if final_tipo == "repuesto" and custom_cost is None:
                sets.append("costo_u_neto=%s"); params.append(resolved_rep_costo)
                auto_precio = resolved_rep_precio
            else:
                sets.append("costo_u_neto=%s"); params.append(custom_cost)
                if final_tipo == "repuesto" and custom_cost is not None:
                    auto_precio = _calc_quote_item_price_from_cost(
                        custom_cost,
                        repuesto_id=resolved_rep_id or target_repuesto_id,
                        repuesto_codigo=resolved_rep_code or target_repuesto_codigo,
                        repuesto_multiplicador=resolved_rep_mult,
                    )
        if "descripcion" in d and desc_from_rep is None:
            sets.append("descripcion=%s"); params.append((d.get("descripcion") or "").strip())
        if desc_from_rep is not None:
            sets.append("descripcion=%s"); params.append(desc_from_rep)
        if "qty" in d:
            try:
                qv = float(d.get("qty"))
            except (TypeError, ValueError):
                raise ValidationError("qty debe ser numérico")
            #if qv < 0:
                #raise ValidationError("qty no puede ser negativo")
            sets.append("qty=%s"); params.append(qv)
        if "precio_u" in d:
            try:
                pv = float(d.get("precio_u"))
            except (TypeError, ValueError):
                raise ValidationError("precio_u debe ser numérico")
            if pv < 0:
                raise ValidationError("precio_u no puede ser negativo")
            sets.append("precio_u=%s"); params.append(pv)
        elif auto_precio is not _QUOTE_COST_MISSING and auto_precio is not None:
            sets.append("precio_u=%s"); params.append(auto_precio)

        if sets:
            _set_audit_user(request)
            params += [current_quote_id, item_id]
            exec_void(
                f"""
                UPDATE quote_items
                   SET {', '.join(sets)}
                 WHERE quote_id=%s AND id=%s
                """,
                params,
            )
            _refresh_quote_totals(current_quote_id)
        return _quote_response(request, ingreso_id)

    def delete(self, request, ingreso_id: int, item_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        current = _ensure_current_quote_editable(ingreso_id)
        _set_audit_user(request)
        exec_void(
            """
            DELETE FROM quote_items
             WHERE quote_id=%s AND id=%s
            """,
            [current["id"], item_id],
        )
        _refresh_quote_totals(current["id"])
        return _quote_response(request, ingreso_id)


class QuoteResumenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        mo = request.data.get("mano_obra")
        if mo is None:
            raise ValidationError("mano_obra requerido")
        try:
            mo = float(mo)
        except (TypeError, ValueError):
            raise ValidationError("mano_obra debe ser numérico")
        if mo < 0:
            raise ValidationError("mano_obra no puede ser negativo")

        current = _ensure_current_quote_editable(ingreso_id)
        qid = current["id"]
        _set_audit_user(request)
        row = q(
            "SELECT id FROM quote_items WHERE quote_id=%s AND tipo='mano_obra' ORDER BY id LIMIT 1",
            [qid],
            one=True,
        )
        if row:
            exec_void(
                "UPDATE quote_items SET qty=1, precio_u=%s, descripcion='Mano de obra' WHERE id=%s",
                [mo, row["id"]],
            )
        else:
            exec_void(
                "INSERT INTO quote_items(quote_id, tipo, descripcion, qty, precio_u) VALUES (%s,'mano_obra','Mano de obra',1,%s)",
                [qid, mo],
            )
        _refresh_quote_totals(qid)
        return _quote_response(request, ingreso_id)


class RechazarPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        if not has_rejected_budget_charge_schema():
            raise ValidationError("Falta aplicar el esquema de cobro para presupuesto rechazado.")
        rechazo_comentario = _clean_optional_note((request.data or {}).get("rechazo_comentario"))
        cobro_neto = _parse_rejected_budget_charge_or_raise(
            (request.data or {}).get("presupuesto_rechazado_cobro_neto"),
            required=True,
        )
        with transaction.atomic():
            current = _get_current_quote_row(ingreso_id, for_update=True)
            if not current:
                raise ValidationError("Ingreso no encontrado o sin presupuesto")
            if not _is_emitted_quote_state(current.get("estado")):
                raise ValidationError("Solo se puede rechazar un presupuesto emitido.")
            _set_audit_user(request)
            exec_void(
                """
                UPDATE quotes
                   SET estado='rechazado',
                       fecha_rechazado=now(),
                       rechazo_comentario=%s
                 WHERE id=%s
                """,
                [rechazo_comentario, current["id"]],
            )
            exec_void(
                """
                UPDATE ingresos
                   SET presupuesto_estado='rechazado',
                       presupuesto_rechazado_cobro_neto=%s,
                       presupuesto_rechazado_quote_id=%s
                 WHERE id=%s
                """,
                [cobro_neto, current["id"], ingreso_id],
            )
        return _quote_response(request, ingreso_id)


class QuoteVersionesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        _set_audit_user(request)
        _clone_current_quote_version_from_rejected(ingreso_id)
        return _quote_response(request, ingreso_id)


class EmitirPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        autorizado_por = _clean_text_or_default(request.data.get("autorizado_por"), DEFAULT_AUTORIZADO_POR)
        forma_pago = _clean_text_or_default(request.data.get("forma_pago"), "A definir")
        plazo_entrega_txt = _clean_text_or_default(request.data.get("plazo_entrega_txt"), DEFAULT_PLAZO_ENTREGA_TXT)
        garantia_txt = _clean_text_or_default(request.data.get("garantia_txt"), DEFAULT_GARANTIA_TXT)
        mant_oferta_txt = _clean_text_or_default(request.data.get("mant_oferta_txt"), DEFAULT_MANT_OFERTA_TXT)
        current = _ensure_current_quote_editable(ingreso_id)
        qid = current["id"]
        estado_actual = _quote_estado_str(current.get("estado"))
        if estado_actual not in {"pendiente", *EMITTED_QUOTE_STATES}:
            raise ValidationError("Solo se puede emitir la versión vigente cuando está pendiente.")
        _refresh_quote_totals(qid)
        with transaction.atomic():
            _set_audit_user(request)
            exec_void(
                """
                UPDATE quotes
                   SET estado='presupuestado',
                       autorizado_por=%s,
                       forma_pago=%s,
                       plazo_entrega_txt=%s,
                       garantia_txt=%s,
                       mant_oferta_txt=%s,
                       fecha_emitido=now()
                 WHERE id=%s
                """,
                [autorizado_por, forma_pago, plazo_entrega_txt, garantia_txt, mant_oferta_txt, qid],
            )
            exec_void("UPDATE ingresos SET presupuesto_estado='presupuestado' WHERE id=%s", [ingreso_id])
            _sync_quote_repuestos_equipo_assoc(ingreso_id, qid)

        fname, pdf = render_quote_pdf(ingreso_id, quote_id=qid)
        try:
            save_dir = getattr(settings, "QUOTES_SAVE_DIR", None)
            if save_dir and pdf:
                os.makedirs(save_dir, exist_ok=True)
                dest = os.path.join(save_dir, fname)
                with open(dest, "wb") as f:
                    f.write(pdf)
        except Exception:
            pass
        pdf_url = f"/api/quotes/{ingreso_id}/pdf/?quote_id={qid}"
        exec_void("UPDATE quotes SET pdf_url=%s WHERE id=%s", [pdf_url, qid])
        return _quote_response(request, ingreso_id)


class QuotePdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico", "recepcion"])
        quote_id = _parse_quote_id(request.query_params.get("quote_id"))
        _ensure_current_quote(ingreso_id)
        if quote_id is not None and not _get_quote_row(ingreso_id, quote_id):
            raise ValidationError("La versión solicitada no pertenece a este ingreso.")
        fname, pdf = render_quote_pdf(ingreso_id, quote_id=quote_id)
        if not pdf:
            raise ValidationError("Ingreso no encontrado o sin presupuesto")
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp


class AprobarPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        qid = None
        was_approved = False
        alert_items = []
        with transaction.atomic():
            current = _get_current_quote_row(ingreso_id, for_update=True)
            if not current:
                raise ValidationError("Ingreso no encontrado o sin presupuesto")
            qid = current["id"]
            current_estado = _quote_estado_str(current.get("estado"))
            was_approved = current_estado == "aprobado"
            if current_estado not in {*EMITTED_QUOTE_STATES, "aprobado"}:
                raise ValidationError("Solo se puede aprobar la versión vigente cuando está emitida.")
            _set_audit_user(request)
            if not was_approved:
                exec_void(
                    """
                    UPDATE quotes
                       SET estado='aprobado',
                           fecha_aprobado=now()
                     WHERE id=%s
                    """,
                    [qid],
                )
            has_permite_reparacion = _ingresos_has_permite_reparacion_col()
            permite_reparacion_sql = (
                "COALESCE(permite_reparacion, TRUE) AS permite_reparacion"
                if has_permite_reparacion
                else "TRUE AS permite_reparacion"
            )
            ingreso_row = q(
                f"""
                SELECT estado, motivo, {permite_reparacion_sql}
                  FROM ingresos
                 WHERE id=%s
                 FOR UPDATE
                """,
                [ingreso_id],
                one=True,
            ) or {}
            bloqueada_por_cotizacion = _motivo_is_cotizacion_equipo(ingreso_row.get("motivo")) and not bool(
                ingreso_row.get("permite_reparacion")
            )
            if bloqueada_por_cotizacion:
                exec_void(
                    """
                    UPDATE ingresos
                       SET presupuesto_estado='aprobado'
                     WHERE id=%s
                    """,
                    [ingreso_id],
                )
            else:
                exec_void(
                    """
                    UPDATE ingresos
                       SET presupuesto_estado='aprobado',
                           estado = CASE
                                      WHEN estado IN ('ingresado','diagnosticado','presupuestado')
                                      THEN 'reparar'
                                      ELSE estado
                                    END
                     WHERE id=%s
                    """,
                    [ingreso_id],
                )

            if not was_approved:
                items = q(
                    """
                    SELECT repuesto_id, SUM(qty) AS qty
                    FROM quote_items
                    WHERE quote_id=%s
                      AND tipo='repuesto'
                      AND repuesto_id IS NOT NULL
                    GROUP BY repuesto_id
                    """,
                    [qid],
                ) or []
                for it in items:
                    rep_id = it.get("repuesto_id")
                    if not rep_id:
                        continue
                    qty = money(it.get("qty") or 0)
                    if qty == 0:
                        continue
                    rep_row = q(
                        """
                        SELECT id, codigo, nombre, stock_on_hand, stock_min, ubicacion_deposito
                        FROM catalogo_repuestos
                        WHERE id=%s
                        FOR UPDATE
                        """,
                        [rep_id],
                        one=True,
                    )
                    if not rep_row:
                        continue
                    stock_prev = money(rep_row.get("stock_on_hand") or 0)
                    stock_min = money(rep_row.get("stock_min") or 0)
                    delta = money(-qty)
                    stock_new = money(stock_prev + delta)
                    exec_void(
                        "UPDATE catalogo_repuestos SET stock_on_hand=%s, updated_at=NOW() WHERE id=%s",
                        [stock_new, rep_id],
                    )
                    exec_void(
                        """
                        INSERT INTO repuestos_movimientos
                          (repuesto_id, tipo, qty, stock_prev, stock_new, ref_tipo, ref_id, created_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        [rep_id, "egreso_aprobado", delta, stock_prev, stock_new, "quote", qid, request.user.id],
                    )
                    if stock_prev > stock_min and stock_new <= stock_min:
                        alert_items.append({
                            "codigo": rep_row.get("codigo"),
                            "nombre": rep_row.get("nombre"),
                            "stock_on_hand": stock_new,
                            "stock_min": stock_min,
                            "ubicacion_deposito": rep_row.get("ubicacion_deposito"),
                        })

        if was_approved:
            return _quote_response(request, ingreso_id)

        try:
            row = q(
                """
                SELECT
                  u.id AS tecnico_id,
                  u.email, COALESCE(u.nombre,'') AS tecnico_nombre,
                  c.razon_social AS cliente,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(d.numero_serie,'') AS numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno
                FROM ingresos t
                LEFT JOIN users   u ON u.id = t.asignado_a
                JOIN devices      d ON d.id = t.device_id
                JOIN customers    c ON c.id = d.customer_id
                LEFT JOIN marcas  b ON b.id = d.marca_id
                LEFT JOIN models  m ON m.id = d.model_id
                WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            ) or {}
            if row.get("tecnico_id"):
                try:
                    notify_presupuesto_aprobado(ingreso_id, row.get("tecnico_id"))
                except Exception:
                    _clear_transaction_rollback()
                    pass
            to_email = (row.get("email") or "").strip()
            if to_email:
                os_label = f"OS {str(ingreso_id).zfill(6)}"
                link = _frontend_url(request, f"/ingresos/{ingreso_id}")
                subject = f"{os_label} - Presupuesto aprobado"
                body_lines = [
                    f"Hola {row.get('tecnico_nombre') or ''},",
                    "",
                    f"El presupuesto de la {os_label} fue Aprobado.",
                    "",
                    "Detalle del equipo:",
                    f"- Cliente: {row.get('cliente') or '-'}",
                    f"- Marca/Modelo: {row.get('marca') or '-'} / {row.get('modelo') or '-'}",
                    f"- Tipo: {row.get('tipo_equipo') or '-'}",
                    f"- N° de serie: {row.get('numero_interno') or row.get('numero_serie') or '-'}",
                    "",
                    f"Abrir hoja de servicio: {link}",
                    "",
                    "Aviso automático - no responder a este correo.",
                ]
                body = "\n".join(body_lines)
                from django.core.mail import send_mail
                send_mail(subject, body, getattr(settings, 'DEFAULT_FROM_EMAIL', None), [to_email], fail_silently=True)
        except Exception:
            _clear_transaction_rollback()
            pass

        if alert_items:
            try:
                _send_stock_min_alerts(alert_items)
            except Exception:
                _clear_transaction_rollback()
                pass

        try:
            fname, pdf = render_quote_pdf(ingreso_id, quote_id=qid)
            save_dir = getattr(settings, "QUOTES_SAVE_DIR", None)
            if save_dir and pdf:
                import os
                os.makedirs(save_dir, exist_ok=True)
                dest = os.path.join(save_dir, fname)
                with open(dest, "wb") as f:
                    f.write(pdf)
        except Exception:
            _clear_transaction_rollback()
            pass

        _clear_transaction_rollback()
        return _quote_response(request, ingreso_id)


class AnularPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        with transaction.atomic():
            row = q(
                """
                SELECT estado, presupuesto_estado
                FROM ingresos
                WHERE id=%s
                FOR UPDATE
                """,
                [ingreso_id],
                one=True,
            )
            if not row:
                raise ValidationError("Ingreso no encontrado")

            estado_ingreso = (row.get("estado") or "").strip()
            current = _get_current_quote_row(ingreso_id, for_update=True)
            if not current:
                raise ValidationError("Ingreso no encontrado o sin presupuesto")
            qid = current["id"]
            current_estado = _quote_estado_str(current.get("estado"))
            if current_estado not in {*EMITTED_QUOTE_STATES, "aprobado"}:
                raise ValidationError("Solo se puede anular cuando la versión vigente está emitida o aprobada.")
            if current_estado == "aprobado" and estado_ingreso in (
                "entregado",
                "alquilado",
                "baja",
                "vendido_pendiente_entrega",
                "vendido_entregado",
            ):
                raise ValidationError("No se puede anular un presupuesto aprobado en un estado cerrado o vendido.")

            _set_audit_user(request)

            if current_estado == "aprobado":
                items = q(
                    """
                    SELECT repuesto_id, SUM(qty) AS qty
                    FROM quote_items
                    WHERE quote_id=%s
                      AND tipo='repuesto'
                      AND repuesto_id IS NOT NULL
                    GROUP BY repuesto_id
                    """,
                    [qid],
                ) or []
                for it in items:
                    rep_id = it.get("repuesto_id")
                    if not rep_id:
                        continue
                    qty = money(it.get("qty") or 0)
                    if qty == 0:
                        continue

                    rep_row = q(
                        """
                        SELECT id, stock_on_hand
                        FROM catalogo_repuestos
                        WHERE id=%s
                        FOR UPDATE
                        """,
                        [rep_id],
                        one=True,
                    )
                    if not rep_row:
                        continue

                    stock_prev = money(rep_row.get("stock_on_hand") or 0)
                    delta = money(qty)
                    stock_new = money(stock_prev + delta)

                    exec_void(
                        "UPDATE catalogo_repuestos SET stock_on_hand=%s, updated_at=NOW() WHERE id=%s",
                        [stock_new, rep_id],
                    )
                    exec_void(
                        """
                        INSERT INTO repuestos_movimientos
                          (repuesto_id, tipo, qty, stock_prev, stock_new, ref_tipo, ref_id, created_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        [
                            rep_id,
                            "ingreso_anulacion_aprobado",
                            delta,
                            stock_prev,
                            stock_new,
                            "quote",
                            qid,
                            getattr(request.user, "id", None),
                        ],
                    )

            exec_void(
                """
                UPDATE quotes
                   SET estado='pendiente',
                       fecha_emitido=NULL,
                       fecha_aprobado=NULL,
                       fecha_rechazado=NULL,
                       rechazo_comentario=NULL,
                       pdf_url=NULL
                 WHERE id=%s
                """,
                [qid],
            )
            if current_estado == "aprobado":
                exec_void(
                    """
                    UPDATE ingresos
                       SET presupuesto_estado='pendiente',
                           estado=CASE
                                    WHEN estado='reparar' THEN 'diagnosticado'
                                    ELSE estado
                                  END
                     WHERE id=%s
                    """,
                    [ingreso_id],
                )
            else:
                exec_void("UPDATE ingresos SET presupuesto_estado='pendiente' WHERE id=%s", [ingreso_id])

        return _quote_response(request, ingreso_id)

class NoAplicaPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        with transaction.atomic():
            current = _get_current_quote_row(ingreso_id, for_update=True)
            if not current:
                raise ValidationError("Ingreso no encontrado o sin presupuesto")
            if (current.get("estado") or "").strip() != "pendiente":
                raise ValidationError("Solo se puede marcar 'No aplica' sobre una versión vigente pendiente.")
            _set_audit_user(request)
            exec_void(
                """
                UPDATE quotes
                   SET estado='no_aplica',
                       fecha_emitido=NULL,
                       fecha_aprobado=NULL,
                       fecha_rechazado=NULL,
                       rechazo_comentario=NULL,
                       pdf_url=NULL
                 WHERE id=%s
                """,
                [current["id"]],
            )
            exec_void("UPDATE ingresos SET presupuesto_estado='no_aplica' WHERE id=%s", [ingreso_id])
        return _quote_response(request, ingreso_id)


class QuitarNoAplicaPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        with transaction.atomic():
            current = _get_current_quote_row(ingreso_id, for_update=True)
            if not current:
                raise ValidationError("Ingreso no encontrado o sin presupuesto")
            if (current.get("estado") or "").strip() != "no_aplica":
                raise ValidationError("Solo se puede quitar cuando la versión vigente está en 'no_aplica'.")
            _set_audit_user(request)
            exec_void(
                """
                UPDATE quotes
                   SET estado='pendiente',
                       fecha_emitido=NULL,
                       fecha_aprobado=NULL,
                       fecha_rechazado=NULL,
                       rechazo_comentario=NULL,
                       pdf_url=NULL
                 WHERE id=%s
                """,
                [current["id"]],
            )
            exec_void("UPDATE ingresos SET presupuesto_estado='pendiente' WHERE id=%s", [ingreso_id])
        return _quote_response(request, ingreso_id)


__all__ = [
    'QuoteDetailView',
    'QuoteItemsView',
    'QuoteItemDetailView',
    'QuoteResumenView',
    'RechazarPresupuestoView',
    'QuoteVersionesView',
    'EmitirPresupuestoView',
    'QuotePdfView',
    'AprobarPresupuestoView',
    'AnularPresupuestoView',
    'NoAplicaPresupuestoView',
    'QuitarNoAplicaPresupuestoView',
]

