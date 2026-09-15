import json
from unittest.mock import patch

import requests
from urllib3.exceptions import ProtocolError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from service.bejerman_ris import _remote_duplicate_check_unavailable_issue
from service.bejerman_sdk import (
    BejermanPdfReference,
    BejermanSDKClient,
    BejermanSdkResponseError,
    BejermanSdkTransportUnavailable,
    BejermanSdkUnavailable,
    _connection_error_was_in_flight,
    _probe_comprobante_pdf_copy,
    is_bejerman_customer_resolution_message,
    is_bejerman_duplicate_candidate_message,
    is_bejerman_partida_inexistente_message,
    is_bejerman_sdk_race_message,
    is_bejerman_sales_query_schema_message,
    is_bejerman_stock_insuficiente_message,
    is_bejerman_table_update_transient_message,
    is_bejerman_transport_transient_message,
    is_transient_http_failure,
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

    def test_detecta_falla_de_resolucion_de_cliente(self):
        self.assertTrue(is_bejerman_customer_resolution_message("BEJERMAN_CUSTOMER_NOT_FOUND"))
        self.assertTrue(is_bejerman_customer_resolution_message("BEJERMAN_CUSTOMER_VAT_MISSING"))
        self.assertTrue(is_bejerman_customer_resolution_message("BEJERMAN_CUSTOMER_DOCUMENT_TYPE_MISSING"))
        self.assertTrue(is_bejerman_customer_resolution_message("BEJERMAN_CUSTOMER_DOCUMENT_MISSING"))
        self.assertFalse(is_bejerman_customer_resolution_message("Partida inexistente"))

    def test_detecta_partida_inexistente(self):
        self.assertTrue(
            is_bejerman_partida_inexistente_message(
                "El comprobante no se importó debido a que el ítem 1103001 tiene asignada una Partida inexistente y/o no informada."
            )
        )
        self.assertFalse(is_bejerman_partida_inexistente_message("No hay stock suficiente en la partida C3-00718"))

    def test_detecta_stock_insuficiente(self):
        self.assertTrue(
            is_bejerman_stock_insuficiente_message(
                "El comprobante no se importó. No hay stock suficiente en la partida C3-00718 para el artículo 1111003."
            )
        )
        self.assertFalse(is_bejerman_stock_insuficiente_message("Partida inexistente y/o no informada"))

    def test_detecta_bloqueo_transitorio_de_tabla(self):
        self.assertTrue(
            is_bejerman_table_update_transient_message(
                "El comprobante no se importó. No ha sido posible actualizar los datos en la tabla."
            )
        )
        self.assertFalse(is_bejerman_table_update_transient_message("Partida inexistente y/o no informada"))

    def test_detecta_candidatos_duplicados(self):
        self.assertTrue(
            is_bejerman_duplicate_candidate_message(
                "Bejerman tiene dos RSS coincidentes para OS-29399 con la misma partida S219D4010"
            )
        )
        self.assertFalse(is_bejerman_duplicate_candidate_message("Partida inexistente y/o no informada"))

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


class _FakeHttpResponse:
    def __init__(self, status_code: int, text: str, reason: str = ""):
        self.status_code = status_code
        self.text = text
        self.reason = reason
        self.ok = 200 <= status_code < 400


# Lo que devuelve IIS cuando el host WCF no llega a activarse: no es SOAP, es la
# página de error de ASP.NET. Es el 500 que rompía el selector de depósitos.
ACTIVATION_ERROR_BODY = (
    "Server Error in '/' Application. System.ServiceModel.ServiceActivationException: "
    "The service '/EFlexSDK_Service.svc' cannot be activated due to an exception during compilation."
)
SOAP_OK_BODY = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
    "<Resultado>OK</Resultado><ErrorMsg /><DatosJSON>[]</DatosJSON>"
    "</s:Body></s:Envelope>"
)
SOAP_FAULT_BODY = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
    "<faultstring>Cliente inexistente</faultstring>"
    "</s:Fault></s:Body></s:Envelope>"
)


@override_settings(
    BEJERMAN_SDK_TRANSPORT_RETRY_ATTEMPTS=2,
    BEJERMAN_SDK_TRANSPORT_RETRY_DELAY_MS=0,
    BEJERMAN_SDK_RACE_RETRY_ATTEMPTS=0,
)
class SdkTransportRetryTests(SimpleTestCase):
    """El servicio WCF de Bejerman se cae solo (reciclado del pool de IIS) y contesta
    un 500 de activación. El pedido nunca se ejecutó, así que las lecturas y el
    registro se repiten solos en vez de degradar la pantalla del usuario."""

    def _client(self, responses):
        client = BejermanSDKClient.__new__(BejermanSDKClient)
        client.endpoint_url = "http://bejerman.test/EFlexSDK_Service.svc"
        client.timeout = 5
        client.token = "TOKEN"
        calls = []

        class _Session:
            def post(self, url, data=None, headers=None, timeout=None):
                calls.append(headers.get("SOAPAction") if headers else "")
                effect = responses.pop(0)
                if isinstance(effect, Exception):
                    raise effect
                return effect

        client._http = _Session()
        return client, calls

    def test_detecta_el_servicio_caido(self):
        self.assertTrue(is_bejerman_transport_transient_message(ACTIVATION_ERROR_BODY))
        self.assertTrue(is_transient_http_failure(500, ACTIVATION_ERROR_BODY))
        self.assertTrue(is_transient_http_failure(503, "Service Unavailable"))
        # Un 500 con un rechazo real de Bejerman no es el servicio caído.
        self.assertFalse(is_transient_http_failure(500, "Cliente inexistente"))
        self.assertFalse(is_bejerman_transport_transient_message("Partida inexistente"))

    @patch("service.bejerman_sdk.time.sleep")
    def test_reintenta_lectura_ante_servicio_caido(self, _sleep):
        client, calls = self._client(
            [
                _FakeHttpResponse(500, ACTIVATION_ERROR_BODY, "Internal Server Error"),
                _FakeHttpResponse(200, SOAP_OK_BODY),
            ]
        )
        response = client.execute("STOCK", "ObtenerDepositos", params_json=[])
        self.assertEqual(response["Resultado"], "OK")
        self.assertEqual(len(calls), 2)

    @patch("service.bejerman_sdk.time.sleep")
    def test_reintenta_conexion_rechazada(self, _sleep):
        client, calls = self._client(
            [
                requests.ConnectionError("Connection refused"),
                _FakeHttpResponse(200, SOAP_OK_BODY),
            ]
        )
        client.execute("STOCK", "ObtenerDepositos", params_json=[])
        self.assertEqual(len(calls), 2)

    @patch("service.bejerman_sdk.time.sleep")
    def test_un_corte_con_el_pedido_en_vuelo_no_se_da_por_no_ejecutado(self, _sleep):
        """Un reset después de mandar el cuerpo puede haber dejado el comprobante emitido.

        Llega como `requests.ConnectionError` igual que un "conexión rechazada", pero
        tratarlo como transporte caído hace que se le diga al operador que reintente una
        emisión que quizá entró, y que la lectura se repita sola.
        """
        for original in (
            requests.ConnectionError(
                ProtocolError("Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))
            ),
            requests.ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed connection'))"),
        ):
            with self.subTest(error=str(original)[:40]):
                client, calls = self._client([original, _FakeHttpResponse(200, SOAP_OK_BODY)])
                with self.assertRaises(BejermanSdkUnavailable) as ctx:
                    client.execute("STOCK", "ObtenerDepositos", params_json=[])
                self.assertNotIsInstance(ctx.exception, BejermanSdkTransportUnavailable)
                # Y no se reintenta solo, aunque sea una lectura.
                self.assertEqual(len(calls), 1)

    def test_la_conexion_que_no_se_abrio_sigue_siendo_transporte_caido(self):
        self.assertFalse(_connection_error_was_in_flight(requests.ConnectionError("Connection refused")))
        self.assertFalse(
            _connection_error_was_in_flight(
                requests.ConnectionError("Failed to establish a new connection: [Errno 111]")
            )
        )
        # El error de urllib3 viaja en args[0], no en __cause__.
        self.assertTrue(
            _connection_error_was_in_flight(
                requests.ConnectionError(ProtocolError("Connection aborted.", BrokenPipeError()))
            )
        )

    @patch("service.bejerman_sdk.time.sleep")
    def test_reintenta_el_registro(self, _sleep):
        client, calls = self._client(
            [
                _FakeHttpResponse(503, "Service Unavailable", "Service Unavailable"),
                _FakeHttpResponse(
                    200,
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    "<Resultado>OK</Resultado><Token>NUEVO</Token></s:Body></s:Envelope>",
                ),
            ]
        )
        client.token = ""
        client.bejerman_username = "user"
        client.bejerman_password = "pass"
        client.bejerman_workstation = "STEC"
        client.company = "SEP"
        self.assertEqual(client.register(), "NUEVO")
        self.assertEqual(len(calls), 2)

    @patch("service.bejerman_sdk.time.sleep")
    def test_no_reintenta_escrituras(self, _sleep):
        """Repetir a ciegas una emisión podría duplicar un comprobante."""
        client, calls = self._client(
            [_FakeHttpResponse(500, ACTIVATION_ERROR_BODY, "Internal Server Error")]
        )
        with self.assertRaises(BejermanSdkTransportUnavailable):
            client.execute("VENTAS", "IngresarComprobanteJSON", params_json=[])
        self.assertEqual(len(calls), 1)

    @patch("service.bejerman_sdk.time.sleep")
    def test_no_reintenta_un_rechazo_de_bejerman(self, _sleep):
        client, calls = self._client([_FakeHttpResponse(500, SOAP_FAULT_BODY, "Internal Server Error")])
        with self.assertRaises(BejermanSdkResponseError):
            client.execute("STOCK", "ObtenerDepositos", params_json=[])
        self.assertEqual(len(calls), 1)

    @patch("service.bejerman_sdk.time.sleep")
    def test_no_reintenta_un_timeout_de_lectura(self, _sleep):
        """El pedido ya viajó: repetirlo sólo alarga la espera del usuario."""
        client, calls = self._client([requests.ReadTimeout("Read timed out")])
        with self.assertRaises(BejermanSdkUnavailable):
            client.execute("STOCK", "ObtenerDepositos", params_json=[])
        self.assertEqual(len(calls), 1)


class ProbeComprobantePdfCopyDirectoryScanTests(SimpleTestCase):
    """`allow_directory_scan=False` no debe darle al listdir() de la carpeta remota
    (caro contra Bejerman/Samtronic, ver bejerman_pdf_remote.read_remote_pdf_candidates)
    ninguna chance de ejecutarse; solo se habilita en los chequeos de última oportunidad."""

    def _reference(self) -> BejermanPdfReference:
        return BejermanPdfReference(
            type="RT",
            number="00000683",
            letter="R",
            point_of_sale="00007",
            issue_date="2026-07-14",
            customer_code="JORGE",
        )

    def _client(self) -> BejermanSDKClient:
        client = BejermanSDKClient.__new__(BejermanSDKClient)
        client.company_key = "MGBIO"
        return client

    def test_allow_directory_scan_false_no_pasa_prefijo(self):
        with (
            patch(
                "service.bejerman_sdk._configured_pdf_output_dir",
                return_value=r"samtronic:C:\3. MGBIO\PDF\REMITOS",
            ),
            patch(
                "service.bejerman_sdk.read_remote_pdf_candidates",
                return_value=(None, ""),
            ) as read_candidates,
        ):
            pdf, status = _probe_comprobante_pdf_copy(
                self._client(), self._reference(), interactive=True, allow_directory_scan=False,
            )

        self.assertIsNone(pdf)
        self.assertEqual(status, "missing")
        self.assertEqual(read_candidates.call_count, 2)
        for candidate_call in read_candidates.call_args_list:
            self.assertIsNone(candidate_call.args[2])

    def test_allow_directory_scan_true_pasa_el_prefijo(self):
        with (
            patch(
                "service.bejerman_sdk._configured_pdf_output_dir",
                return_value=r"samtronic:C:\3. MGBIO\PDF\REMITOS",
            ),
            patch(
                "service.bejerman_sdk.read_remote_pdf_candidates",
                return_value=(None, ""),
            ) as read_candidates,
        ):
            pdf, status = _probe_comprobante_pdf_copy(
                self._client(), self._reference(), interactive=True, allow_directory_scan=True,
            )

        self.assertIsNone(pdf)
        self.assertEqual(status, "missing")
        self.assertEqual(read_candidates.call_count, 2)
        for candidate_call in read_candidates.call_args_list:
            self.assertEqual(candidate_call.args[2], ["RTR00007-00000683", "V-RT -R-00007-00000683-"])


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


class BejermanStartupConfigurationTests(SimpleTestCase):
    @override_settings(BEJERMAN_WSDL_URL="", BEJERMAN_WORKSTATION="STEC")
    def test_healthcheck_de_arranque_falla_claro_sin_wsdl(self):
        with self.assertRaisesRegex(CommandError, "BEJERMAN_WSDL_URL"):
            call_command("check_bejerman_sdk", "--skip-live")
