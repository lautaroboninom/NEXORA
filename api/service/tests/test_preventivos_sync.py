from datetime import date, datetime
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from service.views.preventivos_sync import sync_plan_from_ingreso_fecha_servicio


class PreventivosSyncTests(SimpleTestCase):
    def _aware(self, year, month, day, hour=12, minute=0, second=0):
        return timezone.make_aware(
            datetime(year, month, day, hour, minute, second),
            timezone.get_current_timezone(),
        )

    @patch("service.views.preventivos_sync.exec_void")
    @patch("service.views.preventivos_sync.q")
    def test_sync_noop_when_fecha_servicio_is_null(self, mock_q, mock_exec):
        mock_q.return_value = {"id": 10, "device_id": 22, "fecha_servicio": None}

        result = sync_plan_from_ingreso_fecha_servicio(10, actor_user_id=7)

        self.assertEqual(result["status"], "noop_no_fecha")
        self.assertEqual(result["device_id"], 22)
        mock_exec.assert_not_called()

    @patch("service.views.preventivos_sync.exec_void")
    @patch("service.views.preventivos_sync.q")
    def test_sync_noop_when_no_active_plan(self, mock_q, mock_exec):
        mock_q.side_effect = [
            {"id": 11, "device_id": 33, "fecha_servicio": self._aware(2026, 1, 10)},
            None,
        ]

        result = sync_plan_from_ingreso_fecha_servicio(11, actor_user_id=7)

        self.assertEqual(result["status"], "noop_no_plan")
        self.assertEqual(result["device_id"], 33)
        mock_exec.assert_not_called()

    @patch("service.views.preventivos_sync.exec_void")
    @patch("service.views.preventivos_sync.q")
    def test_sync_noop_when_fecha_is_older_than_ultima_revision(self, mock_q, mock_exec):
        mock_q.side_effect = [
            {"id": 12, "device_id": 44, "fecha_servicio": self._aware(2026, 1, 10)},
            {
                "id": 99,
                "periodicidad_valor": 30,
                "periodicidad_unidad": "dias",
                "ultima_revision_fecha": date(2026, 1, 11),
                "proxima_revision_fecha": date(2026, 2, 10),
            },
        ]

        result = sync_plan_from_ingreso_fecha_servicio(12, actor_user_id=7)

        self.assertEqual(result["status"], "noop_older_than_last")
        self.assertEqual(result["plan_id"], 99)
        mock_exec.assert_not_called()

    @patch("service.views.preventivos_sync.exec_void")
    @patch("service.views.preventivos_sync.q")
    def test_sync_updates_plan_and_recalculates_next_date(self, mock_q, mock_exec):
        mock_q.side_effect = [
            {"id": 13, "device_id": 55, "fecha_servicio": self._aware(2026, 1, 10)},
            {
                "id": 101,
                "periodicidad_valor": 30,
                "periodicidad_unidad": "dias",
                "ultima_revision_fecha": date(2026, 1, 1),
                "proxima_revision_fecha": date(2026, 1, 31),
            },
        ]

        result = sync_plan_from_ingreso_fecha_servicio(13, actor_user_id=7)

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["plan_id"], 101)
        self.assertEqual(result["fecha_servicio"], date(2026, 1, 10))
        self.assertEqual(result["proxima_revision_fecha"], date(2026, 2, 9))

        mock_exec.assert_called_once()
        sql, params = mock_exec.call_args[0]
        self.assertIn("updated_by", sql)
        self.assertEqual(params, [date(2026, 1, 10), date(2026, 2, 9), 7, 101])
