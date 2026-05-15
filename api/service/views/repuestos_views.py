import datetime as dt
import unicodedata
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import q, exec_void, exec_returning, require_roles, money, _email_append_footer_text
from ..notifications import emit_notification
from ..repuestos import (
    get_repuestos_config,
    calc_precio_venta,
    normalize_costo_moneda,
    resolve_repuesto_cost_fields,
)


FOUR_DEC = Decimal("0.0001")


def _can_view_costs(user) -> bool:
    rol = (getattr(user, "rol", "") or "").strip().lower()
    return rol in ("jefe", "jefe_veedor", "admin")


def _parse_decimal_field(val, label, allow_none=False):
    if val is None or (isinstance(val, str) and val.strip() == ""):
        if allow_none:
            return None
        raise ValidationError(f"{label} requerido")
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    s = str(val).strip().replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValidationError(f"{label} inválido")


def _parse_int_decimal_field(val, label, allow_none=False):
    dec = _parse_decimal_field(val, label, allow_none=allow_none)
    if dec is None:
        return None
    if dec != dec.to_integral_value():
        raise ValidationError(f"{label} debe ser entero")
    return dec.quantize(Decimal("1"))


def _to_int_or_none(val):
    if val is None:
        return None
    try:
        if isinstance(val, Decimal):
            return int(val)
        return int(val)
    except (TypeError, ValueError):
        return val


def _as_int_decimal(val):
    if val is None:
        return Decimal("0")
    if isinstance(val, Decimal):
        return val.quantize(Decimal("1"))
    return Decimal(str(val)).quantize(Decimal("1"))


def _rol(user) -> str:
    return (getattr(user, "rol", "") or "").strip().lower()


def _is_manager(user) -> bool:
    return _rol(user) in ("jefe", "jefe_veedor")


def _clean_text(val):
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _parse_date_field(val, label):
    if val is None or (isinstance(val, str) and val.strip() == ""):
        return None
    if isinstance(val, dt.datetime):
        return val.date()
    if isinstance(val, dt.date):
        return val
    s = str(val).strip()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise ValidationError(f"{label} inválido")


def _parse_int_field(val, label):
    if val is None or (isinstance(val, str) and val.strip() == ""):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} inválido")


def _parse_subrubro_codigo(val):
    code = _clean_text(val)
    if not code:
        raise ValidationError("subrubro_codigo requerido")
    if not (code.isdigit() and len(code) == 4):
        raise ValidationError("subrubro_codigo inválido")
    return code


def _can_edit_costs(user) -> bool:
    return _can_view_costs(user)


def _normalize_equipo_token(value):
    raw = str(value or "")
    clean = unicodedata.normalize("NFD", raw)
    clean = "".join(ch for ch in clean if unicodedata.category(ch) != "Mn")
    return " ".join(clean.lower().split())


def _split_equipo_parts(value):
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _parse_equipo_lines(value):
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _is_prefix_parts(short_parts, long_parts):
    if len(short_parts) > len(long_parts):
        return False
    for idx, short_part in enumerate(short_parts):
        long_part = long_parts[idx]
        if not _normalize_equipo_token(long_part).startswith(_normalize_equipo_token(short_part)):
            return False
    return True


def _match_equipo_entry(filter_raw, entry_raw):
    filter_txt = str(filter_raw or "").strip()
    if not filter_txt:
        return True
    entry_parts = _split_equipo_parts(entry_raw)
    if not entry_parts:
        return False
    if "|" not in filter_txt:
        needle = _normalize_equipo_token(filter_txt)
        return any(needle in _normalize_equipo_token(part) for part in entry_parts)
    filter_parts = _split_equipo_parts(filter_txt)
    if not filter_parts:
        return False
    return _is_prefix_parts(filter_parts, entry_parts) or _is_prefix_parts(entry_parts, filter_parts)


def _match_equipo_filter(filter_raw, estado_raw):
    filter_txt = str(filter_raw or "").strip()
    if not filter_txt:
        return True
    entries = _parse_equipo_lines(estado_raw)
    if not entries:
        return False
    return any(_match_equipo_entry(filter_txt, entry) for entry in entries)


def _equipo_hint_rank(equipo_hints, estado_raw):
    best = 0
    for idx, hint in enumerate(equipo_hints or []):
        hint_txt = str(hint or "").strip()
        if not hint_txt:
            continue
        if not _match_equipo_filter(hint_txt, estado_raw):
            continue
        parts = _split_equipo_parts(hint_txt)
        score = ((len(equipo_hints) - idx) * 10) + len(parts)
        if score > best:
            best = score
    return best


def _serialize_repuesto_cost(row, cfg, allow_costs: bool):
    costo = resolve_repuesto_cost_fields(
        costo_moneda=row.get("costo_moneda"),
        costo_usd=row.get("costo_usd"),
        costo_neto=row.get("costo_neto"),
        dolar_ars=cfg.get("dolar_ars"),
    )
    row["costo_moneda"] = costo.get("costo_moneda")
    row["costo_valor"] = costo.get("costo_valor")
    row["costo_ars"] = costo.get("costo_ars")
    row["precio_venta"] = calc_precio_venta(
        None,
        None,
        cfg.get("multiplicador_general"),
        row.get("multiplicador"),
        costo_ars=costo.get("costo_ars"),
    )
    if not allow_costs:
        row["costo_moneda"] = None
        row["costo_valor"] = None
        row["costo_ars"] = None
        row["precio_venta"] = None
    row.pop("costo_usd", None)
    row.pop("costo_neto", None)
    return row


def _parse_repuesto_cost_input(data, user, current=None):
    if not any(key in data for key in ("costo_valor", "costo_moneda", "costo_usd")):
        return None
    if not _can_edit_costs(user):
        raise PermissionDenied("No autorizado para editar costo")

    if "costo_valor" in data:
        valor = _parse_decimal_field(data.get("costo_valor"), "costo_valor", allow_none=True)
    elif "costo_usd" in data:
        valor = _parse_decimal_field(data.get("costo_usd"), "costo_usd", allow_none=True)
    else:
        current_cost = resolve_repuesto_cost_fields(
            costo_moneda=(current or {}).get("costo_moneda"),
            costo_usd=(current or {}).get("costo_usd"),
            costo_neto=(current or {}).get("costo_neto"),
            dolar_ars=get_repuestos_config().get("dolar_ars"),
        )
        valor = current_cost.get("costo_valor")

    if valor is not None and valor < 0:
        raise ValidationError("costo_valor inválido")
    if valor is not None:
        valor = money(valor)

    if "costo_moneda" in data:
        moneda = normalize_costo_moneda(data.get("costo_moneda"))
    elif "costo_usd" in data:
        moneda = "USD"
    else:
        moneda = normalize_costo_moneda((current or {}).get("costo_moneda"))

    if valor is None:
        moneda = "USD"

    return resolve_repuesto_cost_fields(
        costo_moneda=moneda,
        costo_valor=valor,
        dolar_ars=get_repuestos_config().get("dolar_ars"),
    )


def _lock_subrubro(code: str):
    return q(
        """
        SELECT codigo, nombre
        FROM repuestos_subrubros
        WHERE codigo=%s AND activo
        FOR UPDATE
        """,
        [code],
        one=True,
    )


def _get_next_subrubro_num(code: str):
    row = q(
        """
        WITH used AS (
          SELECT DISTINCT CAST(RIGHT(codigo,3) AS INTEGER) AS num
          FROM catalogo_repuestos
          WHERE codigo ~ '^[0-9]{7}$'
            AND LEFT(codigo,4) = %s
        )
        SELECT gs AS num
        FROM generate_series(1,999) gs
        LEFT JOIN used u ON u.num = gs
        WHERE u.num IS NULL
        ORDER BY gs
        LIMIT 1
        """,
        [code],
        one=True,
    )
    return row.get("num") if row else None


def _get_active_stock_perm(tecnico_id: int | None):
    if not tecnico_id:
        return None
    return q(
        """
        SELECT id, tecnico_id, expires_at
        FROM repuestos_stock_permisos
        WHERE tecnico_id=%s
          AND revoked_at IS NULL
          AND expires_at > NOW()
        ORDER BY expires_at DESC, id DESC
        LIMIT 1
        """,
        [tecnico_id],
        one=True,
    )


def _can_edit_stock(user) -> bool:
    if _is_manager(user):
        return True
    if _rol(user) != "tecnico":
        return False
    uid = getattr(user, "id", None)
    if not uid:
        return False
    return bool(_get_active_stock_perm(uid))


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
        pass
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


def _get_or_create_proveedor(nombre: str):
    name = (nombre or "").strip()
    if not name:
        return None
    row = q(
        "SELECT id FROM proveedores_externos WHERE LOWER(nombre)=LOWER(%s)",
        [name],
        one=True,
    )
    if row:
        return row.get("id")
    try:
        pid = exec_returning(
            "INSERT INTO proveedores_externos (nombre) VALUES (%s) RETURNING id",
            [name],
        )
        return pid
    except Exception:
        row2 = q(
            "SELECT id FROM proveedores_externos WHERE LOWER(nombre)=LOWER(%s)",
            [name],
            one=True,
        )
        if row2:
            return row2.get("id")
        raise


def _sync_repuesto_proveedores(repuesto_id: int, proveedores: list[dict]):
    cleaned = []
    for raw in proveedores or []:
        if not isinstance(raw, dict):
            continue
        pid = raw.get("proveedor_id")
        if pid not in (None, ""):
            pid = _parse_int_field(pid, "proveedor_id")
        nombre = (raw.get("proveedor_nombre") or raw.get("nombre") or "").strip()
        if not pid and nombre:
            pid = _get_or_create_proveedor(nombre)
        if not pid:
            continue
        cleaned.append({
            "proveedor_id": pid,
            "sku_proveedor": _clean_text(raw.get("sku_proveedor")),
            "lead_time_dias": _parse_int_field(raw.get("lead_time_dias"), "lead_time_dias"),
            "prioridad": _parse_int_field(raw.get("prioridad"), "prioridad"),
            "ultima_compra": _parse_date_field(raw.get("ultima_compra"), "ultima_compra"),
        })

    by_id = {c["proveedor_id"]: c for c in cleaned}
    target = list(by_id.values())
    existing = q(
        "SELECT proveedor_id FROM repuestos_proveedores WHERE repuesto_id=%s",
        [repuesto_id],
    ) or []
    existing_ids = {r.get("proveedor_id") for r in existing}
    target_ids = {c.get("proveedor_id") for c in target}
    to_delete = [pid for pid in existing_ids if pid and pid not in target_ids]
    if to_delete:
        exec_void(
            "DELETE FROM repuestos_proveedores WHERE repuesto_id=%s AND proveedor_id = ANY(%s)",
            [repuesto_id, to_delete],
        )

    for item in target:
        exec_void(
            """
            INSERT INTO repuestos_proveedores
              (repuesto_id, proveedor_id, sku_proveedor, lead_time_dias, prioridad, ultima_compra, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (repuesto_id, proveedor_id) DO UPDATE SET
              sku_proveedor=EXCLUDED.sku_proveedor,
              lead_time_dias=EXCLUDED.lead_time_dias,
              prioridad=EXCLUDED.prioridad,
              ultima_compra=EXCLUDED.ultima_compra,
              updated_at=NOW()
            """,
            [
                repuesto_id,
                item.get("proveedor_id"),
                item.get("sku_proveedor"),
                item.get("lead_time_dias"),
                item.get("prioridad"),
                item.get("ultima_compra"),
            ],
        )


def _as_date(val):
    if val is None:
        return None
    if isinstance(val, dt.datetime):
        return val.date()
    if isinstance(val, dt.date):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _max_date(curr: dt.date | None, candidate: dt.date | None) -> dt.date | None:
    curr_date = _as_date(curr)
    cand_date = _as_date(candidate)
    if curr_date is None:
        return cand_date
    if cand_date is None:
        return curr_date
    return cand_date if cand_date > curr_date else curr_date


def _log_repuesto_cambio(repuesto_id, codigo, accion, nombre_prev, nombre_new, user_id, nota=None):
    exec_void(
        """
        INSERT INTO repuestos_cambios
          (repuesto_id, codigo, accion, nombre_prev, nombre_new, nota, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        [
            repuesto_id,
            codigo,
            accion,
            nombre_prev,
            nombre_new,
            _clean_text(nota),
            user_id,
        ],
    )


def _load_repuesto_detail(repuesto_id: int, user):
    row = q(
        """
        SELECT
          id, codigo, nombre, costo_neto, costo_usd, costo_moneda, multiplicador,
          stock_on_hand, stock_min,
          tipo_articulo, categoria, unidad_medida, marca_fabricante, nro_parte,
          ubicacion_deposito, estado, notas,
          fecha_ultima_compra, fecha_ultimo_conteo, fecha_vencimiento
        FROM catalogo_repuestos
        WHERE id=%s
        """,
        [repuesto_id],
        one=True,
    )
    if not row:
        return None
    cfg = get_repuestos_config()
    _serialize_repuesto_cost(row, cfg, _can_view_costs(user))
    row["multiplicador_aplicado"] = (
        row.get("multiplicador")
        if row.get("multiplicador") is not None
        else cfg.get("multiplicador_general")
    )
    stock_on_hand = row.get("stock_on_hand") or 0
    stock_min = row.get("stock_min") or 0
    row["stock_alerta"] = stock_on_hand <= stock_min
    row["stock_negativo"] = stock_on_hand < 0
    row["stock_on_hand"] = _to_int_or_none(row.get("stock_on_hand"))
    row["stock_min"] = _to_int_or_none(row.get("stock_min"))

    proveedores = q(
        """
        SELECT
          rp.id, rp.proveedor_id, pe.nombre AS proveedor_nombre,
          rp.sku_proveedor, rp.lead_time_dias, rp.prioridad, rp.ultima_compra
        FROM repuestos_proveedores rp
        JOIN proveedores_externos pe ON pe.id = rp.proveedor_id
        WHERE rp.repuesto_id=%s
        ORDER BY COALESCE(rp.prioridad, 999), pe.nombre
        """,
        [repuesto_id],
    ) or []
    row["proveedores"] = proveedores

    perm = _get_active_stock_perm(getattr(user, "id", None))
    row["stock_permiso"] = {
        "activo": bool(perm),
        "expires_at": perm.get("expires_at") if perm else None,
    }
    return row


class RepuestosSubrubrosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        rows = q(
            """
            SELECT codigo, nombre
            FROM repuestos_subrubros
            WHERE activo
            ORDER BY codigo
            """,
            [],
        ) or []
        return Response(rows)

    def post(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        d = request.data or {}
        codigo = _parse_subrubro_codigo(d.get("codigo") or d.get("subrubro_codigo"))
        nombre = _clean_text(d.get("nombre"))
        if not nombre:
            raise ValidationError("nombre requerido")

        with transaction.atomic():
            row = q(
                """
                SELECT codigo, activo
                FROM repuestos_subrubros
                WHERE codigo=%s
                FOR UPDATE
                """,
                [codigo],
                one=True,
            )
            dup_name = q(
                """
                SELECT codigo
                FROM repuestos_subrubros
                WHERE activo
                  AND LOWER(nombre) = LOWER(%s)
                  AND codigo <> %s
                LIMIT 1
                """,
                [nombre, codigo],
                one=True,
            )
            if dup_name:
                raise ValidationError("nombre ya existe")

            if row and row.get("activo"):
                raise ValidationError("código ya existe")
            if row:
                exec_void(
                    """
                    UPDATE repuestos_subrubros
                       SET nombre=%s,
                           activo=TRUE,
                           updated_at=NOW()
                     WHERE codigo=%s
                    """,
                    [nombre, codigo],
                )
            else:
                exec_void(
                    """
                    INSERT INTO repuestos_subrubros
                      (codigo, nombre, activo, updated_at)
                    VALUES (%s, %s, TRUE, NOW())
                    """,
                    [codigo, nombre],
                )

        created = q(
            """
            SELECT codigo, nombre
            FROM repuestos_subrubros
            WHERE codigo=%s AND activo
            """,
            [codigo],
            one=True,
        )
        return Response(created or {"codigo": codigo, "nombre": nombre}, status=201)


class RepuestosSubrubroDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, subrubro_codigo: str):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        codigo = _parse_subrubro_codigo(subrubro_codigo)
        nombre = _clean_text((request.data or {}).get("nombre"))
        if not nombre:
            raise ValidationError("nombre requerido")

        with transaction.atomic():
            row = q(
                """
                SELECT codigo
                FROM repuestos_subrubros
                WHERE codigo=%s AND activo
                FOR UPDATE
                """,
                [codigo],
                one=True,
            )
            if not row:
                raise ValidationError("subrubro no encontrado")
            dup_name = q(
                """
                SELECT codigo
                FROM repuestos_subrubros
                WHERE activo
                  AND LOWER(nombre)=LOWER(%s)
                  AND codigo<>%s
                LIMIT 1
                """,
                [nombre, codigo],
                one=True,
            )
            if dup_name:
                raise ValidationError("nombre ya existe")
            exec_void(
                """
                UPDATE repuestos_subrubros
                   SET nombre=%s,
                       updated_at=NOW()
                 WHERE codigo=%s
                """,
                [nombre, codigo],
            )

        updated = q(
            """
            SELECT codigo, nombre
            FROM repuestos_subrubros
            WHERE codigo=%s AND activo
            """,
            [codigo],
            one=True,
        )
        return Response(updated or {"codigo": codigo, "nombre": nombre})

    def delete(self, request, subrubro_codigo: str):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        codigo = _parse_subrubro_codigo(subrubro_codigo)
        with transaction.atomic():
            row = q(
                """
                SELECT codigo
                FROM repuestos_subrubros
                WHERE codigo=%s AND activo
                FOR UPDATE
                """,
                [codigo],
                one=True,
            )
            if not row:
                raise ValidationError("subrubro no encontrado")
            used = q(
                """
                SELECT id
                FROM catalogo_repuestos
                WHERE activo
                  AND codigo ~ '^[0-9]{7}$'
                  AND LEFT(codigo,4)=%s
                LIMIT 1
                """,
                [codigo],
                one=True,
            )
            if used:
                raise ValidationError("No se puede eliminar: hay repuestos activos asociados")
            exec_void(
                """
                UPDATE repuestos_subrubros
                   SET activo=FALSE,
                       updated_at=NOW()
                 WHERE codigo=%s
                """,
                [codigo],
            )
        return Response({"ok": True})


class CatalogoRepuestosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico", "recepcion"])
        qtxt = (request.GET.get("q") or "").strip()
        equipo_hints = [str(v or "").strip() for v in request.GET.getlist("equipo_hint") if str(v or "").strip()]
        limit_raw = (request.GET.get("limit") or "").strip()
        try:
            limit = int(limit_raw or 50000)
        except ValueError:
            limit = 50000
        limit = max(1, min(limit, 2000))
        query_limit = 2000 if equipo_hints else limit

        where = ["activo"]
        params = []
        if qtxt:
            like = f"%{qtxt}%"
            where.append("(codigo ILIKE %s OR nombre ILIKE %s)")
            params.extend([like, like])

        where_sql = " WHERE " + " AND ".join(where) if where else ""
        rows = q(
            f"""
            SELECT id, codigo, nombre, costo_neto, costo_usd, costo_moneda, multiplicador, estado
            FROM catalogo_repuestos
            {where_sql}
            ORDER BY codigo DESC
            LIMIT %s
            """,
            [*params, query_limit],
        ) or []

        cfg = get_repuestos_config()
        allow_costs = _can_view_costs(request.user)
        if equipo_hints:
            ranked = []
            for idx, row in enumerate(rows):
                rank = _equipo_hint_rank(equipo_hints, row.get("estado"))
                ranked.append((idx, rank, row))
            if any(rank > 0 for _, rank, _ in ranked):
                ranked.sort(key=lambda item: (-1 if item[1] > 0 else 0, -item[1], item[0]))
                rows = [row for _, _, row in ranked]
        rows = rows[:limit]

        for row in rows:
            _serialize_repuesto_cost(row, cfg, allow_costs)
            row.pop("multiplicador", None)
            row.pop("estado", None)

        return Response(rows)


class RepuestosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        qtxt = (request.GET.get("q") or "").strip()
        limit_raw = (request.GET.get("limit") or "").strip()
        offset_raw = (request.GET.get("offset") or "").strip()
        order_raw = (request.GET.get("order") or "").strip().lower()
        dir_raw = (request.GET.get("dir") or "").strip().lower()
        try:
            limit = int(limit_raw or 2000)
        except ValueError:
            limit = 2000
        try:
            offset = int(offset_raw or 0)
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 2000))
        offset = max(0, offset)

        where = ["activo"]
        params = []
        if qtxt:
            like = f"%{qtxt}%"
            where.append("(codigo ILIKE %s OR nombre ILIKE %s)")
            params.extend([like, like])

        where_sql = " WHERE " + " AND ".join(where) if where else ""
        order_map = {
            "codigo": "codigo",
            "stock": "COALESCE(stock_on_hand, 0)",
            "stock_on_hand": "COALESCE(stock_on_hand, 0)",
            "stock_min": "COALESCE(stock_min, 0)",
        }
        dir_sql = "ASC" if dir_raw == "asc" else "DESC"
        order_expr = order_map.get(order_raw or "", "codigo")
        if order_expr == "codigo":
            order_sql = f"ORDER BY {order_expr} {dir_sql}"
        else:
            order_sql = f"ORDER BY {order_expr} {dir_sql}, codigo DESC"
        rows = q(
            f"""
            SELECT id, codigo, nombre, costo_neto, costo_usd, costo_moneda, multiplicador, stock_on_hand, stock_min, estado
            FROM catalogo_repuestos
            {where_sql}
            {order_sql}
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ) or []

        cfg = get_repuestos_config()
        mult_general = cfg.get("multiplicador_general")
        allow_costs = _can_view_costs(request.user)

        for row in rows:
            _serialize_repuesto_cost(row, cfg, allow_costs)
            row["multiplicador_aplicado"] = (
                row.get("multiplicador")
                if row.get("multiplicador") is not None
                else mult_general
            )
            stock_on_hand = row.get("stock_on_hand") or 0
            stock_min = row.get("stock_min") or 0
            row["stock_alerta"] = stock_on_hand <= stock_min
            row["stock_negativo"] = stock_on_hand < 0
            row["stock_on_hand"] = _to_int_or_none(row.get("stock_on_hand"))
            row["stock_min"] = _to_int_or_none(row.get("stock_min"))

        return Response(rows)

    def post(self, request):
        require_roles(request, ["jefe", "jefe_veedor", "tecnico"])
        d = request.data or {}
        is_manager = _is_manager(request.user)
        is_tech = _rol(request.user) == "tecnico"
        if not is_manager:
            if not (is_tech and _can_edit_stock(request.user)):
                raise PermissionDenied("No autorizado")
            forbidden = set(d.keys()) - {"subrubro_codigo", "nombre", "stock_on_hand"}
            if forbidden:
                raise PermissionDenied("No autorizado para editar detalles")
        if _clean_text(d.get("codigo")):
            raise ValidationError("código no permitido; use subrubro_codigo")
        subrubro_codigo = _parse_subrubro_codigo(d.get("subrubro_codigo"))
        nombre = _clean_text(d.get("nombre"))
        if not nombre:
            raise ValidationError("nombre requerido")

        stock_on_hand = _parse_int_decimal_field(d.get("stock_on_hand"), "stock_on_hand", allow_none=True)
        stock_on_hand = stock_on_hand if stock_on_hand is not None else Decimal("0")
        stock_min = Decimal("0")
        if is_manager:
            stock_min = _parse_int_decimal_field(d.get("stock_min"), "stock_min", allow_none=True)
            stock_min = stock_min if stock_min is not None else Decimal("0")
            if stock_min < 0:
                raise ValidationError("stock_min no puede ser negativo")

        mult = None
        if is_manager:
            mult = _parse_decimal_field(d.get("multiplicador"), "multiplicador", allow_none=True)
            if mult is not None and mult <= 0:
                raise ValidationError("multiplicador inválido")
            if mult is not None:
                mult = mult.quantize(FOUR_DEC)

        costo_usd = None
        if "costo_usd" in d and _can_edit_costs(request.user):
            costo_usd = _parse_decimal_field(d.get("costo_usd"), "costo_usd", allow_none=True)
            if costo_usd is not None:
                if costo_usd < 0:
                    raise ValidationError("costo_usd inválido")
                costo_usd = money(costo_usd)

        costo_payload = _parse_repuesto_cost_input(d, request.user)
        if costo_payload:
            costo_usd = costo_payload.get("costo_usd")
            costo_neto = costo_payload.get("costo_ars") or Decimal("0.00")
            costo_moneda = costo_payload.get("costo_moneda") or "USD"
        else:
            costo_neto = Decimal("0.00")
            costo_moneda = "USD"

        detail_text_fields = [
            "tipo_articulo",
            "categoria",
            "unidad_medida",
            "marca_fabricante",
            "nro_parte",
            "ubicacion_deposito",
            "estado",
            "notas",
        ]
        detail_date_fields = [
            "fecha_ultima_compra",
            "fecha_ultimo_conteo",
            "fecha_vencimiento",
        ]
        if is_manager:
            text_values = {field: _clean_text(d.get(field)) for field in detail_text_fields}
            date_values = {field: _parse_date_field(d.get(field), field) for field in detail_date_fields}
        else:
            text_values = {field: None for field in detail_text_fields}
            date_values = {field: None for field in detail_date_fields}

        with transaction.atomic():
            subrubro = _lock_subrubro(subrubro_codigo)
            if not subrubro:
                raise ValidationError("subrubro inválido")
            next_num = _get_next_subrubro_num(subrubro_codigo)
            if not next_num:
                raise ValidationError("Sin codigos disponibles para el subrubro")
            codigo = f"{subrubro_codigo}{int(next_num):03d}"
            exists = q(
                "SELECT id FROM catalogo_repuestos WHERE codigo=%s",
                [codigo],
                one=True,
            )
            if exists:
                raise ValidationError("código ya existe")
            new_id = exec_returning(
                """
                INSERT INTO catalogo_repuestos
                  (codigo, nombre, costo_neto, costo_usd, costo_moneda, multiplicador,
                   stock_on_hand, stock_min,
                   tipo_articulo, categoria, unidad_medida, marca_fabricante, nro_parte,
                   ubicacion_deposito, estado, notas,
                   fecha_ultima_compra, fecha_ultimo_conteo, fecha_vencimiento,
                   updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                RETURNING id
                """,
                [
                    codigo,
                    nombre,
                    costo_neto,
                    costo_usd,
                    costo_moneda,
                    mult,
                    stock_on_hand,
                    stock_min,
                    text_values.get("tipo_articulo"),
                    text_values.get("categoria"),
                    text_values.get("unidad_medida"),
                    text_values.get("marca_fabricante"),
                    text_values.get("nro_parte"),
                    text_values.get("ubicacion_deposito"),
                    text_values.get("estado"),
                    text_values.get("notas"),
                    date_values.get("fecha_ultima_compra"),
                    date_values.get("fecha_ultimo_conteo"),
                    date_values.get("fecha_vencimiento"),
                ],
            )
            if is_manager and "proveedores" in d:
                _sync_repuesto_proveedores(new_id, d.get("proveedores") or [])
        row = _load_repuesto_detail(new_id, request.user)
        return Response(row or {"id": new_id}, status=201)


class RepuestoDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, repuesto_id: int):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        row = _load_repuesto_detail(repuesto_id, request.user)
        if not row:
            raise ValidationError("Repuesto no encontrado")
        return Response(row)

    def patch(self, request, repuesto_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "tecnico"])
        d = request.data or {}
        is_manager = _is_manager(request.user)
        can_edit_stock = _can_edit_stock(request.user)
        sets = []
        params = []
        nota = _clean_text(d.get("nota"))
        nombre_new = None
        can_edit_costs = _can_edit_costs(request.user)

        stock_new = None
        stock_min_new = None
        if "nombre" in d:
            if not can_edit_stock:
                raise PermissionDenied("No autorizado para editar nombre")
            nombre_new = _clean_text(d.get("nombre"))
            if not nombre_new:
                raise ValidationError("nombre requerido")
            sets.append("nombre=%s"); params.append(nombre_new)

        if "stock_on_hand" in d:
            if not can_edit_stock:
                raise PermissionDenied("No autorizado para editar stock")
            stock_new = _parse_int_decimal_field(d.get("stock_on_hand"), "stock_on_hand")
            sets.append("stock_on_hand=%s"); params.append(stock_new)

        if "stock_min" in d:
            if not is_manager:
                raise PermissionDenied("No autorizado para editar stock_min")
            stock_min = _parse_int_decimal_field(d.get("stock_min"), "stock_min")
            if stock_min < 0:
                raise ValidationError("stock_min no puede ser negativo")
            stock_min_new = stock_min
            sets.append("stock_min=%s"); params.append(stock_min_new)

        if "multiplicador" in d:
            if not is_manager:
                raise PermissionDenied("No autorizado para editar multiplicador")
            raw = d.get("multiplicador")
            mult = _parse_decimal_field(raw, "multiplicador", allow_none=True)
            if mult is not None and mult <= 0:
                raise ValidationError("multiplicador inválido")
            if mult is not None:
                mult = mult.quantize(FOUR_DEC)
            sets.append("multiplicador=%s"); params.append(mult)

        if False and "costo_usd" in d:
            if not can_edit_costs:
                raise PermissionDenied("No autorizado para editar costo")
            costo = _parse_decimal_field(d.get("costo_usd"), "costo_usd", allow_none=True)
            if costo is not None:
                if costo < 0:
                    raise ValidationError("costo_usd inválido")
                costo = money(costo)
            sets.append("costo_usd=%s"); params.append(costo)

        detail_text_fields = [
            "tipo_articulo",
            "categoria",
            "unidad_medida",
            "marca_fabricante",
            "nro_parte",
            "ubicacion_deposito",
            "estado",
            "notas",
        ]
        for field in detail_text_fields:
            if field in d:
                if not can_edit_stock:
                    raise PermissionDenied("No autorizado para editar detalles")
                sets.append(f"{field}=%s"); params.append(_clean_text(d.get(field)))

        detail_date_fields = [
            "fecha_ultima_compra",
            "fecha_ultimo_conteo",
            "fecha_vencimiento",
        ]
        for field in detail_date_fields:
            if field in d:
                if not can_edit_stock:
                    raise PermissionDenied("No autorizado para editar detalles")
                sets.append(f"{field}=%s"); params.append(_parse_date_field(d.get(field), field))

        update_proveedores = "proveedores" in d
        if update_proveedores and not is_manager:
            raise PermissionDenied("No autorizado para editar proveedores")
        update_costs = any(key in d for key in ("costo_valor", "costo_moneda", "costo_usd"))

        if not sets and not update_proveedores and not update_costs:
            raise ValidationError("Sin cambios")

        updated_row = None
        with transaction.atomic():
            row = q(
                """
                SELECT id, codigo, nombre, stock_on_hand, stock_min, ubicacion_deposito,
                       costo_neto, costo_usd, costo_moneda
                FROM catalogo_repuestos
                WHERE id=%s
                FOR UPDATE
                """,
                [repuesto_id],
                one=True,
            )
            if not row:
                raise ValidationError("Repuesto no encontrado")
            stock_prev = _as_int_decimal(row.get("stock_on_hand") or 0)
            stock_min_prev = _as_int_decimal(row.get("stock_min") or 0)
            if stock_new is None:
                stock_new = stock_prev
            if stock_min_new is None:
                stock_min_new = stock_min_prev

            if update_costs:
                costo_payload = _parse_repuesto_cost_input(d, request.user, current=row)
                sets.append("costo_usd=%s"); params.append(costo_payload.get("costo_usd"))
                sets.append("costo_neto=%s"); params.append(costo_payload.get("costo_ars") or Decimal("0.00"))
                sets.append("costo_moneda=%s"); params.append(costo_payload.get("costo_moneda") or "USD")

            if sets:
                sets.append("updated_at=NOW()")
                params += [repuesto_id]
                exec_void(
                    f"""
                    UPDATE catalogo_repuestos
                       SET {', '.join(sets)}
                     WHERE id=%s
                    """,
                    params,
                )
            elif update_proveedores:
                exec_void(
                    "UPDATE catalogo_repuestos SET updated_at=NOW() WHERE id=%s",
                    [repuesto_id],
                )

            nombre_prev = row.get("nombre") or ""
            if nombre_new is not None and nombre_new != nombre_prev:
                _log_repuesto_cambio(
                    repuesto_id,
                    row.get("codigo"),
                    "renombre",
                    nombre_prev,
                    nombre_new,
                    getattr(request.user, "id", None),
                    nota,
                )

            if stock_new is not None and stock_new != stock_prev:
                delta = _as_int_decimal(stock_new - stock_prev)
                exec_void(
                    """
                    INSERT INTO repuestos_movimientos
                      (repuesto_id, tipo, qty, stock_prev, stock_new, nota, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    [
                        repuesto_id,
                        "ajuste",
                        delta,
                        stock_prev,
                        stock_new,
                        nota,
                        getattr(request.user, "id", None),
                    ],
                )

            if update_proveedores:
                _sync_repuesto_proveedores(repuesto_id, d.get("proveedores") or [])

            if (
                stock_new is not None
                and stock_new < stock_prev
                and stock_prev > stock_min_prev
                and stock_new <= stock_min_new
            ):
                ubicacion = row.get("ubicacion_deposito")
                if "ubicacion_deposito" in d:
                    ubicacion = _clean_text(d.get("ubicacion_deposito"))
                item = {
                    "codigo": row.get("codigo"),
                    "nombre": row.get("nombre"),
                    "stock_on_hand": stock_new,
                    "stock_min": stock_min_new,
                    "ubicacion_deposito": ubicacion,
                }
                transaction.on_commit(lambda item=item: _send_stock_min_alerts([item]))

        updated_row = _load_repuesto_detail(repuesto_id, request.user)
        if not updated_row:
            return Response({"ok": True})
        return Response(updated_row)

    def delete(self, request, repuesto_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "tecnico"])
        if not _can_edit_stock(request.user):
            raise PermissionDenied("No autorizado")
        with transaction.atomic():
            row = q(
                """
                SELECT id, codigo, nombre, activo
                FROM catalogo_repuestos
                WHERE id=%s
                FOR UPDATE
                """,
                [repuesto_id],
                one=True,
            )
            if not row:
                raise ValidationError("Repuesto no encontrado")
            _log_repuesto_cambio(
                repuesto_id,
                row.get("codigo"),
                "baja",
                row.get("nombre"),
                None,
                getattr(request.user, "id", None),
                None,
            )
            exec_void(
                "DELETE FROM catalogo_repuestos WHERE id=%s",
                [repuesto_id],
            )
        return Response({"ok": True})


class RepuestosConfigView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        row = q(
            """
            SELECT rc.id, rc.dolar_ars, rc.multiplicador_general, rc.updated_at,
                   rc.updated_by, u.nombre AS updated_by_nombre
            FROM repuestos_config rc
            LEFT JOIN users u ON u.id = rc.updated_by
            ORDER BY rc.id
            LIMIT 1
            """,
            [],
            one=True,
        )
        if not row:
            exec_void("INSERT INTO repuestos_config (dolar_ars, multiplicador_general) VALUES (0, 1)")
            row = q(
                """
                SELECT rc.id, rc.dolar_ars, rc.multiplicador_general, rc.updated_at,
                       rc.updated_by, u.nombre AS updated_by_nombre
                FROM repuestos_config rc
                LEFT JOIN users u ON u.id = rc.updated_by
                ORDER BY rc.id
                LIMIT 1
                """,
                [],
                one=True,
            )

        history = q(
            """
            SELECT h.id, h.dolar_ars, h.multiplicador_general, h.changed_at,
                   h.changed_by, u.nombre AS changed_by_nombre
            FROM repuestos_config_history h
            LEFT JOIN users u ON u.id = h.changed_by
            ORDER BY h.changed_at DESC, h.id DESC
            LIMIT 50
            """,
            [],
        ) or []
        return Response({"config": row, "history": history})

    def patch(self, request):
        require_roles(request, ["jefe", "jefe_veedor"])
        d = request.data or {}

        row = q(
            "SELECT id, dolar_ars, multiplicador_general FROM repuestos_config ORDER BY id LIMIT 1",
            [],
            one=True,
        )
        if not row:
            exec_void("INSERT INTO repuestos_config (dolar_ars, multiplicador_general) VALUES (0, 1)")
            row = q(
                "SELECT id, dolar_ars, multiplicador_general FROM repuestos_config ORDER BY id LIMIT 1",
                [],
                one=True,
            )

        dolar_raw = d.get("dolar_ars")
        mult_raw = d.get("multiplicador_general")
        dolar = _parse_decimal_field(dolar_raw, "dolar_ars", allow_none=True)
        mult = _parse_decimal_field(mult_raw, "multiplicador_general", allow_none=True)
        if dolar is None:
            dolar = row.get("dolar_ars")
        if mult is None:
            mult = row.get("multiplicador_general")
        if dolar is None or mult is None:
            raise ValidationError("Valores invalidos")
        if dolar <= 0:
            raise ValidationError("dolar_ars inválido")
        if mult <= 0:
            raise ValidationError("multiplicador_general inválido")
        dolar = Decimal(dolar).quantize(FOUR_DEC)
        mult = Decimal(mult).quantize(FOUR_DEC)

        exec_void(
            """
            UPDATE repuestos_config
               SET dolar_ars=%s,
                   multiplicador_general=%s,
                   updated_at=now(),
                   updated_by=%s
             WHERE id=%s
            """,
            [dolar, mult, getattr(request.user, "id", None), row["id"]],
        )
        exec_void(
            """
            INSERT INTO repuestos_config_history (dolar_ars, multiplicador_general, changed_by)
            VALUES (%s,%s,%s)
            """,
            [dolar, mult, getattr(request.user, "id", None)],
        )
        return self.get(request)


class RepuestosStockPermisosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "jefe_veedor", "tecnico"])
        rol = _rol(request.user)
        uid = getattr(request.user, "id", None)
        where = ["rsp.revoked_at IS NULL", "rsp.expires_at > NOW()"]
        params = []
        if rol == "tecnico":
            if not uid:
                return Response([])
            where.append("rsp.tecnico_id=%s")
            params.append(uid)

        where_sql = "WHERE " + " AND ".join(where)
        rows = q(
            f"""
            SELECT
              rsp.id, rsp.tecnico_id, COALESCE(t.nombre,'') AS tecnico_nombre,
              rsp.enabled_by, COALESCE(ub.nombre,'') AS enabled_by_nombre,
              rsp.created_at, rsp.expires_at, rsp.revoked_at,
              rsp.revoked_by, COALESCE(ur.nombre,'') AS revoked_by_nombre,
              rsp.nota
            FROM repuestos_stock_permisos rsp
            LEFT JOIN users t ON t.id = rsp.tecnico_id
            LEFT JOIN users ub ON ub.id = rsp.enabled_by
            LEFT JOIN users ur ON ur.id = rsp.revoked_by
            {where_sql}
            ORDER BY rsp.expires_at DESC, rsp.id DESC
            """,
            params,
        ) or []
        return Response(rows)

    def post(self, request):
        require_roles(request, ["jefe", "jefe_veedor"])
        d = request.data or {}
        tecnico_id = _parse_int_field(d.get("tecnico_id"), "tecnico_id")
        if not tecnico_id:
            raise ValidationError("tecnico_id requerido")
        trow = q(
            "SELECT id, nombre, rol, activo FROM users WHERE id=%s",
            [tecnico_id],
            one=True,
        )
        if not trow or not trow.get("activo"):
            raise ValidationError("técnico no encontrado")
        if (trow.get("rol") or "").strip().lower() != "tecnico":
            raise ValidationError("usuario no es técnico")

        expires_at = timezone.now() + dt.timedelta(hours=24)
        exec_void(
            """
            UPDATE repuestos_stock_permisos
               SET revoked_at=NOW(), revoked_by=%s
             WHERE tecnico_id=%s
               AND revoked_at IS NULL
               AND expires_at > NOW()
            """,
            [getattr(request.user, "id", None), tecnico_id],
        )
        pid = exec_returning(
            """
            INSERT INTO repuestos_stock_permisos
              (tecnico_id, enabled_by, expires_at, nota)
            VALUES (%s,%s,%s,%s)
            RETURNING id
            """,
            [
                tecnico_id,
                getattr(request.user, "id", None),
                expires_at,
                _clean_text(d.get("nota")),
            ],
        )
        row = q(
            """
            SELECT
              rsp.id, rsp.tecnico_id, COALESCE(t.nombre,'') AS tecnico_nombre,
              rsp.enabled_by, COALESCE(ub.nombre,'') AS enabled_by_nombre,
              rsp.created_at, rsp.expires_at, rsp.revoked_at,
              rsp.revoked_by, COALESCE(ur.nombre,'') AS revoked_by_nombre,
              rsp.nota
            FROM repuestos_stock_permisos rsp
            LEFT JOIN users t ON t.id = rsp.tecnico_id
            LEFT JOIN users ub ON ub.id = rsp.enabled_by
            LEFT JOIN users ur ON ur.id = rsp.revoked_by
            WHERE rsp.id=%s
            """,
            [pid],
            one=True,
        )
        return Response(row or {"id": pid}, status=201)


class RepuestosStockPermisoDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, perm_id: int):
        require_roles(request, ["jefe", "jefe_veedor"])
        d = request.data or {}
        revoked = d.get("revoked")
        if not revoked:
            raise ValidationError("revoked requerido")
        exec_void(
            """
            UPDATE repuestos_stock_permisos
               SET revoked_at=NOW(), revoked_by=%s
             WHERE id=%s
            """,
            [getattr(request.user, "id", None), perm_id],
        )
        return Response({"ok": True})


class RepuestosCompraMovimientoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_roles(request, ["jefe", "jefe_veedor", "tecnico"])
        if not _can_edit_stock(request.user):
            raise PermissionDenied("No autorizado para editar stock")

        d = request.data or {}
        repuesto_id = _parse_int_field(d.get("repuesto_id"), "repuesto_id")
        if not repuesto_id:
            raise ValidationError("repuesto_id requerido")
        if repuesto_id <= 0:
            raise ValidationError("repuesto_id inválido")

        cantidad = _parse_int_decimal_field(d.get("cantidad"), "cantidad")
        if cantidad <= 0:
            raise ValidationError("cantidad debe ser mayor a 0")

        fecha_compra = _parse_date_field(d.get("fecha_compra"), "fecha_compra")
        if not fecha_compra:
            raise ValidationError("fecha_compra requerido")

        proveedor_raw = d.get("proveedor_id")
        proveedor_id = _parse_int_field(proveedor_raw, "proveedor_id")
        if proveedor_raw not in (None, "") and (not proveedor_id or proveedor_id <= 0):
            raise ValidationError("proveedor_id inválido")
        proveedor_nombre = _clean_text(d.get("proveedor_nombre"))
        if proveedor_id and proveedor_nombre:
            raise ValidationError("proveedor_id y proveedor_nombre son excluyentes")

        nota = _clean_text(d.get("nota"))
        ref_tipo = None
        ref_id = None
        proveedor_row = None

        with transaction.atomic():
            repuesto_row = q(
                """
                SELECT id, stock_on_hand, fecha_ultima_compra
                FROM catalogo_repuestos
                WHERE id=%s
                FOR UPDATE
                """,
                [repuesto_id],
                one=True,
            )
            if not repuesto_row:
                raise ValidationError("Repuesto no encontrado")

            if proveedor_id:
                proveedor_row = q(
                    "SELECT id, nombre FROM proveedores_externos WHERE id=%s",
                    [proveedor_id],
                    one=True,
                )
                if not proveedor_row:
                    raise ValidationError("proveedor_id inválido")
            elif proveedor_nombre:
                proveedor_id = _get_or_create_proveedor(proveedor_nombre)
                proveedor_row = q(
                    "SELECT id, nombre FROM proveedores_externos WHERE id=%s",
                    [proveedor_id],
                    one=True,
                )
                if not proveedor_row:
                    raise ValidationError("No se pudo resolver proveedor")

            stock_prev = _as_int_decimal(repuesto_row.get("stock_on_hand") or 0)
            stock_new = _as_int_decimal(stock_prev + cantidad)
            fecha_ultima_compra_new = _max_date(
                repuesto_row.get("fecha_ultima_compra"),
                fecha_compra,
            )

            exec_void(
                """
                UPDATE catalogo_repuestos
                   SET stock_on_hand=%s,
                       fecha_ultima_compra=%s,
                       updated_at=NOW()
                 WHERE id=%s
                """,
                [stock_new, fecha_ultima_compra_new, repuesto_id],
            )

            if proveedor_row and proveedor_row.get("id"):
                ref_tipo = "proveedor_externo"
                ref_id = int(proveedor_row.get("id"))
                rel = q(
                    """
                    SELECT id, ultima_compra
                    FROM repuestos_proveedores
                    WHERE repuesto_id=%s AND proveedor_id=%s
                    """,
                    [repuesto_id, ref_id],
                    one=True,
                )
                if rel and rel.get("id"):
                    ultima_compra_new = _max_date(rel.get("ultima_compra"), fecha_compra)
                    exec_void(
                        """
                        UPDATE repuestos_proveedores
                           SET ultima_compra=%s,
                               updated_at=NOW()
                         WHERE id=%s
                        """,
                        [ultima_compra_new, rel.get("id")],
                    )
                else:
                    exec_void(
                        """
                        INSERT INTO repuestos_proveedores
                          (repuesto_id, proveedor_id, ultima_compra, created_at, updated_at)
                        VALUES (%s,%s,%s,NOW(),NOW())
                        """,
                        [repuesto_id, ref_id, fecha_compra],
                    )

            mov_id = exec_returning(
                """
                INSERT INTO repuestos_movimientos
                  (repuesto_id, tipo, qty, stock_prev, stock_new, ref_tipo, ref_id, nota, fecha_compra, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    repuesto_id,
                    "ingreso_compra",
                    cantidad,
                    stock_prev,
                    stock_new,
                    ref_tipo,
                    ref_id,
                    nota,
                    fecha_compra,
                    getattr(request.user, "id", None),
                ],
            )

        repuesto = _load_repuesto_detail(repuesto_id, request.user)
        movimiento = q(
            """
            SELECT
              m.id, m.repuesto_id, cr.codigo, cr.nombre,
              m.tipo, m.qty, m.stock_prev, m.stock_new, m.ref_tipo, m.ref_id,
              m.nota, m.fecha_compra, m.created_at, m.created_by,
              u.nombre AS created_by_nombre,
              pe.nombre AS proveedor_nombre
            FROM repuestos_movimientos m
            JOIN catalogo_repuestos cr ON cr.id = m.repuesto_id
            LEFT JOIN users u ON u.id = m.created_by
            LEFT JOIN proveedores_externos pe
              ON m.ref_tipo = 'proveedor_externo'
             AND pe.id = m.ref_id
            WHERE m.id=%s
            """,
            [mov_id],
            one=True,
        )
        return Response({"ok": True, "repuesto": repuesto, "movimiento": movimiento}, status=201)


class RepuestosMovimientosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        rep_id_raw = (request.GET.get("repuesto_id") or "").strip()
        limit_raw = (request.GET.get("limit") or "").strip()
        try:
            limit = int(limit_raw or 100)
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))

        where = []
        params = []
        if rep_id_raw:
            try:
                rep_id = int(rep_id_raw)
            except ValueError:
                raise ValidationError("repuesto_id inválido")
            where.append("m.repuesto_id=%s")
            params.append(rep_id)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = q(
            f"""
            SELECT
              m.id, m.repuesto_id, cr.codigo, cr.nombre,
              m.tipo, m.qty, m.stock_prev, m.stock_new, m.ref_tipo, m.ref_id,
              m.nota, m.fecha_compra, m.created_at, m.created_by,
              u.nombre AS created_by_nombre,
              pe.nombre AS proveedor_nombre
            FROM repuestos_movimientos m
            JOIN catalogo_repuestos cr ON cr.id = m.repuesto_id
            LEFT JOIN users u ON u.id = m.created_by
            LEFT JOIN proveedores_externos pe
              ON m.ref_tipo = 'proveedor_externo'
             AND pe.id = m.ref_id
            {where_sql}
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT %s
            """,
            [*params, limit],
        ) or []
        return Response(rows)


class RepuestosCambiosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor"])
        qtxt = (request.GET.get("q") or "").strip()
        limit_raw = (request.GET.get("limit") or "").strip()
        offset_raw = (request.GET.get("offset") or "").strip()
        try:
            limit = int(limit_raw or 200)
        except ValueError:
            limit = 200
        try:
            offset = int(offset_raw or 0)
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 500))
        offset = max(0, offset)

        where = []
        params = []
        if qtxt:
            like = f"%{qtxt}%"
            where.append("(codigo ILIKE %s OR nombre_prev ILIKE %s OR nombre_new ILIKE %s)")
            params.extend([like, like, like])

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = q(
            f"""
            SELECT
              rc.id, rc.repuesto_id, rc.codigo, rc.accion,
              rc.nombre_prev, rc.nombre_new, rc.nota,
              rc.created_at, rc.created_by,
              u.nombre AS created_by_nombre
            FROM repuestos_cambios rc
            LEFT JOIN users u ON u.id = rc.created_by
            {where_sql}
            ORDER BY rc.created_at DESC, rc.id DESC
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ) or []
        return Response(rows)
