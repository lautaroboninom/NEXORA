from __future__ import annotations

import json
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import connection, transaction


def _rows(cur) -> list[dict]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


class Command(BaseCommand):
    help = (
        "Reconcilia bejerman_article_mappings tras agregarle company_key: para cada mapeo existente "
        "(model_id, variante_norm, código) busca en qué empresa(s) Bejerman se usó realmente ese código "
        "(bejerman_sync_jobs + bejerman_ingreso_remitos). Si una sola empresa lo usó, el mapeo queda "
        "asignado a esa empresa. Si lo usaron ambas (el bug de códigos cruzados SEPID/MG BIO), duplica "
        "la fila por empresa y marca ambas como pendientes de revisión manual, sin bloquear los flujos "
        "existentes. Dry-run por defecto; usar --apply para escribir."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios (sin esto solo muestra).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, model_id, variante, variante_norm, article_code, article_description,
                       match_source, source_payload, confirmed_by, confirmed_at
                  FROM bejerman_article_mappings
                 ORDER BY model_id, variante_norm, id
                """
            )
            mappings = _rows(cur)

            cur.execute(
                """
                SELECT d.model_id, j.article_code, j.company_key
                  FROM bejerman_sync_jobs j
                  JOIN devices d ON d.id = j.device_id
                 WHERE NULLIF(TRIM(COALESCE(j.article_code, '')), '') IS NOT NULL
                """
            )
            job_usage = _rows(cur)

            cur.execute(
                """
                SELECT d.model_id,
                       r.request_payload->'equipment'->>'articleCode' AS article_code,
                       COALESCE(NULLIF(r.company_key, ''), 'SEPID') AS company_key
                  FROM bejerman_ingreso_remitos r
                  JOIN ingresos i ON i.id = r.ingreso_id
                  JOIN devices d ON d.id = i.device_id
                 WHERE NULLIF(TRIM(COALESCE(r.request_payload->'equipment'->>'articleCode', '')), '') IS NOT NULL
                """
            )
            remito_usage = _rows(cur)

        usage: dict[tuple[int, str], set[str]] = defaultdict(set)
        for row in job_usage:
            key = (row["model_id"], (row["article_code"] or "").strip().upper())
            usage[key].add((row["company_key"] or "SEPID").strip().upper() or "SEPID")
        for row in remito_usage:
            key = (row["model_id"], (row["article_code"] or "").strip().upper())
            usage[key].add((row["company_key"] or "SEPID").strip().upper() or "SEPID")

        self.stdout.write(self.style.MIGRATE_HEADING("Reconciliación de bejerman_article_mappings por empresa"))

        simple_assign: list[tuple[int, str]] = []  # (mapping_id, company_key)
        collisions: list[dict] = []  # {mapping, companies}

        for mapping in mappings:
            key = (mapping["model_id"], (mapping["article_code"] or "").strip().upper())
            companies = usage.get(key) or set()
            if len(companies) <= 1:
                company = next(iter(companies), None) or "SEPID"
                simple_assign.append((mapping["id"], company))
                self.stdout.write(
                    f"  id={mapping['id']} modelo={mapping['model_id']} variante={mapping['variante']!r} "
                    f"código={mapping['article_code']} -> empresa única: {company}"
                )
            else:
                collisions.append({"mapping": mapping, "companies": sorted(companies)})
                self.stdout.write(
                    self.style.WARNING(
                        f"  id={mapping['id']} modelo={mapping['model_id']} variante={mapping['variante']!r} "
                        f"código={mapping['article_code']} -> USADO EN VARIAS EMPRESAS: {sorted(companies)} "
                        f"(se duplicará una fila por empresa, marcada para revisión manual)"
                    )
                )

        self.stdout.write(
            f"\nResumen: {len(simple_assign)} mapeos con empresa única, "
            f"{len(collisions)} mapeos en colisión entre empresas."
        )

        if not apply_changes:
            self.stdout.write(self.style.WARNING("\nDRY-RUN: no se aplicaron cambios (usar --apply)."))
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                for mapping_id, company in simple_assign:
                    cur.execute(
                        "UPDATE bejerman_article_mappings SET company_key = %s WHERE id = %s",
                        [company, mapping_id],
                    )
                for entry in collisions:
                    mapping = entry["mapping"]
                    companies = entry["companies"]
                    primary, *rest = companies
                    review_note = {
                        "needs_company_review": True,
                        "review_reason": "article_code_shared_across_companies",
                        "shared_with_companies": companies,
                    }
                    source_payload = mapping["source_payload"] or {}
                    if isinstance(source_payload, str):
                        source_payload = json.loads(source_payload)
                    merged_payload = {**source_payload, **review_note}
                    cur.execute(
                        """
                        UPDATE bejerman_article_mappings
                           SET company_key = %s,
                               source_payload = %s::jsonb
                         WHERE id = %s
                        """,
                        [primary, json.dumps(merged_payload, ensure_ascii=False), mapping["id"]],
                    )
                    for company in rest:
                        cur.execute(
                            """
                            INSERT INTO bejerman_article_mappings(
                              model_id, variante, variante_norm, article_code, article_description,
                              match_source, source_payload, company_key
                            ) VALUES (%s,%s,%s,%s,%s,'auto',%s::jsonb,%s)
                            ON CONFLICT (model_id, variante_norm, company_key) DO UPDATE
                               SET source_payload = bejerman_article_mappings.source_payload || EXCLUDED.source_payload
                            """,
                            [
                                mapping["model_id"],
                                mapping["variante"],
                                mapping["variante_norm"],
                                mapping["article_code"],
                                mapping["article_description"],
                                json.dumps(merged_payload, ensure_ascii=False),
                                company,
                            ],
                        )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nAplicado: {len(simple_assign)} mapeos asignados, {len(collisions)} colisiones "
                "resueltas (duplicadas por empresa y marcadas needs_company_review=true)."
            )
        )
        if collisions:
            self.stdout.write(
                self.style.WARNING(
                    "Revisar manualmente (re-confirmar contra el catálogo real de Bejerman de cada empresa) "
                    "los model_id: " + ", ".join(str(entry["mapping"]["model_id"]) for entry in collisions)
                )
            )
