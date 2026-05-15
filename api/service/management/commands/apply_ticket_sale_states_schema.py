from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Agrega estados de venta al enum ticket_state"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("OMITIDO: ticket_state es específico de PostgreSQL")
            return

        with connection.cursor() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ticket_state') THEN
                    IF NOT EXISTS (
                      SELECT 1
                        FROM pg_type t
                        JOIN pg_enum e ON e.enumtypid = t.oid
                       WHERE t.typname = 'ticket_state'
                         AND e.enumlabel = 'vendido_pendiente_entrega'
                    ) THEN
                      ALTER TYPE ticket_state ADD VALUE 'vendido_pendiente_entrega' AFTER 'alquilado';
                    END IF;

                    IF NOT EXISTS (
                      SELECT 1
                        FROM pg_type t
                        JOIN pg_enum e ON e.enumtypid = t.oid
                       WHERE t.typname = 'ticket_state'
                         AND e.enumlabel = 'vendido_entregado'
                    ) THEN
                      ALTER TYPE ticket_state ADD VALUE 'vendido_entregado' AFTER 'vendido_pendiente_entrega';
                    END IF;
                  END IF;
                END $$;
                """
            )

        self.stdout.write("APLICADO OK: estados de venta de ticket")
