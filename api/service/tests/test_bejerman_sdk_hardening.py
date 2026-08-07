import json
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from service.bejerman_ris import _remote_duplicate_check_unavailable_issue
from service.bejerman_sdk import (
    BejermanPdfReference,
    BejermanSDKClient,
    BejermanSdkResponseError,
    _probe_comprobante_pdf_copy,
    is_bejerman_customer_resolution_message,
    is_bejerman_duplicate_candidate_message,
    is_bejerman_partida_inexistente_message,
    is_bejerman_sdk_race_message,
    is_bejerman_sales_query_schema_message,
    is_bejerman_stock_insuficiente_message,
    is_bejerman_table_update_transient_message,
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
        read_candidates.assert_called_once()
        _output_dir, _filenames, prefixes = read_candidates.call_args.args
        self.assertIsNone(prefixes)

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
        read_candidates.assert_called_once()
        _output_dir, _filenames, prefixes = read_candidates.call_args.args
        self.assertEqual(prefixes, ["RTR00007-00000683"])


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
