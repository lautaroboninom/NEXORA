from django.db import migrations


def add_controlado_enum(apps, schema_editor):
    # Postgres: agregar valor al enum ticket_state si no existe
    schema_editor.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_type
            WHERE typname = 'ticket_state'
          ) AND NOT EXISTS (
            SELECT 1 FROM pg_type t
            JOIN pg_enum e ON t.oid = e.enumtypid
            WHERE t.typname = 'ticket_state' AND e.enumlabel = 'controlado_sin_defecto'
          ) THEN
            ALTER TYPE ticket_state ADD VALUE 'controlado_sin_defecto' AFTER 'reparar';
          END IF;
        END$$;
        """
    )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("service", "0002_state_freeze"),
    ]

    operations = [
        migrations.RunPython(add_controlado_enum, reverse_code=migrations.RunPython.noop),
    ]
