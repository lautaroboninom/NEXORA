import datetime as dt
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from service.activity_audit import classify_activity_path
from service.management.commands.reconcile_legacy_derived_states import choose_reconciled_state
from service.views.derivaciones_views import (
    DerivarIngresoView,
    DevolverDerivacionView,
    EquiposDerivadosView,
)
from service.views.ingresos_views import EntregarIngresoView
from service.views.reportes_views import RemitoSalidaPdfView, RemitosSalidaBulkPdfView


def _without_atomic(method):
    while hasattr(method, "__wrapped__"):
        method = method.__wrapped__
    return method


def _request(data=None):
    return SimpleNamespace(
        data=data or {},
        user=SimpleNamespace(id=7, nombre="Operador"),
        user_id=7,
        query_params={},
        GET={},
    )


ACTIVE_DERIVATION = {
    "derivacion_id": 81,
    "derivacion_estado": "derivado",
    "derivacion_proveedor_id": 3,
    "derivacion_proveedor": "Laboratorio externo",
    "derivacion_fecha": dt.date(2026, 9, 1),
}


class DerivacionesWorkflowViewTest(SimpleTestCase):
    @patch("service.views.derivaciones_views.get_active_derivation")
    @patch("service.views.derivaciones_views.q")
    @patch("service.views.derivaciones_views.exec_void")
    @patch("service.views.helpers.exec_returning", return_value=81)
    @patch("service.views.derivaciones_views.require_permission")
    @patch("service.views.derivaciones_views._set_audit_user")
    def test_derivar_conserva_el_estado_operativo(
        self,
        _audit,
        _permission,
        _insert,
        exec_void,
        query,
        get_active,
    ):
        estados = (
            "ingresado",
            "diagnosticado",
            "presupuestado",
            "reparar",
            "reparado",
            "controlado_sin_defecto",
            "no_se_repara",
        )
        for offset, estado in enumerate(estados):
            with self.subTest(estado=estado):
                ingreso_id = 44 + offset
                query.side_effect = [
                    {"id": ingreso_id, "estado": estado},
                    {"id": 3},
                ]
                get_active.side_effect = [None, ACTIVE_DERIVATION]

                response = _without_atomic(DerivarIngresoView.post)(
                    DerivarIngresoView(),
                    _request({"proveedor_id": 3}),
                    ingreso_id,
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["estado"], estado)
                self.assertTrue(response.data["derivacion_activa"])
        all_sql = " ".join(str(call.args[0]) for call in exec_void.call_args_list).lower()
        self.assertNotIn("update ingresos", all_sql)

    @patch("service.views.derivaciones_views.notify_derivacion_devuelta")
    @patch("service.views.derivaciones_views.q")
    @patch("service.views.derivaciones_views.exec_void")
    @patch("service.views.derivaciones_views.require_permission")
    @patch("service.views.derivaciones_views._set_audit_user")
    def test_devolver_conserva_el_estado_y_notifica_una_vez(
        self,
        _audit,
        _permission,
        exec_void,
        query,
        notify,
    ):
        def answer(sql, _params=None, one=False):
            normalized = " ".join(str(sql).split()).lower()
            if "from equipos_derivados ed" in normalized:
                return {"id": 81, "estado": "en_servicio", "fecha_entrega": None}
            if "select estado::text as estado from ingresos" in normalized:
                return {"estado": "reparado"}
            if "from ingresos t" in normalized:
                return {"tech_email": ""}
            return None

        query.side_effect = answer
        response = _without_atomic(DevolverDerivacionView.post)(
            DevolverDerivacionView(),
            _request({"fecha_entrega": "2026-09-02"}),
            44,
            81,
        )

        self.assertEqual(response.data["estado"], "reparado")
        self.assertFalse(response.data["derivacion_activa"])
        self.assertEqual(notify.call_count, 1)
        all_sql = " ".join(str(call.args[0]) for call in exec_void.call_args_list).lower()
        self.assertIn("update equipos_derivados", all_sql)
        self.assertNotIn("update ingresos", all_sql)

    @patch("service.views.derivaciones_views.notify_derivacion_devuelta")
    @patch("service.views.derivaciones_views.q")
    @patch("service.views.derivaciones_views.exec_void")
    @patch("service.views.derivaciones_views.require_permission")
    @patch("service.views.derivaciones_views._set_audit_user")
    def test_devolucion_repetida_es_idempotente(
        self,
        _audit,
        _permission,
        exec_void,
        query,
        notify,
    ):
        query.side_effect = [
            {"id": 81, "estado": "devuelto", "fecha_entrega": dt.date(2026, 9, 2)},
            {"estado": "diagnosticado"},
        ]
        response = _without_atomic(DevolverDerivacionView.post)(
            DevolverDerivacionView(),
            _request(),
            44,
            81,
        )

        self.assertTrue(response.data["already_returned"])
        exec_void.assert_not_called()
        notify.assert_not_called()

    @patch("service.views.ingresos_views._owner_list_select_sql", return_value="'' AS propietario_nombre,")
    @patch("service.views.derivaciones_views.q", return_value=[])
    @patch("service.views.derivaciones_views.require_permission")
    def test_listado_derivados_separa_ambos_estados(self, _permission, query, _owner_sql):
        response = EquiposDerivadosView().get(_request())

        self.assertEqual(response.status_code, 200)
        sql = " ".join(str(query.call_args.args[0]).split()).lower()
        self.assertIn("t.estado::text as estado", sql)
        self.assertIn("ed.estado::text as derivacion_estado", sql)
        self.assertIn("ed.estado in ('derivado','en_servicio')", sql)


class DerivacionReleaseGuardTest(SimpleTestCase):
    @patch("service.views.reportes_views.get_active_derivation", return_value=ACTIVE_DERIVATION)
    @patch("service.views.reportes_views.q", return_value={"estado": "reparado", "resolucion": "reparado"})
    @patch("service.views.reportes_views.exec_void")
    @patch("service.views.reportes_views.require_roles")
    @patch("service.views.reportes_views._set_audit_user")
    def test_bloquea_orden_de_salida_antes_de_mutar(
        self,
        _audit,
        _roles,
        exec_void,
        _query,
        _active,
    ):
        response = _without_atomic(RemitoSalidaPdfView.get)(
            RemitoSalidaPdfView(),
            _request(),
            44,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "INGRESO_DERIVACION_ACTIVA")
        exec_void.assert_not_called()

    @patch("service.views.ingresos_views.q", return_value={"id": 44})
    @patch("service.views.ingresos_views.get_active_derivation", return_value=ACTIVE_DERIVATION)
    @patch("service.views.ingresos_views.require_roles")
    def test_bloquea_entrega_directa(self, _roles, _active, _query):
        response = _without_atomic(EntregarIngresoView.post)(
            EntregarIngresoView(),
            _request({"remito_salida": "RSS 0001-00000001"}),
            44,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "INGRESO_DERIVACION_ACTIVA")

    @patch("service.views.reportes_views.get_active_derivation")
    @patch("service.views.reportes_views.require_roles")
    def test_bloquea_lote_completo_antes_de_liberar(self, _roles, get_active):
        get_active.side_effect = [None, ACTIVE_DERIVATION]
        request = _request()
        request.GET = {"ids": "43,44", "skip_errors": "1"}

        with patch.object(RemitoSalidaPdfView, "get") as release_one:
            response = RemitosSalidaBulkPdfView().get(request)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["ingresos_derivados"], [44])
        release_one.assert_not_called()


class ReconcileLegacyDerivedStateTest(SimpleTestCase):
    def test_prioriza_resolucion_tecnica(self):
        state, evidence, conflict = choose_reconciled_state(
            {"resolucion": "no_se_encontro_falla"}
        )
        self.assertEqual(state, "controlado_sin_defecto")
        self.assertIn("resolución", evidence)
        self.assertFalse(conflict)

    def test_presupuesto_aprobado_va_a_reparar(self):
        state, _evidence, conflict = choose_reconciled_state(
            {"presupuesto_estado": "aprobado"}
        )
        self.assertEqual(state, "reparar")
        self.assertFalse(conflict)

    def test_diagnostico_o_presupuesto_va_a_diagnosticado(self):
        state, _evidence, conflict = choose_reconciled_state(
            {"quote_estado": "rechazado"}
        )
        self.assertEqual(state, "diagnosticado")
        self.assertFalse(conflict)

    def test_evidencia_de_cierre_es_conflicto(self):
        state, evidence, conflict = choose_reconciled_state(
            {"closed_event_state": "liberado"}
        )
        self.assertIsNone(state)
        self.assertIn("cierre", evidence)
        self.assertTrue(conflict)


class DerivationActivityAuditTest(SimpleTestCase):
    def test_clasifica_derivacion_y_devolucion_desde_audit_log(self):
        derived = classify_activity_path("/api/ingresos/44/derivar/", "POST")
        returned = classify_activity_path(
            "/api/ingresos/44/derivaciones/81/devolver/",
            "POST",
        )

        self.assertEqual(derived["activity_type"], "derivacion")
        self.assertEqual(derived["ingreso_id"], 44)
        self.assertEqual(returned["activity_type"], "devolucion_derivacion")
        self.assertEqual(returned["derivacion_id"], 81)
