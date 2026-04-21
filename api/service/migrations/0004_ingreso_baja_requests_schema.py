from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("service", "0003_controlado_sin_defecto"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE IF NOT EXISTS ingreso_baja_requests (
              id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              ingreso_id  INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
              usuario_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              motivo      TEXT NOT NULL,
              created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
              accepted_at TIMESTAMPTZ NULL,
              canceled_at TIMESTAMPTZ NULL
            );
            CREATE INDEX IF NOT EXISTS ix_ibr_ingreso_created
              ON ingreso_baja_requests(ingreso_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS ix_ibr_ingreso_pending
              ON ingreso_baja_requests(ingreso_id)
              WHERE accepted_at IS NULL AND canceled_at IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ibr_ingreso_pending
              ON ingreso_baja_requests(ingreso_id)
              WHERE accepted_at IS NULL AND canceled_at IS NULL;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
