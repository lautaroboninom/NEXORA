"""Hitos de la hoja de servicio: de dónde sale cada fecha del panel "Fechas importantes".

Los endpoints que mueven `ingresos.estado` tienen que dejar la traza en `ingreso_events`,
porque de ahí salen los hitos del panel y las métricas de tiempos. Estos tests son
`SimpleTestCase` con el acceso a base mockeado: prueban el armado de la consulta y las
reglas de fallback, no la escritura real (eso lo cubre el backfill contra la base).
"""

from contextlib import nullcontext
from unittest.mock import patch

from django.test import SimpleTestCase

from service.views import ingresos_views as iv


def _sin_savepoint():
    """El helper envuelve cada INSERT en su savepoint; acá no hay base que abrir."""
    return patch.object(iv.transaction, "atomic", lambda *a, **k: nullcontext())


class RegistrarIngresoEventTests(SimpleTestCase):
    def test_guarda_de_estado_cuando_es_un_estado_valido(self):
        with (
            _sin_savepoint(),
            patch.object(iv, "_table_exists", return_value=True),
            patch.object(iv, "exec_void") as exec_void,
        ):
            iv.registrar_ingreso_event(7, "reparado", de_estado="reparar", usuario_id=3, comentario="x")

        params = exec_void.call_args.args[1]
        self.assertEqual(params, [7, "reparar", "reparado", 3, "x"])

    def test_descarta_un_de_estado_que_no_existe_en_el_enum(self):
        # 'asignado' nunca entró al enum `ticket_state`: mandarlo haría fallar el INSERT.
        with (
            _sin_savepoint(),
            patch.object(iv, "_table_exists", return_value=True),
            patch.object(iv, "exec_void") as exec_void,
        ):
            iv.registrar_ingreso_event(7, "diagnosticado", de_estado="asignado")

        self.assertEqual(exec_void.call_args.args[1][1], "")

    def test_cae_al_insert_sin_casts_si_el_primero_falla(self):
        # Esquemas donde de_estado/a_estado son TEXT y no el enum.
        with (
            _sin_savepoint(),
            patch.object(iv, "_table_exists", return_value=True),
            patch.object(iv, "exec_void", side_effect=[Exception("no such type"), None]) as exec_void,
        ):
            iv.registrar_ingreso_event(7, "reparado", de_estado="reparar")

        self.assertEqual(exec_void.call_count, 2)
        self.assertNotIn("de_estado", exec_void.call_args.args[0])

    def test_no_explota_si_la_tabla_de_eventos_no_existe(self):
        with (
            _sin_savepoint(),
            patch.object(iv, "_table_exists", return_value=False),
            patch.object(iv, "exec_void") as exec_void,
        ):
            iv.registrar_ingreso_event(7, "reparado")

        exec_void.assert_not_called()


class EventDateHitosTests(SimpleTestCase):
    def _hitos(self):
        with (
            patch.object(iv, "_table_exists", return_value=True),
            patch.object(iv, "_has_table_column", return_value=True),
            patch.object(iv, "q", return_value={}) as q,
        ):
            iv._ingreso_event_date_hitos(42)
        return q.call_args

    def test_reparacion_cuenta_los_tres_cierres_de_reparacion(self):
        # Mismo criterio que la cola de Reparados: un equipo que sale como "controlado sin
        # defecto" o "no se repara" también terminó su etapa de reparación.
        sql, params = self._hitos().args

        self.assertIn("fecha_reparado", sql)
        self.assertEqual(
            params[0],
            ["reparado", "controlado_sin_defecto", "no_se_repara"],
        )

    def test_el_placeholder_de_estados_va_antes_que_los_del_where(self):
        # Está en el SELECT, así que psycopg lo consume primero: si se invierte el orden,
        # el ingreso_id se interpola como lista de estados y la consulta devuelve vacío.
        params = self._hitos().args[1]

        self.assertEqual(params[1:], [42, 42])


class QuoteDateHitosTests(SimpleTestCase):
    def test_toma_la_ultima_version_no_anulada(self):
        vigente = {"fecha_emitido": "2026-08-01", "fecha_aprobado": None, "fecha_rechazado": None}
        with (
            patch.object(iv, "_has_table", return_value=True),
            patch.object(iv, "_has_table_column", return_value=True),
            patch.object(iv, "q", return_value=vigente) as q,
        ):
            hitos = iv._ingreso_quote_date_hitos(42)

        self.assertEqual(hitos["fecha_presupuestado"], "2026-08-01")
        self.assertIn("anulado", q.call_args.args[0])

    def test_si_estan_todas_anuladas_cae_a_la_ultima_version(self):
        anulada = {"fecha_emitido": "2026-07-15", "fecha_aprobado": None, "fecha_rechazado": None}
        with (
            patch.object(iv, "_has_table", return_value=True),
            patch.object(iv, "_has_table_column", return_value=True),
            patch.object(iv, "q", side_effect=[None, anulada]) as q,
        ):
            hitos = iv._ingreso_quote_date_hitos(42)

        self.assertEqual(hitos["fecha_presupuestado"], "2026-07-15")
        self.assertEqual(q.call_count, 2)
        self.assertNotIn("anulado", q.call_args.args[0])

    def test_sin_tabla_de_presupuestos_devuelve_vacio(self):
        with patch.object(iv, "_has_table", return_value=False):
            self.assertEqual(iv._ingreso_quote_date_hitos(42), {})
