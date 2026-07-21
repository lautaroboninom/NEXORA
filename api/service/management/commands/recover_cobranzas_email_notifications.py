from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from service.delivery_orders import _send_delivery_order_billing_email, get_delivery_order
from service.notifications import active_users_for_notification
from service.remito_pdf_notifications import notify_bejerman_remito_pdf_issued
from service.service_order_billing import notify_service_order_ready_to_bill


def _aware_datetime(raw_value: str | None, *, fallback) -> Any:
    if not raw_value:
        return fallback
    parsed = parse_datetime(str(raw_value).strip())
    if parsed is None:
        raise CommandError("Las fechas deben usar formato ISO 8601.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


class Command(BaseCommand):
    help = "Recupera avisos de Cobranzas que no salieron por una falla de correo"

    def add_arguments(self, parser):
        parser.add_argument("--since")
        parser.add_argument("--until")
        parser.add_argument("--remito-group", action="append", default=[])
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        now = timezone.now()
        since = _aware_datetime(opts.get("since"), fallback=now - timedelta(days=2))
        until = _aware_datetime(opts.get("until"), fallback=now)
        if since >= until:
            raise CommandError("--since debe ser anterior a --until.")

        delivery_order_ids = self._delivery_order_ids(since, until)
        service_order_ids = self._service_order_ids(since, until)
        remito_group_ids = list(dict.fromkeys(str(value or "").strip() for value in opts["remito_group"] if str(value or "").strip()))

        self.stdout.write(
            "Recuperación detectada: "
            f"entregas={len(delivery_order_ids)} OS={len(service_order_ids)} PDFs={len(remito_group_ids)}."
        )
        if delivery_order_ids:
            self.stdout.write("Entregas: " + ", ".join(delivery_order_ids))
        if service_order_ids:
            self.stdout.write("OS: " + ", ".join(str(value) for value in service_order_ids))
        if remito_group_ids:
            self.stdout.write("Grupos PDF: " + ", ".join(remito_group_ids))
        if not opts["apply"]:
            self.stdout.write("DRY-RUN: use --apply para encolar estos correos.")
            return

        queued = 0
        delivery_users = active_users_for_notification(
            "sales_order_remito_ready",
            roles=["cobranzas"],
            channel="email",
            require_email=True,
        )
        for order_id in delivery_order_ids:
            order = get_delivery_order(order_id, include_events=False)
            queued += _send_delivery_order_billing_email(order, delivery_users)

        for ingreso_id in service_order_ids:
            result = notify_service_order_ready_to_bill(ingreso_id, actor_name="Recuperación automática")
            queued += int(result.get("emails") or 0)

        for group_id in remito_group_ids:
            row = self._remito_group(group_id)
            if not row:
                raise CommandError(f"No existe el grupo de remito {group_id}.")
            result = notify_bejerman_remito_pdf_issued(
                remito_number=row.get("remito_number") or "",
                document_type=row.get("comprobante_tipo") or "",
                company_key=row.get("company_key") or "",
                customer_name=row.get("customer_name") or "",
                source="Recuperación de correo",
                details=["Aviso recuperado después de una interrupción del servicio de correo."],
                actor_user_id=row.get("created_by_user_id"),
                attachment_kind="delivery_remito_group",
                attachment_reference=group_id,
            )
            queued += int(result.get("emails") or 0)

        self.stdout.write(f"Recuperación aplicada. Correos encolados={queued}.")

    @staticmethod
    def _delivery_order_ids(since, until) -> list[str]:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT e.order_id
                  FROM delivery_order_events e
                  JOIN delivery_orders o ON o.id=e.order_id
                 WHERE e.created_at >= %s
                   AND e.created_at < %s
                   AND e.event_type='notification_sent'
                   AND e.metadata->>'kind'='remito_ready'
                   AND COALESCE((e.metadata->>'emailSent')::integer, 0)=0
                   AND o.status='entregado_pendiente_facturacion'
                   AND NULLIF(TRIM(COALESCE(o.invoice_number, '')), '') IS NULL
                 ORDER BY e.order_id
                """,
                [since, until],
            )
            return [str(row[0]) for row in cur.fetchall()]

    @staticmethod
    def _service_order_ids(since, until) -> list[int]:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT CAST(n.entity_id AS integer)
                  FROM notifications n
                  JOIN ingresos i ON CAST(i.id AS text)=n.entity_id
                 WHERE n.created_at >= %s
                   AND n.created_at < %s
                   AND n.notification_key='service_order_ready_to_bill'
                   AND CAST(i.estado AS text) IN ('liberado','entregado')
                   AND NULLIF(TRIM(COALESCE(i.factura_numero, '')), '') IS NULL
                 ORDER BY CAST(n.entity_id AS integer)
                """,
                [since, until],
            )
            return [int(row[0]) for row in cur.fetchall()]

    @staticmethod
    def _remito_group(group_id: str) -> dict[str, Any] | None:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, remito_number, comprobante_tipo, company_key,
                       customer_name, created_by_user_id
                  FROM bejerman_remito_groups
                 WHERE id=%s
                """,
                [group_id],
            )
            row = cur.fetchone()
            if not row:
                return None
            columns = [column[0] for column in cur.description]
            return dict(zip(columns, row))
