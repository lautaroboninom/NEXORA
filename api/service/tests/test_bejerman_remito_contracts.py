import json
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_ris import (
    BejermanRisError,
    _emit_payload_to_bejerman,
    _register_payload_to_bejerman,
)
from service.bejerman_sdk import BejermanSdkResponseError, build_service_ingress_comprobante


CUSTOMER_FIELDS = {
    "Cliente_RazonSocial": "Cliente Bejerman TEST",
    "Cliente_TipoDocumento": "1",
    "Cliente_NroDocumento": "30700000000",
    "Cliente_Provincia": "02",
    "Cliente_SitIVA": "RI",
}


class FakeRemitoContractClient:
    def __init__(self, *, number="00004578", stock_records=None, import_stock=True):
        self.number = number
        self.stock_records = {key: list(value) for key, value in (stock_records or {}).items()}
        self.import_stock = import_stock
        self.stock_comprobantes = []
        self.comprobantes = []
        self.ingresar_kwargs = []

    @property
    def ingresar_calls(self):
        return len(self.comprobantes)

    def list_comprobantes_ventas(self, filters=None):
        return {"Resultado": "OK", "DatosJSON": "[]"}

    def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
        self.comprobantes.append(comprobante)
        self.ingresar_kwargs.append(kwargs)
        registered = kwargs.get("emite_reg") == "R"
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(
                {
                    "Comprobante_Tipo": comprobante.get("Comprobante_Tipo", "RIS"),
                    "Comprobante_Letra": comprobante.get("Comprobante_Letra", "R"),
                    "Comprobante_PtoVenta": comprobante.get("Comprobante_PtoVenta", "00004"),
                    "Comprobante_Numero": comprobante.get("Comprobante_Numero") if registered else self.number,
                }
            ),
        }

    def obtener_stock_partida(self, partida):
        return {
            "Resultado": "OK",
            "DatosJSON": json.dumps(self.stock_records.get(str(partida or "").strip(), [])),
        }

    def ingresar_lista_comprobantes_stock_json(self, comprobantes, *, numera_flex="N"):
        self.stock_comprobantes.append({"comprobantes": comprobantes, "numeraFlex": numera_flex})
        if self.import_stock:
            for item in comprobantes or []:
                partida = str(item.get("Comprobante_ArtPartida") or "").strip()
                deposit = str(item.get("Comprobante_ArtDeposito") or "").strip()
                article = str(item.get("Comprobante_Art_CodGen") or "").strip()
                if not partida:
                    continue
                self.stock_records.setdefault(partida, []).append(
                    {
                        "Art_CodDeposito": deposit,
                        "Art_Partida": partida,
                        "Art_CodGen": article,
                        "Art_RealUM1": 1,
                        "Art_DispUM1": 1,
                    }
                )
        return {"Resultado": "OK", "DatosJSON": json.dumps(comprobantes or [])}


class PartidaRejectionClient(FakeRemitoContractClient):
    """Simula el WS que procesa la 'emisión' (S) como importador y rechaza la
    partida en el primer intento; tras el ENT acepta el documento con N."""

    def __init__(self, *, number="00004778"):
        super().__init__(number=number)

    def ingresar_comprobante_ventas_json(self, comprobante, **kwargs):
        if comprobante.get("Comprobante_ActualizaStock") == "S":
            self.comprobantes.append(comprobante)
            self.ingresar_kwargs.append(kwargs)
            raise BejermanSdkResponseError("El comprobante no se importó. Partida inexistente.")
        return super().ingresar_comprobante_ventas_json(comprobante, **kwargs)


def _payload(*, company_key="SEPID", bejerman_company="SEP", point="00004", update_stock=True, serial="SN-RIS-001"):
    return {
        "requestId": f"reparaciones-ingreso-{serial}",
        "ingresoId": 29471,
        "ingresoIds": [29471],
        "companyKey": company_key,
        "companyLabel": "MG BIO" if company_key == "MGBIO" else "SEPID SA",
        "bejermanCompany": bejerman_company,
        "issueDate": "2026-07-03",
        "customerCode": "NXTESTCLI",
        "customerName": "Cliente Bejerman TEST",
        "sellerCode": "ADM",
        "paymentTermCode": "30",
        "documentProfile": {
            "key": "ris",
            "type": "RIS",
            "label": "RIS",
            "letter": "R",
            "reason": "Ingreso a servicio técnico.",
            "deposit": "STR",
            "operation": "REP",
            "pointOfSale": point,
            "updateStock": update_stock,
        },
        "equipment": {
            "ingresoId": 29471,
            "articleCode": "NXTEST-PART",
            "articleName": "Artículo TEST con partida",
            "serial": serial,
            "equipmentType": "CPAP",
            "brand": "NEXORA",
            "model": "TEST",
            "repairReason": "Prueba",
        },
    }


def _batch_register_payload(serials, *, existing_stock=False):
    equipments = []
    stock_records = {}
    for index, serial in enumerate(serials, start=1):
        ingreso_id = 29470 + index
        equipments.append(
            {
                "ingresoId": ingreso_id,
                "osLabel": str(ingreso_id),
                "articleCode": "NXTEST-PART",
                "articleName": "Artículo TEST con partida",
                "serial": serial,
                "equipmentType": "CPAP",
                "brand": "NEXORA",
                "model": "TEST",
                "repairReason": "Prueba",
            }
        )
        if existing_stock:
            stock_records[serial] = [
                {
                    "Art_CodDeposito": "STR",
                    "Art_Partida": serial,
                    "Art_CodGen": "NXTEST-PART",
                    "Art_RealUM1": 1,
                    "Art_DispUM1": 1,
                }
            ]
    payload = _payload(serial=serials[0], update_stock=False)
    payload["ingresoIds"] = [item["ingresoId"] for item in equipments]
    payload["equipments"] = equipments
    payload.pop("equipment", None)
    return payload, stock_records


@override_settings(
    BEJERMAN_RIS_UPDATE_STOCK=True,
    BEJERMAN_RIS_NUMERA_FLEX="S",
    BEJERMAN_RIS_EMITE_REG="E",
    BEJERMAN_STOCK_ENTRY_COMPROBANTE="ENT",
    BEJERMAN_STOCK_NUMERA_FLEX="N",
    BEJERMAN_RIS_STOCK_VERIFY_ATTEMPTS=1,
    BEJERMAN_RIS_STOCK_VERIFY_DELAY_MS=0,
)
class BejermanRemitoContractsTests(SimpleTestCase):
    def test_emision_online_ris_actualiza_stock_s_sin_ent(self):
        fake = FakeRemitoContractClient()
        with (
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=CUSTOMER_FIELDS),
            patch("service.bejerman_ris._ensure_equipment_remito_not_duplicated"),
        ):
            request_payload, response = _emit_payload_to_bejerman(_payload())

        self.assertEqual(response["remitoNumber"], "RIS R 00004-00004578")
        self.assertEqual(fake.stock_comprobantes, [])
        self.assertEqual(fake.ingresar_kwargs[0]["emite_reg"], "E")
        self.assertEqual(fake.ingresar_kwargs[0]["numera_flex"], "S")
        comprobante = request_payload["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        self.assertEqual(comprobante["Cliente_TipoDocumento"], "1")
        self.assertEqual(article_lines[0]["Item_Deposito"], "STR")
        self.assertEqual(article_lines[0]["Item_Partida"], "SN-RIS-001")

    def test_registracion_manual_crea_ent_verifica_y_luego_ris_n_con_partida(self):
        fake = FakeRemitoContractClient()
        payload, _ = _batch_register_payload(["SN-REG-001", "SN-REG-002"])
        with (
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=CUSTOMER_FIELDS),
        ):
            request_payload, response = _register_payload_to_bejerman(
                payload,
                "RIS R 00001-00090001",
                payload["ingresoIds"],
            )

        self.assertEqual(response["remitoNumber"], "RIS R 00001-00090001")
        self.assertEqual(len(fake.stock_comprobantes), 1)
        stock_items = fake.stock_comprobantes[0]["comprobantes"]
        self.assertEqual([item["Comprobante_Tipo"] for item in stock_items], ["ENT", "ENT"])
        self.assertEqual([item["Comprobante_ArtDeposito"] for item in stock_items], ["STR", "STR"])
        self.assertEqual([item["Comprobante_ArtPartida"] for item in stock_items], ["SN-REG-001", "SN-REG-002"])
        self.assertEqual(len({item["Comprobante_Numero"] for item in stock_items}), 2)
        self.assertEqual(fake.ingresar_kwargs[0]["emite_reg"], "R")
        self.assertEqual(fake.ingresar_kwargs[0]["numera_flex"], "N")
        comprobante = request_payload["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "N")
        self.assertEqual([item["Item_Deposito"] for item in article_lines], ["STR", "STR"])
        self.assertEqual([item["Item_Partida"] for item in article_lines], ["SN-REG-001", "SN-REG-002"])

    def test_registracion_manual_con_partida_existente_saltea_ent_y_manda_ris_n(self):
        payload, stock_records = _batch_register_payload(["SN-REG-EXISTE"], existing_stock=True)
        fake = FakeRemitoContractClient(stock_records=stock_records)
        with (
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=CUSTOMER_FIELDS),
        ):
            request_payload, _ = _register_payload_to_bejerman(
                payload,
                "RIS R 00001-00090002",
                payload["ingresoIds"],
            )

        self.assertEqual(fake.stock_comprobantes, [])
        comprobante = request_payload["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "N")
        self.assertEqual(article_lines[0]["Item_Deposito"], "STR")
        self.assertEqual(article_lines[0]["Item_Partida"], "SN-REG-EXISTE")

    def test_registracion_manual_si_falla_verificacion_ent_no_manda_ris(self):
        payload, _ = _batch_register_payload(["SN-REG-SIN-STOCK"])
        fake = FakeRemitoContractClient(import_stock=False)
        with (
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=CUSTOMER_FIELDS),
        ):
            with self.assertRaisesRegex(BejermanRisError, "No se envió el RIS"):
                _register_payload_to_bejerman(payload, "RIS R 00001-00090003", payload["ingresoIds"])

        self.assertEqual(len(fake.stock_comprobantes), 1)
        self.assertEqual(fake.ingresar_calls, 0)

    def test_emision_rda_con_partida_rechazada_cae_a_ent_mas_documento_n(self):
        payload = _payload(serial="SN-RDA-REJECT")
        payload["documentProfile"] = {
            **payload["documentProfile"],
            "key": "rda",
            "type": "RDA",
            "label": "RDA",
            "operation": "ALQ",
            "reason": "Baja de alquiler.",
        }
        fake = PartidaRejectionClient(number="00004778")
        with (
            patch("service.bejerman_ris.BejermanSDKClient", return_value=fake),
            patch("service.bejerman_ris.resolve_customer_document_fields", return_value=CUSTOMER_FIELDS),
            patch("service.bejerman_ris._ensure_equipment_remito_not_duplicated"),
        ):
            _, response = _emit_payload_to_bejerman(payload)

        self.assertEqual(response["remitoNumber"], "RDA R 00004-00004778")
        # 1er intento (S) rechazado -> ENT por circuito STOCK -> 2do intento con N y partida
        self.assertEqual(len(fake.stock_comprobantes), 1)
        stock_items = fake.stock_comprobantes[0]["comprobantes"]
        self.assertEqual([item["Comprobante_Tipo"] for item in stock_items], ["ENT"])
        self.assertEqual(stock_items[0]["Comprobante_ArtPartida"], "SN-RDA-REJECT")
        self.assertEqual(len(fake.comprobantes), 2)
        segundo = fake.comprobantes[1]
        self.assertEqual(segundo["Comprobante_ActualizaStock"], "N")
        article_lines = [item for item in segundo["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(article_lines[0]["Item_Partida"], "SN-RDA-REJECT")
        self.assertGreater(article_lines[0]["Item_CantidadUM1"], 0)

    def test_mgbio_conserva_punto_00007_donde_aplica(self):
        payload = _payload(company_key="MGBIO", bejerman_company="MGBI", point="00007", serial="SN-MGBIO-0007")
        payload["documentProfile"] = {
            **payload["documentProfile"],
            "key": "rda",
            "type": "RDA",
            "label": "RDA",
            "operation": "ALQ",
            "reason": "Baja de alquiler.",
        }

        built = build_service_ingress_comprobante(payload, CUSTOMER_FIELDS)
        comprobante = built["comprobante"]
        article_lines = [item for item in comprobante["Comprobante_Items"] if item["Item_Tipo"] == "A"]
        self.assertEqual(comprobante["Comprobante_Tipo"], "RDA")
        self.assertEqual(comprobante["Comprobante_PtoVenta"], "00007")
        self.assertEqual(comprobante["Comprobante_ActualizaStock"], "S")
        self.assertEqual(article_lines[0]["Item_Deposito"], "STR")
        self.assertEqual(article_lines[0]["Item_Partida"], "SN-MGBIO-0007")
