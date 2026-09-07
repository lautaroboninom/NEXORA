from difflib import SequenceMatcher

from django.db import IntegrityError, connection, transaction
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_sdk import BejermanSDKClient, as_string, first_value, normalize_search, records_from_response
from ..customer_bejerman_sync import (
    BEJERMAN_COMPANY_CONTEXT_KEY,
    BEJERMAN_CUSTOMER_COLUMNS,
    bejerman_customer_detail_payload_from_record,
    bejerman_customer_details_from_record,
    customer_bejerman_detail_payload,
    load_bejerman_customer_records_for_companies,
    sync_customers_from_bejerman_records,
)
from ..permissions import require_any_permission
from .helpers import exec_void, q, require_roles, _set_audit_user


CUSTOMER_ADMIN_ROLES = ["jefe", "admin", "supervisor", "ventas", "jefe_veedor", "jefe_suplente", "recepcion"]
CUSTOMER_MANAGE_ROLES = ["jefe", "admin", "supervisor", "ventas", "jefe_veedor", "jefe_suplente"]


def _clean(value):
    return str(value or "").strip()


def _clean_upper(value):
    return _clean(value).upper()


def _digits_only(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _bool_param(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_param(value, default=10, *, minimum=1, maximum=50):
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _table_exists(table_name):
    try:
        return table_name in connection.introspection.table_names()
    except Exception:
        return False


def _table_columns(table_name):
    try:
        with connection.cursor() as cur:
            return {column.name for column in connection.introspection.get_table_description(cur, table_name)}
    except Exception:
        return set()


def _customer_column_expr(columns, name, alias=None):
    target = alias or name
    if name in columns:
        return f"c.{name} AS {target}"
    return f"NULL AS {target}"


def _customer_base_columns(columns):
    return f"""
                   c.id,
                   c.razon_social,
                   {_customer_column_expr(columns, "cod_empresa")},
                   {_customer_column_expr(columns, "alias_interno")},
                   {_customer_column_expr(columns, "cuit")},
                   {_customer_column_expr(columns, "contacto")},
                   {_customer_column_expr(columns, "telefono")},
                   {_customer_column_expr(columns, "telefono_2")},
                   {_customer_column_expr(columns, "email")},
                   {", ".join(_customer_column_expr(columns, column) for column in BEJERMAN_CUSTOMER_COLUMNS)}
    """


def _attach_persisted_bejerman_details(rows):
    for row in rows:
        row["bejerman_details"] = customer_bejerman_detail_payload(row)
    return rows


def _format_datetime(value):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _route_address_from_bejerman(details):
    parts = [
        details.get("domicilio"),
        details.get("localidad"),
        details.get("provincia"),
        details.get("codigoPostal"),
    ]
    return ", ".join(_clean(part) for part in parts if _clean(part))


def _attach_route_locations(rows):
    for row in rows:
        details = row.get("bejerman_details") or {}
        address = _route_address_from_bejerman(details)
        items = []
        if address:
            items.append(
                {
                    "id": None,
                    "sourceType": "bejerman",
                    "label": "Domicilio Bejerman",
                    "name": row.get("razon_social") or "",
                    "address": address,
                    "notes": "",
                    "readOnly": True,
                }
            )
        row["routeLocations"] = items

    if not rows or not _table_exists("route_locations"):
        return rows
    columns = _table_columns("route_locations")
    if "customer_id" not in columns:
        return rows

    by_id = {int(row["id"]): row for row in rows if row.get("id") is not None}
    customer_ids = list(by_id)
    if not customer_ids:
        return rows
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, customer_id, name, address, notes, active, last_used_at, usage_count
              FROM route_locations
             WHERE customer_id = ANY(%s)
               AND active = TRUE
             ORDER BY usage_count DESC, last_used_at DESC NULLS LAST, name ASC, address ASC
            """,
            [customer_ids],
        )
        route_rows = [
            dict(zip([col[0] for col in cur.description], row)) for row in cur.fetchall()
        ]
    for item in route_rows:
        customer = by_id.get(int(item.get("customer_id") or 0))
        if not customer:
            continue
        customer.setdefault("routeLocations", []).append(
            {
                "id": item.get("id"),
                "sourceType": "route_location",
                "label": "Dirección de Hoja de ruta",
                "name": item.get("name") or customer.get("razon_social") or "",
                "address": item.get("address") or "",
                "notes": item.get("notes") or "",
                "active": item.get("active") if item.get("active") is not None else True,
                "lastUsedAt": _format_datetime(item.get("last_used_at")),
                "usageCount": item.get("usage_count") or 0,
                "readOnly": False,
            }
        )
    return rows


def _customer_rows(include_stats=False):
    columns = _table_columns("customers")
    base_columns = _customer_base_columns(columns)
    if include_stats and _table_exists("devices") and _table_exists("ingresos"):
        return _attach_persisted_bejerman_details(q(
            f"""
            SELECT {base_columns},
                   COALESCE(d.cnt, 0) AS equipos_count,
                   COALESCE(i.cnt, 0) AS ingresos_count
              FROM customers c
              LEFT JOIN (
                    SELECT customer_id, COUNT(*) AS cnt
                      FROM devices
                     GROUP BY customer_id
              ) d ON d.customer_id = c.id
              LEFT JOIN (
                    SELECT d.customer_id, COUNT(*) AS cnt
                      FROM ingresos i
                      JOIN devices d ON d.id = i.device_id
                     GROUP BY d.customer_id
              ) i ON i.customer_id = c.id
             ORDER BY c.razon_social
            """
        ))
    return _attach_persisted_bejerman_details(q(
        f"""
        SELECT {base_columns},
               0 AS equipos_count,
               0 AS ingresos_count
          FROM customers c
         ORDER BY c.razon_social
        """
    ))


def _customer_row_by_id(customer_id):
    columns = _table_columns("customers")
    base_columns = _customer_base_columns(columns)
    row = q(
        f"""
        SELECT {base_columns}
          FROM customers c
         WHERE c.id = %s
        """,
        [customer_id],
        one=True,
    )
    if row:
        row["bejerman_details"] = customer_bejerman_detail_payload(row)
    return row


def _bejerman_customer_code(record):
    return as_string(first_value(record, ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente")))


def _bejerman_customer_name(record):
    return as_string(first_value(record, ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Nombre", "Cliente")))


def _bejerman_customer_cuit(record):
    return _digits_only(first_value(record, ("Cliente_NroDocumento", "Cliente_CUIT", "Cliente_Cuit", "CUIT", "Cuit", "TaxId")))


# El puntaje sólo se usa contra dos umbrales: 0.82 ("Razón social similar") y 0.7
# ("Coincide con la búsqueda"). Por debajo de eso el valor exacto nunca se ve, así
# que alcanza con devolver 0.0 y saltear el cálculo caro. El margen contra 0.7 deja
# afuera cualquier diferencia que el redondeo a 3 decimales pudiera hacer visible.
_SCORE_PRUNE_FLOOR = 0.695


def _name_score_norm(left_norm, right_norm, floor=0.0):
    """Similitud entre dos nombres ya normalizados.

    `floor` es el mínimo que le importa al llamador: por debajo de ese valor se
    devuelve 0.0 sin calcular el ratio real. `ratio()` es O(n*m) y es lo que domina
    el cruce de ~500 clientes locales contra ~500 registros de Bejerman; las cotas
    superiores de `SequenceMatcher` (`real_quick_ratio` y `quick_ratio`) son O(n) y
    descartan la enorme mayoría de los pares sin cambiar el resultado.
    """
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    # Un nombre contenido en el otro se promueve a 0.86 aunque su ratio sea bajo
    # (ej. "AMTA" dentro de "AMTA SOCIEDAD ANONIMA"), así que ese caso no se poda.
    contained = left_norm in right_norm or right_norm in left_norm
    if not contained and floor:
        # Misma cota que `real_quick_ratio()`, pero calculada antes de construir el
        # SequenceMatcher: el constructor indexa el segundo texto completo y la
        # mayoría de los pares se descarta acá sin llegar a necesitarlo.
        left_len, right_len = len(left_norm), len(right_norm)
        if 2.0 * min(left_len, right_len) / (left_len + right_len) < floor:
            return 0.0
    matcher = SequenceMatcher(None, left_norm, right_norm)
    if not contained and floor and matcher.quick_ratio() < floor:
        return 0.0
    ratio = matcher.ratio()
    if contained:
        ratio = max(ratio, 0.86)
    return ratio


def _name_score(left, right):
    return _name_score_norm(normalize_search(left), normalize_search(right))


class _RemoteCustomerEntry:
    """Datos derivados de un registro remoto de Bejerman, calculados una sola vez.

    Antes cada comparación reconstruía código, razón social, CUIT y sus normalizados
    a partir del registro crudo; con ~500 clientes locales por ~500 registros remotos
    eso es un cuarto de millón de veces el mismo cálculo por carga de página.
    """

    __slots__ = ("record", "code", "code_upper", "name", "name_norm", "cuit", "flat_norm")

    def __init__(self, record):
        details = bejerman_customer_details_from_record(record)
        self.record = record
        self.code = details.get("cod_empresa") or _bejerman_customer_code(record)
        self.name = details.get("razon_social") or _bejerman_customer_name(record)
        self.cuit = details.get("cuit") or _bejerman_customer_cuit(record)
        self.code_upper = _clean_upper(self.code)
        self.name_norm = normalize_search(self.name)
        self.flat_norm = normalize_search(f"{self.code} {self.name} {self.cuit}")


def _remote_customer_index(records):
    return [_RemoteCustomerEntry(record) for record in records]


def _local_match_keys(local, query=""):
    local = local or {}
    local_name = _clean(local.get("razon_social"))
    local_name_norm = normalize_search(local_name)
    return {
        "code": _clean_upper(local.get("cod_empresa")),
        "cuit": _digits_only(local.get("cuit")),
        "name_norm": local_name_norm,
        # `_name_score` compara contra la razón social local y, si no hay, contra
        # el texto buscado.
        "left_norm": local_name_norm or normalize_search(query),
        "query_norm": normalize_search(query),
    }


def _entry_score(entry, keys, floor=0.0):
    """Puntaje y motivos de un registro remoto, sin construir el payload completo."""
    score = _name_score_norm(keys["left_norm"], entry.name_norm, floor=floor)
    reasons = []

    if entry.code and keys["code"] and entry.code_upper == keys["code"]:
        reasons.append("Código exacto")
        score = max(score, 1.0)
    if entry.cuit and keys["cuit"] and entry.cuit == keys["cuit"]:
        reasons.append("CUIT exacto")
        score = max(score, 0.98)
    if keys["name_norm"] and keys["name_norm"] == entry.name_norm:
        reasons.append("Razón social exacta")
        score = max(score, 0.96)
    elif score >= 0.82:
        reasons.append("Razón social similar")

    if keys["query_norm"] and not reasons and keys["query_norm"] in entry.flat_norm:
        reasons.append("Coincide con la búsqueda")
        score = max(score, 0.7)

    return score, reasons


def _candidate_from_entry(entry, score, reasons):
    return {
        "code": entry.code,
        "name": entry.name,
        "cuit": entry.cuit,
        "score": round(score, 3),
        "reasons": reasons,
        # El detalle es la parte cara del payload y sólo lo necesitan los candidatos
        # que efectivamente se devuelven, no todos los registros que se puntúan.
        "details": bejerman_customer_detail_payload_from_record(entry.record),
    }


def _candidate_payload(record, local=None, query=""):
    entry = _RemoteCustomerEntry(record)
    score, reasons = _entry_score(entry, _local_match_keys(local, query))
    return _candidate_from_entry(entry, score, reasons)


def _load_bejerman_records(user_id: int | None = None, force_refresh: bool = False):
    """Carga clientes de todas las empresas Bejerman configuradas (SEPID, MGBIO)."""
    return load_bejerman_customer_records_for_companies(user_id=user_id, force_refresh=force_refresh)


def _record_company_key(record):
    return _clean_upper((record or {}).get(BEJERMAN_COMPANY_CONTEXT_KEY))


def _candidate_score(candidate):
    if "Código exacto" in candidate.get("reasons", []):
        return 1.2
    if "CUIT exacto" in candidate.get("reasons", []):
        return 1.1
    return float(candidate.get("score") or 0)


def _customer_candidates(local, records, limit=5, query="", index=None):
    entries = _remote_customer_index(records) if index is None else index
    keys = _local_match_keys(local, query)
    scored = []
    for entry in entries:
        score, reasons = _entry_score(entry, keys, floor=_SCORE_PRUNE_FLOOR)
        if reasons or round(score, 3) >= 0.82:
            scored.append((score, reasons, entry))
    scored.sort(key=lambda item: _candidate_score({"reasons": item[1], "score": item[0]}), reverse=True)
    return [_candidate_from_entry(entry, score, reasons) for score, reasons, entry in scored[:limit]]


def _bejerman_sync_payload(local, records, by_code, index=None):
    local_code = _clean_upper(local.get("cod_empresa"))
    local_name = _clean(local.get("razon_social"))
    local_cuit = _digits_only(local.get("cuit"))

    if local_code:
        local_company = _clean_upper(local.get("bejerman_company_key"))
        record = by_code.get((local_company, local_code)) if local_company else by_code.get(local_code)
        if record:
            candidate = _candidate_payload(record, local=local)
            candidate_name = candidate.get("name") or ""
            candidate_cuit = candidate.get("cuit") or ""
            name_ok = not candidate_name or _name_score(local_name, candidate_name) >= 0.82
            cuit_ok = not local_cuit or not candidate_cuit or local_cuit == candidate_cuit
            if name_ok and cuit_ok:
                return {
                    "status": "synced",
                    "label": "Sincronizado",
                    "message": "El código existe en Bejerman y coincide con el cliente.",
                    "candidate": candidate,
                    "candidates": [candidate],
                }
            return {
                "status": "review",
                "label": "Revisar datos",
                "message": "El código existe en Bejerman, pero la razón social o el CUIT no coinciden.",
                "candidate": candidate,
                "candidates": [candidate],
            }

        candidates = _customer_candidates(local, records, limit=5, index=index)
        return {
            "status": "code_mismatch" if candidates else "not_found",
            "label": "Código no encontrado" if not candidates else "Código posible",
            "message": "El código local no aparece en Bejerman.",
            "candidates": candidates,
        }

    candidates = _customer_candidates(local, records, limit=5)
    return {
        "status": "missing_code" if candidates else "unlinked",
        "label": "Sin código" if candidates else "Sin vincular",
        "message": "El cliente no tiene código Bejerman cargado.",
        "candidates": candidates,
    }


def _attach_bejerman_sync(rows, user_id: int | None = None):
    try:
        records, company_errors = _load_bejerman_records(user_id=user_id)
    except Exception as exc:
        for row in rows:
            row["bejerman_sync"] = {
                "status": "unavailable",
                "label": "No disponible",
                "message": str(exc) or "No se pudo consultar Bejerman.",
                "candidates": [],
            }
        return rows

    failed_companies = {_clean_upper(item.get("company")) for item in company_errors}
    by_code = {}
    for record in records:
        code = _clean_upper(_bejerman_customer_code(record))
        if not code:
            continue
        by_code.setdefault((_record_company_key(record), code), record)
        by_code.setdefault(code, record)

    # El índice del catálogo remoto se arma una sola vez y lo comparten todas las
    # filas: el matching difuso (el de los clientes cuyo código no está en Bejerman)
    # es O(clientes locales x clientes Bejerman) y sin esto recalculaba los mismos
    # datos derivados de cada registro remoto una vez por cliente local.
    index = _remote_customer_index(records)
    for row in rows:
        row_company = _clean_upper(row.get("bejerman_company_key"))
        if row_company and row_company in failed_companies:
            row["bejerman_sync"] = {
                "status": "unavailable",
                "label": "No disponible",
                "message": f"No se pudo consultar la empresa {row_company} en Bejerman.",
                "candidates": [],
            }
            continue
        row["bejerman_sync"] = _bejerman_sync_payload(row, records, by_code, index=index)
    return rows


def _nullable_text(value):
    text = _clean(value)
    return text or None


def _validate_alias_unique(alias, customer_id=None):
    alias = _nullable_text(alias)
    if not alias or "alias_interno" not in _table_columns("customers"):
        return
    params = [alias]
    where = "LOWER(BTRIM(alias_interno)) = LOWER(BTRIM(%s))"
    if customer_id is not None:
        where += " AND id <> %s"
        params.append(customer_id)
    row = q(
        f"""
        SELECT id
          FROM customers
         WHERE alias_interno IS NOT NULL
           AND BTRIM(alias_interno) <> ''
           AND {where}
         LIMIT 1
        """,
        params,
        one=True,
    )
    if row:
        raise ValidationError({"alias_interno": "El alias interno ya existe en otro cliente."})


def _customer_write_payload(data):
    return {
        "razon_social": _clean(data.get("razon_social")),
        "cod_empresa": _nullable_text(data.get("cod_empresa")),
        "alias_interno": _nullable_text(data.get("alias_interno")),
        "cuit": _nullable_text(data.get("cuit")),
        "contacto": _nullable_text(data.get("contacto")),
        "telefono": _nullable_text(data.get("telefono")),
        "telefono_2": _nullable_text(data.get("telefono_2")),
        "email": _nullable_text(data.get("email")),
    }


class CustomersListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_any_permission(
            request,
            [
                "page.home_search",
                "action.ingreso.edit_basics",
                "action.ingreso.create",
                "action.ingreso.force_historical",
                "page.new_ingreso",
                "action.delivery_order.create",
            ],
        )
        columns = _table_columns("customers")
        return Response(
            q(
                f"""
                SELECT id,
                       razon_social,
                       cod_empresa,
                       {_customer_column_expr(columns, "alias_interno")},
                       cuit,
                       telefono,
                       email
                  FROM customers c
                 ORDER BY razon_social
                """
            )
        )


class ClientesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, CUSTOMER_ADMIN_ROLES)
        rows = _customer_rows(include_stats=_bool_param(request.GET.get("include_stats")))
        if _bool_param(request.GET.get("include_route_locations")):
            rows = _attach_route_locations(rows)
        if _bool_param(request.GET.get("include_bejerman")):
            user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            rows = _attach_bejerman_sync(rows, user_id=user_id)
        return Response(rows)

    def post(self, request):
        require_roles(request, CUSTOMER_MANAGE_ROLES)
        _set_audit_user(request)
        d = request.data or {}
        if not _clean(d.get("razon_social")):
            raise ValidationError("razon_social es requerido")
        payload = _customer_write_payload(d)
        _validate_alias_unique(payload.get("alias_interno"))
        columns = _table_columns("customers")
        writable = [field for field in payload if field == "razon_social" or field in columns]
        column_sql = ", ".join(writable)
        value_sql = ", ".join(f"%({field})s" for field in writable)
        try:
            exec_void(
                f"INSERT INTO customers({column_sql}) VALUES ({value_sql})",
                payload,
            )
        except IntegrityError:
            raise ValidationError({"alias_interno": "El alias interno ya existe en otro cliente."})
        return Response({"ok": True})


class ClienteBejermanCandidatesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, CUSTOMER_ADMIN_ROLES)
        query = _clean(request.GET.get("q") or request.GET.get("search"))
        limit = _int_param(request.GET.get("limit"), default=8, maximum=20)
        local = {}
        customer_id = _clean(request.GET.get("customer_id"))
        if customer_id.isdigit():
            local = _customer_row_by_id(int(customer_id)) or {}
        if not query and not local:
            return Response({"items": []})

        try:
            user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            records, _company_errors = _load_bejerman_records(user_id=user_id)
        except Exception as exc:
            return Response({"detail": str(exc) or "No se pudo consultar Bejerman."}, status=503)

        index = _remote_customer_index(records)
        items = _customer_candidates(local, records, limit=limit, query=query, index=index)
        if query:
            query_norm = normalize_search(query)
            query_digits = _digits_only(query)
            keys = _local_match_keys(local, query)
            extra = []
            seen_codes = {_clean_upper(item.get("code")) for item in items}
            for entry in index:
                if entry.code_upper in seen_codes:
                    continue
                if query_norm and query_norm in entry.flat_norm:
                    pass
                elif query_digits and query_digits in _digits_only(entry.cuit):
                    pass
                else:
                    continue
                score, reasons = _entry_score(entry, keys, floor=_SCORE_PRUNE_FLOOR)
                extra.append((score, reasons, entry))
            extra.sort(key=lambda item: _candidate_score({"reasons": item[1], "score": item[0]}), reverse=True)
            for score, reasons, entry in extra:
                if len(items) >= limit:
                    break
                items.append(_candidate_from_entry(entry, score, reasons))
        return Response({"items": items[:limit]})


class ClientesBejermanSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_roles(request, CUSTOMER_MANAGE_ROLES)
        _set_audit_user(request)
        dry_run = _bool_param((request.data or {}).get("dry_run"))
        try:
            user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            records, company_errors = _load_bejerman_records(user_id=user_id, force_refresh=True)
            summary = sync_customers_from_bejerman_records(records, dry_run=dry_run)
        except Exception as exc:
            return Response({"detail": str(exc) or "No se pudo sincronizar Bejerman."}, status=503)
        if company_errors:
            summary["company_errors"] = company_errors
        return Response(summary)


class ClienteDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, cid):
        require_roles(request, CUSTOMER_MANAGE_ROLES)
        _set_audit_user(request)
        d = request.data or {}
        payload = _customer_write_payload(d)
        if not payload["razon_social"]:
            raise ValidationError("razon_social es requerido")
        _validate_alias_unique(payload.get("alias_interno"), customer_id=cid)
        columns = _table_columns("customers")
        writable = [field for field in payload if field == "razon_social" or field in columns]
        set_sql = ", ".join(f"{field} = %({field})s" for field in writable)
        payload["id"] = cid
        try:
            exec_void(
                f"UPDATE customers SET {set_sql} WHERE id = %(id)s",
                payload,
            )
            return Response({"ok": True})
        except IntegrityError:
            raise ValidationError({"alias_interno": "El alias interno ya existe en otro cliente."})
        except Exception as e:
            raise ValidationError(str(e) or "No se pudo actualizar el cliente")

    def delete(self, request, cid):
        require_roles(request, CUSTOMER_MANAGE_ROLES)
        refs = q(
            """
            SELECT
              (SELECT COUNT(*) FROM devices d WHERE d.customer_id = %s) AS cnt_devices,
              (SELECT COUNT(*)
                 FROM ingresos t
                 JOIN devices d ON d.id = t.device_id
                WHERE d.customer_id = %s) AS cnt_ingresos
            """,
            [cid, cid], one=True
        ) or {"cnt_devices": 0, "cnt_ingresos": 0}
        if refs["cnt_devices"] or refs["cnt_ingresos"]:
            return Response(
                {"detail": f"No se puede eliminar: el cliente tiene {refs['cnt_devices']} equipos y {refs['cnt_ingresos']} ingresos asociados."},
                status=409
            )
        try:
            _set_audit_user(request)
            exec_void("DELETE FROM customers WHERE id = %(id)s", {"id": cid})
            return Response({"ok": True})
        except Exception:
            return Response({"detail": "No se pudo eliminar por restricciones de integridad."}, status=409)


class ClienteMergeView(APIView):
    """Mover referencias de un cliente duplicado a otro y eliminar el source."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        require_roles(request, CUSTOMER_MANAGE_ROLES)
        _set_audit_user(request)
        d = request.data or {}
        try:
            source_id = int(d.get("source_id"))
            target_id = int(d.get("target_id"))
        except Exception:
            return Response({"detail": "source_id y target_id requeridos"}, status=400)
        if source_id == target_id:
            return Response({"detail": "source y target no pueden ser iguales"}, status=400)

        columns = _table_columns("customers")
        alias_select = ", alias_interno" if "alias_interno" in columns else ""
        src = q(
            f"SELECT id, razon_social, cod_empresa, telefono, telefono_2, email{alias_select} FROM customers WHERE id=%s",
            [source_id],
            one=True,
        )
        dst = q(
            f"SELECT id, razon_social, cod_empresa, telefono, telefono_2, email{alias_select} FROM customers WHERE id=%s",
            [target_id],
            one=True,
        )
        if not src or not dst:
            return Response({"detail": "cliente source/target inexistente"}, status=404)

        # Completar campos faltantes del destino con los del source (sin tocar razon_social)
        def _merge_field(dst_val, src_val):
            dst_clean = (dst_val or "").strip()
            src_clean = (src_val or "").strip()
            return dst_clean or src_clean or None

        updated_target = {
            "cod_empresa": _merge_field(dst.get("cod_empresa"), src.get("cod_empresa")),
            "alias_interno": _merge_field(dst.get("alias_interno"), src.get("alias_interno")),
            "telefono": _merge_field(dst.get("telefono"), src.get("telefono")),
            "telefono_2": _merge_field(dst.get("telefono_2"), src.get("telefono_2")),
            "email": _merge_field(dst.get("email"), src.get("email")),
        }
        if "alias_interno" in columns:
            _validate_alias_unique(updated_target.get("alias_interno"), customer_id=target_id)

        moved_devices = q(
            "SELECT COUNT(*) AS cnt FROM devices WHERE customer_id=%s",
            [source_id],
            one=True,
        ) or {"cnt": 0}

        with transaction.atomic():
            alias_update = "alias_interno = %(alias)s," if "alias_interno" in columns else ""
            exec_void(
                f"""
                UPDATE customers
                   SET cod_empresa = %(cod)s,
                       {alias_update}
                       telefono    = %(tel)s,
                       telefono_2  = %(tel2)s,
                       email       = %(email)s
                 WHERE id = %(id)s
                """,
                {
                    "cod": updated_target["cod_empresa"],
                    "alias": updated_target["alias_interno"],
                    "tel": updated_target["telefono"],
                    "tel2": updated_target["telefono_2"],
                    "email": updated_target["email"],
                    "id": target_id,
                },
            )
            exec_void(
                "UPDATE devices SET customer_id=%(target)s WHERE customer_id=%(source)s",
                {"target": target_id, "source": source_id},
            )
            exec_void(
                "DELETE FROM customers WHERE id=%(id)s",
                {"id": source_id},
            )

        return Response(
            {
                "ok": True,
                "source_id": source_id,
                "target_id": target_id,
                "moved_devices": int(moved_devices.get("cnt") or 0),
            }
        )


__all__ = [
    'CustomersListView',
    'ClientesView',
    'ClienteBejermanCandidatesView',
    'ClientesBejermanSyncView',
    'ClienteDeleteView',
    'ClienteMergeView',
]
