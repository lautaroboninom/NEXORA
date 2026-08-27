"""Rehace el trabajo de Bejerman que quedó a mitad de camino.

Junta en un solo lugar los tres tipos de cosa que se quedan trabadas y que hasta
ahora había que ir a destrabar de a una, cada una por su lado:

- **PDFs fallidos**: los que se dieron por perdidos antes de que existieran el
  sondeo por nombre alternativo y el PDF propio de NEXORA. El comprobante está
  emitido; lo único que faltó fue el papel.
- **Remitos en revisión**: los que quedaron `failed_review` porque la consulta a
  Bejerman volvió ambigua. Con el desempate por sello de grupo, la mayoría se
  reconcilia sola al volver a verificar.
- **Jobs de sincronización zombis**: `running` sin nadie atrás, `pending` con los
  intentos agotados, y los bloqueados por credenciales que ya están cargadas.

Por defecto no toca nada: hay que pedir `--apply` explícitamente.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection

from service.bejerman_pdf_requests import requeue_pdf_request
from service.bejerman_sync import (
    recover_stuck_sync_jobs,
    reopen_jobs_waiting_for_credentials,
)


def _rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        columns = [column[0] for column in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


class Command(BaseCommand):
    help = "Rehace PDFs fallidos, remitos en revisión y jobs de sincronización trabados."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios (por defecto sólo informa).")
        parser.add_argument("--pdfs", action="store_true", help="Sólo los PDFs fallidos.")
        parser.add_argument("--reviews", action="store_true", help="Sólo los remitos en revisión.")
        parser.add_argument("--jobs", action="store_true", help="Sólo los jobs de sincronización.")
        parser.add_argument(
            "--skip-customer",
            action="append",
            default=[],
            metavar="CODIGO",
            help="Código de cliente a excluir (repetible). Ej.: --skip-customer TMD",
        )
        parser.add_argument("--limit", type=int, default=50, help="Tope de elementos por categoría.")

    def handle(self, *args, **options):
        apply_changes = bool(options["apply"])
        limit = max(1, int(options["limit"] or 1))
        skip = {str(code).strip().upper() for code in options["skip_customer"] if str(code).strip()}
        only = {name for name in ("pdfs", "reviews", "jobs") if options.get(name)}
        selected = only or {"pdfs", "reviews", "jobs"}

        if not apply_changes:
            self.stdout.write(self.style.WARNING("Modo simulación: no se modifica nada. Use --apply para ejecutar."))
        if skip:
            self.stdout.write(f"Clientes excluidos: {', '.join(sorted(skip))}")

        report: dict[str, Any] = {}
        if "pdfs" in selected:
            report["pdfs"] = self._recover_pdfs(apply_changes, limit, skip)
        if "reviews" in selected:
            report["reviews"] = self._list_reviews(limit, skip)
        if "jobs" in selected:
            report["jobs"] = self._recover_jobs(apply_changes, skip)

        self.stdout.write("")
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    # -- PDFs ---------------------------------------------------------------

    def _recover_pdfs(self, apply_changes: bool, limit: int, skip: set[str]) -> dict[str, Any]:
        rows = _rows(
            """
            SELECT document_key, company_key, tipo, pto_venta, numero, customer_code,
                   regeneration_count, last_error_code
              FROM bejerman_pdf_requests
             WHERE state = 'failed'
             ORDER BY created_at DESC
             LIMIT %s
            """,
            [limit],
        )
        outcomes: list[dict[str, Any]] = []
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nPDFs fallidos: {len(rows)}"))
        for row in rows:
            key = row["document_key"]
            customer = str(row.get("customer_code") or "").upper()
            if customer in skip:
                outcomes.append({"documentKey": key, "outcome": "omitido", "customerCode": customer})
                self.stdout.write(f"  - {key}: omitido (cliente {customer})")
                continue
            if not apply_changes:
                outcomes.append({"documentKey": key, "outcome": "pendiente"})
                self.stdout.write(f"  - {key}: se reencolaría ({row.get('last_error_code') or 'sin código'})")
                continue
            try:
                # `force` porque el contador de regeneraciones quedó gastado en el
                # ciclo viejo, cuando el sondeo todavía no miraba los otros nombres.
                result = requeue_pdf_request(key, reason="recover_stuck_bejerman_work", force=True)
            except Exception as exc:  # el resto de la cola no se cae por uno
                outcomes.append({"documentKey": key, "outcome": "error", "error": str(exc)})
                self.stdout.write(self.style.ERROR(f"  - {key}: error {exc}"))
                continue
            outcomes.append({"documentKey": key, **result})
            label = result.get("outcome")
            style = self.style.SUCCESS if label == "ready" else self.style.NOTICE
            self.stdout.write(style(f"  - {key}: {label}"))
        return {"total": len(rows), "items": outcomes}

    # -- Remitos en revisión -------------------------------------------------

    def _list_reviews(self, limit: int, skip: set[str]) -> dict[str, Any]:
        rows = _rows(
            """
            SELECT id, comprobante_tipo, customer_code, customer_name, attempt_count,
                   response_summary->>'reviewState' AS review_state,
                   response_summary->>'diagnosticCode' AS diagnostic_code,
                   left(COALESCE(last_error, ''), 120) AS last_error
              FROM bejerman_remito_groups
             WHERE status = 'failed_review'
             ORDER BY created_at DESC
             LIMIT %s
            """,
            [limit],
        )
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nRemitos en revisión: {len(rows)}"))
        items = []
        for row in rows:
            customer = str(row.get("customer_code") or "").upper()
            state = row.get("review_state") or "requires_verification"
            excluded = customer in skip
            items.append(
                {
                    "groupId": row["id"],
                    "customerCode": customer,
                    "reviewState": state,
                    "diagnosticCode": row.get("diagnostic_code"),
                    "excluded": excluded,
                }
            )
            marca = " (omitido)" if excluded else ""
            self.stdout.write(f"  - {row['id']} {customer} {row.get('comprobante_tipo')}: {state}{marca}")
        # La verificación habla con Bejerman y mueve stock: no corre sola desde acá.
        # Con el desempate por sello, alcanza con apretar "Verificar en Bejerman" en
        # el panel; los que sigan sin resolverse ahora tienen "Resolver revisión".
        self.stdout.write(
            "  → Verificar desde el panel de Bejerman. Los que queden ambiguos se cierran con «Resolver revisión»."
        )
        return {"total": len(rows), "items": items}

    # -- Jobs de sincronización ---------------------------------------------

    def _recover_jobs(self, apply_changes: bool, skip: set[str]) -> dict[str, Any]:
        rows = _rows(
            """
            SELECT j.id, j.sync_type, j.status, j.attempts, j.ingreso_id, j.article_code,
                   COALESCE(cu.razon_social, '') AS cliente,
                   left(COALESCE(j.last_error, ''), 90) AS last_error
              FROM bejerman_sync_jobs j
              LEFT JOIN ingresos i ON i.id = j.ingreso_id
              LEFT JOIN devices d ON d.id = i.device_id
              LEFT JOIN customers cu ON cu.id = d.customer_id
             WHERE j.status <> 'succeeded'
             ORDER BY j.id
            """
        )
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nJobs no terminados: {len(rows)}"))
        for row in rows:
            self.stdout.write(
                f"  - #{row['id']} {row['sync_type']} [{row['status']}] "
                f"OS-{row.get('ingreso_id')} {row.get('cliente') or '-'}: {row.get('last_error') or 'sin error'}"
            )
        if not apply_changes:
            return {"total": len(rows), "applied": False}
        stuck = recover_stuck_sync_jobs()
        credentials = reopen_jobs_waiting_for_credentials()
        self.stdout.write(self.style.SUCCESS(f"  → reanudados/reclasificados: {stuck}, por credenciales: {credentials}"))
        return {"total": len(rows), "applied": True, **stuck, "reopened_credentials": credentials}
