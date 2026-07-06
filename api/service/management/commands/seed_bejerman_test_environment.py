import json
import os
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from service.bejerman_companies import require_company
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkError,
    as_string,
    find_bejerman_client_record,
    first_value,
    is_ok_response,
    map_article,
    records_from_response,
)


TEST_CUSTOMER_CODE = "NXTESTCLI"
TEST_ARTICLES = (
    {"code": "NXTEST-PART", "description": "NEXORA TEST con partida", "stockByPartida": True},
    {"code": "NXTEST-NOPART", "description": "NEXORA TEST sin partida", "stockByPartida": False},
)
REQUIRED_DEPOSITS = ("STR", "STL", "STC", "VAL", "DES")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _clean(value).upper()


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _assert_test_target(company_key: str):
    company = require_company(company_key or "TEST")
    if not company.is_test:
        raise CommandError("Este comando solo admite company-key TEST.")
    wsdl = _clean(getattr(settings, "BEJERMAN_WSDL_URL", ""))
    if "Bejerman-SDK-WS-TEST" not in wsdl:
        raise CommandError("El WSDL no apunta a Bejerman-SDK-WS-TEST; se rechaza para evitar producción.")
    if _upper(company.bejerman_company) != "MODE":
        raise CommandError("La empresa TEST debe resolver a MODE. Revisá BEJERMAN_COMPANY_TEST.")
    return company


def _response_detail(response: dict[str, Any]) -> str:
    detail = _clean(response.get("ErrorMsg")) or _clean(response.get("Fault")) or _clean(response.get("Resultado"))
    return detail or json.dumps(response, ensure_ascii=False, default=str)[:500]


def _article_code_from_item(item: dict[str, Any]) -> str:
    article = map_article(item)
    if article and article.get("code"):
        return _upper(article.get("code"))
    generic = as_string(first_value(item, ("Art_CodGenerico", "ArtCodGenerico", "CodigoGenerico")))
    elements = [
        as_string(first_value(item, ("Art_CodElemento1", "ArtCodElemento1", "CodigoElemento1"))),
        as_string(first_value(item, ("Art_CodElemento2", "ArtCodElemento2", "CodigoElemento2"))),
        as_string(first_value(item, ("Art_CodElemento3", "ArtCodElemento3", "CodigoElemento3"))),
    ]
    return _upper("-".join([generic, *[element for element in elements if element]]))


def _existing_article_codes(client: BejermanSDKClient) -> set[str]:
    return {
        code
        for code in (_article_code_from_item(item) for item in records_from_response(client.list_articulos()))
        if code
    }


def _deposit_code_from_item(item: dict[str, Any]) -> str:
    return _upper(
        first_value(
            item,
            (
                "Deposito_Codigo",
                "DepositoCodigo",
                "Deposito",
                "Codigo",
                "dep_Cod",
                "Art_CodDeposito",
                "stkdep_Cod",
            ),
        )
    )


def _existing_deposit_codes(client: BejermanSDKClient) -> set[str]:
    return {code for code in (_deposit_code_from_item(item) for item in records_from_response(client.obtener_depositos())) if code}


def _customer_payload() -> dict[str, Any]:
    return {
        "Cliente_Codigo": TEST_CUSTOMER_CODE,
        "Cliente_RazonSocial": "NEXORA TEST CLIENTE",
        "Cliente_NombreFantasia": "NEXORA TEST",
        "Cliente_Direccion": "Domicilio de prueba 123",
        "Cliente_Localidad": "CABA",
        "Cliente_CodigoProvincia": os.getenv("BEJERMAN_TEST_CUSTOMER_PROVINCE", "02"),
        "Cliente_CodigoPostal": "1000",
        "Cliente_Telefono": " ",
        "Cliente_Fax": " ",
        "Cliente_EMail": "test@nexora.local",
        "Cliente_Modem": " ",
        "Cliente_SituacionIVACodigo": os.getenv("BEJERMAN_TEST_CUSTOMER_IVA", "RI"),
        "Cliente_TipoDocumentoCodigo": int(os.getenv("BEJERMAN_TEST_CUSTOMER_DOC_TYPE", "80") or "80"),
        "Cliente_CUIT": os.getenv("BEJERMAN_TEST_CUSTOMER_CUIT", "30700000000"),
        "Cliente_SituacionIIBBCodigo": os.getenv("BEJERMAN_TEST_CUSTOMER_IIBB", "NI"),
        "Cliente_NroIIBB": " ",
        "Cliente_SituacionGananciasCodigo": os.getenv("BEJERMAN_TEST_CUSTOMER_GANANCIAS", "NI"),
        "Cliente_Habilitado": True,
        "Cliente_VendedorCodigo": os.getenv("BEJERMAN_TEST_SELLER_CODE", "ADM"),
        "Cliente_CondicionVentaCodigo": os.getenv("BEJERMAN_TEST_PAYMENT_TERM", "30"),
        "Cliente_Contacto": " ",
        "Cliente_ResponsablePago": " ",
        "Cliente_LugarPago": " ",
        "Cliente_HorarioPago": " ",
        "Cliente_PaginaWeb": " ",
        "Cliente_Password": " ",
        "Cliente_Activo": "S",
        "Cliente_FotocopiaCUIT": False,
    }


def _article_payload(spec: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    generic, _, element = spec["code"].partition("-")
    element = element or "GEN"
    description = spec["description"]
    return {
        "Art_Gen": False,
        "Art_CodGenerico": generic,
        "Art_CodElemento1": element,
        "Art_CodElemento2": "",
        "Art_CodElemento3": "",
        "Clase_Codigo": options["article_class"],
        "Art_DescripcionGeneral": description,
        "Art_DescripcionElemento1": element,
        "Art_DescripcionElemento2": " ",
        "Art_DescripcionElemento3": " ",
        "Art_DescricpionAdicional": description,
        "Art_DescripcionReducida": description[:20],
        "Art_TipoTasaCompras": "1",
        "Art_CodTasaIVACompras": options["vat_code"],
        "Art_ImpCreditoFiscal": "N",
        "Art_TipoTasaVentas": "1",
        "Art_CodTasaIVAVentas": options["vat_code"],
        "Art_EsBienUso": False,
        "Art_Tipo": "A",
        "Art_ControlaStock": True,
        "Art_StockPorPartida": bool(spec["stockByPartida"]),
        "Art_StockPorSerie": False,
        "Art_PesoBruto": 0,
        "Art_FactorConversion": 1,
        "Art_CantidadPorEnvase": 1,
        "Art_CodMonedaPrecioBase": options["currency_code"],
        "Art_CodTipoCambioPrecioBase": options["exchange_type"],
        "Art_PrecioBase": 0,
        "Art_ParticipaCircuitoStock": True,
        "Art_ParticipaCircuitoCompras": True,
        "Art_ParticipaCircuitoProduccion": False,
        "Art_ParticipaCircuitoVentas": True,
        "Art_IncluirEnListaPrecios": True,
        "Art_LoteMultiplo": 1,
        "Art_LoteMinimo": 1,
        "Art_LoteMaximo": 999999,
        "Art_CodDeposito": "STR",
        "Art_TipoConsumo": "N",
        "Art_Clasificacion": " ",
        "Art_Foto": " ",
        "Art_RegistraAlicuota": False,
        "Art_CodigoUsuario": "ADMIN",
        "Art_ProducSimultaneidad": False,
    }


class Command(BaseCommand):
    help = "Verifica o crea el seed mínimo de Bejerman TEST para remitos NEXORA."

    def add_arguments(self, parser):
        parser.add_argument("--company-key", default="TEST")
        parser.add_argument("--apply", action="store_true", help="Crea cliente/artículos faltantes en Bejerman TEST.")
        parser.add_argument("--article-class", default=os.getenv("BEJERMAN_TEST_ARTICLE_CLASS", "EQ"))
        parser.add_argument("--vat-code", default=os.getenv("BEJERMAN_TEST_VAT_CODE", "01"))
        parser.add_argument("--currency-code", default=os.getenv("BEJERMAN_TEST_CURRENCY_CODE", "1"))
        parser.add_argument("--exchange-type", default=os.getenv("BEJERMAN_TEST_EXCHANGE_TYPE", ""))

    def handle(self, *args, **options):
        company = _assert_test_target(options["company_key"])
        client = BejermanSDKClient(company_key=company.key, allow_system_credentials=True)
        self.stdout.write(f"Objetivo seguro: {company.key}->{company.bejerman_company}")

        try:
            deposits = _existing_deposit_codes(client)
            missing_deposits = [code for code in REQUIRED_DEPOSITS if code not in deposits]
            if missing_deposits:
                raise CommandError("Faltan depósitos en Bejerman TEST: " + ", ".join(missing_deposits))

            customers = client.list_clientes()
            customer_exists = bool(find_bejerman_client_record(customers, TEST_CUSTOMER_CODE))
            if customer_exists:
                self.stdout.write(f"Cliente OK: {TEST_CUSTOMER_CODE}")
            elif options["apply"]:
                response = client.execute("TABLAS", "AltaCliente", params_json=[_json_param(_customer_payload())])
                if not is_ok_response(response):
                    raise CommandError(f"No se pudo crear cliente {TEST_CUSTOMER_CODE}: {_response_detail(response)}")
                self.stdout.write(f"Cliente creado: {TEST_CUSTOMER_CODE}")
            else:
                self.stdout.write(f"Cliente faltante: {TEST_CUSTOMER_CODE} (usar --apply para crear)")

            article_options = {
                "article_class": options["article_class"],
                "vat_code": options["vat_code"],
                "currency_code": options["currency_code"],
                "exchange_type": options["exchange_type"],
            }
            existing_articles = _existing_article_codes(client)
            for spec in TEST_ARTICLES:
                code = _upper(spec["code"])
                if code in existing_articles:
                    self.stdout.write(f"Artículo OK: {code}")
                    continue
                if not options["apply"]:
                    self.stdout.write(f"Artículo faltante: {code} (usar --apply para crear)")
                    continue
                response = client.execute(
                    "TABLAS",
                    "AltaArticulo",
                    params_json=[_json_param(_article_payload(spec, article_options))],
                )
                if not is_ok_response(response):
                    raise CommandError(f"No se pudo crear artículo {code}: {_response_detail(response)}")
                self.stdout.write(f"Artículo creado: {code}")

        except BejermanSdkError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("OK: seed mínimo Bejerman TEST verificado")
