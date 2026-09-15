"""Compara el catálogo SDK con el maestro SQL, sin emitir comprobantes.

Sólo registra una sesión SDK y ejecuta ObtenerArticulos/SELECT. No utiliza
la configuración Django de producción ni consulta la base de NEXORA.
Las credenciales se leen del archivo ya usado por samtronic_sql y no se imprimen.
"""
import json
import sys
from pathlib import Path

from samtronic_sql import Samtronic, load_env

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from django.conf import settings


def main():
    env = load_env()
    settings.configure(
        SECRET_KEY="read-only-catalog-diagnostic",
        BEJERMAN_WSDL_URL=env.get("BEJERMAN_WSDL_URL", ""),
        BEJERMAN_BRANCH=env.get("BEJERMAN_BRANCH", ""),
        BEJERMAN_REQUEST_TIMEOUT=45,
    )
    from service.bejerman_sdk import (
        ARTICLE_NAME_FIELDS, BejermanSDKClient, first_value, map_article,
        parse_soap_response, records_from_response,
    )
    with Samtronic() as server:
        for database, company in (("SBDASEP", "SEP"), ("SBDAMGBI", "MGBI")):
            sql_rows = server.query("""
                SELECT LTRIM(RTRIM(art_CodGen)) AS codigo,
                       CONVERT(varchar(max),CONVERT(varbinary(max),CONVERT(nvarchar(max),art_DescGen)),2) AS texto_hex
                  FROM dbo.Articulos
            """, database=database)
            master = {r["codigo"]: bytes.fromhex(r["texto_hex"] or "").decode("utf-16-le") for r in sql_rows}
            client = BejermanSDKClient(
                bejerman_company=company,
                bejerman_username=env.get("BEJERMAN_USER"),
                bejerman_password=env.get("BEJERMAN_PASSWORD"),
                bejerman_workstation=env.get("BEJERMAN_WORKSTATION"),
            )
            transport = []

            def inspect_response(response, *args, **kwargs):
                # Compara el parser aplicado al texto HTTP y a los bytes XML.
                # No imprime el token, el sobre SOAP ni el catálogo completo.
                text_parsed = parse_soap_response(response.text)
                bytes_parsed = parse_soap_response(response.content)
                transport.append({
                    "content_type": response.headers.get("Content-Type"),
                    "encoding": response.encoding,
                    "datos_json_iguales_desde_bytes_y_texto": text_parsed.get("DatosJSON") == bytes_parsed.get("DatosJSON"),
                })

            client._http.hooks["response"].append(inspect_response)
            try:
                response = client.list_articulos([], 5000)
            finally:
                client.close()
            differences = []
            matched = 0
            seen = set()
            for record in records_from_response(response):
                article = map_article(record)
                if not article:
                    continue
                code = article["code"]
                description = first_value(record, ARTICLE_NAME_FIELDS)
                seen.add(code)
                if code in master and description == master[code]:
                    matched += 1
                else:
                    differences.append({"codigo": code, "sdk": description, "sql": master.get(code)})
            print(json.dumps({
                "base": database, "articulos_sql": len(master), "articulos_sdk": len(seen),
                "coincidencias_exactas": matched, "diferencias": differences,
                "codigos_sql_ausentes_sdk": sorted(master.keys() - seen), "transporte": transport,
            }, ensure_ascii=True))


if __name__ == "__main__":
    main()
