from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from service.email_delivery import dispatch_email_delivery


class EmailDeliveryTests(SimpleTestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_backend_local_entrega_en_la_misma_ejecucion(self):
        callback = Mock()

        dispatch_email_delivery(callback, label="local")

        callback.assert_called_once_with()

    @override_settings(
        EMAIL_BACKEND="service.email_backend.EmailBackend",
        EMAIL_DELIVERY_ASYNC=True,
    )
    @patch("service.email_delivery.Thread")
    def test_backend_remoto_se_desacopla_de_la_respuesta(self, thread_class):
        callback = Mock()

        dispatch_email_delivery(callback, label="remoto")

        thread_class.assert_called_once()
        thread_class.return_value.start.assert_called_once_with()
        callback.assert_not_called()
