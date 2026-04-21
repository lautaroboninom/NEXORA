import os
import unicodedata
from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.http import HttpResponse

from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from ..pdf import render_quote_pdf
from ..permissions import user_has_permission
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


def _clean_text_or_default(value, default: str) -> str:
    cleaned = (value or "").strip()
    return cleaned or default


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
    if not recipients:
        return
    subject = f"Alerta stock minimo - {len(items)} repuesto(s)"
    lines = ["Se alcanzo el stock minimo en:", ""]
    for it in items:
        lines.append(f"- {it.get('codigo') or '-'} | {it.get('nombre') or '-'}")
        lines.append(f"  Stock: {it.get('stock_on_hand')} | Min: {it.get('stock_min')}")
        if it.get("ubicacion_deposito"):
            lines.append(f"  Ubicacion: {it.get('ubicacion_deposito')}")
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

    return rep_id, rep_codigo, rep_nombre, rep_costo, rep_precio


def _ensure_quote(ingreso_id: int):
    """Return quote id for ingreso, creating it if missing.

    Handles possible PK sequence desync safely using a savepoint so that a
    failed insert does not leave the transaction in broken state.
    """
    row = q("SELECT id FROM quotes WHERE ingreso_id=%s", [ingreso_id], one=True)
    if row:
        return row["id"]

    # Attempt insert inside a savepoint; if it fails, the savepoint rollback
    # will clear the aborted state so we can continue.
    try:
        with transaction.atomic():
            new_id = exec_returning(
                "INSERT INTO quotes(ingreso_id) VALUES (%s) RETURNING id",
                [ingreso_id],
            )
            return new_id
    except Exception:
        # If another request raced or sequence was fixed, the row may exist now
        row2 = q("SELECT id FROM quotes WHERE ingreso_id=%s", [ingreso_id], one=True)
        if row2:
            return row2["id"]
        # Proactive sequence resync + retry once inside a savepoint
        if connection.vendor == "postgresql":
            try:
                with transaction.atomic():
                    with connection.cursor() as cur:
                        cur.execute(
                            "SELECT setval(pg_get_serial_sequence('quotes','id'), COALESCE((SELECT MAX(id) FROM quotes), 1))"
                        )
                    new_id2 = exec_returning(
                        "INSERT INTO quotes(ingreso_id) VALUES (%s) RETURNING id",
                        [ingreso_id],
                    )
                    return new_id2
            except Exception:
                pass
        # Last check
        row3 = q("SELECT id FROM quotes WHERE ingreso_id=%s", [ingreso_id], one=True)
        if row3:
            return row3["id"]
        # Give up; propagate to caller
        raise


def _load_quote_payload(ingreso_id: int):
    head = q(
        """
        SELECT
          q.id AS quote_id,
          q.estado,
          q.moneda,
          q.subtotal,
          q.iva_21,
          q.total,
          COALESCE(NULLIF(q.autorizado_por, ''), %s) AS autorizado_por,
          COALESCE(NULLIF(q.forma_pago, ''), %s) AS forma_pago,
          COALESCE(NULLIF(q.plazo_entrega_txt, ''), %s) AS plazo_entrega_txt,
          COALESCE(NULLIF(q.garantia_txt, ''), %s) AS garantia_txt,
          COALESCE(NULLIF(q.mant_oferta_txt, ''), %s) AS mant_oferta_txt
        FROM quotes q
        WHERE q.ingreso_id=%s
        """,
        [
            DEFAULT_AUTORIZADO_POR,
            DEFAULT_FORMA_PAGO,
            DEFAULT_PLAZO_ENTREGA_TXT,
            DEFAULT_GARANTIA_TXT,
            DEFAULT_MANT_OFERTA_TXT,
            ingreso_id,
        ],
        one=True,
    )

    if not head:
        qid = _ensure_quote(ingreso_id)
        head = q(
            """
            SELECT
              q.id AS quote_id,
              q.estado,
              q.moneda,
              q.subtotal,
              q.iva_21,
              q.total,
              COALESCE(NULLIF(q.autorizado_por, ''), %s) AS autorizado_por,
              COALESCE(NULLIF(q.forma_pago, ''), %s) AS forma_pago,
              COALESCE(NULLIF(q.plazo_entrega_txt, ''), %s) AS plazo_entrega_txt,
              COALESCE(NULLIF(q.garantia_txt, ''), %s) AS garantia_txt,
              COALESCE(NULLIF(q.mant_oferta_txt, ''), %s) AS mant_oferta_txt
            FROM quotes q WHERE q.id=%s
            """,
            [
                DEFAULT_AUTORIZADO_POR,
                DEFAULT_FORMA_PAGO,
                DEFAULT_PLAZO_ENTREGA_TXT,
                DEFAULT_GARANTIA_TXT,
                DEFAULT_MANT_OFERTA_TXT,
                qid,
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
        JOIN quotes q ON q.id = qi.quote_id
        WHERE q.ingreso_id=%s
        ORDER BY qi.id ASC
        """,
        [ingreso_id],
    ) or []

    tot_rep = q(
        """
        SELECT COALESCE(SUM(qi.qty*qi.precio_u),0) AS x
        FROM quote_items qi
        JOIN quotes q ON q.id=qi.quote_id
        WHERE q.ingreso_id=%s AND qi.tipo='repuesto'
        """,
        [ingreso_id],
        one=True,
    )["x"]

    mano_obra = q(
        """
        SELECT COALESCE(SUM(qi.qty*qi.precio_u),0) AS x
        FROM quote_items qi
        JOIN quotes q ON q.id=qi.quote_id
        WHERE q.ingreso_id=%s AND qi.tipo='mano_obra'
        """,
        [ingreso_id],
        one=True,
    )["x"]

    subtotal_calc = sum((it.get("subtotal") or money(0)) for it in items)
    subtotal_calc = money(subtotal_calc)

    IVA = money("0.21")
    iva21_calc = money(subtotal_calc * IVA)
    total_calc = money(subtotal_calc + iva21_calc)

    return {
        "ingreso_id": ingreso_id,
        "quote_id": head["quote_id"],
        "estado": head["estado"],
        "moneda": head["moneda"],
        "autorizado_por": head["autorizado_por"],
        "forma_pago": head["forma_pago"],
        "plazo_entrega_txt": head["plazo_entrega_txt"],
        "garantia_txt": head["garantia_txt"],
        "mant_oferta_txt": head["mant_oferta_txt"],
        "items": items,
        "tot_repuestos": tot_rep,
        "mano_obra": mano_obra,
        "subtotal": subtotal_calc,
        "iva_21": iva21_calc,
        "total": total_calc,
    }


class QuoteDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico", "recepcion"])
        _ensure_quote(ingreso_id)
        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)


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

        rep_id = rep_codigo = rep_nombre = rep_costo = rep_precio = None
        if tipo == "repuesto":
            rep_id, rep_codigo, rep_nombre, rep_costo, rep_precio = _resolve_repuesto_data(repuesto_id, repuesto_codigo)
            if rep_nombre:
                desc = rep_nombre
        else:
            repuesto_id = None
            repuesto_codigo = ""

        precio_raw = d.get("precio_u")
        try:
            if precio_raw is None or (isinstance(precio_raw, str) and precio_raw.strip() == ""):
                precio = money(rep_precio) if rep_precio is not None else money(precio_raw)
            else:
                precio = money(precio_raw)
        except (TypeError, ValueError):
            raise ValidationError("qty y precio_u deben ser numéricos")

        if not desc:
            raise ValidationError("descripcion requerida")

        qid = _ensure_quote(ingreso_id)
        _set_audit_user(request)
        exec_void(
            """
            INSERT INTO quote_items(quote_id, tipo, descripcion, qty, precio_u, repuesto_id, repuesto_codigo, costo_u_neto)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [qid, tipo, desc, qty, precio, rep_id, rep_codigo, rep_costo],
        )
        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data, status=201)


class QuoteItemDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, ingreso_id: int, item_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        d = request.data or {}
        sets, params = [], []
        desc_from_rep = None
        if "tipo" in d:
            t = (d.get("tipo") or "").strip()
            if t not in ("repuesto", "mano_obra", "servicio"):
                raise ValidationError("tipo inválido")
            sets.append("tipo=%s"); params.append(t)
            if t != "repuesto":
                sets.append("repuesto_id=%s"); params.append(None)
                sets.append("repuesto_codigo=%s"); params.append(None)
                sets.append("costo_u_neto=%s"); params.append(None)
        if "repuesto_id" in d or "repuesto_codigo" in d:
            repuesto_id = None
            if d.get("repuesto_id") not in (None, ""):
                try:
                    repuesto_id = int(d.get("repuesto_id"))
                except (TypeError, ValueError):
                    repuesto_id = None
            repuesto_codigo = (d.get("repuesto_codigo") or "").strip() if "repuesto_codigo" in d else None

            rep_id, rep_code, rep_nombre, rep_costo, rep_precio = _resolve_repuesto_data(repuesto_id, repuesto_codigo)
            if rep_nombre:
                sets.append("repuesto_id=%s"); params.append(rep_id)
                sets.append("repuesto_codigo=%s"); params.append(rep_code)
                sets.append("costo_u_neto=%s"); params.append(rep_costo)
                if "precio_u" not in d and rep_precio is not None:
                    sets.append("precio_u=%s"); params.append(rep_precio)
                desc_from_rep = rep_nombre
            else:
                if repuesto_id is None and (repuesto_codigo is None or repuesto_codigo == ""):
                    sets.append("repuesto_id=%s"); params.append(None)
                    sets.append("repuesto_codigo=%s"); params.append(None)
                    sets.append("costo_u_neto=%s"); params.append(None)
                else:
                    sets.append("repuesto_id=%s"); params.append(None)
                    if repuesto_codigo is not None:
                        sets.append("repuesto_codigo=%s"); params.append(repuesto_codigo.strip().upper() or None)
                    sets.append("costo_u_neto=%s"); params.append(None)
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

        if sets:
            _set_audit_user(request)
            params += [ingreso_id, item_id]
            exec_void(
                f"""
                UPDATE quote_items qi
                   SET {', '.join(sets)}
                FROM quotes q
                WHERE qi.quote_id=q.id AND q.ingreso_id=%s AND qi.id=%s
                """,
                params,
            )
        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)

    def delete(self, request, ingreso_id: int, item_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        _set_audit_user(request)
        exec_void(
            """
            DELETE FROM quote_items qi
            USING quotes q
            WHERE qi.quote_id=q.id AND q.ingreso_id=%s AND qi.id=%s
            """,
            [ingreso_id, item_id],
        )
        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)


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

        qid = _ensure_quote(ingreso_id)
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
        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)


class EmitirPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        autorizado_por = _clean_text_or_default(request.data.get("autorizado_por"), DEFAULT_AUTORIZADO_POR)
        forma_pago = _clean_text_or_default(request.data.get("forma_pago"), "A definir")
        plazo_entrega_txt = _clean_text_or_default(request.data.get("plazo_entrega_txt"), DEFAULT_PLAZO_ENTREGA_TXT)
        garantia_txt = _clean_text_or_default(request.data.get("garantia_txt"), DEFAULT_GARANTIA_TXT)
        mant_oferta_txt = _clean_text_or_default(request.data.get("mant_oferta_txt"), DEFAULT_MANT_OFERTA_TXT)
        qid = _ensure_quote(ingreso_id)
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

        fname, pdf = render_quote_pdf(ingreso_id)
        try:
            save_dir = getattr(settings, "QUOTES_SAVE_DIR", None)
            if save_dir and pdf:
                os.makedirs(save_dir, exist_ok=True)
                dest = os.path.join(save_dir, fname)
                with open(dest, "wb") as f:
                    f.write(pdf)
        except Exception:
            pass
        pdf_url = f"/api/quotes/{ingreso_id}/pdf/"
        exec_void("UPDATE quotes SET pdf_url=%s WHERE id=%s", [pdf_url, qid])
        data = _load_quote_payload(ingreso_id); data["pdf_url"] = pdf_url
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)


class QuotePdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico", "recepcion"])
        fname, pdf = render_quote_pdf(ingreso_id)
        if not pdf:
            raise ValidationError("Ingreso no encontrado o sin presupuesto")
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp


class AprobarPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        qid = _ensure_quote(ingreso_id)
        was_approved = False
        alert_items = []
        with transaction.atomic():
            row = q("SELECT estado FROM quotes WHERE id=%s FOR UPDATE", [qid], one=True) or {}
            was_approved = (row.get("estado") or "").strip() == "aprobado"
            _set_audit_user(request)
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

        try:
            row = q(
                """
                SELECT
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
            pass

        if alert_items:
            try:
                _send_stock_min_alerts(alert_items)
            except Exception:
                pass

        try:
            fname, pdf = render_quote_pdf(ingreso_id)
            save_dir = getattr(settings, "QUOTES_SAVE_DIR", None)
            if save_dir and pdf:
                import os
                os.makedirs(save_dir, exist_ok=True)
                dest = os.path.join(save_dir, fname)
                with open(dest, "wb") as f:
                    f.write(pdf)
        except Exception:
            pass

        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)


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
            presu_estado = (row.get("presupuesto_estado") or "").strip()
            if presu_estado not in ("presupuestado", "aprobado"):
                raise ValidationError("Solo se puede anular cuando el presupuesto esta 'presupuestado' o 'aprobado'.")
            if presu_estado == "aprobado" and estado_ingreso in ("entregado", "alquilado", "baja"):
                raise ValidationError("No se puede anular un presupuesto aprobado en estado entregado, alquilado o baja.")

            qid = _ensure_quote(ingreso_id)
            q("SELECT id FROM quotes WHERE id=%s FOR UPDATE", [qid], one=True)
            _set_audit_user(request)

            if presu_estado == "aprobado":
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
                       pdf_url=NULL
                 WHERE id=%s
                """,
                [qid],
            )
            if presu_estado == "aprobado":
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

        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)

class NoAplicaPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        row = q("SELECT presupuesto_estado FROM ingresos WHERE id=%s", [ingreso_id], one=True)
        if not row:
            raise ValidationError("Ingreso no encontrado")
        cur = (row.get("presupuesto_estado") or "").strip()
        if cur in ("presupuestado", "aprobado"):
            raise ValidationError("Primero debe anular el presupuesto para marcar 'No aplica'.")

        qid = _ensure_quote(ingreso_id)
        _set_audit_user(request)
        exec_void(
            """
            UPDATE quotes
               SET estado='no_aplica',
                   fecha_emitido=NULL,
                   fecha_aprobado=NULL,
                   pdf_url=NULL
             WHERE id=%s
            """,
            [qid],
        )
        exec_void("UPDATE ingresos SET presupuesto_estado='no_aplica' WHERE id=%s", [ingreso_id])
        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)


class QuitarNoAplicaPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles_strict(request, ["jefe", "admin"])
        row = q("SELECT presupuesto_estado FROM ingresos WHERE id=%s", [ingreso_id], one=True)
        if not row:
            raise ValidationError("Ingreso no encontrado")
        cur = (row.get("presupuesto_estado") or "").strip()
        if cur != "no_aplica":
            raise ValidationError("Solo se puede quitar cuando esta en 'no_aplica'.")

        qid = _ensure_quote(ingreso_id)
        _set_audit_user(request)
        exec_void(
            """
            UPDATE quotes
               SET estado='pendiente',
                   fecha_emitido=NULL,
                   fecha_aprobado=NULL,
                   pdf_url=NULL
             WHERE id=%s
            """,
            [qid],
        )
        exec_void("UPDATE ingresos SET presupuesto_estado='pendiente' WHERE id=%s", [ingreso_id])
        data = _load_quote_payload(ingreso_id)
        data = _mask_costs(data, _can_view_costs(request.user))
        return Response(QuoteDetailSerializer(data).data)


__all__ = [
    'QuoteDetailView',
    'QuoteItemsView',
    'QuoteItemDetailView',
    'QuoteResumenView',
    'EmitirPresupuestoView',
    'QuotePdfView',
    'AprobarPresupuestoView',
    'AnularPresupuestoView',
    'NoAplicaPresupuestoView',
    'QuitarNoAplicaPresupuestoView',
]

