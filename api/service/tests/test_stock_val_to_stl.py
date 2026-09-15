"""Transferencia VAL->STL de un equipo propio recien dado de alta.

Es el unico movimiento de stock que NO nace de una orden de servicio: el equipo se da de
alta en Equipos con su MG y sin ingreso. Lo que se cubre aca es lo que cambia por esa
ausencia, que es donde estan los riesgos:

* el contexto del equipo se resuelve desde `devices` y no desde `ingresos`;
* el `Comprobante_IdOrigen` lleva su propio espacio de nombres (`NEXORA-DEV-`). Con el
  prefijo viejo saldria `NEXORA-OS-None-ENT-STL` para TODOS los equipos, `_is_own_id_origen`
  lo daria por propio y `_send_stock_comprobantes` absorberia como "ya importado" el choque
  entre dos equipos distintos: stock perdido, no un detalle cosmetico;
* la guarda de equipo propio se evalua tambien device-level, en las dos mitades (encolar y
  ejecutar), porque si divergen vuelve el bug de los MG parados en STC.

SimpleTestCase a proposito: no necesitan Postgres ni el SDK.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from service.bejerman_sync import (
    SYNC_TYPE_STOCK_VAL_TO_STL,
    TARGET_STOCK_RESTORE_SYNC_TYPES,
    BejermanBlockedError,
    _is_own_id_origen,
    _job_context,
    _reference_for_job,
    _stock_transfer_comprobante_number,
    build_stock_transfer_in_payload,
    build_stock_transfer_out_payload,
    device_targets_rental_stock,
    enqueue_stock_val_to_stl_for_device,
    is_supported_sync_type,
)

DEVICE = {
    "ingreso_id": None,
    "device_id": 4412,
    "customer_id": 12,
    "numero_serie": "B3CBE407167",
    "numero_interno": "MG 7450",
    "n_de_control": "",
    "mg_estado": "activo",
    "mg_venta_fecha": None,
    "mg_venta_factura_numero": "",
    "mg_venta_remito_numero": "",
    "mg_venta_customer_id": None,
    "model_id": 88,
    "marca": "BMC",
    "modelo": "G3 B30VT",
    "variante": "",
    "variante_norm": "",
    "empresa_bejerman": "SEPID",
    "empresa_facturar": "SEPID",
}

JOB = {
    "id": 901,
    "ingreso_id": None,
    "device_id": 4412,
    "sync_type": SYNC_TYPE_STOCK_VAL_TO_STL,
    "numero_serie": "B3CBE407167",
    "source_deposit": "VAL",
    "target_deposit": "STL",
    "company_key": "SEPID",
    "attempts": 0,
}


def device(**cambios):
    return {**DEVICE, **cambios}


class TipoDeSincronizacionTests(SimpleTestCase):
    def test_el_worker_sabe_ejecutar_el_tipo_nuevo(self):
        # Un `sync_type` fuera de la whitelist muere al nacer: falla, se bloquea y se queda
        # en el panel ofreciendo un "Reintentar" que no puede funcionar.
        self.assertTrue(is_supported_sync_type(SYNC_TYPE_STOCK_VAL_TO_STL))

    def test_el_boton_de_restaurar_destino_lo_alcanza(self):
        self.assertIn(SYNC_TYPE_STOCK_VAL_TO_STL, TARGET_STOCK_RESTORE_SYNC_TYPES)


class ReferenciaDelComprobanteTests(SimpleTestCase):
    def test_sin_ingreso_la_identidad_es_el_equipo(self):
        self.assertEqual(_reference_for_job(None, 4412), "NEXORA-DEV-4412")

    def test_con_ingreso_no_cambia_nada(self):
        self.assertEqual(_reference_for_job(29516, 4412), "NEXORA-OS-29516")

    def test_sin_ingreso_ni_equipo_no_arma_una_referencia_ambigua(self):
        with self.assertRaises(BejermanBlockedError):
            _reference_for_job(None, None)

    def test_el_id_origen_del_equipo_es_propio_pero_distinguible(self):
        self.assertTrue(_is_own_id_origen([{"Comprobante_IdOrigen": "NEXORA-DEV-4412-ENT-STL"}]))
        self.assertTrue(_is_own_id_origen([{"Comprobante_IdOrigen": "NEXORA-OS-29516-ENT-STL"}]))
        self.assertFalse(_is_own_id_origen([{"Comprobante_IdOrigen": "IMPORT-EXTERNO-1"}]))


class PayloadTests(SimpleTestCase):
    def setUp(self):
        observation = patch("service.bejerman_sync.observacion_partida_de_equipo", return_value="MG 7450")
        observation.start()
        self.addCleanup(observation.stop)

    def test_la_sal_descuenta_de_val_con_id_origen_del_equipo(self):
        item = build_stock_transfer_out_payload(JOB, "1104011")[0]
        self.assertEqual(item["Comprobante_IdOrigen"], "NEXORA-DEV-4412-SAL-VAL")
        self.assertEqual(item["Comprobante_ArtDeposito"], "VAL")
        self.assertEqual(item["Comprobante_CantidadUM1"], -1)
        self.assertEqual(item["Comprobante_ArtPartida"], "B3CBE407167")

    def test_el_ent_suma_en_stl(self):
        item = build_stock_transfer_in_payload(JOB, "1104011")[0]
        self.assertEqual(item["Comprobante_IdOrigen"], "NEXORA-DEV-4412-ENT-STL")
        self.assertEqual(item["Comprobante_ArtDeposito"], "STL")
        self.assertEqual(item["Comprobante_CantidadUM1"], 1)

    def test_la_sal_y_el_ent_comparten_numero(self):
        sal = build_stock_transfer_out_payload(JOB, "1104011")[0]
        ent = build_stock_transfer_in_payload(JOB, "1104011")[0]
        self.assertEqual(sal["Comprobante_Numero"], ent["Comprobante_Numero"])

    def test_dos_equipos_distintos_no_comparten_id_origen(self):
        # El bug que evita el prefijo nuevo: con `NEXORA-OS-None-...` los dos serian iguales
        # y el segundo se absorberia como "ya importado" sin mover nada.
        otro = dict(JOB, device_id=4413, numero_serie="B3CBE407168")
        self.assertNotEqual(
            build_stock_transfer_in_payload(JOB, "1104011")[0]["Comprobante_IdOrigen"],
            build_stock_transfer_in_payload(otro, "1104011")[0]["Comprobante_IdOrigen"],
        )

    def test_el_numero_no_pisa_el_espacio_de_las_ordenes_de_servicio(self):
        numero = _stock_transfer_comprobante_number(JOB)
        self.assertEqual(len(numero), 8)
        # Los numerados por OS salen de `ingreso_id % 1e8`; el rango 9xxxxxxx es el que ya
        # usan los movimientos sin OS (correcciones de stock, force_stock_entry).
        self.assertGreaterEqual(int(numero), 90000000)

    def test_el_numero_se_corre_al_reintentar_sin_salirse_del_rango(self):
        # El corrimiento tiene que quedar dentro de 9xxxxxxx: si diera la vuelta, un
        # reintento aterrizaria en el espacio de los comprobantes numerados por OS.
        numeros = {
            _stock_transfer_comprobante_number(dict(JOB, attempts=intento))
            for intento in range(6)
        }
        self.assertEqual(len(numeros), 6)
        for numero in numeros:
            self.assertGreaterEqual(int(numero), 90000000)
            self.assertLess(int(numero), 100000000)

    def test_el_reintento_de_un_equipo_del_borde_del_rango_no_da_la_vuelta(self):
        # Se busca un device_id cuyo numero base caiga cerca del techo del rango, que es
        # donde el modulo mal puesto rompia.
        for device_id in range(1, 4000):
            job = dict(JOB, device_id=device_id)
            if int(_stock_transfer_comprobante_number(job)) > 99800000:
                for intento in range(1, 6):
                    numero = int(_stock_transfer_comprobante_number(dict(job, attempts=intento)))
                    self.assertGreaterEqual(numero, 90000000)
                    self.assertLess(numero, 100000000)
                return
        self.skipTest("ningun device_id de la muestra cae cerca del techo del rango")

    def test_el_numero_es_determinístico_para_el_mismo_equipo(self):
        self.assertEqual(
            _stock_transfer_comprobante_number(JOB),
            _stock_transfer_comprobante_number(dict(JOB)),
        )


class ContextoDelJobTests(SimpleTestCase):
    def test_sin_ingreso_el_contexto_sale_de_la_ficha_del_equipo(self):
        with patch("service.bejerman_sync._device_context", return_value=device()) as por_equipo:
            contexto = _job_context(JOB)
        por_equipo.assert_called_once_with(4412, "SEPID")
        self.assertEqual(contexto["model_id"], 88)

    def test_con_ingreso_sigue_saliendo_de_la_orden_de_servicio(self):
        with (
            patch("service.bejerman_sync._ingreso_context", return_value=device(ingreso_id=29516)) as por_os,
            patch("service.bejerman_sync._device_context") as por_equipo,
        ):
            _job_context(dict(JOB, ingreso_id=29516))
        por_os.assert_called_once_with(29516)
        por_equipo.assert_not_called()

    def test_un_job_sin_ingreso_ni_equipo_se_bloquea_en_vez_de_reventar(self):
        with self.assertRaises(BejermanBlockedError):
            _job_context(dict(JOB, device_id=None))


class GuardaDeEquipoPropioTests(SimpleTestCase):
    def _corre(self, row):
        with (
            patch("service.bejerman_sync._device_context", return_value=row),
            patch("service.bejerman_sync._mg_owner_customer_id", return_value=None),
        ):
            return device_targets_rental_stock(row["device_id"])

    def test_un_equipo_con_numero_interno_va_a_la_estanteria(self):
        self.assertTrue(self._corre(device()))

    def test_acepta_el_numero_interno_con_ceros_a_la_izquierda(self):
        self.assertTrue(self._corre(device(numero_interno="MG 001678")))

    def test_un_mg_vendido_ya_no_es_del_parque_de_alquiler(self):
        self.assertFalse(self._corre(device(mg_estado="inactivo_venta")))
        self.assertFalse(self._corre(device(mg_venta_factura_numero="A-0001")))

    def test_un_equipo_de_cliente_no_puede_transferirse_a_stl(self):
        self.assertFalse(self._corre(device(numero_interno="")))


class EncoladoTests(SimpleTestCase):
    def _encola(self, row, **kwargs):
        with (
            patch("service.bejerman_sync._device_context", return_value=row),
            patch("service.bejerman_sync._mg_owner_customer_id", return_value=None),
            patch("service.bejerman_sync.q", return_value={"id": 901, "status": "pending", "attempts": 0}) as sql,
        ):
            job = enqueue_stock_val_to_stl_for_device(row["device_id"], **kwargs)
        return job, sql

    def test_encola_el_job_con_origen_y_destino_correctos(self):
        _, sql = self._encola(device(), company_key="SEPID", source_deposit="VAL")
        params = sql.call_args[0][1]
        self.assertEqual(params[0], SYNC_TYPE_STOCK_VAL_TO_STL)
        self.assertIsNone(params[1])  # ingreso_id: el equipo no tiene orden de servicio
        self.assertEqual(params[2], 4412)  # device_id
        self.assertEqual(params[5], "VAL")
        self.assertEqual(params[6], "STL")
        self.assertEqual(params[7], "SEPID")

    def test_deduplica_por_equipo_y_no_por_ingreso(self):
        # El indice unico de jobs sin ingreso es (sync_type, device_id): nombrar el predicado
        # es obligatorio porque es parcial, si no Postgres no lo infiere.
        _, sql = self._encola(device(), company_key="SEPID")
        sentencia = sql.call_args[0][0]
        self.assertIn("ON CONFLICT (sync_type, device_id) WHERE ingreso_id IS NULL", sentencia)

    def test_respeta_el_deposito_de_origen_elegido(self):
        _, sql = self._encola(device(), company_key="SEPID", source_deposit="SAR")
        self.assertEqual(sql.call_args[0][1][5], "SAR")

    def test_guarda_el_articulo_del_renglon_de_stock(self):
        _, sql = self._encola(device(), company_key="SEPID", article_code="1104011")
        self.assertEqual(sql.call_args[0][1][11], "1104011")

    def test_sin_articulo_no_escribe_una_cadena_vacia(self):
        # El INSERT usa NULLIF: un '' en `article_code` haria que la cadena de resolucion
        # crea que el articulo ya esta decidido.
        _, sql = self._encola(device(), company_key="SEPID")
        self.assertEqual(sql.call_args[0][1][11], "")
        self.assertIn("NULLIF(%s,'')", sql.call_args[0][0])

    def test_no_encola_un_equipo_que_ya_se_vendio(self):
        with self.assertRaises(BejermanBlockedError):
            self._encola(device(mg_estado="inactivo_venta"), company_key="SEPID")

    def test_no_encola_una_transferencia_de_un_deposito_a_si_mismo(self):
        with self.assertRaises(BejermanBlockedError):
            self._encola(device(), company_key="SEPID", source_deposit="STL")
