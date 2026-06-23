from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from psycopg.rows import dict_row


VALID_STATUSES = {
    "pendiente_armado",
    "armado_pendiente_entrega",
    "entregado_pendiente_facturacion",
    "entregado_no_facturable",
    "facturado",
    "cancelado",
}
VALID_TYPES = {"sale", "service_release", "rental", "demo"}
REQUIRED_PORTAL_TABLES = {
    "companies",
    "sales_orders",
    "sales_order_items",
    "sales_order_item_partidas",
    "sales_order_events",
    "bejerman_remito_groups",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    value = _text(value)
    return value or None


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _rows(cur) -> list[dict[str, Any]]:
    cols = [col[0] for col in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _missing_portal_tables(portal) -> list[str]:
    with portal.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = ANY(current_schemas(false))
               AND table_name = ANY(%s)
            """,
            [list(REQUIRED_PORTAL_TABLES)],
        )
        found = {row["table_name"] for row in cur.fetchall()}
    return sorted(REQUIRED_PORTAL_TABLES - found)


def _portal_counts(portal):
    out = {}
    with portal.cursor() as cur:
        for table in ("sales_orders", "sales_order_items", "sales_order_item_partidas", "bejerman_remito_groups"):
            cur.execute(f"SELECT COUNT(*) AS count FROM {table}")
            out[table] = int(cur.fetchone()["count"])
        cur.execute("SELECT COUNT(*) AS count FROM sales_orders WHERE status = 'entregado_pendiente_facturacion'")
        out["pending_billing"] = int(cur.fetchone()["count"])
    return out


def _portal_grouped_counts(portal):
    with portal.cursor() as cur:
        cur.execute('SELECT status, COUNT(*)::int AS count FROM sales_orders GROUP BY status ORDER BY status')
        by_status = {row["status"]: row["count"] for row in cur.fetchall()}
        cur.execute('SELECT COALESCE("deliveryType", \'sale\') AS type, COUNT(*)::int AS count FROM sales_orders GROUP BY 1 ORDER BY 1')
        by_type = {row["type"]: row["count"] for row in cur.fetchall()}
    return {"by_status": by_status, "by_type": by_type}


def _load_portal_companies(portal):
    with portal.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, "nexoraCustomerId", TRIM(COALESCE("bejermanCustomerCode", '')) AS "bejermanCustomerCode"
            FROM companies
            """
        )
        return {row["id"]: row for row in cur.fetchall()}


def _load_customer_indexes():
    with connection.cursor() as cur:
        cur.execute("SELECT id, razon_social, TRIM(COALESCE(cod_empresa, '')) AS cod_empresa FROM customers")
        rows = _rows(cur)
    by_id = {row["id"]: row for row in rows}
    by_code = {row["cod_empresa"]: row for row in rows if row.get("cod_empresa")}
    return by_id, by_code


def _resolve_customer(company_id, companies, customers_by_id, customers_by_code):
    company = companies.get(company_id)
    if not company:
        return None, None, None
    nexora_id = company.get("nexoraCustomerId")
    code = _optional_text(company.get("bejermanCustomerCode"))
    if nexora_id in customers_by_id:
        customer = customers_by_id[nexora_id]
        return customer["id"], customer.get("cod_empresa") or code, customer.get("razon_social") or company.get("name")
    if code and code in customers_by_code:
        customer = customers_by_code[code]
        return customer["id"], code, customer.get("razon_social") or company.get("name")
    return None, code, company.get("name")


def _fetch_portal_orders(portal):
    with portal.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM sales_orders
            ORDER BY "createdAt", id
            """
        )
        return cur.fetchall()


def _fetch_portal_items(portal):
    with portal.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM sales_order_items
            ORDER BY "orderId", "sortOrder", "createdAt"
            """
        )
        items = cur.fetchall()
        cur.execute(
            """
            SELECT *
            FROM sales_order_item_partidas
            ORDER BY "orderItemId", "sortOrder", "createdAt"
            """
        )
        partidas = cur.fetchall()
    return items, partidas


def _fetch_portal_groups_and_events(portal):
    with portal.cursor() as cur:
        cur.execute("SELECT * FROM bejerman_remito_groups ORDER BY \"createdAt\", id")
        groups = cur.fetchall()
        cur.execute("SELECT * FROM sales_order_events ORDER BY \"createdAt\", id")
        events = cur.fetchall()
    return groups, events


def build_reconciliation(portal, *, sample_limit=20) -> dict[str, Any]:
    missing_tables = _missing_portal_tables(portal)
    if missing_tables:
        return {
            "source_ready": False,
            "missing_portal_tables": missing_tables,
            "counts": {},
            "by_status": {},
            "by_type": {},
            "missing_customer_mappings": {"count": 0, "sample": []},
            "missing_article_mappings": {"count": 0, "sample": []},
            "missing_partida_mappings": {"count": 0, "sample": []},
            "unknown_statuses": {},
            "unknown_types": {},
            "remito_group_pdf_integrity": {
                "orders_with_missing_group_count": 0,
                "orders_with_missing_group_sample": [],
                "generated_groups_missing_pdf_reference_count": 0,
                "generated_groups_missing_pdf_reference_sample": [],
            },
        }
    counts = _portal_counts(portal)
    grouped = _portal_grouped_counts(portal)
    companies = _load_portal_companies(portal)
    customers_by_id, customers_by_code = _load_customer_indexes()
    orders = _fetch_portal_orders(portal)
    items, partidas = _fetch_portal_items(portal)
    groups, _events = _fetch_portal_groups_and_events(portal)

    missing_customers = []
    unknown_status = Counter()
    unknown_type = Counter()
    for row in orders:
        customer_id, code, name = _resolve_customer(row.get("companyId"), companies, customers_by_id, customers_by_code)
        if row.get("companyId") and not customer_id:
            missing_customers.append(
                {
                    "portalCompanyId": row.get("companyId"),
                    "companyName": name,
                    "bejermanCustomerCode": code or "",
                    "orderId": row.get("id"),
                    "orderNumber": row.get("orderNumber"),
                }
            )
        status = _text(row.get("status"))
        if status and status not in VALID_STATUSES:
            unknown_status[status] += 1
        dtype = _text(row.get("deliveryType") or "sale")
        if dtype and dtype not in VALID_TYPES:
            unknown_type[dtype] += 1

    missing_articles = [
        {"itemId": row.get("id"), "orderId": row.get("orderId"), "description": row.get("description") or ""}
        for row in items
        if not _optional_text(row.get("articleCode"))
    ]
    items_with_partida = {row.get("orderItemId") for row in partidas if _optional_text(row.get("partida"))}
    missing_partidas = [
        {
            "itemId": row.get("id"),
            "orderId": row.get("orderId"),
            "articleCode": row.get("articleCode") or "",
            "description": row.get("description") or "",
        }
        for row in items
        if _optional_text(row.get("articleCode")) and row.get("id") not in items_with_partida
    ]
    group_ids = {row.get("id") for row in groups}
    remito_integrity = {
        "orders_with_missing_group": [
            {"orderId": row.get("id"), "orderNumber": row.get("orderNumber"), "groupId": row.get("bejermanRemitoGroupId")}
            for row in orders
            if _optional_text(row.get("bejermanRemitoGroupId")) and row.get("bejermanRemitoGroupId") not in group_ids
        ],
        "generated_groups_missing_pdf_reference": [
            {"groupId": row.get("id"), "remitoNumber": row.get("remitoNumber")}
            for row in groups
            if row.get("status") == "generated"
            and (not _optional_text(row.get("comprobanteNumero")) or not _optional_text(row.get("comprobantePtoVenta")))
        ],
    }

    return {
        "source_ready": True,
        "counts": counts,
        **grouped,
        "missing_customer_mappings": {
            "count": len(missing_customers),
            "sample": missing_customers[:sample_limit],
        },
        "missing_article_mappings": {
            "count": len(missing_articles),
            "sample": missing_articles[:sample_limit],
        },
        "missing_partida_mappings": {
            "count": len(missing_partidas),
            "sample": missing_partidas[:sample_limit],
        },
        "unknown_statuses": dict(unknown_status),
        "unknown_types": dict(unknown_type),
        "remito_group_pdf_integrity": {
            "orders_with_missing_group_count": len(remito_integrity["orders_with_missing_group"]),
            "orders_with_missing_group_sample": remito_integrity["orders_with_missing_group"][:sample_limit],
            "generated_groups_missing_pdf_reference_count": len(remito_integrity["generated_groups_missing_pdf_reference"]),
            "generated_groups_missing_pdf_reference_sample": remito_integrity["generated_groups_missing_pdf_reference"][:sample_limit],
        },
    }


def _status(value):
    value = _text(value or "pendiente_armado")
    return value if value in VALID_STATUSES else "pendiente_armado"


def _delivery_type(value):
    value = _text(value or "sale")
    return value if value in VALID_TYPES else "sale"


def _priority(value):
    value = _text(value or "normal")
    return value if value in {"normal", "urgente"} else "normal"


def apply_import(portal) -> dict[str, int]:
    companies = _load_portal_companies(portal)
    customers_by_id, customers_by_code = _load_customer_indexes()
    orders = _fetch_portal_orders(portal)
    items, partidas = _fetch_portal_items(portal)
    groups, events = _fetch_portal_groups_and_events(portal)

    inserted = Counter()
    with transaction.atomic():
        with connection.cursor() as cur:
            for group in groups:
                cur.execute(
                    """
                    INSERT INTO bejerman_remito_groups (
                      id, comprobante_tipo, comprobante_letra, comprobante_pto_venta, comprobante_numero,
                      remito_number, customer_code, customer_name, seller_code, payment_term_code,
                      operation_code, deposit_code, status, order_ids, response_summary,
                      source_created_by_user_id, created_at, generated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      remito_number = EXCLUDED.remito_number,
                      status = EXCLUDED.status,
                      response_summary = EXCLUDED.response_summary,
                      generated_at = EXCLUDED.generated_at
                    """,
                    [
                        group["id"],
                        group.get("comprobanteTipo") or "RT",
                        group.get("comprobanteLetra") or "R",
                        group.get("comprobantePtoVenta"),
                        group.get("comprobanteNumero"),
                        group.get("remitoNumber"),
                        group.get("customerCode") or "",
                        group.get("customerName") or "",
                        group.get("sellerCode") or "ADM",
                        group.get("paymentTermCode") or "",
                        group.get("operationCode") or "",
                        group.get("depositCode") or "",
                        group.get("status") or "pending",
                        _json(group.get("orderIds") or []),
                        _json(group.get("responseSummary") or {}),
                        group.get("createdByUserId"),
                        group.get("createdAt"),
                        group.get("generatedAt"),
                    ],
                )
                inserted["bejerman_remito_groups"] += 1

            for order in orders:
                customer_id, customer_code, customer_name = _resolve_customer(
                    order.get("companyId"), companies, customers_by_id, customers_by_code
                )
                cur.execute(
                    """
                    INSERT INTO delivery_orders (
                      id, order_number, customer_id, bejerman_customer_code, customer_name,
                      delivery_type, source_system, source_external_id, source_reference, source_company_id,
                      source_sheet, source_row, source_color, equipment_model, equipment_serial,
                      equipment_internal_number, seller_name, order_date, operation_company_label,
                      raw_pedido, commercial_terms, commercial_price, commercial_exchange_rate,
                      commercial_condition, commercial_deadline, status, priority, remito_number,
                      bejerman_remito_group_id, remito_location, invoice_number, imported_delivered_flag,
                      source_created_by_user_id, source_prepared_by_user_id, source_delivered_by_user_id,
                      source_invoiced_by_user_id, source_cancelled_by_user_id,
                      created_at, updated_at, prepared_at, delivered_at, invoiced_at, cancelled_at
                    )
                    VALUES (
                      %s, %s, %s, %s, %s,
                      %s, 'portal', %s, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s,
                      %s, %s,
                      %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                      status = EXCLUDED.status,
                      remito_number = EXCLUDED.remito_number,
                      bejerman_remito_group_id = EXCLUDED.bejerman_remito_group_id,
                      remito_location = EXCLUDED.remito_location,
                      invoice_number = EXCLUDED.invoice_number,
                      updated_at = EXCLUDED.updated_at
                    """,
                    [
                        order["id"],
                        order.get("orderNumber"),
                        customer_id,
                        customer_code,
                        order.get("customerName") or customer_name or "",
                        _delivery_type(order.get("deliveryType")),
                        order["id"],
                        order.get("sourceReference"),
                        order.get("companyId"),
                        order.get("sourceSheet"),
                        order.get("sourceRow"),
                        order.get("sourceColor"),
                        order.get("equipmentModel"),
                        order.get("equipmentSerial"),
                        order.get("equipmentInternalNumber"),
                        order.get("sellerName") or "",
                        order.get("orderDate"),
                        order.get("operationCompanyLabel") or "",
                        order.get("rawPedido") or "",
                        order.get("commercialTerms"),
                        order.get("commercialPrice"),
                        order.get("commercialExchangeRate"),
                        order.get("commercialCondition"),
                        order.get("commercialDeadline"),
                        _status(order.get("status")),
                        _priority(order.get("priority")),
                        order.get("remitoNumber"),
                        order.get("bejermanRemitoGroupId"),
                        order.get("remitoLocation"),
                        order.get("invoiceNumber"),
                        order.get("importedDeliveredFlag"),
                        order.get("createdByUserId"),
                        order.get("preparedByUserId"),
                        order.get("deliveredByUserId"),
                        order.get("invoicedByUserId"),
                        order.get("cancelledByUserId"),
                        order.get("createdAt"),
                        order.get("updatedAt"),
                        order.get("preparedAt"),
                        order.get("deliveredAt"),
                        order.get("invoicedAt"),
                        order.get("cancelledAt"),
                    ],
                )
                inserted["delivery_orders"] += 1

            for item in items:
                cur.execute(
                    """
                    INSERT INTO delivery_order_items (
                      id, order_id, article_code, article_name, description, quantity, unit_price,
                      source_text, partida, partida_expiration_date, stock_deposit_code,
                      stock_available_quantity, stock_checked_at, sort_order, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      article_code = EXCLUDED.article_code,
                      article_name = EXCLUDED.article_name,
                      quantity = EXCLUDED.quantity,
                      unit_price = EXCLUDED.unit_price,
                      partida = EXCLUDED.partida
                    """,
                    [
                        item["id"],
                        item.get("orderId"),
                        item.get("articleCode"),
                        item.get("articleName"),
                        item.get("description") or "",
                        item.get("quantity") or 1,
                        item.get("unitPrice"),
                        item.get("sourceText"),
                        item.get("partida"),
                        item.get("partidaExpirationDate"),
                        item.get("stockDepositCode"),
                        item.get("stockAvailableQuantity"),
                        item.get("stockCheckedAt"),
                        item.get("sortOrder") or 0,
                        item.get("createdAt"),
                    ],
                )
                inserted["delivery_order_items"] += 1

            for partida in partidas:
                cur.execute(
                    """
                    INSERT INTO delivery_order_item_partidas (
                      id, order_item_id, partida, assigned_quantity, partida_expiration_date,
                      stock_deposit_code, stock_available_quantity, stock_checked_at,
                      sort_order, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      partida = EXCLUDED.partida,
                      assigned_quantity = EXCLUDED.assigned_quantity,
                      stock_available_quantity = EXCLUDED.stock_available_quantity,
                      updated_at = EXCLUDED.updated_at
                    """,
                    [
                        partida["id"],
                        partida.get("orderItemId"),
                        partida.get("partida") or "",
                        partida.get("assignedQuantity") or 1,
                        partida.get("partidaExpirationDate"),
                        partida.get("stockDepositCode"),
                        partida.get("stockAvailableQuantity"),
                        partida.get("stockCheckedAt"),
                        partida.get("sortOrder") or 0,
                        partida.get("createdAt"),
                        partida.get("updatedAt"),
                    ],
                )
                inserted["delivery_order_item_partidas"] += 1

            cur.execute("DELETE FROM delivery_order_events WHERE source_actor_user_id IS NOT NULL")
            for event in events:
                cur.execute(
                    """
                    INSERT INTO delivery_order_events (
                      order_id, source_actor_user_id, event_type, note, metadata, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    [
                        event.get("orderId"),
                        event.get("actorUserId"),
                        event.get("eventType") or "portal_event",
                        event.get("note"),
                        _json(event.get("metadata") or {}),
                        event.get("createdAt"),
                    ],
                )
                inserted["delivery_order_events"] += 1

    return dict(inserted)


class Command(BaseCommand):
    help = "Importa órdenes de entrega desde Portal Sepid hacia NEXORA. Dry-run por defecto."

    def add_arguments(self, parser):
        parser.add_argument("--portal-database-url", default=os.getenv("PORTAL_DATABASE_URL", ""))
        parser.add_argument("--apply", action="store_true", help="Escribe datos en NEXORA. Sin esto solo informa la conciliación.")
        parser.add_argument("--sample-limit", type=int, default=20)

    def handle(self, *args, **opts):
        dsn = _text(opts.get("portal_database_url"))
        if not dsn:
            raise CommandError("Falta --portal-database-url o PORTAL_DATABASE_URL")

        try:
            portal = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        except Exception as exc:
            raise CommandError(f"No se pudo conectar al Portal: {exc}") from exc

        with portal:
            with portal.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            reconciliation = build_reconciliation(portal, sample_limit=int(opts.get("sample_limit") or 20))
            self.stdout.write(json.dumps({"dry_run": not opts["apply"], **reconciliation}, ensure_ascii=False, indent=2, default=str))

            if not reconciliation.get("source_ready", True):
                if opts["apply"]:
                    raise CommandError("La base Portal no tiene las tablas requeridas; importación cancelada.")
                return

            if not opts["apply"]:
                return

        # Reabrir sin transacción read-only para que el origen siga siendo solo lectura y NEXORA reciba la importación.
        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as portal_apply:
            result = apply_import(portal_apply)
        self.stdout.write(json.dumps({"applied": True, "imported": result}, ensure_ascii=False, indent=2, default=str))
