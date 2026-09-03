"""Cómo se limpia (y cómo NO) el cambio de equipo al pisar la resolución.

Módulo de puros `SimpleTestCase`: arma las cláusulas SET sin tocar Postgres, así que corre
en cualquier lado. Cubre la regla que separa las dos operaciones:

- El **cambio con equipo** es el resultado de la OS: si se resuelve como reparado, se limpia.
- La **parte de pago** es un hecho patrimonial: al equipo tomado se lo repara igual, y borrar
  el registro dejaría sueltos el pase a MG BIO y el movimiento a Desguace, que no se
  revierten, además de perder el corte que evita emitir un RSS por un equipo que es nuestro.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

import service.views.ingresos_views as vistas


class LimpiezaDelCambioSqlTests(SimpleTestCase):
    def _con_schema(self, tiene_swap=True, tiene_serial=True):
        return (
            patch.object(vistas, "_has_device_swap_schema", return_value=tiene_swap),
            patch.object(vistas, "_has_table_column", return_value=tiene_serial),
        )

    def _sets(self, **kwargs):
        swap, col = self._con_schema(**kwargs)
        with swap, col:
            return vistas.sets_con_limpieza_de_cambio("estado='reparado'", "resolucion='reparado'")

    def test_pisar_la_resolucion_conserva_la_parte_de_pago(self):
        sets = self._sets()
        # Cada columna se limpia sólo si NO es una parte de pago.
        for columna in (
            "serial_cambio",
            "device_cambio_id",
            "cambio_destino_viejo",
            "cambio_fecha",
            "cambio_registrado_por",
        ):
            self.assertIn(
                f"{columna} = CASE WHEN COALESCE(cambio_sin_entrega, FALSE) "
                f"THEN {columna} ELSE NULL END",
                sets,
            )
        # La marca misma nunca se pisa por este camino.
        self.assertNotIn("cambio_sin_entrega = FALSE", sets)

    def test_los_sets_del_llamador_van_primero_y_sin_placeholders_nuevos(self):
        # El orden importa: las vistas pasan sus propios %s y la limpieza no debe correrlos.
        sets = self._sets()
        self.assertTrue(sets.startswith("estado='reparado', resolucion='reparado',"))
        self.assertNotIn("%s", sets)

    def test_deshacer_explicito_borra_todo_incluida_la_marca(self):
        swap, col = self._con_schema()
        with swap, col:
            sets = vistas.clear_device_swap_sql_sets(preservar_parte_de_pago=False)
        self.assertIn("cambio_sin_entrega = FALSE", sets)
        self.assertIn("device_cambio_id = NULL", sets)
        self.assertNotIn(
            "device_cambio_id = CASE WHEN COALESCE(cambio_sin_entrega, FALSE) THEN device_cambio_id ELSE NULL END",
            sets,
        )

    def test_sin_el_schema_del_cambio_solo_se_toca_el_snapshot(self):
        # Base vieja: no existen las columnas nuevas, así que nombrarlas rompería el UPDATE.
        sets = self._sets(tiene_swap=False)
        self.assertEqual(sets, "estado='reparado', resolucion='reparado', serial_cambio = NULL")

    def test_sin_columna_de_snapshot_no_se_arma_nada_de_cambio(self):
        swap, col = self._con_schema(tiene_swap=False, tiene_serial=False)
        with swap, col:
            self.assertEqual(vistas.clear_device_swap_sql_sets(), [])
