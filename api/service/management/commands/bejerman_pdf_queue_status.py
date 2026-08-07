import json

from django.core.management.base import BaseCommand

from service.bejerman_pdf_requests import pdf_queue_status


class Command(BaseCommand):
    help = "Muestra el estado de la cola de PDF de Bejerman y del worker que la procesa"

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Salida cruda en JSON")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--stuck-only", action="store_true", help="Solo filas vencidas o fallidas")

    def handle(self, *args, **opts):
        status = pdf_queue_status(
            limit=int(opts.get("limit") or 20),
            stuck_only=bool(opts.get("stuck_only")),
        )

        if opts.get("json"):
            self.stdout.write(json.dumps(status, indent=2, ensure_ascii=False, default=str))
            return

        worker = status["worker"]
        alive = worker.get("alive")
        if alive is True:
            self.stdout.write(self.style.SUCCESS(f"Worker VIVO — último latido hace {worker.get('ageSeconds')}s"))
        elif alive is False:
            self.stdout.write(self.style.ERROR("Worker CAÍDO o trabado — la cola no está avanzando"))
        else:
            self.stdout.write(self.style.WARNING("Worker DESCONOCIDO — falta aplicar apply_bejerman_worker_heartbeat_schema"))
        if worker.get("stage"):
            self.stdout.write(
                f"  etapa={worker['stage']} documento={worker.get('currentDocumentKey') or '-'} "
                f"host={worker.get('hostname') or '-'} pid={worker.get('pid') or '-'} ticks={worker.get('tickCount')}"
            )

        queue = status["queue"]
        counts = ", ".join(f"{state}={total}" for state, total in sorted(queue["counts"].items())) or "vacía"
        self.stdout.write(f"\nCola: {counts}")
        self.stdout.write(
            f"  más antigua vencida: {queue.get('oldestDueSeconds')}s · trabadas: {queue['stuckCount']} "
            f"· ciclos máximos: {queue['maxRegenerations']}"
        )

        rows = status["rows"]
        if not rows:
            self.stdout.write("\nSin solicitudes activas.")
            return

        self.stdout.write("\nSolicitudes:")
        for row in rows:
            marker = self.style.ERROR("TRABADA") if row["stuck"] else self.style.SUCCESS("ok     ")
            self.stdout.write(
                f"  {marker} {row['comprobante']:<24} {row['companyKey']:<6} {row['state']:<14} "
                f"gen={row['generationAttemptCount']} ciclos={row['regenerationCount']} edad={row['ageSeconds']}s"
            )
            if row["lastErrorCode"]:
                self.stdout.write(f"           {row['lastErrorCode']}: {row['lastErrorDetail'][:160]}")
            if row["remotePath"]:
                self.stdout.write(f"           esperado en: {row['remotePath']}")
