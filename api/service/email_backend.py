import ssl

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend as DjangoSMTPEmailBackend
from django.utils.functional import cached_property


class EmailBackend(DjangoSMTPEmailBackend):
    """
    SMTP backend with optional TLS certificate verification bypass.

    Intended as a temporary workaround for SMTP servers with expired/misconfigured
    certificates. Enable only with EMAIL_INSECURE_SKIP_VERIFY=1.

    En entornos que no son producción, EMAIL_REDIRECT_TO desvía todos los
    destinatarios a esa casilla, para que dev pueda mandar correo real sin que
    las alertas lleguen a clientes o al personal.
    """

    @cached_property
    def ssl_context(self):
        if getattr(settings, "EMAIL_INSECURE_SKIP_VERIFY", False):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if self.ssl_certfile or self.ssl_keyfile:
                try:
                    ctx.load_cert_chain(self.ssl_certfile, self.ssl_keyfile)
                except Exception:
                    pass
            return ctx
        return super().ssl_context

    def send_messages(self, email_messages):
        redirect_to = [
            addr.strip()
            for addr in str(getattr(settings, "EMAIL_REDIRECT_TO", "") or "").split(",")
            if addr.strip()
        ]
        if redirect_to:
            email_messages = [
                self._redirect(message, redirect_to) for message in email_messages
            ]
        return super().send_messages(email_messages)

    @staticmethod
    def _redirect(message, redirect_to):
        original = {
            "To": ", ".join(message.to or []),
            "Cc": ", ".join(getattr(message, "cc", None) or []),
            "Bcc": ", ".join(getattr(message, "bcc", None) or []),
        }
        message.to = list(redirect_to)
        message.cc = []
        message.bcc = []
        for header, value in original.items():
            if value:
                message.extra_headers[f"X-Original-{header}"] = value

        prefix = getattr(settings, "EMAIL_REDIRECT_SUBJECT_PREFIX", "[DEV] ")
        if prefix and not str(message.subject or "").startswith(prefix):
            destinos = original["To"] or "(sin destinatario)"
            message.subject = f"{prefix}{message.subject} -> {destinos}"
        return message
