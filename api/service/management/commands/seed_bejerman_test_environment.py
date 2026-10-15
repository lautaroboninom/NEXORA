import json
import os
import time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from service.bejerman_companies import require_company
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkError,
    as_string,
    build_stock_rows,
    find_bejerman_client_record,
    first_value,
    is_ok_response,
    map_article,
    records_from_response,
)


TEST_CUSTOMER_CODE = "NXT001"
TEST_CUSTOMER_SITIVA5_CODE = "NXT005"
TEST_CUSTOMER_SITIVA6_CODE = "NXT006"
TEST_ARTICLES = (
    {"code": "NXTPART", "description": "NEXORA TEST con partida", "stockByPartida": True},
    {"code": "NXTNPRT", "description": "NEXORA TEST sin partida", "stockByPartida": False},
)
REQUIRED_DEPOSITS = ("STR", "STL", "VAL")
EXPECTED_DEPOSITS = ("STR", "STL", "STC", "VAL", "DES")
SAFE_TEST_COMPANY_KEYS = {"TEST", "SEPID", "MGBIO"}
SAFE_TEST_COMPANIES = {"SEP", "MGBI"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _clean(value).upper()


def _json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _assert_test_target(company_key: str):
    company = require_company(company_key or "TEST")
    wsdl = _clean(getattr(settings, "BEJERMAN_WSDL_URL", ""))
    if "Bejerman-SDK-WS-TEST" not in wsdl:
        raise CommandError("El WSDL no apunta a Bejerman-SDK-WS-TEST; se rechaza para evitar producción.")
    if company.key not in SAFE_TEST_COMPANY_KEYS or _upper(company.bejerman_company) not in SAFE_TEST_COMPANIES:
        raise CommandError("La empresa de prueba debe resolver a SEP o MGBI mediante el WSDL TEST.")
    return company


def _response_detail(response: dict[str, Any]) -> str:
    detail = _clean(response.get("ErrorMsg")) or _clean(response.get("Fault")) or _clean(response.get("Resultado"))
    raw_data = response.get("DatosJSON")
    try:
        parsed_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except (TypeError, ValueError):
        parsed_data = raw_data
    data_detail = _clean(parsed_data) if isinstance(parsed_data, str) else ""
    if data_detail and data_detail.upper() not in {"OK", "TRUE"}:
        return data_detail
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


def _article_real_stock(client: BejermanSDKClient, article_code: str, deposit: str) -> Decimal:
    values = [
        Decimal(str(row.get("realQuantity") or 0))
        for row in build_stock_rows(client.obtener_stock_deposito(deposit))
        if _upper(row.get("articleCode")) == _upper(article_code)
        and _upper(row.get("depositCode")) == _upper(deposit)
    ]
    return sum(values, Decimal("0"))


def _valued_stock_seed_payload(article_code: str, deposit: str, quantity: Decimal) -> dict[str, Any]:
    stamp = time.time_ns()
    number = f"{80000000 + (stamp % 19999999):08d}"
    payload_quantity: int | float
    if quantity == quantity.to_integral_value():
        payload_quantity = int(quantity)
    else:
        payload_quantity = float(quantity)
    return {
        "Comprobante_FechaEmision": timezone.localdate().isoformat(),
        "Comprobante_Tipo": _clean(getattr(settings, "BEJERMAN_STOCK_ENTRY_COMPROBANTE", "ENT")) or "ENT",
        "Comprobante_Letra": "",
        "Comprobante_PtoVenta": "",
        "Comprobante_Numero": number,
        "Comprobante_Art_CodGen": article_code,
        "Comprobante_ArtPartida": "",
        "Comprobante_DescArticulo": "NEXORA TEST stock RT valorizado",
        "Comprobante_ArtCodTipo": "1",
        "Comprobante_ArtDeposito": deposit,
        "Comprobante_CantidadUM1": payload_quantity,
        "Comprobante_CantidadUM2": payload_quantity,
        "Comprobante_PrecioTotalMonLocal": 0,
        "Comprobante_IdOrigen": f"NEXORA-TEST-SEED-{number}"[:50],
        "Partida_Observaciones": "NEXORA TEST SEED",
    }


def _ensure_valued_rt_stock(
    client: BejermanSDKClient,
    *,
    apply: bool,
    minimum: Decimal,
    attempts: int = 5,
    delay: float = 1.0,
) -> tuple[Decimal, bool]:
    article_code = TEST_ARTICLES[1]["code"]
    deposit = "VAL"
    current = _article_real_stock(client, article_code, deposit)
    if current >= minimum or not apply:
        return current, False
    quantity = minimum - current
    response = client.ingresar_lista_comprobantes_stock_json(
        [_valued_stock_seed_payload(article_code, deposit, quantity)],
        numera_flex=_clean(getattr(settings, "BEJERMAN_STOCK_NUMERA_FLEX", "N")) or "N",
    )
    if not is_ok_response(response):
        raise CommandError(f"No se pudo crear stock TEST de {article_code}: {_response_detail(response)}")
    last_stock = current
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
        last_stock = _article_real_stock(client, article_code, deposit)
        if last_stock >= minimum:
            return last_stock, True
    raise CommandError(
        f"El alta de stock TEST respondió OK, pero {article_code} quedó en {last_stock} en {deposit}; "
        f"se esperaba al menos {minimum}."
    )


def _customer_payload(
    code: str = TEST_CUSTOMER_CODE,
    *,
    name: str = "NEXORA TEST CLIENTE",
    iva_code: str | None = None,
    cuit: str | None = None,
) -> dict[str, Any]:
    return {
        "Cliente_Codigo": code,
        "Cliente_RazonSocial": name,
        "Cliente_NombreFantasia": "NEXORA TEST",
        "Cliente_Direccion": "Domicilio de prueba 123",
        "Cliente_Localidad": "CABA",
        "Cliente_CodigoProvincia": os.getenv("BEJERMAN_TEST_CUSTOMER_PROVINCE", "001"),
        "Cliente_CodigoPostal": "1000",
        "Cliente_Telefono": " ",
        "Cliente_Fax": " ",
        "Cliente_EMail": "test@nexora.local",
        "Cliente_Modem": " ",
        "Cliente_SituacionIVACodigo": iva_code or os.getenv("BEJERMAN_TEST_CUSTOMER_IVA", "1"),
        "Cliente_TipoDocumentoCodigo": int(os.getenv("BEJERMAN_TEST_CUSTOMER_DOC_TYPE", "1") or "1"),
        "Cliente_CUIT": cuit or os.getenv("BEJERMAN_TEST_CUSTOMER_CUIT", "30700000008"),
        "Cliente_SituacionIIBBCodigo": os.getenv("BEJERMAN_TEST_CUSTOMER_IIBB", "1"),
        "Cliente_NroIIBB": " ",
        "Cliente_SituacionGananciasCodigo": os.getenv("BEJERMAN_TEST_CUSTOMER_GANANCIAS", "1"),
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
        "Cliente_PaisCodigo": os.getenv("BEJERMAN_TEST_CUSTOMER_COUNTRY", "ARG"),
    }


def _article_payload(spec: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    generic = spec["code"]
    description = spec["description"]
    payload = {
        "Art_Gen": True,
        "Art_CodGenerico": generic,
        "Art_CodElemento1": "",
        "Art_CodElemento2": "",
        "Art_CodElemento3": "",
        "Clase_Codigo": options["article_class"],
        "Proveedor_Codigo": options["supplier_code"],
        "Art_DescripcionGeneral": description,
        "Art_DescripcionElemento1": " ",
        "Art_DescripcionElemento2": " ",
        "Art_DescripcionElemento3": " ",
        "Art_DescricpionAdicional": description,
        "Art_DescripcionReducida": description[:10],
        "Art_TipoTasaCompras": "1",
        "Art_CodTasaIVACompras": options["vat_code"],
        "Art_ImpCreditoFiscal": "1",
        "Art_CodRubroCreditoFiscal": options["tax_credit_category"],
        "Art_TipoTasaVentas": "1",
        "Art_CodTasaIVAVentas": options["vat_code"],
        "Art_EsBienUso": False,
        "Art_Tipo": "1",
        "Art_ControlaStock": True,
        "Art_StockPorPartida": bool(spec["stockByPartida"]),
        "Art_StockPorSerie": False,
        "Art_PesoBruto": 0,
        "Art_FactorConversion": 1,
        "Art_CantidadPorEnvase": 1,
        "Art_CodMonedaPrecioBase": options["currency_code"],
        "Art_CodTipoCambioPrecioBase": options["exchange_type"],
        "Art_PrecioBase": 0,
        "Art_CodMonedaPrecioProveedor": options["currency_code"],
        "Art_CodTipoCambioProveedor": options["exchange_type"],
        "Art_PrecioListaProveedor": 0,
        "Art_Bonif1Proveedor": 0,
        "Art_Bonif2Proveedor": 0,
        "Art_Bonif3Proveedor": 0,
        "Art_ParticipaCircuitoStock": True,
        "Art_ParticipaCircuitoCompras": True,
        "Art_ParticipaCircuitoProduccion": True,
        "Art_ParticipaCircuitoVentas": True,
        "Art_IncluirEnListaPrecios": True,
        "Art_LoteMultiplo": 1,
        "Art_LoteMinimo": 1,
        "Art_LoteMaximo": 999999,
        "Art_CodDeposito": "STR",
        "Art_CodDefinible1": options["definible1_code"],
        "Art_CodDefinible2": options["definible2_code"],
        "Art_TipoConsumo": "N",
        "Art_Clasificacion": "A",
        "Art_AplicaLoteAutomatico": "N",
        "Art_Foto": " ",
        "Art_RegistraAlicuota": False,
        "Art_CodigoUsuario": "ADMIN",
        "Art_ProducSimultaneidad": False,
    }
    if spec["stockByPartida"]:
        payload["Art_MascaraPartida"] = "A"
    return payload


class Command(BaseCommand):
    help = "Verifica o crea el seed mínimo de Bejerman TEST para remitos NEXORA."

    def add_arguments(self, parser):
        parser.add_argument("--company-key", default="TEST")
        parser.add_argument("--apply", action="store_true", help="Crea cliente/artículos faltantes en Bejerman TEST.")
        parser.add_argument("--article-class", default=os.getenv("BEJERMAN_TEST_ARTICLE_CLASS", "UNI"))
        parser.add_argument("--vat-code", default=os.getenv("BEJERMAN_TEST_VAT_CODE", "01"))
        parser.add_argument("--currency-code", default=os.getenv("BEJERMAN_TEST_CURRENCY_CODE", "1"))
        parser.add_argument("--exchange-type", default=os.getenv("BEJERMAN_TEST_EXCHANGE_TYPE", "UNI"))
        parser.add_argument("--supplier-code", default=os.getenv("BEJERMAN_TEST_SUPPLIER_CODE", "MGBIO"))
        parser.add_argument(
            "--tax-credit-category",
            default=os.getenv("BEJERMAN_TEST_TAX_CREDIT_CATEGORY", "7"),
        )
        parser.add_argument("--definible1-code", default=os.getenv("BEJERMAN_TEST_DEFINIBLE1_CODE", "12"))
        parser.add_argument("--definible2-code", default=os.getenv("BEJERMAN_TEST_DEFINIBLE2_CODE", "1402"))
        parser.add_argument(
            "--minimum-valued-stock",
            type=int,
            default=int(os.getenv("BEJERMAN_TEST_VAL_STOCK_MINIMUM", "10") or "10"),
            help="Stock mínimo del artículo NXTNPRT en VAL para el smoke RT valorizado.",
        )

    def handle(self, *args, **options):
        company = _assert_test_target(options["company_key"])
        client = BejermanSDKClient(company_key=company.key, allow_system_credentials=True)
        self.stdout.write(f"Objetivo seguro TEST: {company.key}->{company.bejerman_company}")

        try:
            deposits = _existing_deposit_codes(client)
            missing_deposits = [code for code in REQUIRED_DEPOSITS if code not in deposits]
            if missing_deposits:
                raise CommandError("Faltan depósitos operativos enumerados por el SDK TEST: " + ", ".join(missing_deposits))
            self.stdout.write("Depósitos operativos OK: " + ", ".join(REQUIRED_DEPOSITS))
            not_listed = [code for code in EXPECTED_DEPOSITS if code not in deposits]
            if not_listed:
                self.stdout.write(
                    "Depósitos no enumerados por el SDK (pueden existir sin movimientos visibles): "
                    + ", ".join(not_listed)
                )

            customers = client.list_clientes()
            customer_specs = (
                (TEST_CUSTOMER_CODE, _customer_payload()),
                (
                    TEST_CUSTOMER_SITIVA5_CODE,
                    _customer_payload(
                        TEST_CUSTOMER_SITIVA5_CODE,
                        name="NEXORA TEST CLIENTE EXENTO",
                        iva_code="5",
                        cuit="20000000028",
                    ),
                ),
                (
                    TEST_CUSTOMER_SITIVA6_CODE,
                    _customer_payload(
                        TEST_CUSTOMER_SITIVA6_CODE,
                        name="NEXORA TEST CLIENTE SITUACIÓN IVA 6",
                        iva_code=os.getenv(
                            "BEJERMAN_TEST_CUSTOMER_SITIVA6_IVA_CODE",
                            os.getenv("BEJERMAN_TEST_CUSTOMER_SITIVA6_CODE", "6"),
                        ),
                        cuit=os.getenv("BEJERMAN_TEST_CUSTOMER_SITIVA6_CUIT", "20000000001"),
                    ),
                ),
            )
            for customer_code, customer_payload in customer_specs:
                customer_exists = bool(find_bejerman_client_record(customers, customer_code))
                if customer_exists:
                    self.stdout.write(f"Cliente OK: {customer_code}")
                elif options["apply"]:
                    response = client.execute("TABLAS", "AltaCliente", params_json=[_json_param(customer_payload)])
                    if not is_ok_response(response):
                        raise CommandError(f"No se pudo crear cliente {customer_code}: {_response_detail(response)}")
                    customers = client.list_clientes()
                    if not find_bejerman_client_record(customers, customer_code):
                        raise CommandError(
                            f"El alta del cliente {customer_code} no quedó aplicada: {_response_detail(response)}"
                        )
                    self.stdout.write(f"Cliente creado: {customer_code}")
                else:
                    self.stdout.write(f"Cliente faltante: {customer_code} (usar --apply para crear)")

            article_options = {
                "article_class": options["article_class"],
                "vat_code": options["vat_code"],
                "currency_code": options["currency_code"],
                "exchange_type": options["exchange_type"],
                "supplier_code": options["supplier_code"],
                "tax_credit_category": options["tax_credit_category"],
                "definible1_code": options["definible1_code"],
                "definible2_code": options["definible2_code"],
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
                for attempt in range(3):
                    if attempt:
                        time.sleep(1)
                    if code in _existing_article_codes(client):
                        existing_articles.add(code)
                        break
                if code not in existing_articles:
                    raise CommandError(f"El alta del artículo {code} no quedó aplicada: {_response_detail(response)}")
                self.stdout.write(f"Artículo creado: {code}")

            minimum_stock = Decimal(str(max(1, int(options["minimum_valued_stock"]))))
            stock, created = _ensure_valued_rt_stock(
                client,
                apply=bool(options["apply"]),
                minimum=minimum_stock,
            )
            if stock >= minimum_stock:
                action = "creado" if created else "OK"
                self.stdout.write(f"Stock {action}: {TEST_ARTICLES[1]['code']}={stock} en VAL")
            else:
                self.stdout.write(
                    f"Stock insuficiente: {TEST_ARTICLES[1]['code']}={stock} en VAL "
                    f"(usar --apply para completar a {minimum_stock})"
                )

        except BejermanSdkError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("OK: seed mínimo Bejerman TEST verificado")
