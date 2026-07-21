from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from service.notification_email_outbox import (
    process_notification_email_outbox,
    queue_notification_email,
)


class NotificationEmailOutboxTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("apply_notifications_schema", verbosity=0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_encola_entrega_y_deduplica_solo_despues_del_envio(self):
        mail.outbox = []

        first = queue_notification_email(
            notification_key="billing_pending_summary",
            dedupe_key="summary:2026-07-14",
            recipients=["cobranzas@example.com"],
            subject="Resumen",
            body="Pendientes",
        )
        second = queue_notification_email(
            notification_key="billing_pending_summary",
            dedupe_key="summary:2026-07-14",
            recipients=["cobranzas@example.com"],
            subject="Resumen",
            body="Pendientes",
        )

        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(mail.outbox), 1)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT status, attempt_count, sent_at FROM notification_email_outbox WHERE id=%s",
                [first["delivery_ids"][0]],
            )
            status, attempts, sent_at = cur.fetchone()
        self.assertEqual(status, "sent")
        self.assertEqual(attempts, 1)
        self.assertIsNotNone(sent_at)

    @override_settings(
        EMAIL_BACKEND="service.email_backend.EmailBackend",
        EMAIL_DELIVERY_ASYNC=True,
    )
    @patch("service.notification_email_outbox._send_delivery")
    def test_falla_smtp_queda_para_reintento_y_luego_confirma(self, send_delivery):
        send_delivery.side_effect = [RuntimeError("smtp caído"), None]
        queued = queue_notification_email(
            notification_key="sales_order_remito_ready",
            dedupe_key="order:1",
            recipients=["cobranzas@example.com"],
            subject="Pendiente",
            body="Orden 1",
        )
        delivery_id = queued["delivery_ids"][0]

        first = process_notification_email_outbox(delivery_ids=[delivery_id], limit=1)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE notification_email_outbox SET next_attempt_at=CURRENT_TIMESTAMP WHERE id=%s",
                [delivery_id],
            )
        second = process_notification_email_outbox(delivery_ids=[delivery_id], limit=1)

        self.assertEqual(first, {"claimed": 1, "sent": 0, "retry": 1, "failed": 0})
        self.assertEqual(second, {"claimed": 1, "sent": 1, "retry": 0, "failed": 0})
        with connection.cursor() as cur:
            cur.execute(
                "SELECT status, attempt_count, last_error FROM notification_email_outbox WHERE id=%s",
                [delivery_id],
            )
            status, attempts, last_error = cur.fetchone()
        self.assertEqual(status, "sent")
        self.assertEqual(attempts, 2)
        self.assertEqual(last_error, "")

    @override_settings(
        EMAIL_BACKEND="service.email_backend.EmailBackend",
        EMAIL_DELIVERY_ASYNC=True,
    )
    @patch("service.notification_email_outbox._send_delivery")
    @patch("service.notification_email_outbox._attachment_for_delivery")
    def test_pdf_no_disponible_se_reintenta_sin_perder_el_correo(self, attachment, send_delivery):
        attachment.side_effect = [RuntimeError("PDF pendiente"), (b"%PDF-1.4", "application/pdf", "remito.pdf")]
        queued = queue_notification_email(
            notification_key="remito_pdf_rss",
            dedupe_key="rss:4800",
            recipients=["cobranzas@example.com"],
            subject="Remito",
            body="Adjunto",
            attachment_kind="delivery_remito_group",
            attachment_reference="brg-test",
        )
        delivery_id = queued["delivery_ids"][0]

        first = process_notification_email_outbox(delivery_ids=[delivery_id], limit=1)
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE notification_email_outbox SET next_attempt_at=CURRENT_TIMESTAMP WHERE id=%s",
                [delivery_id],
            )
        second = process_notification_email_outbox(delivery_ids=[delivery_id], limit=1)

        self.assertEqual(first["retry"], 1)
        self.assertEqual(second["sent"], 1)
        send_delivery.assert_called_once()
        args = send_delivery.call_args.args
        self.assertEqual(args[1][0], b"%PDF-1.4")

    @override_settings(
        EMAIL_BACKEND="service.email_backend.EmailBackend",
        EMAIL_DELIVERY_ASYNC=True,
    )
    @patch("service.notification_email_outbox._send_delivery", side_effect=RuntimeError("sin salida"))
    def test_entrega_agotada_puede_reactivarse(self, _send_delivery):
        first = queue_notification_email(
            notification_key="service_order_ready_to_bill",
            dedupe_key="os:100",
            recipients=["cobranzas@example.com"],
            subject="OS",
            body="OS 100",
            max_attempts=1,
        )
        delivery_id = first["delivery_ids"][0]
        process_notification_email_outbox(delivery_ids=[delivery_id], limit=1)

        second = queue_notification_email(
            notification_key="service_order_ready_to_bill",
            dedupe_key="os:100",
            recipients=["cobranzas@example.com"],
            subject="OS",
            body="OS 100",
            max_attempts=2,
        )

        self.assertEqual(second["reactivated"], 1)
        with connection.cursor() as cur:
            cur.execute(
                "SELECT status, attempt_count FROM notification_email_outbox WHERE id=%s",
                [delivery_id],
            )
            self.assertEqual(cur.fetchone(), ("retry", 0))


class RecoverCobranzasEmailNotificationsTests(SimpleTestCase):
    @patch("service.management.commands.recover_cobranzas_email_notifications.Command._remito_group")
    @patch("service.management.commands.recover_cobranzas_email_notifications.Command._service_order_ids")
    @patch("service.management.commands.recover_cobranzas_email_notifications.Command._delivery_order_ids")
    def test_dry_run_no_encola_correos(self, delivery_ids, service_ids, remito_group):
        delivery_ids.return_value = ["do-1"]
        service_ids.return_value = [100]
        remito_group.return_value = {"id": "brg-1"}
        out = StringIO()

        call_command(
            "recover_cobranzas_email_notifications",
            remito_group=["brg-1"],
            stdout=out,
        )

        self.assertIn("entregas=1 OS=1 PDFs=1", out.getvalue())
        self.assertIn("DRY-RUN", out.getvalue())
        remito_group.assert_not_called()

    @patch("service.management.commands.recover_cobranzas_email_notifications.notify_bejerman_remito_pdf_issued")
    @patch("service.management.commands.recover_cobranzas_email_notifications.notify_service_order_ready_to_bill")
    @patch("service.management.commands.recover_cobranzas_email_notifications._send_delivery_order_billing_email")
    @patch("service.management.commands.recover_cobranzas_email_notifications.get_delivery_order")
    @patch("service.management.commands.recover_cobranzas_email_notifications.active_users_for_notification")
    @patch("service.management.commands.recover_cobranzas_email_notifications.Command._remito_group")
    @patch("service.management.commands.recover_cobranzas_email_notifications.Command._service_order_ids")
    @patch("service.management.commands.recover_cobranzas_email_notifications.Command._delivery_order_ids")
    def test_apply_encola_cada_flujo_detectado(
        self,
        delivery_ids,
        service_ids,
        remito_group,
        active_users,
        get_order,
        send_delivery,
        notify_service,
        notify_pdf,
    ):
        delivery_ids.return_value = ["do-1"]
        service_ids.return_value = [100]
        remito_group.return_value = {
            "id": "brg-1",
            "remito_number": "RSS R 00004-00000001",
            "comprobante_tipo": "RSS",
            "company_key": "MGBIO",
            "customer_name": "Cliente",
            "created_by_user_id": 7,
        }
        active_users.return_value = [{"id": 1, "email": "cobranzas@example.com"}]
        get_order.return_value = {"id": "do-1"}
        send_delivery.return_value = 1
        notify_service.return_value = {"emails": 1}
        notify_pdf.return_value = {"emails": 1}
        out = StringIO()

        call_command(
            "recover_cobranzas_email_notifications",
            remito_group=["brg-1"],
            apply=True,
            stdout=out,
        )

        self.assertIn("Correos encolados=3", out.getvalue())
        send_delivery.assert_called_once()
        notify_service.assert_called_once_with(100, actor_name="Recuperación automática")
        notify_pdf.assert_called_once()
