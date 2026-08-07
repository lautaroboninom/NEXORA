from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from service.bejerman_sync_alerts import (
    MANAGEMENT_NOTIFICATION_ROLES,
    NOTIFICATION_KEY,
    queue_stale_blocked_bejerman_job_alerts,
)


NOW = datetime(2026, 7, 27, 15, 0, tzinfo=datetime_timezone.utc)


def _blocked_job(
    *,
    job_id: int = 1070,
    sync_type: str = "stock_str_to_stc",
    updated_at: datetime | None = None,
    last_error: str = (
        "El comprobante no se importó. "
        "No ha sido posible actualizar los datos en la tabla."
    ),
) -> dict:
    return {
        "id": job_id,
        "sync_type": sync_type,
        "ingreso_id": 29473,
        "numero_serie": "SN-29473",
        "source_deposit": "STR",
        "target_deposit": "STC",
        "company_key": "SEPID",
        "status": "blocked",
        "attempts": 2,
        "last_error": last_error,
        "created_at": NOW - timedelta(hours=4),
        "updated_at": updated_at or NOW - timedelta(hours=3),
    }


def _queue_result(*, queued: int = 1, duplicates: int = 0) -> dict:
    return {
        "queued": queued,
        "reactivated": 0,
        "duplicates": duplicates,
        "delivery_ids": [1] if queued else [],
    }


@override_settings(
    BEJERMAN_SYNC_BLOCKED_ALERT_ENABLED=True,
    BEJERMAN_SYNC_BLOCKED_ALERT_AFTER_HOURS=2,
)
class BejermanSyncBlockedAlertTests(SimpleTestCase):
    def _audience_patches(self):
        return (
            patch(
                "service.bejerman_sync_alerts.active_users_for_notification",
                return_value=[
                    {
                        "id": 1,
                        "nombre": "Administración",
                        "email": "admin@nexora.test",
                        "rol": "admin",
                    }
                ],
            ),
            patch(
                "service.bejerman_sync_alerts.notification_email_recipients_for_users",
                return_value=["admin@nexora.test"],
            ),
        )

    @patch("service.bejerman_sync_alerts.timezone.now", return_value=NOW)
    @patch(
        "service.bejerman_sync_alerts._stale_blocked_jobs",
        return_value=[_blocked_job()],
    )
    def test_job_vencido_encola_alerta_para_audiencia_configurada(
        self,
        _jobs,
        _now,
    ):
        audience, recipients = self._audience_patches()
        with (
            audience as active_users,
            recipients,
            patch(
                "service.bejerman_sync_alerts.queue_notification_email",
                return_value=_queue_result(),
            ) as queue_email,
        ):
            result = queue_stale_blocked_bejerman_job_alerts()

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["queued"], 1)
        active_users.assert_called_once_with(
            NOTIFICATION_KEY,
            roles=MANAGEMENT_NOTIFICATION_ROLES,
            require_email=True,
            channel="email",
        )
        kwargs = queue_email.call_args.kwargs
        self.assertEqual(kwargs["notification_key"], NOTIFICATION_KEY)
        self.assertIn(f"{NOTIFICATION_KEY}:1070:", kwargs["dedupe_key"])
        self.assertFalse(kwargs["reactivate_failed"])
        self.assertIn("job #1070", kwargs["subject"])
        self.assertIn("No ha sido posible actualizar", kwargs["body"])

    @patch("service.bejerman_sync_alerts.timezone.now", return_value=NOW)
    @patch(
        "service.bejerman_sync_alerts._stale_blocked_jobs",
        return_value=[
            _blocked_job(updated_at=NOW - timedelta(minutes=30)),
        ],
    )
    @patch("service.bejerman_sync_alerts.queue_notification_email")
    def test_job_reciente_no_alerta(self, queue_email, _jobs, _now):
        result = queue_stale_blocked_bejerman_job_alerts()

        self.assertEqual(result["matched"], 0)
        queue_email.assert_not_called()

    @patch("service.bejerman_sync_alerts.timezone.now", return_value=NOW)
    @patch(
        "service.bejerman_sync_alerts._stale_blocked_jobs",
        return_value=[
            _blocked_job(
                last_error="No se encontró un código de artículo Bejerman.",
            )
        ],
    )
    def test_job_no_transitorio_vencido_tambien_alerta(self, _jobs, _now):
        audience, recipients = self._audience_patches()
        with (
            audience,
            recipients,
            patch(
                "service.bejerman_sync_alerts.queue_notification_email",
                return_value=_queue_result(),
            ) as queue_email,
        ):
            result = queue_stale_blocked_bejerman_job_alerts()

        self.assertEqual(result["matched"], 1)
        queue_email.assert_called_once()

    @patch("service.bejerman_sync_alerts.timezone.now", return_value=NOW)
    @patch(
        "service.bejerman_sync_alerts._stale_blocked_jobs",
        return_value=[
            _blocked_job(job_id=1, sync_type="stock_entry_str"),
            _blocked_job(job_id=2, sync_type="stock_exit_rts"),
        ],
    )
    @patch("service.bejerman_sync_alerts.queue_notification_email")
    def test_jobs_legacy_no_alertan(self, queue_email, _jobs, _now):
        result = queue_stale_blocked_bejerman_job_alerts()

        self.assertEqual(result["matched"], 0)
        queue_email.assert_not_called()

    @patch("service.bejerman_sync_alerts.timezone.now", return_value=NOW)
    @patch(
        "service.bejerman_sync_alerts._stale_blocked_jobs",
        return_value=[_blocked_job()],
    )
    def test_dedupe_por_episodio_es_estable_y_no_reactiva_fallidos(
        self,
        _jobs,
        _now,
    ):
        audience, recipients = self._audience_patches()
        with (
            audience,
            recipients,
            patch(
                "service.bejerman_sync_alerts.queue_notification_email",
                side_effect=[
                    _queue_result(),
                    _queue_result(queued=0, duplicates=1),
                ],
            ) as queue_email,
        ):
            first = queue_stale_blocked_bejerman_job_alerts()
            second = queue_stale_blocked_bejerman_job_alerts()

        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["duplicates"], 1)
        first_kwargs = queue_email.call_args_list[0].kwargs
        second_kwargs = queue_email.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["dedupe_key"], second_kwargs["dedupe_key"])
        self.assertEqual(
            [item.kwargs["reactivate_failed"] for item in queue_email.call_args_list],
            [False, False],
        )

    @patch(
        "service.bejerman_sync_alerts._stale_blocked_jobs",
        side_effect=RuntimeError("tabla no disponible"),
    )
    def test_error_de_consulta_no_se_propaga(self, _jobs):
        result = queue_stale_blocked_bejerman_job_alerts()

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["queued"], 0)
