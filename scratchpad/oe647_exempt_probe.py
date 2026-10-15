"""Reproducción puntual y controlada contra Bejerman TEST."""

import json
import os
import sys
import argparse
from datetime import date

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
sys.path.insert(0, "api")

import django

django.setup()

from service.bejerman_sdk import (  # noqa: E402
    BejermanSDKClient,
    build_delivery_remito_comprobante,
    delivery_remito_config,
    parse_remito_response,
    resolve_customer_document_fields,
)

if "Bejerman-SDK-WS-TEST" not in os.environ.get("BEJERMAN_WSDL_URL", ""):
    raise RuntimeError("Solo Bejerman TEST")

client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
fields = resolve_customer_document_fields(client, "NXT005")
assert fields["Cliente_SitIVA"] == "5"
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=("original", "header-net", "gross-unit", "discount", "usd"), required=True)
args = parser.parse_args()
reference = f"NXTEST-OE647-EX-{args.mode.upper()}-20261014"
request = {
    "groupId": reference,
    "companyKey": "SEPID",
    "issueDate": date.today().isoformat(),
    "customerCode": "NXT005",
    "customerName": "NEXORA TEST CLIENTE EXENTO",
    "sellerCode": "ADM",
    "paymentTermCode": "30",
    "priceCurrency": "ARS",
    "orders": [
        {
            "orderNumber": reference,
            "deliveryType": "sale",
            "items": [
                {
                    "articleCode": "NXTNPRT",
                    "articleName": "NEXORA TEST sin partida",
                    "quantity": 1,
                    "unitPrice": 40,
                    "priceCurrency": "ARS",
                    "discountPercent": 0,
                    "stockDepositCode": "VAL",
                    "articleRequiresPartida": False,
                }
            ],
        }
    ],
}
profile = {
    "type": "RT",
    "operation": "MC",
    "deposit": "VAL",
    "pointOfSale": "00004",
    "companyKey": "SEPID",
}
if args.mode == "discount":
    request["orders"][0]["items"][0]["quantity"] = 2
    request["orders"][0]["items"][0]["discountPercent"] = 10
elif args.mode == "usd":
    request["priceCurrency"] = "USD"
    request["exchangeRate"] = 1400
    request["orders"][0]["items"][0]["priceCurrency"] = "USD"
built = build_delivery_remito_comprobante(
    request,
    delivery_remito_config(profile),
    fields,
    {"NXTNPRT": {"salesVatType": "1", "salesVatRate": 21}},
)
document = built["comprobante"]
article = next(item for item in document["Comprobante_Items"] if item.get("Item_Tipo") == "A")
if args.mode == "header-net":
    document["Comprobante_ImporteTotal"] = article["Item_ImporteTotal"]
elif args.mode in ("gross-unit", "discount"):
    article["Item_PrecioUnitario"] = 48.4
    article["Item_Importe"] = article["Item_ImporteTotal"] + article["Item_ImporteIVAInscrip"]
print(
    "TEST_PREVIEW",
    json.dumps(
        {
            "sitiva": fields["Cliente_SitIVA"],
            "total": document["Comprobante_ImporteTotal"],
            "net": article["Item_ImporteTotal"],
            "iva": article["Item_ImporteIVAInscrip"],
            "rate": article["Item_TasaIVAInscrip"],
        }
    ),
    flush=True,
)
try:
    response = client.ingresar_comprobante_ventas_json(
        document,
        circuito="VENTAS",
        operacion="IngresarComprobanteJSON",
        numera_flex="S",
        emite_reg="E",
    )
    parsed = parse_remito_response(response)
    saved = parsed.get("raw") or {}
    saved_article = next((item for item in saved.get("Comprobante_Items") or [] if item.get("Item_Tipo") == "A"), {})
    print("TEST_RESPONSE", json.dumps({
        "remito": parsed.get("remitoNumber"),
        "header": saved.get("Comprobante_ImporteTotal"),
        "unit": saved_article.get("Item_PrecioUnitario"),
        "net": saved_article.get("Item_ImporteTotal"),
        "iva": saved_article.get("Item_ImporteIVAInscrip"),
        "rate": saved_article.get("Item_TasaIVAInscrip"),
        "importe": saved_article.get("Item_Importe"),
    }, ensure_ascii=False))
except Exception as exc:
    print("TEST_REJECTION", type(exc).__name__, str(exc)[:600])
