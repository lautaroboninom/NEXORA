from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase

from service import bejerman_delivery
from service.views import auth_views, devices_views, ingresos_views
from service.views.derivaciones_views import DerivarIngresoView
from service.views.helpers_impl import repair_pk_sequence


def _request(data=None):
    return SimpleNamespace(
        data=data or {},
        user=SimpleNamespace(id=7, nombre="Operador"),
        user_id=7,
    )


@skipUnless(connection.vendor == "postgresql", "La regresión corresponde a PostgreSQL")
class TransactionSavepointTests(TransactionTestCase):
    reset_sequences = False

    @staticmethod
    def _raise_database_error(*_args, **_kwargs):
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM nexora_tabla_inexistente_para_probar_savepoint")

    def _assert_connection_usable(self):
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_falla_del_registro_de_acceso_no_aborta_el_login(self):
        with transaction.atomic():
            with patch.object(auth_views, "q", side_effect=self._raise_database_error):
                auth_views._record_successful_login(_request(), 7)
            self._assert_connection_usable()

    def test_registro_auxiliar_de_remito_no_aborta_la_transaccion_principal(self):
        with transaction.atomic():
            # Aunque el esquema no exista, o el grupo viole su FK, el registro
            # auxiliar debe fallar sin contaminar la emisión que lo contiene.
            with patch.object(bejerman_delivery, "_table_exists", return_value=True):
                bejerman_delivery._record_bejerman_integration_attempt(
                    "grupo-inexistente-savepoint",
                    ["orden-inexistente"],
                    None,
                    "test",
                )
            self._assert_connection_usable()

    def test_introspeccion_de_equipos_no_aborta_una_edicion(self):
        with transaction.atomic():
            with patch.object(devices_views, "q", side_effect=self._raise_database_error):
                self.assertFalse(devices_views._has_table_column("devices", "numero_serie"))
            self._assert_connection_usable()

    def test_introspeccion_de_ingresos_no_aborta_un_alta(self):
        with transaction.atomic():
            with patch.object(ingresos_views, "q", side_effect=self._raise_database_error):
                self.assertFalse(ingresos_views._has_table_column("ingresos", "empresa_bejerman"))
            self._assert_connection_usable()

    def test_reparacion_best_effort_de_secuencia_no_aborta_el_alta(self):
        with transaction.atomic():
            repair_pk_sequence("nexora_tabla_inexistente_para_probar_savepoint")
            self._assert_connection_usable()

    def test_falla_del_bloqueo_de_derivacion_no_contamina_las_consultas_siguientes(self):
        def query(sql, _params=None, one=False):
            self._assert_connection_usable()
            normalized = " ".join(str(sql).split()).lower()
            if "from ingresos" in normalized:
                return {"id": 44, "estado": "diagnosticado"}
            if "from proveedores_externos" in normalized:
                return {"id": 3}
            return None

        with (
            patch("service.views.derivaciones_views.require_permission"),
            patch("service.views.derivaciones_views._set_audit_user"),
            patch("service.views.derivaciones_views.exec_void", side_effect=self._raise_database_error),
            patch("service.views.derivaciones_views.q", side_effect=query),
            patch(
                "service.views.derivaciones_views.get_active_derivation",
                side_effect=[None, {"id": 81, "estado": "derivado"}],
            ),
            patch("service.views.helpers.exec_returning", return_value=81),
        ):
            response = DerivarIngresoView().post(
                _request({"proveedor_id": 3}),
                44,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["derivacion_activa"])
        self._assert_connection_usable()
