from datetime import datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from service.management.commands.run_billing_pending_notifications import next_daily_run


class BillingSummaryRunnerTests(SimpleTestCase):
    def test_proxima_ejecucion_es_hoy_si_aun_no_llego_la_hora(self):
        timezone_info = ZoneInfo("America/Argentina/Buenos_Aires")
        now = datetime(2026, 7, 14, 7, 30, tzinfo=timezone_info)

        scheduled_at = next_daily_run(now, time(8, 0))

        self.assertEqual(scheduled_at, datetime(2026, 7, 14, 8, 0, tzinfo=timezone_info))

    def test_proxima_ejecucion_es_manana_si_la_hora_ya_paso(self):
        timezone_info = ZoneInfo("America/Argentina/Buenos_Aires")
        now = datetime(2026, 7, 14, 8, 1, tzinfo=timezone_info)

        scheduled_at = next_daily_run(now, time(8, 0))

        self.assertEqual(scheduled_at, datetime(2026, 7, 15, 8, 0, tzinfo=timezone_info))

    @patch("service.management.commands.run_billing_pending_notifications.call_command")
    def test_once_delega_en_el_comando_de_resumen(self, mocked_call_command):
        call_command("run_billing_pending_notifications", once=True, limit=25)

        mocked_call_command.assert_called_once()
        args, kwargs = mocked_call_command.call_args
        self.assertEqual(args, ("send_billing_pending_notifications",))
        self.assertEqual(kwargs["limit"], 25)

    def test_rechaza_hora_invalida(self):
        with self.assertRaisesRegex(CommandError, "formato HH:MM"):
            call_command("run_billing_pending_notifications", once=True, at="25:00")
