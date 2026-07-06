import json
from unittest.mock import patch

from django.test import SimpleTestCase

from service.bejerman_ris import _remote_duplicate_check_unavailable_issue
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkResponseError,
    is_bejerman_sdk_race_message,
    is_bejerman_sales_query_schema_message,
)


class SdkRaceRetryTests(SimpleTestCase):
    def _client_with_execute(self, side_effects):
        client = BejermanSDKClient.__new__(BejermanSDKClient)
        client.token = "token"
        calls = []

        def fake_execute_once(circuito, operacion, *, params=None, params_json=None):
            calls.append(operacion)
            effect = side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect

        client._execute_once = fake_execute_once
        return client, calls

    def test_detecta_mensajes_de_carrera(self):
        self.assertTrue(is_bejerman_sdk_race_message("Invalid object name 'expCuotasV'."))
        self.assertTrue(is_bejerman_sdk_race_message("There is already an object named 'expCuotasV' in the database."))
        self.assertFalse(is_bejerman_sdk_race_message("Cliente inexistente"))

    def test_detecta_error_de_schema_del_listado(self):
        self.assertTrue(is_bejerman_sales_query_schema_message("Invalid column name 'IMPORTE_TOTAL'."))
        self.assertTrue(is_bejerman_sales_query_schema_message('The multi-part identifier "CabVenta.cve_ID" could not be bound.'))
        self.assertFalse(is_bejerman_sales_query_schema_message("Cliente inexistente"))

    @patch("service.bejerman_sdk.time.sleep")
    def test_reintenta_lecturas_ante_carrera(self, _sleep):
        ok = {"Resultado": "OK", "DatosJSON": json.dumps([])}
        client, calls = self._client_with_execute(
            [BejermanSdkResponseError("Invalid object name 'expCuotasV'."), ok]
        )
        response = client.execute("VENTAS", "WSListarComprobantesJSON", params_json=[])
        self.assertEqual(response, ok)
        self.assertEqual(len(calls), 2)

    @patch("service.bejerman_sdk.time.sleep")
    def test_no_reintenta_escrituras(self, _sleep):
        client, calls = self._client_with_execute(
            [BejermanSdkResponseError("Invalid object name 'expCuotasV'.")]
        )
        with self.assertRaises(BejermanSdkResponseError):
            client.execute("VENTAS", "IngresarComprobanteJSON", params_json=[])
        self.assertEqual(len(calls), 1)

    @patch("service.bejerman_sdk.time.sleep")
    def test_no_reintenta_errores_comunes(self, _sleep):
        client, calls = self._client_with_execute(
            [BejermanSdkResponseError("Cliente inexistente")]
        )
        with self.assertRaises(BejermanSdkResponseError):
            client.execute("VENTAS", "WSListarComprobantesJSON", params_json=[])
        self.assertEqual(len(calls), 1)


class DuplicateCheckMessageTests(SimpleTestCase):
    def test_limpia_el_error_de_schema_de_bejerman(self):
        exc = BejermanSdkResponseError(
            "Error en la ejecución. // Invalid column name 'IMPORTE_TOTAL'. "
            "The multi-part identifier \"CabVenta.cve_ID\" could not be bound."
        )
        issue = _remote_duplicate_check_unavailable_issue(exc)
        self.assertNotIn("IMPORTE_TOTAL", issue["message"])
        self.assertIn("error interno", issue["message"])

    def test_conserva_los_errores_normales(self):
        exc = BejermanSdkResponseError("Bejerman no respondió a tiempo")
        issue = _remote_duplicate_check_unavailable_issue(exc)
        self.assertIn("Bejerman no respondió a tiempo", issue["message"])
