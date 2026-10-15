"""Conciliación de solo lectura de OE-00647 en NEXORA y Bejerman productivos."""

import json
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings_prod")
sys.path.insert(0, "/app")

import django

django.setup()

from django.db import connection  # noqa: E402
from service.bejerman_sdk import (  # noqa: E402
    BejermanSDKClient,
    build_sales_filters,
    build_stock_rows,
    first_value,
    records_from_response,
)

assert "Bejerman-SDK-WS-TEST" not in os.environ.get("BEJERMAN_WSDL_URL", "")

with connection.cursor() as cur:
    cur.execute(
        """SELECT id, order_number, status, company_key, bejerman_customer_code,
                  remito_number, bejerman_remito_group_id, price_currency
             FROM delivery_orders WHERE order_number = %s""",
        ["OE-00647"],
    )
    order = cur.fetchone()
    if order is None:
        raise RuntimeError("OE-00647 no existe")
    print("ORDER", json.dumps(order, default=str), flush=True)
    cur.execute(
        """SELECT article_code, quantity, unit_price, price_currency,
                  discount_percent, partida, stock_deposit_code
             FROM delivery_order_items WHERE order_id = %s ORDER BY sort_order""",
        [order[0]],
    )
    print("ITEMS", json.dumps(cur.fetchall(), default=str), flush=True)
    cur.execute(
        """SELECT id, status, remito_number, attempt_count, last_error, created_at,
                  created_by_user_id, seller_code, payment_term_code
             FROM bejerman_remito_groups
            WHERE customer_code = %s AND (order_ids @> %s::jsonb OR id = %s)
            ORDER BY created_at DESC LIMIT 10""",
        ["SNSP", json.dumps([order[0]]), order[6] or ""],
    )
    print("GROUPS", json.dumps(cur.fetchall(), default=str), flush=True)

client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
response = client.list_comprobantes_ventas(build_sales_filters("SNSP", "2026-01-01", "2026-10-14", "RT", ""))
rows = records_from_response(response)
summaries = [
    {
        "type": first_value(row, ("Comprobante_Tipo", "Tipo")),
        "number": first_value(row, ("Comprobante_Numero", "Numero")),
        "point": first_value(row, ("Comprobante_PtoVenta", "PtoVenta")),
        "date": first_value(row, ("Comprobante_FechaEmision", "FechaEmision")),
        "total": first_value(row, ("Comprobante_ImporteTotal", "ImporteTotal")),
    }
    for row in rows
]
print("BEJERMAN_RT", json.dumps(summaries, default=str), flush=True)
stock = build_stock_rows(client.obtener_stock_partida("D2406282H"))
print(
    "PARTIDA",
    json.dumps([row for row in stock if str(row.get("articleCode") or "").strip() == "1202015"], default=str),
    flush=True,
)
