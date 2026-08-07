import json
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase, override_settings

from service import service_order_billing
from service.notification_email_outbox import _send_delivery


def _billing_item(**overrides):
    item = {
        "ingresoId": 29439,
        "os": "29439",
        "cliente": "Clínica <Central>",
        "bejermanCustomerCode": "CLI-001",
        "cuit": "30-12345678-9",
        "equipo": "Concentrador de oxígeno AirSep NewLife Elite",
        "numeroSerie": "N5145150",
        "numeroInterno": "MG 2051",
        "motivo": "Reparación",
        "resolucion": "reparado",
        "tecnico": "Técnico de prueba",
        "rss": "RSS R 00001-00029439",
        "conceptCode": "SERVICIO",
        "conceptDescription": "Servicio técnico",
        "href": "/cobranzas/facturacion?serviceOrderId=29439",
        "presupuestoQuoteId": 93,
        "presupuestoVersion": 3,
        "presupuestoEstado": "aprobado",
        "presupuestoMoneda": "ARS",
        "presupuestoNeto": Decimal("100000.00"),
        "presupuestoIva21": Decimal("21000.00"),
        "presupuestoTotal": Decimal("121000.00"),
        "presupuestoFormaPago": "Transferencia a 30 días",
        "presupuestoPlazoEntrega": "5 días hábiles",
        "presupuestoRechazadoCobroNeto": None,
        "presupuestoItems": [
            {
                "tipo": "mano_obra",
                "descripcion": "Mano de obra especializada",
                "cantidad": Decimal("2"),
                "precioUnitario": Decimal("30000"),
                "subtotal": Decimal("60000"),
            },
            {
                "tipo": "repuesto",
                "descripcion": "Kit de reparación",
                "cantidad": Decimal("1"),
                "precioUnitario": Decimal("40000"),
                "subtotal": Decimal("40000"),
            },
        ],
    }
    item.update(overrides)
    return item


class ServiceOrderBillingEmailSummaryTests(SimpleTestCase):
    @patch("service.service_order_billing._table_columns")
    @patch("service.service_order_billing._table_exists", return_value=True)
    def test_consulta_suma_subtotales_visibles_y_filtra_presupuestos_rechazados(
        self,
        _table_exists,
        table_columns,
    ):
        table_columns.return_value = {
            "id",
            "version_num",
            "estado",
            "moneda",
            "subtotal",
            "forma_pago",
            "plazo_entrega_txt",
        }

        _, join = service_order_billing._quote_join(
            {"resolucion", "presupuesto_rechazado_quote_id"}
        )

        self.assertIn("SUM(ROUND(qi.qty * qi.precio_u, 2))", join)
        self.assertIn("CAST(q.estado AS TEXT) = 'rechazado'", join)
        self.assertIn("q.id = t.presupuesto_rechazado_quote_id", join)

    def test_resumen_aprobado_incluye_importes_items_y_contexto_operativo(self):
        item = _billing_item()

        body = service_order_billing._mail_body(item, actor_name="Operador")
        html_body = service_order_billing._mail_html(item, actor_name="Operador")

        self.assertIn("Presupuesto: Versión 3 | Estado: Aprobado", body)
        self.assertIn("Neto: $ 100.000,00", body)
        self.assertIn("IVA 21 %: $ 21.000,00", body)
        self.assertIn("TOTAL A FACTURAR: $ 121.000,00", body)
        self.assertIn("2 x Mano de obra especializada", body)
        self.assertIn(
            "Facturar en Bejerman como Concepto de servicio al 21%. No facturar desde la RSS.",
            body,
        )
        self.assertIn("Clínica &lt;Central&gt;", html_body)
        self.assertIn("$ 121.000,00", html_body)
        self.assertIn("Kit de reparación", html_body)
        self.assertIn("Abrir y facturar en NEXORA", html_body)

    def test_presupuesto_rechazado_usa_el_cobro_autorizado(self):
        item = _billing_item(
            resolucion="presupuesto_rechazado",
            presupuestoEstado="rechazado",
            presupuestoRechazadoCobroNeto=Decimal("5000.00"),
        )

        body = service_order_billing._mail_body(item)
        html_body = service_order_billing._mail_html(item)

        self.assertIn("TOTAL A FACTURAR: $ 6.050,00", body)
        self.assertIn("Cobro autorizado por presupuesto rechazado", body)
        self.assertIn("Total del presupuesto rechazado: $ 121.000,00", body)
        self.assertIn("Total original del presupuesto: $ 121.000,00", html_body)

    def test_presupuesto_rechazado_redondea_el_iva_como_postgresql(self):
        item = _billing_item(
            resolucion="presupuesto_rechazado",
            presupuestoEstado="rechazado",
            presupuestoRechazadoCobroNeto=Decimal("100.50"),
        )

        amounts = service_order_billing._billing_amounts(item)

        self.assertEqual(amounts["billingVat"], Decimal("21.11"))
        self.assertEqual(amounts["billingTotal"], Decimal("121.61"))

    def test_sin_presupuesto_informa_que_faltan_importes(self):
        item = _billing_item(
            presupuestoQuoteId=None,
            presupuestoVersion=None,
            presupuestoEstado="",
            presupuestoNeto=None,
            presupuestoIva21=None,
            presupuestoTotal=None,
            presupuestoItems=[],
        )

        body = service_order_billing._mail_body(item)
        html_body = service_order_billing._mail_html(item)

        self.assertIn("TOTAL A FACTURAR: -", body)
        self.assertIn("no hay un presupuesto asociado con importes disponibles", body)
        self.assertIn("Revisar la OS antes de facturar", html_body)

    @patch("service.service_order_billing.queue_notification_email")
    @patch("service.service_order_billing.notification_email_recipients_for_users")
    @patch("service.service_order_billing.emit_notification")
    @patch("service.service_order_billing.active_users_for_notification")
    @patch("service.service_order_billing.get_service_order_billing_item")
    def test_notificacion_envia_total_y_version_html(
        self,
        get_item,
        active_users,
        emit_notification,
        email_recipients,
        queue_email,
    ):
        get_item.return_value = _billing_item()
        active_users.return_value = [{"id": 7, "email": "cobranzas@example.com"}]
        emit_notification.return_value = 1
        email_recipients.return_value = ["cobranzas@example.com"]
        queue_email.return_value = {"queued": 1, "reactivated": 0, "duplicates": 0}

        result = service_order_billing.notify_service_order_ready_to_bill(29439, actor_name="Operador")

        self.assertEqual(result["emails"], 1)
        queued = queue_email.call_args.kwargs
        self.assertIn("Total $ 121.000,00", queued["subject"])
        self.assertIn("TOTAL A FACTURAR: $ 121.000,00", queued["body"])
        self.assertIn("$ 121.000,00", queued["html_body"])
        emitted = emit_notification.call_args.kwargs
        self.assertIn("Total a facturar: $ 121.000,00", emitted["body"])
        self.assertEqual(emitted["payload"]["presupuesto_version"], 3)
        self.assertEqual(emitted["payload"]["total_a_facturar"], Decimal("121000.00"))


class NotificationEmailHtmlAlternativeTests(SimpleTestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_outbox_envia_texto_html_y_adjunto(self):
        mail.outbox = []
        delivery = {
            "subject": "OS 29439",
            "body": "Resumen en texto",
            "recipient": "cobranzas@example.com",
            "attachment_context": json.dumps(
                {"html_body": "<html><body><strong>Total $ 121.000,00</strong></body></html>"}
            ),
        }

        _send_delivery(delivery, (b"%PDF-1.4", "application/pdf", "OS-29439.pdf"))

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.body, "Resumen en texto")
        self.assertEqual(len(message.alternatives), 1)
        self.assertIn("Total $ 121.000,00", message.alternatives[0][0])
        self.assertEqual(message.alternatives[0][1], "text/html")
        self.assertEqual(message.attachments[0][0], "OS-29439.pdf")
