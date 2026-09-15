from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection


SCHEMA_LOCK_ID = 7_428_118_037_112_047


# Única lista de esquemas idempotentes que se aplican al preparar la base. Todos los entornos la
# corren llamando a este comando: los compose de dev/prod/internet en el arranque del contenedor.
# No duplicar la lista en ningún otro lado — que estuviera repetida en cuatro archivos distintos,
# los cuatro desincronizados entre sí, es lo que motivó centralizarla acá.
#
# EL ORDEN IMPORTA: los valores de enum van antes de las tablas que los usan.
# Cada entrada es (nombre_del_comando, kwargs).
SCHEMA_COMMANDS: tuple[tuple[str, dict], ...] = (
    ("apply_locations_schema", {}),
    ("apply_ticket_sale_states_schema", {}),
    ("apply_no_se_repara_schema", {}),
    ("apply_delivery_orders_schema", {}),
    # Después de apply_delivery_orders_schema: la FK de actor_user_id necesita users,
    # que esa tanda ya deja en pie.
    ("apply_delivery_remito_jobs_schema", {}),
    ("apply_route_sheet_schema", {}),
    ("apply_bejerman_purchase_entries_schema", {}),
    ("apply_bejerman_user_credentials_schema", {}),
    ("apply_user_permissions_schema", {}),
    ("apply_users_admin_schema", {}),
    ("apply_bejerman_pdf_settings_schema", {}),
    ("apply_bejerman_worker_heartbeat_schema", {}),
    ("apply_bejerman_pdf_requests_schema", {}),
    ("apply_customer_bejerman_schema", {}),
    ("apply_customer_alias_schema", {"seed_examples": True}),
    ("apply_bejerman_sync_schema", {}),
    ("apply_bejerman_ris_schema", {}),
    ("apply_bejerman_article_sync_schema", {}),
    ("apply_bejerman_particular_customer_schema", {}),
    ("apply_bejerman_sale_items_schema", {}),
    ("apply_notifications_schema", {}),
    ("apply_assignment_requests_schema", {}),
    ("apply_models_tipo_equipo_unique_schema", {}),
    # Después de apply_models_tipo_equipo_unique_schema: es la misma corrección una capa más
    # abajo (el humidificador comparte la serie del CPAP) y el backfill de tipo_equipo que
    # hace adentro se apoya en que los modelos ya estén separados por tipo.
    ("apply_devices_ns_tipo_unique_schema", {}),
    ("apply_ingreso_open_conflicts_schema", {}),
    ("apply_derivation_workflow_schema", {}),
    # Estaba excluido como "parche histórico ya aplicado" y resultó no estarlo: sin sus columnas,
    # rechazar un presupuesto devolvía 400. Es idempotente (ADD COLUMN / CREATE INDEX IF NOT EXISTS
    # y la constraint bajo IF NOT EXISTS), así que corre en cada arranque sin costo.
    ("apply_rejected_budget_charge_schema", {}),
    # Agrega el valor 'anulado' al enum quote_estado. Tiene que estar commiteado antes de que la
    # app lo escriba: con ATOMIC_REQUESTS no se puede agregar un valor de enum y usarlo en el
    # mismo request. Es un ALTER TYPE ... ADD VALUE IF NOT EXISTS, así que corre en cada arranque
    # sin costo.
    ("apply_quote_anulacion_schema", {}),
    # Vincula el equipo entregado en un cambio (permuta / parte de pago). Tiene que estar
    # aplicado ANTES de que corra el código: sin `device_cambio_id` la hoja de servicio pide
    # una columna que no existe. Es ADD COLUMN / CREATE INDEX IF NOT EXISTS más un UPDATE
    # con guarda por NULL, así que corre en cada arranque sin costo.
    ("apply_device_swap_schema", {}),
    # Estaba entre los omitidos como "parche histórico ya aplicado" y no lo estaba: en
    # producción faltaban las cuatro tablas del centro de trabajo, así que
    # `send_work_alerts` venía fallando TODOS LOS DÍAS con
    # `relation "work_notification_state" does not exist` y el resumen operativo no
    # salía nunca. Dar un parche por aplicado no lo aplica; correrlo en cada arranque
    # sí, y no cuesta nada porque es CREATE TABLE/INDEX IF NOT EXISTS de punta a punta.
    ("apply_work_upgrade_schema", {}),
    # Suplencias temporales: guarda cómo estaba el suplente antes de cubrir a alguien,
    # que es lo único que permite devolverlo exactamente ahí cuando el titular vuelve.
    ("apply_suplencias_schema", {}),
)


# Comandos `apply_*` que existen en el repo pero deliberadamente NO se corren al preparar la base.
# El test test_prepare_production_schema exige que todo `apply_*.py` esté acá o en SCHEMA_COMMANDS:
# si agregás uno nuevo y no lo declarás, el test falla. Esa es justamente la red que faltaba.
#
# Criterio con el que se armó esta lista: no se subió a SCHEMA_COMMANDS ningún comando que no
# estuviera ya corriendo en alguno de los cuatro lugares que existían antes. Subir uno nuevo pide
# leerlo, confirmar que es idempotente y probarlo; mientras tanto el lado seguro es no ejecutarlo,
# que es exactamente lo que pasaba hasta ahora.
EXCLUDED_SCHEMA_COMMANDS: dict[str, str] = {
    "apply_traced_device_corrections": (
        "No es un applier de esquema: exige --input con un .csv/.xlsx de correcciones aprobadas. "
        "Incluirlo rompería el arranque siempre."
    ),
    "apply_owner_particular": "Parche histórico con backfill de datos (02/2026), ya aplicado.",
    "apply_schema_phase2": "Parche histórico de la fase 2 (10/2025), ya aplicado.",
    "apply_phase3_constraints": "Parche histórico de la fase 3 (02/2026), ya aplicado.",
    "apply_motivo_ingreso_utf8_schema": "Corrección puntual de etiquetas del enum (05/2026), ya aplicada.",
    "apply_historical_corrections_schema": "Parche histórico (05/2026), ya aplicado.",
    "apply_metricas_actividad_schema": "Índices de la bitácora de actividad (05/2026), ya aplicados.",
    "apply_mg_sale_schema": "Parche histórico de venta MG (04/2026), ya aplicado.",
    "apply_repuestos_schema": "Parche histórico de repuestos (04/2026), ya aplicado.",
    "apply_test_schema": "Parche histórico de protocolos de test (04/2026), ya aplicado.",
    "apply_preventivos_schema": "Parche histórico de preventivos (04/2026), ya aplicado.",
    "apply_warranty_schema": "Parche histórico de reglas de garantía (11/2025), ya aplicado.",
    "apply_presupuesto_alerts_schema": "Parche histórico de alertas de presupuesto (01/2026), ya aplicado.",
    "apply_quote_terms_schema": "Parche histórico de condiciones comerciales (02/2026), ya aplicado.",
    "apply_quote_versions_schema": "Parche histórico de versionado de presupuestos (06/2026), ya aplicado.",
    "apply_cotizacion_equipo_schema": "Parche histórico del motivo 'Cotización de equipo' (06/2026), ya aplicado.",
    "apply_revision_tecnica_schema": "Parche histórico del motivo 'Revisión Técnica' (06/2026), ya aplicado.",
}


class Command(BaseCommand):
    help = (
        "Prepara migraciones, esquemas idempotentes y archivos estáticos bajo un único "
        "bloqueo PostgreSQL. Es el único lugar donde vive la lista de esquemas a aplicar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            dest="strict",
            help=(
                "Aborta en el primer esquema que falle en vez de seguir con el resto. "
                "Para correrlo a mano cuando querés que la falla se note con exit code."
            ),
        )

    def handle(self, *args, **options):
        strict = bool(options.get("strict"))
        self.stdout.write("Esperando el bloqueo exclusivo de preparación de esquema...")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [SCHEMA_LOCK_ID])

        fallidos: list[tuple[str, str]] = []
        try:
            self.stdout.write("Preparando la base de datos...")
            # `migrate` sí es fatal: sin las migraciones de Django no hay base sobre la que
            # aplicar nada, y seguir sería aplicar esquemas a ciegas.
            call_command("migrate", interactive=False)
            for name, kwargs in SCHEMA_COMMANDS:
                # Cada esquema va aislado: en el compose de producción este comando está
                # encadenado con `&&` antes de gunicorn, así que si uno solo levanta
                # CommandError la API entera no arranca. Y hay comandos que fallan por
                # datos preexistentes perfectamente legales —`apply_derivation_workflow_schema`
                # aborta si hay dos derivaciones activas para la misma OS, algo que el
                # índice viejo permitía—: dejar la API caída por un índice que no se pudo
                # crear es mucho peor que levantar sin ese índice y avisarlo fuerte.
                try:
                    call_command(name, **kwargs)
                except Exception as exc:  # noqa: BLE001 - se reporta entero más abajo
                    if strict:
                        raise
                    fallidos.append((name, str(exc)))
                    self.stderr.write(self.style.ERROR(f"FALLÓ {name}: {exc}"))
            self._collectstatic()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [SCHEMA_LOCK_ID])

        if fallidos:
            self._report_failures(fallidos)
            return

        self.stdout.write(
            self.style.SUCCESS("Preparación de esquema completada de forma serializada.")
        )

    def _report_failures(self, fallidos: list[tuple[str, str]]) -> None:
        """Deja el fallo a la vista sin tumbar el arranque.

        No se devuelve exit code distinto de cero a propósito: el compose corta la cadena
        antes de gunicorn y el remedio para un esquema que no aplicó nunca es dejar el
        sistema caído. Para que se note igual, el resumen va a stderr y repite los nombres.
        """
        borde = "!" * 78
        self.stderr.write(self.style.ERROR(borde))
        self.stderr.write(
            self.style.ERROR(
                f"ESQUEMAS QUE NO SE APLICARON ({len(fallidos)}). La app arranca igual, "
                "pero estos cambios NO están en la base:"
            )
        )
        for name, error in fallidos:
            self.stderr.write(self.style.ERROR(f"  - {name}: {error}"))
        self.stderr.write(
            self.style.ERROR(
                "Resolvé la causa y volvé a correr `python manage.py prepare_production_schema "
                "--strict` para confirmar que quedan aplicados."
            )
        )
        self.stderr.write(self.style.ERROR(borde))

    def _collectstatic(self) -> None:
        # STATIC_ROOT solo está definido en settings_prod/settings_internet. Con los settings de
        # desarrollo collectstatic aborta con ImproperlyConfigured, y eso volvía inusable este
        # comando fuera de producción: se saltea en vez de fallar.
        if not getattr(settings, "STATIC_ROOT", ""):
            self.stdout.write("STATIC_ROOT sin configurar: se omite collectstatic.")
            return
        call_command("collectstatic", interactive=False)
