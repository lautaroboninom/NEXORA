"""Auditoría de solo lectura de clientes monotributistas en NEXORA y Bejerman."""

import json
import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings_prod")

import django

django.setup()

from django.db import connection, transaction  # noqa: E402
from service.bejerman_sdk import BejermanSDKClient, first_value, records_from_response  # noqa: E402


with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(
            """SELECT COALESCE(bejerman_company_key, 'sin empresa'), COUNT(*)
                 FROM customers WHERE TRIM(bejerman_condicion_iva) = '6'
                 GROUP BY bejerman_company_key"""
        )
        print("NEXORA_CACHE", json.dumps(cursor.fetchall(), default=str), flush=True)
        cursor.execute(
            """SELECT g.company_key, g.status, COUNT(*)
                 FROM bejerman_remito_groups g
                WHERE g.comprobante_tipo = 'RT'
                  AND g.response_summary->'raw'->>'Cliente_SitIVA' = '6'
                GROUP BY g.company_key, g.status"""
        )
        print("NEXORA_RT_MONOTRIBUTO", json.dumps(cursor.fetchall(), default=str), flush=True)

for company_key in ("SEPID", "MGBIO"):
    client = BejermanSDKClient(company_key=company_key, allow_system_credentials=True)
    customers = records_from_response(client.list_clientes())
    monotributistas = [
        {
            "code": str(first_value(row, ("Cliente_Codigo", "Codigo")) or "").strip(),
            "name": str(first_value(row, ("Cliente_RazonSocial", "RazonSocial")) or "").strip(),
        }
        for row in customers
        if str(first_value(row, ("Cliente_SitIVA", "Cliente_SituacionIVACodigo")) or "").strip() == "6"
    ]
    print(
        "BEJERMAN_MONOTRIBUTO",
        json.dumps({"company": company_key, "count": len(monotributistas), "examples": monotributistas[:5]}, ensure_ascii=False),
        flush=True,
    )
