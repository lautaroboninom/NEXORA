from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica el esquema de carga técnica y sus datos iniciales de tarifas y precios completos."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("La carga técnica requiere PostgreSQL.")
        # Los nuevos valores de enum deben confirmarse antes de usarse.
        with connection.cursor() as cursor:
            cursor.execute("ALTER TYPE quote_estado ADD VALUE IF NOT EXISTS 'sustituido'")
        sql = (Path(__file__).resolve().parents[2] / "sql" / "technical_interventions.sql").read_text(encoding="utf-8")
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql)
        self.stdout.write(self.style.SUCCESS("Esquema de carga técnica aplicado; seed de precios iniciales verificado."))
