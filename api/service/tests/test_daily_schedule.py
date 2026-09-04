"""Los dos runners diarios se salteaban el día si arrancaban después de la hora.

Un deploy al mediodía, el reboot de la madrugada que terminó tarde o un corte de luz
alcanzaban para que `next_daily_run` saltara directo a mañana: ese día nadie recibía
ni las alertas de la mañana ni el resumen de Cobranzas, y no quedaba rastro de que se
habían perdido. Estos tests fijan el comportamiento nuevo —correr al arrancar si la
hora ya pasó— y el viejo, que sigue valiendo cuando todavía no llegó.

Puros `SimpleTestCase`: no tocan la base.
"""

from datetime import datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import SimpleTestCase

from service.daily_schedule import already_due_today, next_daily_run


ZONA = ZoneInfo("America/Argentina/Buenos_Aires")


class _CorteDelLoop(Exception):
    """Sale del `while True` del runner en el primer sleep."""


class YaPasoLaHoraTests(SimpleTestCase):
    def test_todavia_no_llego_la_hora(self):
        self.assertFalse(already_due_today(datetime(2026, 7, 14, 7, 59, tzinfo=ZONA), time(8, 0)))

    def test_justo_en_la_hora_cuenta_como_pasada(self):
        self.assertTrue(already_due_today(datetime(2026, 7, 14, 8, 0, tzinfo=ZONA), time(8, 0)))

    def test_arranque_del_mediodia_despues_de_un_deploy(self):
        self.assertTrue(already_due_today(datetime(2026, 7, 14, 12, 30, tzinfo=ZONA), time(8, 0)))

    def test_arranque_de_madrugada_no_dispara_el_dia_anterior(self):
        # 02:00 de hoy no tiene que "recuperar" el resumen de ayer: sería un correo
        # a la madrugada por algo que ya se mandó.
        self.assertFalse(already_due_today(datetime(2026, 7, 14, 2, 0, tzinfo=ZONA), time(8, 0)))

    def test_sigue_calculando_bien_la_proxima_corrida(self):
        proxima = next_daily_run(datetime(2026, 7, 14, 8, 1, tzinfo=ZONA), time(8, 0))
        self.assertEqual(proxima, datetime(2026, 7, 15, 8, 0, tzinfo=ZONA))


class ResumenDeCobranzasAlArrancarTests(SimpleTestCase):
    RUNNER = "service.management.commands.run_billing_pending_notifications"

    def _correr(self, ahora):
        with patch(f"{self.RUNNER}.timezone.now", return_value=ahora), patch(
            f"{self.RUNNER}.time.sleep", side_effect=_CorteDelLoop
        ), patch(f"{self.RUNNER}.call_command") as comando:
            with self.assertRaises(_CorteDelLoop):
                call_command("run_billing_pending_notifications", at="08:00")
        return comando

    def test_arranque_tardio_manda_el_resumen_en_el_momento(self):
        comando = self._correr(datetime(2026, 7, 14, 12, 30, tzinfo=ZONA))

        comando.assert_called_once()
        self.assertEqual(comando.call_args.args, ("send_billing_pending_notifications",))

    def test_arranque_temprano_espera_a_la_hora(self):
        comando = self._correr(datetime(2026, 7, 14, 6, 0, tzinfo=ZONA))

        comando.assert_not_called()

    def test_no_catchup_desactiva_la_recuperacion(self):
        ahora = datetime(2026, 7, 14, 12, 30, tzinfo=ZONA)
        with patch(f"{self.RUNNER}.timezone.now", return_value=ahora), patch(
            f"{self.RUNNER}.time.sleep", side_effect=_CorteDelLoop
        ), patch(f"{self.RUNNER}.call_command") as comando:
            with self.assertRaises(_CorteDelLoop):
                call_command("run_billing_pending_notifications", at="08:00", no_catchup=True)

        comando.assert_not_called()


class AlertasDiariasAlArrancarTests(SimpleTestCase):
    RUNNER = "service.management.commands.run_daily_alerts"

    def _correr(self, ahora, **extra):
        with patch(f"{self.RUNNER}.timezone.now", return_value=ahora), patch(
            f"{self.RUNNER}.time.sleep", side_effect=_CorteDelLoop
        ), patch(f"{self.RUNNER}.close_old_connections"), patch(
            f"{self.RUNNER}.call_command"
        ) as comando:
            with self.assertRaises(_CorteDelLoop):
                call_command("run_daily_alerts", at="07:30", **extra)
        return comando

    def test_arranque_tardio_corre_las_tres_alertas(self):
        comando = self._correr(datetime(2026, 7, 14, 10, 0, tzinfo=ZONA))

        corridas = [llamada.args[0] for llamada in comando.call_args_list]
        self.assertEqual(
            corridas,
            ["send_presupuesto_alerts", "send_preventivo_alerts", "send_work_alerts"],
        )

    def test_arranque_temprano_no_corre_nada_todavia(self):
        comando = self._correr(datetime(2026, 7, 14, 5, 0, tzinfo=ZONA))

        comando.assert_not_called()

    def test_una_alerta_que_falla_no_frena_a_las_otras(self):
        ahora = datetime(2026, 7, 14, 10, 0, tzinfo=ZONA)
        with patch(f"{self.RUNNER}.timezone.now", return_value=ahora), patch(
            f"{self.RUNNER}.time.sleep", side_effect=_CorteDelLoop
        ), patch(f"{self.RUNNER}.close_old_connections"), patch(
            f"{self.RUNNER}.call_command",
            side_effect=[RuntimeError("Bejerman no responde"), None, None],
        ) as comando:
            with self.assertRaises(_CorteDelLoop):
                call_command("run_daily_alerts", at="07:30")

        self.assertEqual(comando.call_count, 3)

    def test_recicla_la_conexion_antes_de_correr(self):
        # Es el punto del arreglo: si Postgres se reinició mientras dormíamos, la
        # conexión vieja está rota y el trabajo diario fallaría todos los días igual.
        ahora = datetime(2026, 7, 14, 10, 0, tzinfo=ZONA)
        with patch(f"{self.RUNNER}.timezone.now", return_value=ahora), patch(
            f"{self.RUNNER}.time.sleep", side_effect=_CorteDelLoop
        ), patch(f"{self.RUNNER}.close_old_connections") as reciclar, patch(
            f"{self.RUNNER}.call_command"
        ):
            with self.assertRaises(_CorteDelLoop):
                call_command("run_daily_alerts", at="07:30")

        self.assertTrue(reciclar.called)
