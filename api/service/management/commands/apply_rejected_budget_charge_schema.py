from django.core.management.base import BaseCommand
from django.db import connection, transaction

from service.rejected_budget import REJECTED_BUDGET_CHARGE_DDL


class Command(BaseCommand):
    help = "Agrega cobro neto y vínculo de presupuesto rechazado en ingresos"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("SKIP: comando disponible solo para PostgreSQL.")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                for statement in REJECTED_BUDGET_CHARGE_DDL:
                    cur.execute(statement)

        self.stdout.write(
            "APLICADO OK: ingresos (presupuesto_rechazado_cobro_neto, presupuesto_rechazado_quote_id)."
        )
