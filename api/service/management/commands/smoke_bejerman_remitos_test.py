import json
import time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from service.bejerman_companies import require_company
from service.bejerman_ris import _apply_registered_document, _send_service_ingress_comprobante
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkError,
    as_string,
    bejerman_filter,
    build_delivery_remito_comprobante,
    build_sales_filters,
    build_service_ingress_comprobante,
    build_stock_rows,
    comprobante_id_of,
    delivery_remito_config,
    first_value,
    format_remito_number,
    is_ok_response,
    parse_remito_response,
    records_from_response,
    resolve_customer_document_fields,
)
from service.management.commands.seed_bejerman_test_environment import (
    SAFE_TEST_COMPANIES,
    SAFE_TEST_COMPANY_KEYS,
    TEST_ARTICLES,
    TEST_CUSTOMER_CODE,
    TEST_CUSTOMER_SITIVA6_CODE,
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _clean(value).upper()


def _assert_test_target(company_key: str):
    company = require_company(company_key or "TEST")
    wsdl = _clean(getattr(settings, "BEJERMAN_WSDL_URL", ""))
    if "Bejerman-SDK-WS-TEST" not in wsdl:
        raise CommandError("El WSDL no apunta a Bejerman-SDK-WS-TEST; se rechaza para evitar producción.")
    if company.key not in SAFE_TEST_COMPANY_KEYS or _upper(company.bejerman_company) not in SAFE_TEST_COMPANIES:
        raise CommandError("La empresa de prueba debe resolver a SEP o MGBI mediante el WSDL TEST.")
    return company


def _rt_point(company) -> str:
    return "00007" if company.key == "MGBIO" else "00004"


def _scenarios_for_company(company, requested: str) -> tuple[str, ...]:
    supported = ("rt-valued",) if company.key == "MGBIO" else ("emission", "manual", "rt-valued")
    if requested == "all":
        return supported
    if requested not in supported:
        raise CommandError(f"El escenario {requested} no está definido para {company.key} en Bejerman TEST.")
    return (requested,)


def _stock_rows_for_partida(response: dict[str, Any], partida: str, deposit: str) -> list[dict[str, Any]]:
    partida_norm = _upper(partida)
    deposit_norm = _upper(deposit)
    rows = []
    for record in records_from_response(response):
        row_partida = _upper(first_value(record, ("Art_Partida", "ArtPartida", "Comprobante_ArtPartida", "Partida", "stp_Partida")))
        row_deposit = _upper(first_value(record, ("Art_CodDeposito", "ArtCodDeposito", "Comprobante_ArtDeposito", "Deposito", "stkdep_Cod")))
        if row_partida == partida_norm and row_deposit == deposit_norm:
            rows.append(record)
    return rows


def _verify_stock(client: BejermanSDKClient, partida: str, deposit: str, *, attempts: int = 5, delay: float = 2.0):
    last_response = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
        last_response = client.obtener_stock_partida(partida)
        rows = _stock_rows_for_partida(last_response, partida, deposit)
        if rows:
            return rows
    raise CommandError(
        f"No se encontró stock de partida {partida} en depósito {deposit}. "
        f"Última respuesta: {json.dumps(last_response or {}, ensure_ascii=False, default=str)[:500]}"
    )


def _response_detail(response: dict[str, Any]) -> str:
    return _clean(response.get("ErrorMsg")) or _clean(response.get("Fault")) or _clean(response.get("Resultado"))


def _base_payload(company, serial: str, *, update_stock: bool, point: str, ingreso_id: int) -> dict[str, Any]:
    return {
        "requestId": f"nexora-smoke-{serial}",
        "ingresoId": ingreso_id,
        "ingresoIds": [ingreso_id],
        "companyKey": company.key,
        "companyLabel": company.label,
        "bejermanCompany": company.bejerman_company,
        "issueDate": timezone.localdate().isoformat(),
        "customerCode": TEST_CUSTOMER_CODE,
        "customerName": "NEXORA TEST CLIENTE",
        "sellerCode": _clean(getattr(settings, "BEJERMAN_RIS_SELLER_CODE", "ADM")) or "ADM",
        "paymentTermCode": _clean(getattr(settings, "BEJERMAN_RIS_PAYMENT_TERM", "30")) or "30",
        "documentProfile": {
            "key": "ris",
            "type": "RIS",
            "label": "RIS",
            "letter": "R",
            "reason": "Smoke Bejerman TEST.",
            "deposit": "STR",
            "operation": "REP",
            "pointOfSale": point,
            "updateStock": update_stock,
        },
        "equipment": {
            "ingresoId": ingreso_id,
            "articleCode": TEST_ARTICLES[0]["code"],
            "articleName": TEST_ARTICLES[0]["description"],
            "serial": serial,
            "equipmentType": "CPAP",
            "brand": "NEXORA",
            "model": "TEST",
            "repairReason": "Smoke Bejerman TEST",
        },
    }


def _ensure_ok(response: dict[str, Any], action: str):
    if not is_ok_response(response):
        raise CommandError(f"{action} falló: {_response_detail(response)}")


def _find_free_manual_number(client: BejermanSDKClient, explicit: str | None = None) -> str:
    if explicit:
        return str(int(explicit)).zfill(8)
    seed = 90000000 + (int(time.time()) % 9000000)
    for offset in range(50):
        candidate = str(seed + offset).zfill(8)
        response = client.list_comprobantes_ventas(
            [
                bejerman_filter("Comprobante_Tipo", "IGUAL", "RIS"),
                bejerman_filter("Comprobante_PtoVenta", "IGUAL", "00001"),
                bejerman_filter("Comprobante_Numero", "IGUAL", candidate),
            ]
        )
        _ensure_ok(response, f"Consulta de número manual libre {candidate}")
        if not records_from_response(response):
            return candidate
    raise CommandError("No se encontró número manual libre para RIS 00001 en el rango reservado.")


def _run_emission(client: BejermanSDKClient, company, serial: str):
    payload = _base_payload(
        company,
        serial,
        update_stock=True,
        point="00004",
        ingreso_id=int(time.time()) % 100000,
    )
    customer_fields = resolve_customer_document_fields(client, TEST_CUSTOMER_CODE)
    _, response = _send_service_ingress_comprobante(
        client,
        payload,
        customer_fields,
        operation="IngresarComprobanteJSON",
        numera_flex="S",
        emite_reg="E",
    )
    if not response.get("remitoNumber"):
        raise CommandError("Bejerman TEST no devolvió número de remito para emisión online.")
    _verify_stock(client, serial, "STR")
    return response["remitoNumber"]


def _run_manual_registration(client: BejermanSDKClient, company, serial: str, manual_number: str | None):
    issue_date = timezone.localdate().isoformat()
    ingreso_id = int(time.time()) % 100000
    stock_item = {
        "Comprobante_FechaEmision": issue_date,
        "Comprobante_Tipo": _clean(getattr(settings, "BEJERMAN_STOCK_ENTRY_COMPROBANTE", "ENT")) or "ENT",
        "Comprobante_Letra": "",
        "Comprobante_PtoVenta": "",
        "Comprobante_Numero": str(ingreso_id).zfill(8),
        "Comprobante_Art_CodGen": TEST_ARTICLES[0]["code"],
        "Comprobante_ArtPartida": serial,
        "Comprobante_DescArticulo": f"NEXORA {serial}"[:50],
        "Comprobante_ArtCodTipo": "1",
        "Comprobante_ArtDeposito": "STR",
        "Comprobante_CantidadUM1": 1,
        "Comprobante_CantidadUM2": 1,
        "Comprobante_PrecioTotalMonLocal": 0,
        "Comprobante_IdOrigen": f"NEXORA-SMOKE-{serial}"[:50],
        "Partida_Observaciones": "NEXORA-SMOKE",
    }
    stock_response = client.ingresar_lista_comprobantes_stock_json(
        [stock_item],
        numera_flex=_clean(getattr(settings, "BEJERMAN_STOCK_NUMERA_FLEX", "N")) or "N",
    )
    _ensure_ok(stock_response, "ENT de registración manual")
    _verify_stock(client, serial, "STR")

    number = _find_free_manual_number(client, manual_number)
    document = {
        "type": "RIS",
        "letter": "R",
        "point": "00001",
        "number": number,
        "remitoNumber": format_remito_number("RIS", "R", "00001", number),
    }
    payload = _base_payload(company, serial, update_stock=False, point="00001", ingreso_id=ingreso_id)
    payload["preserveRegisteredDocumentStockFlag"] = True
    customer_fields = resolve_customer_document_fields(client, TEST_CUSTOMER_CODE)
    built = build_service_ingress_comprobante(payload, customer_fields)
    comprobante = _apply_registered_document(built["comprobante"], payload, document)
    article_lines = [item for item in comprobante["Comprobante_Items"] if item.get("Item_Tipo") == "A"]
    if comprobante.get("Comprobante_ActualizaStock") != "N" or not article_lines or article_lines[0].get("Item_Partida") != serial:
        raise CommandError("Contrato local inválido: el RIS manual debe ir con ActualizaStock=N y partida informada.")
    response = client.ingresar_comprobante_ventas_json(
        comprobante,
        circuito="VENTAS",
        operacion="IngresarComprobanteJSON",
        numera_flex="N",
        emite_reg="R",
    )
    _ensure_ok(response, "RIS de registración manual")
    return document["remitoNumber"]


def _article_real_stock(client: BejermanSDKClient, article_code: str, deposit: str) -> Decimal:
    rows = build_stock_rows(client.obtener_stock_deposito(deposit))
    values = [
        Decimal(str(row.get("realQuantity") or 0))
        for row in rows
        if _upper(row.get("articleCode")) == _upper(article_code) and _upper(row.get("depositCode")) == _upper(deposit)
    ]
    if not values:
        raise CommandError(f"No se encontró stock de {article_code} en {deposit} para ejecutar el smoke RT valorizado.")
    return sum(values, Decimal("0"))


def _run_valued_rt_sitiva6(client: BejermanSDKClient, company, reference: str) -> str:
    article = TEST_ARTICLES[1]
    deposit = "VAL"
    before_stock = _article_real_stock(client, article["code"], deposit)
    if before_stock < 1:
        raise CommandError(f"El artículo {article['code']} necesita al menos una unidad en {deposit} para el smoke RT.")
    customer_fields = resolve_customer_document_fields(client, TEST_CUSTOMER_SITIVA6_CODE)
    if _clean(customer_fields.get("Cliente_SitIVA")) != "6":
        raise CommandError(
            f"El cliente {TEST_CUSTOMER_SITIVA6_CODE} no resolvió Cliente_SitIVA=6: {customer_fields.get('Cliente_SitIVA')}"
        )
    issue_date = timezone.localdate().isoformat()
    request = {
        "groupId": reference,
        "companyKey": company.key,
        "issueDate": issue_date,
        "customerCode": TEST_CUSTOMER_SITIVA6_CODE,
        "customerName": "NEXORA TEST CLIENTE SITUACIÓN IVA 6",
        "sellerCode": _clean(getattr(settings, "BEJERMAN_RIS_SELLER_CODE", "ADM")) or "ADM",
        "paymentTermCode": _clean(getattr(settings, "BEJERMAN_RIS_PAYMENT_TERM", "30")) or "30",
        "priceCurrency": "ARS",
        "orders": [
            {
                "orderNumber": reference,
                "deliveryType": "sale",
                "items": [
                    {
                        "articleCode": article["code"],
                        "articleName": article["description"],
                        "quantity": 1,
                        "unitPrice": 100,
                        "priceCurrency": "ARS",
                        "discountPercent": 0,
                        "stockDepositCode": deposit,
                    }
                ],
            }
        ],
    }
    profile = {
        "type": "RT",
        "operation": "MC",
        "deposit": deposit,
        "pointOfSale": _rt_point(company),
        "companyKey": company.key,
    }
    built = build_delivery_remito_comprobante(
        request,
        delivery_remito_config(profile),
        customer_fields,
        {article["code"]: {"salesVatType": "1", "salesVatRate": 21}},
    )
    comprobante = built["comprobante"]
    article_lines = [item for item in comprobante["Comprobante_Items"] if item.get("Item_Tipo") == "A"]
    if (
        built.get("amountMode") != "tax_discriminated"
        or len(article_lines) != 1
        or Decimal(str(article_lines[0].get("Item_ImporteTotal"))) != Decimal("100.0")
        or Decimal(str(article_lines[0].get("Item_ImporteIVAInscrip"))) != Decimal("21.0")
        or Decimal(str(comprobante.get("Comprobante_ImporteTotal"))) != Decimal("121.0")
    ):
        raise CommandError("El contrato local SitIVA 6 no produjo un RT de 100,00 neto + 21,00 de IVA discriminado.")
    response = client.ingresar_comprobante_ventas_json(
        comprobante,
        circuito="VENTAS",
        operacion="IngresarComprobanteJSON",
        numera_flex="S",
        emite_reg="E",
    )
    _ensure_ok(response, "Emisión RT valorizado SitIVA 6")
    summary = parse_remito_response(response)
    remito_number = summary.get("remitoNumber")
    if not remito_number:
        raise CommandError("Bejerman TEST no devolvió número para el RT valorizado SitIVA 6.")

    headers = records_from_response(
        client.list_comprobantes_ventas(build_sales_filters(TEST_CUSTOMER_SITIVA6_CODE, issue_date, issue_date, "RT", ""))
    )
    number = _clean(summary.get("comprobanteNumero"))
    point = _clean(summary.get("comprobantePtoVenta"))
    matches = [
        header
        for header in headers
        if _clean(first_value(header, ("Comprobante_Numero", "Numero"))).lstrip("0") == number.lstrip("0")
        and _clean(first_value(header, ("Comprobante_PtoVenta", "PtoVenta"))).zfill(5) == point.zfill(5)
    ]
    if len(matches) != 1 or not comprobante_id_of(matches[0]):
        raise CommandError(f"El read-back del RT devolvió {len(matches)} coincidencias; se esperaba una.")
    details = records_from_response(client.detalle_comprobante_ventas(comprobante_id_of(matches[0])))
    detail = details[0] if details and isinstance(details[0], dict) else {}
    detail_articles = [item for item in detail.get("Comprobante_Items") or [] if isinstance(item, dict) and item.get("Item_Tipo") == "A"]
    if len(detail_articles) != 1:
        raise CommandError(f"El RT leído tiene {len(detail_articles)} movimientos de artículo; se esperaba uno.")
    detail_total = Decimal(str(first_value(detail, ("Comprobante_ImporteTotal", "ImporteTotal")) or 0))
    if detail_total != Decimal("121.0"):
        raise CommandError(f"El read-back del RT devolvió total {detail_total}; se esperaba 121,00.")

    after_stock = None
    for attempt in range(5):
        if attempt:
            time.sleep(2)
        after_stock = _article_real_stock(client, article["code"], deposit)
        if before_stock - after_stock == Decimal("1"):
            break
    if after_stock is None or before_stock - after_stock != Decimal("1"):
        raise CommandError(
            f"El stock de {article['code']} cambió de {before_stock} a {after_stock}; se esperaba un único movimiento de salida."
        )
    return remito_number


class Command(BaseCommand):
    help = "Smoke live de remitos contra Bejerman TEST. Emite comprobantes solo en la empresa de prueba."

    def add_arguments(self, parser):
        parser.add_argument("--company-key", default="TEST")
        parser.add_argument("--scenario", choices=("all", "emission", "manual", "rt-valued"), default="all")
        parser.add_argument("--manual-number", default="", help="Número RIS 00001 reservado; si se omite se busca uno alto libre.")

    def handle(self, *args, **options):
        company = _assert_test_target(options["company_key"])
        client = BejermanSDKClient(company_key=company.key, allow_system_credentials=True)
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        self.stdout.write(f"Objetivo seguro TEST: {company.key}->{company.bejerman_company}")
        scenarios = _scenarios_for_company(company, options["scenario"])
        if options["scenario"] == "all" and company.key == "MGBIO":
            self.stdout.write("Escenarios RIS omitidos: MGBIO TEST no tiene definido el tipo de comprobante RIS.")
        try:
            if "emission" in scenarios:
                remito = _run_emission(client, company, f"NXTEST-{stamp}-E")
                self.stdout.write(f"OK emisión online: {remito}")
            if "manual" in scenarios:
                remito = _run_manual_registration(client, company, f"NXTEST-{stamp}-R", options["manual_number"] or None)
                self.stdout.write(f"OK registración manual: {remito}")
            if "rt-valued" in scenarios:
                remito = _run_valued_rt_sitiva6(client, company, f"NXTEST-{stamp}-RT6")
                self.stdout.write(f"OK RT valorizado SitIVA 6: {remito}")
        except BejermanSdkError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("OK: smoke Bejerman TEST completado")
