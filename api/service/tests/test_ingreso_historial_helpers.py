from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from service.views.ingresos_views import (
    _compact_history_rows,
    _drop_mirrored_device_history_rows,
    _resolve_assigned_history_values,
)


class IngresoHistorialHelpersTest(SimpleTestCase):
    def _row(self, **kwargs):
        base = {
            "_id": 1,
            "ts": timezone.now(),
            "user_id": 1,
            "user_role": "admin",
            "user_nombre": "Admin",
            "table_name": "ingresos",
            "record_id": 10,
            "column_name": "alquiler_a",
            "old_value": None,
            "new_value": None,
        }
        base.update(kwargs)
        return base

    def test_compacta_cambios_consecutivos_del_mismo_campo(self):
        ts = timezone.now()
        rows = [
            self._row(_id=1, ts=ts, old_value=None, new_value="p"),
            self._row(
                _id=10,
                ts=ts + timedelta(seconds=5),
                table_name="devices",
                old_value=None,
                new_value="p",
            ),
            self._row(_id=2, ts=ts + timedelta(seconds=10), old_value="p", new_value="pu"),
            self._row(_id=3, ts=ts + timedelta(seconds=20), old_value="pu", new_value="pug"),
        ]

        compacted = _compact_history_rows(rows)
        ingreso_row = next(row for row in compacted if row["table_name"] == "ingresos")

        self.assertEqual(len(compacted), 2)
        self.assertIsNone(ingreso_row["old_value"])
        self.assertEqual(ingreso_row["new_value"], "pug")
        self.assertEqual(ingreso_row["_compact_count"], 3)

    def test_no_compacta_cambios_separados_por_tiempo(self):
        ts = timezone.now()
        rows = [
            self._row(_id=1, ts=ts, old_value=None, new_value="Cliente A"),
            self._row(_id=2, ts=ts + timedelta(minutes=10), old_value="Cliente A", new_value="Cliente B"),
        ]

        compacted = _compact_history_rows(rows)

        self.assertEqual(len(compacted), 2)

    def test_oculta_duplicado_de_equipo_cuando_refleja_el_cambio_del_ingreso(self):
        ts = timezone.now()
        rows = [
            self._row(_id=1, ts=ts, table_name="ingresos", old_value=None, new_value="Cliente A"),
            self._row(_id=2, ts=ts + timedelta(seconds=1), table_name="devices", old_value=None, new_value="Cliente A"),
        ]

        filtered = _drop_mirrored_device_history_rows(rows)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["table_name"], "ingresos")

    def test_resuelve_asignado_a_con_nombre_de_usuario(self):
        rows = [
            self._row(column_name="asignado_a", old_value="7", new_value="8"),
        ]

        with patch(
            "service.views.ingresos_views.q",
            return_value=[{"id": 7, "nombre": "Técnico Anterior"}, {"id": 8, "nombre": "Técnico Nuevo"}],
        ):
            resolved = _resolve_assigned_history_values(rows)

        self.assertEqual(resolved[0]["old_value"], "Técnico Anterior")
        self.assertEqual(resolved[0]["new_value"], "Técnico Nuevo")
