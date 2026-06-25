from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import defaultdict, deque
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from service.customer_bejerman_sync import BEJERMAN_CUSTOMER_COLUMNS
from service.models import User


DEFAULT_REPORT_DIR = "outputs/customer_duplicates"
REPORT_PREFIX = "clientes_duplicados"
FUZZY_NAME_THRESHOLD = 0.93
COMPATIBLE_NAME_THRESHOLD = 0.90

BASE_MERGE_FIELDS = (
    "cod_empresa",
    "razon_social",
    "cuit",
    "contacto",
    "telefono",
    "telefono_2",
    "email",
)
LOCAL_FILL_FIELDS = ("alias_interno",)
REF_SPECS = (
    ("devices_customer", "devices", "customer_id"),
    ("devices_mg_venta_customer", "devices", "mg_venta_customer_id"),
    ("device_mg_events_venta_customer", "device_mg_events", "venta_customer_id"),
    ("delivery_orders_customer", "delivery_orders", "customer_id"),
    ("preventivo_planes_customer", "preventivo_planes", "customer_id"),
)
REPORT_FIELDS = (
    "approved",
    "confidence",
    "match_reasons",
    "block_reason",
    "target_id",
    "source_id",
    "target_razon_social",
    "source_razon_social",
    "target_cod_empresa",
    "source_cod_empresa",
    "target_bejerman_cod_empresa",
    "source_bejerman_cod_empresa",
    "target_bejerman_company_key",
    "source_bejerman_company_key",
    "target_cuit",
    "source_cuit",
    "target_alias_interno",
    "source_alias_interno",
    "target_bejerman_synced",
    "source_bejerman_synced",
    "target_bejerman_condicion_iva",
    "source_bejerman_condicion_iva",
    "target_bejerman_email",
    "source_bejerman_email",
    "proposed_razon_social",
    "proposed_cod_empresa",
    "proposed_bejerman_cod_empresa",
    "proposed_bejerman_company_key",
    "proposed_cuit",
    "proposed_alias_interno",
    "proposed_contacto",
    "proposed_telefono",
    "proposed_telefono_2",
    "proposed_email",
    "proposed_bejerman_condicion_iva",
    "proposed_bejerman_email",
    "target_devices_customer",
    "source_devices_customer",
    "total_devices_customer",
    "target_devices_mg_venta_customer",
    "source_devices_mg_venta_customer",
    "total_devices_mg_venta_customer",
    "target_device_mg_events_venta_customer",
    "source_device_mg_events_venta_customer",
    "total_device_mg_events_venta_customer",
    "target_delivery_orders_customer",
    "source_delivery_orders_customer",
    "total_delivery_orders_customer",
    "target_preventivo_planes_customer",
    "source_preventivo_planes_customer",
    "total_preventivo_planes_customer",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_upper(value: Any) -> str:
    return _clean(value).upper()


def _digits_only(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _strip_accents(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _name_key(value: Any) -> str:
    value = _strip_accents(value).lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def _legal_name_key(value: Any) -> str:
    value = _strip_accents(value).lower()
    value = re.sub(
        r"\b(s\.?a\.?s?|s\.?r\.?l\.?|sociedad anonima|sa|srl|sas|de|del|la|el|y)\b",
        " ",
        value,
    )
    return re.sub(r"[^a-z0-9]+", "", value)


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _compatible_names(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _ratio(_legal_name_key(left.get("razon_social")), _legal_name_key(right.get("razon_social"))) >= COMPATIBLE_NAME_THRESHOLD


def _table_columns(table_name: str) -> set[str]:
    try:
        with connection.cursor() as cur:
            return {column.name for column in connection.introspection.get_table_description(cur, table_name)}
    except Exception:
        return set()


def _table_has_column(table_name: str, column_name: str) -> bool:
    return column_name in _table_columns(table_name)


def _fetch_dicts(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        names = [column[0] for column in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def _select_expr(columns: set[str], name: str) -> str:
    if name in columns:
        return name
    return f"NULL AS {name}"


def _load_customers() -> list[dict[str, Any]]:
    columns = _table_columns("customers")
    if not {"id", "razon_social"}.issubset(columns):
        raise CommandError("Faltan columnas requeridas en customers.")
    select_columns = [
        "id",
        "razon_social",
        _select_expr(columns, "cod_empresa"),
        _select_expr(columns, "cuit"),
        _select_expr(columns, "contacto"),
        _select_expr(columns, "telefono"),
        _select_expr(columns, "telefono_2"),
        _select_expr(columns, "email"),
        _select_expr(columns, "alias_interno"),
        *(_select_expr(columns, column) for column in BEJERMAN_CUSTOMER_COLUMNS),
    ]
    rows = _fetch_dicts(f"SELECT {', '.join(select_columns)} FROM customers ORDER BY id ASC")
    ids = [int(row["id"]) for row in rows]
    counts = _reference_counts(ids)
    for row in rows:
        row["id"] = int(row["id"])
        row["ref_counts"] = counts.get(int(row["id"]), _empty_counts())
    return rows


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key, _, _ in REF_SPECS}


def _reference_counts(customer_ids: list[int]) -> dict[int, dict[str, int]]:
    result = {customer_id: _empty_counts() for customer_id in customer_ids}
    if not customer_ids:
        return result
    placeholders = ", ".join(["%s"] * len(customer_ids))
    for key, table, column in REF_SPECS:
        if not _table_has_column(table, column):
            continue
        rows = _fetch_dicts(
            f"""
            SELECT {column} AS customer_id, COUNT(*) AS cnt
              FROM {table}
             WHERE {column} IN ({placeholders})
             GROUP BY {column}
            """,
            customer_ids,
        )
        for row in rows:
            customer_id = row.get("customer_id")
            if customer_id is not None:
                result[int(customer_id)][key] = int(row.get("cnt") or 0)
    return result


def _non_empty_raw(value: Any) -> bool:
    if value in (None, "", {}, []):
        return False
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return bool(value.strip())
        return parsed not in ({}, [], None, "")
    return True


def _is_bejerman_row(row: dict[str, Any]) -> bool:
    if row.get("bejerman_synced_at"):
        return True
    if _non_empty_raw(row.get("bejerman_raw")):
        return True
    return any(_clean(row.get(column)) for column in BEJERMAN_CUSTOMER_COLUMNS if column != "bejerman_raw")


def _row_rank(row: dict[str, Any], target_id: int) -> tuple[int, int]:
    synced_rank = 0 if _is_bejerman_row(row) else 1
    target_rank = 0 if int(row["id"]) == target_id else 1
    return (synced_rank, target_rank)


def _first_text(rows: list[dict[str, Any]], column: str) -> Any:
    for row in rows:
        value = row.get(column)
        if _clean(value):
            return _clean(value)
    return None


def _first_value(rows: list[dict[str, Any]], column: str) -> Any:
    for row in rows:
        value = row.get(column)
        if column == "bejerman_raw":
            if _non_empty_raw(value):
                return value
        elif value not in (None, ""):
            return value
    return None


def _alias_conflict(target: dict[str, Any], source: dict[str, Any]) -> str:
    target_alias = _clean(target.get("alias_interno"))
    source_alias = _clean(source.get("alias_interno"))
    if target_alias and source_alias and target_alias.casefold() != source_alias.casefold():
        return "alias_conflict"
    return ""


def _merged_values(target: dict[str, Any], source: dict[str, Any], columns: set[str]) -> tuple[dict[str, Any], str]:
    target_id = int(target["id"])
    ordered = sorted([target, source], key=lambda row: _row_rank(row, target_id))
    values: dict[str, Any] = {}
    for column in BASE_MERGE_FIELDS:
        if column in columns:
            values[column] = _first_text(ordered, column)
    if "razon_social" in columns and not values.get("razon_social"):
        values["razon_social"] = _clean(target.get("razon_social")) or _clean(source.get("razon_social"))

    if "alias_interno" in columns:
        block_reason = _alias_conflict(target, source)
        values["alias_interno"] = _clean(target.get("alias_interno")) or _clean(source.get("alias_interno")) or None
    else:
        block_reason = ""

    for column in BEJERMAN_CUSTOMER_COLUMNS:
        if column not in columns:
            continue
        if column in {"bejerman_raw", "bejerman_synced_at"}:
            values[column] = _first_value(ordered, column)
        else:
            values[column] = _first_text(ordered, column)
    return values, block_reason


def _add_pair(pairs: dict[tuple[int, int], set[str]], left_id: int, right_id: int, reason: str) -> None:
    if left_id == right_id:
        return
    key = tuple(sorted((int(left_id), int(right_id))))
    pairs.setdefault(key, set()).add(reason)


def _bejerman_company_key(row: dict[str, Any]) -> str:
    return _clean_upper(row.get("bejerman_company_key"))


def _bejerman_code_key(row: dict[str, Any]) -> str:
    return _clean_upper(row.get("bejerman_cod_empresa")) or _clean_upper(row.get("cod_empresa"))


def _compatible_bejerman_company(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_company = _bejerman_company_key(left)
    right_company = _bejerman_company_key(right)
    return not (left_company and right_company and left_company != right_company)


def _index_pairs(rows: list[dict[str, Any]]) -> dict[tuple[int, int], set[str]]:
    pairs: dict[tuple[int, int], set[str]] = {}
    for reason, key_fn in (
        ("same_code", _bejerman_code_key),
        ("same_cuit", lambda row: _digits_only(row.get("cuit"))),
        ("same_name", lambda row: _name_key(row.get("razon_social"))),
    ):
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            key = key_fn(row)
            if key:
                buckets[key].append(row)
        for bucket_rows in buckets.values():
            if len(bucket_rows) < 2:
                continue
            bucket_rows = sorted(bucket_rows, key=lambda row: int(row["id"]))
            for index, left in enumerate(bucket_rows):
                for right in bucket_rows[index + 1 :]:
                    if reason in {"same_code", "same_cuit"} and not _compatible_bejerman_company(left, right):
                        continue
                    _add_pair(pairs, int(left["id"]), int(right["id"]), reason)

    for index, left in enumerate(rows):
        left_key = _legal_name_key(left.get("razon_social"))
        if len(left_key) < 5:
            continue
        for right in rows[index + 1 :]:
            right_key = _legal_name_key(right.get("razon_social"))
            if len(right_key) < 5 or left_key == right_key:
                continue
            similarity = _ratio(left_key, right_key)
            if similarity >= FUZZY_NAME_THRESHOLD and (_is_bejerman_row(left) or _is_bejerman_row(right)):
                _add_pair(pairs, int(left["id"]), int(right["id"]), f"fuzzy_name_{similarity:.3f}")
    return pairs


def _pair_confidence(left: dict[str, Any], right: dict[str, Any], reasons: set[str]) -> str:
    if "same_name" in reasons:
        return "safe"
    if any(reason.startswith("fuzzy_name_") for reason in reasons):
        return "safe"
    if "same_code" in reasons and "same_cuit" in reasons and _compatible_names(left, right):
        return "safe"
    return "review"


def _connected_components(ids: set[int], edges: dict[int, set[int]]) -> list[list[int]]:
    seen: set[int] = set()
    components: list[list[int]] = []
    for start in sorted(ids):
        if start in seen:
            continue
        queue = deque([start])
        seen.add(start)
        component: list[int] = []
        while queue:
            item = queue.popleft()
            component.append(item)
            for neighbor in sorted(edges.get(item, set())):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(sorted(component))
    return components


def _candidate_rows(customers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {int(row["id"]): row for row in customers}
    pairs = _index_pairs(customers)
    safe_pairs: dict[tuple[int, int], set[str]] = {}
    review_pairs: dict[tuple[int, int], set[str]] = {}
    for pair, reasons in pairs.items():
        left, right = by_id[pair[0]], by_id[pair[1]]
        if _pair_confidence(left, right, reasons) == "safe":
            safe_pairs[pair] = reasons
        else:
            review_pairs[pair] = reasons

    safe_edges: dict[int, set[int]] = defaultdict(set)
    safe_ids: set[int] = set()
    for left_id, right_id in safe_pairs:
        safe_edges[left_id].add(right_id)
        safe_edges[right_id].add(left_id)
        safe_ids.update({left_id, right_id})

    rows: list[dict[str, Any]] = []
    for component in _connected_components(safe_ids, safe_edges):
        target_id = min(component)
        for source_id in sorted(customer_id for customer_id in component if customer_id != target_id):
            reasons = set()
            pair_key = tuple(sorted((target_id, source_id)))
            reasons.update(safe_pairs.get(pair_key, set()))
            if not reasons:
                for other_id in component:
                    if other_id == source_id:
                        continue
                    reasons.update(safe_pairs.get(tuple(sorted((source_id, other_id))), set()))
            rows.append(_report_row("safe", reasons, by_id[target_id], by_id[source_id]))

    for pair, reasons in sorted(review_pairs.items()):
        if pair in safe_pairs:
            continue
        target_id, source_id = min(pair), max(pair)
        rows.append(_report_row("review", reasons, by_id[target_id], by_id[source_id]))

    return sorted(rows, key=lambda row: (int(row["target_id"]), int(row["source_id"])))


def _count(row: dict[str, Any], key: str) -> int:
    return int((row.get("ref_counts") or {}).get(key) or 0)


def _report_row(confidence: str, reasons: set[str], target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    columns = _table_columns("customers")
    proposed, block_reason = _merged_values(target, source, columns)
    result = {
        "approved": "",
        "confidence": confidence,
        "match_reasons": ",".join(sorted(reasons)),
        "block_reason": block_reason,
        "target_id": target["id"],
        "source_id": source["id"],
        "target_razon_social": target.get("razon_social") or "",
        "source_razon_social": source.get("razon_social") or "",
        "target_cod_empresa": target.get("cod_empresa") or "",
        "source_cod_empresa": source.get("cod_empresa") or "",
        "target_bejerman_cod_empresa": target.get("bejerman_cod_empresa") or "",
        "source_bejerman_cod_empresa": source.get("bejerman_cod_empresa") or "",
        "target_bejerman_company_key": target.get("bejerman_company_key") or "",
        "source_bejerman_company_key": source.get("bejerman_company_key") or "",
        "target_cuit": target.get("cuit") or "",
        "source_cuit": source.get("cuit") or "",
        "target_alias_interno": target.get("alias_interno") or "",
        "source_alias_interno": source.get("alias_interno") or "",
        "target_bejerman_synced": "SI" if target.get("bejerman_synced_at") else "",
        "source_bejerman_synced": "SI" if source.get("bejerman_synced_at") else "",
        "target_bejerman_condicion_iva": target.get("bejerman_condicion_iva") or "",
        "source_bejerman_condicion_iva": source.get("bejerman_condicion_iva") or "",
        "target_bejerman_email": target.get("bejerman_email") or "",
        "source_bejerman_email": source.get("bejerman_email") or "",
        "proposed_razon_social": proposed.get("razon_social") or "",
        "proposed_cod_empresa": proposed.get("cod_empresa") or "",
        "proposed_bejerman_cod_empresa": proposed.get("bejerman_cod_empresa") or "",
        "proposed_bejerman_company_key": proposed.get("bejerman_company_key") or "",
        "proposed_cuit": proposed.get("cuit") or "",
        "proposed_alias_interno": proposed.get("alias_interno") or "",
        "proposed_contacto": proposed.get("contacto") or "",
        "proposed_telefono": proposed.get("telefono") or "",
        "proposed_telefono_2": proposed.get("telefono_2") or "",
        "proposed_email": proposed.get("email") or "",
        "proposed_bejerman_condicion_iva": proposed.get("bejerman_condicion_iva") or "",
        "proposed_bejerman_email": proposed.get("bejerman_email") or "",
    }
    for key, _, _ in REF_SPECS:
        result[f"target_{key}"] = _count(target, key)
        result[f"source_{key}"] = _count(source, key)
        result[f"total_{key}"] = _count(target, key) + _count(source, key)
    return result


def _report_path(value: str | None) -> Path:
    raw = _clean(value) or DEFAULT_REPORT_DIR
    path = Path(raw)
    if path.suffix.lower() != ".csv":
        path = path / f"{REPORT_PREFIX}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in REPORT_FIELDS})


def _approved(value: Any) -> bool:
    return _strip_accents(value).strip().upper() in {"SI", "S", "YES", "Y", "TRUE", "1"}


def _read_approved_rows(path: str) -> list[dict[str, Any]]:
    in_path = Path(path)
    if not in_path.exists():
        raise CommandError(f"No existe el archivo de entrada: {in_path}")
    with in_path.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.DictReader(handle) if _approved(row.get("approved"))]
    return rows


def _set_audit_user(user: User) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SET app.user_id = %s;", [str(user.id)])
        cur.execute("SET app.user_role = %s;", [user.rol or ""])


def _json_db_value(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _update_target(customer_id: int, values: dict[str, Any], columns: set[str]) -> None:
    writable = {key: value for key, value in values.items() if key in columns}
    if not writable:
        return
    sets: list[str] = []
    params: list[Any] = []
    for column, value in writable.items():
        if column == "bejerman_raw" and connection.vendor == "postgresql":
            sets.append(f"{column} = %s::jsonb")
            params.append(_json_db_value(value))
        else:
            sets.append(f"{column} = %s")
            params.append(_json_db_value(value) if column == "bejerman_raw" else value)
    params.append(customer_id)
    with connection.cursor() as cur:
        cur.execute(f"UPDATE customers SET {', '.join(sets)} WHERE id = %s", params)


def _move_references(source_id: int, target_id: int) -> dict[str, int]:
    moved: dict[str, int] = {}
    with connection.cursor() as cur:
        for key, table, column in REF_SPECS:
            if not _table_has_column(table, column):
                moved[key] = 0
                continue
            cur.execute(f"UPDATE {table} SET {column} = %s WHERE {column} = %s", [target_id, source_id])
            moved[key] = int(cur.rowcount or 0)
    return moved


def _delete_source(source_id: int) -> None:
    with connection.cursor() as cur:
        cur.execute("DELETE FROM customers WHERE id = %s", [source_id])
        if cur.rowcount != 1:
            raise CommandError(f"No se pudo eliminar el cliente source_id={source_id}.")


def _lock_customers(source_id: int, target_id: int) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE id IN (%s, %s) FOR UPDATE", [source_id, target_id])


def _customer_by_id(customer_id: int) -> dict[str, Any] | None:
    rows = _load_customers()
    for row in rows:
        if int(row["id"]) == int(customer_id):
            return row
    return None


def _merge_pair(source_id: int, target_id: int) -> dict[str, Any]:
    if source_id == target_id:
        raise CommandError("source_id y target_id no pueden ser iguales.")
    if target_id != min(source_id, target_id):
        raise CommandError(f"El target debe ser el menor ID: source_id={source_id}, target_id={target_id}.")

    _lock_customers(source_id, target_id)
    source = _customer_by_id(source_id)
    target = _customer_by_id(target_id)
    if not source or not target:
        raise CommandError(f"Cliente source/target inexistente: source_id={source_id}, target_id={target_id}.")

    columns = _table_columns("customers")
    values, block_reason = _merged_values(target, source, columns)
    if block_reason:
        raise CommandError(
            f"Merge bloqueado por alias distinto: source_id={source_id}, target_id={target_id}."
        )
    moved = _move_references(source_id, target_id)
    _delete_source(source_id)
    _update_target(target_id, values, columns)
    return {"source_id": source_id, "target_id": target_id, **moved}


class Command(BaseCommand):
    help = "Genera reporte de clientes duplicados y aplica merges aprobados."

    def add_arguments(self, parser):
        parser.add_argument("--report", nargs="?", const=DEFAULT_REPORT_DIR, default="", help="Ruta CSV o directorio para el reporte.")
        parser.add_argument("--apply", action="store_true", help="Aplica solo filas aprobadas con approved=SI desde --input.")
        parser.add_argument("--input", default="", help="CSV revisado para aplicar merges aprobados.")
        parser.add_argument("--actor-email", default="", help="Usuario activo que queda registrado en auditoria al aplicar.")

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        if apply_changes:
            input_path = _clean(options.get("input"))
            actor_email = _clean(options.get("actor_email")).lower()
            if not input_path:
                raise CommandError("--input es requerido con --apply.")
            if not actor_email:
                raise CommandError("--actor-email es requerido con --apply.")
            user = User.objects.filter(email__iexact=actor_email, activo=True).first()
            if not user:
                raise CommandError(f"Usuario no encontrado o inactivo: {actor_email}")
            approved_rows = _read_approved_rows(input_path)
            if not approved_rows:
                self.stdout.write("MERGE_DUPLICATE_CUSTOMERS mode=apply approved=0 merged=0")
                return

            _set_audit_user(user)
            merged: list[dict[str, Any]] = []
            with transaction.atomic():
                for row in approved_rows:
                    try:
                        source_id = int(row.get("source_id") or 0)
                        target_id = int(row.get("target_id") or 0)
                    except Exception as exc:
                        raise CommandError("source_id y target_id deben ser numericos en el CSV.") from exc
                    merged.append(_merge_pair(source_id, target_id))
            self.stdout.write(
                "MERGE_DUPLICATE_CUSTOMERS "
                f"mode=apply approved={len(approved_rows)} merged={len(merged)}"
            )
            return

        rows = _candidate_rows(_load_customers())
        report = _report_path(options.get("report") or "")
        _write_report(report, rows)
        safe_count = sum(1 for row in rows if row.get("confidence") == "safe")
        review_count = sum(1 for row in rows if row.get("confidence") == "review")
        self.stdout.write(f"Reporte CSV: {report}")
        self.stdout.write(
            "MERGE_DUPLICATE_CUSTOMERS "
            f"mode=report candidates={len(rows)} safe={safe_count} review={review_count}"
        )
