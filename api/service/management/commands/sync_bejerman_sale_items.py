from django.core.management.base import BaseCommand

from service.bejerman_sales import sync_bejerman_sale_items
from service.bejerman_sdk import BejermanSDKClient


class Command(BaseCommand):
    help = "Sincroniza renglones de venta Bejerman con partida/serie hacia la caché local"

    def add_arguments(self, parser):
        parser.add_argument("--company-key", default="SEPID")
        parser.add_argument("--date-from", default="")
        parser.add_argument("--date-to", default="")
        parser.add_argument("--days", type=int, default=365)
        parser.add_argument("--types", default="")
        parser.add_argument("--max-comprobantes", type=int, default=500)
        parser.add_argument("--user-id", type=int, default=None)
        parser.add_argument("--allow-system-credentials", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        types = [item.strip().upper() for item in (opts.get("types") or "").split(",") if item.strip()] or None
        client = BejermanSDKClient(
            company_key=opts.get("company_key") or "SEPID",
            actor_user_id=opts.get("user_id"),
            allow_system_credentials=bool(opts.get("allow_system_credentials")),
        )
        summary = sync_bejerman_sale_items(
            client,
            date_from=opts.get("date_from") or None,
            date_to=opts.get("date_to") or None,
            days=opts.get("days"),
            types=types,
            max_comprobantes=opts.get("max_comprobantes"),
            dry_run=bool(opts.get("dry_run")),
        )
        self.stdout.write(
            "OK ventas Bejerman: "
            f"headers={summary['headers']} details={summary['details']} "
            f"items={summary['items']} upserted={summary['upserted']} "
            f"errors={len(summary['errors'])} dry_run={summary['dryRun']}"
        )
        if summary["errors"]:
            for error in summary["errors"][:10]:
                self.stderr.write(f"{error.get('comprobanteId')}: {error.get('error')}")
