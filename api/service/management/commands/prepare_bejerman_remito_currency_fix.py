from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from service.bejerman_remito_currency_fix import (
    build_bejerman_currency_fix_sql,
    clean_text,
    clean_upper,
    collect_local_usd_remito_targets,
    default_out_dir,
    default_test_database,
    target_from_row,
    timestamp_slug,
    write_reports,
)


class Command(BaseCommand):
    help = "Prepara la corrección de moneda para remitos Bejerman emitidos en USD que deben facturarse en pesos."

    def add_arguments(self, parser):
        parser.add_argument("--company-key", default="SEPID", help="Empresa NEXORA/Bejerman: SEPID o MGBIO.")
        parser.add_argument("--from-date", default="", help="Fecha mínima de generación local, formato YYYY-MM-DD.")
        parser.add_argument("--to-date", default="", help="Fecha máxima de generación local, formato YYYY-MM-DD.")
        parser.add_argument("--limit", type=int, default=0, help="Límite de candidatos locales.")
        parser.add_argument(
            "--include-all-delivery-types",
            action="store_true",
            help="Incluye otros tipos de orden además de venta. Por defecto solo venta.",
        )
        parser.add_argument(
            "--skip-local-scan",
            action="store_true",
            help="No consulta NEXORA; usa solo los remitos pasados por --remito.",
        )
        parser.add_argument(
            "--remito",
            action="append",
            default=[],
            help="Remito puntual para agregar al SQL. Ejemplo: RT R 00004-00000059.",
        )
        parser.add_argument("--customer-code", default="", help="Código Bejerman del cliente para remitos pasados por --remito.")
        parser.add_argument("--customer-name", default="", help="Nombre de cliente para remitos pasados por --remito.")
        parser.add_argument("--order-numbers", default="", help="Órdenes asociadas para remitos pasados por --remito.")
        parser.add_argument("--default-type", default="RT", help="Tipo default si --remito no lo trae explícito.")
        parser.add_argument("--default-point", default="", help="Punto de venta default si --remito no lo trae explícito.")
        parser.add_argument(
            "--bejerman-database",
            default="",
            help="Base SQL Server objetivo. Default seguro: SBDPSEP/SBDPMGBI, no producción.",
        )
        parser.add_argument(
            "--sql-apply",
            action="store_true",
            help="Genera el SQL con @Apply=1. Usar solo después de revisar el dry-run.",
        )
        parser.add_argument("--out-dir", default="", help="Directorio de salida. Default: informes/bejerman_remitos_usd_moneda_<fecha>.")

    def handle(self, *args, **options):
        company_key = clean_upper(options.get("company_key")) or "SEPID"
        targets = []

        if not options.get("skip_local_scan"):
            try:
                targets.extend(
                    collect_local_usd_remito_targets(
                        company_key=company_key,
                        from_date=clean_text(options.get("from_date")),
                        to_date=clean_text(options.get("to_date")),
                        limit=int(options.get("limit") or 0),
                        include_all_delivery_types=bool(options.get("include_all_delivery_types")),
                    )
                )
            except DatabaseError as exc:
                raise CommandError(f"No se pudo consultar NEXORA para detectar remitos candidatos: {exc}") from exc

        manual_remitos = [clean_text(item) for item in options.get("remito") or [] if clean_text(item)]
        if manual_remitos:
            customer_code = clean_text(options.get("customer_code"))
            if not customer_code:
                raise CommandError("--customer-code es obligatorio cuando se usa --remito")
            for index, remito in enumerate(manual_remitos, start=1):
                targets.append(
                    target_from_row(
                        {
                            "group_id": f"manual-{index}",
                            "company_key": company_key,
                            "comprobante_tipo": clean_upper(options.get("default_type")) or "RT",
                            "comprobante_letra": "R",
                            "comprobante_pto_venta": clean_text(options.get("default_point")),
                            "remito_number": remito,
                            "customer_code": customer_code,
                            "customer_name": clean_text(options.get("customer_name")),
                            "order_numbers": clean_text(options.get("order_numbers")) or "manual",
                            "generated_at": "",
                            "commercial_exchange_rate": "",
                        }
                    )
                )

        unique = {}
        for target in targets:
            unique[(target.company_key, target.tipo, target.letra, target.punto, target.numero, target.customer_code)] = target
        targets = list(unique.values())

        database = clean_text(options.get("bejerman_database")) or default_test_database(company_key)
        backup_name = f"NEXORA_BKP_REMITOS_USD_MONEDA_{timestamp_slug()}"
        sql_script = build_bejerman_currency_fix_sql(
            targets,
            database=database,
            apply=bool(options.get("sql_apply")),
            backup_name=backup_name,
        )
        out_dir = Path(options.get("out_dir") or default_out_dir())
        out_path = write_reports(
            out_dir,
            targets=targets,
            sql_script=sql_script,
            metadata={
                "company_key": company_key,
                "bejerman_database": database,
                "sql_apply": bool(options.get("sql_apply")),
                "backup_name": backup_name,
                "from_date": clean_text(options.get("from_date")),
                "to_date": clean_text(options.get("to_date")),
                "local_scan": not bool(options.get("skip_local_scan")),
            },
        )

        self.stdout.write(f"Reportes: {out_path}")
        self.stdout.write(f"Candidatos: {len(targets)}")
        self.stdout.write(f"Base Bejerman del SQL: {database}")
        self.stdout.write(f"SQL: {out_path / 'bejerman_remitos_usd_moneda_fix.sql'}")
        if options.get("sql_apply"):
            self.stdout.write("SQL generado con @Apply=1. Revisar backup y coincidencias antes de ejecutarlo.")
        else:
            self.stdout.write("SQL dry-run generado con @Apply=0. No corrige datos hasta cambiar/aplicar explícitamente.")
