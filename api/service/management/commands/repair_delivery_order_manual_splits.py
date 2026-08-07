import json
from dataclasses import dataclass
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


MANUAL_SPLIT_EVENT = "delivery_order_manual_split_for_remito"
REPAIR_EVENT = "delivery_order_manual_split_repaired"
PREPARABLE_STATUSES = {"pendiente_armado", "armado_pendiente_entrega"}
REQUIRED_TABLES = {
    "delivery_orders",
    "delivery_order_items",
    "delivery_order_events",
    "bejerman_remito_groups",
    "bejerman_integration_attempts",
}
MULTI_REMITO_TABLES = {
    "delivery_order_remito_links",
    "delivery_order_remito_item_allocations",
}


@dataclass(frozen=True)
class SplitCandidate:
    source_id: str
    split_ids: tuple[str, ...]
    event_order_ids: frozenset[str]
    discovery_errors: tuple[str, ...] = ()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _metadata_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values = tuple(_clean_text(item) for item in value)
    if any(not item for item in values) or len(set(values)) != len(values):
        return ()
    return values


def _rows(cur) -> list[dict[str, Any]]:
    columns = [column[0] for column in (cur.description or [])]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _existing_tables() -> set[str]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = ANY(current_schemas(true))
            """
        )
        return {row[0] for row in cur.fetchall()}


def _load_candidates() -> tuple[list[SplitCandidate], list[str]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, order_id, metadata
              FROM delivery_order_events
             WHERE event_type = %s
             ORDER BY id
            """,
            [MANUAL_SPLIT_EVENT],
        )
        event_rows = _rows(cur)

    grouped: dict[str, dict[str, Any]] = {}
    discovery_errors: list[str] = []
    for row in event_rows:
        metadata = _metadata_dict(row.get("metadata"))
        source_id = _clean_text(metadata.get("sourceOrderId"))
        split_ids = _metadata_ids(metadata.get("splitOrderIds"))
        if not source_id:
            discovery_errors.append(
                f"Evento #{row.get('id')} ({row.get('order_id')}): falta metadata.sourceOrderId."
            )
            continue
        entry = grouped.setdefault(
            source_id,
            {"signatures": set(), "event_order_ids": set(), "errors": []},
        )
        entry["event_order_ids"].add(_clean_text(row.get("order_id")))
        if not split_ids:
            entry["errors"].append(
                f"Evento #{row.get('id')}: splitOrderIds está ausente, vacío, repetido o mal formado."
            )
            continue
        entry["signatures"].add(split_ids)

    candidates: list[SplitCandidate] = []
    for source_id, entry in sorted(grouped.items()):
        signatures = entry["signatures"]
        errors = list(entry["errors"])
        if len(signatures) != 1:
            errors.append("Los eventos del origen no coinciden en la lista y el orden de fragmentos.")
            split_ids: tuple[str, ...] = ()
        else:
            split_ids = next(iter(signatures))
        if split_ids and source_id not in split_ids:
            errors.append("La orden origen no figura dentro de splitOrderIds.")
        if split_ids and len(split_ids) < 2:
            errors.append("La división no contiene ningún fragmento derivado.")
        candidates.append(
            SplitCandidate(
                source_id=source_id,
                split_ids=split_ids,
                event_order_ids=frozenset(entry["event_order_ids"]),
                discovery_errors=tuple(errors),
            )
        )
    return candidates, discovery_errors


def _repair_event_matches(source_id: str, derived_ids: set[str]) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT metadata
              FROM delivery_order_events
             WHERE order_id = %s
               AND event_type = %s
             ORDER BY id DESC
            """,
            [source_id, REPAIR_EVENT],
        )
        for (raw_metadata,) in cur.fetchall():
            metadata = _metadata_dict(raw_metadata)
            removed_ids = set(_metadata_ids(metadata.get("removedOrderIds")))
            if removed_ids == derived_ids:
                return True
    return False


def _linked_group_ids(order_ids: list[str], *, table_names: set[str]) -> list[str]:
    group_ids: set[str] = set()
    if "delivery_order_remito_links" in table_names:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT remito_group_id
                  FROM delivery_order_remito_links
                 WHERE order_id = ANY(%s)
                """,
                [order_ids],
            )
            group_ids.update(_clean_text(row[0]) for row in cur.fetchall())

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT remito.id
              FROM bejerman_remito_groups AS remito
              JOIN LATERAL jsonb_array_elements_text(
                CASE
                  WHEN jsonb_typeof(remito.order_ids) = 'array' THEN remito.order_ids
                  ELSE '[]'::jsonb
                END
              ) AS linked(order_id) ON TRUE
             WHERE linked.order_id = ANY(%s)
            """,
            [order_ids],
        )
        group_ids.update(_clean_text(row[0]) for row in cur.fetchall())
    return sorted(group_id for group_id in group_ids if group_id)


def _integration_attempt_ids(order_ids: list[str]) -> list[int]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT attempt.id
              FROM bejerman_integration_attempts AS attempt
              JOIN LATERAL jsonb_array_elements_text(
                CASE
                  WHEN jsonb_typeof(attempt.order_ids) = 'array' THEN attempt.order_ids
                  ELSE '[]'::jsonb
                END
              ) AS linked(order_id) ON TRUE
             WHERE linked.order_id = ANY(%s)
             ORDER BY attempt.id
            """,
            [order_ids],
        )
        return [int(row[0]) for row in cur.fetchall()]


def _route_stop_ids(order_ids: list[str], *, table_names: set[str]) -> list[str]:
    if "route_stops" not in table_names:
        return []
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id
              FROM route_stops
             WHERE delivery_order_id = ANY(%s)
             ORDER BY id
            """,
            [order_ids],
        )
        return [_clean_text(row[0]) for row in cur.fetchall()]


def _inspect_candidate(
    candidate: SplitCandidate,
    *,
    table_names: set[str],
    lock: bool,
) -> dict[str, Any]:
    errors = list(candidate.discovery_errors)
    if not candidate.split_ids:
        return {"status": "blocked", "errors": errors or ["No hay una lista de fragmentos confiable."]}

    split_ids = list(candidate.split_ids)
    source_id = candidate.source_id
    derived_ids = [order_id for order_id in split_ids if order_id != source_id]
    lock_sql = " FOR UPDATE" if lock else ""
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, order_number, customer_id, bejerman_customer_code, customer_name,
                   delivery_type, company_key, price_currency, status, remito_number,
                   bejerman_remito_group_id, remito_location, invoice_number,
                   imported_delivered_flag, delivered_by_user_id, invoiced_by_user_id,
                   cancelled_by_user_id, delivered_at, invoiced_at, cancelled_at
              FROM delivery_orders
             WHERE id = ANY(%s)
             ORDER BY id
             {lock_sql}
            """,
            [split_ids],
        )
        order_rows = _rows(cur)

    orders_by_id = {row["id"]: row for row in order_rows}
    missing_ids = [order_id for order_id in split_ids if order_id not in orders_by_id]
    if missing_ids:
        all_derived_missing = set(missing_ids) == set(derived_ids) and source_id in orders_by_id
        if all_derived_missing and _repair_event_matches(source_id, set(derived_ids)):
            return {
                "status": "already_repaired",
                "source": orders_by_id[source_id],
                "removed_ids": derived_ids,
            }
        errors.append("Faltan órdenes de la división: " + ", ".join(missing_ids) + ".")
        return {"status": "blocked", "errors": errors}

    if set(candidate.event_order_ids) != set(split_ids):
        missing_events = sorted(set(split_ids) - set(candidate.event_order_ids))
        unexpected_events = sorted(set(candidate.event_order_ids) - set(split_ids))
        if missing_events:
            errors.append("Falta el evento manual en: " + ", ".join(missing_events) + ".")
        if unexpected_events:
            errors.append("Hay eventos manuales ajenos a la división: " + ", ".join(unexpected_events) + ".")

    source = orders_by_id[source_id]
    comparison_fields = (
        "customer_id",
        "bejerman_customer_code",
        "customer_name",
        "delivery_type",
        "company_key",
        "price_currency",
    )
    for order_id in split_ids:
        order = orders_by_id[order_id]
        label = f"{order.get('order_number') or order_id} ({order_id})"
        status = _clean_text(order.get("status")).lower()
        if status not in PREPARABLE_STATUSES:
            errors.append(f"{label}: estado no preparable {status or '(vacío)' }.")
        for field, description in (
            ("remito_number", "remito"),
            ("bejerman_remito_group_id", "grupo Bejerman"),
            ("invoice_number", "factura"),
            ("remito_location", "ubicación de remito"),
        ):
            if _clean_text(order.get(field)):
                errors.append(f"{label}: ya tiene {description}.")
        if order.get("imported_delivered_flag") is True:
            errors.append(f"{label}: figura como entregada en la importación.")
        if order.get("delivered_at") is not None or order.get("delivered_by_user_id") is not None:
            errors.append(f"{label}: tiene evidencia de entrega.")
        if order.get("invoiced_at") is not None or order.get("invoiced_by_user_id") is not None:
            errors.append(f"{label}: tiene evidencia de facturación.")
        if order.get("cancelled_at") is not None or order.get("cancelled_by_user_id") is not None:
            errors.append(f"{label}: tiene evidencia de cancelación.")
        if order_id != source_id:
            changed_fields = [field for field in comparison_fields if order.get(field) != source.get(field)]
            if changed_fields:
                errors.append(
                    f"{label}: difiere del origen en datos maestros ({', '.join(changed_fields)})."
                )

    group_ids = _linked_group_ids(split_ids, table_names=table_names)
    if group_ids:
        errors.append("Hay grupos Bejerman asociados: " + ", ".join(group_ids) + ".")
    attempt_ids = _integration_attempt_ids(split_ids)
    if attempt_ids:
        errors.append(
            "Hay intentos de integración Bejerman asociados: "
            + ", ".join(str(attempt_id) for attempt_id in attempt_ids)
            + "."
        )
    route_stop_ids = _route_stop_ids(split_ids, table_names=table_names)
    if route_stop_ids:
        errors.append("Hay paradas de reparto asociadas: " + ", ".join(route_stop_ids) + ".")

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, order_id, sort_order, created_at
              FROM delivery_order_items
             WHERE order_id = ANY(%s)
             {lock_sql}
            """,
            [split_ids],
        )
        item_rows = _rows(cur)
    items_by_order = {order_id: [] for order_id in split_ids}
    for item in item_rows:
        items_by_order.setdefault(item["order_id"], []).append(item)
    for order_id in split_ids:
        if not items_by_order.get(order_id):
            order = orders_by_id[order_id]
            errors.append(f"{order.get('order_number') or order_id}: no tiene artículos para fusionar.")

    if errors:
        return {
            "status": "blocked",
            "errors": errors,
            "source": source,
            "orders": order_rows,
        }

    fragment_position = {order_id: index for index, order_id in enumerate(split_ids)}
    item_rows.sort(
        key=lambda item: (
            fragment_position[item["order_id"]],
            int(item.get("sort_order") or 0),
            item.get("created_at"),
            item["id"],
        )
    )
    return {
        "status": "safe",
        "source": source,
        "orders": order_rows,
        "items": item_rows,
        "derived_ids": derived_ids,
        "split_ids": split_ids,
    }


def _apply_repair(inspection: dict[str, Any]) -> dict[str, Any]:
    source = inspection["source"]
    source_id = source["id"]
    derived_ids = inspection["derived_ids"]
    item_rows = inspection["items"]
    order_rows = inspection["orders"]
    original_order_numbers = [
        {"id": row["id"], "orderNumber": row.get("order_number") or ""}
        for row in sorted(order_rows, key=lambda row: inspection["split_ids"].index(row["id"]))
    ]

    with connection.cursor() as cur:
        for sort_order, item in enumerate(item_rows):
            cur.execute(
                """
                UPDATE delivery_order_items
                   SET order_id = %s,
                       sort_order = %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                [source_id, sort_order, item["id"]],
            )
            if cur.rowcount != 1:
                raise CommandError(f"No se pudo mover el artículo {item['id']} a {source_id}.")

        cur.execute(
            "UPDATE delivery_orders SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            [source_id],
        )
        cur.execute("DELETE FROM delivery_orders WHERE id = ANY(%s)", [derived_ids])
        if cur.rowcount != len(derived_ids):
            raise CommandError(
                f"Se esperaban borrar {len(derived_ids)} fragmentos y se borraron {cur.rowcount}."
            )

        metadata = {
            "sourceOrderId": source_id,
            "splitOrderIds": inspection["split_ids"],
            "removedOrderIds": derived_ids,
            "originalOrders": original_order_numbers,
            "itemCount": len(item_rows),
            "repairCommand": "repair_delivery_order_manual_splits",
        }
        cur.execute(
            """
            INSERT INTO delivery_order_events (
              order_id, actor_user_id, source_actor_user_id, event_type, note, metadata
            )
            VALUES (%s, NULL, %s, %s, %s, %s::jsonb)
            """,
            [
                source_id,
                "management:repair_delivery_order_manual_splits",
                REPAIR_EVENT,
                "Se revirtió la división manual creada para registrar remitos.",
                json.dumps(metadata, ensure_ascii=False),
            ],
        )
        cur.execute(
            """
            SELECT COUNT(*), MIN(sort_order), MAX(sort_order), COUNT(DISTINCT sort_order)
              FROM delivery_order_items
             WHERE order_id = %s
            """,
            [source_id],
        )
        count, minimum, maximum, distinct_count = cur.fetchone()
        expected_count = len(item_rows)
        if (
            int(count or 0) != expected_count
            or int(distinct_count or 0) != expected_count
            or int(minimum if minimum is not None else -1) != 0
            or int(maximum if maximum is not None else -1) != expected_count - 1
        ):
            raise CommandError(f"La secuencia final de artículos de {source_id} no es íntegra.")

    return {
        "source_id": source_id,
        "order_number": source.get("order_number") or source_id,
        "item_count": len(item_rows),
        "removed_ids": derived_ids,
    }


class Command(BaseCommand):
    help = (
        "Detecta y revierte divisiones físicas de órdenes creadas para cargar remitos. "
        "Por defecto solo informa; usar --apply para persistir."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo informa los casos seguros y bloqueados; es también el modo predeterminado.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Fusiona únicamente los casos seguros. Sin este flag funciona en dry-run.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("repair_delivery_order_manual_splits requiere PostgreSQL.")

        if options.get("apply") and options.get("dry_run"):
            raise CommandError("Use --dry-run o --apply, no ambos.")
        apply_changes = bool(options.get("apply"))
        table_names = _existing_tables()
        missing_tables = sorted(REQUIRED_TABLES - table_names)
        if missing_tables:
            raise CommandError("Faltan tablas requeridas: " + ", ".join(missing_tables) + ".")
        if apply_changes:
            missing_multi_remito_tables = sorted(MULTI_REMITO_TABLES - table_names)
            if missing_multi_remito_tables:
                raise CommandError(
                    "Ejecutá apply_delivery_orders_schema antes de reparar. Faltan: "
                    + ", ".join(missing_multi_remito_tables)
                    + "."
                )

        candidates, discovery_errors = _load_candidates()
        mode = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(f"Modo {mode}. Divisiones detectadas: {len(candidates)}.")
        blocked_count = 0
        safe_count = 0
        repaired_count = 0
        already_repaired_count = 0

        for error in discovery_errors:
            blocked_count += 1
            self.stderr.write(self.style.ERROR("BLOQUEADO: " + error))

        for candidate in candidates:
            if apply_changes:
                with transaction.atomic():
                    inspection = _inspect_candidate(candidate, table_names=table_names, lock=True)
                    if inspection["status"] == "safe":
                        result = _apply_repair(inspection)
                    else:
                        result = None
            else:
                inspection = _inspect_candidate(candidate, table_names=table_names, lock=False)
                result = None

            if inspection["status"] == "already_repaired":
                already_repaired_count += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"YA REPARADO: {candidate.source_id}; los fragmentos derivados ya no existen."
                    )
                )
                continue
            if inspection["status"] == "blocked":
                blocked_count += 1
                self.stderr.write(self.style.ERROR(f"BLOQUEADO: origen {candidate.source_id}."))
                for error in inspection["errors"]:
                    self.stderr.write(f"  - {error}")
                continue

            safe_count += 1
            source = inspection["source"]
            order_number = source.get("order_number") or candidate.source_id
            if not apply_changes:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"SEGURO: {order_number} ({candidate.source_id}); "
                        f"{len(inspection['items'])} artículos, "
                        f"{len(inspection['derived_ids'])} fragmentos derivados."
                    )
                )
                continue

            repaired_count += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"REPARADO: {result['order_number']} ({result['source_id']}); "
                    f"{result['item_count']} artículos, eliminados: {', '.join(result['removed_ids'])}."
                )
            )

        self.stdout.write(
            "Resumen: "
            f"seguros={safe_count}, reparados={repaired_count}, "
            f"ya_reparados={already_repaired_count}, bloqueados={blocked_count}."
        )
        if not apply_changes and safe_count:
            self.stdout.write("Dry-run finalizado. Usá --apply para persistir únicamente los casos seguros.")
        if blocked_count:
            raise CommandError(f"Se bloquearon {blocked_count} caso(s) por seguridad.")
