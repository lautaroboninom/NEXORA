from django.core.management.base import BaseCommand, CommandError

from service.customer_bejerman_sync import load_bejerman_customer_records, sync_customers_from_bejerman_records


class Command(BaseCommand):
    help = "Sincroniza customers contra ObtenerClientes de Bejerman"

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, default=None, help="ID del usuario Nexora con credenciales Bejerman")
        parser.add_argument("--system-credentials", action="store_true", help="Usa credenciales globales Bejerman explícitamente")
        parser.add_argument("--dry-run", action="store_true", help="Calcula altas/actualizaciones sin escribir")

    def handle(self, *args, **opts):
        user_id = opts.get("user_id")
        use_system = bool(opts.get("system_credentials"))
        if not user_id and not use_system:
            raise CommandError("Indique --user-id o use --system-credentials explícitamente.")

        records = load_bejerman_customer_records(user_id=user_id, allow_system_credentials=use_system)
        summary = sync_customers_from_bejerman_records(records, dry_run=bool(opts.get("dry_run")))
        self.stdout.write(
            "OK clientes Bejerman: "
            f"total={summary['total_bejerman']} "
            f"updated={summary['updated']} "
            f"created={summary['created']} "
            f"skipped={summary['skipped']}"
        )
        for item in summary.get("errors", [])[:10]:
            self.stdout.write(f"omitido {item.get('customer')}: {item.get('reason')}")
