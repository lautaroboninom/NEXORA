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
    ("apply_ticket_sale_states_schema", {}),
    ("apply_no_se_repara_schema", {}),
    ("apply_delivery_orders_schema", {}),
    ("apply_route_sheet_schema", {}),
    ("apply_bejerman_purchase_entries_schema", {}),
    ("apply_bejerman_user_credentials_schema", {}),
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
    "apply_work_upgrade_schema": "Parche histórico del centro de trabajo (05/2026), ya aplicado.",
    "apply_metricas_actividad_schema": "Índices de la bitácora de actividad (05/2026), ya aplicados.",
    "apply_mg_sale_schema": "Parche histórico de venta MG (04/2026), ya aplicado.",
    "apply_repuestos_schema": "Parche histórico de repuestos (04/2026), ya aplicado.",
    "apply_test_schema": "Parche histórico de protocolos de test (04/2026), ya aplicado.",
    "apply_preventivos_schema": "Parche histórico de preventivos (04/2026), ya aplicado.",
    "apply_warranty_schema": "Parche histórico de reglas de garantía (11/2025), ya aplicado.",
    "apply_presupuesto_alerts_schema": "Parche histórico de alertas de presupuesto (01/2026), ya aplicado.",
    "apply_quote_terms_schema": "Parche histórico de condiciones comerciales (02/2026), ya aplicado.",
    "apply_quote_versions_schema": "Parche histórico de versionado de presupuestos (06/2026), ya aplicado.",
    "apply_rejected_budget_charge_schema": "Parche histórico de presupuesto rechazado (06/2026), ya aplicado.",
    "apply_cotizacion_equipo_schema": "Parche histórico del motivo 'Cotización de equipo' (06/2026), ya aplicado.",
    "apply_revision_tecnica_schema": "Parche histórico del motivo 'Revisión Técnica' (06/2026), ya aplicado.",
    # Los dos que siguen NO son históricos: crean tablas que la app usa hoy, pero nunca estuvieron
    # en ninguna de las cuatro listas, así que en producción se aplicaron a mano. Se dejan afuera
    # para no cambiar el comportamiento en el mismo paso en que se centraliza la lista.
    # Son los primeros candidatos a subir a SCHEMA_COMMANDS, tras confirmar que son idempotentes.
    "apply_user_permissions_schema": (
        "Crea user_permission_overrides, que la app usa. Nunca estuvo en las listas de arranque; "
        "candidato a subir a SCHEMA_COMMANDS tras verificar idempotencia."
    ),
    "apply_users_admin_schema": (
        "Seguimiento del último acceso de usuarios. Nunca estuvo en las listas de arranque; "
        "candidato a subir a SCHEMA_COMMANDS tras verificar idempotencia."
    ),
}


class Command(BaseCommand):
    help = (
        "Prepara migraciones, esquemas idempotentes y archivos estáticos bajo un único "
        "bloqueo PostgreSQL. Es el único lugar donde vive la lista de esquemas a aplicar."
    )

    def handle(self, *args, **options):
        self.stdout.write("Esperando el bloqueo exclusivo de preparación de esquema...")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [SCHEMA_LOCK_ID])

        try:
            self.stdout.write("Preparando la base de datos...")
            call_command("migrate", interactive=False)
            for name, kwargs in SCHEMA_COMMANDS:
                call_command(name, **kwargs)
            self._collectstatic()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [SCHEMA_LOCK_ID])

        self.stdout.write(
            self.style.SUCCESS("Preparación de esquema completada de forma serializada.")
        )

    def _collectstatic(self) -> None:
        # STATIC_ROOT solo está definido en settings_prod/settings_internet. Con los settings de
        # desarrollo collectstatic aborta con ImproperlyConfigured, y eso volvía inusable este
        # comando fuera de producción: se saltea en vez de fallar.
        if not getattr(settings, "STATIC_ROOT", ""):
            self.stdout.write("STATIC_ROOT sin configurar: se omite collectstatic.")
            return
        call_command("collectstatic", interactive=False)
