"""Alta masiva de equipos propios y su transferencia VAL->STL en lote.

Dos piezas, las dos sin Postgres ni SDK (`SimpleTestCase`):

* `evaluar_filas`, la decisión fila por fila. Tiene que coincidir con lo que después hace el
  worker —sobre todo con el artículo confirmado a mano del modelo, el único que veta un
  movimiento— y tiene que ver los errores de carga típicos de Excel antes de grabar nada.
* el lote: una SAL y una ENT con un renglón por equipo, y cómo se retoma cuando no se sabe si un
  comprobante entró. Se ejercita contra un Bejerman de juguete que mueve stock de verdad, con las
  escrituras a la base reemplazadas por registros en memoria.
"""
from contextlib import ExitStack
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_sync import (
    JOB_STATUS_SUCCEEDED,
    LOTE_PHASE_DONE,
    LOTE_PHASE_ENT_PENDING,
    LOTE_PHASE_PENDING,
    LOTE_PHASE_REVIEW,
    LOTE_PHASE_SAL_DONE,
    LOTE_PHASE_SAL_PENDING,
    BejermanBlockedError,
    _is_own_id_origen,
    _reference_for_lote,
    _run_stock_lote,
    _stock_lote_comprobante_number,
    build_stock_lote_transfer_payloads,
)
from service.views.devices_alta_masiva_views import (
    ACCION_TRANSFERIR,
    ACCION_YA_EN_DESTINO,
    _source_deposits,
    evaluar_filas,
)

CABECERA = {
    "company_key": "SEPID",
    "source": "VAL",
    "target": "STL",
    "tipo_equipo": "CPAP",
    "marca_id": 5,
    "model_id": 88,
    "variante": "",
    "modelo_label": "BMC G2S A20",
}


def _fila(numero, serie, mg):
    return {"fila": numero, "numero_serie": serie, "numero_interno": mg}


def _stock(deposito, article_code="1103001"):
    return {"article_code": article_code, "deposito": deposito, "partida": "", "cantidad": 1}


class DepositoDeOrigenTests(SimpleTestCase):
    # El selector ofrecía sólo VAL porque la lista salía del cambio de equipo; en SAR (Sarmiento)
    # también hay equipos propios que van a la estantería (10/09/2026).
    def test_el_origen_ofrece_val_y_sar_y_nunca_el_destino(self):
        with patch("service.views.ingresos_views._swap_source_deposits", return_value=["VAL", "STL"]):
            with override_settings(BEJERMAN_TARGET_DEPOSIT="STL"):
                self.assertEqual(_source_deposits(), ["VAL", "SAR"])


class EvaluarFilasTests(SimpleTestCase):
    def _evaluar(self, filas, **cambios):
        contexto = {
            "cabecera": CABECERA,
            "stock_por_serie": {},
            "descripciones": {},
            "modelos_por_articulo": {},
            "articulo_confirmado": "",
            "articulo_mapeado": "",
            "series_existentes": {},
            "mg_existentes": {},
            **cambios,
        }
        return evaluar_filas(filas, **contexto)

    def test_unidad_en_val_se_transfiere_y_el_mg_queda_canonico(self):
        resultado = self._evaluar(
            [_fila(1, "E21BE708153", "9525")],
            stock_por_serie={"E21BE708153": [_stock("VAL")]},
            modelos_por_articulo={"1103001": {"model_id": 88, "marca": "BMC", "modelo": "G2S A20"}},
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok", fila)
        self.assertEqual(fila["accion"], ACCION_TRANSFERIR)
        self.assertEqual(fila["numero_interno"], "MG 9525")
        self.assertEqual(fila["article_code"], "1103001")
        self.assertEqual(resultado["resumen"]["transferir"], 1)

    def test_el_articulo_confirmado_del_modelo_veta_la_fila_antes_de_grabar(self):
        # El alta de a uno deja pasar este caso y el worker bloquea la transferencia después, con
        # el equipo ya creado. En el alta masiva tiene que frenar en la validación.
        resultado = self._evaluar(
            [_fila(1, "E21BE708153", "MG 9525")],
            stock_por_serie={"E21BE708153": [_stock("VAL", "1103001")]},
            articulo_confirmado="1103004",
            articulo_mapeado="1103004",
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "error")
        self.assertIn("1103004", " ".join(fila["errores"]))
        self.assertEqual(resultado["resumen"]["con_error"], 1)

    def test_articulo_sin_modelo_es_un_aviso_y_no_frena(self):
        resultado = self._evaluar(
            [_fila(1, "E21BE708153", "MG 9525")],
            stock_por_serie={"E21BE708153": [_stock("VAL")]},
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok")
        self.assertTrue(fila["avisos"])

    def test_la_serie_en_notacion_cientifica_y_las_columnas_cruzadas_frenan(self):
        resultado = self._evaluar([_fila(1, "2,1E+11", "MG 9525"), _fila(2, "MG 9526", "E21BE708154")])
        cientifica, cruzada = resultado["filas"]
        self.assertIn("notación científica", " ".join(cientifica["errores"]))
        self.assertIn("no es un número interno", " ".join(cruzada["errores"]))

    def test_repetidos_en_la_lista_se_marcan_en_las_dos_filas(self):
        resultado = self._evaluar(
            [_fila(1, "A1", "9525"), _fila(2, "A2", "MG 9525"), _fila(3, "a-1", "MG 9527")],
            stock_por_serie={"A1": [_stock("VAL")], "A2": [_stock("VAL")]},
        )
        errores = [" ".join(fila["errores"]) for fila in resultado["filas"]]
        self.assertIn("MG 9525 está repetido", errores[0])
        self.assertIn("MG 9525 está repetido", errores[1])
        self.assertIn("serie está repetida", errores[0])
        self.assertIn("serie está repetida", errores[2])

    def test_serie_o_mg_ya_cargados_frenan(self):
        resultado = self._evaluar(
            [_fila(1, "A1", "MG 9525")],
            stock_por_serie={"A1": [_stock("VAL")]},
            series_existentes={"A1": 4412},
            mg_existentes={"MG 9525": 4413},
        )
        errores = " ".join(resultado["filas"][0]["errores"])
        self.assertIn("#4412", errores)
        self.assertIn("#4413", errores)

    def test_ya_en_stl_no_mueve_stock(self):
        resultado = self._evaluar([_fila(1, "A1", "MG 9525")], stock_por_serie={"A1": [_stock("STL")]})
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok")
        self.assertEqual(fila["accion"], ACCION_YA_EN_DESTINO)
        self.assertEqual(resultado["resumen"]["ya_en_destino"], 1)

    def test_sin_stock_en_origen_ni_destino_frena(self):
        resultado = self._evaluar([_fila(1, "A1", "MG 9525")])
        self.assertIn("No tiene stock", " ".join(resultado["filas"][0]["errores"]))

    def test_la_misma_partida_en_val_y_stl_a_la_vez_frena(self):
        resultado = self._evaluar(
            [_fila(1, "A1", "MG 9525")],
            stock_por_serie={"A1": [_stock("VAL"), _stock("STL")]},
        )
        self.assertIn("a la vez", " ".join(resultado["filas"][0]["errores"]))

    def test_cpap_y_humidificador_con_la_misma_serie_desempata_el_mapeo(self):
        resultado = self._evaluar(
            [_fila(1, "A1", "MG 9525")],
            stock_por_serie={"A1": [_stock("VAL", "1103001"), _stock("VAL", "1301002")]},
            articulo_mapeado="1103001",
        )
        fila = resultado["filas"][0]
        self.assertEqual(fila["estado"], "ok", fila)
        self.assertEqual(fila["article_code"], "1103001")


class BejermanDeJuguete:
    """Stock por (depósito, partida, artículo). Los comprobantes lo mueven de verdad."""

    def __init__(self, stock):
        self.stock = dict(stock)
        self.enviados = []

    def stock_by_deposit_partida(self, deposit, serial):
        filas = [
            {"Art_Partida": partida, "Art_CodDeposito": deposito, "Art_CodGen": articulo, "Art_DispUM1": cantidad}
            for (deposito, partida, articulo), cantidad in self.stock.items()
            if partida == serial and cantidad > 0
        ]
        return {"DatosJSON": filas}

    def ingresar_lista_comprobantes_json(self, comprobantes):
        self.enviados.append(comprobantes)
        for renglon in comprobantes:
            clave = (
                renglon["Comprobante_ArtDeposito"],
                renglon["Comprobante_ArtPartida"],
                renglon["Comprobante_Art_CodGen"],
            )
            self.stock[clave] = self.stock.get(clave, 0) + renglon["Comprobante_CantidadUM1"]
        return {"Resultado": "OK"}


def _job(job_id, serie):
    return {
        "id": job_id,
        "device_id": 4400 + job_id,
        "numero_serie": serie,
        "article_code": "1103001",
        "lote_id": 7,
        "request_payload": {},
        "response_payload": {},
    }


@override_settings(BEJERMAN_SYNC_STOCK_VERIFY_DELAY_MS=0)
class LoteDeTransferenciasTests(SimpleTestCase):
    def setUp(self):
        self.fases = []
        self.jobs = {}
        self.desprendidos = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        prefix = "service.bejerman_sync."
        stack.enter_context(patch(prefix + "validate_bejerman_config", return_value=None))
        stack.enter_context(patch(prefix + "device_targets_rental_stock", return_value=True))
        stack.enter_context(patch(prefix + "observacion_partida_de_equipo", return_value=""))
        stack.enter_context(patch(prefix + "_mapping_article_codes_for_job", return_value=("", "")))
        stack.enter_context(patch(prefix + "_update_stock_lote", side_effect=self._update_lote))
        stack.enter_context(patch(prefix + "_finish_job", side_effect=self._finish_job))
        stack.enter_context(
            patch(
                prefix + "_detach_stock_lote_member",
                side_effect=lambda job, reason: self.desprendidos.append((job["id"], reason)),
            )
        )
        stack.enter_context(patch(prefix + "_release_stock_lote_leftovers", return_value=None))

    def _update_lote(self, lote_id, *, phase=None, request_payload=None, response_payload=None, error=None):
        if phase:
            self.fases.append(phase)

    def _finish_job(self, job_id, status, **kwargs):
        self.jobs[job_id] = (status, kwargs)

    def _lote(self, phase=LOTE_PHASE_PENDING, **request_payload):
        return {
            "id": 7,
            "phase": phase,
            "source_deposit": "VAL",
            "target_deposit": "STL",
            "request_payload": request_payload,
            "response_payload": {},
            "last_error": None,
        }

    def _congelado(self, series, plan=0):
        reference = _reference_for_lote(7)
        items = [
            {"job_id": index, "device_id": 4400 + index, "serial": serie, "article_code": "1103001"}
            for index, serie in enumerate(series, start=1)
        ]
        number = _stock_lote_comprobante_number(reference, plan)
        sal, ent = build_stock_lote_transfer_payloads(reference, number, items, source="VAL", target="STL")
        return {
            "reference": reference,
            "plan": plan,
            "comprobante_numero": number,
            "items": items,
            "sal_comprobantes": sal,
            "ent_comprobantes": ent,
        }

    def test_mueve_varios_equipos_con_una_sal_y_una_ent(self):
        bejerman = BejermanDeJuguete(
            {("VAL", "S1", "1103001"): 1, ("VAL", "S2", "1103001"): 1, ("STL", "S3", "1103001"): 1}
        )
        _run_stock_lote(self._lote(), [_job(1, "S1"), _job(2, "S2"), _job(3, "S3")], bejerman)

        self.assertEqual(len(bejerman.enviados), 2)
        sal, ent = bejerman.enviados
        self.assertEqual([r["Comprobante_ArtPartida"] for r in sal], ["S1", "S2"])
        self.assertEqual({r["Comprobante_IdOrigen"] for r in sal}, {"NEXORA-LOTE-7-SAL-VAL"})
        self.assertEqual({r["Comprobante_IdOrigen"] for r in ent}, {"NEXORA-LOTE-7-ENT-STL"})
        self.assertEqual(len({r["Comprobante_Numero"] for r in sal + ent}), 1)
        self.assertEqual(bejerman.stock[("VAL", "S1", "1103001")], 0)
        self.assertEqual(bejerman.stock[("STL", "S2", "1103001")], 1)
        # S3 ya estaba en STL: se da por hecho sin renglón en los comprobantes.
        self.assertEqual(self.jobs[3][0], JOB_STATUS_SUCCEEDED)
        self.assertTrue(self.jobs[3][1]["request_payload"]["idempotent"])
        self.assertEqual(self.jobs[1][0], JOB_STATUS_SUCCEEDED)
        self.assertEqual(self.jobs[2][0], JOB_STATUS_SUCCEEDED)
        self.assertEqual(
            self.fases,
            [LOTE_PHASE_SAL_PENDING, LOTE_PHASE_SAL_DONE, LOTE_PHASE_ENT_PENDING, LOTE_PHASE_DONE],
        )

    def test_una_unidad_que_no_esta_en_origen_sale_del_lote_sin_frenar_a_las_demas(self):
        bejerman = BejermanDeJuguete({("VAL", "S1", "1103001"): 1})
        _run_stock_lote(self._lote(), [_job(1, "S1"), _job(2, "S2")], bejerman)
        self.assertEqual([job_id for job_id, _ in self.desprendidos], [2])
        self.assertEqual([r["Comprobante_ArtPartida"] for r in bejerman.enviados[0]], ["S1"])

    def test_sal_que_entro_sin_confirmarse_no_se_reenvia(self):
        # El worker se cayó después de mandar la SAL: el stock ya no está en VAL.
        bejerman = BejermanDeJuguete({})
        congelado = self._congelado(["S1", "S2"])
        _run_stock_lote(self._lote(LOTE_PHASE_SAL_PENDING, **congelado), [_job(1, "S1"), _job(2, "S2")], bejerman)
        self.assertEqual(bejerman.enviados, [congelado["ent_comprobantes"]])
        self.assertEqual(bejerman.stock[("STL", "S1", "1103001")], 1)
        self.assertEqual(self.fases[-1], LOTE_PHASE_DONE)

    def test_sal_que_quedo_a_medias_manda_el_lote_a_revision_sin_emitir(self):
        bejerman = BejermanDeJuguete({("VAL", "S1", "1103001"): 1})
        lote = self._lote(LOTE_PHASE_SAL_PENDING, **self._congelado(["S1", "S2"]))
        with self.assertRaises(BejermanBlockedError):
            _run_stock_lote(lote, [_job(1, "S1"), _job(2, "S2")], bejerman)
        self.assertEqual(bejerman.enviados, [])
        self.assertEqual(self.fases, [LOTE_PHASE_REVIEW])

    def test_sal_que_no_entro_se_replanifica_con_otro_numero_y_el_mismo_id_origen(self):
        bejerman = BejermanDeJuguete({("VAL", "S1", "1103001"): 1, ("VAL", "S2", "1103001"): 1})
        congelado = self._congelado(["S1", "S2"])
        _run_stock_lote(self._lote(LOTE_PHASE_SAL_PENDING, **congelado), [_job(1, "S1"), _job(2, "S2")], bejerman)
        sal = bejerman.enviados[0]
        self.assertEqual({r["Comprobante_IdOrigen"] for r in sal}, {"NEXORA-LOTE-7-SAL-VAL"})
        self.assertNotEqual(sal[0]["Comprobante_Numero"], congelado["comprobante_numero"])
        self.assertEqual(bejerman.stock[("STL", "S2", "1103001")], 1)

    def test_ent_que_ya_entro_no_se_reenvia(self):
        bejerman = BejermanDeJuguete({("STL", "S1", "1103001"): 1, ("STL", "S2", "1103001"): 1})
        lote = self._lote(LOTE_PHASE_ENT_PENDING, **self._congelado(["S1", "S2"]))
        _run_stock_lote(lote, [_job(1, "S1"), _job(2, "S2")], bejerman)
        self.assertEqual(bejerman.enviados, [])
        self.assertEqual(self.jobs[1][0], JOB_STATUS_SUCCEEDED)
        self.assertEqual(self.fases, [LOTE_PHASE_DONE])

    def test_si_una_unidad_volvio_al_origen_despues_de_la_sal_no_manda_la_ent(self):
        bejerman = BejermanDeJuguete({("VAL", "S2", "1103001"): 1})
        lote = self._lote(LOTE_PHASE_SAL_DONE, **self._congelado(["S1", "S2"]))
        with self.assertRaises(BejermanBlockedError):
            _run_stock_lote(lote, [_job(1, "S1"), _job(2, "S2")], bejerman)
        self.assertEqual(bejerman.enviados, [])
        self.assertEqual(self.fases, [LOTE_PHASE_REVIEW])


class IdentidadDelLoteTests(SimpleTestCase):
    def test_el_id_origen_del_lote_es_propio(self):
        self.assertEqual(_reference_for_lote(12), "NEXORA-LOTE-12")
        self.assertTrue(_is_own_id_origen([{"Comprobante_IdOrigen": "NEXORA-LOTE-12-SAL-VAL"}]))

    def test_sin_identificador_no_arma_una_referencia_ambigua(self):
        with self.assertRaises(BejermanBlockedError):
            _reference_for_lote(None)

    def test_el_numero_queda_en_el_rango_de_movimientos_sin_os(self):
        numero = _stock_lote_comprobante_number("NEXORA-LOTE-12")
        self.assertTrue(numero.startswith("9"))
        self.assertEqual(len(numero), 8)
        self.assertEqual(numero, _stock_lote_comprobante_number("NEXORA-LOTE-12"))
        self.assertNotEqual(numero, _stock_lote_comprobante_number("NEXORA-LOTE-12", plan=1))
