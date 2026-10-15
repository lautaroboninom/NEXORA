"""Emite OE-00647 una sola vez, tras conciliar NEXORA y Bejerman."""

import hashlib
import json
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings_prod")
sys.path.insert(0, "/app")

import django

django.setup()

from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402
from service.bejerman_delivery import generate_bejerman_remito  # noqa: E402
from service.bejerman_sdk import (  # noqa: E402
    BejermanSDKClient,
    build_sales_filters,
    build_stock_rows,
    records_from_response,
)

EXPECTED_SDK_SHA256 = "af742469a9ea0cbd3d8c346bc539a7c8d59380397b4c3262d136b5d2befc5dff"
assert "Bejerman-SDK-WS-TEST" not in os.environ.get("BEJERMAN_WSDL_URL", "")
assert hashlib.sha256(open("/app/service/bejerman_sdk.py", "rb").read()).hexdigest() == EXPECTED_SDK_SHA256

with connection.cursor() as cur:
    cur.execute(
        """SELECT id, status, company_key, bejerman_customer_code, remito_number
             FROM delivery_orders WHERE order_number = %s""",
        ["OE-00647"],
    )
    order = cur.fetchone()
    assert order is not None and order[1:] == ("pendiente_armado", "SEPID", "SNSP", None), order
    order_id = order[0]
    cur.execute(
        """SELECT article_code, quantity, unit_price, price_currency, discount_percent
             FROM delivery_order_items WHERE order_id = %s""",
        [order_id],
    )
    items = cur.fetchall()
    assert len(items) == 1 and str(items[0][0]).strip() == "1202015", items
    assert str(items[0][1]) == "1" and str(items[0][2]) == "40", items
    assert items[0][3] == "USD" and str(items[0][4]) == "0.00", items
    cur.execute(
        """SELECT status FROM bejerman_remito_groups
            WHERE order_ids @> %s::jsonb""",
        [json.dumps([order_id])],
    )
    statuses = [row[0] for row in cur.fetchall()]
    assert statuses == ["failed", "failed"] or sorted(statuses) == ["failed", "failed"], statuses

client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
rows = records_from_response(
    client.list_comprobantes_ventas(
        build_sales_filters("SNSP", "2026-01-01", timezone.localdate().isoformat(), "RT", "")
    )
)
assert not rows, f"SNSP ya tiene {len(rows)} RT en Bejerman"
stock = build_stock_rows(client.obtener_stock_partida("D2406282H"))
available = sum(
    float(row.get("availableQuantity") or 0)
    for row in stock
    if str(row.get("articleCode") or "").strip() == "1202015"
    and str(row.get("depositCode") or "").strip() == "VAL"
)
assert available >= 1, f"Stock insuficiente: {available}"
print("PRECHECK_OK", json.dumps({"order": "OE-00647", "oldFailedGroups": len(statuses), "rtInBejerman": 0, "availableVal": available}), flush=True)

# Única llamada de emisión. Ante cualquier resultado incierto se concilia antes de pensar en otro intento.
result = generate_bejerman_remito(
    [order_id],
    14,
    {"sellerCode": "TOM", "paymentTermCode": "30", "issueDate": timezone.localdate().isoformat()},
)
print(
    "EMISSION_RESULT",
    json.dumps(
        {
            "success": result.get("success"),
            "groupId": result.get("groupId"),
            "remitoNumber": result.get("remitoNumber"),
            "companyKey": result.get("companyKey"),
            "stockWarnings": result.get("stockWarnings"),
        },
        ensure_ascii=False,
    ),
    flush=True,
)
