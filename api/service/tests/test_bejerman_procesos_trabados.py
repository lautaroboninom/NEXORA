"""Lo que hacía que un proceso de Bejerman no pudiera terminar nunca.

Cada caso de acá salió de una fila real del panel "Sincronización Bejerman" que
llevaba semanas ofreciendo un "Reintentar" que volvía a fallar igual.
"""

from django.test import SimpleTestCase

from service.bejerman_ris import BejermanRisError, _assert_issue_date_plausible
from service.bejerman_sdk import (
    is_bejerman_already_imported_message,
    is_bejerman_imported_but_not_ok,
    is_ok_response,
)
from service.bejerman_sync import (
    BejermanBlockedError,
    SUPPORTED_SYNC_TYPES,
    _is_own_id_origen,
    _send_stock_comprobantes,
    is_supported_sync_type,
)


class YaExisteEnBejermanTests(SimpleTestCase):
    def test_reconoce_los_dos_textos_reales(self):
        # Comprobante de venta con número manual (RDA del punto 00001, OS 29671).
        self.assertTrue(
            is_bejerman_already_imported_message(
                "El comprobante no se importó debido a que ya existe en la empresa."
            )
        )
        # Importador de stock, dedupe por IdOrigen (transferencia de la OS 29668).
        self.assertTrue(
            is_bejerman_already_imported_message(
                "El comprobante no se importó. El mismo ya existe en la empresa "
                "debido a una importación anterior."
            )
        )

    def test_no_confunde_otros_rechazos(self):
        for message in (
            "El comprobante no se importó. No hay stock suficiente para el artículo 1208009 .",
            "El comprobante no se importó debido a que el ítem 1110001 tiene asignada una "
            "Partida inexistente y/o no informada.",
            "Los comprobantes no se importaron. Se han detectado inconsistencias en el archivo de texto.",
        ):
            self.assertFalse(is_bejerman_already_imported_message(message), message)


class ImportadoPeroNoOkTests(SimpleTestCase):
    AVISO = (
        "El comprobante se importó correctamente. La Fecha de Contabilización se modificó "
        "ya que debe ser posterior a la del último cierre del Libro IVA Ventas."
    )

    def test_resultado_error_con_aviso_de_importacion_no_es_ok_pero_si_dudoso(self):
        response = {"Resultado": "ERROR", "ErrorMsg": self.AVISO}
        # No es un éxito: el número no vino y no se puede dar el remito por emitido.
        self.assertFalse(is_ok_response(response))
        # Pero tampoco es un rechazo limpio: el comprobante quedó en Bejerman.
        self.assertTrue(is_bejerman_imported_but_not_ok(response))

    def test_una_respuesta_ok_no_necesita_revision(self):
        self.assertFalse(is_bejerman_imported_but_not_ok({"Resultado": "OK", "ErrorMsg": self.AVISO}))

    def test_un_rechazo_de_verdad_no_pasa_por_revision(self):
        self.assertFalse(
            is_bejerman_imported_but_not_ok(
                {"Resultado": "ERROR", "ErrorMsg": "No hay stock suficiente para el artículo 1208009 ."}
            )
        )


class _FakeStockClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def ingresar_lista_comprobantes_json(self, comprobantes):
        self.calls += 1
        if self.error:
            raise self.error
        return {"Resultado": "OK"}


YA_EXISTE = BejermanBlockedError(
    "El comprobante no se importó. El mismo ya existe en la empresa debido a una importación anterior."
)


class ReenvioDeMovimientosDeStockTests(SimpleTestCase):
    ENT = [
        {
            "Comprobante_Tipo": "ENT",
            "Comprobante_Numero": "00229668",
            "Comprobante_IdOrigen": "NEXORA-OS-29668-ENT-STL",
        }
    ]

    def test_reenviar_nuestro_propio_movimiento_ya_importado_es_exito(self):
        client = _FakeStockClient(error=YA_EXISTE)
        response = _send_stock_comprobantes(client, self.ENT, phase="transfer_ent")
        self.assertTrue(response["alreadyImported"])
        self.assertEqual(response["alreadyImportedPhase"], "transfer_ent")

    def test_no_absorbe_el_choque_contra_un_comprobante_ajeno(self):
        ajeno = [{**self.ENT[0], "Comprobante_IdOrigen": "IMPORTACION-MANUAL-7"}]
        with self.assertRaises(BejermanBlockedError):
            _send_stock_comprobantes(_FakeStockClient(error=YA_EXISTE), ajeno, phase="transfer_ent")

    def test_sin_confirmacion_del_stock_el_error_sigue_siendo_error(self):
        with self.assertRaises(BejermanBlockedError):
            _send_stock_comprobantes(
                _FakeStockClient(error=YA_EXISTE),
                self.ENT,
                phase="baja_ent",
                verify=lambda: False,
            )

    def test_con_confirmacion_del_stock_se_absorbe(self):
        response = _send_stock_comprobantes(
            _FakeStockClient(error=YA_EXISTE),
            self.ENT,
            phase="baja_ent",
            verify=lambda: True,
        )
        self.assertTrue(response["alreadyImported"])

    def test_otros_rechazos_no_se_tocan(self):
        sin_stock = BejermanBlockedError("El comprobante no se importó. No hay stock suficiente .")
        with self.assertRaises(BejermanBlockedError):
            _send_stock_comprobantes(_FakeStockClient(error=sin_stock), self.ENT, phase="transfer_ent")

    def test_id_origen_propio(self):
        self.assertTrue(_is_own_id_origen(self.ENT))
        self.assertFalse(_is_own_id_origen([]))
        self.assertFalse(_is_own_id_origen([{"Comprobante_IdOrigen": ""}]))


class TiposDeSincronizacionTests(SimpleTestCase):
    def test_el_tipo_que_dejo_una_auditoria_vieja_no_esta_soportado(self):
        # Ocho jobs con este tipo llevaban desde el 02/07/2026 en "Bloqueada".
        self.assertFalse(is_supported_sync_type("manual_stock_restore_str_audit_20260701"))
        self.assertFalse(is_supported_sync_type(""))
        self.assertFalse(is_supported_sync_type(None))

    def test_los_tipos_que_el_worker_ejecuta_estan_soportados(self):
        for sync_type in SUPPORTED_SYNC_TYPES:
            self.assertTrue(is_supported_sync_type(sync_type), sync_type)


class FechaDeEmisionTests(SimpleTestCase):
    def test_rechaza_el_anio_tipeado_con_dos_digitos(self):
        # OS 29664: fecha_ingreso 0026-08-28 -> Bejerman contestaba
        # "Se han detectado inconsistencias en el archivo de texto".
        with self.assertRaises(BejermanRisError) as ctx:
            _assert_issue_date_plausible({"issueDate": "0026-08-28"})
        self.assertIn("0026-08-28", str(ctx.exception))

    def test_acepta_una_fecha_normal(self):
        _assert_issue_date_plausible({"issueDate": "2026-08-13"})

    def test_sin_fecha_no_opina(self):
        _assert_issue_date_plausible({})
