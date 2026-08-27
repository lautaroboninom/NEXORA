from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Agrega el estado 'anulado' al enum quote_estado"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("SKIP: comando disponible solo para PostgreSQL.")
            return

        # Sin transacción explícita a propósito: los comandos corren en autocommit y
        # PostgreSQL no deja usar un valor de enum recién agregado dentro de la misma
        # transacción que lo agregó. Como los requests corren con ATOMIC_REQUESTS, el
        # valor tiene que quedar commiteado antes de que la app lo escriba.
        with connection.cursor() as cur:
            cur.execute("ALTER TYPE quote_estado ADD VALUE IF NOT EXISTS 'anulado'")

        self.stdout.write(self.style.SUCCESS("Estado 'anulado' disponible en quote_estado."))
