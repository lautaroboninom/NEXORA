"""Consejos de reintento y veredicto de «Ver en Bejerman».

Los mensajes son los textos reales del panel del 15/10/2026: los mismos que llevaron a
reintentar diez veces un RIS que no podía salir.
"""

from django.test import SimpleTestCase

from service.bejerman_retry_advice import (
    RETRY_DANGEROUS,
    RETRY_POINTLESS,
    RETRY_RECOMMENDED,
    advice_for_delivery_group,
    advice_for_ingreso_remito,
    advice_for_stock_job,
    can_change_source,
    repeated_serials_by_ingreso,
    stock_diagnosis,
)


def _job(last_error, status="blocked", **extra):
    return {"status": status, "last_error": last_error, **extra}


class StockJobAdviceTests(SimpleTestCase):
    def test_ya_existe_en_la_empresa_se_cierra_reintentando(self):
        advice = advice_for_stock_job(
            _job(
                "El comprobante no se importó. El comprobante no se importó. El mismo ya existe en "
                "la empresa debido a una importación anterior. "
            )
        )
        self.assertEqual(advice["kind"], "already_done")
        self.assertEqual(advice["retry"], RETRY_RECOMMENDED)

    def test_sin_serie_pide_editar(self):
        advice = advice_for_stock_job(_job("Número de serie requerido para sincronizar Bejerman"))
        self.assertEqual(advice["action"], "edit")
        self.assertEqual(advice["retry"], RETRY_POINTLESS)

    def test_articulo_distinto_del_confirmado_pide_ver_en_bejerman(self):
        advice = advice_for_stock_job(
            _job(
                "La partida H21BD502189 figura como artículo 1110001 en STR, pero el equipo tiene "
                "confirmado el artículo 1111001. No se emitió ningún movimiento: revise la partida o "
                "el mapeo del equipo."
            )
        )
        self.assertEqual(advice["action"], "diagnose")
        self.assertEqual(advice["retry"], RETRY_POINTLESS)

    def test_articulo_ambiguo_pide_elegirlo(self):
        advice = advice_for_stock_job(_job("Hay más de un artículo Bejerman posible para este modelo/variante"))
        self.assertEqual(advice["action"], "article")

    def test_sin_stock_en_origen_pide_ver_en_bejerman(self):
        for message in (
            "La partida N5109393 no tiene stock positivo en STR; no se emitió ningún movimiento.",
            "No se encontró stock positivo para la partida M1A23626094; no se puede mover a DES",
        ):
            with self.subTest(message=message):
                advice = advice_for_stock_job(_job(message))
                self.assertEqual(advice["kind"], "check_bejerman")
                self.assertEqual(advice["action"], "diagnose")

    def test_corte_de_conexion_en_stock_se_reintenta(self):
        # En el stock no hay riesgo de duplicar: Bejerman deduplica por IdOrigen.
        for message in (
            "Error HTTP Bejerman: HTTPConnectionPool(host='45.173.2.155', port=80): Read timed out.",
            "Máximos reintentos agotados: Error HTTP Bejerman: Read timed out.",
        ):
            with self.subTest(message=message):
                advice = advice_for_stock_job(_job(message, status="failed"))
                self.assertEqual(advice["kind"], "transient")
                self.assertEqual(advice["retry"], RETRY_RECOMMENDED)

    def test_sin_consejo_para_lo_que_no_fallo_ni_para_tipos_sin_implementacion(self):
        self.assertIsNone(advice_for_stock_job(_job("Read timed out", status="succeeded")))
        self.assertIsNone(
            advice_for_stock_job(
                _job("Tipo de sincronización no soportado: manual_stock_restore", supported_sync_type=False)
            )
        )
        self.assertIsNone(advice_for_stock_job(_job("")))


class IngresoRemitoAdviceTests(SimpleTestCase):
    def test_serie_repetida_en_el_remito_no_se_arregla_reintentando(self):
        advice = advice_for_ingreso_remito(
            {
                "status": "failed",
                "numero_serie": "Y662125180022",
                "last_error": "El comprobante no se importó. No hay stock suficiente en la partida "
                "Y662125180022 para el artículo 1105013 .",
            },
            repeated_serial_os=["OS 29881"],
        )
        self.assertEqual(advice["title"], "Serie repetida en el remito")
        self.assertIn("OS 29881", advice["detail"])
        self.assertIn("Y662125180022", advice["detail"])
        self.assertEqual(advice["retry"], RETRY_POINTLESS)

    def test_stock_insuficiente_en_la_partida(self):
        advice = advice_for_ingreso_remito(
            {
                "status": "failed",
                "last_error": "El comprobante no se importó. No hay stock suficiente en la partida "
                "P133516769DED para el artículo 1103010 .",
            }
        )
        self.assertEqual(advice["action"], "partida_stock")
        self.assertIn("P133516769DED", advice["detail"])

    def test_articulo_sin_stock_ingresado(self):
        advice = advice_for_ingreso_remito(
            {
                "status": "failed",
                "last_error": "El comprobante no se importó. No hay stock ingresado para el artículo 1199061 .",
            }
        )
        self.assertIn("El artículo 1199061 no tiene stock", advice["detail"])
        self.assertEqual(advice["retry"], RETRY_POINTLESS)

    def test_alta_de_stock_sin_confirmar(self):
        advice = advice_for_ingreso_remito(
            {
                "status": "failed",
                "last_error": "No se envió el RIS: no se pudo confirmar el alta de stock de Y662124390011 en el depósito STR.",
            }
        )
        self.assertEqual(advice["kind"], "check_bejerman")

    def test_en_revision_pide_buscar_en_bejerman(self):
        advice = advice_for_ingreso_remito({"status": "failed_review"})
        self.assertEqual(advice["action"], "verify")
        self.assertIsNone(advice_for_ingreso_remito({"status": "failed_review", "reviewState": "superseded"}))

    def test_corte_con_el_pedido_en_camino_es_peligroso(self):
        advice = advice_for_ingreso_remito(
            {"status": "failed", "last_error": "Error HTTP Bejerman: Read timed out."}
        )
        self.assertEqual(advice["kind"], "uncertain")
        self.assertEqual(advice["retry"], RETRY_DANGEROUS)

    def test_bejerman_caido_se_reintenta(self):
        advice = advice_for_ingreso_remito(
            {"status": "failed", "last_error": "HTTPConnectionPool: Failed to establish a new connection"}
        )
        self.assertEqual(advice["retry"], RETRY_RECOMMENDED)


class DeliveryGroupAdviceTests(SimpleTestCase):
    def test_orden_que_ya_salio_con_otro_remito(self):
        advice = advice_for_delivery_group(
            {"status": "failed", "last_error": "Error HTTP Bejerman: Read timed out."},
            issued_remitos=["RT R 00004-00004862"],
        )
        self.assertEqual(advice["kind"], "already_done")
        self.assertIn("RT R 00004-00004862", advice["detail"])
        self.assertEqual(advice["retry"], RETRY_DANGEROUS)

    def test_corte_en_vuelo_y_sin_respuesta(self):
        self.assertEqual(
            advice_for_delivery_group({"status": "failed", "last_error": "Read timed out."})["kind"],
            "uncertain",
        )
        self.assertEqual(advice_for_delivery_group({"status": "sent_unknown"})["kind"], "uncertain")

    def test_rechazo_de_bejerman_pide_corregir_la_orden(self):
        advice = advice_for_delivery_group(
            {"status": "failed", "last_error": "El comprobante no se importó. Falta el CUIT del cliente."}
        )
        self.assertEqual(advice["retry"], RETRY_POINTLESS)


class RepeatedSerialTests(SimpleTestCase):
    def test_detecta_la_serie_repetida_dentro_del_mismo_remito(self):
        rows = [
            {"ingreso_id": 29880, "numero_serie": "Y662125180022", "request_payload": {"ingresoIds": [29880, 29881]}},
            {"ingreso_id": 29881, "numero_serie": "y662125180022 ", "request_payload": {"ingresoIds": [29880, 29881]}},
            {"ingreso_id": 29879, "numero_serie": "Y662125180022", "request_payload": {"ingresoIds": [29879]}},
        ]
        self.assertEqual(
            repeated_serials_by_ingreso(rows),
            {29880: ["OS 29881"], 29881: ["OS 29880"]},
        )


class StockDiagnosisTests(SimpleTestCase):
    def test_ya_esta_en_el_destino(self):
        result = stock_diagnosis(
            serial="F1121504012",
            source_deposit="STR",
            target_deposit="STL",
            records=[{"deposito": "STL", "article_code": "1102001", "quantity": 1}],
            equipment_article="1102001",
            movable=True,
        )
        self.assertEqual(result["verdict"], "already_in_target")
        self.assertEqual([option["action"] for option in result["options"]], ["retry"])

    def test_esta_en_otro_deposito_ofrece_moverlo_desde_ahi(self):
        result = stock_diagnosis(
            serial="MZJ5S121916",
            source_deposit="STR",
            target_deposit="SAR",
            records=[{"deposito": "STL", "article_code": "1115003", "quantity": 1}],
            movable=True,
        )
        self.assertEqual(result["verdict"], "elsewhere")
        self.assertEqual(result["options"][0], {**result["options"][0], "action": "move_from", "deposit": "STL"})
        self.assertEqual(result["options"][-1]["action"], "discard")

    def test_sin_poder_cambiar_el_origen_no_ofrece_mover(self):
        result = stock_diagnosis(
            serial="MZJ5S121916",
            source_deposit="STR",
            target_deposit="SAR",
            records=[{"deposito": "STL", "article_code": "1115003", "quantity": 1}],
            movable=False,
        )
        self.assertNotIn("move_from", [option["action"] for option in result["options"]])

    def test_el_articulo_del_equipo_elige_cual_de_los_stocks_es_el_suyo(self):
        # OS 29837: la partida figura en STR como 1110001 y en VAL como 1111001.
        result = stock_diagnosis(
            serial="H21BD502189",
            source_deposit="STR",
            target_deposit="STL",
            records=[
                {"deposito": "STR", "article_code": "1110001", "quantity": 1},
                {"deposito": "VAL", "article_code": "1111001", "quantity": 1},
            ],
            equipment_article="1111001",
            movable=True,
        )
        self.assertEqual(result["verdict"], "elsewhere")
        self.assertEqual(result["options"][0]["deposit"], "VAL")
        self.assertEqual(len(result["locations"]), 2)

    def test_no_esta_en_ningun_deposito(self):
        result = stock_diagnosis(
            serial="N5132536", source_deposit="STR", target_deposit="VAL", records=[], movable=True
        )
        self.assertEqual(result["verdict"], "not_in_stock")
        self.assertIn("N5132536", result["options"][0]["reason"])

    def test_pase_a_des_sin_origen_fijo(self):
        en_str = stock_diagnosis(
            serial="M1",
            source_deposit="AUTO",
            target_deposit="DES",
            records=[{"deposito": "STR", "article_code": "A", "quantity": 1}],
        )
        self.assertEqual(en_str["verdict"], "in_source")
        ya_en_des = stock_diagnosis(
            serial="M1",
            source_deposit="AUTO",
            target_deposit="DES",
            records=[{"deposito": "DES", "article_code": "A", "quantity": 1}],
        )
        self.assertEqual(ya_en_des["verdict"], "already_in_target")

    def test_sin_serie_y_con_otro_articulo(self):
        self.assertEqual(
            stock_diagnosis(serial="", source_deposit="STR", target_deposit="STL", records=[])["verdict"],
            "no_serial",
        )
        otro = stock_diagnosis(
            serial="S1",
            source_deposit="STR",
            target_deposit="STL",
            records=[{"deposito": "STR", "article_code": "X", "quantity": 1}],
            equipment_article="Y",
        )
        self.assertEqual(otro["verdict"], "other_article")


class CanChangeSourceTests(SimpleTestCase):
    def test_solo_traslados_que_no_emitieron_nada(self):
        base = {"sync_type": "stock_location_transfer", "status": "blocked", "request_payload": {}}
        self.assertTrue(can_change_source(base))
        self.assertFalse(can_change_source({**base, "request_payload": {"transfer_phase": "sal_done"}}))
        self.assertFalse(can_change_source({**base, "request_payload": {"sal_done": True}}))
        self.assertFalse(can_change_source({**base, "lote_id": 3}))
        self.assertFalse(can_change_source({**base, "sync_type": "stock_to_desguace"}))
        self.assertFalse(can_change_source({**base, "status": "succeeded"}))
