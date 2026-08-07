from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_particular_customers import (
    disable_particular_customer_if_idle,
    ensure_particular_customer_enabled,
    is_nexora_created_code,
)
from service.bejerman_sdk import (
    bejerman_client_record_is_enabled,
    build_customer_toggle_payload,
    find_bejerman_client_record_by_document,
)


def _clientes_response(*records):
    import json

    return {"Resultado": "OK", "DatosJSON": json.dumps(list(records))}


def _ficha(codigo, *, documento="30710698", habilitado=True, razon_social="Juan Perez"):
    return {
        "Cliente_Codigo": codigo,
        "Cliente_RazonSocial": razon_social,
        "Cliente_CUIT": documento,
        "Cliente_TipoDocumentoCodigo": 5,
        "Cliente_CodigoProvincia": "001",
        "Cliente_SituacionIVACodigo": "3",
        "Cliente_SituacionIIBBCodigo": "1",
        "Cliente_SituacionGananciasCodigo": "1",
        "Cliente_VendedorCodigo": " ADM",
        "Cliente_CondicionVentaCodigo": "30",
        "Cliente_Habilitado": habilitado,
    }


class ParticularCustomerSdkHelpersTests(SimpleTestCase):
    def test_encuentra_la_ficha_por_documento_ignorando_puntos_y_guiones(self):
        response = _clientes_response(_ficha("000012", documento="30710698"))

        record = find_bejerman_client_record_by_document(response, "30.710.698")

        self.assertIsNotNone(record)
        self.assertEqual(record["Cliente_Codigo"], "000012")

    def test_encuentra_tambien_las_fichas_deshabilitadas(self):
        # ObtenerClientes las devuelve igual; es lo que permite rehabilitarlas.
        response = _clientes_response(_ficha("000012", habilitado=False))

        record = find_bejerman_client_record_by_document(response, "30710698")

        self.assertIsNotNone(record)
        self.assertFalse(bejerman_client_record_is_enabled(record))

    def test_no_confunde_documentos_distintos(self):
        response = _clientes_response(_ficha("000012", documento="30710698"))

        self.assertIsNone(find_bejerman_client_record_by_document(response, "27263312258"))
        self.assertIsNone(find_bejerman_client_record_by_document(response, ""))

    def test_el_payload_de_toggle_conserva_la_ficha_entera(self):
        record = _ficha("000012", habilitado=True)

        payload = build_customer_toggle_payload(record, habilitado=False)

        self.assertFalse(payload["Cliente_Habilitado"])
        # ModificarCliente exige los obligatorios completos: se reenvia todo, no un parche.
        for key, value in record.items():
            if key != "Cliente_Habilitado":
                self.assertEqual(payload[key], value)

    def test_distingue_las_fichas_creadas_por_nexora(self):
        for code in ("000012", "1", "  000012  "):
            with self.subTest(code=code):
                self.assertTrue(is_nexora_created_code(code))
        for code in ("PELLEG", " CESAR", "RVRDS", "", None):
            with self.subTest(code=code):
                self.assertFalse(is_nexora_created_code(code))


@override_settings(BEJERMAN_PARTICULAR_CUSTOMER_TOGGLE_ENABLED=True)
@patch("service.bejerman_particular_customers._log_action")
class EnsureParticularCustomerEnabledTests(SimpleTestCase):
    def test_rehabilita_la_ficha_deshabilitada_antes_de_emitir(self, _log):
        client = Mock()
        client.list_clientes.return_value = _clientes_response(_ficha("000012", habilitado=False))
        client.modificar_cliente.return_value = {"Resultado": "OK"}

        ensure_particular_customer_enabled(client, "SEPID", "30.710.698")

        client.modificar_cliente.assert_called_once()
        enviado = client.modificar_cliente.call_args[0][0]
        self.assertIs(enviado["Cliente_Habilitado"], True)
        self.assertEqual(enviado["Cliente_Codigo"], "000012")

    def test_no_escribe_si_la_ficha_ya_esta_habilitada(self, _log):
        client = Mock()
        client.list_clientes.return_value = _clientes_response(_ficha("000012", habilitado=True))

        ensure_particular_customer_enabled(client, "SEPID", "30710698")

        client.modificar_cliente.assert_not_called()

    def test_no_hace_nada_si_no_existe_ficha(self, _log):
        # La va a crear el codigo ocasional al emitir.
        client = Mock()
        client.list_clientes.return_value = _clientes_response()

        self.assertIsNone(ensure_particular_customer_enabled(client, "SEPID", "30710698"))
        client.modificar_cliente.assert_not_called()

    def test_un_fallo_al_rehabilitar_no_interrumpe_la_emision(self, _log):
        client = Mock()
        client.list_clientes.side_effect = RuntimeError("Bejerman caido")

        self.assertIsNone(ensure_particular_customer_enabled(client, "SEPID", "30710698"))

    def test_apagado_por_setting_no_toca_bejerman(self, _log):
        client = Mock()

        with override_settings(BEJERMAN_PARTICULAR_CUSTOMER_TOGGLE_ENABLED=False):
            ensure_particular_customer_enabled(client, "SEPID", "30710698")

        client.list_clientes.assert_not_called()


@override_settings(BEJERMAN_PARTICULAR_CUSTOMER_TOGGLE_ENABLED=True)
@patch("service.bejerman_particular_customers._log_action")
class DisableParticularCustomerTests(SimpleTestCase):
    def _client(self, record):
        client = Mock()
        client.list_clientes.return_value = _clientes_response(record) if record else _clientes_response()
        client.modificar_cliente.return_value = {"Resultado": "OK"}
        return client

    def test_deshabilita_cuando_no_queda_nada_abierto(self, _log):
        client = self._client(_ficha("000012", habilitado=True))

        with patch("service.bejerman_particular_customers.has_other_open_ingresos", return_value=False):
            deshabilito = disable_particular_customer_if_idle(
                "SEPID", "30710698", exclude_ingreso_id=7, client=client
            )

        self.assertTrue(deshabilito)
        self.assertIs(client.modificar_cliente.call_args[0][0]["Cliente_Habilitado"], False)

    def test_no_deshabilita_si_el_particular_tiene_otro_equipo_adentro(self, _log):
        # El caso que motivo toda la salvaguarda: trae dos equipos y se entrega uno solo.
        client = self._client(_ficha("000012", habilitado=True))

        with patch("service.bejerman_particular_customers.has_other_open_ingresos", return_value=True):
            deshabilito = disable_particular_customer_if_idle(
                "SEPID", "30710698", exclude_ingreso_id=7, client=client
            )

        self.assertFalse(deshabilito)
        client.modificar_cliente.assert_not_called()

    def test_nunca_toca_una_ficha_cargada_a_mano(self, _log):
        client = self._client(_ficha("PELLEG", habilitado=True))

        with patch("service.bejerman_particular_customers.has_other_open_ingresos", return_value=False):
            deshabilito = disable_particular_customer_if_idle("SEPID", "30710698", client=client)

        self.assertFalse(deshabilito)
        client.modificar_cliente.assert_not_called()

    def test_un_fallo_al_deshabilitar_no_revierte_la_entrega(self, _log):
        client = self._client(_ficha("000012", habilitado=True))
        client.modificar_cliente.side_effect = RuntimeError("Bejerman caido")

        with patch("service.bejerman_particular_customers.has_other_open_ingresos", return_value=False):
            self.assertFalse(disable_particular_customer_if_idle("SEPID", "30710698", client=client))

    def test_apagado_por_setting_no_toca_bejerman(self, _log):
        client = self._client(_ficha("000012", habilitado=True))

        with override_settings(BEJERMAN_PARTICULAR_CUSTOMER_TOGGLE_ENABLED=False):
            self.assertFalse(disable_particular_customer_if_idle("SEPID", "30710698", client=client))

        client.list_clientes.assert_not_called()
