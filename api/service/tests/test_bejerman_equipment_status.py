import json
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from service.bejerman_sdk import BejermanSdkConfigError
from service.views.ingresos_views import IngresoBejermanEstadoView


class FakeBejermanEstadoClient:
    def __init__(self, *, stock_records=None, deposits=None, partidas=None, error=None):
        self.stock_records = list(stock_records or [])
        self.deposits = list(deposits or [])
        self.partidas = list(partidas or [])
        self.error = error
        self.stock_calls = []

    def obtener_stock_partida(self, partida):
        self.stock_calls.append(partida)
        if self.error:
            raise self.error
        return {"Resultado": "OK", "DatosJSON": json.dumps(self.stock_records)}

    def obtener_depositos(self):
        return {"Resultado": "OK", "DatosJSON": json.dumps(self.deposits)}

    def obtener_partidas(self):
        return {"Resultado": "OK", "DatosJSON": json.dumps(self.partidas)}


class IngresoBejermanEstadoViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = type("AuthUser", (), {"is_authenticated": True, "id": 1152, "rol": "jefe"})()

    def _row(self, *, serial="SN-001", interno="MG 0001", company="MGBIO"):
        return {
            "id": 29400,
            "numero_serie": serial,
            "numero_interno": interno,
            "empresa_bejerman": company,
        }

    def _response(self, row, client):
        request = self.factory.get("/api/ingresos/29400/bejerman-estado/")
        force_authenticate(request, user=self.user)
        with (
            patch("service.views.ingresos_views.user_has_any_permission", return_value=True),
            patch("service.views.ingresos_views._has_table_column", return_value=True),
            patch("service.views.ingresos_views.q", return_value=row),
            patch("service.views.ingresos_views.BejermanSDKClient", return_value=client) as client_cls,
        ):
            response = IngresoBejermanEstadoView.as_view()(request, ingreso_id=29400)
        return response, client_cls

    def test_encuentra_partida_con_stock_positivo_en_deposito(self):
        client = FakeBejermanEstadoClient(
            stock_records=[
                {
                    "Art_CodDeposito": "VAL",
                    "Art_Partida": "SN-001",
                    "Art_CodGen": "1202018",
                    "Art_DescripcionGeneral": "Concentrador",
                    "Art_RealUM1": 2,
                    "Art_CompUM1": 0,
                    "Art_DispUM1": 2,
                }
            ],
            deposits=[{"Deposito_Codigo": "VAL", "Deposito_Descripcion": "Valdenegro"}],
            partidas=[{"Partida_Codigo": "SN-001", "Deposito_Codigo": "VAL", "Partida_FechaVtoIngreso": "2030-12-31"}],
        )

        response, client_cls = self._response(self._row(), client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "found")
        self.assertEqual(response.data["companyKey"], "MGBIO")
        self.assertEqual(response.data["identifier"], "SN-001")
        client_cls.assert_called_once_with(company_key="MGBIO", actor_user_id=1152)
        self.assertEqual(client.stock_calls, ["SN-001"])
        item = response.data["items"][0]
        self.assertEqual(item["depositCode"], "VAL")
        self.assertEqual(item["depositName"], "Valdenegro")
        self.assertEqual(item["articleCode"], "1202018")
        self.assertEqual(item["articleDescription"], "Concentrador")
        self.assertEqual(item["availableQuantity"], 2.0)
        self.assertEqual(item["expirationDate"], "2030-12-31")

    def test_informa_sin_stock_positivo(self):
        client = FakeBejermanEstadoClient(
            stock_records=[
                {
                    "Art_CodDeposito": "STC",
                    "Art_Partida": "SN-001",
                    "Art_CodGen": "1202018",
                    "Art_RealUM1": 0,
                    "Art_CompUM1": 0,
                    "Art_DispUM1": 0,
                }
            ]
        )

        response, _ = self._response(self._row(), client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "zero_stock")
        self.assertIn("sin stock positivo", response.data["warning"])

    def test_informa_no_encontrado(self):
        response, _ = self._response(self._row(), FakeBejermanEstadoClient())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "not_found")
        self.assertEqual(response.data["items"], [])

    def test_usa_numero_interno_si_falta_serie(self):
        client = FakeBejermanEstadoClient()

        response, _ = self._response(self._row(serial="", interno="MG 6499"), client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["identifier"], "MG 6499")
        self.assertEqual(response.data["identifierSource"], "numero_interno")
        self.assertEqual(client.stock_calls, ["MG 6499"])

    def test_informa_sin_identificador(self):
        request = self.factory.get("/api/ingresos/29400/bejerman-estado/")
        force_authenticate(request, user=self.user)
        with (
            patch("service.views.ingresos_views.user_has_any_permission", return_value=True),
            patch("service.views.ingresos_views._has_table_column", return_value=True),
            patch("service.views.ingresos_views.q", return_value=self._row(serial="", interno="")),
            patch("service.views.ingresos_views.BejermanSDKClient") as client_cls,
        ):
            response = IngresoBejermanEstadoView.as_view()(request, ingreso_id=29400)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "missing_identifier")
        client_cls.assert_not_called()

    def test_error_de_credenciales_devuelve_503_claro(self):
        client = FakeBejermanEstadoClient(error=BejermanSdkConfigError("Debe cargar sus credenciales de Bejerman."))

        response, _ = self._response(self._row(), client)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "BEJERMAN_CREDENTIALS_REQUIRED")
        self.assertIn("credenciales", response.data["detail"])
