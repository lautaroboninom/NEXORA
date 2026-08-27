from decimal import Decimal
from uuid import uuid4

from django.core.management.base import BaseCommand
from django.utils import timezone

from service.delivery_orders import (
    delivery_order_billing_totals,
    delivery_order_billing_url,
    delivery_order_remito_print_url,
    list_delivery_orders,
)
from service.notification_email_outbox import queue_notification_email
from service.notifications import (
    active_users_for_notification,
    emit_notification,
    notification_email_recipients_for_users,
)
from service.views.helpers import _email_append_footer_text


NOTIFICATION_KEY = "billing_pending_summary"


def _money_label(value):
    if value is None:
        return "-"
    amount = Decimal(str(value))
    return f"$ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _order_line(order):
    totals = delivery_order_billing_totals(order)
    return (
        f"- {order.get('remitoNumber') or '-'} | {order.get('orderNumber') or '-'} | "
        f"{order.get('customerName') or '-'} | {_money_label(totals['total'])}"
    )


def _email_body(user, items, total_pending):
    listed_total = sum((delivery_order_billing_totals(order)["total"] for order in items), Decimal("0"))
    lines = [
        f"Hola {user.get('nombre') or ''},",
        "",
        f"Hay {total_pending} remito(s) pendiente(s) de facturación.",
        f"Total estimado de los remitos listados: {_money_label(listed_total)}.",
        "",
        "Remitos:",
    ]
    lines.extend(_order_line(order) for order in items)
    if total_pending > len(items):
        lines.append(f"- +{total_pending - len(items)} remito(s) más")
    lines.extend(["", "Ver pendientes:", delivery_order_billing_url({"id": ""}).split("?")[0]])
    first_print_url = next((delivery_order_remito_print_url(order) for order in items if delivery_order_remito_print_url(order)), "")
    if first_print_url:
        lines.extend(["", f"Primer remito imprimible: {first_print_url}"])
    return _email_append_footer_text("\n".join(lines))


class Command(BaseCommand):
    help = "Envía a Cobranzas el resumen de remitos pendientes de facturación"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **opts):
        dry_run = bool(opts["dry_run"])
        force = bool(opts["force"])
        limit = max(1, min(200, int(opts["limit"] or 50)))

        users = active_users_for_notification(NOTIFICATION_KEY, roles=["cobranzas"], require_email=True)
        if not users:
            self.stdout.write("Sin destinatarios activos de cobranzas con email habilitado.")
            return

        result = list_delivery_orders({"pendingBilling": True, "limit": limit})
        items = result.get("items") or []
        total_pending = int(result.get("total") or len(items))
        if total_pending <= 0:
            self.stdout.write("Sin remitos pendientes de facturación.")
            return

        today = timezone.localdate().isoformat()
        # El correo se sigue deduplicando por día (una copia diaria como
        # máximo); la campana usa una clave estable y refresca el renglón.
        dedupe_key = f"{NOTIFICATION_KEY}:{today}"
        bell_dedupe_key = NOTIFICATION_KEY
        queued_total = 0
        skipped = 0
        subject = f"Remitos pendientes de facturación: {total_pending}"

        for user in users:
            body = _email_body(user, items, total_pending)
            if dry_run:
                self.stdout.write(f"DRY-RUN {user.get('email')}: {subject}")
                queued_total += 1
                continue

            email_dedupe_key = f"{dedupe_key}:force:{uuid4()}" if force else dedupe_key
            recipients = notification_email_recipients_for_users([user])
            queued = queue_notification_email(
                notification_key=NOTIFICATION_KEY,
                dedupe_key=email_dedupe_key,
                recipients=recipients,
                subject=subject,
                body=body,
            )
            accepted = int(queued["queued"] + queued["reactivated"])
            queued_total += accepted
            skipped += int(queued["duplicates"])

            emit_notification(
                NOTIFICATION_KEY,
                user_ids=[user.get("id")],
                title="Resumen de remitos pendientes de facturación",
                body=f"{total_pending} remito(s) pendiente(s).",
                href="/cobranzas/facturacion",
                severity="warning",
                entity_type="billing",
                entity_id=today,
                dedupe_key=bell_dedupe_key,
                payload={"totalPending": total_pending, "listed": len(items)},
                push=True,
                refresh=True,
            )

        self.stdout.write(f"Resumen de cobranzas procesado. encolados={queued_total} omitidos={skipped}")
