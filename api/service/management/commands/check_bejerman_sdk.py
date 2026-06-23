from django.core.management.base import BaseCommand, CommandError

from service.bejerman_companies import list_ingress_companies, require_company
from service.bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkError,
    build_article_filters,
    build_articles_result,
    resolve_customer_document_fields,
    validate_sdk_config,
)


class Command(BaseCommand):
    help = "Valida configuración y conectividad del SDK Bejerman interno sin emitir comprobantes"

    def add_arguments(self, parser):
        parser.add_argument("--company-key", default="SEPID")
        parser.add_argument("--article-search", default="")
        parser.add_argument("--customer-code", default="")
        parser.add_argument("--skip-live", action="store_true")

    def handle(self, *args, **options):
        try:
            validate_sdk_config()
            company = require_company(options["company_key"] or "SEPID")
        except (ValueError, BejermanSdkError) as exc:
            raise CommandError(str(exc)) from exc

        allowed = ", ".join(f"{item.key}->{item.bejerman_company}" for item in list_ingress_companies())
        self.stdout.write(f"SDK Bejerman configurado. Empresas permitidas: {allowed}")
        self.stdout.write(f"Empresa de prueba actual: {company.key}->{company.bejerman_company}")

        if options["skip_live"]:
            self.stdout.write("OK: validación live omitida")
            return

        try:
            client = BejermanSDKClient(company_key=company.key, allow_system_credentials=True)
            article_search = (options["article_search"] or "").strip()
            articles = build_articles_result(
                client.list_articulos(build_article_filters(article_search), 1),
                {"search": article_search, "limit": 1},
            )
            self.stdout.write(f"Artículos OK: {len(articles.get('items') or [])} resultado(s)")

            customer_code = (options["customer_code"] or "").strip()
            if customer_code:
                fields = resolve_customer_document_fields(client, customer_code)
                sit_iva = fields.get("Cliente_SitIVA") or ""
                self.stdout.write(f"Cliente OK: {customer_code} SitIVA={sit_iva}")
            else:
                client.list_clientes()
                self.stdout.write("Clientes OK: ObtenerClientes respondió")
        except BejermanSdkError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("OK: SDK Bejerman accesible desde NEXORA")
