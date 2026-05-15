from django.core.management.base import BaseCommand
from django.db import connection, transaction


INDEX_DEFINITIONS = [
    (
        "ix_audit_change_log_user_ts",
        "CREATE INDEX IF NOT EXISTS ix_audit_change_log_user_ts ON audit.change_log(user_id, ts DESC)",
    ),
    (
        "ix_audit_log_user_ts_method",
        "CREATE INDEX IF NOT EXISTS ix_audit_log_user_ts_method ON audit_log(user_id, ts DESC, method)",
    ),
    (
        "ix_ingreso_events_usuario_ts",
        "CREATE INDEX IF NOT EXISTS ix_ingreso_events_usuario_ts ON ingreso_events(usuario_id, ts DESC)",
    ),
    (
        "ix_repuestos_movimientos_created_by_ts",
        "CREATE INDEX IF NOT EXISTS ix_repuestos_movimientos_created_by_ts ON repuestos_movimientos(created_by, created_at DESC)",
    ),
]


class Command(BaseCommand):
    help = "Aplica índices de soporte para la bitácora de actividad de técnicos"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_metricas_actividad_schema requiere PostgreSQL")

        with transaction.atomic():
            with connection.cursor() as cur:
                for _, sql in INDEX_DEFINITIONS:
                    cur.execute(sql)

                expected_names = [name for name, _ in INDEX_DEFINITIONS]
                cur.execute(
                    """
                    SELECT indexname
                      FROM pg_indexes
                     WHERE indexname = ANY(%s)
                    """,
                    [expected_names],
                )
                found = {row[0] for row in cur.fetchall()}
                missing = [name for name in expected_names if name not in found]
                if missing:
                    raise RuntimeError(
                        f"No se pudieron validar los índices requeridos: {', '.join(missing)}"
                    )

        self.stdout.write("APLICADO OK: índices de actividad de técnicos")
