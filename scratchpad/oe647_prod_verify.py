"""Verificación de solo lectura del RT real de OE-00647."""

import json
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings_prod")
sys.path.insert(0, "/app")

import django

django.setup()

from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402
from service.bejerman_sdk import (  # noqa: E402
    BejermanSDKClient,
    bejerman_filter,
    build_sales_filters,
    build_stock_rows,
    comprobante_id_of,
    records_from_response,
)

GROUP_ID = "brg-429cab1f-9160-403d-bdc6-cf0ed437d436"
NUMBER = "00005477"
assert "Bejerman-SDK-WS-TEST" not in os.environ.get("BEJERMAN_WSDL_URL", "")

with connection.cursor() as cur:
    cur.execute(
        """SELECT order_number, status, company_key, bejerman_customer_code,
                  remito_number, bejerman_remito_group_id
             FROM delivery_orders WHERE order_number = %s""",
        ["OE-00647"],
    )
    order = cur.fetchone()
    assert order and order[0] == "OE-00647" and order[2:4] == ("SEPID", "SNSP"), order
    assert NUMBER in str(order[4]) and order[5] == GROUP_ID, order
    cur.execute(
        """SELECT status, remito_number, comprobante_tipo, comprobante_pto_venta,
                  comprobante_numero, attempt_count, response_summary
             FROM bejerman_remito_groups WHERE id = %s""",
        [GROUP_ID],
    )
    group = cur.fetchone()
    assert group and group[0] == "generated" and group[2:5] == ("RT", "00004", NUMBER), group
    summary = json.loads(group[6]) if isinstance(group[6], str) else (group[6] if isinstance(group[6], dict) else {})
    raw = summary.get("raw") if isinstance(summary.get("raw"), dict) else {}
    print("GROUP_DETAIL", json.dumps({"order": order, "group": group[:6], "summaryKeys": list(summary.keys()), "rawType": type(raw).__name__, "responseId": raw.get("Comprobante_ID")}, default=str), flush=True)
    cur.execute(
        """SELECT state, octet_length(pdf_payload), pdf_payload
             FROM bejerman_pdf_requests WHERE remito_group_id = %s""",
        [GROUP_ID],
    )
    pdf_rows = cur.fetchall()

client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
padded_headers = records_from_response(
    client.list_comprobantes_ventas(
        build_sales_filters("  SNSP", "2026-01-01", timezone.localdate().isoformat(), "RT", "")
    )
)
print("PADDED_CUSTOMER_RT", len(padded_headers), flush=True)
headers = records_from_response(
    client.list_comprobantes_ventas(
        build_sales_filters("SNSP", "2026-01-01", timezone.localdate().isoformat(), "RT", "")
    )
)
if not headers:
    headers = records_from_response(
        client.list_comprobantes_ventas(
            build_sales_filters("SNSP", "2026-01-01", timezone.localdate().isoformat(), "RT", ""),
            en_mon_ext="S",
        )
    )
if not headers:
    headers = records_from_response(
        client.list_comprobantes_ventas(
            [bejerman_filter("Comprobante_Tipo", "IGUAL", "RT"), bejerman_filter("Comprobante_Numero", "IGUAL", NUMBER)]
        )
    )
print("SDK_LIST_COUNT", len(headers), flush=True)
header = headers[0] if len(headers) == 1 else raw
assert str(header.get("Comprobante_Numero") or "").strip() == NUMBER, header.get("Comprobante_Numero")
detail_rows = records_from_response(client.detalle_comprobante_ventas(comprobante_id_of(header)))
assert len(detail_rows) == 1
detail = detail_rows[0]
articles = [item for item in detail.get("Comprobante_Items") or [] if item.get("Item_Tipo") == "A"]
assert len(articles) == 1, len(articles)
item = articles[0]
assert str(detail.get("Cliente_Codigo") or "").strip() == "SNSP"
assert str(detail.get("Cliente_SitIVA") or "").strip() == "5"
assert str(item.get("Item_CodigoArticulo") or "").strip() == "1202015"
assert str(item.get("Item_Partida") or "").strip() == "D2406282H"
assert str(item.get("Item_Deposito") or "").strip() == "VAL"
values = {
    "quantity": float(item.get("Item_CantidadUM1") or 0),
    "unit": float(item.get("Item_PrecioUnitario") or 0),
    "net": float(item.get("Item_ImporteTotal") or 0),
    "vat": float(item.get("Item_ImporteIVAInscrip") or 0),
    "rate": float(item.get("Item_TasaIVAInscrip") or 0),
    "total": float(detail.get("Comprobante_ImporteTotal") or 0),
}
assert values == {"quantity": 1, "unit": 48.4, "net": 40, "vat": 8.4, "rate": 21, "total": 48.4}, values
stock = build_stock_rows(client.obtener_stock_partida("D2406282H"))
val_stock = sum(
    float(row.get("realQuantity") or 0)
    for row in stock
    if str(row.get("articleCode") or "").strip() == "1202015"
    and str(row.get("depositCode") or "").strip() == "VAL"
)
assert val_stock == 10, val_stock

pdf_status = []
for state, size, payload in pdf_rows:
    extracted = ""
    if payload:
        import pymupdf

        with pymupdf.open(stream=bytes(payload), filetype="pdf") as pdf:
            extracted = " ".join(page.get_text() for page in pdf)
    pdf_status.append({"state": state, "bytes": size, "exento": "EXENTO" in extracted.upper()})
assert len(pdf_status) == 1 and pdf_status[0]["exento"], pdf_status
print(
    "VERIFIED",
    json.dumps(
        {
            "order": order,
            "group": group[:6],
            "rtCountForCustomer": len(headers),
            "article": "1202015",
            "partida": "D2406282H",
            "amounts": values,
            "stockVal": val_stock,
            "pdf": pdf_status,
        },
        default=str,
        ensure_ascii=False,
    ),
)
